"""A11: reject untrusted SQL before writes and preserve recovery evidence."""

import sqlite3
from pathlib import Path

import pytest

from database.engine import FinanceDB
from database.protection import DataProtectionError
from database.repo import Repo
from database.schema_contract import (
    financial_integrity_report,
    migration_dry_run,
    normalized_sql,
)


@pytest.fixture
def db(tmp_path, empty_profile):
    value = FinanceDB(tmp_path / "integrity.sqlite3")
    yield value
    if not value._closed:
        value.conn.close()


@pytest.mark.parametrize("name", ["trg_forged", "validate_forged"])
def test_unknown_prefix_trigger_is_rejected_before_startup_writes(db, name):
    db.conn.execute(
        f"CREATE TRIGGER {name} AFTER INSERT ON settings BEGIN SELECT 1; END"
    )
    db.conn.commit()
    path = db.path
    db.conn.close()
    db._closed = True
    with pytest.raises(
        DataProtectionError, match="(?i)(estrutura|sql|trigger|inesperad)"
    ):
        FinanceDB(path)
    with sqlite3.connect(path) as reader:
        assert reader.execute(
            "SELECT 1 FROM sqlite_master WHERE name=?", (name,)
        ).fetchone()


def test_known_trigger_name_does_not_authorize_changed_body(db):
    name = "trg_transactions_identity_insert"
    db.conn.execute(f"DROP TRIGGER {name}")
    db.conn.execute(
        f"CREATE TRIGGER {name} AFTER INSERT ON transactions BEGIN SELECT 1; END"
    )
    db.conn.commit()
    path = db.path
    db.conn.close()
    db._closed = True
    with pytest.raises(DataProtectionError):
        FinanceDB(path)


def test_existing_index_name_does_not_hide_column_drift(db):
    db.conn.execute("DROP INDEX idx_transactions_account_date")
    db.conn.execute(
        "CREATE INDEX idx_transactions_account_date ON transactions(description)"
    )
    db.conn.commit()
    path = db.path
    db.conn.close()
    db._closed = True
    with pytest.raises(DataProtectionError):
        FinanceDB(path)


def test_cleanup_defaults_to_dry_run_and_keeps_orphan_evidence(db):
    db.conn.execute("PRAGMA foreign_keys=OFF")
    for row in db.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='transaction_meta'"
    ).fetchall():
        db.conn.execute(f'DROP TRIGGER "{row[0]}"')
    db.conn.execute(
        "INSERT INTO transaction_meta(transaction_id,tags) VALUES(99999,'user evidence')"
    )
    db.conn.commit()
    counts = db.cleanup_orphaned_records()
    assert counts["transaction_meta"] == 1
    assert (
        db.conn.execute(
            "SELECT tags FROM transaction_meta WHERE transaction_id=99999"
        ).fetchone()[0]
        == "user evidence"
    )


def test_transfer_does_not_commit_an_outer_transaction(db):
    db.conn.execute("CREATE TABLE caller_marker(value INTEGER)")
    db.conn.execute("INSERT INTO caller_marker VALUES(1)")
    Repo(db).add_internal_transfer("2026-01-05", 30, "Source", "Target", commit=True)
    assert db.conn.in_transaction
    db.conn.rollback()
    assert db.scalar("SELECT COUNT(*) FROM transactions") == 0
    assert db.scalar("SELECT COUNT(*) FROM caller_marker") == 0


def test_current_schema_upgrade_preserves_category_rows(
    tmp_path, empty_profile, monkeypatch
):
    from database.protection import DataProtectionSession

    path = tmp_path / "upgrade.sqlite3"
    old = FinanceDB(path, app_version="3.0.9")
    old.conn.execute(
        "INSERT INTO transactions(date,description,amount,category,account,kind) VALUES('2026-01-02','Legacy alias',-10,'Outros','Conta principal','expense')"
    )
    old.conn.commit()
    before = {row["name"]: dict(row) for row in old.query("SELECT * FROM categories")}
    old.conn.close()
    verify = DataProtectionSession.verify_after_upgrade

    def compare(session, connection, actual):
        current = {
            row["name"]: dict(row)
            for row in connection.execute("SELECT * FROM categories")
        }
        differences = {
            name: {
                column: (value, current.get(name, {}).get(column))
                for column, value in previous.items()
                if value != current.get(name, {}).get(column)
            }
            for name, previous in before.items()
            if current.get(name) != previous
        }
        assert not differences, differences
        return verify(session, connection, actual)

    monkeypatch.setattr(DataProtectionSession, "verify_after_upgrade", compare)
    upgraded = FinanceDB(path, app_version="3.1.0")
    upgraded.conn.close()


