#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Finanças Pessoais – Liquid Glass
Nova interface PySide6 para a app local de finanças pessoais.
Mantém a base de dados e a app clássica intactas; esta é apenas uma camada visual moderna.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from typing import Any, Optional

from utils.qt_runtime import configure_qt_runtime

configure_qt_runtime()

try:
    from PySide6.QtCore import QDate, Qt, QTimer
    from PySide6.QtGui import QColor
    from PySide6.QtWidgets import (
        QApplication,
        QAbstractItemView,
        QCheckBox,
        QComboBox,
        QDateEdit,
        QDialog,
        QDialogButtonBox,
        QDoubleSpinBox,
        QFileDialog,
        QFormLayout,
        QFrame,
        QGridLayout,
        QHBoxLayout,
        QHeaderView,
        QLabel,
        QLineEdit,
        QMessageBox,
        QPushButton,
        QScrollArea,
        QSizePolicy,
        QSpinBox,
        QTableWidget,
        QTextEdit,
        QVBoxLayout,
        QWidget,
    )
except (
    Exception
) as exc:  # pragma: no cover - only used when PySide6 is missing on the user's PC.
    print("PySide6 não está instalado. Instala com: pip install PySide6")
    print(exc)
    sys.exit(1)

from database.repo import Repo
from services.documents import AttachmentValidationError, DocumentService
from ui.core import (
    EuroSpinBox,
    PortugueseMoneyDelegate,
    prepare_date_edit,
    theme_for_widget,
)
from ui.core.smart_table import SmartTableWidgetItem as QTableWidgetItem
from ui.core.widgets import LiquidSheetDialog, OptionalDateEdit
from ui.core.form_accessibility import bind_field
# BUG-DLG-04: parse_date estrito deixou de ser usado neste módulo (substituído
# por parse_flexible_date, tolerante a timestamps ISO).
from utils.helpers import (
    format_eur_number,
    parse_dot_decimal,
    parse_eur_input,
    parse_flexible_date,
    row_get,
    safe_float,
    to_qdate,
)
from utils.app_logging import log_warning
from utils.output_security import html_text


