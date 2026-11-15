# -*- coding: utf-8 -*-
"""
Test Suite for Batch 21 (Defects: BUG-HIST-01, BUG-CHART-DONUT-ARC-UNDERFLOW-005, DOC-SEC-03, AUD-SIM-04)
Phase 29 Validation.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from PySide6.QtCore import QRectF
from PySide6.QtGui import QImage, QPainter
from PySide6.QtWidgets import QApplication

from database.engine import FinanceDB
from database.repo import Repo
from motores_matematicos_simulacao.crisis_backtester_and_architecture_engine import (
    HISTORICAL_CRISES,
    CrisisScenario,
    generate_market_path,
)
from services.documents import resolve_managed_attachment_path
from ui.components.charts.donut import DonutChart
from ui.core.smart_table import SORT_VALUE_ROLE
from ui.core.theme import Theme
from ui.pages.history import HistoryPage


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture
def temp_db(tmp_path: Path):
    db_path = tmp_path / "test_batch21.db"
    db = FinanceDB(db_path)
    db.init_schema()
    return db


@pytest.fixture
def repo(temp_db):
    return Repo(temp_db)


@pytest.fixture
def mock_app(repo):
    app = MagicMock()
    app.theme = Theme()
    app.theme.privacy = False
    app.theme.dark = False
    app.page_density = "normal"
    app.repo = repo
    app.content_scroll.viewport().width.return_value = 1200
    app.background_task_registry = MagicMock()
    app.change_journal_service = MagicMock()
    app.document_service = MagicMock()
    return app


# ============================================================================
# BUG-HIST-01: History table sorting in privacy mode
# ============================================================================


def test_hist_01_privacy_mode_sorting_preserves_numeric_order(qapp, mock_app):
    """Ensure HistoryPage table sets sort values explicitly so sorting works in privacy mode."""
    mock_app.theme.privacy = True
    page = HistoryPage(mock_app)

    # Mock analytics_service category_totals
    page.analytics_service = MagicMock()
    page.analytics_service.category_totals.return_value = [
        ("Alimentação", 450.0, "#EF4444"),
        ("Transportes", 120.0, "#3B82F6"),
        ("Habitação", 850.0, "#10B981"),
    ]
    page.repo.category_budgets = MagicMock(return_value={
        "Alimentação": 500.0,
        "Transportes": 150.0,
        "Habitação": 800.0,
    })

    page.refresh()

    assert page.table.rowCount() == 3

    # Check that visible text is masked by privacy mode
    item_food = page.table.item(0, 1)
    item_trans = page.table.item(1, 1)
    item_house = page.table.item(2, 1)

    assert "•" in item_food.text()
    assert "•" in item_trans.text()
    assert "•" in item_house.text()

    # Check that sort values preserve the real numeric values
    assert item_food.data(SORT_VALUE_ROLE) == 450.0
    assert item_trans.data(SORT_VALUE_ROLE) == 120.0
    assert item_house.data(SORT_VALUE_ROLE) == 850.0

    # Verify that comparing item_trans (120) with item_food (450) evaluates < True
    assert item_trans < item_food
    assert item_food < item_house
    assert not (item_house < item_trans)


# ============================================================================
# BUG-CHART-DONUT-ARC-UNDERFLOW-005: DonutChart degenerate geometry guard
# ============================================================================


def test_donut_05_degenerate_geometry_guard(qapp):
    """Ensure DonutChart does not draw degenerate arcs when resized to tiny dimensions."""
    chart = DonutChart(lambda: Theme())
    chart.data = [("A", 100.0, "#EF4444"), ("B", 200.0, "#3B82F6")]
    chart.resize(20, 20)

    img = QImage(20, 20, QImage.Format_ARGB32_Premultiplied)
    painter = QPainter(img)
    try:
        # Must execute cleanly without exception
        chart.render_chart(painter)

        # Also test _draw_comparison_ring with tiny rect
        tiny_rect = QRectF(0, 0, 1.0, 1.0)
        chart._draw_comparison_ring(painter, tiny_rect, stroke=20.0, progress=1.0)
    finally:
        painter.end()


# ============================================================================
# DOC-SEC-03: POSIX path resolution without drive letter on Windows
# ============================================================================


def test_doc_sec_03_posix_path_without_drive_letter(tmp_path: Path):
    """Ensure paths starting with / or \\ are treated as absolute and rejected if outside root."""
    attachments_root = tmp_path / "attachments"
    attachments_root.mkdir()

    # 1. Path starting with / (e.g. /Windows/Temp or /etc/shadow)
    resolved_slash = resolve_managed_attachment_path(attachments_root, "/Windows/Temp")
    assert resolved_slash is None

    # 2. Path starting with \\
    resolved_bslash = resolve_managed_attachment_path(attachments_root, r"\Windows\System32")
    assert resolved_bslash is None

    # 3. Valid relative path inside root
    tx_folder = attachments_root / "42"
    tx_folder.mkdir()
    doc_file = tx_folder / "recibo.pdf"
    doc_file.write_text("dummy", encoding="utf-8")

    resolved_rel = resolve_managed_attachment_path(attachments_root, "42/recibo.pdf")
    assert resolved_rel == doc_file.resolve()

    # 4. Traversal attempt
    resolved_traversal = resolve_managed_attachment_path(attachments_root, "../secret.txt")
    assert resolved_traversal is None


# ============================================================================
# AUD-SIM-04: Synthetic crisis trajectory respects full duration
# ============================================================================


def test_sim_04_synthetic_crisis_respects_full_duration():
    """Ensure crisis trajectories extend through scenario.duration_months even when recovery is shorter."""
    dotcom = HISTORICAL_CRISES[0]  # duration=60, recovery=55, drop=-0.491
    assert dotcom.duration_months == 60
    assert dotcom.recovery_months == 55

    prices = generate_market_path(dotcom)

    # 60 months of simulation means 61 points (month 0 to 60)
    assert len(prices) == 61
    assert prices[0] == 100.0

    # At trough (month 30), price is at bottom (~50.9)
    assert prices[30] < 60.0

    # At recovery (month 55), price reaches 100.0
    assert prices[55] >= 99.0

    # In post-recovery (months 56 to 60), prices grow beyond 100.0
    assert prices[60] > prices[55]
    assert prices[60] > 100.0


def test_sim_04_custom_duration_longer_than_recovery():
    """Ensure custom scenario with longer duration than recovery does not truncate."""
    scenario = CrisisScenario(
        name="Test Crash",
        duration_months=36,
        trough_month=6,
        peak_to_trough_drop=-0.30,
        recovery_months=18,
    )
    prices = generate_market_path(scenario)
    assert len(prices) == 37  # month 0 to 36
    assert prices[18] >= 99.0
    assert prices[36] > prices[18]
