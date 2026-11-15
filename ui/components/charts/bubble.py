import math

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPen, QRadialGradient
from PySide6.QtWidgets import QSizePolicy

from ui.components.charts.base import StableChart
from ui.components.charts.utils import category_color, compact_value_items
from ui.core.theme import Theme
from utils.helpers import eur, safe_float

RED = "#FF3B30"
GREEN = "#34C759"


class BubbleChart(StableChart):
    category_clicked = Signal(str)

    def __init__(self, theme_provider=lambda: Theme(), parent=None):
        super().__init__(theme_provider, parent)
        self.zoom_factor = 1.0
        self.pan_offset = 0.0
        self.setMinimumSize(300, 420)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.data: dict[str, float] = {}
        self.category_limit: int | None = 16
        self.hit_boxes: list[tuple[QRectF, str]] = []
        self.hovered_label = None

    def set_data(self, data):
        if isinstance(data, list):
            self.data = {str(r[0]): safe_float(r[1]) for r in data}
        else:
            self.data = data
        self.hovered_label = None
        self.invalidate_cache()

    def set_category_limit(self, limit: int | None) -> None:
        normalized = None if limit is None else max(1, int(limit))
        if normalized != self.category_limit:
            self.category_limit = normalized
            self.hovered_label = None
            self.invalidate_cache()

    def mouseMoveEvent(self, event):  # type: ignore[override]
        hovered = None
        for rect, label in reversed(self.hit_boxes):
            if rect.contains(event.position()):
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
            if self.open_detail(
                {"label": label, "value": safe_float(self.data.get(label))},
                action_label=self._detail_action_label,
            ):
                self.category_clicked.emit(label)
            event.accept()
            return
        super().mousePressEvent(event)

    def leaveEvent(self, event):  # type: ignore[override]
        self.hovered_label = None
        self.setCursor(Qt.ArrowCursor)
        self.category_hovered.emit("")
        self.invalidate_cache()
        super().leaveEvent(event)

    def render_chart(self, painter: QPainter):
        if not self.data:
            self.draw_empty(painter, "Sem despesas neste período")
            return

        # BUG-CHART-BUBBLE-NEGATIVE-SCALE-011: below ~40px the fit scale
        # (width - 40) goes negative, flipping bubble radii and hit boxes.
        # Tiny viewports cannot host the spiral layout; bail out early.
        if self.width() < 60 or self.height() < 60:
            self.draw_empty(painter, "Espaço insuficiente para o gráfico")
            return

        # A normal category set is rendered in full. Extremely large custom
        # sets remain readable, with every remaining euro grouped explicitly.
        sorted_items = compact_value_items(
            self.data.items(), visible_limit=self.category_limit
        )
        max_val = max(x[1] for x in sorted_items) if sorted_items else 1
        total_exp = sum(x[1] for x in sorted_items)
        if total_exp <= 0:
            self.draw_empty(painter, "Sem despesas neste período")
            return
        theme = self.theme_provider()

        bubbles_raw = []
        angle = 0.0

        # 1. Compute local coordinates without caring about screen bounds
        for i, (label, val) in enumerate(sorted_items):
            # Bubble area, rather than diameter, follows the financial value.
            r = max(30.0, 28.0 + 62.0 * math.sqrt(val / max_val))

            placed = False
            rad = 0.0
            bx, by = 0.0, 0.0

            while not placed and rad < 2000:
                bx = rad * math.cos(angle)
                by = rad * math.sin(angle)

                overlap = False
                for ox, oy, or_, _, _, _ in bubbles_raw:
                    dist = math.hypot(bx - ox, by - oy)
                    if dist < (r + or_ + 8):
                        overlap = True
                        break

                if not overlap:
                    placed = True
                else:
                    angle += 0.3
                    rad += 2.0

            bubbles_raw.append((bx, by, r, label, val, i))

        # 2. Scale and shift to fit EXACTLY in the widget
        if not bubbles_raw:
            return

        min_x = min(bx - r for bx, by, r, _, _, _ in bubbles_raw)
        max_x = max(bx + r for bx, by, r, _, _, _ in bubbles_raw)
        min_y = min(by - r for bx, by, r, _, _, _ in bubbles_raw)
        max_y = max(by + r for bx, by, r, _, _, _ in bubbles_raw)

        content_w = max_x - min_x
        content_h = max_y - min_y

        if content_w == 0 or content_h == 0:
            return

        scale_x = (self.width() - 40) / content_w
        scale_y = (self.height() - 40) / content_h
        scale = min(scale_x, scale_y)  # Uniform scaling to prevent egg-shaped bubbles
        # BUG-CHART-BUBBLE-NEGATIVE-SCALE-011: never allow a negative or null
        # scale (degenerate viewports) and clamp the final radius so a bubble
        # is always paintable with a positive radius >= 2px.
        scale = max(0.01, min(scale, 1.0))

        # Center offset
        off_x = (self.width() - (content_w * scale)) / 2 - (min_x * scale)
        off_y = (self.height() - (content_h * scale)) / 2 - (min_y * scale)

        # 3. Draw scaled bubbles
        self.hit_boxes.clear()
        for bx, by, r, label, val, i in bubbles_raw:
            final_x = bx * scale + off_x
            final_y = by * scale + off_y
            hovered = label == self.hovered_label
            focused = self.category_is_highlighted(label)
            active = hovered or focused
            final_r = r * scale * self.anim_progress * (1.06 if active else 1.0)
            final_r = max(2.0, final_r)

            col = QColor(category_color(label))
            grad = QRadialGradient(
                final_x - final_r * 0.3, final_y - final_r * 0.3, final_r
            )
            dimmed = bool(self.highlighted_category) and not focused
            grad.setColorAt(0, QColor(255, 255, 255, 90 if dimmed else 220))
            grad.setColorAt(
                0.3,
                QColor(col.red(), col.green(), col.blue(), 62 if dimmed else 220),
            )
            grad.setColorAt(
                1,
                QColor(col.red(), col.green(), col.blue(), 24 if dimmed else 90),
            )

            painter.setPen(
                QPen(
                    QColor(255, 255, 255, 235 if active else 74 if dimmed else 150),
                    5 if focused else 4 if hovered else 3,
                )
            )
            painter.setBrush(QBrush(grad))
            painter.drawEllipse(QPointF(final_x, final_y), final_r, final_r)
            self.hit_boxes.append(
                (
                    QRectF(
                        final_x - final_r,
                        final_y - final_r,
                        final_r * 2,
                        final_r * 2,
                    ),
                    str(label),
                )
            )

            pct = (val / total_exp) * 100

            def draw_text_centered(
                ty,
                size,
                text,
                is_bold=True,
                col_text=QColor(255, 255, 255),
                *,
                center_x=final_x,
                radius=final_r,
            ):
                f = QFont("Segoe UI", max(6, int(size * scale)))
                if is_bold:
                    f.setBold(True)
                painter.setFont(f)
                rect_b = QRectF(
                    center_x - radius, ty - radius / 4, radius * 2, radius / 2
                )
                # Pure black heavy shadow for absolute readability on any color
                painter.setPen(QColor(0, 0, 0, 200))
                painter.drawText(
                    QRectF(
                        center_x - radius + 1,
                        ty - radius / 4 + 1,
                        radius * 2,
                        radius / 2,
                    ),
                    Qt.AlignCenter,
                    text,
                )
                painter.drawText(
                    QRectF(
                        center_x - radius - 1,
                        ty - radius / 4 - 1,
                        radius * 2,
                        radius / 2,
                    ),
                    Qt.AlignCenter,
                    text,
                )
                # Text
                painter.setPen(col_text)
                painter.drawText(rect_b, Qt.AlignCenter, text)

            draw_text_centered(final_y - 12 * scale, 9, str(label)[:12], True)
            draw_text_centered(
                final_y + 4 * scale,
                11,
                eur(val, theme.privacy),
                True,
                QColor(255, 255, 255),
            )
            draw_text_centered(
                final_y + 20 * scale, 8, f"{pct:.1f}%", False, QColor(220, 220, 220)
            )
