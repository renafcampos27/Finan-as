"""Exact retained-profile compatibility without relaxing SQL trust boundaries."""

from __future__ import annotations

import hashlib
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from database.engine import FinanceDB
from database.protection import DataProtectionError
from database.retained_schema import (
    COVERING_INDEX_SQL,
    LEGACY_SEARCH_SQL,
    RETAINED_TABLE_SQL,
)
from database.schema_contract import (
    financial_integrity_report,
    inspect_schema_contract,
    normalized_sql,
    trusted_schema_contracts,
)
from services.startup_security import clear_active_session
from utils.config import paths_for_database


OPAQUE_TABLES = (
    "annual_goals",
    "monthly_split_plans",
    "personal_debts",
    "query_cache",
    "recurring_items",
    "warranties",
)
PRESERVED_TABLES = OPAQUE_TABLES + (
    "categories",
    "financial_state_snapshots",
    "monthly_targets",
    "subcategories",
    "transaction_attachments",
    "transactions",
    "transaction_meta",
)


@pytest.fixture(autouse=True)
def synthetic_profile(empty_profile):
    clear_active_session()
    yield
    clear_active_session()


def retained_profile(
    path: Path, *, declarations: dict[str, str] | None = None, seed_opaque: bool = True
) -> Path:
    """Build only synthetic evidence from fixed source-owned declarations."""
    legacy = FinanceDB.__new__(FinanceDB)
    legacy.conn = sqlite3.connect(path)
    legacy.conn.row_factory = sqlite3.Row
    legacy.path = path
    legacy.attachments_directory = paths_for_database(path).attachments
    legacy._closed, legacy._read_only, legacy._startup_progress = False, False, None
    for sql in (declarations or RETAINED_TABLE_SQL).values():
        legacy.conn.execute(sql)
    legacy.init_schema()

    class ReachedThirty(Exception):
        pass

    def stop(version):
        if version >= 16:
            legacy._defer_validation_trigger_rebuild = False
            try:
                legacy._migration_value_constraints()
            finally:
                legacy._defer_validation_trigger_rebuild = True
        if version == 30:
            raise ReachedThirty

    legacy._schema_contract_observer = stop
    try:
        legacy._apply_pending_migrations()
    except ReachedThirty:
        legacy.conn.commit()
    legacy.ensure_analytics_tables()
    legacy.seed_if_empty()
    legacy._ensure_stable_financial_entity_identity()
    legacy.ensure_reference_data()
    legacy._ensure_stable_financial_entity_identity()
    legacy.conn.execute(
        "INSERT INTO settings(key,value) VALUES('retained_startup_marker','unchanged')"
    )
    legacy.conn.execute(
        "INSERT INTO categories(name,kind,colour,budget,parent_name) "
        "VALUES('Retained category','expense','#123456',12.3456789,'Legacy parent')"
    )
    legacy.conn.execute(
        "INSERT INTO transactions(date,description,amount,category,account,kind,notes) "
        "VALUES('2026-01-05','Synthetic retained evidence',-12.34,"
        "'Retained category','Synthetic account','expense','Original note')"
    )
    transaction_id = int(legacy.conn.execute("SELECT MAX(id) FROM transactions").fetchone()[0])
    category_id = legacy.conn.execute(
        "SELECT stable_id FROM categories WHERE name='Retained category'"
    ).fetchone()[0]
    legacy.conn.execute(
        "INSERT INTO subcategories(category,name,budget,category_id) VALUES(?,?,?,?)",
        ("Retained category", "Legacy detail", 1.23456789, category_id),
    )
    legacy.conn.execute(
        "INSERT INTO monthly_targets(month,expense_target,notes) VALUES('2026-01',123.456789,'Target note')"
    )
    legacy.conn.execute(
        "INSERT INTO financial_state_snapshots(day,holdings_json,goals_json) VALUES(?,?,NULL)",
        ("2026-01-05", '[{"quantity":0.123456789,"unit_price":123.456789}]'),
    )
    legacy.conn.execute(
        "INSERT INTO transaction_meta(transaction_id,external_reference,tags) VALUES(?,?,?)",
        (transaction_id, "externaloriginaltoken", "originaltagtoken"),
    )
    legacy.attachments_directory.mkdir()
    document = legacy.attachments_directory / "synthetic.txt"
    document.write_bytes(b"Synthetic attachment evidence\n")
    stat = document.stat()
    legacy.conn.execute(
        "INSERT INTO transaction_attachments(transaction_id,file_name,stored_name,stored_path,"
        "tags,invoice_number,content_sha256,file_size,file_mtime_ns) VALUES(?,?,?,?,?,?,?,?,?)",
        (
            transaction_id,
            "synthetic.txt",
            "synthetic.txt",
            str(document),
            "legacy attachment tags",
            "SYNTHETIC-2026-001",
            hashlib.sha256(document.read_bytes()).hexdigest(),
            stat.st_size,
            stat.st_mtime_ns,
        ),
    )
    if seed_opaque:
        legacy.conn.execute(
            "INSERT INTO annual_goals(year,name,target_amount,current_amount,notes) "
            "VALUES(2026,'Old goal',1000.123456789,234.56789,'Keep goal evidence')"
        )
        legacy.conn.execute(
            "INSERT INTO monthly_split_plans(month,category,percent,amount) "
            "VALUES('2026-01','Retained category',33.333333333,123.456789)"
        )
        legacy.conn.execute(
            "INSERT INTO personal_debts(person,total_amount,paid_amount,notes) "
            "VALUES('Synthetic counterpart',900.123456789,13.123456789,'Keep debt evidence')"
        )
        legacy.conn.execute(
            "INSERT INTO query_cache(key,value,expires_at) "
            "VALUES('legacy-key','{\"precise\":0.123456789}','2027-01-01')"
        )
        legacy.conn.execute(
            "INSERT INTO recurring_items(name,amount,notes) "
            "VALUES('Old recurrence',10.123456789,'Keep recurrence evidence')"
        )
        legacy.conn.execute(
            "INSERT INTO warranties(title,amount,attachment_id,notes) "
            "VALUES('Old warranty',99.123456789,1,'Keep warranty evidence')"
        )
    legacy._ensure_stable_financial_entity_identity()
    for name, sql in LEGACY_SEARCH_SQL.items():
        legacy.conn.execute(f'DROP TRIGGER "{name}"')
        legacy.conn.execute(sql)
    legacy.conn.execute("DROP INDEX idx_transactions_date_amount_cat")
    legacy.conn.execute(COVERING_INDEX_SQL)
    legacy.conn.execute("DROP INDEX IF EXISTS idx_transaction_splits_category_tx")
    legacy.conn.execute("DROP TRIGGER IF EXISTS validate_transactions_insert")
    legacy.conn.commit()
    legacy.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    legacy.conn.close()
    return path


