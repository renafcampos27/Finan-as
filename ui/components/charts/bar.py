from typing import Any, Callable, Optional

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget

from ui.components.charts.base import StableChart
from ui.components.charts.utils import (
    _chart_bg,
    _chart_grid,
    _chart_muted,
    _chart_text,
    _chart_tooltip_bg,
)
from ui.core import Theme
from ui.components.charts.presentation import format_axis_money, monotone_cubic_path

from utils.helpers import safe_float, eur, percent

RED = "#FF3B30"
GREEN = "#34C759"


class BarChart(StableChart):
    def __init__(
        self, theme_provider: Callable[[], Theme], parent: Optional[QWidget] = None
    ):
        super().__init__(theme_provider, parent)
        self.items: list[tuple[str, float, str]] = []
        self.hover_index: Optional[int] = None
        self._row_rects: list[QRectF] = []
        self.setMinimumHeight(240)
        self.setAccessibleName("Valores por grupo")

    def set_items(self, items: list[tuple[str, float, str]]) -> None:
        clean: list[tuple[str, float, str]] = []
        for label, value, color in items or []:
            v = abs(safe_float(value))
            if v > 0:
                clean.append(
                    (str(label or "Sem categoria"), v, str(color or "#94A3B8"))
                )
        ordered = sorted(clean, key=lambda item: item[1], reverse=True)
        if len(ordered) > 8:
            remainder = ordered[8:]
            ordered = ordered[:8] + [
                (
                    f"Outros ({len(remainder)})",
                    sum(value for _label, value, _color in remainder),
                    "#94A3B8",
                )
            ]
        self.items = ordered
        self.hover_index = None
        self._row_rects.clear()
        chart_height = max(240, 54 + len(self.items) * 40)
        self.setMinimumHeight(chart_height)
        self.updateGeometry()
        self.invalidate_cache()

    def mouseMoveEvent(self, event):  # type: ignore[override]
        hovered = next(
            (
                index
                for index, rect in enumerate(self._row_rects)
                if rect.contains(event.position())
            ),
            None,
        )
        if hovered != self.hover_index:
            self.hover_index = hovered
            self.setCursor(
                Qt.PointingHandCursor if hovered is not None else Qt.ArrowCursor
            )
            self.invalidate_cache()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):  # type: ignore[override]
        self.hover_index = None
        self.setCursor(Qt.ArrowCursor)
        self.invalidate_cache()
        super().leaveEvent(event)

    def render_chart(self, p: QPainter) -> None:
        theme = self.theme_provider()
        p.fillRect(self.rect(), _chart_bg(theme))
        self._row_rects = []
        if not self.items:
            self.draw_empty(p, "Sem dados")
            return
        maxv = max(v for _, v, _ in self.items) or 1
        left, top, bottom_margin = 16, 20, 48
        count = len(self.items)
        bar_h = max(
            16, min(30, int((self.height() - top - bottom_margin) / max(1, count) - 8))
        )

        for i, (label, value, color) in enumerate(self.items):
            y = top + i * (bar_h + 10)
            row_rect = QRectF(0, y - 4, self.width(), bar_h + 8)
            self._row_rects.append(row_rect)
            if i == self.hover_index:
                highlight = QColor(color)
                highlight.setAlpha(24 if theme.dark else 18)
                p.fillRect(row_rect, highlight)
            p.setPen(_chart_muted(theme))
            p.setFont(QFont("Segoe UI", 8))
            p.drawText(left, int(y + bar_h - 2), label[:22])
            bx = left + 132
            bw = max(40, self.width() - bx - 96)
            path = QPainterPath()
            path.addRoundedRect(QRectF(bx, y, bw, bar_h), 8, 8)
            p.fillPath(path, QColor(148, 163, 184, 50))
            path2 = QPainterPath()
            path2.addRoundedRect(QRectF(bx, y, max(6, bw * value / maxv), bar_h), 8, 8)
            p.fillPath(path2, QColor(color))
            p.setPen(_chart_text(theme))
            p.drawText(int(bx + bw + 10), int(y + bar_h - 3), eur(value, theme.privacy))


