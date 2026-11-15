from __future__ import annotations

import sqlite3
from pathlib import Path

from database.engine import FinanceDB
from database.protection import (
    DATA_GUARD_SCHEMA_KEY,
    DATA_GUARD_VERSION_KEY,
    DataProtectionGuard,
)


def _arguments(root: Path, *, app_version: str) -> dict[str, object]:
    return {
        "app_version": app_version,
        "attachments_directory": root / "anexos",
        "history_directory": root / "historico",
        "preferences_file": root / "backup_preferences.json",
        "protection_directory": root / "protecoes_de_versao",
    }


def _downgrade_transfer_schema(
    database_file: Path,
    *,
    keep_migration_20: bool,
) -> None:
    connection = sqlite3.connect(str(database_file))
    try:
        # A genuine pre-transfer profile cannot contain later product evidence
        # or guards that reference the column this synthetic fixture removes.
        connection.execute("DROP TRIGGER IF EXISTS protect_reimbursement_ledger_delete")
        connection.execute("DROP TRIGGER IF EXISTS protect_reimbursement_ledger_update")
        connection.execute("DROP TABLE IF EXISTS reimbursement_receipts")
        connection.execute("DROP TABLE IF EXISTS reimbursement_claims")
        connection.execute("DROP TABLE IF EXISTS subscription_price_reviews")
        connection.execute("DROP TRIGGER IF EXISTS trg_transactions_cascade_transfer_leg")
        connection.execute("DROP INDEX IF EXISTS idx_transactions_transfer_group")
        connection.execute("ALTER TABLE transactions DROP COLUMN transfer_group_id")
        first_removed = 21 if keep_migration_20 else 19
        connection.execute(
            "DELETE FROM schema_migrations WHERE version>=?", (first_removed,)
        )
        source_schema = 20 if keep_migration_20 else 18
        connection.executemany(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (
                (DATA_GUARD_VERSION_KEY, "3.22.0"),
                (DATA_GUARD_SCHEMA_KEY, str(source_schema)),
            ),
        )
        connection.commit()
    finally:
        connection.close()


def test_v322_database_without_transfer_column_upgrades_and_preserves_data(
    tmp_path: Path,
) -> None:
    database_file = tmp_path / "financas.sqlite3"
    original = FinanceDB(database_file, **_arguments(tmp_path, app_version="3.22.0"))
    cursor = original.conn.execute(
        "INSERT INTO transactions(date,description,amount,category,account,kind,notes) "
        "VALUES(?,?,?,?,?,?,?)",
        (
            "2026-08-12",
            "Movimento pessoal preservado",
            -31.45,
            "Diversos",
            "Conta principal",
            "expense",
            "base restaurada da V3.22",
        ),
    )
    transaction_id = int(cursor.lastrowid)
    original.conn.commit()
    original.conn.close()
    _downgrade_transfer_schema(database_file, keep_migration_20=False)

    upgraded = FinanceDB(
        database_file,
        **_arguments(tmp_path, app_version="3.29.1"),
    )
    try:
        columns = {
            str(row["name"])
            for row in upgraded.query("PRAGMA table_info(transactions)")
        }
        row = upgraded.conn.execute(
            "SELECT description,amount,notes,transfer_group_id "
            "FROM transactions WHERE id=?",
            (transaction_id,),
        ).fetchone()
        assert "transfer_group_id" in columns
        assert tuple(row) == (
            "Movimento pessoal preservado",
            -31.45,
            "base restaurada da V3.22",
            "",
        )
        assert upgraded.schema_version() == FinanceDB.CURRENT_SCHEMA_VERSION
        assert upgraded.schema_versions() == set(range(1, FinanceDB.CURRENT_SCHEMA_VERSION + 1))
        assert (
            upgraded.scalar(
                "SELECT COUNT(*) FROM sqlite_master "
                "WHERE type='index' AND name='idx_transactions_transfer_group'"
            )
            == 1
        )
    finally:
        upgraded.conn.close()

    protections = sorted((tmp_path / "protecoes_de_versao").glob("*.zip"))
    assert protections
    manifest = DataProtectionGuard.validate_archive(protections[-1])
    assert manifest["source_app_version"] == "3.22.0"
    assert manifest["target_app_version"] == "3.29.1"


def test_repair_migration_restores_column_when_migration_20_was_recorded(
    tmp_path: Path,
) -> None:
    database_file = tmp_path / "ledger-drift.sqlite3"
    original = FinanceDB(database_file, **_arguments(tmp_path, app_version="3.22.0"))
    original.conn.close()
    _downgrade_transfer_schema(database_file, keep_migration_20=True)

    repaired = FinanceDB(
        database_file,
        **_arguments(tmp_path, app_version="3.29.1"),
    )
    try:
        columns = {
            str(row["name"])
            for row in repaired.query("PRAGMA table_info(transactions)")
        }
        assert "transfer_group_id" in columns
        # Migration 30 (monthly_category_budgets_update_cascade) is part of
        # the current schema; the repair path runs every migration up to the
        # live CURRENT_SCHEMA_VERSION.
        assert (
            repaired.schema_version()
            == FinanceDB.CURRENT_SCHEMA_VERSION
        )
        assert (
            repaired.scalar("SELECT COUNT(*) FROM schema_migrations WHERE version=23")
            == 1
        )
    finally:
        repaired.conn.close()