def _legacy_profile(path: Path, version: int) -> Path:
    """Stop actual source migrations at a supported intermediate version."""
    legacy = FinanceDB.__new__(FinanceDB)
    legacy.conn = sqlite3.connect(path)
    legacy.conn.row_factory = sqlite3.Row
    legacy.path = path
    legacy.attachments_directory = path.parent / "empty-attachments"
    legacy._closed = False
    legacy._read_only = False
    legacy._startup_progress = None
    legacy.init_schema()

    class ReachedVersion(Exception):
        pass

    def stop(current):
        if current >= 16:
            legacy._defer_validation_trigger_rebuild = False
            try:
                legacy._migration_value_constraints()
            finally:
                legacy._defer_validation_trigger_rebuild = True
        if current == version:
            raise ReachedVersion

    if version:
        legacy._schema_contract_observer = stop
        try:
            legacy._apply_pending_migrations()
        except ReachedVersion:
            legacy.conn.commit()
    legacy.conn.execute(
        "INSERT INTO categories(name,kind,colour,budget) VALUES('Legacy category','expense','#123456',12.34)"
    )
    legacy.conn.execute(
        "INSERT INTO transactions(date,description,amount,category,account,kind) VALUES('2026-01-05','Original legacy movement',-12.34,'Legacy category','Legacy account','expense')"
    )
    legacy.conn.commit()
    legacy.conn.close()
    return path


@pytest.mark.parametrize("version", [0, 15, 23, 29])
def test_supported_versions_upgrade_without_financial_changes(
    tmp_path, empty_profile, version
):
    path = _legacy_profile(tmp_path / f"legacy-{version}.sqlite3", version)
    upgraded = FinanceDB(path)
    try:
        assert upgraded.schema_version() == FinanceDB.CURRENT_SCHEMA_VERSION
        row = upgraded.query(
            "SELECT date,description,amount,category,account,kind FROM transactions"
        )[0]
        assert tuple(row) == (
            "2026-01-05",
            "Original legacy movement",
            -12.34,
            "Legacy category",
            "Legacy account",
            "expense",
        )
        assert financial_integrity_report(upgraded.conn)["ok"]
    finally:
        upgraded.conn.close()


def test_failed_upgrade_rolls_back_then_reopens_supported_source(
    tmp_path, empty_profile, monkeypatch
):
    path = _legacy_profile(tmp_path / "interrupted.sqlite3", 23)
    migration = FinanceDB._migration_holding_currency_and_exchange_rate

    def fail_after_ddl(instance):
        migration(instance)
        instance.conn.execute("CREATE TABLE interrupted_phase4(value TEXT)")
        raise RuntimeError("injected interrupted upgrade")

    from database.schema_contract import trusted_schema_contracts

    assert trusted_schema_contracts()
    with monkeypatch.context() as patch:
        patch.setattr(
            FinanceDB, "_migration_holding_currency_and_exchange_rate", fail_after_ddl
        )
        # Reference construction is trusted source and must not observe injected
        # failure hooks while establishing the pre-upgrade source contract.
        with pytest.raises(DataProtectionError):
            FinanceDB(path)
    with sqlite3.connect(path) as reader:
        assert (
            reader.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
            == 23
        )
        assert not reader.execute(
            "SELECT 1 FROM sqlite_master WHERE name='interrupted_phase4'"
        ).fetchone()
        assert reader.execute("SELECT amount FROM transactions").fetchone()[0] == -12.34
    reopened = FinanceDB(path)
    reopened.conn.close()


def test_dry_run_lists_actual_pending_registry_without_writes(tmp_path, empty_profile):
    path = _legacy_profile(tmp_path / "dry-run.sqlite3", 23)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        before = path.read_bytes()
        changes = connection.total_changes
        report = migration_dry_run(connection)
        assert not report["would_block"]
        assert [item["version"] for item in report["pending_migrations"]] == list(
            range(24, FinanceDB.CURRENT_SCHEMA_VERSION + 1)
        )
        assert (
            report["pending_migrations"][0]["name"]
            == "stable_financial_entity_identity"
        )
        assert report["before_account_category_totals"]["account"] == {
            "Legacy account": "-12.34"
        }
        assert (
            report["mode"] == "source_registry_plan_only"
            and report["data_transformations"]
        )
        assert path.read_bytes() == before and connection.total_changes == changes
        assert not connection.in_transaction
    finally:
        connection.close()


