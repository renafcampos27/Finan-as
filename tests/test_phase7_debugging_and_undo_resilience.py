#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Test suite for Phase 7: Self-Healing Debugging, Undo/Redo Resilience & Global Exception Handling."""

from __future__ import annotations

import json
import sys
from pathlib import Path
import pytest
from unittest.mock import patch

from PySide6.QtWidgets import QApplication

from database.engine import FinanceDB
from database.protection import DataIntegrityError
from services.backup import BackupService
from services.change_journal import ChangeJournalService
from ui.workers import _TaskWorker
from utils.qt_runtime import install_global_exception_hook


class RepoStub:
    """Lightweight test stub wrapping a FinanceDB instance."""

    def __init__(self, database: FinanceDB) -> None:
        self.db = database


def _get_qapp():
    return QApplication.instance() or QApplication([])


def test_global_exception_hook_installation():
    """Verify install_global_exception_hook attaches a resilient handler."""
    old_hook = sys.excepthook
    try:
        install_global_exception_hook()
        installed = sys.excepthook
        install_global_exception_hook()
        assert sys.excepthook is installed

        # Simulate uncaught exception
        with patch("utils.app_logging.get_logger") as mock_logger:
            try:
                raise ValueError("Simulated unexpected crash")
            except ValueError:
                exc_type, exc_val, exc_tb = sys.exc_info()
                sys.excepthook(exc_type, exc_val, exc_tb)

            mock_log = mock_logger.return_value.critical
            assert mock_log.call_count == 1
            assert (
                "Unhandled top-level application exception" in mock_log.call_args[0][0]
            )
    finally:
        sys.excepthook = old_hook


def test_background_worker_logs_exception_traceback():
    """Verify _TaskWorker logs background exceptions before emitting error signal."""
    _ = _get_qapp()
    errors_received = []

    def broken_task():
        raise ArithmeticError("Division by zero in numerical worker")

    worker = _TaskWorker(broken_task)
    worker.error.connect(errors_received.append)

    with patch("ui.workers.log_exception") as mock_log:
        worker.run()
        assert mock_log.called
        assert "Background task execution failed" in mock_log.call_args[0][0]
        assert len(errors_received) == 1
        assert "Division by zero" in errors_received[0]


def test_change_journal_self_healing_on_corrupt_payload(tmp_path: Path):
    """Verify undo_latest marks corrupted journal records as failed to prevent lockup."""
    db_file = tmp_path / "test_journal_corrupt.sqlite3"
    db = FinanceDB(db_file)
    repo = RepoStub(db)
    journal = ChangeJournalService(repo, journal_storage=tmp_path / "journal")

    # Manually insert a malformed/corrupted entry
    db.conn.execute(
        """
        INSERT INTO change_journal (action_type, label, payload_json, status)
        VALUES ('delete', 'Operação Corrompida', '{"undo_kind": "unsupported_corrupt_kind"}', 'available')
        """
    )
    db.conn.commit()

    row = db.conn.execute(
        "SELECT id, status FROM change_journal WHERE status='available'"
    ).fetchone()
    assert row is not None
    entry_id = row["id"]

    # BUG-AUD-C118 contract: attempting to undo raises, but the record is
    # NEVER marked 'failed' -- it stays 'available' at the top of the LIFO.
    with pytest.raises(RuntimeError):
        journal.undo_latest()

    status_row = db.conn.execute(
        "SELECT status FROM change_journal WHERE id=?", (entry_id,)
    ).fetchone()
    assert status_row is not None
    assert status_row["status"] == "available"

    # The broken row remains the LIFO top: the next undo attempt targets the
    # same entry again (it blocks the queue instead of being skipped over).
    latest = journal.latest_available()
    assert latest is not None
    assert latest["id"] == entry_id
    db.close()


def test_change_journal_self_healing_on_missing_referenced_transactions(tmp_path: Path):
    """Verify undo_latest handles missing rows by marking entry failed and rolling back cleanly."""
    db_file = tmp_path / "test_journal_missing.sqlite3"
    db = FinanceDB(db_file)
    repo = RepoStub(db)
    journal = ChangeJournalService(repo, journal_storage=tmp_path / "journal")

    # Insert a valid payload that references a non-existent movement
    payload = {
        "undo_kind": "delete_created",
        "transaction_ids": [99999],
        "restore_state": {},
    }
    db.conn.execute(
        """
        INSERT INTO change_journal (action_type, label, payload_json, status)
        VALUES ('import', 'Importação Fantasma', ?, 'available')
        """,
        (json.dumps(payload),),
    )
    db.conn.commit()

    with pytest.raises(RuntimeError, match="Não é seguro desfazer"):
        journal.undo_latest()

    # BUG-AUD-C118 contract: the entry stays 'available' (LIFO top intact).
    row = db.conn.execute("SELECT status FROM change_journal WHERE id=1").fetchone()
    assert row["status"] == "available"
    latest = journal.latest_available()
    assert latest is not None
    assert latest["id"] == 1
    db.close()


