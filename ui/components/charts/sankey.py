from math import ceil

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
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
from PySide6.QtWidgets import QSizePolicy

from ui.components.charts.base import StableChart
from ui.components.charts.utils import (
    _chart_muted,
    _chart_text,
    category_color,
    compact_value_items,
)
from ui.core.theme import Theme
from utils.helpers import eur, safe_float

RED = "#FF3B30"
GREEN = "#34C759"

# BUG-CHART-SANKEY-CONTRAST-004: the neon accents (#00B4C8, #30D158) fail the
# WCAG 2.2 AA 3:1 non-text contrast minimum on light surfaces.  Darkened
# light-theme variants keep the hue identity while reaching >= 3:1.
_FLOW_ACCENT_DARK = "#00B4C8"  # cyan (dark theme)
_FLOW_ACCENT_LIGHT = "#007A8A"  # cyan darkened for light theme contrast
_FLOW_GREEN_DARK = "#30D158"  # green (dark theme)
_FLOW_GREEN_LIGHT = "#1E7A34"  # green darkened for light theme contrast


def _flow_accent_color(theme: Theme) -> QColor:
    return QColor(_FLOW_ACCENT_LIGHT if not theme.dark else _FLOW_ACCENT_DARK)


def _flow_green_color(theme: Theme) -> QColor:
    return QColor(_FLOW_GREEN_LIGHT if not theme.dark else _FLOW_GREEN_DARK)


