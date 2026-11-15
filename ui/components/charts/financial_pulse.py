"""Interactive charts for cash-flow momentum and monthly savings efficiency."""

from __future__ import annotations

import math
from datetime import date
from typing import Any, Callable, Optional

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
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
from utils.helpers import eur, safe_float


_MONTHS = (
    "jan",
    "fev",
    "mar",
    "abr",
    "mai",
    "jun",
    "jul",
    "ago",
    "set",
    "out",
    "nov",
    "dez",
)


def _compact_eur(value: float, privacy: bool) -> str:
    return format_axis_money(value, privacy=privacy)


def _safe_date(value: Any) -> Optional[date]:
    try:
        return date.fromisoformat(str(value or "")[:10])
    except (TypeError, ValueError):
        return None


def _period_label(row: dict[str, Any]) -> str:
    current = _safe_date(row.get("date"))
    if current is None:
        return "Data inválida"
    resolution = str(row.get("resolution") or "daily")
    if resolution == "monthly":
        return f"{_MONTHS[current.month - 1].capitalize()} {current.year}"
    if resolution == "weekly":
        return f"Semana {current.isocalendar()[1]} · {current.year}"
    return f"{current.day} {_MONTHS[current.month - 1]} {current.year}"


class CumulativeCashflowChart(StableChart):
    """Compare cumulative income and expenses inside the selected period."""

    def __init__(
        self,
        theme_provider: Callable[[], Theme],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(theme_provider, parent)
        self.rows: list[dict[str, Any]] = []
        self.comparison_rows: list[dict[str, Any]] = []
        self.comparison_label = ""
        self.hover_index: Optional[int] = None
        self._point_positions: list[tuple[QPointF, QPointF]] = []
        self.setMinimumHeight(340)
        self.setAccessibleName("Fluxo acumulado de receitas e despesas")
        self.setAccessibleDescription(
            "Compara entradas e saídas acumuladas no período selecionado."
        )

    @staticmethod
    def _normalise_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        clean: list[dict[str, Any]] = []
        income_total = 0.0
        expense_total = 0.0
        for row in rows or []:
            current = _safe_date(row.get("date"))
            if current is None:
                continue
            income = max(0.0, safe_float(row.get("income")))
            expenses = max(
                0.0,
                safe_float(row.get("expenses", row.get("expense"))),
            )
            income_total += income
            expense_total += expenses
            clean.append(
                {
                    "date": current.isoformat(),
                    "resolution": str(row.get("resolution") or "daily"),
                    "income": income,
                    "expenses": expenses,
                    "cumulative_income": income_total,
                    "cumulative_expenses": expense_total,
                    "net": income_total - expense_total,
                }
            )
        return clean

    def set_rows(self, rows: list[dict[str, Any]]) -> None:
        self.clear_period_hover()
        clean = self._normalise_rows(rows)
        self.rows = clean
        self.hover_index = None
        self._point_positions = []
        theme = self.theme_provider()
        if clean:
            final = clean[-1]
            self.setAccessibleDescription(
                f"No fim do período, entradas acumuladas "
                f"{eur(final['cumulative_income'], theme.privacy)}, saídas "
                f"{eur(final['cumulative_expenses'], theme.privacy)} e margem "
                f"{eur(final['net'], theme.privacy)}."
            )
        else:
            self.setAccessibleDescription(
                "Sem movimentos financeiros no período selecionado."
            )
        self.invalidate_cache()

    def set_comparison_rows(
        self,
        rows: list[dict[str, Any]],
        label: str = "",
    ) -> None:
        self.comparison_rows = self._normalise_rows(rows)
        self.comparison_label = str(label or "período de referência")
        self.invalidate_cache()

    def _plot_rect(self) -> tuple[float, float, float, float]:
        left = 68.0 if self.width() >= 500 else 56.0
        return (
            left,
            52.0,
            max(left + 80.0, self.width() - 42.0),
            max(
                120.0,
                self.height() - 48.0,
            ),
        )

    def _point(
        self,
        index: int,
        value: float,
        maximum: float,
        row_count: Optional[int] = None,
    ) -> QPointF:
        left, top, right, bottom = self._plot_rect()
        count = len(self.rows) if row_count is None else max(0, int(row_count))
        x = left + (right - left) * index / max(1, count - 1)
        y = bottom - (bottom - top) * value / max(1.0, maximum)
        return QPointF(x, y)

    def _comparison_for_index(self, index: int) -> Optional[dict[str, Any]]:
        if not self.comparison_rows or not self.rows:
            return None
        mapped = round(
            index * (len(self.comparison_rows) - 1) / max(1, len(self.rows) - 1)
        )
        return self.comparison_rows[max(0, min(len(self.comparison_rows) - 1, mapped))]

    def mouseMoveEvent(self, event):  # type: ignore[override]
        if not self.rows:
            super().mouseMoveEvent(event)
            return
        left, top, right, bottom = self._plot_rect()
        position = event.position()
        hovered = None
        if left <= position.x() <= right and top <= position.y() <= bottom:
            ratio = (position.x() - left) / max(1.0, right - left)
            hovered = max(
                0,
                min(len(self.rows) - 1, round(ratio * (len(self.rows) - 1))),
            )
        if hovered != self.hover_index:
            self.hover_index = hovered
            self.emit_period_for_hover(hovered)
            self.setCursor(Qt.CrossCursor if hovered is not None else Qt.ArrowCursor)
            self.invalidate_cache()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):  # type: ignore[override]
        self.hover_index = None
        self.emit_period_for_hover(None)
        self.setCursor(Qt.ArrowCursor)
        self.invalidate_cache()
        super().leaveEvent(event)

    def _draw_area(
        self,
        painter: QPainter,
        points: list[QPointF],
        color: QColor,
        bottom: float,
    ) -> None:
        if not points:
            return
        path = QPainterPath(points[0])
        for point in points[1:]:
            path.lineTo(point)
        area = QPainterPath(path)
        area.lineTo(points[-1].x(), bottom)
        area.lineTo(points[0].x(), bottom)
        area.closeSubpath()
        # BUG-CHART-PULSE-01: a flat series (top == bottom) would create a
        # QLinearGradient with coincident start/end points (invalid); clamp
        # the span to a minimum non-zero length.
        gradient_top = points[0].y()
        gradient_span = bottom - gradient_top
        if gradient_span < 1.0:
            gradient_top = bottom - 1.0
            gradient_span = 1.0
        fill = QLinearGradient(0, gradient_top, 0, bottom)
        top_color = QColor(color)
        top_color.setAlpha(52)
        bottom_color = QColor(color)
        bottom_color.setAlpha(3)
        fill.setColorAt(0.0, top_color)
        fill.setColorAt(1.0, bottom_color)
        painter.fillPath(area, fill)
        pen = QPen(color, 2.6)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawPath(path)

    @staticmethod
    def _draw_reference_line(
        painter: QPainter,
        points: list[QPointF],
        color: QColor,
    ) -> None:
        if not points:
            return
        reference = QColor(color)
        reference.setAlpha(168)
        # BUG-CHART-PULSE-01: with a single comparison point the dashed path
        # collapses to an empty segment and nothing is drawn; render an
        # explicit dot so the reference stays visible.
        if len(points) == 1:
            painter.setPen(Qt.NoPen)
            painter.setBrush(reference)
            painter.drawEllipse(points[0], 3.2, 3.2)
            return
        path = QPainterPath(points[0])
        for point in points[1:]:
            path.lineTo(point)
        pen = QPen(reference, 1.8, Qt.DashLine)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawPath(path)

    def _draw_tooltip(
        self,
        painter: QPainter,
        anchor: QPointF,
        row: dict[str, Any],
        reference: Optional[dict[str, Any]] = None,
    ) -> None:
        theme = self.theme_provider()
        width = min(
            340.0 if reference else 288.0,
            max(210.0, self.width() - 20.0),
        )
        height = 176.0 if reference else 134.0
        x = min(max(8.0, anchor.x() + 12.0), self.width() - width - 8.0)
        y = min(
            max(8.0, anchor.y() - height - 12.0),
            self.height() - height - 8.0,
        )
        rect = QRectF(x, y, width, height)
        shadow = QColor(0, 0, 0, 38 if theme.dark else 22)
        painter.setPen(Qt.NoPen)
        painter.setBrush(shadow)
        painter.drawRoundedRect(rect.translated(0, 4), 13, 13)
        painter.setPen(QPen(_chart_grid(theme), 1))
        painter.setBrush(_chart_tooltip_bg(theme))
        painter.drawRoundedRect(rect, 13, 13)

        painter.setPen(_chart_text(theme))
        painter.setFont(QFont("Segoe UI", 9, QFont.Bold))
        painter.drawText(
            QRectF(x + 13, y + 8, width - 26, 22),
            Qt.AlignLeft | Qt.AlignVCenter,
            _period_label(row),
        )
        lines = (
            ("Entradas acumuladas", row["cumulative_income"], _chart_positive(theme)),
            ("Saídas acumuladas", row["cumulative_expenses"], _chart_negative(theme)),
            (
                "Margem acumulada",
                row["net"],
                _chart_positive(theme)
                if safe_float(row["net"]) >= 0
                else _chart_negative(theme),
            ),
        )
        for index, (label, value, color) in enumerate(lines):
            line_y = y + 38 + index * 27
            painter.setPen(_chart_muted(theme))
            painter.setFont(QFont("Segoe UI", 8))
            painter.drawText(
                QRectF(x + 13, line_y, width * 0.58, 20),
                Qt.AlignLeft | Qt.AlignVCenter,
                label,
            )
            painter.setPen(color)
            painter.setFont(QFont("Segoe UI", 8, QFont.Bold))
            painter.drawText(
                QRectF(x + width * 0.54, line_y, width * 0.41 - 13, 20),
                Qt.AlignRight | Qt.AlignVCenter,
                eur(value, theme.privacy),
            )
        if reference:
            painter.setPen(_chart_muted(theme))
            painter.setFont(QFont("Segoe UI", 8, QFont.Bold))
            painter.drawText(
                QRectF(x + 13, y + 120, width - 26, 18),
                Qt.AlignLeft | Qt.AlignVCenter,
                f"Referência · {self.comparison_label}",
            )
            painter.setFont(QFont("Segoe UI", 8))
            painter.drawText(
                QRectF(x + 13, y + 140, width - 26, 20),
                Qt.AlignLeft | Qt.AlignVCenter,
                "Entradas "
                f"{eur(reference['cumulative_income'], theme.privacy)} · "
                f"Saídas {eur(reference['cumulative_expenses'], theme.privacy)} · "
                f"Margem {eur(reference['net'], theme.privacy)}",
            )

    def render_chart(self, painter: QPainter) -> None:
        theme = self.theme_provider()
        painter.fillRect(self.rect(), _chart_bg(theme))
        self._point_positions = []
        if not self.rows:
            self.draw_empty(painter, "Sem movimentos no período selecionado")
            return

        left, top, right, bottom = self._plot_rect()
        maximum = (
            max(
                max(
                    safe_float(row["cumulative_income"]),
                    safe_float(row["cumulative_expenses"]),
                )
                for row in self.rows
            )
            * 1.08
        )
        if self.comparison_rows:
            maximum = max(
                maximum,
                max(
                    max(
                        safe_float(row["cumulative_income"]),
                        safe_float(row["cumulative_expenses"]),
                    )
                    for row in self.comparison_rows
                )
                * 1.08,
            )
        maximum = max(1.0, maximum)
        painter.setFont(QFont("Segoe UI", 8))
        for index in range(5):
            ratio = index / 4
            y = top + (bottom - top) * ratio
            painter.setPen(QPen(_chart_grid(theme), 1))
            painter.drawLine(QPointF(left, y), QPointF(right, y))
            painter.setPen(_chart_muted(theme))
            painter.drawText(
                QRectF(2, y - 10, left - 8, 20),
                Qt.AlignRight | Qt.AlignVCenter,
                _compact_eur(maximum * (1.0 - ratio), theme.privacy),
            )

        income_points = [
            self._point(index, safe_float(row["cumulative_income"]), maximum)
            for index, row in enumerate(self.rows)
        ]
        expense_points = [
            self._point(index, safe_float(row["cumulative_expenses"]), maximum)
            for index, row in enumerate(self.rows)
        ]
        comparison_income_points = [
            self._point(
                index,
                safe_float(row["cumulative_income"]),
                maximum,
                len(self.comparison_rows),
            )
            for index, row in enumerate(self.comparison_rows)
        ]
        comparison_expense_points = [
            self._point(
                index,
                safe_float(row["cumulative_expenses"]),
                maximum,
                len(self.comparison_rows),
            )
            for index, row in enumerate(self.comparison_rows)
        ]
        self._point_positions = list(zip(income_points, expense_points))
        self._draw_reference_line(
            painter,
            comparison_income_points,
            _chart_positive(theme),
        )
        self._draw_reference_line(
            painter,
            comparison_expense_points,
            _chart_negative(theme),
        )
        self._draw_area(painter, expense_points, _chart_negative(theme), bottom)
        self._draw_area(painter, income_points, _chart_positive(theme), bottom)

        painter.setFont(QFont("Segoe UI", 8, QFont.Bold))
        legend = (
            ("● Entradas", _chart_positive(theme)),
            ("● Saídas", _chart_negative(theme)),
            *((("┄ Referência", _chart_muted(theme)),) if self.comparison_rows else ()),
        )
        legend_x = left
        for label, color in legend:
            painter.setPen(color)
            painter.drawText(
                QRectF(legend_x, 17, 94, 22),
                Qt.AlignLeft | Qt.AlignVCenter,
                label,
            )
            legend_x += 104

        label_count = min(5, len(self.rows))
        label_indices = {
            round(index * (len(self.rows) - 1) / max(1, label_count - 1))
            for index in range(label_count)
        }
        painter.setPen(_chart_muted(theme))
        painter.setFont(QFont("Segoe UI", 8))
        for index in sorted(label_indices):
            point = income_points[index]
            painter.drawText(
                QRectF(point.x() - 36, bottom + 8, 72, 22),
                Qt.AlignHCenter | Qt.AlignTop,
                _period_label(self.rows[index]).split(" · ")[0],
            )

        if self.hover_index is not None:
            index = max(0, min(len(self.rows) - 1, self.hover_index))
            income_point, expense_point = self._point_positions[index]
            painter.setPen(QPen(_chart_accent(theme), 1.3, Qt.DashLine))
            painter.drawLine(
                QPointF(income_point.x(), top),
                QPointF(income_point.x(), bottom),
            )
            for point, color in (
                (income_point, _chart_positive(theme)),
                (expense_point, _chart_negative(theme)),
            ):
                painter.setPen(QPen(_chart_tooltip_bg(theme), 2))
                painter.setBrush(color)
                painter.drawEllipse(point, 5, 5)
            self._draw_tooltip(
                painter,
                income_point if income_point.y() < expense_point.y() else expense_point,
                self.rows[index],
                self._comparison_for_index(index),
            )


