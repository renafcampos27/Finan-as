"""Identity lookups must follow SQLite visibility, including caller-owned rollback."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from database.engine import FinanceDB
from database.repo import Repo
from services.account_identity import account_identity_key
from services.category_taxonomy import category_identity_key


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Repo]:
    monkeypatch.setenv("FINANCAS_PESSOAIS_DEMO_DATA", "0")
    instance = Repo(FinanceDB(tmp_path / "identity-rollback.sqlite3"))
    try:
        yield instance
    finally:
        instance.db.conn.close()


@pytest.fixture(params=["account", "category"])
def entity(request: pytest.FixtureRequest) -> str:
    return str(request.param)


def resolve(
    repo: Repo, entity: str, name: str, *, create: bool = False
) -> tuple[str, str]:
    if entity == "account":
        return repo.account_identity(name, create=create)
    return repo.category_identity(name, create=create, kind="expense")


def table_for(entity: str) -> str:
    return "financial_accounts" if entity == "account" else "categories"


@pytest.mark.parametrize("boundary", ["rollback", "savepoint", "released_savepoint"])
def test_created_identity_is_not_reused_after_caller_rollback(
    repo: Repo, entity: str, boundary: str
) -> None:
    connection = repo.db.conn
    name = "Rollback identity"
    connection.execute("BEGIN")
    connection.execute(
        "INSERT INTO settings(key,value) VALUES('caller-pending','kept')"
    )
    if boundary != "rollback":
        connection.execute("SAVEPOINT identity_scope")

    original = resolve(repo, entity, name, create=True)
    # Exercise an actual cache hit in the old implementation for both types.
    assert resolve(repo, entity, name, create=True) == original
    assert original[0]
    changes_before_rollback = connection.total_changes

    if boundary == "savepoint":
        connection.execute("ROLLBACK TO SAVEPOINT identity_scope")
        assert connection.in_transaction
        assert (
            connection.execute(
                "SELECT value FROM settings WHERE key='caller-pending'"
            ).fetchone()[0]
            == "kept"
        )
    else:
        if boundary == "released_savepoint":
            connection.execute("RELEASE SAVEPOINT identity_scope")
        connection.rollback()
        assert not connection.in_transaction
    # Neither this counter nor in_transaction can detect all savepoint rollbacks.
    assert connection.total_changes == changes_before_rollback
    assert resolve(repo, entity, name) == ("", name)
    recreated = resolve(repo, entity, name, create=True)
    assert recreated[0] and recreated[0] != original[0]
    assert (
        connection.execute(
            f"SELECT stable_id FROM {table_for(entity)} WHERE name=?", (name,)
        ).fetchone()[0]
        == recreated[0]
    )
    assert connection.in_transaction
    connection.rollback()
    assert resolve(repo, entity, name) == ("", name)


@pytest.mark.parametrize("boundary", ["rollback", "savepoint"])
def test_negative_identity_lookup_is_not_reused_after_rollback(
    repo: Repo, entity: str, boundary: str
) -> None:
    connection = repo.db.conn
    name = "Restored identity"
    original = resolve(repo, entity, name, create=True)
    connection.commit()
    connection.execute("BEGIN")
    if boundary == "savepoint":
        connection.execute("SAVEPOINT temporary_archive")
    connection.execute(
        f"UPDATE {table_for(entity)} SET is_active=0 WHERE stable_id=?", (original[0],)
    )
    assert resolve(repo, entity, name) == ("", name)
    assert resolve(repo, entity, name) == ("", name)
    if boundary == "savepoint":
        connection.execute("ROLLBACK TO SAVEPOINT temporary_archive")
    else:
        connection.rollback()
    assert resolve(repo, entity, name) == original


@pytest.mark.parametrize("separate_connection", [False, True])
def test_negative_lookup_sees_identity_created_by_another_repo(
    repo: Repo, entity: str, separate_connection: bool
) -> None:
    peer = Repo(FinanceDB(repo.db.path)) if separate_connection else Repo(repo.db)
    try:
        name = "Peer identity"
        assert resolve(repo, entity, name) == ("", name)
        created = resolve(peer, entity, name, create=True)
        if separate_connection:
            peer.db.conn.commit()
        assert resolve(repo, entity, name) == created
        assert resolve(repo, entity, name, create=True) == created
    finally:
        if separate_connection:
            peer.db.conn.close()


def test_alias_resolution_follows_retarget_and_rollback(
    repo: Repo, entity: str
) -> None:
    connection = repo.db.conn
    first = resolve(repo, entity, "First identity", create=True)
    second = resolve(repo, entity, "Second identity", create=True)
    alias = "Legacy alias"
    key = (
        account_identity_key(alias)
        if entity == "account"
        else category_identity_key(alias)
    )
    connection.execute(
        "INSERT INTO entity_aliases(entity_type,entity_id,alias,identity_key) VALUES(?,?,?,?)",
        (entity, first[0], alias, key),
    )
    connection.commit()
    assert resolve(repo, entity, alias) == first

    connection.execute("SAVEPOINT alias_change")
    connection.execute(
        "UPDATE entity_aliases SET entity_id=? WHERE entity_type=? AND identity_key=?",
        (second[0], entity, key),
    )
    assert resolve(repo, entity, alias) == second
    connection.execute("ROLLBACK TO SAVEPOINT alias_change")
    assert resolve(repo, entity, alias) == first


def test_positive_lookup_observes_archive_and_inactive_category_option(
    repo: Repo, entity: str
) -> None:
    connection = repo.db.conn
    name = "Archived identity"
    original = resolve(repo, entity, name, create=True)
    connection.commit()
    assert resolve(repo, entity, name) == original
    connection.execute(
        f"UPDATE {table_for(entity)} SET is_active=0 WHERE stable_id=?", (original[0],)
    )
    assert resolve(repo, entity, name) == ("", name)
    if entity == "category":
        assert repo.category_identity(name, active_only=False) == original
    connection.rollback()
    assert resolve(repo, entity, name) == original
