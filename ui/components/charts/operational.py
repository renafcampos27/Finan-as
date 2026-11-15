from __future__ import annotations

from typing import Any, Callable, Optional

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget

from ui.components.charts.base import StableChart
from ui.components.charts.financial_health import ResilienceScenarioChart
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


def _draw_tooltip_panel(painter: QPainter, theme: Theme, rect: QRectF) -> None:
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor(0, 0, 0, 40))
    painter.drawRoundedRect(rect.translated(0, 3), 11, 11)
    painter.setPen(QPen(_chart_grid(theme), 1))
    painter.setBrush(_chart_tooltip_bg(theme))
    painter.drawRoundedRect(rect, 11, 11)


class CashRunwayProbabilityChart(ResilienceScenarioChart):
    """Operational cash fan with a separate end-balance probability per month."""

    def __init__(
        self,
        theme_provider: Callable[[], Theme],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(theme_provider, parent)
        self.projection: dict[str, Any] = {}
        self.setMinimumHeight(330)
        self.setAccessibleName("Probabilidade de cada mês terminar com saldo positivo")
        self.set_detail_field_types(
            {"probability_positive": "percent", "month_index": "count"}
        )

    def _plot(self) -> QRectF:
        return QRectF(
            78,
            66,
            max(120, self.width() - 102),
            max(130, self.height() - 120),
        )

    def set_projection(self, data: dict[str, Any]) -> None:
        self.projection = dict(data or {})
        super().set_data(list(self.projection.get("rows") or []))
        if self.rows:
            ending = self.rows[-1]
            self.setAccessibleDescription(
                f"Saldo base final {eur(ending.get('base', 0), self.theme_provider().privacy)}; "
                f"probabilidade de o mês final terminar acima de zero "
                f"{safe_float(ending.get('probability_positive')):.0f} por cento. "
                "Esta percentagem não descreve todo o percurso anterior."
            )

    def render_chart(self, painter: QPainter) -> None:
        super().render_chart(painter)
        if not self.rows:
            return
        theme = self.theme_provider()
        selected = (
            self.rows[self.hover_index]
            if self.hover_index is not None and 0 <= self.hover_index < len(self.rows)
            else self.rows[-1]
        )
        probability = safe_float(selected.get("probability_positive"))
        color = (
            _chart_positive(theme)
            if probability >= 80
            else QColor("#FF9F0A")
            if probability >= 50
            else _chart_negative(theme)
        )
        label = (
            f"{selected.get('label', '')} · {probability:.0f}% de fechar acima de 0 €"
        )
        width = min(270.0, max(190.0, self.width() * 0.42))
        badge = QRectF(max(8.0, self.width() - width - 44.0), 35.0, width, 25.0)
        background = QColor(color)
        background.setAlpha(35 if theme.dark else 24)
        painter.setPen(QPen(color, 1))
        painter.setBrush(background)
        painter.drawRoundedRect(badge, 10, 10)
        painter.setPen(_chart_text(theme))
        painter.setFont(QFont("Segoe UI", 8, QFont.DemiBold))
        painter.drawText(badge.adjusted(8, 0, -8, 0), Qt.AlignCenter, label)


class BudgetVarianceHeatmapChart(StableChart):
    """Month/category heatmap that never drops categories with visible data."""

    def __init__(
        self,
        theme_provider: Callable[[], Theme],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(theme_provider, parent)
        self.months: list[dict[str, Any]] = []
        self.categories: list[str] = []
        self.rows: list[dict[str, Any]] = []
        self.hover_index: Optional[int] = None
        self._hits: list[tuple[int, QRectF]] = []
        self.setMinimumHeight(340)
        self.setAccessibleName("Mapa de desvios do orçamento por mês e categoria")
        self.set_detail_field_types(
            {
                "spent": "money",
                "budget": "money",
                "variance": "money",
                "variance_pct": "percent",
                "utilisation": "percent",
            }
        )

    def set_data(self, data: dict[str, Any]) -> None:
        self.months = [dict(row) for row in data.get("months", []) or []]
        self.categories = [str(value) for value in data.get("categories", []) or []]
        self.rows = [dict(row) for row in data.get("cells", []) or []]
        self.hover_index = None
        self._hits.clear()
        self.setAccessibleDescription(
            f"{len(self.categories)} categorias em {len(self.months)} meses. "
            "Verde indica controlo, laranja proximidade do limite e vermelho excesso."
        )
        self.invalidate_cache()

    def _cell_at(self, position: QPointF) -> Optional[int]:
        return next(
            (index for index, rect in self._hits if rect.contains(position)),
            None,
        )

    def mouseMoveEvent(self, event):  # type: ignore[override]
        selected = self._cell_at(event.position())
        if selected != self.hover_index:
            self.hover_index = selected
            self.setCursor(
                Qt.PointingHandCursor if selected is not None else Qt.ArrowCursor
            )
            self.invalidate_cache()

    def leaveEvent(self, event):  # type: ignore[override]
        self.hover_index = None
        self.setCursor(Qt.ArrowCursor)
        self.invalidate_cache()
        super().leaveEvent(event)

    @staticmethod
    def _cell_color(theme: Theme, row: dict[str, Any]) -> QColor:
        utilisation = safe_float(row.get("utilisation"))
        budget = safe_float(row.get("budget"))
        spent = safe_float(row.get("spent"))
        if budget <= 0 and spent > 0 or utilisation > 100:
            base = _chart_negative(theme)
        elif utilisation >= 80:
            base = QColor("#FF9F0A")
        elif utilisation >= 55:
            base = _chart_secondary(theme)
        else:
            base = _chart_positive(theme)
        color = QColor(base)
        intensity = max(0.30, min(1.0, utilisation / 115.0 if budget > 0 else 0.9))
        color.setAlpha(round(70 + intensity * 155))
        return color

    def render_chart(self, painter: QPainter) -> None:
        theme = self.theme_provider()
        painter.fillRect(self.rect(), _chart_bg(theme))
        self._hits.clear()
        if not self.rows or not self.months or not self.categories:
            self.draw_empty(painter, "Sem orçamentos ou despesas para comparar")
            return
        label_width = min(170.0, max(104.0, self.width() * 0.22))
        top, right, bottom = 50.0, 10.0, 48.0
        grid_width = max(120.0, self.width() - label_width - right - 8.0)
        grid_height = max(150.0, self.height() - top - bottom)
        cell_width = grid_width / max(1, len(self.months))
        cell_height = min(28.0, grid_height / max(1, len(self.categories)))
        actual_height = cell_height * len(self.categories)
        start_y = top + max(0.0, (grid_height - actual_height) / 2.0)
        cell_map = {
            (str(row.get("category")), str(row.get("month"))): index
            for index, row in enumerate(self.rows)
        }
        painter.setFont(QFont("Segoe UI", 7 if cell_width < 34 else 8, QFont.DemiBold))
        painter.setPen(_chart_muted(theme))
        for column, month in enumerate(self.months):
            label = str(month.get("label") or month.get("month") or "")
            if cell_width < 42:
                label = label.split(" ", 1)[0]
            painter.drawText(
                QRectF(label_width + column * cell_width, 20, cell_width, 25),
                Qt.AlignCenter,
                label,
            )
        painter.setFont(QFont("Segoe UI", 8, QFont.DemiBold))
        for row_index, category in enumerate(self.categories):
            y = start_y + row_index * cell_height
            category_text = QFontMetrics(painter.font()).elidedText(
                category, Qt.ElideRight, max(40, int(label_width - 12))
            )
            painter.setPen(_chart_text(theme))
            painter.drawText(
                QRectF(4, y, label_width - 10, cell_height),
                Qt.AlignLeft | Qt.AlignVCenter,
                category_text,
            )
            for column, month in enumerate(self.months):
                index = cell_map.get((category, str(month.get("month"))))
                rect = QRectF(
                    label_width + column * cell_width + 1.5,
                    y + 1.5,
                    max(3.0, cell_width - 3.0),
                    max(3.0, cell_height - 3.0),
                )
                if index is None:
                    painter.setPen(QPen(_chart_grid(theme), 1))
                    painter.setBrush(_chart_grid(theme))
                    painter.drawRoundedRect(rect, 4, 4)
                    continue
                item = self.rows[index]
                color = self._cell_color(theme, item)
                hovered = index == self.hover_index
                painter.setPen(
                    QPen(
                        _chart_text(theme) if hovered else QColor(color).lighter(120),
                        2 if hovered else 1,
                    )
                )
                painter.setBrush(color)
                painter.drawRoundedRect(rect, 4, 4)
                self._hits.append((index, rect))
        painter.setFont(QFont("Segoe UI", 8, QFont.DemiBold))
        painter.setPen(_chart_muted(theme))
        painter.drawText(
            QRectF(label_width, 0, grid_width, 18),
            Qt.AlignLeft | Qt.AlignVCenter,
            "● controlado   ● perto do limite   ● ultrapassado",
        )
        if self.hover_index is not None and 0 <= self.hover_index < len(self.rows):
            item = self.rows[self.hover_index]
            width, height = min(300.0, max(230.0, self.width() - 16.0)), 94.0
            rect = QRectF(8.0, max(8.0, self.height() - height - 8.0), width, height)
            _draw_tooltip_panel(painter, theme, rect)
            painter.setPen(_chart_text(theme))
            painter.setFont(QFont("Segoe UI", 8, QFont.Bold))
            painter.drawText(
                rect.adjusted(11, 8, -11, -8),
                Qt.AlignLeft | Qt.AlignTop,
                f"{item.get('category')} · {item.get('month_label')}\n"
                f"Realizado {eur(item.get('spent', 0), theme.privacy)} · "
                f"orçamento {eur(item.get('budget', 0), theme.privacy)}\n"
                f"Desvio {eur(item.get('variance', 0), theme.privacy)} · "
                f"{safe_float(item.get('utilisation')):.0f}% usado\n"
                f"{item.get('status')}",
            )


class RecurringCommitmentsTimelineChart(StableChart):
    """Stack explicit, inferred and unlinked recurring commitments by month."""

    def __init__(
        self,
        theme_provider: Callable[[], Theme],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(theme_provider, parent)
        self.rows: list[dict[str, Any]] = []
        self.hover_index: Optional[int] = None
        self._hits: list[tuple[int, QRectF]] = []
        self.setMinimumHeight(330)
        self.setAccessibleName("Linha temporal dos compromissos recorrentes")
        self.set_detail_field_types(
            {
                "confirmed": "money",
                "probable": "money",
                "unlinked": "money",
                "total": "money",
                "event_count": "count",
            }
        )

    def set_data(self, data: dict[str, Any]) -> None:
        self.rows = [dict(row) for row in data.get("rows", []) or []]
        self.hover_index = None
        self._hits.clear()
        self.setAccessibleDescription(
            f"{len(self.rows)} meses; compromissos confirmados, prováveis e por associar."
        )
        self.invalidate_cache()

    def mouseMoveEvent(self, event):  # type: ignore[override]
        selected = next(
            (index for index, rect in self._hits if rect.contains(event.position())),
            None,
        )
        if selected != self.hover_index:
            self.hover_index = selected
            self.setCursor(
                Qt.PointingHandCursor if selected is not None else Qt.ArrowCursor
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
        self._hits.clear()
        if not self.rows or not any(
            safe_float(row.get("total")) > 0 for row in self.rows
        ):
            self.draw_empty(painter, "Sem compromissos recorrentes neste horizonte")
            return
        plot = QRectF(62, 48, max(130, self.width() - 82), max(140, self.height() - 96))
        maximum = max(
            1.0, max(safe_float(row.get("total")) for row in self.rows) * 1.15
        )

        def y_for(value: float) -> float:
            return plot.bottom() - max(0.0, value) / maximum * plot.height()

        for tick in range(5):
            value = maximum * tick / 4
            y = y_for(value)
            painter.setPen(QPen(_chart_grid(theme), 1))
            painter.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
            painter.setPen(_chart_muted(theme))
            painter.setFont(QFont("Segoe UI", 7))
            painter.drawText(
                QRectF(0, y - 9, 56, 18),
                Qt.AlignRight | Qt.AlignVCenter,
                format_axis_money(value, theme.privacy),
            )
        colors = {
            "confirmed": _chart_positive(theme),
            "probable": QColor("#FF9F0A"),
            "unlinked": _chart_negative(theme),
        }
        painter.setFont(QFont("Segoe UI", 8, QFont.DemiBold))
        painter.setPen(_chart_muted(theme))
        painter.drawText(
            QRectF(plot.left(), 10, plot.width(), 25),
            Qt.AlignLeft | Qt.AlignVCenter,
            "● Confirmado    ● Provável · confirmar    ● Por associar",
        )
        slot = plot.width() / len(self.rows)
        bar_width = max(12.0, min(54.0, slot * 0.62))
        for index, row in enumerate(self.rows):
            x = plot.left() + slot * (index + 0.5)
            bottom = plot.bottom()
            accumulated = 0.0
            for key in ("confirmed", "probable", "unlinked"):
                value = safe_float(row.get(key))
                if value <= 0:
                    continue
                top = y_for(accumulated + value)
                segment = QRectF(x - bar_width / 2, top, bar_width, bottom - top)
                color = QColor(colors[key])
                color.setAlpha(235 if index == self.hover_index else 190)
                painter.setPen(QPen(color.lighter(120), 1))
                painter.setBrush(color)
                painter.drawRoundedRect(segment, 4, 4)
                bottom = top
                accumulated += value
            hit = QRectF(x - slot / 2, plot.top(), slot, plot.height() + 32)
            self._hits.append((index, hit))
            painter.setPen(_chart_muted(theme))
            painter.drawText(
                QRectF(x - slot / 2, plot.bottom() + 7, slot, 22),
                Qt.AlignHCenter | Qt.AlignTop,
                str(row.get("label") or ""),
            )
        if self.hover_index is not None and 0 <= self.hover_index < len(self.rows):
            item = self.rows[self.hover_index]
            width = min(280.0, max(220.0, self.width() - 16.0))
            rect = QRectF(8, 54, width, 104)
            _draw_tooltip_panel(painter, theme, rect)
            painter.setPen(_chart_text(theme))
            painter.setFont(QFont("Segoe UI", 8, QFont.Bold))
            painter.drawText(
                rect.adjusted(11, 8, -11, -8),
                Qt.AlignLeft | Qt.AlignTop,
                f"{item.get('label')} · {int(item.get('event_count', 0) or 0)} evento(s)\n"
                f"Confirmado {eur(item.get('confirmed', 0), theme.privacy)}\n"
                f"Provável {eur(item.get('probable', 0), theme.privacy)}\n"
                f"Por associar {eur(item.get('unlinked', 0), theme.privacy)}\n"
                f"Total possível {eur(item.get('total', 0), theme.privacy)}",
            )


class DataQualityTrendChart(StableChart):
    """Twelve-month data quality score with visible issue volume."""

    def __init__(
        self,
        theme_provider: Callable[[], Theme],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(theme_provider, parent)
        self.rows: list[dict[str, Any]] = []
        self.hover_index: Optional[int] = None
        self.setMinimumHeight(300)
        self.setAccessibleName("Tendência mensal da qualidade dos dados")
        self.set_detail_field_types(
            {
                "score": "number",
                "total": "count",
                "generic": "count",
                "duplicates": "count",
                "weak_descriptions": "count",
                "issues": "count",
            }
        )

    def set_data(self, rows: list[dict[str, Any]]) -> None:
        self.rows = [dict(row) for row in rows]
        self.hover_index = None
        if self.rows:
            self.setAccessibleDescription(
                f"Qualidade de {self.rows[0].get('score', 0)} no primeiro mês "
                f"até {self.rows[-1].get('score', 0)} no último mês com dados."
            )
        else:
            self.setAccessibleDescription("Sem meses ativos para avaliar")
        self.invalidate_cache()

    def _plot(self) -> QRectF:
        return QRectF(48, 42, max(120, self.width() - 68), max(130, self.height() - 92))

    def mouseMoveEvent(self, event):  # type: ignore[override]
        plot = self._plot()
        if self.rows and plot.adjusted(-10, -10, 10, 25).contains(event.position()):
            ratio = (event.position().x() - plot.left()) / max(1.0, plot.width())
            selected = max(
                0, min(len(self.rows) - 1, round(ratio * (len(self.rows) - 1)))
            )
        else:
            selected = None
        if selected != self.hover_index:
            self.hover_index = selected
            self.setCursor(
                Qt.PointingHandCursor if selected is not None else Qt.ArrowCursor
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
        if not self.rows:
            self.draw_empty(painter, "Sem meses ativos para avaliar a qualidade")
            return
        plot = self._plot()

        def point(index: int) -> QPointF:
            x = plot.left() + index / max(1, len(self.rows) - 1) * plot.width()
            score = max(0.0, min(100.0, safe_float(self.rows[index].get("score"))))
            return QPointF(x, plot.bottom() - score / 100.0 * plot.height())

        for value in (0, 25, 50, 75, 100):
            y = plot.bottom() - value / 100.0 * plot.height()
            painter.setPen(QPen(_chart_grid(theme), 1))
            painter.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
            painter.setPen(_chart_muted(theme))
            painter.setFont(QFont("Segoe UI", 7))
            painter.drawText(
                QRectF(0, y - 9, 40, 18),
                Qt.AlignRight | Qt.AlignVCenter,
                str(value),
            )
        maximum_issues = max(
            1, max(int(row.get("issues", 0) or 0) for row in self.rows)
        )
        slot = plot.width() / max(1, len(self.rows))
        for index, row in enumerate(self.rows):
            issues = int(row.get("issues", 0) or 0)
            bar_height = plot.height() * 0.22 * issues / maximum_issues
            bar = QRectF(
                plot.left() + slot * index + slot * 0.22,
                plot.bottom() - bar_height,
                max(3.0, slot * 0.56),
                bar_height,
            )
            color = QColor(_chart_negative(theme))
            color.setAlpha(58 if theme.dark else 42)
            painter.setPen(Qt.NoPen)
            painter.setBrush(color)
            painter.drawRoundedRect(bar, 3, 3)
        points = [point(index) for index in range(len(self.rows))]
        path = QPainterPath(points[0])
        for current in points[1:]:
            path.lineTo(current)
        painter.setPen(
            QPen(_chart_accent(theme), 2.7, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
        )
        painter.setBrush(Qt.NoBrush)
        painter.drawPath(path)
        for index, current in enumerate(points):
            row = self.rows[index]
            score = safe_float(row.get("score"))
            color = (
                _chart_positive(theme)
                if score >= 72
                else QColor("#FF9F0A")
                if score >= 50
                else _chart_negative(theme)
            )
            if index == self.hover_index:
                halo = QColor(color)
                halo.setAlpha(55)
                painter.setPen(Qt.NoPen)
                painter.setBrush(halo)
                painter.drawEllipse(current, 9, 9)
            painter.setPen(QPen(_chart_bg(theme), 2))
            painter.setBrush(color)
            painter.drawEllipse(current, 4.5, 4.5)
            painter.setPen(_chart_muted(theme))
            painter.setFont(QFont("Segoe UI", 7))
            painter.drawText(
                QRectF(current.x() - slot / 2, plot.bottom() + 7, slot, 20),
                Qt.AlignHCenter | Qt.AlignTop,
                str(row.get("label") or ""),
            )
        painter.setPen(_chart_muted(theme))
        painter.setFont(QFont("Segoe UI", 8, QFont.DemiBold))
        painter.drawText(
            QRectF(plot.left(), 9, plot.width(), 23),
            Qt.AlignLeft | Qt.AlignVCenter,
            "● Qualidade /100    ▮ ocorrências a rever",
        )
        if self.hover_index is not None:
            row = self.rows[self.hover_index]
            anchor = points[self.hover_index]
            width, height = 230.0, 92.0
            x = max(8.0, min(anchor.x() + 12.0, self.width() - width - 8.0))
            y = max(
                36.0,
                min(anchor.y() - height - 8.0, self.height() - height - 8.0),
            )
            rect = QRectF(x, y, width, height)
            _draw_tooltip_panel(painter, theme, rect)
            painter.setPen(_chart_text(theme))
            painter.setFont(QFont("Segoe UI", 8, QFont.Bold))
            painter.drawText(
                rect.adjusted(11, 8, -11, -8),
                Qt.AlignLeft | Qt.AlignTop,
                f"{row.get('month')} · {safe_float(row.get('score')):.0f}/100\n"
                f"{row.get('marker', '')} {row.get('status', '')}\n"
                f"Genéricas {int(row.get('generic', 0) or 0)} · "
                f"duplicados {int(row.get('duplicates', 0) or 0)}\n"
                f"Descrições fracas {int(row.get('weak_descriptions', 0) or 0)}",
            )


__all__ = [
    "BudgetVarianceHeatmapChart",
    "CashRunwayProbabilityChart",
    "DataQualityTrendChart",
    "RecurringCommitmentsTimelineChart",
]