class SavingsEfficiencyChart(StableChart):
    """Show monthly monetary margin together with the savings-rate line."""

    TARGET_RATE = 20.0

    def __init__(
        self,
        theme_provider: Callable[[], Theme],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(theme_provider, parent)
        self.rows: list[dict[str, Any]] = []
        self.comparison_rows: list[dict[str, Any]] = []
        self.comparison_label = ""
        self.hover_index: Optional[int] = None
        self._bar_rects: list[QRectF] = []
        self._rate_points: list[QPointF] = []
        self._comparison_rate_points: list[QPointF] = []
        self.setMinimumHeight(340)
        self.setAccessibleName("Margem mensal e taxa de poupança")

    @staticmethod
    def _normalise_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        clean: list[dict[str, Any]] = []
        for row in rows or []:
            income = max(0.0, safe_float(row.get("income")))
            expenses = max(
                0.0,
                safe_float(row.get("expenses", row.get("expense"))),
            )
            savings = safe_float(row.get("savings"), income - expenses)
            rate = safe_float(
                row.get("savings_rate"),
                savings / income * 100.0 if income else 0.0,
            )
            clean.append(
                {
                    "month": str(row.get("month") or ""),
                    "label": str(row.get("label") or row.get("month") or ""),
                    "income": income,
                    "expenses": expenses,
                    "savings": savings,
                    "savings_rate": rate,
                    "partial": bool(row.get("partial")),
                    "movement_count": max(
                        0,
                        int(safe_float(row.get("movement_count", 0))),
                    ),
                }
            )
        return clean

    def set_rows(self, rows: list[dict[str, Any]]) -> None:
        self.clear_period_hover()
        clean = self._normalise_rows(rows)
        self.rows = clean
        self.hover_index = None
        self._bar_rects = []
        self._rate_points = []
        self._comparison_rate_points = []
        if clean:
            with_income = [row for row in clean if safe_float(row["income"]) > 0]
            if with_income:
                average_rate = sum(
                    safe_float(row["savings_rate"]) for row in with_income
                ) / len(with_income)
                income_period_label = (
                    "1 mês com receita"
                    if len(with_income) == 1
                    else f"{len(with_income)} meses com receita"
                )
                self.setAccessibleDescription(
                    f"Taxa média de poupança de {average_rate:.1f}% em "
                    f"{income_period_label}; "
                    f"{len(clean)} blocos apresentados."
                )
            else:
                self.setAccessibleDescription(
                    f"{len(clean)} blocos apresentados, sem meses com receita."
                )
        else:
            self.setAccessibleDescription(
                "Sem movimentos financeiros no período selecionado."
            )
        self.invalidate_cache()

    def set_comparison_rows(
        self,
        rows: list[dict[str, Any]],
        label: str = "",
    ) -> None:
        self.comparison_rows = self._normalise_rows(rows)
        self.comparison_label = str(label or "período de referência")
        self._comparison_rate_points = []
        self.invalidate_cache()

    def _comparison_for_index(self, index: int) -> Optional[dict[str, Any]]:
        if not self.comparison_rows or not self.rows:
            return None
        mapped = round(
            index * (len(self.comparison_rows) - 1) / max(1, len(self.rows) - 1)
        )
        return self.comparison_rows[max(0, min(len(self.comparison_rows) - 1, mapped))]

    def _plot_rect(self) -> tuple[float, float, float, float]:
        return (
            58.0,
            72.0 if self.comparison_rows else 56.0,
            max(150.0, self.width() - 48.0),
            max(
                130.0,
                self.height() - 48.0,
            ),
        )

    def mouseMoveEvent(self, event):  # type: ignore[override]
        position = event.position()
        hovered = next(
            (
                index
                for index, rect in enumerate(self._bar_rects)
                if rect.adjusted(-5, -8, 5, 8).contains(position)
            ),
            None,
        )
        if hovered is None and self.rows:
            left, top, right, bottom = self._plot_rect()
            if left <= position.x() <= right and top <= position.y() <= bottom:
                ratio = (position.x() - left) / max(1.0, right - left)
                hovered = max(
                    0,
                    min(len(self.rows) - 1, round(ratio * (len(self.rows) - 1))),
                )
        if hovered != self.hover_index:
            self.hover_index = hovered
            self.emit_period_for_hover(hovered)
            self.setCursor(Qt.CrossCursor if hovered is not None else Qt.ArrowCursor)
            self.invalidate_cache()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):  # type: ignore[override]
        self.hover_index = None
        self.emit_period_for_hover(None)
        self.setCursor(Qt.ArrowCursor)
        self.invalidate_cache()
        super().leaveEvent(event)

    def _draw_tooltip(
        self,
        painter: QPainter,
        anchor: QPointF,
        row: dict[str, Any],
        reference: Optional[dict[str, Any]] = None,
    ) -> None:
        theme = self.theme_provider()
        width = min(278.0, max(212.0, self.width() - 18.0))
        height = 176.0 if reference else 150.0
        x = min(max(8.0, anchor.x() + 12.0), self.width() - width - 8.0)
        y = min(
            max(8.0, anchor.y() - height - 12.0),
            self.height() - height - 8.0,
        )
        rect = QRectF(x, y, width, height)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(0, 0, 0, 36 if theme.dark else 22))
        painter.drawRoundedRect(rect.translated(0, 4), 13, 13)
        painter.setPen(QPen(_chart_grid(theme), 1))
        painter.setBrush(_chart_tooltip_bg(theme))
        painter.drawRoundedRect(rect, 13, 13)

        title = str(row.get("label") or row.get("month") or "Mês")
        if row.get("partial"):
            title += " · em curso"
        painter.setPen(_chart_text(theme))
        painter.setFont(QFont("Segoe UI", 9, QFont.Bold))
        painter.drawText(
            QRectF(x + 13, y + 8, width - 26, 22),
            Qt.AlignLeft | Qt.AlignVCenter,
            title,
        )
        entries = (
            ("Receitas", eur(row["income"], theme.privacy), _chart_positive(theme)),
            ("Despesas", eur(row["expenses"], theme.privacy), _chart_negative(theme)),
            (
                "Margem",
                eur(row["savings"], theme.privacy),
                _chart_positive(theme)
                if safe_float(row["savings"]) >= 0
                else _chart_negative(theme),
            ),
            (
                "Taxa de poupança",
                f"{safe_float(row['savings_rate']):.1f}%".replace(".", ","),
                _chart_accent(theme),
            ),
        )
        for index, (label, value, color) in enumerate(entries):
            line_y = y + 38 + index * 26
            painter.setPen(_chart_muted(theme))
            painter.setFont(QFont("Segoe UI", 8))
            painter.drawText(
                QRectF(x + 13, line_y, width * 0.58, 20),
                Qt.AlignLeft | Qt.AlignVCenter,
                label,
            )
            painter.setPen(color)
            painter.setFont(QFont("Segoe UI", 8, QFont.Bold))
            painter.drawText(
                QRectF(x + width * 0.53, line_y, width * 0.42 - 13, 20),
                Qt.AlignRight | Qt.AlignVCenter,
                value,
            )
        if reference:
            reference_rate = f"{safe_float(reference['savings_rate']):.1f}%".replace(
                ".",
                ",",
            )
            painter.setPen(_chart_muted(theme))
            painter.setFont(QFont("Segoe UI", 8, QFont.Bold))
            painter.drawText(
                QRectF(x + 13, y + 144, width - 26, 20),
                Qt.AlignLeft | Qt.AlignVCenter,
                f"Ref. {self.comparison_label}: "
                f"{eur(reference['savings'], theme.privacy)} · {reference_rate}",
            )

    def render_chart(self, painter: QPainter) -> None:
        theme = self.theme_provider()
        painter.fillRect(self.rect(), _chart_bg(theme))
        self._bar_rects = []
        self._rate_points = []
        self._comparison_rate_points = []
        if not self.rows:
            self.draw_empty(painter, "Sem meses suficientes para medir a poupança")
            return

        left, top, right, bottom = self._plot_rect()
        savings_values = [
            safe_float(row["savings"]) for row in (*self.rows, *self.comparison_rows)
        ]
        maximum_savings = max(0.0, max(savings_values))
        minimum_savings = min(0.0, min(savings_values))
        span = maximum_savings - minimum_savings
        if math.isclose(span, 0.0):
            maximum_savings = max(1.0, maximum_savings + 1.0)
            minimum_savings = min(0.0, minimum_savings - 1.0)
            span = maximum_savings - minimum_savings
        padding = span * 0.12
        maximum_savings += padding
        minimum_savings -= padding
        span = maximum_savings - minimum_savings

        rate_values = [
            max(-100.0, min(100.0, safe_float(row["savings_rate"])))
            for row in (*self.rows, *self.comparison_rows)
        ]
        rate_minimum = min(-20.0, min(rate_values))
        rate_maximum = max(40.0, max(rate_values), self.TARGET_RATE)
        rate_padding = max(5.0, (rate_maximum - rate_minimum) * 0.08)
        rate_minimum -= rate_padding
        rate_maximum += rate_padding

        def savings_y(value: float) -> float:
            return bottom - (value - minimum_savings) / span * (bottom - top)

        def rate_y(value: float) -> float:
            clipped = max(-100.0, min(100.0, value))
            return bottom - (clipped - rate_minimum) / max(
                1.0,
                rate_maximum - rate_minimum,
            ) * (bottom - top)

        painter.setFont(QFont("Segoe UI", 8))
        for index in range(5):
            ratio = index / 4
            y = top + (bottom - top) * ratio
            painter.setPen(QPen(_chart_grid(theme), 1))
            painter.drawLine(QPointF(left, y), QPointF(right, y))
            value = maximum_savings - span * ratio
            painter.setPen(_chart_muted(theme))
            painter.drawText(
                QRectF(1, y - 10, left - 7, 20),
                Qt.AlignRight | Qt.AlignVCenter,
                _compact_eur(value, theme.privacy),
            )

        zero_y = savings_y(0.0)
        painter.setPen(QPen(_chart_grid(theme), 1.5))
        painter.drawLine(QPointF(left, zero_y), QPointF(right, zero_y))

        slot = (right - left) / max(1, len(self.rows))
        bar_width = max(2.0, min(38.0, slot * 0.58))
        # A label needs about 60 px once the year is included. Keeping that
        # spacing also avoids adjacent sampled indices after rounding.
        label_count = min(
            len(self.rows),
            max(1, int(max(1.0, right - left) // 60)),
        )
        label_indices = {
            round(index * (len(self.rows) - 1) / max(1, label_count - 1))
            for index in range(label_count)
        }
        years = {
            str(row.get("month") or "")[:4]
            for row in self.rows
            if len(str(row.get("month") or "")) >= 4
        }
        show_year = len(years) > 1
        for index, row in enumerate(self.rows):
            center_x = left + slot * (index + 0.5)
            reference = self._comparison_for_index(index)
            if reference is not None:
                reference_y = savings_y(safe_float(reference["savings"]))
                reference_rect = QRectF(
                    center_x - (bar_width + 5.0) / 2,
                    min(zero_y, reference_y),
                    bar_width + 5.0,
                    max(2.0, abs(zero_y - reference_y)),
                )
                reference_pen = QPen(_chart_muted(theme), 1.4, Qt.DashLine)
                painter.setPen(reference_pen)
                painter.setBrush(Qt.NoBrush)
                painter.drawRoundedRect(reference_rect, 6, 6)
            value_y = savings_y(safe_float(row["savings"]))
            rect = QRectF(
                center_x - bar_width / 2,
                min(zero_y, value_y),
                bar_width,
                max(2.0, abs(zero_y - value_y)),
            )
            self._bar_rects.append(rect)
            color = (
                _chart_positive(theme)
                if safe_float(row["savings"]) >= 0
                else _chart_negative(theme)
            )
            fill = QColor(color)
            fill.setAlpha(210 if index == self.hover_index else 160)
            painter.setPen(QPen(color, 2 if index == self.hover_index else 1))
            painter.setBrush(fill)
            painter.drawRoundedRect(rect, 6, 6)

            if index in label_indices:
                month_key = str(row.get("month") or "")
                axis_label = str(row.get("label") or month_key)[:3]
                if show_year and len(month_key) >= 4:
                    axis_label += f" {month_key[2:4]}"
                painter.setPen(_chart_muted(theme))
                painter.setFont(QFont("Segoe UI", 8))
                painter.drawText(
                    QRectF(
                        center_x - max(24.0, slot / 2), bottom + 7, max(48.0, slot), 22
                    ),
                    Qt.AlignHCenter | Qt.AlignTop,
                    axis_label,
                )

        target_y = rate_y(self.TARGET_RATE)
        target_pen = QPen(QColor("#FF9F0A"), 1.5, Qt.DashLine)
        painter.setPen(target_pen)
        painter.drawLine(QPointF(left, target_y), QPointF(right, target_y))

        comparison_rate_path = QPainterPath()
        for index, row in enumerate(self.rows):
            reference = self._comparison_for_index(index)
            if reference is None:
                continue
            center_x = left + slot * (index + 0.5)
            point = QPointF(
                center_x,
                rate_y(safe_float(reference["savings_rate"])),
            )
            self._comparison_rate_points.append(point)
            if len(self._comparison_rate_points) == 1:
                comparison_rate_path.moveTo(point)
            else:
                comparison_rate_path.lineTo(point)
        if self._comparison_rate_points:
            comparison_rate_pen = QPen(_chart_muted(theme), 1.8, Qt.DashLine)
            comparison_rate_pen.setCapStyle(Qt.RoundCap)
            comparison_rate_pen.setJoinStyle(Qt.RoundJoin)
            painter.setPen(comparison_rate_pen)
            painter.setBrush(Qt.NoBrush)
            painter.drawPath(comparison_rate_path)
            # BUG-AUD-C140 hardening: a single comparison point collapses the
            # dashed path to an empty segment; draw an explicit dot so the
            # reference stays visible (same treatment as CHART-PULSE-01).
            if len(self._comparison_rate_points) == 1:
                painter.setPen(Qt.NoPen)
                painter.setBrush(_chart_muted(theme))
                painter.drawEllipse(self._comparison_rate_points[0], 3.2, 3.2)

        rate_path = QPainterPath()
        for index, row in enumerate(self.rows):
            center_x = left + slot * (index + 0.5)
            point = QPointF(center_x, rate_y(safe_float(row["savings_rate"])))
            self._rate_points.append(point)
            if index == 0:
                rate_path.moveTo(point)
            else:
                rate_path.lineTo(point)
        rate_pen = QPen(_chart_accent(theme), 2.6)
        rate_pen.setCapStyle(Qt.RoundCap)
        rate_pen.setJoinStyle(Qt.RoundJoin)
        painter.setPen(rate_pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawPath(rate_path)
        for index, point in enumerate(self._rate_points):
            painter.setPen(QPen(_chart_tooltip_bg(theme), 2))
            painter.setBrush(_chart_accent(theme))
            painter.drawEllipse(
                point,
                5 if index == self.hover_index else 3.5,
                5 if index == self.hover_index else 3.5,
            )

        painter.setFont(QFont("Segoe UI", 8, QFont.Bold))
        painter.setPen(_chart_positive(theme))
        painter.drawText(
            QRectF(left, 17, 96, 22),
            Qt.AlignLeft | Qt.AlignVCenter,
            "■ Margem",
        )
        painter.setPen(_chart_accent(theme))
        painter.drawText(
            QRectF(left + 94, 17, 124, 22),
            Qt.AlignLeft | Qt.AlignVCenter,
            "● Taxa de poupança",
        )
        painter.setPen(QColor("#FF9F0A"))
        painter.drawText(
            QRectF(max(left + 210, right - 106), 17, 106, 22),
            Qt.AlignRight | Qt.AlignVCenter,
            "Meta 20%",
        )
        if self.comparison_rows:
            painter.setPen(_chart_muted(theme))
            painter.drawText(
                QRectF(left, 37, max(180.0, right - left), 20),
                Qt.AlignLeft | Qt.AlignVCenter,
                f"┄ Referência: {self.comparison_label}",
            )

        painter.setFont(QFont("Segoe UI", 8))
        painter.setPen(_chart_muted(theme))
        painter.drawText(
            QRectF(right + 4, top - 10, 42, 20),
            Qt.AlignLeft | Qt.AlignVCenter,
            f"{rate_maximum:.0f}%",
        )
        painter.drawText(
            QRectF(right + 4, bottom - 10, 42, 20),
            Qt.AlignLeft | Qt.AlignVCenter,
            f"{rate_minimum:.0f}%",
        )

        if self.hover_index is not None:
            index = max(0, min(len(self.rows) - 1, self.hover_index))
            point = self._rate_points[index]
            painter.setPen(QPen(_chart_accent(theme), 1.2, Qt.DashLine))
            painter.drawLine(QPointF(point.x(), top), QPointF(point.x(), bottom))
            self._draw_tooltip(
                painter,
                point,
                self.rows[index],
                self._comparison_for_index(index),
            )


__all__ = ["CumulativeCashflowChart", "SavingsEfficiencyChart"]
