import math
import random

from PySide6.QtCore import (
    Property,
    QEasingCurve,
    QPoint,
    QPointF,
    QPropertyAnimation,
    QRectF,
    Qt,
    QTimer,
)
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QRadialGradient
from PySide6.QtWidgets import (
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)


class AuroraBackgroundWidget(QWidget):
    def __init__(self, parent=None, is_dark=True):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.is_dark = is_dark
        self._time = 0.0

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._animate)
        self.timer.start(50)

    def set_dark_mode(self, is_dark: bool):
        self.is_dark = is_dark
        self.update()

    def _animate(self):
        self._time += 0.05
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()

        if self.is_dark:
            painter.fillRect(self.rect(), QColor("#090B10"))
            alpha_mult = 1.0
        else:
            painter.fillRect(self.rect(), QColor("#F1F5F9"))
            alpha_mult = (
                0.45  # Reduce opacity slightly in light mode for a pastel effect
            )

        cx1 = w * 0.5 + math.sin(self._time * 0.5) * (w * 0.3)
        cy1 = h * 0.5 + math.cos(self._time * 0.4) * (h * 0.3)
        r1 = max(w, h) * 0.7
        g1 = QRadialGradient(QPointF(cx1, cy1), r1)
        g1.setColorAt(0, QColor(0, 150, 255, int(80 * alpha_mult)))
        g1.setColorAt(1, QColor(0, 150, 255, 0))
        painter.fillRect(self.rect(), g1)

        cx2 = w * 0.5 + math.cos(self._time * 0.3) * (w * 0.4)
        cy2 = h * 0.5 + math.sin(self._time * 0.6) * (h * 0.4)
        r2 = max(w, h) * 0.8
        g2 = QRadialGradient(QPointF(cx2, cy2), r2)
        g2.setColorAt(0, QColor(150, 0, 255, int(70 * alpha_mult)))
        g2.setColorAt(1, QColor(150, 0, 255, 0))
        painter.fillRect(self.rect(), g2)
        painter.end()


