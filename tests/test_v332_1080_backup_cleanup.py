from __future__ import annotations

import json
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox, QWidget

from database.protection import DataProtectionGuard
from database.repo import Repo
from services.backup import BackupService
from services.documents import DocumentService
from ui.dialogs import TransactionDialog
from ui.startup import StartupProgressWindow, prepare_foreground_dialog


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import database.repo.base as repo_base

    database = tmp_path / "financas-v332.sqlite3"
    monkeypatch.setattr(repo_base, "db_path", lambda: database)
    instance = Repo()
    yield instance
    instance.db.conn.close()


def test_selected_primary_backup_folder_is_persisted_and_used_by_workers(
    repo: Repo,
    tmp_path: Path,
) -> None:
    custom = tmp_path / "backup-principal"
    service = BackupService(repo)

    service.save_preferences(
        0,
        5,
        False,
        False,
        False,
        "",
        str(custom),
    )

    assert service.backup_directory == custom.resolve()
    assert service.preferences()["primary_backup_directory"] == str(custom.resolve())
    reloaded = BackupService(repo)
    assert reloaded.backup_directory == custom.resolve()

    target = BackupService.run_manual_backup_for_database(Path(repo.db.path))
    assert target.parent == custom.resolve()
    assert target.is_file()
    reloaded.validate_backup_file(target)


def test_custom_backup_folder_is_considered_during_startup_recovery(
    repo: Repo,
    tmp_path: Path,
) -> None:
    custom = tmp_path / "recuperacao-personalizada"
    service = BackupService(repo)
    service.save_preferences(0, 5, False, False, False, "", str(custom))
    backup = service.create_manual_backup()
    guard = DataProtectionGuard(
        Path(repo.db.path),
        supported_schema_version=repo.db.CURRENT_SCHEMA_VERSION,
        attachments_directory=service.attachments_directory,
        history_directory=service.history_directory,
        preferences_file=service.preferences_file,
        protection_directory=service.protection_directory,
    )

    assert backup.resolve() in {
        candidate.resolve() for candidate in guard.recovery_candidates()
    }


def test_clear_all_movements_is_atomic_and_preserves_planning_data(
    repo: Repo,
    tmp_path: Path,
) -> None:
    first = repo.add_transaction(
        "2026-08-01",
        "Compra com comprovativo",
        -22.5,
        "Outros",
        "Conta principal",
    )
    second = repo.add_transaction(
        "2026-08-02",
        "Salário preservando categorias",
        1800,
        "Rendimentos",
        "Conta principal",
    )
    repo.save_transaction_splits(first, [("Outros", 100.0)])
    managed = tmp_path / "anexos"
    documents = DocumentService(repo, attachments_directory=managed)
    source = tmp_path / "fatura.pdf"
    source.write_bytes(b"%PDF-1.4\nmovimento\n%%EOF\n")
    stored = documents.attach_file(first, source)
    subscription_id = repo.db.conn.execute(
        "INSERT INTO subscriptions(name,amount,category) VALUES(?,?,?)",
        ("Serviço mantido", 12.0, "Subscrições"),
    ).lastrowid
    repo.db.conn.execute(
        "INSERT INTO subscription_occurrences(subscription_id,due_date,transaction_id) "
        "VALUES(?,?,?)",
        (subscription_id, "2026-08-01", first),
    )
    repo.db.conn.execute(
        "INSERT INTO change_journal(action_type,label,payload_json) VALUES(?,?,?)",
        ("edit", "Alteração anterior", json.dumps({"undo_kind": "delete_created"})),
    )
    repo.db.conn.commit()
    category_count = int(repo.scalar("SELECT COUNT(*) FROM categories") or 0)
    subscription_count = int(repo.scalar("SELECT COUNT(*) FROM subscriptions") or 0)
    movement_count = int(repo.scalar("SELECT COUNT(*) FROM transactions") or 0)

    report = documents.clear_all_transactions()

    assert report == {
        "movements": movement_count,
        "attachment_records": 1,
        "attachment_files": 1,
    }
    assert repo.scalar("SELECT COUNT(*) FROM transactions") == 0
    assert repo.scalar("SELECT COUNT(*) FROM transaction_meta") == 0
    assert repo.scalar("SELECT COUNT(*) FROM transaction_splits") == 0
    assert repo.scalar("SELECT COUNT(*) FROM transaction_attachments") == 0
    assert repo.scalar("SELECT COUNT(*) FROM categories") == category_count
    assert repo.scalar("SELECT COUNT(*) FROM subscriptions") == subscription_count
    occurrence_link = repo.db.conn.execute(
        "SELECT transaction_id FROM subscription_occurrences "
        "WHERE subscription_id=? AND due_date=?",
        (subscription_id, "2026-08-01"),
    ).fetchone()
    assert occurrence_link is not None
    assert occurrence_link[0] is None
    assert repo.setting("backup_on_close", "0") == "0"
    assert (
        repo.scalar("SELECT COUNT(*) FROM change_journal WHERE status='available'") == 0
    )
    assert repo.scalar("SELECT COUNT(*) FROM change_journal WHERE status='undone'") == 1
    assert not stored.exists()
    assert second > first


