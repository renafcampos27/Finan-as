from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from database.engine import FinanceDB
from database.protection import (
    DATA_GUARD_SCHEMA_KEY,
    DATA_GUARD_VERSION_KEY,
    DataIntegrityError,
    DataProtectionGuard,
)
from main import is_sqlite_corruption_error


def _arguments(root: Path, *, app_version: str) -> dict[str, object]:
    return {
        "app_version": app_version,
        "attachments_directory": root / "anexos",
        "history_directory": root / "historico",
        "preferences_file": root / "backup_preferences.json",
        "protection_directory": root / "protecoes_de_versao",
    }


def _install_and_damage_legacy_search(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE VIRTUAL TABLE transactions_fts USING fts5(
            description,
            category,
            content='transactions',
            content_rowid='id'
        );

        CREATE TRIGGER transactions_ai AFTER INSERT ON transactions BEGIN
            INSERT INTO transactions_fts(rowid,description,category)
            VALUES(NEW.id,NEW.description,NEW.category);
        END;

        CREATE TRIGGER transactions_ad AFTER DELETE ON transactions BEGIN
            INSERT INTO transactions_fts(
                transactions_fts,rowid,description,category
            ) VALUES('delete',OLD.id,OLD.description,OLD.category);
        END;

        CREATE TRIGGER transactions_au AFTER UPDATE ON transactions BEGIN
            INSERT INTO transactions_fts(
                transactions_fts,rowid,description,category
            ) VALUES('delete',OLD.id,OLD.description,OLD.category);
            INSERT INTO transactions_fts(rowid,description,category)
            VALUES(NEW.id,NEW.description,NEW.category);
        END;

        INSERT INTO transactions_fts(rowid,description,category)
        SELECT id,description,category FROM transactions;
        """
    )
    # Removing the FTS root block reproduces the real failure: ordinary SQLite
    # integrity checks still say "ok", but any movement mutation fails because
    # the obsolete trigger tries to update the damaged derived index.
    connection.execute("DELETE FROM transactions_fts_data WHERE id=1")


def test_v335_blocks_unsupported_search_overlay_without_changing_movements(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FINANCAS_PESSOAIS_DEMO_DATA", "0")
    database_file = tmp_path / "financas.sqlite3"
    original = FinanceDB(
        database_file,
        **_arguments(tmp_path, app_version="3.34.0"),
    )
    category = original.conn.execute(
        "SELECT name,stable_id FROM categories "
        "WHERE kind='expense' AND stable_id<>'' ORDER BY name LIMIT 1"
    ).fetchone()
    account = original.conn.execute(
        "SELECT name,stable_id FROM financial_accounts "
        "WHERE stable_id<>'' ORDER BY id LIMIT 1"
    ).fetchone()
    assert category is not None
    if account is None:
        original.conn.execute(
            "INSERT INTO financial_accounts(name,account_key,stable_id) VALUES(?,?,?)",
            ("Conta principal", "conta principal", "account-test-main"),
        )
        account = original.conn.execute(
            "SELECT name,stable_id FROM financial_accounts "
            "WHERE stable_id='account-test-main'"
        ).fetchone()
    assert account is not None
    cursor = original.conn.execute(
        "INSERT INTO transactions("
        "date,description,amount,category,account,kind,notes,category_id,account_id"
        ") VALUES(?,?,?,?,?,?,?,?,?)",
        (
            "2026-08-22",
            "Movimento pessoal preservado",
            -19.95,
            str(category[0]),
            str(account[0]),
            "expense",
            "não alterar",
            str(category[1]),
            str(account[1]),
        ),
    )
    transaction_id = int(cursor.lastrowid)
    original.conn.commit()
    before = tuple(
        original.conn.execute(
            "SELECT * FROM transactions WHERE id=?",
            (transaction_id,),
        ).fetchone()
    )
    _install_and_damage_legacy_search(original.conn)
    original.conn.execute("DELETE FROM schema_migrations WHERE version>=25")
    original.conn.executemany(
        "INSERT INTO settings(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (
            (DATA_GUARD_VERSION_KEY, "3.34.0"),
            (DATA_GUARD_SCHEMA_KEY, "24"),
        ),
    )
    original.conn.commit()
    original.conn.close()

    damaged = sqlite3.connect(str(database_file))
    try:
        assert damaged.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        with pytest.raises(sqlite3.DatabaseError, match="malformed"):
            damaged.execute(
                "UPDATE transactions SET description=? WHERE id=?",
                ("Falha esperada", transaction_id),
            )
        damaged.rollback()
    finally:
        damaged.close()

    # This fixture overlays an obsolete executable index on a current database
    # and edits its migration ledger. It is not a genuine historical schema.
    # The strict SQL contract must reject it before modifying personal records.
    import hashlib

    before_hash = hashlib.sha256(database_file.read_bytes()).hexdigest()
    with pytest.raises(DataIntegrityError, match="Objetos SQL inesperados"):
        FinanceDB(database_file, **_arguments(tmp_path, app_version="3.35.0"))
    assert hashlib.sha256(database_file.read_bytes()).hexdigest() == before_hash
    with sqlite3.connect(database_file) as unchanged:
        after = unchanged.execute(
            "SELECT * FROM transactions WHERE id=?", (transaction_id,)
        ).fetchone()
        assert tuple(after) == before
        assert unchanged.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name='transactions_fts'"
        ).fetchone()[0] == 1


def test_sqlite_corruption_classifier_does_not_block_transient_errors() -> None:
    assert is_sqlite_corruption_error(
        sqlite3.DatabaseError("database disk image is malformed")
    )
    assert is_sqlite_corruption_error(sqlite3.DatabaseError("FILE IS NOT A DATABASE"))
    assert not is_sqlite_corruption_error(
        sqlite3.OperationalError("database is locked")
    )
    assert not is_sqlite_corruption_error(
        RuntimeError("database disk image is malformed")
    )


def test_targeted_fts_check_reports_active_index_corruption() -> None:
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(
            """
            CREATE VIRTUAL TABLE transaction_search_fts USING fts5(search_text);
            INSERT INTO transaction_search_fts(search_text) VALUES('movimento');
            DELETE FROM transaction_search_fts_data WHERE id=1;
            """
        )
        with pytest.raises(DataIntegrityError, match="FTS5"):
            DataProtectionGuard.validate_current_search_index(connection)
    finally:
        connection.close()
