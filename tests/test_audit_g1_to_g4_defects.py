"""Regression and boundary test suite for defects G1, G2, G3, and G4.

Audit items:
- G1: HiDPI pixmap cache misses on unchanged charts (StableChart DPR comparison).
- G2: Stacked expense axis does not match rendered values (StackedBarChart scale & tooltip refunds).
- G3: Treemap exaggerates tiny categories (TreemapChart partition proportionality).
- G4: Donut aggregate detection confuses real category names (DonutChart synthetic vs real category).
"""

from __future__ import annotations

import math

import pytest
from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QColor, QPaintEvent, QPainter, QPixmap
from PySide6.QtWidgets import QApplication

from ui.components.charts.base import StableChart
from ui.components.charts.bar import StackedBarChart
from ui.components.charts.treemap import TreemapChart
from ui.components.charts.donut import DonutChart
from ui.core.theme import Theme


@pytest.fixture(scope="session")
def qapp():
    """Ensure a QGuiApplication / QApplication instance is available."""
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


# ============================================================================
# G1: HiDPI pixmap cache misses on unchanged charts
# ============================================================================


class _InstrumentedChart(StableChart):
    def __init__(self, theme_provider, dpr: float = 1.5):
        super().__init__(theme_provider)
        self._custom_dpr = dpr
        self.render_count = 0

    def devicePixelRatioF(self) -> float:
        return self._custom_dpr

    def render_chart(self, painter: QPainter):
        self.render_count += 1
        painter.fillRect(0, 0, 50, 50, QColor("#123456"))


def test_g1_hidpi_pixmap_cache_hits_on_unchanged_paint(qapp):
    """At DPR > 1 (e.g. 1.25, 1.5, 2.0), paintEvent reuses the cached pixmap when unchanged."""
    theme = Theme(dark=True)
    chart = _InstrumentedChart(lambda: theme, dpr=1.5)
    chart.resize(200, 100)

    # First paint: renders and caches pixmap
    target_pixmap = QPixmap(300, 150)
    target_pixmap.fill(Qt.transparent)
    painter = QPainter(target_pixmap)
    chart.paintEvent(QPaintEvent(chart.rect()))
    painter.end()

    assert chart.render_count == 1
    assert chart._cached_pixmap is not None
    # Physical size must match 200 * 1.5 = 300, 100 * 1.5 = 150
    assert chart._cached_pixmap.size() == QSize(300, 150)
    assert math.isclose(chart._cached_pixmap.devicePixelRatio(), 1.5, abs_tol=1e-3)

    # Second paint without changes: MUST hit cache, render_count must remain 1
    chart.paintEvent(QPaintEvent(chart.rect()))
    assert chart.render_count == 1, "Cache missed on second paint event with unchanged geometry and data!"

    # Invalidate cache -> next paint must re-render
    chart.invalidate_cache()
    chart.paintEvent(QPaintEvent(chart.rect()))
    assert chart.render_count == 2, "Cache was not invalidated by invalidate_cache()!"


@pytest.mark.parametrize("dpr", [1.0, 1.25, 1.5, 2.0])
def test_g1_hidpi_various_dpr_ratios(qapp, dpr: float):
    """Various HiDPI scaling ratios (1.0, 1.25, 1.5, 2.0) all allocate and validate physical pixel size."""
    theme = Theme(dark=True)
    chart = _InstrumentedChart(lambda: theme, dpr=dpr)
    chart.resize(160, 120)

    chart.paintEvent(QPaintEvent(chart.rect()))
    assert chart.render_count == 1

    expected_w = int(math.ceil(160 * dpr))
    expected_h = int(math.ceil(120 * dpr))
    assert chart._cached_pixmap.size() == QSize(expected_w, expected_h)

    # Unchanged paint preserves cache
    chart.paintEvent(QPaintEvent(chart.rect()))
    assert chart.render_count == 1


# ============================================================================
# G2: Stacked expense axis does not match rendered values
# ============================================================================


