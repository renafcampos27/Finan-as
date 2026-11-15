#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Finanças Pessoais – Liquid Glass
Nova interface PySide6 para a app local de finanças pessoais.
Mantém a base de dados e a app clássica intactas; esta é apenas uma camada visual moderna.
"""

from __future__ import annotations

import sys
import hashlib
from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from utils.qt_runtime import configure_qt_runtime

configure_qt_runtime()

try:
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QColor
    from PySide6.QtWidgets import (
        QCheckBox,
        QComboBox,
        QDoubleSpinBox,
        QFileDialog,
        QGridLayout,
        QHBoxLayout,
        QHeaderView,
        QLabel,
        QMessageBox,
        QPushButton,
        QSizePolicy,
        QTableWidget,
        QTextEdit,
        QToolTip,
        QVBoxLayout,
        QWidget,
    )
except (
    Exception
) as exc:  # pragma: no cover - only used when PySide6 is missing on the user's PC.
    print("PySide6 não está instalado. Instala com: pip install PySide6")
    print(exc)
    sys.exit(1)

from utils.app_logging import log_exception
from utils.output_security import html_text, privacy_export_cell
from utils.helpers import (
    APP_TITLE,
    GREEN,
    ORANGE,
    RED,
    add_months,
    eur,
    row_get,
    safe_float,
)
from ui.core.page import BasePage
from ui.core.form_accessibility import bind_field
from ui.core.smart_table import SmartTableWidgetItem as QTableWidgetItem
from ui.core.widgets import EuroSpinBox
from ui.components.cards import GlassCard
from ui.components.charts import DonutChart
from services.imports import (
    AMOUNT_LOCALE_AUTO,
    AMOUNT_LOCALE_INTERNATIONAL,
    AMOUNT_LOCALE_PT,
    ImportReviewBatch,
)
from utils.import_dates import DATE_FORMATS

if TYPE_CHECKING:
    from main import LiquidFinanceApp

# BUG-TOOL-SIM-01: month offsets (1, 2, 3) used with add_months to prevent skipping February.
_FALLBACK_MONTH_OFFSETS = (1, 2, 3)


class SplitsPage(BasePage):
    """Smart transaction splitting focused purely on splitting a single expense."""

    def __init__(self, app: "LiquidFinanceApp"):
        super().__init__(
            app,
            "Divisão inteligente",
            "Divide uma despesa complexa (ex: fatura de supermercado) em múltiplas categorias.",
        )
        self._updating = False
        self._split_rows = []

        # Editor Card
        editor_card = GlassCard("Divisão do Movimento", strong=True)

        top_layout = QHBoxLayout()
        top_layout.addWidget(QLabel("Seleciona o movimento a dividir:"))
        self.tx = QComboBox()
        self.tx.setMinimumWidth(400)
        self.tx.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.tx.currentIndexChanged.connect(self.on_transaction_changed)
        top_layout.addWidget(self.tx)
        top_layout.addStretch()

        main_layout = QHBoxLayout()

        # Donut Chart
        chart_container = QVBoxLayout()
        self.donut = DonutChart(lambda: self.app.theme)
        self.donut.setFixedSize(220, 220)
        self.donut.setAccessibleName("Divisão do movimento por categoria")
        self.donut.configure_details(self._split_detail_payload)
        chart_container.addWidget(self.donut, alignment=Qt.AlignCenter)
        self.donut_label = QLabel("Selecione um movimento")
        self.donut_label.setAlignment(Qt.AlignCenter)
        self.donut_label.setObjectName("Subtle")
        chart_container.addWidget(self.donut_label)
        chart_container.addStretch()

        main_layout.addLayout(chart_container)

        # Rows Grid
        grid_container = QVBoxLayout()
        grid = QGridLayout()
        grid.addWidget(QLabel("Categoria"), 0, 0)
        grid.addWidget(QLabel("Percentagem"), 0, 1)
        grid.addWidget(QLabel("Valor calculado"), 0, 2)

        cats = self.repo.categories("expense") or self.repo.categories()
        for i in range(6):
            cb = QComboBox()
            cb.addItem("—")
            cb.addItems(cats)
            pct = QDoubleSpinBox()
            pct.setRange(0, 100)
            pct.setDecimals(2)
            pct.setSuffix(" %")
            val = EuroSpinBox()
            val.setRange(0, 100_000)
            val.setDecimals(2)
            val.setSuffix(" €")
            grid.addWidget(cb, i + 1, 0)
            grid.addWidget(pct, i + 1, 1)
            grid.addWidget(val, i + 1, 2)
            self._split_rows.append({"category": cb, "percent": pct, "value": val})
            cb.currentTextChanged.connect(self.update_totals)
            pct.valueChanged.connect(self.on_percent_changed)
            val.valueChanged.connect(self.on_value_changed)

        grid_container.addLayout(grid)

        # Totals and Actions
        actions_layout = QHBoxLayout()
        self.total_diff_label = QLabel("Por atribuir: 0 €")
        self.total_diff_label.setStyleSheet("font-weight: bold;")

        self.apply_btn = QPushButton("Aplicar à despesa")
        self.apply_btn.setObjectName("Primary")
        self.apply_btn.clicked.connect(self.apply_split)
        self.clear_btn = QPushButton("Limpar")
        self.clear_btn.clicked.connect(self.clear_split)

        actions_layout.addWidget(self.total_diff_label)
        actions_layout.addStretch()
        actions_layout.addWidget(self.clear_btn)
        actions_layout.addWidget(self.apply_btn)

        grid_container.addSpacing(16)
        grid_container.addLayout(actions_layout)
        grid_container.addStretch()

        main_layout.addSpacing(32)
        main_layout.addLayout(grid_container)
        main_layout.setStretch(1, 1)

        editor_card.v.addLayout(top_layout)
        editor_card.v.addSpacing(16)
        editor_card.v.addLayout(main_layout)
        self.body.addWidget(editor_card)

        # Existing splits table
        self.table = QTableWidget(0, 5)
        self.table.setProperty("columnPersistenceId", "splits.list")
        self.table.setHorizontalHeaderLabels(
            ["Movimento", "Descrição", "Categoria", "%", "Valor"]
        )
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        existing_card = GlassCard("Divisões aplicadas a movimentos", strong=True)
        existing_card.v.addWidget(self.table)
        self.body.addWidget(existing_card)

        self.refresh()

    @property
    def _privacy(self) -> bool:
        return bool(getattr(getattr(self.app, "theme", None), "privacy", False))

    def current_transaction_amount(self) -> float:
        data = self.tx.currentData()
        if not data:
            return 0.0
        try:
            return abs(float(data.split("|")[1]))
        except (AttributeError, IndexError, TypeError, ValueError):
            return 0.0

    def reload_transactions(self) -> None:
        current_data = self.tx.currentData()
        selected_tx_id = ""
        if current_data:
            try:
                selected_tx_id = str(current_data).split("|")[0]
            except Exception:
                selected_tx_id = ""

        self.tx.blockSignals(True)
        self.tx.clear()

        txs = self.repo.transactions_filtered(kind="Despesas", limit=None)
        self.tx.addItem("— Não selecionado", "")
        selected_index = 0
        for tx in txs:
            tx = dict(tx)
            if str(tx.get("kind") or "").lower() != "expense":
                continue
            splits = self.repo.transaction_splits(tx["id"])
            amt = abs(safe_float(tx.get("amount", 0)))
            desc = tx.get("description", "")
            dt = tx.get("date", "")
            label = f"{dt} | {desc} | {eur(amt, self._privacy)}"
            if splits:
                label = "[Dividido] " + label
            idx = self.tx.count()
            self.tx.addItem(label, f"{tx['id']}|{amt}")
            if selected_tx_id and str(tx["id"]) == selected_tx_id:
                selected_index = idx

        if selected_index > 0:
            self.tx.setCurrentIndex(selected_index)
        else:
            self.clear_split()
        self.tx.blockSignals(False)

    def on_transaction_changed(self) -> None:
        if self._updating:
            return
        self.clear_split()
        data = self.tx.currentData()
        if not data:
            self.update_totals()
            return

        txid = int(data.split("|")[0])
        base = self.current_transaction_amount()
        splits = self.repo.transaction_splits(txid)
        if splits:
            self._updating = True
            has_stored = all(row_get(s, "amount", None) is not None for s in splits)
            for i, s in enumerate(splits[:6]):
                cat = str(row_get(s, "category", "") or "—")
                pct = safe_float(row_get(s, "percent", 0))
                cb = self._split_rows[i]["category"]
                if cat != "—" and cb.findText(cat) < 0:
                    cb.addItem(cat)
                cb.setCurrentText(cat)
                self._split_rows[i]["percent"].setValue(pct)
                if has_stored:
                    self._split_rows[i]["value"].setValue(
                        safe_float(row_get(s, "amount", 0))
                    )

            if not has_stored:
                total_pct = sum(
                    safe_float(row_get(s, "percent", 0)) for s in splits[:6]
                )
                norm = [
                    (
                        str(row_get(s, "category", "") or "—"),
                        safe_float(row_get(s, "percent", 0)),
                    )
                    for s in splits[:6]
                ]
                if total_pct < 100.0 - 1e-5:
                    norm.append(("__unassigned__", 100.0 - total_pct))
                allocated = self.repo._allocate_split_amounts(base, norm)
                for i, (_cat, _pct, split_amt) in enumerate(
                    allocated[: len(splits[:6])]
                ):
                    self._split_rows[i]["value"].setValue(split_amt)
            self._updating = False
        else:
            self._updating = True
            tx_row = self.repo.transaction_by_id(txid)
            tx_cat = ""
            if tx_row is not None:
                tx_cat = str(row_get(tx_row, "category", "") or "")
            cb = self._split_rows[0]["category"]
            if tx_cat:
                if cb.findText(tx_cat) < 0:
                    cb.addItem(tx_cat)
                cb.setCurrentText(tx_cat)
            elif cb.count() > 1:
                cb.setCurrentIndex(1)
            self._split_rows[0]["percent"].setValue(100.0)
            self._split_rows[0]["value"].setValue(base)
            self._updating = False

        self.update_totals()

    def on_percent_changed(self) -> None:
        if self._updating:
            return
        self._updating = True
        base = self.current_transaction_amount()
        active_rows = [
            (idx, r["category"].currentText(), r["percent"].value())
            for idx, r in enumerate(self._split_rows)
            if r["percent"].value() > 0
        ]
        if not active_rows or base <= 0:
            for r in self._split_rows:
                r["value"].setValue(0.0)
        else:
            total_pct = sum(pct for _idx, _cat, pct in active_rows)
            if total_pct <= 100.0 + 1e-5:
                unassigned_pct = max(0.0, 100.0 - total_pct)
                norm = [(cat, pct) for _idx, cat, pct in active_rows]
                if unassigned_pct > 0.001:
                    norm.append(("__unassigned__", unassigned_pct))
                try:
                    allocated = self.repo._allocate_split_amounts(base, norm)
                    for (idx, _cat, _pct), (_alloc_cat, _alloc_pct, alloc_amt) in zip(
                        active_rows, allocated[: len(active_rows)], strict=False
                    ):
                        self._split_rows[idx]["value"].setValue(alloc_amt)
                except Exception:
                    for idx, _cat, pct in active_rows:
                        self._split_rows[idx]["value"].setValue(
                            round(base * (pct / 100.0), 2)
                        )
            else:
                for idx, _cat, pct in active_rows:
                    self._split_rows[idx]["value"].setValue(
                        round(base * (pct / 100.0), 2)
                    )

            for idx, r in enumerate(self._split_rows):
                if r["percent"].value() <= 0:
                    r["value"].setValue(0.0)

        self._updating = False
        self.update_totals()

    def on_value_changed(self) -> None:
        if self._updating:
            return
        self._updating = True
        base = self.current_transaction_amount()
        for r in self._split_rows:
            if base > 0:
                r["percent"].setValue((r["value"].value() / base) * 100.0)
            else:
                r["percent"].setValue(0)
        self._updating = False
        self.update_totals()

    def clear_split(self) -> None:
        self._updating = True
        for r in self._split_rows:
            r["category"].setCurrentIndex(0)
            r["percent"].setValue(0)
            r["value"].setValue(0)
        self._updating = False
        self.update_totals()

    def update_totals(self) -> None:
        if self._updating:
            return

        base = self.current_transaction_amount()
        allocated = sum(
            r["value"].value()
            for r in self._split_rows
            if r["category"].currentText() != "—"
        )
        diff = base - allocated

        if diff < -0.01:
            self.total_diff_label.setStyleSheet(f"color: {RED}; font-weight: bold;")
        elif diff > 0.01:
            self.total_diff_label.setStyleSheet(f"color: {ORANGE}; font-weight: bold;")
        else:
            self.total_diff_label.setStyleSheet(f"color: {GREEN}; font-weight: bold;")

        diff_str = f"{diff:,.2f} €".replace(",", "X").replace(".", ",").replace("X", " ")
        self.total_diff_label.setText(
            f"Por atribuir: {diff_str}"
        )
        self.donut_label.setText(f"Total:\n{eur(base, self._privacy)}")

        slices = []
        colors = ["#3B82F6", "#10B981", "#F59E0B", "#EF4444", "#8B5CF6", "#EC4899"]
        idx = 0
        for row in self._split_rows:
            v = row["value"].value()
            cat = row["category"].currentText()
            if cat != "—" and v > 0:
                slices.append((cat, v, colors[idx % len(colors)]))
                idx += 1

        if diff > 0.01:
            slices.append(("Por atribuir", diff, "#94A3B8"))

        self.donut.set_data(slices)

    def _split_detail_payload(self, item: dict) -> dict:
        category = str(item.get("label") or "Categoria")
        value = safe_float(item.get("value"))
        total = sum(
            safe_float(slice_value) for _label, slice_value, _color in self.donut.data
        )
        share = value / total * 100 if total else 0.0
        return {
            "icon": "🧩",
            "title": "Divisão do movimento",
            "subtitle": category,
            "explanation": (
                "Esta parcela pertence apenas ao movimento atualmente "
                "selecionado; alterar a divisão atualiza o gráfico em tempo real."
            ),
            "metrics": [
                {
                    "label": "Valor atribuído",
                    "value": eur(value, self._privacy),
                    "tone": "neutral",
                },
                {
                    "label": "Peso no movimento",
                    "value": f"{share:.1f}%".replace(".", ","),
                    "tone": "neutral",
                },
                {
                    "label": "Total dividido",
                    "value": eur(total, self._privacy),
                    "tone": "neutral",
                },
            ],
        }

    def selected_splits(self) -> list[dict]:
        res = []
        for r in self._split_rows:
            c = r["category"].currentText()
            p = r["percent"].value()
            if c != "—" and p > 0:
                res.append({"category": c, "percent": p})
        return res

    def apply_split(self) -> None:
        data = self.tx.currentData()
        if not data:
            # BUG-UI-08: silent failure confused users; surface an explicit hint.
            QMessageBox.information(
                self,
                APP_TITLE,
                "Seleciona primeiro um movimento para dividir.",
            )
            return
        txid = int(data.split("|")[0])
        splits = self.selected_splits()
        if not splits:
            QMessageBox.warning(self, APP_TITLE, "Define pelo menos uma categoria.")
            return

        # Verify 100% total
        total_pct = sum(s["percent"] for s in splits)
        if abs(total_pct - 100.0) > 0.1:
            QMessageBox.warning(
                self, APP_TITLE, "As percentagens têm de somar exatamente 100%."
            )
            return

        try:
            self.app.change_journal_service.run_with_snapshot(
                [txid],
                lambda: self.repo.save_transaction_splits(
                    txid,
                    [(s["category"], s["percent"]) for s in splits],
                    commit=False,
                ),
                label="Divisão de movimento",
                action_type="split",
            )
        except Exception as exc:
            log_exception("Failed to save movement split with undo history", exc)
            QMessageBox.critical(
                self,
                APP_TITLE,
                "Não foi possível aplicar a divisão. O movimento ficou intacto.",
            )
            return
        self.app.notify_data_changed("Divisão aplicada.", selected_tx_id=txid)
        self.refresh()

    def set_privacy(self, privacy: bool) -> None:
        """Update presentation privacy and refresh splits table immediately."""
        theme = getattr(self.app, "theme", None)
        if theme is not None:
            theme.privacy = bool(privacy)
        self.refresh()

    def refresh(self) -> None:
        cats = self.repo.categories("expense") or self.repo.categories()
        for row_data in self._split_rows:
            cb = row_data["category"]
            current = cb.currentText()
            cb.blockSignals(True)
            cb.clear()
            cb.addItem("—")
            cb.addItems(cats)
            if current and cb.findText(current) >= 0:
                cb.setCurrentText(current)
            else:
                cb.setCurrentIndex(0)
            cb.blockSignals(False)

        self.reload_transactions()
        self.update_totals()

        # Refresh existing splits table
        self.table.setRowCount(0)
        txs = self.repo.transactions_with_splits()
        row = 0
        privacy = self._privacy
        for tx in txs:
            tx = dict(tx)
            splits = self.repo.transaction_splits(tx["id"])
            if splits:
                amt = abs(safe_float(tx.get("amount", 0)))
                self.table.insertRow(row)
                self.table.setItem(row, 0, QTableWidgetItem(str(tx.get("date", ""))))
                self.table.setItem(row, 1, QTableWidgetItem(str(tx.get("description", ""))))

                # Format splits to show in columns
                cats = "\n".join(str(row_get(s, "category", "") or "—") for s in splits)
                pct_items: list[str] = []
                for s in splits:
                    p = safe_float(row_get(s, "percent", 0))
                    pct_str = (
                        f"{p:.2f}".rstrip("0").rstrip(".")
                        if "." in f"{p:.2f}"
                        else f"{p:.0f}"
                    )
                    pct_items.append(f"{pct_str}%")
                pcts = "\n".join(pct_items)

                # D3: Use stored exact cent amount if present; otherwise derive via canonical allocator
                allocations: list[float] = []
                has_stored = all(row_get(s, "amount", None) is not None for s in splits)
                if has_stored:
                    allocations = [safe_float(row_get(s, "amount", 0)) for s in splits]
                else:
                    total_pct = sum(
                        safe_float(row_get(s, "percent", 0)) for s in splits
                    )
                    norm = [
                        (
                            str(row_get(s, "category", "") or "—"),
                            safe_float(row_get(s, "percent", 0)),
                        )
                        for s in splits
                    ]
                    if total_pct < 100.0 - 1e-5:
                        norm.append(("__unassigned__", 100.0 - total_pct))
                    allocated = self.repo._allocate_split_amounts(amt, norm)
                    allocations = [item[2] for item in allocated[: len(splits)]]

                # D2: Use presentation privacy policy on table amount strings
                def _fmt_split_amt(v: float, priv: bool) -> str:
                    if priv:
                        return "••••• €"
                    return f"{v:,.2f} €".replace(",", "X").replace(".", ",").replace("X", " ")

                vals = "\n".join(_fmt_split_amt(val, privacy) for val in allocations)

                c_item = QTableWidgetItem(cats)
                p_item = QTableWidgetItem(pcts)
                v_item = QTableWidgetItem(vals)
                # Retain numeric values in UserRole for calculation/inspection without presenting unmasked text
                v_item.setData(Qt.UserRole, allocations)
                if privacy:
                    v_item.setToolTip("Modo de privacidade ativo")
                else:
                    v_item.setToolTip(vals)

                self.table.setItem(row, 2, c_item)
                self.table.setItem(row, 3, p_item)
                self.table.setItem(row, 4, v_item)

                self.table.setRowHeight(row, max(40, len(splits) * 24))
                row += 1


class SmartImportPage(BasePage):
    def __init__(self, app: "LiquidFinanceApp"):
        super().__init__(
            app,
            "Importação inteligente",
            "Importar CSV/Excel simples, mapear colunas, ignorar duplicados e aplicar regras",
        )
        self.rows = []
        self.headers = []
        self.csv_encoding = ""
        self.csv_warnings: tuple[str, ...] = ()
        self.source_sha256 = ""
        self.source_path: Path | None = None
        self._review_batch: ImportReviewBatch | None = None
        self._review_dialog = None
        self.account_nature = "bank"
        top = QGridLayout()
        self.open_btn = QPushButton("Abrir CSV")
        self.open_btn.setObjectName("Primary")
        self.import_btn = QPushButton("Importar movimentos")
        self.import_btn.setEnabled(False)
        self.review_btn = QPushButton("Rever categorias e linhas…")
        self.review_btn.setEnabled(False)
        self.review_btn.setAccessibleName("Rever todas as linhas e sugestões do lote")
        self.ignore_dup = QCheckBox("Ignorar apenas duplicados certos")
        self.ignore_dup.setToolTip(
            "Compras prováveis ou possíveis nunca são descartadas automaticamente."
        )
        self.ignore_dup.setChecked(True)
        self.apply_rules = QCheckBox("Sugerir categorias por regras locais")
        self.apply_rules.setChecked(True)
        self.is_credit_card = QCheckBox("Inverter os sinais do ficheiro")
        self.is_credit_card.setToolTip(
            "Ativa apenas se o ficheiro tiver compras positivas e pagamentos negativos. A natureza da conta não altera os sinais automaticamente."
        )
        self.is_credit_card.setChecked(False)
        self.account_nature_label = QLabel("Conta: bancária / não identificada")
        self.account_nature_label.setWordWrap(True)
        self.account_nature_label.setMinimumWidth(0)
        for index, control in enumerate([
            self.open_btn, self.ignore_dup, self.apply_rules, self.is_credit_card,
            self.account_nature_label, self.review_btn, self.import_btn,
        ]):
            top.addWidget(control, index // 4, index % 4)
        self.register_responsive_grid(top, max_columns=4, min_column_width=220)
        map_card = GlassCard("Mapeamento de colunas", strong=True)
        grid = QGridLayout()
        map_card.v.addLayout(grid)
        self.map_date = QComboBox()
        self.map_desc = QComboBox()
        self.map_amount = QComboBox()
        self.map_amount_debit = QComboBox()
        self.map_amount_credit = QComboBox()
        self.map_category = QComboBox()
        self.map_account = QComboBox()
        self.map_method = QComboBox()
        self.map_merchant = QComboBox()
        self.map_reference = QComboBox()
        self.amount_locale = QComboBox()
        self.amount_locale.addItem(
            "Automático · assinalar ambiguidades", AMOUNT_LOCALE_AUTO
        )
        self.amount_locale.addItem("Português · 1.234,56", AMOUNT_LOCALE_PT)
        self.amount_locale.addItem(
            "Internacional · 1,234.56",
            AMOUNT_LOCALE_INTERNATIONAL,
        )
        self.date_format = QComboBox()
        self.date_format.setToolTip(
            "Escolhe a ordem indicada pelo banco/ficheiro. Anos com dois dígitos usam 2000–2099; para anos anteriores, usa datas com quatro dígitos."
        )
        self.date_format.addItem("Automático · pedir decisão se ambíguo", "")
        for fmt in DATE_FORMATS:
            self.date_format.addItem(fmt.replace("%Y", "AAAA").replace("%y", "AA").replace("%m", "MM").replace("%d", "DD"), fmt)
        maps = [
            ("Data", self.map_date),
            ("Descrição", self.map_desc),
            ("Valor único", self.map_amount),
            ("Débito (saída)", self.map_amount_debit),
            ("Crédito (entrada)", self.map_amount_credit),
            ("Categoria", self.map_category),
            ("Conta", self.map_account),
            ("Forma pag.", self.map_method),
            ("Comerciante", self.map_merchant),
            ("Referência bancária", self.map_reference),
            ("Formato dos valores", self.amount_locale),
            ("Formato das datas", self.date_format),
        ]
        for i, (label, combo) in enumerate(maps):
            field_group = QWidget()
            field_layout = QVBoxLayout(field_group)
            field_layout.setContentsMargins(0, 0, 0, 0)
            caption = QLabel(label)
            bind_field(caption, combo)
            field_layout.addWidget(caption)
            field_layout.addWidget(combo)
            combo.setMinimumWidth(0)
            combo.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
            grid.addWidget(field_group, i // 4, i % 4)
        self.register_responsive_grid(grid, max_columns=4, min_column_width=220)
        self.import_status = QLabel(
            "Abre um extrato CSV, OFX ou QIF para pré-visualizar, validar duplicados e importar."
        )
        self.import_status.setObjectName("Subtle")
        self.import_status.setWordWrap(True)
        self.import_status.setTextFormat(Qt.RichText)
        self.import_status.setAccessibleName("Estado da pré-visualização do extrato")
        self.preview = QTableWidget(0, 7)
        self.preview.setProperty("columnPersistenceId", "smart_import.preview")
        self.preview.setHorizontalHeaderLabels(
            ["Data", "Descrição", "Valor", "Categoria", "Conta", "Estado", "Motivo"]
        )
        self.preview.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.preview.horizontalHeader().setSectionResizeMode(6, QHeaderView.Stretch)
        self.preview.setAlternatingRowColors(True)
        self.preview.verticalHeader().setVisible(False)
        self.preview.setMinimumHeight(180)
        self.preview.setAccessibleName("Movimentos do extrato para revisão")
        card = GlassCard("Pré-visualização", strong=True)
        card.v.addWidget(self.preview)
        card.v.addWidget(self.import_status)
        self.body.addLayout(top)
        self.body.addWidget(map_card)
        self.body.addWidget(card)
        self.open_btn.clicked.connect(self.open_csv)
        self.import_btn.clicked.connect(self.import_rows)
        self.review_btn.clicked.connect(self.review_rows)
        for c in [
            self.map_date,
            self.map_desc,
            self.map_amount,
            self.map_amount_debit,
            self.map_amount_credit,
            self.map_category,
            self.map_account,
            self.map_method,
            self.map_merchant,
            self.map_reference,
        ]:
            c.currentIndexChanged.connect(self.populate_preview)
        self.amount_locale.currentIndexChanged.connect(self.populate_preview)
        self.date_format.currentIndexChanged.connect(self.populate_preview)
        self.apply_rules.stateChanged.connect(self.populate_preview)
        self.is_credit_card.stateChanged.connect(self.populate_preview)
        self.ignore_dup.stateChanged.connect(self.populate_preview)
        tab_fields = [self.open_btn, *(combo for _, combo in maps), self.ignore_dup,
                      self.apply_rules, self.is_credit_card, self.preview, self.review_btn, self.import_btn]
        for previous, following in zip(tab_fields, tab_fields[1:]):
            QWidget.setTabOrder(previous, following)

    def open_csv(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Abrir extrato bancário",
            str(Path.home()),
            "Extratos (*.csv *.ofx *.qfx *.qif);;CSV (*.csv);;OFX (*.ofx *.qfx);;QIF (*.qif);;Todos os ficheiros (*.*)",
        )
        if not path:
            return
        try:
            data = self.import_service.read_file(Path(path))
        except Exception as exc:
            QMessageBox.warning(
                self, APP_TITLE, f"Não foi possível ler o extrato:\n{exc}"
            )
            return
        self.headers = data.headers
        self.rows = data.rows
        self.csv_encoding = data.encoding
        self.csv_warnings = data.warnings
        self.detected_bank = self.import_service.detect_bank_dialect(self.headers)
        self.source_sha256 = getattr(data, "source_sha256", "")
        self.source_path = Path(path)
        self._review_batch = None
        self.account_nature = "credit_card" if data.is_credit_card else "bank"
        self.account_nature_label.setText("Conta: cartão de crédito" if data.is_credit_card else "Conta: bancária / não identificada")
        # Account nature is informational; sign inversion is always a separate choice.
        self.is_credit_card.blockSignals(True)
        self.is_credit_card.setChecked(False)
        self.is_credit_card.blockSignals(False)
        self.date_format.blockSignals(True)
        self.date_format.setCurrentIndex(max(0, self.date_format.findData(getattr(data, "date_format", ""))))
        self.date_format.blockSignals(False)
        opts = [""] + self.headers

        mapping_combos = [
            self.map_date,
            self.map_desc,
            self.map_amount,
            self.map_amount_debit,
            self.map_amount_credit,
            self.map_category,
            self.map_account,
            self.map_method,
            self.map_merchant,
            self.map_reference,
        ]
        for combo in mapping_combos:
            combo.blockSignals(True)
        try:
            for combo in mapping_combos:
                combo.clear()
                combo.addItems(opts)
            suggested = self.import_service.suggested_mapping(self.headers)
            combos = {
                "date": self.map_date,
                "desc": self.map_desc,
                "amount": self.map_amount,
                "amount_debit": self.map_amount_debit,
                "amount_credit": self.map_amount_credit,
                "category": self.map_category,
                "account": self.map_account,
                "method": self.map_method,
                "merchant": self.map_merchant,
                "reference": self.map_reference,
            }
            for key, combo in combos.items():
                value = suggested.get(key, "")
                if value:
                    combo.setCurrentText(value)
            self.load_import_mapping()
        finally:
            for combo in mapping_combos:
                combo.blockSignals(False)
        self.populate_preview()

    def import_mapping(self) -> dict[str, str]:
        return {
            "date": self.map_date.currentText(),
            "desc": self.map_desc.currentText(),
            "amount": self.map_amount.currentText(),
            "amount_debit": self.map_amount_debit.currentText(),
            "amount_credit": self.map_amount_credit.currentText(),
            "category": self.map_category.currentText(),
            "account": self.map_account.currentText(),
            "method": self.map_method.currentText(),
            "merchant": self.map_merchant.currentText(),
            "reference": self.map_reference.currentText(),
        }

    def save_import_mapping(self) -> None:
        try:
            self.import_service.save_mapping(self.import_mapping())
        except Exception as exc:
            log_exception("Failed to save smart import mapping", exc)

    def load_import_mapping(self) -> None:
        mapping = self.import_service.load_mapping()
        combos = {
            "date": self.map_date,
            "desc": self.map_desc,
            "amount": self.map_amount,
            "amount_debit": self.map_amount_debit,
            "amount_credit": self.map_amount_credit,
            "category": self.map_category,
            "account": self.map_account,
            "method": self.map_method,
            "merchant": self.map_merchant,
            "reference": self.map_reference,
        }
        for key, combo in combos.items():
            value = str(mapping.get(key, ""))
            if value and combo.findText(value) >= 0:
                combo.setCurrentText(value)

    def populate_preview(self):
        if not self.rows:
            self._review_batch = None
            self.review_btn.setEnabled(False)
            self.preview.setRowCount(0)
            self.import_btn.setEnabled(False)
            self.import_status.setText(
                "Abre um extrato CSV, OFX ou QIF para pré-visualizar, validar duplicados e importar."
            )
            return
        try:
            self._populate_preview()
        except Exception as exc:
            log_exception("Failed to calculate smart import preview", exc)
            self.preview.setRowCount(0)
            self.import_btn.setEnabled(False)
            self.review_btn.setEnabled(False)
            self.import_status.setText(
                "Não foi possível calcular a pré-visualização. "
                "Revê o mapeamento ou abre novamente o ficheiro. "
                + ("Detalhes ocultos em modo privado." if self.app.theme.privacy
                   else html_text(str(exc)))
            )

    def _review_options(self):
        return dict(
            apply_rules=self.apply_rules.isChecked(),
            amount_locale=str(self.amount_locale.currentData() or AMOUNT_LOCALE_AUTO),
            invert_signs=self.is_credit_card.isChecked(),
            date_format=str(self.date_format.currentData() or ""),
            source_sha256=self.source_sha256,
            ignore_duplicates=self.ignore_dup.isChecked(),
        )

    def review_rows(self):
        if self._review_batch is None:
            return
        from ui.import_review import ImportReviewDialog

        dialog = ImportReviewDialog(self._review_batch, self, privacy=self.app.theme.privacy)
        self._review_dialog = dialog
        try:
            if dialog.exec():
                self._review_batch = dialog.review
                self.populate_preview()
        finally:
            self._review_dialog = None
            dialog.deleteLater()

    def update_export_privacy_labels(self):
        super().update_export_privacy_labels()
        dialog = getattr(self, "_review_dialog", None)
        if dialog is not None:
            dialog.sync_privacy()
        if getattr(self, "rows", None):
            if self.app.theme.privacy:
                QToolTip.hideText()
                # Mask immediately; rebuilding a large reviewed batch must not
                # leave the previous public cells visible during normalization.
                for row in range(self.preview.rowCount()):
                    for column in range(self.preview.columnCount()):
                        item = self.preview.item(row, column)
                        if item is not None:
                            if column in (1, 3, 4, 6):
                                item.setText("Dados ocultos")
                            elif column == 2:
                                item.setText("••••• €")
                            item.setToolTip("Dados ocultos")
                self.import_btn.setEnabled(False)
                self.import_status.setText("Pré-visualização em modo privado. Desativa a privacidade para confirmar o lote.")
            else:
                self.populate_preview()

    def _populate_preview(self):
        # The app's service declares this API. Legacy injected preview providers
        # can continue rendering without a fabricated MagicMock review object.
        reviewer = getattr(type(self.import_service), "review_batch", None)
        if callable(reviewer):
            fresh = self.import_service.review_batch(self.rows, self.import_mapping(), **self._review_options())
            previous = self._review_batch
            if previous is None or any(getattr(previous, key) != getattr(fresh, key) for key in (
                "source_rows_json", "mapping_json", "options_json", "rules_fingerprint",
                "catalogue_fingerprint", "snapshot_fingerprint",
            )):
                self._review_batch = fresh
            result = self.import_service.review_preview(self._review_batch, limit=80)
            self.review_btn.setEnabled(True)
        else:
            self._review_batch = None
            self.review_btn.setEnabled(False)
            result = self.import_service.preview(
                self.rows,
                self.import_mapping(),
                apply_rules=self.apply_rules.isChecked(),
                amount_locale=str(self.amount_locale.currentData() or AMOUNT_LOCALE_AUTO),
                limit=80,
                invert_signs=self.is_credit_card.isChecked(),
                date_format=str(self.date_format.currentData() or ""),
            )
        self.preview.setRowCount(len(result.rows))
        for i, preview_row in enumerate(result.rows):
            transaction = preview_row.transaction
            status = preview_row.status
            vals = [
                transaction.date,
                privacy_export_cell(transaction.description, "description", self.app.theme.privacy),
                eur(transaction.amount, self.app.theme.privacy),
                privacy_export_cell(transaction.category, "category", self.app.theme.privacy, mask_categories=True),
                privacy_export_cell(transaction.account, "account", self.app.theme.privacy),
                status,
                privacy_export_cell(preview_row.reason, "notes", self.app.theme.privacy),
            ]
            for c, v in enumerate(vals):
                item = QTableWidgetItem(str(v))
                if c == 2:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                    item.setForeground(
                        QColor(GREEN if transaction.amount >= 0 else RED)
                    )
                if c in (5, 6):
                    item.setForeground(
                        QColor(
                            RED
                            if status in {"Rever", "Duplicado certo"}
                            else ORANGE
                            if status == "Duplicado provável"
                            else "#B7791F"
                            if status == "Possível duplicado"
                            else GREEN
                        )
                    )
                item.setToolTip(
                    "Dados ocultos" if self.app.theme.privacy else
                    html_text("Originais: " + " · ".join(f"{key}: {value}" for key, value in transaction.source_values))
                )
                self.preview.setItem(i, c, item)
        bank_badge = (
            f"🏦 <b>{html_text(str(self.detected_bank))}</b> · "
            if getattr(self, "detected_bank", None)
            else ""
        )
        summary = (
            f"{bank_badge}<b>{result.total}</b> linha(s) validadas · a apresentar {len(result.rows)} "
            f"· certos {result.certain_duplicates} · prováveis {result.probable_duplicates} "
            f"· possíveis {result.possible_duplicates} · a rever {result.invalid}"
        )
        context_line = f"<br>Ficheiro: {html_text(self.csv_encoding)}" if self.csv_encoding else ""

        warning_line = (
            "<br>⚠️ " + " · ".join(html_text(str(warning)) for warning in self.csv_warnings) if self.csv_warnings else ""
        )
        repeated_file = self.import_service.file_history_warning(self.source_sha256, [entry.transaction.account for entry in result.rows])
        if repeated_file:
            warning_line += "<br>⚠️ " + html_text(repeated_file)
        self.import_btn.setEnabled(not result.ambiguous_dates)
        if self._review_batch is not None:
            pending = self._review_batch.pending_count
            summary += (
                f"<br>Revisão do lote completo: {pending} sugestão(ões) pendente(s) "
                f"· {self._review_batch.excluded_count} linha(s) excluída(s). "
                "Abre a revisão para aceitar, rejeitar ou excluir; nenhuma regra nova será criada."
            )
            self.import_btn.setEnabled(
                not result.ambiguous_dates and not pending and result.total > result.invalid
            )
        if self.app.theme.privacy:
            self.import_btn.setEnabled(False)
            summary = "Pré-visualização em modo privado. Os dados originais mantêm-se disponíveis ao desativar a privacidade."
            context_line = ""
            warning_line = ""
        self.import_status.setText(summary + context_line + warning_line)

    def import_rows(self):
        if not self.rows:
            return
        if self.app.theme.privacy:
            self.import_btn.setEnabled(False)
            self.import_status.setText("Desativa o modo privado para confirmar a importação do lote revisto.")
            return
        try:
            if self._review_batch is not None:
                if self.source_path is not None and hashlib.sha256(self.source_path.read_bytes()).hexdigest() != self.source_sha256:
                    raise ValueError("O ficheiro mudou desde a pré-visualização. Abre novamente o extrato e revê o lote.")
                result = self.import_service.import_reviewed_batch(
                    self._review_batch, rows=self.rows, mapping=self.import_mapping(),
                    **self._review_options(),
                )
            elif callable(getattr(type(self.import_service), "review_batch", None)):
                raise ValueError("Atualiza a pré-visualização e revê o lote antes de importar.")
            else:
                result = self.import_service.import_rows(
                    self.rows,
                    self.import_mapping(),
                    apply_rules=self.apply_rules.isChecked(),
                    ignore_duplicates=self.ignore_dup.isChecked(),
                    amount_locale=str(
                        self.amount_locale.currentData() or AMOUNT_LOCALE_AUTO
                    ),
                    invert_signs=self.is_credit_card.isChecked(),
                    date_format=str(self.date_format.currentData() or ""),
                    source_sha256=self.source_sha256,
                )
        except Exception as exc:
            detail = "Detalhes ocultos em modo privado." if self.app.theme.privacy else str(exc)
            QMessageBox.critical(
                self,
                APP_TITLE,
                "<p>A importação foi cancelada e nenhuma linha foi guardada.</p><p>"
                + html_text(detail) + "</p>",
            )
            return
        self.save_import_mapping()
        self._review_batch = None
        self.review_btn.setEnabled(False)
        self.import_btn.setEnabled(False)
        self.import_status.setText(
            f"Importação concluída. Criados: {result.created}. "
            f"Ignorados: {result.skipped}."
        )
        QMessageBox.information(
            self,
            APP_TITLE,
            f"Importação concluída.\nCriados: {result.created}\n"
            f"Ignorados: {result.skipped}",
        )
        self.app.notify_data_changed()


class SimulatorPage(BasePage):
    def __init__(self, app: "LiquidFinanceApp"):
        super().__init__(
            app,
            "Simulador de decisões",
            "Testa o impacto de cortar gastos, cancelar serviços ou comprar algo grande",
        )
        form_card = GlassCard("Cenário", strong=True)
        form = QGridLayout()
        form_card.v.addLayout(form)
        self.income = EuroSpinBox()
        self.income.setRange(0, 1_000_000)
        self.income.setSuffix(" €")
        self.income.setDecimals(2)
        self.expenses = EuroSpinBox()
        self.expenses.setRange(0, 1_000_000)
        self.expenses.setSuffix(" €")
        self.expenses.setDecimals(2)
        self.cut = EuroSpinBox()
        self.cut.setRange(0, 100_000)
        self.cut.setSuffix(" € / mês")
        self.cut.setDecimals(2)
        self.extra_income = EuroSpinBox()
        self.extra_income.setRange(0, 100_000)
        self.extra_income.setSuffix(" € / mês")
        self.extra_income.setDecimals(2)
        self.extra_invest = EuroSpinBox()
        self.extra_invest.setRange(0, 100_000)
        self.extra_invest.setSuffix(" € / mês")
        self.extra_invest.setDecimals(2)
        self.purchase = EuroSpinBox()
        self.purchase.setRange(0, 1_000_000)
        self.purchase.setSuffix(" €")
        self.purchase.setDecimals(2)
        widgets = [
            ("Rendimento mensal", self.income),
            ("Gastos mensais", self.expenses),
            ("Cortar gastos", self.cut),
            ("Rendimento extra", self.extra_income),
            ("Investir extra", self.extra_invest),
            ("Compra única", self.purchase),
        ]
        for i, (label, w) in enumerate(widgets):
            form.addWidget(QLabel(label), i // 2 * 2, i % 2)
            form.addWidget(w, i // 2 * 2 + 1, i % 2)
        self.result = QTextEdit()
        self.result.setReadOnly(True)
        self.result.setMinimumHeight(360)
        result_card = GlassCard("Impacto estimado", strong=True)
        result_card.v.addWidget(self.result)
        self.body.addWidget(form_card)
        self.body.addWidget(result_card)
        for w in [
            self.income,
            self.expenses,
            self.cut,
            self.extra_income,
            self.extra_invest,
            self.purchase,
        ]:
            w.valueChanged.connect(self.refresh)
        self.prefill()
        self.refresh()

    def prefill(self):
        today = date.today()
        s = today.replace(day=1).isoformat()
        e = today.isoformat()
        totals = self.analytics_service.totals_for_period(s, e)
        income = safe_float(totals["income"])
        expenses = safe_float(totals["expenses"])
        # BUG-TOOL-01: on day 1 of a month the running month is empty and would
        # prefill 0 € everywhere. Fall back to the 3-month average so the
        # simulator starts from a realistic scenario.
        if income <= 0.0 and expenses <= 0.0:
            average_income = 0.0
            average_expenses = 0.0
            for offset in _FALLBACK_MONTH_OFFSETS:
                first = add_months(today.replace(day=1), -offset, 1)
                next_month_first = (
                    date(first.year + 1, 1, 1)
                    if first.month == 12
                    else first.replace(month=first.month + 1)
                )
                month_end = next_month_first - timedelta(days=1)
                month_totals = self.analytics_service.totals_for_period(
                    first.isoformat(),
                    month_end.isoformat(),
                )
                average_income += safe_float(month_totals["income"])
                average_expenses += safe_float(month_totals["expenses"])
            income = average_income / 3.0
            expenses = average_expenses / 3.0
        self.income.setValue(round(income, 2))
        self.expenses.setValue(round(expenses, 2))

    def refresh(self):
        income = self.income.value() + self.extra_income.value()
        expenses = max(0, self.expenses.value() - self.cut.value())
        monthly = income - expenses - self.extra_invest.value()
        annual = monthly * 12 - self.purchase.value()
        before = self.income.value() - self.expenses.value()
        gain = (income - expenses) - before
        lines = [
            "RESULTADO DO CENÁRIO",
            "",
            f"Poupança mensal atual estimada: {eur(before, self.app.theme.privacy)}",
            f"Poupança mensal no cenário: {eur(monthly, self.app.theme.privacy)}",
            f"Melhoria mensal antes de investimento extra: {eur(gain, self.app.theme.privacy)}",
            f"Investimento extra mensal: {eur(self.extra_invest.value(), self.app.theme.privacy)}",
            f"Impacto anual depois da compra única: {eur(annual, self.app.theme.privacy)}",
            "",
        ]
        if annual > 0:
            lines.append(
                "Conclusão: o cenário aumenta a folga anual e pode acelerar objetivos."
            )
        else:
            lines.append(
                "Conclusão: o cenário cria pressão no saldo anual; revê compra única, gastos ou rendimento."
            )
        self.result.setPlainText("\n".join(lines))


class CompoundInterestPage(BasePage):
    def __init__(self, app: "LiquidFinanceApp"):
        super().__init__(
            app,
            "Juros Compostos",
            "Calculadora para simular o crescimento de investimentos a longo prazo.",
        )
        form_card = GlassCard("Parâmetros do Investimento", strong=True)
        form = QGridLayout()
        form_card.v.addLayout(form)

        self.initial = EuroSpinBox()
        self.initial.setRange(0, 10_000_000)
        self.initial.setSuffix(" €")
        self.initial.setValue(1000)

        self.monthly = EuroSpinBox()
        self.monthly.setRange(0, 1_000_000)
        self.monthly.setSuffix(" €")
        self.monthly.setValue(100)

        self.rate = QDoubleSpinBox()
        self.rate.setRange(0, 100)
        self.rate.setSuffix(" %")
        self.rate.setDecimals(2)
        self.rate.setValue(7.0)

        self.years = QDoubleSpinBox()
        self.years.setRange(1, 100)
        self.years.setSuffix(" anos")
        self.years.setDecimals(0)
        self.years.setValue(10)

        widgets = [
            ("Capital Inicial", self.initial),
            ("Reforço Mensal", self.monthly),
            ("Taxa de Juro Anual", self.rate),
            ("Prazo", self.years),
        ]
        for i, (label, w) in enumerate(widgets):
            form.addWidget(QLabel(label), i // 2 * 2, i % 2)
            form.addWidget(w, i // 2 * 2 + 1, i % 2)
            w.valueChanged.connect(self.calculate)

        self.result = QTextEdit()
        self.result.setReadOnly(True)
        self.result.setMinimumHeight(250)
        result_card = GlassCard("Projeção", strong=True)
        result_card.v.addWidget(self.result)

        self.body.addWidget(form_card)
        self.body.addWidget(result_card)
        self.calculate()

    def calculate(self):
        p = self.initial.value()
        pmt = self.monthly.value()
        r = self.rate.value() / 100.0
        t = int(self.years.value())

        # Cálculo composto mensal
        n = 12
        r_periodic = r / n
        growth = (1.0 + r_periodic) ** (n * t)
        total = p * growth

        # Valor futuro de uma série (PMT)
        if abs(r_periodic) > 1e-12:
            total += pmt * ((growth - 1.0) / r_periodic)
        else:
            total += pmt * n * t

        invested = p + (pmt * 12 * t)
        interest = total - invested

        lines = [
            "RESULTADOS DA SIMULAÇÃO",
            "",
            f"Capital Inicial: {eur(p, self.app.theme.privacy)}",
            f"Total de Reforços: {eur(pmt * 12 * t, self.app.theme.privacy)}",
            f"Total Investido: {eur(invested, self.app.theme.privacy)}",
            f"Juros Ganhos: {eur(interest, self.app.theme.privacy)}",
            "",
            f"VALOR FINAL ESTIMADO: {eur(total, self.app.theme.privacy)}",
        ]
        self.result.setPlainText("\n".join(lines))
