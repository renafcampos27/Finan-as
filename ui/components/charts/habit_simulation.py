from __future__ import annotations

from typing import Any, Callable, Optional

from PySide6.QtCore import QEvent, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen, QPolygonF
from PySide6.QtWidgets import QWidget

from ui.components.charts.base import StableChart
from ui.components.charts.utils import (
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


class HabitImpactChart(StableChart):
    """Monthly, time-proportional comparison of habit-cost scenarios."""

    horizon_selected = Signal(int)
    SERIES_KEYS = (
        "gross_spend",
        "cost_base",
        "cost_reduce_25",
        "cost_half",
        "cost_alternative",
        "cost_low",
        "cost_high",
        "savings_alternative",
    )

    def __init__(
        self,
        theme_provider: Callable[[], Theme],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(theme_provider, parent)
        self.rows: list[dict[str, Any]] = []
        self.series: list[dict[str, Any]] = []
        self.previous_series: list[dict[str, Any]] = []
        self.habit_name = ""
        self.alternative_name = "Alternativa"
        self.gambling = False
        self.active_scenario = "current"
        self.hover_index: Optional[int] = None
        self._point_rects: list[QRectF] = []
        self.setMinimumHeight(500)
        self.setToolTip("")
        self.setAccessibleName(
            "Comparação mensal do custo atual, redução e alternativa realista"
        )

    def set_active_scenario(self, scenario: str = "current") -> None:
        """Emphasise one already-rendered scenario without changing its data."""
        valid = {"current", "reduce_25", "half", "alternative"}
        selected = str(scenario or "current")
        self.active_scenario = selected if selected in valid else "current"
        self.invalidate_cache()

    def event(self, event) -> bool:  # type: ignore[override]
        if event.type() == QEvent.ToolTip:
            event.accept()
            return True
        return super().event(event)

    @staticmethod
    def _clean_row(source: dict[str, Any]) -> dict[str, Any]:
        months = max(1, int(source.get("months", 1) or 1))
        row: dict[str, Any] = {
            "months": months,
            "label": str(source.get("label") or f"Mês {months}"),
        }
        for key in HabitImpactChart.SERIES_KEYS:
            row[key] = max(0.0, safe_float(source.get(key)))
        return row

    def _display_series(self) -> list[dict[str, Any]]:
        if (
            not self.previous_series
            or len(self.previous_series) != len(self.series)
            or self.anim_progress >= 0.999
        ):
            return [dict(row) for row in self.series]
        progress = max(0.0, min(1.0, self.anim_progress))
        displayed: list[dict[str, Any]] = []
        for previous, target in zip(self.previous_series, self.series):
            row: dict[str, Any] = {
                "months": int(target["months"]),
                "label": str(target["label"]),
            }
            for key in self.SERIES_KEYS:
                start = safe_float(previous.get(key))
                row[key] = start + (safe_float(target.get(key)) - start) * progress
            displayed.append(row)
        return displayed

    def set_data(self, data: dict[str, Any]) -> None:
        current = self._display_series()
        source_series = list(data.get("monthly_series") or data.get("rows") or [])
        clean_series = [self._clean_row(dict(source)) for source in source_series]
        clean_series.sort(key=lambda row: int(row["months"]))
        self.rows = [
            self._clean_row(dict(source)) for source in (data.get("rows") or [])
        ]
        self.rows.sort(key=lambda row: int(row["months"]))

        can_transition = bool(current) and len(current) == len(clean_series)
        self.previous_series = (
            [dict(row) for row in current]
            if can_transition
            else [dict(row) for row in clean_series]
        )
        self.series = clean_series
        preset = data.get("preset", {}) or {}
        self.habit_name = str(preset.get("name") or "Hábito")
        self.alternative_name = str(preset.get("alternative_name") or "Alternativa")
        inputs = data.get("inputs", {}) or {}
        self.gambling = bool(inputs.get("gambling"))
        self.hover_index = None
        self._point_rects.clear()

        theme = self.theme_provider()
        if clean_series:
            last = clean_series[-1]
            self.setAccessibleDescription(
                f"{self.habit_name}: em 30 anos, cenário atual "
                f"{eur(last['cost_base'], theme.privacy)}, alternativa "
                f"{eur(last['cost_alternative'], theme.privacy)} e poupança real "
                f"{eur(last['savings_alternative'], theme.privacy)}."
            )
        else:
            self.setAccessibleDescription("Sem dados para a simulação")

        if can_transition and self.isVisible():
            self.start_entrance_animation(260)
        else:
            self._anim_progress = 1.0
            self.invalidate_cache()

    def _plot_rect(self) -> QRectF:
        return QRectF(
            78.0,
            88.0,
            max(160.0, self.width() - 101.0),
            max(190.0, self.height() - 145.0),
        )

    @staticmethod
    def _axis_value(value: float, privacy: bool) -> str:
        return format_axis_money(value, privacy=privacy)

    @staticmethod
    def _time_label(months: int) -> str:
        count = max(1, int(months))
        if count == 1:
            return "1 mês"
        if count < 12:
            return f"{count} meses"
        years, remaining = divmod(count, 12)
        year_text = f"{years} ano" if years == 1 else f"{years} anos"
        return year_text if remaining == 0 else f"{year_text} e {remaining} meses"

    @staticmethod
    def _line_path(points: list[QPointF]) -> QPainterPath:
        path = QPainterPath()
        if not points:
            return path
        path.moveTo(points[0])
        for point in points[1:]:
            path.lineTo(point)
        return path

    def _points(
        self,
        rows: list[dict[str, Any]],
        key: str,
        high: float,
    ) -> list[QPointF]:
        plot = self._plot_rect()
        maximum_month = max(1, max(int(row["months"]) for row in rows))
        denominator = max(1, maximum_month - 1)
        return [
            QPointF(
                plot.left()
                + plot.width() * (max(1, int(row["months"])) - 1) / denominator,
                plot.bottom()
                - plot.height() * safe_float(row.get(key)) / max(1.0, high),
            )
            for row in rows
        ]

    def _index_at_x(self, x: float) -> Optional[int]:
        if not self.series:
            return None
        plot = self._plot_rect()
        ratio = max(0.0, min(1.0, (x - plot.left()) / max(1.0, plot.width())))
        target_month = 1 + ratio * max(0, int(self.series[-1]["months"]) - 1)
        return min(
            range(len(self.series)),
            key=lambda index: abs(int(self.series[index]["months"]) - target_month),
        )

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

    def mousePressEvent(self, event):  # type: ignore[override]
        if event.button() == Qt.LeftButton:
            index = self._index_at_x(float(event.position().x()))
            if index is not None and 0 <= index < len(self.series):
                self.hover_index = index
                detail = dict(self.series[index])
                month = int(detail["months"])
                if self.rows:
                    month = min(
                        (int(row["months"]) for row in self.rows),
                        key=lambda value: abs(value - month),
                    )
                if self.open_detail(
                    detail,
                    label=str(detail.get("label") or f"{month} meses"),
                    action_label=self._detail_action_label,
                ):
                    self.horizon_selected.emit(month)
                self.invalidate_cache()
                event.accept()
                return
        super().mousePressEvent(event)

    def _tooltip_rect(self) -> QRectF:
        if self.hover_index is None or not self.series:
            return QRectF()
        rows = self._display_series()
        index = max(0, min(self.hover_index, len(rows) - 1))
        high = self._high_value(rows)
        anchor = self._points(rows, "cost_base", high)[index]
        width = min(370.0, max(300.0, self.width() - 18.0))
        height = 218.0 if self.gambling else 193.0
        x = max(8.0, min(anchor.x() + 14.0, self.width() - width - 8.0))
        y = anchor.y() - height - 13.0
        if y < 8.0:
            y = anchor.y() + 15.0
        y = max(8.0, min(y, self.height() - height - 8.0))
        return QRectF(x, y, width, height)

    def _high_value(self, rows: list[dict[str, Any]]) -> float:
        keys = [
            "cost_high",
            "cost_base",
            "cost_reduce_25",
            "cost_half",
            "cost_alternative",
        ]
        if self.gambling:
            keys.append("gross_spend")
        return max(
            1.0,
            max(safe_float(row.get(key)) for row in rows for key in keys) * 1.14,
        )

    def _draw_tooltip(self, painter: QPainter, rows: list[dict[str, Any]]) -> None:
        if self.hover_index is None or not (0 <= self.hover_index < len(rows)):
            return
        row = rows[self.hover_index]
        theme = self.theme_provider()
        rect = self._tooltip_rect()
        painter.setPen(QPen(_chart_grid(theme), 1))
        painter.setBrush(_chart_tooltip_bg(theme))
        painter.drawRoundedRect(rect, 13.0, 13.0)

        painter.setFont(QFont("Segoe UI", 9, QFont.Bold))
        painter.setPen(_chart_text(theme))
        painter.drawText(
            QRectF(rect.left() + 13.0, rect.top() + 7.0, rect.width() - 26.0, 22.0),
            Qt.AlignVCenter,
            f"{self.habit_name} · {self._time_label(int(row['months']))}",
        )
        lines = [
            ("Atual", eur(row["cost_base"], theme.privacy)),
            ("−25% ocorrências", eur(row["cost_reduce_25"], theme.privacy)),
            ("Metade", eur(row["cost_half"], theme.privacy)),
            (self.alternative_name, eur(row["cost_alternative"], theme.privacy)),
            ("Poupança real", eur(row["savings_alternative"], theme.privacy)),
        ]
        if self.gambling:
            lines.append(("Valor apostado", eur(row["gross_spend"], theme.privacy)))
        painter.setFont(QFont("Segoe UI", 8))
        line_height = 27.0
        for index, (label, value) in enumerate(lines):
            y = rect.top() + 35.0 + index * line_height
            painter.setPen(_chart_muted(theme))
            painter.drawText(
                QRectF(rect.left() + 13.0, y, 155.0, 21.0),
                Qt.AlignLeft | Qt.AlignVCenter,
                label,
            )
            painter.setPen(_chart_text(theme))
            painter.drawText(
                QRectF(rect.left() + 172.0, y, rect.width() - 185.0, 21.0),
                Qt.AlignRight | Qt.AlignVCenter,
                value,
            )

    @staticmethod
    def _scenario_colors(theme: Theme) -> tuple[QColor, QColor]:
        if theme.dark:
            return QColor("#FF9F0A"), QColor("#64A8FF")
        return QColor("#C66A00"), QColor("#1769C2")

    @staticmethod
    def _draw_legend_item(
        painter: QPainter,
        x: float,
        y: float,
        label: str,
        pen: QPen,
        text_color: QColor,
    ) -> None:
        painter.setPen(pen)
        painter.drawLine(QPointF(x, y + 10.0), QPointF(x + 18.0, y + 10.0))
        painter.setPen(text_color)
        painter.drawText(
            QRectF(x + 24.0, y, 125.0, 20.0),
            Qt.AlignVCenter,
            label,
        )

    def render_chart(self, painter: QPainter) -> None:
        theme = self.theme_provider()
        painter.fillRect(self.rect(), _chart_bg(theme))
        rows = self._display_series()
        if not rows or max(safe_float(row.get("cost_high")) for row in rows) <= 0:
            self._point_rects.clear()
            self.draw_empty(
                painter, "Ajusta o valor ou a frequência para criar a previsão"
            )
            return

        plot = self._plot_rect()
        high = self._high_value(rows)
        current_color = _chart_negative(theme)
        reduce_color, half_color = self._scenario_colors(theme)
        alternative_color = _chart_positive(theme)
        gross_color = _chart_muted(theme)

        painter.setFont(QFont("Segoe UI", 8))
        for step in range(5):
            ratio = step / 4.0
            y = plot.top() + plot.height() * ratio
            painter.setPen(QPen(_chart_grid(theme), 1))
            painter.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
            painter.setPen(_chart_muted(theme))
            painter.drawText(
                QRectF(2.0, y - 9.0, 68.0, 18.0),
                Qt.AlignRight | Qt.AlignVCenter,
                self._axis_value(high * (1.0 - ratio), theme.privacy),
            )

        low_points = self._points(rows, "cost_low", high)
        high_points = self._points(rows, "cost_high", high)
        current_points = self._points(rows, "cost_base", high)
        reduce_points = self._points(rows, "cost_reduce_25", high)
        half_points = self._points(rows, "cost_half", high)
        alternative_points = self._points(rows, "cost_alternative", high)
        gross_points = self._points(rows, "gross_spend", high)

        band = QColor(current_color)
        band.setAlpha(31 if theme.dark else 22)
        painter.setPen(Qt.NoPen)
        painter.setBrush(band)
        painter.drawPolygon(QPolygonF(high_points + list(reversed(low_points))))

        selected_width = 4.4
        quiet_width = 1.8
        current_pen = QPen(
            current_color,
            selected_width if self.active_scenario == "current" else quiet_width,
        )
        reduce_pen = QPen(
            reduce_color,
            selected_width if self.active_scenario == "reduce_25" else quiet_width,
        )
        half_pen = QPen(
            half_color,
            selected_width if self.active_scenario == "half" else quiet_width,
        )
        alternative_pen = QPen(
            alternative_color,
            selected_width if self.active_scenario == "alternative" else quiet_width,
        )
        scenario_paths = (
            ("current", current_points, current_pen),
            ("reduce_25", reduce_points, reduce_pen),
            ("half", half_points, half_pen),
            ("alternative", alternative_points, alternative_pen),
        )
        for scenario, points, pen in scenario_paths:
            if scenario != self.active_scenario:
                quiet = QColor(pen.color())
                quiet.setAlpha(145 if theme.dark else 125)
                pen.setColor(quiet)
            pen.setCapStyle(Qt.RoundCap)
            pen.setJoinStyle(Qt.RoundJoin)
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)
            painter.drawPath(self._line_path(points))

        gross_pen = QPen(gross_color, 1.8, Qt.DotLine)
        if self.gambling:
            painter.setPen(gross_pen)
            painter.setBrush(Qt.NoBrush)
            painter.drawPath(self._line_path(gross_points))

        month_to_index = {int(row["months"]): index for index, row in enumerate(rows)}
        self._point_rects = []
        for milestone in self.rows:
            index = month_to_index.get(int(milestone["months"]))
            if index is None:
                continue
            current_point = current_points[index]
            alternative_point = alternative_points[index]
            painter.setPen(QPen(_chart_bg(theme), 2))
            painter.setBrush(current_color)
            painter.drawEllipse(current_point, 4.5, 4.5)
            painter.setBrush(alternative_color)
            painter.drawEllipse(alternative_point, 4.0, 4.0)
            self._point_rects.append(
                QRectF(current_point.x() - 10.0, plot.top(), 20.0, plot.height())
            )

        painter.setFont(QFont("Segoe UI", 8, QFont.DemiBold))
        maximum_month = max(1, int(rows[-1]["months"]))
        for months, label in (
            (1, "1m"),
            (60, "5a"),
            (120, "10a"),
            (240, "20a"),
            (360, "30a"),
        ):
            if months > maximum_month:
                continue
            x = plot.left() + plot.width() * (months - 1) / max(1, maximum_month - 1)
            painter.setPen(_chart_grid(theme))
            painter.drawLine(QPointF(x, plot.bottom()), QPointF(x, plot.bottom() + 5.0))
            painter.setPen(_chart_muted(theme))
            painter.drawText(
                QRectF(x - 28.0, plot.bottom() + 8.0, 56.0, 20.0),
                Qt.AlignHCenter | Qt.AlignTop,
                label,
            )

        painter.setFont(QFont("Segoe UI", 8, QFont.DemiBold))
        legend_x = plot.left()
        self._draw_legend_item(
            painter, legend_x, 8.0, "Atual", current_pen, _chart_text(theme)
        )
        self._draw_legend_item(
            painter, legend_x + 145.0, 8.0, "−25%", reduce_pen, _chart_text(theme)
        )
        self._draw_legend_item(
            painter, legend_x + 290.0, 8.0, "Metade", half_pen, _chart_text(theme)
        )
        self._draw_legend_item(
            painter,
            legend_x,
            36.0,
            "Alternativa",
            alternative_pen,
            _chart_text(theme),
        )
        band_pen = QPen(current_color, 7.0)
        band_pen.setColor(band)
        self._draw_legend_item(
            painter,
            legend_x + 145.0,
            36.0,
            "Faixa provável",
            band_pen,
            _chart_text(theme),
        )
        if self.gambling:
            self._draw_legend_item(
                painter,
                legend_x + 290.0,
                36.0,
                "Apostado",
                gross_pen,
                _chart_text(theme),
            )

        if self.hover_index is not None and 0 <= self.hover_index < len(rows):
            point = current_points[self.hover_index]
            painter.setPen(QPen(_chart_grid(theme), 1, Qt.DashLine))
            painter.drawLine(
                QPointF(point.x(), plot.top()), QPointF(point.x(), plot.bottom())
            )
            self._draw_tooltip(painter, rows)


__all__ = ["HabitImpactChart"]
