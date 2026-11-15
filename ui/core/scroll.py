"""Scroll helpers for dense pages and nested data regions."""

from __future__ import annotations

from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import QAbstractScrollArea, QScrollArea, QSlider


class ScrollPassthroughSlider(QSlider):
    """Keep page scrolling from changing a slider under the pointer."""

    def wheelEvent(self, event: QWheelEvent) -> None:  # type: ignore[override]
        ancestor = self.parentWidget()
        while ancestor is not None:
            if isinstance(ancestor, QAbstractScrollArea):
                event.ignore()
                ancestor.wheelEvent(event)
                if event.isAccepted():
                    return
            ancestor = ancestor.parentWidget()
        event.ignore()


class EdgePropagatingScrollArea(QScrollArea):
    """Let the page continue scrolling when a nested area reaches an edge."""

    def wheelEvent(self, event: QWheelEvent) -> None:  # type: ignore[override]
        bar = self.verticalScrollBar()
        direction = event.angleDelta().y()
        at_top = bar.value() <= bar.minimum()
        at_bottom = bar.value() >= bar.maximum()
        if (direction > 0 and at_top) or (direction < 0 and at_bottom):
            event.ignore()
            return
        super().wheelEvent(event)
