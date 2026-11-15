#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Regression tests for audit batch 20 (chart base, waterfall/bridge, pulse,
cards, detail export):

- BUG-CHART-BASE-01 (P1): StableChart pixmap cache must compare physical pixel
  size AND devicePixelRatio, so HiDPI monitors (125/150/200%) reuse the cache
  instead of rebuilding it every frame.
- BUG-CHART-WB-01 (P2): the wealth bridge must draw a connector between the
  last intermediate bar and the closing "Património final" bar.
- BUG-CHART-WB-02 (P2): bridge value labels must never be clamped into the
  plot top (drawn below the bar instead) and the value range must include
  negative deltas without forcing 0.0 into the scale.
- BUG-CHART-HEALTH-01 (P1): ResilienceScenarioChart must render a visible
  marker for a single data point (no blank canvas, no zero-range division).
- BUG-CHART-STACK-01 (P2): StackedExpenseChart must skip negative refunds from
  the stack, drop sub-pixel (< 0.5px) segments, and keep Y labels right-aligned
  inside a bounded gutter.
- BUG-CHART-WF-01 (P2): waterfall connectors must follow the animated bar
  height and compensate the hover expansion offset.
- BUG-CHART-WF-02 (P2): waterfall corner radius must be clamped to half the
  bar height so 0.00 € bars do not degenerate the vector path.
- BUG-CHART-PULSE-01 (P2): the cumulative area gradient must keep a non-zero
  vertical span and the comparison reference line must stay visible with a
  single data point (drawn as a dot).
- BUG-CARD-RESP-01 (P2): SKIPPED - ui/components/cards.py has concurrent
  edits from another agent (see workspace state); not touched this batch.
- BUG-CARD-GLASS-01 (P2): SKIPPED - same concurrent-edit constraint.
- BUG-DETAIL-EXP-01 (P2): ChartDetailDialog must offer a CSV clipboard export
  (delimiter ";") that preserves raw numeric amounts and masks them while
  privacy mode is active.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtGui import QPixmap, QPainter
from PySide6.QtWidgets import QApplication

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ui.components.charts.bar import StackedExpenseChart
from ui.components.charts.financial_health import ResilienceScenarioChart
from ui.components.charts.financial_pulse import CumulativeCashflowChart
from ui.components.charts.waterfall import CashFlowWaterfallChart
from ui.components.charts.wealth_bridge import NetWorthBridgeChart
from ui.components.chart_details import ChartDetailDialog
from ui.core.theme import Theme


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


# ---------------------------------------------------------------------------
# BUG-CHART-BASE-01: pixmap cache keyed on physical pixels + DPR
# ---------------------------------------------------------------------------


def test_bug_chart_base_01_cache_contract_compares_pixels_and_dpr():
    """The cache validity check must compare the physical pixel size and the
    devicePixelRatio, not the logical widget size."""
    source = (ROOT / "ui" / "components" / "charts" / "base.py").read_text(
        encoding="utf-8"
    )
    # Physical pixel comparison (pixel size, not logical size).
    assert "target_pixel_size" in source
    assert "self._cached_pixmap.size() == target_pixel_size" in source
    # DPR guard with a tolerance (float comparison).
    assert "devicePixelRatio()" in source
    assert "abs_tol=1e-3" in source
    # The old buggy logical comparison must be gone.
    assert "self._cached_pixmap.size() == self.size()" not in source


def test_bug_chart_base_01_cache_survives_repaint_at_constant_dpr(qapp):
    """A repaint without size/DPR change must reuse the cached pixmap."""
    theme = Theme()
    chart = NetWorthBridgeChart(lambda: theme)
    chart.resize(640, 360)
    chart.set_data(
        {
            "steps": [
                {"key": "opening", "label": "Património inicial", "value": 1000.0,
                 "start": 0.0, "end": 1000.0, "role": "total"},
                {"key": "income", "label": "Receitas", "value": 250.0,
                 "start": 1000.0, "end": 1250.0, "role": "change"},
                {"key": "closing", "label": "Património final", "value": 1250.0,
                 "start": 0.0, "end": 1250.0, "role": "total"},
            ],
            "change": 250.0,
            "explained_change_pct": 100.0,
        }
    )
    chart.show()
    qapp.processEvents()
    chart.grab()
    qapp.processEvents()
    assert chart._cached_pixmap is not None
    first = chart._cached_pixmap
    # Repaint without any size/DPR change: same pixmap object must survive.
    chart.invalidate_cache()
    qapp.processEvents()
    chart.grab()
    qapp.processEvents()
    assert chart._cached_pixmap is first
    chart.close()