def test_clear_all_movements_rolls_back_before_removing_files(
    repo: Repo,
    tmp_path: Path,
) -> None:
    transaction_id = repo.add_transaction(
        "2026-08-03",
        "Movimento que deve sobreviver",
        -19.0,
        "Outros",
        "Conta principal",
    )
    documents = DocumentService(repo, attachments_directory=tmp_path / "anexos")
    source = tmp_path / "protegido.pdf"
    source.write_bytes(b"%PDF-1.4\nprotegido\n%%EOF\n")
    stored = documents.attach_file(transaction_id, source)
    repo.db.conn.executescript(
        """
        CREATE TRIGGER reject_complete_ledger_clear
        BEFORE DELETE ON transactions
        BEGIN
            SELECT RAISE(ABORT, 'simulated clear failure');
        END;
        """
    )
    repo.db.conn.commit()

    with pytest.raises(Exception, match="simulated clear failure"):
        documents.clear_all_transactions()

    assert repo.transaction_by_id(transaction_id) is not None
    assert repo.scalar("SELECT COUNT(*) FROM transaction_attachments") == 1
    assert stored.is_file()


def test_transaction_editor_is_compact_crisp_and_scroll_safe(repo: Repo) -> None:
    app = QApplication.instance() or QApplication([])
    parent = QWidget()
    parent.resize(1200, 800)
    parent.show()
    transaction_id = repo.add_transaction(
        "2026-08-04",
        "Movimento editável com descrição longa",
        -31.25,
        "Outros",
        "Conta principal",
    )
    dialog = TransactionDialog(
        repo,
        parent,
        row=repo.transaction_by_id(transaction_id),
    )
    dialog.show()
    QTest.qWait(40)
    app.processEvents()

    assert parent.graphicsEffect() is None
    assert not hasattr(dialog, "sheet")
    assert 620 <= dialog.width() <= 780
    assert 520 <= dialog.height() <= 720
    assert dialog._dialog_scroll.horizontalScrollBar().maximum() == 0
    assert dialog.heading_label.text() == "Editar movimento"
    assert len(dialog._field_labels) == 13
    assert all(label.isVisibleTo(dialog) for label in dialog._field_labels)

    dialog.close()
    parent.close()


def test_startup_warning_is_application_modal_and_topmost() -> None:
    app = QApplication.instance() or QApplication([])
    startup = StartupProgressWindow()
    startup.show()
    warning = QMessageBox(startup)
    warning.setText("Falha de recuperação")

    prepare_foreground_dialog(warning, startup)
    app.processEvents()

    assert warning.parentWidget() is startup
    assert warning.windowModality() == Qt.ApplicationModal
    assert bool(warning.windowFlags() & Qt.WindowStaysOnTopHint)
    assert warning.isVisible()
    warning.close()
    startup.dismiss()
