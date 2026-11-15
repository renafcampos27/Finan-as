from __future__ import annotations

from typing import Any, Callable, Optional

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import QWidget

from ui.components.charts.base import StableChart
from ui.components.charts.utils import (
    _chart_accent,
    _chart_bg,
    _chart_grid,
    _chart_muted,
    _chart_negative,
    _chart_positive,
    _chart_text,
    _chart_tooltip_bg,
)
from ui.core.theme import Theme
from ui.components.charts.presentation import format_axis_money
from utils.helpers import eur, safe_float


class NetWorthBridgeChart(StableChart):
    """Waterfall bridge between the opening and closing net worth."""

    def __init__(
        self,
        theme_provider: Callable[[], Theme],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(theme_provider, parent)
        self.data: dict[str, Any] = {}
        self.rows: list[dict[str, Any]] = []
        self.hover_index: Optional[int] = None
        self._bar_hits: list[tuple[int, QRectF]] = []
        self.setMinimumHeight(350)
        self.setAccessibleName("Ponte interativa do património")
        self.setAccessibleDescription(
            "Explica como receitas, despesas, capital, mercado e dívida "
            "alteraram o património líquido."
        )

    def set_data(self, data: dict[str, Any]) -> None:
        self.data = dict(data or {})
        self.rows = [
            dict(row)
            for row in list(self.data.get("steps") or [])
            if isinstance(row, dict)
        ]
        self.hover_index = None
        self._bar_hits.clear()
        if self.rows:
            change = safe_float(self.data.get("change"))
            explained = safe_float(
                self.data.get(
                    "explained_change_pct",
                    self.data.get("coverage_pct"),
                )
            )
            self.setAccessibleDescription(
                f"Património variou {eur(change, self.theme_provider().privacy)}. "
                f"Variação explicada {explained:.0f}%."
            )
        else:
            self.setAccessibleDescription(
                "Sem dados para construir a ponte patrimonial."
            )
        if self.rows and self.isVisible():
            self.start_entrance_animation(650)
        else:
            self.invalidate_cache()

    def _plot_rect(self) -> QRectF:
        compact = self.width() < 640
        top_pad = 58.0 if compact else 44.0
        return QRectF(
            75.0,
            top_pad,
            max(120.0, self.width() - 98.0),
            max(120.0, self.height() - top_pad - 70.0),
        )

    def _value_range(self) -> tuple[float, float]:
        values = [0.0]
        for row in self.rows:
            values.extend(
                (
                    safe_float(row.get("start")),
                    safe_float(row.get("end")),
                )
            )
        low, high = min(values), max(values)
        # BUG-CHART-WB-02: never force 0.0 into the scale - a fully negative
        # bridge (debts) must keep its true domain, otherwise negative bars
        # are flattened onto the axis.
        if abs(high - low) < 0.01:
            high = low + 1.0
        padding = (high - low) * 0.14
        return low - padding, high + padding

    @staticmethod
    def _axis_value(value: float, privacy: bool) -> str:
        return format_axis_money(value, privacy=privacy)

    @staticmethod
    def _row_color(row: dict[str, Any], theme: Theme) -> QColor:
        role = str(row.get("role") or "")
        key = str(row.get("key") or "")
        value = safe_float(row.get("value"))
        if role == "total":
            if key == "opening":
                return _chart_accent(theme)
            return _chart_positive(theme) if value >= 0 else _chart_negative(theme)
        if str(row.get("tone") or "") == "warning":
            return QColor("#FFB020" if theme.dark else "#C46A00")
        return _chart_positive(theme) if value >= 0 else _chart_negative(theme)

    def _index_at(self, position: QPointF) -> Optional[int]:
        for index, rect in reversed(self._bar_hits):
            if rect.adjusted(-5.0, -5.0, 5.0, 5.0).contains(position):
                return index
        return None

    def mouseMoveEvent(self, event):  # type: ignore[override]
        index = self._index_at(event.position())
        if index != self.hover_index:
            self.hover_index = index
            self.setCursor(
                Qt.PointingHandCursor if index is not None else Qt.ArrowCursor
            )
            self.invalidate_cache()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):  # type: ignore[override]
        self.hover_index = None
        self.setCursor(Qt.ArrowCursor)
        self.invalidate_cache()
        super().leaveEvent(event)

    def _draw_legend(self, painter: QPainter, plot: QRectF, theme: Theme) -> None:
        entries = (
            ("Aumenta", _chart_positive(theme)),
            ("Reduz", _chart_negative(theme)),
            (
                "Sem histórico",
                QColor("#FFB020" if theme.dark else "#C46A00"),
            ),
            ("Total", _chart_accent(theme)),
        )
        painter.setFont(QFont("Segoe UI", 8, QFont.DemiBold))

        explained = safe_float(
            self.data.get(
                "explained_change_pct",
                self.data.get("coverage_pct"),
            )
        )
        coverage_text = f"Variação explicada {explained:.0f}%"

        # Check whether legend and coverage collide horizontally on a single line
        compact = plot.width() < 580.0

        x = plot.left()
        y = 9.0
        row_height = 20.0
        for label, color in entries:
            item_width = 108.0 if label == "Sem histórico" else 78.0
            if x + item_width > plot.right() and x > plot.left():
                x = plot.left()
                y += row_height
            painter.setPen(Qt.NoPen)
            painter.setBrush(color)
            painter.drawRoundedRect(QRectF(x, y + 8.0, 13.0, 8.0), 4.0, 4.0)
            painter.setPen(_chart_muted(theme))
            painter.drawText(
                QRectF(x + 18.0, y, item_width - 18.0, 24.0),
                Qt.AlignLeft | Qt.AlignVCenter,
                label,
            )
            x += item_width + 4.0

        painter.setPen(
            _chart_positive(theme)
            if explained >= 99.5
            else QColor("#FFB020" if theme.dark else "#A85B00")
        )
        if compact:
            cov_y = max(y + row_height, 32.0)
            painter.drawText(
                QRectF(plot.left(), cov_y, plot.width(), 20.0),
                Qt.AlignRight | Qt.AlignVCenter,
                coverage_text,
            )
        else:
            painter.drawText(
                QRectF(plot.right() - 230.0, 8.0, 230.0, 25.0),
                Qt.AlignRight | Qt.AlignVCenter,
                coverage_text,
            )

    def _draw_tooltip(
        self,
        painter: QPainter,
        theme: Theme,
        bar_rect: QRectF,
    ) -> None:
        if self.hover_index is None or not 0 <= self.hover_index < len(self.rows):
            return
        row = self.rows[self.hover_index]
        width = min(330.0, max(230.0, self.width() - 18.0))
        height = 101.0
        x = max(8.0, min(bar_rect.center().x() + 12.0, self.width() - width - 8.0))
        y = max(8.0, min(bar_rect.top() - height - 10.0, self.height() - height - 8.0))
        if (
            y + height > bar_rect.top()
            and bar_rect.bottom() + height + 10 < self.height()
        ):
            y = bar_rect.bottom() + 10.0
        rect = QRectF(x, y, width, height)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(0, 0, 0, 38))
        painter.drawRoundedRect(rect.translated(0.0, 3.0), 12.0, 12.0)
        painter.setPen(QPen(_chart_grid(theme), 1))
        painter.setBrush(_chart_tooltip_bg(theme))
        painter.drawRoundedRect(rect, 12.0, 12.0)

        painter.setPen(_chart_text(theme))
        painter.setFont(QFont("Segoe UI", 9, QFont.Bold))
        painter.drawText(
            QRectF(x + 13.0, y + 8.0, width - 26.0, 22.0),
            Qt.AlignVCenter,
            str(row.get("label") or "Etapa"),
        )
        painter.setFont(QFont("Segoe UI", 8))
        painter.setPen(_chart_muted(theme))
        value = safe_float(row.get("value"))
        prefix = "+" if value > 0 and str(row.get("role")) != "total" else ""
        painter.drawText(
            QRectF(x + 13.0, y + 35.0, width - 26.0, 20.0),
            Qt.AlignVCenter,
            f"Impacto: {prefix}{eur(value, theme.privacy)}",
        )
        result = safe_float(row.get("after", row.get("end")))
        painter.drawText(
            QRectF(x + 13.0, y + 58.0, width - 26.0, 20.0),
            Qt.AlignVCenter,
            f"Património após esta etapa: {eur(result, theme.privacy)}",
        )
        painter.setPen(_chart_accent(theme))
        painter.setFont(QFont("Segoe UI", 8, QFont.DemiBold))
        painter.drawText(
            QRectF(x + 13.0, y + 79.0, width - 26.0, 16.0),
            Qt.AlignVCenter,
            "Clica para ver a explicação e fixar a comparação",
        )

    def render_chart(self, painter: QPainter) -> None:
        theme = self.theme_provider()
        painter.fillRect(self.rect(), _chart_bg(theme))
        self._bar_hits.clear()
        if len(self.rows) < 2:
            self.draw_empty(
                painter, "Sem histórico suficiente para a ponte patrimonial"
            )
            return

        plot = self._plot_rect()
        self._draw_legend(painter, plot, theme)
        low, high = self._value_range()

        def y_for(value: float) -> float:
            return plot.bottom() - plot.height() * (value - low) / max(1e-9, high - low)

        painter.setFont(QFont("Segoe UI", 8))
        for tick_index in range(5):
            value = high - (high - low) * tick_index / 4.0
            y = y_for(value)
            painter.setPen(QPen(_chart_grid(theme), 1))
            painter.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
            painter.setPen(_chart_muted(theme))
            painter.drawText(
                QRectF(3.0, y - 9.0, 67.0, 18.0),
                Qt.AlignRight | Qt.AlignVCenter,
                self._axis_value(value, theme.privacy),
            )

        zero_y = y_for(0.0)
        if plot.top() <= zero_y <= plot.bottom():
            painter.setPen(QPen(_chart_muted(theme), 1.2, Qt.DashLine))
            painter.drawLine(
                QPointF(plot.left(), zero_y), QPointF(plot.right(), zero_y)
            )

        slot = plot.width() / max(1, len(self.rows))
        bar_width = max(16.0, min(62.0, slot * 0.58))
        progress = max(0.0, min(1.0, self.anim_progress))
        label_font = QFont("Segoe UI", 8, QFont.DemiBold)
        value_font = QFont("Segoe UI", 8, QFont.Bold)
        previous_end: Optional[float] = None
        hovered_rect: Optional[QRectF] = None

        for index, row in enumerate(self.rows):
            center_x = plot.left() + slot * (index + 0.5)
            start = safe_float(row.get("start"))
            end = safe_float(row.get("end"))
            role = str(row.get("role") or "")
            # BUG-CHART-WB-01: the closing bar ("Património final") must also
            # receive a connector; link every bar with a left neighbour to the
            # previous running total (identical to start for change rows).
            if previous_end is not None and index > 0:
                connector_y = y_for(previous_end)
                painter.setPen(QPen(_chart_muted(theme), 1.0, Qt.DashLine))
                painter.drawLine(
                    QPointF(center_x - slot + bar_width / 2.0, connector_y),
                    QPointF(center_x - bar_width / 2.0, connector_y),
                )
            previous_end = end

            animated_start = 0.0 if role == "total" else start
            animated_end = animated_start + (end - animated_start) * progress
            top = min(y_for(animated_start), y_for(animated_end))
            bottom = max(y_for(animated_start), y_for(animated_end))
            rect = QRectF(
                center_x - bar_width / 2.0,
                top,
                bar_width,
                max(3.0, bottom - top),
            )
            color = self._row_color(row, theme)
            fill = QColor(color)
            fill.setAlpha(225 if index == self.hover_index else 185)
            painter.setPen(
                QPen(color.lighter(120), 2.0 if index == self.hover_index else 1.0)
            )
            painter.setBrush(fill)
            painter.drawRoundedRect(rect, 5.0, 5.0)
            hit_rect = rect.adjusted(-slot * 0.17, -6.0, slot * 0.17, 6.0)
            self._bar_hits.append((index, hit_rect))
            if index == self.hover_index:
                hovered_rect = rect

            value = safe_float(row.get("value"))
            prefix = "+" if value > 0 and role != "total" else ""
            value_text = prefix + self._axis_value(value, theme.privacy)
            painter.setFont(value_font)
            painter.setPen(_chart_text(theme))
            # BUG-CHART-WB-02: when the bar top reaches the plot top the label
            # would overlap the frame; draw it inside the bar instead of
            # clamping it onto the edge.
            label_y = max(plot.top(), rect.top() - 22.0)
            if label_y <= plot.top() + 0.5:
                label_y = rect.top() + 4.0
            painter.drawText(
                QRectF(center_x - slot / 2.0, label_y, slot, 18.0),
                Qt.AlignHCenter | Qt.AlignVCenter,
                value_text,
            )

            painter.setFont(label_font)
            painter.setPen(_chart_muted(theme))
            raw_label = str(row.get("label") or "")
            label = QFontMetrics(label_font).elidedText(
                raw_label,
                Qt.ElideRight,
                max(42, int(slot - 7.0)),
            )
            painter.drawText(
                QRectF(
                    center_x - slot / 2.0,
                    plot.bottom() + 12.0,
                    slot,
                    38.0,
                ),
                Qt.AlignHCenter | Qt.AlignTop | Qt.TextWordWrap,
                label,
            )

        if hovered_rect is not None:
            self._draw_tooltip(painter, theme, hovered_rect)


__all__ = ["NetWorthBridgeChart"]
