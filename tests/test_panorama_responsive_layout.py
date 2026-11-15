from __future__ import annotations

import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtTest import QTest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication

from main import LiquidFinanceApp
from ui.components.charts.bar import MonthlyEvolutionChart, StackedExpenseChart
from ui.core.theme import Theme


@pytest.fixture(scope="module", autouse=True)
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def drain_owned_windows(qapp):
    yield
    for window in qapp.topLevelWidgets():
        if not isinstance(window, LiquidFinanceApp):
            continue
        window.close()
        deadline = time.monotonic() + 15
        while not window._database_closed and time.monotonic() < deadline:
            qapp.processEvents()
            QTest.qWait(10)
        assert window._database_closed
        window.deleteLater()
        QCoreApplication.sendPostedEvents(window, QEvent.Type.DeferredDelete)
    qapp.processEvents()


def test_chart_plot_rect_has_sufficient_bottom_margin():
    _qapp = QApplication.instance() or QApplication([])
    theme = Theme()
    evo = MonthlyEvolutionChart(lambda: theme)
    evo.resize(800, 400)
    left, top, right, bottom = evo.plot_rect()
    margin = evo.height() - bottom
    assert margin >= 48, f"Expected bottom margin >= 48, got {margin}"

    stacked = StackedExpenseChart(lambda: theme)
    stacked.resize(800, 400)
    left, top, right, bottom = stacked.plot_rect()
    margin_stacked = stacked.height() - bottom
    assert margin_stacked >= 48, f"Expected bottom margin >= 48, got {margin_stacked}"
    assert stacked.minimumHeight() >= 340


def test_panorama_page_reflows_cards_on_show_and_viewport_sync(tmp_path, monkeypatch):
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

    window.show()
    QTest.qWait(100)

    # Navigate to panorama
    window.goto("panorama")
    QTest.qWait(100)

    panorama = window.pages["panorama"]
    assert panorama.comp_card.height() >= 360, (
        f"Expected comp_card height >= 360 on open, got {panorama.comp_card.height()}"
    )
    assert panorama.composition.geometry().bottom() <= panorama.comp_card.height(), (
        "Expected chart to fit fully within card on open"
    )

    # Verify that content_grid reflows dynamically when viewport width updates
    window.resize(1920, 1080)
    window.content_scroll.resize(1650, 1000)
    window.content_scroll.viewport().resize(1650, 1000)
    panorama.resize(1650, 1000)
    window.sync_content_size()
    QTest.qWait(150)

    # In wide mode (>= 900px), comp_card should span 3 columns
    idx = panorama.content_grid.indexOf(panorama.comp_card)
    item_pos = panorama.content_grid.getItemPosition(idx)
    row, col, row_span, col_span = item_pos
    assert col_span == 3, (
        f"Expected comp_card to span 3 columns on wide screen, got {col_span}"
    )

    # In medium mode (620 <= width < 900)
    window.resize(1000, 800)
    window.content_scroll.resize(800, 700)
    window.content_scroll.viewport().resize(800, 700)
    panorama.resize(800, 700)
    window.sync_content_size()
    QTest.qWait(150)

    idx_med = panorama.content_grid.indexOf(panorama.comp_card)
    item_pos_med = panorama.content_grid.getItemPosition(idx_med)
    _row, _col, _row_span, col_span = item_pos_med
    assert col_span == 2, (
        f"Expected comp_card to span 2 columns on medium screen, got {col_span}"
    )

    window.close()
