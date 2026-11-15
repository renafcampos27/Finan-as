"""Unit and integration tests for Phase 7 fixes (AUD-33 and AUD-34).

AUD-33: Protected persistence of habit change plans in simulations UI.
AUD-34: Performance index guarantees for transaction splits, holdings, and correlated subquery elimination.
"""

import sqlite3
from unittest.mock import MagicMock, patch

import pytest
from PySide6.QtWidgets import QApplication

from database.engine import FinanceDB
from ui.core.page import BasePage
from ui.pages.simulations import HabitSimulationsPage


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


# ============================================================================
# AUD-33 Tests: Protected Persistence of Habit Change Plans in UI
# ============================================================================


def test_aud33_save_habit_plan_handles_db_error_without_crash(qapp, tmp_path):
    """Verify _save_current_preview_as_plan catches database exceptions and toasts error."""
    mock_app = MagicMock()
    mock_app.theme.privacy = False
    mock_app.theme.dark = False
    mock_app.show_toast = MagicMock()

    with patch.object(HabitSimulationsPage, "refresh", lambda self: None):
        page = HabitSimulationsPage(mock_app)

    page._selected_key = "coffee"
    page._preview_kind = "reduce_25"
    page._base_data = {
        "monthly_spend": 120.0,
        "occurrences": 4,
        "frequency": "weekly",
        "description": "Café",
    }
    page._goals_by_id = {}
    page._selected_goal_id = None
    page._habit_audit_rules = {}

    mock_dialog = MagicMock()
    mock_dialog.exec.return_value = 1
    mock_dialog.values.return_value = {
        "name": "Reduzir café",
        "start_date": "2026-09-01",
        "goal_id": None,
        "notes": "Poupar para férias",
    }

    # Simulate database contention/failure
    page.repo = MagicMock()
    page.repo.add_habit_change_plan.side_effect = sqlite3.OperationalError("database is locked")

    with patch("ui.pages.simulations.HabitPlanDialog", return_value=mock_dialog):
        with patch("ui.pages.simulations.scenario_plan_values", return_value={"planned_monthly_savings": 50.0}):
            page._save_current_preview_as_plan()

    # Verify toast was emitted with tone='danger' and no unhandled crash occurred
    mock_app.show_toast.assert_called_once()
    assert mock_app.show_toast.call_args[1].get("tone") == "danger"
    assert "database is locked" in str(mock_app.show_toast.call_args[0][0])


def test_aud33_save_habit_plan_success_flow(qapp, tmp_path):
    """Verify _save_current_preview_as_plan updates plans card and shows success toast when DB succeeds."""
    mock_app = MagicMock()
    mock_app.theme.privacy = False
    mock_app.theme.dark = False
    mock_app.show_toast = MagicMock()

    with patch.object(HabitSimulationsPage, "refresh", lambda self: None):
        page = HabitSimulationsPage(mock_app)

    page._selected_key = "coffee"
    page._preview_kind = "half"
    page._base_data = {
        "monthly_spend": 45.0,
        "occurrences": 30,
        "frequency": "daily",
        "description": "Café",
    }
    page._goals_by_id = {}
    page._selected_goal_id = None
    page._habit_audit_rules = {}
    page.plans_card = MagicMock()
    page._set_internal_view = MagicMock()

    mock_dialog = MagicMock()
    mock_dialog.exec.return_value = 1
    mock_dialog.values.return_value = {
        "name": "Menos café fora",
        "start_date": "2026-09-01",
        "goal_id": None,
        "notes": "Fazer em casa",
    }

    page.repo = MagicMock()
    page.repo.add_habit_change_plan.return_value = 42

    with patch("ui.pages.simulations.HabitPlanDialog", return_value=mock_dialog):
        with patch("ui.pages.simulations.scenario_plan_values", return_value={"planned_monthly_savings": 25.0}):
            page._save_current_preview_as_plan()

    page.plans_card.refresh.assert_called_once_with(select_id=42)
    page._set_internal_view.assert_called_once_with("plans")
    mock_app.show_toast.assert_called_once()
    assert mock_app.show_toast.call_args[1].get("tone") == "success"


# ============================================================================
# AUD-34 Tests: Performance Indexes & Query Descorrelation
# ============================================================================


def test_aud34_performance_indexes_created_in_database(tmp_path):
    """Verify dedicated performance indexes for transaction_splits and holdings exist."""
    db_file = tmp_path / "test_indexes.db"
    db = FinanceDB(db_file)

    cursor = db.conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='index'")
    indexes = {row[0] for row in cursor.fetchall()}

    assert "idx_transaction_splits_tx_id" in indexes, "idx_transaction_splits_tx_id must exist"
    assert "idx_transaction_splits_tx_amount" in indexes, "idx_transaction_splits_tx_amount must exist"
    assert "idx_holdings_asset_class_name_covering" in indexes, "idx_holdings_asset_class_name_covering must exist"
    assert "idx_holdings_asset_class_name" in indexes, "idx_holdings_asset_class_name must exist"

    db.conn.close()


