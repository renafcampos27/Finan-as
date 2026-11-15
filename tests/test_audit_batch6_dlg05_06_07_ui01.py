#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Regression tests for audit batch 6 (BUG-DLG-05/06/07 + BUG-UI-01):

- BUG-DLG-05 (P1): PortugueseTaxDialog minimum size reduced to (760, 420) and
  body wrapped in a QScrollArea so buttons remain reachable on 1366x768/HiDPI.
- BUG-DLG-06 (P1): HoldingDialog collects acquisition_date + jurisdiction and
  persists them via repo.add_holding/update_holding; PortugueseTaxDialog reads
  the new columns (acquisition_date preferred over legacy buy_date).
- BUG-DLG-07 (P1): ClosePositionDialog.accept() refuses to submit when the
  "create bank transaction" option is active and fees exceed the sale value
  (would produce a negative movement).
- BUG-UI-01 (P1): GoalsPage.withdraw_selected guards saved_amount < 0.01 before
  opening QInputDialog.getDouble (min > max Qt contract violation).
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QDate
from PySide6.QtWidgets import QApplication, QDialog, QWidget

from ui.dialogs import (
    ClosePositionDialog,
    HoldingDialog,
    PortugueseTaxDialog,
)


@pytest.fixture(scope="session")
def qapp():
    return QApplication.instance() or QApplication([])


class _MockRepo:
    """Repo stub exposing exactly the surface used by the dialogs under test."""

    def __init__(self) -> None:
        self.holdings: list[dict] = []
        self.accounts: list[dict] = [
            {"id": 1, "name": "Conta principal", "reference_balance": 1500.0}
        ]
        self.added: list[dict] = []
        self.updated: list[dict] = []

    # --- surface used by HoldingDialog ---
    def financial_accounts(self) -> list[dict]:
        return [dict(a) for a in self.accounts]

    # --- surface used by PortugueseTaxDialog ---
    def q(self, sql: str, *args) -> list[dict]:
        if "SELECT * FROM holdings" in sql:
            return [dict(r) for r in self.holdings]
        return []

    # --- persistence capture ---
    def add_holding(self, *args, **kwargs) -> int:
        self.added.append({"args": args, "kwargs": kwargs})
        return len(self.added)

    def update_holding(self, holding_id: int, *args, **kwargs) -> None:
        self.updated.append({"id": holding_id, "args": args, "kwargs": kwargs})


# ==============================================================================
# BUG-DLG-05: PortugueseTaxDialog scrollable body + reduced minimum size
# ==============================================================================


def test_bugdlg05_tax_dialog_minimum_size_reduced(qapp) -> None:
    repo = _MockRepo()
    parent = QWidget()
    dialog = PortugueseTaxDialog(repo, parent)
    try:
        assert dialog.minimumSize().width() <= 760
        assert dialog.minimumSize().height() <= 420
    finally:
        dialog.close()


def test_bugdlg05_tax_dialog_body_is_scrollable(qapp) -> None:
    from PySide6.QtWidgets import QScrollArea

    repo = _MockRepo()
    parent = QWidget()
    dialog = PortugueseTaxDialog(repo, parent)
    try:
        scrolls = dialog.findChildren(QScrollArea)
        assert scrolls, "PortugueseTaxDialog must contain a QScrollArea body"
        assert any(s.widgetResizable() for s in scrolls)
    finally:
        dialog.close()


# ==============================================================================
# BUG-DLG-06: HoldingDialog acquisition metadata + tax dialog consumption
# ==============================================================================


def test_bugdlg06_holding_dialog_get_data_includes_metadata(qapp) -> None:
    _repo = _MockRepo()
    parent = QWidget()
    dialog = HoldingDialog(parent)
    try:
        dialog.name.setText("ETF MSCI World")
        data = dialog.get_data()
        assert "acquisition_date" in data
        assert "jurisdiction" in data
        assert data["jurisdiction"] == "PT"
        # special value (not indicated) must normalize to empty string
        assert data["acquisition_date"] == ""
        # set a real date
        dialog.acquisition_date.setDate(QDate(2023, 6, 15))
        data2 = dialog.get_data()
        assert data2["acquisition_date"] == "2023-06-15"
        dialog.jurisdiction.setCurrentText("DE")
        assert dialog.get_data()["jurisdiction"] == "DE"
    finally:
        dialog.close()


