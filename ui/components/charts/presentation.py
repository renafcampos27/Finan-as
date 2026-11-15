"""Shared presentation helpers for custom QPainter charts.

Provides unified formatting for axis labels, font scales, tooltip positioning,
monotone cubic splines, and ghost axes across all custom finance charts.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Literal

from PySide6.QtCore import QPointF, QRectF, QSizeF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen

if TYPE_CHECKING:
    from ui.core.theme import Theme


def format_axis_money(value: float, privacy: bool = False) -> str:
    """Format euro amounts concisely for chart axis ticks.

    - Under 1 000 €: full integer euro ('150 €', '-50 €', '0 €')
    - 1 000 € to 999 999 €: one decimal k€ ('1,5 k€', '25,0 k€')
    - 1 000 000 € and above: one decimal M€ ('1,2 M€')
    - Decimal comma, signed, zero is never negative.
    - If privacy is active, masks without leaking magnitude.
    """
    if privacy:
        return "••••• €"

    if not math.isfinite(value):
        return "0 €"

    abs_val = abs(value)
    if abs_val < 1e-6:
        return "0 €"

    sign = "-" if value < 0 else ""

    if abs_val >= 999_950.0:
        formatted = f"{abs_val / 1_000_000:.1f}".replace(".", ",")
        return f"{sign}{formatted} M€"
    elif abs_val >= 1_000.0:
        formatted = f"{abs_val / 1_000:.1f}".replace(".", ",")
        return f"{sign}{formatted} k€"
    else:
        rounded_int = round(abs_val)
        if rounded_int == 0:
            return "0 €"
        return f"{sign}{rounded_int} €"


def chart_font(
    role: Literal["axis", "label", "title", "tooltip"] = "axis",
    point_size: int | None = None,
    bold: bool = False,
) -> QFont:
    """Create a standardized QFont for charts with Segoe UI."""
    font = QFont("Segoe UI")
    if point_size is not None:
        font.setPointSize(point_size)
    else:
        size_map = {
            "axis": 8,
            "label": 9,
            "title": 11,
            "tooltip": 9,
        }
        font.setPointSize(size_map.get(role, 8))

    if bold or role == "title":
        font.setWeight(QFont.Weight.DemiBold)
    else:
        font.setWeight(QFont.Weight.Normal)

    return font


def fit_tooltip_rect(
    anchor: QPointF,
    size: QSizeF,
    bounds: QRectF,
    offset_y: float = -14.0,
) -> QRectF:
    """Compute tooltip geometry anchored at `anchor`, kept strictly within `bounds`."""
    width = size.width()
    height = size.height()

    # Center horizontally on anchor
    x = anchor.x() - width / 2.0
    if x < bounds.left() + 6.0:
        x = bounds.left() + 6.0
    elif x + width > bounds.right() - 6.0:
        x = bounds.right() - width - 6.0

    # Prefer placing above anchor
    y = anchor.y() + offset_y - height
    if y < bounds.top() + 6.0:
        # Flip below anchor if it overflows top
        y = anchor.y() - offset_y

    # If it still overflows bottom, clamp
    if y + height > bounds.bottom() - 6.0:
        y = bounds.bottom() - height - 6.0

    return QRectF(x, y, width, height)


def paint_hover_card(
    painter: QPainter,
    rect: QRectF,
    theme: Theme,
    *,
    surface: Literal["theme", "opaque_light"] = "theme",
    border_color: QColor | None = None,
) -> None:
    """Paint an elegant, high-contrast hover card surface with rounded corners."""
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

    # Soft subtle shadow
    shadow_rect = rect.translated(0, 3)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(0, 0, 0, 40 if getattr(theme, "dark", False) else 20))
    painter.drawRoundedRect(shadow_rect, 10.0, 10.0)

    # Card background
    if surface == "opaque_light" or not getattr(theme, "dark", False):
        bg = QColor("#FFFFFF")
        stroke = border_color or QColor(226, 232, 240)
    else:
        bg = QColor(22, 27, 34, 240)
        stroke = border_color or QColor(56, 68, 86, 200)

    painter.setBrush(bg)
    painter.setPen(QPen(stroke, 1.0))
    painter.drawRoundedRect(rect, 10.0, 10.0)
    painter.restore()


def monotone_cubic_path(points: list[QPointF]) -> QPainterPath:
    """Compute a Fritsch-Carlson monotone cubic spline through `points`.

    Guarantees monotonicity: the spline never overshoots local minima or maxima,
    preventing spurious dips into negative numbers or phantom peaks.
    """
    path = QPainterPath()
    valid_points = [
        p for p in points if math.isfinite(p.x()) and math.isfinite(p.y())
    ]
    n = len(valid_points)
    if n == 0:
        return path
    if n == 1:
        path.moveTo(valid_points[0])
        return path
    if n == 2:
        path.moveTo(valid_points[0])
        path.lineTo(valid_points[1])
        return path

    path.moveTo(valid_points[0])

    # Extract x and y coordinates
    xs = [p.x() for p in valid_points]
    ys = [p.y() for p in valid_points]

    # Secants (slopes between consecutive points)
    dxs = [xs[i + 1] - xs[i] for i in range(n - 1)]
    dys = [ys[i + 1] - ys[i] for i in range(n - 1)]

    ms = [0.0 if abs(dxs[i]) < 1e-9 else dys[i] / dxs[i] for i in range(n - 1)]

    # Tangents at vertices
    tangents = [0.0] * n
    tangents[0] = ms[0]
    for i in range(1, n - 1):
        if ms[i - 1] * ms[i] <= 0:
            tangents[i] = 0.0
        else:
            tangents[i] = (ms[i - 1] + ms[i]) / 2.0
    tangents[n - 1] = ms[-1]

    # Fritsch-Carlson adjustment
    for i in range(n - 1):
        if abs(ms[i]) < 1e-9 or abs(dys[i]) < 1e-9 or abs(dxs[i]) < 1e-9:
            tangents[i] = 0.0
            tangents[i + 1] = 0.0
        else:
            alpha = tangents[i] / ms[i]
            beta = tangents[i + 1] / ms[i]
            h = math.hypot(alpha, beta)
            if h > 3.0:
                tau = 3.0 / h
                tangents[i] = tau * alpha * ms[i]
                tangents[i + 1] = tau * beta * ms[i]

    # Construct cubic Bezier segments
    for i in range(n - 1):
        dx = dxs[i]
        c1x = xs[i] + dx / 3.0
        c1y = ys[i] + tangents[i] * dx / 3.0
        c2x = xs[i + 1] - dx / 3.0
        c2y = ys[i + 1] - tangents[i + 1] * dx / 3.0
        path.cubicTo(QPointF(c1x, c1y), QPointF(c2x, c2y), valid_points[i + 1])

    return path


def draw_ghost_axes(
    painter: QPainter,
    rect: QRectF,
    theme: Theme,
    num_ticks: int = 4,
) -> None:
    """Draw clean, elegant ghost gridlines for empty or loading chart states."""
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)

    grid_color = QColor(255, 255, 255, 20) if getattr(theme, "dark", False) else QColor(0, 0, 0, 18)
    pen = QPen(grid_color, 1.0, Qt.PenStyle.DashLine)
    painter.setPen(pen)

    top = rect.top()
    bottom = rect.bottom()
    left = rect.left()
    right = rect.right()
    step = (bottom - top) / max(1, num_ticks)

    for i in range(num_ticks + 1):
        y = top + i * step
        painter.drawLine(QPointF(left, y), QPointF(right, y))

    painter.restore()
