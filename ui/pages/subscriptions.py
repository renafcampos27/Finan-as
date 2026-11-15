"""Subscription management page.

This page used to live at the end of ``investments.py``. Keeping recurring
payments separate makes both investment and subscription changes safer.
"""

from __future__ import annotations

import sys
from datetime import date
from typing import TYPE_CHECKING, Optional

from utils.qt_runtime import configure_qt_runtime, is_qt_valid

configure_qt_runtime()

try:
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QColor
    from PySide6.QtWidgets import (
        QAbstractItemView,
        QCheckBox,
        QDialog,
        QGridLayout,
        QHBoxLayout,
        QHeaderView,
        QLabel,
        QLineEdit,
        QMessageBox,
        QPushButton,
        QTableWidget,
    )
except Exception as exc:  # pragma: no cover - PySide6 missing on the user's PC.
    print("PySide6 não está instalado. Instala com: pip install PySide6")
    print(exc)
    sys.exit(1)

from ui.components import GlassCard, MetricCard
from ui.core import BasePage
from ui.core.smart_table import SmartTableWidgetItem as QTableWidgetItem
from ui.dialogs import SubscriptionDialog
from services.subscription_matching import subscription_history_linkage
from utils.helpers import (
    APP_TITLE,
    BLUE,
    GREEN,
    ORANGE,
    PURPLE,
    RED,
    TEAL,
    YELLOW,
    eur,
    row_get,
)

if TYPE_CHECKING:
    from main import LiquidFinanceApp


def _effort_color(value: float) -> Optional[str]:
    if value >= 45:
        return RED
    if value >= 35:
        return ORANGE
    if value >= 25:
        return YELLOW
    if value >= 15:
        return PURPLE
    if value >= 10:
        return BLUE
    if value >= 5:
        return TEAL
    if value <= 1:
        return GREEN
    return None


def _monthly_subscription_amount(row) -> float:
    amount = float(row["amount"] or 0)
    divisors = {
        "Anual": 12,
        "Semestral": 6,
        "Trimestral": 3,
    }
    return amount / divisors.get(str(row["period"]), 1)


def _subscription_history_costs(repo, rows) -> tuple[float, float]:
    summary = subscription_history_linkage(repo, rows)
    return summary.year_to_date, summary.all_time