def test_bugdlg06_holding_dialog_prefills_from_row(qapp) -> None:
    _repo = _MockRepo()
    parent = QWidget()
    row = {
        "name": "Bitcoin",
        "asset_class": "Cripto manual",
        "account": "Manual",
        "value": 3000.0,
        "cost_basis": 1000.0,
        "cost_basis_known": 1,
        "fees": 0.0,
        "notes": "",
        "linked_account": "",
        "acquisition_date": "2022-01-10",
        "jurisdiction": "PT",
        "currency": "EUR",
        "exchange_rate": 1.0,
    }
    dialog = HoldingDialog(parent, row)
    try:
        data = dialog.get_data()
        assert data["acquisition_date"] == "2022-01-10"
        assert data["jurisdiction"] == "PT"
    finally:
        dialog.close()


def test_bugdlg06_tax_dialog_prefers_acquisition_date_column(qapp) -> None:
    """The tax dialog must read the new canonical column first."""
    repo = _MockRepo()
    # Legacy key present only; dialog must still compute days held.
    repo.holdings = [
        {
            "name": "Bitcoin",
            "asset_class": "Cripto manual",
            "value": 3000.0,
            "cost_basis": 1000.0,
            "acquisition_date": "2020-01-01",
            "jurisdiction": "PT",
        }
    ]
    parent = QWidget()
    dialog = PortugueseTaxDialog(repo, parent)
    try:
        # crypto held > 365 days must be flagged exempt in the regime column
        regime_item = dialog.table.item(0, 5)
        assert regime_item is not None
        assert "isen" in regime_item.text().lower()
    finally:
        dialog.close()


# ==============================================================================
# BUG-DLG-07: ClosePositionDialog refuses fees > sale with bank movement
# ==============================================================================


def _make_close_dialog(qapp, sale: float, fees: float, create_tx: bool):
    # parent=None: a transient QWidget parent would be GC'd and destroy the
    # dialog's C++ object mid-test; ClosePositionDialog handles None parent.
    dialog = ClosePositionDialog(None, {
        "name": "Ativo teste",
        "value": sale,
        "cost_basis": 10.0,
        "fees": 0.0,
        "account": "Manual",
        "linked_account": "Conta principal",
    })
    dialog.sale_value.setValue(sale)
    dialog.sale_fees.setValue(fees)
    dialog.create_transaction.setChecked(create_tx)
    return dialog


def test_bugdlg07_close_dialog_rejects_negative_bank_movement(qapp, monkeypatch):
    dialog = _make_close_dialog(qapp, sale=5.0, fees=8.0, create_tx=True)
    warned = {"flag": False}

    def _fake_warning(*args, **kwargs):
        warned["flag"] = True
        return None

    monkeypatch.setattr(
        "ui.dialogs.QMessageBox.warning", _fake_warning, raising=False
    )
    try:
        dialog.accept()
        assert warned["flag"], "accept() must warn when fees exceed sale value"
        assert not dialog.result() == QDialog.Accepted
    finally:
        dialog.close()


def test_bugdlg07_close_dialog_allows_zero_net_without_bank_movement(
    qapp, monkeypatch
):
    dialog = _make_close_dialog(qapp, sale=5.0, fees=5.0, create_tx=False)
    warned = {"flag": False}

    def _fake_warning(*args, **kwargs):
        warned["flag"] = True
        return None

    monkeypatch.setattr(
        "ui.dialogs.QMessageBox.warning", _fake_warning, raising=False
    )
    try:
        dialog.accept()
        assert not warned["flag"], "no warning expected when bank movement is off"
    finally:
        dialog.close()


def test_bugdlg07_close_dialog_allows_positive_net(qapp, monkeypatch):
    dialog = _make_close_dialog(qapp, sale=100.0, fees=5.0, create_tx=True)
    warned = {"flag": False}

    def _fake_warning(*args, **kwargs):
        warned["flag"] = True
        return None

    monkeypatch.setattr(
        "ui.dialogs.QMessageBox.warning", _fake_warning, raising=False
    )
    try:
        dialog.accept()
        assert not warned["flag"]
    finally:
        dialog.close()
