"""Readable, reusable detail dialog for interactive financial charts."""

from __future__ import annotations

import csv
from html import escape
import io
import re
from typing import Any

from PySide6.QtWidgets import QApplication

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ui.core.layouts import FlowLayout
from ui.core.theme import Theme
from utils.helpers import eur
from utils.output_security import privacy_export_cell, spreadsheet_safe_cell


_EMAIL_REGEX = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
_IBAN_REGEX = re.compile(r"(?i)\b(?:PT\d{2}|[A-Z]{2}\d{2})(?:[\s-]?[A-Z0-9]){11,30}\b")
# BUG-VIZ-PRIVACY-LEAK-DETAIL-001: prose fields (explanation, subtitles) may
# embed formatted currency from detail providers. Under privacy mode every
# €-amount is redacted so no real value can leak through free text. The
# currency token is mandatory so plain numbers (years, percentages) survive.
_MONEY_TEXT_REGEX = re.compile(
    r"(?i)-?\s*\d{1,3}(?:[. \u00a0]\d{3})*(?:,\d{1,2})?\s*(?:€|euros?)"
    r"|-?\d+(?:[.,]\d{1,2})?\s*(?:€|euros?)"
)


def _redact_detail_text(text: str, privacy: bool) -> str:
    if not privacy or not text:
        return text
    text = _EMAIL_REGEX.sub("[EMAIL]", text)
    text = _IBAN_REGEX.sub("[IBAN]", text)
    text = _MONEY_TEXT_REGEX.sub("••••• €", text)
    return text


_TONE_COLOURS = {
    "positive": "#16803A",
    "negative": "#D92D20",
    "warning": "#C66A00",
    "info": "#0A84FF",
    "neutral": "#64748B",
}