def test_composite_foreign_key_is_not_two_independent_foreign_keys():
    from database.schema_contract import _shape

    shapes = []
    for foreign in (
        "FOREIGN KEY(x,y) REFERENCES parent(a,b)",
        "FOREIGN KEY(x) REFERENCES parent(a), FOREIGN KEY(y) REFERENCES parent(b)",
    ):
        connection = sqlite3.connect(":memory:")
        try:
            connection.execute(
                "CREATE TABLE parent(a TEXT UNIQUE,b TEXT UNIQUE,UNIQUE(a,b))"
            )
            sql = "CREATE TABLE child(x TEXT,y TEXT," + foreign + ")"
            connection.execute(sql)
            shapes.append(_shape(connection, "child", sql))
        finally:
            connection.close()
    assert shapes[0] != shapes[1]


def test_sql_normalization_preserves_literal_whitespace_and_operators():
    assert normalized_sql("SELECT 1 /* harmless */ + 2") == normalized_sql("select 1+2")
    assert normalized_sql("SELECT 'a b'") != normalized_sql("SELECT 'ab'")
    assert normalized_sql("SELECT x>=1") != normalized_sql("SELECT x>1")


@pytest.mark.parametrize("column", ["account_id", "category_id"])
def test_declared_identity_mismatch_is_rejected_before_repair(db, column):
    repo = Repo(db)
    txid = repo.add_transaction(
        "2026-01-05", "Original movement", -10, "Diversos", "Source"
    )
    triggers = list(
        db.conn.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name='transactions'"
        )
    )
    for row in triggers:
        db.conn.execute('DROP TRIGGER "' + str(row[0]).replace('"', '""') + '"')
    db.conn.execute(
        f"UPDATE transactions SET {column}='nonexistent-identity' WHERE id=?", (txid,)
    )
    for row in triggers:
        db.conn.execute(row[1])
    db.conn.commit()
    inventory = financial_integrity_report(db.conn)
    assert not inventory["ok"] and "identidade" in ";".join(inventory["issues"])
    path = db.path
    db.conn.close()
    db._closed = True
    before = path.read_bytes()
    with pytest.raises(DataProtectionError, match="identidade"):
        FinanceDB(path)
    assert path.read_bytes() == before


def test_repo_inventory_never_commits_or_deletes_caller_state(db):
    repo = Repo(db)
    db.conn.execute(
        "INSERT INTO settings(key,value) VALUES('uncommitted-phase4','retain')"
    )
    report = repo.integrity_inventory()
    assert report["dry_run"] and report["migration_plan"]["pending_migrations"] == []
    assert db.conn.in_transaction
    with pytest.raises(ValueError, match="remoção automática"):
        repo.cleanup_orphaned_records(dry_run=False)
    db.conn.rollback()
    assert repo.setting("uncommitted-phase4") == ""


def test_deleted_holding_undo_cannot_replace_another_stable_identity(db):
    from services.change_journal import ChangeJournalService

    repo = Repo(db)
    holding_id = repo.add_holding("Deleted", "ETF", "Broker", 1200, 1000)
    stable_id = repo.holding_by_id(holding_id)["stable_id"]
    journal = ChangeJournalService(repo)
    journal.delete_holding(holding_id, label="Synthetic deletion")
    other = repo.add_holding("Current owner", "ETF", "Broker", 200, 100)
    db.conn.execute("UPDATE holdings SET stable_id=? WHERE id=?", (stable_id, other))
    db.conn.commit()
    before = dict(repo.holding_by_id(other))
    with pytest.raises(RuntimeError, match="identidade"):
        journal.undo_latest()
    assert dict(repo.holding_by_id(other)) == before
    assert repo.holding_by_id(holding_id) is None


def test_legacy_claim_does_not_authorize_missing_financial_capabilities(
    tmp_path, empty_profile
):
    from database.schema_contract import inspect_schema_contract

    path = _legacy_profile(tmp_path / "legacy-missing.sqlite3", 23)
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE holdings")
        connection.execute("DROP TABLE financial_accounts")
        connection.execute(
            "INSERT INTO schema_migrations(version,name) VALUES(24,'forged capability')"
        )
        report = inspect_schema_contract(connection)
        assert not report["ok"]
        assert "financial_accounts.stable_id" in report["missing_capabilities"]


