"""Unit tests for ui/components/charts/presentation.py."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPointF, QRectF, QSizeF
from PySide6.QtGui import QFont, QImage, QPainter

from ui.components.charts.presentation import (
    chart_font,
    draw_ghost_axes,
    fit_tooltip_rect,
    format_axis_money,
    monotone_cubic_path,
    paint_hover_card,
)
from ui.core.theme import Theme


def test_format_axis_money_sub_thousand() -> None:
    assert format_axis_money(0.0) == "0 €"
    assert format_axis_money(-0.0) == "0 €"
    assert format_axis_money(-0.4) == "0 €"
    assert format_axis_money(-0.1) == "0 €"
    assert format_axis_money(-0.6) == "-1 €"
    assert format_axis_money(150.0) == "150 €"
    assert format_axis_money(999.4) == "999 €"
    assert format_axis_money(999.96) == "1000 €"
    assert format_axis_money(-75.0) == "-75 €"


def test_format_axis_money_thousands_k() -> None:
    assert format_axis_money(1000.0) == "1,0 k€"
    assert format_axis_money(1500.0) == "1,5 k€"
    assert format_axis_money(25400.0) == "25,4 k€"
    assert format_axis_money(-12300.0) == "-12,3 k€"
    assert format_axis_money(999_999.96) == "1,0 M€"


def test_format_axis_money_millions_m() -> None:
    assert format_axis_money(1_000_000.0) == "1,0 M€"
    assert format_axis_money(1_250_000.0) == "1,2 M€"
    assert format_axis_money(-2_500_000.0) == "-2,5 M€"


def test_format_axis_money_privacy_and_nonfinite() -> None:
    assert format_axis_money(54321.0, privacy=True) == "••••• €"
    assert format_axis_money(float("nan")) == "0 €"
    assert format_axis_money(float("inf")) == "0 €"


def test_chart_font_scales() -> None:
    axis_font = chart_font("axis")
    assert isinstance(axis_font, QFont)
    assert axis_font.pointSize() == 8

    title_font = chart_font("title")
    assert title_font.pointSize() == 11
    assert title_font.weight() == QFont.Weight.DemiBold


def test_fit_tooltip_rect_clamping() -> None:
    bounds = QRectF(0.0, 0.0, 500.0, 300.0)
    size = QSizeF(100.0, 50.0)

    # Near top-left: must not overflow top (flips below anchor) and must clamp left
    anchor_top_left = QPointF(10.0, 10.0)
    rect = fit_tooltip_rect(anchor_top_left, size, bounds)
    assert rect.left() >= bounds.left()
    assert rect.right() <= bounds.right()
    assert rect.top() >= bounds.top()
    assert rect.bottom() <= bounds.bottom()

    # Near bottom-right: must stay within bounds
    anchor_bottom_right = QPointF(495.0, 295.0)
    rect2 = fit_tooltip_rect(anchor_bottom_right, size, bounds)
    assert rect2.left() >= bounds.left()
    assert rect2.right() <= bounds.right()
    assert rect2.top() >= bounds.top()
    assert rect2.bottom() <= bounds.bottom()


def test_monotone_cubic_path() -> None:
    assert monotone_cubic_path([]).isEmpty()

    single = monotone_cubic_path([QPointF(10, 10)])
    assert single.elementCount() == 1

    line = monotone_cubic_path([QPointF(10, 10), QPointF(50, 50)])
    assert line.elementCount() == 2

    # Monotone series
    points = [QPointF(0, 10), QPointF(20, 30), QPointF(40, 30), QPointF(60, 50)]
    spline = monotone_cubic_path(points)
    assert spline.elementCount() > 2

    # Duplicate X coordinates (must not raise ZeroDivisionError)
    duplicate_x = [QPointF(10, 20), QPointF(10, 50), QPointF(20, 60)]
    spline_dup = monotone_cubic_path(duplicate_x)
    assert spline_dup.elementCount() > 2

    # Extreme spike
    spike = [QPointF(0, 0), QPointF(10, 1_000_000), QPointF(20, 0)]
    spline_spike = monotone_cubic_path(spike)
    assert spline_spike.elementCount() > 2

    # NaN / non-finite coordinates filtered safely
    with_nan = [QPointF(0, 0), QPointF(float("nan"), 10), QPointF(20, 20)]
    spline_nan = monotone_cubic_path(with_nan)
    assert spline_nan.elementCount() == 2


def test_paint_hover_card_and_ghost_axes() -> None:
    image = QImage(300, 200, QImage.Format_ARGB32_Premultiplied)
    image.fill(0)
    painter = QPainter(image)
    try:
        theme = Theme(dark=False)
        rect = QRectF(20, 20, 120, 60)
        paint_hover_card(painter, rect, theme, surface="opaque_light")
        draw_ghost_axes(painter, QRectF(0, 0, 300, 200), theme)
    finally:
        painter.end()
