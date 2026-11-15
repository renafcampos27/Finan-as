from __future__ import annotations

from typing import Any, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ui.core.theme import Theme, theme_for_widget


class CardPhraseCustomizationDialog(QDialog):
    """Liquid Glass modal to customize and dynamically toggle elements/phrases on a card."""

    def __init__(
        self,
        card_title: str,
        original_description: str = "",
        current_description: str = "",
        preferences: Optional[dict[str, Any]] = None,
        has_comparison: bool = False,
        has_footer: bool = False,
        parent: Optional[QWidget] = None,
        *,
        card: Optional[Any] = None,
        elements: Optional[dict[str, dict[str, Any]]] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Personalizar Elementos · {card_title}")
        self.setModal(True)
        self.resize(600, 530)
        self.setMinimumSize(460, 420)

        self._card_title = str(card_title or "Card")
        self._card = card
        self._prefs = dict(preferences or {})
        self._theme = theme_for_widget(parent)

        # Build elements dictionary dynamically
        if elements is not None:
            self._elements = dict(elements)
        elif card is not None and hasattr(card, "available_customization_elements"):
            self._elements = card.available_customization_elements()
        else:
            # Fallback for legacy calls
            self._elements = {}
            if original_description or current_description:
                self._elements["description"] = {
                    "key": "show_description",
                    "title": "Frase explicativa / subtítulo",
                    "preview": original_description or current_description,
                    "custom_key": "custom_description",
                    "custom_text": current_description,
                    "icon": "💬",
                    "enabled": bool(self._prefs.get("show_description", True)),
                    "supports_text": True,
                }
            if has_comparison:
                self._elements["comparison"] = {
                    "key": "show_comparison",
                    "title": "Nota de comparação de períodos",
                    "preview": "",
                    "icon": "📊",
                    "enabled": bool(self._prefs.get("show_comparison", True)),
                    "supports_text": False,
                }
            if has_footer:
                self._elements["footer"] = {
                    "key": "show_footer",
                    "title": "Notas de rodapé / metodologia",
                    "preview": "",
                    "custom_key": "custom_footer",
                    "custom_text": str(
                        self._prefs.get("custom_footer", "") or ""
                    ).strip(),
                    "icon": "📌",
                    "enabled": bool(self._prefs.get("show_footer", True)),
                    "supports_text": True,
                }

        self._original_description = str(
            original_description
            or (
                self._elements.get("description", {}).get("preview", "")
                if "description" in self._elements
                else ""
            )
        ).strip()

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(20, 18, 20, 18)
        main_layout.setSpacing(12)

        heading = QLabel(f"🧩 Personalização Dinâmica · {self._card_title}")
        heading.setObjectName("CardPhraseHeading")
        heading.setWordWrap(True)
        main_layout.addWidget(heading)

        intro = QLabel(
            "Ativa ou desativa os elementos e frases deste card de forma independente "
            "e personaliza os comentários. As escolhas são guardadas para o teu perfil."
        )
        intro.setObjectName("CardPhraseIntro")
        intro.setWordWrap(True)
        main_layout.addWidget(intro)

        # Scroll area for responsive dynamic element controls
        scroll = QScrollArea(self)
        scroll.setObjectName("CardPhraseScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.viewport().setObjectName("CardPhraseViewport")
        scroll.viewport().setAutoFillBackground(False)
        scroll.viewport().setAttribute(Qt.WA_StyledBackground, True)
        scroll_content = QWidget()
        scroll_content.setObjectName("CardPhraseScrollContent")
        scroll_content.setAutoFillBackground(False)
        scroll_content.setAttribute(Qt.WA_StyledBackground, True)
        scroll_layout = QVBoxLayout(scroll_content)
        scroll_layout.setContentsMargins(4, 4, 8, 4)
        scroll_layout.setSpacing(8)

        self._checkboxes: dict[str, QCheckBox] = {}
        self.show_description_check: Optional[QCheckBox] = None
        self.show_comparison_check: Optional[QCheckBox] = None
        self.show_footer_check: Optional[QCheckBox] = None
        self.show_data_coverage_check: Optional[QCheckBox] = None

        section_label = QLabel("Elementos detetados neste card:")
        section_label.setObjectName("CardPhraseSectionTitle")
        scroll_layout.addWidget(section_label)

        for _elem_id, info in self._elements.items():
            key = str(info["key"])
            title = str(info["title"])
            icon = str(info.get("icon", "👁️"))
            enabled = bool(self._prefs.get(key, info.get("enabled", True)))

            cb = QCheckBox(f"{icon}  Mostrar {title.lower()}")
            cb.setObjectName(f"Check_{key}")
            cb.setChecked(enabled)
            self._checkboxes[key] = cb
            scroll_layout.addWidget(cb)

            if key == "show_description":
                self.show_description_check = cb
            elif key == "show_comparison":
                self.show_comparison_check = cb
            elif key == "show_footer":
                self.show_footer_check = cb
            elif key == "show_data_coverage":
                self.show_data_coverage_check = cb

            preview = str(info.get("preview", "") or "").strip()
            if preview:
                prev_lbl = QLabel(f"Exemplo atual: {preview}")
                prev_lbl.setObjectName("CardPhrasePreview")
                prev_lbl.setWordWrap(True)
                scroll_layout.addWidget(prev_lbl)

        # Fallbacks for legacy attribute references
        if self.show_description_check is None:
            self.show_description_check = QCheckBox(
                "💬  Mostrar frase explicativa principal / comentário"
            )
            self.show_description_check.setChecked(
                bool(self._prefs.get("show_description", True))
            )
            self._checkboxes["show_description"] = self.show_description_check
            scroll_layout.addWidget(self.show_description_check)

        if has_comparison and self.show_comparison_check is None:
            self.show_comparison_check = QCheckBox(
                "📊  Mostrar nota de comparação de períodos"
            )
            self.show_comparison_check.setChecked(
                bool(self._prefs.get("show_comparison", True))
            )
            self._checkboxes["show_comparison"] = self.show_comparison_check
            scroll_layout.addWidget(self.show_comparison_check)

        if has_footer and self.show_footer_check is None:
            self.show_footer_check = QCheckBox(
                "📌  Mostrar notas de rodapé / metodologia"
            )
            self.show_footer_check.setChecked(
                bool(self._prefs.get("show_footer", True))
            )
            self._checkboxes["show_footer"] = self.show_footer_check
            scroll_layout.addWidget(self.show_footer_check)

        # Text editor for custom explanation
        editor_label = QLabel("Texto da frase explicativa personalizada:")
        editor_label.setObjectName("CardPhraseLabel")
        scroll_layout.addWidget(editor_label)

        self.text_edit = QPlainTextEdit()
        self.text_edit.setObjectName("CardPhraseEdit")
        self.text_edit.setPlaceholderText(
            self._original_description
            if self._original_description
            else "Escreve um comentário ou frase personalizada para este card…"
        )
        init_desc = str(
            current_description
            or self._prefs.get("custom_description", "")
            or self._elements.get("description", {}).get("custom_text", "")
            or ""
        ).strip()
        self.text_edit.setPlainText(init_desc)
        self.text_edit.setMinimumHeight(65)
        self.text_edit.setMaximumHeight(105)
        scroll_layout.addWidget(self.text_edit)

        # Text editor for custom footer if card has footer
        has_footer_elem = (
            "footer" in self._elements
            or has_footer
            or self.show_footer_check is not None
        )
        self.footer_edit: Optional[QPlainTextEdit] = None
        if has_footer_elem:
            footer_lbl = QLabel(
                "Texto personalizado para rodapé / metodologia (opcional):"
            )
            footer_lbl.setObjectName("CardPhraseLabel")
            scroll_layout.addWidget(footer_lbl)

            self.footer_edit = QPlainTextEdit()
            self.footer_edit.setObjectName("CardPhraseFooterEdit")
            self.footer_edit.setPlaceholderText(
                "Deixa em branco para usar o rodapé original…"
            )
            self.footer_edit.setPlainText(
                str(self._prefs.get("custom_footer", "") or "").strip()
            )
            self.footer_edit.setMinimumHeight(55)
            self.footer_edit.setMaximumHeight(85)
            scroll_layout.addWidget(self.footer_edit)

        scroll_layout.addStretch(1)
        scroll.setWidget(scroll_content)
        main_layout.addWidget(scroll, 1)

        # Actions row
        actions_row = QHBoxLayout()
        self.btn_reset_text = QPushButton("Restaurar frase original")
        self.btn_reset_text.setObjectName("CardPhraseBtnSecondary")
        self.btn_reset_text.clicked.connect(self._reset_to_original)
        actions_row.addWidget(self.btn_reset_text)
        actions_row.addStretch(1)
        main_layout.addLayout(actions_row)

        # Footer Dialog Buttons
        footer = QHBoxLayout()
        footer.addStretch(1)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        btn_save = buttons.button(QDialogButtonBox.Save)
        btn_cancel = buttons.button(QDialogButtonBox.Cancel)
        if btn_save is not None:
            btn_save.setObjectName("CardPhraseBtnPrimary")
            btn_save.setText("Guardar")
        if btn_cancel is not None:
            btn_cancel.setObjectName("CardPhraseBtnSecondary")
            btn_cancel.setText("Cancelar")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        footer.addWidget(buttons)
        main_layout.addLayout(footer)

        self._apply_theme(self._theme)

    def _reset_to_original(self) -> None:
        self.text_edit.setPlainText(self._original_description)
        if self.footer_edit is not None:
            self.footer_edit.setPlainText("")
        for cb in self._checkboxes.values():
            cb.setChecked(True)

    def state(self) -> dict[str, Any]:
        result = dict(self._prefs)
        for key, cb in self._checkboxes.items():
            result[key] = cb.isChecked()

        custom_text = self.text_edit.toPlainText().strip()
        result["custom_description"] = (
            custom_text if custom_text != self._original_description else ""
        )
        if self.footer_edit is not None:
            result["custom_footer"] = self.footer_edit.toPlainText().strip()

        if "show_description" not in result and self.show_description_check is not None:
            result["show_description"] = self.show_description_check.isChecked()
        if "show_comparison" not in result and self.show_comparison_check is not None:
            result["show_comparison"] = self.show_comparison_check.isChecked()
        if "show_footer" not in result and self.show_footer_check is not None:
            result["show_footer"] = self.show_footer_check.isChecked()

        return result

    def _apply_theme(self, theme: Theme) -> None:
        if theme.dark:
            background = "#0F172A"
            surface = "#182235"
            alternate = "#202C40"
            text = "#F8FAFC"
            muted = "#94A3B8"
            border = "#475569"
            button = "#273449"
        else:
            background = "#F6F9FE"
            surface = "#FFFFFF"
            alternate = "#EEF4FB"
            text = "#142035"
            muted = "#52637A"
            border = "#AFC0D5"
            button = "#FFFFFF"
        accent = "#0A84FF"
        positive = "#10B981"

        palette = self.palette()
        palette.setColor(QPalette.Window, QColor(background))
        palette.setColor(QPalette.Base, QColor(surface))
        palette.setColor(QPalette.Text, QColor(text))
        palette.setColor(QPalette.WindowText, QColor(text))
        palette.setColor(QPalette.Button, QColor(button))
        palette.setColor(QPalette.ButtonText, QColor(text))
        palette.setColor(QPalette.Highlight, QColor(accent))
        self.setPalette(palette)
        self.setStyleSheet(
            f"""
            QDialog {{
                background-color: {background};
                color: {text};
            }}
            QLabel#CardPhraseHeading {{
                color: {text};
                font-size: 18px;
                font-weight: 750;
            }}
            QLabel#CardPhraseIntro {{
                color: {muted};
                font-size: 13px;
                line-height: 1.3;
            }}
            QLabel#CardPhraseSectionTitle {{
                color: {accent};
                font-size: 13px;
                font-weight: 700;
                margin-top: 4px;
                margin-bottom: 2px;
            }}
            QLabel#CardPhrasePreview {{
                color: {muted};
                font-size: 11px;
                font-style: italic;
                margin-left: 28px;
                margin-bottom: 6px;
            }}
            QLabel#CardPhraseLabel {{
                color: {text};
                font-size: 13px;
                font-weight: 650;
                margin-top: 6px;
            }}
            QScrollArea#CardPhraseScroll,
            QScrollArea#CardPhraseScroll > QWidget,
            QWidget#CardPhraseViewport,
            QWidget#CardPhraseScrollContent {{
                background: transparent;
                background-color: transparent;
                border: none;
            }}
            QPlainTextEdit#CardPhraseEdit, QPlainTextEdit#CardPhraseFooterEdit {{
                background-color: {surface};
                color: {text};
                border: 1px solid {border};
                border-radius: 10px;
                padding: 8px 10px;
                font-size: 13px;
            }}
            QPlainTextEdit#CardPhraseEdit:focus, QPlainTextEdit#CardPhraseFooterEdit:focus {{
                border-color: {accent};
            }}
            QCheckBox {{
                color: {text};
                spacing: 8px;
                font-size: 13px;
                font-weight: 550;
            }}
            QCheckBox::indicator {{
                width: 19px;
                height: 19px;
                border: 2px solid {border};
                border-radius: 5px;
                background-color: {surface};
            }}
            QCheckBox::indicator:hover {{
                border-color: {accent};
            }}
            QCheckBox::indicator:checked {{
                background-color: {positive};
                border-color: {positive};
            }}
            QPushButton {{
                min-height: 22px;
                padding: 6px 14px;
                border-radius: 8px;
                font-weight: 600;
                font-size: 12px;
            }}
            QPushButton#CardPhraseBtnSecondary {{
                background-color: {button};
                color: {text};
                border: 1px solid {border};
            }}
            QPushButton#CardPhraseBtnSecondary:hover {{
                border-color: {accent};
                background-color: {alternate};
            }}
            QPushButton#CardPhraseBtnPrimary {{
                background-color: {accent};
                color: #FFFFFF;
                border: 1px solid {accent};
            }}
            QPushButton#CardPhraseBtnPrimary:hover {{
                background-color: #0875E1;
            }}
            QScrollBar:vertical {{
                background: transparent;
                width: 10px;
                margin: 2px;
            }}
            QScrollBar::handle:vertical {{
                background: {border};
                min-height: 26px;
                border-radius: 5px;
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
                height: 0;
            }}
            """
        )