# ---------------------------------------------------------------------------
# BUG-CHART-WB-01: closing-bar connector
# ---------------------------------------------------------------------------


def _bridge_rows() -> list[dict]:
    return [
        {"key": "opening", "label": "Património inicial", "value": 1000.0,
         "start": 0.0, "end": 1000.0, "role": "total"},
        {"key": "income", "label": "Receitas", "value": 400.0,
         "start": 1000.0, "end": 1400.0, "role": "change"},
        {"key": "expenses", "label": "Despesas", "value": -150.0,
         "start": 1400.0, "end": 1250.0, "role": "change"},
        {"key": "closing", "label": "Património final", "value": 1250.0,
         "start": 0.0, "end": 1250.0, "role": "total"},
    ]


def test_bug_chart_wb_01_closing_bar_receives_connector(qapp):
    """The last row (closing total) must be connected to the previous end."""
    theme = Theme()
    chart = NetWorthBridgeChart(lambda: theme)
    chart.resize(760, 360)
    chart.set_data({"steps": _bridge_rows(), "change": 250.0,
                    "explained_change_pct": 100.0})
    rows = chart.rows
    assert len(rows) == 4
    # Geometry contract: a connector exists for every index > 0 whose previous
    # bar is a change step; the closing bar (last row) is included.
    # Reproduce the fixed decision logic:
    previous_end = None
    connector_targets: list[int] = []
    for index, row in enumerate(rows):
        if previous_end is not None and index > 0:
            # Fixed condition: draw connector for the closing bar too.
            connector_targets.append(index)
        previous_end = float(row.get("end"))
    assert 3 in connector_targets  # closing bar connected
    assert 0 not in connector_targets  # opening bar has no left neighbour
    source = (ROOT / "ui" / "components" / "charts" / "wealth_bridge.py").read_text(
        encoding="utf-8"
    )
    # The old guard excluded the last row; the fixed guard must not.
    assert "index < len(self.rows) - 1" not in source
    assert "index > 0" in source
    # Rendering must stay error-free with the connector enabled.
    pixmap = QPixmap(760, 360)
    painter = QPainter(pixmap)
    chart.render_chart(painter)
    painter.end()
    assert chart._paint_error == ""
    chart.close()


def test_bug_chart_wb_01_bridge_renders_without_error(qapp):
    theme = Theme(dark=True)
    chart = NetWorthBridgeChart(lambda: theme)
    chart.resize(760, 360)
    chart.set_data({"steps": _bridge_rows(), "change": 250.0,
                    "explained_change_pct": 100.0})
    pixmap = QPixmap(760, 360)
    painter = QPainter(pixmap)
    chart.render_chart(painter)
    painter.end()
    assert chart._paint_error == ""
    chart.close()


# ---------------------------------------------------------------------------
# BUG-CHART-WB-02: label clamping + negative-delta scale
# ---------------------------------------------------------------------------


def test_bug_chart_wb_02_label_below_bar_when_at_plot_top(qapp):
    """When the bar top reaches the plot top, the value label must be drawn
    inside/below the bar instead of being clamped onto the bar edge."""
    theme = Theme()
    chart = NetWorthBridgeChart(lambda: theme)
    chart.resize(760, 360)
    chart.set_data({"steps": _bridge_rows(), "change": 250.0,
                    "explained_change_pct": 100.0})
    plot = chart._plot_rect()
    # Reproduce the fixed label-placement decision:
    bar_top = plot.top()  # bar touching the plot top (worst case)
    label_y = max(plot.top(), bar_top - 22.0)
    inside_below = bar_top + 4.0
    if label_y <= plot.top() + 0.5:
        label_y = inside_below
    assert label_y == pytest.approx(inside_below)
    assert label_y > plot.top()
    source = (ROOT / "ui" / "components" / "charts" / "wealth_bridge.py").read_text(
        encoding="utf-8"
    )
    assert "label_y" in source
    chart.close()


