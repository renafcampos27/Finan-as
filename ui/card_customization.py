from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)

from ui.core.theme import Theme, theme_for_widget
from ui.core.smart_table import SmartTableWidgetItem as QTableWidgetItem
from utils.qt_runtime import is_qt_valid


class PageCardCustomizationDialog(QDialog):
    """Visibility and safe permanent-disable controls for one application page."""

    def __init__(
        self,
        page_title: str,
        entries: list[dict[str, Any]],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Personalizar · {page_title}")
        self.setModal(True)
        screen = parent.screen() if parent is not None else QApplication.primaryScreen()
        available = screen.availableGeometry() if screen is not None else None
        maximum_width = (
            max(320, available.width() - 48) if available is not None else 650
        )
        maximum_height = (
            max(320, available.height() - 72) if available is not None else 720
        )
        desired_height = max(430, 220 + len(entries) * 48)
        target_width = min(650, maximum_width)
        target_height = min(720, desired_height, maximum_height)
        self.setMinimumSize(
            min(460, target_width),
            min(360, target_height),
        )
        self.setMaximumSize(maximum_width, maximum_height)
        self.resize(target_width, target_height)
        self._rows: list[tuple[str, QCheckBox, QCheckBox]] = []
        self._theme = theme_for_widget(parent)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)
        heading = QLabel(f"🧩 Personalizar {page_title}")
        heading.setObjectName("CardCustomizationHeading")
        layout.addWidget(heading)
        intro = QLabel(
            "Escolhe os cards visíveis. “Desativar” mantém o card recuperável, "
            "mas impede o seu desenho e interação nos próximos arranques."
        )
        intro.setWordWrap(True)
        intro.setObjectName("CardCustomizationIntro")
        layout.addWidget(intro)

        self.table = QTableWidget(len(entries), 4)
        self.table.setProperty("columnPersistenceId", "customization.page_cards")
        self.table.setObjectName("CardCustomizationTable")
        self.table.setHorizontalHeaderLabels(
            ["Card de informação ou gráfico", "Mostrar", "Desativar", "Elementos"]
        )
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(42)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionMode(QAbstractItemView.NoSelection)
        self.table.horizontalHeader().setStretchLastSection(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Fixed)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Fixed)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Fixed)
        self.table.setColumnWidth(1, 85)
        self.table.setColumnWidth(2, 95)
        self.table.setColumnWidth(3, 135)

        for row, entry in enumerate(entries):
            key = str(entry["key"])
            title = str(entry["title"])
            visible = bool(entry.get("visible", True))
            permanent = bool(entry.get("permanent", False))
            title_item = QTableWidgetItem(title)
            title_item.setFlags(Qt.ItemIsEnabled)
            self.table.setItem(row, 0, title_item)
            show = QCheckBox()
            show.setChecked(visible and not permanent)
            disable = QCheckBox()
            disable.setChecked(permanent)
            show.setEnabled(not permanent)
            show.setAccessibleName(f"Mostrar {title}")
            disable.setAccessibleName(f"Desativar {title}")
            show_item = QTableWidgetItem("")
            disable_item = QTableWidgetItem("")
            show_item.setFlags(Qt.ItemIsEnabled)
            disable_item.setFlags(Qt.ItemIsEnabled)
            self.table.setItem(row, 1, show_item)
            self.table.setItem(row, 2, disable_item)
            disable.toggled.connect(
                lambda checked, show_box=show: self._toggle_permanent(show_box, checked)
            )
            phrase_btn = QPushButton("⚙️ Elementos…")
            phrase_btn.setObjectName("CardPhraseTableBtn")
            phrase_btn.setToolTip("Personalizar ou desativar elementos dinâmicos deste card (frases, base real, badges, métricas)")
            phrase_btn.clicked.connect(
                lambda _checked=False, e=entry: self._edit_card_phrase(e)
            )
            phrase_item = QTableWidgetItem("")
            phrase_item.setFlags(Qt.ItemIsEnabled)
            self.table.setItem(row, 3, phrase_item)
            self.table.setCellWidget(row, 1, self._centred(show))
            self.table.setCellWidget(row, 2, self._centred(disable))
            self.table.setCellWidget(row, 3, self._centred(phrase_btn))
            self.table.setRowHeight(row, 42)
            self._rows.append((key, show, disable))
        layout.addWidget(self.table, 1)

        footer = QHBoxLayout()
        reset = QPushButton("Restaurar todos")
        reset.setObjectName("CardDialogSecondary")
        reset.clicked.connect(self._restore_all)
        footer.addWidget(reset)
        footer.addStretch(1)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        save_button = buttons.button(QDialogButtonBox.Save)
        cancel_button = buttons.button(QDialogButtonBox.Cancel)
        if save_button is not None:
            save_button.setObjectName("CardDialogPrimary")
            save_button.setText("Guardar")
        if cancel_button is not None:
            cancel_button.setObjectName("CardDialogSecondary")
            cancel_button.setText("Cancelar")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        footer.addWidget(buttons)
        layout.addLayout(footer)
        self._apply_theme(self._theme)

    def _apply_theme(self, theme: Theme) -> None:
        if theme.dark:
            background = "#0F172A"
            surface = "#182235"
            alternate = "#202C40"
            header = "#273449"
            text = "#F8FAFC"
            muted = "#CBD5E1"
            border = "#52647C"
            button = "#273449"
            disabled = "#64748B"
        else:
            background = "#F6F9FE"
            surface = "#FFFFFF"
            alternate = "#EEF4FB"
            header = "#DFEAF7"
            text = "#142035"
            muted = "#52637A"
            border = "#AFC0D5"
            button = "#FFFFFF"
            disabled = "#94A3B8"
        accent = "#0A84FF"
        positive = "#10B981"

        palette = self.palette()
        palette.setColor(QPalette.Window, QColor(background))
        palette.setColor(QPalette.Base, QColor(surface))
        palette.setColor(QPalette.AlternateBase, QColor(alternate))
        palette.setColor(QPalette.Text, QColor(text))
        palette.setColor(QPalette.WindowText, QColor(text))
        palette.setColor(QPalette.Button, QColor(button))
        palette.setColor(QPalette.ButtonText, QColor(text))
        palette.setColor(QPalette.Highlight, QColor(accent))
        palette.setColor(QPalette.HighlightedText, QColor("#FFFFFF"))
        self.setPalette(palette)
        self.setAutoFillBackground(True)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet(
            f"""
            QDialog {{
                background-color: {background};
                color: {text};
            }}
            QLabel#CardCustomizationHeading {{
                color: {text};
                font-size: 19px;
                font-weight: 750;
                padding: 2px 2px 4px 2px;
            }}
            QLabel#CardCustomizationIntro {{
                color: {muted};
                font-size: 13px;
                line-height: 1.3;
                padding: 0 2px 5px 2px;
            }}
            QTableWidget#CardCustomizationTable {{
                background-color: {surface};
                alternate-background-color: {alternate};
                color: {text};
                gridline-color: {border};
                border: 1px solid {border};
                border-radius: 12px;
                selection-background-color: {accent};
                selection-color: #FFFFFF;
                outline: none;
            }}
            QTableWidget#CardCustomizationTable::item {{
                color: {text};
                padding: 8px 10px;
                border-bottom: 1px solid {border};
            }}
            QHeaderView::section {{
                background-color: {header};
                color: {text};
                border: none;
                border-right: 1px solid {border};
                border-bottom: 1px solid {border};
                padding: 10px 9px;
                font-weight: 700;
            }}
            QTableCornerButton::section {{
                background-color: {header};
                border: 1px solid {border};
            }}
            QCheckBox {{
                color: {text};
                spacing: 8px;
                background: transparent;
            }}
            QCheckBox::indicator {{
                width: 20px;
                height: 20px;
                border: 2px solid {border};
                border-radius: 6px;
                background-color: {surface};
            }}
            QCheckBox::indicator:hover {{
                border-color: {accent};
            }}
            QCheckBox::indicator:checked {{
                background-color: {positive};
                border-color: {positive};
            }}
            QCheckBox::indicator:disabled {{
                background-color: {alternate};
                border-color: {disabled};
            }}
            QPushButton {{
                min-height: 22px;
                padding: 7px 15px;
                border-radius: 9px;
                font-weight: 650;
            }}
            QPushButton#CardDialogSecondary {{
                background-color: {button};
                color: {text};
                border: 1px solid {border};
            }}
            QPushButton#CardDialogSecondary:hover {{
                border-color: {accent};
                background-color: {alternate};
            }}
            QPushButton#CardDialogPrimary {{
                background-color: {accent};
                color: #FFFFFF;
                border: 1px solid {accent};
            }}
            QPushButton#CardDialogPrimary:hover {{
                background-color: #0875E1;
                border-color: #0875E1;
            }}
            QPushButton#CardPhraseTableBtn {{
                min-height: 24px;
                max-height: 28px;
                min-width: 84px;
                padding: 2px 8px;
                font-size: 11px;
                font-weight: 600;
                border-radius: 6px;
                background-color: {button};
                color: {text};
                border: 1px solid {border};
            }}
            QPushButton#CardPhraseTableBtn:hover {{
                border-color: {accent};
                background-color: {alternate};
            }}
            QScrollBar:vertical {{
                background: transparent;
                width: 11px;
                margin: 3px;
            }}
            QScrollBar::handle:vertical {{
                background: {border};
                min-height: 30px;
                border-radius: 5px;
            }}
            QScrollBar::add-line:vertical,
            QScrollBar::sub-line:vertical {{
                height: 0;
            }}
            """
        )

    @staticmethod
    def _centred(widget: QWidget) -> QWidget:
        container = QWidget()
        row = QHBoxLayout(container)
        row.setContentsMargins(0, 0, 0, 0)
        row.setAlignment(Qt.AlignCenter)
        row.addWidget(widget)
        return container

    @staticmethod
    def _toggle_permanent(show: QCheckBox, checked: bool) -> None:
        if checked:
            show.setChecked(False)
        show.setEnabled(not checked)

    def _restore_all(self) -> None:
        for _key, show, disable in self._rows:
            disable.setChecked(False)
            show.setChecked(True)

    def state(self) -> dict[str, list[str]]:
        hidden: list[str] = []
        permanent: list[str] = []
        for key, show, disable in self._rows:
            if disable.isChecked():
                permanent.append(key)
            elif not show.isChecked():
                hidden.append(key)
        return {"hidden": hidden, "permanent": permanent}

    def _edit_card_phrase(self, entry: dict[str, Any]) -> None:
        card = entry.get("card")
        if card is not None and is_qt_valid(card) and hasattr(card, "show_phrase_dialog"):
            try:
                card.show_phrase_dialog()
                return
            except RuntimeError:
                pass
        from ui.card_phrase_dialog import CardPhraseCustomizationDialog

        title = str(entry.get("title", "Card"))
        dialog = CardPhraseCustomizationDialog(
            card_title=title,
            original_description="",
            current_description="",
            preferences={},
            has_comparison=True,
            has_footer=True,
            parent=self,
        )
        dialog.exec()
