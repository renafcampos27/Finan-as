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
from ui.components.charts.presentation import format_axis_money
from utils.helpers import eur, format_period_variation, percent, safe_float


class MonteCarloChart(StableChart):
    def __init__(
        self, theme_provider: Callable[[], Theme], parent: Optional[QWidget] = None
    ) -> None:
        super().__init__(theme_provider, parent)
        self.set_detail_field_types(
            {
                "year": "count",
                "invested": "money",
                "p10": "money",
                "p50": "money",
                "p90": "money",
                "probability_target": "percent",
                "probability_loss_real": "percent",
            }
        )
        self.rows: list[dict[str, float]] = []
        self.target = 0.0
        self.hover_index: Optional[int] = None
        self.setMinimumHeight(390)
        self.setMouseTracking(True)
        self.setAccessibleName("Distribuição da simulação Monte Carlo")

    def set_data(self, data: dict[str, Any]) -> None:
        self.rows = [
            {
                "year": float(row.get("year", 0)),
                "invested": safe_float(row.get("invested")),
                "p10": safe_float(row.get("p10")),
                "p50": safe_float(row.get("p50")),
                "p90": safe_float(row.get("p90")),
                "probability_target": safe_float(row.get("probability_target")),
                "probability_loss_real": safe_float(row.get("probability_loss_real")),
            }
            for row in data.get("rows", []) or []
        ]
        self.target = safe_float(data.get("target"))
        self.hover_index = None
        theme = self.theme_provider()
        if self.rows:
            last = self.rows[-1]
            self.setAccessibleDescription(
                f"Mediana final {eur(last['p50'], theme.privacy)}, intervalo P10 a P90 "
                f"entre {eur(last['p10'], theme.privacy)} e {eur(last['p90'], theme.privacy)}; "
                f"probabilidade do objetivo {percent(last['probability_target'])}."
            )
        self.invalidate_cache()

    def mouseMoveEvent(self, event):  # type: ignore[override]
        if not self.rows:
            return
        plot = QRectF(72, 42, max(100, self.width() - 94), max(100, self.height() - 92))
        if plot.contains(event.position()):
            ratio = (event.position().x() - plot.left()) / max(1.0, plot.width())
            self.hover_index = max(
                0, min(len(self.rows) - 1, int(round(ratio * (len(self.rows) - 1))))
            )
        else:
            self.hover_index = None
        self.invalidate_cache()

    def leaveEvent(self, event):  # type: ignore[override]
        self.hover_index = None
        self.invalidate_cache()
        super().leaveEvent(event)

    def render_chart(self, painter: QPainter) -> None:
        theme = self.theme_provider()
        painter.fillRect(self.rect(), _chart_bg(theme))
        if not self.rows:
            self.draw_empty(painter, "Sem simulações disponíveis")
            return
        plot = QRectF(72, 42, max(100, self.width() - 94), max(100, self.height() - 92))
        high = (
            max(
                1.0,
                self.target,
                *(max(row["p90"], row["invested"]) for row in self.rows),
            )
            * 1.10
        )

        def point(index: int, key: str) -> QPointF:
            x = plot.left() + index / max(1, len(self.rows) - 1) * plot.width()
            y = plot.bottom() - safe_float(self.rows[index][key]) / high * plot.height()
            return QPointF(x, y)

        painter.setFont(QFont("Segoe UI", 8))
        for index in range(5):
            y = plot.top() + plot.height() * index / 4
            value = high * (1 - index / 4)
            painter.setPen(QPen(_chart_grid(theme), 1))
            painter.drawLine(int(plot.left()), int(y), int(plot.right()), int(y))
            painter.setPen(_chart_muted(theme))
            painter.drawText(
                QRectF(2, y - 10, 64, 20),
                Qt.AlignRight | Qt.AlignVCenter,
                format_axis_money(value, theme.privacy),
            )

        upper = [point(index, "p90") for index in range(len(self.rows))]
        lower = [point(index, "p10") for index in range(len(self.rows))]
        band = QPainterPath(upper[0])
        for item in upper[1:]:
            band.lineTo(item)
        for item in reversed(lower):
            band.lineTo(item)
        band.closeSubpath()
        band_color = QColor(_chart_secondary(theme))
        band_color.setAlpha(52 if theme.dark else 38)
        painter.setPen(Qt.NoPen)
        painter.setBrush(band_color)
        painter.drawPath(band)

        for key, color, style, width in (
            ("p50", _chart_accent(theme), Qt.SolidLine, 2.7),
            ("invested", _chart_positive(theme), Qt.DashLine, 1.8),
        ):
            path = QPainterPath(point(0, key))
            for index in range(1, len(self.rows)):
                path.lineTo(point(index, key))
            painter.setPen(QPen(color, width, style))
            painter.setBrush(Qt.NoBrush)
            painter.drawPath(path)

        if self.target > 0:
            target_y = plot.bottom() - min(high, self.target) / high * plot.height()
            painter.setPen(QPen(_chart_negative(theme), 1.4, Qt.DotLine))
            painter.drawLine(
                int(plot.left()), int(target_y), int(plot.right()), int(target_y)
            )
            painter.setPen(_chart_negative(theme))
            painter.drawText(
                QRectF(plot.left() + 5, target_y - 20, 160, 18),
                Qt.AlignLeft | Qt.AlignVCenter,
                f"Objetivo · {eur(self.target, theme.privacy)}",
            )

        painter.setPen(_chart_text(theme))
        painter.setFont(QFont("Segoe UI", 8, QFont.Bold))
        painter.drawText(
            QRectF(plot.left(), 8, 190, 24),
            Qt.AlignLeft,
            "Euros de hoje · P10–P90  ·  P50  ·  Contribuições reais",
        )
        for index, row in enumerate(self.rows):
            if index == 0 or index == len(self.rows) - 1 or int(row["year"]) % 5 == 0:
                x = point(index, "p50").x()
                painter.setPen(_chart_muted(theme))
                painter.drawText(
                    QRectF(x - 28, plot.bottom() + 8, 56, 20),
                    Qt.AlignHCenter,
                    f"{int(row['year'])}a",
                )

        if self.hover_index is not None:
            row = self.rows[self.hover_index]
            anchor = point(self.hover_index, "p50")
            box = QRectF(
                min(max(8.0, anchor.x() + 12), self.width() - 238.0),
                max(36.0, min(anchor.y() - 106, self.height() - 134.0)),
                238,
                126,
            )
            painter.setPen(QPen(_chart_grid(theme), 1))
            painter.setBrush(_chart_tooltip_bg(theme))
            painter.drawRoundedRect(box, 12, 12)
            painter.setPen(_chart_text(theme))
            painter.drawText(
                box.adjusted(12, 8, -12, -8),
                Qt.AlignLeft | Qt.AlignTop,
                f"Ano {int(row['year'])}\n"
                f"P10  {eur(row['p10'], theme.privacy)}\n"
                f"P50  {eur(row['p50'], theme.privacy)}\n"
                f"P90  {eur(row['p90'], theme.privacy)}\n"
                f"Objetivo  {percent(row['probability_target'])}\n"
                f"Risco de perda real  {percent(row['probability_loss_real'])}",
            )