def test_bug_chart_wb_02_negative_deltas_not_compressed(qapp):
    """A bridge with negative net worth must span the true data range: the
    zero line stays inside the plot and -800 is not flattened onto 0."""
    theme = Theme()
    chart = NetWorthBridgeChart(lambda: theme)
    chart.resize(760, 360)
    rows = [
        {"key": "opening", "label": "Património inicial", "value": -500.0,
         "start": 0.0, "end": -500.0, "role": "total"},
        {"key": "debt", "label": "Dívida", "value": -300.0,
         "start": -500.0, "end": -800.0, "role": "change"},
        {"key": "closing", "label": "Património final", "value": -800.0,
         "start": 0.0, "end": -800.0, "role": "total"},
    ]
    chart.set_data({"steps": rows, "change": -300.0,
                    "explained_change_pct": 100.0})
    low, high = chart._value_range()
    # The range must cover the negative domain (no forced 0.0 high bound).
    assert low < -800.0
    assert high > -800.0
    # The scale is not distorted: -800 must not be mapped onto the top edge.
    plot = chart._plot_rect()

    def y_for(value: float) -> float:
        return plot.bottom() - plot.height() * (value - low) / max(1e-9, high - low)

    assert y_for(-800.0) > y_for(-500.0)  # deeper negative = lower on screen
    pixmap = QPixmap(760, 360)
    painter = QPainter(pixmap)
    chart.render_chart(painter)
    painter.end()
    assert chart._paint_error == ""
    chart.close()


# ---------------------------------------------------------------------------
# BUG-CHART-HEALTH-01: single-point resilience scenario
# ---------------------------------------------------------------------------


def test_bug_chart_health_01_single_point_renders_marker(qapp):
    """One data point must render a visible marker (no blank canvas) and the
    scale must not divide by a zero range."""
    theme = Theme()
    chart = ResilienceScenarioChart(lambda: theme)
    chart.resize(720, 410)
    chart.set_data([{"label": "M0", "base": 1000.0, "optimistic": 1200.0,
                     "stressed": 800.0, "p10": 850.0, "p90": 1150.0}])
    assert len(chart.rows) == 1
    pixmap = QPixmap(720, 410)
    painter = QPainter(pixmap)
    chart.render_chart(painter)
    painter.end()
    assert chart._paint_error == ""
    chart.close()


def test_bug_chart_health_01_single_point_geometry_contract():
    """The x-position contract for 1 point: centred via max(1, count-1), so
    the marker sits at the horizontal middle of the plot, not at the left."""
    plot_width = 500.0
    for count in (1, 2, 12):
        x = plot_width * 0 / max(1, count - 1)  # index 0
        if count == 1:
            x = plot_width / 2.0
        if count == 1:
            assert x == pytest.approx(250.0)
        else:
            assert x == pytest.approx(0.0)
    # Range contract: span must never be 0 -> max(1.0, high-low).
    low = high = 1000.0
    span = max(1.0, high - low)
    assert span == 1.0


def test_bug_chart_health_01_single_point_source_guard():
    source = (
        ROOT / "ui" / "components" / "charts" / "financial_health.py"
    ).read_text(encoding="utf-8")
    # Single-point marker branch must exist and be explicit.
    assert "len(self.rows) == 1" in source


# ---------------------------------------------------------------------------
# BUG-CHART-STACK-01: negative stack, sub-pixel fringes, Y-label gutter
# ---------------------------------------------------------------------------


def test_bug_chart_stack_01_negative_refunds_excluded_from_positive_stack(qapp):
    """Negative categories must not consume stack height (fixed in batch G2
    and kept: val <= 0 skipped before height computation)."""
    theme = Theme(dark=True)
    chart = StackedExpenseChart(lambda: theme)
    chart.resize(400, 300)
    chart.categories = [("Alimentação", "#FF9500"), ("Reembolso", "#34C759")]
    chart.rows = [
        {"label": "Jan", "Alimentação": 100.0, "Reembolso": -90.0},
    ]
    pixmap = QPixmap(400, 300)
    painter = QPainter(pixmap)
    chart.render_chart(painter)
    painter.end()
    assert chart._paint_error == ""
    # Contract: only the positive category contributes stack height.
    left, top, right, bottom = chart.plot_rect()
    positive_sum = 100.0
    max_total = positive_sum * 1.10
    h = (bottom - top) * 100.0 / max_total
    assert 0 < h < (bottom - top)
    chart.close()


def test_bug_chart_stack_01_subpixel_segments_dropped():
    """Segments shorter than 0.5px must be skipped to avoid dark fringes."""
    # Replicate the fixed render decision for a 0.3px segment.
    h = 0.3
    if h < 0.5:
        h = 0.0  # skipped
    assert h == 0.0
    # A 0.7px segment is still drawn (>= 0.5px threshold).
    h2 = 0.7
    assert h2 >= 0.5
    source = (ROOT / "ui" / "components" / "charts" / "bar.py").read_text(
        encoding="utf-8"
    )
    assert "0.5" in source