class ToastNotification(QWidget):
    _tones = {
        "success": ("#22C55E", "rgba(34,197,94,0.16)"),
        "warning": ("#F59E0B", "rgba(245,158,11,0.17)"),
        "error": ("#EF4444", "rgba(239,68,68,0.16)"),
        "info": ("#3B82F6", "rgba(59,130,246,0.16)"),
        "neutral": ("#8B5CF6", "rgba(139,92,246,0.16)"),
    }

    def __init__(
        self,
        parent,
        message: str,
        icon: str = "🎉",
        duration_ms: int = 4000,
        *,
        title: str = "",
        tone: str = "info",
        anchor_widget=None,
        theme=None,
    ):
        super().__init__(parent)
        self.setWindowFlags(Qt.SubWindow | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setObjectName("FinanceToast")
        self.duration_ms = max(1500, int(duration_ms))
        self.anchor_widget = anchor_widget
        self.tone = tone if tone in self._tones else "info"
        self._elapsed_ms = 0

        accent, accent_soft = self._tones[self.tone]
        dark = bool(getattr(theme, "dark", True))
        surface = "rgba(15,23,42,246)" if dark else "rgba(255,255,255,248)"
        text = "#F8FAFC" if dark else "#142035"
        muted = "#CBD5E1" if dark else "#526077"
        border = "rgba(255,255,255,0.16)" if dark else "rgba(20,32,53,0.16)"
        close_hover = "rgba(255,255,255,0.12)" if dark else "rgba(20,32,53,0.08)"
        self.setStyleSheet(f"""
            QWidget#FinanceToast {{
                background-color: {surface};
                border: 1px solid {border};
                border-left: 4px solid {accent};
                border-radius: 17px;
            }}
            QLabel {{ background: transparent; border: none; color: {text}; }}
            QLabel#ToastIcon {{
                background: {accent_soft}; border: 1px solid {accent};
                border-radius: 21px; font-size: 21px;
            }}
            QLabel#ToastTitle {{ font-size: 14px; font-weight: 750; color: {text}; }}
            QLabel#ToastMessage {{ font-size: 12px; font-weight: 500; color: {muted}; }}
            QToolButton#ToastClose {{
                color: {muted}; background: transparent; border: none;
                border-radius: 11px; font-size: 16px; font-weight: 700;
            }}
            QToolButton#ToastClose:hover {{ background: {close_hover}; color: {text}; }}
            QProgressBar#ToastProgress {{
                min-height: 3px; max-height: 3px; border: none;
                border-radius: 1px; background: {accent_soft};
            }}
            QProgressBar#ToastProgress::chunk {{ background: {accent}; border-radius: 1px; }}
        """)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 12, 14, 9)
        outer.setSpacing(9)
        content = QHBoxLayout()
        content.setContentsMargins(0, 0, 0, 0)
        content.setSpacing(11)

        self.icon_lbl = QLabel(icon)
        self.icon_lbl.setObjectName("ToastIcon")
        self.icon_lbl.setFixedSize(42, 42)
        self.icon_lbl.setAlignment(Qt.AlignCenter)
        copy = QVBoxLayout()
        copy.setContentsMargins(0, 1, 0, 0)
        copy.setSpacing(3)
        self.title_lbl = QLabel(title)
        self.title_lbl.setObjectName("ToastTitle")
        self.title_lbl.setVisible(bool(title))
        self.msg_lbl = QLabel(message)
        self.msg_lbl.setObjectName("ToastMessage")
        self.msg_lbl.setWordWrap(True)
        self.msg_lbl.setMaximumWidth(310)
        copy.addWidget(self.title_lbl)
        copy.addWidget(self.msg_lbl)

        self.close_btn = QToolButton()
        self.close_btn.setObjectName("ToastClose")
        self.close_btn.setText("×")
        self.close_btn.setToolTip("Fechar notificação")
        self.close_btn.setCursor(Qt.PointingHandCursor)
        self.close_btn.setFixedSize(24, 24)
        self.close_btn.clicked.connect(self.fade_out)

        content.addWidget(self.icon_lbl, 0, Qt.AlignTop)
        content.addLayout(copy, 1)
        content.addWidget(self.close_btn, 0, Qt.AlignTop)
        outer.addLayout(content)

        self.progress = QProgressBar()
        self.progress.setObjectName("ToastProgress")
        self.progress.setRange(0, 1000)
        self.progress.setValue(1000)
        self.progress.setTextVisible(False)
        outer.addWidget(self.progress)

        target_width = min(420, max(320, parent.width() - 36)) if parent else 420
        self.setFixedWidth(target_width)
        self.setAccessibleName(title or "Notificação")
        self.setAccessibleDescription(message)

        self.adjustSize()

        self.opacity_eff = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self.opacity_eff)

        self._fade_timer = QTimer(self)
        self._fade_timer.setSingleShot(True)
        self._fade_timer.timeout.connect(self.fade_out)
        self._progress_timer = QTimer(self)
        self._progress_timer.setInterval(50)
        self._progress_timer.timeout.connect(self._update_progress)

    def _anchor_position(self, active) -> QPoint:
        parent = self.parentWidget()
        if parent is None:
            return QPoint(18, 82)
        x = max(18, parent.width() - self.width() - 24)
        y = 82
        anchor = self.anchor_widget
        try:
            if anchor is not None and anchor.isVisible():
                anchor_top_left = anchor.mapTo(parent, QPoint(0, 0))
                anchor_right = anchor_top_left.x() + anchor.width()
                x = anchor_right - self.width()
                y = anchor_top_left.y() + anchor.height() + 10
        except RuntimeError:
            self.anchor_widget = None
        try:
            position = active.index(self)
        except ValueError:
            position = len(active)
        y += sum(toast.height() + 10 for toast in active[:position])
        x = max(18, min(x, parent.width() - self.width() - 18))
        y = max(16, min(y, parent.height() - self.height() - 18))
        return QPoint(x, y)

    @staticmethod
    def _visible_group(parent) -> list:
        active = []
        for toast in list(getattr(parent, "_active_finance_toasts", [])):
            try:
                if toast.parent() is parent and toast.isVisible():
                    active.append(toast)
            except RuntimeError:
                continue
        return active

    def _move_to_group_position(self, active) -> None:
        target = self._anchor_position(active)
        if self.pos() == target:
            return
        animation = getattr(self, "slide_anim", None)
        if animation is not None and animation.state():
            animation.stop()
        self.move(target)

    def _reflow_group(self) -> None:
        try:
            parent = self.parentWidget()
        except RuntimeError:
            return
        if parent is None:
            return
        active = self._visible_group(parent)
        setattr(parent, "_active_finance_toasts", active)
        for toast in active:
            toast._move_to_group_position(active)

    def _update_progress(self):
        self._elapsed_ms += self._progress_timer.interval()
        remaining = max(0.0, 1.0 - self._elapsed_ms / self.duration_ms)
        self.progress.setValue(round(remaining * 1000))
        # The dashboard can finish its layout after the first notice appears.
        # Recheck twice per second so every visible notice follows the same edge.
        if self._elapsed_ms % 500 == 0:
            self._reflow_group()

    def show_toast(self):
        if not self.parent():
            return

        parent = self.parent()
        active = []
        for toast in list(getattr(parent, "_active_finance_toasts", [])):
            try:
                if toast is not self and toast.parent() is parent and toast.isVisible():
                    active.append(toast)
            except RuntimeError:
                continue
        active.append(self)
        setattr(parent, "_active_finance_toasts", active)

        # Existing notices may have been positioned before the dashboard finished
        # its first layout. Realign them before animating the new notice.
        for toast in active[:-1]:
            toast._move_to_group_position(active)

        end_pos = self._anchor_position(active)
        # Fade in at the final position so the group remains perfectly aligned
        # and never overlaps while another notice is already visible.
        start_pos = QPoint(end_pos)

        self.move(start_pos)
        self.opacity_eff.setOpacity(0)
        self.show()
        self.raise_()

        self.fade_anim = QPropertyAnimation(self.opacity_eff, b"opacity")
        self.fade_anim.setDuration(400)
        self.fade_anim.setStartValue(0.0)
        self.fade_anim.setEndValue(1.0)

        self.slide_anim = QPropertyAnimation(self, b"pos")
        self.slide_anim.setDuration(400)
        self.slide_anim.setStartValue(start_pos)
        self.slide_anim.setEndValue(end_pos)
        self.slide_anim.setEasingCurve(QEasingCurve.OutBack)

        self.fade_anim.start()
        self.slide_anim.start()

        self._progress_timer.start()
        self._fade_timer.start(self.duration_ms)

    def fade_out(self):
        if hasattr(self, "fade_out_anim") and self.fade_out_anim.state():
            return
        self._fade_timer.stop()
        self._progress_timer.stop()
        self.fade_out_anim = QPropertyAnimation(self.opacity_eff, b"opacity")
        self.fade_out_anim.setDuration(500)
        self.fade_out_anim.setStartValue(1.0)
        self.fade_out_anim.setEndValue(0.0)
        self.fade_out_anim.finished.connect(self._finish_toast)
        self.fade_out_anim.start()

    def _finish_toast(self):
        parent = self.parent()
        if parent is not None:
            remaining = []
            for toast in list(getattr(parent, "_active_finance_toasts", [])):
                try:
                    if (
                        toast is not self
                        and toast.parent() is parent
                        and toast.isVisible()
                    ):
                        remaining.append(toast)
                except RuntimeError:
                    continue
            setattr(parent, "_active_finance_toasts", remaining)
            for toast in remaining:
                toast._move_to_group_position(remaining)
        self.deleteLater()