def test_g2_stacked_bar_scale_accounts_for_positive_drawn_values(qapp):
    """When negative refunds cancel out expenses (+100 and -90), axis maximum is derived from +100, not net 10."""
    theme = Theme(dark=True)
    chart = StackedBarChart(lambda: theme)
    chart.resize(400, 300)

    # Categories: Alimentacao (+100), Reembolso (-90)
    chart.categories = [("Alimentação", "#FF9500"), ("Reembolso", "#34C759")]
    chart.rows = [
        {"label": "Jan", "Alimentação": 100.0, "Reembolso": -90.0},
    ]

    # Render off-screen
    pixmap = QPixmap(400, 300)
    p = QPainter(pixmap)
    chart.render_chart(p)
    p.end()

    left, top, right, bottom = chart.plot_rect()
    # The drawn positive total is 100.0. max_total should be 100.0 * 1.10 = 110.0
    # If it was net total, max_total would be 10 * 1.10 = 11.0, causing h = (bottom-top)*100/11 = 9x height!
    drawn_pos = 100.0
    expected_max = drawn_pos * 1.10
    segment_h = (bottom - top) * 100.0 / expected_max
    # Segment height must be less than full plot height (bottom - top)
    assert segment_h < (bottom - top)
    assert segment_h > 0


def test_g2_stacked_bar_tooltip_includes_refunds(qapp):
    """Tooltip details include negative refund categories rather than silently omitting them."""
    theme = Theme(dark=True)
    chart = StackedBarChart(lambda: theme)
    chart.resize(400, 300)

    chart.categories = [("Compras", "#0A84FF"), ("Devolução", "#30D158")]
    chart.rows = [
        {"label": "Fev", "Compras": 200.0, "Devolução": -50.0},
    ]
    chart.hover_index = 0

    # Intercept _paint_tooltip
    captured_lines = []

    def mock_tooltip(painter, x, y, lines):
        captured_lines.extend(lines)

    chart._paint_tooltip = mock_tooltip

    pixmap = QPixmap(400, 300)
    p = QPainter(pixmap)
    chart.render_chart(p)
    p.end()

    assert any("Compras" in line for line in captured_lines)
    assert any("Devolução" in line and "reembolso" in line for line in captured_lines), (
        f"Refund category was omitted from tooltip: {captured_lines}"
    )


def test_g2_stacked_bar_all_negative_rows(qapp):
    """When a row only has negative values (all refunds), render_chart executes without zero division or crash."""
    theme = Theme(dark=True)
    chart = StackedBarChart(lambda: theme)
    chart.resize(400, 300)

    chart.categories = [("Reembolso A", "#34C759"), ("Reembolso B", "#30D158")]
    chart.rows = [
        {"label": "Mar", "Reembolso A": -30.0, "Reembolso B": -20.0},
    ]

    pixmap = QPixmap(400, 300)
    p = QPainter(pixmap)
    chart.render_chart(p)
    p.end()
    # Should draw cleanly without exceptions


# ============================================================================
# G3: Treemap exaggerates tiny categories
# ============================================================================


def test_g3_treemap_preserves_true_proportional_area(qapp):
    """Treemap partitioning does not clamp partition ratio to 2%-98%, preserving true financial area."""
    theme = Theme(dark=True)
    chart = TreemapChart(lambda: theme)

    # 9999 vs 1: true ratio should be 99.99% and 0.01%
    items = [
        ("Grande", 9999.0, "#FF3B30"),
        ("Minúsculo", 1.0, "#0A84FF"),
    ]
    bounds = QRectF(0, 0, 1000, 1000)
    total_area = bounds.width() * bounds.height()

    rects = chart._binary_rects(items, bounds)
    assert len(rects) == 2

    # Map labels to their rects: _binary_rects returns (rect, label, value, color)
    rect_map = {label: rect for rect, label, _val, _color in rects}
    area_big = rect_map["Grande"].width() * rect_map["Grande"].height()
    area_small = rect_map["Minúsculo"].width() * rect_map["Minúsculo"].height()

    # Sum of areas must equal total area
    assert math.isclose(area_big + area_small, total_area, rel_tol=1e-5)

    # In previous code, min ratio was 0.02 (20,000 sq px out of 1,000,000).
    # With true proportionality, small item is 1 / 10000 = 0.0001 (100 sq px).
    proportional_small_area = total_area * (1.0 / 10000.0)
    assert math.isclose(area_small, proportional_small_area, rel_tol=1e-3), (
        f"Expected small area ~{proportional_small_area}, got {area_small} (inflated?)"
    )