def snapshot(connection):
    return {
        table: [tuple(row) for row in connection.execute(f'SELECT * FROM "{table}" ORDER BY rowid')]
        for table in PRESERVED_TABLES
    }


def search_triggers(connection):
    return {
        name: normalized_sql(
            connection.execute("SELECT sql FROM sqlite_master WHERE name=?", (name,)).fetchone()[0]
        )
        for name in LEGACY_SEARCH_SQL
    }


def assert_blocked_without_database_writes(path):
    before = path.read_bytes()
    with pytest.raises(DataProtectionError, match="(?i)(estrutura|sql|inesperad|alterad)"):
        FinanceDB(path)
    assert path.read_bytes() == before
    with closing(sqlite3.connect(path)) as reader:
        assert reader.execute(
            "SELECT value FROM settings WHERE key='retained_startup_marker'"
        ).fetchone()[0] == "unchanged"
        assert reader.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 30


def test_exact_retained_profile_upgrades_reopens_and_preserves_all_evidence(tmp_path):
    path = retained_profile(tmp_path / "retained.sqlite3")
    document_file = paths_for_database(path).attachments / "synthetic.txt"
    document = document_file.read_bytes()
    with closing(sqlite3.connect(path)) as source:
        before = snapshot(source)
        assert inspect_schema_contract(source)["ok"]
        assert source.execute("PRAGMA foreign_key_list(transactions)").fetchall() == []
        assert "AUTOINCREMENT" in source.execute(
            "SELECT sql FROM sqlite_master WHERE name='transactions'"
        ).fetchone()[0]
        for name, sql in LEGACY_SEARCH_SQL.items():
            assert search_triggers(source)[name] == normalized_sql(sql)
    expected = trusted_schema_contracts()[FinanceDB.CURRENT_SCHEMA_VERSION]
    for _ in range(2):
        opened = FinanceDB(path)
        try:
            assert opened.schema_version() == FinanceDB.CURRENT_SCHEMA_VERSION
            assert snapshot(opened.conn) == before
            assert inspect_schema_contract(opened.conn, allow_legacy=False)["ok"]
            assert financial_integrity_report(opened.conn)["ok"]
            assert search_triggers(opened.conn) == {
                name: expected[name]["sql"] for name in LEGACY_SEARCH_SQL
            }
            for name, item in expected.items():
                if item["kind"] == "trigger" and name.startswith("validate_"):
                    actual = opened.conn.execute(
                        "SELECT sql FROM sqlite_master WHERE name=?", (name,)
                    ).fetchone()
                    assert actual is not None and normalized_sql(actual[0]) == item["sql"]
            assert normalized_sql(opened.conn.execute(
                "SELECT sql FROM sqlite_master WHERE name='idx_transaction_splits_category_tx'"
            ).fetchone()[0]) == expected["idx_transaction_splits_category_tx"]["sql"]
            assert opened.conn.execute("PRAGMA foreign_key_list(transactions)").fetchall() == []
        finally:
            opened.conn.close()
    assert document_file.read_bytes() == document