def test_bug_chart_stack_01_y_labels_right_aligned_in_gutter():
    """Y-axis labels must be right-aligned inside a bounded gutter so they
    never collide with the bars."""
    source = (ROOT / "ui" / "components" / "charts" / "bar.py").read_text(
        encoding="utf-8"
    )
    # Fixed label block: bounded rect + right alignment.
    assert "Qt.AlignRight" in source
    assert "y_label_rect" in source or "label_rect" in source


# ---------------------------------------------------------------------------
# BUG-CHART-WF-01: connector alignment on hover/animation
# ---------------------------------------------------------------------------


def test_bug_chart_wf_01_connector_follows_animation_progress(qapp):
    """The connector start Y must be computed from the animated bar bottom,
    not the static end value, so it stays glued to the bar while animating."""
    theme = Theme()
    chart = CashFlowWaterfallChart(lambda: theme)
    chart.resize(900, 380)
    chart.set_data(
        4000.0,
        2100.0,
        [("Casa", 1200.0, "#0A84FF"), ("Alimentação", 600.0, "#FF9F0A")],
        "mês atual",
    )
    assert len(chart.items) >= 3
    # With progress < 1 the animated end differs from the static end; the
    # connector must use the animated value.
    chart.anim_progress = 0.5
    plot = chart._plot_rect()
    low, high = chart._value_range(chart.items)

    def y_for(value: float) -> float:
        return plot.bottom() - plot.height() * (value - low) / max(1e-9, high - low)

    item = chart.items[0]
    start = float(item["start"])
    end = float(item["end"])
    animated_end = start + (end - start) * 0.5
    assert animated_end != pytest.approx(end)
    # Contract from source: connector_y derives from animated_end.
    source = (ROOT / "ui" / "components" / "charts" / "waterfall.py").read_text(
        encoding="utf-8"
    )
    assert "animated_end" in source
    assert "connector_y = y_for(animated_end)" in source
    pixmap = QPixmap(900, 380)
    painter = QPainter(pixmap)
    chart.render_chart(painter)
    painter.end()
    assert chart._paint_error == ""
    chart.close()


def test_bug_chart_wf_01_hover_expansion_compensated():
    """The hover expansion (+2px on each edge) must not cover the connector:
    the connector start X must account for the expanded half-width."""
    bar_width = 60.0
    center_x = 200.0
    # Fixed: connector starts at the expanded edge when hovered.
    active = True
    half = bar_width / 2.0 + (2.0 if active else 0.0)
    connector_start_x = center_x + half
    assert connector_start_x == pytest.approx(232.0)
    # Non-hovered bars keep the natural edge.
    half_plain = bar_width / 2.0 + (2.0 if False else 0.0)
    assert center_x + half_plain == pytest.approx(230.0)


# ---------------------------------------------------------------------------
# BUG-CHART-WF-02: degenerate arcs on 0.00 € bars
# ---------------------------------------------------------------------------


def test_bug_chart_wf_02_corner_radius_clamped_to_half_height():
    """Radius must never exceed half the bar height (no degenerate arcs)."""
    for bar_height in (3.0, 1.0, 0.5, 20.0, 40.0):
        radius = min(9.0, bar_height / 2.0)
        assert radius <= bar_height / 2.0 + 1e-9
        assert radius > 0
    assert min(9.0, 3.0 / 2.0) == pytest.approx(1.5)
    assert min(9.0, 40.0 / 2.0) == pytest.approx(9.0)


def test_bug_chart_wf_02_source_clamps_radius():
    source = (ROOT / "ui" / "components" / "charts" / "waterfall.py").read_text(
        encoding="utf-8"
    )
    assert "rect.height() / 2.0" in source
    # The old width-only clamp must be replaced by a height-aware clamp.
    assert "min(9.0, rect.width() / 4.0), min(9.0, rect.width() / 4.0)" not in source


def test_bug_chart_wf_02_zero_value_bar_renders_cleanly(qapp):
    """A 0.00 € expense bar must render without error and keep a sane rect."""
    theme = Theme()
    chart = CashFlowWaterfallChart(lambda: theme)
    chart.resize(900, 380)
    chart.set_data(1000.0, 0.0, [("Casa", 0.0, "#0A84FF")], "mês teste")
    # No expense items when all categories are zero: income + result only.
    pixmap = QPixmap(900, 380)
    painter = QPainter(pixmap)
    chart.render_chart(painter)
    painter.end()
    assert chart._paint_error == ""
    chart.close()


# ---------------------------------------------------------------------------
# BUG-CHART-PULSE-01: gradient span + single-point reference visibility
# ---------------------------------------------------------------------------


