# -*- coding: utf-8 -*-
"""
Test Suite for Batch 20 (P2 Defects: BUG-INV-03, BUG-DASH-01, BUG-DASH-02, BUG-SUB-01)
Phase 28 Validation.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from PySide6.QtCore import QPointF
from PySide6.QtGui import QImage, QPainter
from PySide6.QtWidgets import QApplication

from database.engine import FinanceDB
from database.repo import Repo
from services.payments import PaymentsService
from ui.components.charts.donut import DonutChart
from ui.core.theme import Theme
from ui.pages.dashboard import DashboardPage
from ui.pages.investments import PortfolioPage
from ui.pages.subscriptions import SubscriptionsPage


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture
def temp_db(tmp_path: Path):
    db_path = tmp_path / "test_batch20.db"
    db = FinanceDB(db_path)
    db.init_schema()
    return db


@pytest.fixture
def repo(temp_db):
    return Repo(temp_db)


@pytest.fixture
def mock_app(repo):
    app = MagicMock()
    app.theme = Theme()
    app.theme.privacy = False
    app.theme.dark = False
    app.page_density = "normal"
    app.repo = repo
    app.payments_service = PaymentsService(repo)
    app.background_task_registry = MagicMock()
    app.change_journal_service = MagicMock()
    app.document_service = MagicMock()
    return app


# ============================================================================
# BUG-INV-03: DonutChart ZeroDivisionError guard & empty_message customization
# ============================================================================


def test_inv_03_donut_tooltip_zero_total_safe(qapp):
    """Ensure _draw_tooltip handles zero total without ZeroDivisionError."""
    chart = DonutChart(lambda: Theme())
    chart.data = [("Ações", 0.0, "#2563EB")]
    chart.hover_index = 0
    chart._hover_pos = QPointF(100.0, 100.0)

    img = QImage(300, 300, QImage.Format_ARGB32_Premultiplied)
    painter = QPainter(img)
    try:
        # Calling _draw_tooltip with total=0.0 must not raise ZeroDivisionError
        chart._draw_tooltip(painter, total=0.0)
    finally:
        painter.end()


def test_inv_03_donut_custom_empty_message(qapp):
    """Ensure empty_message attribute can be customized and is passed to draw_empty."""
    chart = DonutChart(lambda: Theme())
    assert chart.empty_message == "Sem despesas neste período"
    chart.empty_message = "Sem investimentos registados na carteira"

    img = QImage(300, 300, QImage.Format_ARGB32_Premultiplied)
    painter = QPainter(img)
    try:
        with patch.object(chart, "draw_empty") as mock_draw_empty:
            chart.data = []
            chart.render_chart(painter)
            mock_draw_empty.assert_called_once_with(painter, "Sem investimentos registados na carteira")
    finally:
        painter.end()


def test_inv_03_investments_donut_has_custom_empty_message(qapp, mock_app):
    """Ensure PortfolioPage sets customized empty_message on investment_donut."""
    page = PortfolioPage(mock_app)
    assert hasattr(page, "investment_donut")
    assert page.investment_donut.empty_message == "Sem investimentos registados na carteira"


# ============================================================================
# BUG-DASH-01: Cold-start zero balance and expenses handling
# ============================================================================


def _create_mock_dashboard_data(*, balance=0.0, monthly_expense=0.0, income=0.0, expenses=0.0, reserve_data=None):
    return {
        "totals": {"income": income, "expenses": expenses, "savings": income - expenses},
        "totals_prev": {},
        "savings_rate": (income - expenses) / income * 100 if income else 0.0,
        "balance": balance,
        "net": balance,
        "monthly_expense": monthly_expense,
        "expense_structure": {},
        "emergency_reserve": reserve_data or {"available": False},
        "work_value": {"available": False},
        "income_context": {},
        "categories": [],
        "previous_categories": [],
        "forecast_data_context": {},
        "data_context": {},
        "forecast": [],
        "cashflow_pulse": [],
        "cashflow_pulse_previous": [],
        "savings_efficiency": [],
        "savings_efficiency_previous": [],
    }


def test_dash_01_cold_start_zero_balance_and_expenses(qapp, mock_app):
    """Verify that a newly created database with 0 balance and 0 expenses reports 'Sem saldo registado'."""
    page = DashboardPage(mock_app)
    data = _create_mock_dashboard_data(balance=0.0, monthly_expense=0.0)

    page._on_data_loaded(page._dashboard_request_id, data)

    balance_card = next(c for c in page.metric_widgets if c.title_label.text() == "Saldo atual")
    assert "Sem saldo registado" in balance_card.status_label.text()
    assert balance_card.tone == "neutral"
    assert balance_card.progress_bar.value() == 0


def test_dash_01_positive_and_negative_balance_states(qapp, mock_app):
    """Verify that positive balance without expenses and negative balance report correctly."""
    page = DashboardPage(mock_app)

    # Negative balance
    data_neg = _create_mock_dashboard_data(balance=-500.0, monthly_expense=200.0, income=100.0, expenses=200.0)
    page._on_data_loaded(page._dashboard_request_id, data_neg)
    balance_card_neg = next(c for c in page.metric_widgets if c.title_label.text() == "Saldo atual")
    assert "Saldo negativo" in balance_card_neg.status_label.text()
    assert balance_card_neg.tone == "negative"
    assert balance_card_neg.progress_bar.value() == 0

    # Positive balance without monthly expenses
    data_pos_no_exp = _create_mock_dashboard_data(balance=1500.0, monthly_expense=0.0, income=500.0, expenses=0.0)
    page._on_data_loaded(page._dashboard_request_id, data_pos_no_exp)
    balance_card_pos = next(c for c in page.metric_widgets if c.title_label.text() == "Saldo atual")
    assert "Saldo positivo" in balance_card_pos.status_label.text()
    assert balance_card_pos.tone == "positive"
    assert balance_card_pos.progress_bar.value() == 100


# ============================================================================
# BUG-DASH-02: Emergency reserve card without compound truncation
# ============================================================================


def test_dash_02_emergency_reserve_value_not_compound_truncated(qapp, mock_app):
    """Verify that reserve_value shows clean current balance and target is in reserve_delta."""
    page = DashboardPage(mock_app)

    reserve_data = {
        "available": True,
        "source": "goal",
        "goal_name": "Fundo de emergência",
        "current": 1500.0,
        "target": 10000.0,
        "coverage_months": 1.5,
        "status_key": "building",
        "missing": 8500.0,
        "above": 0.0,
        "tone": "warning",
        "progress": 0.15,
    }

    data_reserve = _create_mock_dashboard_data(
        balance=5000.0,
        monthly_expense=1000.0,
        income=2000.0,
        expenses=1000.0,
        reserve_data=reserve_data,
    )
    page._on_data_loaded(page._dashboard_request_id, data_reserve)

    reserve_card = next(c for c in page.metric_widgets if c.title_label.text() == "Reserva de emergência")
    # reserve_value must NOT contain "/"
    assert "/" not in reserve_card.value_label.text()
    assert "1 500" in reserve_card.value_label.text()
    # reserve_delta must contain the target
    assert "Meta:" in reserve_card.delta_label.text()
    assert "10 000" in reserve_card.delta_label.text()
    assert "1.5 meses cobertos" in reserve_card.delta_label.text()


# ============================================================================
# BUG-SUB-01: SubscriptionsPage toggle with active_only filter feedback
# ============================================================================


def test_sub_01_toggle_subscription_active_only_filter_notice(qapp, mock_app, repo):
    """Verify that deactivating a subscription when active_only is checked notifies user clearly."""
    # Create an active subscription
    sid = repo.add_subscription(
        name="Netflix Premium",
        amount=17.99,
        category="Lazer",
        renewal_day=1,
        period="Mensal",
        active=1,
    )

    page = SubscriptionsPage(mock_app)
    assert hasattr(page, "status_label")
    page.active_only.setChecked(True)

    # Mock selected_id to return sid
    with patch.object(page, "selected_id", return_value=sid):
        page.toggle_selected()

    # Subscription is now inactive (active=0)
    sub_after = repo.subscription_by_id(sid)
    assert sub_after["active"] == 0

    # Status label and notify_data_changed must have received the clear filter notification
    expected_msg = (
        "ℹ️ A subscrição «Netflix Premium» foi desativada. "
        "Ficou oculta pelo filtro «Mostrar só ativas» "
        "(desmarca o filtro para visualizares todas as subscrições)."
    )
    assert page.status_label.text() == expected_msg
    mock_app.notify_data_changed.assert_called_with(expected_msg)


def test_sub_01_toggle_subscription_reactivation_notice(qapp, mock_app, repo):
    """Verify that activating an inactive subscription provides clear activation feedback."""
    sid = repo.add_subscription(
        name="Spotify Family",
        amount=13.99,
        category="Música",
        renewal_day=1,
        period="Mensal",
        active=0,
    )

    page = SubscriptionsPage(mock_app)
    page.active_only.setChecked(False)

    with patch.object(page, "selected_id", return_value=sid):
        page.toggle_selected()

    sub_after = repo.subscription_by_id(sid)
    assert sub_after["active"] == 1

    expected_msg = "✅ A subscrição «Spotify Family» foi ativada com sucesso."
    assert page.status_label.text() == expected_msg
    mock_app.notify_data_changed.assert_called_with(expected_msg)
