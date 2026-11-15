#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tests for Phase 19 - Batch 11 P2 Defect Resolutions:
- BUG-SIM-FIRE-ZERO-EXPENSES-001: Simulação FIRE com Despesas Zero Rotulada como "Inatingível"
- BUG-PANORAMA-DIRTY-YEAR-PARSE-002: ValueError ao extrair ano com espaços ou formatação no combobox
- BUG-PANORAMA-NEGATIVE-REFUND-MAXV-003: Escala invertida em meses com reembolsos superando despesas
- BUG-CHART-HEATMAP-OVERFLOW-003: Overflow da grelha de 53 semanas em janelas estreitas (< 526 px)
"""

from __future__ import annotations

import os
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtGui import QImage, QPainter
from PySide6.QtWidgets import QApplication

from ui.components import CompactProgressPill, MetricCard
from ui.components.charts.heatmap import HeatmapChart
from ui.pages.panorama import PanoramaPage
from ui.pages.simulations import HabitSimulationsPage


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


class DummyTheme:
    def __init__(self, dark: bool = True, privacy: bool = False):
        self.dark = dark
        self.privacy = privacy
        self.muted = "#8b949e"
        self.text = "#c9d1d9"
        self.background = "#0d1117"
        self.card_bg = "#161b22"
        self.border = "#30363d"


def create_mock_app(tmp_path):
    mock_app = MagicMock()
    mock_app.theme = DummyTheme(dark=True, privacy=False)
    mock_app.page_density = "normal"
    mock_app.content_scroll.viewport().width.return_value = 1280
    mock_app.content_scroll.verticalScrollBar().value.return_value = 0
    mock_app.repo.db.path = tmp_path / "test.db"
    mock_app.repo.setting.side_effect = lambda k, default="": str(default)
    mock_app.repo.saved_filters.return_value = []
    mock_app.repo.monthly_snapshot_notes.return_value = ""
    mock_app.repo.month_period.return_value = ("2026-01-01", "2026-01-31", "2026-01")
    mock_app.repo.available_years.return_value = [2024, 2025, 2026]
    return mock_app


# ============================================================================
# 1. BUG-SIM-FIRE-ZERO-EXPENSES-001 Tests
# ============================================================================


def test_fire_zero_expenses_with_positive_invested(qapp, tmp_path):
    """When expenses <= 0 and invested > 0, FIRE milestones should display '✅ Objetivo Atingido'."""
    mock_app = create_mock_app(tmp_path)

    with patch.object(HabitSimulationsPage, "refresh", lambda self: None):
        page = HabitSimulationsPage(mock_app)

    # Set expenses to 0 and capital to 50,000 €
    page.fire_capital_spin.setValue(50000.0)
    page.fire_expenses_spin.setValue(0.0)
    page.fire_savings_spin.setValue(1000.0)
    page.fire_growth_spin.setValue(5.0)
    page.fire_swr_spin.setValue(4.0)

    page._update_fire_calculations()

    # Verify that all milestone cards in grid display "✅ Objetivo Atingido"
    assert page.fire_milestones_grid.count() > 0
    for i in range(page.fire_milestones_grid.count()):
        item = page.fire_milestones_grid.itemAt(i)
        card = item.widget()
        assert isinstance(card, MetricCard)
        # Verify delta_label text contains "✅ Objetivo Atingido" and "100%"
        assert "✅ Objetivo Atingido" in card.delta_label.text()
        assert "100%" in card.delta_label.text()
        # Must NOT say "Inatingível"
        assert "Inatingível" not in card.delta_label.text()


def test_fire_zero_expenses_with_zero_invested(qapp, tmp_path):
    """When expenses <= 0 and invested <= 0, FIRE milestones should prompt for expenses."""
    mock_app = create_mock_app(tmp_path)

    with patch.object(HabitSimulationsPage, "refresh", lambda self: None):
        page = HabitSimulationsPage(mock_app)

    # Set both capital and expenses to 0
    page.fire_capital_spin.setValue(0.0)
    page.fire_expenses_spin.setValue(0.0)
    page.fire_savings_spin.setValue(0.0)
    page.fire_growth_spin.setValue(5.0)
    page.fire_swr_spin.setValue(4.0)

    page._update_fire_calculations()

    assert page.fire_milestones_grid.count() > 0
    for i in range(page.fire_milestones_grid.count()):
        item = page.fire_milestones_grid.itemAt(i)
        card = item.widget()
        assert isinstance(card, MetricCard)
        assert "Indica as despesas" in card.delta_label.text()
        assert "Inatingível" not in card.delta_label.text()


def test_fire_calculations_clears_layout_recursively(qapp, tmp_path):
    """Verify that multiple updates don't leak orphaned widgets in fire_milestones_grid."""
    mock_app = create_mock_app(tmp_path)

    with patch.object(HabitSimulationsPage, "refresh", lambda self: None):
        page = HabitSimulationsPage(mock_app)

    # Trigger calculation three times
    for _ in range(3):
        page._update_fire_calculations()

    # The grid should have exactly 4 cards (standard, lean, fat, coast)
    assert page.fire_milestones_grid.count() == 4


