from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Optional

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
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
    _chart_secondary,
    _chart_text,
    _chart_tooltip_bg,
)
from ui.core.theme import Theme
from ui.components.charts.presentation import format_axis_money
from utils.helpers import eur, safe_float


def _percentage(value: float, signed: bool = False) -> str:
    prefix = "+" if signed and value > 0 else ""
    return f"{prefix}{value:.1f}%".replace(".", ",")


def _percentage_points(value: float, signed: bool = False) -> str:
    prefix = "+" if signed and value > 0 else ""
    return f"{prefix}{value:.1f} p.p.".replace(".", ",")


def _display_date(value: str, compact: bool = False) -> str:
    try:
        parsed = datetime.strptime(str(value)[:10], "%Y-%m-%d")
        return parsed.strftime("%m/%y" if compact else "%d/%m/%Y")
    except (TypeError, ValueError):
        return str(value or "")


class InvestmentEvolutionChart(StableChart):
    """Two-series history of invested capital and current portfolio value."""

    def __init__(
        self, theme_provider: Callable[[], Theme], parent: Optional[QWidget] = None
    ):
        super().__init__(theme_provider, parent)
        self.rows: list[dict[str, Any]] = []
        self.portfolio_return_pct = 0.0
        self.hover_index: Optional[int] = None
        self.setMinimumHeight(340)
        self.setAccessibleName("Evolução do capital investido e do valor total")

    def set_data(
        self, rows: list[dict[str, Any]], portfolio_return_pct: float = 0.0
    ) -> None:
        clean: dict[str, dict[str, Any]] = {}
        for row in rows or []:
            day = str(row.get("date") or "")[:10]
            if not day:
                continue
            invested = max(0.0, safe_float(row.get("invested")))
            total = max(0.0, safe_float(row.get("total")))
            clean[day] = {
                "date": day,
                "invested": invested,
                "total": total,
                "gain": total - invested,
                "return_pct": (
                    (total - invested) / invested * 100 if invested else 0.0
                ),
            }
        self.rows = [clean[key] for key in sorted(clean)]
        self.portfolio_return_pct = safe_float(portfolio_return_pct)
        self.hover_index = None
        self.setAccessibleDescription(
            f"{len(self.rows)} pontos. Rentabilidade ponderada da carteira "
            f"{_percentage(self.portfolio_return_pct, signed=True)}."
        )
        if self.isVisible() and self.rows:
            self.start_entrance_animation(650)
        else:
            self.invalidate_cache()

    def _plot_rect(self) -> tuple[float, float, float, float]:
        return (
            78.0,
            45.0,
            max(110.0, self.width() - 24.0),
            max(118.0, self.height() - 48.0),
        )

    def _scale(self) -> tuple[float, float]:
        values = [
            safe_float(row.get(key))
            for row in self.rows
            for key in ("invested", "total")
        ]
        if not values:
            return 0.0, 1.0
        low, high = min(values), max(values)
        padding = max((high - low) * 0.18, high * 0.035, 1.0)
        return max(0.0, low - padding), max(low + 1.0, high + padding)

    def _point(
        self,
        index: int,
        value: float,
        low: float,
        high: float,
    ) -> QPointF:
        left, top, right, bottom = self._plot_rect()
        if len(self.rows) == 1:
            x = (left + right) / 2.0
        else:
            x = left + (right - left) * index / max(1, len(self.rows) - 1)
        y = bottom - (bottom - top) * (value - low) / max(1.0, high - low)
        return QPointF(x, y)

    def mouseMoveEvent(self, event):  # type: ignore[override]
        if not self.rows:
            return
        left, _top, right, _bottom = self._plot_rect()
        ratio = min(max((event.position().x() - left) / max(1.0, right - left), 0), 1)
        self.hover_index = (
            0
            if len(self.rows) == 1
            else max(
                0,
                min(
                    len(self.rows) - 1,
                    int(round(ratio * (len(self.rows) - 1))),
                ),
            )
        )
        self.invalidate_cache()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):  # type: ignore[override]
        self.hover_index = None
        self.invalidate_cache()
        super().leaveEvent(event)

    @staticmethod
    def _line_path(points: list[QPointF]) -> QPainterPath:
        path = QPainterPath()
        if not points:
            return path
        path.moveTo(points[0])
        for point in points[1:]:
            path.lineTo(point)
        return path

    def _draw_tooltip(
        self,
        painter: QPainter,
        index: int,
        invested_point: QPointF,
        total_point: QPointF,
    ) -> None:
        theme = self.theme_provider()
        row = self.rows[index]
        width = min(300.0, max(210.0, self.width() - 24.0))
        height = 118.0
        anchor = total_point if total_point.y() < invested_point.y() else invested_point
        x = min(max(anchor.x() + 14, 8.0), self.width() - width - 8.0)
        y = max(8.0, anchor.y() - height - 12.0)
        if y + height > self.height() - 8:
            y = max(8.0, self.height() - height - 8.0)
        rect = QRectF(x, y, width, height)
        painter.setPen(QPen(_chart_grid(theme), 1))
        painter.setBrush(_chart_tooltip_bg(theme))
        painter.drawRoundedRect(rect, 13, 13)

        painter.setFont(QFont("Segoe UI", 9, QFont.Bold))
        painter.setPen(_chart_text(theme))
        painter.drawText(
            QRectF(x + 14, y + 8, width - 28, 22),
            Qt.AlignLeft | Qt.AlignVCenter,
            _display_date(str(row["date"])),
        )
        lines = [
            ("Investido", eur(row["invested"], theme.privacy), _chart_accent(theme)),
            ("Valor total", eur(row["total"], theme.privacy), _chart_secondary(theme)),
            (
                "Ganho/perda",
                f"{eur(row['gain'], theme.privacy)} · "
                f"{_percentage(safe_float(row['return_pct']), signed=True)}",
                _chart_positive(theme)
                if safe_float(row["gain"]) >= 0
                else _chart_negative(theme),
            ),
        ]
        painter.setFont(QFont("Segoe UI", 8))
        for line, (label, value, color) in enumerate(lines):
            line_y = y + 35 + line * 24
            painter.setPen(Qt.NoPen)
            painter.setBrush(color)
            painter.drawEllipse(QRectF(x + 14, line_y + 6, 7, 7))
            painter.setPen(_chart_muted(theme))
            painter.drawText(
                QRectF(x + 28, line_y, 86, 20),
                Qt.AlignLeft | Qt.AlignVCenter,
                label,
            )
            painter.setPen(_chart_text(theme))
            painter.drawText(
                QRectF(x + 110, line_y, width - 124, 20),
                Qt.AlignRight | Qt.AlignVCenter,
                value,
            )

    def render_chart(self, painter: QPainter) -> None:
        theme = self.theme_provider()
        painter.fillRect(self.rect(), _chart_bg(theme))
        if not self.rows:
            self.draw_empty(painter, "Sem investimentos registados")
            return

        left, top, right, bottom = self._plot_rect()
        low, high = self._scale()
        invested_color = _chart_accent(theme)
        total_color = _chart_secondary(theme)

        painter.setFont(QFont("Segoe UI", 8))
        for step in range(5):
            ratio = step / 4
            y = top + (bottom - top) * ratio
            value = high - (high - low) * ratio
            painter.setPen(QPen(_chart_grid(theme), 1))
            painter.drawLine(QPointF(left, y), QPointF(right, y))
            painter.setPen(_chart_muted(theme))
            painter.drawText(
                QRectF(2, y - 10, left - 9, 20),
                Qt.AlignRight | Qt.AlignVCenter,
                eur(value, theme.privacy),
            )

        invested_points = [
            self._point(i, safe_float(row["invested"]), low, high)
            for i, row in enumerate(self.rows)
        ]
        total_points = [
            self._point(i, safe_float(row["total"]), low, high)
            for i, row in enumerate(self.rows)
        ]

        if len(self.rows) > 1:
            band_color = (
                _chart_positive(theme)
                if safe_float(self.rows[-1]["gain"]) >= 0
                else _chart_negative(theme)
            )
            band_color.setAlpha(30)
            painter.setPen(Qt.NoPen)
            painter.setBrush(band_color)
            painter.drawPolygon(
                QPolygonF(total_points + list(reversed(invested_points)))
            )

        for points, color in (
            (invested_points, invested_color),
            (total_points, total_color),
        ):
            pen = QPen(color, 3)
            pen.setCapStyle(Qt.RoundCap)
            pen.setJoinStyle(Qt.RoundJoin)
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)
            if len(points) > 1:
                painter.drawPath(self._line_path(points))
            painter.setPen(QPen(_chart_bg(theme), 2))
            painter.setBrush(color)
            for point in points:
                painter.drawEllipse(point, 4.5, 4.5)

        painter.setFont(QFont("Segoe UI", 8))
        label_every = max(1, len(self.rows) // 6)
        for index, row in enumerate(self.rows):
            if index % label_every == 0 or index == len(self.rows) - 1:
                painter.setPen(_chart_muted(theme))
                painter.drawText(
                    QRectF(invested_points[index].x() - 30, bottom + 8, 60, 20),
                    Qt.AlignHCenter | Qt.AlignTop,
                    _display_date(str(row["date"]), compact=True),
                )

        painter.setPen(QPen(invested_color, 3))
        painter.drawLine(QPointF(left, 15), QPointF(left + 18, 15))
        painter.setPen(_chart_text(theme))
        painter.drawText(QRectF(left + 24, 5, 82, 20), Qt.AlignVCenter, "Investido")
        painter.setPen(QPen(total_color, 3))
        painter.drawLine(QPointF(left + 110, 15), QPointF(left + 128, 15))
        painter.setPen(_chart_text(theme))
        painter.drawText(QRectF(left + 134, 5, 82, 20), Qt.AlignVCenter, "Valor total")

        gain_color = (
            _chart_positive(theme)
            if self.portfolio_return_pct >= 0
            else _chart_negative(theme)
        )
        badge = QRectF(max(left + 220, right - 190), 5, 190, 25)
        badge_fill = QColor(gain_color)
        badge_fill.setAlpha(35)
        painter.setPen(QPen(gain_color, 1))
        painter.setBrush(badge_fill)
        painter.drawRoundedRect(badge, 11, 11)
        painter.setPen(_chart_text(theme))
        painter.setFont(QFont("Segoe UI", 8, QFont.Bold))
        painter.drawText(
            badge,
            Qt.AlignCenter,
            "Rentabilidade da carteira: "
            f"{_percentage(self.portfolio_return_pct, signed=True)}",
        )

        if len(self.rows) == 1:
            painter.setFont(QFont("Segoe UI", 8))
            painter.setPen(_chart_muted(theme))
            painter.drawText(
                QRectF(left, bottom - 26, right - left, 20),
                Qt.AlignCenter,
                "Histórico iniciado hoje · os próximos dias formarão a evolução",
            )

        if self.hover_index is not None and 0 <= self.hover_index < len(self.rows):
            index = self.hover_index
            x = invested_points[index].x()
            painter.setPen(QPen(_chart_grid(theme), 1, Qt.DashLine))
            painter.drawLine(QPointF(x, top), QPointF(x, bottom))
            self._draw_tooltip(
                painter, index, invested_points[index], total_points[index]
            )


class PortfolioContributionChart(StableChart):
    """Interactive attribution of net portfolio return to each holding."""

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
        self.setMinimumHeight(310)
        self.setAccessibleName(
            "Contribuição de cada ativo para a rentabilidade líquida"
        )
        self.set_detail_field_types(
            {
                "current_value": "money",
                "cost_basis": "money",
                "fees": "money",
                "gross_gain": "money",
                "net_gain": "money",
                "contribution_pp": "percent",
                "net_return_pct": "percent",
            }
        )

    def set_data(self, data: dict[str, Any]) -> None:
        self.metrics = dict(data or {})
        self.rows = [dict(row) for row in self.metrics.get("rows", []) or []]
        self.hover_index = None
        self._row_rects.clear()
        self.setMinimumHeight(max(310, 98 + len(self.rows) * 46))
        self.updateGeometry()
        theme = self.theme_provider()
        gap = data.get("return_gap_pp", data.get("fee_impact_pp")) if data.get("net_return_pct") is not None else None
        gap_text = (
            "não disponível" if gap is None else
            "••• p.p." if theme.privacy else f"{safe_float(gap):.1f} p.p."
        )
        self.setAccessibleDescription(
            f"{'•••' if theme.privacy else len(self.rows)} ativos. Rentabilidade líquida ponderada "
            + (
                ("•••%; " if theme.privacy else f"{_percentage(safe_float(data.get('net_return_pct')), signed=True)}; ")
                if data.get("net_return_pct") is not None
                else "não disponível; "
            )
            + f"comissões {eur(data.get('total_fees'), theme.privacy)}; "
            f"diferença bruto/líquido em bases distintas {gap_text}."
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
                "Regista investimentos para calcular a contribuição por ativo",
            )
            return

        label_width = min(230.0, max(135.0, self.width() * 0.22))
        info_width = min(285.0, max(205.0, self.width() * 0.28))
        plot_left = label_width + 20.0
        plot_width = max(
            130.0,
            self.width() - plot_left - info_width - 14.0,
        )
        zero_x = plot_left + plot_width / 2.0
        top = 70.0
        row_height = 46.0
        maximum = max(
            0.25,
            *(abs(safe_float(row.get("contribution_pp"))) for row in self.rows),
        )

        net_return_value = self.metrics.get("net_return_pct")
        net_return = safe_float(net_return_value)
        total_fees = safe_float(self.metrics.get("total_fees"))
        gap = self.metrics.get("return_gap_pp", self.metrics.get("fee_impact_pp")) if net_return_value is not None else None
        gap_text = (
            "N/D" if gap is None else
            "••• p.p." if theme.privacy else _percentage_points(safe_float(gap), signed=True)
        )
        return_color = (
            _chart_positive(theme) if net_return >= 0 else _chart_negative(theme)
        )
        painter.setPen(return_color)
        painter.setFont(QFont("Segoe UI", 9, QFont.Bold))
        painter.drawText(
            QRectF(8, 7, max(250.0, self.width() * 0.48), 24),
            Qt.AlignLeft | Qt.AlignVCenter,
            (
                "Rentabilidade líquida ponderada "
                + ("•••%" if theme.privacy else _percentage(net_return, signed=True))
                if net_return_value is not None
                else "Rentabilidade não disponível · custo de aquisição em falta"
            ),
        )
        painter.setPen(_chart_muted(theme))
        painter.setFont(QFont("Segoe UI", 8))
        fee_text = (
            f"Comissões {eur(total_fees, theme.privacy)} · diferença de retornos (bases distintas) {gap_text}"
            if total_fees > 0 or gap is None
            else "Sem comissões registadas · bruto e líquido coincidem"
        )
        painter.drawText(
            QRectF(8, 31, self.width() - 16, 23),
            Qt.AlignLeft | Qt.AlignVCenter,
            (
                f"{fee_text} · as contribuições somam "
                + ("•••%" if theme.privacy else _percentage(net_return, signed=True))
                if net_return_value is not None
                else f"{fee_text} · cobertura do custo "
                + ("•••%" if theme.privacy else _percentage(safe_float(self.metrics.get('data_coverage_pct'))))
            ),
        )

        painter.setPen(QPen(_chart_grid(theme), 1, Qt.DashLine))
        painter.drawLine(
            QPointF(zero_x, top - 8),
            QPointF(zero_x, top + len(self.rows) * row_height - 8),
        )
        painter.setPen(_chart_muted(theme))
        painter.setFont(QFont("Segoe UI", 8, QFont.Bold))
        painter.drawText(
            QRectF(plot_left, 51, plot_width, 18),
            Qt.AlignCenter,
            "Contribuição para a rentabilidade · pontos percentuais",
        )

        for index, row in enumerate(self.rows):
            y = top + index * row_height
            full_rect = QRectF(0, y - 3, self.width(), row_height)
            self._row_rects.append(full_rect)
            contribution = safe_float(row.get("contribution_pp"))
            performance_available = bool(row.get("performance_available"))
            color = (
                _chart_positive(theme) if contribution >= 0 else _chart_negative(theme)
            )
            if index == self.hover_index:
                hover = QColor(color)
                hover.setAlpha(27 if theme.dark else 20)
                painter.fillRect(full_rect, hover)

            painter.setPen(_chart_text(theme))
            painter.setFont(QFont("Segoe UI", 8, QFont.Bold))
            painter.drawText(
                QRectF(6, y, label_width, 30),
                Qt.AlignRight | Qt.AlignVCenter,
                painter.fontMetrics().elidedText(
                    "Dados ocultos" if theme.privacy else str(row.get("name") or "Investimento"),
                    Qt.ElideRight,
                    int(label_width - 6),
                ),
            )

            if not performance_available:
                painter.setPen(_chart_muted(theme))
                painter.setFont(QFont("Segoe UI", 8, QFont.Bold))
                painter.drawText(
                    QRectF(plot_left + 8, y, plot_width + info_width - 4, 30),
                    Qt.AlignLeft | Qt.AlignVCenter,
                    "Custo de aquisição desconhecido · rentabilidade N/D",
                )
                continue

            half_width = plot_width / 2.0 - 4.0
            magnitude = max(
                2.0,
                half_width * abs(contribution) / maximum,
            )
            bar_x = zero_x if contribution >= 0 else zero_x - magnitude
            track = QRectF(plot_left, y + 8, plot_width, 16)
            painter.setPen(Qt.NoPen)
            painter.setBrush(_chart_grid(theme))
            painter.drawRoundedRect(track, 8, 8)
            painter.setBrush(color)
            painter.drawRoundedRect(
                QRectF(bar_x, y + 8, magnitude, 16),
                8,
                8,
            )

            net_gain = safe_float(row.get("net_gain"))
            fee = safe_float(row.get("fees"))
            painter.setPen(color)
            painter.setFont(QFont("Segoe UI", 8, QFont.Bold))
            painter.drawText(
                QRectF(
                    plot_left + plot_width + 9,
                    y,
                    info_width - 15,
                    30,
                ),
                Qt.AlignRight | Qt.AlignVCenter,
                ("••• p.p." if theme.privacy else _percentage_points(contribution, signed=True)) + " · "
                f"{eur(net_gain, theme.privacy)} · "
                f"custos {eur(fee, theme.privacy)}",
            )

        if self.hover_index is not None:
            row = self.rows[self.hover_index]
            y = top + self.hover_index * row_height
            width = min(330.0, max(260.0, self.width() - 24.0))
            height = 142.0
            box = QRectF(
                min(max(8.0, zero_x - width / 2.0), self.width() - width - 8),
                max(56.0, min(y - 148.0, self.height() - height - 8)),
                width,
                height,
            )
            painter.setPen(QPen(_chart_grid(theme), 1))
            painter.setBrush(_chart_tooltip_bg(theme))
            painter.drawRoundedRect(box, 12, 12)
            painter.setPen(_chart_text(theme))
            painter.setFont(QFont("Segoe UI", 8, QFont.Bold))
            identity = "Dados ocultos" if theme.privacy else f"{row.get('name', '')} · {row.get('asset_class', '')}"
            tooltip_text = (
                f"{identity}\n"
                f"Valor atual: {eur(row.get('current_value', 0), theme.privacy)}\n"
                "Capital: N/D · rentabilidade não disponível\n"
                "Confirma o custo de aquisição para calcular ganhos."
                if not row.get("performance_available")
                else f"{identity}\n"
                f"Valor atual: {eur(row.get('current_value', 0), theme.privacy)}\n"
                f"Capital: {eur(row.get('cost_basis', 0), theme.privacy)} · "
                f"comissões: {eur(row.get('fees', 0), theme.privacy)}\n"
                f"Ganho bruto: {eur(row.get('gross_gain', 0), theme.privacy)}\n"
                f"Ganho líquido: {eur(row.get('net_gain', 0), theme.privacy)}\n"
                f"Contribuição: "
                + ("••• p.p." if theme.privacy else _percentage_points(safe_float(row.get('contribution_pp')), signed=True))
            )
            painter.drawText(
                box.adjusted(13, 9, -13, -9),
                Qt.AlignLeft | Qt.AlignVCenter,
                tooltip_text,
            )


class InvestmentProjectionChart(StableChart):
    """Five-year real-value forecast with pessimistic/base/optimistic paths."""

    def __init__(
        self, theme_provider: Callable[[], Theme], parent: Optional[QWidget] = None
    ):
        super().__init__(theme_provider, parent)
        self.rows: list[dict[str, Any]] = []
        self.previous_rows: list[dict[str, Any]] = []
        self.mode = "monthly"
        self.contribution_amount = 0.0
        self.monthly_contribution = 0.0
        self.initial_capital = 0.0
        self.annual_rate_pct = 0.0
        self.inflation_pct = 0.0
        self.tax_pct = 0.0
        self.fee_pct = 0.0
        self.scenario_spread_pct = 2.0
        self.hover_index: Optional[int] = None
        self.setMinimumHeight(370)
        self.setAccessibleName("Projeção de investimento entre um e cinco anos")
        self.setAccessibleDescription(
            "Altera os parâmetros e passa o rato pelos anos para explorar os valores."
        )

    def _has_animation_data(self) -> bool:
        # Avoid an entrance animation; animation is reserved for slider transitions.
        return False

    def _display_rows(self) -> list[dict[str, Any]]:
        if (
            not self.previous_rows
            or len(self.previous_rows) != len(self.rows)
            or self.anim_progress >= 0.999
        ):
            return [dict(row) for row in self.rows]
        progress = max(0.0, min(1.0, self.anim_progress))
        displayed: list[dict[str, Any]] = []
        for previous, target in zip(self.previous_rows, self.rows):
            row: dict[str, Any] = {
                "year": int(target.get("year", 0) or 0),
                "months": int(target.get("months", 0) or 0),
            }
            for key in (
                "invested",
                "invested_nominal",
                "invested_real",
                "projected_base",
                "projected_pessimistic",
                "projected_optimistic",
                "nominal_base",
                "projected_nominal",
                "gain_nominal",
                "gain_real",
                "fees_paid",
                "taxes_paid",
            ):
                start = safe_float(previous.get(key))
                row[key] = start + (safe_float(target.get(key)) - start) * progress
            row["projected"] = row["projected_base"]
            row["gain"] = row["projected_base"] - row["invested"]
            row["return_pct"] = (
                row["gain"] / row["invested"] * 100.0 if row["invested"] else 0.0
            )
            displayed.append(row)
        return displayed

    def set_projection(self, data: dict[str, Any]) -> None:
        current_rows = self._display_rows()
        clean_rows: list[dict[str, Any]] = []
        for row in data.get("rows", []) or []:
            year = max(1, int(row.get("year", 1) or 1))
            invested_nominal = max(
                0.0,
                safe_float(row.get("invested_nominal", row.get("invested"))),
            )
            invested_real = max(
                0.0,
                safe_float(row.get("invested_real", invested_nominal)),
            )
            base = max(
                0.0,
                safe_float(row.get("projected_base", row.get("projected"))),
            )
            pessimistic = max(0.0, safe_float(row.get("projected_pessimistic", base)))
            optimistic = max(0.0, safe_float(row.get("projected_optimistic", base)))
            gain = base - invested_real
            clean_rows.append(
                {
                    "year": year,
                    "months": max(1, int(row.get("months", year * 12) or year * 12)),
                    "invested": invested_real,
                    "invested_nominal": invested_nominal,
                    "invested_real": invested_real,
                    "projected": base,
                    "projected_base": base,
                    "projected_pessimistic": pessimistic,
                    "projected_optimistic": optimistic,
                    "nominal_base": max(0.0, safe_float(row.get("nominal_base", base))),
                    "projected_nominal": max(
                        0.0,
                        safe_float(
                            row.get("projected_nominal", row.get("nominal_base", base))
                        ),
                    ),
                    "gain": gain,
                    "gain_real": safe_float(row.get("gain_real", gain)),
                    "gain_nominal": safe_float(
                        row.get(
                            "gain_nominal",
                            safe_float(row.get("nominal_base", base))
                            - invested_nominal,
                        )
                    ),
                    "return_pct": (
                        gain / invested_real * 100.0 if invested_real else 0.0
                    ),
                    "return_real_pct": safe_float(
                        row.get(
                            "return_real_pct",
                            gain / invested_real * 100.0 if invested_real else 0.0,
                        )
                    ),
                    "return_nominal_pct": safe_float(
                        row.get(
                            "return_nominal_pct",
                            (
                                safe_float(row.get("nominal_base", base))
                                - invested_nominal
                            )
                            / invested_nominal
                            * 100.0
                            if invested_nominal
                            else 0.0,
                        )
                    ),
                    "fees_paid": max(0.0, safe_float(row.get("fees_paid"))),
                    "taxes_paid": max(0.0, safe_float(row.get("taxes_paid"))),
                }
            )
        clean_rows.sort(key=lambda row: int(row["year"]))
        can_transition = bool(current_rows) and len(current_rows) == len(clean_rows)
        self.previous_rows = (
            [dict(row) for row in current_rows]
            if can_transition
            else [dict(row) for row in clean_rows]
        )
        self.rows = clean_rows
        self.mode = str(data.get("mode") or "monthly")
        self.contribution_amount = max(
            0.0,
            safe_float(data.get("contribution", data.get("monthly_contribution"))),
        )
        self.monthly_contribution = max(
            0.0, safe_float(data.get("monthly_contribution"))
        )
        self.initial_capital = max(0.0, safe_float(data.get("initial_capital")))
        self.annual_rate_pct = safe_float(data.get("annual_rate_pct"))
        self.inflation_pct = safe_float(data.get("inflation_pct"))
        self.tax_pct = safe_float(data.get("tax_pct"))
        self.fee_pct = safe_float(data.get("fee_pct"))
        self.scenario_spread_pct = safe_float(data.get("scenario_spread_pct", 2.0))
        self.hover_index = None

        theme = self.theme_provider()
        if self.rows:
            last = self.rows[-1]
            mode_label = (
                "reforço mensal" if self.mode == "monthly" else "investimento único"
            )
            self.setAccessibleDescription(
                f"{mode_label.capitalize()} "
                f"{eur(self.contribution_amount, theme.privacy)}. "
                f"Ao fim de {last['year']} anos, cenário base em euros de hoje "
                f"{eur(last['projected_base'], theme.privacy)}, entre "
                f"{eur(last['projected_pessimistic'], theme.privacy)} e "
                f"{eur(last['projected_optimistic'], theme.privacy)}, com rentabilidade "
                f"{_percentage(safe_float(last['return_pct']), signed=True)}."
            )
        else:
            self.setAccessibleDescription("Sem dados de projeção")

        if can_transition and self.isVisible():
            self.start_entrance_animation(280)
        else:
            self._anim_progress = 1.0
            self.invalidate_cache()

    def _plot_rect(self) -> QRectF:
        return QRectF(
            78.0,
            72.0,
            max(130.0, self.width() - 104.0),
            max(130.0, self.height() - 122.0),
        )

    @staticmethod
    def _axis_value(value: float, privacy: bool) -> str:
        return format_axis_money(value, privacy=privacy)

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
        count = len(rows)
        points: list[QPointF] = []
        for index, row in enumerate(rows):
            x = (
                plot.center().x()
                if count == 1
                else plot.left() + plot.width() * index / max(1, count - 1)
            )
            y = plot.bottom() - plot.height() * safe_float(row.get(key)) / max(
                1.0, high
            )
            points.append(QPointF(x, y))
        return points

    def mouseMoveEvent(self, event):  # type: ignore[override]
        if not self.rows:
            return
        plot = self._plot_rect()
        ratio = max(
            0.0,
            min(1.0, (event.position().x() - plot.left()) / max(1.0, plot.width())),
        )
        self.hover_index = max(
            0,
            min(len(self.rows) - 1, round(ratio * max(0, len(self.rows) - 1))),
        )
        self.setCursor(Qt.CrossCursor)
        self.invalidate_cache()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):  # type: ignore[override]
        self.hover_index = None
        self.setCursor(Qt.ArrowCursor)
        self.invalidate_cache()
        super().leaveEvent(event)

    def _draw_tooltip(
        self,
        painter: QPainter,
        rows: list[dict[str, Any]],
        projected_points: list[QPointF],
    ) -> None:
        if self.hover_index is None or not (0 <= self.hover_index < len(rows)):
            return
        theme = self.theme_provider()
        row = rows[self.hover_index]
        anchor = projected_points[self.hover_index]
        width = min(336.0, max(250.0, self.width() - 18.0))
        height = 209.0
        x = max(8.0, min(anchor.x() + 14.0, self.width() - width - 8.0))
        y = anchor.y() - height - 13.0
        if y < 8.0:
            y = anchor.y() + 15.0
        y = max(8.0, min(y, self.height() - height - 8.0))
        rect = QRectF(x, y, width, height)

        painter.setPen(QPen(_chart_grid(theme), 1))
        painter.setBrush(_chart_tooltip_bg(theme))
        painter.drawRoundedRect(rect, 13.0, 13.0)
        painter.setFont(QFont("Segoe UI", 9, QFont.Bold))
        painter.setPen(_chart_text(theme))
        year = int(row["year"])
        contribution_label = (
            f"{eur(self.contribution_amount, theme.privacy)}/mês"
            if self.mode == "monthly"
            else f"{eur(self.contribution_amount, theme.privacy)} único"
        )
        painter.drawText(
            QRectF(x + 13.0, y + 7.0, width - 26.0, 22.0),
            Qt.AlignVCenter,
            f"{year} {'ano' if year == 1 else 'anos'} · {contribution_label}",
        )
        lines = [
            ("Contribuições (hoje)", eur(row["invested_real"], theme.privacy)),
            ("Contribuições nominais", eur(row["invested_nominal"], theme.privacy)),
            ("Pessimista", eur(row["projected_pessimistic"], theme.privacy)),
            ("Base · euros de hoje", eur(row["projected_base"], theme.privacy)),
            ("Otimista", eur(row["projected_optimistic"], theme.privacy)),
            (
                "Ganho base",
                f"{eur(row['gain'], theme.privacy)} · "
                f"{_percentage(safe_float(row['return_pct']), signed=True)}",
            ),
        ]
        painter.setFont(QFont("Segoe UI", 8))
        for index, (label, value) in enumerate(lines):
            line_y = y + 35.0 + index * 28.0
            painter.setPen(_chart_muted(theme))
            painter.drawText(
                QRectF(x + 13.0, line_y, 106.0, 21.0),
                Qt.AlignLeft | Qt.AlignVCenter,
                label,
            )
            painter.setPen(_chart_text(theme))
            painter.drawText(
                QRectF(x + 121.0, line_y, width - 134.0, 21.0),
                Qt.AlignRight | Qt.AlignVCenter,
                value,
            )

    def render_chart(self, painter: QPainter) -> None:
        theme = self.theme_provider()
        painter.fillRect(self.rect(), _chart_bg(theme))
        rows = self._display_rows()
        if (
            not rows
            or max(safe_float(row.get("projected_optimistic")) for row in rows) <= 0
        ):
            self.draw_empty(painter, "Define um investimento para criar os cenários")
            return

        plot = self._plot_rect()
        high = max(
            1.0,
            max(
                safe_float(row.get(key))
                for row in rows
                for key in (
                    "invested",
                    "projected_pessimistic",
                    "projected_base",
                    "projected_optimistic",
                )
            )
            * 1.18,
        )
        invested_color = _chart_muted(theme)
        pessimistic_color = _chart_negative(theme)
        base_color = _chart_accent(theme)
        optimistic_color = _chart_positive(theme)

        painter.setFont(QFont("Segoe UI", 8))
        for step in range(5):
            ratio = step / 4.0
            y = plot.top() + plot.height() * ratio
            value = high * (1.0 - ratio)
            painter.setPen(QPen(_chart_grid(theme), 1))
            painter.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
            painter.setPen(_chart_muted(theme))
            painter.drawText(
                QRectF(2.0, y - 9.0, 69.0, 18.0),
                Qt.AlignRight | Qt.AlignVCenter,
                self._axis_value(value, theme.privacy),
            )

        invested_points = self._points(rows, "invested", high)
        pessimistic_points = self._points(rows, "projected_pessimistic", high)
        base_points = self._points(rows, "projected_base", high)
        optimistic_points = self._points(rows, "projected_optimistic", high)
        scenario_fill = QColor(base_color)
        scenario_fill.setAlpha(35 if theme.dark else 23)
        painter.setPen(Qt.NoPen)
        painter.setBrush(scenario_fill)
        painter.drawPolygon(
            QPolygonF(optimistic_points + list(reversed(pessimistic_points)))
        )

        invested_pen = QPen(invested_color, 2.0, Qt.DotLine)
        invested_pen.setCapStyle(Qt.RoundCap)
        invested_pen.setJoinStyle(Qt.RoundJoin)
        painter.setPen(invested_pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawPath(self._line_path(invested_points))

        scenario_pens = (
            (pessimistic_points, QPen(pessimistic_color, 2.0, Qt.DashLine)),
            (base_points, QPen(base_color, 3.0)),
            (optimistic_points, QPen(optimistic_color, 2.0, Qt.DashLine)),
        )
        for points, pen in scenario_pens:
            pen.setCapStyle(Qt.RoundCap)
            pen.setJoinStyle(Qt.RoundJoin)
            painter.setPen(pen)
            painter.drawPath(self._line_path(points))

        for invested_point, pessimistic_point, base_point, optimistic_point in zip(
            invested_points, pessimistic_points, base_points, optimistic_points
        ):
            painter.setPen(QPen(_chart_bg(theme), 2))
            painter.setBrush(invested_color)
            painter.drawEllipse(invested_point, 3.7, 3.7)
            painter.setBrush(pessimistic_color)
            painter.drawEllipse(pessimistic_point, 3.8, 3.8)
            painter.setBrush(base_color)
            painter.drawEllipse(base_point, 5.0, 5.0)
            painter.setBrush(optimistic_color)
            painter.drawEllipse(optimistic_point, 3.8, 3.8)

        painter.setFont(QFont("Segoe UI", 8, QFont.DemiBold))
        for index, (row, point) in enumerate(zip(rows, base_points)):
            year = int(row["year"])
            painter.setPen(_chart_muted(theme))
            painter.drawText(
                QRectF(point.x() - 34.0, plot.bottom() + 8.0, 68.0, 19.0),
                Qt.AlignHCenter | Qt.AlignTop,
                f"{year} {'ano' if year == 1 else 'anos'}",
            )

            label = _percentage(safe_float(row.get("return_pct")), signed=True)
            badge = QRectF(
                max(plot.left(), min(point.x() - 29.0, plot.right() - 58.0)),
                max(37.0, point.y() - 31.0 - (index % 2) * 3.0),
                58.0,
                22.0,
            )
            label_color = (
                base_color
                if safe_float(row.get("return_pct")) >= 0
                else pessimistic_color
            )
            badge_fill = QColor(label_color)
            badge_fill.setAlpha(39 if theme.dark else 27)
            painter.setPen(QPen(label_color, 1))
            painter.setBrush(badge_fill)
            painter.drawRoundedRect(badge, 10.0, 10.0)
            painter.setPen(_chart_text(theme))
            painter.drawText(badge, Qt.AlignCenter, label)

        painter.setFont(QFont("Segoe UI", 8, QFont.DemiBold))
        legend_entries = [
            ("Contribuições reais", invested_pen),
            ("Pessimista", QPen(pessimistic_color, 2.0, Qt.DashLine)),
            ("Base", QPen(base_color, 3.0)),
            ("Otimista", QPen(optimistic_color, 2.0, Qt.DashLine)),
        ]
        # BUG-CHART-INVESTMENT-PROJECTION-COLLISION-009: the fixed 105px
        # advance overflowed narrow viewports and the labels collided with the
        # rate badge below.  Compute a compact step from the available width,
        # elide every label into its slot and stack the rate badge below the
        # legend only when there is room for it in the header band.
        legend_metrics = QFontMetrics(painter.font())
        available_w = plot.width()
        legend_count = len(legend_entries)
        compact = available_w < legend_count * 105.0
        legend_step = available_w / legend_count if compact else 105.0
        label_w = max(24.0, legend_step - 24.0 - 6.0)
        legend_x = plot.left()
        for label, pen in legend_entries:
            painter.setPen(pen)
            painter.drawLine(QPointF(legend_x, 19.0), QPointF(legend_x + 18.0, 19.0))
            painter.setPen(_chart_text(theme))
            elided = legend_metrics.elidedText(
                label, Qt.ElideRight, int(label_w)
            )
            painter.drawText(
                QRectF(legend_x + 24.0, 9.0, label_w, 20.0),
                Qt.AlignVCenter,
                elided,
            )
            legend_x += legend_step

        rate_badge_width = 228.0
        rate_badge = QRectF(
            max(plot.left(), plot.right() - rate_badge_width), 37.0, rate_badge_width, 25.0
        )
        if rate_badge.left() < plot.left() + 8.0:
            # Narrow viewport: keep the badge inside the plot area instead of
            # clipping the summary text.
            rate_badge.moveLeft(plot.left())
            rate_badge.setWidth(min(rate_badge_width, plot.width()))
        rate_fill = QColor(base_color)
        rate_fill.setAlpha(34)
        painter.setPen(QPen(base_color, 1))
        painter.setBrush(rate_fill)
        painter.drawRoundedRect(rate_badge, 11.0, 11.0)
        painter.setPen(_chart_text(theme))
        painter.drawText(
            rate_badge,
            Qt.AlignCenter,
            f"Taxas: {_percentage(self.annual_rate_pct - self.scenario_spread_pct)} · "
            f"{_percentage(self.annual_rate_pct)} · "
            f"{_percentage(self.annual_rate_pct + self.scenario_spread_pct)}",
        )

        if self.hover_index is not None and 0 <= self.hover_index < len(rows):
            x = base_points[self.hover_index].x()
            painter.setPen(QPen(_chart_grid(theme), 1, Qt.DashLine))
            painter.drawLine(QPointF(x, plot.top()), QPointF(x, plot.bottom()))
            self._draw_tooltip(painter, rows, base_points)
