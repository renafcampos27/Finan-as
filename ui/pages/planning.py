#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Finanças Pessoais – Liquid Glass
Nova interface PySide6 para a app local de finanças pessoais.
Mantém a base de dados e a app clássica intactas; esta é apenas uma camada visual moderna.
"""

from __future__ import annotations

import csv
import math
import sys
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from utils.qt_runtime import configure_qt_runtime
from utils.output_security import privacy_export_row

configure_qt_runtime()

try:
    from PySide6.QtCore import QTimer, Qt
    from PySide6.QtGui import QColor
    from PySide6.QtWidgets import (
        QAbstractItemView,
        QComboBox,
        QDialog,
        QFileDialog,
        QGridLayout,
        QHBoxLayout,
        QHeaderView,
        QInputDialog,
        QLabel,
        QMessageBox,
        QProgressBar,
        QPushButton,
        QSizePolicy,
        QTableWidget,
    )
except (
    Exception
) as exc:  # pragma: no cover - only used when PySide6 is missing on the user's PC.
    print("PySide6 não está instalado. Instala com: pip install PySide6")
    print(exc)
    sys.exit(1)

from ui.components import BudgetForecastChart, GlassCard, MetricCard
from ui.chart_detail_support import movement_detail_payload
from ui.core import BasePage
from ui.core.smart_table import SmartTableWidgetItem as QTableWidgetItem
from ui.dialogs import (
    BudgetAssistantDialog,
    BudgetEditDialog,
    GoalDialog,
    MonthlyGoalDialog,
    ScheduledPaymentDialog,
)
from utils.app_logging import log_exception
from utils.config import parse_date
from utils.helpers import (
    APP_TITLE,
    BLUE,
    GREEN,
    MUTED,
    ORANGE,
    PINK,
    PT_MONTHS,
    PURPLE,
    RED,
    YELLOW,
    add_months,
    eur,
    percent,
    row_get,
    safe_float,
    safe_year_from_combo,
    to_qdate,
)

if TYPE_CHECKING:
    from main import LiquidFinanceApp


class BudgetsPage(BasePage):
    TABLE_ROW_HEIGHT = 38

    @staticmethod
    def _month_options(reference: Optional[date] = None, count: int = 18) -> list[date]:
        current = (reference or date.today()).replace(day=1)
        return [add_months(current, -index, 1) for index in range(max(1, count))]

    @classmethod
    def _fit_table_to_rows(
        cls,
        table: QTableWidget,
        row_count: int,
        *,
        minimum_rows: int,
    ) -> int:
        """Give the table enough height to expose every populated row."""
        visible_rows = max(int(minimum_rows), max(0, int(row_count)))
        table.verticalHeader().setDefaultSectionSize(cls.TABLE_ROW_HEIGHT)
        for row in range(max(0, int(row_count))):
            table.setRowHeight(row, cls.TABLE_ROW_HEIGHT)

        header_height = max(30, table.horizontalHeader().sizeHint().height())
        scrollbar_height = max(16, table.horizontalScrollBar().sizeHint().height())
        target = (
            header_height
            + visible_rows * cls.TABLE_ROW_HEIGHT
            + scrollbar_height
            + table.frameWidth() * 2
            + 8
        )
        table.setMinimumHeight(target)
        table.setMaximumHeight(target)
        table.updateGeometry()
        return target

    def _sync_budget_card_heights(self) -> None:
        """Resize the three category-heavy cards after their data is populated."""
        self._fit_table_to_rows(
            self.table,
            self.table.rowCount(),
            minimum_rows=10,
        )
        self._fit_table_to_rows(
            self.smart_budget_table,
            self.smart_budget_table.rowCount(),
            minimum_rows=7,
        )

        for card, baseline in (
            (self.forecast_card, 420),
            (self.smart_budget_card, 430),
            (self.execution_card, 500),
        ):
            card.v.activate()
            card.setMinimumHeight(max(baseline, card.v.sizeHint().height()))
            card.updateGeometry()

        sync_content_size = getattr(self.app, "sync_content_size", None)
        if callable(sync_content_size):
            QTimer.singleShot(0, sync_content_size)
            QTimer.singleShot(80, sync_content_size)

    def _apply_smart_budget_default_widths(self) -> None:
        """Install a balanced first layout without overriding later user changes."""
        table = self.smart_budget_table
        header = table.horizontalHeader()
        base_widths = [170, 120, 125, 110, 120, 130, 105]
        available = max(1, table.viewport().width())
        widths = list(base_widths)
        extra = max(0, available - sum(base_widths))
        weights = [0.25, 0.13, 0.14, 0.12, 0.14, 0.13, 0.09]
        if extra:
            widths = [
                base + int(round(extra * weight))
                for base, weight in zip(base_widths, weights)
            ]
            widths[-1] += available - sum(widths)

        restoring = bool(table.property("columnPersistenceRestoring"))
        table.setProperty("columnPersistenceRestoring", True)
        try:
            for column, width in enumerate(widths):
                header.resizeSection(column, max(80, int(width)))
            table.horizontalScrollBar().setValue(0)
        finally:
            table.setProperty("columnPersistenceRestoring", restoring)
        table.viewport().update()

    def _migrate_smart_budget_columns_v303(self) -> None:
        """Replace the malformed legacy widths once, then resume persistence."""
        migration_key = "smart_budget_columns_v303"
        if str(self.repo.setting(migration_key, "0") or "0") == "1":
            return
        if not bool(self.smart_budget_table.property("columnPersistenceRegistered")):
            QTimer.singleShot(25, self._migrate_smart_budget_columns_v303)
            return

        state_key = str(self.smart_budget_table.property("columnPersistenceKey") or "")
        if state_key:
            self.repo.set_setting(state_key, "{}")
        self._apply_smart_budget_default_widths()
        manager = getattr(self.app, "table_column_manager", None)
        if manager is not None:
            manager.save_table(self.smart_budget_table)
        self.repo.set_setting(migration_key, "1")

    def __init__(self, app: "LiquidFinanceApp"):
        super().__init__(
            app,
            "Orçamentos",
            "Limites mensais, progresso por categoria e estado do mês",
        )
        top = QHBoxLayout()
        self.month = QComboBox()
        for d in self._month_options():
            self.month.addItem(
                f"{PT_MONTHS[d.month - 1].capitalize()} {d.year}", d.strftime("%Y-%m")
            )
        self.edit_btn = QPushButton("Editar orçamento")
        self.edit_btn.setObjectName("BudgetEditButton")
        self.assistant_btn = QPushButton("Assistente Inteligente")
        self.assistant_btn.setObjectName("Primary")
        self.export_btn = QPushButton("Exportar CSV")
        top.addWidget(QLabel("Mês"))
        top.addWidget(self.month)
        top.addStretch()
        top.addWidget(self.assistant_btn)
        top.addWidget(self.edit_btn)
        top.addWidget(self.export_btn)
        self.summary = QGridLayout()
        self.summary.setSpacing(14)
        self.table = QTableWidget(0, 8)
        self.table.setProperty("columnPersistenceId", "budgets.list")
        self.table.setHorizontalHeaderLabels(
            [
                "Categoria",
                "Gasto",
                "Orçamento",
                "Projeção fim do mês",
                "Margem prevista",
                "% usado",
                "Mês anterior",
                "Estado",
            ]
        )
        budget_header = self.table.horizontalHeader()
        budget_header.setStretchLastSection(False)
        budget_header.setSectionResizeMode(0, QHeaderView.Interactive)
        budget_header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        budget_header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        budget_header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        budget_header.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        budget_header.setSectionResizeMode(5, QHeaderView.ResizeToContents)
        budget_header.setSectionResizeMode(6, QHeaderView.ResizeToContents)
        budget_header.setSectionResizeMode(7, QHeaderView.Stretch)
        self.table.setColumnWidth(0, 170)
        self.table.setColumnWidth(7, 170)
        self.table.setWordWrap(False)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.verticalHeader().setVisible(False)
        self.table.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.execution_card = GlassCard("Execução dos orçamentos", strong=True)
        self.table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.execution_card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        self.execution_card.setMinimumHeight(500)
        self.execution_card.v.addWidget(self.table)

        self.forecast_chart = BudgetForecastChart(
            lambda: self.app.theme,
            row_height=48,
        )
        self.forecast_chart.configure_details(
            self._forecast_detail_payload,
            action_label="Selecionar categoria",
        )
        self.forecast_chart.category_clicked.connect(self.select_category)
        self.forecast_card = GlassCard("Previsão visual por categoria", strong=True)
        self.forecast_card.set_compact_header()
        self.forecast_card.setMinimumHeight(420)
        self.forecast_hint = QLabel()
        self.forecast_hint.setObjectName("Subtle")
        self.forecast_hint.setWordWrap(True)
        self.forecast_card.fit_label_height(self.forecast_hint, word_wrap=True)
        self.forecast_card.v.addWidget(self.forecast_hint)
        self.forecast_card.v.addWidget(self.forecast_chart)

        self.smart_budget_table = QTableWidget(0, 7)
        self.smart_budget_table.setProperty(
            "columnPersistenceId", "budgets.smart_forecast"
        )
        self.smart_budget_table.setObjectName("SmartBudgetTable")
        self.smart_budget_table.setHorizontalHeaderLabels(
            [
                "Categoria",
                "Saldo transitado",
                "Limite efetivo",
                "Projeção",
                "Sugestão",
                "Rutura prevista",
                "Estado",
            ]
        )
        smart_header = self.smart_budget_table.horizontalHeader()
        smart_header.setStretchLastSection(False)
        smart_header.setMinimumSectionSize(80)
        for column, width in enumerate([170, 120, 125, 110, 120, 130, 105]):
            smart_header.setSectionResizeMode(column, QHeaderView.Interactive)
            smart_header.resizeSection(column, width)
        self.smart_budget_table.setAlternatingRowColors(True)
        self.smart_budget_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.smart_budget_table.setHorizontalScrollMode(
            QAbstractItemView.ScrollPerPixel
        )
        self.smart_budget_table.verticalHeader().setVisible(False)
        self.smart_budget_table.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.smart_budget_table.setMinimumHeight(290)
        self.smart_budget_hint = QLabel()
        self.smart_budget_hint.setObjectName("Subtle")
        self.smart_budget_hint.setWordWrap(True)
        smart_actions = QHBoxLayout()
        self.apply_suggestion_btn = QPushButton("Aplicar sugestão selecionada")
        self.apply_all_suggestions_btn = QPushButton("Aplicar todas as sugestões")
        self.apply_all_suggestions_btn.setObjectName("Primary")
        smart_actions.addStretch(1)
        smart_actions.addWidget(self.apply_suggestion_btn)
        smart_actions.addWidget(self.apply_all_suggestions_btn)
        self.smart_budget_card = GlassCard(
            "🧠 Orçamentos inteligentes · saldo transitado e previsão de rutura",
            strong=True,
        )
        self.smart_budget_card.set_compact_header(word_wrap=True)
        self.smart_budget_card.setMinimumHeight(430)
        self.smart_budget_card.fit_label_height(
            self.smart_budget_hint,
            word_wrap=True,
        )
        self.smart_budget_card.v.addWidget(self.smart_budget_hint)
        self.smart_budget_card.v.addLayout(smart_actions)
        self.smart_budget_card.v.addWidget(self.smart_budget_table)
        self.body.addLayout(top)
        self.body.addLayout(self.summary)
        self.body.addWidget(self.forecast_card)
        self.body.addWidget(self.smart_budget_card)
        self.body.addWidget(self.execution_card)
        self.month.currentIndexChanged.connect(self.refresh)
        self.edit_btn.clicked.connect(self.edit_selected)
        self.assistant_btn.clicked.connect(self.open_assistant)
        self.export_btn.clicked.connect(self.export_csv)
        self.apply_suggestion_btn.clicked.connect(self.apply_selected_suggestion)
        self.apply_all_suggestions_btn.clicked.connect(self.apply_all_suggestions)
        self.refresh()
        QTimer.singleShot(0, self._migrate_smart_budget_columns_v303)

    def open_assistant(self) -> None:
        key = self.month.currentData() or date.today().strftime("%Y-%m")
        dlg = BudgetAssistantDialog(self.repo, key, self)
        if dlg.exec():
            plan = dlg.get_plan()
            for cat, val in plan:
                self.repo.set_category_budget(cat, val, key)
            self.app.notify_data_changed(
                "Orçamentos gerados pelo assistente aplicados."
            )
            self.refresh()

    def _forecast(self) -> dict[str, Any]:
        key = self.month.currentData() or date.today().strftime("%Y-%m")
        return self.analytics_service.monthly_budget_forecast(key, date.today())

    def _forecast_detail_payload(self, item: dict[str, Any]) -> dict[str, Any]:
        key = str(self.month.currentData() or date.today().strftime("%Y-%m"))
        try:
            year, month = (int(part) for part in key.split("-", 1))
            start, end, _ym = self.repo.month_period(year, month)
        except (TypeError, ValueError):
            start = end = date.today().isoformat()
        category = str(item.get("category") or item.get("label") or "")
        return movement_detail_payload(
            self.repo,
            self.app.theme,
            title="Previsão do orçamento por categoria",
            item=item,
            start=start,
            end=end,
            category=category,
            kind="Despesas",
            subtitle=category,
            explanation=(
                "Compara o valor já realizado, a previsão até ao fim do mês "
                "e o orçamento definido. Os movimentos abaixo explicam o realizado."
            ),
            rows_title="Movimentos já realizados nesta categoria",
        )

    def _rows(self) -> list[dict[str, Any]]:
        return self._forecast().get("rows", [])

    def _smart_budget(self) -> dict[str, Any]:
        key = self.month.currentData() or date.today().strftime("%Y-%m")
        return self.analytics_service.smart_budget_analysis(key, date.today())

    def clear_summary(self) -> None:
        while self.summary.count():
            item = self.summary.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def refresh(self) -> None:
        forecast = self._forecast()
        coverage = dict(forecast.get("data_coverage") or {})
        coverage_text = self.data_coverage_text(coverage)
        coverage_tone = (
            "positive"
            if int(coverage.get("active_months", 0) or 0) >= 6
            and not int(coverage.get("invalid_transactions_excluded", 0) or 0)
            else "warning"
        )
        self.forecast_card.set_data_coverage(coverage_text, coverage_tone)
        self.smart_budget_card.set_data_coverage(coverage_text, coverage_tone)
        rows = forecast.get("rows", []) or []
        total_budget = safe_float(forecast.get("total_budget"))
        total_spent = safe_float(forecast.get("total_spent"))
        total_projected = safe_float(forecast.get("total_projected"))
        projected_margin = safe_float(forecast.get("projected_margin"))
        coverage = safe_float(forecast.get("coverage_pct"))
        overall_tone = str(forecast.get("overall_tone") or "neutral")
        actual_utilisation = total_spent / total_budget if total_budget else 0.0
        projected_utilisation = total_projected / total_budget if total_budget else 0.0
        critical_count = int(forecast.get("over_count", 0) or 0) + int(
            forecast.get("risk_count", 0) or 0
        )
        self.clear_summary()
        data = [
            (
                (
                    "Orçamento total",
                    eur(total_budget, self.app.theme.privacy),
                    str(forecast.get("month_label") or "mês selecionado"),
                    BLUE,
                    "🎯",
                ),
                {
                    "status": (
                        f"{percent(coverage)} do gasto coberto"
                        if total_budget
                        else "Sem limites definidos"
                    ),
                    "tone": (
                        "positive"
                        if total_budget and coverage >= 90
                        else "warning"
                        if total_budget
                        else "neutral"
                    ),
                    "progress": coverage / 100 if total_budget else None,
                },
            ),
            (
                (
                    "Realizado",
                    eur(total_spent, self.app.theme.privacy),
                    f"dia {forecast.get('days_elapsed', 0)} de {forecast.get('days_in_month', 0)}",
                    PINK,
                    "📍",
                ),
                {
                    "status": f"{percent(actual_utilisation * 100)} do orçamento",
                    "tone": (
                        "negative"
                        if actual_utilisation > 1
                        else "warning"
                        if actual_utilisation > 0.8
                        else "positive"
                    ),
                    "progress": actual_utilisation if total_budget else None,
                },
            ),
            (
                (
                    "Previsão fim do mês",
                    eur(total_projected, self.app.theme.privacy),
                    "ritmo atual + histórico",
                    ORANGE,
                    "🔮",
                ),
                {
                    "status": str(forecast.get("overall_status") or "Sem estado"),
                    "tone": overall_tone,
                    "progress": projected_utilisation if total_budget else None,
                },
            ),
            (
                (
                    "Margem prevista",
                    eur(projected_margin, self.app.theme.privacy),
                    f"{critical_count} categoria(s) crítica(s)",
                    GREEN if projected_margin >= 0 else RED,
                    "🛟" if projected_margin >= 0 else "🚨",
                ),
                {
                    "status": (
                        "Dentro do orçamento"
                        if projected_margin >= 0
                        else "Défice previsto"
                    ),
                    "tone": "positive" if projected_margin >= 0 else "negative",
                    "progress": (
                        max(0.0, min(1.0, projected_margin / total_budget))
                        if total_budget and projected_margin >= 0
                        else 0.0
                        if total_budget
                        else None
                    ),
                },
            ),
        ]
        for i, (args, kwargs) in enumerate(data):
            self.summary.addWidget(MetricCard(*args, **kwargs), 0, i)

        self.forecast_chart.set_rows(
            rows, str(forecast.get("month_label") or "Mês selecionado")
        )
        hint = str(forecast.get("method_label") or "")
        unbudgeted = safe_float(forecast.get("unbudgeted_spend"))
        if unbudgeted > 0:
            hint += (
                f" ⚠️ {eur(unbudgeted, self.app.theme.privacy)} de despesas "
                "ainda não têm orçamento definido."
            )
        self.forecast_hint.setText(hint.strip())

        self.table.clearContents()
        self.table.setRowCount(len(rows))
        for row, r in enumerate(rows):
            vals = [
                r.get("category"),
                eur(r.get("spent", 0), self.app.theme.privacy),
                eur(r.get("budget", 0), self.app.theme.privacy),
                eur(r.get("projected", 0), self.app.theme.privacy),
                eur(r.get("projected_margin", 0), self.app.theme.privacy),
                percent(r.get("utilisation", 0)),
                eur(r.get("previous_spend", 0), self.app.theme.privacy),
                r.get("forecast_status"),
            ]
            for col, val in enumerate(vals):
                item = QTableWidgetItem(str(val) if col != 5 else "")
                if col == 5:
                    item.set_sort_value(safe_float(r.get("utilisation", 0)))
                if col in (1, 2, 3, 4, 5, 6):
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                if col == 7:
                    st = str(val)
                    item.setForeground(
                        QColor(
                            GREEN
                            if st == "Controlado"
                            else (
                                ORANGE
                                if st in {"Atenção", "Em risco"}
                                else (
                                    RED
                                    if st == "Ultrapassado"
                                    else self.app.theme.muted
                                )
                            )
                        )
                    )
                self.table.setItem(row, col, item)

            pb = QProgressBar()
            pb.setRange(0, 100)
            util = safe_float(r.get("utilisation", 0))
            pb.setValue(min(100, int(util)))
            pb.setFormat(f"{util:.1f}%")
            pb.setTextVisible(True)
            status = str(r.get("forecast_status") or "")
            color = (
                GREEN
                if status == "Controlado"
                else YELLOW
                if status == "Atenção"
                else ORANGE
                if status == "Em risco"
                else RED
            )
            pb.setStyleSheet(
                f"QProgressBar {{ margin: 2px; border: 1px solid {self.app.theme.border}; border-radius: 4px; text-align: center; color: {self.app.theme.text}; background-color: transparent; }} QProgressBar::chunk {{ background-color: {color}; border-radius: 3px; }}"
            )
            self.table.setCellWidget(row, 5, pb)
        if self.should_compute_card(self.smart_budget_card):
            self._refresh_smart_budget()
        self._sync_budget_card_heights()

    def _refresh_smart_budget(self) -> None:
        analysis = self._smart_budget()
        self._smart_budget_data = analysis
        rows = list(analysis.get("rows") or [])
        self.smart_budget_hint.setText(
            f"{analysis.get('method_label', '')} "
            f"Saldo transitado disponível: {eur(analysis.get('total_rollover', 0), self.app.theme.privacy)} · "
            f"{int(analysis.get('risk_count', 0) or 0)} categoria(s) em risco."
        )
        self.smart_budget_table.clearContents()
        self.smart_budget_table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            breach = str(row.get("breach_date") or "")
            if breach:
                breach = parse_date(breach).strftime("%d/%m/%Y")
            values = [
                row.get("category"),
                eur(row.get("rollover", 0), self.app.theme.privacy),
                eur(row.get("effective_budget", 0), self.app.theme.privacy),
                eur(row.get("projected", 0), self.app.theme.privacy),
                eur(row.get("suggested_budget", 0), self.app.theme.privacy),
                breach or "—",
                row.get("status"),
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if column in (1, 2, 3, 4):
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                if column == 6:
                    tone = str(row.get("tone") or "neutral")
                    item.setForeground(
                        QColor(
                            RED
                            if tone == "negative"
                            else ORANGE
                            if tone == "warning"
                            else GREEN
                            if tone == "positive"
                            else self.app.theme.muted
                        )
                    )
                self.smart_budget_table.setItem(row_index, column, item)

    def _smart_row_by_category(self, category: str) -> Optional[dict[str, Any]]:
        for row in (getattr(self, "_smart_budget_data", {}) or {}).get("rows", []):
            if str(row.get("category")) == category:
                return row
        return None

    def apply_selected_suggestion(self) -> None:
        selected = self.smart_budget_table.currentRow()
        if selected < 0:
            QMessageBox.information(self, APP_TITLE, "Seleciona uma categoria.")
            return
        category_item = self.smart_budget_table.item(selected, 0)
        smart_row = (
            self._smart_row_by_category(category_item.text())
            if category_item is not None
            else None
        )
        if not smart_row:
            return
        self.repo.set_category_budget(
            str(smart_row["category"]),
            safe_float(smart_row["suggested_budget"]),
            self.month.currentData() or date.today().strftime("%Y-%m"),
        )
        self.app.notify_data_changed(f"Sugestão aplicada a {smart_row['category']}.")

    def apply_all_suggestions(self) -> None:
        rows = list((getattr(self, "_smart_budget_data", {}) or {}).get("rows", []))
        applicable = [
            row for row in rows if safe_float(row.get("suggested_budget")) > 0
        ]
        if not applicable:
            QMessageBox.information(self, APP_TITLE, "Ainda não existem sugestões.")
            return
        if (
            QMessageBox.question(
                self,
                APP_TITLE,
                f"Aplicar {len(applicable)} sugestões de orçamento?",
            )
            != QMessageBox.Yes
        ):
            return
        for row in applicable:
            self.repo.set_category_budget(
                str(row["category"]),
                safe_float(row["suggested_budget"]),
                self.month.currentData() or date.today().strftime("%Y-%m"),
            )
        self.app.notify_data_changed("Sugestões de orçamento aplicadas.")

    def select_category(self, category: str) -> None:
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item is not None and item.text() == category:
                self.table.selectRow(row)
                self.table.scrollToItem(item, QAbstractItemView.PositionAtCenter)
                return

    def edit_selected(self) -> None:
        row = self.table.currentRow()
        if row < 0:
            QMessageBox.information(self, APP_TITLE, "Seleciona uma categoria.")
            return
        item = self.table.item(row, 0)
        if item is None:
            QMessageBox.information(
                self, APP_TITLE, "Seleciona uma categoria com orçamento válido."
            )
            return
        category = item.text()
        month_key = self.month.currentData() or date.today().strftime("%Y-%m")
        current = self.repo.category_budget(category, month_key)
        d = BudgetEditDialog(category, current, self.app)
        if d.exec() == QDialog.Accepted:
            data = d.get_data()
            self.repo.set_category_budget(category, data["budget"], month_key)
            self.app.notify_data_changed("Orçamento atualizado.")

    def export_csv(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Exportar orçamentos",
            str(Path.home() / "orcamentos.csv"),
            "CSV (*.csv)",
        )
        if not path:
            return
        try:
            rows = self._rows()
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f, delimiter=";")
                w.writerow(
                    [
                        "categoria",
                        "gasto",
                        "orcamento",
                        "projecao_fim_mes",
                        "margem_prevista",
                        "percentagem_usada",
                        "mes_anterior",
                        "estado_previsto",
                    ]
                )
                for r in rows:
                    w.writerow(
                        privacy_export_row(
                            [
                                r.get("category"),
                                r.get("spent"),
                                r.get("budget"),
                                r.get("projected"),
                                r.get("projected_margin"),
                                r.get("utilisation"),
                                r.get("previous_spend"),
                                r.get("forecast_status"),
                            ],
                            [
                                "category",
                                "spent",
                                "budget",
                                "forecast",
                                "remaining",
                                "percent",
                                "spent",
                                "status",
                            ],
                            self.app.theme.privacy,
                        )
                    )
            QMessageBox.information(self, APP_TITLE, f"Exportado para:\n{path}")
        except Exception as exc:
            log_exception(f"Falha ao exportar orçamentos para {path}", exc)
            QMessageBox.critical(self, APP_TITLE, f"Erro ao exportar CSV:\n{exc}")


class GoalsPage(BasePage):
    def __init__(self, app: "LiquidFinanceApp"):
        super().__init__(
            app,
            "Objetivos",
            "Lista compacta com percentagem, previsão, estado e ações rápidas",
        )
        top = QHBoxLayout()
        self.add_btn = QPushButton("+ Novo objetivo (Pote)")
        self.add_btn.setObjectName("CardAction")
        self.edit_btn = QPushButton("Editar selecionado")
        self.delete_btn = QPushButton("Eliminar")
        self.deposit_btn = QPushButton("Depositar")
        self.deposit_btn.setObjectName("Secondary")
        self.withdraw_btn = QPushButton("Levantar")
        top.addStretch()
        top.addWidget(self.withdraw_btn)
        top.addWidget(self.deposit_btn)
        top.addWidget(self.delete_btn)
        top.addWidget(self.edit_btn)
        top.addWidget(self.add_btn)
        self.body.addLayout(top)

        self.summary = QGridLayout()
        self.summary.setSpacing(14)
        self.body.addLayout(self.summary)

        card = GlassCard("Objetivos", strong=True)
        self.table = QTableWidget(0, 10)
        self.table.setProperty("columnPersistenceId", "goals.list")
        self.table.setHorizontalHeaderLabels(
            [
                "ID",
                "Objetivo",
                "Progresso",
                "Atual / Meta",
                "Falta",
                "Mensal",
                "Prazo",
                "Previsão",
                "Estado",
                "Notas",
            ]
        )
        self.table.setColumnHidden(0, True)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeToContents
        )
        self.table.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.ResizeToContents
        )
        self.table.horizontalHeader().setSectionResizeMode(9, QHeaderView.Stretch)
        self.table.setMinimumHeight(360)
        card.v.addWidget(self.table)
        hint = QLabel(
            "Dica: duplo clique num objetivo para editar. A vista é compacta para caberem muitos objetivos sem ocupar o ecrã todo."
        )
        hint.setObjectName("Subtle")
        card.v.addWidget(hint)
        self.body.addWidget(card)

        self.add_btn.clicked.connect(self.add_goal)
        self.edit_btn.clicked.connect(self.edit_selected)
        self.delete_btn.clicked.connect(self.delete_selected)
        self.deposit_btn.clicked.connect(self.deposit_selected)
        self.withdraw_btn.clicked.connect(self.withdraw_selected)
        self.table.doubleClicked.connect(self.edit_selected)
        self.refresh()

    def clear_summary(self) -> None:
        while self.summary.count():
            item = self.summary.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def selected_id(self) -> Optional[int]:
        row = self.table.currentRow()
        item = self.table.item(row, 0) if row >= 0 else None
        try:
            return int(item.text()) if item else None
        except (AttributeError, TypeError, ValueError):
            return None

    def months_until_deadline(self, deadline: str) -> int:
        try:
            d = parse_date(deadline)
            today = date.today()
            return max(
                0,
                (d.year - today.year) * 12
                + d.month
                - today.month
                + (1 if d.day >= today.day else 0),
            )
        except (AttributeError, TypeError, ValueError):
            return 0

    def goal_state(
        self, progress: float, missing: float, monthly: float, deadline: str
    ) -> tuple[str, str]:
        if progress >= 1:
            return "Concluído", GREEN
        if not deadline:
            return "Sem prazo", MUTED
        months_left = self.months_until_deadline(deadline)
        if months_left <= 0:
            return "Atrasado", RED
        if monthly <= 0:
            return "Sem plano", ORANGE
        needed_months = math.ceil(missing / monthly) if missing > 0 else 0
        if needed_months <= months_left:
            return "No prazo", GREEN
        return "Atrasado", RED

    def forecast_text(self, missing: float, monthly: float) -> str:
        if missing <= 0:
            return "Já atingido"
        if monthly <= 0:
            return "Sem contribuição"
        months = math.ceil(missing / monthly)
        return f"{months} meses"

    def refresh(self) -> None:
        rows = self.repo.goals()
        self.clear_summary()
        total_target = sum(safe_float(r["target_amount"]) for r in rows)
        total_saved = sum(safe_float(r["saved_amount"]) for r in rows)
        total_missing = max(0, total_target - total_saved)
        avg_progress = (total_saved / total_target * 100) if total_target else 0
        completed = sum(
            1
            for r in rows
            if safe_float(r["target_amount"])
            and safe_float(r["saved_amount"]) >= safe_float(r["target_amount"])
        )
        for i, metric in enumerate(
            [
                ("Objetivos", str(len(rows)), f"{completed} concluídos", BLUE, "◎"),
                (
                    "Progresso médio",
                    percent(avg_progress),
                    "global",
                    GREEN if avg_progress >= 50 else ORANGE,
                    "%",
                ),
                (
                    "Poupado",
                    eur(total_saved, self.app.theme.privacy),
                    "em objetivos",
                    PURPLE,
                    "↗",
                ),
                (
                    "Falta",
                    eur(total_missing, self.app.theme.privacy),
                    "para as metas",
                    ORANGE if total_missing else GREEN,
                    "→",
                ),
            ]
        ):
            self.summary.addWidget(MetricCard(*metric), 0, i)

        self.table.setRowCount(len(rows))
        if not rows:
            return
        for row, r in enumerate(rows):
            target = safe_float(r["target_amount"])
            saved = safe_float(r["saved_amount"])
            monthly = safe_float(r["monthly_contribution"])
            progress = saved / target if target else 0
            progress_pct = max(0, min(100, int(round(progress * 100))))
            missing = max(0, target - saved)
            state, colour = self.goal_state(
                progress, missing, monthly, r["deadline"] or ""
            )
            vals = [
                r["id"],
                (
                    f"🛟 {r['name']}"
                    if str(r["goal_type"] or "") == "emergency_fund"
                    else r["name"]
                ),
                "",
                f"{eur(saved, self.app.theme.privacy)} / {eur(target, self.app.theme.privacy)}",
                eur(missing, self.app.theme.privacy),
                eur(monthly, self.app.theme.privacy),
                r["deadline"] or "—",
                self.forecast_text(missing, monthly),
                state,
                r["notes"] or "",
            ]
            for col, val in enumerate(vals):
                item = QTableWidgetItem(str(val))
                if col == 2:
                    item.set_sort_value(progress_pct)
                if col in (3, 4, 5):
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                if col == 8:
                    item.setForeground(QColor(colour))
                    font = item.font()
                    font.setBold(True)
                    item.setFont(font)
                self.table.setItem(row, col, item)

            pb = QProgressBar()
            pb.setRange(0, 100)
            pb.setValue(progress_pct)
            pb.setMinimumWidth(130)

            if progress_pct >= 100:
                pb.setFormat(f"🎉 {progress_pct}% 🎉")
                pb.setStyleSheet(
                    f"QProgressBar {{ border: 1px solid {YELLOW}; border-radius: 6px; text-align: center; color: {self.app.theme.text}; font-weight: bold; background: transparent; }} QProgressBar::chunk {{ background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {YELLOW}, stop:1 #FFD700); border-radius: 5px; }}"
                )
            else:
                pb.setFormat(f"{progress_pct}%")
                color = GREEN if progress_pct >= 50 else BLUE
                pb.setStyleSheet(
                    f"QProgressBar {{ border: 1px solid {self.app.theme.border}; border-radius: 6px; text-align: center; color: {self.app.theme.text}; }} QProgressBar::chunk {{ background-color: {color}; border-radius: 5px; }}"
                )

            self.table.setCellWidget(row, 2, pb)
            self.table.setRowHeight(row, 40)

    def add_goal(self) -> None:
        d = GoalDialog(self.repo, self.app)
        if d.exec() == QDialog.Accepted:
            data = d.get_data()
            if not data["name"]:
                QMessageBox.warning(self, APP_TITLE, "Indica o nome do objetivo.")
                return
            self.repo.add_goal(
                data["name"],
                data["target"],
                data["saved"],
                data["monthly"],
                data["deadline"],
                data["notes"],
                data["goal_type"],
            )
            self.app.notify_data_changed("Objetivo guardado.")

    def edit_selected(self) -> None:
        gid = self.selected_id()
        if not gid:
            QMessageBox.information(self, APP_TITLE, "Seleciona um objetivo.")
            return
        r = self.repo.goal_by_id(gid)
        if not r:
            return
        d = GoalDialog(self.repo, self.app)
        d.setWindowTitle("Editar objetivo")
        d.name.setText(r["name"] or "")
        d.target.setValue(safe_float(r["target_amount"]))
        d.saved.setValue(safe_float(r["saved_amount"]))
        d.monthly.setValue(safe_float(r["monthly_contribution"]))
        try:
            dd = parse_date(r["deadline"])
            d.deadline.setDate(to_qdate(dd))
        except Exception as _exc:
            log_exception(f"{type(self).__name__} failed to parse goal deadline", _exc)
        d.notes.setPlainText(r["notes"] or "")
        type_index = d.goal_type.findData(str(r["goal_type"] or "standard"))
        d.goal_type.setCurrentIndex(max(0, type_index))
        if d.exec() == QDialog.Accepted:
            data = d.get_data()
            if not data["name"]:
                QMessageBox.warning(self, APP_TITLE, "Indica o nome do objetivo.")
                return
            self.repo.update_goal(
                gid,
                data["name"],
                data["target"],
                data["saved"],
                data["monthly"],
                data["deadline"],
                data["notes"],
                data["goal_type"],
            )
            self.app.notify_data_changed("Objetivo atualizado.")

    def delete_selected(self) -> None:
        gid = self.selected_id()
        if not gid:
            QMessageBox.information(
                self, APP_TITLE, "Seleciona primeiro um objetivo para eliminar."
            )
            return
        row = self.repo.goal_by_id(gid)
        if not row:
            return
        if (
            QMessageBox.question(self, APP_TITLE, f"Eliminar objetivo {row['name']}?")
            == QMessageBox.Yes
        ):
            self.repo.delete_goal(gid)
            self.refresh()
            self.app.notify_data_changed()

    def deposit_selected(self) -> None:
        gid = self.selected_id()
        if not gid:
            QMessageBox.information(
                self,
                APP_TITLE,
                "Seleciona primeiro um objetivo para depositar dinheiro.",
            )
            return
        row = self.repo.goal_by_id(gid)
        if not row:
            return

        amount, ok = QInputDialog.getDouble(
            self,
            APP_TITLE,
            f"Que valor queres depositar no pote '{row['name']}'?",
            0.01,
            0.01,
            1000000,
            2,
        )
        if ok and amount > 0:
            from datetime import date

            try:
                created_ids = (
                    self.app.change_journal_service.run_with_created_transactions(
                        lambda: [
                            self.repo.adjust_goal_with_transaction(
                                gid,
                                amount,
                                deposit=True,
                                transaction_date=date.today().isoformat(),
                                commit=False,
                            )
                        ],
                        label=f"Depósito no objetivo · {row['name']}",
                        action_type="goal_deposit",
                        state_targets={
                            "goals": {
                                "ids": [gid],
                                "columns": ("saved_amount",),
                            }
                        },
                    )
                )
            except Exception as exc:
                log_exception("Failed to deposit into goal atomically", exc)
                QMessageBox.critical(
                    self,
                    APP_TITLE,
                    "Não foi possível guardar o depósito. O objetivo e os movimentos não foram alterados.",
                )
                return
            self.refresh()
            self.app.notify_data_changed(
                f"Depositaste {eur(amount, self.app.theme.privacy)} no pote '{row['name']}'.",
                selected_tx_id=created_ids[0],
            )

    def withdraw_selected(self) -> None:
        gid = self.selected_id()
        if not gid:
            QMessageBox.information(
                self,
                APP_TITLE,
                "Seleciona primeiro um objetivo para levantar dinheiro.",
            )
            return
        row = self.repo.goal_by_id(gid)
        if not row:
            return

        saved_amount = safe_float(row_get(row, "saved_amount", 0.0))
        if saved_amount < 0.01:
            QMessageBox.information(
                self,
                APP_TITLE,
                "O objetivo está a 0,00 €. Não há valor para levantar.",
            )
            return

        amount, ok = QInputDialog.getDouble(
            self,
            APP_TITLE,
            f"Que valor queres levantar do pote '{row['name']}'?",
            0.01,
            0.01,
            saved_amount,
            2,
        )
        if ok and amount > 0:
            from datetime import date

            try:
                created_ids = (
                    self.app.change_journal_service.run_with_created_transactions(
                        lambda: [
                            self.repo.adjust_goal_with_transaction(
                                gid,
                                amount,
                                deposit=False,
                                transaction_date=date.today().isoformat(),
                                commit=False,
                            )
                        ],
                        label=f"Levantamento do objetivo · {row['name']}",
                        action_type="goal_withdrawal",
                        state_targets={
                            "goals": {
                                "ids": [gid],
                                "columns": ("saved_amount",),
                            }
                        },
                    )
                )
            except Exception as exc:
                log_exception("Failed to withdraw from goal atomically", exc)
                QMessageBox.critical(
                    self,
                    APP_TITLE,
                    "Não foi possível guardar o levantamento. O objetivo e os movimentos não foram alterados.",
                )
                return
            self.refresh()
            self.app.notify_data_changed(
                f"Levantaste {eur(amount, self.app.theme.privacy)} do pote '{row['name']}'.",
                selected_tx_id=created_ids[0],
            )


class ScheduledPaymentsPage(BasePage):
    def __init__(self, app: "LiquidFinanceApp"):
        super().__init__(
            app,
            "Pagamentos futuros",
            "Compras, serviços, subscrições e prestações debitadas mais tarde",
        )
        top = QHBoxLayout()
        self.add_btn = QPushButton("+ Novo pagamento")
        self.add_btn.setObjectName("CardAction")
        self.edit_btn = QPushButton("Editar")
        self.launch_btn = QPushButton("Lançar pagamentos em falta até hoje")
        self.delete_btn = QPushButton("Eliminar")
        self.delete_btn.setObjectName("Danger")
        top.addStretch()
        top.addWidget(self.launch_btn)
        top.addWidget(self.delete_btn)
        top.addWidget(self.edit_btn)
        top.addWidget(self.add_btn)
        self.table = QTableWidget(0, 10)
        self.table.setProperty("columnPersistenceId", "scheduled_payments.list")
        self.table.setHorizontalHeaderLabels(
            [
                "ID",
                "Nome",
                "Valor",
                "Categoria",
                "Método",
                "Início",
                "Dia",
                "Duração",
                "Pago",
                "Próximo débito",
            ]
        )
        self.table.setColumnHidden(0, True)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setMinimumHeight(520)
        card = GlassCard("Pagamentos configurados", strong=True)
        card.v.addWidget(self.table)
        self.body.addLayout(top)
        self.body.addWidget(card)
        self.add_btn.clicked.connect(self.add_payment)
        self.edit_btn.clicked.connect(self.edit_selected)
        self.launch_btn.clicked.connect(self.launch_due)
        self.delete_btn.clicked.connect(self.delete_selected)
        self.table.itemDoubleClicked.connect(lambda *_: self.edit_selected())
        self.refresh()

    def refresh(self):
        rows = self.repo.scheduled_payments()
        self.table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            total = int(row_get(row, "months_total", 0) or 0)
            paid = int(row_get(row, "months_paid", 0) or 0)
            vals = [
                row["id"],
                row["name"],
                eur(row["amount"], self.app.theme.privacy),
                row["category"],
                row["payment_method"],
                row["start_date"],
                row["debit_day"],
                "Sem fim" if total == 0 else f"{total} meses",
                paid,
                self.payments_service.next_due_for_payment(row),
            ]
            for c, v in enumerate(vals):
                item = QTableWidgetItem(str(v))
                if c in (2, 6, 7, 8):
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self.table.setItem(r, c, item)

    def add_payment(self):
        d = ScheduledPaymentDialog(self.repo, self)
        if d.exec() == QDialog.Accepted:
            data = d.get_data()
            if not data["name"]:
                QMessageBox.warning(self, APP_TITLE, "Indica o nome do pagamento.")
                return
            self.repo.add_scheduled_payment(
                data["name"],
                data["category"],
                data["subcategory"],
                data["amount"],
                data["account"],
                data["payment_method"],
                data["start_date"],
                data["debit_day"],
                data["months"],
                data["notes"],
            )
            self.app.notify_data_changed()

    def selected_id(self) -> Optional[int]:
        row = self.table.currentRow()
        item = self.table.item(row, 0) if row >= 0 else None
        # BUG-UI-03: seleção vazia ou texto não numérico devolve None em vez
        # de crashar com ValueError (os callers já verificam None).
        try:
            return int(item.text()) if item else None
        except (AttributeError, TypeError, ValueError):
            return None

    def _select_id(self, sid: int) -> None:
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item and item.text() == str(sid):
                self.table.selectRow(row)
                self.table.scrollToItem(item, QAbstractItemView.PositionAtCenter)
                break

    def edit_selected(self):
        sid = self.selected_id()
        if not sid:
            QMessageBox.information(self, APP_TITLE, "Seleciona um pagamento futuro.")
            return
        row = self.repo.scheduled_payment_by_id(sid)
        if not row:
            QMessageBox.warning(
                self, APP_TITLE, "Não encontrei o pagamento selecionado."
            )
            self.refresh()
            return
        d = ScheduledPaymentDialog(self.repo, self, row)
        if d.exec() == QDialog.Accepted:
            data = d.get_data()
            if not data["name"]:
                QMessageBox.warning(self, APP_TITLE, "Indica o nome do pagamento.")
                return
            self.repo.update_scheduled_payment(
                sid,
                data["name"],
                data["category"],
                data["subcategory"],
                data["amount"],
                data["account"],
                data["payment_method"],
                data["start_date"],
                data["debit_day"],
                data["months"],
                data["notes"],
            )
            self.app.notify_data_changed()
            self._select_id(sid)

    def delete_selected(self):
        sid = self.selected_id()
        if not sid:
            return
        if (
            QMessageBox.question(self, APP_TITLE, "Eliminar este pagamento futuro?")
            == QMessageBox.Yes
        ):
            self.repo.delete_scheduled_payment(sid)
            self.app.notify_data_changed()

    def launch_due(self):
        n = self.payments_service.generate_due_scheduled_payments(date.today())
        report = dict(
            getattr(self.payments_service, "last_generation_report", {}) or {}
        )
        pending = int(report.get("remaining_due", 0) or 0)
        invalid = int(report.get("invalid_payments", 0) or 0)
        details = [f"Foram criados {n} movimento(s) em falta."]
        if pending:
            details.append(
                f"Ficaram {pending} pagamento(s) vencido(s) por rever. "
                "Por segurança, cada operação cria no máximo 24 movimentos "
                "e 12 por pagamento configurado."
            )
        if invalid:
            details.append(
                f"{invalid} pagamento(s) têm valor inválido ou zero e não foram lançados."
            )
        QMessageBox.information(
            self,
            APP_TITLE,
            "\n\n".join(details),
        )
        self.app.notify_data_changed()


class MonthlyGoalsPage(BasePage):
    def __init__(self, app: "LiquidFinanceApp"):
        super().__init__(
            app, "Metas mensais", "Metas de poupança, investimento e limites de gasto"
        )
        top = QHBoxLayout()
        self.year = QComboBox()
        self.year.addItems([str(y) for y in self.repo.available_years()])
        self.month = QComboBox()
        self.month.addItems([f"{i:02d} — {PT_MONTHS[i - 1]}" for i in range(1, 13)])
        self.month.setCurrentIndex(date.today().month - 1)
        self.add_btn = QPushButton("+ Nova meta")
        self.add_btn.setObjectName("CardAction")
        self.del_btn = QPushButton("Eliminar")
        self.del_btn.setObjectName("Danger")
        top.addWidget(QLabel("Ano"))
        top.addWidget(self.year)
        top.addWidget(QLabel("Mês"))
        top.addWidget(self.month)
        top.addStretch()
        top.addWidget(self.add_btn)
        top.addWidget(self.del_btn)
        self.table = QTableWidget(0, 7)
        self.table.setProperty("columnPersistenceId", "monthly_goals.list")
        self.table.setHorizontalHeaderLabels(
            ["ID", "Meta", "Tipo", "Categoria", "Alvo", "Atual", "Estado"]
        )
        self.table.setColumnHidden(0, True)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setMinimumHeight(520)
        card = GlassCard("Metas do mês", strong=True)
        card.v.addWidget(self.table)
        self.body.addLayout(top)
        self.body.addWidget(card)
        self.add_btn.clicked.connect(self.add_goal)
        self.del_btn.clicked.connect(self.delete_goal)
        self.year.currentIndexChanged.connect(self.refresh)
        self.month.currentIndexChanged.connect(self.refresh)
        self.refresh()

    def ym(self):
        year = safe_year_from_combo(self.year)
        month = max(1, min(12, self.month.currentIndex() + 1))
        return f"{year:04d}-{month:02d}"

    def refresh(self):
        rows = self.repo.monthly_goals(self.ym())
        self.table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            pct, current, status = self.analytics_service.monthly_goal_progress(
                row, self.app.theme.privacy
            )
            vals = [
                row["id"],
                row["name"],
                row["goal_type"],
                row["category"],
                eur(row["target_value"], self.app.theme.privacy),
                current,
                f"{status} ({percent(pct)})",
            ]
            for c, v in enumerate(vals):
                self.table.setItem(r, c, QTableWidgetItem(str(v)))

    def add_goal(self):
        d = MonthlyGoalDialog(self.repo, self.ym(), self)
        if d.exec() == QDialog.Accepted:
            data = d.get_data()
            if not data["name"]:
                return
            try:
                self.repo.add_monthly_goal(
                    data["ym"],
                    data["name"],
                    data["goal_type"],
                    data["category"],
                    data["target"],
                    data["notes"],
                )
                self.app.notify_data_changed("Meta mensal guardada.")
            except Exception as exc:
                log_exception("MonthlyGoalsPage.add_goal", exc)
                QMessageBox.critical(
                    self, APP_TITLE, f"Erro ao guardar a meta mensal: {exc}"
                )

    def delete_goal(self):
        row = self.table.currentRow()
        if row < 0:
            return
        item = self.table.item(row, 0)
        if not item:
            return
        raw_id = (item.text() or "").strip()
        if not raw_id.isdigit():
            return
        goal_id = int(raw_id)
        name_item = self.table.item(row, 1)
        name = (
            name_item.text().strip()
            if (name_item and name_item.text() and name_item.text().strip())
            else "esta meta"
        )
        if (
            QMessageBox.question(
                self, APP_TITLE, f"Eliminar a meta mensal {name}?"
            )
            != QMessageBox.Yes
        ):
            return
        try:
            self.repo.delete_monthly_goal(goal_id)
            self.app.notify_data_changed("Meta mensal eliminada.")
        except Exception as exc:
            log_exception("MonthlyGoalsPage.delete_goal", exc)
            QMessageBox.critical(
                self, APP_TITLE, f"Erro ao eliminar a meta mensal: {exc}"
            )


def __getattr__(name: str):
    """Legacy import bridge for the extracted planner page.

    ``PlanningPage`` lives in ``ui.pages.planned_purchases`` since it was
    extracted; this keeps ``from ui.pages.planning import PlanningPage``
    working without shadowing the live classes defined above.  Importing it
    eagerly here would re-bind the module attribute on every import and hide
    which class actually backs the "Planeador" page.
    """
    if name == "PlanningPage":
        from .planned_purchases import PlanningPage

        return PlanningPage
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
