#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Regression tests for dialogs batch 5 (ui/dialogs.py only):

- BUG-DLG-01 (P0): MonthlyGoalDialog.get_data() omitted the "ym" key; the caller
  ui/pages/planning.py reads data["ym"] and crashed with a fatal KeyError when
  creating a monthly goal.
- BUG-DLG-02 (P1): TransferDialog populated its account combos only from
  SELECT DISTINCT account FROM transactions; accounts without movements never
  appeared and the combos were not editable.
- BUG-DLG-04 (P1): TransactionDialog used the strict parse_date (requires
  exactly 10 chars) and crashed with ValueError when editing a row whose date
  is an ISO timestamp such as "2026-08-01T14:30:00".
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QDate
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox, QWidget

from ui.dialogs import MonthlyGoalDialog, TransactionDialog, TransferDialog


@pytest.fixture(scope="session")
def qapp():
    return QApplication.instance() or QApplication([])


class _MockRepo:
    """Repo stub exposing exactly the surface used by the dialogs under test."""

    def __init__(self) -> None:
        self._accounts: list[dict] = [
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
        self._distinct_sql_accounts: list[tuple] = []

    def set_distinct_sql_accounts(self, names: list[str]) -> None:
        self._distinct_sql_accounts = [(name,) for name in names]

    def categories(self, kind: str = "all") -> list[str]:
        return ["Alimentação", "Transporte", "Casa", "Lazer"]

    def category_kind(self, category: str) -> str:
        return "expense" if category in {"Alimentação", "Casa"} else "income"

    def subcategories(self, category: str) -> list[str]:
        return []

    def financial_accounts(self) -> list[dict]:
        return [dict(account) for account in self._accounts]

    def q(self, sql: str, *args) -> list[tuple]:
        if "SELECT DISTINCT account FROM transactions" in sql:
            return list(self._distinct_sql_accounts)
        return []

    def validate_transaction_category(self, category: str, amount: float) -> bool:
        return True

    def transaction_meta_ext(self, transaction_id: int) -> dict:
        return {}


# ==============================================================================
# BUG-DLG-01: MonthlyGoalDialog.get_data() must include "ym"
# ==============================================================================


def test_bugdlg01_monthly_goal_get_data_includes_ym(qapp) -> None:
    repo = _MockRepo()
    parent = QWidget()
    dialog = MonthlyGoalDialog(repo, "2026-08", parent)
    # ym is stored as a plain string attribute (not callable)
    assert dialog.ym == "2026-08"
    dialog.name.setText("Fundo de emergência")
    dialog.target.setValue(250.0)
    data = dialog.get_data()
    assert data["ym"] == "2026-08"
    assert data["name"] == "Fundo de emergência"
    assert data["goal_type"] == "Poupar pelo menos"
    assert data["category"] == ""
    assert data["target"] == 250.0
    assert data["notes"] == ""
    dialog.close()


def test_bugdlg01_monthly_goal_get_data_defaults_per_month(qapp) -> None:
    repo = _MockRepo()
    parent = QWidget()
    for ym in ("2026-08", "2026-09"):
        dialog = MonthlyGoalDialog(repo, ym, parent)
        data = dialog.get_data()
        assert data["ym"] == ym
        assert data["target"] == 0.0
        dialog.close()


# ==============================================================================
# BUG-DLG-02: TransferDialog must list accounts without movements
# ==============================================================================


def test_bugdlg02_transfer_dialog_lists_accountless_accounts(
    qapp, monkeypatch
) -> None:
    repo = _MockRepo()
    # Only "Conta principal" has movements; "Poupança" exists only in
    # financial_accounts and must still appear in both combos.
    repo.set_distinct_sql_accounts(["Conta principal"])
    parent = QWidget()
    dialog = TransferDialog(repo, parent)

    source_items = [dialog.source.itemText(i) for i in range(dialog.source.count())]
    dest_items = [dialog.dest.itemText(i) for i in range(dialog.dest.count())]
    assert "Poupança" in source_items
    assert "Poupança" in dest_items
    assert "Conta principal" in source_items
    assert source_items.count("Poupança") == 1
    assert dialog.source.isEditable()
    assert dialog.dest.isEditable()

    warnings_seen: list = []
    monkeypatch.setattr(
        QMessageBox, "warning", lambda *args: warnings_seen.append(args)
    )
    dialog.source.setCurrentText("Conta principal")
    dialog.dest.setCurrentText("Nova Conta Sem Movimentos")
    dialog.amount.setValue(15.0)
    dialog.accept()
    assert len(warnings_seen) == 0
    assert dialog.result() == QDialog.Accepted
    data = dialog.get_data()
    assert data["source"] == "Conta principal"
    assert data["dest"] == "Nova Conta Sem Movimentos"
    assert data["amount"] == 15.0
    dialog.close()


def test_bugdlg02_transfer_dialog_merges_sources_without_duplicates(qapp) -> None:
    repo = _MockRepo()
    repo.set_distinct_sql_accounts(["Conta principal", "Revolut"])
    parent = QWidget()
    dialog = TransferDialog(repo, parent)
    names = [dialog.source.itemText(i) for i in range(dialog.source.count())]
    # financial_accounts first, then SQL-only extras, deduplicated
    assert names == ["Conta principal", "Poupança", "Revolut"]
    assert dialog.dest.currentText() == "Poupança"
    dialog.close()


# ==============================================================================
# BUG-DLG-04: TransactionDialog must tolerate ISO timestamps in row["date"]
# ==============================================================================


def test_bugdlg04_transaction_dialog_accepts_iso_timestamp_row(qapp) -> None:
    repo = _MockRepo()
    parent = QWidget()
    row = {
        "id": 42,
        "date": "2026-08-01T14:30:00",
        "description": "Compra com timestamp ISO",
        "amount": -37.5,
        "category": "Alimentação",
        "account": "Conta principal",
        "notes": "",
    }
    dialog = TransactionDialog(repo, parent, row=row)
    assert dialog.payment_date.date() == QDate(2026, 8, 1)
    assert dialog.purchase_date.date() == QDate(2026, 8, 1)
    dialog.close()


def test_bugdlg04_transaction_dialog_keeps_plain_iso_dates(qapp) -> None:
    repo = _MockRepo()
    parent = QWidget()
    row = {
        "id": 43,
        "date": "2026-08-04",
        "description": "Movimento com data simples",
        "amount": -20.0,
        "category": "Alimentação",
        "account": "Conta principal",
        "notes": "",
    }
    dialog = TransactionDialog(repo, parent, row=row)
    assert dialog.payment_date.date() == QDate(2026, 8, 4)
    assert dialog.purchase_date.date() == QDate(2026, 8, 4)
    dialog.close()
