"""Paged, explicit category decisions over an immutable full import batch."""

from __future__ import annotations

import math

from PySide6.QtCore import QEvent, QRect, Qt, QTimer
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QGridLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QStyle,
    QStyleOptionViewItem,
    QScrollArea,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from services.imports import ImportReviewBatch
from utils.helpers import eur
from utils.output_security import html_text, privacy_export_cell


class ImportReviewDialog(QDialog):
    PAGE_SIZE = 100

    def __init__(self, batch: ImportReviewBatch, parent=None, *, privacy: bool = False):
        super().__init__(parent)
        self._layout_timer = QTimer(self)
        self._layout_timer.setSingleShot(True)
        self._layout_timer.timeout.connect(self._resize_row_editors)
        self.review = batch
        self.privacy = privacy
        self._fallback_privacy = privacy
        self._theme_identity = None
        self.setWindowTitle("Rever categorias e linhas do extrato")
        self.setMinimumSize(300, 260)
        screen = self.screen().availableGeometry()
        self.resize(min(1100, screen.width() - 32), min(650, screen.height() - 32))
        root = QVBoxLayout(self)
        self.content_scroll = QScrollArea(self)
        self.content_scroll.setWidgetResizable(True)
        self.content_scroll.setMinimumSize(0, 0)
        self.content_scroll.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        content = QWidget(self.content_scroll)
        layout = QVBoxLayout(content)
        self.content_scroll.setWidget(content)
        root.addWidget(self.content_scroll, 1)
        explanation = QLabel(
            "As categorias sugeridas só serão usadas depois de aceites. Rejeitar "
            "mantém a categoria original/base; excluir remove apenas essa linha "
            "desta importação. Nenhuma escolha cria uma regra nova."
        )
        explanation.setWordWrap(True)
        layout.addWidget(explanation)
        toolbar = QGridLayout()
        self.previous = QPushButton("Página anterior")
        self.previous.setShortcut("Alt+Left")
        self.following = QPushButton("Página seguinte")
        self.following.setShortcut("Alt+Right")
        self.page = QSpinBox()
        self.page.setRange(1, max(1, math.ceil(len(batch.rows) / self.PAGE_SIZE)))
        self.page.setAccessibleName("Página de revisão")
        self.accept_all = QPushButton("Todas: aceitar…")
        self.accept_all.setToolTip(
            "Aceitar sugestões de todas as páginas do lote, após confirmação."
        )
        self.reject_all = QPushButton("Todas: rejeitar…")
        self.reject_all.setToolTip(
            "Manter as categorias originais/base em todas as páginas, após confirmação."
        )
        for index, control in enumerate(
            (self.previous, self.page, self.following, self.accept_all, self.reject_all)
        ):
            toolbar.addWidget(control, index // 3, index % 3)
        layout.addLayout(toolbar)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.summary.setTextFormat(Qt.PlainText)
        layout.addWidget(self.summary)
        self.table = QTableWidget(0, 8)
        self.table.setAccessibleName(
            "Revisão de todas as linhas do extrato, por páginas"
        )
        self.table.setHorizontalHeaderLabels(
            [
                "Linha",
                "Data",
                "Descrição",
                "Valor",
                "Original/base",
                "Sugestão",
                "Explicação",
                "Escolha",
            ]
        )
        self.table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.Stretch
        )
        self.table.horizontalHeader().setSectionResizeMode(
            6, QHeaderView.ResizeMode.Stretch
        )
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setTabKeyNavigation(False)
        layout.addWidget(self.table, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Save).setText("Guardar escolhas")
        self.save_button = buttons.button(QDialogButtonBox.Save)
        self.save_button.setShortcut("Ctrl+Return")
        buttons.button(QDialogButtonBox.Cancel).setText("Cancelar")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)
        for button in self.findChildren(QPushButton):
            button.setObjectName("Secondary")
        self.page.valueChanged.connect(self._fill)
        self.previous.clicked.connect(lambda: self.page.setValue(self.page.value() - 1))
        self.following.clicked.connect(
            lambda: self.page.setValue(self.page.value() + 1)
        )
        self.accept_all.clicked.connect(lambda: self._choose_all("accept"))
        self.reject_all.clicked.connect(lambda: self._choose_all("reject"))
        self._privacy_timer = QTimer(self)
        self._privacy_timer.setInterval(100)
        self._privacy_timer.timeout.connect(self.sync_privacy)
        self._privacy_timer.start()
        self._fill()
        self.sync_privacy()

    def _owner_theme(self):
        owner = self.parentWidget()
        while owner is not None:
            app = getattr(owner, "app", None)
            theme = getattr(app, "theme", getattr(owner, "theme", None))
            if theme is not None:
                return theme
            owner = owner.parentWidget()
        return None

    def sync_privacy(self):
        theme = self._owner_theme()
        private = bool(getattr(theme, "privacy", self._fallback_privacy))
        if theme is not None and self._theme_identity != id(theme):
            self._theme_identity = id(theme)
            stylesheet = getattr(theme, "stylesheet", None)
            if callable(stylesheet):
                self.setStyleSheet(stylesheet())
        if self.privacy != private:
            self.privacy = private
            if private:
                QToolTip.hideText()
            start = (self.page.value() - 1) * self.PAGE_SIZE
            entries = self.review.rows[start : start + self.PAGE_SIZE]
            # Change text in place: a privacy check can run inside a combo's
            # signal handler, so never destroy that emitting receiver here.
            for index, entry in enumerate(entries):
                for column, value in enumerate(self._visible_values(entry)):
                    item = self.table.item(index, column)
                    if item is not None:
                        item.setText(str(value))
                        item.setToolTip(
                            "Dados ocultos" if private else html_text(str(value))
                        )
                choice = self.table.cellWidget(index, 7)
                if choice is not None:
                    choice.setEnabled(not private)
                    choice.setAccessibleName(
                        "Escolha do movimento"
                        if private
                        else f"Escolha da linha {entry.original.source_row}"
                    )
                    if private:
                        self._restore_choice(index, entry)
        self._update_summary()

    def accept(self):
        self.sync_privacy()
        if not self.privacy:
            super().accept()

    def done(self, result):
        if result == QDialog.DialogCode.Accepted:
            self.sync_privacy()
            if self.privacy:
                return
        self._privacy_timer.stop()
        self._layout_timer.stop()
        super().done(result)

    def _update_summary(self):
        if self.privacy:
            self.summary.setText(
                "Revisão em modo privado. Valores, categorias e originais estão ocultos."
            )
        else:
            self.summary.setText(
                f"{len(self.review.rows)} linha(s) no lote completo · "
                f"{self.review.pending_count} sugestão(ões) pendente(s) · "
                f"{self.review.excluded_count} linha(s) excluída(s) · "
                f"página {self.page.value()} de {self.page.maximum()}"
            )
        self.previous.setEnabled(self.page.value() > 1)
        self.following.setEnabled(self.page.value() < self.page.maximum())
        self.accept_all.setEnabled(not self.privacy)
        self.reject_all.setEnabled(not self.privacy)
        self.save_button.setEnabled(not self.privacy)

    def _fill(self, _index=0):
        theme = self._owner_theme()
        self.privacy = bool(getattr(theme, "privacy", self._fallback_privacy))
        start = (self.page.value() - 1) * self.PAGE_SIZE
        entries = self.review.rows[start : start + self.PAGE_SIZE]
        self.table.setRowCount(len(entries))
        for index, entry in enumerate(entries):
            source_row = entry.original.source_row
            private = self.privacy
            values = self._visible_values(entry)
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setToolTip("Dados ocultos" if private else html_text(str(value)))
                self.table.setItem(index, column, item)
            choice = QComboBox()
            choice.setProperty("import_review_row", index)
            choice.installEventFilter(self)
            if entry.has_suggestion:
                choice.addItem("Pendente", "pending")
                choice.addItem("Aceitar sugestão", "accept")
                choice.addItem("Rejeitar · manter original/base", "reject")
            else:
                choice.addItem("Manter original/base", "keep")
            choice.addItem("Excluir esta linha", "exclude")
            choice.setAccessibleName(
                "Escolha do movimento" if private else f"Escolha da linha {source_row}"
            )
            selected = choice.findData(entry.decision)
            if selected < 0 and entry.decision == "keep":
                selected = choice.findData("reject")
            choice.setCurrentIndex(max(0, selected))
            choice.setEnabled(not private)
            choice.currentIndexChanged.connect(
                lambda _value, row=source_row, combo=choice: self._choose(
                    row, combo.currentData()
                )
            )
            self.table.setItem(index, 7, QTableWidgetItem(""))
            self.table.setCellWidget(index, 7, choice)
        focus_order = (
            [self.table]
            + [self.table.cellWidget(row, 7) for row in range(len(entries))]
            + [self.save_button]
        )
        for previous, following in zip(focus_order, focus_order[1:]):
            QWidget.setTabOrder(previous, following)
        self._update_summary()
        self._layout_timer.start(0)

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() in (QEvent.FontChange, QEvent.StyleChange):
            timer = getattr(self, "_layout_timer", None)
            if timer is not None:
                timer.start(0)

    def _resize_row_editors(self) -> None:
        if not self.table.rowCount():
            return
        option = QStyleOptionViewItem()
        option.initFrom(self.table)
        option.rect = QRect(0, 0, 1000, 1000)
        content = self.table.style().subElementRect(
            QStyle.SE_ItemViewItemText, option, self.table
        )
        hints = [
            self.table.cellWidget(row, 7).sizeHint()
            for row in range(self.table.rowCount())
        ]
        # Embedded editor geometry excludes the table item's styled padding.
        for row, hint in enumerate(hints):
            self.table.setRowHeight(row, hint.height() + 1000 - content.height() + 2)
        self.table.setColumnWidth(
            7, max(hint.width() for hint in hints) + 1000 - content.width() + 2
        )

    def eventFilter(self, watched, event) -> bool:
        if event.type() == QEvent.FocusIn:
            row = watched.property("import_review_row")
            if row is not None:
                self.table.setCurrentCell(int(row), 7)
                self.table.scrollToItem(self.table.item(int(row), 7))
                self.content_scroll.ensureWidgetVisible(watched)
        return super().eventFilter(watched, event)

    def _visible_values(self, entry):
        private = self.privacy
        source_row = entry.original.source_row
        return [
            "•••" if private else str(source_row),
            privacy_export_cell(entry.original.date, "date", private),
            privacy_export_cell(entry.original.description, "description", private),
            eur(entry.original.amount, private),
            privacy_export_cell(
                entry.original.category, "category", private, mask_categories=True
            ),
            privacy_export_cell(
                entry.proposed.category, "category", private, mask_categories=True
            ),
            privacy_export_cell(
                entry.reason
                + (
                    " · " + entry.proposed.validation_message
                    if entry.proposed.validation_message
                    else ""
                ),
                "notes",
                private,
            ),
        ]

    def _choose(self, source_row: int, decision: str):
        self.sync_privacy()
        if self.privacy:
            start = (self.page.value() - 1) * self.PAGE_SIZE
            for index, entry in enumerate(
                self.review.rows[start : start + self.PAGE_SIZE]
            ):
                if entry.original.source_row == source_row:
                    self._restore_choice(index, entry)
            return
        self.review = self.review.choose(source_row, decision)
        self._update_summary()

    def _restore_choice(self, index, entry):
        choice = self.table.cellWidget(index, 7)
        selected = choice.findData(entry.decision)
        if selected < 0 and entry.decision == "keep":
            selected = choice.findData("reject")
        blocked = choice.blockSignals(True)
        choice.setCurrentIndex(max(0, selected))
        choice.blockSignals(blocked)

    def _choose_all(self, decision: str):
        self.sync_privacy()
        if self.privacy:
            return
        verb = "Aceitar" if decision == "accept" else "Rejeitar"
        detail = "As linhas excluídas mantêm-se excluídas. A escolha abrange todas as páginas."
        answer = QMessageBox.question(
            self,
            "Confirmar escolhas do lote",
            f"{verb} as sugestões incluídas deste lote?\n\n{detail}",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        self.sync_privacy()
        if answer == QMessageBox.Yes and not self.privacy:
            self.review = self.review.choose_all(decision)
            self._fill()
