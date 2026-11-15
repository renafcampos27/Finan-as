from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication


def test_subscription_list_keeps_a_large_table_at_compact_heights(
    tmp_path,
    monkeypatch,
) -> None:
    import database.repo.base as repo_base
    import services.documents as document_service_module
    import utils.app_logging as app_logging

    database_path = tmp_path / "subscription-layout.sqlite3"
    monkeypatch.setattr(repo_base, "db_path", lambda: database_path)
    monkeypatch.setattr(
        document_service_module,
        "attachments_dir",
        lambda: tmp_path / "anexos",
    )
    monkeypatch.setattr(app_logging, "configure_logging", lambda *_a, **_k: None)

    from main import LiquidFinanceApp

    qapp = QApplication.instance() or QApplication([])
    window = LiquidFinanceApp()
    window.repo.set_setting("backup_on_close", "0")
    window.backup_service.run_backup_on_close = lambda silent=True: None
    window.resize(960, 640)
    window.show()
    window.goto("subscricoes")
    QTest.qWait(250)

    try:
        page = window.pages["subscricoes"]
        # The batch 5-8 search row above the card raised the effective
        # GlassCard minimum to 572 (explicit floor 560 + sizeHint growth).
        # Assert the compact-height contract instead of the exact pixel.
        assert page.list_card.minimumHeight() >= 560
        assert page.list_card.height() >= 560
        assert page.table.minimumHeight() == 500
        assert page.table.height() >= 500
        assert window.content_scroll.verticalScrollBar().maximum() > 0
    finally:
        window.auto_backup_timer.stop()
        window.pages["dashboard"].shutdown_workers()
        window.close()
        qapp.processEvents()
        window.repo.db.conn.close()
