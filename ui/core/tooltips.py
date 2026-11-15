"""Readable in-app tooltips that do not depend on the Windows native theme."""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QEvent, QObject, QPoint, Qt
from PySide6.QtGui import QColor, QHelpEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QGraphicsDropShadowEffect,
    QLabel,
    QVBoxLayout,
    QWidget,
    QToolTip,
)

from ui.core.accessibility import plain_accessible_text

try:
    from shiboken6 import isValid as _shiboken_is_valid
except ImportError:  # pragma: no cover - shipped with PySide6
    _shiboken_is_valid = None


class ReadableToolTip(QFrame):
    """A compact light tooltip rendered inside the application window."""

    def __init__(self, parent: QWidget, accent: str = "#0A84FF") -> None:
        super().__init__(parent)
        self.setObjectName("ReadableToolTip")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(0)
        self.text_label = QLabel()
        self.text_label.setObjectName("ReadableToolTipText")
        self.text_label.setTextFormat(Qt.PlainText)
        self.text_label.setWordWrap(True)
        self.text_label.setMaximumWidth(300)
        layout.addWidget(self.text_label)

        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(18)
        shadow.setOffset(0, 5)
        shadow.setColor(QColor(15, 23, 42, 75))
        self.setGraphicsEffect(shadow)
        self.set_accent(accent)
        self.hide()

    def set_accent(self, accent: str) -> None:
        self.setStyleSheet(
            f"""
            QFrame#ReadableToolTip {{
                background-color: #FFFFFF;
                border: 2px solid {accent};
                border-radius: 11px;
            }}
            QLabel#ReadableToolTipText {{
                background: transparent;
                border: none;
                color: #0F172A;
                font-size: 13px;
                font-weight: 600;
            }}
            """
        )

    def show_text(self, text: str, source: QWidget, global_position: QPoint) -> None:
        host = source.window()
        if not isinstance(host, QWidget) or not host.isVisible():
            self.hide()
            return
        if self.parentWidget() is not host:
            self.setParent(host)
            self.setWindowFlags(Qt.Widget)

        clean_text = str(text).strip()
        self.text_label.setText(clean_text)
        natural_width = self.text_label.fontMetrics().horizontalAdvance(clean_text) + 4
        content_width = max(170, min(300, natural_width))
        self.text_label.setFixedWidth(content_width)
        self.adjustSize()

        target = host.mapFromGlobal(global_position)
        x = target.x() + 14
        y = target.y() + 20
        if x + self.width() > host.width() - 10:
            x = target.x() - self.width() - 12
        if y + self.height() > host.height() - 10:
            y = target.y() - self.height() - 12
        x = max(10, min(x, max(10, host.width() - self.width() - 10)))
        y = max(10, min(y, max(10, host.height() - self.height() - 10)))
        self.move(x, y)
        self.raise_()
        self.show()


class ReadableToolTipManager(QObject):
    """Replace direct QWidget tooltips with the reliable in-app variant."""

    # Class-level fallback: the app-wide event filter can be invoked by
    # shiboken before instance __init__ finishes assigning ``_source``;
    # without this default every early event raises AttributeError.
    _source: Optional[QWidget] = None

    _HIDE_EVENTS = {
        QEvent.Type.Leave,
        QEvent.Type.Hide,
        QEvent.Type.Close,
        QEvent.Type.Destroy,
        QEvent.Type.MouseButtonPress,
        QEvent.Type.Wheel,
        QEvent.Type.KeyPress,
    }

    def __init__(
        self,
        owner: QWidget,
        *,
        accent: str = "#0A84FF",
    ) -> None:
        super().__init__(owner)
        self.owner = owner
        self.accent = accent
        self.popup = ReadableToolTip(owner, accent)
        self._source: Optional[QWidget] = None

    def _ensure_popup(self) -> ReadableToolTip:
        valid = True
        if _shiboken_is_valid is not None:
            valid = bool(_shiboken_is_valid(self.popup))
        else:  # pragma: no cover - compatibility fallback.
            try:
                self.popup.objectName()
            except RuntimeError:
                valid = False
        if not valid:
            self.popup = ReadableToolTip(self.owner, self.accent)
        return self.popup

    def set_accent(self, accent: str) -> None:
        self.accent = str(accent or "#0A84FF")
        self._ensure_popup().set_accent(self.accent)

    def hide_tooltip(self) -> None:
        self._ensure_popup().hide()
        self._source = None

    @staticmethod
    def _item_tooltip(watched: QWidget, event: QEvent) -> str:
        """Resolve Qt.ToolTipRole from table/list viewports."""
        if not isinstance(event, QHelpEvent):
            return ""
        view = watched.parentWidget()
        if not isinstance(view, QAbstractItemView) or watched is not view.viewport():
            return ""
        if bool(view.property("readableItemToolTipDisabled")):
            return ""
        index = view.indexAt(event.pos())
        if not index.isValid():
            return ""
        return str(index.data(Qt.ToolTipRole) or "").strip()

    @staticmethod
    def _widget_tooltip(watched: QWidget) -> str:
        """Resolve tooltips inherited through a child viewport or icon label."""

        current: Optional[QWidget] = watched
        for _ in range(8):
            if current is None:
                break
            text = str(current.toolTip() or "").strip()
            if text:
                return text
            if current.isWindow():
                break
            current = current.parentWidget()
        return ""

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if not isinstance(watched, QWidget):
            return False

        event_type = event.type()
        if event_type == QEvent.Type.ToolTip:
            text = self._item_tooltip(watched, event)
            if not text:
                text = self._widget_tooltip(watched)
            if not text:
                # No native tooltip path remains: on some Windows themes an
                # empty/late tooltip can still create the unreadable black box.
                QToolTip.hideText()
                self.hide_tooltip()
                return True
            text = plain_accessible_text(text)
            if isinstance(event, QHelpEvent):
                global_position = event.globalPos()
            else:
                global_position = watched.mapToGlobal(watched.rect().bottomLeft())
            self._source = watched
            self._ensure_popup().show_text(text, watched, global_position)
            return True

        if event_type == QEvent.Type.FocusIn:
            text = plain_accessible_text(self._widget_tooltip(watched))
            if text and not plain_accessible_text(watched.accessibleDescription()):
                watched.setAccessibleDescription(text)

        if watched is self._source and event_type in self._HIDE_EVENTS:
            self.hide_tooltip()
        return False
