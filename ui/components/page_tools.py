"""Small floating shortcuts for long application pages."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QPushButton, QWidget


class PageToolsBar(QFrame):
    """Keep top, filters and reset actions reachable while scrolling."""

    def __init__(
        self,
        parent: QWidget,
        *,
        on_top: Callable[[], None],
        on_filters: Callable[[], None],
        on_reset: Callable[[], None],
    ) -> None:
        super().__init__(parent)
        self.setObjectName("PageToolsBar")
        self.setAttribute(Qt.WA_StyledBackground, True)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(5, 5, 5, 5)
        layout.setSpacing(2)
        self.top_button = self._button(
            "\u2191 Topo", "Ir para o início da página", on_top
        )
        self.filters_button = self._button(
            "\u2699 Filtros",
            "Abrir e localizar os filtros desta página",
            on_filters,
        )
        self.reset_button = self._button(
            "\u21ba Repor",
            "Repor filtros, zoom e comparações desta página",
            on_reset,
        )
        layout.addWidget(self.top_button)
        layout.addWidget(self.filters_button)
        layout.addWidget(self.reset_button)
        self.adjustSize()
        self.hide()

    def _button(
        self,
        text: str,
        tooltip: str,
        callback: Callable[[], None],
    ) -> QPushButton:
        button = QPushButton(text, self)
        button.setObjectName("PageToolButton")
        button.setCursor(Qt.PointingHandCursor)
        button.setToolTip(tooltip)
        button.clicked.connect(callback)
        return button

    def set_context(self, *, filter_available: bool, scroll_available: bool) -> None:
        self.filters_button.setVisible(bool(filter_available))
        self.top_button.setEnabled(bool(scroll_available))
        self.adjustSize()
        self.setVisible(bool(filter_available or scroll_available))
        self.reposition()

    def reposition(self) -> None:
        parent = self.parentWidget()
        if parent is None:
            return
        self.adjustSize()
        margin = 14
        self.move(
            max(margin, parent.width() - self.width() - margin),
            max(margin, parent.height() - self.height() - margin),
        )
        self.raise_()


__all__ = ["PageToolsBar"]
