"""Monthly history and snapshot page extracted from reports.py."""

from __future__ import annotations

import html
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from utils.qt_runtime import configure_qt_runtime

configure_qt_runtime()

try:
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QColor
    from PySide6.QtWidgets import (
        QComboBox,
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
    )
except Exception as exc:  # pragma: no cover - PySide6 missing on the user's PC.
    print("PySide6 não está instalado. Instala com: pip install PySide6")
    print(exc)
    sys.exit(1)

from ui.components import BarChart, GlassCard, MetricCard
from ui.chart_detail_support import movement_detail_payload, open_movements_view
from ui.core import BasePage
from ui.core.smart_table import SmartTableWidgetItem as QTableWidgetItem
from utils.app_logging import log_exception
from utils.output_security import privacy_export_cell
from utils.helpers import (
    APP_TITLE,
    BLUE,
    GREEN,
    PURPLE,
    RED,
    eur,
    iso,
    percent,
    safe_float,
)

if TYPE_CHECKING:
    from main import LiquidFinanceApp


class HistoryPage(BasePage):
    def __init__(self, app: "LiquidFinanceApp"):
        super().__init__(
            app,
            "Histórico mensal",
            "Compara meses, categorias, notas, poupança e evolução face a meses anteriores",
        )
        top = GlassCard(strong=True)
        row = QHBoxLayout()
        top.v.addLayout(row)
        self.month = QComboBox()
        self.reload_months()
        self.close_btn = QPushButton("Fechar mês / guardar snapshot")
        self.close_btn.setObjectName("Primary")
        self.export_btn = QPushButton("Exportar relatório HTML")
        row.addWidget(QLabel("Mês"))
        row.addWidget(self.month)
        row.addWidget(self.close_btn)
        row.addWidget(self.export_btn)
        row.addStretch()
        self.register_filter_panel(top)

        self.summary = QGridLayout()
        self.summary.setSpacing(14)
        self.register_responsive_grid(self.summary)

        self.notes = QTextEdit()
        self.notes.setPlaceholderText(
            "Notas do mês: decisões, imprevistos, despesas relevantes, "
            "objetivos, contexto pessoal..."
        )
        self.notes.setMinimumHeight(105)
        self.notes.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._notes_month = ""
        self._notes_loaded_text = ""
        self._note_drafts: dict[str, str] = {}
        self._loading_notes = False
        self.notes.textChanged.connect(self._remember_note_draft)
        self.save_notes_btn = QPushButton("Guardar notas do mês")
        self.save_notes_btn.setObjectName("Primary")
        notes_card = GlassCard("Notas do mês", strong=True)
        self.notes_privacy_hint = QLabel("Notas ocultas no modo privado.")
        notes_card.v.addWidget(self.notes_privacy_hint)
        notes_card.v.addWidget(self.notes)
        notes_row = QHBoxLayout()
        notes_row.addStretch()
        notes_row.addWidget(self.save_notes_btn)
        notes_card.v.addLayout(notes_row)

        self.comparison_label = QLabel()
        self.comparison_label.setWordWrap(True)
        self.comparison_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.comparison_card = GlassCard(
            "Comparação com meses anteriores",
            strong=True,
        )
        self.comparison_card.set_compact_header()
        self.comparison_card.fit_label_height(
            self.comparison_label,
            word_wrap=True,
        )
        self.comparison_card.v.addWidget(self.comparison_label)

        self.bar = BarChart(lambda: self.app.theme)
        self.bar.setAccessibleName("Distribuição mensal por categoria")
        self.bar.configure_details(
            self._category_detail_payload,
            action_label="Abrir movimentos",
            action_handler=self._open_category_movements,
        )
        self.table = QTableWidget(0, 5)
        self.table.setProperty("columnPersistenceId", "history.changes")
        self.table.setHorizontalHeaderLabels(
            ["Categoria", "Gasto", "Orçamento", "% usado", "Diferença"]
        )
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setMinimumHeight(430)
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.table.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        for column in range(1, 5):
            self.table.horizontalHeader().setSectionResizeMode(
                column,
                QHeaderView.ResizeToContents,
            )
        category_card = GlassCard("Resumo por categoria", strong=True)
        category_card.setMinimumHeight(520)
        category_card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        category_card.v.addWidget(self.table)
        self.monthly_distribution_card = GlassCard("Distribuição mensal")
        self.monthly_distribution_card.setMinimumHeight(520)
        self.monthly_distribution_card.setSizePolicy(
            QSizePolicy.Expanding,
            QSizePolicy.Expanding,
        )
        self.monthly_distribution_card.set_compact_header()
        self.bar.setMinimumHeight(430)
        self.monthly_distribution_card.v.addWidget(self.bar)

        history_blocks = QHBoxLayout()
        history_blocks.setSpacing(12)
        history_blocks.addWidget(category_card, 1)
        history_blocks.addWidget(self.monthly_distribution_card, 1)
        details = QHBoxLayout()
        details.setSpacing(12)
        details.addWidget(notes_card, 1)
        details.addWidget(self.comparison_card, 1)
        self.body.addWidget(top)
        self.body.addLayout(self.summary)
        self.body.addLayout(details)
        self.body.addLayout(history_blocks)

        self.month.currentTextChanged.connect(self.refresh)
        self.close_btn.clicked.connect(self.save_snapshot)
        self.export_btn.clicked.connect(self.export_html)
        self.save_notes_btn.clicked.connect(self.save_notes)
        self.refresh()

    def reload_months(self) -> None:
        if hasattr(self, "notes") and not self.prepare_to_leave("refresh"):
            return
        selected = self.month.currentText()
        self.month.blockSignals(True)
        try:
            self.month.clear()
            self.month.addItems(self.repo.report_months())
            if self.month.findText(selected) >= 0:
                self.month.setCurrentText(selected)
        finally:
            self.month.blockSignals(False)

    @staticmethod
    def _period_for_ym(ym: str) -> tuple[str, str]:
        try:
            start = datetime.strptime(ym + "-01", "%Y-%m-%d").date()
        except (TypeError, ValueError):
            start = date.today().replace(day=1)
        if start.month == 12:
            next_month = start.replace(year=start.year + 1, month=1)
        else:
            next_month = start.replace(month=start.month + 1)
        return iso(start), iso(next_month - timedelta(days=1))

    @staticmethod
    def _shift_month(ym: str, delta: int) -> str:
        try:
            current = datetime.strptime(ym + "-01", "%Y-%m-%d").date()
        except (TypeError, ValueError):
            current = date.today().replace(day=1)
        shifted = current.month - 1 + delta
        year = current.year + shifted // 12
        month = shifted % 12 + 1
        return f"{year:04d}-{month:02d}"

    def period(self) -> tuple[str, str]:
        month = self.month.currentText() or date.today().strftime("%Y-%m")
        return self._period_for_ym(month)

    @staticmethod
    def _bar_category(item: dict) -> str:
        category = str(item.get("label") or "")
        return "" if category.startswith("Outros (") else category

    def _category_detail_payload(self, item: dict) -> dict:
        start, end = self.period()
        category = self._bar_category(item)
        return movement_detail_payload(
            self.repo,
            self.app.theme,
            title="Distribuição mensal",
            item=item,
            start=start,
            end=end,
            category=category,
            kind="Despesas",
            subtitle=str(item.get("label") or ""),
            explanation=(
                "A barra apresenta a despesa da categoria no mês selecionado."
                if category
                else (
                    "Esta barra agrega as categorias que ficaram fora das oito "
                    "maiores; a lista apresenta os maiores movimentos do mês."
                )
            ),
        )

    def _open_category_movements(self, item: dict) -> None:
        start, end = self.period()
        open_movements_view(
            self.app,
            start=start,
            end=end,
            kind="Despesas",
            category=self._bar_category(item),
        )

    def clear_summary(self) -> None:
        while self.summary.count():
            item = self.summary.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def _load_notes(self) -> None:
        month = self.month.currentText() or date.today().strftime("%Y-%m")
        text = self.repo.monthly_snapshot_notes(month)
        self._loading_notes = True
        try:
            self._notes_month = month
            self._notes_loaded_text = text
            self.notes.setPlainText(text)
            self.notes.document().setModified(False)
        finally:
            self._loading_notes = False

    def _remember_note_draft(self) -> None:
        if not self._loading_notes and self._notes_month:
            text = self.notes.toPlainText()
            if text != self._notes_loaded_text:
                self._note_drafts[self._notes_month] = text
            else:
                self._note_drafts.pop(self._notes_month, None)

    def _persist_note_draft(self) -> bool:
        """The editor owns its original month, regardless of selector changes."""
        month = self._notes_month
        if not month:
            return True
        text = self.notes.toPlainText()
        start, end = self._period_for_ym(month)
        try:
            self.repo.save_monthly_snapshot_notes(month, start, end, text)
        except Exception as exc:
            log_exception("Failed to save month-owned notes", exc)
            QMessageBox.critical(self, APP_TITLE, f"Não foi possível guardar as notas:\n{exc}")
            return False
        self._notes_loaded_text = text
        self._note_drafts.pop(month, None)
        self.notes.document().setModified(False)
        return True

    def prepare_to_leave(self, reason: str = "refresh") -> bool:
        if not self._notes_month or self.notes.toPlainText() == self._notes_loaded_text:
            return True
        self._remember_note_draft()
        answer = QMessageBox.question(
            self,
            "Notas por guardar",
            f"As notas de {self._notes_month} têm alterações. Queres guardá-las antes de continuar?",
            QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        if answer == QMessageBox.Save:
            return self._persist_note_draft()
        if answer == QMessageBox.Discard:
            self._note_drafts.pop(self._notes_month, None)
            self._loading_notes = True
            try:
                self.notes.setPlainText(self._notes_loaded_text)
                self.notes.document().setModified(False)
            finally:
                self._loading_notes = False
            return True
        return False

    def closeEvent(self, event) -> None:
        if not self.prepare_to_leave("close"):
            event.ignore()
            return
        super().closeEvent(event)

    def _comparison_html(self) -> str:
        month = self.month.currentText() or date.today().strftime("%Y-%m")
        start, end = self._period_for_ym(month)
        current = self.analytics_service.totals_for_period(start, end)
        previous_month = self._shift_month(month, -1)
        previous_start, previous_end = self._period_for_ym(previous_month)
        previous = self.analytics_service.totals_for_period(
            previous_start,
            previous_end,
        )
        previous_totals = []
        for offset in (-1, -2, -3):
            compared_month = self._shift_month(month, offset)
            compared_start, compared_end = self._period_for_ym(compared_month)
            previous_totals.append(
                self.analytics_service.totals_for_period(
                    compared_start,
                    compared_end,
                )
            )

        def average(key: str) -> float:
            values = [safe_float(item.get(key)) for item in previous_totals]
            return sum(values) / len(values) if values else 0.0

        def difference_line(
            label: str,
            key: str,
            positive_when_higher: bool = True,
        ) -> str:
            current_value = safe_float(current.get(key))
            previous_value = safe_float(previous.get(key))
            difference = current_value - previous_value
            colour = GREEN if (difference >= 0) == positive_when_higher else RED
            sign = "+" if difference >= 0 and not self.app.theme.privacy else ""
            return (
                f"<b>{label}:</b> "
                f"{eur(current_value, self.app.theme.privacy)} "
                f"<span style='color:{colour}'>"
                f"({sign}{eur(difference, self.app.theme.privacy)} "
                f"vs {previous_month})</span>"
            )

        income = safe_float(current.get("income"))
        savings = safe_float(current.get("savings"))
        previous_income = safe_float(previous.get("income"))
        previous_savings = safe_float(previous.get("savings"))
        savings_rate = savings / income * 100 if income else 0
        previous_rate = (
            previous_savings / previous_income * 100 if previous_income else 0
        )
        average_expenses = average("expenses")
        average_savings = average("savings")
        expenses_difference = safe_float(current.get("expenses")) - average_expenses
        savings_difference = savings - average_savings
        expenses_colour = RED if expenses_difference > 0 else GREEN
        savings_colour = GREEN if savings_difference >= 0 else RED
        return (
            f"{difference_line('Receitas', 'income', True)}<br>"
            f"{difference_line('Gastos', 'expenses', False)}<br>"
            f"{difference_line('Poupança', 'savings', True)}<br>"
            f"<b>Taxa de poupança:</b> {percent(savings_rate, self.app.theme.privacy)} "
            f"<span style='color:{GREEN if savings_rate >= previous_rate else RED}'>"
            f"({percent(savings_rate - previous_rate, self.app.theme.privacy)} vs {previous_month})"
            f"</span><br><br>"
            "<b>Média dos 3 meses anteriores</b><br>"
            f"Gastos: {eur(average_expenses, self.app.theme.privacy)} "
            f"<span style='color:{expenses_colour}'>"
            f"({('+' if expenses_difference >= 0 and not self.app.theme.privacy else '')}"
            f"{eur(expenses_difference, self.app.theme.privacy)} vs média)"
            f"</span><br>"
            f"Poupança: {eur(average_savings, self.app.theme.privacy)} "
            f"<span style='color:{savings_colour}'>"
            f"({('+' if savings_difference >= 0 and not self.app.theme.privacy else '')}"
            f"{eur(savings_difference, self.app.theme.privacy)} vs média)"
            "</span>"
        )

    def refresh(self) -> None:
        privacy = self.app.theme.privacy
        self.notes.setVisible(not privacy)
        self.save_notes_btn.setVisible(not privacy)
        self.notes_privacy_hint.setVisible(privacy)
        reload_notes = self.prepare_to_leave("refresh")
        if not reload_notes:
            self.month.blockSignals(True)
            try:
                self.month.setCurrentText(self._notes_month)
            finally:
                self.month.blockSignals(False)
            # Retain the draft while applying privacy to read-only panels.
        start, end = self.period()
        totals = self.analytics_service.totals_for_period(start, end)
        income = safe_float(totals["income"])
        expenses = safe_float(totals["expenses"])
        savings = safe_float(totals["savings"])
        rate = savings / income * 100 if income else 0
        investment_available = bool(totals.get("investments_available"))
        investment_as_of = str(totals.get("investment_as_of") or "")

        self.clear_summary()
        metrics = [
            ("Receitas", eur(income, self.app.theme.privacy), "mês", GREEN, "↓"),
            ("Gastos", eur(expenses, self.app.theme.privacy), "mês", RED, "↑"),
            (
                "Poupança",
                eur(savings, self.app.theme.privacy),
                percent(rate, self.app.theme.privacy),
                BLUE,
                "◎",
            ),
            (
                "Investimentos manuais",
                (
                    eur(totals["investments"], self.app.theme.privacy)
                    if investment_available
                    else "—"
                ),
                (
                    f"snapshot de {investment_as_of}"
                    if investment_available and investment_as_of
                    else "sem snapshot histórico"
                ),
                PURPLE,
                "◆",
            ),
        ]
        for index, metric in enumerate(metrics):
            self.summary.addWidget(MetricCard(*metric), 0, index)
        self.reflow_responsive_grid(self.summary)

        categories = self.analytics_service.category_totals(start, end)
        self.bar.set_items(categories)
        month_key = self.month.currentText() or date.today().strftime("%Y-%m")
        budgets = self.repo.category_budgets(month=month_key)
        self.table.setRowCount(len(categories))
        for row, (name, value, _colour) in enumerate(categories):
            budget = budgets.get(name, 0)
            used = value / budget * 100 if budget else 0
            difference = budget - value if budget else 0
            values = [
                (name, name),
                (eur(value, self.app.theme.privacy), value),
                (eur(budget, self.app.theme.privacy), budget),
                (percent(used, self.app.theme.privacy), used),
                (eur(difference, self.app.theme.privacy), difference),
            ]
            for column, (text, sort_val) in enumerate(values):
                item = QTableWidgetItem(str(text))
                item.set_sort_value(sort_val)
                if column in {1, 2, 4}:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                if column == 4:
                    item.setForeground(QColor(GREEN if difference >= 0 else RED))
                self.table.setItem(row, column, item)
        if reload_notes:
            self._load_notes()
        self.comparison_label.setText(self._comparison_html())

    def save_notes(self) -> None:
        self._persist_note_draft()

    def save_snapshot(self) -> None:
        start, end = self.period()
        month = self.month.currentText()
        existing_notes = self.notes.toPlainText()
        self.repo.save_monthly_snapshot(
            month,
            start,
            end,
            existing_notes or "Snapshot guardado na interface Liquid Glass",
        )
        self._notes_loaded_text = self.notes.toPlainText()
        self._note_drafts.pop(self._notes_month, None)
        self.notes.document().setModified(False)
        QMessageBox.information(self, APP_TITLE, f"Snapshot de {month} guardado.")
        self.refresh()

    def export_html(self) -> None:
        start, end = self.period()
        totals = self.analytics_service.totals_for_period(start, end)
        categories = self.analytics_service.category_totals(start, end)
        month = self.month.currentText()
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Exportar relatório",
            str(Path.home() / f"relatorio_{month}.html"),
            "HTML (*.html)",
        )
        if not path:
            return

        privacy = self.app.theme.privacy
        category_rows = "".join(
            f"<tr><td>{html.escape(str(category))}</td>"
            f"<td>{html.escape(eur(value, privacy))}</td></tr>"
            for category, value, _colour in categories
        )
        notes_html = html.escape(str(privacy_export_cell(
            self.notes.toPlainText(), "notes", privacy,
        ))).replace("\n", "<br>")
        investment_html = (
            f"{html.escape(eur(totals['investments'], privacy))} "
            f"(snapshot de {html.escape(str(totals.get('investment_as_of') or ''))})"
            if totals.get("investments_available")
            else "Sem snapshot histórico para este período"
        )
        report = (
            "<!doctype html><meta charset='utf-8'>"
            f"<title>Relatório {html.escape(month)}</title>"
            "<style>"
            "body{font-family:Segoe UI,Arial;padding:32px;color:#172033}"
            ".card{border:1px solid #e5e7eb;border-radius:18px;"
            "padding:18px;margin:14px 0}"
            "td,th{padding:9px 14px;border-bottom:1px solid #eef2f7}"
            "</style>"
            f"<h1>Relatório mensal — {html.escape(month)}</h1>"
            "<div class='card'>"
            f"<b>Receitas:</b> {html.escape(eur(totals['income'], privacy))}<br>"
            f"<b>Gastos:</b> {html.escape(eur(totals['expenses'], privacy))}<br>"
            f"<b>Poupança:</b> {html.escape(eur(totals['savings'], privacy))}<br>"
            f"<b>Investimentos manuais:</b> {investment_html}"
            "</div>"
            f"<h2>Notas do mês</h2><div class='card'>{notes_html or 'Sem notas.'}</div>"
            f"<h2>Comparação</h2><div class='card'>{self._comparison_html()}</div>"
            "<h2>Categorias</h2><table>"
            f"<tr><th>Categoria</th><th>Gasto</th></tr>{category_rows}</table>"
        )
        try:
            Path(path).write_text(report, encoding="utf-8")
            QMessageBox.information(
                self,
                APP_TITLE,
                f"Relatório exportado para:\n{path}",
            )
        except Exception as exc:
            log_exception(f"Falha ao exportar relatório para {path}", exc)
            QMessageBox.critical(self, APP_TITLE, f"Erro ao exportar relatório:\n{exc}")