class ChartDetailDialog(QDialog):
    """Show the evidence and exact values behind one selected chart point."""

    def __init__(
        self,
        payload: dict[str, Any],
        theme: Theme,
        *,
        action_label: str = "",
        pin_label: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.payload = dict(payload or {})
        self.theme = theme
        self.pin_requested = False
        self.setObjectName("ChartDetailDialog")
        self.setWindowTitle(
            "Detalhes do gráfico" if theme.privacy
            else str(self.payload.get("title") or "Detalhes do gráfico")
        )
        self.setModal(True)
        self.setMinimumSize(540, 420)
        self.setMaximumSize(780, 720)

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(10)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(10)
        icon = QLabel(str(self.payload.get("icon") or "🔎"))
        icon.setObjectName("ChartDetailIcon")
        icon.setAlignment(Qt.AlignCenter)
        icon.setFixedSize(42, 42)
        title_wrap = QVBoxLayout()
        title_wrap.setContentsMargins(0, 0, 0, 0)
        title_wrap.setSpacing(1)
        self.title_label = QLabel(
            _redact_detail_text(
                "Detalhes do gráfico" if self.theme.privacy
                else str(self.payload.get("title") or "Detalhes do gráfico"),
                self.theme.privacy,
            )
        )
        self.title_label.setObjectName("ChartDetailTitle")
        self.title_label.setTextFormat(Qt.PlainText)
        self.title_label.setWordWrap(True)
        title_wrap.addWidget(self.title_label)
        self.subtitle_label = QLabel(
            _redact_detail_text(
                str(privacy_export_cell(
                    str(self.payload.get("subtitle") or ""),
                    "description", self.theme.privacy,
                )) if self.payload.get("subtitle") else "",
                self.theme.privacy,
            )
        )
        self.subtitle_label.setObjectName("ChartDetailSubtitle")
        self.subtitle_label.setTextFormat(Qt.PlainText)
        self.subtitle_label.setWordWrap(True)
        self.subtitle_label.setVisible(bool(self.subtitle_label.text().strip()))
        title_wrap.addWidget(self.subtitle_label)
        header.addWidget(icon, 0, Qt.AlignTop)
        header.addLayout(title_wrap, 1)
        root.addLayout(header)

        self.explanation_label = QLabel(
            _redact_detail_text(
                str(
                    self.payload.get("explanation")
                    or "Este detalhe corresponde ao ponto selecionado no gráfico."
                ),
                self.theme.privacy,
            )
        )
        self.explanation_label.setObjectName("ChartDetailExplanation")
        self.explanation_label.setTextFormat(Qt.PlainText)
        self.explanation_label.setWordWrap(True)
        root.addWidget(self.explanation_label)

        self.metrics_host = QWidget()
        self.metrics_host.setObjectName("ChartDetailMetrics")
        self.metrics_flow = FlowLayout(
            self.metrics_host,
            margin=0,
            hspacing=7,
            vspacing=7,
        )
        self.metric_labels: list[QLabel] = []
        is_privacy = bool(self.theme.privacy)
        for metric in list(self.payload.get("metrics") or []):
            if isinstance(metric, dict):
                label = str(metric.get("label") or "Valor")
                value = metric.get("value")
                tone = str(metric.get("tone") or "neutral")
                sensitive = bool(metric.get("sensitive"))
            elif isinstance(metric, (tuple, list)) and len(metric) >= 2:
                label = str(metric[0] or "Valor")
                value = metric[1]
                tone = str(metric[2] if len(metric) > 2 else "neutral")
                sensitive = False
            else:
                continue

            if is_privacy:
                label = _redact_detail_text(label, True)
                if sensitive:
                    value_str = "•••"
                elif isinstance(value, (int, float)):
                    value_str = eur(float(value), True)
                elif isinstance(value, str):
                    if "•••" in value:
                        value_str = value
                    elif value.strip().endswith("%"):
                        value_str = value
                    else:
                        clean_v = (
                            value.replace("€", "")
                            .replace(" ", "")
                            .replace(",", ".")
                            .strip()
                        )
                        try:
                            float(clean_v)
                            value_str = "••••• €"
                        except ValueError:
                            value_str = _redact_detail_text(value, True)
                else:
                    value_str = "—" if value is None else str(value)
            else:
                value_str = str(value if value is not None else "—")

            chip = QLabel(f"{label}\n{value_str}")
            chip.setProperty("detailTone", tone)
            chip.setTextFormat(Qt.PlainText)
            chip.setWordWrap(True)
            chip.setMinimumWidth(138)
            chip.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
            self.metrics_flow.addWidget(chip)
            self.metric_labels.append(chip)
        self.metrics_host.setVisible(bool(self.metric_labels))
        root.addWidget(self.metrics_host)

        calculation = dict(self.payload.get("calculation") or {})
        self.calculation_host = QFrame()
        self.calculation_host.setObjectName("ChartCalculationHost")
        calculation_layout = QVBoxLayout(self.calculation_host)
        calculation_layout.setContentsMargins(12, 10, 12, 10)
        calculation_layout.setSpacing(5)
        calculation_title = QLabel("🧮 Como este valor foi calculado")
        calculation_title.setObjectName("ChartCalculationTitle")
        calculation_layout.addWidget(calculation_title)
        calculation_fields = (
            ("Período", calculation.get("period")),
            ("Filtros", calculation.get("filters")),
            ("Movimentos considerados", calculation.get("movements")),
            ("Exclusões", calculation.get("exclusions")),
            ("Cobertura real", calculation.get("data_coverage")),
            ("Leitura concluída", calculation.get("updated_at")),
        )
        self.calculation_labels: list[QLabel] = []
        for label, value in calculation_fields:
            if value in (None, ""):
                continue
            if self.theme.privacy and label not in ("Período", "Leitura concluída"):
                value = "Dados ocultos" if label in ("Filtros", "Exclusões") else "•••"
            detail = QLabel(f"<b>{escape(label)}:</b> {escape(str(value))}")
            detail.setObjectName("ChartCalculationRow")
            detail.setTextFormat(Qt.RichText)
            detail.setWordWrap(True)
            detail.setTextInteractionFlags(Qt.TextSelectableByMouse)
            calculation_layout.addWidget(detail)
            self.calculation_labels.append(detail)
        self.calculation_host.setVisible(bool(self.calculation_labels))
        root.addWidget(self.calculation_host)

        rows = self._movement_rows()
        self.rows_title = QLabel(
            str(self.payload.get("rows_title") or "Movimentos que explicam o valor")
        )
        self.rows_title.setObjectName("ChartDetailRowsTitle")
        self.rows_title.setTextFormat(Qt.PlainText)
        self.rows_title.setVisible(
            bool(rows) or bool(self.payload.get("show_empty_rows"))
        )
        root.addWidget(self.rows_title)

        self.rows_scroll = QScrollArea()
        self.rows_scroll.setObjectName("ChartDetailRowsScroll")
        self.rows_scroll.setWidgetResizable(True)
        self.rows_scroll.setFrameShape(QFrame.NoFrame)
        self.rows_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.rows_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.rows_scroll.setMinimumHeight(110)
        self.rows_scroll.setMaximumHeight(300)
        rows_host = QWidget()
        rows_host.setObjectName("ChartDetailRowsHost")
        self.rows_layout = QVBoxLayout(rows_host)
        self.rows_layout.setContentsMargins(0, 0, 4, 0)
        self.rows_layout.setSpacing(6)
        self.rows_layout.setAlignment(Qt.AlignTop)
        if rows:
            for row in rows:
                self.rows_layout.addWidget(self._movement_row(row))
        else:
            empty = QLabel(
                str(
                    self.payload.get("empty_rows_text")
                    or "Não existem movimentos individuais associados a este ponto."
                )
            )
            empty.setObjectName("ChartDetailEmpty")
            empty.setTextFormat(Qt.PlainText)
            empty.setWordWrap(True)
            self.rows_layout.addWidget(empty)
        self.rows_scroll.setWidget(rows_host)
        self.rows_scroll.setVisible(
            bool(rows) or bool(self.payload.get("show_empty_rows"))
        )
        root.addWidget(self.rows_scroll, 1)

        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 2, 0, 0)
        buttons.addStretch(1)
        # BUG-DETAIL-EXP-01: CSV clipboard export for the movement rows. The
        # raw numeric amount is preserved in the payload rows; while privacy
        # mode is active the CSV carries the masked value instead.
        self.copy_csv_button: QPushButton | None = None
        if rows:
            self.copy_csv_button = QPushButton("Copiar CSV")
            self.copy_csv_button.setObjectName("Secondary")
            self.copy_csv_button.setToolTip(
                "Copia os movimentos listados para a área de transferência "
                "em formato CSV (delimitador ;)"
            )
            self.copy_csv_button.clicked.connect(self._copy_rows_csv)
            buttons.addWidget(self.copy_csv_button)
        close_button = QPushButton("Fechar")
        close_button.setObjectName("Secondary")
        close_button.clicked.connect(self.reject)
        buttons.addWidget(close_button)
        self.pin_button: QPushButton | None = None
        selected_pin = str(pin_label or "").strip()
        if selected_pin:
            self.pin_button = QPushButton(selected_pin)
            self.pin_button.setObjectName("Secondary")
            self.pin_button.clicked.connect(self._request_pin)
            buttons.addWidget(self.pin_button)
        self.action_button: QPushButton | None = None
        selected_action = str(
            action_label or self.payload.get("action_label") or ""
        ).strip()
        if selected_action:
            self.action_button = QPushButton(selected_action)
            self.action_button.setObjectName("Primary")
            self.action_button.clicked.connect(self.accept)
            buttons.addWidget(self.action_button)
        root.addLayout(buttons)

        self._apply_theme()
        self._fit_to_screen(parent)

    def _request_pin(self) -> None:
        self.pin_requested = True
        self.reject()

    def _movement_rows(self) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in list(self.payload.get("rows") or [])
            if isinstance(row, dict)
        ]

    def _csv_value(self, row: dict[str, Any]) -> str:
        raw_val = row.get("value")
        if raw_val is None:
            raw_val = row.get("amount")
        if self.theme.privacy:
            if isinstance(raw_val, (int, float)):
                return eur(float(raw_val), True)
            if isinstance(raw_val, str) and raw_val.strip() and "•••" not in raw_val:
                return "••••• €"
            return str(raw_val or "—")
        if isinstance(raw_val, (int, float)):
            return repr(float(raw_val))
        return str(raw_val if raw_val is not None else "—")

    def _build_rows_csv(self) -> str:
        """Build the semicolon-delimited CSV of the movement rows.

        BUG-DETAIL-EXP-01: the raw numeric amount is exported verbatim
        (machine-readable, dot decimal) unless privacy mode masks it.
        """
        output = io.StringIO(newline="")
        writer = csv.writer(output, delimiter=";", lineterminator="\n")
        writer.writerow(["Data", "Descrição", "Categoria", "Valor"])
        for row in self._movement_rows():
            date_text = str(row.get("date") or "—")
            title_text = str(row.get("title") or row.get("description") or "Movimento")
            subtitle_text = str(row.get("subtitle") or row.get("category") or "")
            writer.writerow([
                spreadsheet_safe_cell(date_text),
                privacy_export_cell(title_text, "description", self.theme.privacy),
                privacy_export_cell(subtitle_text, "category", self.theme.privacy, mask_categories=True),
                spreadsheet_safe_cell(self._csv_value(row)),
            ])
        return output.getvalue()

    def _copy_rows_csv(self) -> None:
        QApplication.clipboard().setText(self._build_rows_csv())

    def _movement_row(self, row: dict[str, Any]) -> QFrame:
        frame = QFrame()
        frame.setObjectName("ChartDetailRow")
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(11, 8, 11, 8)
        layout.setSpacing(10)

        date_label = QLabel(str(row.get("date") or "—"))
        date_label.setObjectName("ChartDetailRowDate")
        date_label.setTextFormat(Qt.PlainText)
        date_label.setMinimumWidth(72)
        date_label.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        layout.addWidget(date_label, 0)

        text_wrap = QVBoxLayout()
        text_wrap.setContentsMargins(0, 0, 0, 0)
        text_wrap.setSpacing(1)
        raw_title = str(row.get("title") or row.get("description") or "Movimento")
        raw_subtitle = str(row.get("subtitle") or row.get("category") or "")
        if self.theme.privacy:
            raw_title = str(privacy_export_cell(raw_title, "description", True))
            raw_subtitle = str(privacy_export_cell(raw_subtitle, "category", True, mask_categories=True))
        title = QLabel(raw_title)
        title.setObjectName("ChartDetailRowTitle")
        title.setTextFormat(Qt.PlainText)
        title.setWordWrap(True)
        subtitle = QLabel(raw_subtitle)
        subtitle.setObjectName("ChartDetailRowSubtitle")
        subtitle.setTextFormat(Qt.PlainText)
        subtitle.setWordWrap(True)
        text_wrap.addWidget(title)
        subtitle.setVisible(bool(subtitle.text().strip()))
        text_wrap.addWidget(subtitle)
        layout.addLayout(text_wrap, 1)

        raw_val = row.get("value")
        if raw_val is None:
            raw_val = row.get("amount")
        if self.theme.privacy:
            if isinstance(raw_val, (int, float)):
                val_str = eur(float(raw_val), True)
            elif isinstance(raw_val, str) and raw_val.strip() and "•••" not in raw_val:
                val_str = "••••• €"
            elif raw_val is None:
                val_str = "—"
            else:
                val_str = str(raw_val)
        else:
            if isinstance(raw_val, (int, float)):
                val_str = eur(float(raw_val), False)
            else:
                val_str = str(raw_val or "—")

        value = QLabel(val_str)
        value.setObjectName("ChartDetailRowValue")
        value.setTextFormat(Qt.PlainText)
        value.setProperty("valueTone", str(row.get("tone") or "neutral"))
        value.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        value.setMinimumWidth(92)
        layout.addWidget(value, 0)
        return frame

    def _fit_to_screen(self, parent: QWidget | None) -> None:
        screen = self.screen()
        if screen is None and parent is not None:
            screen = parent.screen()
        if screen is None:
            self.resize(680, 560)
            return
        available = screen.availableGeometry()
        width = min(720, max(540, round(available.width() * 0.62)))
        height = min(680, max(440, round(available.height() * 0.68)))
        self.resize(width, height)

    def _apply_theme(self) -> None:
        dark = bool(self.theme.dark)
        surface = "#111827" if dark else "#F8FBFF"
        card = "#1E293B" if dark else "#FFFFFF"
        text = "#F8FAFC" if dark else "#142035"
        muted = "#A7B2C4" if dark else "#64748B"
        border = "rgba(255,255,255,0.16)" if dark else "rgba(20,32,53,0.14)"
        self.setStyleSheet(
            f"""
            QDialog#ChartDetailDialog {{
                background: {surface};
                color: {text};
            }}
            QLabel {{
                color: {text};
                background: transparent;
            }}
            QLabel#ChartDetailIcon {{
                background: rgba(10,132,255,0.14);
                border: 1px solid rgba(10,132,255,0.35);
                border-radius: 14px;
                font-size: 20px;
            }}
            QLabel#ChartDetailTitle {{
                font-size: 18px;
                font-weight: 800;
            }}
            QLabel#ChartDetailSubtitle,
            QLabel#ChartDetailExplanation,
            QLabel#ChartDetailRowDate,
            QLabel#ChartDetailRowSubtitle,
            QLabel#ChartDetailEmpty {{
                color: {muted};
            }}
            QLabel#ChartDetailExplanation {{
                padding: 8px 10px;
                background: rgba(10,132,255,0.08);
                border: 1px solid rgba(10,132,255,0.20);
                border-radius: 10px;
            }}
            QFrame#ChartCalculationHost {{
                background: {card};
                border: 1px solid {border};
                border-radius: 11px;
            }}
            QLabel#ChartCalculationTitle {{
                font-size: 13px;
                font-weight: 800;
            }}
            QLabel#ChartCalculationRow {{
                color: {muted};
                background: transparent;
                font-size: 11px;
            }}
            QWidget#ChartDetailMetrics QLabel {{
                padding: 8px 11px;
                background: {card};
                border: 1px solid {border};
                border-radius: 11px;
                font-weight: 650;
            }}
            QLabel#ChartDetailRowsTitle {{
                font-size: 13px;
                font-weight: 750;
                margin-top: 2px;
            }}
            QFrame#ChartDetailRow {{
                background: {card};
                border: 1px solid {border};
                border-radius: 11px;
            }}
            QLabel#ChartDetailRowTitle {{
                font-weight: 700;
            }}
            QLabel#ChartDetailRowValue {{
                font-weight: 800;
            }}
            QScrollArea#ChartDetailRowsScroll,
            QWidget#ChartDetailRowsHost {{
                background: transparent;
                border: none;
            }}
            """
        )
        for chip in self.metric_labels:
            tone = str(chip.property("detailTone") or "neutral")
            colour = _TONE_COLOURS.get(tone, _TONE_COLOURS["neutral"])
            parsed = QColor(colour)
            chip.setStyleSheet(
                f"color: {text}; border-color: {colour};"
                f"background: rgba({parsed.red()},{parsed.green()},"
                f"{parsed.blue()},28);"
            )
        for value in self.findChildren(QLabel, "ChartDetailRowValue"):
            tone = str(value.property("valueTone") or "neutral")
            colour = _TONE_COLOURS.get(tone, _TONE_COLOURS["neutral"])
            value.setStyleSheet(f"color: {colour};")


__all__ = ["ChartDetailDialog"]
