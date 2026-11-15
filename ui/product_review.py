"""Optional, local explanation of data gaps; no setup or repair is automatic."""

from __future__ import annotations

from PySide6.QtCore import QEvent, QRect, Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QHeaderView,
    QLabel,
    QPushButton,
    QScrollArea,
    QStyle,
    QStyleOptionViewItem,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from services.product_review import ProductReviewService


class QualityChecklistDialog(QDialog):
    def __init__(self, app, parent=None):
        super().__init__(parent)
        self.app = app
        self._layout_timer = QTimer(self)
        self._layout_timer.setSingleShot(True)
        self._layout_timer.timeout.connect(self._resize_table_rows)
        self.setWindowTitle("Qualidade e origem dos números")
        self.setStyleSheet(app.theme.stylesheet())
        layout = QVBoxLayout(self)
        self.body_scroll = QScrollArea()
        self.body_scroll.setWidgetResizable(True)
        content = QWidget()
        body = QVBoxLayout(content)
        hint = QLabel(
            "Consulta opcional e apenas de leitura. Abrir uma origem permite rever "
            "os dados; nada é corrigido, pago ou importado automaticamente. "
            "Uma checklist sem faltas assinaladas não certifica um extrato completo."
        )
        hint.setWordWrap(True)
        hint.setTextFormat(Qt.PlainText)
        body.addWidget(hint)
        rows = ProductReviewService(app.repo).quality_checklist()
        self._rows = rows
        self._presentation_state = (
            bool(app.theme.privacy),
            bool(app.theme.dark),
            app.theme.primary_color,
        )
        self.table = QTableWidget(len(rows), 5)
        self.table.setAccessibleName("Checklist opcional de qualidade dos dados")
        self.table.setHorizontalHeaderLabels(
            ["Estado", "Dado", "Registos", "Explicação local", "Origem"]
        )
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setTabKeyNavigation(False)
        self.table.setWordWrap(False)
        self.table.setMinimumHeight(200)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Interactive)
        self.table.setColumnWidth(3, 320)
        self.table.verticalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        for column in (0, 1, 2):
            self.table.horizontalHeader().setSectionResizeMode(
                column, QHeaderView.ResizeToContents
            )
        for index, item in enumerate(rows):
            values = [
                "Rever" if item["needs_review"] else "Sem falta assinalada",
                item["label"],
                "•••" if app.theme.privacy else str(item["count"]),
                item["explanation"],
            ]
            for column, value in enumerate(values):
                cell = QTableWidgetItem(value)
                if column == 3:
                    cell.setToolTip(item["explanation"])
                self.table.setItem(index, column, cell)
            action = QPushButton("Abrir origem")
            action.setObjectName("Secondary")
            action.setAccessibleName("Abrir origem · " + item["label"])
            action.setProperty("quality_row", index)
            action.installEventFilter(self)
            action.clicked.connect(
                lambda _checked=False, page=item["action_page"]: self._open(app, page)
            )
            self.table.setItem(index, 4, QTableWidgetItem(""))
            self.table.setCellWidget(index, 4, action)
        body.addWidget(self.table, 1)
        self.detail = QLabel()
        self.detail.setWordWrap(True)
        self.detail.setTextFormat(Qt.PlainText)
        self.detail.setAccessibleName("Explicação local do dado selecionado")
        body.addWidget(self.detail)
        self.table.currentCellChanged.connect(self._show_explanation)
        self.table.setCurrentCell(0, 0)
        explanation = QLabel(
            "Receitas e despesas usam a data de pagamento, ou a do movimento quando "
            "a primeira não existe. Transferências internas não são receitas/despesas; "
            "reembolsos recebidos já registados entram nas receitas. Poupança = receitas − despesas."
        )
        explanation.setWordWrap(True)
        explanation.setTextFormat(Qt.PlainText)
        body.addWidget(explanation)
        self.body_scroll.setWidget(content)
        layout.addWidget(self.body_scroll, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.button(QDialogButtonBox.Close).setText("Fechar")
        buttons.button(QDialogButtonBox.Close).setObjectName("Secondary")
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        available = self.screen().availableGeometry()
        self.resize(min(960, available.width() - 32), min(560, available.height() - 32))
        self._privacy_timer = QTimer(self)
        self._privacy_timer.setInterval(120)
        self._privacy_timer.timeout.connect(self.update_privacy)
        self._privacy_timer.start()
        self._layout_timer.start(0)

    def update_privacy(self) -> None:
        state = (
            bool(self.app.theme.privacy),
            bool(self.app.theme.dark),
            self.app.theme.primary_color,
        )
        if state == self._presentation_state:
            return
        self._presentation_state = state
        self.setStyleSheet(self.app.theme.stylesheet())
        for index, item in enumerate(self._rows):
            self.table.item(index, 2).setText("•••" if state[0] else str(item["count"]))

    def _open(self, app, page: str) -> None:
        self.accept()
        app.goto(page)

    def _show_explanation(self, row: int, *_args) -> None:
        if 0 <= row < len(self._rows):
            item = self._rows[row]
            self.detail.setText(item["label"] + " · " + item["explanation"])

    def eventFilter(self, watched, event) -> bool:
        if event.type() == QEvent.FocusIn:
            row = watched.property("quality_row")
            if row is not None:
                self.table.setCurrentCell(int(row), 4)
                self.table.scrollToItem(self.table.item(int(row), 4))
                self.body_scroll.ensureWidgetVisible(watched)
        return super().eventFilter(watched, event)

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() in (QEvent.FontChange, QEvent.StyleChange):
            timer = getattr(self, "_layout_timer", None)
            if timer is not None:
                timer.start(0)

    def _resize_table_rows(self) -> None:
        # QHeaderView's item-only hint can clip the embedded themed buttons.
        # Measure the actual item's styled content rect rather than assuming
        # that the table's cell padding is available to the child button.
        option = QStyleOptionViewItem()
        option.initFrom(self.table)
        option.rect = QRect(0, 0, 1000, 1000)
        content = self.table.style().subElementRect(
            QStyle.SE_ItemViewItemText, option, self.table
        )
        hints = [
            self.table.cellWidget(row, 4).sizeHint()
            for row in range(self.table.rowCount())
        ]
        height = max(hint.height() for hint in hints) + 1000 - content.height() + 2
        width = max(hint.width() for hint in hints) + 1000 - content.width() + 2
        self.table.verticalHeader().setMinimumSectionSize(height)
        self.table.setColumnWidth(4, width)
        self.table.resizeRowsToContents()
