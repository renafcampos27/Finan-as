#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tests for Phase 20 - Batch 12 P2 Defect Resolutions:
- BUG-CHART-WEALTH-BRIDGE-COLLISION-004: Colisão entre legenda e indicador de cobertura em resoluções compactas
- CASH-03: Subscrições inativas distorciam a média móvel de gastos discricionários futuros
- HEALTH-02: Score de saúde financeira colapsava para utilizadores sem rendimento ativo salarial (FIRE / reformados)
- TIME-03: Agregação anual omitia meses sem transações gerando KeyError
"""

from __future__ import annotations

import os
from datetime import date
from unittest.mock import MagicMock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtGui import QImage, QPainter
from PySide6.QtWidgets import QApplication

from database.engine import FinanceDB
from database.repo import Repo
from services.cashflow_forecast import CashflowForecastService
from services.financial_health import FinancialHealthService
from ui.components.charts.wealth_bridge import NetWorthBridgeChart


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


class DummyTheme:
    def __init__(self, dark: bool = True, privacy: bool = False):
        self.dark = dark
        self.privacy = privacy
        self.muted = "#8b949e"
        self.text = "#c9d1d9"
        self.background = "#0d1117"
        self.card_bg = "#161b22"
        self.border = "#30363d"
        self.positive = "#2ea043"
        self.accent = "#58a6ff"


# ============================================================================
# 1. BUG-CHART-WEALTH-BRIDGE-COLLISION-004 Tests
# ============================================================================


def test_wealth_bridge_plot_rect_geometry(qapp):
    """_plot_rect() should adjust top_pad based on chart width to prevent legend collision."""
    theme = DummyTheme(dark=True)
    chart = NetWorthBridgeChart(lambda: theme)

    # Wide window (width >= 640)
    chart.resize(720, 400)
    rect_wide = chart._plot_rect()
    assert rect_wide.top() == 44.0
    assert rect_wide.width() == 720.0 - 98.0

    # Compact window (width < 640)
    chart.resize(500, 400)
    rect_compact = chart._plot_rect()
    assert rect_compact.top() == 58.0
    assert rect_compact.width() == 500.0 - 98.0


def test_wealth_bridge_draw_legend_compact_and_wide(qapp):
    """_draw_legend() should render safely in both compact (< 580 px) and wide layouts."""
    theme = DummyTheme(dark=True)
    chart = NetWorthBridgeChart(lambda: theme)
    chart.data = {
        "explained_change_pct": 94.2,
        "coverage_pct": 94.2,
        "bars": [
            {"label": "Inicial", "start": 0.0, "end": 1000.0, "change": 1000.0, "kind": "base"},
            {"label": "Poupança", "start": 1000.0, "end": 1500.0, "change": 500.0, "kind": "positive"},
            {"label": "Final", "start": 0.0, "end": 1500.0, "change": 1500.0, "kind": "total"},
        ],
    }

    # Test compact rendering (< 580 px wide)
    chart.resize(460, 320)
    img_compact = QImage(460, 320, QImage.Format_ARGB32_Premultiplied)
    img_compact.fill(0)
    painter_compact = QPainter(img_compact)
    try:
        plot_rect = chart._plot_rect()
        assert plot_rect.top() == 58.0
        chart._draw_legend(painter_compact, plot_rect, theme)
    finally:
        painter_compact.end()

    # Test wide rendering (>= 580 px wide)
    chart.resize(750, 320)
    img_wide = QImage(750, 320, QImage.Format_ARGB32_Premultiplied)
    img_wide.fill(0)
    painter_wide = QPainter(img_wide)
    try:
        plot_rect = chart._plot_rect()
        assert plot_rect.top() == 44.0
        chart._draw_legend(painter_wide, plot_rect, theme)
    finally:
        painter_wide.end()


# ============================================================================
# 2. CASH-03 Tests
# ============================================================================


def test_cashflow_forecast_inactive_subscriptions_in_explicit_signatures():
    """CashflowForecastService must fetch inactive subscriptions (active_only=False)

    so their historical occurrences are recognized as fixed charges and not conflated
    with discretionary variable spend.
    """
    mock_repo = MagicMock()
    mock_repo.subscriptions.return_value = [
        {"name": "Netflix", "notes": "", "amount": 15.99, "active": 1},
        {"name": "Old Gym", "notes": "cancelled", "amount": 40.0, "active": 0},
    ]
    mock_repo.scheduled_payments.return_value = [
        {"name": "Internet", "notes": "", "amount": 35.0, "active": 1},
    ]

    service = CashflowForecastService(mock_repo, MagicMock())
    signatures, _ = service._explicit_spend_identifiers()

    # Verify active_only=False was requested
    mock_repo.subscriptions.assert_called_once_with(active_only=False)

    # Verify both active and inactive subscriptions are present in signatures
    names = [s.get("name") for s in signatures]
    assert "Netflix" in names
    assert "Old Gym" in names
    assert "Internet" in names


def test_cashflow_forecast_inactive_subscription_excluded_from_variable_spend():
    """Ensure that past transactions belonging to inactive subscriptions are filtered out

    of variable spend calculations.
    """
    mock_repo = MagicMock()
    mock_repo.subscriptions.return_value = [
        {"name": "Old Gym", "notes": "", "amount": 40.0, "active": 0},
    ]
    mock_repo.scheduled_payments.return_value = []
    # Historical transactions returned by repo.q: 1 grocery (variable) + 1 old gym (past fixed charge)
    mock_repo.q.return_value = [
        {
            "id": 1,
            "date": "2026-01-15",
            "amount": -50.0,
            "description": "Continente Supermercado",
            "category": "Alimentação",
        },
        {
            "id": 2,
            "date": "2026-01-15",
            "amount": -40.0,
            "description": "Old Gym Mensalidade",
            "category": "Desporto",
        },
    ]

    service = CashflowForecastService(mock_repo, MagicMock())
    daily_spend = service._variable_spend_by_day(date(2026, 1, 1), date(2026, 1, 31))

    # On 2026-01-15, only the 50.0 grocery expense should be counted as variable spend;
    # the Old Gym expense of 40.0 must be excluded.
    assert daily_spend.get(date(2026, 1, 15)) == pytest.approx(50.0)


# ============================================================================
# 3. HEALTH-02 Tests
# ============================================================================


def test_financial_health_score_fire_retiree_coverage():
    """Users without active salary (average_income <= 0) who have accumulated wealth

    (coverage_years >= 5) must receive full savings score (100.0) and proportionate debt/subscription scores.
    """
    mock_repo = MagicMock()
    mock_repo.recurring_transactions.return_value = []
    mock_repo.categories.return_value = []
    mock_repo.planned_expenses.return_value = []
    mock_repo.month_period.return_value = ("2026-02-01", "2026-02-28", "2026-02")
    mock_repo.q.return_value = [{"data_as_of": "2026-02-01"}]

    mock_payments = MagicMock()
    mock_payments.subscription_monthly_amount.return_value = 50.0

    service = FinancialHealthService(mock_repo, mock_payments)

    # Average income is 0 (retiree / FIRE living off assets)
    # Average expenses = 1500 € / month => 18000 € / year
    # Net worth = 240,000 € => coverage_years = 240,000 / 18,000 = 13.33 years >= 5.0
    service._monthly_rows = MagicMock(
        return_value=[
            {"month": "2026-01", "income": 0.0, "expenses": 1500.0, "savings": -1500.0, "savings_rate": 0.0}
        ]
    )
    service._totals = MagicMock(
        return_value={"income": 0.0, "expenses": 1500.0, "savings": -1500.0}
    )
    service._net_worth = MagicMock(
        return_value={
            "liquid_assets": 50000.0,
            "net_worth": 240000.0,
            "monthly_debt_payment": 100.0,
            "debt_ratio_pct": 4.2,
            "total_liabilities": 10000.0,
            "liabilities_available": True,
            "subscriptions_available": True,
            "subscription_rows": [{"name": "Utility", "amount": 50.0, "active": 1}],
            "holdings_detail_available": True,
            "holding_rows": [
                {"asset_class": "Stocks", "value": 150000.0},
                {"asset_class": "Bonds", "value": 50000.0},
                {"asset_class": "Cash", "value": 40000.0},
            ],
        }
    )
    service._budget_metrics = MagicMock(return_value=(85.0, "Orçamento controlado.", {}))
    service._quality_metrics = MagicMock(
        return_value=(90.0, "Excelente qualidade.", {"transaction_count": 50.0, "categorized_pct": 98.0})
    )

    result = service._score_snapshot("2026-02")

    dim_by_key = {d["key"]: d for d in result.get("dimensions", [])}
    savings = dim_by_key["savings"]
    debt = dim_by_key["debt"]
    fixed = dim_by_key["fixed_costs"]

    # Savings score should be 100.0 due to FIRE/asset coverage
    assert savings.get("score") == 100.0
    assert "cobertura patrimonial" in savings.get("reading", "").lower()
    assert "13,3 anos" in savings.get("reading", "")

    # Debt score should reflect healthy low debt-to-equity ratio
    assert debt.get("score") >= 80.0

    # Fixed / subscriptions score should be evaluated against expenses without crash
    assert fixed.get("score") is not None
    assert fixed.get("score") > 0.0

    # Overall score should be strong (Grade A or B)
    assert result.get("score") >= 75.0


def test_financial_health_score_zero_income_low_networth():
    """Users with zero income and low/no net worth should receive 0.0 savings score."""
    mock_repo = MagicMock()
    mock_repo.recurring_transactions.return_value = []
    mock_repo.categories.return_value = []
    mock_repo.planned_expenses.return_value = []
    mock_repo.subscriptions.return_value = []
    mock_repo.investments.return_value = []
    mock_repo.month_period.return_value = ("2026-02-01", "2026-02-28", "2026-02")
    mock_repo.q.return_value = [{"data_as_of": "2026-02-01"}]

    service = FinancialHealthService(mock_repo, MagicMock())

    service._monthly_rows = MagicMock(
        return_value=[
            {"month": "2026-01", "income": 0.0, "expenses": 1000.0, "savings": -1000.0, "savings_rate": 0.0}
        ]
    )
    service._totals = MagicMock(
        return_value={"income": 0.0, "expenses": 1000.0, "savings": -1000.0}
    )
    service._net_worth = MagicMock(
        return_value={
            "liquid_assets": 2000.0,
            "net_worth": 2000.0,
            "monthly_debt_payment": 0.0,
            "debt_ratio_pct": 0.0,
        }
    )
    service._budget_metrics = MagicMock(return_value=(50.0, "Neutro.", {}))
    service._quality_metrics = MagicMock(
        return_value=(50.0, "Neutro.", {"transaction_count": 10.0, "categorized_pct": 50.0})
    )

    result = service._score_snapshot("2026-02")

    dim_by_key = {d["key"]: d for d in result.get("dimensions", [])}
    savings = dim_by_key["savings"]

    # Coverage is only 2000 / 12000 = 0.17 years (< 1.0)
    assert savings.get("score") == 0.0
    assert "Sem rendimento ativo e cobertura patrimonial inferior a 1 ano." in savings.get("reading", "")


# ============================================================================
# 4. TIME-03 Tests
# ============================================================================


def test_analytics_monthly_totals_grouped_for_year_all_12_months(tmp_path):
    """monthly_totals_grouped_for_year must pre-populate and return all 12 months (01 to 12)

    even if transactions exist in only a few months.
    """
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir(parents=True, exist_ok=True)
    db = FinanceDB(
        sandbox / "test_analytics_time03.sqlite3",
        attachments_directory=sandbox / "anexos",
        history_directory=sandbox / "hist",
        preferences_file=sandbox / "prefs.json",
        protection_directory=sandbox / "prot",
    )
    repo = Repo(db)

    # Insert transactions in March (2026-03) and August (2026-08)
    with db.conn:
        db.conn.execute("DELETE FROM transactions")
        db.conn.execute(
            """INSERT INTO transactions (id, account, category, date, amount, kind, description)
               VALUES (1, 'Conta Principal', 'Salário', '2026-03-10', 2500.0, 'income', 'Salário Março')"""
        )
        db.conn.execute(
            """INSERT INTO transactions (id, account, category, date, amount, kind, description)
               VALUES (2, 'Conta Principal', 'Supermercado', '2026-03-15', -300.0, 'expense', 'Compras Março')"""
        )
        db.conn.execute(
            """INSERT INTO transactions (id, account, category, date, amount, kind, description)
               VALUES (3, 'Conta Principal', 'Supermercado', '2026-08-20', -450.0, 'expense', 'Compras Agosto')"""
        )

    try:
        totals = repo.monthly_totals_grouped_for_year(2026)

        # Must contain exactly all 12 months
        expected_keys = [f"2026-{m:02d}" for m in range(1, 13)]
        assert list(totals.keys()) == expected_keys

        # Check empty month (e.g., January 2026-01)
        assert totals["2026-01"] == {"income": 0.0, "expenses": 0.0, "savings": 0.0}

        # Check March (2026-03)
        assert totals["2026-03"]["income"] == 2500.0
        assert totals["2026-03"]["expenses"] == 300.0
        assert totals["2026-03"]["savings"] == 2200.0

        # Check August (2026-08)
        assert totals["2026-08"]["income"] == 0.0
        assert totals["2026-08"]["expenses"] == 450.0
        assert totals["2026-08"]["savings"] == -450.0

        # Ensure no KeyError for any month 1..12
        for m in range(1, 13):
            key = f"2026-{m:02d}"
            assert key in totals
            assert "income" in totals[key]
            assert "expenses" in totals[key]
            assert "savings" in totals[key]
    finally:
        db.conn.close()
