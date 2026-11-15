"""Compact, readable history of reversible finance changes."""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QSize
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
)

if TYPE_CHECKING:
    from main import LiquidFinanceApp


class ChangeHistoryDialog(QDialog):
    def __init__(self, app: "LiquidFinanceApp") -> None:
        super().__init__(app)
        self.app = app
        self.setWindowTitle("Histórico de alterações")
        self.setMinimumSize(620, 440)
        self.resize(720, 520)
        self.setStyleSheet(app.styleSheet())

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(10)
        title = QLabel("🕘 Alterações recentes")
        title.setObjectName("SectionTitle")
        hint = QLabel(
            "Importações, edições e eliminações reversíveis. "
            "A alteração disponível mais recente pode ser desfeita."
        )
        hint.setObjectName("Subtle")
        hint.setWordWrap(True)
        self.list = QListWidget()
        self.list.setAlternatingRowColors(True)
        self.list.setSelectionMode(QListWidget.NoSelection)
        self.status = QLabel()
        self.status.setObjectName("Subtle")

        self.undo = QPushButton("↶ Desfazer alteração mais recente")
        self.undo.setObjectName("Primary")
        self.undo.clicked.connect(self._undo_latest)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)

        layout.addWidget(title)
        layout.addWidget(hint)
        layout.addWidget(self.list, 1)
        layout.addWidget(self.status)
        layout.addWidget(self.undo)
        layout.addWidget(buttons)
        self.reload()

    def reload(self) -> None:
        self.list.clear()
        rows = self.app.change_journal_service.history(60)
        available = 0
        for row in rows:
            is_available = str(row.get("status")) == "available"
            available += int(is_available)
            state = "🟢 Disponível" if is_available else "↶ Desfeita"
            created = str(row.get("created_at") or "").replace("T", " ")
            item = QListWidgetItem(
                f"{state}   {row.get('label', 'Alteração')}\n          {created}"
            )
            item.setToolTip(str(row.get("action_type") or "alteração"))
            item.setSizeHint(QSize(0, 52))
            self.list.addItem(item)
        if not rows:
            self.list.addItem("Ainda não existem alterações registadas.")
        self.status.setText(
            f"{len(rows)} registo(s) mostrado(s) · {available} por desfazer."
        )
        self.undo.setEnabled(
            self.app.change_journal_service.latest_available() is not None
        )

    def _undo_latest(self) -> None:
        if self.app.undo_latest_change():
            self.reload()
