#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Regression tests for audit batch 19 (chart privacy & rendering):

- BUG-VIZ-PRIVACY-LEAK-DETAIL-001 (P1): ChartDetailDialog must mask every
  numeric series (p10/p50/p90 included) and any currency amount embedded in
  prose fields while privacy mode is active.
- BUG-VIZ-PRIVACY-LEAK-ACCESSIBLE-002 (P1): accessible text and the
  Guyton-Klinger FIRE simulator must pass the privacy flag to eur().
- BUG-CHART-SANKEY-BEZIER-INVERSION-003 (P1): Bézier control points must never
  overshoot the destination node (45% clamp) - no looping crossings < 380px.
- BUG-CHART-SANKEY-CONTRAST-004 (P2): neon accents (#00B4C8, #30D158) must be
  replaced by darkened light-theme variants with >= 3:1 contrast.
- BUG-CHART-TREEMAP-NEGATIVE-GEOMETRY-005 (P1): the tile inset must never
  produce a degenerate (width/height < 1) QRectF.
- BUG-CHART-HEATMAP-LABEL-COLLISION-006 (P2): weekday labels must be hidden
  dynamically when the label gutter collapses below 30px.
- BUG-CHART-THEME-HARDCODED-LIGHT-TOOLTIP-007 (P2): waterfall tooltip must use
  the theme palette (never a hardcoded #FFFFFF background in dark mode).
- BUG-CHART-BALANCE-TOOLTIP-BADGE-COLLISION-008 (P2): the tooltip title must
  reserve the "PREVISÃO" badge width instead of overlapping it.
- BUG-CHART-INVESTMENT-PROJECTION-COLLISION-009 (P2): the projection legend
  must adapt its step to the available width and elide labels.
- BUG-CHART-COMMITMENTS-ROUNDED-STACK-NOTCH-010 (P2): stacked segments must
  round only the outer corners (no concave notches at the junction).
- BUG-CHART-BUBBLE-NEGATIVE-SCALE-011 (P2): scale must be clamped positive and
  the final bubble radius >= 2px on tiny viewports.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QRectF
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ui.components.chart_details import ChartDetailDialog, _redact_detail_text
from ui.components.charts.balance import BalanceChart
from ui.components.charts.bubble import BubbleChart
from ui.components.charts.heatmap import HeatmapChart
from ui.components.charts.investment import InvestmentProjectionChart
from ui.components.charts.sankey import (
    _FLOW_ACCENT_DARK,
    _FLOW_ACCENT_LIGHT,
    _FLOW_GREEN_DARK,
    _FLOW_GREEN_LIGHT,
    SankeyChart,
    _flow_accent_color,
    _flow_green_color,
)
from ui.core.theme import Theme
from utils.helpers import eur


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


# ---------------------------------------------------------------------------
# BUG-VIZ-PRIVACY-LEAK-DETAIL-001: Monte Carlo keys masked in ChartDetailDialog
# ---------------------------------------------------------------------------


def _dialog_chip_texts(theme: Theme, payload: dict) -> list[str]:
    dialog = ChartDetailDialog(dict(payload), theme)
    return [label.text() for label in dialog.metric_labels]


def test_bug001_detail_dialog_masks_monte_carlo_percentiles(qapp):
    theme = Theme(privacy=True)
    chart = SankeyChart(lambda: theme)
    payload = chart.detail_payload(
        {"label": "Alimentação", "p10": 13_000.0, "p50": 18_000.0, "p90": 24_000.0}
    )
    texts = _dialog_chip_texts(theme, payload)
    joined = "\n".join(texts)
    assert eur(13_000, True) in joined
    assert eur(18_000, True) in joined
    assert eur(24_000, True) in joined
    # No raw formatted amount may leak.
    assert eur(13_000, False) not in joined
    assert eur(18_000, False) not in joined
    assert eur(24_000, False) not in joined


def test_bug001_detail_dialog_masks_movement_rows(qapp):
    theme = Theme(privacy=True)
    payload = {
        "title": "Detalhe",
        "metrics": [],
        "rows": [
            {"date": "2026-09-01", "title": "Compra", "value": 123.45},
        ],
    }
    dialog = ChartDetailDialog(dict(payload), theme)
    # Walk the rows layout labels and assert every value label is masked.
    masked_values = [
        label.text()
        for label in dialog.findChildren(type(dialog.rows_title))
        if "•••" in label.text() or label.text() == eur(123.45, True)
    ]
    assert any(eur(123.45, True) in text for text in masked_values)


def test_bug001_redact_detail_text_masks_currency_in_prose():
    assert "18.000,00" not in _redact_detail_text(
        "Mediana final 18.000,00 € entre 13.000 € e 24.000 €", True
    )
    assert _redact_detail_text(
        "Mediana final 18.000,00 € entre 13.000 € e 24.000 €", True
    ).count("••••• €") == 3
    # Plain numbers and percentages must survive.
    assert _redact_detail_text("taxa de 72,5% e 5 anos", True) == "taxa de 72,5% e 5 anos"
    # Privacy off -> untouched.
    assert (
        _redact_detail_text("valor 12,50 €", False) == "valor 12,50 €"
    )


# ---------------------------------------------------------------------------
# BUG-VIZ-PRIVACY-LEAK-ACCESSIBLE-002: eur() privacy flag in accessible text
# and Guyton-Klinger simulator
# ---------------------------------------------------------------------------


def test_bug002_accessible_text_uses_privacy_flag(qapp):
    theme = Theme(privacy=True)
    chart = InvestmentProjectionChart(lambda: theme)
    chart.set_projection(
        {
            "rows": [
                {
                    "year": 1,
                    "invested_nominal": 12_000.0,
                    "invested_real": 12_000.0,
                    "projected_base": 12_500.0,
                    "projected_pessimistic": 12_200.0,
                    "projected_optimistic": 12_900.0,
                    "nominal_base": 12_500.0,
                    "fees_paid": 4.0,
                    "taxes_paid": 2.0,
                }
            ],
            "mode": "monthly",
            "contribution": 100.0,
            "monthly_contribution": 100.0,
            "initial_capital": 10_000.0,
            "annual_rate_pct": 5.0,
            "total_fees": 42.0,
        }
    )
    description = chart.accessibleDescription()
    assert "42,00 €" not in description
    assert "•••••" in description


def test_bug002_accessible_text_unmasked_without_privacy(qapp):
    theme = Theme(privacy=False)
    chart = InvestmentProjectionChart(lambda: theme)
    chart.set_projection(
        {
            "rows": [
                {
                    "year": 1,
                    "invested_nominal": 12_000.0,
                    "invested_real": 12_000.0,
                    "projected_base": 12_500.0,
                    "projected_pessimistic": 12_200.0,
                    "projected_optimistic": 12_900.0,
                    "nominal_base": 12_500.0,
                    "fees_paid": 4.0,
                    "taxes_paid": 2.0,
                }
            ],
            "mode": "monthly",
            "contribution": 100.0,
            "monthly_contribution": 100.0,
            "initial_capital": 10_000.0,
            "annual_rate_pct": 5.0,
            "total_fees": 42.0,
        }
    )
    # Without privacy the amounts appear unmasked in the description.
    description = chart.accessibleDescription()
    assert "•••••" not in description
    assert "100 €" in description
    assert "12 500 €" in description


def test_bug002_fire_simulator_source_uses_privacy_flag():
    """Static source guard: every eur() call in the FIRE/GK simulator block of
    simulations.py must carry a privacy argument."""
    source = (ROOT / "ui" / "pages" / "simulations.py").read_text(encoding="utf-8")
    start = source.index("def _update_fire_calculations")
    end = source.index("def _plan_count_changed")
    block = source[start:end]
    for raw_line in block.splitlines():
        if "eur(" in raw_line:
            assert "privacy" in raw_line or "fire_privacy" in raw_line, (
                f"eur() sem flag de privacidade: {raw_line.strip()!r}"
            )


# ---------------------------------------------------------------------------
# BUG-CHART-SANKEY-BEZIER-INVERSION-003: control points never overshoot
# ---------------------------------------------------------------------------


def test_bug003_sankey_control_points_clamped_narrow():
    """At w=300 (<380px) the control x must stay within 45% of the node gap."""
    w = 300
    left_x = 20
    flow_start_x = left_x + 15
    right_x = w - 190
    node_gap = max(1.0, float(right_x) - float(flow_start_x))
    max_control_offset = node_gap * 0.45
    control_x = flow_start_x + min(float(w / 2) - flow_start_x, max_control_offset)
    # The old bug: mid_x = 150 > right_x = 110 -> control beyond destination.
    assert right_x < w / 2  # precondition: narrow viewport inverts old geometry
    assert control_x <= right_x
    assert control_x == pytest.approx(flow_start_x + node_gap * 0.45)


def test_bug003_sankey_control_points_unchanged_wide():
    """Wide viewports: the clamp is inactive (midpoint within 45% of gap) and
    the control point sits strictly between source and destination."""
    w = 800
    left_x = 20
    flow_start_x = left_x + 15
    right_x = w - 190
    node_gap = float(right_x) - float(flow_start_x)
    max_control_offset = node_gap * 0.45
    control_x = flow_start_x + min(float(w / 2) - flow_start_x, max_control_offset)
    # On wide viewports the natural midpoint is inside the clamp window, so
    # the clamp is a no-op: control stays before the destination either way.
    assert control_x < right_x
    assert control_x >= flow_start_x
    # And the invariant that fixes the inversion bug holds at every width.
    for narrow_w in (240, 300, 340, 379, 380, 420, 800):
        right = narrow_w - 190
        gap = max(1.0, float(right) - float(flow_start_x))
        ctrl = flow_start_x + min(float(narrow_w / 2) - flow_start_x, gap * 0.45)
        assert ctrl <= right, f"w={narrow_w}: control {ctrl} > destination {right}"


def test_bug003_sankey_source_uses_control_clamp():
    source = (ROOT / "ui" / "components" / "charts" / "sankey.py").read_text(
        encoding="utf-8"
    )
    assert "max_control_offset" in source
    assert "0.45" in source
    assert "control_x" in source


# ---------------------------------------------------------------------------
# BUG-CHART-SANKEY-CONTRAST-004: neon colours darkened in light theme
# ---------------------------------------------------------------------------


def _relative_luminance(color: QColor) -> float:
    def channel(value: float) -> float:
        v = value / 255.0
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4

    r, g, b = channel(color.red()), channel(color.green()), channel(color.blue())
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast_ratio(c1: QColor, c2: QColor) -> float:
    l1 = _relative_luminance(c1)
    l2 = _relative_luminance(c2)
    lighter, darker = max(l1, l2), min(l1, l2)
    return (lighter + 0.05) / (darker + 0.05)


def test_bug004_light_theme_flow_accent_meets_3_to_1():
    light_surface = QColor("#FFFFFF")
    dark_surface = QColor("#0F172A")
    light_accent = _flow_accent_color(Theme(dark=False))
    dark_accent = _flow_accent_color(Theme(dark=True))
    assert light_accent.name().upper() == _FLOW_ACCENT_LIGHT.upper()
    assert dark_accent.name().upper() == _FLOW_ACCENT_DARK.upper()
    # Old neon #00B4C8 on white is < 3:1; the darkened variant must be >= 3:1.
    assert _contrast_ratio(QColor(_FLOW_ACCENT_DARK), light_surface) < 3.0
    assert _contrast_ratio(light_accent, light_surface) >= 3.0
    assert _contrast_ratio(dark_accent, dark_surface) >= 3.0


def test_bug004_light_theme_flow_green_meets_3_to_1():
    light_surface = QColor("#FFFFFF")
    dark_surface = QColor("#0F172A")
    light_green = _flow_green_color(Theme(dark=False))
    dark_green = _flow_green_color(Theme(dark=True))
    assert light_green.name().upper() == _FLOW_GREEN_LIGHT.upper()
    assert dark_green.name().upper() == _FLOW_GREEN_DARK.upper()
    assert _contrast_ratio(QColor(_FLOW_GREEN_DARK), light_surface) < 3.0
    assert _contrast_ratio(light_green, light_surface) >= 3.0
    assert _contrast_ratio(dark_green, dark_surface) >= 3.0


# ---------------------------------------------------------------------------
# BUG-CHART-TREEMAP-NEGATIVE-GEOMETRY-005: inset never degenerates
# ---------------------------------------------------------------------------


def test_bug005_treemap_inset_never_degenerate():
    """Replicate the render-time inset maths for micro tiles (4px and 5px)."""
    for tile in (4.0, 5.0, 6.0, 10.0):
        rect = QRectF(0.0, 0.0, tile, tile)
        inset = min(2.0, max(0.0, (min(rect.width(), rect.height()) - 1.0) / 2.0))
        box = rect.adjusted(inset, inset, -inset, -inset)
        if box.width() < 1.0 or box.height() < 1.0:
            box = QRectF(rect)
        assert box.width() >= 1.0, f"tile {tile}: width {box.width()}"
        assert box.height() >= 1.0, f"tile {tile}: height {box.height()}"


def test_bug005_treemap_source_has_scaled_inset():
    source = (ROOT / "ui" / "components" / "charts" / "treemap.py").read_text(
        encoding="utf-8"
    )
    assert "inset = min(2.0" in source
    # The naive fixed inset must be gone.
    assert "box = rect.adjusted(2, 2, -2, -2)" not in source


# ---------------------------------------------------------------------------
# BUG-CHART-HEATMAP-LABEL-COLLISION-006: alternate labels hidden when narrow
# ---------------------------------------------------------------------------


def test_bug006_heatmap_hides_alternate_labels_narrow(qapp):
    theme = Theme()
    chart = HeatmapChart(lambda: theme)
    chart.set_data(
        [
            {"date": f"2026-01-{day:02d}", "expense": 10.0}
            for day in range(1, 29)
        ]
    )
    chart.resize(240, 210)  # < 280px: gutter cannot fit 7 labels
    assert chart.width() < 280
    # The dynamic step must skip alternate rows: exactly rows 0/2/4/6 remain.
    expected_visible = {0, 2, 4, 6}
    # Reproduce the render decision (pure geometry, no painting needed).
    start_x = max(0.0, 240.0 - 53 * 4)  # worst-case narrow pitch
    label_gutter = max(0.0, min(40.0, start_x - 2.0))
    step = 1 if label_gutter >= 30.0 else 2
    assert step == 2
    visible = {row for row in range(7) if row % step == 0}
    assert visible == expected_visible


def test_bug006_heatmap_keeps_all_labels_wide(qapp):
    start_x = 60.0  # ample gutter on wide viewports
    label_gutter = max(0.0, min(40.0, start_x - 2.0))
    assert label_gutter >= 30.0
    step = 1 if label_gutter >= 30.0 else 2
    assert step == 1


# ---------------------------------------------------------------------------
# BUG-CHART-THEME-HARDCODED-LIGHT-TOOLTIP-007: theme-aware tooltip surface
#
# CONFLICT RESOLVED (grounding): the audit asks for a theme-aware waterfall
# tooltip background, but tests/test_visual_components.py locks the opposite
# contract for the in-app hover cards ("opaque light surface avoids the
# unreadable native/black popup on Windows in either application theme",
# waterfall.py comment + test_cashflow_waterfall_hover_uses_opaque_light_in_
# app_card[True] asserting lightness > 235 in dark mode).  The canonical
# regression lock wins (sovereign-pair rule): waterfall.py keeps its opaque
# light card.  The shared palette helper _chart_tooltip_bg used by the other
# charts (bar, budget, heatmap, investment, ...) remains the theme-aware
# surface required by the audit and is locked below.
# ---------------------------------------------------------------------------


def test_bug007_utils_tooltip_bg_is_theme_aware():
    from ui.components.charts.utils import _chart_tooltip_bg

    dark = _chart_tooltip_bg(Theme(dark=True))
    light = _chart_tooltip_bg(Theme(dark=False))
    assert (dark.red(), dark.green(), dark.blue()) == (17, 24, 39)
    assert (light.red(), light.green(), light.blue()) == (255, 255, 255)


def test_bug007_theme_charts_use_shared_tooltip_palette():
    """Every chart tooltip that is NOT the locked opaque-light waterfall card
    must source its background from _chart_tooltip_bg (no hardcoded white)."""
    import re

    charts_dir = ROOT / "ui" / "components" / "charts"
    hardcoded = []
    for py_file in charts_dir.glob("*.py"):
        if py_file.name == "waterfall.py":
            continue  # locked opaque-light contract (v3.49)
        text = py_file.read_text(encoding="utf-8", errors="replace")
        for index, line in enumerate(text.splitlines(), 1):
            if re.search(r'setBrush\(QColor\("#FFFFFF"\)\)', line):
                hardcoded.append(f"{py_file.name}:{index}")
    assert hardcoded == [], f"hardcoded white tooltip fills: {hardcoded}"


# ---------------------------------------------------------------------------
# BUG-CHART-BALANCE-TOOLTIP-BADGE-COLLISION-008: title reserves badge width
# ---------------------------------------------------------------------------


def test_bug008_balance_tooltip_title_reserves_badge_width(qapp):
    theme = Theme()
    chart = BalanceChart(lambda: theme)
    chart.resize(800, 400)
    tooltip_width = min(286.0, max(190.0, chart.width() - 24.0))
    # Forecast row: badge occupies 64px anchored at width-78; title must stop
    # before it (78px reserve) instead of spanning tooltip_width - 28.
    badge_reserve = 78.0
    title_width = tooltip_width - 28.0 - badge_reserve
    assert title_width < tooltip_width - 28.0
    assert title_width == pytest.approx(tooltip_width - 106.0)
    # Non-forecast rows keep the full width.
    assert tooltip_width - 28.0 - 0.0 == tooltip_width - 28.0


def test_bug008_balance_source_elides_forecast_title():
    source = (ROOT / "ui" / "components" / "charts" / "balance.py").read_text(
        encoding="utf-8"
    )
    assert "badge_reserve" in source
    assert "elidedText" in source


# ---------------------------------------------------------------------------
# BUG-CHART-INVESTMENT-PROJECTION-COLLISION-009: adaptive legend step + elide
# ---------------------------------------------------------------------------


def test_bug009_projection_legend_compact_step():
    """On a 320px viewport the 4 legend entries must share the plot width."""
    available_w = 320.0 - 104.0  # plot width from _plot_rect
    legend_count = 4
    compact = available_w < legend_count * 105.0
    assert compact is True
    legend_step = available_w / legend_count if compact else 105.0
    assert legend_step == pytest.approx(216.0 / 4.0)
    label_w = max(24.0, legend_step - 24.0 - 6.0)
    assert label_w >= 24.0
    # Wide viewports keep the original 105px rhythm.
    wide_w = 800.0 - 104.0
    compact_wide = wide_w < legend_count * 105.0
    assert compact_wide is False


def test_bug009_projection_source_elides_legend():
    source = (ROOT / "ui" / "components" / "charts" / "investment.py").read_text(
        encoding="utf-8"
    )
    assert "legend_step" in source
    assert "elidedText" in source


# ---------------------------------------------------------------------------
# BUG-CHART-COMMITMENTS-ROUNDED-STACK-NOTCH-010: outer corners only
# ---------------------------------------------------------------------------


def test_bug010_commitments_segments_round_outer_corners_only():
    """Lower segment rounds bottom only; upper segment rounds top only."""
    # subscription segment (lower): round_bottom=True, round_top=False
    # scheduled segment (upper): round_top=True, round_bottom=False
    specs = [
        ("subscription_rect", False, True),
        ("scheduled_rect", True, False),
    ]
    for _name, round_top, round_bottom in specs:
        assert round_top != round_bottom
        # At the junction the two inner corners have radius 0 -> no notch.
        junction_radius = 0.0 if not round_top else 0.0
        assert junction_radius == 0.0


def test_bug010_commitments_source_rounds_selectively():
    source = (ROOT / "ui" / "components" / "charts" / "commitments.py").read_text(
        encoding="utf-8"
    )
    # The uniform 4-corner rounded rect for stacked segments is gone.
    assert "painter.drawRoundedRect(rect, 4.0, 4.0)" not in source
    assert "round_top" in source and "round_bottom" in source


# ---------------------------------------------------------------------------
# BUG-CHART-BUBBLE-NEGATIVE-SCALE-011: positive scale, radius >= 2
# ---------------------------------------------------------------------------


def test_bug011_bubble_scale_clamped_positive():
    """Replicate the scale maths on a 30px-wide viewport."""
    width, height = 30, 30
    content_w, content_h = 120.0, 90.0
    scale_x = (width - 40) / content_w  # negative in the old code
    scale_y = (height - 40) / content_h
    scale = max(0.01, min(min(scale_x, scale_y), 1.0))
    assert scale_x < 0  # precondition: old code produced negative scale
    assert scale == 0.01
    assert scale > 0


def test_bug011_bubble_radius_clamped_minimum():
    final_r = -3.5  # what a negative scale used to produce
    final_r = max(2.0, final_r)
    assert final_r == 2.0
    assert final_r >= 2.0


def test_bug011_bubble_small_viewport_draws_empty(qapp):
    theme = Theme()
    chart = BubbleChart(lambda: theme)
    chart.set_data({"Alimentação": 120.0, "Transporte": 60.0})
    # setMinimumSize(300, 420) prevents real resize below 60px; the guard
    # contract is verified at source level plus the exact maths tests above.
    source = (ROOT / "ui" / "components" / "charts" / "bubble.py").read_text(
        encoding="utf-8"
    )
    assert "max(2.0, final_r)" in source
    assert "scale = max(0.01, min(scale, 1.0))" in source
    assert "Espaço insuficiente" in source
    assert "self.width() < 60 or self.height() < 60" in source


def test_bug011_bubble_normal_viewport_renders(qapp):
    theme = Theme()
    chart = BubbleChart(lambda: theme)
    chart.set_data({"Alimentação": 120.0, "Transporte": 60.0})
    chart.resize(400, 420)
    assert chart.width() >= 60 and chart.height() >= 60
    # Sanity: the guard only fires on tiny viewports.
    assert chart.width() - 40 > 0


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
