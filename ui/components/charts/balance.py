import math
from datetime import date
from typing import Callable, Optional

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetrics,
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
    _chart_secondary,
    _chart_text,
    _chart_tooltip_bg,
)
from ui.core.theme import Theme
from utils.helpers import eur as _format_eur
from utils.helpers import parse_backup_datetime, safe_float
from utils.helpers import strict_float
from utils.app_logging import log_warning

PT_MONTHS = [
    "janeiro",
    "fevereiro",
    "março",
    "abril",
    "maio",
    "junho",
    "julho",
    "agosto",
    "setembro",
    "outubro",
    "novembro",
    "dezembro",
]


def _safe_chart_date(value) -> Optional[date]:
    if not value:
        return None
    if isinstance(value, date):
        return value
    try:
        if "T" in str(value):
            parsed = parse_backup_datetime(str(value))
            return parsed.date() if parsed is not None else None
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


from ui.components.charts.presentation import format_axis_money


def eur(value: float, privacy: bool = False) -> str:
    """Backward-compatible chart currency formatter."""
    return _format_eur(safe_float(value), privacy)


def _axis_eur(value: float, privacy: bool = False) -> str:
    return format_axis_money(value, privacy=privacy)


class BalanceChart(StableChart):
    supports_zoom = True

    def __init__(
        self, theme_provider: Callable[[], Theme], parent: Optional[QWidget] = None
    ):
        super().__init__(theme_provider, parent)
        self.points: list[dict] = []
        self.hover_index: Optional[int] = None
        self.invalid_points = 0
        self.quality_warnings: tuple[str, ...] = ()
        self.setMinimumHeight(280)
        self.setMouseTracking(True)
        self.setAccessibleName("Gráfico de evolução do saldo")
        self.update_accessible_description()

    def update_accessible_description(self) -> None:
        """Rebuild assistive text from scratch using current data, warnings, and privacy."""
        theme = self.theme_provider()
        privacy = bool(getattr(theme, "privacy", False))

        if not self.points:
            desc = (
                "Gráfico de evolução do saldo sem dados disponíveis. "
                "Usa Control e a roda para ampliar, Shift e a roda para navegar."
            )
        else:
            forecast_rows = [row for row in self.points if row.get("is_forecast")]
            historical_rows = [row for row in self.points if not row.get("is_forecast")]
            if forecast_rows:
                last = forecast_rows[-1]
                base_val = last.get("balance_base", last.get("balance", 0.0))
                pess_val = last.get("balance_pessimistic", last.get("balance", 0.0))
                opt_val = last.get("balance_optimistic", last.get("balance", 0.0))
                desc = (
                    "Previsão de saldo com cenário base "
                    f"{eur(base_val, privacy)}, entre "
                    f"{eur(pess_val, privacy)} e "
                    f"{eur(opt_val, privacy)}. "
                    "Control e roda ampliam; duplo clique repõe."
                )
            elif historical_rows:
                last = historical_rows[-1]
                last_bal = last.get("balance", 0.0)
                desc = (
                    f"Evolução histórica do saldo com {len(historical_rows)} ponto(s), "
                    f"saldo final de {eur(last_bal, privacy)}. "
                    "Control e roda ampliam; duplo clique repõe."
                )
            else:
                desc = (
                    "Gráfico de evolução do saldo. "
                    "Control e roda ampliam; duplo clique repõe."
                )

        if self.invalid_points:
            desc += f" {self.invalid_points} ponto(s) inválido(s) foram excluídos."
        if self.quality_warnings:
            clean_warnings = [
                str(w).strip() for w in self.quality_warnings if str(w).strip()
            ]
            if clean_warnings:
                desc += f" Aviso de qualidade: {' '.join(clean_warnings)}"

        self.setAccessibleDescription(desc)

    def invalidate_cache(self) -> None:
        self.update_accessible_description()
        super().invalidate_cache()

    def set_points(self, points: list[dict]) -> None:
        self.clear_period_hover()
        clean: list[dict] = []
        invalid_points = 0
        quality_warnings: list[str] = []
        for row in points or []:
            try:
                point_date = _safe_chart_date(row.get("date"))
                if point_date is None:
                    raise ValueError("data inválida")
                clean.append(
                    {
                        "date": point_date.isoformat(),
                        "balance": strict_float(row.get("balance"), field_name="saldo"),
                        "balance_base": strict_float(
                            row.get("balance_base", row.get("balance")),
                            field_name="saldo base",
                        ),
                        "balance_pessimistic": strict_float(
                            row.get("balance_pessimistic", row.get("balance")),
                            field_name="saldo pessimista",
                        ),
                        "balance_optimistic": strict_float(
                            row.get("balance_optimistic", row.get("balance")),
                            field_name="saldo otimista",
                        ),
                        "income": strict_float(
                            row.get("income", 0), field_name="entradas"
                        ),
                        "expense": strict_float(
                            row.get("expense", 0), field_name="saídas"
                        ),
                        "delta": strict_float(
                            row.get("delta", 0), field_name="variação"
                        ),
                        "resolution": row.get("resolution", "daily"),
                        "is_forecast": bool(row.get("is_forecast", False)),
                    }
                )
                for warning in row.get("quality_warnings", ()) or ():
                    text = str(warning or "").strip()
                    if text and text not in quality_warnings:
                        quality_warnings.append(text)
            except (AttributeError, TypeError, ValueError) as exc:
                invalid_points += 1
                if invalid_points == 1:
                    log_warning(
                        "O gráfico de saldo ignorou um ponto inválido: %s",
                        exc,
                    )
                continue
        self.invalid_points = invalid_points
        self.quality_warnings = tuple(quality_warnings)
        self.setProperty("invalidPointCount", invalid_points)
        self.setProperty("qualityWarningCount", len(quality_warnings))
        self.points = clean
        self.hover_index = None
        self.zoom_factor = 1.0
        self.pan_offset = 0.0
        self.update_accessible_description()
        self.updateGeometry()
        if self.points and self.isVisible():
            self.start_entrance_animation(650)
        else:
            self.invalidate_cache()

    def plot_rect(self) -> tuple[float, float, float, float]:
        width = max(1.0, float(self.width()))
        height = max(1.0, float(self.height()))
        left = 76.0 if width >= 460.0 else 58.0
        right = max(left + 40.0, width - 18.0)
        top = 34.0
        bottom = max(top + 48.0, height - 48.0)
        return left, top, right, bottom

    def zoom_viewport_width(self) -> float:
        left, _top, right, _bottom = self.plot_rect()
        return max(1.0, right - left)

    def zoom_anchor_x(self, event) -> float:
        left, _top, right, _bottom = self.plot_rect()
        return max(0.0, min(right - left, float(event.position().x()) - left))

    def _view_state(self) -> dict[str, float]:
        left, top, right, bottom = self.plot_rect()
        plot_width = max(1.0, right - left)
        zoom = max(self.minimum_zoom, min(self.maximum_zoom, self.zoom_factor))
        self.zoom_factor = zoom
        max_pan = plot_width * (zoom - 1.0)
        self.pan_offset = max(0.0, min(float(self.pan_offset), max_pan))
        virtual_width = plot_width * zoom

        count = len(self.points)
        if count <= 1:
            start_position = end_position = 0.0
        else:
            start_position = (self.pan_offset / virtual_width) * (count - 1)
            end_position = min(float(count - 1), start_position + (count - 1) / zoom)
        return {
            "left": left,
            "top": top,
            "right": right,
            "bottom": bottom,
            "plot_width": plot_width,
            "virtual_width": virtual_width,
            "start_position": start_position,
            "end_position": end_position,
        }

    def _point_x(self, index: int, state: dict[str, float]) -> float:
        if len(self.points) <= 1:
            return state["left"] + state["plot_width"] / 2.0
        return (
            state["left"]
            + index / (len(self.points) - 1) * state["virtual_width"]
            - self.pan_offset
        )

    def _visible_bounds(
        self, state: dict[str, float], padding: int = 0
    ) -> tuple[int, int]:
        if not self.points:
            return 0, -1
        low = max(0, int(math.floor(state["start_position"])) - padding)
        high = min(
            len(self.points) - 1,
            int(math.ceil(state["end_position"])) + padding,
        )
        return low, high

    def mouseMoveEvent(self, event):  # type: ignore[override]
        if not self.points:
            super().mouseMoveEvent(event)
            return
        state = self._view_state()
        pos = event.position()
        inside = (
            state["left"] <= pos.x() <= state["right"]
            and state["top"] <= pos.y() <= state["bottom"]
        )
        hovered: Optional[int] = None
        if inside:
            content_x = pos.x() - state["left"] + self.pan_offset
            ratio = max(0.0, min(1.0, content_x / state["virtual_width"]))
            hovered = int(round(ratio * (len(self.points) - 1)))
            hovered = max(0, min(len(self.points) - 1, hovered))
        if hovered != self.hover_index:
            self.hover_index = hovered
            self.emit_period_for_hover(hovered)
            self.invalidate_cache()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):  # type: ignore[override]
        if self.hover_index is not None:
            self.hover_index = None
            self.emit_period_for_hover(None)
            self.invalidate_cache()
        super().leaveEvent(event)

    @staticmethod
    def _date_label(row: dict) -> str:
        current = _safe_chart_date(row.get("date"))
        if current is None:
            return "Data inválida"
        resolution = row.get("resolution", "daily")
        if resolution == "monthly":
            return f"{PT_MONTHS[current.month - 1].capitalize()} {current.year}"
        if resolution == "weekly":
            return f"Semana {current.isocalendar()[1]}, {current.year}"
        return f"{current.day} {PT_MONTHS[current.month - 1][:3]} {current.year}"

    def _label_indices(self, state: dict[str, float]) -> list[int]:
        if not self.points:
            return []
        start = state["start_position"]
        end = state["end_position"]
        visible_count = max(1, int(math.ceil(end) - math.floor(start) + 1))
        label_count = min(6, visible_count)
        if label_count <= 1 or math.isclose(start, end):
            return [max(0, min(len(self.points) - 1, int(round(start))))]
        indices = {
            max(
                0,
                min(
                    len(self.points) - 1,
                    int(round(start + (end - start) * index / (label_count - 1))),
                ),
            )
            for index in range(label_count)
        }
        return sorted(indices)

    def _draw_zoom_hint(self, painter: QPainter, state: dict[str, float]) -> None:
        theme = self.theme_provider()
        zoomed = self.zoom_factor > 1.01
        if zoomed and self.width() >= 560:
            text = (
                f"{self.zoom_factor:.1f}×  ·  Shift+roda navega  ·  duplo clique repõe"
            )
        elif zoomed:
            text = f"{self.zoom_factor:.1f}×  ·  duplo clique repõe"
        else:
            text = "Ctrl+roda: zoom"

        font = QFont("Segoe UI", 8, QFont.Bold if zoomed else QFont.Normal)
        metrics = QFontMetrics(font)
        width = min(
            max(84, metrics.horizontalAdvance(text) + 18),
            max(84, int(self.width() - state["left"] - 18)),
        )
        rect = QRectF(self.width() - width - 10, 7, width, 20)
        fill = _chart_grid(theme)
        fill.setAlpha(74 if theme.dark else 52)
        painter.setPen(QPen(_chart_grid(theme), 1))
        painter.setBrush(fill)
        painter.drawRoundedRect(rect, 10, 10)
        painter.setPen(_chart_accent(theme) if zoomed else _chart_muted(theme))
        painter.setFont(font)
        painter.drawText(rect.adjusted(8, 0, -8, 0), Qt.AlignCenter, text)

    def _draw_tooltip(
        self,
        painter: QPainter,
        point: QPointF,
        row: dict,
        state: dict[str, float],
    ) -> None:
        theme = self.theme_provider()
        tooltip_width = min(286.0, max(190.0, self.width() - 24.0))
        tooltip_height = 164.0 if row.get("is_forecast") else 140.0
        x = point.x() + 14.0
        x = max(8.0, min(x, self.width() - tooltip_width - 8.0))
        y = point.y() - tooltip_height - 12.0
        if y < 8.0:
            y = point.y() + 14.0
        y = max(8.0, min(y, self.height() - tooltip_height - 8.0))
        rect = QRectF(x, y, tooltip_width, tooltip_height)

        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(0, 0, 0, 42 if theme.dark else 28))
        painter.drawRoundedRect(rect.translated(0, 4), 14, 14)
        painter.setPen(QPen(_chart_grid(theme), 1))
        painter.setBrush(_chart_tooltip_bg(theme))
        painter.drawRoundedRect(rect, 14, 14)

        title = self._date_label(row)
        painter.setPen(_chart_text(theme))
        painter.setFont(QFont("Segoe UI", 10, QFont.Bold))
        # BUG-CHART-BALANCE-TOOLTIP-BADGE-COLLISION-008: when the row is a
        # forecast the "PREVISÃO" badge occupies the right edge of the header
        # row; the title must stop before it (badge 64px + margins) instead of
        # using the full tooltip width and colliding with the badge.
        badge_reserve = 78.0 if row.get("is_forecast") else 0.0
        title_metrics = QFontMetrics(painter.font())
        title_width = max(0.0, tooltip_width - 28.0 - badge_reserve)
        elided_title = title_metrics.elidedText(
            title, Qt.ElideRight, int(title_width)
        )
        painter.drawText(
            QRectF(x + 14, y + 8, title_width, 23),
            Qt.AlignLeft | Qt.AlignVCenter,
            elided_title,
        )

        if row.get("is_forecast"):
            badge = QRectF(x + tooltip_width - 78, y + 10, 64, 18)
            forecast = _chart_secondary(theme)
            badge_fill = QColor(forecast)
            badge_fill.setAlpha(38)
            painter.setPen(Qt.NoPen)
            painter.setBrush(badge_fill)
            painter.drawRoundedRect(badge, 9, 9)
            painter.setPen(forecast)
            painter.setFont(QFont("Segoe UI", 7, QFont.Bold))
            painter.drawText(badge, Qt.AlignCenter, "PREVISÃO")

        delta = safe_float(row.get("delta"))
        income = safe_float(row.get("income"))
        expense = safe_float(row.get("expense"))
        lines = [
            (
                "Saldo base",
                eur(row.get("balance", 0), theme.privacy),
                _chart_text(theme),
            ),
        ]
        if row.get("is_forecast"):
            lines.append(
                (
                    "Faixa",
                    f"{eur(row.get('balance_pessimistic', row.get('balance')), theme.privacy)} — "
                    f"{eur(row.get('balance_optimistic', row.get('balance')), theme.privacy)}",
                    _chart_secondary(theme),
                )
            )
        lines.extend(
            [
                (
                    "Variação",
                    eur(delta, theme.privacy),
                    _chart_positive(theme) if delta >= 0 else _chart_negative(theme),
                ),
                ("Entradas", eur(income, theme.privacy), _chart_positive(theme)),
                ("Saídas", eur(expense, theme.privacy), _chart_negative(theme)),
            ]
        )
        painter.setFont(QFont("Segoe UI", 8))
        for index, (label, value, color) in enumerate(lines):
            line_y = y + 36 + index * 24
            painter.setPen(_chart_muted(theme))
            painter.drawText(
                QRectF(x + 14, line_y, 78, 20),
                Qt.AlignLeft | Qt.AlignVCenter,
                label,
            )
            painter.setPen(color)
            painter.setFont(QFont("Segoe UI", 8, QFont.Bold))
            painter.drawText(
                QRectF(x + 92, line_y, tooltip_width - 106, 20),
                Qt.AlignRight | Qt.AlignVCenter,
                value,
            )
            painter.setFont(QFont("Segoe UI", 8))

    def render_chart(self, painter: QPainter) -> None:
        self.update_accessible_description()
        theme = self.theme_provider()
        painter.fillRect(self.rect(), _chart_bg(theme))
        if not self.points:
            self.draw_empty(painter, "Ainda sem evolução do saldo")
            return

        state = self._view_state()
        left = state["left"]
        top = state["top"]
        right = state["right"]
        bottom = state["bottom"]
        if right <= left or bottom <= top:
            return

        scale_low, scale_high = self._visible_bounds(state)
        visible_values = [
            safe_float(self.points[index].get(key, self.points[index].get("balance")))
            for index in range(scale_low, scale_high + 1)
            for key in (
                "balance_pessimistic",
                "balance_base",
                "balance_optimistic",
            )
        ]
        if not visible_values:
            self.draw_empty(painter, "Sem dados suficientes")
            return

        minimum, maximum = min(visible_values), max(visible_values)
        if math.isclose(minimum, maximum):
            padding = max(1.0, abs(maximum) * 0.08)
        else:
            padding = max(1.0, (maximum - minimum) * 0.12)
        minimum -= padding
        maximum += padding

        def point_for(index: int, key: str = "balance") -> QPointF:
            value = safe_float(
                self.points[index].get(key, self.points[index].get("balance"))
            )
            x = self._point_x(index, state)
            y = bottom - (bottom - top) * (
                (value - minimum) / max(0.000001, maximum - minimum)
            )
            return QPointF(float(x), float(y))

        label_indices = self._label_indices(state)
        painter.setFont(QFont("Segoe UI", 8))
        for grid_index in range(5):
            y = top + (bottom - top) * grid_index / 4.0
            value = maximum - (maximum - minimum) * grid_index / 4.0
            painter.setPen(QPen(_chart_grid(theme), 1))
            painter.drawLine(int(left), int(y), int(right), int(y))
            painter.setPen(_chart_muted(theme))
            painter.drawText(
                QRectF(4, y - 10, max(1.0, left - 12), 20),
                Qt.AlignRight | Qt.AlignVCenter,
                _axis_eur(value, theme.privacy),
            )

        for index in label_indices:
            x = self._point_x(index, state)
            painter.setPen(QPen(_chart_grid(theme), 1))
            painter.drawLine(int(x), int(top), int(x), int(bottom))
            current = _safe_chart_date(self.points[index].get("date"))
            label = (
                f"{current.day} {PT_MONTHS[current.month - 1][:3]}"
                if current is not None
                else "Data inválida"
            )
            painter.setPen(_chart_muted(theme))
            painter.drawText(
                QRectF(x - 36, bottom + 6, 72, 22),
                Qt.AlignHCenter | Qt.AlignTop,
                label,
            )

        if minimum < 0 < maximum:
            zero_y = bottom - (bottom - top) * ((0 - minimum) / (maximum - minimum))
            zero_pen = QPen(_chart_muted(theme), 1, Qt.DashLine)
            painter.setPen(zero_pen)
            painter.drawLine(int(left), int(zero_y), int(right), int(zero_y))

        draw_low, draw_high = self._visible_bounds(state, padding=1)
        coordinates = {
            index: point_for(index) for index in range(draw_low, draw_high + 1)
        }
        fill_path = QPainterPath()
        historical_path = QPainterPath()
        forecast_path = QPainterPath()
        first_point = coordinates[draw_low]
        fill_path.moveTo(QPointF(first_point.x(), bottom))
        fill_path.lineTo(first_point)

        previous = first_point
        for index in range(draw_low + 1, draw_high + 1):
            current = coordinates[index]
            smooth = (current.x() - previous.x()) * 0.42
            control_1 = QPointF(previous.x() + smooth, previous.y())
            control_2 = QPointF(current.x() - smooth, current.y())
            fill_path.cubicTo(control_1, control_2, current)
            target = (
                forecast_path
                if self.points[index].get("is_forecast", False)
                else historical_path
            )
            target.moveTo(previous)
            target.cubicTo(control_1, control_2, current)
            previous = current

        fill_path.lineTo(QPointF(previous.x(), bottom))
        fill_path.closeSubpath()

        forecast_indices = [
            index
            for index in range(draw_low, draw_high + 1)
            if self.points[index].get("is_forecast")
        ]
        scenario_band = QPainterPath()
        scenario_edges: list[QPainterPath] = []
        if forecast_indices:
            first_forecast = forecast_indices[0]
            band_indices = list(forecast_indices)
            if first_forecast > 0 and first_forecast - 1 >= draw_low:
                band_indices.insert(0, first_forecast - 1)
            optimistic_points = [
                point_for(index, "balance_optimistic") for index in band_indices
            ]
            pessimistic_points = [
                point_for(index, "balance_pessimistic") for index in band_indices
            ]
            scenario_band.moveTo(optimistic_points[0])
            for point in optimistic_points[1:]:
                scenario_band.lineTo(point)
            for point in reversed(pessimistic_points):
                scenario_band.lineTo(point)
            scenario_band.closeSubpath()
            for edge_points in (optimistic_points, pessimistic_points):
                edge = QPainterPath(edge_points[0])
                for point in edge_points[1:]:
                    edge.lineTo(point)
                scenario_edges.append(edge)

        plot_rect = QRectF(left, top, right - left, bottom - top)
        painter.save()
        painter.setClipRect(plot_rect)
        if self.anim_progress < 1.0:
            reveal = QRectF(
                left,
                top,
                (right - left) * max(0.0, self.anim_progress),
                bottom - top,
            )
            painter.setClipRect(reveal, Qt.IntersectClip)

        accent = _chart_accent(theme)
        if not scenario_band.isEmpty():
            band_color = QColor(_chart_secondary(theme))
            band_color.setAlpha(44 if theme.dark else 30)
            painter.setPen(Qt.NoPen)
            painter.setBrush(band_color)
            painter.drawPath(scenario_band)
            edge_pen = QPen(_chart_secondary(theme), 1.0, Qt.DotLine)
            painter.setPen(edge_pen)
            painter.setBrush(Qt.NoBrush)
            for edge in scenario_edges:
                painter.drawPath(edge)
        gradient = QLinearGradient(0, top, 0, bottom)
        fill_top = QColor(accent)
        fill_top.setAlpha(88 if theme.dark else 70)
        fill_bottom = QColor(accent)
        fill_bottom.setAlpha(2)
        gradient.setColorAt(0, fill_top)
        gradient.setColorAt(1, fill_bottom)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(gradient))
        painter.drawPath(fill_path)

        line_pen = QPen(accent, 2.6)
        line_pen.setJoinStyle(Qt.RoundJoin)
        line_pen.setCapStyle(Qt.RoundCap)
        painter.setPen(line_pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawPath(historical_path)

        forecast_color = _chart_secondary(theme)
        forecast_pen = QPen(forecast_color, 2.4, Qt.DashLine)
        forecast_pen.setJoinStyle(Qt.RoundJoin)
        forecast_pen.setCapStyle(Qt.RoundCap)
        painter.setPen(forecast_pen)
        painter.drawPath(forecast_path)

        last_visible = max(
            0,
            min(len(self.points) - 1, int(math.floor(state["end_position"]))),
        )
        last_point = point_for(last_visible)
        last_row = self.points[last_visible]
        marker_color = (
            forecast_color
            if last_row.get("is_forecast")
            else _chart_positive(theme)
            if safe_float(last_row.get("delta")) >= 0
            else _chart_negative(theme)
        )
        painter.setPen(QPen(_chart_bg(theme), 2))
        painter.setBrush(marker_color)
        painter.drawEllipse(last_point, 5, 5)

        hover_point: Optional[QPointF] = None
        hover_row: Optional[dict] = None
        if self.hover_index is not None and 0 <= self.hover_index < len(self.points):
            hover_point = point_for(self.hover_index)
            if left - 2 <= hover_point.x() <= right + 2:
                hover_row = self.points[self.hover_index]
                painter.setPen(
                    QPen(
                        QColor(accent.red(), accent.green(), accent.blue(), 135),
                        1,
                        Qt.DashLine,
                    )
                )
                painter.drawLine(
                    int(hover_point.x()), int(top), int(hover_point.x()), int(bottom)
                )
                painter.setPen(QPen(accent, 3))
                painter.setBrush(_chart_tooltip_bg(theme))
                painter.drawEllipse(hover_point, 7, 7)
            else:
                hover_point = None
        painter.restore()

        self._draw_zoom_hint(painter, state)
        if hover_point is not None and hover_row is not None:
            self._draw_tooltip(painter, hover_point, hover_row, state)
