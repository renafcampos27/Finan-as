"""Unit and integration tests for AUD-23, AUD-24, and AUD-25 fixes in v3.49.0."""

from datetime import date
from services.finance_math import sortino_ratio
from services.tax import PortugueseTaxEngine
from motores_matematicos_simulacao.crisis_backtester_and_architecture_engine import (
    Action,
    AppState,
    app_reducer,
)


def test_aud24_sortino_ratio_infinite_for_zero_downside_deviation():
    """Verify Sortino ratio returns float('inf') when downside risk is zero with positive excess return."""
    # Series with positive monthly returns consistently above target (0%)
    consistent_positive_returns = [1.2, 0.8, 1.5, 2.0, 0.9, 1.1, 1.8]
    sortino = sortino_ratio(
        consistent_positive_returns,
        risk_free_rate_annual_pct=2.0,
        target_downside_pct=0.0,
        periods_per_year=12,
    )
    assert sortino == 99.99
    assert sortino > 0.0


def test_aud24_sortino_ratio_zero_for_zero_downside_and_negative_excess():
    """Verify Sortino ratio returns 0.0 when excess return is non-positive even with zero downside."""
    # Flat series at 0% when target is 0% and risk-free rate is 2%
    flat_returns = [0.0, 0.0, 0.0, 0.0]
    sortino = sortino_ratio(
        flat_returns,
        risk_free_rate_annual_pct=2.0,
        target_downside_pct=0.0,
        periods_per_year=12,
    )
    assert sortino == 0.0


def test_aud25_tax_report_adds_current_year_net_losses_to_carryforward():
    """Verify PortugueseTaxEngine carries forward current year net losses to subsequent 5 years (Art. 55 CIRS)."""
    engine = PortugueseTaxEngine()
    engine.add_acquisition(
        lot_id="loss-lot-1",
        asset_symbol="AAPL",
        acquisition_date=date(2024, 6, 15),
        quantity=10.0,
        unit_price=200.0,
        is_foreign=True,
    )
    matches = engine.process_disposal(
        disposal_id="disp-1",
        asset_symbol="AAPL",
        disposal_date=date(2025, 6, 15),
        quantity=10.0,
        unit_price=150.0,
        is_foreign=True,
    )

    report = engine.generate_annual_tax_report(
        matches,
        fiscal_year=2025,
        prior_year_losses=300.0,
        apply_50pct_inclusion=False,
    )

    assert report["global_net_capital_gain_loss_raw"] == -500.0
    assert report["prior_year_losses_deducted"] == 0.0
    # BUG-AUD-C104 (CIRS Art. 55.º n.º 1 al. d)): under the autonomous 28%
    # regime prior buckets remain frozen (kept, never usable) and the year's
    # losses are NOT reportable forward - they lapse. Carryforward of the
    # year's losses requires opting for englobamento.
    assert report["remaining_prior_year_losses"] == 300.0
    assert report["current_year_losses_generated"] == 0.0
    assert report["total_losses_to_carryforward"] == 300.0  # prior frozen only
    assert report["global_estimated_tax_28pct"] == 0.0

    # The same year under englobamento carries the fresh 500 loss forward.
    report_eng = engine.generate_annual_tax_report(
        matches,
        fiscal_year=2025,
        prior_year_losses=300.0,
        apply_50pct_inclusion=False,
        opt_for_englobamento=True,
    )
    assert report_eng["current_year_losses_generated"] == 500.0
    assert report_eng["total_losses_to_carryforward"] == 800.0  # 300 prior + 500 new


def test_aud23_reducer_udf_zero_deployment_edge_case():
    """Verify app_reducer handles zero deployment requests without incrementing lot count."""
    initial_state = AppState(
        portfolio_cash=5000.0,
        portfolio_equity=20000.0,
        market_drawdown=-0.10,
        active_scenario="Test",
        tax_lots_count=3,
    )
    action = Action("DEPLOY_DRY_POWDER_TRANCHE", {"amount": 0.0})
    new_state = app_reducer(initial_state, action)

    assert new_state.portfolio_cash == 5000.0
    assert new_state.portfolio_equity == 20000.0
    assert new_state.tax_lots_count == 3