# ============================================================================
# 2. BUG-PANORAMA-DIRTY-YEAR-PARSE-002 Tests
# ============================================================================


def test_panorama_selected_year_dirty_inputs(qapp, tmp_path):
    """Verify _selected_year handles whitespace, labels, and malformed text gracefully."""
    mock_app = create_mock_app(tmp_path)
    with patch.object(PanoramaPage, "refresh", lambda self: None):
        page = PanoramaPage(mock_app)

    # Test whitespace
    page.year.addItem(" 2026 ")
    page.year.setCurrentText(" 2026 ")
    assert page._selected_year() == 2026

    # Test text prefix
    page.year.addItem("Ano 2024")
    page.year.setCurrentText("Ano 2024")
    assert page._selected_year() == 2024

    # Test text suffix
    page.year.addItem("2025 (atual)")
    page.year.setCurrentText("2025 (atual)")
    assert page._selected_year() == 2025

    # Test empty or non-numeric string falls back to current year
    page.year.addItem("")
    page.year.setCurrentText("")
    assert page._selected_year() == date.today().year

    page.year.addItem("Inválido")
    page.year.setCurrentText("Inválido")
    assert page._selected_year() == date.today().year


def test_panorama_selected_period_and_annual_period_with_dirty_year(qapp, tmp_path):
    """Verify selected_period and _annual_period execute without ValueError with formatted year text."""
    mock_app = create_mock_app(tmp_path)
    with patch.object(PanoramaPage, "refresh", lambda self: None):
        page = PanoramaPage(mock_app)

    page.year.addItem(" 2026 ")
    page.year.setCurrentText(" 2026 ")
    s, e, ym, y, m = page.selected_period()
    assert y == 2026
    assert isinstance(s, str) and isinstance(e, str)

    ann_start, ann_end = page._annual_period()
    assert ann_start.startswith("2026-01-01")


# ============================================================================
# 3. BUG-PANORAMA-NEGATIVE-REFUND-MAXV-003 Tests
# ============================================================================


def test_panorama_refresh_diagnostic_with_negative_refunds(qapp, tmp_path):
    """Verify refresh_diagnostic handles months with net negative category refunds without crashing or negative scale."""
    mock_app = create_mock_app(tmp_path)
    with patch.object(PanoramaPage, "refresh", lambda self: None):
        page = PanoramaPage(mock_app)

    top_negative = [
        ("Reembolso Vestuário", -150.0, "#ff3b30"),
        ("Devolução Compras", -30.0, "#ff9500"),
    ]
    page.repo.top_categories_payment_period.return_value = top_negative
    page.analytics_service.detailed_financial_score.return_value = {
        "score": 85,
        "confidence": 95,
        "data_as_of": "2026-01-31",
        "marker": "🌟",
        "grade": "A",
        "status": "Excelente",
        "confidence_label": "Alta",
    }

    # Should not raise ZeroDivisionError or ValueError
    page.refresh_diagnostic(
        "2026-01-01",
        "2026-01-31",
        "2026-01",
        {"income": 2000.0, "expenses": -180.0, "savings": 2180.0},
        500.0,
    )

    # Layout should contain pills and progress should be clamped >= 0.0
    pills = page.diagnostic.findChildren(CompactProgressPill)
    assert len(pills) == 2
    for pill in pills:
        assert 0.0 <= pill.progress <= 1.0