class SankeyChart(StableChart):
    category_clicked = Signal(str)
    VISIBLE_CATEGORY_LIMIT = 8
    MINIMUM_CHART_HEIGHT = 420
    FLOW_HEIGHT = 250.0
    FLOW_GAP = 28.0
    TOP_MARGIN = 20.0
    BOTTOM_MARGIN = 48.0

    def __init__(self, theme_provider=lambda: Theme(), parent=None):
        super().__init__(theme_provider, parent)
        self.zoom_factor = 1.0
        self.pan_offset = 0.0
        self.setMinimumSize(300, self.MINIMUM_CHART_HEIGHT)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.data: dict[str, float] = {}
        self.items: list[tuple[str, float]] = []
        self.category_limit: int | None = self.VISIBLE_CATEGORY_LIMIT
        self.hit_boxes: list[tuple[QRectF, str]] = []
        self.hovered_label: str | None = None

    def _compact_items(self) -> list[tuple[str, float]]:
        return compact_value_items(
            (
                (label, safe_float(value))
                for label, value in self.data.items()
                if safe_float(value) > 0
            ),
            visible_limit=self.category_limit,
        )

    def set_category_limit(self, limit: int | None) -> None:
        normalized = None if limit is None else max(1, int(limit))
        if normalized == self.category_limit:
            return
        self.category_limit = normalized
        self.items = self._compact_items()
        self.hovered_label = None
        self.hit_boxes.clear()
        self._sync_required_height()
        self.invalidate_cache()

    @classmethod
    def required_height_for_items(cls, items: list[tuple[str, float]]) -> int:
        """Return enough vertical room for every node, label and flow gap."""
        total = sum(value for _label, value in items)
        if not items or total <= 0:
            return cls.MINIMUM_CHART_HEIGHT
        flow_total = sum(
            max(8.0, cls.FLOW_HEIGHT * value / total) for _label, value in items
        )
        content_bottom = (
            cls.TOP_MARGIN + flow_total + cls.FLOW_GAP * max(0, len(items) - 1)
        )
        return max(
            cls.MINIMUM_CHART_HEIGHT,
            ceil(content_bottom + cls.BOTTOM_MARGIN),
        )

    def _sync_required_height(self) -> None:
        required_height = self.required_height_for_items(self.items)
        if self.minimumHeight() != required_height:
            self.setMinimumHeight(required_height)
            self.updateGeometry()
            parent = self.parentWidget()
            if parent is not None:
                parent.updateGeometry()

    def set_data(self, data):
        if isinstance(data, list):
            self.data = {r[0]: r[1] for r in data}
        else:
            self.data = data
        self.items = self._compact_items()
        self._sync_required_height()
        self.setAccessibleDescription(
            f"Distribuição de despesas por {len(self.items)} linhas visíveis."
        )
        self.invalidate_cache()

    @classmethod
    def flow_heights_for_items(cls, items, canvas_h: float) -> list[float]:
        """BUG-CHART-SANKEY-NEGATIVE-GEOMETRY-001: per-item flow heights.

        Floor (8px) keeps every node visible; the per-item ceiling and the
        global shrink keep the stacked flow column inside the canvas even
        with many categories or a small viewport.  Pure function so tests
        can verify canvas-fit invariants without painting.
        """

        if not items:
            return []
        total = sum(max(0.0, float(value)) for _label, value in items)
        if total <= 0:
            return [8.0] * len(items)
        left_h = min(canvas_h - 100, cls.FLOW_HEIGHT)
        heights = [
            min(left_h * 0.9, max(8.0, left_h * max(0.0, float(value)) / total))
            for _label, value in items
        ]
        overflow = (
            sum(heights)
            + cls.FLOW_GAP * (len(heights) - 1)
            - (canvas_h - cls.TOP_MARGIN - 30)
        )
        if overflow > 0 and len(heights) > 1:
            shrink = max(8.0, (sum(heights) - overflow) / len(heights))
            heights = [min(fh, shrink) for fh in heights]
        return heights

    def render_chart(self, painter: QPainter):

        if not self.data:
            self.draw_empty(painter, "Sem despesas neste período")
            return

        theme = self.theme_provider()
        sorted_items = self.items
        total_exp = sum(x[1] for x in sorted_items if x[1] > 0)
        if total_exp <= 0:
            self.draw_empty(painter, "Sem despesas neste período")
            return

        w, h = self.width(), self.height()

        # BUG-CHART-SANKEY-NEGATIVE-GEOMETRY-001: too-small viewports cannot
        # host a legible flow diagram; bail out instead of drawing negative
        # heights (Qt warning + inverted Bézier control points).
        if w <= 220 or h <= 120 or (h - 100) <= 0:
            self.draw_empty(painter, "Espaço insuficiente para o diagrama")
            return

        # Dimensions
        left_x = 20
        mid_x = w / 2
        right_x = w - 190

        # BUG-CHART-SANKEY-BEZIER-INVERSION-003: on narrow viewports (< 380px)
        # the old mid_x = w/2 control point sat beyond right_x, so the cubic
        # Bézier reversed direction and drew looping crossings.  Clamp the
        # control-point horizontal distance to 45% of the gap between the
        # source and destination nodes (pure geometry, testable exactly).
        flow_start_x = left_x + 15
        node_gap = max(1.0, float(right_x) - float(flow_start_x))
        max_control_offset = node_gap * 0.45
        control_x = flow_start_x + min(float(mid_x) - flow_start_x, max_control_offset)

        left_h = min(h - 100, self.FLOW_HEIGHT)
        left_y = (h - left_h) / 2

        current_left_y = left_y
        current_right_y = self.TOP_MARGIN
        self.hit_boxes.clear()

        # Shared pure helper: floor + per-item ceiling + global shrink.
        flow_heights = self.flow_heights_for_items(self.items, float(h))

        for i, (label, val) in enumerate(sorted_items):
            flow_thickness = flow_heights[i]
            hovered = label == self.hovered_label
            focused = self.category_is_highlighted(label)
            active = hovered or focused
            dimmed = bool(self.highlighted_category) and not focused
            col = QColor(category_color(label)).lighter(112 if active else 100)

            path = QPainterPath()
            path.moveTo(left_x + 15, current_left_y + flow_thickness / 2)
            path.cubicTo(
                control_x,
                current_left_y + flow_thickness / 2,
                control_x,
                current_right_y + flow_thickness / 2,
                right_x,
                current_right_y + flow_thickness / 2,
            )

            grad = QLinearGradient(left_x, 0, right_x, 0)
            grad.setColorAt(0, QColor(48, 209, 88, 48 if dimmed else 150))
            grad.setColorAt(
                1,
                QColor(
                    col.red(),
                    col.green(),
                    col.blue(),
                    52 if dimmed else 210 if focused else 180,
                ),
            )

            pen = QPen(
                QBrush(grad),
                flow_thickness + (3.0 if focused else 0.0),
            )
            pen.setCapStyle(Qt.FlatCap)
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)
            painter.drawPath(path)

            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(48, 209, 88))
            painter.drawRect(left_x, int(current_left_y), 15, int(flow_thickness))

            painter.setPen(QPen(QColor(0, 0, 0, 30), 1))
            painter.setBrush(Qt.NoBrush)
            painter.drawRect(left_x, int(current_left_y), 15, int(flow_thickness))

            painter.setPen(Qt.NoPen)
            painter.setBrush(col)
            painter.drawRoundedRect(
                right_x, int(current_right_y), 15, int(flow_thickness), 3, 3
            )
            self.hit_boxes.append(
                (
                    QRectF(
                        right_x,
                        current_right_y - 6,
                        max(1.0, w - right_x),
                        max(52.0, flow_thickness + 12),
                    ),
                    str(label),
                )
            )

            pct = (val / total_exp) * 100

            font_bold = QFont("Segoe UI", 10, QFont.Bold)
            painter.setFont(font_bold)
            painter.setPen(_chart_text(theme))
            metrics = QFontMetrics(font_bold)
            avail_label_w = max(60, min(140, int(w - (right_x + 85))))
            elided_label = metrics.elidedText(str(label), Qt.ElideRight, avail_label_w)
            painter.drawText(
                int(right_x + 25),
                int(current_right_y + flow_thickness / 2 - 8),
                avail_label_w,
                20,
                Qt.AlignLeft | Qt.AlignVCenter,
                elided_label,
            )

            painter.setFont(QFont("Segoe UI", 9, QFont.Bold))
            painter.setPen(_flow_accent_color(theme))
            painter.drawText(
                int(right_x + 25),
                int(current_right_y + flow_thickness / 2 + 8),
                160,
                20,
                Qt.AlignLeft | Qt.AlignVCenter,
                eur(val, theme.privacy),
            )

            pct_x = int(right_x + 25 + avail_label_w + 4)
            pct_str = "•••%" if getattr(theme, "privacy", False) else f"{pct:.1f}%"
            painter.setFont(QFont("Segoe UI", 8))
            painter.setPen(_chart_muted(theme))
            painter.drawText(
                pct_x,
                int(current_right_y + flow_thickness / 2 - 8),
                50,
                20,
                Qt.AlignLeft | Qt.AlignVCenter,
                f"[{pct_str}]",
            )

            current_left_y += flow_thickness
            current_right_y += flow_thickness + self.FLOW_GAP

        painter.setPen(QPen(QColor(0, 0, 0, 50), 2))
        painter.setBrush(Qt.NoBrush)
        painter.drawRoundedRect(left_x, int(left_y), 15, int(left_h), 4, 4)

        painter.setFont(QFont("Segoe UI", 12, QFont.Bold))
        painter.setPen(_chart_text(theme))
        painter.drawText(
            left_x - 10,
            int(left_y - 45),
            180,
            25,
            Qt.AlignLeft,
            "Total de despesas",
        )

        painter.setFont(QFont("Segoe UI", 11, QFont.Bold))
        painter.setPen(_flow_green_color(theme))
        painter.drawText(
            left_x - 10,
            int(left_y - 25),
            180,
            25,
            Qt.AlignLeft,
            eur(total_exp, theme.privacy),
        )

    def mouseMoveEvent(self, event):  # type: ignore[override]
        hovered = None
        for rect, label in reversed(self.hit_boxes):
            if rect.contains(QPointF(event.position())):
                hovered = label
                break
        if hovered != self.hovered_label:
            self.hovered_label = hovered
            self.setCursor(Qt.PointingHandCursor if hovered else Qt.ArrowCursor)
            self.category_hovered.emit(str(hovered or ""))
            self.invalidate_cache()
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event):  # type: ignore[override]
        if event.button() == Qt.LeftButton and self.hovered_label:
            label = str(self.hovered_label)
            value = next(
                (amount for item_label, amount in self.items if item_label == label),
                safe_float(self.data.get(label)),
            )
            if self.open_detail(
                {"label": label, "value": value},
                action_label=self._detail_action_label,
            ):
                self.category_clicked.emit(label)
            event.accept()
            return
        super().mousePressEvent(event)

    def leaveEvent(self, event):  # type: ignore[override]
        if self.hovered_label is not None:
            self.hovered_label = None
            self.setCursor(Qt.ArrowCursor)
            self.invalidate_cache()
        self.category_hovered.emit("")
        super().leaveEvent(event)