class MonthlyEvolutionChart(StableChart):
    def __init__(
        self, theme_provider: Callable[[], Theme], parent: Optional[QWidget] = None
    ):
        super().__init__(theme_provider, parent)
        self.rows: list[dict[str, Any]] = []
        self.hover_index: Optional[int] = None
        self.setMinimumHeight(280)

    def set_rows(self, rows: list[dict[str, Any]]) -> None:
        self.clear_period_hover()
        self.rows = rows or []
        self.hover_index = None
        self.invalidate_cache()

    def plot_rect(self) -> tuple[int, int, int, int]:
        return 58, 28, max(96, self.width() - 28), max(90, self.height() - 48)

    def mouseMoveEvent(self, event):  # type: ignore[override]
        # BUG-AUD-C137: always propagate to the Qt base handler; early returns
        # previously swallowed parent/gesture event processing.
        if not self.rows:
            super().mouseMoveEvent(event)
            return
        left, _top, right, _bottom = self.plot_rect()
        if right <= left:
            super().mouseMoveEvent(event)
            return
        ratio = min(max((event.position().x() - left) / max(1, right - left), 0), 1)
        hovered = max(
            0, min(len(self.rows) - 1, int(round(ratio * (len(self.rows) - 1))))
        )
        if hovered != self.hover_index:
            self.hover_index = hovered
            self.emit_period_for_hover(hovered)
            self.invalidate_cache()
        # BUG-AUD-C137: propagate to the Qt base handler.
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):  # type: ignore[override]
        self.hover_index = None
        self.emit_period_for_hover(None)
        self.invalidate_cache()
        super().leaveEvent(event)

    def _paint_tooltip(self, p: QPainter, x: float, y: float, lines: list[str]) -> None:
        tooltip_w = 260
        tooltip_h = 26 + max(1, len(lines) - 1) * 20
        left, top, right, _bottom = self.plot_rect()
        tx = min(max(x + 14, left), right - tooltip_w)
        ty = max(top + 4, y - tooltip_h - 12)
        rect = QRectF(float(tx), float(ty), tooltip_w, tooltip_h)
        path = QPainterPath()
        path.addRoundedRect(rect, 16, 16)
        theme = self.theme_provider()
        border_color = QColor(theme.border)
        p.setPen(QPen(border_color, 1))
        p.setBrush(_chart_tooltip_bg(theme))
        p.drawPath(path)
        p.setPen(_chart_text(theme))
        p.setFont(QFont("Segoe UI", 9, QFont.Bold))
        p.drawText(int(tx + 14), int(ty + 23), lines[0])
        p.setFont(QFont("Segoe UI", 8))
        p.setPen(_chart_muted(theme))
        for j, text in enumerate(lines[1:], 1):
            p.drawText(int(tx + 14), int(ty + 23 + j * 20), text)

    def render_chart(self, p: QPainter) -> None:
        theme = self.theme_provider()
        p.fillRect(self.rect(), _chart_bg(theme))
        rows = self.rows
        if not rows:
            self.draw_empty(p, "Sem dados")
            return
        left, top, right, bottom = self.plot_rect()
        p.setPen(QPen(_chart_grid(theme), 1))
        for i in range(5):
            y = top + (bottom - top) * i / 4
            p.drawLine(left, int(y), right, int(y))
        vals = [
            max(
                safe_float(r.get("income")),
                safe_float(r.get("expenses")),
                safe_float(r.get("prev_expenses")),
            )
            for r in rows
        ]
        mx = max(vals) if vals else 1
        mx = max(1, mx * 1.12)

        def xy(i: int, val: float) -> QPointF:
            x = left + (right - left) * (i / max(1, len(rows) - 1))
            y = bottom - (bottom - top) * (val / mx)
            return QPointF(float(x), float(y))

        # y labels
        p.setFont(QFont("Segoe UI", 8))
        p.setPen(_chart_muted(theme))
        for i in range(5):
            val = mx - mx * i / 4
            p.drawText(
                4,
                int(top + (bottom - top) * i / 4 + 4),
                format_axis_money(val, theme.privacy),
            )
        # Draw prev_expenses first (dashed)
        prev_points = [
            xy(i, safe_float(rows[i].get("prev_expenses")))
            for i in range(len(rows))
        ]
        path_prev = monotone_cubic_path(prev_points)
        pen_prev = QPen(QColor(RED), 2, Qt.DashLine)
        pen_prev.setCapStyle(Qt.RoundCap)
        pen_prev.setJoinStyle(Qt.RoundJoin)
        # make it semi-transparent
        color_prev = QColor(RED)
        color_prev.setAlpha(100)
        pen_prev.setColor(color_prev)
        p.setPen(pen_prev)
        p.drawPath(path_prev)

        for key, color in [("income", GREEN), ("expenses", RED)]:
            points = [
                xy(i, safe_float(rows[i].get(key)))
                for i in range(len(rows))
            ]
            path = monotone_cubic_path(points)
            pen = QPen(QColor(color), 3)
            pen.setCapStyle(Qt.RoundCap)
            pen.setJoinStyle(Qt.RoundJoin)
            p.setPen(pen)
            p.drawPath(path)
            p.setBrush(QColor(color))
            p.setPen(Qt.NoPen)
            for i, r in enumerate(rows):
                pt = xy(i, safe_float(r.get(key)))
                p.drawEllipse(pt, 4, 4)
        p.setFont(QFont("Segoe UI", 8))
        p.setPen(_chart_muted(theme))
        for i, r in enumerate(rows):
            if i % 2 == 0 or len(rows) <= 8:
                pt = xy(i, 0)
                p.drawText(int(pt.x() - 12), bottom + 22, str(r.get("label", ""))[:3])
        # legend
        p.setBrush(QColor(GREEN))
        p.setPen(Qt.NoPen)
        p.drawEllipse(right - 220, 8, 10, 10)
        p.setPen(_chart_text(theme))
        p.drawText(right - 205, 18, "Receitas")

        p.setBrush(QColor(RED))
        p.setPen(Qt.NoPen)
        p.drawEllipse(right - 138, 8, 10, 10)
        p.setPen(_chart_text(theme))
        p.drawText(right - 123, 18, "Despesas")

        pen_prev_leg = QPen(color_prev, 2, Qt.DashLine)
        p.setPen(pen_prev_leg)
        p.drawLine(right - 50, 13, right - 35, 13)
        p.setPen(_chart_text(theme))
        p.drawText(right - 30, 18, "Ano Ant.")

        if self.hover_index is not None and 0 <= self.hover_index < len(rows):
            i = self.hover_index
            row = rows[i]
            income = safe_float(row.get("income"))
            expenses = safe_float(row.get("expenses"))
            prev_expenses = safe_float(row.get("prev_expenses"))
            balance = income - expenses
            lines = [
                row.get("month", ""),
                f"Receitas: {eur(income, theme.privacy)}",
                f"Despesas: {eur(expenses, theme.privacy)}",
                f"Despesas Ano Ant.: {eur(prev_expenses, theme.privacy)}",
                f"Balanço: {eur(balance, theme.privacy)}",
            ]
            xpt = xy(i, max(income, expenses))
            self._paint_tooltip(p, xpt.x(), xpt.y(), lines)


