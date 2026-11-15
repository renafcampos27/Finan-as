"""Interactive charts for forecast quality and spending structure."""

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
    _chart_negative,
    _chart_positive,
    _chart_secondary,
    _chart_text,
    _chart_tooltip_bg,
)
from ui.core.theme import Theme
from utils.helpers import eur, percent, safe_float


class ForecastAccuracyChart(StableChart):
    """Actual versus forecasts with point error and interval-quality scoring."""

    supports_zoom = True
    maximum_zoom = 4.0

    def __init__(
        self,
        theme_provider: Callable[[], Theme],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(theme_provider, parent)
        self.rows: list[dict[str, Any]] = []
        self.metrics: dict[str, Any] = {}
        self.hover_index: Optional[int] = None
        self.setMinimumHeight(360)
        self.setAccessibleName("Qualidade histórica das previsões de despesas")

    def set_data(self, data: dict[str, Any]) -> None:
        self.rows = [dict(row) for row in data.get("rows", [])]
        self.metrics = dict(data)
        self.hover_index = None
        if self.rows:
            accuracy_kind = str(data.get("accuracy_kind") or "")
            validation_months = int(data.get("validation_months", 0) or 0)
            accuracy_label = (
                f"qualidade validada em {validation_months} meses"
                if accuracy_kind == "forecast_quality_validated"
                else "qualidade base das previsões"
            )
            self.setAccessibleDescription(
                f"{len(self.rows)} meses comparados; {accuracy_label} "
                f"{safe_float(data.get('accuracy_pct')):.1f} por cento, "
                f"cobertura de {safe_float(data.get('interval_coverage_pct')):.1f} "
                "por cento e penalização por faixas demasiado largas."
            )
        self.invalidate_cache()

    def _plot(self) -> QRectF:
        return QRectF(
            66,
            64,
            max(150, self.width() - 92),
            max(160, self.height() - 112),
        )

    def _maximum(self) -> float:
        return max(
            1.0,
            *(
                max(
                    safe_float(row.get("actual")),
                    safe_float(row.get("predicted")),
                    safe_float(row.get("high")),
                )
                for row in self.rows
            ),
        )

    def _point(self, index: int, value: float, maximum: float) -> QPointF:
        plot = self._plot()
        content_width = plot.width() * self.zoom_factor
        x = (
            plot.left()
            + index / max(1, len(self.rows) - 1) * content_width
            - self.pan_offset
        )
        y = plot.bottom() - max(0.0, value) / maximum * plot.height()
        return QPointF(x, y)

    def _index_at(self, position: QPointF) -> Optional[int]:
        if not self.rows or not self._plot().adjusted(-12, -12, 12, 12).contains(
            position
        ):
            return None
        maximum = self._maximum()
        positions = [
            self._point(index, safe_float(row.get("actual")), maximum)
            for index, row in enumerate(self.rows)
        ]
        candidates = [
            (abs(point.x() - position.x()), index)
            for index, point in enumerate(positions)
            if self._plot().left() - 10 <= point.x() <= self._plot().right() + 10
        ]
        if not candidates:
            return None
        distance, index = min(candidates)
        return index if distance <= 24 else None

    def mouseMoveEvent(self, event):  # type: ignore[override]
        self.hover_index = self._index_at(event.position())
        self.setCursor(
            Qt.PointingHandCursor if self.hover_index is not None else Qt.ArrowCursor
        )
        self.invalidate_cache()

    def leaveEvent(self, event):  # type: ignore[override]
        self.hover_index = None
        self.setCursor(Qt.ArrowCursor)
        self.invalidate_cache()
        super().leaveEvent(event)

    @staticmethod
    def _month_label(value: str) -> str:
        text = str(value or "")
        return f"{text[5:7]}/{text[2:4]}" if len(text) >= 7 else text

    def render_chart(self, painter: QPainter) -> None:
        theme = self.theme_provider()
        painter.fillRect(self.rect(), _chart_bg(theme))
        if not self.rows:
            self.draw_empty(
                painter,
                "São necessários pelo menos quatro meses completos para testar previsões",
            )
            return

        plot = self._plot()
        maximum = self._maximum()
        painter.setFont(QFont("Segoe UI", 8))
        for step in range(5):
            value = maximum * step / 4
            y = plot.bottom() - plot.height() * step / 4
            painter.setPen(QPen(_chart_grid(theme), 1))
            painter.drawLine(int(plot.left()), int(y), int(plot.right()), int(y))
            painter.setPen(_chart_muted(theme))
            painter.drawText(
                QRectF(0, y - 10, 58, 20),
                Qt.AlignRight | Qt.AlignVCenter,
                eur(value, theme.privacy),
            )

        accuracy = safe_float(self.metrics.get("accuracy_pct"))
        coverage = safe_float(self.metrics.get("interval_coverage_pct"))
        calibrated = (
            str(self.metrics.get("accuracy_kind") or "") == "forecast_quality_validated"
        )
        validation_months = int(self.metrics.get("validation_months", 0) or 0)
        painter.setFont(QFont("Segoe UI", 9, QFont.Bold))
        painter.setPen(
            _chart_positive(theme) if accuracy >= 75 else _chart_negative(theme)
        )
        painter.drawText(
            QRectF(plot.left(), 8, min(390.0, plot.width()), 24),
            Qt.AlignLeft | Qt.AlignVCenter,
            (
                f"Qualidade {percent(accuracy)} · cobertura {percent(coverage)} · "
                f"{validation_months} meses"
                if calibrated
                else f"Qualidade base {percent(accuracy)} · cobertura {percent(coverage)}"
            ),
        )
        painter.setFont(QFont("Segoe UI", 8))
        painter.setPen(_chart_muted(theme))
        painter.drawText(
            QRectF(plot.left(), 31, plot.width(), 22),
            Qt.AlignLeft | Qt.AlignVCenter,
            f"largura média {percent(self.metrics.get('mean_band_width_pct', 0))}  ·  "
            f"erro médio {eur(self.metrics.get('mae', 0), theme.privacy)}  ·  "
            f"Viés {eur(self.metrics.get('bias', 0), theme.privacy)}  ·  "
            "qualidade combina erro, cobertura e precisão da faixa",
        )

        actual_color = _chart_accent(theme)
        forecast_color = QColor("#BF5AF2" if theme.dark else "#7C3AED")
        upper = [
            self._point(index, safe_float(row.get("high")), maximum)
            for index, row in enumerate(self.rows)
        ]
        lower = [
            self._point(index, safe_float(row.get("low")), maximum)
            for index, row in enumerate(self.rows)
        ]
        band = QPainterPath()
        band.moveTo(upper[0])
        for point in upper[1:]:
            band.lineTo(point)
        for point in reversed(lower):
            band.lineTo(point)
        band.closeSubpath()
        band_color = QColor(forecast_color)
        band_color.setAlpha(30 if theme.dark else 22)
        painter.setPen(Qt.NoPen)
        painter.setBrush(band_color)
        painter.drawPath(band)

        actual_points = [
            self._point(index, safe_float(row.get("actual")), maximum)
            for index, row in enumerate(self.rows)
        ]
        forecast_points = [
            self._point(index, safe_float(row.get("predicted")), maximum)
            for index, row in enumerate(self.rows)
        ]
        for points, color, style in (
            (forecast_points, forecast_color, Qt.DashLine),
            (actual_points, actual_color, Qt.SolidLine),
        ):
            path = QPainterPath(points[0])
            for point in points[1:]:
                path.lineTo(point)
            painter.setBrush(Qt.NoBrush)
            painter.setPen(QPen(color, 2.6, style, Qt.RoundCap, Qt.RoundJoin))
            painter.drawPath(path)

        for index, row in enumerate(self.rows):
            actual = actual_points[index]
            predicted = forecast_points[index]
            if plot.left() <= actual.x() <= plot.right():
                actual_marker = (
                    _chart_positive(theme)
                    if bool(row.get("within_band"))
                    else _chart_negative(theme)
                    if bool(row.get("is_interval_evaluation"))
                    else actual_color
                )
                painter.setPen(QPen(_chart_bg(theme), 1.5))
                painter.setBrush(actual_marker)
                painter.drawEllipse(actual, 4.5, 4.5)
                painter.setBrush(forecast_color)
                painter.drawEllipse(predicted, 3.8, 3.8)
                if (
                    len(self.rows) <= 8
                    or index in {0, len(self.rows) - 1}
                    or index % 2 == 0
                ):
                    painter.setPen(_chart_muted(theme))
                    painter.setFont(QFont("Segoe UI", 8))
                    painter.drawText(
                        QRectF(actual.x() - 28, plot.bottom() + 8, 56, 20),
                        Qt.AlignHCenter | Qt.AlignTop,
                        self._month_label(str(row.get("month") or "")),
                    )

        painter.setFont(QFont("Segoe UI", 8, QFont.Bold))
        painter.setPen(actual_color)
        painter.drawText(
            QRectF(plot.right() - 185, 8, 85, 22),
            Qt.AlignRight | Qt.AlignVCenter,
            "● Real",
        )
        painter.setPen(forecast_color)
        painter.drawText(
            QRectF(plot.right() - 95, 8, 95, 22),
            Qt.AlignRight | Qt.AlignVCenter,
            "● Previsto",
        )

        if self.hover_index is not None:
            row = self.rows[self.hover_index]
            anchor = actual_points[self.hover_index]
            box = QRectF(
                min(max(8.0, anchor.x() - 104.0), self.width() - 224.0),
                max(58.0, min(anchor.y() - 154.0, self.height() - 158.0)),
                216,
                142,
            )
            painter.setPen(QPen(_chart_grid(theme), 1))
            painter.setBrush(_chart_tooltip_bg(theme))
            painter.drawRoundedRect(box, 11, 11)
            painter.setPen(_chart_text(theme))
            painter.setFont(QFont("Segoe UI", 8, QFont.Bold))
            painter.drawText(
                box.adjusted(12, 8, -12, -8),
                Qt.AlignLeft | Qt.AlignVCenter,
                f"{row.get('month', '')}\n"
                f"Real: {eur(row.get('actual', 0), theme.privacy)}\n"
                f"Previsto: {eur(row.get('predicted', 0), theme.privacy)}\n"
                f"Faixa: {eur(row.get('low', 0), theme.privacy)} a "
                f"{eur(row.get('high', 0), theme.privacy)}\n"
                f"Qualidade: {percent(row.get('forecast_quality_pct', 0))} · "
                f"largura {percent(row.get('band_width_pct', 0))}\n"
                f"{row.get('band_status', 'Sem avaliação')} · "
                f"erro pontual {percent(row.get('error_pct', 0))}",
            )


class CategoryForecastAccuracyChart(StableChart):
    """Show forecast quality, coverage and regularity for each category."""

    def __init__(
        self,
        theme_provider: Callable[[], Theme],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(theme_provider, parent)
        self.rows: list[dict[str, Any]] = []
        self.metrics: dict[str, Any] = {}
        self.hover_index: Optional[int] = None
        self._row_rects: list[QRectF] = []
        self.setMinimumHeight(320)
        self.setAccessibleName("Qualidade das previsões por categoria")

    def set_data(self, data: dict[str, Any]) -> None:
        self.rows = [dict(row) for row in data.get("rows", [])]
        self.metrics = dict(data)
        self.hover_index = None
        self._row_rects.clear()
        height = max(320, 94 + len(self.rows) * 43)
        self.setMinimumHeight(height)
        self.updateGeometry()
        best = dict(data.get("best") or {})
        hardest = dict(data.get("hardest") or {})
        if self.rows:
            self.setAccessibleDescription(
                f"{len(self.rows)} categorias avaliadas. Maior qualidade: "
                f"{best.get('category', 'sem dados')}; menor qualidade: "
                f"{hardest.get('category', 'sem dados')}."
            )
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

    @staticmethod
    def _tone_color(theme: Theme, tone: str) -> QColor:
        if tone == "positive":
            return _chart_positive(theme)
        if tone == "negative":
            return _chart_negative(theme)
        if tone == "warning":
            return QColor("#FFB020" if theme.dark else "#C66A00")
        return _chart_secondary(theme)

    def render_chart(self, painter: QPainter) -> None:
        theme = self.theme_provider()
        painter.fillRect(self.rect(), _chart_bg(theme))
        self._row_rects = []
        if not self.rows:
            self.draw_empty(
                painter,
                "Ainda não existem meses suficientes por categoria",
            )
            return

        label_width = min(235.0, max(140.0, self.width() * 0.25))
        left = label_width + 18.0
        right = 190.0
        width = max(120.0, self.width() - left - right)
        top = 54.0
        row_height = 43.0

        painter.setFont(QFont("Segoe UI", 8, QFont.Bold))
        painter.setPen(_chart_muted(theme))
        painter.drawText(
            QRectF(left, 12, width, 24),
            Qt.AlignLeft | Qt.AlignVCenter,
            "Qualidade combinada 0–100",
        )
        painter.drawText(
            QRectF(left + width + 8, 12, right - 12, 24),
            Qt.AlignRight | Qt.AlignVCenter,
            "Estado · cobertura",
        )
        for threshold, label in ((60.0, "60"), (80.0, "80")):
            x = left + width * threshold / 100.0
            painter.setPen(QPen(_chart_grid(theme), 1, Qt.DashLine))
            painter.drawLine(
                QPointF(x, top - 9),
                QPointF(x, top + len(self.rows) * row_height - 5),
            )
            painter.setPen(_chart_muted(theme))
            painter.drawText(
                QRectF(x - 14, 33, 28, 18),
                Qt.AlignCenter,
                label,
            )

        for index, row in enumerate(self.rows):
            y = top + index * row_height
            full_rect = QRectF(0, y - 3, self.width(), row_height)
            self._row_rects.append(full_rect)
            color = self._tone_color(theme, str(row.get("tone") or "neutral"))
            if index == self.hover_index:
                hover = QColor(color)
                hover.setAlpha(26 if theme.dark else 20)
                painter.fillRect(full_rect, hover)

            category = str(row.get("category") or "Outros")
            painter.setFont(QFont("Segoe UI", 8, QFont.Bold))
            painter.setPen(_chart_text(theme))
            painter.drawText(
                QRectF(6, y, label_width, 30),
                Qt.AlignRight | Qt.AlignVCenter,
                painter.fontMetrics().elidedText(
                    category,
                    Qt.ElideRight,
                    int(label_width - 6),
                ),
            )

            accuracy = max(
                0.0,
                min(100.0, safe_float(row.get("accuracy_pct"))),
            )
            painter.setPen(Qt.NoPen)
            painter.setBrush(_chart_grid(theme))
            painter.drawRoundedRect(QRectF(left, y + 7, width, 17), 8.5, 8.5)
            painter.setBrush(color)
            painter.drawRoundedRect(
                QRectF(left, y + 7, max(2.0, width * accuracy / 100.0), 17),
                8.5,
                8.5,
            )
            painter.setPen(_chart_text(theme))
            painter.setFont(QFont("Segoe UI", 8, QFont.Bold))
            painter.drawText(
                QRectF(left + 7, y + 5, width - 14, 21),
                Qt.AlignLeft | Qt.AlignVCenter,
                percent(accuracy),
            )
            painter.setPen(color)
            painter.drawText(
                QRectF(left + width + 8, y, right - 12, 30),
                Qt.AlignRight | Qt.AlignVCenter,
                f"{row.get('status', '—')} · "
                f"{percent(row.get('interval_coverage_pct', 0))}",
            )

        if self.hover_index is not None:
            row = self.rows[self.hover_index]
            y = top + self.hover_index * row_height
            box = QRectF(
                min(max(8.0, left + width * 0.30), self.width() - 280.0),
                max(42.0, min(y - 132.0, self.height() - 146.0)),
                272,
                134,
            )
            painter.setPen(QPen(_chart_grid(theme), 1))
            painter.setBrush(_chart_tooltip_bg(theme))
            painter.drawRoundedRect(box, 12, 12)
            painter.setPen(_chart_text(theme))
            painter.setFont(QFont("Segoe UI", 8, QFont.Bold))
            painter.drawText(
                box.adjusted(12, 8, -12, -8),
                Qt.AlignLeft | Qt.AlignVCenter,
                f"{row.get('category', '')} · {row.get('status', '')}\n"
                f"Qualidade: {percent(row.get('accuracy_pct', 0))}\n"
                f"Cobertura: {percent(row.get('interval_coverage_pct', 0))} · "
                f"{row.get('interval_hits', 0)}/"
                f"{row.get('interval_total', 0)} resultados\n"
                f"Largura média: {percent(row.get('mean_band_width_pct', 0))} · "
                f"erro {percent(row.get('wape_pct', 0))}\n"
                f"Regularidade: {percent(row.get('regularity_pct', 0))} · "
                f"{row.get('active_months', 0)} meses ativos",
            )


class ForecastErrorCausesChart(StableChart):
    """Explain a monthly forecast miss with reconciled category deltas."""

    def __init__(
        self,
        theme_provider: Callable[[], Theme],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(theme_provider, parent)
        self.rows: list[dict[str, Any]] = []
        self.month = ""
        self.actual = 0.0
        self.predicted = 0.0
        self.hover_index: Optional[int] = None
        self._row_rects: list[QRectF] = []
        self.setMinimumHeight(320)
        self.setAccessibleName("Causas do erro da previsão mensal")

    def set_data(self, row: dict[str, Any]) -> None:
        self.month = str(row.get("month") or "")
        self.actual = safe_float(row.get("actual"))
        self.predicted = safe_float(row.get("predicted"))
        self.rows = [dict(cause) for cause in row.get("causes", [])]
        self.hover_index = None
        self._row_rects.clear()
        height = max(300, 102 + len(self.rows) * 45)
        self.setMinimumHeight(height)
        self.updateGeometry()
        self.setAccessibleDescription(
            f"{self.month}: previsto {eur(self.predicted)}, real "
            f"{eur(self.actual)}; {len(self.rows)} causas principais."
        )
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

    def render_chart(self, painter: QPainter) -> None:
        theme = self.theme_provider()
        painter.fillRect(self.rect(), _chart_bg(theme))
        self._row_rects = []
        if not self.rows:
            self.draw_empty(
                painter,
                "Sem diferenças materiais para explicar neste mês",
            )
            return

        label_width = min(220.0, max(135.0, self.width() * 0.24))
        right = 118.0
        left = label_width + 16.0
        width = max(150.0, self.width() - left - right)
        center = left + width / 2.0
        half = width / 2.0
        top = 72.0
        row_height = 45.0
        maximum = max(
            1.0,
            *(abs(safe_float(row.get("delta"))) for row in self.rows),
        )
        difference = self.actual - self.predicted

        painter.setFont(QFont("Segoe UI", 9, QFont.Bold))
        painter.setPen(
            _chart_negative(theme) if difference > 0 else _chart_positive(theme)
        )
        painter.drawText(
            QRectF(8, 8, self.width() - 16, 26),
            Qt.AlignLeft | Qt.AlignVCenter,
            f"{self.month} · previsto {eur(self.predicted, theme.privacy)} · "
            f"real {eur(self.actual, theme.privacy)} · "
            f"diferença {eur(difference, theme.privacy)}",
        )
        painter.setFont(QFont("Segoe UI", 8))
        painter.setPen(_chart_muted(theme))
        painter.drawText(
            QRectF(left, 37, width, 24),
            Qt.AlignCenter,
            "Menos despesa do que previsto  ←  |  →  Mais despesa do que previsto",
        )
        painter.setPen(QPen(_chart_grid(theme), 1.4))
        painter.drawLine(
            QPointF(center, top - 8),
            QPointF(center, top + len(self.rows) * row_height - 7),
        )

        for index, row in enumerate(self.rows):
            y = top + index * row_height
            full_rect = QRectF(0, y - 3, self.width(), row_height)
            self._row_rects.append(full_rect)
            delta = safe_float(row.get("delta"))
            color = _chart_negative(theme) if delta > 0 else _chart_positive(theme)
            if index == self.hover_index:
                hover = QColor(color)
                hover.setAlpha(25 if theme.dark else 19)
                painter.fillRect(full_rect, hover)

            category = str(row.get("category") or "Outros")
            painter.setPen(_chart_text(theme))
            painter.setFont(QFont("Segoe UI", 8, QFont.Bold))
            painter.drawText(
                QRectF(6, y, label_width, 30),
                Qt.AlignRight | Qt.AlignVCenter,
                painter.fontMetrics().elidedText(
                    category,
                    Qt.ElideRight,
                    int(label_width - 6),
                ),
            )
            bar_width = max(2.0, half * abs(delta) / maximum)
            bar_rect = (
                QRectF(center, y + 7, bar_width, 17)
                if delta >= 0
                else QRectF(center - bar_width, y + 7, bar_width, 17)
            )
            painter.setPen(Qt.NoPen)
            painter.setBrush(color)
            painter.drawRoundedRect(bar_rect, 8.5, 8.5)
            painter.setPen(color)
            painter.drawText(
                QRectF(left + width + 8, y, right - 12, 30),
                Qt.AlignRight | Qt.AlignVCenter,
                ("+" if delta > 0 else "") + eur(delta, theme.privacy),
            )

        if self.hover_index is not None:
            row = self.rows[self.hover_index]
            y = top + self.hover_index * row_height
            box = QRectF(
                min(max(8.0, center - 118.0), self.width() - 280.0),
                max(44.0, min(y - 118.0, self.height() - 132.0)),
                272,
                120,
            )
            painter.setPen(QPen(_chart_grid(theme), 1))
            painter.setBrush(_chart_tooltip_bg(theme))
            painter.drawRoundedRect(box, 12, 12)
            painter.setPen(_chart_text(theme))
            painter.setFont(QFont("Segoe UI", 8, QFont.Bold))
            painter.drawText(
                box.adjusted(12, 8, -12, -8),
                Qt.AlignLeft | Qt.AlignVCenter,
                f"{row.get('category', '')}\n"
                f"{row.get('reason', '')}\n"
                f"Previsto: {eur(row.get('predicted', 0), theme.privacy)} · "
                f"real: {eur(row.get('actual', 0), theme.privacy)}\n"
                f"Impacto: {eur(row.get('delta', 0), theme.privacy)} · "
                f"{percent(row.get('contribution_pct', 0))} da variação bruta",
            )


class SpendingSeasonalityChart(StableChart):
    """Twelve-by-seven heat map of average spending per calendar day."""

    def __init__(
        self,
        theme_provider: Callable[[], Theme],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(theme_provider, parent)
        self.cells: list[dict[str, Any]] = []
        self.maximum = 0.0
        self.total = 0.0
        self.hover_index: Optional[int] = None
        self._cell_rects: list[QRectF] = []
        self.setMinimumHeight(390)
        self.setAccessibleName("Mapa de sazonalidade das despesas")

    def set_data(self, data: dict[str, Any]) -> None:
        self.cells = [dict(cell) for cell in data.get("cells", [])]
        self.maximum = safe_float(data.get("maximum"))
        self.total = safe_float(data.get("total"))
        self.hover_index = None
        peak = data.get("peak") or {}
        if peak:
            self.setAccessibleDescription(
                f"Pico em {peak.get('weekday_label')} de {peak.get('month_label')}: "
                f"{eur(peak.get('value'))} por dia."
            )
        self.invalidate_cache()

    def mouseMoveEvent(self, event):  # type: ignore[override]
        self.hover_index = next(
            (
                index
                for index, rect in enumerate(self._cell_rects)
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

    def render_chart(self, painter: QPainter) -> None:
        theme = self.theme_provider()
        painter.fillRect(self.rect(), _chart_bg(theme))
        self._cell_rects = []
        if not self.cells or self.total <= 0:
            self.draw_empty(
                painter,
                "Sem despesas suficientes para calcular sazonalidade",
            )
            return

        left = 54.0
        top = 52.0
        right = 16.0
        bottom = 48.0
        gap = 4.0
        cell_width = max(
            18.0,
            (self.width() - left - right - gap * 11) / 12,
        )
        cell_height = max(
            27.0,
            (self.height() - top - bottom - gap * 6) / 7,
        )
        month_labels = (
            "Jan",
            "Fev",
            "Mar",
            "Abr",
            "Mai",
            "Jun",
            "Jul",
            "Ago",
            "Set",
            "Out",
            "Nov",
            "Dez",
        )
        weekday_labels = ("Seg", "Ter", "Qua", "Qui", "Sex", "Sáb", "Dom")
        painter.setFont(QFont("Segoe UI", 8, QFont.Bold))
        painter.setPen(_chart_muted(theme))
        for month, label in enumerate(month_labels):
            x = left + month * (cell_width + gap)
            painter.drawText(
                QRectF(x, 18, cell_width, 24),
                Qt.AlignHCenter | Qt.AlignVCenter,
                label,
            )
        for weekday, label in enumerate(weekday_labels):
            y = top + weekday * (cell_height + gap)
            painter.drawText(
                QRectF(0, y, left - 9, cell_height),
                Qt.AlignRight | Qt.AlignVCenter,
                label,
            )

        base = _chart_accent(theme)
        for index, cell in enumerate(self.cells):
            weekday = int(cell.get("weekday", 0))
            month = int(cell.get("month", 1)) - 1
            rect = QRectF(
                left + month * (cell_width + gap),
                top + weekday * (cell_height + gap),
                cell_width,
                cell_height,
            )
            self._cell_rects.append(rect)
            value = safe_float(cell.get("value"))
            ratio = value / self.maximum if self.maximum else 0.0
            color = QColor(base)
            color.setAlpha(
                int((35 if theme.dark else 25) + ratio * (190 if theme.dark else 175))
            )
            painter.setPen(
                QPen(
                    _chart_text(theme)
                    if index == self.hover_index
                    else _chart_grid(theme),
                    2 if index == self.hover_index else 1,
                )
            )
            painter.setBrush(color)
            painter.drawRoundedRect(rect, 7, 7)
            if cell_width >= 44 and value > 0:
                painter.setPen(_chart_text(theme))
                painter.setFont(QFont("Segoe UI", 7, QFont.Bold))
                painter.drawText(
                    rect.adjusted(2, 1, -2, -1),
                    Qt.AlignCenter,
                    "•• €" if theme.privacy else eur(value),
                )

        painter.setFont(QFont("Segoe UI", 8))
        painter.setPen(_chart_muted(theme))
        painter.drawText(
            QRectF(left, self.height() - 24, self.width() - left - right, 18),
            Qt.AlignLeft | Qt.AlignVCenter,
            "Mais intenso = maior despesa média por dia · inclui dias sem gastos",
        )

        if self.hover_index is not None and self.hover_index < len(self.cells):
            cell = self.cells[self.hover_index]
            rect = self._cell_rects[self.hover_index]
            box_width = 230.0
            box = QRectF(
                min(
                    max(8.0, rect.center().x() - 100.0),
                    self.width() - box_width - 8,
                ),
                max(8.0, min(rect.top() - 102.0, self.height() - 104.0)),
                box_width,
                94,
            )
            painter.setPen(QPen(_chart_grid(theme), 1))
            painter.setBrush(_chart_tooltip_bg(theme))
            painter.drawRoundedRect(box, 11, 11)
            painter.setPen(_chart_text(theme))
            painter.setFont(QFont("Segoe UI", 8, QFont.Bold))
            painter.drawText(
                box.adjusted(12, 8, -12, -8),
                Qt.AlignLeft | Qt.AlignVCenter,
                f"{cell.get('weekday_label')} · {cell.get('month_label')}\n"
                f"Média/dia: {eur(cell.get('value', 0), theme.privacy)}\n"
                f"Total: {eur(cell.get('total', 0), theme.privacy)} · "
                f"{cell.get('movements', 0)} movimento(s)",
            )


class MerchantParetoChart(StableChart):
    """Rank merchants and overlay their cumulative share of all spending."""

    def __init__(
        self,
        theme_provider: Callable[[], Theme],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(theme_provider, parent)
        self.rows: list[dict[str, Any]] = []
        self.metrics: dict[str, Any] = {}
        self.hover_index: Optional[int] = None
        self._row_rects: list[QRectF] = []
        self.setMinimumHeight(300)
        self.setAccessibleName("Concentração 80/20 de despesas por comerciante")

    def set_data(self, data: dict[str, Any]) -> None:
        self.rows = [dict(row) for row in data.get("rows", [])]
        self.metrics = dict(data)
        self.hover_index = None
        self.setMinimumHeight(max(280, 98 + len(self.rows) * 38))
        self.updateGeometry()
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

    def render_chart(self, painter: QPainter) -> None:
        theme = self.theme_provider()
        painter.fillRect(self.rect(), _chart_bg(theme))
        self._row_rects = []
        if not self.rows:
            self.draw_empty(painter, "Sem comerciantes no período selecionado")
            return

        label_width = min(210.0, max(125.0, self.width() * 0.24))
        left = label_width + 14.0
        right = 100.0
        width = max(120.0, self.width() - left - right)
        top = 54.0
        row_height = max(
            32.0,
            min(40.0, (self.height() - 82.0) / len(self.rows)),
        )
        maximum = max(
            1.0,
            *(safe_float(row.get("total")) for row in self.rows),
        )
        accent = _chart_accent(theme)
        cumulative_color = _chart_secondary(theme)

        painter.setFont(QFont("Segoe UI", 8, QFont.Bold))
        painter.setPen(_chart_muted(theme))
        painter.drawText(
            QRectF(left, 12, width, 24),
            Qt.AlignLeft | Qt.AlignVCenter,
            "Despesa",
        )
        painter.setPen(cumulative_color)
        painter.drawText(
            QRectF(left + width - 170, 12, 170, 24),
            Qt.AlignRight | Qt.AlignVCenter,
            "● Acumulado",
        )
        cumulative_points: list[QPointF] = []
        for index, row in enumerate(self.rows):
            y = top + index * row_height
            full_rect = QRectF(0, y - 2, self.width(), row_height)
            self._row_rects.append(full_rect)
            if index == self.hover_index:
                hover = QColor(accent)
                hover.setAlpha(24 if theme.dark else 18)
                painter.fillRect(full_rect, hover)
            merchant = str(row.get("merchant") or "Sem comerciante")
            painter.setFont(QFont("Segoe UI", 8, QFont.Bold))
            painter.setPen(_chart_text(theme))
            painter.drawText(
                QRectF(5, y, label_width, row_height - 5),
                Qt.AlignRight | Qt.AlignVCenter,
                painter.fontMetrics().elidedText(
                    merchant,
                    Qt.ElideRight,
                    int(label_width - 6),
                ),
            )
            total = safe_float(row.get("total"))
            bar_width = width * total / maximum
            color = QColor(accent)
            color.setAlpha(205 if index < 3 else 145)
            painter.setPen(Qt.NoPen)
            painter.setBrush(color)
            painter.drawRoundedRect(
                QRectF(left, y + 7, max(2.0, bar_width), 16),
                8,
                8,
            )
            painter.setPen(_chart_text(theme))
            painter.setFont(QFont("Segoe UI", 8))
            painter.drawText(
                QRectF(left + width + 8, y, right - 12, row_height - 4),
                Qt.AlignRight | Qt.AlignVCenter,
                eur(total, theme.privacy),
            )
            cumulative = safe_float(row.get("cumulative_pct"))
            cumulative_points.append(QPointF(left + width * cumulative / 100.0, y + 15))

        if cumulative_points:
            path = QPainterPath(cumulative_points[0])
            for point in cumulative_points[1:]:
                path.lineTo(point)
            painter.setBrush(Qt.NoBrush)
            painter.setPen(
                QPen(
                    cumulative_color,
                    2.4,
                    Qt.SolidLine,
                    Qt.RoundCap,
                    Qt.RoundJoin,
                )
            )
            painter.drawPath(path)
            painter.setBrush(cumulative_color)
            for point in cumulative_points:
                painter.drawEllipse(point, 3.5, 3.5)

        if self.hover_index is not None:
            row = self.rows[self.hover_index]
            y = top + self.hover_index * row_height
            box = QRectF(
                min(max(8.0, left + width * 0.42), self.width() - 244.0),
                max(42.0, min(y - 88.0, self.height() - 102.0)),
                236,
                90,
            )
            painter.setPen(QPen(_chart_grid(theme), 1))
            painter.setBrush(_chart_tooltip_bg(theme))
            painter.drawRoundedRect(box, 11, 11)
            painter.setPen(_chart_text(theme))
            painter.setFont(QFont("Segoe UI", 8, QFont.Bold))
            painter.drawText(
                box.adjusted(12, 8, -12, -8),
                Qt.AlignLeft | Qt.AlignVCenter,
                f"{row.get('merchant', '')}\n"
                f"{eur(row.get('total', 0), theme.privacy)} · "
                f"{percent(row.get('share_pct', 0))} do total\n"
                f"Acumulado {percent(row.get('cumulative_pct', 0))} · "
                f"{row.get('movements', 0)} movimento(s)",
            )
