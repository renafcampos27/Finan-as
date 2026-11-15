from __future__ import annotations

from typing import Any, Callable, Optional

from PySide6.QtCore import QEvent, QPointF, QRectF, Qt
from PySide6.QtGui import QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget

from ui.components.charts.base import StableChart
from ui.components.charts.utils import (
    _chart_accent,
    _chart_bg,
    _chart_grid,
    _chart_muted,
    _chart_positive,
    _chart_text,
    _chart_tooltip_bg,
)
from ui.core.theme import Theme
from ui.components.charts.presentation import format_axis_money
from utils.helpers import eur, safe_float


class HabitPlanTrackingChart(StableChart):
    """Cumulative planned-versus-real spending for one saved habit plan."""

    def __init__(
        self,
        theme_provider: Callable[[], Theme],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(theme_provider, parent)
        self.rows: list[dict[str, Any]] = []
        self.plan_name = "Plano"
        self.hover_index: Optional[int] = None
        self.setMinimumHeight(370)
        self.setAccessibleName("Evolução do custo planeado face ao custo realizado")
        self.setToolTip("")

    def event(self, event) -> bool:  # type: ignore[override]
        if event.type() == QEvent.ToolTip:
            event.accept()
            return True
        return super().event(event)

    def set_data(self, data: dict[str, Any]) -> None:
        self.plan_name = str(data.get("plan_name") or "Plano")
        self.rows = [
            {
                "label": str(row.get("label") or row.get("month") or ""),
                "month": str(row.get("month") or ""),
                "planned": max(0.0, safe_float(row.get("cumulative_planned"))),
                "actual": max(0.0, safe_float(row.get("cumulative_actual"))),
                "planned_month": max(0.0, safe_float(row.get("planned_cost"))),
                "actual_month": max(0.0, safe_float(row.get("actual_cost"))),
                "partial": bool(row.get("partial")),
            }
            for row in (data.get("rows") or [])
        ]
        self.hover_index = None
        if self.rows:
            last = self.rows[-1]
            theme = self.theme_provider()
            self.setAccessibleDescription(
                f"{self.plan_name}: planeado acumulado {eur(last['planned'], theme.privacy)} "
                f"e realizado {eur(last['actual'], theme.privacy)}."
            )
        else:
            self.setAccessibleDescription(
                "Ainda não existem meses observados neste plano."
            )
        self.invalidate_cache()

    def _plot(self) -> QRectF:
        return QRectF(
            76.0,
            55.0,
            max(180.0, self.width() - 98.0),
            max(190.0, self.height() - 104.0),
        )

    @staticmethod
    def _path(points: list[QPointF]) -> QPainterPath:
        path = QPainterPath()
        if not points:
            return path
        path.moveTo(points[0])
        for point in points[1:]:
            path.lineTo(point)
        return path

    def _points(self, key: str, high: float) -> list[QPointF]:
        plot = self._plot()
        denominator = max(1, len(self.rows) - 1)
        return [
            QPointF(
                plot.left() + plot.width() * index / denominator,
                plot.bottom()
                - plot.height() * safe_float(row.get(key)) / max(1.0, high),
            )
            for index, row in enumerate(self.rows)
        ]

    def _index_at_x(self, x: float) -> Optional[int]:
        if not self.rows:
            return None
        plot = self._plot()
        ratio = max(0.0, min(1.0, (x - plot.left()) / max(1.0, plot.width())))
        return min(len(self.rows) - 1, max(0, round(ratio * (len(self.rows) - 1))))

    def mouseMoveEvent(self, event):  # type: ignore[override]
        self.hover_index = self._index_at_x(float(event.position().x()))
        self.setCursor(
            Qt.CrossCursor if self.hover_index is not None else Qt.ArrowCursor
        )
        self.invalidate_cache()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):  # type: ignore[override]
        self.hover_index = None
        self.setCursor(Qt.ArrowCursor)
        self.invalidate_cache()
        super().leaveEvent(event)

    @staticmethod
    def _axis_value(value: float, privacy: bool) -> str:
        return format_axis_money(value, privacy=privacy)

    def _tooltip(self, painter: QPainter, points: list[QPointF]) -> None:
        if self.hover_index is None or not (0 <= self.hover_index < len(self.rows)):
            return
        theme = self.theme_provider()
        row = self.rows[self.hover_index]
        anchor = points[self.hover_index]
        width = min(330.0, max(285.0, self.width() - 16.0))
        height = 132.0
        x = max(8.0, min(anchor.x() + 13.0, self.width() - width - 8.0))
        y = max(8.0, min(anchor.y() - height - 12.0, self.height() - height - 8.0))
        if anchor.y() - height < 8.0:
            y = max(8.0, min(anchor.y() + 13.0, self.height() - height - 8.0))
        rect = QRectF(x, y, width, height)
        painter.setPen(QPen(_chart_grid(theme), 1))
        painter.setBrush(_chart_tooltip_bg(theme))
        painter.drawRoundedRect(rect, 13.0, 13.0)
        painter.setPen(_chart_text(theme))
        painter.setFont(QFont("Segoe UI", 9, QFont.Bold))
        suffix = " · mês parcial" if row["partial"] else ""
        painter.drawText(
            QRectF(rect.left() + 13, rect.top() + 8, rect.width() - 26, 22),
            Qt.AlignVCenter,
            f"{row['label']}{suffix}",
        )
        lines = (
            ("Planeado no mês", eur(row["planned_month"], theme.privacy)),
            ("Realizado no mês", eur(row["actual_month"], theme.privacy)),
            ("Diferença acumulada", eur(row["actual"] - row["planned"], theme.privacy)),
        )
        painter.setFont(QFont("Segoe UI", 8))
        for index, (label, value) in enumerate(lines):
            line_y = rect.top() + 38 + index * 27
            painter.setPen(_chart_muted(theme))
            painter.drawText(
                QRectF(rect.left() + 13, line_y, 155, 21),
                Qt.AlignLeft | Qt.AlignVCenter,
                label,
            )
            painter.setPen(_chart_text(theme))
            painter.drawText(
                QRectF(rect.left() + 170, line_y, rect.width() - 183, 21),
                Qt.AlignRight | Qt.AlignVCenter,
                value,
            )

    def render_chart(self, painter: QPainter) -> None:
        theme = self.theme_provider()
        painter.fillRect(self.rect(), _chart_bg(theme))
        if not self.rows:
            self.draw_empty(
                painter, "O acompanhamento começa na data definida no plano"
            )
            return
        plot = self._plot()
        high = max(
            1.0,
            max(max(row["planned"], row["actual"]) for row in self.rows) * 1.14,
        )
        painter.setFont(QFont("Segoe UI", 8))
        for step in range(5):
            ratio = step / 4.0
            y = plot.top() + plot.height() * ratio
            painter.setPen(QPen(_chart_grid(theme), 1))
            painter.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
            painter.setPen(_chart_muted(theme))
            painter.drawText(
                QRectF(2, y - 9, 66, 18),
                Qt.AlignRight | Qt.AlignVCenter,
                self._axis_value(high * (1 - ratio), theme.privacy),
            )

        planned_points = self._points("planned", high)
        actual_points = self._points("actual", high)
        planned_pen = QPen(_chart_accent(theme), 2.5, Qt.DashLine)
        actual_pen = QPen(_chart_positive(theme), 3.2)
        for pen in (planned_pen, actual_pen):
            pen.setCapStyle(Qt.RoundCap)
            pen.setJoinStyle(Qt.RoundJoin)
        painter.setBrush(Qt.NoBrush)
        painter.setPen(planned_pen)
        painter.drawPath(self._path(planned_points))
        painter.setPen(actual_pen)
        painter.drawPath(self._path(actual_points))

        for point in planned_points:
            painter.setPen(QPen(_chart_bg(theme), 1.5))
            painter.setBrush(_chart_accent(theme))
            painter.drawEllipse(point, 3.5, 3.5)
        for point in actual_points:
            painter.setPen(QPen(_chart_bg(theme), 1.5))
            painter.setBrush(_chart_positive(theme))
            painter.drawEllipse(point, 4.0, 4.0)

        tick_count = min(6, len(self.rows))
        indices = sorted(
            {
                round(index * (len(self.rows) - 1) / max(1, tick_count - 1))
                for index in range(tick_count)
            }
        )
        painter.setFont(QFont("Segoe UI", 8, QFont.DemiBold))
        for index in indices:
            point = planned_points[index]
            painter.setPen(_chart_muted(theme))
            painter.drawText(
                QRectF(point.x() - 36, plot.bottom() + 8, 72, 20),
                Qt.AlignHCenter | Qt.AlignTop,
                self.rows[index]["label"],
            )

        painter.setFont(QFont("Segoe UI", 8, QFont.DemiBold))
        painter.setPen(planned_pen)
        painter.drawLine(QPointF(plot.left(), 18), QPointF(plot.left() + 20, 18))
        painter.setPen(_chart_text(theme))
        painter.drawText(
            QRectF(plot.left() + 27, 8, 105, 20),
            Qt.AlignVCenter,
            "Planeado",
        )
        painter.setPen(actual_pen)
        painter.drawLine(QPointF(plot.left() + 145, 18), QPointF(plot.left() + 165, 18))
        painter.setPen(_chart_text(theme))
        painter.drawText(
            QRectF(plot.left() + 172, 8, 105, 20),
            Qt.AlignVCenter,
            "Realizado",
        )

        if self.hover_index is not None and 0 <= self.hover_index < len(self.rows):
            point = actual_points[self.hover_index]
            painter.setPen(QPen(_chart_grid(theme), 1, Qt.DotLine))
            painter.drawLine(
                QPointF(point.x(), plot.top()), QPointF(point.x(), plot.bottom())
            )
            self._tooltip(painter, actual_points)


__all__ = ["HabitPlanTrackingChart"]