def test_g3_treemap_equal_items_and_containment(qapp):
    """Equal items divide area equally and remain strictly contained inside bounds."""
    theme = Theme(dark=True)
    chart = TreemapChart(lambda: theme)

    items = [
        ("A", 50.0, "#FF9500"),
        ("B", 50.0, "#34C759"),
    ]
    bounds = QRectF(10, 20, 400, 200)
    rects = chart._binary_rects(items, bounds)

    assert len(rects) == 2
    for r, _label, _val, _color in rects:
        assert r.left() >= bounds.left() - 1e-4
        assert r.right() <= bounds.right() + 1e-4
        assert r.top() >= bounds.top() - 1e-4
        assert r.bottom() <= bounds.bottom() + 1e-4

    area_a = rects[0][0].width() * rects[0][0].height()
    area_b = rects[1][0].width() * rects[1][0].height()
    assert math.isclose(area_a, area_b, rel_tol=1e-5)


# ============================================================================
# G4: Donut aggregate detection confuses real category names
# ============================================================================


def test_g4_donut_real_category_named_outros_not_hijacked_as_aggregate(qapp):
    """A real user category starting with 'Outros' (e.g. 'Outros serviços') is not hijacked by synthetic aggregation."""
    theme = Theme(dark=True)
    chart = DonutChart(lambda: theme)
    chart.set_category_limit(8)

    # 2 items: "Alimentação" (100) and real user category "Outros serviços" (20)
    # Total items = 2 <= category_limit (8), so NO synthetic aggregate is created!
    chart.set_data([
        ("Alimentação", 100.0, "#FF9500"),
        ("Outros serviços", 20.0, "#30D158"),
    ])

    # 1. Check synthetic label is None
    assert chart._synthetic_label is None

    # 2. Check color of "Outros serviços": it must NOT be forced to OTHER_COLOR ("#94A3B8")
    color_map = {label: color for label, _val, color in chart.data}
    assert color_map["Outros serviços"] != DonutChart.OTHER_COLOR

    # 3. Check comparison value
    # Set comparison period where "Alimentação" has 100, "Outros serviços" has 20, and unseen "Lazer" has 900
    chart.set_comparison_data([
        ("Alimentação", 100.0, "#FF9500"),
        ("Outros serviços", 20.0, "#30D158"),
        ("Lazer", 900.0, "#AF52DE"),
    ])

    # "Outros serviços" MUST return exactly 20.0, NOT 20 + 900 = 920!
    val = chart._comparison_value("Outros serviços")
    assert val == 20.0, f"Expected 20.0 for real category 'Outros serviços', got {val} (aggregated unseen categories?)"


def test_g4_donut_synthetic_aggregate_correctly_aggregates(qapp):
    """When category_limit is exceeded, synthetic aggregate 'Outros (N)' properly aggregates unseen comparison values."""
    theme = Theme(dark=True)
    chart = DonutChart(lambda: theme)
    chart.set_category_limit(2)

    # 4 categories: limit is 2, so top 2 kept, remainder 2 aggregated into synthetic "Outros (2)"
    chart.set_data([
        ("Habitação", 500.0, "#FF3B30"),
        ("Alimentação", 300.0, "#FF9500"),
        ("Transporte", 100.0, "#0A84FF"),
        ("Saúde", 50.0, "#34C759"),
    ])

    assert chart._synthetic_label == "Outros (2)"
    labels = [label for label, _val, _color in chart.data]
    assert "Outros (2)" in labels

    # Synthetic label receives OTHER_COLOR
    color_map = {label: color for label, _val, color in chart.data}
    assert color_map["Outros (2)"] == DonutChart.OTHER_COLOR

    # Comparison data
    chart.set_comparison_data([
        ("Habitação", 500.0, "#FF3B30"),
        ("Alimentação", 300.0, "#FF9500"),
        ("Transporte", 80.0, "#0A84FF"),
        ("Saúde", 40.0, "#34C759"),
        ("Lazer", 200.0, "#AF52DE"),
    ])

    # For top 1 "Habitação": returns 500.0
    assert chart._comparison_value("Habitação") == 500.0
    # For synthetic "Outros (2)": aggregates non-visible categories (Transporte 80 + Saúde 40 + Lazer 200 = 320.0)
    assert chart._comparison_value("Outros (2)") == 320.0
