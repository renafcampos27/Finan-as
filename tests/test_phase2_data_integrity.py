#!/usr/bin/env python3
"""Phase-2 data integrity & safety regression tests.

Guards (v3.49 audit, Fase 2):
  * ``backup_to`` copies only the committed state and never commits pending
    user changes on the source connection (rollback keeps working).
  * ``add_internal_transfer`` validates before writing and creates account
    identities inside the savepoint, so a rejected transfer leaves no
    orphan accounts behind.
  * ``Repo(db)`` resolves companion paths from the injected database file.
  * Migration DDL runs through the atomic schema runner: one failing
    statement rolls back the whole script (SQLite DDL is transactional),
    and trigger bodies survive the splitter intact.
  * Pending migrations and their version ledger commit or roll back together.
  * Transfer accounts, both legs and undo records respect outer transactions;
    commit=False never persists a standalone transfer implicitly.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from database.engine import FinanceDB  # noqa: E402
from database.repo import Repo  # noqa: E402


@pytest.fixture
def repo(tmp_path, monkeypatch):
    import utils.config as config

    monkeypatch.setattr(config, "DB_NAME", str(tmp_path / "phase2.sqlite3"))
    r = Repo()
    yield r
    r.db.conn.close()


def _fresh_db(tmp_path: Path, name: str) -> FinanceDB:
    """FinanceDB without the session-wide demo seeding."""
    import os

    previous = os.environ.pop("FINANCAS_PESSOAIS_DEMO_DATA", None)
    try:
        return FinanceDB(tmp_path / name)
    finally:
        if previous is not None:
            os.environ["FINANCAS_PESSOAIS_DEMO_DATA"] = previous


# ------------------------------------------------------- backup semantics ----


def test_backup_to_never_commits_pending_changes(tmp_path):
    db = _fresh_db(tmp_path, "pending.sqlite3")
    try:
        db.add_transaction(
            "2026-01-02", "Confirmado", 100.0, "Rendimentos", "CGD", "income"
        )
        db.conn.commit()
        # A change the user may still want to roll back (commit=False
        # keeps the write inside an open transaction).
        db.add_transaction(
            "2026-01-03",
            "Pendente",
            500.0,
            "Rendimentos",
            "CGD",
            "income",
            commit=False,
        )
        assert db.conn.in_transaction

        target = tmp_path / "backups" / "snap.sqlite3"
        db.backup_to(target)

        assert target.exists()
        from database.read_only import open_read_only_connection

        reader = open_read_only_connection(target)
        try:
            descriptions = {
                str(row["description"])
                for row in reader.execute("SELECT description FROM transactions")
            }
        finally:
            reader.close()
        assert "Confirmado" in descriptions
        assert "Pendente" not in descriptions
        # The live connection kept its pending change and can roll back.
        assert db.conn.in_transaction
        db.conn.rollback()
        remaining = [
            str(row["description"])
            for row in db.conn.execute("SELECT description FROM transactions")
        ]
        assert remaining == ["Confirmado"]
    finally:
        db.prepare_for_shutdown()


@pytest.mark.parametrize("external", [False, True])
@pytest.mark.parametrize("commit", [False, True])
def test_transfer_transaction_ownership(tmp_path, external, commit):
    db = _fresh_db(tmp_path, "transfer_ownership.sqlite3")
    connection = db.conn
    try:
        connection.execute("CREATE TABLE caller_marker(value INTEGER)")
        if external:
            connection.execute("BEGIN")
            connection.execute("INSERT INTO caller_marker VALUES(1)")
        repo = Repo(db)
        repo.add_internal_transfer(
            "2026-01-05", 30, "Ownership Source", "Ownership Target", commit=commit
        )
        pending = external or not commit
        assert connection.in_transaction == pending
        # A second connection must see either both committed legs or neither.
        with sqlite3.connect(db.path) as reader:
            assert reader.execute("SELECT COUNT(*) FROM transactions").fetchone()[
                0
            ] == (0 if pending else 2)
        connection.rollback()
        assert db.scalar("SELECT COUNT(*) FROM transactions") == (0 if pending else 2)
        assert db.scalar(
            "SELECT COUNT(*) FROM financial_accounts WHERE name LIKE 'Ownership %'"
        ) == (0 if pending else 2)
        assert db.scalar("SELECT COUNT(*) FROM caller_marker") == 0
    finally:
        connection.close()


@pytest.mark.parametrize("external", [False, True])
@pytest.mark.parametrize("commit", [False, True])
def test_transfer_credit_failure_preserves_caller_transaction(
    tmp_path, external, commit
):
    db = _fresh_db(tmp_path, "transfer_failure.sqlite3")
    connection = db.conn
    try:
        connection.execute("CREATE TABLE caller_marker(value INTEGER)")
        connection.execute(
            "CREATE TRIGGER reject_phase2_credit BEFORE INSERT ON transactions "
            "WHEN NEW.amount>0 BEGIN SELECT RAISE(ABORT, 'credit rejected'); END"
        )
        baseline_categories = db.scalar("SELECT COUNT(*) FROM categories")
        if external:
            connection.execute("BEGIN")
            connection.execute("INSERT INTO caller_marker VALUES(1)")
        with pytest.raises(sqlite3.IntegrityError, match="credit rejected"):
            Repo(db).add_internal_transfer(
                "2026-01-05", 30, "Failure Source", "Failure Target", commit=commit
            )
        assert connection.in_transaction == external
        assert db.scalar("SELECT COUNT(*) FROM caller_marker") == int(external)
        assert db.scalar("SELECT COUNT(*) FROM transactions") == 0
        assert db.scalar("SELECT COUNT(*) FROM categories") == baseline_categories
        assert (
            db.scalar(
                "SELECT COUNT(*) FROM financial_accounts WHERE name LIKE 'Failure %'"
            )
            == 0
        )
        connection.commit()
        with sqlite3.connect(db.path) as reader:
            assert (
                reader.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0
            )
            assert reader.execute("SELECT COUNT(*) FROM caller_marker").fetchone()[
                0
            ] == int(external)
    finally:
        connection.close()


@pytest.mark.parametrize("external", [False, True])
@pytest.mark.parametrize("reject_marker", [False, True])
def test_migration_schema_and_version_share_transaction(
    tmp_path, external, reject_marker
):
    db = _fresh_db(tmp_path, "migration_ownership.sqlite3")
    connection = db.conn
    index_sql = (
        "SELECT COUNT(*) FROM sqlite_master "
        "WHERE type='index' AND name='idx_transactions_account_date'"
    )
    version_sql = "SELECT COUNT(*) FROM schema_migrations WHERE version=10"
    try:
        connection.execute("DELETE FROM schema_migrations WHERE version=10")
        connection.execute("DROP INDEX idx_transactions_account_date")
        connection.execute("CREATE TABLE caller_marker(value INTEGER)")
        if reject_marker:
            connection.execute(
                "CREATE TRIGGER reject_phase2_version BEFORE INSERT ON schema_migrations "
                "WHEN NEW.version=10 BEGIN SELECT RAISE(ABORT, 'version rejected'); END"
            )
        connection.commit()
        if external:
            connection.execute("BEGIN")
            connection.execute("INSERT INTO caller_marker VALUES(1)")
        if reject_marker:
            with pytest.raises(sqlite3.IntegrityError, match="version rejected"):
                db._ensure_migrations()
        else:
            db._ensure_migrations()
        assert connection.in_transaction == external
        assert db.scalar(index_sql) == int(not reject_marker)
        assert db.scalar(version_sql) == int(not reject_marker)
        assert db.scalar("SELECT COUNT(*) FROM caller_marker") == int(external)
        with sqlite3.connect(db.path) as reader:
            durable = int(not external and not reject_marker)
            assert reader.execute(index_sql).fetchone()[0] == durable
            assert reader.execute(version_sql).fetchone()[0] == durable
        connection.rollback()
        durable = int(not external and not reject_marker)
        assert db.scalar(index_sql) == durable
        assert db.scalar(version_sql) == durable
        assert db.scalar("SELECT COUNT(*) FROM caller_marker") == 0
    finally:
        connection.close()


def test_schema_failure_rolls_back_only_its_savepoint(tmp_path):
    db = _fresh_db(tmp_path, "nested_script.sqlite3")
    try:
        db.conn.execute("CREATE TABLE caller_marker(value INTEGER)")
        db.conn.execute("INSERT INTO caller_marker VALUES(1)")
        with pytest.raises(sqlite3.OperationalError, match="already exists"):
            db.run_schema_script(
                "CREATE TABLE partial_schema(value INTEGER);"
                "CREATE TABLE caller_marker(value INTEGER);"
            )
        assert db.conn.in_transaction
        assert db.scalar("SELECT COUNT(*) FROM caller_marker") == 1
        assert (
            db.scalar("SELECT COUNT(*) FROM sqlite_master WHERE name='partial_schema'")
            == 0
        )
        db.conn.commit()
    finally:
        db.conn.close()


@pytest.mark.parametrize("external", [False, True])
def test_later_migration_failure_rolls_back_schema_and_ledger(
    tmp_path, monkeypatch, external
):
    db = _fresh_db(tmp_path, "migration_batch.sqlite3")
    try:
        db.conn.execute("DELETE FROM schema_migrations WHERE version IN (10,11)")
        db.conn.execute("DROP INDEX idx_transactions_account_date")
        db.conn.execute("CREATE TABLE caller_marker(value INTEGER)")
        db.conn.commit()
        original_migration = db._migration_habit_change_plans

        def failing_migration():
            original_migration()
            db.run_schema_script("CREATE TABLE partial_migration(value INTEGER);")
            raise RuntimeError("later migration rejected")

        monkeypatch.setattr(db, "_migration_habit_change_plans", failing_migration)
        if external:
            db.conn.execute("INSERT INTO caller_marker VALUES(1)")
        with pytest.raises(RuntimeError, match="later migration rejected"):
            db._ensure_migrations()
        assert db.conn.in_transaction == external
        assert db.scalar("SELECT COUNT(*) FROM caller_marker") == int(external)
        assert (
            db.scalar("SELECT COUNT(*) FROM schema_migrations WHERE version IN (10,11)")
            == 0
        )
        assert (
            db.scalar(
                "SELECT COUNT(*) FROM sqlite_master "
                "WHERE name IN ('idx_transactions_account_date', 'partial_migration')"
            )
            == 0
        )
        assert not db._defer_validation_trigger_rebuild
        # A retry succeeds in the same connection without leaking the failed DDL.
        monkeypatch.setattr(db, "_migration_habit_change_plans", original_migration)
        db._ensure_migrations()
        assert (
            db.scalar("SELECT COUNT(*) FROM schema_migrations WHERE version IN (10,11)")
            == 2
        )
        assert db.conn.in_transaction == external
        db.conn.commit()
        assert db.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert not db.conn.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        db.conn.close()


@pytest.mark.parametrize("reject_journal", [False, True])
def test_transfer_and_undo_ledger_are_atomic(tmp_path, reject_journal):
    from services.change_journal import ChangeJournalService

    db = _fresh_db(tmp_path, "transfer_journal.sqlite3")
    try:
        repo = Repo(db)
        journal = ChangeJournalService(
            repo,
            attachments_directory=tmp_path / "attachments",
            journal_storage=tmp_path / "history",
        )
        if reject_journal:
            db.conn.execute(
                "CREATE TRIGGER reject_phase2_journal BEFORE INSERT ON change_journal "
                "BEGIN SELECT RAISE(ABORT, 'journal rejected'); END"
            )
        db.conn.commit()

        def transfer():
            return journal.run_with_created_transactions(
                lambda: list(
                    repo.add_internal_transfer(
                        "2026-01-05",
                        30,
                        "Journal Source",
                        "Journal Target",
                        commit=False,
                    )
                ),
                label="Phase 2 atomic transfer",
                action_type="transfer",
            )

        if reject_journal:
            with pytest.raises(sqlite3.IntegrityError, match="journal rejected"):
                transfer()
        else:
            assert len(transfer()) == 2
        expected = 0 if reject_journal else 2
        assert not db.conn.in_transaction
        assert db.scalar("SELECT COUNT(*) FROM transactions") == expected
        assert (
            db.scalar(
                "SELECT COUNT(*) FROM financial_accounts WHERE name LIKE 'Journal %'"
            )
            == expected
        )
        assert db.scalar("SELECT COUNT(*) FROM change_journal") == int(
            not reject_journal
        )
        if not reject_journal:
            journal.undo_latest()
            assert db.scalar("SELECT COUNT(*) FROM transactions") == 0
    finally:
        db.conn.close()


@pytest.mark.parametrize("external", [False, True])
def test_transfer_commit_false_allows_explicit_caller_commit(tmp_path, external):
    db = _fresh_db(tmp_path, "explicit_commit.sqlite3")
    try:
        if external:
            db.conn.execute("BEGIN")
        Repo(db).add_internal_transfer(
            "2026-01-05", 30, "Explicit Source", "Explicit Target", commit=False
        )
        assert db.conn.in_transaction
        db.conn.commit()
        with sqlite3.connect(db.path) as reader:
            assert (
                reader.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 2
            )
            assert (
                reader.execute("SELECT SUM(amount) FROM transactions").fetchone()[0]
                == 0
            )
    finally:
        db.conn.close()


def test_backup_to_keeps_source_writable_after_snapshot(tmp_path):
    db = _fresh_db(tmp_path, "writable.sqlite3")
    try:
        db.add_transaction("2026-01-02", "Base", 10.0, "Rendimentos", "CGD", "income")
        db.conn.commit()
        target = tmp_path / "snap.sqlite3"
        db.backup_to(target)
        from database.read_only import open_read_only_connection

        reader = open_read_only_connection(target)
        try:
            rows = reader.execute("SELECT COUNT(*) AS n FROM transactions").fetchone()
            assert int(rows["n"]) == 1
        finally:
            reader.close()
        # The source connection can still write afterwards.
        db.add_transaction("2026-01-03", "Depois", 20.0, "Rendimentos", "CGD", "income")
        db.conn.commit()
        assert db.scalar("SELECT COUNT(*) FROM transactions") == 2
    finally:
        db.prepare_for_shutdown()


# --------------------------------------------------- transfer atomicity ------


def test_invalid_transfer_leaves_no_orphan_accounts(repo):
    before = {str(row["name"]) for row in repo.q("SELECT name FROM financial_accounts")}
    baseline_transactions = repo.scalar("SELECT COUNT(*) FROM transactions")
    with pytest.raises(ValueError):
        repo.add_internal_transfer("2026-01-05", 0.0, "Banco A", "Banco B")
    after = {str(row["name"]) for row in repo.q("SELECT name FROM financial_accounts")}
    assert after == before
    assert repo.scalar("SELECT COUNT(*) FROM transactions") == baseline_transactions


def test_same_account_transfer_creates_nothing(repo):
    before = {str(row["name"]) for row in repo.q("SELECT name FROM financial_accounts")}
    with pytest.raises(ValueError):
        repo.add_internal_transfer("2026-01-05", 25.0, "Unica", "unica")
    assert {
        str(row["name"]) for row in repo.q("SELECT name FROM financial_accounts")
    } == before


def test_valid_transfer_is_atomic_and_creates_both_legs(repo):
    debit_id, credit_id = repo.add_internal_transfer(
        "2026-01-05", 30.0, "Origem X", "Destino Y"
    )
    rows = repo.q(
        "SELECT id, amount, account, transfer_group_id FROM transactions "
        "WHERE transfer_group_id<>''"
    )
    assert len(rows) == 2
    amounts = sorted(float(row["amount"]) for row in rows)
    assert amounts == [-30.0, 30.0]
    assert len({str(row["account"]) for row in rows}) == 2
    assert debit_id and credit_id


# ---------------------------------------------------------- Repo(db) paths ---


def test_repo_with_injected_db_uses_that_database_path(tmp_path):
    db = _fresh_db(tmp_path, "injected.sqlite3")
    try:
        repo = Repo(db)
        assert repo.paths.database == db.path
        assert repo.paths.database.name == "injected.sqlite3"
    finally:
        db.prepare_for_shutdown()


# ------------------------------------------------ atomic schema execution ----


def test_run_schema_script_is_atomic_on_failure(tmp_path):
    db = _fresh_db(tmp_path, "schema.sqlite3")
    try:
        db.run_schema_script(
            "CREATE TABLE IF NOT EXISTS phase2_a(id INTEGER PRIMARY KEY);"
            "CREATE TABLE IF NOT EXISTS phase2_b(id INTEGER PRIMARY KEY);"
        )
        names = {
            str(row["name"])
            for row in db.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {"phase2_a", "phase2_b"} <= names

        broken = (
            "CREATE TABLE IF NOT EXISTS phase2_c(id INTEGER PRIMARY KEY);"
            "CREATE TABLE phase2_b(id INTEGER PRIMARY KEY);"  # already exists
            "CREATE TABLE IF NOT EXISTS phase2_d(id INTEGER PRIMARY KEY);"
        )
        from sqlite3 import OperationalError

        with pytest.raises(OperationalError):
            db.run_schema_script(broken)
        names = {
            str(row["name"])
            for row in db.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        # Neither the statement before nor after the failure survived.
        assert "phase2_c" not in names
        assert "phase2_d" not in names
    finally:
        db.prepare_for_shutdown()


def test_run_schema_script_preserves_trigger_bodies(tmp_path):
    db = _fresh_db(tmp_path, "triggers.sqlite3")
    try:
        db.run_schema_script(
            "CREATE TABLE IF NOT EXISTS trg_t(id INTEGER PRIMARY KEY, v TEXT);"
            "CREATE TRIGGER trg_t_after_insert AFTER INSERT ON trg_t "
            "BEGIN UPDATE trg_t SET v='x' WHERE id=NEW.id; "
            "UPDATE trg_t SET v=v||'!' WHERE id=NEW.id; END;"
        )
        rows = db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' "
            "AND name='trg_t_after_insert'"
        ).fetchall()
        assert len(rows) == 1
        db.conn.execute("INSERT INTO trg_t(id) VALUES(1)")
        db.conn.commit()
        row = db.conn.execute("SELECT v FROM trg_t WHERE id=1").fetchone()
        # Both body statements ran: first sets 'x', then appends '!'.
        assert str(row[0]) == "x!"
    finally:
        db.prepare_for_shutdown()


def test_run_schema_script_joins_open_transaction_instead_of_committing(tmp_path):
    db = _fresh_db(tmp_path, "marker.sqlite3")
    try:
        db.run_schema_script(
            "CREATE TABLE IF NOT EXISTS phase2_marker(id INTEGER PRIMARY KEY);"
        )
        # Caller-managed transaction: the DDL must NOT commit it silently.
        db.conn.execute("BEGIN IMMEDIATE")
        db.conn.execute("INSERT INTO phase2_marker(id) VALUES(1)")
        db.run_schema_script(
            "CREATE TABLE IF NOT EXISTS phase2_marker2(id INTEGER PRIMARY KEY);"
        )
        assert db.conn.in_transaction
        db.conn.rollback()
        # The DDL itself rolled back with the caller's transaction:
        # SQLite DDL is transactional, so nothing partial can leak.
        names = {
            str(row["name"])
            for row in db.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "phase2_marker2" not in names
        assert db.scalar("SELECT COUNT(*) FROM phase2_marker") == 0
    finally:
        db.prepare_for_shutdown()
