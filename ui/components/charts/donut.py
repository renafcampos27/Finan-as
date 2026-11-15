import math
from typing import Any, Callable, Optional

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import QWidget

from ui.components.charts.base import StableChart
from ui.components.charts.utils import (
    _chart_bg,
    _chart_grid,
    _chart_muted,
    _chart_text,
    _chart_tooltip_bg,
    category_color,
)
from ui.core.theme import Theme
from utils.helpers import eur, percent, safe_float


class DonutChart(StableChart):
    category_clicked = Signal(str)

    BASE_MINIMUM_HEIGHT = 230
    DASHBOARD_HEIGHT_SCALE = 1.20

    # High-contrast, colour-blind-friendlier categorical palette. Assignment is
    # retained by category while this chart is open, even if its ranking changes.
    DISTINCT_PALETTE = (
        "#0072B2",  # blue
        "#E69F00",  # orange
        "#CC79A7",  # reddish purple
        "#009E73",  # blue-green
        "#D55E00",  # vermillion
        "#56B4E9",  # sky blue
        "#D6B800",  # accessible yellow/gold
        "#6F42C1",  # violet
    )
    OTHER_COLOR = "#64748B"

    def __init__(
        self, theme_provider: Callable[[], Theme], parent: Optional[QWidget] = None
    ):
        super().__init__(theme_provider, parent)
        self.data: list[tuple[str, float, str]] = []
        self.hover_index: Optional[int] = None
        self._hover_pos = QPointF()
        self._slice_ranges: list[tuple[float, float]] = []
        self._legend_hit_rects: list[tuple[int, QRectF]] = []
        self._legend_label_rects: list[tuple[str, QRectF]] = []
        self._legend_amount_rects: list[QRectF] = []
        self._legend_percent_rects: list[QRectF] = []
        self._category_colors: dict[str, str] = {}
        self._source_data: list[tuple[str, float, str]] = []
        self._detail_items: list[dict[str, Any]] = []
        self._comparison_values: dict[str, float] = {}
        self.comparison_label = ""
        self.category_limit: int | None = 8
        self.empty_message: str = "Sem despesas neste período"
        self._center_label_rect = QRectF()
        self._center_amount_rect = QRectF()
        self._synthetic_label: Optional[str] = None
        self.setMinimumHeight(self.BASE_MINIMUM_HEIGHT)
        self.setAccessibleName("Gráfico de distribuição por categoria")

    @staticmethod
    def _valid_color(value: str) -> QColor:
        color = QColor(str(value or "#94A3B8"))
        return color if color.isValid() else QColor("#94A3B8")

    def _compact_data(
        self,
        rows: list[tuple[str, float, str]],
    ) -> list[tuple[str, float, str]]:
        ordered = sorted(rows, key=lambda item: item[1], reverse=True)
        if self.category_limit is None or len(ordered) <= self.category_limit:
            self._synthetic_label = None
            return ordered
        visible = ordered[: self.category_limit]
        remainder = ordered[self.category_limit :]
        other_total = sum(value for _label, value, _color in remainder)
        if other_total > 0:
            synth = f"Outros ({len(remainder)})"
            self._synthetic_label = synth
            visible.append(
                (synth, other_total, DonutChart.OTHER_COLOR)
            )
        else:
            self._synthetic_label = None
        return visible

    def _with_distinct_colors(
        self,
        rows: list[tuple[str, float, str]],
    ) -> list[tuple[str, float, str]]:
        """Keep visible colours distinct and stable while this chart is open."""
        result: list[tuple[str, float, str]] = []
        for label, value, _color in rows:
            if self._synthetic_label is not None and label == self._synthetic_label:
                result.append((label, value, self.OTHER_COLOR))
                continue
            chosen = category_color(label)
            self._category_colors[label] = chosen
            result.append((label, value, chosen))
        return result

    def set_category_limit(self, limit: int | None) -> None:
        normalized = None if limit is None else max(1, int(limit))
        if normalized == self.category_limit:
            return
        self.category_limit = normalized
        if self._source_data:
            self.set_data(self._source_data)

    def set_data(self, data: list[Any]) -> None:
        self._source_data = list(data or [])
        grouped: dict[str, dict[str, Any]] = {}
        for row in data or []:
            if isinstance(row, dict):
                label = row.get("label") or row.get("name")
                value = row.get("value", row.get("amount", 0))
                color = row.get("color", "#94A3B8")
                detail = dict(row)
            else:
                try:
                    label, value, color = row[:3]
                except (TypeError, ValueError):
                    continue
                detail = {
                    "label": label,
                    "value": value,
                    "color": color,
                }
                if len(row) > 3:
                    detail["id"] = row[3]
            name = str(label or "Sem categoria")
            amount = abs(safe_float(value))
            if amount <= 0:
                continue
            identity = (
                f"id:{detail.get('id')}"
                if detail.get("id") not in (None, "")
                else f"label:{name}"
            )
            previous = grouped.get(identity)
            if previous is None:
                detail.update(
                    {
                        "label": name,
                        "value": amount,
                        "color": str(color or "#94A3B8"),
                    }
                )
                grouped[identity] = detail
            else:
                previous["value"] = safe_float(previous.get("value")) + amount

        ordered = sorted(
            grouped.values(),
            key=lambda item: safe_float(item.get("value")),
            reverse=True,
        )
        if self.category_limit is not None and len(ordered) > self.category_limit:
            remainder = ordered[self.category_limit :]
            ordered = ordered[: self.category_limit]
            synth = f"Outros ({len(remainder)})"
            self._synthetic_label = synth
            ordered.append(
                {
                    "label": synth,
                    "value": sum(safe_float(item.get("value")) for item in remainder),
                    "color": self.OTHER_COLOR,
                    "grouped_items": [dict(item) for item in remainder],
                    "is_aggregate": True,
                }
            )
        else:
            self._synthetic_label = None
        clean = [
            (
                str(item.get("label") or "Sem categoria"),
                safe_float(item.get("value")),
                str(item.get("color") or "#94A3B8"),
            )
            for item in ordered
        ]
        self.data = self._with_distinct_colors(clean)
        self._detail_items = []
        for item, (_label, value, color) in zip(ordered, self.data):
            detail = dict(item)
            detail["value"] = value
            detail["color"] = color
            self._detail_items.append(detail)
        self.hover_index = None
        self._slice_ranges.clear()
        self._legend_hit_rects.clear()
        self.updateGeometry()
        self._update_accessible_description()
        if self.data and self.isVisible():
            self.start_entrance_animation(650)
        else:
            self.invalidate_cache()

    def _update_accessible_description(self) -> None:
        if self.data:
            total = sum(value for _label, value, _color in self.data)
            theme = self.theme_provider()
            details = "; ".join(
                f"{label}: {eur(value, theme.privacy)}, "
                f"{percent(value / total * 100 if total else 0, theme.privacy)}"
                for label, value, _color in self.data
            )
            self.setAccessibleDescription(details)
        else:
            self.setAccessibleDescription("Sem dados")

    def invalidate_cache(self) -> None:
        self._update_accessible_description()
        super().invalidate_cache()

    def _current_detail_item(self) -> dict[str, Any] | None:
        if self.hover_index is not None and 0 <= self.hover_index < len(
            self._detail_items
        ):
            return dict(self._detail_items[self.hover_index])
        return super()._current_detail_item()

    def set_comparison_data(
        self,
        data: list[tuple[str, float, str]],
        label: str = "",
    ) -> None:
        grouped: dict[str, float] = {}
        for row in data or []:
            try:
                name, value, _color = row
            except (TypeError, ValueError):
                continue
            category = str(name or "Sem categoria")
            grouped[category] = grouped.get(category, 0.0) + abs(safe_float(value))
        self._comparison_values = grouped
        self.comparison_label = str(label or "período de referência")
        self.invalidate_cache()

    def _comparison_value(self, label: str) -> Optional[float]:
        if not self._comparison_values:
            return None
        if self._synthetic_label is not None and label == self._synthetic_label:
            visible = {
                name
                for name, _value, _color in self.data
                if name != self._synthetic_label
            }
            return sum(
                value
                for name, value in self._comparison_values.items()
                if name not in visible
            )
        return self._comparison_values.get(str(label), 0.0)

    def comparison_total(self) -> float:
        """Total of all categories in the reference period."""
        return sum(max(0.0, safe_float(v)) for v in self._comparison_values.values())

    def comparison_share(self, label: str) -> Optional[float]:
        """Proportion (0.0 to 1.0) that ``label`` represents of the entire reference period."""
        total = self.comparison_total()
        if total <= 0:
            return None
        val = self._comparison_value(label)
        if val is None:
            return None
        return max(0.0, safe_float(val)) / total

    def _geometry(self) -> tuple[QRectF, Optional[QRectF], float, float, float]:
        width = max(1.0, float(self.width()))
        height = max(1.0, float(self.height()))
        margin = 6.0
        # Dashboard cards are about 410 px wide in the three-column layout.
        # That is still enough for a compact legend and must not hide the
        # permanently visible amount/percentage requested by the user.
        wide = width >= 380.0

        if wide:
            size = min(height - margin * 2, width * 0.42, width - 205.0)
            size = max(96.0, size)
            container = QRectF(margin, (height - size) / 2.0, size, size)
            legend_left = container.right() + 22.0
            legend = QRectF(
                legend_left,
                margin,
                max(1.0, width - legend_left - margin),
                max(1.0, height - margin * 2),
            )
            if legend.width() < 150.0:
                wide = False

        if not wide:
            size = max(88.0, min(width - margin * 2, height - margin * 2))
            container = QRectF(
                (width - size) / 2.0,
                (height - size) / 2.0,
                size,
                size,
            )
            legend = None

        stroke = max(16.0, min(30.0, size * 0.13))
        inset = stroke / 2.0 + 4.0
        arc_rect = container.adjusted(inset, inset, -inset, -inset)
        outer_radius = arc_rect.width() / 2.0 + stroke / 2.0
        inner_radius = max(1.0, arc_rect.width() / 2.0 - stroke / 2.0)
        return arc_rect, legend, stroke, inner_radius, outer_radius

    def _legend_rows(self, legend: Optional[QRectF]) -> list[tuple[int, QRectF]]:
        if legend is None or not self.data:
            return []
        two_lines = self._legend_uses_two_lines(legend)
        minimum_height, maximum_height = (34.0, 42.0) if two_lines else (18.0, 25.0)
        row_height = max(
            minimum_height,
            min(maximum_height, legend.height() / len(self.data)),
        )
        content_height = row_height * len(self.data)
        top = legend.top() + max(0.0, (legend.height() - content_height) / 2.0)
        return [
            (
                index,
                QRectF(
                    legend.left(), top + index * row_height, legend.width(), row_height
                ),
            )
            for index in range(len(self.data))
        ]

    def _legend_uses_two_lines(self, legend: Optional[QRectF]) -> bool:
        return bool(
            legend is not None
            and self.data
            and legend.width() < 360.0
            and legend.height() / len(self.data) >= 34.0
        )

    def _prepare_hit_regions(self) -> None:
        total = sum(value for _label, value, _color in self.data)
        self._slice_ranges = []
        start = 0.0
        if total > 0:
            for _label, value, _color in self.data:
                span = value / total * 360.0
                self._slice_ranges.append((start, span))
                start += span
        _arc, legend, _stroke, _inner, _outer = self._geometry()
        self._legend_hit_rects = self._legend_rows(legend)

    def _hit_test(self, pos: QPointF) -> Optional[int]:
        self._prepare_hit_regions()
        for index, rect in self._legend_hit_rects:
            if rect.contains(pos):
                return index

        arc_rect, _legend, _stroke, inner_radius, outer_radius = self._geometry()
        center = arc_rect.center()
        dx = pos.x() - center.x()
        dy = pos.y() - center.y()
        distance = math.hypot(dx, dy)
        if not (inner_radius - 3.0 <= distance <= outer_radius + 4.0):
            return None

        clockwise_from_top = (math.degrees(math.atan2(dx, -dy)) + 360.0) % 360.0
        for index, (start, span) in enumerate(self._slice_ranges):
            if start <= clockwise_from_top < start + span:
                return index
        if self._slice_ranges and math.isclose(clockwise_from_top, 360.0):
            return 0
        return None

    def mouseMoveEvent(self, event):  # type: ignore[override]
        hovered = self._hit_test(event.position()) if self.data else None
        self._hover_pos = event.position()
        if hovered != self.hover_index:
            self.hover_index = hovered
            self.setCursor(
                Qt.PointingHandCursor if hovered is not None else Qt.ArrowCursor
            )
            self.category_hovered.emit(
                self.data[hovered][0]
                if hovered is not None and 0 <= hovered < len(self.data)
                else ""
            )
            self.invalidate_cache()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):  # type: ignore[override]
        if self.hover_index is not None:
            self.hover_index = None
            self.invalidate_cache()
        self.category_hovered.emit("")
        self.setCursor(Qt.ArrowCursor)
        super().leaveEvent(event)

    def mousePressEvent(self, event):  # type: ignore[override]
        if event.button() == Qt.LeftButton and self.data:
            index = self._hit_test(event.position())
            if index is not None and 0 <= index < len(self.data):
                label = str(self.data[index][0])
                detail_item = (
                    self._detail_items[index]
                    if index < len(self._detail_items)
                    else self.data[index]
                )
                if self.open_detail(
                    detail_item,
                    label=label,
                    action_label=self._detail_action_label,
                ):
                    self.category_clicked.emit(label)
                event.accept()
                return
        super().mousePressEvent(event)

    def _draw_center(
        self,
        painter: QPainter,
        arc_rect: QRectF,
        stroke: float,
        total: float,
    ) -> None:
        theme = self.theme_provider()
        center = arc_rect.center()
        maximum_hole = max(36.0, arc_rect.width() - stroke - 8.0)
        diameter = min(maximum_hole, max(40.0, arc_rect.width() * 0.68))
        hole = QRectF(
            center.x() - diameter / 2.0,
            center.y() - diameter / 2.0,
            diameter,
            diameter,
        )
        painter.setPen(QPen(_chart_grid(theme), 1))
        hole_color = _chart_bg(theme)
        hole_color.setAlpha(244 if theme.dark else 238)
        painter.setBrush(hole_color)
        painter.drawEllipse(hole)

        compact = diameter < 66.0
        label_height = 13.0 if compact else 16.0
        amount_height = 16.0 if compact else 22.0
        vertical_gap = 4.0 if compact else 7.0
        block_height = label_height + vertical_gap + amount_height
        block_top = center.y() - block_height / 2.0
        self._center_label_rect = QRectF(
            hole.left() + 5.0,
            block_top,
            max(1.0, hole.width() - 10.0),
            label_height,
        )
        self._center_amount_rect = QRectF(
            hole.left() + 5.0,
            self._center_label_rect.bottom() + vertical_gap,
            max(1.0, hole.width() - 10.0),
            amount_height,
        )

        painter.setPen(_chart_muted(theme))
        painter.setFont(QFont("Segoe UI", 7 if compact else 8, QFont.Bold))
        painter.drawText(self._center_label_rect, Qt.AlignCenter, "TOTAL")

        amount_font = QFont(
            "Segoe UI",
            8 if compact else 9 if diameter < 95 else 10,
            QFont.Bold,
        )
        painter.setFont(amount_font)
        painter.setPen(_chart_text(theme))
        amount = eur(total, theme.privacy)
        amount = QFontMetrics(amount_font).elidedText(
            amount,
            Qt.ElideRight,
            max(1, int(self._center_amount_rect.width())),
        )
        painter.drawText(self._center_amount_rect, Qt.AlignCenter, amount)

    def _draw_legend(
        self, painter: QPainter, legend: Optional[QRectF], total: float
    ) -> None:
        theme = self.theme_provider()
        rows = self._legend_rows(legend)
        two_lines = self._legend_uses_two_lines(legend)
        self._legend_hit_rects = rows
        self._legend_label_rects = []
        self._legend_amount_rects = []
        self._legend_percent_rects = []
        label_font = QFont("Segoe UI", 9)
        value_font = QFont("Segoe UI", 8, QFont.Bold)
        value_metrics = QFontMetrics(value_font)
        formatted_values = [
            (
                eur(value, theme.privacy),
                percent(value / total * 100 if total else 0, theme.privacy),
            )
            for _label, value, _color in self.data
        ]
        row_width = rows[0][1].width() if rows else 1.0
        percentage_column_width = max(
            38.0,
            max(
                (
                    value_metrics.horizontalAdvance(pct)
                    for _amount, pct in formatted_values
                ),
                default=0,
            )
            + 6.0,
        )
        desired_amount_width = max(
            46.0,
            max(
                (
                    value_metrics.horizontalAdvance(amount)
                    for amount, _pct in formatted_values
                ),
                default=0,
            )
            + 6.0,
        )
        minimum_label_space = 18.0 if two_lines else 72.0
        available_amount_width = max(
            34.0,
            row_width - percentage_column_width - 8.0 - minimum_label_space,
        )
        amount_column_width = min(desired_amount_width, available_amount_width)

        for index, row_rect in rows:
            label, value, color_value = self.data[index]
            color = self._valid_color(color_value)
            focused = self.category_is_highlighted(label)
            active = self.hover_index == index or focused
            if active:
                highlight = _chart_grid(theme)
                highlight.setAlpha(68 if theme.dark else 48)
                painter.setPen(Qt.NoPen)
                painter.setBrush(highlight)
                painter.drawRoundedRect(row_rect.adjusted(-5, 1, 1, -1), 7, 7)

            label_line = (
                QRectF(
                    row_rect.left(),
                    row_rect.top(),
                    row_rect.width(),
                    row_rect.height() * 0.52,
                )
                if two_lines
                else row_rect
            )
            detail_line = (
                QRectF(
                    row_rect.left(),
                    label_line.bottom(),
                    row_rect.width(),
                    max(1.0, row_rect.bottom() - label_line.bottom()),
                )
                if two_lines
                else row_rect
            )
            marker_size = min(10.0, label_line.height() * 0.48)
            marker = QRectF(
                row_rect.left(),
                label_line.center().y() - marker_size / 2.0,
                marker_size,
                marker_size,
            )
            painter.setPen(Qt.NoPen)
            marker_color = color.lighter(115) if active else QColor(color)
            if self.highlighted_category and not focused:
                marker_color.setAlpha(72)
            painter.setBrush(marker_color)
            painter.drawEllipse(marker)

            label_left = marker.right() + 8.0
            amount_text, pct_text = formatted_values[index]
            painter.setFont(value_font)
            if two_lines:
                amount_rect = QRectF(
                    label_left,
                    detail_line.top(),
                    amount_column_width,
                    detail_line.height(),
                )
                pct_rect = QRectF(
                    amount_rect.right() + 8.0,
                    detail_line.top(),
                    percentage_column_width,
                    detail_line.height(),
                )
                value_alignment = Qt.AlignLeft | Qt.AlignVCenter
            else:
                pct_rect = QRectF(
                    row_rect.right() - percentage_column_width,
                    detail_line.top(),
                    percentage_column_width,
                    detail_line.height(),
                )
                amount_rect = QRectF(
                    pct_rect.left() - amount_column_width - 8.0,
                    detail_line.top(),
                    amount_column_width,
                    detail_line.height(),
                )
                value_alignment = Qt.AlignRight | Qt.AlignVCenter

            self._legend_percent_rects.append(pct_rect)
            painter.setPen(_chart_muted(theme))
            painter.drawText(pct_rect, value_alignment, pct_text)

            amount_text = value_metrics.elidedText(
                amount_text,
                Qt.ElideRight,
                max(1, int(amount_column_width - 4.0)),
            )
            self._legend_amount_rects.append(amount_rect)
            painter.setPen(_chart_text(theme))
            painter.drawText(
                amount_rect,
                value_alignment,
                amount_text,
            )

            label_width = max(
                8,
                int(
                    row_rect.right() - label_left - 4.0
                    if two_lines
                    else amount_rect.left() - label_left - 8.0
                ),
            )
            painter.setFont(label_font)
            label_text = QFontMetrics(label_font).elidedText(
                str(label), Qt.ElideRight, label_width
            )
            painter.setPen(_chart_text(theme))
            label_rect = QRectF(
                label_left,
                label_line.top(),
                label_width,
                label_line.height(),
            )
            self._legend_label_rects.append((str(label), label_rect))
            painter.drawText(
                label_rect,
                Qt.AlignLeft | Qt.AlignVCenter,
                label_text,
            )

    def _draw_tooltip(self, painter: QPainter, total: float) -> None:
        if self.hover_index is None or not (0 <= self.hover_index < len(self.data)):
            return
        theme = self.theme_provider()
        label, value, color_value = self.data[self.hover_index]
        tip_width = min(230.0, max(150.0, self.width() - 20.0))
        comparison_value = self._comparison_value(str(label))
        tip_height = 84.0 if comparison_value is not None else 60.0
        x = self._hover_pos.x() + 14.0
        x = max(8.0, min(x, self.width() - tip_width - 8.0))
        y = self._hover_pos.y() - tip_height - 12.0
        if y < 8.0:
            y = self._hover_pos.y() + 14.0
        y = max(8.0, min(y, self.height() - tip_height - 8.0))
        rect = QRectF(x, y, tip_width, tip_height)

        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(0, 0, 0, 34))
        painter.drawRoundedRect(rect.translated(0, 3), 10, 10)
        painter.setPen(QPen(_chart_grid(theme), 1))
        painter.setBrush(_chart_tooltip_bg(theme))
        painter.drawRoundedRect(rect, 10, 10)

        color = self._valid_color(color_value)
        painter.setPen(Qt.NoPen)
        painter.setBrush(color)
        painter.drawEllipse(QRectF(x + 12, y + 14, 9, 9))
        painter.setPen(_chart_text(theme))
        painter.setFont(QFont("Segoe UI", 9, QFont.Bold))
        label_rect = QRectF(x + 28, y + 7, tip_width - 38, 24)
        label_text = QFontMetrics(painter.font()).elidedText(
            str(label), Qt.ElideRight, max(1, int(label_rect.width()))
        )
        painter.drawText(label_rect, Qt.AlignLeft | Qt.AlignVCenter, label_text)

        pct_str = (
            "•••%"
            if theme.privacy
            else (percent(value / total * 100) if total > 1e-9 else "0,0%")
        )
        detail = f"{eur(value, theme.privacy)}  ·  {pct_str}"
        painter.setPen(_chart_muted(theme))
        painter.setFont(QFont("Segoe UI", 8))
        painter.drawText(
            QRectF(x + 12, y + 31, tip_width - 24, 21),
            Qt.AlignLeft | Qt.AlignVCenter,
            detail,
        )
        if comparison_value is not None:
            ref_total = self.comparison_total()
            ref_pct = (comparison_value / ref_total * 100.0) if ref_total > 1e-9 else 0.0
            if theme.privacy:
                comparison = f"{self.comparison_label}: {eur(comparison_value, True)} (•••%)"
            elif comparison_value > 0.005:
                delta = (value - comparison_value) / comparison_value * 100.0
                direction = "↑" if delta > 0.5 else "↓" if delta < -0.5 else "＝"
                comparison = (
                    f"{self.comparison_label}: "
                    f"{eur(comparison_value, theme.privacy)} ({ref_pct:.0f}%) · "
                    f"{direction} {abs(delta):.0f}%"
                )
            elif value > 0.005:
                comparison = f"{self.comparison_label}: sem gasto · categoria nova"
            else:
                comparison = f"{self.comparison_label}: sem gasto"
            painter.setPen(_chart_muted(theme))
            painter.setFont(QFont("Segoe UI", 8, QFont.Bold))
            painter.drawText(
                QRectF(x + 12, y + 54, tip_width - 24, 20),
                Qt.AlignLeft | Qt.AlignVCenter,
                comparison,
            )

    def _draw_comparison_ring(
        self,
        painter: QPainter,
        arc_rect: QRectF,
        stroke: float,
        progress: float,
    ) -> None:
        ref_total = self.comparison_total()
        if ref_total <= 0:
            return
        reference_rect = arc_rect.adjusted(
            stroke * 0.62,
            stroke * 0.62,
            -stroke * 0.62,
            -stroke * 0.62,
        )
        if reference_rect.width() <= 2.0 or reference_rect.height() <= 2.0:
            return
        start_angle = 90.0

        if self.data:
            covered_ref = 0.0
            for label, _current, color_value in self.data:
                value = max(0.0, safe_float(self._comparison_value(label)))
                covered_ref += value
                if value <= 0:
                    continue
                full_span = value / ref_total * 360.0
                draw_span = max(0.2, full_span - min(2.2, full_span * 0.18))
                color = self._valid_color(color_value)
                color.setAlpha(
                    190
                    if self.category_is_highlighted(label)
                    else 130
                    if not self.highlighted_category
                    else 54
                )
                pen = QPen(
                    color,
                    max(3.0, stroke * 0.16),
                    Qt.DashLine,
                    Qt.RoundCap,
                )
                painter.setPen(pen)
                painter.setBrush(Qt.NoBrush)
                painter.drawArc(
                    reference_rect,
                    int(start_angle * 16),
                    -int(draw_span * progress * 16),
                )
                start_angle -= full_span

            # Draw explicit reference remainder for disappeared categories
            remainder = max(0.0, ref_total - covered_ref)
            if remainder > 0.005:
                rem_span = remainder / ref_total * 360.0
                draw_span = max(0.2, rem_span - min(2.2, rem_span * 0.18))
                rem_color = QColor(self.OTHER_COLOR)
                rem_color.setAlpha(130 if not self.highlighted_category else 54)
                pen = QPen(
                    rem_color,
                    max(3.0, stroke * 0.16),
                    Qt.DashLine,
                    Qt.RoundCap,
                )
                painter.setPen(pen)
                painter.setBrush(Qt.NoBrush)
                painter.drawArc(
                    reference_rect,
                    int(start_angle * 16),
                    -int(draw_span * progress * 16),
                )
                start_angle -= rem_span
        else:
            ordered = sorted(
                self._comparison_values.items(),
                key=lambda item: safe_float(item[1]),
                reverse=True,
            )
            for label, value in ordered:
                val = max(0.0, safe_float(value))
                if val <= 0:
                    continue
                full_span = val / ref_total * 360.0
                draw_span = max(0.2, full_span - min(2.2, full_span * 0.18))
                color = self._valid_color(category_color(label))
                color.setAlpha(130)
                pen = QPen(
                    color,
                    max(3.0, stroke * 0.16),
                    Qt.DashLine,
                    Qt.RoundCap,
                )
                painter.setPen(pen)
                painter.setBrush(Qt.NoBrush)
                painter.drawArc(
                    reference_rect,
                    int(start_angle * 16),
                    -int(draw_span * progress * 16),
                )
                start_angle -= full_span

    def render_chart(self, painter: QPainter) -> None:
        theme = self.theme_provider()
        painter.fillRect(self.rect(), _chart_bg(theme))
        total = sum(value for _label, value, _color in self.data)
        has_comparison = self.comparison_total() > 0
        if total <= 0 and not has_comparison:
            self.draw_empty(painter, getattr(self, "empty_message", "Sem despesas neste período"))
            return

        arc_rect, legend, stroke, inner_radius, _outer = self._geometry()
        if arc_rect.width() <= 2.0 or arc_rect.height() <= 2.0 or inner_radius <= 0.0:
            return
        self._prepare_hit_regions()
        start_angle = 90.0
        progress = max(0.0, min(1.0, self.anim_progress))
        self._draw_comparison_ring(
            painter,
            arc_rect,
            stroke,
            progress,
        )

        if total > 0:
            for index, (label, value, color_value) in enumerate(self.data):
                full_span = value / total * 360.0
                draw_span = max(0.2, full_span - min(1.5, full_span * 0.15))
                color = self._valid_color(color_value)
                focused = self.category_is_highlighted(label)
                active = self.hover_index == index or focused
                line_width = stroke + (4.0 if active else 0.0)
                if active:
                    color = color.lighter(118)
                elif self.highlighted_category:
                    color.setAlpha(72)
                pen = QPen(color, line_width, Qt.SolidLine, Qt.RoundCap)
                painter.setPen(pen)
                painter.setBrush(Qt.NoBrush)
                painter.drawArc(
                    arc_rect,
                    int(start_angle * 16),
                    -int(draw_span * progress * 16),
                )
                start_angle -= full_span

        self._draw_center(painter, arc_rect, stroke, total)
        self._draw_legend(painter, legend, total)
        self._draw_tooltip(painter, total)