class NetWorthChart(StableChart):
    def __init__(
        self, theme_provider: Callable[[], Theme], parent: Optional[QWidget] = None
    ) -> None:
        super().__init__(theme_provider, parent)
        self.data: dict[str, Any] = {}
        self.setMinimumHeight(270)
        self.setAccessibleName("Composição do património líquido")

    def set_data(self, data: dict[str, Any]) -> None:
        self.data = dict(data or {})
        theme = self.theme_provider()
        self.setAccessibleDescription(
            f"Ativos {eur(data.get('total_assets', 0), theme.privacy)}, passivos "
            f"{eur(data.get('total_liabilities', 0), theme.privacy)} e património líquido "
            f"{eur(data.get('net_worth', 0), theme.privacy)}."
        )
        self.invalidate_cache()

    def _current_detail_item(self) -> dict[str, Any] | None:
        return dict(self.data) if self.data else None

    def render_chart(self, painter: QPainter) -> None:
        theme = self.theme_provider()
        painter.fillRect(self.rect(), _chart_bg(theme))
        assets = safe_float(self.data.get("total_assets"))
        liabilities = safe_float(self.data.get("total_liabilities"))
        if assets <= 0 and liabilities <= 0:
            self.draw_empty(
                painter, "Regista ativos ou dívidas para compor o património"
            )
            return
        maximum = max(1.0, assets, liabilities)
        left = 150.0
        width = max(120.0, self.width() - left - 34.0)
        for index, (label, value, color) in enumerate(
            (
                ("Ativos", assets, _chart_positive(theme)),
                ("Passivos", liabilities, _chart_negative(theme)),
            )
        ):
            y = 48.0 + index * 64.0
            painter.setPen(_chart_text(theme))
            painter.setFont(QFont("Segoe UI", 9, QFont.Bold))
            painter.drawText(
                QRectF(8, y, 132, 25), Qt.AlignRight | Qt.AlignVCenter, label
            )
            painter.setPen(Qt.NoPen)
            painter.setBrush(_chart_grid(theme))
            painter.drawRoundedRect(QRectF(left, y + 4, width, 18), 9, 9)
            painter.setBrush(color)
            painter.drawRoundedRect(
                QRectF(left, y + 4, max(2.0, width * value / maximum), 18), 9, 9
            )
            painter.setPen(_chart_text(theme))
            painter.drawText(
                QRectF(left, y + 27, width, 22),
                Qt.AlignLeft,
                eur(value, theme.privacy),
            )
        net = assets - liabilities
        painter.setPen(_chart_accent(theme) if net >= 0 else _chart_negative(theme))
        painter.setFont(QFont("Segoe UI", 11, QFont.Bold))
        painter.drawText(
            QRectF(left, 180, width, 34),
            Qt.AlignLeft | Qt.AlignVCenter,
            f"Património líquido  {eur(net, theme.privacy)}",
        )
        painter.setPen(_chart_muted(theme))
        painter.setFont(QFont("Segoe UI", 8))
        debt_ratio = safe_float(self.data.get("debt_ratio_pct"))
        painter.drawText(
            QRectF(left, 214, width, 24),
            Qt.AlignLeft | Qt.AlignVCenter,
            f"Rácio de dívida {percent(debt_ratio)} · prestação mensal "
            f"{eur(self.data.get('monthly_debt_payment', 0), theme.privacy)}",
        )


