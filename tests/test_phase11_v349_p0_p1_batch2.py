# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import sqlite3
import tempfile
from datetime import date, datetime
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QDialog, QMessageBox, QWidget

from database.engine import FinanceDB
from services.finance_math import _coerce_date, xirr
from ui.dialogs import (
    GoalDialog,
    HoldingDialog,
    LiabilityDialog,
    MonthlyGoalDialog,
    PlannedPurchaseDialog,
    ScheduledPaymentDialog,
    SubscriptionDialog,
    TransferDialog,
)


# ==============================================================================
# Mocks & Fixtures
# ==============================================================================

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


@pytest.fixture(scope="session")
def qapp():
    return QApplication.instance() or QApplication([])


# ==============================================================================
# 1. Tests for AUD-MATH-01: Heterogeneous Dates in XIRR & Date Coercion
# ==============================================================================

def test_coerce_date_various_inputs():
    """Verify _coerce_date handles datetime, date, strings, and bad inputs."""
    d = date(2026, 8, 15)
    dt = datetime(2026, 8, 15, 14, 30, 0)

    assert _coerce_date(d) == d
    assert _coerce_date(dt) == d
    assert _coerce_date("2026-08-15") == d
    assert _coerce_date("2026-08-15T12:00:00Z") == d
    assert _coerce_date("invalid-date") is None
    assert _coerce_date(None) is None
    assert _coerce_date(12345) is None


def test_xirr_mixed_date_types_no_crash():
    """Verify xirr does not raise TypeError when given mixed date types."""
    flows = [
        (datetime(2025, 1, 1, 10, 0), -1000.0),
        (date(2025, 7, 1), -500.0),
        ("2026-01-01", 1700.0),
    ]

    rate = xirr(flows)
    assert rate is not None
    assert 0.05 < rate < 0.30  # Should be approximately 15% - 20% annualized return


def test_xirr_unsorted_mixed_dates():
    """Verify that sorting works correctly with mixed date representations."""
    flows = [
        ("2026-01-01", 1200.0),
        (datetime(2025, 1, 1, 9, 30), -1000.0),
    ]

    rate = xirr(flows)
    assert rate is not None
    assert pytest.approx(rate, abs=0.01) == 0.20


# ==============================================================================
# 2. Tests for BUG-DLG-EMPTY-NAME-DATA-LOSS-001: Modal Dialogs Empty Name Validation
# ==============================================================================

def test_dialogs_reject_empty_name_without_closing(qapp, monkeypatch):
    """Verify that modal dialogs do not accept/close when required name is empty."""
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: warnings.append(args))

    repo = _MockRepo()
    parent = QWidget()

    dialog_classes_and_instances = [
        ScheduledPaymentDialog(repo, parent),
        MonthlyGoalDialog(repo, "2026-08", parent),
        GoalDialog(repo, parent),
        SubscriptionDialog(repo, parent),
        HoldingDialog(parent),
        LiabilityDialog(parent),
        PlannedPurchaseDialog(parent),
    ]

    for dlg in dialog_classes_and_instances:
        warnings.clear()
        dlg.name.setText("   ")  # Whitespace only
        dlg.accept()

        assert len(warnings) == 1, f"Expected warning for {type(dlg).__name__}"
        assert dlg.result() != QDialog.Accepted, f"{type(dlg).__name__} closed on empty name"

        # Now provide a valid name (and amount where the dialog validates it)
        # and ensure accept() succeeds. ScheduledPaymentDialog gained an
        # amount > 0 guard together with the empty-name guard
        # (BUG-DLG-EMPTY-NAME-DATA-LOSS-001), so a valid amount is part of
        # the accept contract.
        dlg.name.setText("Nome Válido")
        amount_widget = getattr(dlg, "amount", None)
        if amount_widget is not None and hasattr(amount_widget, "setValue"):
            amount_widget.setValue(50.0)
        target_widget = getattr(dlg, "target", None)
        if target_widget is not None and hasattr(target_widget, "setValue"):
            target_widget.setValue(100.0)
        value_widget = getattr(dlg, "value", None)
        if value_widget is not None and hasattr(value_widget, "setValue"):
            # HoldingDialog asks a confirmation question for a zero market
            # value; a positive value keeps the accept path unambiguous.
            value_widget.setValue(250.0)
        cost_widget = getattr(dlg, "cost", None)
        cost_known = getattr(dlg, "cost_known", None)
        if cost_widget is not None and hasattr(cost_widget, "setValue"):
            # HoldingDialog defaults cost_known=True; a zero cost then trips
            # the "indica um valor superior a zero" guard.
            cost_widget.setValue(200.0)
        if cost_known is not None and hasattr(cost_known, "setChecked"):
            cost_known.setChecked(True)
        balance_widget = getattr(dlg, "balance", None)
        if balance_widget is not None and hasattr(balance_widget, "setValue"):
            balance_widget.setValue(1000.0)
        dlg.accept()
        assert dlg.result() == QDialog.Accepted, f"{type(dlg).__name__} failed to accept valid name"


