from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetrics,
    QLinearGradient,
    QPainter,
    QPen,
)
from PySide6.QtWidgets import QSizePolicy

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
from utils.helpers import eur, safe_float


class TreemapChart(StableChart):
    """Balanced treemap with every category represented by a tile."""

    category_clicked = Signal(str)

    _fallback_colors = (
        "#FF2D55",
        "#FF9500",
        "#FFCC00",
        "#34C759",
        "#5AC8FA",
        "#007AFF",
        "#AF52DE",
        "#14B8A6",
        "#6366F1",
        "#F43F5E",
        "#84CC16",
        "#06B6D4",
    )

    def __init__(self, theme_provider=lambda: Theme(), parent=None):
        super().__init__(theme_provider, parent)
        self.setMinimumSize(300, 540)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.data: dict[str, float] = {}
        self.category_colors: dict[str, str] = {}
        self.hit_boxes: list[tuple[QRectF, str]] = []
        self.hovered_label: str | None = None
        self._hover_pos = QPointF()
        self.setAccessibleName("Peso das categorias de despesa")

    @staticmethod
    def _valid_color(value: str, fallback: str) -> QColor:
        color = QColor(str(value or ""))
        return color if color.isValid() else QColor(fallback)

    def set_data(self, data):
        grouped: dict[str, float] = {}
        colors: dict[str, str] = {}
        if isinstance(data, dict):
            rows = [(label, value, "") for label, value in data.items()]
        else:
            rows = data or []

        for row in rows:
            try:
                label = str(row[0] or "Sem categoria")
                value = abs(safe_float(row[1]))
                color = str(row[2] if len(row) > 2 else "")
            except (IndexError, TypeError, ValueError):
                continue
            if value <= 0:
                continue
            grouped[label] = grouped.get(label, 0.0) + value
            if color:
                colors[label] = color

        self.data = grouped
        self.category_colors = {
            label: colors.get(label) or category_color(label) for label in grouped
        }
        self.hovered_label = None
        self.hit_boxes.clear()
        self.setAccessibleDescription(
            f"{len(self.data)} categorias" if self.data else "Sem despesas"
        )
        self.setMinimumHeight(540)
        if self.data and self.isVisible():
            self.start_entrance_animation(650)
        else:
            self.invalidate_cache()

    def _sorted_items(self) -> list[tuple[str, float, str]]:
        ordered = sorted(self.data.items(), key=lambda item: item[1], reverse=True)
        return [
            (
                label,
                value,
                self.category_colors.get(
                    label, self._fallback_colors[index % len(self._fallback_colors)]
                ),
            )
            for index, (label, value) in enumerate(ordered)
        ]

    def _binary_rects(
        self,
        items: list[tuple[str, float, str]],
        bounds: QRectF,
    ) -> list[tuple[QRectF, str, float, str]]:
        """Recursively split near half the value to avoid long, thin slivers."""
        if not items:
            return []
        if len(items) == 1:
            label, value, color = items[0]
            return [(QRectF(bounds), label, value, color)]

        total = sum(value for _label, value, _color in items)
        if total <= 0:
            return []
        target = total / 2.0
        running = 0.0
        split_at = 1
        best_distance = float("inf")
        for index in range(1, len(items)):
            running += items[index - 1][1]
            distance = abs(target - running)
            if distance < best_distance:
                best_distance = distance
                split_at = index

        first = items[:split_at]
        second = items[split_at:]
        first_total = sum(value for _label, value, _color in first)
        ratio = max(0.0, min(1.0, first_total / total))
        if bounds.width() >= bounds.height():
            first_width = bounds.width() * ratio
            first_rect = QRectF(
                bounds.left(), bounds.top(), first_width, bounds.height()
            )
            second_rect = QRectF(
                bounds.left() + first_width,
                bounds.top(),
                bounds.width() - first_width,
                bounds.height(),
            )
        else:
            first_height = bounds.height() * ratio
            first_rect = QRectF(
                bounds.left(), bounds.top(), bounds.width(), first_height
            )
            second_rect = QRectF(
                bounds.left(),
                bounds.top() + first_height,
                bounds.width(),
                bounds.height() - first_height,
            )
        return self._binary_rects(first, first_rect) + self._binary_rects(
            second, second_rect
        )

    @staticmethod
    def _tile_text_color(color: QColor) -> QColor:
        luminance = (
            0.2126 * color.red() + 0.7152 * color.green() + 0.0722 * color.blue()
        )
        return QColor("#102033") if luminance > 174 else QColor("#FFFFFF")

    def _draw_tile_text(
        self,
        painter: QPainter,
        box: QRectF,
        rank: int,
        label: str,
        value: float,
        percentage: float,
        color: QColor,
    ) -> None:
        foreground = self._tile_text_color(color)
        inner = box.adjusted(9, 7, -9, -7)
        if inner.width() < 26 or inner.height() < 18:
            return

        if inner.width() >= 96 and inner.height() >= 64:
            label_font = QFont("Segoe UI", 10, QFont.Bold)
            painter.setFont(label_font)
            painter.setPen(foreground)
            label_text = QFontMetrics(label_font).elidedText(
                label, Qt.ElideRight, max(1, int(inner.width()))
            )
            painter.drawText(
                QRectF(inner.left(), inner.top(), inner.width(), 21),
                Qt.AlignLeft | Qt.AlignVCenter,
                label_text,
            )
            painter.setFont(QFont("Segoe UI", 11, QFont.Bold))
            painter.drawText(
                QRectF(inner.left(), inner.top() + 22, inner.width(), 22),
                Qt.AlignLeft | Qt.AlignVCenter,
                eur(value, self.theme_provider().privacy),
            )
            muted = QColor(foreground)
            muted.setAlpha(215)
            painter.setPen(muted)
            painter.setFont(QFont("Segoe UI", 8, QFont.Bold))
            impact_text = (
                "Impacto •••%"
                if self.theme_provider().privacy
                else f"Impacto {percentage:.1f}%"
            )
            painter.drawText(
                QRectF(inner.left(), inner.top() + 45, inner.width(), 18),
                Qt.AlignLeft | Qt.AlignVCenter,
                impact_text,
            )
        elif inner.width() >= 58 and inner.height() >= 34:
            label_font = QFont("Segoe UI", 8, QFont.Bold)
            painter.setFont(label_font)
            painter.setPen(foreground)
            label_text = QFontMetrics(label_font).elidedText(
                label, Qt.ElideRight, max(1, int(inner.width()))
            )
            painter.drawText(
                QRectF(inner.left(), inner.top(), inner.width(), 17),
                Qt.AlignLeft | Qt.AlignVCenter,
                label_text,
            )
            painter.setFont(QFont("Segoe UI", 8, QFont.Bold))
            pct_text = "•••%" if self.theme_provider().privacy else f"{percentage:.1f}%"
            painter.drawText(
                QRectF(inner.left(), inner.top() + 17, inner.width(), 17),
                Qt.AlignLeft | Qt.AlignVCenter,
                pct_text,
            )
        else:
            painter.setFont(QFont("Segoe UI", 7, QFont.Bold))
            painter.setPen(foreground)
            painter.drawText(box, Qt.AlignCenter, f"#{rank}")

    def _draw_tooltip(
        self,
        painter: QPainter,
        items: list[tuple[str, float, str]],
        total: float,
    ) -> None:
        if not self.hovered_label:
            return
        selected = next((item for item in items if item[0] == self.hovered_label), None)
        if selected is None:
            return
        theme = self.theme_provider()
        label, value, color_value = selected
        percentage = value / total * 100 if total else 0.0
        width = min(250.0, max(170.0, self.width() - 20.0))
        height = 62.0
        x = max(8.0, min(self._hover_pos.x() + 14.0, self.width() - width - 8.0))
        y = self._hover_pos.y() - height - 12.0
        if y < 8.0:
            y = self._hover_pos.y() + 14.0
        y = max(8.0, min(y, self.height() - height - 8.0))
        rect = QRectF(x, y, width, height)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(0, 0, 0, 36))
        painter.drawRoundedRect(rect.translated(0, 3), 11, 11)
        painter.setPen(QPen(_chart_grid(theme), 1))
        painter.setBrush(_chart_tooltip_bg(theme))
        painter.drawRoundedRect(rect, 11, 11)

        color = self._valid_color(color_value, self._fallback_colors[0])
        painter.setPen(Qt.NoPen)
        painter.setBrush(color)
        painter.drawEllipse(QRectF(x + 12, y + 13, 9, 9))
        painter.setFont(QFont("Segoe UI", 9, QFont.Bold))
        painter.setPen(_chart_text(theme))
        painter.drawText(
            QRectF(x + 28, y + 6, width - 40, 24),
            Qt.AlignLeft | Qt.AlignVCenter,
            QFontMetrics(painter.font()).elidedText(
                label, Qt.ElideRight, max(1, int(width - 40))
            ),
        )
        painter.setFont(QFont("Segoe UI", 8))
        painter.setPen(_chart_muted(theme))
        impact_str = "Impacto •••%" if theme.privacy else f"Impacto {percentage:.1f}%"
        painter.drawText(
            QRectF(x + 12, y + 32, width - 24, 21),
            Qt.AlignLeft | Qt.AlignVCenter,
            f"{eur(value, theme.privacy)} · {impact_str}",
        )

    def mouseMoveEvent(self, event):  # type: ignore[override]
        self._hover_pos = event.position()
        hovered = None
        for rect, label in self.hit_boxes:
            if rect.contains(event.position()):
                hovered = label
                break
        if hovered != self.hovered_label:
            self.hovered_label = hovered
            self.setCursor(Qt.PointingHandCursor if hovered else Qt.ArrowCursor)
            self.category_hovered.emit(str(hovered or ""))
            self.invalidate_cache()
            self.update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):  # type: ignore[override]
        self.hovered_label = None
        self.setCursor(Qt.ArrowCursor)
        self.category_hovered.emit("")
        self.invalidate_cache()
        super().leaveEvent(event)

    def mousePressEvent(self, event):  # type: ignore[override]
        if event.button() == Qt.LeftButton:
            for rect, label in self.hit_boxes:
                if rect.contains(event.position()):
                    if self.open_detail(
                        {"label": label, "value": safe_float(self.data.get(label))},
                        action_label=self._detail_action_label,
                    ):
                        self.category_clicked.emit(label)
                    event.accept()
                    return
        super().mousePressEvent(event)

    def render_chart(self, painter: QPainter):
        theme = self.theme_provider()
        painter.fillRect(self.rect(), _chart_bg(theme))
        items = self._sorted_items()
        total = sum(value for _label, value, _color in items)
        if total <= 0:
            self.draw_empty(painter, "Sem despesas neste período")
            return

        # Use the full chart area: details already live inside each tile and
        # remain available in the hover tooltip, so no footer legend is needed.
        map_rect = QRectF(self.rect().adjusted(8, 8, -8, -8))
        rectangles = self._binary_rects(items, map_rect)
        self.hit_boxes.clear()
        for rank, (rect, label, value, color_value) in enumerate(rectangles, 1):
            if rect.width() <= 3 or rect.height() <= 3:
                continue
            # BUG-CHART-TREEMAP-NEGATIVE-GEOMETRY-005: a fixed 2px inset on a
            # 4px-wide tile produced a degenerate (zero/negative) QRectF and a
            # Qt warning. Scale the inset down so the inner box keeps >= 1px
            # per side; for tiles smaller than 5px draw them un-insetted.
            inset = min(2.0, max(0.0, (min(rect.width(), rect.height()) - 1.0) / 2.0))
            box = rect.adjusted(inset, inset, -inset, -inset)
            if box.width() < 1.0 or box.height() < 1.0:
                box = QRectF(rect)
            color = self._valid_color(
                color_value,
                self._fallback_colors[(rank - 1) % len(self._fallback_colors)],
            )
            hovered = label == self.hovered_label
            focused = self.category_is_highlighted(label)
            active = hovered or focused
            gradient = QLinearGradient(box.topLeft(), box.bottomRight())
            start_color = color.lighter(112 if active else 104)
            if self.highlighted_category and not focused:
                start_color.setAlpha(68)
            gradient.setColorAt(0, start_color)
            end_color = QColor(color)
            end_color.setAlpha(
                210 if active else 172 if not self.highlighted_category else 48
            )
            gradient.setColorAt(1, end_color)
            painter.setPen(
                QPen(
                    QColor(255, 255, 255, 225 if active else 74),
                    4 if focused else 3 if hovered else 1,
                )
            )
            painter.setBrush(QBrush(gradient))
            painter.drawRoundedRect(box, 9, 9)
            self.hit_boxes.append((box, label))
            self._draw_tile_text(
                painter,
                box,
                rank,
                label,
                value,
                value / total * 100,
                color,
            )

        self._draw_tooltip(painter, items, total)
