from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import qInstallMessageHandler
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

from ui.core.theme import Theme
from ui.dialogs import (
    AccountBalanceDialog,
    BudgetAssistantDialog,
    BudgetEditDialog,
    GoalDialog,
    HoldingDialog,
    LiabilityDialog,
    MonthlyGoalDialog,
    PlannedPurchaseDialog,
    ScheduledPaymentDialog,
    SubscriptionDialog,
    TransferDialog,
)


class _MockRepo:
    def categories(self, kind="all"):
        return ["Alimentação", "Transporte", "Casa", "Lazer"]

    def financial_accounts(self):
        return [
            {
                "id": 1,
                "name": "Conta principal",
                "balance_known": 1,
                "reference_balance": 1500.0,
                "reference_date": "2026-08-01",
            },
            {
                "id": 2,
                "name": "Poupança",
                "balance_known": 1,
                "reference_balance": 5000.0,
                "reference_date": "2026-08-01",
            },
        ]

    def all_transactions(self):
        return []

    def month_period(self, year: int, month: int):
        return f"{year:04d}-{month:02d}-01", f"{year:04d}-{month:02d}-28", "2026-08"

    def aggregate_category_spending(self, start: str, end: str):
        return {}

    def totals_for_payment_period(self, start: str, end: str):
        return {"income": 2000.0, "expenses": 1200.0}

    def q(self, sql: str, *args):
        return [("Conta principal",), ("Poupança",)]

    def total_income_for_period(self, start: str, end: str):
        return 2000.0


def test_liquid_sheet_dialogs_populate_widgets_and_have_no_layout_warnings() -> None:
    _qapp = QApplication.instance() or QApplication([])
    host = QWidget()
    host.theme = Theme(dark=False)
    host.setStyleSheet(host.theme.stylesheet())
    host.resize(960, 640)
    host.show()

    repo = _MockRepo()
    qt_messages: list[str] = []

    def message_handler(_mode, _context, message) -> None:
        qt_messages.append(str(message))

    previous_handler = qInstallMessageHandler(message_handler)

    try:
        dialogs = [
            LiabilityDialog(host),
            LiabilityDialog(
                host,
                {
                    "name": "Crédito Carro",
                    "liability_type": "Crédito automóvel",
                    "account": "Banco",
                    "balance": 12000,
                    "interest_rate": 4.5,
                    "monthly_payment": 250,
                    "notes": "Teste",
                },
            ),
            ScheduledPaymentDialog(repo, host),
            MonthlyGoalDialog(repo, "2026-08", host),
            GoalDialog(repo, host),
            BudgetEditDialog("Alimentação", 400.0, host),
            SubscriptionDialog(repo, host),
            HoldingDialog(host),
            AccountBalanceDialog(repo, host),
            PlannedPurchaseDialog(host),
            BudgetAssistantDialog(repo, "2026-08", host),
            TransferDialog(repo, host),
        ]

        for dialog in dialogs:
            qt_messages.clear()
            dialog.show()
            QTest.qWait(50)

            # Ensure Qt did not reject any layout attempt
            assert not any(
                "Attempting to add QLayout" in message for message in qt_messages
            ), f"Layout warning in {type(dialog).__name__}: {qt_messages}"

            # Ensure sheet contains the content layout
            assert dialog.sheet_layout.count() >= 1, (
                f"Empty sheet in {type(dialog).__name__}"
            )

            # Ensure the sheet surface height is properly sized for form content
            assert dialog.sheet.height() > 80, (
                f"Sheet too short in {type(dialog).__name__}: {dialog.sheet.height()}px"
            )
            dialog.close()

    finally:
        qInstallMessageHandler(previous_handler)
