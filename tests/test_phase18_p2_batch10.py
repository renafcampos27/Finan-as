#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tests for Phase 18 - Batch 10 P2 Defect Resolutions:
- LEAK-03: Fuga de Memória em Layouts Dinâmicos por Não Eliminação Recursiva
- BUG-DLG-ZERO-AMOUNT-ACCEPTANCE-003: Aceitação de Metas e Orçamentos com Valor 0,00 €
- BUG-DLG-UNSTRIPPED-INPUTS-004: Nomes de Contas e Categorias Gravados com Espaços Acidentais
- BUG-DLG-BUDGET-ASSISTANT-NONE-005: AttributeError e Divisão Estática no Assistente 50/30/20
"""

from __future__ import annotations

import os
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ui.core.layouts import clear_layout_recursive
from ui.dialogs import (
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
from ui.pages.planned_purchases import PlanningPage


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


class MockRepo:
    def __init__(self):
        self._categories = ["Alimentação", "Habitação", "Transportes", "Lazer", "Restaurantes", "Saúde"]

    def categories(self, kind="all"):
        return list(self._categories)

    def category_rows(self, kind="expense"):
        return [
            {"id": 1, "name": "Alimentação", "kind": "expense", "budget_bucket": "Necessidades"},
            {"id": 2, "name": "Habitação", "kind": "expense", "budget_bucket": "Necessidades"},
            {"id": 3, "name": "Transportes", "kind": "expense", "budget_bucket": "Necessidades"},
            {"id": 4, "name": "Saúde", "kind": "expense", "budget_bucket": "Necessidades"},
            {"id": 5, "name": "Lazer", "kind": "expense", "budget_bucket": "Desejos"},
            {"id": 6, "name": "Restaurantes", "kind": "expense", "budget_bucket": "Desejos"},
        ]

    def financial_accounts(self):
        return [
            {"id": 1, "name": "Conta Principal", "balance_known": 1, "reference_balance": 1500.0},
            {"id": 2, "name": "Conta Poupança", "balance_known": 1, "reference_balance": 5000.0},
        ]

    def all_transactions(self):
        return []

    def q(self, sql: str, params: tuple = ()):
        return [("Conta Principal",), ("Conta Poupança",)]

    def month_period(self, year: int, month: int):
        return f"{year:04d}-{month:02d}-01", f"{year:04d}-{month:02d}-28", f"{year:04d}-{month:02d}"

    def aggregate_category_spending(self, start: str, end: str):
        return {}

    def totals_for_payment_period(self, start: str, end: str):
        return {"income": 2000.0, "expenses": 1200.0}

    def total_income(self, start: str, end: str):
        return 2000.0


# ==============================================================================
# LEAK-03: Recursively Cleared Dynamic Layouts
# ==============================================================================


def test_clear_layout_recursive_deletes_nested_widgets_and_layouts(qapp):
    root_widget = QWidget()
    root_layout = QVBoxLayout(root_widget)

    top_label = QLabel("Top")
    root_layout.addWidget(top_label)

    nested_h = QHBoxLayout()
    nested_label_1 = QLabel("Child 1")
    nested_label_2 = QLabel("Child 2")
    nested_h.addWidget(nested_label_1)
    nested_h.addWidget(nested_label_2)
    root_layout.addLayout(nested_h)

    nested_grid = QGridLayout()
    grid_label = QLabel("Grid Child")
    nested_grid.addWidget(grid_label, 0, 0)
    nested_h.addLayout(nested_grid)

    assert root_layout.count() == 2
    assert nested_h.count() == 3

    clear_layout_recursive(root_layout)

    assert root_layout.count() == 0
    assert top_label.parent() is None
    assert nested_label_1.parent() is None
    assert nested_label_2.parent() is None
    assert grid_label.parent() is None


def test_planned_purchases_clear_delegates_to_clear_layout_recursive(qapp):
    root_widget = QWidget()
    root_layout = QVBoxLayout(root_widget)
    label = QLabel("Test")
    root_layout.addWidget(label)
    assert root_layout.count() == 1

    PlanningPage.clear(root_layout)
    assert root_layout.count() == 0
    assert label.parent() is None


# ==============================================================================
# BUG-DLG-ZERO-AMOUNT-ACCEPTANCE-003: Rejection of 0.00 Amounts
# ==============================================================================


def test_scheduled_payment_dialog_rejects_zero_amount(qapp):
    repo = MockRepo()
    parent = QWidget()
    dialog = ScheduledPaymentDialog(repo, parent)
    dialog.name.setText("Gás")
    dialog.amount.setValue(0.0)

    with patch("ui.dialogs.QMessageBox.warning") as mock_warn:
        dialog.accept()
        assert mock_warn.called
        assert dialog.result() != QDialog.Accepted

    dialog.amount.setValue(45.50)
    dialog.accept()
    assert dialog.result() == QDialog.Accepted


def test_monthly_goal_dialog_rejects_zero_amount(qapp):
    repo = MockRepo()
    parent = QWidget()
    dialog = MonthlyGoalDialog(repo, "2026-09", parent)
    dialog.name.setText("Poupança Férias")
    dialog.target.setValue(0.0)

    with patch("ui.dialogs.QMessageBox.warning") as mock_warn:
        dialog.accept()
        assert mock_warn.called
        assert dialog.result() != QDialog.Accepted

    dialog.target.setValue(100.0)
    dialog.accept()
    assert dialog.result() == QDialog.Accepted


def test_goal_dialog_rejects_zero_amount(qapp):
    repo = MockRepo()
    parent = QWidget()
    dialog = GoalDialog(repo, parent)
    dialog.name.setText("Reserva de Emergência")
    dialog.target.setValue(0.0)

    with patch("ui.dialogs.QMessageBox.warning") as mock_warn:
        dialog.accept()
        assert mock_warn.called
        assert dialog.result() != QDialog.Accepted

    dialog.target.setValue(5000.0)
    dialog.accept()
    assert dialog.result() == QDialog.Accepted


def test_budget_edit_dialog_accepts_zero_amount_as_removal(qapp):
    # BUG-DLG-08 (audit batch 7): 0,00 € passou a ser válido e significa
    # "remover o orçamento"; apenas montantes negativos são rejeitados.
    parent = QWidget()
    dialog = BudgetEditDialog("Alimentação", 0.0, parent)
    dialog.value.setValue(0.0)

    with patch("ui.dialogs.QMessageBox.warning") as mock_warn:
        dialog.accept()
        assert not mock_warn.called
        assert dialog.result() == QDialog.Accepted

    dialog.value.setValue(350.0)
    dialog.accept()
    assert dialog.result() == QDialog.Accepted


def test_subscription_dialog_rejects_zero_amount(qapp):
    repo = MockRepo()
    parent = QWidget()
    dialog = SubscriptionDialog(repo, parent)
    dialog.name.setText("Spotify")
    dialog.amount.setValue(0.0)

    with patch("ui.dialogs.QMessageBox.warning") as mock_warn:
        dialog.accept()
        assert mock_warn.called
        assert dialog.result() != QDialog.Accepted

    dialog.amount.setValue(10.99)
    dialog.accept()
    assert dialog.result() == QDialog.Accepted


def test_liability_dialog_rejects_zero_amount(qapp):
    parent = QWidget()
    dialog = LiabilityDialog(parent)
    dialog.name.setText("Crédito Automóvel")
    dialog.balance.setValue(0.0)

    with patch("ui.dialogs.QMessageBox.warning") as mock_warn:
        dialog.accept()
        assert mock_warn.called
        assert dialog.result() != QDialog.Accepted

    dialog.balance.setValue(8500.0)
    dialog.accept()
    assert dialog.result() == QDialog.Accepted


def test_planned_purchase_dialog_rejects_zero_amount(qapp):
    parent = QWidget()
    dialog = PlannedPurchaseDialog(parent)
    dialog.name.setText("Computador Novo")
    dialog.target.setValue(0.0)

    with patch("ui.dialogs.QMessageBox.warning") as mock_warn:
        dialog.accept()
        assert mock_warn.called
        assert dialog.result() != QDialog.Accepted

    dialog.target.setValue(1200.0)
    dialog.accept()
    assert dialog.result() == QDialog.Accepted


def test_transfer_dialog_rejects_zero_amount(qapp):
    repo = MockRepo()
    parent = QWidget()
    dialog = TransferDialog(repo, parent)
    dialog.amount.setValue(0.0)

    with patch("ui.dialogs.QMessageBox.warning") as mock_warn:
        dialog.accept()
        assert mock_warn.called
        assert dialog.result() != QDialog.Accepted

    dialog.amount.setValue(150.0)
    dialog.accept()
    assert dialog.result() == QDialog.Accepted


# ==============================================================================
# BUG-DLG-UNSTRIPPED-INPUTS-004: Sanitization of Whitespace in Inputs
# ==============================================================================


def test_dialogs_strip_whitespace_in_get_data(qapp):
    repo = MockRepo()
    parent = QWidget()

    # ScheduledPaymentDialog
    spd = ScheduledPaymentDialog(repo, parent)
    spd.name.setText("  Seguro Auto  ")
    spd.subcategory.setText("  Allianz  ")
    spd.account.setText("  Conta BCP  ")
    spd.amount.setValue(250.0)
    spd_data = spd.get_data()
    assert spd_data["name"] == "Seguro Auto"
    assert spd_data["subcategory"] == "Allianz"
    assert spd_data["account"] == "Conta BCP"

    # MonthlyGoalDialog
    mgd = MonthlyGoalDialog(repo, "2026-09", parent)
    mgd.name.setText("  Poupar Mais  ")
    mgd.target.setValue(75.0)
    mgd_data = mgd.get_data()
    assert mgd_data["name"] == "Poupar Mais"

    # GoalDialog
    gd = GoalDialog(repo, parent)
    gd.name.setText("  Férias Japão  ")
    gd.target.setValue(3000.0)
    gd_data = gd.get_data()
    assert gd_data["name"] == "Férias Japão"

    # BudgetEditDialog
    bed = BudgetEditDialog("  Lazer  ", 50.0, parent)
    bed.value.setValue(100.0)
    bed_data = bed.get_data()
    assert bed_data["category"] == "Lazer"
    assert bed_data["budget"] == 100.0
    assert bed_data["amount"] == 100.0

    # SubscriptionDialog
    sd = SubscriptionDialog(repo, parent)
    sd.name.setText("  Netflix 4K  ")
    sd.aliases.setText("  netflix, streaming  ")
    sd.amount.setValue(17.99)
    sd_data = sd.get_data()
    assert sd_data["name"] == "Netflix 4K"
    assert sd_data["aliases"] == "netflix, streaming"

    # HoldingDialog
    hd = HoldingDialog(parent)
    hd.name.setText("  VWCE ETF  ")
    hd.account.setText("  Degiro  ")
    hd.value.setValue(1000.0)
    hd_data = hd.get_data()
    assert hd_data["name"] == "VWCE ETF"
    assert hd_data["account"] == "Degiro"

    # LiabilityDialog
    ld = LiabilityDialog(parent)
    ld.name.setText("  Cartão de Crédito  ")
    ld.account.setText("  Santander  ")
    ld.balance.setValue(500.0)
    ld_data = ld.get_data()
    assert ld_data["name"] == "Cartão de Crédito"
    assert ld_data["account"] == "Santander"

    # PlannedPurchaseDialog
    ppd = PlannedPurchaseDialog(parent)
    ppd.name.setText("  Smartphone  ")
    ppd.target.setValue(600.0)
    ppd_data = ppd.get_data()
    assert ppd_data["name"] == "Smartphone"

    # TransferDialog
    td = TransferDialog(repo, parent)
    td.source.setCurrentText("  Conta Principal  ")
    td.dest.setCurrentText("  Conta Poupança  ")
    td_data = td.extended_data()
    assert td_data["source"] == "Conta Principal"
    assert td_data["dest"] == "Conta Poupança"
    assert td.get_data() == td_data


# ==============================================================================
# BUG-DLG-BUDGET-ASSISTANT-NONE-005: 50/30/20 Assistant Proportions & None-Safety
# ==============================================================================


def test_budget_assistant_zero_income_warning(qapp):
    repo = MockRepo()
    parent = QWidget()
    dialog = BudgetAssistantDialog(repo, "2026-09", parent)
    dialog.income_base.setValue(0.0)

    with patch("ui.dialogs.QMessageBox.warning") as mock_warn:
        dialog.apply_503020()
        assert mock_warn.called


def test_budget_assistant_dynamic_allocation_proportions(qapp):
    repo = MockRepo()
    parent = QWidget()
    dialog = BudgetAssistantDialog(repo, "2026-09", parent)
    dialog.income_base.setValue(2000.0)

    dialog.apply_503020()

    plan = dict(dialog.get_plan())
    assert len(plan) == 6

    # 50% of 2000.0 is 1000.0. Needs categories count = 4 (Alimentação, Habitação, Transportes, Saúde).
    # Expected each = 1000.0 / 4 = 250.00 €
    assert plan["Alimentação"] == 250.0
    assert plan["Habitação"] == 250.0
    assert plan["Transportes"] == 250.0
    assert plan["Saúde"] == 250.0

    # 30% of 2000.0 is 600.0. Wants categories count = 2 (Lazer, Restaurantes).
    # Expected each = 600.0 / 2 = 300.00 € (not static / 3 = 200.00 €)
    assert plan["Lazer"] == 300.0
    assert plan["Restaurantes"] == 300.0


def test_budget_assistant_get_plan_none_safe(qapp):
    repo = MockRepo()
    parent = QWidget()
    dialog = BudgetAssistantDialog(repo, "2026-09", parent)

    # Insert a blank/None row in the table
    dialog.table.setRowCount(2)
    dialog.table.setItem(0, 0, None)
    dialog.table.setItem(0, 1, None)

    # Valid row
    dialog.table.setItem(1, 0, QTableWidgetItem("  Educação  "))
    dialog.table.setItem(1, 1, QTableWidgetItem("125,50 €"))

    # get_plan must handle None without AttributeError
    plan = dict(dialog.get_plan())
    assert len(plan) == 1
    assert "Educação" in plan
    assert plan["Educação"] == 125.50
