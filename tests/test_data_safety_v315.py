from __future__ import annotations

import json
from pathlib import Path

import pytest

from database.engine import FinanceDB
from database.protection import (
    DataProtectionGuard,
    MissingDatabaseRecoveryError,
)
from services.backup import BackupService
from services.change_journal import ChangeJournalService
from services.data_location import ensure_persistent_data_location
from utils import config
from utils.config import paths_for_database
from utils.version import APP_VERSION


class _RepoStub:
    def __init__(self, database: FinanceDB) -> None:
        self.db = database


def _counts(database: FinanceDB) -> tuple[int, int, int, int]:
    return tuple(
        int(database.scalar(f'SELECT COUNT(*) FROM "{table}"'))
        for table in ("transactions", "subscriptions", "holdings", "goals")
    )


def test_new_production_profile_contains_no_invented_finances(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FINANCAS_PESSOAIS_DEMO_DATA", raising=False)
    database = FinanceDB(tmp_path / "empty.sqlite3")
    try:
        assert _counts(database) == (0, 0, 0, 0)
        assert int(database.scalar("SELECT COUNT(*) FROM categories")) == 16
    finally:
        database.conn.close()


def test_demo_mode_is_explicit_deterministic_and_never_duplicates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FINANCAS_PESSOAIS_DEMO_DATA", "1")
    path = tmp_path / "demo.sqlite3"
    first = FinanceDB(path)
    original_counts = _counts(first)
    supermarket_amounts = [
        float(row["amount"])
        for row in first.query(
            "SELECT amount FROM transactions WHERE description='Supermercado' ORDER BY id"
        )
    ]
    assert original_counts == (90, 5, 5, 1)
    # Data-integrity semantics: the trigger now blocks deleting categories
    # that still have transactions (merge instead). The idempotent re-open
    # contract is preserved: closing and reopening the same profile keeps
    # every count and value byte-identical, with no duplicate seeding.
    first.conn.close()

    reopened = FinanceDB(path)
    try:
        assert _counts(reopened) == original_counts
        assert [
            float(row["amount"])
            for row in reopened.query(
                "SELECT amount FROM transactions WHERE description='Supermercado' ORDER BY id"
            )
        ] == supermarket_amounts
    finally:
        reopened.conn.close()


def test_missing_established_database_requires_recovery_or_explicit_empty_choice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FINANCAS_PESSOAIS_DEMO_DATA", raising=False)
    path = tmp_path / "profile" / "financas.sqlite3"
    database = FinanceDB(path)
    database.conn.close()
    paths = paths_for_database(path)
    paths.attachments.mkdir(parents=True, exist_ok=True)
    proof = paths.attachments / "documento.pdf"
    proof.write_bytes(b"documento existente")
    path.unlink()

    with pytest.raises(MissingDatabaseRecoveryError) as captured:
        FinanceDB(path)
    assert proof in captured.value.evidence

    guard = DataProtectionGuard(
        path,
        supported_schema_version=FinanceDB.CURRENT_SCHEMA_VERSION,
        current_app_version=APP_VERSION,
        attachments_directory=paths.attachments,
        history_directory=paths.history,
        preferences_file=paths.backup_preferences,
        protection_directory=paths.protections,
    )
    guard.authorize_empty_profile_once()
    recovered = FinanceDB(path)
    try:
        assert _counts(recovered) == (0, 0, 0, 0)
        assert proof.read_bytes() == b"documento existente"
    finally:
        recovered.conn.close()


def test_stable_data_location_routes_missing_database_to_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = tmp_path / "estado"
    legacy = tmp_path / "programa_vazio"
    legacy.mkdir()
    monkeypatch.setenv("FINANCAS_PESSOAIS_STATE_DIR", str(state))
    monkeypatch.setattr(config, "DB_NAME", config.DEFAULT_DB_NAME)
    paths = config.paths_for_database()
    paths.backups.mkdir(parents=True, exist_ok=True)
    candidate = paths.backups / "backup_existente.sqlite3"
    source = FinanceDB(tmp_path / "origem" / "source.sqlite3")
    source.backup_to(candidate)
    source.conn.close()

    with pytest.raises(MissingDatabaseRecoveryError) as captured:
        ensure_persistent_data_location(
            APP_VERSION,
            legacy_root=legacy,
            paths=paths,
        )
    assert candidate in captured.value.candidates


def test_startup_recovery_restores_a_valid_legacy_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FINANCAS_PESSOAIS_DEMO_DATA", raising=False)
    source = tmp_path / "source" / "backup.sqlite3"
    original = FinanceDB(source)
    original.add_transaction(
        "2026-08-11",
        "Movimento recuperado",
        -42.5,
        "Diversos",
        "Conta principal",
        "expense",
    )
    original.conn.close()

    target = tmp_path / "target" / "financas.sqlite3"
    preserved = BackupService.restore_missing_profile(
        source,
        database_file=target,
        supported_schema_version=FinanceDB.CURRENT_SCHEMA_VERSION,
    )
    assert preserved is None
    restored = FinanceDB(target)
    try:
        assert (
            int(
                restored.scalar(
                    "SELECT COUNT(*) FROM transactions WHERE description=?",
                    ("Movimento recuperado",),
                )
            )
            == 1
        )
    finally:
        restored.conn.close()


def test_tampered_undo_cannot_move_files_outside_managed_directories(
    tmp_path: Path,
) -> None:
    database = FinanceDB(tmp_path / "journal.sqlite3")
    journal = ChangeJournalService(
        _RepoStub(database),
        attachments_directory=tmp_path / "anexos",
        journal_storage=tmp_path / "historico",
    )
    cursor = database.conn.execute(
        "INSERT INTO change_journal(action_type,label,payload_json,status) "
        "VALUES('delete','Adulterado','{}','available')"
    )
    journal_id = int(cursor.lastrowid)
    outside = tmp_path / "fora" / "documento.txt"
    stash = tmp_path / "historico" / str(journal_id) / "documento.txt"
    stash.parent.mkdir(parents=True, exist_ok=True)
    stash.write_text("não mover", encoding="utf-8")
    payload = {
        "undo_kind": "restore_deleted",
        "snapshot": {"transaction_ids": []},
        "files": [{"source": str(outside), "stash": str(stash)}],
    }
    database.conn.execute(
        "UPDATE change_journal SET payload_json=? WHERE id=?",
        (json.dumps(payload), journal_id),
    )
    database.conn.commit()
    try:
        with pytest.raises(RuntimeError, match="pastas autorizadas"):
            journal.undo_latest()
        assert stash.read_text(encoding="utf-8") == "não mover"
        assert not outside.exists()
    finally:
        database.conn.close()


def test_tampered_undo_rejects_unknown_database_columns(tmp_path: Path) -> None:
    database = FinanceDB(tmp_path / "columns.sqlite3")
    journal = ChangeJournalService(
        _RepoStub(database),
        attachments_directory=tmp_path / "anexos",
        journal_storage=tmp_path / "historico",
    )
    database.add_transaction(
        "2026-08-11",
        "Original",
        -10,
        "Diversos",
        "Conta principal",
        "expense",
    )
    transaction_id = int(
        database.scalar(
            "SELECT id FROM transactions WHERE description='Original' ORDER BY id DESC LIMIT 1"
        )
    )
    row = dict(
        database.conn.execute(
            "SELECT * FROM transactions WHERE id=?",
            (transaction_id,),
        ).fetchone()
    )
    row['description", amount=999 WHERE id=1 --'] = "adulterado"
    payload = {
        "undo_kind": "restore_existing",
        "snapshot": {
            "transaction_ids": [transaction_id],
            "transactions": [row],
        },
    }
    database.conn.execute(
        "INSERT INTO change_journal(action_type,label,payload_json,status) "
        "VALUES('edit','Adulterado',?,'available')",
        (json.dumps(payload),),
    )
    database.conn.commit()
    try:
        with pytest.raises(RuntimeError, match="campos inválidos"):
            journal.undo_latest()
        description = database.conn.execute(
            "SELECT description FROM transactions WHERE id=?",
            (transaction_id,),
        ).fetchone()[0]
        assert str(description) == "Original"
    finally:
        database.conn.close()


def test_legacy_v310_database_upgrades_without_operational_error(
    tmp_path: Path,
) -> None:
    import sqlite3

    db_file = tmp_path / "legacy_v310_test.sqlite3"
    # Build a genuine supported historical profile from source-owned DDL.
    # A hand-written subset claiming nine migrations lacks mandatory fields.
    legacy = FinanceDB.__new__(FinanceDB)
    legacy.conn = sqlite3.connect(db_file)
    legacy.conn.row_factory = sqlite3.Row
    legacy.path = db_file
    legacy.attachments_directory = tmp_path / "anexos"
    legacy._closed = False
    legacy._read_only = False
    legacy._startup_progress = None
    legacy.init_schema()

    class ReachedVersion(Exception):
        pass

    def stop(version):
        if version == 9:
            raise ReachedVersion

    legacy._schema_contract_observer = stop
    try:
        with pytest.raises(ReachedVersion):
            legacy._apply_pending_migrations()
        legacy.conn.execute("INSERT INTO categories(name,kind,colour,budget) VALUES('Legacy category','expense','#123456',0)")
        legacy.conn.execute("INSERT INTO transactions(date,description,amount,category,account,kind) VALUES('2026-01-05','Original legacy marker',-12.34,'Legacy category','Conta principal','expense')")
        legacy.conn.commit()
    finally:
        legacy.conn.close()

    db = FinanceDB(db_file)
    try:
        assert db.schema_version() == FinanceDB.CURRENT_SCHEMA_VERSION
        columns = [row["name"] for row in db.query("PRAGMA table_info(transactions)")]
        assert "account_id" in columns
        assert "category_id" in columns
        assert db.scalar("SELECT amount FROM transactions WHERE description='Original legacy marker'") == -12.34
    finally:
        db.close()