class TransactionDialog(QDialog):
    def __init__(
        self, repo: Repo, parent: Optional[QWidget] = None, row: Optional[Any] = None
    ):
        super().__init__(parent)
        self.repo = repo
        self.row = row
        self._attachment_paths: list[Path] = []
        self.setWindowTitle("Editar movimento" if row is not None else "Novo movimento")
        self.setObjectName("TransactionDialog")
        self.setModal(True)
        self.setWindowFlag(Qt.WindowContextHelpButtonHint, False)
        # A centred, ordinary dialog stays crisp.  The former full-window sheet
        # blurred the parent and then expanded back to the complete app bounds.
        self.setMinimumSize(620, 520)
        self.setSizeGripEnabled(True)
        theme = theme_for_widget(parent)
        dialog_surface = "#111827" if theme.dark else "#F8FAFF"
        header_surface = "#172033" if theme.dark else "#EAF3FF"
        self.setStyleSheet(
            (parent.styleSheet() if parent else "")
            + f"""
            QDialog#TransactionDialog {{
                background-color: {dialog_surface};
            }}
            QFrame#TransactionDialogHeader {{
                background-color: {header_surface};
                border: 1px solid {theme.border};
                border-radius: 16px;
            }}
            QLabel#TransactionDialogIcon {{
                background-color: {theme.primary_color};
                color: white;
                border-radius: 14px;
                font-size: 20px;
            }}
            QLabel#TransactionDialogTitle {{
                color: {theme.text};
                font-weight: 800;
            }}
            QLabel#TransactionDialogSubtitle,
            QLabel#TransactionFieldLabel {{
                color: {theme.muted};
            }}
            QLabel#TransactionFieldLabel {{
                font-weight: 700;
            }}
            QWidget#TransactionDialogContent {{
                background: transparent;
            }}
            """
        )

        main = QVBoxLayout(self)
        main.setContentsMargins(16, 16, 16, 14)
        main.setSpacing(10)

        header = QFrame(self)
        header.setObjectName("TransactionDialogHeader")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(16, 12, 16, 12)
        header_layout.setSpacing(12)
        icon = QLabel("✏️" if row is not None else "➕", header)
        icon.setObjectName("TransactionDialogIcon")
        icon.setAlignment(Qt.AlignCenter)
        icon.setFixedSize(42, 42)
        header_layout.addWidget(icon)
        header_text = QVBoxLayout()
        header_text.setContentsMargins(0, 0, 0, 0)
        header_text.setSpacing(1)
        self.heading_label = QLabel(
            "Editar movimento" if row is not None else "Novo movimento",
            header,
        )
        self.heading_label.setObjectName("TransactionDialogTitle")
        self.heading_label.setWordWrap(True)
        subtitle = QLabel(
            "Revê os dados principais e guarda apenas quando tudo estiver correto.",
            header,
        )
        subtitle.setObjectName("TransactionDialogSubtitle")
        subtitle.setWordWrap(True)
        header_text.addWidget(self.heading_label)
        header_text.addWidget(subtitle)
        header_layout.addLayout(header_text, 1)
        main.addWidget(header)

        self._dialog_scroll = QScrollArea(self)
        self._dialog_scroll.setWidgetResizable(True)
        self._dialog_scroll.setFrameShape(QFrame.NoFrame)
        self._dialog_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)

        content = QWidget()
        content.setObjectName("TransactionDialogContent")
        content.setMinimumWidth(0)
        form = QGridLayout(content)
        form.setContentsMargins(8, 8, 8, 8)
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(7)
        form.setColumnStretch(1, 1)
        form.setColumnStretch(3, 1)
        self._field_labels: list[QLabel] = []

        def field_label(
            text: str,
            field: QWidget | None = None,
            *,
            accessible_name: str | None = None,
        ) -> QLabel:
            label = QLabel(text, content)
            label.setObjectName("TransactionFieldLabel")
            label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            label.setMinimumWidth(82)
            if field is not None:
                bind_field(label, field, accessible_name or text)
            self._field_labels.append(label)
            return label

        self.kind = QComboBox()
        self.kind.addItems(["Despesa", "Receita"])
        self.category = QComboBox()
        self.category_warning = QLabel()
        self.category_warning.setObjectName("WarningText")
        self.category_warning.setWordWrap(True)
        self.category_warning.hide()
        self.subcategory = QComboBox()
        self.subcategory.setEditable(True)
        self.subcategory.setToolTip(
            "Escreve uma nova subcategoria ou escolhe uma existente"
        )
        self.desc = QLineEdit()
        self.desc.setPlaceholderText("Ex.: Continente, salário, renda...")
        self.amount = EuroSpinBox()
        self.amount.setRange(0.01, 1_000_000)
        self.amount.setDecimals(2)
        self.amount.setSingleStep(1)
        self.amount.setSuffix(" €")
        self.account = QComboBox()
        self.account.setEditable(True)
        transaction_account_rows = (
            list(repo.financial_accounts())
            if callable(getattr(repo, "financial_accounts", None))
            else []
        )
        transaction_account_names = [
            str(row_get(item, "name") or "").strip()
            for item in transaction_account_rows
            if str(row_get(item, "name") or "").strip()
        ]
        if "Conta principal" not in transaction_account_names:
            transaction_account_names.insert(0, "Conta principal")
        self.account.addItems(transaction_account_names)
        self.payment_method = QComboBox()
        self.payment_method.setEditable(True)
        self.payment_method.addItems(
            [
                "À vista",
                "Cartão de crédito",
                "Cartão de débito",
                "Transferência",
                "Débito direto",
                "MB Way",
                "Dinheiro",
                "Outro",
            ]
        )
        self.merchant = QLineEdit()
        self.merchant.setPlaceholderText("Comerciante/entidade, opcional")
        self.tags = QLineEdit()
        self.tags.setPlaceholderText("etiquetas separadas por vírgula")
        self.purchase_date = QDateEdit()
        prepare_date_edit(self.purchase_date, theme_for_widget(parent))
        self.purchase_date.setDate(QDate.currentDate())
        self.payment_date = QDateEdit()
        prepare_date_edit(self.payment_date, theme_for_widget(parent))
        self.payment_date.setDate(QDate.currentDate())
        self.notes = QTextEdit()
        self.notes.setTabChangesFocus(True)
        self.notes.setMinimumHeight(86)
        self.notes.setMaximumHeight(125)

        for w in [
            self.kind,
            self.category,
            self.subcategory,
            self.desc,
            self.amount,
            self.account,
            self.payment_method,
            self.merchant,
            self.tags,
            self.purchase_date,
            self.payment_date,
        ]:
            try:
                w.setMinimumWidth(0)
                w.setMinimumHeight(36)
                w.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
            except AttributeError:
                pass

        self.kind.currentTextChanged.connect(lambda _text: self._reload_categories())
        self.category.currentIndexChanged.connect(self._category_selection_changed)
        self.category.currentTextChanged.connect(self._update_subcategories)

        if row is not None:
            meta = repo.transaction_meta_ext(int(row["id"]))
            amount = float(row["amount"] or 0)
            self.kind.blockSignals(True)
            self.kind.setCurrentText("Receita" if amount > 0 else "Despesa")
            self.kind.blockSignals(False)
            self._reload_categories(str(row["category"] or ""))
            self.amount.setValue(abs(amount))
            # BUG-DLG-04: linhas importadas/antigas podem trazer timestamps ISO
            # ("2026-08-01T14:30:00"); parse_date estrito (exigia 10 chars)
            # crashava com ValueError ao abrir o diálogo de edição.
            # parse_flexible_date tolera "T"/espaço + hora e devolve None em vez
            # de lançar; se falhar, o widget mantém a data por omissão (hoje).
            payment_day = parse_flexible_date(
                meta.get("payment_date") or row["date"]
            )
            self.payment_date.setDate(to_qdate(payment_day or date.today()))
            purchase_day = parse_flexible_date(
                meta.get("purchase_date") or row["date"]
            )
            self.purchase_date.setDate(to_qdate(purchase_day or date.today()))
            self.desc.setText(str(row["description"] or ""))
            self._update_subcategories()
            self.subcategory.setCurrentText(str(meta.get("subcategory") or ""))
            account = str(row["account"] or "Conta principal")
            if self.account.findText(account) < 0:
                self.account.addItem(account)
            self.account.setCurrentText(account)
            pm = str(meta.get("payment_method") or "")
            if pm:
                if self.payment_method.findText(pm) < 0:
                    self.payment_method.addItem(pm)
                self.payment_method.setCurrentText(pm)
            self.merchant.setText(str(meta.get("merchant") or ""))
            self.tags.setText(str(meta.get("tags") or ""))
            self.notes.setPlainText(str(row["notes"] or ""))
        else:
            self._reload_categories()
            self.account.setCurrentText("Conta principal")

        self.attach_btn = QPushButton("+ Adicionar faturas/anexos")
        self.attach_btn.setObjectName("Secondary")
        self.attach_btn.clicked.connect(self.choose_attachments)
        self.attach_label = QLabel("Nenhum anexo novo selecionado")
        self.attach_label.setTextFormat(Qt.PlainText)
        self.attach_label.setObjectName("Subtle")
        self.attach_label.setWordWrap(True)
        self.attach_label.setMinimumWidth(0)
        self.attach_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)

        form.addWidget(field_label("Tipo", self.kind), 0, 0)
        form.addWidget(self.kind, 0, 1)
        form.addWidget(field_label("Valor", self.amount), 0, 2)
        form.addWidget(self.amount, 0, 3)
        form.addWidget(field_label("Descrição", self.desc), 1, 0)
        form.addWidget(self.desc, 1, 1, 1, 3)
        form.addWidget(field_label("Categoria", self.category), 2, 0)
        form.addWidget(self.category, 2, 1)
        form.addWidget(field_label("Subcategoria", self.subcategory), 2, 2)
        form.addWidget(self.subcategory, 2, 3)
        form.addWidget(self.category_warning, 3, 1, 1, 3)
        form.addWidget(field_label("Data da compra", self.purchase_date), 4, 0)
        form.addWidget(self.purchase_date, 4, 1)
        form.addWidget(field_label("Pagamento", self.payment_date), 4, 2)
        form.addWidget(self.payment_date, 4, 3)
        form.addWidget(field_label("Conta", self.account), 5, 0)
        form.addWidget(self.account, 5, 1)
        form.addWidget(
            field_label(
                "Método", self.payment_method, accessible_name="Método de pagamento"
            ),
            5,
            2,
        )
        form.addWidget(self.payment_method, 5, 3)
        form.addWidget(field_label("Comerciante", self.merchant), 6, 0)
        form.addWidget(self.merchant, 6, 1)
        form.addWidget(field_label("Etiquetas", self.tags), 6, 2)
        form.addWidget(self.tags, 6, 3)
        form.addWidget(field_label("Notas", self.notes), 7, 0, Qt.AlignTop)
        form.addWidget(self.notes, 7, 1, 1, 3)
        form.addWidget(field_label("Anexos", self.attach_btn), 8, 0, Qt.AlignTop)
        form.addWidget(self.attach_btn, 8, 1)
        form.addWidget(self.attach_label, 8, 2, 1, 2)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        save_button = buttons.button(QDialogButtonBox.Save)
        cancel_button = buttons.button(QDialogButtonBox.Cancel)
        if save_button is not None:
            save_button.setText("Guardar movimento")
            save_button.setObjectName("Primary")
        if cancel_button is not None:
            cancel_button.setText("Cancelar")

        self._dialog_scroll.setWidget(content)
        main.addWidget(self._dialog_scroll, 1)
        button_area = QWidget()
        button_layout = QHBoxLayout(button_area)
        button_layout.setContentsMargins(4, 2, 4, 0)
        button_layout.addStretch(1)
        button_layout.addWidget(buttons)
        main.addWidget(button_area, 0)
        tab_fields = [
            self.kind, self.amount, self.desc, self.category, self.subcategory,
            self.purchase_date, self.payment_date, self.account, self.payment_method,
            self.merchant, self.tags, self.notes, self.attach_btn,
            save_button, cancel_button,
        ]
        for previous, following in zip(tab_fields, tab_fields[1:]):
            if previous is not None and following is not None:
                QWidget.setTabOrder(previous, following)

        # Apply after Qt calculates size hints, so the initial geometry fits the active screen.
        QTimer.singleShot(0, self, self._fit_initial_geometry)

    def _fit_initial_geometry(self) -> None:
        screen = self.screen() or QApplication.primaryScreen()
        if not screen:
            self.resize(760, 680)
            return
        available = screen.availableGeometry()
        self.setMinimumSize(
            min(620, max(1, available.width() - 24)),
            min(520, max(1, available.height() - 48)),
        )
        target_w = min(780, int(available.width() * 0.72))
        target_h = min(720, int(available.height() * 0.78))
        target_w = min(max(self.minimumWidth(), target_w), available.width() - 24)
        target_h = min(max(self.minimumHeight(), target_h), available.height() - 48)
        self.resize(target_w, target_h)
        parent = self.parentWidget()
        center = (
            parent.frameGeometry().center()
            if parent is not None and parent.isVisible()
            else available.center()
        )
        x = max(
            available.left(),
            min(center.x() - target_w // 2, available.right() - target_w + 1),
        )
        y = max(
            available.top(),
            min(center.y() - target_h // 2, available.bottom() - target_h + 1),
        )
        self.move(x, y)

    def _selected_category(self) -> str:
        return str(self.category.currentData() or self.category.currentText()).strip()

    def _reload_categories(self, preserve_category: object = "") -> None:
        selected = str(preserve_category or "").strip()
        kind = "income" if self.kind.currentText() == "Receita" else "expense"
        valid = list(self.repo.categories(kind))
        self.category.blockSignals(True)
        self.category.clear()
        for name in valid:
            self.category.addItem(str(name), str(name))
        if selected:
            match = next(
                (
                    index
                    for index in range(self.category.count())
                    if str(
                        self.category.itemData(index) or self.category.itemText(index)
                    )
                    == selected
                ),
                -1,
            )
            if match >= 0:
                self.category.setCurrentIndex(match)
                self.category_warning.hide()
            else:
                self.category.insertItem(0, f"⚠️ {selected} — incompatível", selected)
                self.category.setCurrentIndex(0)
                expected = "receita" if kind == "income" else "despesa"
                self.category_warning.setText(
                    f"⚠️ Este registo antigo usa uma categoria que não corresponde a {expected}. "
                    "Escolhe uma categoria válida antes de guardar."
                )
                self.category_warning.show()
        elif self.category.count() > 0:
            self.category.setCurrentIndex(0)
            self.category_warning.hide()
        self.category.blockSignals(False)
        self._update_subcategories()

    def _category_selection_changed(self, _index: int) -> None:
        category = self._selected_category()
        expected = "income" if self.kind.currentText() == "Receita" else "expense"
        if self.repo.category_kind(category) == expected:
            self.category_warning.hide()

    def _update_subcategories(self) -> None:
        cat = self._selected_category()
        subs = self.repo.subcategories(cat)
        self.subcategory.clear()
        if subs:
            self.subcategory.addItems([""] + subs)
        else:
            self.subcategory.setCurrentText("")

    def choose_attachments(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(
            self,
            "Escolher faturas/anexos",
            str(Path.home()),
            DocumentService.file_dialog_filter(),
        )
        if not files:
            return
        rejected: list[str] = []
        for f in files:
            path = Path(f)
            try:
                DocumentService.validate_source(path)
            except (AttachmentValidationError, FileNotFoundError, OSError) as exc:
                rejected.append(f"{path.name}: {exc}")
                continue
            if path not in self._attachment_paths:
                self._attachment_paths.append(path)
        if rejected:
            QMessageBox.warning(
                self,
                "Anexos não adicionados",
                "Alguns ficheiros foram recusados por segurança:\n\n"
                + "\n".join(rejected[:6]),
            )
        names = ", ".join(p.name for p in self._attachment_paths[:4])
        extra = (
            ""
            if len(self._attachment_paths) <= 4
            else f" +{len(self._attachment_paths) - 4}"
        )
        self.attach_label.setText(
            f"{len(self._attachment_paths)} anexo(s): {names}{extra}"
        )

    def _amount_value_from_editor(self) -> float:
        """Read the amount that is currently visible in the spinbox.

        Qt can keep the previous numeric value in QDoubleSpinBox until the edit
        is interpreted.  That was the real reason why, after pressing Save, the
        movements table sometimes kept the old value. We force interpretation
        and parse the visible text with the app's comma-only money rule.
        """
        try:
            self.amount.interpretText()
        except RuntimeError:
            pass
        candidates: list[str] = []
        try:
            candidates.append(self.amount.cleanText())
        except RuntimeError:
            pass
        try:
            if self.amount.lineEdit() is not None:
                candidates.append(self.amount.lineEdit().text())
        except RuntimeError:
            pass
        invalid_error: ValueError | None = None
        for raw in candidates:
            text = str(raw).strip()
            if not text:
                continue
            try:
                return parse_eur_input(text, field_name="valor")
            except ValueError as exc:
                invalid_error = exc
                continue
        if invalid_error is not None:
            raise invalid_error
        return float(self.amount.value())

    def accept(self) -> None:  # type: ignore[override]
        desc = self.desc.text().strip()
        if not desc:
            QMessageBox.warning(
                self,
                "Descrição em falta",
                "A descrição do movimento é obrigatória.",
            )
            self.desc.setFocus(Qt.FocusReason.OtherFocusReason)
            self.desc.selectAll()
            return
        try:
            amount = self._amount_value_from_editor()
        except ValueError as exc:
            QMessageBox.warning(self, "Valor inválido", str(exc))
            self.amount.setFocus(Qt.FocusReason.OtherFocusReason)
            self.amount.lineEdit().selectAll()
            return
        # BUG-AUD-C141: validate the amount inside accept() so a zero/invalid
        # value can never close the dialog and force the page to reopen a
        # warning modal (modal reentrancy).  The spinbox minimum clamps typed
        # zeros to 0.01, so the explicit cent-precision check is the only
        # barrier that keeps an effective zero from being submitted.
        if abs(round(amount, 2)) < 0.01:
            QMessageBox.warning(
                self,
                "Valor inválido",
                "O valor do movimento não pode ser zero. "
                "Corrige o valor para guardar — os dados preenchidos foram mantidos.",
            )
            self.amount.setFocus(Qt.FocusReason.OtherFocusReason)
            self.amount.lineEdit().selectAll()
            return
        signed = abs(amount) if self.kind.currentText() == "Receita" else -abs(amount)
        try:
            self.repo.validate_transaction_category(
                self._selected_category(),
                signed,
            )
        except ValueError as exc:
            QMessageBox.warning(self, "Categoria incompatível", str(exc))
            self.category.setFocus(Qt.FocusReason.OtherFocusReason)
            return
        super().accept()

    def extended_data(self) -> dict[str, Any]:
        signed_amount = self._amount_value_from_editor()
        if self.kind.currentText() == "Despesa":
            signed_amount = -abs(signed_amount)
        else:
            signed_amount = abs(signed_amount)
        purchase = self.purchase_date.date().toPython().isoformat()
        payment = self.payment_date.date().toPython().isoformat()
        return {
            "date": payment,
            "description": self.desc.text().strip(),
            "amount": signed_amount,
            "category": self._selected_category(),
            "account": self.account.currentText().strip() or "Conta principal",
            "notes": self.notes.toPlainText().strip(),
            "subcategory": self.subcategory.currentText().strip(),
            "merchant": self.merchant.text().strip(),
            "payment_method": self.payment_method.currentText().strip(),
            "tags": self.tags.text().strip(),
            "purchase_date": purchase,
            "payment_date": payment,
        }

    def data(self) -> tuple[str, str, float, str, str, str]:
        d = self.extended_data()
        return (
            d["date"],
            d["description"],
            d["amount"],
            d["category"],
            d["account"],
            d["notes"],
        )

    def attachment_paths(self) -> list[Path]:
        return list(self._attachment_paths)


class ScheduledPaymentDialog(LiquidSheetDialog):
    def __init__(
        self, repo: Repo, parent: Optional[QWidget] = None, row: Optional[Any] = None
    ):
        super().__init__(parent)
        self.repo = repo
        self.setWindowTitle("Novo pagamento futuro / prestação")
        self.setMinimumWidth(560)
        form = QFormLayout()
        self.set_content_layout(form)
        form.setContentsMargins(22, 22, 22, 22)
        form.setSpacing(12)
        self.name = QLineEdit()
        self.name.setPlaceholderText("Ex.: seguro, subscrição, compra em prestações")
        self.amount = EuroSpinBox()
        self.amount.setRange(0, 1_000_000)
        self.amount.setDecimals(2)
        self.amount.setSuffix(" €")
        self.category = QComboBox()
        self.category.addItems(repo.categories() or ["Outros"])
        self.subcategory = QLineEdit()
        self.account = QLineEdit("Conta principal")
        self.payment_method = QComboBox()
        self.payment_method.setEditable(True)
        self.payment_method.addItems(
            [
                "Cartão de crédito",
                "Débito direto",
                "Transferência",
                "Cartão de débito",
                "MB Way",
                "Outro",
            ]
        )
        self.start_date = QDateEdit()
        prepare_date_edit(self.start_date, theme_for_widget(parent))
        self.start_date.setDate(QDate.currentDate())
        self.debit_day = QSpinBox()
        self.debit_day.setRange(1, 31)
        self.debit_day.setValue(date.today().day)
        self.months = QSpinBox()
        self.months.setRange(0, 240)
        self.months.setValue(1)
        self.months.setSuffix(" meses")
        self.notes = QTextEdit()
        self.notes.setMaximumHeight(85)
        form.addRow("Nome", self.name)
        form.addRow("Valor debitado", self.amount)
        form.addRow("Categoria", self.category)
        form.addRow("Subcategoria", self.subcategory)
        form.addRow("Conta", self.account)
        form.addRow("Forma de pagamento", self.payment_method)
        form.addRow("Primeiro débito", self.start_date)
        form.addRow("Dia do débito", self.debit_day)
        form.addRow("Duração", self.months)
        form.addRow("Notas", self.notes)
        hint = QLabel(
            "Usa 0 meses para pagamentos mensais sem fim definido. A app cria os movimentos apenas quando carregas em 'Lançar pagamentos em falta'."
        )
        hint.setObjectName("Subtle")
        hint.setWordWrap(True)
        form.addRow("", hint)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addWidget(buttons)
        if row is not None:
            self.setWindowTitle("Editar pagamento futuro / prestação")
            self.name.setText(str(row_get(row, "name", "") or ""))
            self.amount.setValue(abs(safe_float(row_get(row, "amount", 0))))
            category = str(row_get(row, "category", "") or "")
            if category and self.category.findText(category) < 0:
                self.category.addItem(category)
            if category:
                self.category.setCurrentText(category)
            self.subcategory.setText(str(row_get(row, "subcategory", "") or ""))
            self.account.setText(str(row_get(row, "account", "") or "Conta principal"))
            method = str(row_get(row, "payment_method", "") or "")
            if method:
                self.payment_method.setCurrentText(method)
            start = parse_flexible_date(row_get(row, "start_date")) or date.today()
            self.start_date.setDate(to_qdate(start))
            self.debit_day.setValue(
                max(1, min(31, int(row_get(row, "debit_day", start.day) or start.day)))
            )
            self.months.setValue(max(0, int(row_get(row, "months_total", 1) or 0)))
            self.notes.setPlainText(str(row_get(row, "notes", "") or ""))

    def accept(self) -> None:  # type: ignore[override]
        name = self.name.text().strip()
        if not name:
            QMessageBox.warning(
                self,
                "Campo Obrigatório",
                "O nome do pagamento futuro não pode estar em branco.",
            )
            self.name.setFocus()
            return
        if self.amount.value() <= 0.0:
            QMessageBox.warning(
                self,
                "Montante Inválido",
                "O valor debitado deve ser superior a 0,00 €.",
            )
            self.amount.setFocus()
            return
        self.name.setText(name)
        super().accept()

    def get_data(self) -> dict[str, Any]:
        return {
            "name": self.name.text().strip(),
            "amount": self.amount.value(),
            "category": self.category.currentText().strip(),
            "subcategory": self.subcategory.text().strip(),
            "account": self.account.text().strip() or "Conta principal",
            "payment_method": self.payment_method.currentText().strip(),
            "start_date": self.start_date.date().toPython().isoformat(),
            "debit_day": self.debit_day.value(),
            "months": self.months.value(),
            "notes": self.notes.toPlainText().strip(),
        }


class MonthlyGoalDialog(LiquidSheetDialog):
    def __init__(self, repo: Repo, ym: str, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.repo = repo
        self.ym = ym
        self.setWindowTitle("Nova meta mensal")
        self.setMinimumWidth(500)
        form = QFormLayout()
        self.set_content_layout(form)
        form.setContentsMargins(22, 22, 22, 22)
        form.setSpacing(12)
        self.name = QLineEdit()
        self.name.setPlaceholderText(
            "Ex.: poupar 500 €, gastar menos em restaurantes..."
        )
        self.goal_type = QComboBox()
        self.goal_type.addItems(
            ["Poupar pelo menos", "Investir pelo menos", "Gastar no máximo"]
        )
        self.category = QComboBox()
        self.category.setEditable(True)
        self.category.addItem("")
        self.category.addItems(repo.categories() or [])
        self.target = EuroSpinBox()
        self.target.setRange(0, 1_000_000)
        self.target.setDecimals(2)
        self.target.setSuffix(" €")
        self.notes = QTextEdit()
        self.notes.setMaximumHeight(80)
        form.addRow("Mês", QLabel(ym))
        form.addRow("Nome", self.name)
        form.addRow("Tipo", self.goal_type)
        form.addRow("Categoria", self.category)
        form.addRow("Meta", self.target)
        form.addRow("Notas", self.notes)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addWidget(buttons)

    def accept(self) -> None:  # type: ignore[override]
        name = self.name.text().strip()
        if not name:
            QMessageBox.warning(
                self,
                "Campo Obrigatório",
                "O nome da meta mensal não pode estar em branco.",
            )
            self.name.setFocus()
            return
        if self.target.value() <= 0.0:
            QMessageBox.warning(
                self,
                "Montante Inválido",
                "O montante da meta deve ser superior a 0,00 €.",
            )
            self.target.setFocus()
            return
        self.name.setText(name)
        super().accept()

    def get_data(self) -> dict[str, Any]:
        # BUG-DLG-01: o chamador ui/pages/planning.py lê data["ym"]; sem esta
        # chave a criação de metas mensais crashava com KeyError fatal.
        # self.ym é o atributo string guardado em __init__ (não é callable).
        return {
            "ym": self.ym,
            "name": self.name.text().strip(),
            "goal_type": self.goal_type.currentText().strip(),
            "category": self.category.currentText().strip(),
            "target": self.target.value(),
            "notes": self.notes.toPlainText().strip(),
        }


class GoalDialog(LiquidSheetDialog):
    def __init__(self, repo: Repo, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.repo = repo
        self.setWindowTitle("Novo objetivo")
        self.setMinimumWidth(420)
        form = QFormLayout()
        self.set_content_layout(form)
        form.setContentsMargins(22, 22, 22, 22)
        self.name = QLineEdit()
        self.goal_type = QComboBox()
        self.goal_type.addItem("Objetivo geral", "standard")
        self.goal_type.addItem("Reserva de emergência", "emergency_fund")
        self.goal_type.setAccessibleName("Finalidade do objetivo")
        self.goal_type.setToolTip(
            "Só um objetivo pode representar a reserva de emergência."
        )
        self.target = EuroSpinBox()
        self.target.setRange(0, 10_000_000)
        self.target.setDecimals(2)
        self.target.setSuffix(" €")
        self.saved = EuroSpinBox()
        self.saved.setRange(0, 10_000_000)
        self.saved.setDecimals(2)
        self.saved.setSuffix(" €")
        self.monthly = EuroSpinBox()
        self.monthly.setRange(0, 100_000)
        self.monthly.setDecimals(2)
        self.monthly.setSuffix(" €")
        self.deadline = QDateEdit()
        prepare_date_edit(self.deadline, theme_for_widget(parent))
        self.deadline.setDate(QDate.currentDate())
        self.notes = QTextEdit()
        self.notes.setMaximumHeight(80)
        form.addRow("Nome", self.name)
        form.addRow("Finalidade", self.goal_type)
        form.addRow("Meta", self.target)
        form.addRow("Atual", self.saved)
        form.addRow("Poupança mensal", self.monthly)
        form.addRow("Prazo", self.deadline)
        form.addRow("Notas", self.notes)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addWidget(buttons)

    def accept(self) -> None:  # type: ignore[override]
        name = self.name.text().strip()
        if not name:
            QMessageBox.warning(
                self,
                "Campo Obrigatório",
                "O nome do objetivo não pode estar em branco.",
            )
            self.name.setFocus()
            return
        if self.target.value() <= 0.0:
            QMessageBox.warning(
                self,
                "Montante Inválido",
                "O montante do objetivo deve ser superior a 0,00 €.",
            )
            self.target.setFocus()
            return
        self.name.setText(name)
        super().accept()

    def get_data(self) -> dict[str, Any]:
        return {
            "name": self.name.text().strip(),
            "target": self.target.value(),
            "saved": self.saved.value(),
            "monthly": self.monthly.value(),
            "deadline": self.deadline.date().toPython().isoformat(),
            "notes": self.notes.toPlainText().strip(),
            "goal_type": str(self.goal_type.currentData() or "standard").strip(),
        }


class BudgetEditDialog(LiquidSheetDialog):
    def __init__(self, category: str, current: float, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle(f"Orçamento · {category}")
        self.category = category
        lay = QFormLayout()
        self.set_content_layout(lay)
        self.value = EuroSpinBox()
        self.value.setRange(0, 1_000_000)
        self.value.setDecimals(2)
        self.value.setValue(float(current or 0))
        self.value.setSuffix(" €")
        lay.addRow(QLabel(f"Categoria: <b>{html_text(category)}</b>"))
        lay.addRow("Orçamento mensal", self.value)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

    def accept(self) -> None:  # type: ignore[override]
        # BUG-DLG-08: 0,00 € é válido e significa "remover o orçamento" da
        # categoria; apenas montantes negativos são rejeitados.
        if self.value.value() < 0.0:
            QMessageBox.warning(
                self,
                "Montante Inválido",
                "O montante do orçamento não pode ser negativo.",
            )
            self.value.setFocus()
            return
        super().accept()

    def get_data(self) -> dict[str, Any]:
        val = float(self.value.value())
        return {
            "category": str(self.category or "").strip(),
            "amount": val,
            "budget": val,
        }


class SubscriptionDialog(LiquidSheetDialog):
    def __init__(
        self, repo: Repo, parent: Optional[QWidget] = None, row: Optional[Any] = None
    ):
        super().__init__(parent)
        self.setWindowTitle("Editar subscrição" if row else "Nova subscrição")
        self.setMinimumWidth(500)
        lay = QFormLayout()
        self.set_content_layout(lay)
        self.name = QLineEdit()
        self.name.setPlaceholderText("Netflix, ginásio, seguro...")
        self.amount = EuroSpinBox()
        self.amount.setRange(0, 1_000_000)
        self.amount.setDecimals(2)
        self.amount.setSuffix(" €")
        self.category = QComboBox()
        self.category.addItems(repo.categories("expense") or repo.categories())
        self.renewal = QSpinBox()
        self.renewal.setRange(1, 31)
        self.renewal.setValue(min(date.today().day, 28))
        self.next_renewal = QDateEdit()
        prepare_date_edit(self.next_renewal)
        self.next_renewal.setCalendarPopup(True)
        self.next_renewal.setDisplayFormat("dd/MM/yyyy")
        self.next_renewal.setDate(to_qdate(date.today()))
        self.period = QComboBox()
        self.period.addItems(["Mensal", "Anual", "Trimestral", "Semestral"])
        self.account = QComboBox()
        self.account.setEditable(True)
        account_rows = (
            list(repo.financial_accounts())
            if callable(getattr(repo, "financial_accounts", None))
            else []
        )
        account_names = [
            str(row_get(item, "name") or "").strip()
            for item in account_rows
            if str(row_get(item, "name") or "").strip()
        ]
        if "Conta principal" not in account_names:
            account_names.insert(0, "Conta principal")
        self.account.addItems(account_names)
        self.account.setPlaceholderText("Conta associada à subscrição")
        self.payment_method = QComboBox()
        self.payment_method.setEditable(True)
        self.payment_method.addItems(
            [
                "",
                "Débito direto",
                "Cartão de crédito",
                "Cartão de débito",
                "Transferência",
                "MB Way",
                "Dinheiro",
                "Outro",
            ]
        )
        self.active = QCheckBox("Ativa")
        self.active.setChecked(True)
        self.aliases = QLineEdit()
        self.aliases.setPlaceholderText(
            "Nomes exatos no extrato, separados por ; (opcional)"
        )
        self.aliases.setToolTip(
            "Apenas estes nomes confirmados serão associados à subscrição; "
            "não é usada correspondência aproximada automática."
        )
        self.notes = QTextEdit()
        self.notes.setFixedHeight(90)

        if row is not None:
            self.name.setText(str(row["name"] or ""))
            self.amount.setValue(safe_float(row["amount"]))
            idx = self.category.findText(str(row["category"] or ""))
            if idx < 0 and row["category"]:
                self.category.addItem(str(row["category"]))
                idx = self.category.findText(str(row["category"]))
            if idx >= 0:
                self.category.setCurrentIndex(idx)
            self.renewal.setValue(max(1, min(31, int(row["renewal_day"] or 1))))
            from services.payments import PaymentsService

            next_due = PaymentsService(repo).next_subscription_due_date(row)
            self.next_renewal.setDate(to_qdate(next_due))
            pidx = self.period.findText(str(row["period"] or "Mensal"))
            if pidx >= 0:
                self.period.setCurrentIndex(pidx)
            account = str(
                row_get(row, "account", "Conta principal") or "Conta principal"
            )
            if self.account.findText(account) < 0:
                self.account.addItem(account)
            self.account.setCurrentText(account)
            method = str(row_get(row, "payment_method", "") or "")
            if method:
                if self.payment_method.findText(method) < 0:
                    self.payment_method.addItem(method)
                self.payment_method.setCurrentText(method)
            self.active.setChecked(bool(int(row["active"] or 0)))
            self.aliases.setText(str(row_get(row, "aliases", "") or ""))
            self.notes.setPlainText(str(row["notes"] or ""))

        lay.addRow("Nome", self.name)
        lay.addRow("Valor", self.amount)
        lay.addRow("Categoria", self.category)
        lay.addRow("Dia de renovação", self.renewal)
        lay.addRow("Próxima renovação", self.next_renewal)
        lay.addRow("Período", self.period)
        lay.addRow("Conta", self.account)
        lay.addRow("Forma de pagamento", self.payment_method)
        lay.addRow("Estado", self.active)
        lay.addRow("Nomes no extrato", self.aliases)
        lay.addRow("Notas", self.notes)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

        def sync_due_day(value: int) -> None:
            current = self.next_renewal.date()
            day = min(max(1, int(value)), current.daysInMonth())
            self.next_renewal.blockSignals(True)
            self.next_renewal.setDate(QDate(current.year(), current.month(), day))
            self.next_renewal.blockSignals(False)

        def sync_renewal_day(value: QDate) -> None:
            self.renewal.blockSignals(True)
            self.renewal.setValue(value.day())
            self.renewal.blockSignals(False)

        self.renewal.valueChanged.connect(sync_due_day)
        self.next_renewal.dateChanged.connect(sync_renewal_day)

    def accept(self) -> None:  # type: ignore[override]
        name = self.name.text().strip()
        if not name:
            QMessageBox.warning(
                self,
                "Campo Obrigatório",
                "O nome da subscrição não pode estar em branco.",
            )
            self.name.setFocus()
            return
        if self.amount.value() <= 0.0:
            QMessageBox.warning(
                self,
                "Montante Inválido",
                "O valor da subscrição deve ser superior a 0,00 €.",
            )
            self.amount.setFocus()
            return
        self.name.setText(name)
        super().accept()

    def get_data(self) -> dict[str, Any]:
        return {
            "name": self.name.text().strip(),
            "amount": self.amount.value(),
            "category": self.category.currentText().strip(),
            "renewal": self.renewal.value(),
            "period": self.period.currentText().strip(),
            "notes": self.notes.toPlainText().strip(),
            "active": 1 if self.active.isChecked() else 0,
            "next_renewal_date": self.next_renewal.date().toString("yyyy-MM-dd"),
            "aliases": self.aliases.text().strip(),
            "account": self.account.currentText().strip() or "Conta principal",
            "payment_method": self.payment_method.currentText().strip(),
        }



class BudgetBucketDialog(LiquidSheetDialog):
    """Compact editor for the real 50/30/20 category classification."""

    BUCKETS = ("Necessidades", "Desejos", "Poupança")

    def __init__(self, repo: Repo, parent: Optional[QWidget] = None):
        host = parent.window() if parent is not None else None
        super().__init__(
            host,
            theme_provider=lambda: theme_for_widget(host),
        )
        self.repo = repo
        self.setWindowTitle("Classificar categorias · 50/30/20")
        self.setModal(True)
        if host is not None:
            self.setStyleSheet(host.styleSheet())

        self.sheet.setMinimumSize(640, 520)
        self.sheet.setMaximumSize(720, 600)

        self.content_layout = QVBoxLayout()
        self.content_layout.setContentsMargins(0, 0, 0, 0)
        self.content_layout.setSpacing(10)
        self.set_content_layout(self.content_layout)

        rows = repo.category_rows("expense")
        heading = QHBoxLayout()
        heading.setContentsMargins(0, 0, 0, 0)
        heading.setSpacing(10)
        self.heading_label = QLabel("⚖️ Classificação 50/30/20")
        self.heading_label.setObjectName("SectionTitle")
        heading.addWidget(self.heading_label, 1)
        self.category_count_label = QLabel(f"{len(rows)} categorias")
        self.category_count_label.setObjectName("MetricStatus")
        self.category_count_label.setProperty("tone", "neutral")
        heading.addWidget(self.category_count_label, 0, Qt.AlignRight)
        self.content_layout.addLayout(heading)

        self.intro_label = QLabel(
            "Escolhe como cada categoria entra na análise. "
            "A classificação fica guardada e pode ser alterada a qualquer momento."
        )
        self.intro_label.setObjectName("Subtle")
        self.intro_label.setWordWrap(True)
        self.content_layout.addWidget(self.intro_label)

        self.bucket_help_label = QLabel(
            "<b>Necessidades</b> · essenciais &nbsp;&nbsp; "
            "<b>Desejos</b> · flexíveis &nbsp;&nbsp; "
            "<b>Poupança</b> · futuro"
        )
        self.bucket_help_label.setObjectName("Subtle")
        self.bucket_help_label.setWordWrap(True)
        self.content_layout.addWidget(self.bucket_help_label)

        self.table = QTableWidget(len(rows), 2)
        self.table.setProperty("columnPersistenceId", "budgets.bucket_allocation")
        self.table.setObjectName("BudgetBucketTable")
        self.table.setHorizontalHeaderLabels(["Categoria", "Grupo 50/30/20"])
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.table.setMinimumHeight(320)
        self.table.setMaximumHeight(390)
        header = self.table.horizontalHeader()
        header.setMinimumHeight(38)
        header.setMinimumSectionSize(160)
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self._selectors: list[tuple[str, QComboBox]] = []
        for index, row in enumerate(rows):
            name = str(row["name"] or "")
            item = QTableWidgetItem(name)
            item.setToolTip(name)
            self.table.setItem(index, 0, item)
            selector = QComboBox()
            selector.setObjectName("BudgetBucketSelector")
            selector.addItems(self.BUCKETS)
            selected = str(row["budget_bucket"] or "Desejos")
            selector.setCurrentText(selected if selected in self.BUCKETS else "Desejos")
            selector.setAccessibleName(f"Grupo 50/30/20 para {name}")
            bucket_item = QTableWidgetItem(selector.currentText())
            bucket_item.setFlags(Qt.ItemIsEnabled)
            self.table.setItem(index, 1, bucket_item)
            selector.currentTextChanged.connect(bucket_item.setText)
            self.table.setCellWidget(index, 1, selector)
            self.table.setRowHeight(index, 46)
            self._selectors.append((name, selector))
        self.content_layout.addWidget(self.table, 1)

        self.buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        save_button = self.buttons.button(QDialogButtonBox.Save)
        cancel_button = self.buttons.button(QDialogButtonBox.Cancel)
        save_button.setText("Guardar")
        save_button.setObjectName("Primary")
        cancel_button.setText("Cancelar")
        cancel_button.setObjectName("Secondary")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        self.content_layout.addWidget(self.buttons)

    def values(self) -> list[tuple[str, str]]:
        return [(name, selector.currentText()) for name, selector in self._selectors]


class HoldingDialog(LiquidSheetDialog):
    def __init__(self, parent: Optional[QWidget] = None, row: Optional[Any] = None):
        super().__init__(parent)
        self.setWindowTitle(
            "Editar investimento" if row is not None else "Novo investimento"
        )
        self.setMinimumWidth(460)
        lay = QFormLayout()
        self.set_content_layout(lay)
        self.name = QLineEdit()
        self.name.setPlaceholderText("ETF, PPR, depósito, ação...")
        self.asset_class = QComboBox()
        self.asset_class.addItems(
            [
                "ETF",
                "Ações",
                "PPR",
                "Depósito",
                "Certificados",
                "Cripto manual",
                "Outro",
            ]
        )
        self.account = QLineEdit()
        self.account.setText("Manual")
        self.value = EuroSpinBox()
        self.value.setRange(0, 100_000_000)
        self.value.setDecimals(2)
        self.value.setSuffix(" €")
        self.cost = EuroSpinBox()
        self.cost.setRange(0, 100_000_000)
        self.cost.setDecimals(2)
        self.cost.setSuffix(" €")
        self.cost_known = QCheckBox("Conheço o capital investido")
        self.cost_known.setChecked(True)
        self.fees = EuroSpinBox()
        self.fees.setRange(0, 100_000_000)
        self.fees.setDecimals(2)
        self.fees.setSuffix(" €")
        self.fees.setAccessibleName("Comissões e custos acumulados")

        self.currency = QComboBox()
        self.currency.addItems(["EUR", "USD", "GBP", "CHF"])
        self.exchange_rate = QDoubleSpinBox()
        self.exchange_rate.setRange(0.0001, 10000.0)
        self.exchange_rate.setDecimals(4)
        self.exchange_rate.setSingleStep(0.01)
        self.exchange_rate.setValue(1.0000)
        self.exchange_rate.setAccessibleName("Taxa de câmbio direta para EUR")
        self.exchange_rate.setToolTip(
            "Taxa direta: 1 Moeda Estrangeira = X EUR (ex.: 0.9200 se 1 USD = 0.92 EUR)"
        )
        self.eur_preview = QLabel("Moeda base (EUR)")
        self.eur_preview.setObjectName("Subtle")

        # BUG-DLG-06: acquisition date + jurisdiction for crypto >365d exemption (CIRS Art. 10.º/19)
        self.acquisition_date = OptionalDateEdit()
        self.acquisition_date.setCalendarPopup(True)
        self.acquisition_date.setDisplayFormat("yyyy-MM-dd")
        self.acquisition_date.setSpecialValueText("Não indicada")
        self.acquisition_date.setMinimumDate(QDate(2000, 1, 1))
        self.acquisition_date.setDate(self.acquisition_date.minimumDate())
        self.acquisition_date.setAccessibleName("Data de aquisição do ativo")
        self.acquisition_date.setToolTip(
            "Necessária para a isenção de mais-valias de criptoativos detidos há mais de 365 dias."
        )
        self.jurisdiction = QComboBox()
        self.jurisdiction.setEditable(True)
        self.jurisdiction.addItems(
            ["PT", "DE", "ES", "FR", "NL", "LU", "IE", "US", "GB", "CH"]
        )
        self.jurisdiction.setCurrentText("PT")
        self.jurisdiction.setAccessibleName("Jurisdição do emissor")
        self.jurisdiction.setToolTip(
            "País da plataforma/emissor. Fora de PT ativa a análise de taxa de convenção."
        )

        self.currency.currentTextChanged.connect(self._on_currency_changed)
        self.exchange_rate.valueChanged.connect(self._update_preview)
        self.value.valueChanged.connect(self._update_preview)

        fee_hint = QLabel(
            "Regista aqui comissões de compra, venda, custódia ou outros "
            "custos já pagos. O capital investido fica separado."
        )
        fee_hint.setObjectName("Subtle")
        fee_hint.setWordWrap(True)
        self.notes = QTextEdit()
        self.notes.setFixedHeight(70)
        self.linked_account = QComboBox()
        self.linked_account.addItem("Não associar a um saldo bancário", "")
        repo = getattr(parent, "repo", None)
        if repo is not None:
            try:
                for account_row in repo.financial_accounts():
                    account_name = str(row_get(account_row, "name") or "").strip()
                    if account_name:
                        self.linked_account.addItem(account_name, account_name)
            except Exception as exc:
                log_warning(
                    "Não foi possível carregar contas associadas no diálogo de investimento: %s",
                    exc,
                )

        link_hint = QLabel(
            "Associa apenas quando este valor já está incluído no saldo dessa "
            "conta. Assim o património não conta o mesmo dinheiro duas vezes."
        )
        link_hint.setObjectName("Subtle")
        link_hint.setWordWrap(True)

        if row is not None:
            self.name.setText(str(row["name"] or ""))
            idx = self.asset_class.findText(str(row["asset_class"] or ""))
            if idx < 0 and row["asset_class"]:
                self.asset_class.addItem(str(row["asset_class"]))
                idx = self.asset_class.findText(str(row["asset_class"]))
            if idx >= 0:
                self.asset_class.setCurrentIndex(idx)
            self.account.setText(str(row["account"] or "Manual"))
            self.value.setValue(safe_float(row["value"]))
            self.cost.setValue(safe_float(row["cost_basis"]))
            known = bool(
                int(
                    row_get(
                        row,
                        "cost_basis_known",
                        1 if safe_float(row["cost_basis"]) > 0 else 0,
                    )
                    or 0
                )
            )
            self.cost_known.setChecked(known)
            self.fees.setValue(safe_float(row_get(row, "fees")))
            self.notes.setPlainText(str(row["notes"] or ""))
            linked = str(row_get(row, "linked_account", "") or "").strip()
            if linked and self.linked_account.findData(linked) < 0:
                self.linked_account.addItem(linked, linked)
            self.linked_account.setCurrentIndex(
                max(0, self.linked_account.findData(linked))
            )
            curr = str(row_get(row, "currency", "EUR") or "EUR").strip().upper()
            rate = safe_float(row_get(row, "exchange_rate", 1.0), 1.0)
            if rate <= 0:
                rate = 1.0
            cur_idx = self.currency.findText(curr)
            if cur_idx < 0 and curr:
                self.currency.addItem(curr)
                cur_idx = self.currency.findText(curr)
            if cur_idx >= 0:
                self.currency.setCurrentIndex(cur_idx)
            self.exchange_rate.setValue(rate)
            # BUG-DLG-06: restore acquisition metadata
            acq = str(row_get(row, "acquisition_date", "") or "").strip()[:10]
            if acq:
                try:
                    parsed = QDate.fromString(acq, "yyyy-MM-dd")
                    if parsed.isValid():
                        self.acquisition_date.setDate(parsed)
                except (ValueError, TypeError):
                    pass
            juris = str(row_get(row, "jurisdiction", "PT") or "PT").strip().upper()
            if juris:
                idx_j = self.jurisdiction.findText(juris)
                if idx_j < 0:
                    self.jurisdiction.addItem(juris)
                    self.jurisdiction.setCurrentText(juris)
                else:
                    self.jurisdiction.setCurrentIndex(idx_j)

        self._on_currency_changed(self.currency.currentText())

        self.cost.setEnabled(self.cost_known.isChecked())
        self.cost_known.toggled.connect(self.cost.setEnabled)

        lay.addRow("Ativo", self.name)
        lay.addRow("Tipo", self.asset_class)
        lay.addRow("Conta/plataforma", self.account)
        lay.addRow("Moeda", self.currency)
        lay.addRow("Câmbio para EUR", self.exchange_rate)
        lay.addRow("Valor atual (nativo)", self.value)
        lay.addRow("Estimativa em EUR", self.eur_preview)
        lay.addRow("Capital investido", self.cost)
        lay.addRow("Disponibilidade do custo", self.cost_known)
        lay.addRow("Comissões acumuladas", self.fees)
        lay.addRow("Data de aquisição", self.acquisition_date)
        lay.addRow("Jurisdição", self.jurisdiction)
        lay.addRow(fee_hint)
        lay.addRow("Saldo bancário associado", self.linked_account)
        lay.addRow(link_hint)
        lay.addRow("Notas", self.notes)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Save).setText("Guardar investimento")
        buttons.button(QDialogButtonBox.Cancel).setText("Cancelar")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

    def _on_currency_changed(self, text: str) -> None:
        curr = str(text or "EUR").strip().upper()
        symbols = {"EUR": " €", "USD": " $", "GBP": " £", "CHF": " CHF"}
        sym = symbols.get(curr, f" {curr}")
        self.value.setSuffix(sym)
        self.cost.setSuffix(sym)
        self.fees.setSuffix(sym)
        if curr == "EUR":
            self.exchange_rate.setValue(1.0)
            self.exchange_rate.setEnabled(False)
            self.eur_preview.setText("Moeda base (EUR)")
        else:
            self.exchange_rate.setEnabled(True)
            self._update_preview()

    def _update_preview(self) -> None:
        curr = self.currency.currentText().strip().upper()
        if curr == "EUR":
            self.eur_preview.setText("Moeda base (EUR)")
        else:
            val_eur = self.value.value() * self.exchange_rate.value()
            formatted = f"{val_eur:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
            self.eur_preview.setText(f"≈ {formatted} €")

    def accept(self) -> None:  # type: ignore[override]
        name = self.name.text().strip()
        if not name:
            QMessageBox.warning(
                self,
                "Campo Obrigatório",
                "O nome do investimento não pode estar em branco.",
            )
            self.name.setFocus()
            return
        if self.cost_known.isChecked() and self.cost.value() <= 0:
            QMessageBox.warning(
                self,
                "Campo Obrigatório",
                "Se conheces o capital investido, indica um valor superior a zero. "
                "Caso contrário, desmarca essa opção.",
            )
            self.cost.setFocus()
            return
        if self.value.value() <= 0.0:
            reply = QMessageBox.question(
                self,
                "Valor de Mercado Nulo",
                "O valor de mercado atual indicado é 0,00 €.\n\n"
                "Isto fará com que o ativo seja considerado totalmente desvalorizado (-100%) "
                "e não surja na distribuição de ativos da carteira.\n\n"
                "Se pretendes fechar ou vender esta posição, podes utilizar o fluxo 'Fechar posição / Vender'.\n\n"
                "Tens a certeza de que pretendes registar este ativo com valor zero?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                self.value.setFocus()
                return
        self.name.setText(name)
        super().accept()

    def get_data(self) -> dict[str, Any]:
        acq_date = self.acquisition_date.date()
        acq_str = (
            acq_date.toString("yyyy-MM-dd")
            if self.acquisition_date.date() > self.acquisition_date.minimumDate()
            else ""
        )
        return {
            "name": self.name.text().strip(),
            "asset_class": self.asset_class.currentText().strip(),
            "account": self.account.text().strip(),
            "value": self.value.value(),
            "cost": self.cost.value(),
            "notes": self.notes.toPlainText().strip(),
            "fees": self.fees.value(),
            "cost_basis_known": self.cost_known.isChecked(),
            "linked_account": str(self.linked_account.currentData() or "").strip(),
            "currency": self.currency.currentText().strip().upper() or "EUR",
            "exchange_rate": self.exchange_rate.value(),
            "acquisition_date": acq_str,
            "jurisdiction": self.jurisdiction.currentText().strip().upper() or "PT",
        }


class ClosePositionDialog(LiquidSheetDialog):
    """BUG-INV-01: Diálogo dedicado para fechar ou alienar posição de investimento com apuramento de mais-valias."""

    def __init__(self, parent: Optional[QWidget] = None, holding: Optional[Any] = None):
        super().__init__(parent)
        self.setWindowTitle("Fechar posição / Vender ativo")
        self.setMinimumWidth(500)
        self.holding = holding or {}
        self._privacy = bool(getattr(getattr(parent, "theme", None), "privacy", False))

        name = str(row_get(self.holding, "name") or "Investimento")
        curr_val = safe_float(row_get(self.holding, "value", 0.0))
        cost_basis = safe_float(row_get(self.holding, "cost_basis", 0.0))
        account_name = str(row_get(self.holding, "account") or "Manual")
        linked_account = str(row_get(self.holding, "linked_account") or "").strip()

        lay = QFormLayout()
        self.set_content_layout(lay)

        cost_known = bool(row_get(self.holding, "cost_basis_known", cost_basis > 0))
        currency = str(row_get(self.holding, "currency", "EUR") or "EUR").upper()
        formatted_cost = (
            f"{cost_basis:.2f} {currency}" if cost_known else "Desconhecido"
        )
        info_lbl = QLabel(
            f"<b>Ativo:</b> {html_text('Dados ocultos' if self._privacy else name)} · "
            f"<b>Custo de aquisição registado:</b> {html_text('•••••' if self._privacy else formatted_cost)}"
        )
        info_lbl.setWordWrap(True)
        lay.addRow(info_lbl)

        self.sale_value = EuroSpinBox()
        self.sale_value.setRange(0, 100_000_000)
        self.sale_value.setDecimals(2)
        self.sale_value.setSuffix(" €")
        current_rate = safe_float(row_get(self.holding, "exchange_rate", 1.0), 1.0)
        self.sale_value.setValue((curr_val if curr_val > 0 else cost_basis) * current_rate)

        self.sale_date = QDateEdit()
        self.sale_date.setCalendarPopup(True)
        self.sale_date.setDisplayFormat("yyyy-MM-dd")
        self.sale_date.setDate(QDate.currentDate())
        prepare_date_edit(self.sale_date, theme_for_widget(parent))

        self.sale_fees = EuroSpinBox()
        self.sale_fees.setRange(0, 100_000_000)
        self.sale_fees.setDecimals(2)
        self.sale_fees.setSuffix(" €")
        self.sale_fees.setValue(0.0)

        self.dest_account = QComboBox()
        repo = getattr(parent, "repo", None)
        if repo is not None:
            try:
                for acc in repo.financial_accounts():
                    aname = str(row_get(acc, "name") or "").strip()
                    if aname:
                        self.dest_account.addItem(aname, aname)
            except Exception:
                pass
        if self.dest_account.count() == 0:
            self.dest_account.addItem(linked_account or account_name or "Conta principal")
        elif linked_account:
            idx = self.dest_account.findText(linked_account)
            if idx >= 0:
                self.dest_account.setCurrentIndex(idx)

        self.create_transaction = QCheckBox("Registar receita da venda na conta bancária selecionada")
        self.create_transaction.setChecked(True)

        self.archive_holding = QCheckBox("Arquivar posição integral vendida; preservar o registo de aquisição")
        self.archive_holding.setChecked(True)
        self.archive_holding.setEnabled(False)
        self.archive_holding.setToolTip(
            "Uma posição vendida deixa de contar como investimento aberto. "
            "Os dados originais são preservados no registo da venda e no histórico para desfazer."
        )

        self.gain_summary = QLabel()
        self.gain_summary.setWordWrap(True)
        self.gain_summary.setTextFormat(Qt.RichText)
        self.gain_summary.setAccessibleName("Apuramento estimado da venda")

        self.acquisition_fx_known = QCheckBox("Câmbio de aquisição confirmado por comprovativo")
        self.acquisition_fx = QDoubleSpinBox()
        self.acquisition_fx.setRange(0.00000001, 100000000)
        self.acquisition_fx.setDecimals(8)
        self.acquisition_fx.setValue(1.0)
        self.acquisition_fx.setSuffix(" EUR por unidade")
        self.acquisition_fx_known.setChecked(currency == "EUR")
        self.acquisition_fx_known.setVisible(currency != "EUR")
        self.acquisition_fx.setVisible(currency != "EUR")
        self.acquisition_fx.setEnabled(False)
        self.acquisition_fx_known.toggled.connect(self.acquisition_fx.setEnabled)
        self.acquisition_fx_known.toggled.connect(self._update_gain_preview)
        self.acquisition_fx.valueChanged.connect(self._update_gain_preview)

        self.sale_value.valueChanged.connect(self._update_gain_preview)
        self.sale_fees.valueChanged.connect(self._update_gain_preview)
        self.create_transaction.toggled.connect(self._update_gain_preview)

        lay.addRow("Valor de realização (venda)", self.sale_value)
        lay.addRow("Data da venda", self.sale_date)
        lay.addRow("Encargos de alienação", self.sale_fees)
        if currency != "EUR":
            lay.addRow(self.acquisition_fx_known)
            lay.addRow("Câmbio histórico de aquisição", self.acquisition_fx)
        lay.addRow("Conta para crédito do saldo", self.dest_account)
        lay.addRow(self.create_transaction)
        lay.addRow(self.archive_holding)
        lay.addRow("Mais/Menos-valia líquida", self.gain_summary)

        self._update_gain_preview()

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Confirmar venda")
        buttons.button(QDialogButtonBox.Cancel).setText("Cancelar")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

    def _update_gain_preview(self) -> None:
        cost_basis = safe_float(row_get(self.holding, "cost_basis", 0.0))
        past_fees = safe_float(row_get(self.holding, "fees", 0.0))
        sale_val = self.sale_value.value()
        sale_fee = self.sale_fees.value()
        rate = self.acquisition_fx.value() if self.acquisition_fx_known.isChecked() else None
        known = bool(row_get(self.holding, "cost_basis_known", cost_basis > 0))
        if not known or rate is None:
            missing = "Custo de aquisição desconhecido" if not known else "Câmbio histórico de aquisição desconhecido"
            self.gain_summary.setText(missing + "; a venda ficará com apuramento incompleto.")
            return
        net_gain = sale_val - (cost_basis + past_fees) * rate - sale_fee
        sign = "+" if net_gain >= 0 else ""
        theme = theme_for_widget(self)
        color = (
            ("#4ADE80" if theme.dark else "#15803D")
            if net_gain >= 0
            else ("#F87171" if theme.dark else "#B91C1C")
        )
        formatted_gain = f"{net_gain:,.2f} €".replace(",", "X").replace(".", ",").replace("X", ".")
        text = (
            f"<span style='color: {color}; font-weight: bold;'>"
            f"{sign}{formatted_gain}</span> "
            f"({'Mais-valia' if net_gain >= 0 else 'Menos-valia'} líquida deduzindo encargos)"
        )
        net_received = round(sale_val - sale_fee, 2)
        if self.create_transaction.isChecked() and sale_val > 0 and abs(net_received) < 0.01:
            text += (
                f"<br><span style='color: {theme.muted}; font-style: italic;'>"
                "(Receita líquida nula: nenhum movimento monetário será registado na conta bancária)</span>"
            )
        self.gain_summary.setText("••••• €" if self._privacy else text)

    def accept(self) -> None:  # type: ignore[override]
        if self.sale_value.value() < 0.0:
            QMessageBox.warning(self, "Valor Inválido", "O valor de venda não pode ser negativo.")
            self.sale_value.setFocus()
            return
        # BUG-DLG-07: block submission that would produce a negative bank movement
        net_received = round(self.sale_value.value() - self.sale_fees.value(), 2)
        if self.create_transaction.isChecked() and net_received < -0.005:
            QMessageBox.warning(
                self,
                "Encargos superiores à receita",
                "Os encargos de alienação excedem o valor de realização. "
                "Com esta opção ativa seria registado um movimento bancário negativo.\n\n"
                "Desativa «Registar receita da venda na conta bancária» ou corrige os valores.",
            )
            self.sale_fees.setFocus()
            return
        super().accept()

    def get_data(self) -> dict[str, Any]:
        cost_basis = safe_float(row_get(self.holding, "cost_basis", 0.0))
        past_fees = safe_float(row_get(self.holding, "fees", 0.0))
        sale_val = self.sale_value.value()
        sale_fee = self.sale_fees.value()
        total_fees = past_fees + sale_fee
        rate = self.acquisition_fx.value() if self.acquisition_fx_known.isChecked() else None
        known = bool(row_get(self.holding, "cost_basis_known", cost_basis > 0))
        net_gain = sale_val - (cost_basis + past_fees) * rate - sale_fee if known and rate is not None else None
        return {
            "sale_value": sale_val,
            "sale_date": self.sale_date.date().toString("yyyy-MM-dd"),
            "sale_fees": sale_fee,
            "total_fees": total_fees,
            "net_gain": net_gain,
            "acquisition_exchange_rate": rate,
            "account": self.dest_account.currentText().strip() or "Conta principal",
            "create_transaction": self.create_transaction.isChecked(),
            "archive_holding": self.archive_holding.isChecked(),
        }


class AccountBalanceDialog(LiquidSheetDialog):
    """Configure one trustworthy account balance without rewriting movements."""

    def __init__(self, repo, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.repo = repo
        self.setWindowTitle("Saldos de referência das contas")
        self.setMinimumWidth(520)
        layout = QFormLayout()
        self.set_content_layout(layout)
        self.account = QComboBox()
        self._accounts = {
            str(row_get(row, "name") or "Conta principal"): row
            for row in repo.financial_accounts()
        }
        self.account.addItems(list(self._accounts) or ["Conta principal"])
        self.known = QCheckBox("Conheço o saldo real desta conta numa data")
        self.balance = EuroSpinBox()
        self.balance.setRange(-100_000_000, 100_000_000)
        self.balance.setDecimals(2)
        self.balance.setSuffix(" €")
        self.reference_date = QDateEdit()
        prepare_date_edit(self.reference_date)
        self.reference_date.setDate(QDate.currentDate())
        hint = QLabel(
            "A aplicação parte deste saldo confirmado e soma apenas os movimentos "
            "posteriores à data. Sem esta referência, usa todo o histórico disponível "
            "e marca o património como parcial."
        )
        hint.setObjectName("Subtle")
        hint.setWordWrap(True)
        layout.addRow("Conta", self.account)
        layout.addRow("Qualidade", self.known)
        layout.addRow("Saldo confirmado", self.balance)
        layout.addRow("Saldo confirmado em", self.reference_date)
        layout.addRow(hint)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Save).setText("Guardar saldo de referência")
        buttons.button(QDialogButtonBox.Cancel).setText("Cancelar")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.account.currentTextChanged.connect(self._load_account)
        self.known.toggled.connect(self._sync_enabled)
        self._load_account(self.account.currentText())

    def _sync_enabled(self, checked: bool) -> None:
        self.balance.setEnabled(bool(checked))
        self.reference_date.setEnabled(bool(checked))

    def _load_account(self, name: str) -> None:
        row = self._accounts.get(str(name), {})
        known = bool(int(row_get(row, "balance_known", 0) or 0))
        self.known.setChecked(known)
        self.balance.setValue(safe_float(row_get(row, "reference_balance")))
        parsed = QDate.fromString(
            str(row_get(row, "reference_date", "") or ""),
            "yyyy-MM-dd",
        )
        self.reference_date.setDate(parsed if parsed.isValid() else QDate.currentDate())
        self._sync_enabled(known)

    def values(self) -> dict[str, Any]:
        return {
            "name": self.account.currentText().strip() or "Conta principal",
            "known": self.known.isChecked(),
            "balance": self.balance.value(),
            "date": self.reference_date.date().toPython().isoformat(),
        }


class LiabilityDialog(LiquidSheetDialog):
    def __init__(self, parent: Optional[QWidget] = None, row: Optional[Any] = None):
        super().__init__(parent)
        self.setWindowTitle("Editar dívida" if row is not None else "Nova dívida")
        self.setMinimumWidth(470)
        lay = QFormLayout()
        self.set_content_layout(lay)
        self.name = QLineEdit()
        self.name.setPlaceholderText("Crédito habitação, automóvel, cartão…")
        self.liability_type = QComboBox()
        self.liability_type.addItems(
            [
                "Crédito habitação",
                "Crédito automóvel",
                "Crédito pessoal",
                "Cartão de crédito",
                "Empréstimo familiar",
                "Outro",
            ]
        )
        self.account = QLineEdit()
        self.account.setPlaceholderText("Banco ou entidade")
        self.balance = EuroSpinBox()
        self.balance.setRange(0, 100_000_000)
        self.balance.setDecimals(2)
        self.balance.setSuffix(" €")
        self.interest_rate = QDoubleSpinBox()
        self.interest_rate.setRange(0, 100)
        self.interest_rate.setDecimals(2)
        self.interest_rate.setSuffix(" % / ano")
        self.monthly_payment = EuroSpinBox()
        self.monthly_payment.setRange(0, 10_000_000)
        self.monthly_payment.setDecimals(2)
        self.monthly_payment.setSuffix(" € / mês")
        self.notes = QTextEdit()
        self.notes.setFixedHeight(70)

        if row is not None:
            self.name.setText(str(row["name"] or ""))
            index = self.liability_type.findText(str(row["liability_type"] or ""))
            if index < 0 and row["liability_type"]:
                self.liability_type.addItem(str(row["liability_type"]))
                index = self.liability_type.findText(str(row["liability_type"]))
            self.liability_type.setCurrentIndex(max(0, index))
            self.account.setText(str(row["account"] or ""))
            self.balance.setValue(safe_float(row["balance"]))
            self.interest_rate.setValue(safe_float(row["interest_rate"]))
            self.monthly_payment.setValue(safe_float(row["monthly_payment"]))
            self.notes.setPlainText(str(row["notes"] or ""))

        lay.addRow("Dívida", self.name)
        lay.addRow("Tipo", self.liability_type)
        lay.addRow("Banco/entidade", self.account)
        lay.addRow("Capital em dívida", self.balance)
        lay.addRow("Taxa de juro", self.interest_rate)
        lay.addRow("Prestação mensal", self.monthly_payment)
        lay.addRow("Notas", self.notes)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

    def accept(self) -> None:  # type: ignore[override]
        name = self.name.text().strip()
        if not name:
            QMessageBox.warning(
                self,
                "Campo Obrigatório",
                "O nome da dívida não pode estar em branco.",
            )
            self.name.setFocus()
            return
        if self.balance.value() <= 0.0:
            QMessageBox.warning(
                self,
                "Montante Inválido",
                "O capital em dívida deve ser superior a 0,00 €.",
            )
            self.balance.setFocus()
            return
        self.name.setText(name)
        super().accept()

    def get_data(self) -> dict[str, Any]:
        return {
            "name": self.name.text().strip(),
            "liability_type": self.liability_type.currentText().strip(),
            "account": self.account.text().strip(),
            "balance": self.balance.value(),
            "interest_rate": self.interest_rate.value(),
            "monthly_payment": self.monthly_payment.value(),
            "notes": self.notes.toPlainText().strip(),
        }


class PlannedPurchaseDialog(LiquidSheetDialog):
    def __init__(self, parent: Optional[QWidget] = None, row: Optional[Any] = None):
        super().__init__(parent)
        self.setWindowTitle(
            "Editar compra planeada" if row is not None else "Nova compra planeada"
        )
        lay = QFormLayout()
        self.set_content_layout(lay)
        self.name = QLineEdit()
        self.name.setPlaceholderText("Portátil, carro, férias...")
        self.target = EuroSpinBox()
        self.target.setRange(0, 100_000_000)
        self.target.setDecimals(2)
        self.target.setSuffix(" €")
        self.saved = EuroSpinBox()
        self.saved.setRange(0, 100_000_000)
        self.saved.setDecimals(2)
        self.saved.setSuffix(" €")
        self.date = QDateEdit()
        prepare_date_edit(self.date, theme_for_widget(parent))
        self.date.setDate(QDate.currentDate().addMonths(6))
        self.priority = QComboBox()
        self.priority.addItems(["Alta", "Média", "Baixa"])
        self.notes = QTextEdit()
        self.notes.setFixedHeight(70)
        if row is not None:
            self.name.setText(str(row_get(row, "name", "") or ""))
            self.target.setValue(safe_float(row_get(row, "target_amount", 0)))
            self.saved.setValue(safe_float(row_get(row, "saved_amount", 0)))
            desired = parse_flexible_date(row_get(row, "desired_date")) or date.today()
            self.date.setDate(to_qdate(desired))
            priority = str(row_get(row, "priority", "") or "")
            idx = self.priority.findText(priority)
            if idx < 0 and priority:
                self.priority.addItem(priority)
                idx = self.priority.findText(priority)
            if idx >= 0:
                self.priority.setCurrentIndex(idx)
            self.notes.setPlainText(str(row_get(row, "notes", "") or ""))
        lay.addRow("Compra", self.name)
        lay.addRow("Valor alvo", self.target)
        lay.addRow("Já guardado", self.saved)
        lay.addRow("Data desejada", self.date)
        lay.addRow("Prioridade", self.priority)
        lay.addRow("Notas", self.notes)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

    def accept(self) -> None:  # type: ignore[override]
        name = self.name.text().strip()
        if not name:
            QMessageBox.warning(
                self,
                "Campo Obrigatório",
                "O nome da compra planeada não pode estar em branco.",
            )
            self.name.setFocus()
            return
        if self.target.value() <= 0.0:
            QMessageBox.warning(
                self,
                "Montante Inválido",
                "O valor alvo da compra planeada deve ser superior a 0,00 €.",
            )
            self.target.setFocus()
            return
        self.name.setText(name)
        super().accept()

    def get_data(self) -> dict[str, Any]:
        return {
            "name": self.name.text().strip(),
            "target": self.target.value(),
            "saved": self.saved.value(),
            "date": self.date.date().toPython().isoformat(),
            "priority": self.priority.currentText().strip(),
            "notes": self.notes.toPlainText().strip(),
        }


class DashboardConfigDialog(QDialog):
    def __init__(self, current_config: dict, theme, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Personalizar dashboard")
        self.setModal(True)
        self.setWindowFlag(Qt.WindowContextHelpButtonHint, False)

        screen = parent.screen() if parent is not None else QApplication.primaryScreen()
        available = screen.availableGeometry() if screen is not None else None
        max_width = max(320, available.width() - 48) if available is not None else 540
        max_height = max(340, available.height() - 72) if available is not None else 620
        target_width = min(440, max_width)
        target_height = min(480, max_height)
        self.setMinimumSize(min(380, target_width), min(400, target_height))
        self.setMaximumSize(max_width, max_height)
        self.resize(target_width, target_height)

        bg_color = theme.bg1
        self._bg_hex = bg_color.name()
        fg = theme.text

        if theme.dark:
            card_hex = "#1e2738"
            border_hex = "#334155"
        else:
            card_hex = "#ffffff"
            border_hex = "#c7d4e8"

        self.setStyleSheet(f"""
            QDialog {{ background-color: {self._bg_hex}; }}
            QPushButton {{
                background: {card_hex};
                color: {fg};
                border: 1px solid {border_hex};
                border-radius: 8px;
                padding: 6px 14px;
                font-size: 13px;
                font-weight: 600;
                min-width: 80px;
            }}
            QPushButton:hover {{
                background: #2563EB;
                color: #FFFFFF;
                border-color: #2563EB;
            }}
            QDialogButtonBox {{ background: transparent; }}
        """)

        self.config = dict(current_config)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(10)

        lbl = QLabel(
            "Arrasta os módulos para reordenar. Usa o botão “Cards” para "
            "mostrar, ocultar ou desativar módulos."
        )
        lbl.setWordWrap(True)
        lbl.setStyleSheet(
            f"font-weight: 700; font-size: 14px; color: {fg}; background: transparent; margin-bottom: 6px;"
        )
        layout.addWidget(lbl)

        from PySide6.QtWidgets import QAbstractItemView, QListWidget, QListWidgetItem

        self.list_widget = QListWidget()
        self.list_widget.setDragDropMode(QAbstractItemView.InternalMove)
        self.list_widget.setDefaultDropAction(Qt.MoveAction)
        self.list_widget.setSelectionMode(QAbstractItemView.SingleSelection)
        self.list_widget.setStyleSheet(
            f"background: {card_hex}; border: 1px solid {border_hex}; border-radius: 8px; outline: none; color: {fg}; padding: 4px; font-size: 14px;"
        )

        default_items = [
            ("show_chart", "📈 Saldo histórico e previsão", True),
            ("show_waterfall", "🌊 Fluxo do dinheiro no período", True),
            ("show_budget_forecast", "🎯 Orçamento vs. realizado", True),
            ("show_cashflow_calendar", "🗓️ Tesouraria futura · 30/60/90 dias", True),
            ("show_donut", "🍩 Despesas por categoria", True),
            (
                "show_expense_work_time",
                "⏳ Despesas em tempo de trabalho",
                True,
            ),
            ("show_goals", "🎯 Objetivos em destaque", True),
            ("show_insights", "🧠 Prioridades financeiras", True),
            ("show_radar", "🧭 Perfil relativo das categorias", False),
            ("show_treemap", "🧩 Peso das categorias", False),
            ("show_bubble", "🫧 Categorias por volume", False),
            ("show_sankey", "🌊 Distribuição das despesas", False),
            ("show_heatmap", "🗓️ Ritmo diário de gastos · 12 meses", False),
        ]

        saved_order = list(
            self.config.get("order", [key for key, _text, _default in default_items])
        )
        if "show_waterfall" not in saved_order:
            chart_position = (
                saved_order.index("show_chart") + 1
                if "show_chart" in saved_order
                else 0
            )
            saved_order.insert(chart_position, "show_waterfall")
        if "show_budget_forecast" not in saved_order:
            budget_position = (
                saved_order.index("show_waterfall") + 1
                if "show_waterfall" in saved_order
                else len(saved_order)
            )
            saved_order.insert(budget_position, "show_budget_forecast")
        if "show_cashflow_calendar" not in saved_order:
            cashflow_position = (
                saved_order.index("show_budget_forecast") + 1
                if "show_budget_forecast" in saved_order
                else len(saved_order)
            )
            saved_order.insert(cashflow_position, "show_cashflow_calendar")
        if "show_expense_work_time" not in saved_order:
            work_time_position = (
                saved_order.index("show_donut") + 1
                if "show_donut" in saved_order
                else len(saved_order)
            )
            saved_order.insert(work_time_position, "show_expense_work_time")

        items_dict = {
            key: (text, default_visible) for key, text, default_visible in default_items
        }
        ordered_keys = [k for k in saved_order if k in items_dict]
        for k in items_dict:
            if k not in ordered_keys:
                ordered_keys.append(k)

        for key in ordered_keys:
            text, _default_visible = items_dict[key]
            item = QListWidgetItem(f"   {text}")
            item.setData(Qt.UserRole, key)

            item.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled | Qt.ItemIsDragEnabled)
            self.list_widget.addItem(item)

        self.list_widget.setMinimumHeight(240)
        layout.addWidget(self.list_widget, 1)

        layout.addSpacing(4)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self.parentWidget() is not None:
            frame = self.frameGeometry()
            frame.moveCenter(self.parentWidget().window().frameGeometry().center())
            self.move(frame.topLeft())

    def get_config(self) -> dict:
        order = []
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            key = item.data(Qt.UserRole)
            order.append(key)
        self.config["order"] = order
        return self.config


class BudgetAssistantDialog(LiquidSheetDialog):
    def __init__(self, repo, month_key: str, parent=None):
        super().__init__(parent)
        self.repo = repo
        self.month_key = month_key
        self.setWindowTitle(f"Assistente de Orçamento - {month_key}")
        self.setMinimumWidth(500)

        main_layout = QVBoxLayout()

        self.set_content_layout(main_layout)

        info = QLabel(
            "Calcula limites sugeridos para todas as categorias automaticamente."
        )
        info.setObjectName("Subtle")
        info.setWordWrap(True)
        main_layout.addWidget(info)

        form = QFormLayout()
        self.income_base = EuroSpinBox()
        self.income_base.setRange(0, 100_000_000)
        self.income_base.setDecimals(2)
        self.income_base.setSuffix(" €")

        year, month = (int(part) for part in month_key[:7].split("-"))
        period_start, period_end, _ = repo.month_period(year, month)
        totals = repo.totals_for_payment_period(period_start, period_end)
        self.income_base.setValue(float(totals.get("income", 0) or 0))

        form.addRow("Rendimento Líquido", self.income_base)
        main_layout.addLayout(form)

        btn_layout = QHBoxLayout()
        btn_503020 = QPushButton("Regra 50/30/20")
        btn_503020.clicked.connect(self.apply_503020)
        btn_layout.addWidget(btn_503020)
        main_layout.addLayout(btn_layout)

        self.table = QTableWidget(0, 2)
        self.table.setProperty("columnPersistenceId", "budgets.assistant_categories")
        self.table.setHorizontalHeaderLabels(["Categoria", "Orçamento Sugerido"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.setItemDelegateForColumn(1, PortugueseMoneyDelegate(self.table))
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        main_layout.addWidget(self.table)

        btns = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        main_layout.addWidget(btns)

    def apply_503020(self) -> None:
        base = self.income_base.value()
        if base <= 0.0:
            QMessageBox.warning(
                self,
                "Rendimento Inválido",
                "Indica um rendimento líquido superior a 0,00 € para calcular as sugestões orçamentais.",
            )
            self.income_base.setFocus()
            return

        # Check existing classified category buckets if configured
        try:
            cat_rows = self.repo.category_rows("expense") or []
        except Exception:
            cat_rows = []

        bucket_map: dict[str, str] = {}
        for r in cat_rows:
            if isinstance(r, dict):
                cname = str(r.get("name") or "").strip().lower()
                cbucket = str(r.get("budget_bucket") or "").strip()
                if cname and cbucket:
                    bucket_map[cname] = cbucket

        cats = self.repo.categories("expense") or []
        needs_cats: list[str] = []
        wants_cats: list[str] = []

        for c in cats:
            c_str = str(c or "").strip()
            if not c_str:
                continue
            cl = c_str.lower()
            bucket = bucket_map.get(cl, "")
            if bucket == "Necessidades":
                needs_cats.append(c_str)
            elif bucket == "Desejos":
                wants_cats.append(c_str)
            elif bucket == "Poupança":
                continue
            else:
                # Fallback to keyword heuristics
                if any(
                    k in cl
                    for k in [
                        "casa",
                        "transporte",
                        "alimenta",
                        "saúde",
                        "supermercado",
                        "luz",
                        "água",
                        "farmácia",
                        "educação",
                        "combustível",
                        "seguro",
                    ]
                ):
                    needs_cats.append(c_str)
                elif any(
                    k in cl
                    for k in [
                        "restaurante",
                        "lazer",
                        "diversos",
                        "pessoal",
                        "roupa",
                        "viagens",
                        "hobbies",
                        "cultura",
                    ]
                ):
                    wants_cats.append(c_str)
                else:
                    wants_cats.append(c_str)

        needs_share = round((base * 0.50) / len(needs_cats), 2) if needs_cats else 0.0
        wants_share = round((base * 0.30) / len(wants_cats), 2) if wants_cats else 0.0

        suggestions: list[tuple[str, float]] = []
        for c in needs_cats:
            suggestions.append((c, needs_share))
        for c in wants_cats:
            suggestions.append((c, wants_share))

        self.table.setRowCount(len(suggestions))
        for r, (c, v) in enumerate(suggestions):
            self.table.setItem(r, 0, QTableWidgetItem(c))
            self.table.setItem(
                r,
                1,
                QTableWidgetItem(format_eur_number(v, grouping=False)),
            )

    def get_plan(self) -> list[tuple[str, float]]:
        res: list[tuple[str, float]] = []
        for r in range(self.table.rowCount()):
            item0 = self.table.item(r, 0)
            item1 = self.table.item(r, 1)
            if item0 is None or item1 is None:
                continue
            c = item0.text().strip()
            if not c:
                continue
            raw_text = item1.text().strip()
            if not raw_text:
                continue
            try:
                # BUG-DLG-09: aceitar decimais com vírgula e com ponto
                # ("250,50" e "250.50"); outros formatos continuam a ser
                # descartados com a categoria preservada para tentativa
                # de parsing no formato alternativo.
                try:
                    v = parse_eur_input(raw_text, field_name="orçamento")
                except ValueError:
                    v = parse_dot_decimal(raw_text)
                if v >= 0.0:
                    res.append((c, round(v, 2)))
            except (AttributeError, TypeError, ValueError):
                continue
        return res


class TransferDialog(LiquidSheetDialog):
    def __init__(self, repo, parent=None, transfer: Optional[dict] = None):
        super().__init__(parent)
        self.repo = repo
        self.setWindowTitle(
            "Editar transferência" if transfer is not None else "Nova transferência"
        )
        self.setMinimumSize(400, 300)

        main = QVBoxLayout()
        self.set_content_layout(main)

        form = QFormLayout()

        self.amount = EuroSpinBox()
        self.amount.setRange(0.0, 999999)
        self.amount.setSuffix(" €")

        self.date_edit = QDateEdit()
        self.date_edit.setCalendarPopup(True)
        prepare_date_edit(self.date_edit, theme_for_widget(parent))
        from datetime import date

        self.date_edit.setDate(date.today())

        self.source = QComboBox()
        self.dest = QComboBox()
        self.source.setEditable(True)
        self.dest.setEditable(True)

        # BUG-DLG-02: contas registadas em financial_accounts sem movimentos
        # nunca apareciam (só havia SELECT DISTINCT sobre transactions).
        # Carregamos as duas fontes: repositório (se existir o método) + SQL.
        accounts: list[str] = []
        if callable(getattr(self.repo, "financial_accounts", None)):
            for account_row in self.repo.financial_accounts():
                name = str(row_get(account_row, "name") or "").strip()
                if name and name not in accounts:
                    accounts.append(name)
        if "Conta principal" not in accounts:
            accounts.append("Conta principal")
        for x in self.repo.q(
            "SELECT DISTINCT account FROM transactions UNION SELECT 'Conta principal'"
        ):
            name = str(x[0] or "").strip()
            if name and name not in accounts:
                accounts.append(name)

        self.source.addItems(accounts)
        self.dest.addItems(accounts)
        if len(accounts) > 1:
            self.dest.setCurrentIndex(1)

        if transfer is not None:
            for combo, value in (
                (self.source, str(transfer.get("source") or "")),
                (self.dest, str(transfer.get("destination") or "")),
            ):
                if value and combo.findText(value) < 0:
                    combo.addItem(value)
                combo.setCurrentText(value)
            self.amount.setValue(safe_float(transfer.get("amount")))
            parsed = QDate.fromString(str(transfer.get("date") or ""), "yyyy-MM-dd")
            if parsed.isValid():
                self.date_edit.setDate(parsed)

        form.addRow("Valor a transferir:", self.amount)
        form.addRow("Data:", self.date_edit)
        form.addRow("Da Conta (Saída):", self.source)
        form.addRow("Para a Conta (Entrada):", self.dest)

        main.addLayout(form)
        main.addStretch()
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Save).setText("Guardar transferência")
        buttons.button(QDialogButtonBox.Cancel).setText("Cancelar")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        main.addWidget(buttons)

    def accept(self) -> None:  # type: ignore[override]
        source = self.source.currentText().strip()
        dest = self.dest.currentText().strip()
        if not source or not dest:
            QMessageBox.warning(
                self,
                "Contas em Falta",
                "Seleciona a conta de origem e a conta de destino.",
            )
            return
        if source == dest:
            QMessageBox.warning(
                self,
                "Contas Inválidas",
                "A conta de destino deve ser diferente da conta de origem.",
            )
            self.dest.setFocus()
            return
        if self.amount.value() <= 0.0:
            QMessageBox.warning(
                self,
                "Montante Inválido",
                "O valor da transferência deve ser superior a 0,00 €.",
            )
            self.amount.setFocus()
            return
        super().accept()

    def extended_data(self) -> dict:
        return {
            "amount": self.amount.value(),
            "date": self.date_edit.date().toPython().isoformat(),
            "source": self.source.currentText().strip(),
            "dest": self.dest.currentText().strip(),
        }

    def get_data(self) -> dict:
        return self.extended_data()


class PortugueseTaxDialog(QDialog):
    """Keep hypothetical open-position estimates separate from realized evidence."""

    def __init__(self, repo: Repo, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.repo = repo
        self._summary_text = ""
        self.setWindowTitle("Fiscalidade · Cenários e vendas registadas")
        self.setObjectName("PortugueseTaxDialog")
        self.setModal(True)
        self.setWindowFlag(Qt.WindowContextHelpButtonHint, False)
        self.setMinimumSize(760, 420)
        self.setSizeGripEnabled(True)

        if hasattr(parent, "theme") and getattr(parent, "theme", None) is not None:
            theme = parent.theme
        else:
            theme = theme_for_widget(parent)
        self.theme = theme
        dialog_surface = "#111827" if theme.dark else "#F8FAFF"
        header_surface = "#172033" if theme.dark else "#EAF3FF"
        self.setStyleSheet(
            (parent.styleSheet() if parent else "")
            + f"""
            QDialog#PortugueseTaxDialog {{
                background-color: {dialog_surface};
            }}
            QFrame#PortugueseTaxDialogHeader {{
                background-color: {header_surface};
                border: 1px solid {theme.border};
                border-radius: 16px;
            }}
            """
        )

        main = QVBoxLayout(self)
        main.setContentsMargins(16, 16, 16, 14)
        main.setSpacing(10)

        header = QFrame(self)
        header.setObjectName("PortugueseTaxDialogHeader")
        h_box = QHBoxLayout(header)
        h_box.setContentsMargins(14, 12, 14, 12)
        h_box.setSpacing(12)

        icon_lbl = QLabel("🏛️", header)
        icon_lbl.setFixedSize(36, 36)
        icon_lbl.setAlignment(Qt.AlignCenter)
        icon_lbl.setStyleSheet("font-size: 22px;")
        h_box.addWidget(icon_lbl)

        title_box = QVBoxLayout()
        title_box.setSpacing(2)
        title_lbl = QLabel("Fiscalidade · Cenários e vendas registadas", header)
        title_lbl.setStyleSheet(
            f"font-size: 16px; font-weight: 800; color: {theme.text};"
        )
        sub_lbl = QLabel(
            "As posições abertas mostram um cenário hipotético de venda hoje. "
            "O mapa de vendas usa apenas encerramentos registados explicitamente, "
            "com os respetivos custos, datas e câmbio histórico.",
            header,
        )
        sub_lbl.setStyleSheet(f"font-size: 12px; color: {theme.muted};")
        sub_lbl.setWordWrap(True)
        title_box.addWidget(title_lbl)
        title_box.addWidget(sub_lbl)
        h_box.addLayout(title_box, 1)
        main.addWidget(header)

        self.metrics_grid = QGridLayout()
        self.metrics_grid.setSpacing(8)

        # BUG-DLG-05: scrollable body keeps buttons reachable on 1366x768/HiDPI
        body_scroll = QScrollArea(self)
        body_scroll.setWidgetResizable(True)
        body_scroll.setFrameShape(QFrame.NoFrame)
        body = QWidget(body_scroll)
        body_lay = QVBoxLayout(body)
        body_lay.setContentsMargins(0, 0, 0, 0)
        body_lay.setSpacing(10)

        body_lay.addLayout(self.metrics_grid)

        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(
            [
                "Ativo / Símbolo",
                "Categoria",
                "Custo Aquisição",
                "Valor Atual",
                "Ganho potencial",
                "Cenário hipotético",
                "Estimativa do cenário",
            ]
        )
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        for col in range(1, 7):
            self.table.horizontalHeader().setSectionResizeMode(
                col, QHeaderView.ResizeToContents
            )
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setMinimumHeight(240)
        body_lay.addWidget(self.table, 1)

        year_row = QHBoxLayout()
        year_row.addWidget(QLabel("Vendas realizadas · ano:"))
        self.fiscal_year = QSpinBox()
        self.fiscal_year.setRange(1900, 2200)
        self.fiscal_year.setValue(date.today().year)
        year_row.addWidget(self.fiscal_year)
        year_row.addStretch(1)
        body_lay.addLayout(year_row)
        self.realized_status = QLabel()
        self.realized_status.setWordWrap(True)
        self.realized_status.setTextFormat(Qt.PlainText)
        body_lay.addWidget(self.realized_status)
        self.realized_table = QTableWidget(0, 6)
        self.realized_table.setHorizontalHeaderLabels([
            "Ativo", "Aquisição", "Venda", "Realização EUR", "Ganho líquido EUR", "Estado / Proveniência",
        ])
        self.realized_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.realized_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        body_lay.addWidget(self.realized_table)

        actions = QHBoxLayout()
        self.copy_btn = QPushButton("📋 Copiar Resumo Fiscal")
        self.copy_btn.setObjectName("Secondary")
        self.copy_btn.clicked.connect(self._copy_summary)
        self.close_btn = QPushButton("Fechar")
        self.close_btn.setObjectName("Primary")
        self.close_btn.clicked.connect(self.accept)
        actions.addWidget(self.copy_btn)
        self.csv_btn = QPushButton("Exportar vendas CSV")
        self.pdf_btn = QPushButton("Exportar vendas PDF")
        self.csv_btn.clicked.connect(lambda: self._export_realized("csv"))
        self.pdf_btn.clicked.connect(lambda: self._export_realized("pdf"))
        actions.addWidget(self.csv_btn)
        actions.addWidget(self.pdf_btn)
        actions.addStretch(1)
        actions.addWidget(self.close_btn)
        body_lay.addLayout(actions)
        body_scroll.setWidget(body)
        main.addWidget(body_scroll, 1)

        self.fiscal_year.valueChanged.connect(self._populate_tax_data)
        self._populate_tax_data()

    def _populate_tax_data(self) -> None:
        from services.tax import (
            OFFSHORE_JURISDICTIONS,
            TAXA_AGRAVADA_OFFSHORE,
            TAXA_LIBERATORIA_PADRAO,
        )
        from utils.helpers import eur, row_get, safe_float

        if not hasattr(self, "theme") or self.theme is None:
            if hasattr(self.parent(), "theme") and getattr(self.parent(), "theme", None) is not None:
                self.theme = self.parent().theme
            else:
                self.theme = theme_for_widget(self.parent() or self)
        privacy = bool(getattr(self.theme, "privacy", False))

        holdings = list(
            self.repo.q("SELECT * FROM holdings ORDER BY asset_class, name")
        )

        total_cost = 0.0
        total_value = 0.0
        total_gains_gross = 0.0
        total_gains_exempt = 0.0
        total_gains_taxable = 0.0
        total_taxable_losses = 0.0
        weighted_tax_rate_sum = 0.0
        total_estimated_tax = 0.0

        today = date.today()
        summary_lines = [
            "=== CENÁRIO HIPOTÉTICO DE VENDA DAS POSIÇÕES ABERTAS ===",
            "Não representa ganhos realizados nem uma declaração fiscal.",
            f"Data: {today.isoformat()}",
        ]

        self.table.setRowCount(len(holdings))
        for row_idx, row in enumerate(holdings):
            name = str(row_get(row, "name") or "Ativo")
            cat = str(
                row_get(row, "asset_class") or row_get(row, "category") or "Outro"
            )
            cost = max(
                0.0, safe_float(row_get(row, "cost_basis", row_get(row, "value", 0)))
            )
            val = max(0.0, safe_float(row_get(row, "value", 0)))
            acquisition_fees = max(0.0, safe_float(row_get(row, "fees", 0)))
            cost_known = bool(row_get(row, "cost_basis_known", cost > 0))
            currency = str(row_get(row, "currency", "EUR") or "EUR").upper()
            if currency != "EUR":
                from services.tax_reporting import _historical_rate

                historical = _historical_rate(row, None)
                val *= safe_float(row_get(row, "exchange_rate", 1.0), 1.0)
                if historical is None:
                    cost_known = False
                else:
                    cost *= historical
                    acquisition_fees *= historical
            gain = val - cost - acquisition_fees
            if not cost_known:
                gain = 0.0

            is_crypto = (
                "crypto" in cat.lower()
                or "cripto" in cat.lower()
                or "bitcoin" in name.lower()
            )
            country_code = str(
                row_get(row, "jurisdiction", row_get(row, "country_code", "PT"))
            ).strip().upper()[:2] or "PT"
            is_foreign = bool(row_get(row, "is_foreign", False)) or (country_code != "PT")
            is_offshore = country_code in OFFSHORE_JURISDICTIONS

            acq_date_str = str(
                row_get(
                    row,
                    "acquisition_date",
                    row_get(row, "buy_date", row_get(row, "created_at", "")),
                )
                or ""
            )
            days_held: int | None = None
            if acq_date_str:
                try:
                    acq_d = date.fromisoformat(acq_date_str[:10])
                    days_held = max(0, (today - acq_d).days)
                except (ValueError, TypeError):
                    days_held = None

            total_cost += cost
            total_value += val
            if gain > 0:
                total_gains_gross += gain

            is_exempt = False
            if is_crypto:
                if is_offshore:
                    rate = TAXA_AGRAVADA_OFFSHORE
                    is_exempt = False
                    regime = f"Cripto (Offshore · Art. 72.º n.º 14) · {rate:.0f}%"
                    tax_val = (
                        round(gain * (rate / 100.0), 2)
                        if gain > 0
                        else 0.0
                    )
                else:
                    rate = TAXA_LIBERATORIA_PADRAO
                    if days_held is None:
                        is_exempt = False
                        regime = "Cripto (Sem data comprovada) · 28%"
                        tax_val = (
                            round(gain * (rate / 100.0), 2)
                            if gain > 0
                            else 0.0
                        )
                    elif days_held >= 365:
                        is_exempt = True
                        regime = f"Isento (>{days_held}d Art. 10.º CIRS)"
                        tax_val = 0.0
                    else:
                        is_exempt = False
                        regime = f"Cripto <365d ({days_held}d) · 28%"
                        tax_val = (
                            round(gain * (rate / 100.0), 2)
                            if gain > 0
                            else 0.0
                        )
            else:
                rate = (
                    TAXA_AGRAVADA_OFFSHORE if is_offshore else TAXA_LIBERATORIA_PADRAO
                )
                rate_str = f"{rate:.0f}%"
                regime = (
                    f"Anexo J · {rate_str}" if is_foreign else f"Anexo G · {rate_str}"
                )
                tax_val = round(gain * (rate / 100.0), 2) if gain > 0 else 0.0

            if is_exempt:
                if gain > 0:
                    total_gains_exempt += gain
            else:
                if gain > 0:
                    total_gains_taxable += gain
                    weighted_tax_rate_sum += gain * (rate / 100.0)
                elif gain < 0:
                    total_taxable_losses += abs(gain)

            gain_str = "••••• €" if privacy else (("+" if gain > 0 else "") + eur(gain))
            tax_str = (
                "••••• €"
                if privacy
                else (eur(tax_val) if not is_exempt else "Isento (€0,00)")
            )
            if not cost_known:
                regime = "Incompleto: custo ou câmbio histórico desconhecido"
                gain_str = tax_str = "Indisponível"
            if privacy:
                regime = "Cenário oculto (modo de privacidade)"
            vals = [
                "Dados ocultos" if privacy else name,
                "Dados ocultos" if privacy else cat,
                eur(cost, privacy) if cost_known else "Desconhecido",
                eur(val, privacy),
                gain_str,
                regime,
                tax_str,
            ]
            for col_idx, text in enumerate(vals):
                item = QTableWidgetItem(text)
                if col_idx in (2, 3, 4, 6):
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                    if privacy:
                        item.setToolTip("Modo de privacidade ativo")
                if col_idx == 4:
                    if gain > 0:
                        item.setForeground(QColor("#30D158"))
                    elif gain < 0:
                        item.setForeground(QColor("#FF453A"))
                if col_idx == 5 and is_exempt:
                    item.setForeground(QColor("#30D158"))
                self.table.setItem(row_idx, col_idx, item)

            summary_lines.append(
                f"• {'Dados ocultos' if privacy else name} ({'Dados ocultos' if privacy else cat}): "
                f"Custo {eur(cost, privacy) if cost_known else 'Desconhecido'} | "
                f"Valor {eur(val, privacy)} | Ganho {gain_str} | {regime}"
            )

        # BUG-AUD-C61: Net capital losses against capital gains within the fiscal year (Art. 43.º CIRS)
        net_taxable_balance = total_gains_taxable - total_taxable_losses
        if net_taxable_balance > 0 and total_gains_taxable > 0:
            effective_rate = weighted_tax_rate_sum / total_gains_taxable
            total_estimated_tax = round(net_taxable_balance * effective_rate, 2)
        else:
            total_estimated_tax = 0.0

        # BUG-AUD-C100: Set informative copy button tooltip in privacy mode
        if privacy:
            self.copy_btn.setToolTip(
                "Copia o resumo fiscal com montantes mascarados (modo de privacidade ativo)"
            )
        else:
            self.copy_btn.setToolTip(
                "Copia o resumo fiscal detalhado para a área de transferência"
            )

        # Metrics cards
        from ui.components.cards import MetricCard
        from utils.helpers import BLUE, GREEN, ORANGE, PURPLE

        while self.metrics_grid.count():
            item = self.metrics_grid.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        metrics = (
            (
                "Mais-Valias Brutas",
                eur(total_gains_gross, privacy),
                "ganho potencial total",
                BLUE,
                "📈",
            ),
            (
                "Isenções Cripto",
                eur(total_gains_exempt, privacy),
                ">365 dias isento",
                GREEN,
                "🛡️",
            ),
            (
                "Ganhos do cenário",
                eur(total_gains_taxable, privacy),
                "hipótese de venda hoje",
                PURPLE,
                "🏷️",
            ),
            (
                "Imposto do cenário",
                eur(total_estimated_tax, privacy),
                "estimativa; confirmar regras do ano",
                ORANGE,
                "🏛️",
            ),
        )
        for index, m in enumerate(metrics):
            self.metrics_grid.addWidget(MetricCard(*m), 0, index)

        summary_lines.extend(
            [
                "---------------------------------------",
                f"Total Mais-Valias Brutas: {eur(total_gains_gross, privacy)}",
                f"Total Isenções Cripto: {eur(total_gains_exempt, privacy)}",
                f"Ganhos do cenário hipotético: {eur(total_gains_taxable, privacy)}",
                f"Imposto estimado do cenário: {eur(total_estimated_tax, privacy)}",
            ]
        )
        self._refresh_realized_report(summary_lines, privacy)
        self._summary_text = "\n".join(summary_lines)

    def _refresh_realized_report(self, summary_lines: list[str], privacy: bool) -> None:
        from services.tax_reporting import build_realized_report, load_disposal_records
        from utils.helpers import eur

        self._realized_report = build_realized_report(
            load_disposal_records(self.repo), self.fiscal_year.value(),
        )
        report = self._realized_report
        status = report["limitation"]
        if not report["rows"]:
            status += " Não existem vendas registadas neste ano."
        if report["issues"] or any(not row["complete"] for row in report["rows"]):
            status += " Apuramento incompleto: confirma os dados em falta antes de exportar."
        self.realized_status.setText(status)
        self.realized_table.setRowCount(len(report["rows"]))
        summary_lines.extend(["", f"=== VENDAS REALIZADAS · {report['fiscal_year']} ===", status])
        for index, row in enumerate(report["rows"]):
            gain = eur(row["net_gain_eur"], privacy) if row["net_gain_eur"] is not None else "Indisponível"
            values = [
                "Dados ocultos" if privacy else row["name"],
                "Dados ocultos" if privacy else row["acquisition_date"],
                "Dados ocultos" if privacy else row["sale_date"],
                eur(row["sale_value_eur"], privacy), gain,
                "; ".join(row["issues"]) or row["provenance"],
            ]
            for column, value in enumerate(values):
                self.realized_table.setItem(index, column, QTableWidgetItem(value))
            summary_lines.append(" | ".join(values))
        enabled = report["export_allowed"] and not privacy
        self.csv_btn.setEnabled(enabled)
        self.pdf_btn.setEnabled(enabled)
        tooltip = "Confirma os dados e a proveniência; exportação indisponível em modo de privacidade."
        self.csv_btn.setToolTip(tooltip)
        self.pdf_btn.setToolTip(tooltip)

    def _export_realized(self, kind: str) -> None:
        from services.tax_reporting import realized_report_csv, realized_report_html

        # Rebuild immediately before exporting: neither stale privacy state nor
        # a changed persistent receipt may bypass the evidence gate.
        self._populate_tax_data()
        privacy = bool(getattr(self.theme, "privacy", False))
        try:
            content = (
                realized_report_csv(self._realized_report, privacy=privacy)
                if kind == "csv" else realized_report_html(self._realized_report, privacy=privacy)
            )
        except ValueError as exc:
            QMessageBox.warning(self, "Mapa de apoio fiscal", str(exc))
            return
        if QMessageBox.question(
            self, "Confirmar proveniência", self._realized_report["limitation"],
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        ) != QMessageBox.Yes:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Exportar mapa de vendas", f"vendas_{self.fiscal_year.value()}.{kind}",
            "CSV (*.csv)" if kind == "csv" else "PDF (*.pdf)",
        )
        if not path:
            return
        try:
            if kind == "csv":
                Path(path).write_text(content, encoding="utf-8-sig")
            else:
                from services.export_documents import render_html_to_pdf

                render_html_to_pdf(content, path)
        except (OSError, RuntimeError) as exc:
            QMessageBox.warning(self, "Exportação fiscal", str(exc))

    def _copy_summary(self) -> None:
        app = QApplication.instance()
        clipboard = app.clipboard() if app is not None else None
        if clipboard is not None and self._summary_text:
            clipboard.setText(self._summary_text)
            QMessageBox.information(
                self,
                "Fiscalidade IRS",
                "Resumo fiscal copiado para a área de transferência com sucesso!",
            )
