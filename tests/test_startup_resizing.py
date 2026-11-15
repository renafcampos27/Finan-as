from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from main import LiquidFinanceApp


def test_startup_window_resizes_and_reflows_immediately_on_first_show(
    tmp_path, monkeypatch
):
    import database.repo.base as repo_base
    import services.documents as document_service_module
    import utils.app_logging as app_logging

    database_path = tmp_path / "financas_pessoais.sqlite3"
    monkeypatch.setattr(repo_base, "db_path", lambda: database_path)
    monkeypatch.setattr(
        document_service_module, "attachments_dir", lambda: tmp_path / "anexos"
    )
    monkeypatch.setattr(app_logging, "configure_logging", lambda *_a, **_k: None)

    _qapp = QApplication.instance() or QApplication([])
    window = LiquidFinanceApp()
    window.repo.set_setting("backup_on_close", "0")
    window.backup_service.run_backup_on_close = lambda silent=True: None
    window.check_subscription_reminders = lambda: None

    # By default, window starts with WindowMaximized state requested
    assert (
        window.windowState() & Qt.WindowMaximized
        or window.repo.setting("window_maximized", "1") == "1"
    )

    window.show()
    QTest.qWait(100)

    # Force initial load and verify responsive layout
    dashboard = window.pages["dashboard"]
    dashboard.start_initial_load()
    QTest.qWait(100)

    # Content stack is synchronized with scroll viewport
    assert window.stack.maximumWidth() == window.content_scroll.viewport().width()

    # Simulate WindowStateChange (e.g. Maximized -> Normal or vice versa)
    state_event = QEvent(QEvent.Type.WindowStateChange)
    window.changeEvent(state_event)
    QTest.qWait(100)

    # Content size remains synchronized with viewport bounds
    assert window.stack.maximumWidth() == window.content_scroll.viewport().width()

    # Verify shutdown state saving
    saved, _err = window._save_shutdown_state()
    assert saved
    assert window.repo.setting("window_maximized", "") in {"0", "1"}
    window.close()