class StackedExpenseChart(StableChart):
    def __init__(
        self, theme_provider: Callable[[], Theme], parent: Optional[QWidget] = None
    ):
        super().__init__(theme_provider, parent)
        self.rows: list[dict[str, Any]] = []
        self.categories: list[tuple[str, str]] = []
        self.hover_index: Optional[int] = None
        self.setMinimumHeight(340)

    def set_data(
        self, rows: list[dict[str, Any]], categories: list[tuple[str, str]]
    ) -> None:
        self.clear_period_hover()
        self.rows = rows or []
        self.categories = categories or []
        self.hover_index = None
        self.invalidate_cache()

    def plot_rect(self) -> tuple[int, int, int, int]:
        return 54, 24, max(96, self.width() - 22), max(90, self.height() - 48)

    def mouseMoveEvent(self, event):  # type: ignore[override]
        # BUG-AUD-C137: propagate to the Qt base handler on every exit path.
        if not self.rows or not self.categories:
            super().mouseMoveEvent(event)
            return
        left, _top, right, _bottom = self.plot_rect()
        bar_area = right - left
        gap = 8
        bw = max(
            10,
            min(42, (bar_area - gap * (len(self.rows) - 1)) / max(1, len(self.rows))),
        )
        x = event.position().x()
        nearest = None
        nearest_dist = 999999.0
        for i in range(len(self.rows)):
            bx = left + i * (bw + gap)
            if bx - gap / 2 <= x <= bx + bw + gap / 2:
                dist = abs((bx + bw / 2) - x)
                if dist < nearest_dist:
                    nearest = i
                    nearest_dist = dist
        if nearest != self.hover_index:
            self.hover_index = nearest
            self.emit_period_for_hover(nearest)
            self.invalidate_cache()
        # BUG-AUD-C137: propagate to the Qt base handler.
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):  # type: ignore[override]
        self.hover_index = None
        self.emit_period_for_hover(None)
        self.invalidate_cache()
        super().leaveEvent(event)

    def _paint_tooltip(self, p: QPainter, x: float, y: float, lines: list[str]) -> None:
        tooltip_w = 285
        tooltip_h = 28 + max(1, len(lines) - 1) * 19
        left, top, right, _bottom = self.plot_rect()
        tx = min(max(x + 14, left), right - tooltip_w)
        ty = max(top + 4, y - tooltip_h - 12)
        rect = QRectF(float(tx), float(ty), tooltip_w, tooltip_h)
        path = QPainterPath()
        path.addRoundedRect(rect, 16, 16)
        theme = self.theme_provider()
        border_color = QColor(theme.border)
        p.setPen(QPen(border_color, 1))
        p.setBrush(_chart_tooltip_bg(theme))
        p.drawPath(path)
        p.setPen(_chart_text(theme))
        p.setFont(QFont("Segoe UI", 9, QFont.Bold))
        p.drawText(int(tx + 14), int(ty + 23), lines[0])
        p.setFont(QFont("Segoe UI", 8))
        p.setPen(_chart_muted(theme))
        for j, text in enumerate(lines[1:], 1):
            p.drawText(int(tx + 14), int(ty + 23 + j * 19), text)

    def render_chart(self, p: QPainter) -> None:
        theme = self.theme_provider()
        p.fillRect(self.rect(), _chart_bg(theme))
        if not self.rows or not self.categories:
            self.draw_empty(p, "Sem composição de despesas")
            return
        left, top, right, bottom = self.plot_rect()
        def _row_positive_sum(r: dict[str, Any]) -> float:
            return sum(max(0.0, safe_float(r.get(cat, 0))) for cat, _ in self.categories)

        max_drawn = max((_row_positive_sum(r) for r in self.rows), default=0.0)
        max_total = (max_drawn or 1.0) * 1.10
        p.setPen(QPen(QColor(148, 163, 184, 55), 1))
        for i in range(5):
            y = top + (bottom - top) * i / 4
            p.drawLine(left, int(y), right, int(y))
            p.setPen(_chart_muted(theme))
            p.setFont(QFont("Segoe UI", 8))
            # BUG-CHART-STACK-01: right-align the axis labels inside a bounded
            # gutter (left..plot.left()-6) so long values never collide with
            # the first bar.
            y_label_rect = QRectF(4, y - 10, left - 10, 20)
            p.drawText(
                y_label_rect,
                Qt.AlignRight | Qt.AlignVCenter,
                format_axis_money(max_total - max_total * i / 4, theme.privacy),
            )
            p.setPen(QPen(QColor(148, 163, 184, 55), 1))
        bar_area = right - left
        gap = 8
        bw = max(
            10,
            min(42, (bar_area - gap * (len(self.rows) - 1)) / max(1, len(self.rows))),
        )
        # BUG-AUD-C138: in compact widths the summed minimum bar widths can
        # exceed the plot area; clip the bars (and hover highlight) to the
        # chart rect so nothing paints over the neighbouring panel.  The
        # tooltip is drawn afterwards with clipping restored.
        p.save()
        p.setClipRect(QRectF(left, top, right - left, bottom - top))
        bar_centres: list[tuple[float, float, float]] = []
        for i, r in enumerate(self.rows):
            x = left + i * (bw + gap)
            y = bottom
            if self.hover_index == i:
                p.fillRect(
                    QRectF(x - 4, top, bw + 8, bottom - top), QColor(10, 132, 255, 26)
                )
            for cat, color in self.categories:
                # BUG-CHART-STACKED-EXPENSE-NEGATIVE-002: refunds/negative
                # category balances must never produce negative segment
                # heights (bar grows downwards over the axis/legend).
                # Negative values are skipped from the stack; the tooltip
                # row still reports them so nothing becomes invisible.
                val = safe_float(r.get(cat, 0))
                if val <= 0.0:
                    continue
                h = (bottom - top) * val / max_total
                # BUG-CHART-STACK-01: sub-pixel segments (< 0.5px) paint dark
                # fringes between stacked segments; skip them entirely.
                if h < 0.5:
                    continue
                rect = QRectF(x, y - h, bw, h)
                p.fillRect(rect, QColor(color))
                y -= h
            if self.hover_index == i:
                p.setPen(QPen(QColor(10, 132, 255, 170), 2))
                p.setBrush(Qt.NoBrush)
                p.drawRoundedRect(QRectF(x - 2, top, bw + 4, bottom - top), 8, 8)
            p.setPen(_chart_muted(theme))
            p.setFont(QFont("Segoe UI", 8))
            p.drawText(int(x - 2), bottom + 24, str(r.get("label", ""))[:3])
            bar_centres.append((x + bw / 2, x, bw))

        p.restore()
        if self.hover_index is not None and 0 <= self.hover_index < len(self.rows):
            row = self.rows[self.hover_index]
            total = sum(safe_float(row.get(cat, 0)) for cat, _ in self.categories)
            cx, _x, _bw = bar_centres[self.hover_index]
            lines = [f"{row.get('label', 'Mês')} · Total: {eur(total, theme.privacy)}"]
            sorted_cats = sorted(
                (
                    (cat, safe_float(row.get(cat, 0)), color)
                    for cat, color in self.categories
                ),
                key=lambda item: item[1],
                reverse=True,
            )
            positive_cats = [item for item in sorted_cats if item[1] > 0]
            negative_cats = [item for item in sorted_cats if item[1] < 0]
            for cat, val, _color in positive_cats[:5]:
                part = val / total * 100 if total > 0 else 0
                lines.append(f"{cat}: {eur(val, theme.privacy)} · {percent(part, theme.privacy)}")
            for cat, val, _color in negative_cats[:3]:
                lines.append(f"{cat}: {eur(val, theme.privacy)} (reembolso)")
            if len(lines) == 1:
                lines.append("Sem despesas neste mês.")
            self._paint_tooltip(p, cx, top + 20, lines)


StackedBarChart = StackedExpenseChart