def test_panorama_refresh_diagnostic_with_mixed_refunds_and_expenses(qapp, tmp_path):
    """Verify refresh_diagnostic computes maxv from positive expenses and clamps refunds to 0.0."""
    mock_app = create_mock_app(tmp_path)
    with patch.object(PanoramaPage, "refresh", lambda self: None):
        page = PanoramaPage(mock_app)

    top_mixed = [
        ("Alimentação", 300.0, "#34c759"),
        ("Devolução Equipamento", -100.0, "#ff3b30"),
    ]
    page.repo.top_categories_payment_period.return_value = top_mixed
    page.analytics_service.detailed_financial_score.return_value = {
        "score": 75,
        "confidence": 80,
        "data_as_of": "2026-01-31",
        "marker": "👍",
        "grade": "B",
        "status": "Bom",
        "confidence_label": "Média",
    }

    page.refresh_diagnostic(
        "2026-01-01",
        "2026-01-31",
        "2026-01",
        {"income": 1500.0, "expenses": 200.0, "savings": 1300.0},
        400.0,
    )

    pills = page.diagnostic.findChildren(CompactProgressPill)
    assert len(pills) == 2
    # First pill (Alimentação, 300.0 / 300.0) -> 1.0
    assert pills[0].progress == pytest.approx(1.0, abs=1e-3)
    # Second pill (Devolução, -100.0) -> 0.0 (clamped, not negative)
    assert pills[1].progress == 0.0


# ============================================================================
# 4. BUG-CHART-HEATMAP-OVERFLOW-003 Tests
# ============================================================================


def test_heatmap_chart_no_overflow_narrow_window(qapp):
    """Verify HeatmapChart scales dynamically and prevents cell overflow in narrow layouts (< 526 px)."""
    chart = HeatmapChart(lambda: DummyTheme(dark=True))

    # Generate 365 daily points
    start_dt = date(2025, 1, 1)
    points = []
    for d in range(365):
        cur = start_dt + timedelta(days=d)
        points.append({
            "date": cur.isoformat(),
            "expense": 25.0 if d % 3 == 0 else 0.0,
        })
    chart.set_data(points)

    # Test narrow widths: 380px, 420px, 480px, 520px (< 526px boundary)
    for width in (380, 420, 480, 520):
        chart.resize(width, 220)
        img = QImage(width, 220, QImage.Format_ARGB32)
        painter = QPainter(img)
        try:
            chart.render_chart(painter)
        finally:
            painter.end()

        assert len(chart.tooltip_rects) == 365
        # Assert EVERY cell rectangle fits within [0, width]
        for idx, rect, _ in chart.tooltip_rects:
            assert rect.right() <= float(width), (
                f"Cell {idx} right edge {rect.right()} exceeds chart width {width}"
            )
            assert rect.left() >= 0.0, (
                f"Cell {idx} left edge {rect.left()} is negative in chart width {width}"
            )

        # Assert weekday labels fit within chart boundaries
        for label, rect in chart.weekday_label_rects:
            assert rect.left() >= 0.0
            assert rect.right() <= float(width)


def test_heatmap_chart_standard_width(qapp):
    """Verify HeatmapChart renders normally at standard desktop width (800 px)."""
    chart = HeatmapChart(lambda: DummyTheme(dark=False))

    start_dt = date(2025, 1, 1)
    points = [{"date": (start_dt + timedelta(days=d)).isoformat(), "expense": float(d)} for d in range(365)]
    chart.set_data(points)

    chart.resize(800, 240)
    img = QImage(800, 240, QImage.Format_ARGB32)
    painter = QPainter(img)
    try:
        chart.render_chart(painter)
    finally:
        painter.end()

    assert len(chart.tooltip_rects) == 365
    for _, rect, _ in chart.tooltip_rects:
        assert rect.right() <= 800.0
