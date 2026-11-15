"""Explicit reimbursement review inside the existing movements workflow."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QDate, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGridLayout,
    QLabel,
    QLayout,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from services.reimbursements import ReimbursementsService
from ui.core.form_accessibility import prepare_form_fields
from ui.core.widgets import EuroSpinBox, OptionalDateEdit
from utils.helpers import eur


def _error(parent: QWidget, detail: Any) -> None:
    message = QMessageBox(parent)
    message.setWindowTitle("Reembolsos")
    message.setIcon(QMessageBox.Warning)
    message.setTextFormat(Qt.PlainText)
    message.setText(str(detail))
    message.exec()


def _buttons(dialog: QDialog, caption: str) -> QDialogButtonBox:
    buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
    buttons.button(QDialogButtonBox.Save).setText(caption)
    buttons.button(QDialogButtonBox.Cancel).setText("Cancelar")
    buttons.button(QDialogButtonBox.Save).setObjectName("Secondary")
    buttons.button(QDialogButtonBox.Cancel).setObjectName("Secondary")
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    return buttons


class _ReviewDialog(QDialog):
    def showEvent(self, event) -> None:
        super().showEvent(event)
        available = self.screen().availableGeometry()
        width, height = max(1, available.width() - 24), max(1, available.height() - 24)
        self.layout().setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        self.setMinimumSize(min(300, width), min(160, height))
        self.resize(min(self.width(), width), min(self.height(), height))
        for buttons in self.findChildren(QDialogButtonBox):
            buttons.setOrientation(Qt.Vertical if width < 480 else Qt.Horizontal)


def _scroll_body(dialog: QDialog, layout: QVBoxLayout, body: QWidget) -> QScrollArea:
    scroll = QScrollArea(dialog)
    scroll.setWidgetResizable(True)
    scroll.setMinimumSize(0, 0)
    scroll.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
    scroll.setWidget(body)
    layout.addWidget(scroll, 1)
    return scroll


class ReimbursementClaimDialog(_ReviewDialog):
    def __init__(
        self, service: ReimbursementsService, transaction_id: int, parent=None
    ):
        super().__init__(parent)
        self.service, self.transaction_id = service, transaction_id
        self.created_id = None
        source = service.source(transaction_id)
        self.setWindowTitle("Novo pedido de reembolso")
        layout = QVBoxLayout(self)
        body = QWidget()
        body_layout = QVBoxLayout(body)
        form = QFormLayout()
        self.label = QLineEdit(str(source["description"] or "Reembolso"))
        self.counterpart = QLineEdit()
        self.amount = EuroSpinBox()
        self.amount.setMaximum(abs(float(source["amount"])))
        self.amount.setValue(abs(float(source["amount"])))
        self.due = OptionalDateEdit()
        self.due.setMinimumDate(QDate(2000, 1, 1))
        self.due.setSpecialValueText("Sem prazo")
        self.due.setDate(self.due.minimumDate())
        self.notes = QLineEdit()
        for name, field in [
            ("Pedido", self.label),
            ("Entidade/pessoa", self.counterpart),
            ("Valor esperado", self.amount),
            ("Prazo opcional", self.due),
            ("Notas", self.notes),
        ]:
            form.addRow(name, field)
        body_layout.addLayout(form)
        explanation = QLabel(
            "Este pedido não cria uma receita. O recebimento será ligado a um movimento já existente."
        )
        explanation.setWordWrap(True)
        body_layout.addWidget(explanation)
        self.scroll = _scroll_body(self, layout, body)
        self.buttons = _buttons(self, "Guardar pedido")
        layout.addWidget(self.buttons)
        prepare_form_fields(self)
        self.resize(460, 440)

    def accept(self) -> None:
        if self.parent() and self.parent().is_private():
            self.reject()
            return
        try:
            self.created_id = self.service.create(
                self.transaction_id,
                self.amount.value(),
                label=self.label.text(),
                counterpart=self.counterpart.text(),
                notes=self.notes.text(),
                due_date=""
                if self.due.date() == self.due.minimumDate()
                else self.due.date().toString("yyyy-MM-dd"),
            )
        except (ValueError, RuntimeError) as exc:
            _error(self, exc)
            return
        super().accept()


class ReimbursementReceiptDialog(_ReviewDialog):
    def __init__(self, service: ReimbursementsService, claim_id: int, parent=None):
        super().__init__(parent)
        self.service, self.claim_id = service, claim_id
        self.created_id = None
        self.setWindowTitle("Ligar recebimento existente")
        layout = QVBoxLayout(self)
        body = QWidget()
        body_layout = QVBoxLayout(body)
        form = QFormLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Descrição ou #ID do movimento")
        self.receipt = QComboBox()
        self.receipt.setMinimumContentsLength(18)
        self.amount = EuroSpinBox()
        form.addRow("Procurar entrada", self.search)
        form.addRow("Movimento recebido", self.receipt)
        form.addRow("Valor a atribuir", self.amount)
        body_layout.addLayout(form)
        note = QLabel(
            "Mostra até 200 entradas por pesquisa. Só liga dinheiro já registado; não cria outra receita. Datas financeiras futuras contam apenas quando ocorrerem."
        )
        note.setWordWrap(True)
        body_layout.addWidget(note)
        self.scroll = _scroll_body(self, layout, body)
        self.buttons = _buttons(self, "Ligar movimento")
        layout.addWidget(self.buttons)
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(180)
        self._search_timer.timeout.connect(self.populate)
        self.search.textChanged.connect(lambda *_: self._search_timer.start())
        self.receipt.currentIndexChanged.connect(self.update_amount)
        prepare_form_fields(self)
        self.populate()
        self.resize(560, 380)

    def populate(self) -> None:
        self.receipt.blockSignals(True)
        self.receipt.clear()
        for row in self.service.receipt_candidates(self.search.text()):
            self.receipt.addItem(
                f"#{row['id']} · {row['financial_date']} · {row['description']} · {eur(row['available_cents'] / 100)}",
                (row["id"], row["available_cents"]),
            )
        self.receipt.blockSignals(False)
        self.update_amount()

    def update_amount(self, *_args) -> None:
        selected = self.receipt.currentData()
        claim = self.service.repo.reimbursement_claim(self.claim_id)
        remaining = (
            max(0, claim["expected_cents"] - claim["allocated_cents"]) if claim else 0
        )
        maximum = min(remaining, selected[1]) if selected else 0
        self.amount.setMaximum(maximum / 100)
        self.amount.setValue(maximum / 100)
        self.buttons.button(QDialogButtonBox.Save).setEnabled(maximum > 0)

    def accept(self) -> None:
        if self.parent() and self.parent().is_private():
            self.reject()
            return
        selected = self.receipt.currentData()
        if selected is None:
            return
        try:
            self.created_id = self.service.link(
                self.claim_id, selected[0], self.amount.value()
            )
        except (ValueError, RuntimeError) as exc:
            _error(self, exc)
            return
        super().accept()


class ReimbursementsDialog(_ReviewDialog):
    changed = Signal()

    def __init__(
        self, service: ReimbursementsService, parent=None, *, source_transaction_id=None
    ):
        super().__init__(parent)
        self.service, self.source_transaction_id = service, source_transaction_id
        self.setWindowTitle("Carteira de reembolsos")
        self.setMinimumSize(320, 240)
        self.resize(900, 600)
        layout = QVBoxLayout(self)
        explanation = QLabel(
            "Pedidos ligados às despesas e aos respetivos documentos. Esperado, parcial e recebido são estados desta carteira; o estado geral «Revisto» não é alterado. Seleciona uma despesa em Movimentos para criar um pedido."
        )
        explanation.setWordWrap(True)
        explanation.setTextFormat(Qt.PlainText)
        layout.addWidget(explanation)
        body = QWidget()
        body_layout = QVBoxLayout(body)
        self.claims = QTableWidget(0, 7)
        self.claims.setAccessibleName("Pedidos de reembolso")
        self.claims.setHorizontalHeaderLabels(
            [
                "Pedido",
                "Estado",
                "Esperado",
                "Recebido",
                "Por receber",
                "Prazo",
                "Documentos",
            ]
        )
        self.claims.setSelectionBehavior(QTableWidget.SelectRows)
        self.claims.setSelectionMode(QTableWidget.SingleSelection)
        self.claims.setEditTriggers(QTableWidget.NoEditTriggers)
        self.claims.setTabKeyNavigation(False)
        body_layout.addWidget(self.claims)
        self.receipts = QTableWidget(0, 3)
        self.receipts.setAccessibleName("Recebimentos ligados ao pedido")
        self.receipts.setHorizontalHeaderLabels(
            ["Movimento", "Data financeira", "Valor ligado"]
        )
        self.receipts.setSelectionBehavior(QTableWidget.SelectRows)
        self.receipts.setSelectionMode(QTableWidget.SingleSelection)
        self.receipts.setEditTriggers(QTableWidget.NoEditTriggers)
        self.receipts.setTabKeyNavigation(False)
        body_layout.addWidget(self.receipts)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(body)
        layout.addWidget(scroll, 1)
        self.notice = QLabel()
        self.notice.setWordWrap(True)
        self.notice.setTextFormat(Qt.PlainText)
        layout.addWidget(self.notice)
        actions = QGridLayout()
        self.new_button = QPushButton("Novo pedido da despesa selecionada")
        self.link_button = QPushButton("Ligar recebimento")
        self.unlink_button = QPushButton("Remover ligação selecionada")
        self.delete_button = QPushButton("Remover pedido sem recebimentos")
        for index, button in enumerate(
            [self.new_button, self.link_button, self.unlink_button, self.delete_button]
        ):
            button.setObjectName("Secondary")
            actions.addWidget(button, index // 2, index % 2)
        body_layout.addLayout(actions)
        close = QDialogButtonBox(QDialogButtonBox.Close)
        close.button(QDialogButtonBox.Close).setText("Fechar")
        close.button(QDialogButtonBox.Close).setObjectName("Secondary")
        close.rejected.connect(self.reject)
        layout.addWidget(close)
        self.new_button.clicked.connect(self.new_claim)
        self.link_button.clicked.connect(self.link_receipt)
        self.unlink_button.clicked.connect(self.unlink_receipt)
        self.delete_button.clicked.connect(self.delete_claim)
        self.claims.itemSelectionChanged.connect(self.populate_receipts)
        self.receipts.itemSelectionChanged.connect(self.update_actions)
        self._private = self.is_private()
        self._privacy_timer = QTimer(self)
        self._privacy_timer.setInterval(100)
        self._privacy_timer.timeout.connect(self.sync_privacy)
        self._privacy_timer.start()
        self.refresh()

    def done(self, result: int) -> None:
        self._privacy_timer.stop()
        super().done(result)

    def closeEvent(self, event) -> None:
        self._privacy_timer.stop()
        super().closeEvent(event)

    def is_private(self) -> bool:
        parent = self.parent()
        app = getattr(parent, "app", None)
        theme = getattr(app, "theme", getattr(parent, "theme", None))
        return bool(getattr(theme, "privacy", False))

    def sync_privacy(self) -> None:
        if self._private != self.is_private():
            self._private = self.is_private()
            if self._private:
                for dialog in self.findChildren(QDialog):
                    for timer in dialog.findChildren(QTimer):
                        timer.stop()
                    for combo in dialog.findChildren(QComboBox):
                        blocked = combo.blockSignals(True)
                        combo.clear()
                        combo.blockSignals(blocked)
                    for amount in dialog.findChildren(EuroSpinBox):
                        blocked = amount.blockSignals(True)
                        amount.setValue(0)
                        amount.blockSignals(blocked)
                    for editor in dialog.findChildren(QLineEdit):
                        blocked = editor.blockSignals(True)
                        editor.clear()
                        editor.blockSignals(blocked)
                    dialog.reject()
            self.refresh()

    def selected_claim(self) -> int | None:
        row = self.claims.currentRow()
        item = self.claims.item(row, 0) if row >= 0 else None
        return item.data(Qt.UserRole) if item else None

    def selected_receipt(self) -> int | None:
        row = self.receipts.currentRow()
        item = self.receipts.item(row, 0) if row >= 0 else None
        return item.data(Qt.UserRole) if item else None

    def refresh(self) -> None:
        private = self.is_private()
        rows = self.service.list_claims()
        self.claims.setRowCount(0)
        states = {"expected": "Esperado", "partial": "Parcial", "received": "Recebido"}
        for row in rows:
            index = self.claims.rowCount()
            self.claims.insertRow(index)
            values = [
                row["label"],
                states[row["state"]],
                eur(row["expected_amount"]),
                eur(row["received_amount"]),
                eur(row["outstanding_amount"]),
                row["due_date"] or "Sem prazo",
                str(row["document_count"]),
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem("•••••" if private else str(value))
                if column == 0:
                    item.setData(Qt.UserRole, row["id"])
                self.claims.setItem(index, column, item)
        self.notice.setText(
            "Desativa o modo privado para rever e alterar os pedidos."
            if private
            else (
                "Ainda não há pedidos. Seleciona uma despesa em Movimentos e escolhe «Novo pedido»."
                if not rows
                else "Ligar ou remover um recebimento não cria nem apaga o movimento da conta."
            )
        )
        self.populate_receipts()

    def populate_receipts(self) -> None:
        self.receipts.setRowCount(0)
        identity = self.selected_claim()
        if identity:
            for row in self.service.repo.reimbursement_receipts(identity):
                index = self.receipts.rowCount()
                self.receipts.insertRow(index)
                for column, value in enumerate(
                    [
                        row["description"],
                        row["financial_date"],
                        eur(row["amount_cents"] / 100),
                    ]
                ):
                    item = QTableWidgetItem(
                        "•••••" if self.is_private() else str(value)
                    )
                    if column == 0:
                        item.setData(Qt.UserRole, row["id"])
                    self.receipts.setItem(index, column, item)
        self.update_actions()

    def update_actions(self) -> None:
        writable = not self.is_private()
        self.new_button.setEnabled(writable and self.source_transaction_id is not None)
        self.link_button.setEnabled(writable and self.selected_claim() is not None)
        self.delete_button.setEnabled(writable and self.selected_claim() is not None)
        self.unlink_button.setEnabled(writable and self.selected_receipt() is not None)

    def _notify(self) -> None:
        self.refresh()
        self.changed.emit()

    def new_claim(self) -> None:
        if self.is_private() or self.source_transaction_id is None:
            return
        try:
            dialog = ReimbursementClaimDialog(
                self.service, self.source_transaction_id, self
            )
        except (ValueError, RuntimeError) as exc:
            _error(self, exc)
            return
        try:
            if dialog.exec() == QDialog.DialogCode.Accepted:
                self._notify()
        finally:
            dialog.deleteLater()

    def link_receipt(self) -> None:
        identity = self.selected_claim()
        if self.is_private() or identity is None:
            return
        dialog = ReimbursementReceiptDialog(self.service, identity, self)
        try:
            if dialog.exec() == QDialog.DialogCode.Accepted:
                self._notify()
        finally:
            dialog.deleteLater()

    def unlink_receipt(self) -> None:
        identity = self.selected_receipt()
        if self.is_private() or identity is None:
            return
        try:
            self.service.unlink(identity)
            self._notify()
        except (ValueError, RuntimeError) as exc:
            _error(self, exc)

    def delete_claim(self) -> None:
        identity = self.selected_claim()
        if self.is_private() or identity is None:
            return
        try:
            self.service.delete(identity)
            self._notify()
        except (ValueError, RuntimeError) as exc:
            _error(self, exc)
