import math

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
)
from PySide6.QtWidgets import QSizePolicy

from ui.components.charts.base import StableChart
from ui.components.charts.utils import compact_value_items
from ui.core.theme import Theme
from utils.helpers import eur, safe_float

RED = "#FF3B30"
GREEN = "#34C759"


class RadarChart(StableChart):
    def __init__(self, theme_provider=lambda: Theme(), parent=None):
        super().__init__(theme_provider, parent)
        self.zoom_factor = 1.0
        self.pan_offset = 0.0
        self.setMinimumSize(300, 420)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.data: dict[str, float] = {}
        self.comparison_data: dict[str, float] = {}
        self.comparison_label = ""
        self.category_limit: int | None = 8

    def set_data(self, data):
        if isinstance(data, list):
            self.data = {r[0]: r[1] for r in data}
        else:
            self.data = data
        self.invalidate_cache()

    def set_comparison_data(self, data, label: str = "") -> None:
        if isinstance(data, list):
            self.comparison_data = {
                str(row[0]): abs(safe_float(row[1])) for row in data if len(row) >= 2
            }
        else:
            self.comparison_data = {
                str(key): abs(safe_float(value))
                for key, value in dict(data or {}).items()
            }
        self.comparison_label = str(label or "período de referência")
        self.invalidate_cache()

    def _comparison_value(
        self,
        label: str,
        visible_labels: set[str],
    ) -> float:
        if str(label).casefold().startswith("outros"):
            return sum(
                value
                for name, value in self.comparison_data.items()
                if name not in visible_labels
            )
        return max(0.0, safe_float(self.comparison_data.get(str(label))))

    def set_category_limit(self, limit: int | None) -> None:
        normalized = None if limit is None else max(3, int(limit))
        if normalized != self.category_limit:
            self.category_limit = normalized
            self.invalidate_cache()

    def render_chart(self, painter: QPainter):
        if not self.data:
            self.draw_empty(painter, "Sem categorias neste período")
            return

        theme = self.theme_provider()
        sorted_items = compact_value_items(
            ((label, safe_float(value)) for label, value in self.data.items()),
            visible_limit=self.category_limit,
        )
        if len(sorted_items) < 3:
            self.draw_empty(painter, "São necessárias pelo menos 3 categorias")
            return

        visible_labels = {
            str(label)
            for label, _value in sorted_items
            if not str(label).casefold().startswith("outros")
        }
        comparison_values = [
            self._comparison_value(label, visible_labels)
            for label, _value in sorted_items
        ]
        max_val = max(
            [safe_float(value) for _label, value in sorted_items]
            + comparison_values
            + [1.0]
        )
        if max_val == 0:
            max_val = 1

        cx, cy = self.width() / 2, self.height() / 2
        # Shift the chart upwards visually to compensate for label spacing
        cy -= 50

        # Auto-scaling radius with enough padding for labels
        radius = min(cx, cy) * 0.60 * self.anim_progress
        num_points = len(sorted_items)

        # 1. DRAW SCALES (CRISP VISIBLE WEB FOR LIGHT THEME)
        painter.setFont(QFont("Segoe UI", 7, QFont.Bold))
        for step in range(1, 6):
            ring = step / 5.0
            r = radius * ring

            poly = QPainterPath()
            for i in range(num_points):
                angle = i * (2 * math.pi / num_points) - math.pi / 2
                x = cx + r * math.cos(angle)
                y = cy + r * math.sin(angle)
                if i == 0:
                    poly.moveTo(x, y)
                else:
                    poly.lineTo(x, y)
            poly.closeSubpath()

            # Soft rings that work in both themes
            painter.setPen(
                QPen(
                    QColor(148, 163, 184, 54 if theme.dark else 42),
                    1,
                )
            )
            painter.setBrush(QColor(148, 163, 184, 8))
            painter.drawPath(poly)

        # 2. DRAW AXES
        painter.setPen(QPen(QColor(148, 163, 184, 42), 1))
        for i in range(num_points):
            angle = i * (2 * math.pi / num_points) - math.pi / 2
            x = cx + radius * math.cos(angle)
            y = cy + radius * math.sin(angle)
            painter.drawLine(int(cx), int(cy), int(x), int(y))

        # Previous period: a restrained dashed polygon on the same axes/scale.
        if any(value > 0 for value in comparison_values):
            reference_poly = QPainterPath()
            for index, value in enumerate(comparison_values):
                angle = index * (2 * math.pi / num_points) - math.pi / 2
                reference_radius = radius * value / max_val
                point = QPointF(
                    cx + reference_radius * math.cos(angle),
                    cy + reference_radius * math.sin(angle),
                )
                if index == 0:
                    reference_poly.moveTo(point)
                else:
                    reference_poly.lineTo(point)
            reference_poly.closeSubpath()
            reference_fill = QColor(148, 163, 184, 24)
            reference_pen = QColor(100, 116, 139, 190)
            painter.setBrush(reference_fill)
            painter.setPen(QPen(reference_pen, 2, Qt.DashLine))
            painter.drawPath(reference_poly)

        # 3. DRAW DATA POLYGON (Standard linear radar mapping)
        data_poly = QPainterPath()
        points = []
        for i, (label, val) in enumerate(sorted_items):
            angle = i * (2 * math.pi / num_points) - math.pi / 2
            r = radius * (val / max_val) if max_val else 0
            x = cx + r * math.cos(angle)
            y = cy + r * math.sin(angle)
            points.append((x, y, label, val, angle, r))
            if i == 0:
                data_poly.moveTo(x, y)
            else:
                data_poly.lineTo(x, y)
        data_poly.closeSubpath()
        grad = QLinearGradient(0, 0, self.width(), self.height())
        grad.setColorAt(0, QColor(14, 165, 233, 90))  # Sky Blue
        grad.setColorAt(1, QColor(99, 102, 241, 90))  # Indigo

        painter.setPen(QPen(QColor(14, 165, 233, 220), 2))  # Premium thin line
        painter.setBrush(QBrush(grad))
        painter.drawPath(data_poly)

        # 4. RICH EXTERNAL LABELS WITH HIERARCHY
        for x, y, label, val, angle, r in points:
            focused = self.category_is_highlighted(label)
            dimmed = bool(self.highlighted_category) and not focused
            # Clean premium vertices (White center, Blue stroke)
            point_color = QColor(14, 165, 233)
            if dimmed:
                point_color.setAlpha(64)
            painter.setPen(QPen(point_color, 4 if focused else 2))
            painter.setBrush(QColor(255, 255, 255, 90 if dimmed else 255))
            painter.drawEllipse(QPointF(x, y), 6 if focused else 4, 6 if focused else 4)

            # Labels at the fixed axes perimeter so they don't jump around
            lx = cx + (radius + 25) * math.cos(angle)
            ly = cy + (radius + 25) * math.sin(angle)

            align = Qt.AlignCenter
            rect_x = lx - 100

            if math.cos(angle) > 0.1:
                align = Qt.AlignLeft | Qt.AlignVCenter
                lx += 5
                rect_x = lx
            elif math.cos(angle) < -0.1:
                align = Qt.AlignRight | Qt.AlignVCenter
                lx -= 5
                rect_x = lx - 200

            if math.sin(angle) > 0.1:
                ly += 12
            elif math.sin(angle) < -0.1:
                ly -= 12

            # Category Name - Muted and Normal weight
            painter.setFont(
                QFont("Segoe UI", 9, QFont.Bold if focused else QFont.Normal)
            )
            rect_label = QRectF(rect_x, ly - 20, 200, 20)
            label_color = QColor(theme.primary_color if focused else theme.muted)
            if dimmed:
                label_color.setAlpha(82)
            painter.setPen(label_color)
            painter.drawText(rect_label, align, str(label))

            # Category Value - Bold and Highlight Color
            painter.setFont(QFont("Segoe UI", 11, QFont.Bold))
            rect_val = QRectF(rect_x, ly, 200, 20)
            value_color = QColor(14, 165, 233, 255)
            if dimmed:
                value_color.setAlpha(82)
            painter.setPen(value_color)
            painter.drawText(rect_val, align, eur(val, theme.privacy))
