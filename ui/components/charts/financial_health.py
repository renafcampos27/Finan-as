from __future__ import annotations

from typing import Any, Callable, Optional

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget

from ui.components.charts.base import StableChart
from ui.components.charts.utils import (
    _chart_accent,
    _chart_bg,
    _chart_grid,
    _chart_muted,
    _chart_negative,
    _chart_positive,
    _chart_secondary,
    _chart_text,
    _chart_tooltip_bg,
)
from ui.core.theme import Theme
from ui.components.charts.presentation import format_axis_money
from utils.helpers import eur, safe_float


def _tone_color(theme: Theme, tone: str) -> QColor:
    if tone == "positive":
        return _chart_positive(theme)
    if tone == "negative":
        return _chart_negative(theme)
    if tone == "warning":
        secondary = QColor("#FF9F0A")
        return secondary.lighter(112) if theme.dark else secondary.darker(104)
    return _chart_accent(theme)


class ScoreTrendChart(StableChart):
    """Clickable twelve-month trend used by Score Financeiro 2.0."""

    month_clicked = Signal(str)

    def __init__(
        self, theme_provider: Callable[[], Theme], parent: Optional[QWidget] = None
    ) -> None:
        super().__init__(theme_provider, parent)
        self.rows: list[dict[str, Any]] = []
        self.hover_index: Optional[int] = None
        self.selected_month = ""
        self.setMinimumHeight(290)
        self.setAccessibleName("Evolução interativa do score financeiro")

    def set_data(self, rows: list[dict[str, Any]], selected_month: str = "") -> None:
        self.rows = [dict(row) for row in rows]
        self.selected_month = str(selected_month or "")
        self.hover_index = None
        if self.rows:
            first, last = self.rows[0], self.rows[-1]
            self.setAccessibleDescription(
                f"Score de {first.get('score', 0)} em {first.get('month', '')} "
                f"até {last.get('score', 0)} em {last.get('month', '')}. "
                "Seleciona um ponto para abrir esse mês."
            )
        self.invalidate_cache()

    def _plot(self) -> QRectF:
        return QRectF(48, 34, max(120, self.width() - 70), max(120, self.height() - 84))

    def _index_at(self, x: float) -> Optional[int]:
        if not self.rows or not self._plot().adjusted(-12, -8, 12, 8).contains(
            x, self._plot().center().y()
        ):
            return None
        ratio = (x - self._plot().left()) / max(1.0, self._plot().width())
        return max(0, min(len(self.rows) - 1, round(ratio * (len(self.rows) - 1))))

    def mouseMoveEvent(self, event):  # type: ignore[override]
        plot = self._plot()
        if plot.adjusted(-12, -10, 12, 10).contains(event.position()):
            ratio = (event.position().x() - plot.left()) / max(1.0, plot.width())
            self.hover_index = (
                max(0, min(len(self.rows) - 1, round(ratio * (len(self.rows) - 1))))
                if self.rows
                else None
            )
        else:
            self.hover_index = None
        self.setCursor(
            Qt.PointingHandCursor if self.hover_index is not None else Qt.ArrowCursor
        )
        self.invalidate_cache()

    def leaveEvent(self, event):  # type: ignore[override]
        self.hover_index = None
        self.setCursor(Qt.ArrowCursor)
        self.invalidate_cache()
        super().leaveEvent(event)

    def mousePressEvent(self, event):  # type: ignore[override]
        if event.button() == Qt.LeftButton:
            if self.hover_index is not None:
                row = dict(self.rows[self.hover_index])
                month = str(row.get("month") or "")
                if month:
                    self.selected_month = month
                    self.invalidate_cache()
                    if self.open_detail(
                        row,
                        label=month,
                        action_label=self._detail_action_label,
                    ):
                        self.month_clicked.emit(month)
            event.accept()
            return
        super().mousePressEvent(event)

    def render_chart(self, painter: QPainter) -> None:
        theme = self.theme_provider()
        painter.fillRect(self.rect(), _chart_bg(theme))
        if not self.rows:
            self.draw_empty(
                painter, "Sem histórico suficiente para calcular a tendência"
            )
            return
        plot = self._plot()

        def point(index: int) -> QPointF:
            x = plot.left() + index / max(1, len(self.rows) - 1) * plot.width()
            score = max(0.0, min(100.0, safe_float(self.rows[index].get("score"))))
            y = plot.bottom() - score / 100.0 * plot.height()
            return QPointF(x, y)

        painter.setFont(QFont("Segoe UI", 8))
        for value in (0, 25, 50, 75, 100):
            y = plot.bottom() - value / 100.0 * plot.height()
            painter.setPen(QPen(_chart_grid(theme), 1))
            painter.drawLine(int(plot.left()), int(y), int(plot.right()), int(y))
            painter.setPen(_chart_muted(theme))
            painter.drawText(
                QRectF(0, y - 10, 40, 20), Qt.AlignRight | Qt.AlignVCenter, str(value)
            )

        points = [point(index) for index in range(len(self.rows))]
        area = QPainterPath(points[0])
        for current in points[1:]:
            area.lineTo(current)
        area.lineTo(points[-1].x(), plot.bottom())
        area.lineTo(points[0].x(), plot.bottom())
        area.closeSubpath()
        fill = QColor(_chart_accent(theme))
        fill.setAlpha(35 if theme.dark else 24)
        painter.setPen(Qt.NoPen)
        painter.setBrush(fill)
        painter.drawPath(area)

        path = QPainterPath(points[0])
        for current in points[1:]:
            path.lineTo(current)
        painter.setPen(
            QPen(_chart_accent(theme), 2.6, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
        )
        painter.setBrush(Qt.NoBrush)
        painter.drawPath(path)

        for index, (row, current) in enumerate(zip(self.rows, points)):
            selected = str(row.get("month")) == self.selected_month
            hovered = index == self.hover_index
            color = _tone_color(theme, str(row.get("tone") or "neutral"))
            if selected or hovered:
                halo = QColor(color)
                halo.setAlpha(56)
                painter.setPen(Qt.NoPen)
                painter.setBrush(halo)
                painter.drawEllipse(current, 8.0, 8.0)
            painter.setPen(QPen(_chart_bg(theme), 2))
            painter.setBrush(color)
            painter.drawEllipse(
                current, 5.2 if selected else 4.2, 5.2 if selected else 4.2
            )

            if (
                index in {0, len(self.rows) - 1}
                or len(self.rows) <= 8
                or index % 2 == 0
            ):
                label = str(row.get("month") or "")
                if len(label) >= 7:
                    label = label[5:7] + "/" + label[2:4]
                painter.setPen(_chart_muted(theme))
                painter.drawText(
                    QRectF(current.x() - 28, plot.bottom() + 9, 56, 20),
                    Qt.AlignHCenter | Qt.AlignTop,
                    label,
                )

        if self.hover_index is not None:
            row = self.rows[self.hover_index]
            anchor = points[self.hover_index]
            box = QRectF(
                min(max(8.0, anchor.x() - 72.0), self.width() - 160.0),
                max(6.0, anchor.y() - 72.0),
                152,
                58,
            )
            painter.setPen(QPen(_chart_grid(theme), 1))
            painter.setBrush(_chart_tooltip_bg(theme))
            painter.drawRoundedRect(box, 10, 10)
            painter.setPen(_chart_text(theme))
            painter.setFont(QFont("Segoe UI", 8, QFont.Bold))
            painter.drawText(
                box.adjusted(10, 7, -10, -7),
                Qt.AlignLeft | Qt.AlignVCenter,
                f"{row.get('month', '')}  ·  {row.get('score', 0)}/100\n"
                f"Nota {row.get('grade', '—')}  ·  {row.get('status', '')}",
            )


class ScoreBreakdownChart(StableChart):
    """Interactive, status-labelled breakdown for score dimensions."""

    dimension_clicked = Signal(str)

    def __init__(
        self, theme_provider: Callable[[], Theme], parent: Optional[QWidget] = None
    ) -> None:
        super().__init__(theme_provider, parent)
        self.rows: list[dict[str, Any]] = []
        self.hover_index: Optional[int] = None
        self.selected_key = ""
        self._row_rects: list[QRectF] = []
        self.setMinimumHeight(330)
        self.setAccessibleName("Componentes interativos do score")

    def set_data(self, rows: list[dict[str, Any]], selected_key: str = "") -> None:
        self.rows = [dict(row) for row in rows]
        self.selected_key = str(selected_key or "")
        self.hover_index = None
        self.setMinimumHeight(max(330, 84 + len(self.rows) * 48))
        self.setAccessibleDescription(
            "; ".join(
                f"{row.get('name')}: {safe_float(row.get('score')):.0f} em 100, {row.get('status')}"
                for row in self.rows
            )
        )
        self.invalidate_cache()

    def mouseMoveEvent(self, event):  # type: ignore[override]
        self.hover_index = next(
            (
                index
                for index, rect in enumerate(self._row_rects)
                if rect.contains(event.position())
            ),
            None,
        )
        self.setCursor(
            Qt.PointingHandCursor if self.hover_index is not None else Qt.ArrowCursor
        )
        self.invalidate_cache()

    def leaveEvent(self, event):  # type: ignore[override]
        self.hover_index = None
        self.setCursor(Qt.ArrowCursor)
        self.invalidate_cache()
        super().leaveEvent(event)

    def mousePressEvent(self, event):  # type: ignore[override]
        if event.button() == Qt.LeftButton:
            if self.hover_index is not None:
                row = dict(self.rows[self.hover_index])
                key = str(row.get("key") or "")
                if key:
                    self.selected_key = key
                    self.dimension_clicked.emit(key)
                    self.invalidate_cache()
                    self.open_detail(
                        row,
                        label=str(row.get("label") or key),
                        action_label=self._detail_action_label,
                    )
            event.accept()
            return
        super().mousePressEvent(event)

    def render_chart(self, painter: QPainter) -> None:
        theme = self.theme_provider()
        painter.fillRect(self.rect(), _chart_bg(theme))
        self._row_rects = []
        if not self.rows:
            self.draw_empty(painter, "Sem componentes para apresentar")
            return
        label_width = min(188.0, max(112.0, self.width() * 0.35))
        status_width = min(98.0, max(70.0, self.width() * 0.19))
        plot_left = label_width + 14.0
        plot_width = max(44.0, self.width() - plot_left - status_width - 14.0)
        row_height = max(42.0, min(52.0, (self.height() - 46.0) / len(self.rows)))
        top = 18.0
        painter.setFont(QFont("Segoe UI", 8, QFont.Bold))
        for index, row in enumerate(self.rows):
            y = top + index * row_height
            row_rect = QRectF(4, y - 4, self.width() - 8, row_height - 2)
            self._row_rects.append(row_rect)
            selected = str(row.get("key")) == self.selected_key
            hovered = index == self.hover_index
            if selected or hovered:
                highlight = QColor(_chart_accent(theme))
                highlight.setAlpha(30 if selected else 18)
                painter.setPen(Qt.NoPen)
                painter.setBrush(highlight)
                painter.drawRoundedRect(row_rect, 9, 9)

            painter.setPen(_chart_text(theme))
            name = f"{row.get('icon', '')} {row.get('name', '')}".strip()
            painter.drawText(
                QRectF(7, y, label_width - 5, row_height - 10),
                Qt.AlignLeft | Qt.AlignVCenter,
                name,
            )
            bar = QRectF(plot_left, y + row_height / 2 - 7, plot_width, 14)
            painter.setPen(Qt.NoPen)
            painter.setBrush(_chart_grid(theme))
            painter.drawRoundedRect(bar, 7, 7)
            value = _clamped_score(row.get("score"))
            color = _tone_color(theme, str(row.get("tone") or "neutral"))
            painter.setBrush(color)
            painter.drawRoundedRect(
                QRectF(
                    bar.left(),
                    bar.top(),
                    max(2.0, bar.width() * value / 100.0),
                    bar.height(),
                ),
                7,
                7,
            )
            painter.setPen(_chart_text(theme))
            painter.drawText(
                QRectF(bar.right() + 8, y, status_width - 10, row_height - 10),
                Qt.AlignLeft | Qt.AlignVCenter,
                f"{value:.0f} · {row.get('marker', '')}",
            )
        painter.setPen(_chart_muted(theme))
        painter.setFont(QFont("Segoe UI", 8))
        painter.drawText(
            QRectF(plot_left, self.height() - 26, plot_width + status_width, 18),
            Qt.AlignRight | Qt.AlignVCenter,
            "Seleciona uma dimensão para ver causa e ação →",
        )


def _clamped_score(value: Any) -> float:
    return max(0.0, min(100.0, safe_float(value)))


class ResilienceScenarioChart(StableChart):
    """Interactive liquidity fan chart with base, optimistic and stress paths."""

    point_clicked = Signal(dict)

    def __init__(
        self, theme_provider: Callable[[], Theme], parent: Optional[QWidget] = None
    ) -> None:
        super().__init__(theme_provider, parent)
        self.set_detail_field_types(
            {
                "month": "count",
                "base": "money",
                "optimistic": "money",
                "stressed": "money",
                "p10": "money",
                "p50": "money",
                "p90": "money",
            }
        )
        self.rows: list[dict[str, Any]] = []
        self.hover_index: Optional[int] = None
        self.setMinimumHeight(410)
        self.setAccessibleName("Cenários interativos de liquidez")

    def set_data(self, rows: list[dict[str, Any]]) -> None:
        self.rows = [dict(row) for row in rows]
        self.hover_index = None
        theme = self.theme_provider()
        if self.rows:
            last = self.rows[-1]
            self.setAccessibleDescription(
                f"No fim do horizonte: cenário base {eur(last.get('base', 0), theme.privacy)}, "
                f"otimista {eur(last.get('optimistic', 0), theme.privacy)} e de stress "
                f"{eur(last.get('stressed', 0), theme.privacy)}."
            )
        self.invalidate_cache()

    def _plot(self) -> QRectF:
        return QRectF(
            78, 48, max(120, self.width() - 102), max(130, self.height() - 102)
        )

    def mouseMoveEvent(self, event):  # type: ignore[override]
        plot = self._plot()
        if self.rows and plot.adjusted(-10, -10, 10, 10).contains(event.position()):
            ratio = (event.position().x() - plot.left()) / max(1.0, plot.width())
            self.hover_index = max(
                0, min(len(self.rows) - 1, round(ratio * (len(self.rows) - 1)))
            )
        else:
            self.hover_index = None
        self.setCursor(
            Qt.PointingHandCursor if self.hover_index is not None else Qt.ArrowCursor
        )
        self.invalidate_cache()

    def leaveEvent(self, event):  # type: ignore[override]
        self.hover_index = None
        self.setCursor(Qt.ArrowCursor)
        self.invalidate_cache()
        super().leaveEvent(event)

    def mousePressEvent(self, event):  # type: ignore[override]
        if event.button() == Qt.LeftButton and self.hover_index is not None:
            row = dict(self.rows[self.hover_index])
            if self.open_detail(
                row,
                action_label=self._detail_action_label,
            ):
                self.point_clicked.emit(row)
            event.accept()
            return
        super().mousePressEvent(event)

    def render_chart(self, painter: QPainter) -> None:
        theme = self.theme_provider()
        painter.fillRect(self.rect(), _chart_bg(theme))
        if not self.rows:
            self.draw_empty(painter, "Sem dados suficientes para testar os cenários")
            return
        plot = self._plot()
        keys = ("p10", "p90", "base", "optimistic", "stressed")
        values = [safe_float(row.get(key)) for row in self.rows for key in keys]
        low = min(0.0, min(values))
        high = max(1.0, max(values))
        span = max(1.0, high - low)
        low -= span * 0.08
        high += span * 0.10

        def point(index: int, key: str) -> QPointF:
            x = plot.left() + index / max(1, len(self.rows) - 1) * plot.width()
            value = safe_float(self.rows[index].get(key))
            y = plot.bottom() - (value - low) / max(1.0, high - low) * plot.height()
            return QPointF(x, y)

        painter.setFont(QFont("Segoe UI", 8))
        for index in range(5):
            value = low + (high - low) * index / 4
            y = plot.bottom() - index / 4 * plot.height()
            painter.setPen(QPen(_chart_grid(theme), 1))
            painter.drawLine(int(plot.left()), int(y), int(plot.right()), int(y))
            painter.setPen(_chart_muted(theme))
            label = format_axis_money(value, theme.privacy)
            painter.drawText(
                QRectF(0, y - 10, 70, 20), Qt.AlignRight | Qt.AlignVCenter, label
            )
        if low < 0 < high:
            zero_y = plot.bottom() - (0.0 - low) / (high - low) * plot.height()
            painter.setPen(QPen(_chart_negative(theme), 1.2, Qt.DashLine))
            painter.drawLine(
                int(plot.left()), int(zero_y), int(plot.right()), int(zero_y)
            )
            painter.drawText(
                QRectF(plot.left() + 5, zero_y - 20, 130, 18),
                Qt.AlignLeft,
                "Saldo zero",
            )

        upper = [point(index, "p90") for index in range(len(self.rows))]
        lower = [point(index, "p10") for index in range(len(self.rows))]
        band = QPainterPath(upper[0])
        for current in upper[1:]:
            band.lineTo(current)
        for current in reversed(lower):
            band.lineTo(current)
        band.closeSubpath()
        band_color = QColor(_chart_secondary(theme))
        band_color.setAlpha(45 if theme.dark else 28)
        painter.setPen(Qt.NoPen)
        painter.setBrush(band_color)
        painter.drawPath(band)

        # BUG-CHART-HEALTH-01: with a single data point the band degenerates
        # into an empty path and the series loops never run; draw an explicit
        # centred marker so the chart is not blank.
        base_marker: Optional[QPointF] = None
        if len(self.rows) == 1:
            marker_x = plot.left() + plot.width() / 2.0
            band_top = point(0, "p90")
            band_bottom = point(0, "p10")
            painter.setPen(Qt.NoPen)
            band_dot = QColor(_chart_secondary(theme))
            band_dot.setAlpha(120)
            painter.setBrush(band_dot)
            painter.drawEllipse(QPointF(marker_x, band_top.y()), 5.0, 5.0)
            painter.drawEllipse(QPointF(marker_x, band_bottom.y()), 5.0, 5.0)
            base_marker = QPointF(marker_x, point(0, "base").y())

        series = (
            ("optimistic", _chart_positive(theme), Qt.DotLine, 1.8),
            ("base", _chart_accent(theme), Qt.SolidLine, 2.8),
            ("stressed", _chart_negative(theme), Qt.DashLine, 2.3),
        )
        for key, color, style, width in series:
            path = QPainterPath(point(0, key))
            for index in range(1, len(self.rows)):
                path.lineTo(point(index, key))
            painter.setPen(QPen(color, width, style, Qt.RoundCap, Qt.RoundJoin))
            painter.setBrush(Qt.NoBrush)
            painter.drawPath(path)

        # BUG-CHART-HEALTH-01: single-point base marker on top of the paths.
        if base_marker is not None:
            painter.setPen(QPen(_chart_bg(theme), 2))
            painter.setBrush(_chart_accent(theme))
            painter.drawEllipse(base_marker, 4.5, 4.5)

        painter.setFont(QFont("Segoe UI", 8, QFont.Bold))
        painter.setPen(_chart_text(theme))
        painter.drawText(
            QRectF(plot.left(), 10, plot.width(), 25),
            Qt.AlignLeft | Qt.AlignVCenter,
            "● Base    ⋯ Otimista    – – Stress    ▧ P10–P90",
        )
        for index, row in enumerate(self.rows):
            if index == 0 or index == len(self.rows) - 1 or index % 3 == 0:
                anchor = point(index, "base")
                painter.setPen(_chart_muted(theme))
                painter.drawText(
                    QRectF(anchor.x() - 24, plot.bottom() + 9, 48, 20),
                    Qt.AlignHCenter | Qt.AlignTop,
                    str(row.get("label") or f"M{index}"),
                )

        if self.hover_index is not None:
            row = self.rows[self.hover_index]
            anchor = point(self.hover_index, "base")
            painter.setPen(QPen(_chart_text(theme), 1, Qt.DotLine))
            painter.drawLine(
                int(anchor.x()), int(plot.top()), int(anchor.x()), int(plot.bottom())
            )
            has_probability = row.get("probability_positive") is not None
            box_height = 122 if has_probability else 104
            box = QRectF(
                min(max(8.0, anchor.x() + 12), self.width() - 246.0),
                max(
                    42.0,
                    min(
                        anchor.y() - box_height + 12,
                        self.height() - box_height - 22.0,
                    ),
                ),
                232,
                box_height,
            )
            painter.setPen(QPen(_chart_grid(theme), 1))
            painter.setBrush(_chart_tooltip_bg(theme))
            painter.drawRoundedRect(box, 11, 11)
            painter.setPen(_chart_text(theme))
            painter.drawText(
                box.adjusted(11, 8, -11, -8),
                Qt.AlignLeft | Qt.AlignTop,
                f"{row.get('label', '')}\n"
                f"Base  {eur(row.get('base', 0), theme.privacy)}\n"
                f"Otimista  {eur(row.get('optimistic', 0), theme.privacy)}\n"
                f"Stress  {eur(row.get('stressed', 0), theme.privacy)}\n"
                f"Intervalo  {eur(row.get('p10', 0), theme.privacy)} – {eur(row.get('p90', 0), theme.privacy)}"
                + (
                    f"\n{str(row.get('probability_label') or 'Este mês termina acima de 0 €')}  "
                    f"{safe_float(row.get('probability_positive')):.0f}%"
                    if has_probability
                    else ""
                ),
            )
