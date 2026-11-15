"""Responsive related-page switcher used by consolidated workspaces."""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QPushButton, QSizePolicy, QVBoxLayout, QWidget

from ui.core.layouts import FlowLayout

if TYPE_CHECKING:
    from main import LiquidFinanceApp
    from ui.navigation import WorkspaceSpec


class WorkspaceSwitcher(QWidget):
    """Keep secondary pages discoverable without crowding the sidebar."""

    def __init__(
        self,
        app: "LiquidFinanceApp",
        workspace: "WorkspaceSpec",
        active_key: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.app = app
        self.workspace = workspace
        self.buttons: dict[str, QPushButton] = {}
        self.setObjectName("WorkspaceSwitcher")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(5)
        title = QLabel(workspace.title)
        title.setObjectName("WorkspaceTitle")
        layout.addWidget(title)

        tabs = FlowLayout(hspacing=7, vspacing=7)
        for item in workspace.items:
            button = QPushButton(item.label)
            button.setObjectName("WorkspaceTab")
            button.setCursor(Qt.PointingHandCursor)
            button.setMinimumHeight(32)
            button.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
            button.setToolTip(f"Abrir {item.label.replace('  ', ' ')}")
            button.clicked.connect(
                lambda _checked=False, page_key=item.key: self.app.goto(page_key)
            )
            tabs.addWidget(button)
            self.buttons[item.key] = button
        layout.addLayout(tabs)
        self.set_active(active_key)

    def set_active(self, active_key: str) -> None:
        for key, button in self.buttons.items():
            button.setProperty("active", "true" if key == active_key else "false")
            button.style().unpolish(button)
            button.style().polish(button)