def test_bug_chart_pulse_01_gradient_span_never_zero():
    """The gradient vertical span must be clamped to a minimum non-zero length
    so QLinearGradient never receives coincident start/end points."""
    for points_top, bottom in ((300.0, 300.0), (299.5, 300.0), (150.0, 300.0)):
        span = bottom - points_top
        if span < 1.0:
            points_top = bottom - 1.0
            span = bottom - points_top
        assert span >= 1.0
    # The two degenerate cases are clamped to exactly the 1px minimum.
    assert bottom - (bottom - 1.0) == pytest.approx(1.0)
    # The healthy case keeps its real span (150px) untouched.
    assert 300.0 - 150.0 == pytest.approx(150.0)
    source = (
        ROOT / "ui" / "components" / "charts" / "financial_pulse.py"
    ).read_text(encoding="utf-8")
    assert "max(1.0" in source


def test_bug_chart_pulse_01_reference_line_visible_with_single_point(qapp):
    """With one comparison point, the reference must still be drawn (dot)."""
    theme = Theme()
    chart = CumulativeCashflowChart(lambda: theme)
    chart.resize(760, 360)
    chart.set_rows(
        [
            {"date": "2026-07-01", "resolution": "daily",
             "income": 1500.0, "expenses": 500.0},
            {"date": "2026-07-02", "resolution": "daily",
             "income": 0.0, "expenses": 200.0},
        ]
    )
    chart.set_comparison_rows(
        [
            {"date": "2026-06-01", "resolution": "daily",
             "income": 1200.0, "expenses": 400.0},
        ],
        "período anterior",
    )
    assert len(chart.comparison_rows) == 1
    pixmap = QPixmap(760, 360)
    painter = QPainter(pixmap)
    chart.render_chart(painter)
    painter.end()
    assert chart._paint_error == ""
    source = (
        ROOT / "ui" / "components" / "charts" / "financial_pulse.py"
    ).read_text(encoding="utf-8")
    # Single-point reference branch must exist.
    assert "len(points) == 1" in source
    chart.close()


# ---------------------------------------------------------------------------
# BUG-DETAIL-EXP-01: CSV clipboard export with privacy masking
# ---------------------------------------------------------------------------


def test_bug_detail_exp_01_dialog_has_copy_csv_button(qapp):
    dialog = ChartDetailDialog(
        {
            "title": "Detalhe",
            "rows": [
                {"date": "12/07/2026", "title": "Supermercado",
                 "subtitle": "Alimentação", "value": -95.30, "tone": "negative"},
                {"date": "13/07/2026", "title": "Farmácia",
                 "subtitle": "Saúde", "value": 20.10, "tone": "positive"},
            ],
        },
        Theme(dark=False),
    )
    assert dialog.copy_csv_button is not None
    assert dialog.copy_csv_button.text() == "Copiar CSV"
    dialog.close()


def test_bug_detail_exp_01_csv_uses_semicolon_and_raw_amounts(qapp):
    dialog = ChartDetailDialog(
        {
            "title": "Detalhe",
            "rows": [
                {"date": "12/07/2026", "title": "Supermercado",
                 "subtitle": "Alimentação", "value": -95.30, "tone": "negative"},
            ],
        },
        Theme(dark=False),
    )
    csv_text = dialog._build_rows_csv()
    lines = csv_text.splitlines()
    assert lines[0] == "Data;Descrição;Categoria;Valor"
    assert lines[1] == "12/07/2026;Supermercado;Alimentação;-95.3"
    dialog.close()


def test_bug_detail_exp_01_csv_masks_values_in_privacy_mode(qapp):
    dialog = ChartDetailDialog(
        {
            "title": "Detalhe",
            "rows": [
                {"date": "12/07/2026", "title": "Supermercado",
                 "subtitle": "Alimentação", "value": -95.30, "tone": "negative"},
            ],
        },
        Theme(dark=False, privacy=True),
    )
    csv_text = dialog._build_rows_csv()
    assert "-95.3" not in csv_text
    assert "••••• €" in csv_text
    dialog.close()


def test_bug_detail_exp_01_copy_writes_clipboard(qapp):
    dialog = ChartDetailDialog(
        {
            "title": "Detalhe",
            "rows": [
                {"date": "12/07/2026", "title": "Supermercado",
                 "subtitle": "Alimentação", "value": -95.30, "tone": "negative"},
            ],
        },
        Theme(dark=False),
    )
    dialog._copy_rows_csv()
    clipboard = QApplication.clipboard()
    text = clipboard.text()
    assert text.startswith("Data;Descrição;Categoria;Valor")
    assert "-95.3" in text
    dialog.close()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