# ==============================================================================
# 3. Tests for BUG-DLG-TRANSFER-SAME-ACCOUNT-001: TransferDialog Validation
# ==============================================================================

def test_transfer_dialog_rejects_same_account(qapp, monkeypatch):
    """Verify TransferDialog does not accept transfer when source == dest."""
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: warnings.append(args))

    repo = _MockRepo()
    parent = QWidget()
    dlg = TransferDialog(repo, parent)

    # Set same account
    dlg.source.setCurrentText("Conta principal")
    dlg.dest.setCurrentText("Conta principal")
    dlg.amount.setValue(100.0)

    dlg.accept()
    assert len(warnings) == 1
    assert "diferente" in str(warnings[0][2]).lower()
    assert dlg.result() != QDialog.Accepted


def test_transfer_dialog_rejects_zero_or_negative_amount(qapp, monkeypatch):
    """Verify TransferDialog does not accept non-positive transfer amounts."""
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: warnings.append(args))

    repo = _MockRepo()
    parent = QWidget()
    dlg = TransferDialog(repo, parent)

    dlg.source.setCurrentText("Conta principal")
    dlg.dest.setCurrentText("Poupança")
    dlg.amount.setValue(0.0)

    dlg.accept()
    assert len(warnings) == 1
    assert "superior a 0" in str(warnings[0][2]).lower()
    assert dlg.result() != QDialog.Accepted


def test_transfer_dialog_accepts_valid_transfer(qapp, monkeypatch):
    """Verify TransferDialog accepts valid distinct accounts and positive amount."""
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: warnings.append(args))

    repo = _MockRepo()
    parent = QWidget()
    dlg = TransferDialog(repo, parent)

    dlg.source.setCurrentText("Conta principal")
    dlg.dest.setCurrentText("Poupança")
    dlg.amount.setValue(250.0)

    dlg.accept()
    assert len(warnings) == 0
    assert dlg.result() == QDialog.Accepted
    data = dlg.extended_data()
    assert data["amount"] == 250.0
    assert data["source"] == "Conta principal"
    assert data["dest"] == "Poupança"


# ==============================================================================
# 4. Tests for AUD-DB-02: Category Deletion Protection & Orphan Rules Cleanup
# ==============================================================================

