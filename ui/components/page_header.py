"""Unified page header component for standard title, subtitle, period and actions."""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ui.components.charts.presentation import chart_font


class PageHeaderWidget(QWidget):
    """Unified header with title, subtitle, period selector and context actions."""

    def __init__(
        self,
        title: str,
        subtitle: str = "",
        show_period_selector: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("PageHeaderWidget")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)

        main_layout = QHBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 12)
        main_layout.setSpacing(12)

        # Left: title and subtitle
        left_box = QVBoxLayout()
        left_box.setContentsMargins(0, 0, 0, 0)
        left_box.setSpacing(2)

        self.title_label = QLabel(title)
        self.title_label.setObjectName("PageHeaderTitle")
        self.title_label.setFont(chart_font("title", point_size=16, bold=True))
        left_box.addWidget(self.title_label)

        self.sub_label = QLabel(subtitle)
        self.sub_label.setObjectName("Subtle")
        self.sub_label.setFont(chart_font("label", point_size=11, bold=False))
        self.sub_label.setVisible(bool(subtitle))
        left_box.addWidget(self.sub_label)

        main_layout.addLayout(left_box, 1)

        # Center: optional period selector slot
        self.period_container = QWidget()
        self.period_container.setObjectName("PageHeaderPeriodContainer")
        self.period_layout = QHBoxLayout(self.period_container)
        self.period_layout.setContentsMargins(0, 0, 0, 0)
        self.period_layout.setSpacing(6)
        self.period_container.setVisible(show_period_selector)
        main_layout.addWidget(self.period_container, 0)

        # Right: context actions
        self.actions_container = QWidget()
        self.actions_container.setObjectName("PageHeaderActionsContainer")
        self.actions_layout = QHBoxLayout(self.actions_container)
        self.actions_layout.setContentsMargins(0, 0, 0, 0)
        self.actions_layout.setSpacing(8)
        main_layout.addWidget(self.actions_container, 0)

    def set_title(self, title: str) -> None:
        self.title_label.setText(str(title or ""))

    def set_subtitle(self, subtitle: str) -> None:
        text = str(subtitle or "").strip()
        self.sub_label.setText(text)
        self.sub_label.setVisible(bool(text))

    def add_action_button(
        self,
        text: str,
        icon: str = "",
        callback: Callable | None = None,
        variant: str = "Secondary",
    ) -> QPushButton:
        label = f"{icon} {text}".strip() if icon else text
        btn = QPushButton(label)
        btn.setObjectName(variant)
        btn.setCursor(Qt.PointingHandCursor)
        if callback is not None:
            btn.clicked.connect(callback)
        self.actions_layout.addWidget(btn)
        return btn
