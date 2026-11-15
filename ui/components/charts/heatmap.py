from datetime import datetime

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen

from ui.components.charts.base import StableChart
from ui.components.charts.utils import (
    _chart_muted,
    _chart_text,
    _chart_tooltip_bg,
)
from utils.helpers import eur, safe_float

RED = "#FF3B30"
GREEN = "#34C759"


class HeatmapChart(StableChart):
    WEEKDAY_LABELS = ("Seg", "Ter", "Qua", "Qui", "Sex", "Sáb", "Dom")

    def __init__(self, theme_provider, parent=None):
        super().__init__(theme_provider, parent)
        self.setMinimumHeight(210)
        self.points: list[dict] = []
        self.hover_index = None
        self.setMouseTracking(True)
        self.tooltip_rects = []
        self.weekday_label_rects: list[tuple[str, QRectF]] = []
        self.setAccessibleName("Ritmo diário de gastos dos últimos 12 meses")
        self.setAccessibleDescription(
            "Mapa com sete linhas: Seg, Ter, Qua, Qui, Sex, Sáb e Dom."
        )

    def set_data(self, data: list[dict]):
        # data expected: [{"date": "2026-06-25", "expense": 50.0}, ...]
        sorted_points = sorted(data or [], key=lambda p: str(p.get("date") or ""))
        parsed_points = []
        expenses = []
        for p in sorted_points:
            date_str = str(p.get("date") or "")
            try:
                dt = datetime.strptime(date_str, "%Y-%m-%d")
            except Exception:
                continue
            exp = safe_float(p.get("expense"))
            if exp > 0:
                expenses.append(exp)
            parsed_points.append(
                {
                    "date": date_str,
                    "date_str": date_str,
                    "month": dt.month,
                    "weekday": dt.weekday(),
                    "expense": exp,
                    "resolution": "daily",
                }
            )
        self.points = parsed_points
        self._max_exp = max(expenses) if expenses else 1.0
        self.invalidate_cache()

    def mouseMoveEvent(self, event):
        pos = event.position().toPoint() if hasattr(event, "position") else event.pos()
        hovered = None
        for i, rect, info in self.tooltip_rects:
            if rect.contains(pos):
                hovered = i
                break
        if hovered != self.hover_index:
            self.hover_index = hovered
            self.invalidate_cache()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        if self.hover_index is not None:
            self.hover_index = None
            self.invalidate_cache()
        super().leaveEvent(event)

    def render_chart(self, painter: QPainter):

        if not self.points:
            self.draw_empty(painter, "Sem dados suficientes para o heatmap.")
            return

        theme = self.theme_provider()
        max_exp = getattr(self, "_max_exp", 1.0)
        w = self.width()
        h = self.height()

        # Calc total width needed for ~53 columns
        days_total = len(self.points)
        cols_total = max(1, (days_total // 7) + 2)

        # Scale down pitch and margins dynamically on compact screens to prevent overflow (< 526px)
        left_pad = 40.0
        right_pad = 6.0
        avail_w = max(50.0, float(w) - left_pad - right_pad)
        target_pitch = avail_w / cols_total

        if target_pitch >= 16:
            margin = 3
            box_size = min(14, int(target_pitch - margin))
        elif target_pitch >= 9:
            margin = 2
            box_size = int(target_pitch - margin)
        elif target_pitch >= 6:
            margin = 1
            box_size = max(4, int(target_pitch - margin))
        else:
            margin = 1
            box_size = max(3, int(target_pitch - margin))

        column_pitch = box_size + margin
        total_w = cols_total * column_pitch
        # Keep all seven labels readable even when the annual grid is compact.
        row_pitch = max(column_pitch, 13)
        total_h = 7 * row_pitch

        # BUG-CHART-HEATMAP-LABEL-COLLISION-006: with ~53 weekly columns the
        # grid can start further right than the 40px label gutter when the
        # widget is narrower than 280px, so the weekday labels collide with
        # the first painted cells.  Hide alternate labels dynamically and
        # shrink the gutter requirement to the space actually available.
        start_x = max(40.0, (float(w) - float(total_w)) / 2.0)
        if start_x + total_w > float(w) - 4.0:
            start_x = max(38.0, float(w) - float(total_w) - 4.0)
            if start_x + total_w > float(w):
                start_x = max(0.0, float(w) - float(total_w))
        label_gutter = max(0.0, min(40.0, start_x - 2.0))
        weekday_label_step = 1 if label_gutter >= 30.0 else 2

        start_y = max(30.0, (float(h) - float(total_h)) / 2.0)

        # Month labels setup
        months = [
            "Jan",
            "Fev",
            "Mar",
            "Abr",
            "Mai",
            "Jun",
            "Jul",
            "Ago",
            "Set",
            "Out",
            "Nov",
            "Dez",
        ]
        last_month = None

        painter.setFont(QFont("Segoe UI", 8 if row_pitch >= 13 else 7, QFont.Bold))
        painter.setPen(QColor(theme.muted))

        # Draw weekdays; alternate labels are hidden dynamically when the
        # gutter is too narrow (BUG-CHART-HEATMAP-LABEL-COLLISION-006).
        self.weekday_label_rects.clear()
        for weekday_row, label in enumerate(self.WEEKDAY_LABELS):
            if weekday_row % weekday_label_step != 0:
                continue
            label_rect = QRectF(
                max(0.0, start_x - 40),
                start_y + weekday_row * row_pitch,
                min(36.0, max(18.0, label_gutter - 4.0)) if label_gutter else 0.0,
                row_pitch,
            )
            if label_rect.width() < 8.0:
                continue
            painter.drawText(label_rect, Qt.AlignRight | Qt.AlignVCenter, label)
            self.weekday_label_rects.append((label, label_rect))

        self.tooltip_rects.clear()

        # If we have 365 days, we align them so that day 0 is placed at the correct weekday row.
        first_weekday = self.points[0]["weekday"]  # 0 = Monday, 6 = Sunday

        col = 0
        row = first_weekday

        for i, pt in enumerate(self.points):
            dt_month = pt["month"]
            dt_str = pt["date_str"]
            expense = pt["expense"]

            # Print month label if month changes and we are near the top row
            if (
                dt_month != last_month and row <= 3
            ):  # avoid printing label at bottom rows where it overlaps next column
                painter.setPen(QColor(theme.muted))
                painter.drawText(
                    QRectF(start_x + col * column_pitch, start_y - 25, 40, 20),
                    Qt.AlignLeft | Qt.AlignBottom,
                    months[dt_month - 1],
                )
                last_month = dt_month

            x = start_x + col * column_pitch
            y = start_y + row * row_pitch

            rect = QRectF(x, y, box_size, box_size)

            # Color logic
            if expense <= 0:
                color = QColor("#161b22") if theme.dark else QColor("#ebedf0")
                border = (
                    QColor(255, 255, 255, 10) if theme.dark else QColor(0, 0, 0, 10)
                )
            else:
                intensity = min(
                    1.0, max(0.0, expense) / (max(1.0, max_exp) * 0.4)
                )  # curve to show colors even on small expenses
                if intensity < 0.25:
                    color = QColor("#0e4429") if theme.dark else QColor("#9be9a8")
                elif intensity < 0.5:
                    color = QColor("#006d32") if theme.dark else QColor("#40c463")
                elif intensity < 0.75:
                    color = QColor("#26a641") if theme.dark else QColor("#30a14e")
                else:
                    color = QColor("#39d353") if theme.dark else QColor("#216e39")
                border = color

            if self.hover_index == i:
                color = color.lighter(130)
                painter.setPen(QPen(QColor(theme.text), 1.5))
            else:
                painter.setPen(QPen(border, 1))

            painter.setBrush(color)
            corner_r = min(3, max(1, box_size // 2))
            painter.drawRoundedRect(rect, corner_r, corner_r)

            self.tooltip_rects.append((i, rect, (dt_str, expense)))

            row += 1
            if row > 6:
                row = 0
                col += 1

        # Draw tooltip
        if self.hover_index is not None and self.hover_index < len(self.tooltip_rects):
            idx, rect, (dt_str, exp) = self.tooltip_rects[self.hover_index]
            tt_w = 120
            tt_h = 40
            tt_x = rect.x() - tt_w / 2 + box_size / 2
            if rect.y() - tt_h - 10 < 8:
                tt_y = rect.y() + box_size + 10
            else:
                tt_y = rect.y() - tt_h - 10

            # adjust if off-screen
            if tt_x < 0:
                tt_x = 10
            if tt_x + tt_w > w:
                tt_x = w - tt_w - 10
            tt_y = max(8, min(tt_y, h - tt_h - 8))

            painter.setPen(QPen(QColor(theme.border), 1))
            painter.setBrush(_chart_tooltip_bg(theme))
            painter.drawRoundedRect(QRectF(tt_x, tt_y, tt_w, tt_h), 6, 6)

            painter.setPen(_chart_text(theme))
            painter.setFont(QFont("Segoe UI", 9, QFont.Bold))
            painter.drawText(
                QRectF(tt_x, tt_y + 4, tt_w, 16),
                Qt.AlignCenter,
                eur(exp, theme.privacy),
            )
            painter.setFont(QFont("Segoe UI", 8))
            painter.setPen(_chart_muted(theme))
            painter.drawText(QRectF(tt_x, tt_y + 20, tt_w, 16), Qt.AlignCenter, dt_str)

        # Compact intensity legend keeps the colour encoding understandable.
        legend_y = min(h - 22, start_y + total_h + 12)
        if legend_y > start_y + total_h:
            painter.setFont(QFont("Segoe UI", 7))
            painter.setPen(QColor(theme.muted))
            painter.drawText(int(start_x), int(legend_y + 8), "Menos")
            legend_colors = (
                ["#0e4429", "#006d32", "#26a641", "#39d353"]
                if theme.dark
                else ["#9be9a8", "#40c463", "#30a14e", "#216e39"]
            )
            lx = start_x + 34
            for color in legend_colors:
                painter.setPen(Qt.NoPen)
                painter.setBrush(QColor(color))
                painter.drawRoundedRect(QRectF(lx, legend_y, 10, 10), 2, 2)
                lx += 13
            painter.setPen(QColor(theme.muted))
            painter.drawText(int(lx + 2), int(legend_y + 8), "Mais")
