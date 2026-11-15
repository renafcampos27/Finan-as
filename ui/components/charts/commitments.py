from __future__ import annotations

from typing import Any, Callable, Optional

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget

from ui.components.charts.base import StableChart
from ui.components.charts.utils import (
    _chart_accent,
    _chart_bg,
    _chart_grid,
    _chart_muted,
    _chart_text,
    _chart_tooltip_bg,
)
from ui.core.theme import Theme
from ui.components.charts.presentation import format_axis_money
from utils.helpers import eur, safe_float


class FixedCommitmentsChart(StableChart):
    """Stacked future evolution of explicit fixed commitments."""

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
        self.setMinimumHeight(335)
        self.setAccessibleName("Evolução dos compromissos fixos")
        self.setAccessibleDescription(
            "Barras mensais com subscrições e pagamentos agendados."
        )

    @staticmethod
    def _subscription_color(theme: Theme) -> QColor:
        return QColor("#FFB84D" if theme.dark else "#E87900")

    @staticmethod
    def _scheduled_color(theme: Theme) -> QColor:
        return QColor("#8B7CFF" if theme.dark else "#5B4FD6")

    def set_data(self, data: dict[str, Any]) -> None:
        self.data = dict(data or {})
        self.rows = [
            dict(row)
            for row in list(self.data.get("rows") or [])
            if isinstance(row, dict)
        ]
        self.hover_index = None
        self._bar_hits.clear()
        if self.rows:
            average = safe_float(self.data.get("monthly_average"))
            self.setAccessibleDescription(
                f"{len(self.rows)} meses. Compromisso médio mensal "
                f"{eur(average, self.theme_provider().privacy)}."
            )
        else:
            self.setAccessibleDescription("Sem compromissos fixos previstos.")
        if self.rows and self.isVisible():
            self.start_entrance_animation(620)
        else:
            self.invalidate_cache()

    def _plot_rect(self) -> QRectF:
        return QRectF(
            70.0,
            49.0,
            max(120.0, self.width() - 92.0),
            max(120.0, self.height() - 103.0),
        )

    @staticmethod
    def _axis_value(value: float, privacy: bool) -> str:
        return format_axis_money(value, privacy=privacy)

    def _index_at(self, position: QPointF) -> Optional[int]:
        for index, rect in reversed(self._bar_hits):
            if rect.contains(position):
                return index
        return None

    def mouseMoveEvent(self, event):  # type: ignore[override]
        index = self._index_at(event.position())
        if index != self.hover_index:
            self.hover_index = index
            self.emit_period_for_hover(index)
            self.setCursor(
                Qt.PointingHandCursor if index is not None else Qt.ArrowCursor
            )
            self.invalidate_cache()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):  # type: ignore[override]
        self.hover_index = None
        self.emit_period_for_hover(None)
        self.setCursor(Qt.ArrowCursor)
        self.invalidate_cache()
        super().leaveEvent(event)

    def _draw_legend(self, painter: QPainter, plot: QRectF, theme: Theme) -> None:
        entries = (
            ("Subscrições", self._subscription_color(theme)),
            ("Pagamentos agendados", self._scheduled_color(theme)),
            ("Total mensal", _chart_accent(theme)),
        )
        painter.setFont(QFont("Segoe UI", 8, QFont.DemiBold))
        x = plot.left()
        for index, (label, color) in enumerate(entries):
            painter.setPen(QPen(color, 2.0 if index == 2 else 1.0))
            if index == 2:
                painter.drawLine(QPointF(x, 21.0), QPointF(x + 15.0, 21.0))
            else:
                painter.setBrush(color)
                painter.drawRoundedRect(QRectF(x, 16.5, 15.0, 9.0), 3.0, 3.0)
            painter.setPen(_chart_muted(theme))
            width = 82.0 if index == 0 else 142.0 if index == 1 else 82.0
            painter.drawText(
                QRectF(x + 20.0, 9.0, width, 24.0),
                Qt.AlignLeft | Qt.AlignVCenter,
                label,
            )
            x += width + 26.0

    def _draw_tooltip(
        self,
        painter: QPainter,
        theme: Theme,
        bar_rect: QRectF,
    ) -> None:
        if self.hover_index is None or not 0 <= self.hover_index < len(self.rows):
            return
        row = self.rows[self.hover_index]
        width = min(315.0, max(225.0, self.width() - 18.0))
        height = 118.0
        x = max(8.0, min(bar_rect.center().x() + 12.0, self.width() - width - 8.0))
        y = bar_rect.top() - height - 10.0
        if y < 8.0:
            y = min(self.height() - height - 8.0, bar_rect.bottom() + 10.0)
        rect = QRectF(x, max(8.0, y), width, height)

        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(0, 0, 0, 38))
        painter.drawRoundedRect(rect.translated(0.0, 3.0), 12.0, 12.0)
        painter.setPen(QPen(_chart_grid(theme), 1))
        painter.setBrush(_chart_tooltip_bg(theme))
        painter.drawRoundedRect(rect, 12.0, 12.0)
        painter.setPen(_chart_text(theme))
        painter.setFont(QFont("Segoe UI", 9, QFont.Bold))
        partial = " · mês parcial" if bool(row.get("partial")) else ""
        painter.drawText(
            QRectF(rect.left() + 13.0, rect.top() + 8.0, width - 26.0, 22.0),
            Qt.AlignVCenter,
            f"{row.get('label') or row.get('month')}{partial}",
        )
        painter.setFont(QFont("Segoe UI", 8))
        painter.setPen(_chart_muted(theme))
        lines = (
            f"Subscrições: {eur(row.get('subscriptions', 0), theme.privacy)}",
            f"Agendados: {eur(row.get('scheduled', 0), theme.privacy)}",
            f"Total: {eur(row.get('total', 0), theme.privacy)} · "
            f"{int(row.get('event_count', 0) or 0)} evento(s)",
        )
        for index, line in enumerate(lines):
            painter.drawText(
                QRectF(
                    rect.left() + 13.0,
                    rect.top() + 35.0 + index * 22.0,
                    width - 26.0,
                    20.0,
                ),
                Qt.AlignVCenter,
                line,
            )
        painter.setPen(_chart_accent(theme))
        painter.setFont(QFont("Segoe UI", 8, QFont.DemiBold))
        painter.drawText(
            QRectF(rect.left() + 13.0, rect.bottom() - 20.0, width - 26.0, 15.0),
            Qt.AlignVCenter,
            "Clica para consultar os compromissos",
        )

    def render_chart(self, painter: QPainter) -> None:
        theme = self.theme_provider()
        painter.fillRect(self.rect(), _chart_bg(theme))
        self._bar_hits.clear()
        if not self.rows or not any(
            safe_float(row.get("total")) > 0 for row in self.rows
        ):
            self.draw_empty(
                painter, "Sem compromissos fixos conhecidos neste horizonte"
            )
            return

        plot = self._plot_rect()
        self._draw_legend(painter, plot, theme)
        maximum = max(safe_float(row.get("total")) for row in self.rows)
        maximum = max(1.0, maximum * 1.16)

        def y_for(value: float) -> float:
            return plot.bottom() - plot.height() * max(0.0, value) / maximum

        painter.setFont(QFont("Segoe UI", 8))
        for tick_index in range(5):
            value = maximum * (4 - tick_index) / 4.0
            y = y_for(value)
            painter.setPen(QPen(_chart_grid(theme), 1))
            painter.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
            painter.setPen(_chart_muted(theme))
            painter.drawText(
                QRectF(3.0, y - 9.0, 62.0, 18.0),
                Qt.AlignRight | Qt.AlignVCenter,
                self._axis_value(value, theme.privacy),
            )

        slot = plot.width() / max(1, len(self.rows))
        bar_width = max(10.0, min(48.0, slot * 0.62))
        progress = max(0.0, min(1.0, self.anim_progress))
        subscription_color = self._subscription_color(theme)
        scheduled_color = self._scheduled_color(theme)
        total_color = _chart_accent(theme)
        total_points: list[QPointF] = []
        hovered_rect: Optional[QRectF] = None

        for index, row in enumerate(self.rows):
            center_x = plot.left() + slot * (index + 0.5)
            subscriptions = safe_float(row.get("subscriptions")) * progress
            scheduled = safe_float(row.get("scheduled")) * progress
            total = subscriptions + scheduled
            bottom = plot.bottom()
            subscription_top = y_for(subscriptions)
            total_top = y_for(total)
            subscription_rect = QRectF(
                center_x - bar_width / 2.0,
                subscription_top,
                bar_width,
                max(0.0, bottom - subscription_top),
            )
            scheduled_rect = QRectF(
                center_x - bar_width / 2.0,
                total_top,
                bar_width,
                max(0.0, subscription_top - total_top),
            )
            for rect, color, round_top, round_bottom in (
                (subscription_rect, subscription_color, False, True),
                (scheduled_rect, scheduled_color, True, False),
            ):
                if rect.height() <= 0:
                    continue
                fill = QColor(color)
                fill.setAlpha(235 if index == self.hover_index else 190)
                painter.setPen(
                    QPen(
                        color.lighter(120),
                        2.0 if index == self.hover_index else 1.0,
                    )
                )
                painter.setBrush(fill)
                # BUG-CHART-COMMITMENTS-ROUNDED-STACK-NOTCH-010: rounding all
                # four corners of both stacked segments carved concave notches
                # at the junction.  Round only the outer corners of the column
                # (bottom of the lower segment, top of the upper segment).
                radius = 4.0
                segment = QPainterPath()
                top_left_r = radius if round_top else 0.0
                top_right_r = radius if round_top else 0.0
                bottom_left_r = radius if round_bottom else 0.0
                bottom_right_r = radius if round_bottom else 0.0
                if top_left_r or top_right_r or bottom_left_r or bottom_right_r:
                    segment.moveTo(rect.left(), rect.top() + top_left_r)
                    segment.lineTo(rect.right() - top_right_r, rect.top())
                    segment.arcTo(
                        rect.right() - 2 * top_right_r,
                        rect.top(),
                        2 * top_right_r,
                        2 * top_right_r,
                        90,
                        -90,
                    )
                    segment.lineTo(rect.right(), rect.bottom() - bottom_right_r)
                    segment.arcTo(
                        rect.right() - 2 * bottom_right_r,
                        rect.bottom() - 2 * bottom_right_r,
                        2 * bottom_right_r,
                        2 * bottom_right_r,
                        0,
                        -90,
                    )
                    segment.lineTo(rect.left() + bottom_left_r, rect.bottom())
                    segment.arcTo(
                        rect.left(),
                        rect.bottom() - 2 * bottom_left_r,
                        2 * bottom_left_r,
                        2 * bottom_left_r,
                        270,
                        -90,
                    )
                    segment.lineTo(rect.left(), rect.top() + top_left_r)
                    segment.arcTo(
                        rect.left(),
                        rect.top(),
                        2 * top_left_r,
                        2 * top_left_r,
                        180,
                        -90,
                    )
                    segment.closeSubpath()
                else:
                    segment.addRect(rect)
                painter.drawPath(segment)

            hit_rect = QRectF(
                center_x - slot / 2.0,
                plot.top(),
                slot,
                plot.height() + 34.0,
            )
            self._bar_hits.append((index, hit_rect))
            total_points.append(QPointF(center_x, total_top))
            if index == self.hover_index:
                hovered_rect = QRectF(
                    center_x - bar_width / 2.0,
                    total_top,
                    bar_width,
                    max(3.0, bottom - total_top),
                )

            painter.setPen(_chart_muted(theme))
            painter.setFont(QFont("Segoe UI", 8, QFont.DemiBold))
            label = str(row.get("label") or row.get("month") or "")
            if bool(row.get("partial")):
                label += "*"
            painter.drawText(
                QRectF(
                    center_x - slot / 2.0,
                    plot.bottom() + 9.0,
                    slot,
                    19.0,
                ),
                Qt.AlignHCenter | Qt.AlignTop,
                label,
            )
            if self.width() >= 760 or index == self.hover_index:
                painter.setPen(_chart_text(theme))
                painter.setFont(QFont("Segoe UI", 7, QFont.Bold))
                painter.drawText(
                    QRectF(
                        center_x - slot / 2.0,
                        max(plot.top(), total_top - 19.0),
                        slot,
                        16.0,
                    ),
                    Qt.AlignHCenter | Qt.AlignVCenter,
                    self._axis_value(
                        safe_float(row.get("total")),
                        theme.privacy,
                    ),
                )

        if total_points:
            path = QPainterPath(total_points[0])
            for point in total_points[1:]:
                path.lineTo(point)
            painter.setPen(QPen(total_color, 2.2))
            painter.setBrush(Qt.NoBrush)
            painter.drawPath(path)
            painter.setBrush(total_color)
            for index, point in enumerate(total_points):
                radius = 4.5 if index == self.hover_index else 3.0
                painter.drawEllipse(point, radius, radius)

        if hovered_rect is not None:
            self._draw_tooltip(painter, theme, hovered_rect)


__all__ = ["FixedCommitmentsChart"]
