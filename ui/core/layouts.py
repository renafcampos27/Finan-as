#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Finanças Pessoais – Liquid Glass
Nova interface PySide6 para a app local de finanças pessoais.
Mantém a base de dados e a app clássica intactas; esta é apenas uma camada visual moderna.
"""

from __future__ import annotations

import sys
from typing import Any, Optional

from utils.qt_runtime import configure_qt_runtime

configure_qt_runtime()

try:
    from PySide6.QtCore import QPointF, QRect, QSize, Qt
    from PySide6.QtWidgets import QLayout, QSizePolicy, QWidget
except (
    Exception
) as exc:  # pragma: no cover - only used when PySide6 is missing on the user's PC.
    print("PySide6 não está instalado. Instala com: pip install PySide6")
    print(exc)
    sys.exit(1)


class FlowLayout(QLayout):
    """Small responsive layout that wraps controls to new rows.

    Qt box/grid layouts keep fixed columns and can clip widgets when the window
    becomes narrower. This layout repositions children across multiple rows so
    cards and AI action buttons stay legible on different window sizes.
    """

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        margin: int = 0,
        hspacing: int = 8,
        vspacing: int = 8,
    ):
        super().__init__(parent)
        self._items: list[Any] = []
        self._hspacing = hspacing
        self._vspacing = vspacing
        self.setContentsMargins(margin, margin, margin, margin)

    def addItem(self, item):  # type: ignore[override]
        self._items.append(item)

    def count(self) -> int:  # type: ignore[override]
        return len(self._items)

    def itemAt(self, index: int):  # type: ignore[override]
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int):  # type: ignore[override]
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def expandingDirections(self):  # type: ignore[override]
        return Qt.Orientations(Qt.Orientation(0))

    def hasHeightForWidth(self) -> bool:  # type: ignore[override]
        return True

    def heightForWidth(self, width: int) -> int:  # type: ignore[override]
        return self._do_layout(QRect(0, 0, width, 0), test_only=True)

    def setGeometry(self, rect):  # type: ignore[override]
        super().setGeometry(rect)
        self._do_layout(rect, test_only=False)

    def sizeHint(self) -> QSize:  # type: ignore[override]
        parent = self.parentWidget()
        if parent is not None and parent.width() > 0:
            h = self.heightForWidth(parent.width())
            return QSize(parent.width(), max(self.minimumSize().height(), h))
        return self.minimumSize()

    def minimumSize(self) -> QSize:  # type: ignore[override]
        size = QSize(0, 0)
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        m = self.contentsMargins()
        size += QSize(m.left() + m.right(), m.top() + m.bottom())
        return size

    def _do_layout(self, rect: QRect, test_only: bool = False) -> int:
        m = self.contentsMargins()
        effective = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        x = effective.x()
        y = effective.y()
        line_height = 0
        available_width = max(1, effective.width())
        for item in self._items:
            hint = item.sizeHint()
            min_size = item.minimumSize()
            item_w = max(min_size.width(), hint.width())
            widget = item.widget()
            if (
                widget is not None
                and widget.sizePolicy().horizontalPolicy() == QSizePolicy.Policy.Ignored
            ):
                # A wrapping label with an ignored horizontal size hint would
                # otherwise receive a zero-width rectangle and disappear.
                item_w = available_width
            # Let wide controls shrink to the available row width instead of being clipped.
            item_w = min(item_w, available_width)
            item_h = max(min_size.height(), hint.height())
            if item.hasHeightForWidth():
                fitted_height = item.heightForWidth(max(1, item_w))
                if fitted_height >= 0:
                    # The one-line size hint of a wrapping label may have been
                    # measured before it received its real width.  Prefer the
                    # width-aware result so compact rows do not retain stale
                    # blank height.
                    item_h = max(min_size.height(), fitted_height)
            next_x = x + item_w + self._hspacing
            if x > effective.x() and next_x - self._hspacing > effective.right():
                x = effective.x()
                y += line_height + self._vspacing
                next_x = x + item_w + self._hspacing
                line_height = 0
            if not test_only:
                item.setGeometry(QRect(QPointF(x, y).toPoint(), QSize(item_w, item_h)))
            x = next_x
            line_height = max(line_height, item_h)
        return y + line_height - rect.y() + m.bottom()


def clear_layout_recursive(layout: Optional[QLayout]) -> None:
    """Recursively remove and delete all widgets and nested sublayouts in a QLayout."""
    if layout is None:
        return
    while layout.count():
        item = layout.takeAt(0)
        if item is None:
            continue
        widget = item.widget()
        if widget is not None:
            widget.hide()
            widget.setParent(None)
            widget.deleteLater()
        elif item.layout() is not None:
            clear_layout_recursive(item.layout())