class PeriodComparisonChart(StableChart):
    def __init__(
        self, theme_provider: Callable[[], Theme], parent: Optional[QWidget] = None
    ) -> None:
        super().__init__(theme_provider, parent)
        self.rows: list[dict[str, Any]] = []
        self.hover_index: Optional[int] = None
        self._row_rects: list[QRectF] = []
        self.setMinimumHeight(390)
        self.setAccessibleName("Comparação de despesas entre dois períodos")

    def set_data(self, data: dict[str, Any]) -> None:
        self.rows = [dict(row) for row in (data.get("categories", []) or [])]
        self.hover_index = None
        self._row_rects.clear()
        self.setMinimumHeight(max(390, 52 + len(self.rows) * 46))
        self.updateGeometry()
        if self.rows:
            active = [
                row
                for row in self.rows
                if safe_float(row.get("current")) or safe_float(row.get("reference"))
            ]
            largest = active[0] if active else self.rows[0]
            largest_variation = format_period_variation(
                safe_float(largest.get("current")),
                safe_float(largest.get("reference")),
            )
            self.setAccessibleDescription(
                f"{len(self.rows)} categorias comparadas; {len(active)} com movimentos. "
                f"Maior categoria: {largest.get('category')}, variação "
                f"{largest_variation['display']}, {largest_variation['context']}."
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
        self._row_rects.clear()
        self.setCursor(Qt.ArrowCursor)
        self.invalidate_cache()
        super().leaveEvent(event)

    def render_chart(self, painter: QPainter) -> None:
        theme = self.theme_provider()
        painter.fillRect(self.rect(), _chart_bg(theme))
        self._row_rects = []
        if not self.rows:
            self.draw_empty(painter, "Sem despesas nos períodos selecionados")
            return
        label_width = min(190.0, max(125.0, self.width() * 0.22))
        plot_left = label_width + 16
        plot_width = max(100.0, self.width() - plot_left - 96)
        maximum = max(
            1.0,
            *(
                max(safe_float(row.get("current")), safe_float(row.get("reference")))
                for row in self.rows
            ),
        )
        row_height = max(36.0, min(46.0, (self.height() - 58.0) / len(self.rows)))
        current_color = _chart_accent(theme)
        reference_color = _chart_secondary(theme)
        painter.setFont(QFont("Segoe UI", 8, QFont.Bold))
        painter.setPen(_chart_text(theme))
        painter.drawText(
            QRectF(plot_left, 8, 250, 22), Qt.AlignLeft, "Atual  ·  Referência"
        )
        for index, row in enumerate(self.rows):
            y = 38.0 + index * row_height
            row_rect = QRectF(0, y - 2, self.width(), row_height)
            self._row_rects.append(row_rect)
            if index == self.hover_index:
                highlight = QColor(current_color)
                highlight.setAlpha(24 if theme.dark else 18)
                painter.fillRect(row_rect, highlight)
            category = str(row.get("category") or "Outros")
            painter.setPen(_chart_text(theme))
            painter.setFont(QFont("Segoe UI", 8, QFont.Bold))
            painter.drawText(
                QRectF(6, y, label_width, row_height - 4),
                Qt.AlignRight | Qt.AlignVCenter,
                category,
            )
            current = safe_float(row.get("current"))
            reference = safe_float(row.get("reference"))
            for offset, value, color in (
                (5.0, current, current_color),
                (21.0, reference, reference_color),
            ):
                painter.setPen(Qt.NoPen)
                painter.setBrush(color)
                painter.drawRoundedRect(
                    QRectF(
                        plot_left,
                        y + offset,
                        max(1.5, plot_width * value / maximum),
                        10,
                    ),
                    5,
                    5,
                )
            change = safe_float(row.get("change_pct"))
            variation = format_period_variation(
                current,
                reference,
                privacy=theme.privacy,
            )
            painter.setPen(
                _chart_negative(theme)
                if change > 0
                else _chart_positive(theme)
                if change < 0
                else _chart_muted(theme)
            )
            painter.drawText(
                QRectF(plot_left + plot_width + 8, y + 5, 82, 28),
                Qt.AlignLeft | Qt.AlignVCenter,
                ("—" if current == 0 and reference == 0 else str(variation["display"])),
            )
