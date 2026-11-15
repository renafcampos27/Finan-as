from __future__ import annotations

import math
from typing import Any, Callable, Optional

from PySide6.QtCore import QEvent, QPointF, QRectF, Qt, Signal
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
from utils.helpers import eur, percent, safe_float


class BudgetForecastChart(StableChart):
    """Shared-scale bullet chart for actual, projected and budget values."""

    category_clicked = Signal(str)
    DEFAULT_ROW_HEIGHT = 36
    BASE_CHART_HEIGHT = 108

    def __init__(
        self,
        theme_provider: Callable[[], Theme],
        parent: Optional[QWidget] = None,
        *,
        row_height: int = DEFAULT_ROW_HEIGHT,
    ):
        super().__init__(theme_provider, parent)
        self.row_height = max(self.DEFAULT_ROW_HEIGHT, int(row_height))
        self.rows: list[dict[str, Any]] = []
        self.items: list[dict[str, Any]] = []
        self.has_budget_rows = False
        self.month_label = ""
        self.hover_index: Optional[int] = None
        self._hover_pos = QPointF()
        self._hit_rects: list[tuple[int, QRectF]] = []
        self._summary_rects: list[QRectF] = []
        self.setMinimumHeight(220)
        self.setAccessibleName("Orçamento real e previsão por categoria")
        # The chart already draws a richer in-app hover card.  A native tooltip
        # creates a second Windows popup which can ignore the application palette
        # and appear as an unreadable black rectangle.
        self.setToolTip("")

    def required_height_for_rows(self, row_count: int) -> int:
        """Return enough height to draw every category without compression."""
        return max(
            220,
            self.BASE_CHART_HEIGHT + max(0, int(row_count)) * self.row_height,
        )

    def event(self, event) -> bool:  # type: ignore[override]
        if event.type() == QEvent.ToolTip:
            event.accept()
            return True
        return super().event(event)

    @staticmethod
    def _priority(row: dict[str, Any]) -> tuple[int, float, float]:
        order = {
            "Ultrapassado": 0,
            "Em risco": 1,
            "Sem orçamento": 2,
            "Atenção": 3,
            "Controlado": 4,
        }
        return (
            order.get(str(row.get("forecast_status")), 9),
            -safe_float(row.get("projected_utilisation")),
            -safe_float(row.get("spent")),
        )

    def set_rows(self, rows: list[dict[str, Any]], month_label: str = "") -> None:
        clean: list[dict[str, Any]] = []
        self.has_budget_rows = any(
            safe_float(row.get("budget")) > 0 for row in (rows or [])
        )
        for source in rows or []:
            row = dict(source)
            budget = max(0.0, safe_float(row.get("budget")))
            row["spent"] = max(0.0, safe_float(row.get("spent")))
            row["budget"] = budget
            row["projected"] = max(
                row["spent"], safe_float(row.get("projected"), row["spent"])
            )
            row["previous_spend"] = max(0.0, safe_float(row.get("previous_spend")))
            row["projected_utilisation"] = (
                row["projected"] / budget * 100 if budget else 0.0
            )
            if (
                budget <= 0
                and max(row["spent"], row["projected"], row["previous_spend"]) <= 0
            ):
                continue
            clean.append(row)
        self.rows = sorted(clean, key=self._priority)
        self.items = self.rows
        chart_height = self.required_height_for_rows(len(self.rows))
        self.setMinimumHeight(chart_height)
        self.setMaximumHeight(chart_height)
        self.updateGeometry()
        self.month_label = str(month_label or "")
        self.hover_index = None
        self._hit_rects.clear()
        risk = sum(
            1
            for row in self.rows
            if row.get("forecast_status") in {"Ultrapassado", "Em risco"}
        )
        realised = sum(1 for row in self.rows if safe_float(row.get("spent")) > 0)
        budgeted = sum(1 for row in self.rows if safe_float(row.get("budget")) > 0)
        unbudgeted = len(self.rows) - budgeted
        self.setAccessibleDescription(
            f"{self.month_label}. {len(self.rows)} categorias com dados; "
            f"{budgeted} com orçamento; {unbudgeted} sem orçamento; "
            f"{realised} com valor realizado; {risk} em risco ou ultrapassadas."
        )
        if self.rows and self.isVisible():
            self.start_entrance_animation(650)
        else:
            self.invalidate_cache()

    def _plot_rect(self) -> QRectF:
        left = 176.0 if self.width() >= 620 else 126.0
        return QRectF(
            left,
            54.0,
            max(100.0, self.width() - left - 28.0),
            max(100.0, self.height() - 102.0),
        )

    def _scale_max(self) -> float:
        peak = max(
            (safe_float(row.get("projected_utilisation")) for row in self.rows),
            default=100.0,
        )
        rounded = math.ceil(max(120.0, peak) / 25.0) * 25.0
        return min(200.0, rounded)

    @staticmethod
    def _status_icon(status: str) -> str:
        return {
            "Controlado": "✅",
            "Atenção": "🟠",
            "Em risco": "⚠️",
            "Ultrapassado": "🚨",
            "Sem orçamento": "🧾",
        }.get(status, "•")

    @staticmethod
    def _status_color(status: str, theme: Theme) -> QColor:
        if status == "Controlado":
            return _chart_positive(theme)
        if status == "Atenção":
            return QColor("#FFB020" if theme.dark else "#C46A00")
        if status == "Em risco":
            return QColor("#FF8A4C" if theme.dark else "#D9531E")
        if status == "Ultrapassado":
            return _chart_negative(theme)
        if status == "Sem orçamento":
            return QColor("#FFB020" if theme.dark else "#C46A00")
        return _chart_accent(theme)

    def _item_at(self, pos: QPointF) -> Optional[int]:
        for index, rect in self._hit_rects:
            if rect.contains(pos):
                return index
        return None

    def mouseMoveEvent(self, event):  # type: ignore[override]
        hovered = self._item_at(event.position())
        moved = (event.position() - self._hover_pos).manhattanLength() > 2.0
        self._hover_pos = event.position()
        if hovered != self.hover_index or (hovered is not None and moved):
            self.hover_index = hovered
            self.setCursor(
                Qt.PointingHandCursor if hovered is not None else Qt.ArrowCursor
            )
            self.category_hovered.emit(
                str(self.rows[hovered].get("category") or "")
                if hovered is not None and 0 <= hovered < len(self.rows)
                else ""
            )
            self.invalidate_cache()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):  # type: ignore[override]
        self.hover_index = None
        self.setCursor(Qt.ArrowCursor)
        self.category_hovered.emit("")
        self.invalidate_cache()
        super().leaveEvent(event)

    def mousePressEvent(self, event):  # type: ignore[override]
        if event.button() == Qt.LeftButton:
            index = self._item_at(event.position())
            if index is not None and 0 <= index < len(self.rows):
                row = dict(self.rows[index])
                category = str(row.get("category") or "")
                if self.open_detail(
                    row,
                    label=category,
                    action_label=self._detail_action_label,
                ):
                    self.category_clicked.emit(category)
                event.accept()
                return
        super().mousePressEvent(event)

    def _draw_legend(self, painter: QPainter, plot: QRectF, theme: Theme) -> None:
        painter.setFont(QFont("Segoe UI", 8, QFont.DemiBold))
        painter.setPen(_chart_muted(theme))
        x = plot.left()

        actual = _chart_accent(theme)
        painter.setPen(Qt.NoPen)
        painter.setBrush(actual)
        painter.drawRoundedRect(QRectF(x, 16.0, 22.0, 8.0), 4.0, 4.0)
        painter.setPen(_chart_muted(theme))
        painter.drawText(
            QRectF(x + 29.0, 10.0, 70.0, 20.0), Qt.AlignVCenter, "Realizado"
        )

        x += 104.0
        forecast = QColor(actual)
        forecast.setAlpha(72)
        painter.setPen(QPen(actual, 1, Qt.DashLine))
        painter.setBrush(forecast)
        painter.drawRoundedRect(QRectF(x, 16.0, 22.0, 8.0), 4.0, 4.0)
        painter.setPen(_chart_muted(theme))
        painter.drawText(
            QRectF(x + 29.0, 10.0, 66.0, 20.0), Qt.AlignVCenter, "Previsto"
        )

        x += 100.0
        painter.setPen(QPen(_chart_text(theme), 2))
        painter.drawLine(QPointF(x + 10.0, 12.0), QPointF(x + 10.0, 28.0))
        painter.setPen(_chart_muted(theme))
        painter.drawText(
            QRectF(x + 20.0, 10.0, 86.0, 20.0), Qt.AlignVCenter, "Limite 100%"
        )

    @staticmethod
    def _row_summary_rect(
        plot: QRectF,
        center_y: float,
        limit_x: float,
    ) -> QRectF:
        """Keep each row label clear of the vertical 100% marker."""
        gap = 12.0
        available_width = max(
            24.0,
            min(plot.width(), limit_x - plot.left() - gap),
        )
        return QRectF(plot.left(), center_y - 18.0, available_width, 18.0)

    def _tooltip_rect(self) -> QRectF:
        width = min(310.0, max(210.0, self.width() - 18.0))
        height = 102.0
        x = max(8.0, min(self._hover_pos.x() + 14.0, self.width() - width - 8.0))
        y = self._hover_pos.y() - height - 12.0
        if y < 8.0:
            y = self._hover_pos.y() + 14.0
        y = max(8.0, min(y, self.height() - height - 8.0))
        return QRectF(x, y, width, height)

    def _draw_tooltip(self, painter: QPainter, theme: Theme) -> None:
        if self.hover_index is None or not (0 <= self.hover_index < len(self.rows)):
            return
        row = self.rows[self.hover_index]
        rect = self._tooltip_rect()
        x, y, width = rect.x(), rect.y(), rect.width()

        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(15, 23, 42, 52))
        painter.drawRoundedRect(rect.translated(0.0, 3.0), 12.0, 12.0)

        status = str(row.get("forecast_status") or "")
        color = self._status_color(status, theme)
        border = QColor(color)
        border.setAlpha(210)
        painter.setPen(QPen(border, 1.5))
        painter.setBrush(_chart_tooltip_bg(theme))
        painter.drawRoundedRect(rect, 12.0, 12.0)

        painter.setPen(Qt.NoPen)
        painter.setBrush(color)
        painter.drawEllipse(QRectF(x + 13.0, y + 13.0, 10.0, 10.0))
        painter.setPen(_chart_text(theme))
        painter.setFont(QFont("Segoe UI", 9, QFont.Bold))
        title = QFontMetrics(painter.font()).elidedText(
            str(row.get("category") or "Categoria"),
            Qt.ElideRight,
            max(80, int(width - 128)),
        )
        painter.drawText(
            QRectF(x + 30.0, y + 7.0, width - 135.0, 24.0), Qt.AlignVCenter, title
        )
        painter.setPen(color)
        painter.drawText(
            QRectF(x + width - 116.0, y + 7.0, 103.0, 24.0),
            Qt.AlignRight | Qt.AlignVCenter,
            status,
        )

        spent = safe_float(row.get("spent"))
        budget = safe_float(row.get("budget"))
        projected = safe_float(row.get("projected"))
        margin = budget - projected
        previous = safe_float(row.get("previous_spend"))
        if budget > 0:
            lines = [
                f"Realizado: {eur(spent, theme.privacy)} de {eur(budget, theme.privacy)}",
                f"Fim do mês: {eur(projected, theme.privacy)} · margem {eur(margin, theme.privacy)}",
                f"Mês anterior: {eur(previous, theme.privacy)}",
            ]
        else:
            lines = [
                f"Realizado: {eur(spent, theme.privacy)} · sem orçamento definido",
                f"Fim do mês: {eur(projected, theme.privacy)} previsto",
                f"Mês anterior: {eur(previous, theme.privacy)}",
            ]
        painter.setFont(QFont("Segoe UI", 8))
        painter.setPen(_chart_muted(theme))
        for line_index, text in enumerate(lines):
            painter.drawText(
                QRectF(x + 13.0, y + 34.0 + line_index * 20.0, width - 26.0, 18.0),
                Qt.AlignVCenter,
                text,
            )

    def render_chart(self, painter: QPainter) -> None:
        theme = self.theme_provider()
        painter.fillRect(self.rect(), _chart_bg(theme))
        self._hit_rects.clear()
        self._summary_rects.clear()
        if not self.rows:
            self.draw_empty(
                painter,
                (
                    "Sem despesas ativas neste mês"
                    if self.has_budget_rows
                    else "Define orçamentos para comparar categorias"
                ),
            )
            return

        plot = self._plot_rect()
        self._draw_legend(painter, plot, theme)
        scale_max = self._scale_max()
        limit_x = plot.left() + plot.width() * 100.0 / scale_max
        row_height = plot.height() / max(1, len(self.rows))
        bar_height = max(10.0, min(15.0, row_height * 0.30))

        ticks = [0.0, 50.0, 100.0]
        if scale_max >= 150.0:
            ticks.append(150.0)
        if scale_max >= 200.0:
            ticks.append(200.0)
        for tick in sorted(set(ticks)):
            if tick > scale_max:
                continue
            x = plot.left() + plot.width() * tick / scale_max
            pen = QPen(_chart_grid(theme), 1)
            if tick == 100.0:
                pen = QPen(_chart_text(theme), 1.4, Qt.DashLine)
            painter.setPen(pen)
            painter.drawLine(QPointF(x, plot.top() - 4.0), QPointF(x, plot.bottom()))
            painter.setPen(_chart_muted(theme))
            painter.setFont(QFont("Segoe UI", 8))
            painter.drawText(
                QRectF(x - 24.0, plot.top() - 25.0, 48.0, 18.0),
                Qt.AlignCenter,
                f"{tick:.0f}%",
            )

        progress = max(0.0, min(1.0, self.anim_progress))
        for index, row in enumerate(self.rows):
            row_top = plot.top() + index * row_height
            center_y = row_top + row_height / 2.0
            row_rect = QRectF(6.0, row_top, self.width() - 12.0, row_height)
            self._hit_rects.append((index, row_rect))
            category = str(row.get("category") or "")
            focused = self.category_is_highlighted(category)
            active = self.hover_index == index or focused
            dimmed = bool(self.highlighted_category) and not focused
            if active:
                highlight = _chart_grid(theme)
                highlight.setAlpha(88 if focused else 64 if theme.dark else 42)
                focus_border = QColor(theme.primary_color)
                focus_border.setAlpha(170)
                painter.setPen(QPen(focus_border, 1.6) if focused else Qt.NoPen)
                painter.setBrush(highlight)
                painter.drawRoundedRect(
                    row_rect.adjusted(0.0, 2.0, 0.0, -2.0), 8.0, 8.0
                )

            status = str(row.get("forecast_status") or "Controlado")
            label = f"{self._status_icon(status)} {row.get('category', 'Categoria')}"
            label_width = max(92, int(plot.left() - 18.0))
            painter.setFont(QFont("Segoe UI", 8, QFont.DemiBold))
            label = QFontMetrics(painter.font()).elidedText(
                label, Qt.ElideRight, label_width
            )
            label_color = _chart_text(theme)
            if dimmed:
                label_color.setAlpha(84)
            painter.setPen(label_color)
            painter.drawText(
                QRectF(8.0, center_y - 11.0, label_width, 22.0),
                Qt.AlignLeft | Qt.AlignVCenter,
                label,
            )

            track = QRectF(
                plot.left(),
                center_y - bar_height / 2.0 + 5.0,
                plot.width(),
                bar_height,
            )
            painter.setPen(Qt.NoPen)
            painter.setBrush(_chart_grid(theme))
            painter.drawRoundedRect(track, bar_height / 2.0, bar_height / 2.0)

            budget = safe_float(row.get("budget"))
            spent = safe_float(row.get("spent"))
            spent_pct = spent / budget * 100 if budget > 0 else 0.0
            projected_pct = safe_float(row.get("projected_utilisation"))
            color = self._status_color(status, theme)
            if dimmed:
                color.setAlpha(74)

            if budget <= 0:
                alert_width = min(18.0, track.width()) * progress
                painter.setPen(Qt.NoPen)
                painter.setBrush(color)
                painter.drawRoundedRect(
                    QRectF(track.left(), track.top(), alert_width, track.height()),
                    bar_height / 2.0,
                    bar_height / 2.0,
                )
                painter.setFont(QFont("Segoe UI", 8, QFont.DemiBold))
                painter.setPen(color)
                summary_rect = self._row_summary_rect(plot, center_y, limit_x)
                self._summary_rects.append(summary_rect)
                summary = QFontMetrics(painter.font()).elidedText(
                    f"{eur(spent, theme.privacy)} agora · sem orçamento",
                    Qt.ElideRight,
                    max(1, int(summary_rect.width())),
                )
                painter.drawText(
                    summary_rect,
                    Qt.AlignLeft | Qt.AlignVCenter,
                    summary,
                )
                continue

            projected_width = (
                track.width() * min(scale_max, projected_pct * progress) / scale_max
            )
            forecast_color = QColor(color)
            forecast_color.setAlpha(78 if theme.dark else 62)
            projected_rect = QRectF(
                track.left(), track.top(), projected_width, track.height()
            )
            painter.setPen(QPen(color, 1.0, Qt.DashLine))
            painter.setBrush(forecast_color)
            painter.drawRoundedRect(projected_rect, bar_height / 2.0, bar_height / 2.0)

            spent_width = (
                track.width() * min(scale_max, spent_pct * progress) / scale_max
            )
            spent_rect = QRectF(track.left(), track.top(), spent_width, track.height())
            painter.setPen(Qt.NoPen)
            painter.setBrush(color)
            painter.drawRoundedRect(spent_rect, bar_height / 2.0, bar_height / 2.0)

            summary = (
                f"{percent(spent_pct)} agora  →  {percent(projected_pct)} previsto"
            )
            painter.setFont(QFont("Segoe UI", 8, QFont.DemiBold))
            painter.setPen(_chart_muted(theme))
            summary_rect = self._row_summary_rect(plot, center_y, limit_x)
            self._summary_rects.append(summary_rect)
            summary = QFontMetrics(painter.font()).elidedText(
                summary,
                Qt.ElideRight,
                max(1, int(summary_rect.width())),
            )
            painter.drawText(
                summary_rect,
                Qt.AlignLeft | Qt.AlignVCenter,
                summary,
            )

        self._draw_tooltip(painter, theme)