def test_primary_key_conflict_replace_policy_is_not_a_trusted_shape(db):
    from database.schema_contract import inspect_schema_contract

    db.conn.executescript(
        "ALTER TABLE settings RENAME TO settings_old;"
        "CREATE TABLE settings(key TEXT PRIMARY KEY ON CONFLICT REPLACE,value TEXT NOT NULL);"
        "INSERT INTO settings SELECT * FROM settings_old;"
        "DROP TABLE settings_old;"
    )
    report = inspect_schema_contract(db.conn)
    assert not report["ok"] and "settings" in report["drifted_objects"]
    path = db.path
    db.conn.close()
    db._closed = True
    before = path.read_bytes()
    with pytest.raises(DataProtectionError):
        FinanceDB(path)
    assert path.read_bytes() == before


def test_deferred_foreign_key_timing_is_part_of_the_contract():
    from database.schema_contract import _shape

    shapes = []
    for timing in ("", " DEFERRABLE INITIALLY DEFERRED"):
        connection = sqlite3.connect(":memory:")
        try:
            connection.execute("CREATE TABLE parent(id INTEGER PRIMARY KEY)")
            sql = (
                "CREATE TABLE child(parent_id INTEGER REFERENCES parent(id)"
                + timing
                + ")"
            )
            connection.execute(sql)
            shapes.append(_shape(connection, "child", sql))
        finally:
            connection.close()
    assert shapes[0] != shapes[1]


@pytest.mark.parametrize("operation", ["add_meta", "update_meta", "update_transfer", "update_fields", "update_plain"])
def test_metadata_and_update_helpers_preserve_caller_transaction(db, operation):
    repo = Repo(db)
    txid = repo.add_transaction("2026-01-05", "Original", -10, "Diversos", "Source")
    legs = repo.add_internal_transfer("2026-01-05", 30, "Source", "Target")
    group = repo.transaction_by_id(legs[0])["transfer_group_id"]
    original = [tuple(row) for row in repo.q("SELECT * FROM transactions ORDER BY id")]
    db.conn.execute("INSERT INTO settings(key,value) VALUES('caller-phase4','uncommitted')")
    if operation == "add_meta":
        repo.add_transaction_with_meta("2026-01-06", "Nested metadata", -20, "Diversos", "Source", tags="synthetic", commit=True)
    elif operation == "update_meta":
        repo.update_transaction_with_meta(txid, {"amount": -99}, tags="changed", commit=True)
    elif operation == "update_transfer":
        repo.update_internal_transfer(group, "2026-01-06", 40, "Source changed", "Target changed", commit=True)
    elif operation == "update_fields":
        repo.update_transaction_fields(txid, {"amount": -99}, commit=True)
    else:
        repo.update_transaction(txid, amount=-99)
    assert db.conn.in_transaction
    db.conn.rollback()
    assert repo.setting("caller-phase4") == ""
    assert [tuple(row) for row in repo.q("SELECT * FROM transactions ORDER BY id")] == original


def test_partial_update_commit_false_never_commits_its_own_batch(db):
    repo = Repo(db)
    txid = repo.add_transaction("2026-01-05", "Original", -10, "Diversos", "Source")
    repo.update_transaction_fields(txid, {"amount": -99}, commit=False)
    assert db.conn.in_transaction
    db.conn.rollback()
    assert repo.transaction_by_id(txid)["amount"] == -10


@pytest.mark.parametrize("operation", ["add", "update"])
def test_failed_metadata_write_rolls_back_only_its_savepoint(db, monkeypatch, operation):
    repo = Repo(db)
    txid = repo.add_transaction("2026-01-05", "Original", -10, "Diversos", "Source")
    db.conn.execute("INSERT INTO settings(key,value) VALUES('caller-phase4','uncommitted')")

    def fail(*args, **kwargs):
        db.conn.execute("INSERT INTO settings(key,value) VALUES('failed-metadata-leak','must rollback')")
        raise RuntimeError("injected metadata failure")

    monkeypatch.setattr(repo, "_set_transaction_meta_ext", fail)
    with pytest.raises(RuntimeError, match="injected"):
        if operation == "add":
            repo.add_transaction_with_meta("2026-01-06", "Rejected", -20, "Diversos", "Source", commit=True)
        else:
            repo.update_transaction_with_meta(txid, {"amount": -99}, commit=True)
    assert db.conn.in_transaction
    assert repo.setting("caller-phase4") == "uncommitted"
    assert repo.setting("failed-metadata-leak") == ""
    assert repo.transaction_by_id(txid)["amount"] == -10
    assert repo.movement_count() == 1
    db.conn.rollback()