def test_category_delete_protection_with_transactions():
    """Verify that deleting a category with associated transactions raises an SQLite ABORT."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_aud_db_02.db"
        engine = FinanceDB(db_path)
        try:
            # Confirm trigger exists
            triggers = {
                str(r["name"])
                for r in engine.query("SELECT name FROM sqlite_master WHERE type='trigger'")
            }
            assert "trg_categories_cleanup_before_delete" in triggers

            # Insert test category and associated transaction
            engine.conn.execute(
                "INSERT INTO categories(name, kind, colour, stable_id) "
                "VALUES('CatComTx', 'expense', '#FF5500', 'cat_com_tx')"
            )
            engine.conn.execute(
                "INSERT INTO transactions(date, description, amount, category, category_id, account) "
                "VALUES('2026-08-15', 'Jantar', -45.0, 'CatComTx', 'cat_com_tx', 'Conta principal')"
            )
            engine.conn.commit()

            # Attempting to delete category should fail with SQLite error
            with pytest.raises(sqlite3.DatabaseError) as exc_info:
                engine.conn.execute("DELETE FROM categories WHERE name='CatComTx'")

            assert "Não é possível eliminar uma categoria com transações associadas" in str(exc_info.value)
        finally:
            engine.conn.close()


def test_category_delete_protection_with_splits():
    """Verify that deleting a category with associated transaction_splits raises an SQLite ABORT."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_aud_db_02_splits.db"
        engine = FinanceDB(db_path)
        try:
            # Insert test categories
            engine.conn.execute(
                "INSERT INTO categories(name, kind, colour, stable_id) "
                "VALUES('CatComSplitPai', 'expense', '#00AA00', 'cat_split_pai')"
            )
            engine.conn.execute(
                "INSERT INTO categories(name, kind, colour, stable_id) "
                "VALUES('CatComSplitFilho', 'expense', '#0000AA', 'cat_split_filho')"
            )
            cur = engine.conn.execute(
                "INSERT INTO transactions(date, description, amount, category, category_id, account) "
                "VALUES('2026-08-15', 'Compras', -100.0, 'CatComSplitPai', 'cat_split_pai', 'Conta principal')"
            )
            tx_id = cur.lastrowid
            engine.conn.execute(
                "INSERT INTO transaction_splits(transaction_id, category, category_id, percent, amount) "
                "VALUES(?, 'CatComSplitFilho', 'cat_split_filho', 50.0, 50.0)",
                (tx_id,),
            )
            engine.conn.commit()

            # Attempting to delete category 'CatComSplitFilho' which only appears in splits should fail
            with pytest.raises(sqlite3.DatabaseError) as exc_info:
                engine.conn.execute("DELETE FROM categories WHERE name='CatComSplitFilho'")

            assert "Não é possível eliminar uma categoria com transações associadas" in str(exc_info.value)
        finally:
            engine.conn.close()


def test_category_delete_cleans_rules_and_updates_goals():
    """Verify that deleting an unused category deletes its category_rules and updates monthly_goals."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_aud_db_02_rules.db"
        engine = FinanceDB(db_path)
        try:
            # Create category with no transactions
            engine.conn.execute(
                "INSERT INTO categories(name, kind, colour, stable_id) "
                "VALUES('AntigaSemMovimentos', 'expense', '#888888', 'cat_antiga')"
            )
            # Add a rule pointing to this category
            engine.conn.execute(
                "INSERT INTO category_rules(keyword, category, category_id) "
                "VALUES('velho_keyword', 'AntigaSemMovimentos', 'cat_antiga')"
            )
            # Add a goal pointing to this category
            engine.conn.execute(
                "INSERT INTO monthly_goals(month, name, goal_type, target_value, category, category_id) "
                "VALUES('2026-08', 'Meta Antiga', 'Gastar no máximo', 100.0, 'AntigaSemMovimentos', 'cat_antiga')"
            )
            engine.conn.commit()

            # Delete the category
            engine.conn.execute("DELETE FROM categories WHERE name='AntigaSemMovimentos'")
            engine.conn.commit()

            # 1. Rule should be deleted
            rule = engine.conn.execute(
                "SELECT 1 FROM category_rules WHERE keyword='velho_keyword'"
            ).fetchone()
            assert rule is None, "Category rule was not cleaned up on category delete"

            # 2. Goal should be updated to 'Diversos' (with stable_id resolved by trigger)
            goal = engine.conn.execute(
                "SELECT category, category_id FROM monthly_goals WHERE name='Meta Antiga'"
            ).fetchone()
            assert goal is not None
            assert goal["category"] == "Diversos"
            diversos_id = engine.conn.execute(
                "SELECT stable_id FROM categories WHERE name='Diversos'"
            ).fetchone()["stable_id"]
            assert goal["category_id"] == diversos_id
        finally:
            engine.conn.close()