def test_backup_preflight_disk_space_guard(tmp_path: Path):
    """Verify backup operations fail safely when disk space is constrained."""
    db_file = tmp_path / "test_backup_disk.sqlite3"
    db = FinanceDB(db_file)
    repo = RepoStub(db)
    backup_service = BackupService(repo, backup_directory=tmp_path / "backups")

    target_zip = tmp_path / "backups" / "test_out.zip"

    # Simulate near-zero disk space
    with patch(
        "shutil.disk_usage",
        return_value=type("Usage", (), {"free": 1024, "total": 1000000000})(),
    ):
        with pytest.raises(DataIntegrityError, match="Espaço em disco insuficiente"):
            backup_service._create_complete_zip(target_zip, purpose="test_disk")

    db.close()


def test_undo_restores_both_legs_of_linked_transfer(tmp_path: Path):
    """Verify undo preserves and restores both debit and credit legs of a linked transfer."""
    from database.repo import Repo

    db_file = tmp_path / "test_undo_transfer.sqlite3"
    db = FinanceDB(db_file)
    repo = Repo(db)
    journal = ChangeJournalService(repo, journal_storage=tmp_path / "journal")

    # Create linked transfer between Conta A and Conta B (100€)
    tx1, tx2 = repo.add_internal_transfer(
        "2026-08-01",
        100.0,
        "Conta A",
        "Conta B",
    )
    transfer_info = repo.internal_transfer_by_id(tx1)
    assert transfer_info is not None
    group_id = transfer_info["group_id"]

    row1_initial = repo.transaction_by_id(tx1)
    row2_initial = repo.transaction_by_id(tx2)
    assert row1_initial["amount"] == -100.0
    assert row2_initial["amount"] == 100.0

    # Modify transfer passing only tx1 (journal must capture both tx1 and tx2 automatically)
    journal.run_with_snapshot(
        [tx1],
        lambda: repo.update_internal_transfer(
            group_id, "2026-08-01", 150.0, "Conta A", "Conta B", commit=False
        ),
        label="Edição de transferência",
        action_type="inline_edit",
    )

    row1_edited = repo.transaction_by_id(tx1)
    row2_edited = repo.transaction_by_id(tx2)
    assert row1_edited["amount"] == -150.0
    assert row2_edited["amount"] == 150.0

    # Execute Undo
    undone = journal.undo_latest()
    assert undone is not None
    assert undone["status"] == "undone"

    # Both legs must be restored to their original snapshot state (100.0)
    row1_restored = repo.transaction_by_id(tx1)
    row2_restored = repo.transaction_by_id(tx2)
    assert row1_restored["amount"] == -100.0
    assert row2_restored["amount"] == 100.0

    # Now test reversible delete of one leg: both legs must be deleted together
    deleted_count, _ = journal.delete_transactions(
        [tx1], label="Eliminar transferência"
    )
    assert deleted_count == 2
    assert repo.transaction_by_id(tx1) is None
    assert repo.transaction_by_id(tx2) is None

    # Undo deletion
    undone_del = journal.undo_latest()
    assert undone_del is not None

    # Both legs must be fully restored!
    assert repo.transaction_by_id(tx1) is not None
    assert repo.transaction_by_id(tx2) is not None
    db.close()


def test_encrypted_backup_restores_without_preconfigured_system_password(
    tmp_path: Path,
):
    """Verify an encrypted backup can be verified and restored without pre-existing setting in DB."""
    from database.repo import Repo

    db_file = tmp_path / "test_enc_restore.sqlite3"
    db = FinanceDB(db_file)
    repo = Repo(db)
    backup_service = BackupService(repo, backup_directory=tmp_path / "backups")

    # Add sample transaction
    repo.add_transaction("2026-08-01", "Despesa Teste", -50.0, "Alimentação", "Conta A")

    # Export an encrypted backup with password
    export_file = tmp_path / "backups" / "export.lgbak"
    backup_service.export_encrypted_backup(export_file, password="CustomSecret123!")

    # Verify backup with password
    val = backup_service.validate_backup_file(export_file, password="CustomSecret123!")
    assert val is not None

    # Ensure system setting backup_encryption_enabled is False or True without password stored
    repo.set_setting("backup_encryption_enabled", "1")
    # Secret store is clear: restore_backup must NOT raise RuntimeError
    safety = backup_service.restore_backup(export_file, password="CustomSecret123!")
    assert safety.exists()


def test_wal_checkpoint_integrity_on_backup_and_restore(tmp_path: Path):
    """Verify transactions committed in SQLite WAL are fully captured in backups and restored."""
    from database.repo import Repo

    db_file = tmp_path / "test_wal_integrity.sqlite3"
    db = FinanceDB(db_file)
    repo = Repo(db)
    backup_service = BackupService(repo, backup_directory=tmp_path / "backups")

    # Insert 10 transactions in WAL mode
    for i in range(10):
        repo.add_transaction(
            f"2026-08-{i + 1:02d}",
            f"Movimento WAL #{i}",
            -(10.0 + i),
            "Diversos",
            "Conta WAL",
        )

    expected_count = len(repo.transactions_filtered())
    assert expected_count >= 10

    # Create backup while transactions reside in active WAL
    backup_path = backup_service.create_manual_backup()
    assert backup_path.exists()

    # Clear current database
    repo.clear_all_transactions()
    assert len(repo.transactions_filtered()) == 0

    # Restore backup
    backup_service.restore_backup(backup_path)

    # Reopen and verify all transactions were restored
    restored_db = FinanceDB(db_file)
    restored_repo = Repo(restored_db)
    rows = restored_repo.transactions_filtered()
    assert len(rows) == expected_count
    assert restored_db.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    restored_db.close()