def test_refreshed_search_indexes_external_reference_and_removes_old_terms(tmp_path):
    path = retained_profile(tmp_path / "search.sqlite3")
    opened = FinanceDB(path)
    try:
        def matches(term):
            return [row[0] for row in opened.conn.execute(
                "SELECT rowid FROM transaction_search_fts WHERE transaction_search_fts MATCH ?",
                (term,),
            )]

        transaction_id = opened.conn.execute("SELECT id FROM transactions").fetchone()[0]
        assert matches("externaloriginaltoken") == [transaction_id]
        opened.conn.execute(
            "UPDATE transaction_meta SET external_reference='externalreplacementtoken' WHERE transaction_id=?",
            (transaction_id,),
        )
        assert matches("externaloriginaltoken") == []
        assert matches("externalreplacementtoken") == [transaction_id]
        opened.conn.execute(
            "UPDATE transactions SET description='replacementdescriptiontoken' WHERE id=?",
            (transaction_id,),
        )
        assert matches("externalreplacementtoken") == [transaction_id]
        assert matches("replacementdescriptiontoken") == [transaction_id]
        cursor = opened.conn.execute(
            "INSERT INTO transactions(date,description,amount,category,account,kind) "
            "VALUES('2026-01-06','freshinsertuniquetoken',-1.23,'Retained category','Synthetic account','expense')"
        )
        new_id = cursor.lastrowid
        assert matches("freshinsertuniquetoken") == [new_id]
        opened.conn.execute(
            "UPDATE transactions SET description='freshreplacementuniquetoken' WHERE id=?", (new_id,)
        )
        assert matches("freshinsertuniquetoken") == []
        assert matches("freshreplacementuniquetoken") == [new_id]
        opened.conn.execute(
            "INSERT INTO transaction_meta(transaction_id,external_reference) VALUES(?,'freshreferenceuniquetoken')",
            (new_id,),
        )
        assert matches("freshreferenceuniquetoken") == [new_id]
        opened.conn.rollback()
    finally:
        opened.conn.close()


@pytest.mark.parametrize("table", tuple(RETAINED_TABLE_SQL))
def test_extra_column_in_any_retained_table_is_rejected_before_writes(tmp_path, table):
    path = retained_profile(tmp_path / "changed.sqlite3")
    with closing(sqlite3.connect(path)) as source, source:
        source.execute(f'ALTER TABLE "{table}" ADD COLUMN unreviewed_payload TEXT')
    assert_blocked_without_database_writes(path)


@pytest.mark.parametrize(
    "replacement",
    [
        "CREATE TABLE query_cache(key TEXT PRIMARY KEY,value TEXT NOT NULL)",
        "CREATE TABLE query_cache(key TEXT PRIMARY KEY CHECK(length(key)>0),value TEXT NOT NULL,expires_at TEXT NOT NULL)",
        "CREATE TABLE query_cache(key TEXT PRIMARY KEY,value TEXT NOT NULL DEFAULT 'changed',expires_at TEXT NOT NULL)",
        "CREATE TABLE query_cache(key TEXT PRIMARY KEY ON CONFLICT REPLACE,value TEXT NOT NULL,expires_at TEXT NOT NULL)",
    ],
    ids=("missing-column", "added-check", "changed-default", "replace-policy"),
)
def test_retained_metadata_declarations_require_exact_constraints(tmp_path, replacement):
    declarations = dict(RETAINED_TABLE_SQL, query_cache=replacement)
    path = retained_profile(tmp_path / "metadata.sqlite3", declarations=declarations, seed_opaque=False)
    assert_blocked_without_database_writes(path)


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE TRIGGER retained_unknown AFTER INSERT ON annual_goals BEGIN UPDATE settings SET value='forged' WHERE key='retained_startup_marker'; END",
        "CREATE INDEX retained_unknown ON annual_goals(name)",
        "CREATE VIEW retained_unknown AS SELECT * FROM annual_goals",
        "CREATE TABLE retained_unknown(value TEXT)",
    ],
    ids=("trigger", "index", "view", "table"),
)
def test_retained_table_does_not_authorize_unknown_objects(tmp_path, sql):
    path = retained_profile(tmp_path / "unknown.sqlite3")
    with closing(sqlite3.connect(path)) as source, source:
        source.execute(sql)
    assert_blocked_without_database_writes(path)