class SkeletonWidget(QWidget):
    def __init__(self, parent=None, border_radius=12):
        super().__init__(parent)
        self.border_radius = border_radius
        self._progress = 0.0
        self.anim = QPropertyAnimation(self, b"progress")
        self.anim.setDuration(1200)
        self.anim.setStartValue(0.0)
        self.anim.setEndValue(1.0)
        self.anim.setLoopCount(-1)  # infinite
        self.anim.start()

    @Property(float)
    def progress(self):
        return self._progress

    @progress.setter
    def progress(self, val):
        self._progress = val
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        theme_dark = True
        w = self.window()
        if hasattr(w, "theme"):
            theme_dark = bool(getattr(w.theme, "dark", True))

        bg_col = QColor(255, 255, 255, 10) if theme_dark else QColor(0, 0, 0, 10)
        fg_col = QColor(255, 255, 255, 25) if theme_dark else QColor(0, 0, 0, 25)

        grad = QLinearGradient(0, 0, self.width() * 2, 0)
        offset = self._progress * 2.0 - 1.0  # -1 to 1

        grad.setColorAt(max(0.0, min(1.0, offset)), bg_col)
        grad.setColorAt(max(0.0, min(1.0, offset + 0.2)), fg_col)
        grad.setColorAt(max(0.0, min(1.0, offset + 0.4)), bg_col)

        painter.setPen(Qt.NoPen)
        painter.setBrush(grad)
        painter.drawRoundedRect(self.rect(), self.border_radius, self.border_radius)


class ConfettiWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.particles = []
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.update_particles)

    def burst(self):
        self.particles = []
        colors = ["#FF3B30", "#34C759", "#007AFF", "#FFCC00", "#AF52DE", "#FF9500"]
        for _ in range(80):
            self.particles.append(
                {
                    "x": self.width() / 2,
                    "y": self.height() / 2 + 100,
                    "vx": random.uniform(-15, 15),
                    "vy": random.uniform(-25, -10),
                    "color": QColor(random.choice(colors)),
                    "size": random.uniform(6, 12),
                    "rotation": random.uniform(0, 360),
                    "rspeed": random.uniform(-10, 10),
                }
            )
        self.timer.start(30)
        QTimer.singleShot(4000, self.timer.stop)
        self.show()
        self.raise_()

    def update_particles(self):
        alive = []
        for p in self.particles:
            p["vy"] += 0.8  # gravity
            p["x"] += p["vx"]
            p["y"] += p["vy"]
            p["rotation"] += p["rspeed"]
            if p["y"] < self.height() + 20:
                alive.append(p)
        self.particles = alive
        self.update()
        if not self.particles:
            self.timer.stop()
            self.hide()

    def paintEvent(self, event):
        if not self.particles:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        for p in self.particles:
            painter.save()
            painter.translate(p["x"], p["y"])
            painter.rotate(p["rotation"])
            painter.setBrush(p["color"])
            painter.setPen(Qt.NoPen)
            painter.drawRect(
                QRectF(
                    float(-p["size"] / 2),
                    float(-p["size"] / 2),
                    float(p["size"]),
                    float(p["size"]),
                )
            )
            painter.restore()
