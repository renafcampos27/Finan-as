from __future__ import annotations

from typing import Any, Callable, Optional

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPolygonF,
)
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
from utils.config import parse_date
from utils.helpers import eur, safe_float


class CashflowTimelineChart(StableChart):
    """Interactive future balance line with explicit cash event markers."""

    event_clicked = Signal(dict)

    def __init__(
        self, theme_provider: Callable[[], Theme], parent: Optional[QWidget] = None
    ):
        super().__init__(theme_provider, parent)
        self.all_points: list[dict[str, Any]] = []
        self.all_events: list[dict[str, Any]] = []
        self.points: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []
        self.horizon_days = 60
        self.hover_index: Optional[int] = None
        self.hover_event: Optional[dict[str, Any]] = None
        self._hover_pos = QPointF()
        self._event_hits: list[tuple[dict[str, Any], QRectF]] = []
        self.setMinimumHeight(390)
        self.setAccessibleName("Linha temporal da tesouraria futura")
        self.setAccessibleDescription(
            "Passa o rato para explorar o saldo e clica num evento para abrir os detalhes."
        )

    def set_data(self, data: dict[str, Any], horizon_days: int = 60) -> None:
        self.all_points = [dict(point) for point in data.get("points", []) or []]
        self.all_events = [dict(event) for event in data.get("events", []) or []]
        self.set_horizon(horizon_days, animate=False)

    def set_horizon(self, horizon_days: int, *, animate: bool = True) -> None:
        available = max(1, len(self.all_points) - 1)
        self.horizon_days = max(1, min(available, int(horizon_days or 60)))
        self.points = self.all_points[: self.horizon_days + 1]
        if self.points:
            end = parse_date(str(self.points[-1].get("date"))[:10])
            self.events = [
                event
                for event in self.all_events
                if parse_date(str(event.get("date"))[:10]) <= end
            ]
        else:
            self.events = []
        self.hover_index = None
        self.hover_event = None
        self._event_hits.clear()
        self._update_accessible_description()
        if animate and self.isVisible() and self.points:
            self.start_entrance_animation(620)
        else:
            self.invalidate_cache()

    def _update_accessible_description(self) -> None:
        if self.points:
            theme = self.theme_provider()
            minimum = min(safe_float(point.get("balance")) for point in self.points)
            ending = safe_float(self.points[-1].get("balance"))
            pessimistic = safe_float(self.points[-1].get("balance_pessimistic", ending))
            optimistic = safe_float(self.points[-1].get("balance_optimistic", ending))
            self.setAccessibleDescription(
                f"Previsão a {self.horizon_days} dias com {len(self.events)} eventos. "
                f"Saldo final base {eur(ending, theme.privacy)}, entre "
                f"{eur(pessimistic, theme.privacy)} e "
                f"{eur(optimistic, theme.privacy)}; mínimo "
                f"{eur(minimum, theme.privacy)}."
            )
        else:
            self.setAccessibleDescription("Sem dados de previsão")

    def invalidate_cache(self) -> None:
        self._update_accessible_description()
        super().invalidate_cache()

    def _plot_rect(self) -> QRectF:
        return QRectF(
            72.0,
            48.0,
            max(100.0, self.width() - 96.0),
            max(100.0, self.height() - 98.0),
        )

    def _value_range(self) -> tuple[float, float]:
        values = [
            safe_float(point.get(key, point.get("balance")))
            for point in self.points
            for key in (
                "balance_pessimistic",
                "balance_base",
                "balance_optimistic",
            )
        ] + [0.0]
        low, high = min(values), max(values)
        if abs(high - low) < 0.01:
            high = low + 1.0
        padding = (high - low) * 0.13
        return low - padding, high + padding

    @staticmethod
    def _axis_value(value: float, privacy: bool) -> str:
        return format_axis_money(value, privacy=privacy)

    @staticmethod
    def _source_color(source: str, theme: Theme) -> QColor:
        if source == "recurring_income":
            return _chart_positive(theme)
        if source == "subscription":
            return QColor("#FFB020" if theme.dark else "#C46A00")
        if source == "scheduled":
            return _chart_negative(theme)
        return _chart_accent(theme)

    @staticmethod
    def _source_label(source: str) -> str:
        return {
            "recurring_income": "Receita prevista",
            "subscription": "Subscrição",
            "scheduled": "Pagamento agendado",
            "recorded": "Já registado",
        }.get(source, "Evento")

    @staticmethod
    def _draw_marker(
        painter: QPainter,
        center: QPointF,
        source: str,
        color: QColor,
        size: float = 6.0,
    ) -> None:
        painter.setPen(QPen(color.lighter(125), 1.0))
        painter.setBrush(color)
        if source == "recurring_income":
            painter.drawPolygon(
                QPolygonF(
                    [
                        QPointF(center.x(), center.y() - size),
                        QPointF(center.x() - size, center.y() + size),
                        QPointF(center.x() + size, center.y() + size),
                    ]
                )
            )
        elif source == "scheduled":
            painter.drawPolygon(
                QPolygonF(
                    [
                        QPointF(center.x(), center.y() - size),
                        QPointF(center.x() - size, center.y()),
                        QPointF(center.x(), center.y() + size),
                        QPointF(center.x() + size, center.y()),
                    ]
                )
            )
        elif source == "recorded":
            painter.drawRoundedRect(
                QRectF(
                    center.x() - size,
                    center.y() - size,
                    size * 2,
                    size * 2,
                ),
                2.0,
                2.0,
            )
        else:
            painter.drawEllipse(center, size, size)

    def _event_at(self, pos: QPointF) -> Optional[dict[str, Any]]:
        for event, rect in reversed(self._event_hits):
            if rect.contains(pos):
                return event
        return None

    def _point_index_at(self, x: float, y: Optional[float] = None) -> Optional[int]:
        if not self.points:
            return None
        plot = self._plot_rect()
        tol = 0.5
        if x < (plot.left() - tol) or x > (plot.right() + tol):
            return None
        if y is not None and (y < (plot.top() - tol) or y > (plot.bottom() + tol)):
            return None
        ratio = (x - plot.left()) / max(1.0, plot.width())
        ratio = max(0.0, min(1.0, ratio))
        return max(0, min(len(self.points) - 1, round(ratio * (len(self.points) - 1))))

    def mouseMoveEvent(self, event):  # type: ignore[override]
        pos = event.position()
        hovered_event = self._event_at(pos)
        hovered_index = self._point_index_at(pos.x(), pos.y())
        self._hover_pos = pos
        if hovered_event is not self.hover_event or hovered_index != self.hover_index:
            self.hover_event = hovered_event
            self.hover_index = hovered_index
            self.setCursor(
                Qt.PointingHandCursor
                if hovered_event is not None
                else Qt.CrossCursor
                if hovered_index is not None
                else Qt.ArrowCursor
            )
            self.invalidate_cache()
            self.update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):  # type: ignore[override]
        self.hover_index = None
        self.hover_event = None
        self.setCursor(Qt.ArrowCursor)
        self.invalidate_cache()
        super().leaveEvent(event)

    def mousePressEvent(self, event):  # type: ignore[override]
        if event.button() == Qt.LeftButton:
            selected = self._event_at(event.position())
            if selected is not None:
                detail = dict(selected)
                if self.open_detail(
                    detail,
                    label=str(detail.get("name") or ""),
                    action_label=self._detail_action_label,
                ):
                    self.event_clicked.emit(detail)
                event.accept()
                return
        super().mousePressEvent(event)

    def _draw_legend(self, painter: QPainter, plot: QRectF, theme: Theme) -> None:
        entries = [
            ("recurring_income", "Receita"),
            ("subscription", "Subscrição"),
            ("scheduled", "Agendado"),
            ("recorded", "Registado"),
        ]
        painter.setFont(QFont("Segoe UI", 8, QFont.DemiBold))
        x = plot.left()
        for source, label in entries:
            color = self._source_color(source, theme)
            self._draw_marker(painter, QPointF(x + 6.0, 19.0), source, color, 4.2)
            painter.setPen(_chart_muted(theme))
            painter.drawText(
                QRectF(x + 16.0, 9.0, 78.0, 21.0),
                Qt.AlignLeft | Qt.AlignVCenter,
                label,
            )
            x += 91.0
        range_x = plot.right() - 132.0
        if range_x > x + 6.0:
            band = QColor(_chart_accent(theme))
            band.setAlpha(38 if theme.dark else 25)
            painter.setPen(QPen(_chart_accent(theme), 1))
            painter.setBrush(band)
            painter.drawRoundedRect(QRectF(range_x, 13.0, 24.0, 12.0), 5.0, 5.0)
            painter.setPen(_chart_muted(theme))
            painter.drawText(
                QRectF(range_x + 31.0, 9.0, 101.0, 21.0),
                Qt.AlignLeft | Qt.AlignVCenter,
                "Faixa provável",
            )

    def _draw_tooltip(
        self,
        painter: QPainter,
        theme: Theme,
        point_positions: list[QPointF],
    ) -> None:
        if self.hover_index is None or not (0 <= self.hover_index < len(self.points)):
            return
        point = self.points[self.hover_index]
        date_text = str(point.get("date") or "")
        day_events = [event for event in self.events if event.get("date") == date_text]
        width = min(326.0, max(220.0, self.width() - 18.0))
        visible_events = day_events[:2]
        hidden_events = max(0, len(day_events) - len(visible_events))
        event_lines = len(visible_events) + (1 if hidden_events else 0)
        height = 108.0 + event_lines * 19.0
        anchor = point_positions[self.hover_index]
        x = max(8.0, min(anchor.x() + 14.0, self.width() - width - 8.0))
        y = anchor.y() - height - 12.0
        if y < 8.0:
            y = anchor.y() + 14.0
        y = max(8.0, min(y, self.height() - height - 8.0))
        rect = QRectF(x, y, width, height)

        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(0, 0, 0, 42))
        painter.drawRoundedRect(rect.translated(0.0, 3.0), 12.0, 12.0)
        painter.setPen(QPen(_chart_grid(theme), 1))
        painter.setBrush(_chart_tooltip_bg(theme))
        painter.drawRoundedRect(rect, 12.0, 12.0)

        try:
            formatted_date = parse_date(date_text).strftime("%d/%m/%Y")
        except Exception:
            formatted_date = date_text
        painter.setPen(_chart_text(theme))
        painter.setFont(QFont("Segoe UI", 9, QFont.Bold))
        painter.drawText(
            QRectF(x + 13.0, y + 7.0, width - 26.0, 23.0),
            Qt.AlignVCenter,
            f"{formatted_date}  ·  {eur(safe_float(point.get('balance')), theme.privacy)}",
        )
        painter.setPen(_chart_muted(theme))
        painter.setFont(QFont("Segoe UI", 8))
        painter.drawText(
            QRectF(x + 13.0, y + 33.0, width - 26.0, 18.0),
            Qt.AlignVCenter,
            "Faixa "
            f"{eur(point.get('balance_pessimistic', point.get('balance', 0)), theme.privacy)} "
            "↔ "
            f"{eur(point.get('balance_optimistic', point.get('balance', 0)), theme.privacy)}",
        )
        painter.drawText(
            QRectF(x + 13.0, y + 55.0, width - 26.0, 18.0),
            Qt.AlignVCenter,
            f"Entradas {eur(point.get('income', 0), theme.privacy)}  ·  Saídas {eur(point.get('expense', 0), theme.privacy)}",
        )
        baseline = safe_float(point.get("baseline_expense"))
        painter.drawText(
            QRectF(x + 13.0, y + 75.0, width - 26.0, 18.0),
            Qt.AlignVCenter,
            f"Estimativa diária incluída: {eur(baseline, theme.privacy)}",
        )
        for index, cash_event in enumerate(visible_events):
            amount = safe_float(cash_event.get("amount"))
            # BUG-AUD-C135: under privacy mode eur() masks the value; a manual
            # "+" prefix would still leak the movement direction, so drop it.
            prefix = "" if theme.privacy else ("+" if amount > 0 else "")
            event_text = (
                f"{self._source_label(str(cash_event.get('source')))}: "
                f"{cash_event.get('name')} · {prefix}{eur(amount, theme.privacy)}"
            )
            event_text = QFontMetrics(painter.font()).elidedText(
                event_text, Qt.ElideRight, max(80, int(width - 26.0))
            )
            painter.drawText(
                QRectF(x + 13.0, y + 97.0 + index * 19.0, width - 26.0, 18.0),
                Qt.AlignVCenter,
                event_text,
            )
        if hidden_events:
            painter.setFont(QFont("Segoe UI", 8, QFont.Bold))
            painter.setPen(_chart_accent(theme))
            painter.drawText(
                QRectF(
                    x + 13.0,
                    y + 97.0 + len(visible_events) * 19.0,
                    width - 26.0,
                    18.0,
                ),
                Qt.AlignVCenter,
                f"+ {hidden_events} evento(s) adicional(is) neste dia",
            )

    def render_chart(self, painter: QPainter) -> None:
        theme = self.theme_provider()
        painter.fillRect(self.rect(), _chart_bg(theme))
        self._event_hits.clear()
        if len(self.points) < 2:
            self.draw_empty(painter, "Sem dados suficientes para prever a tesouraria")
            return

        plot = self._plot_rect()
        self._draw_legend(painter, plot, theme)
        low, high = self._value_range()

        def y_for(value: float) -> float:
            return plot.bottom() - plot.height() * (value - low) / max(1e-9, high - low)

        def x_for(index: int) -> float:
            return plot.left() + plot.width() * index / max(1, len(self.points) - 1)

        painter.setFont(QFont("Segoe UI", 8))
        for tick_index in range(5):
            value = high - (high - low) * tick_index / 4.0
            y = y_for(value)
            painter.setPen(QPen(_chart_grid(theme), 1))
            painter.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
            painter.setPen(_chart_muted(theme))
            painter.drawText(
                QRectF(3.0, y - 9.0, 64.0, 18.0),
                Qt.AlignRight | Qt.AlignVCenter,
                self._axis_value(value, theme.privacy),
            )

        zero_y = y_for(0.0)
        if plot.top() <= zero_y <= plot.bottom():
            negative_area = QRectF(
                plot.left(), zero_y, plot.width(), plot.bottom() - zero_y
            )
            negative_fill = _chart_negative(theme)
            negative_fill.setAlpha(18 if theme.dark else 12)
            painter.fillRect(negative_area, negative_fill)
            painter.setPen(QPen(_chart_negative(theme), 1.3, Qt.DashLine))
            painter.drawLine(
                QPointF(plot.left(), zero_y), QPointF(plot.right(), zero_y)
            )

        label_every = (
            7 if self.horizon_days <= 30 else 15 if self.horizon_days <= 60 else 30
        )
        for index, point in enumerate(self.points):
            if index % label_every != 0 and index != len(self.points) - 1:
                continue
            x = x_for(index)
            painter.setPen(QPen(_chart_grid(theme), 1))
            painter.drawLine(QPointF(x, plot.top()), QPointF(x, plot.bottom()))
            try:
                label = parse_date(str(point.get("date"))[:10]).strftime("%d/%m")
            except Exception:
                label = str(point.get("date", ""))[5:10]
            painter.setPen(_chart_muted(theme))
            painter.drawText(
                QRectF(x - 28.0, plot.bottom() + 10.0, 56.0, 18.0),
                Qt.AlignHCenter | Qt.AlignTop,
                label,
            )

        progress = max(0.0, min(1.0, self.anim_progress))
        positions = [
            QPointF(x_for(index), y_for(safe_float(point.get("balance"))))
            for index, point in enumerate(self.points)
        ]
        optimistic_positions = [
            QPointF(
                x_for(index),
                y_for(
                    safe_float(point.get("balance_optimistic", point.get("balance")))
                ),
            )
            for index, point in enumerate(self.points)
        ]
        pessimistic_positions = [
            QPointF(
                x_for(index),
                y_for(
                    safe_float(point.get("balance_pessimistic", point.get("balance")))
                ),
            )
            for index, point in enumerate(self.points)
        ]
        line_path = QPainterPath(positions[0])
        for previous, current in zip(positions, positions[1:]):
            middle_x = (previous.x() + current.x()) / 2.0
            line_path.cubicTo(
                QPointF(middle_x, previous.y()),
                QPointF(middle_x, current.y()),
                current,
            )

        area = QPainterPath(line_path)
        area.lineTo(positions[-1].x(), plot.bottom())
        area.lineTo(positions[0].x(), plot.bottom())
        area.closeSubpath()
        fill = QLinearGradient(0.0, plot.top(), 0.0, plot.bottom())
        fill_top = _chart_accent(theme)
        fill_top.setAlpha(72 if theme.dark else 48)
        fill_bottom = _chart_accent(theme)
        fill_bottom.setAlpha(3)
        fill.setColorAt(0.0, fill_top)
        fill.setColorAt(1.0, fill_bottom)
        reveal_right = plot.left() + plot.width() * progress
        painter.save()
        painter.setClipRect(
            QRectF(
                plot.left() - 10.0,
                plot.top() - 26.0,
                max(1.0, reveal_right - plot.left() + 20.0),
                plot.height() + 54.0,
            )
        )
        scenario_fill = _chart_accent(theme)
        scenario_fill.setAlpha(34 if theme.dark else 22)
        painter.setPen(Qt.NoPen)
        painter.setBrush(scenario_fill)
        painter.drawPolygon(
            QPolygonF(optimistic_positions + list(reversed(pessimistic_positions)))
        )
        for scenario_positions, scenario_color in (
            (optimistic_positions, _chart_positive(theme)),
            (pessimistic_positions, _chart_negative(theme)),
        ):
            scenario_path = QPainterPath(scenario_positions[0])
            for scenario_point in scenario_positions[1:]:
                scenario_path.lineTo(scenario_point)
            painter.setPen(QPen(scenario_color, 1.4, Qt.DashLine))
            painter.setBrush(Qt.NoBrush)
            painter.drawPath(scenario_path)
        painter.fillPath(area, fill)

        painter.setPen(
            QPen(_chart_accent(theme), 2.8, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
        )
        painter.setBrush(Qt.NoBrush)
        painter.drawPath(line_path)

        minimum_index = min(
            range(len(self.points)),
            key=lambda index: safe_float(self.points[index].get("balance")),
        )
        minimum_point = positions[minimum_index]
        painter.setPen(QPen(_chart_text(theme), 1.2))
        painter.setBrush(_chart_bg(theme))
        painter.drawEllipse(minimum_point, 4.5, 4.5)
        painter.setPen(_chart_muted(theme))
        painter.setFont(QFont("Segoe UI", 8, QFont.DemiBold))
        painter.drawText(
            QRectF(minimum_point.x() - 54.0, minimum_point.y() + 7.0, 108.0, 18.0),
            Qt.AlignHCenter | Qt.AlignTop,
            "Mínimo previsto",
        )

        index_by_date = {
            str(point.get("date")): index for index, point in enumerate(self.points)
        }
        offsets_by_date: dict[str, int] = {}
        counts_by_date: dict[str, int] = {}
        for event in self.events:
            key = str(event.get("date"))
            counts_by_date[key] = counts_by_date.get(key, 0) + 1
        for event in self.events:
            key = str(event.get("date"))
            point_index = index_by_date.get(key)
            if point_index is None:
                continue
            offset_index = offsets_by_date.get(key, 0)
            offsets_by_date[key] = offset_index + 1
            total = counts_by_date.get(key, 1)
            offset_x = (offset_index - (total - 1) / 2.0) * 8.0
            center = QPointF(
                positions[point_index].x() + offset_x,
                positions[point_index].y() - 12.0,
            )
            if center.x() > reveal_right + 9.0:
                continue
            color = self._source_color(str(event.get("source")), theme)
            self._draw_marker(
                painter,
                center,
                str(event.get("source")),
                color,
                5.0 + (1.0 if self.hover_event is event else 0.0),
            )
            self._event_hits.append(
                (event, QRectF(center.x() - 9.0, center.y() - 9.0, 18.0, 18.0))
            )
        painter.restore()

        painter.setPen(_chart_text(theme))
        painter.setFont(QFont("Segoe UI", 8, QFont.Bold))
        painter.drawText(
            QRectF(plot.left(), plot.top() - 23.0, 150.0, 19.0),
            Qt.AlignLeft | Qt.AlignVCenter,
            f"Hoje  {eur(self.points[0].get('balance', 0), theme.privacy)}",
        )
        painter.drawText(
            QRectF(plot.right() - 180.0, plot.top() - 23.0, 180.0, 19.0),
            Qt.AlignRight | Qt.AlignVCenter,
            f"+{self.horizon_days} dias  {eur(self.points[-1].get('balance', 0), theme.privacy)}",
        )
        self._draw_tooltip(painter, theme, positions)
