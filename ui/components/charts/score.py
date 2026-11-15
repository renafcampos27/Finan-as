from typing import Callable

from PySide6.QtCore import Qt, QRect
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QWidget

from ui.components.charts.utils import _chart_grid
from ui.core import Theme

RED = "#FF3B30"
GREEN = "#34C759"


class ScoreGauge(QWidget):
    def __init__(self, theme_provider: Callable[[], Theme], score: int = 0):
        super().__init__()
        self.theme_provider = theme_provider
        self.score = score
        self.setMinimumSize(140, 140)
        self.setMaximumSize(180, 180)

    def paintEvent(self, event):
        p = QPainter(self)
        try:
            p.setRenderHint(QPainter.Antialiasing)
            theme = self.theme_provider()

            rect = self.rect()
            size = min(rect.width(), rect.height()) - 30
            x = (rect.width() - size) // 2
            y = (rect.height() - size) // 2

            # Background arc
            bg_pen = QPen(_chart_grid(theme), 16, Qt.SolidLine, Qt.RoundCap)
            p.setPen(bg_pen)
            p.drawArc(x, y, size, size, -210 * 16, 240 * 16)

            # Foreground arc
            if self.score > 0:
                if self.score >= 80:
                    color = QColor(48, 209, 88)
                elif self.score >= 50:
                    color = QColor(255, 159, 10)
                else:
                    color = QColor(255, 69, 58)

                fg_pen = QPen(color, 16, Qt.SolidLine, Qt.RoundCap)
                p.setPen(fg_pen)
                span = int(-240 * (self.score / 100) * 16)
                p.drawArc(x, y, size, size, -210 * 16, span)

                # Glow effect
                glow_pen = QPen(
                    QColor(color.red(), color.green(), color.blue(), 60),
                    24,
                    Qt.SolidLine,
                    Qt.RoundCap,
                )
                p.setPen(glow_pen)
                p.drawArc(x, y, size, size, -210 * 16, span)

            # Draw text in center
            p.setPen(theme.text)
            p.setFont(QFont("Segoe UI", 32, QFont.Bold))
            p.drawText(
                QRect(rect.x(), rect.y() - 10, rect.width(), rect.height()),
                Qt.AlignCenter,
                str(self.score),
            )

            p.setFont(QFont("Segoe UI", 11))
            p.setPen(theme.muted)
            p.drawText(
                QRect(rect.x(), rect.y() + 35, rect.width(), rect.height()),
                Qt.AlignCenter,
                "/ 100",
            )
        finally:
            p.end()
