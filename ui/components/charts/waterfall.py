from __future__ import annotations

from typing import Any, Callable, Optional

from PySide6.QtCore import QEvent, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import QWidget

from ui.components.charts.base import StableChart
from ui.components.charts.utils import (
    _chart_bg,
    _chart_grid,
    _chart_muted,
    _chart_negative,
    _chart_positive,
    _chart_text,
)

from ui.core.theme import Theme
from ui.components.charts.presentation import format_axis_money
from utils.helpers import eur, percent, safe_float


class CashFlowWaterfallChart(StableChart):
    """Interactive waterfall that explains the result of the selected period."""

    filter_clicked = Signal(str, str)

    def __init__(
        self, theme_provider: Callable[[], Theme], parent: Optional[QWidget] = None
    ):
        super().__init__(theme_provider, parent)
        self.income = 0.0
        self.expenses = 0.0
        self.categories: list[tuple[str, float, str]] = []
        self.period_label = ""
        self.previous_income = 0.0
        self.previous_expenses = 0.0
        self.previous_categories: dict[str, float] = {}
        self.comparison_label = ""
        self.items: list[dict[str, Any]] = []
        self.hover_index: Optional[int] = None
        self._hover_pos = QPointF()
        self._bar_hit_rects: list[tuple[int, QRectF]] = []
        self.setMinimumHeight(340)
        self.setAccessibleName("Gráfico interativo do fluxo de dinheiro")
        # This chart already draws a richer in-app hover card.  Keeping a native
        # tooltip as well makes Windows show a second, sometimes black, popup.
        self.setToolTip("")

    def event(self, event) -> bool:  # type: ignore[override]
        if event.type() == QEvent.ToolTip:
            event.accept()
            return True
        return super().event(event)

    def set_data(
        self,
        income: float,
        expenses: float,
        categories: list[tuple[str, float, str]],
        period_label: str = "",
        *,
        previous_income: float = 0.0,
        previous_expenses: float = 0.0,
        previous_categories: Optional[list[tuple[str, float, str]]] = None,
        comparison_label: str = "",
    ) -> None:
        self.income = max(0.0, safe_float(income))
        self.expenses = max(0.0, safe_float(expenses))
        clean: list[tuple[str, float, str]] = []
        for row in categories or []:
            try:
                label, value, color = row
            except (TypeError, ValueError):
                continue
            amount = abs(safe_float(value))
            if amount <= 0:
                continue
            candidate = QColor(str(color or "#94A3B8"))
            clean.append(
                (
                    str(label or "Sem categoria"),
                    amount,
                    candidate.name() if candidate.isValid() else "#94A3B8",
                )
            )
        self.categories = sorted(clean, key=lambda item: item[1], reverse=True)
        self.period_label = str(period_label or "")
        self.previous_income = max(0.0, safe_float(previous_income))
        self.previous_expenses = max(0.0, safe_float(previous_expenses))
        self.previous_categories = {}
        for row in previous_categories or []:
            try:
                label, value, _color = row
            except (TypeError, ValueError):
                continue
            name = str(label or "Sem categoria")
            self.previous_categories[name] = self.previous_categories.get(
                name, 0.0
            ) + abs(safe_float(value))
        self.comparison_label = str(comparison_label or "")
        self.hover_index = None
        self._bar_hit_rects.clear()
        self.items = self._build_items()

        result = self.income - self.expenses
        rate = result / self.income * 100 if self.income else 0.0
        self.setAccessibleDescription(
            f"{self.period_label}. Receitas {eur(self.income, self.theme_provider().privacy)}, "
            f"despesas {eur(self.expenses, self.theme_provider().privacy)} e "
            f"resultado {eur(result, self.theme_provider().privacy)}. "
            f"Taxa de poupança {percent(rate)}."
        )
        if self.items and self.isVisible():
            self.start_entrance_animation(720)
        else:
            self.invalidate_cache()

    def _comparison_amount(self, item: dict[str, Any]) -> Optional[float]:
        if not self.comparison_label:
            return None
        if item["role"] == "income":
            return self.previous_income
        if item["role"] == "result":
            return self.previous_income - self.previous_expenses
        category = str(item.get("category") or "")
        if category:
            return self.previous_categories.get(category, 0.0)
        visible_categories = {
            str(candidate.get("category") or "")
            for candidate in self.items
            if candidate.get("role") == "expense" and candidate.get("category")
        }
        return max(
            0.0,
            self.previous_expenses
            - sum(
                amount
                for name, amount in self.previous_categories.items()
                if name in visible_categories
            ),
        )

    def _category_limit(self) -> int:
        if self.width() >= 850:
            return 5
        if self.width() >= 620:
            return 4
        return 3

    def _build_items(self) -> list[dict[str, Any]]:
        if self.income <= 0 and self.expenses <= 0:
            return []

        items: list[dict[str, Any]] = [
            {
                "label": "Receitas",
                "amount": self.income,
                "start": 0.0,
                "end": self.income,
                "role": "income",
                "kind": "Receitas",
                "category": "",
                "color": "#30D158",
            }
        ]
        running = self.income
        remaining = self.expenses
        for label, raw_amount, color in self.categories[: self._category_limit()]:
            amount = min(raw_amount, remaining)
            if amount <= 0:
                continue
            end = running - amount
            items.append(
                {
                    "label": label,
                    "amount": amount,
                    "start": running,
                    "end": end,
                    "role": "expense",
                    "kind": "Despesas",
                    "category": label,
                    "color": color,
                }
            )
            running = end
            remaining -= amount

        if remaining > 0.005:
            end = running - remaining
            items.append(
                {
                    "label": "Outras despesas",
                    "amount": remaining,
                    "start": running,
                    "end": end,
                    "role": "expense",
                    "kind": "Despesas",
                    "category": "",
                    "color": "#FF9F0A",
                }
            )
            running = end

        result = self.income - self.expenses
        items.append(
            {
                "label": "Resultado",
                "amount": result,
                "start": 0.0,
                "end": result,
                "role": "result",
                "kind": "Todos",
                "category": "",
                "color": "#30D158" if result >= 0 else "#FF453A",
            }
        )
        return items

    def _plot_rect(self) -> QRectF:
        return QRectF(
            72.0, 44.0, max(96.0, self.width() - 92.0), max(90.0, self.height() - 106.0)
        )

    @staticmethod
    def _axis_value(value: float, privacy: bool) -> str:
        return format_axis_money(value, privacy=privacy)

    @staticmethod
    def _bar_color(item: dict[str, Any], theme: Theme) -> QColor:
        if item["role"] == "income":
            return _chart_positive(theme)
        if item["role"] == "result":
            return (
                _chart_positive(theme)
                if item["amount"] >= 0
                else _chart_negative(theme)
            )
        color = QColor(str(item.get("color") or "#FF9F0A"))
        return color if color.isValid() else _chart_negative(theme)

    def _value_range(self, items: list[dict[str, Any]]) -> tuple[float, float]:
        values = [0.0]
        for item in items:
            values.extend((safe_float(item["start"]), safe_float(item["end"])))
            comparison_amount = self._comparison_amount(item)
            if comparison_amount is not None:
                values.append(self._comparison_end(item, comparison_amount))
        low, high = min(values), max(values)
        if abs(high - low) < 0.01:
            high = low + 1.0
        padding = (high - low) * 0.14
        return low - padding, high + padding

    @staticmethod
    def _comparison_end(item: dict[str, Any], amount: float) -> float:
        if item.get("role") == "expense":
            return safe_float(item.get("start")) - max(0.0, safe_float(amount))
        return safe_float(amount)

    def _item_at(self, pos: QPointF) -> Optional[int]:
        for index, rect in self._bar_hit_rects:
            if rect.adjusted(-4.0, -3.0, 4.0, 3.0).contains(pos):
                return index
        return None

    def mouseMoveEvent(self, event):  # type: ignore[override]
        hovered = self._item_at(event.position())
        self._hover_pos = event.position()
        if hovered != self.hover_index:
            self.hover_index = hovered
            self.setCursor(
                Qt.PointingHandCursor if hovered is not None else Qt.ArrowCursor
            )
            category = (
                str(self.items[hovered].get("category") or "")
                if hovered is not None and 0 <= hovered < len(self.items)
                else ""
            )
            self.category_hovered.emit(category)
            self.invalidate_cache()
            self.update()
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
            if index is not None and 0 <= index < len(self.items):
                item = dict(self.items[index])
                if self.open_detail(
                    item,
                    label=str(item.get("category") or item.get("label") or ""),
                    action_label=self._detail_action_label,
                ):
                    self.filter_clicked.emit(
                        str(item["kind"]),
                        str(item["category"]),
                    )
                event.accept()
                return
        super().mousePressEvent(event)

    def _draw_legend(self, painter: QPainter, plot: QRectF, theme: Theme) -> None:
        entries = [
            ("Entrada", _chart_positive(theme)),
            ("Saídas por categoria", _chart_negative(theme)),
            ("Resultado líquido", QColor("#0A84FF")),
        ]
        painter.setFont(QFont("Segoe UI", 8, QFont.DemiBold))
        x = plot.left()
        for label, color in entries:
            painter.setPen(Qt.NoPen)
            painter.setBrush(color)
            painter.drawEllipse(QRectF(x, 12.0, 8.0, 8.0))
            painter.setPen(_chart_muted(theme))
            painter.drawText(
                QRectF(x + 13.0, 7.0, 126.0, 20.0),
                Qt.AlignLeft | Qt.AlignVCenter,
                label,
            )
            x += 142.0

    def _draw_tooltip(self, painter: QPainter, theme: Theme) -> None:
        if self.hover_index is None or not (0 <= self.hover_index < len(self.items)):
            return
        item = self.items[self.hover_index]
        rect = self._tooltip_rect()
        x, y, width = rect.x(), rect.y(), rect.width()
        color = self._bar_color(item, theme)

        painter.setPen(Qt.NoPen)
        shadow = QColor(15, 23, 42, 52)
        painter.setBrush(shadow)
        painter.drawRoundedRect(rect.translated(0.0, 3.0), 12.0, 12.0)
        # Match the fixed movement preview: an opaque light surface avoids the
        # unreadable native/black popup on Windows in either application theme.
        border = QColor(color)
        border.setAlpha(210)
        painter.setPen(QPen(border, 1.5))
        painter.setBrush(QColor("#FFFFFF"))
        painter.drawRoundedRect(rect, 12.0, 12.0)

        painter.setPen(Qt.NoPen)
        painter.setBrush(color)
        painter.drawEllipse(QRectF(x + 13.0, y + 13.0, 10.0, 10.0))
        painter.setPen(QColor("#0F172A"))
        painter.setFont(QFont("Segoe UI", 9, QFont.Bold))

        title = QFontMetrics(painter.font()).elidedText(
            str(item["label"]), Qt.ElideRight, max(60, int(width - 48))
        )
        painter.drawText(
            QRectF(x + 30.0, y + 7.0, width - 42.0, 24.0), Qt.AlignVCenter, title
        )

        value = safe_float(item["amount"])
        if item["role"] == "expense":
            share = value / self.expenses * 100 if self.expenses else 0.0
            detail = (
                f"Saída: {eur(value, theme.privacy)}  ·  {percent(share)} das despesas"
            )
            balance_line = f"Saldo após esta etapa: {eur(item['end'], theme.privacy)}"
        elif item["role"] == "income":
            detail = f"Entrada total: {eur(value, theme.privacy)}"
            balance_line = "Clica para ver todas as receitas"
        else:
            rate = value / self.income * 100 if self.income else 0.0
            detail = f"Resultado: {eur(value, theme.privacy)}  ·  taxa {percent(rate)}"
            balance_line = "Clica para ver os movimentos do período"
        painter.setPen(QColor("#475569"))
        painter.setFont(QFont("Segoe UI", 8))
        painter.drawText(
            QRectF(x + 13.0, y + 34.0, width - 26.0, 18.0), Qt.AlignVCenter, detail
        )
        painter.drawText(
            QRectF(x + 13.0, y + 54.0, width - 26.0, 17.0),
            Qt.AlignVCenter,
            balance_line,
        )
        comparison_amount = self._comparison_amount(item)
        if comparison_amount is not None:
            if abs(comparison_amount) > 0.005:
                delta = (value - comparison_amount) / abs(comparison_amount) * 100.0
                direction = "↑" if delta > 0.5 else "↓" if delta < -0.5 else "＝"
                comparison_text = (
                    f"{self.comparison_label}: "
                    f"{eur(comparison_amount, theme.privacy)} · "
                    f"{direction} {abs(delta):.0f}%"
                )
            elif abs(value) > 0.005:
                comparison_text = f"{self.comparison_label}: sem valor comparável"
            else:
                comparison_text = f"{self.comparison_label}: sem alteração"
            painter.setPen(QColor("#475569"))
            painter.setFont(QFont("Segoe UI", 8, QFont.Bold))
            painter.drawText(
                QRectF(x + 13.0, y + 75.0, width - 26.0, 18.0),
                Qt.AlignVCenter,
                comparison_text,
            )

    def _tooltip_rect(self) -> QRectF:
        width = min(258.0, max(188.0, self.width() - 18.0))
        height = 102.0 if self.comparison_label else 78.0
        x = max(8.0, min(self._hover_pos.x() + 14.0, self.width() - width - 8.0))
        y = self._hover_pos.y() - height - 12.0
        if y < 8.0:
            y = self._hover_pos.y() + 14.0
        y = max(8.0, min(y, self.height() - height - 8.0))
        return QRectF(x, y, width, height)

    def render_chart(self, painter: QPainter) -> None:
        theme = self.theme_provider()
        painter.fillRect(self.rect(), _chart_bg(theme))
        self.items = self._build_items()
        self._bar_hit_rects.clear()
        if not self.items:
            self.draw_empty(painter, "Sem movimentos neste período")
            return

        plot = self._plot_rect()
        self._draw_legend(painter, plot, theme)
        low, high = self._value_range(self.items)

        def y_for(value: float) -> float:
            return plot.bottom() - plot.height() * (value - low) / max(1e-9, high - low)

        zero_y = y_for(0.0)
        grid = _chart_grid(theme)
        painter.setPen(QPen(grid, 1))
        for index in range(5):
            grid_value = low + (high - low) * index / 4.0
            grid_y = y_for(grid_value)
            painter.drawLine(
                QPointF(plot.left(), grid_y), QPointF(plot.right(), grid_y)
            )

        zero_pen = QPen(_chart_muted(theme), 1.4)
        zero_pen.setStyle(Qt.DashLine)
        painter.setPen(zero_pen)
        painter.drawLine(QPointF(plot.left(), zero_y), QPointF(plot.right(), zero_y))

        count = len(self.items)
        step_width = plot.width() / max(1, count)
        bar_width = max(22.0, min(76.0, step_width * 0.58))
        progress = max(0.0, min(1.0, self.anim_progress))

        for index, item in enumerate(self.items):
            center_x = plot.left() + step_width * (index + 0.5)
            start = safe_float(item["start"])
            end = safe_float(item["end"])
            animated_end = start + (end - start) * progress
            top = min(y_for(start), y_for(animated_end))
            bottom = max(y_for(start), y_for(animated_end))
            if bottom - top < 3.0:
                top -= 1.5
                bottom += 1.5
            rect = QRectF(center_x - bar_width / 2.0, top, bar_width, bottom - top)
            self._bar_hit_rects.append((index, rect))

            color = self._bar_color(item, theme)
            category = str(item.get("category") or "")
            focused = bool(category) and self.category_is_highlighted(category)
            active = self.hover_index == index or focused
            dimmed = (
                bool(self.highlighted_category)
                and item.get("role") == "expense"
                and not focused
            )
            comparison_amount = self._comparison_amount(item)
            if comparison_amount is not None and abs(comparison_amount) > 0.005:
                comparison_start = (
                    safe_float(item["start"]) if item.get("role") == "expense" else 0.0
                )
                comparison_end = self._comparison_end(item, comparison_amount)
                comparison_top = min(
                    y_for(comparison_start),
                    y_for(comparison_end),
                )
                comparison_bottom = max(
                    y_for(comparison_start),
                    y_for(comparison_end),
                )
                comparison_rect = QRectF(
                    center_x - bar_width / 2.0 - 3.0,
                    comparison_top,
                    bar_width + 6.0,
                    max(3.0, comparison_bottom - comparison_top),
                )
                comparison_pen = QColor(theme.muted)
                comparison_pen.setAlpha(210 if focused else 145)
                painter.setPen(QPen(comparison_pen, 2.0, Qt.DashLine))
                painter.setBrush(Qt.NoBrush)
                painter.drawRoundedRect(comparison_rect, 8.0, 8.0)
            if active:
                color = color.lighter(118)
                rect = rect.adjusted(-2.0, -2.0, 2.0, 2.0)
            fill = QColor(color)
            fill.setAlpha(58 if dimmed else 226 if theme.dark else 216)
            border = color.lighter(118)
            if dimmed:
                border.setAlpha(74)
            painter.setPen(QPen(border, 3.0 if focused else 1.0))
            painter.setBrush(fill)
            # BUG-CHART-WF-02: the corner radius must never exceed half the
            # bar height, otherwise a 0.00 € sliver (3px tall) degenerates the
            # vector path into a bow-tie artifact.
            corner_radius = min(9.0, rect.width() / 4.0, rect.height() / 2.0)
            painter.drawRoundedRect(rect, corner_radius, corner_radius)

            value = safe_float(item["amount"])
            prefix = "−" if item["role"] == "expense" else ""
            value_text = prefix + eur(abs(value), theme.privacy)
            painter.setFont(QFont("Segoe UI", 8, QFont.Bold))
            painter.setPen(_chart_text(theme))
            value_width = min(step_width - 4.0, 118.0)
            value_y = top - 22.0 if top > plot.top() + 22.0 else bottom + 4.0
            painter.drawText(
                QRectF(center_x - value_width / 2.0, value_y, value_width, 18.0),
                Qt.AlignHCenter | Qt.AlignVCenter,
                QFontMetrics(painter.font()).elidedText(
                    value_text, Qt.ElideRight, max(12, int(value_width))
                ),
            )

            label_width = max(20.0, step_width - 5.0)
            painter.setFont(QFont("Segoe UI", 8, QFont.DemiBold))
            painter.setPen(_chart_muted(theme))
            label = QFontMetrics(painter.font()).elidedText(
                str(item["label"]), Qt.ElideRight, max(16, int(label_width))
            )
            painter.drawText(
                QRectF(
                    center_x - label_width / 2.0,
                    plot.bottom() + 12.0,
                    label_width,
                    35.0,
                ),
                Qt.AlignHCenter | Qt.AlignTop | Qt.TextWordWrap,
                label,
            )

            if index < count - 1:
                next_item = self.items[index + 1]
                target_value = (
                    safe_float(next_item["end"])
                    if next_item["role"] == "result"
                    else safe_float(next_item["start"])
                )
                # BUG-CHART-WF-01: the connector must follow the animated bar
                # height (not the static end value) and start at the same edge
                # the bar is painted at, otherwise hover expansion (+2px) and
                # the 720ms entrance animation leave the line floating.
                connector_y = y_for(animated_end)
                target_y = y_for(target_value)
                half_width = bar_width / 2.0 + (2.0 if active else 0.0)
                connector = QColor(theme.muted)
                connector.setAlpha(118 if theme.dark else 92)
                painter.setPen(QPen(connector, 1.0, Qt.DashLine))
                painter.drawLine(
                    QPointF(center_x + half_width, connector_y),
                    QPointF(center_x + step_width - bar_width / 2.0, target_y),
                )

        painter.setFont(QFont("Segoe UI", 8))
        painter.setPen(_chart_muted(theme))
        painter.drawText(
            QRectF(3.0, plot.top() - 8.0, 64.0, 18.0),
            Qt.AlignRight | Qt.AlignVCenter,
            self._axis_value(high, theme.privacy),
        )
        painter.drawText(
            QRectF(3.0, plot.bottom() - 9.0, 64.0, 18.0),
            Qt.AlignRight | Qt.AlignVCenter,
            self._axis_value(low, theme.privacy),
        )
        self._draw_tooltip(painter, theme)