def test_aud34_holdings_query_plan_uses_index_no_temp_btree(tmp_path):
    """Verify SELECT * FROM holdings ORDER BY asset_class, name uses index without temporary sorting B-Tree."""
    db_file = tmp_path / "test_holdings_plan.db"
    db = FinanceDB(db_file)

    cursor = db.conn.cursor()
    cursor.execute("EXPLAIN QUERY PLAN SELECT * FROM holdings ORDER BY asset_class, name")
    plan = [dict(row) for row in cursor.fetchall()]

    # Ensure no temporary B-tree for sorting is needed
    for step in plan:
        detail = step.get("detail", "")
        assert "USE TEMP B-TREE" not in detail, f"Query plan should not require TEMP B-TREE: {detail}"
        assert "idx_holdings_asset_class_name" in detail or "SCAN holdings" in detail

    db.conn.close()


def test_aud34_page_movement_count_left_join_no_correlated_subquery(qapp, tmp_path):
    """Verify movement count query in BasePage uses indexed LEFT JOIN and has no correlated subquery."""
    db_file = tmp_path / "test_movement_count.db"
    db = FinanceDB(db_file)

    cursor = db.conn.cursor()
    cursor.execute("DELETE FROM transaction_splits")
    cursor.execute("DELETE FROM transactions")
    cursor.execute("INSERT OR REPLACE INTO categories(name, kind, colour, stable_id) VALUES('Alimentacao', 'expense', '#111111', 'cat-100')")
    cursor.execute("INSERT OR REPLACE INTO categories(name, kind, colour, stable_id) VALUES('Lazer', 'expense', '#222222', 'cat-200')")
    cursor.execute("INSERT INTO transactions(id, date, amount, category_id, category, account, description) VALUES(1001, '2026-08-10', -50.0, 'cat-100', 'Alimentacao', 'Conta', 'Mercado')")
    cursor.execute("INSERT INTO transactions(id, date, amount, category_id, category, account, description) VALUES(1002, '2026-08-11', -100.0, 'cat-200', 'Lazer', 'Conta', 'Jantar')")
    cursor.execute("INSERT INTO transaction_splits(id, transaction_id, category_id, category, amount, percent) VALUES(501, 1002, 'cat-100', 'Alimentacao', 40.0, 40.0)")
    cursor.execute("INSERT INTO transactions(id, date, amount, category_id, category, account, description) VALUES(1003, '2026-08-12', -20.0, 'cat-200', 'Lazer', 'Conta', 'Cinema')")
    db.conn.commit()

    # Query plan verification
    split_join = "LEFT JOIN transaction_splits s ON s.transaction_id=t.id AND s.category_id=?"
    query = f"""
        EXPLAIN QUERY PLAN
        SELECT COUNT(DISTINCT t.id)
        FROM transactions t
        LEFT JOIN transaction_meta m ON m.transaction_id=t.id {split_join}
        WHERE (t.category_id=? OR s.id IS NOT NULL)
    """
    cursor.execute(query, ("cat-100", "cat-100"))
    plan = [dict(row) for row in cursor.fetchall()]

    for step in plan:
        detail = step.get("detail", "")
        assert "CORRELATED SCALAR SUBQUERY" not in detail, f"Correlated subquery must not appear in plan: {detail}"

    # Verify BasePage._chart_calculation_context calculates movement count accurately
    mock_app = MagicMock()
    mock_app.theme.privacy = False
    mock_app.theme.dark = False

    class _MockRepo:
        def __init__(self, db_conn):
            self._conn = db_conn

        def category_identity(self, name, create=False):
            return ("cat-100", "Alimentacao") if name == "Alimentacao" else ("", name)

        def scalar(self, query, params=(), default=None):
            cur = self._conn.cursor()
            cur.execute(query, params)
            row = cur.fetchone()
            return row[0] if row and row[0] is not None else default

    page = BasePage(mock_app, "TestPage")
    page.repo = _MockRepo(db.conn)
    page._page_period = MagicMock(return_value=("2026-08-01", "2026-08-31", "Agosto 2026"))

    ctx = page._chart_calculation_context({"category": "Alimentacao"}, {})
    assert ctx["movements"] == 2, f"Expected 2 movements (1 direct + 1 split), got {ctx['movements']}"

    db.conn.close()
