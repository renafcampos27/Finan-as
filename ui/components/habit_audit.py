"""Compact, explainable auditor for movements used by habit simulations."""

from __future__ import annotations

import unicodedata
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QGridLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)
from ui.core.smart_table import SmartTableWidgetItem as QTableWidgetItem

from services.habit_personalization import normalise_habit_audit_rules
from utils.helpers import eur


def _fold(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or "").casefold())
    return " ".join("".join(ch for ch in text if not unicodedata.combining(ch)).split())


class HabitAuditDialog(QDialog):
    ROLE_ID = Qt.UserRole
    ROLE_BUILT_IN = Qt.UserRole + 1
    ROLE_SEARCH = Qt.UserRole + 2
    ROLE_TOUCHED = Qt.UserRole + 3

    def __init__(
        self,
        *,
        habit_name: str,
        rows: list[dict[str, Any]],
        audit_rules: dict[str, Any],
        privacy: bool,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Movimentos usados · {habit_name}")
        self.setModal(True)
        self.resize(820, 560)
        self.setMinimumSize(680, 480)
        self.setMaximumSize(920, 620)
        self._privacy = bool(privacy)
        self._source_rows = list(rows)
        self._updating = False

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(9)
        title = QLabel(f"🔍 Rever movimentos de {habit_name}")
        title.setObjectName("DialogTitle")
        root.addWidget(title)
        intro = QLabel(
            "Marca apenas os movimentos que pertencem a este hábito. As alterações "
            "afetam a média, a frequência e os futuros planos, mas nunca alteram os movimentos."
        )
        intro.setObjectName("Subtle")
        intro.setWordWrap(True)
        root.addWidget(intro)

        controls = QGridLayout()
        controls.setHorizontalSpacing(10)
        controls.setVerticalSpacing(5)
        controls.addWidget(QLabel("Filtrar"), 0, 0)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Descrição, comerciante ou categoria…")
        self.search.setClearButtonEnabled(True)
        controls.addWidget(self.search, 0, 1)
        controls.addWidget(QLabel("Palavras-chave pessoais"), 1, 0)
        self.keywords = QLineEdit()
        normalised = normalise_habit_audit_rules(audit_rules)
        self.keywords.setText(", ".join(normalised["keywords"]))
        self.keywords.setPlaceholderText("ex.: pastelaria central, café trabalho")
        controls.addWidget(self.keywords, 1, 1)
        controls.setColumnStretch(1, 1)
        root.addLayout(controls)

        self.table = QTableWidget(0, 6)
        self.table.setProperty("columnPersistenceId", "simulations.habit_audit")
        self.table.setObjectName("HabitAuditTable")
        self.table.setHorizontalHeaderLabels(
            ["Usar", "Data", "Descrição", "Categoria", "Valor", "Origem"]
        )
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(38)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        header.setSectionResizeMode(3, QHeaderView.Interactive)
        header.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeToContents)
        self._fill_rows()
        root.addWidget(self.table, 1)

        bottom = QGridLayout()
        self.summary = QLabel()
        self.summary.setObjectName("SimulationControlValue")
        bottom.addWidget(self.summary, 0, 0)
        self.reset_btn = QPushButton("↺ Repor deteção automática")
        self.reset_btn.setObjectName("CardAction")
        bottom.addWidget(self.reset_btn, 0, 1, Qt.AlignRight)
        root.addLayout(bottom)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        save_button = buttons.button(QDialogButtonBox.Save)
        if save_button is not None:
            save_button.setText("Guardar e recalcular")
            save_button.setObjectName("Primary")
        cancel_button = buttons.button(QDialogButtonBox.Cancel)
        if cancel_button is not None:
            cancel_button.setText("Cancelar")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self.search.textChanged.connect(self._filter_rows)
        self.keywords.textChanged.connect(self._keywords_changed)
        self.table.itemChanged.connect(self._item_changed)
        self.reset_btn.clicked.connect(self._reset_detection)
        self._update_summary()

    @staticmethod
    def _origin(row: dict[str, Any]) -> str:
        labels = {
            "keyword": "Palavra-chave da app",
            "category": "Categoria",
            "custom": "Regra pessoal",
            "manual": "Incluído manualmente",
            "excluded": "Excluído manualmente",
        }
        return labels.get(str(row.get("match_kind") or ""), "Não detetado")

    def _fill_rows(self) -> None:
        self._updating = True
        try:
            self.table.setRowCount(len(self._source_rows))
            for index, row in enumerate(self._source_rows):
                use_item = QTableWidgetItem("")
                use_item.setFlags(
                    Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsUserCheckable
                )
                use_item.setCheckState(
                    Qt.Checked if bool(row.get("selected")) else Qt.Unchecked
                )
                use_item.setData(self.ROLE_ID, int(row.get("id", 0) or 0))
                use_item.setData(
                    self.ROLE_BUILT_IN,
                    bool(row.get("built_in_match")),
                )
                use_item.setData(
                    self.ROLE_TOUCHED,
                    str(row.get("match_kind") or "") in {"manual", "excluded"},
                )
                searchable = " ".join(
                    str(row.get(field) or "")
                    for field in (
                        "financial_date",
                        "description",
                        "merchant",
                        "category",
                        "subcategory",
                    )
                )
                use_item.setData(self.ROLE_SEARCH, _fold(searchable))
                values = [
                    use_item,
                    QTableWidgetItem(str(row.get("financial_date") or "—")),
                    QTableWidgetItem(str(row.get("description") or "—")),
                    QTableWidgetItem(str(row.get("category") or "—")),
                    QTableWidgetItem(eur(row.get("amount", 0), self._privacy)),
                    QTableWidgetItem(self._origin(row)),
                ]
                values[4].setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                for column, item in enumerate(values):
                    self.table.setItem(index, column, item)
                self.table.setRowHeight(index, 38)
        finally:
            self._updating = False

    def _custom_keywords(self) -> list[str]:
        return normalise_habit_audit_rules(
            {"keywords": self.keywords.text().split(",")}
        )["keywords"]

    def _keywords_changed(self, _text: str) -> None:
        keywords = tuple(_fold(value) for value in self._custom_keywords())
        self._updating = True
        try:
            for row in range(self.table.rowCount()):
                item = self.table.item(row, 0)
                if item is None or bool(item.data(self.ROLE_TOUCHED)):
                    continue
                searchable = str(item.data(self.ROLE_SEARCH) or "")
                custom = any(value and value in searchable for value in keywords)
                selected = bool(item.data(self.ROLE_BUILT_IN)) or custom
                item.setCheckState(Qt.Checked if selected else Qt.Unchecked)
                origin = self.table.item(row, 5)
                if origin is not None:
                    origin.setText(
                        "Palavra-chave da app"
                        if bool(item.data(self.ROLE_BUILT_IN))
                        else ("Regra pessoal" if custom else "Não detetado")
                    )
        finally:
            self._updating = False
        self._update_summary()

    def _item_changed(self, item: QTableWidgetItem) -> None:
        if self._updating or item.column() != 0:
            return
        item.setData(self.ROLE_TOUCHED, True)
        origin = self.table.item(item.row(), 5)
        if origin is not None:
            origin.setText(
                "Incluído manualmente"
                if item.checkState() == Qt.Checked
                else "Excluído manualmente"
            )
        self._update_summary()

    def _filter_rows(self, text: str) -> None:
        needle = _fold(text)
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            searchable = str(item.data(self.ROLE_SEARCH) or "") if item else ""
            self.table.setRowHidden(row, bool(needle and needle not in searchable))

    def _reset_detection(self) -> None:
        self._updating = True
        try:
            self.keywords.clear()
            for row in range(self.table.rowCount()):
                item = self.table.item(row, 0)
                if item is None:
                    continue
                item.setData(self.ROLE_TOUCHED, False)
                selected = bool(item.data(self.ROLE_BUILT_IN))
                item.setCheckState(Qt.Checked if selected else Qt.Unchecked)
                origin = self.table.item(row, 5)
                if origin is not None:
                    origin.setText(
                        "Palavra-chave da app" if selected else "Não detetado"
                    )
        finally:
            self._updating = False
        self._update_summary()

    def _update_summary(self) -> None:
        selected = sum(
            1
            for row in range(self.table.rowCount())
            if self.table.item(row, 0) is not None
            and self.table.item(row, 0).checkState() == Qt.Checked
        )
        self.summary.setText(
            f"{selected} usados · {self.table.rowCount() - selected} ignorados"
        )

    def rules(self) -> dict[str, Any]:
        keywords = self._custom_keywords()
        folded_keywords = tuple(_fold(value) for value in keywords)
        included: list[int] = []
        excluded: list[int] = []
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item is None:
                continue
            identifier = int(item.data(self.ROLE_ID) or 0)
            searchable = str(item.data(self.ROLE_SEARCH) or "")
            automatic = bool(item.data(self.ROLE_BUILT_IN)) or any(
                value and value in searchable for value in folded_keywords
            )
            selected = item.checkState() == Qt.Checked
            if identifier > 0 and selected and not automatic:
                included.append(identifier)
            elif identifier > 0 and not selected and automatic:
                excluded.append(identifier)
        return normalise_habit_audit_rules(
            {
                "included_transaction_ids": included,
                "excluded_transaction_ids": excluded,
                "keywords": keywords,
            }
        )


__all__ = ["HabitAuditDialog"]