@pytest.mark.parametrize("trigger", tuple(LEGACY_SEARCH_SQL))
def test_known_historical_trigger_name_does_not_authorize_extra_write(tmp_path, trigger):
    path = retained_profile(tmp_path / "forged-trigger.sqlite3")
    sql = LEGACY_SEARCH_SQL[trigger].rsplit("END", 1)[0] + (
        "UPDATE settings SET value='forged' WHERE key='retained_startup_marker'; END"
    )
    with closing(sqlite3.connect(path)) as source, source:
        source.execute(f'DROP TRIGGER "{trigger}"')
        source.execute(sql)
    assert_blocked_without_database_writes(path)


@pytest.mark.parametrize("trigger", tuple(LEGACY_SEARCH_SQL))
def test_old_search_body_is_refused_after_current_schema_upgrade(tmp_path, trigger):
    path = retained_profile(tmp_path / "old-current-trigger.sqlite3")
    opened = FinanceDB(path)
    opened.conn.close()
    with closing(sqlite3.connect(path)) as source, source:
        source.execute(f'DROP TRIGGER "{trigger}"')
        source.execute(LEGACY_SEARCH_SQL[trigger])
        report = inspect_schema_contract(source)
        assert not report["ok"] and trigger in report["drifted_objects"]
    before = path.read_bytes()
    with pytest.raises(DataProtectionError):
        FinanceDB(path)
    assert path.read_bytes() == before


def test_retained_transaction_variant_does_not_allow_changed_foreign_key(tmp_path):
    declarations = dict(RETAINED_TABLE_SQL)
    declarations["transactions"] = declarations["transactions"].rsplit(")", 1)[0] + (
        ", FOREIGN KEY(goal_id) REFERENCES goals(id) ON DELETE CASCADE)"
    )
    path = retained_profile(tmp_path / "foreign-key.sqlite3", declarations=declarations)
    assert_blocked_without_database_writes(path)


def test_migration_failure_restores_original_ledger_metadata_and_search_bodies(tmp_path, monkeypatch):
    path = retained_profile(tmp_path / "rollback.sqlite3")
    with closing(sqlite3.connect(path)) as source:
        before = snapshot(source)
        triggers = search_triggers(source)
        assert inspect_schema_contract(source)["ok"]
    assert trusted_schema_contracts()
    migration = FinanceDB._migration_reimbursement_evidence
    expected = trusted_schema_contracts()[FinanceDB.CURRENT_SCHEMA_VERSION]

    def fail_after_ddl(instance):
        assert search_triggers(instance.conn) == {
            name: expected[name]["sql"] for name in LEGACY_SEARCH_SQL
        }
        migration(instance)
        instance.conn.execute("INSERT INTO query_cache VALUES('interrupted','discard','2027-01-01')")
        raise RuntimeError("injected retained migration interruption")

    with monkeypatch.context() as patch:
        patch.setattr(FinanceDB, "_migration_reimbursement_evidence", fail_after_ddl)
        with pytest.raises(RuntimeError) as failed:
            FinanceDB(path)
        if "injected retained migration interruption" not in str(failed.value):
            raise failed.value
    with closing(sqlite3.connect(path)) as source:
        assert source.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 30
        assert snapshot(source) == before
        assert search_triggers(source) == triggers
        assert not source.execute(
            "SELECT 1 FROM sqlite_master WHERE name='reimbursement_claims'"
        ).fetchone()
        assert inspect_schema_contract(source)["ok"]
    reopened = FinanceDB(path)
    try:
        assert reopened.schema_version() == FinanceDB.CURRENT_SCHEMA_VERSION
        assert snapshot(reopened.conn) == before
    finally:
        reopened.conn.close()