class SubscriptionsPage(BasePage):
    def __init__(self, app: "LiquidFinanceApp"):
        super().__init__(
            app,
            "Subscrições",
            "Custos recorrentes, estado e impacto mensal/anual",
        )
        top = QHBoxLayout()
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("🔍 Pesquisar subscrição...")
        self.search_input.setClearButtonEnabled(True)
        self.search_input.setMaximumWidth(260)
        self.search_input.textChanged.connect(self.refresh)

        self.detect_btn = QPushButton("🔍 Detetar recorrentes")
        self.detect_btn.setObjectName("Secondary")
        self.detect_btn.setToolTip(
            "Analisa extratos para encontrar possíveis assinaturas ou despesas recorrentes não registadas"
        )
        self.detect_btn.clicked.connect(self.detect_recurring)

        self.active_only = QCheckBox("Mostrar só ativas")
        self.active_only.setChecked(True)
        self.add_btn = QPushButton("+ Nova subscrição")
        self.add_btn.setObjectName("CardAction")
        self.edit_btn = QPushButton("Editar subscrição")
        self.delete_btn = QPushButton("Eliminar")
        self.toggle_btn = QPushButton("Ativar/desativar")
        self.pay_now_btn = QPushButton("Pagar agora")
        self.pay_now_btn.setToolTip(
            "Gera imediatamente um movimento na lista de transações com a data de hoje"
        )
        self.pay_now_btn.setObjectName("Secondary")
        top.addWidget(self.active_only)
        top.addWidget(self.search_input)
        top.addWidget(self.detect_btn)
        top.addStretch()
        top.addWidget(self.pay_now_btn)
        top.addWidget(self.edit_btn)
        top.addWidget(self.delete_btn)
        top.addWidget(self.toggle_btn)
        top.addWidget(self.add_btn)

        self.summary = QGridLayout()
        self.summary.setSpacing(14)
        self.table = QTableWidget(0, 13)
        self.table.setProperty("columnPersistenceId", "subscriptions.list")
        self.table.setHorizontalHeaderLabels(
            [
                "ID",
                "Nome",
                "Valor",
                "Categoria",
                "Conta",
                "Forma de pagamento",
                "Dia",
                "Próxima renovação",
                "Período",
                "Esforço (%)",
                "Estado",
                "Notas",
                "Nomes no extrato",
            ]
        )
        self.table.setColumnHidden(0, True)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.verticalHeader().setVisible(False)
        self.list_card = GlassCard("Lista", strong=True)
        self.list_card.setMinimumHeight(560)
        self.table.setMinimumHeight(500)
        self.list_card.v.addWidget(self.table)
        self.status_label = QLabel()
        self.status_label.setObjectName("Subtle")
        self.status_label.setWordWrap(True)
        self.list_card.v.addWidget(self.status_label)
        self.body.addLayout(top)
        radar_row = QHBoxLayout()
        self.radar_btn = QPushButton("Radar de aumentos")
        self.radar_btn.setAccessibleName("Rever possíveis aumentos de subscrições")
        self.radar_btn.clicked.connect(self.open_price_radar)
        radar_row.addWidget(self.radar_btn)
        radar_hint = QLabel("Rever cobranças maiores sem alterar preços ou criar pagamentos automaticamente.")
        radar_hint.setWordWrap(True)
        radar_row.addWidget(radar_hint, 1)
        self.body.addLayout(radar_row)
        self.body.addLayout(self.summary)
        self.body.addWidget(self.list_card)

        self.active_only.toggled.connect(self.refresh)
        self.add_btn.clicked.connect(self.add_subscription)
        self.edit_btn.clicked.connect(self.edit_selected)
        self.delete_btn.clicked.connect(self.delete_selected)
        self.toggle_btn.clicked.connect(self.toggle_selected)
        self.pay_now_btn.clicked.connect(self.pay_now_selected)
        self.table.itemDoubleClicked.connect(lambda *_: self.edit_selected())
        self.refresh()

    def clear_summary(self) -> None:
        while self.summary.count():
            item = self.summary.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def _current_month_income(self) -> float:
        today = date.today()
        period_start, period_end, _ = self.repo.month_period(today.year, today.month)
        totals = self.repo.totals_for_payment_period(period_start, period_end)
        return float(totals.get("income", 0) or 0)

    def _refresh_summary(self, rows, active, monthly: float, income: float) -> None:
        linkage = subscription_history_linkage(self.repo, rows)
        year_to_date, all_time = linkage.year_to_date, linkage.all_time
        next_week = [
            row
            for row in active
            if 0
            <= self.payments_service.subscription_days_until(row, date.today())
            <= 7
        ]
        monthly_color = PURPLE
        monthly_value = eur(monthly, self.app.theme.privacy)
        if income > 0:
            total_effort = monthly / income * 100
            monthly_color = _effort_color(total_effort) or PURPLE
            monthly_value += (
                f" <span style='color: {monthly_color};'>({total_effort:.1f}%)</span>"
            )
        metrics = [
            ("Custo mensal", monthly_value, "estimado", monthly_color, "◔"),
            (
                "Custo anual",
                eur(monthly * 12, self.app.theme.privacy),
                "projeção",
                BLUE,
                "Σ",
            ),
            (
                "Pago no ano",
                eur(year_to_date, self.app.theme.privacy),
                str(date.today().year),
                ORANGE,
                "📅",
            ),
            (
                "Total Histórico",
                eur(all_time, self.app.theme.privacy),
                "desde sempre",
                TEAL,
                "∞",
            ),
            ("Ativas", str(len(active)), "subscrições", GREEN, "✓"),
            ("Próximos 7 dias", str(len(next_week)), "renovações", ORANGE, "→"),
            (
                "Histórico associado",
                str(linkage.linked),
                "movimentos confirmados",
                TEAL,
                "🔗",
            ),
            (
                "A confirmar",
                str(linkage.probable + linkage.unlinked + linkage.ambiguous),
                "não entra nos totais",
                ORANGE
                if linkage.probable + linkage.unlinked + linkage.ambiguous
                else GREEN,
                "?",
            ),
        ]
        for index, metric in enumerate(metrics):
            self.summary.addWidget(MetricCard(*metric), index // 3, index % 3)

    def refresh(self) -> None:
        rows = self.repo.subscriptions(active_only=self.active_only.isChecked())
        query = (
            self.search_input.text().strip().lower()
            if hasattr(self, "search_input")
            else ""
        )
        if query:
            rows = [
                r
                for r in rows
                if query in str(r["name"] or "").lower()
                or query in str(r["category"] or "").lower()
                or query in str(r["notes"] or "").lower()
                or query in str(row_get(r, "aliases", "") or "").lower()
                or query in str(row_get(r, "account", "") or "").lower()
            ]
        active = self.repo.subscriptions(active_only=True)
        monthly = self.payments_service.active_subscription_monthly_total()
        income = self._current_month_income()

        self.clear_summary()
        self._refresh_summary(rows, active, monthly, income)
        self.table.clearContents()
        self.table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            monthly_amount = _monthly_subscription_amount(row)
            effort_value = monthly_amount / income * 100 if income > 0 else None
            effort_text = f"{effort_value:.1f}%" if effort_value is not None else "—"
            effort_color = (
                _effort_color(effort_value) if effort_value is not None else None
            )
            values = [
                row["id"],
                row["name"],
                eur(row["amount"], self.app.theme.privacy),
                row["category"],
                row_get(row, "account", "Conta principal"),
                row_get(row, "payment_method", "") or "—",
                row["renewal_day"],
                self.payments_service.next_subscription_due_date(row).strftime(
                    "%d/%m/%Y"
                ),
                row["period"],
                effort_text,
                "Ativa" if int(row["active"] or 0) else "Inativa",
                row["notes"],
                row_get(row, "aliases", ""),
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if column in {2, 9}:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                if column == 9 and effort_color:
                    item.setForeground(QColor(effort_color))
                if column == 10:
                    item.setForeground(
                        QColor(GREEN if value == "Ativa" else self.app.theme.muted)
                    )
                self.table.setItem(row_index, column, item)

    def selected_id(self) -> Optional[int]:
        row = self.table.currentRow()
        item = self.table.item(row, 0) if row >= 0 else None
        try:
            return int(item.text()) if item else None
        except (AttributeError, TypeError, ValueError):
            return None

    def open_price_radar(self) -> None:
        from ui.subscription_radar import SubscriptionRadarDialog

        dialog = SubscriptionRadarDialog(self.repo, self)
        self._price_radar_dialog = dialog
        try:
            dialog.exec()
            if dialog.changed:
                notify = getattr(self.app, "notify_data_changed", None)
                if callable(notify):
                    notify()
        finally:
            self._price_radar_dialog = None
            if is_qt_valid(dialog):
                dialog.deleteLater()

    def update_export_privacy_labels(self) -> None:
        """Reuse the app's immediate privacy notification for the open review."""
        dialog = getattr(self, "_price_radar_dialog", None)
        if is_qt_valid(dialog):
            dialog._check_privacy()

    def detect_recurring(self) -> None:
        try:
            suspects = self.analytics_service.detect_recurring_subscriptions()
        except Exception:
            suspects = []
        if not suspects:
            QMessageBox.information(
                self,
                APP_TITLE,
                "Não foram detetadas novas subscrições ou despesas recorrentes nos extratos recentes.",
            )
            return
        items_text = "\n".join(
            f"• {s.get('name', '')}: {eur(float(s.get('amount', 0.0)), self.app.theme.privacy)} (~{s.get('occurrences', 0)} cobranças)"
            for s in suspects[:10]
        )
        msg = (
            f"Foram detetadas {len(suspects)} possíveis despesas recorrentes nos últimos 90 dias:\n\n"
            f"{items_text}\n\n"
            "Desejas registar uma nova subscrição agora?"
        )
        reply = QMessageBox.question(
            self,
            APP_TITLE,
            msg,
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        if reply == QMessageBox.Yes:
            self.add_subscription()

    def add_subscription(self) -> None:
        dialog = SubscriptionDialog(self.repo, self.app)
        if dialog.exec() != QDialog.Accepted:
            return
        data = dialog.get_data()
        if not data["name"]:
            QMessageBox.warning(self, APP_TITLE, "Indica o nome da subscrição.")
            return
        self.repo.add_subscription(
            data["name"],
            data["amount"],
            data["category"],
            data["renewal_day"],
            data["period"],
            data["notes"],
            data["active"],
            next_renewal_date=data["next_renewal_date"],
            aliases=data["aliases"],
            account=data["account"],
            payment_method=data["payment_method"],
        )
        self.app.notify_data_changed()

    def edit_selected(self) -> None:
        subscription_id = self.selected_id()
        if not subscription_id:
            QMessageBox.information(self, APP_TITLE, "Seleciona uma subscrição.")
            return
        row = self.repo.subscription_by_id(subscription_id)
        if not row:
            QMessageBox.warning(
                self,
                APP_TITLE,
                "Não encontrei a subscrição selecionada.",
            )
            self.refresh()
            return
        dialog = SubscriptionDialog(self.repo, self.app, row)
        if dialog.exec() != QDialog.Accepted:
            return
        data = dialog.get_data()
        if not data["name"]:
            QMessageBox.warning(self, APP_TITLE, "Indica o nome da subscrição.")
            return
        self.repo.update_subscription(
            subscription_id,
            data["name"],
            data["amount"],
            data["category"],
            data["renewal_day"],
            data["period"],
            data["notes"],
            data["active"],
            next_renewal_date=data["next_renewal_date"],
            aliases=data["aliases"],
            account=data["account"],
            payment_method=data["payment_method"],
        )
        self.app.notify_data_changed()
        self._select_id(subscription_id)

    def delete_selected(self) -> None:
        subscription_id = self.selected_id()
        if not subscription_id:
            QMessageBox.information(
                self,
                APP_TITLE,
                "Seleciona primeiro uma subscrição para eliminar.",
            )
            return
        current_row = self.table.currentRow()
        name_item = self.table.item(current_row, 1) if current_row >= 0 else None
        name = name_item.text() if name_item else "esta subscrição"
        if (
            QMessageBox.question(
                self,
                APP_TITLE,
                f"Eliminar subscrição {name}?",
            )
            != QMessageBox.Yes
        ):
            return
        self.repo.delete_subscription(subscription_id)
        self.refresh()
        self.app.notify_data_changed()

    def pay_now_selected(self) -> None:
        subscription_id = self.selected_id()
        if not subscription_id:
            QMessageBox.information(
                self,
                APP_TITLE,
                "Seleciona primeiro a subscrição que queres pagar.",
            )
            return
        subscription = self.repo.subscription_by_id(subscription_id)
        if not subscription:
            return
        reply = QMessageBox.question(
            self,
            APP_TITLE,
            (
                f"Criar movimento de pagamento para '{subscription['name']}' "
                "agora com a data de hoje?"
            ),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        today_text = date.today().isoformat()
        if self.repo.subscription_occurrence_exists(subscription_id, today_text):
            QMessageBox.information(
                self,
                APP_TITLE,
                "Esta subscrição já tem um pagamento registado hoje.",
            )
            return
        next_due = self.payments_service.advance_subscription_due(
            subscription,
            date.today(),
        )
        try:
            created_ids = self.app.change_journal_service.run_with_created_transactions(
                lambda: [
                    self.repo.record_subscription_payment(
                        subscription_id,
                        today_text,
                        next_due.isoformat(),
                        commit=False,
                    )
                ],
                label=f"Pagamento de subscrição · {subscription['name']}",
                action_type="subscription_payment",
                state_targets={
                    "subscriptions": {
                        "ids": [subscription_id],
                        "columns": ("last_used_date", "next_renewal_date"),
                    }
                },
                delete_linked_occurrences=True,
            )
        except Exception as exc:
            from utils.app_logging import log_exception

            log_exception("Failed to record journaled subscription payment", exc)
            QMessageBox.critical(
                self,
                APP_TITLE,
                "Não foi possível registar o pagamento. Nenhum dado foi alterado.",
            )
            return
        transaction_id = created_ids[0] if created_ids else None
        if transaction_id is None:
            QMessageBox.information(
                self,
                APP_TITLE,
                "Esta subscrição já tem um pagamento registado hoje.",
            )
            return
        self.refresh()
        self.app.notify_data_changed(
            "Movimento criado com sucesso!",
            selected_tx_id=transaction_id,
        )

    def _select_id(self, subscription_id: int) -> None:
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item and item.text() == str(subscription_id):
                self.table.selectRow(row)
                self.table.scrollToItem(
                    item,
                    QAbstractItemView.PositionAtCenter,
                )
                break

    def toggle_selected(self) -> None:
        subscription_id = self.selected_id()
        if not subscription_id:
            QMessageBox.information(self, APP_TITLE, "Seleciona uma subscrição.")
            return
        row = self.repo.subscription_by_id(subscription_id)
        if not row:
            QMessageBox.warning(
                self,
                APP_TITLE,
                "Não encontrei a subscrição selecionada.",
            )
            self.refresh()
            return
        was_active = bool(int(row_get(row, "active", 1) or 0))
        sub_name = str(row_get(row, "name", "") or "Subscrição").strip()

        try:
            new_value = self.repo.toggle_subscription_active(subscription_id)
        except ValueError:
            QMessageBox.warning(
                self,
                APP_TITLE,
                "Não encontrei a subscrição selecionada.",
            )
            self.refresh()
            return

        if was_active and new_value == 0 and self.active_only.isChecked():
            msg = (
                f"ℹ️ A subscrição «{sub_name}» foi desativada. "
                "Ficou oculta pelo filtro «Mostrar só ativas» "
                "(desmarca o filtro para visualizares todas as subscrições)."
            )
        elif new_value == 1:
            msg = f"✅ A subscrição «{sub_name}» foi ativada com sucesso."
        else:
            msg = f"⏸️ A subscrição «{sub_name}» foi desativada."

        if hasattr(self, "status_label"):
            self.status_label.setText(msg)
        self.app.notify_data_changed(msg)
        self._select_id(subscription_id)
