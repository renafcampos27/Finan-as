from __future__ import annotations

import math
from datetime import date

import pytest

from motores_matematicos_simulacao.portfolio_stress_and_tax_optimizer import (
    CrisisDrawdownManager,
)
from services.finance_math import calmar_ratio, sharpe_ratio, sortino_ratio
from services.fire import FirePlanningEngine
from services.investment_performance import portfolio_performance_attribution
from services.tax import (
    ForeignDividendPayment,
    PortugueseTaxEngine,
    calculate_foreign_dividend_tax,
    summarize_foreign_dividends,
)


def test_bug_r6_tax_01_offshore_lot_rate_and_no_exemption():
    """BUG-R6-TAX-01: An asset acquired in an offshore territory (e.g. Cayman Islands)
    must NOT receive the >365d crypto exemption and must be taxed at the aggravated 35% rate.
    """
    engine = PortugueseTaxEngine()
    engine.add_acquisition(
        lot_id="crypto_offshore",
        asset_symbol="BTC",
        acquisition_date=date(2023, 1, 1),
        quantity=1.0,
        unit_price=20_000.0,
        is_crypto=True,
        country_code="KY",  # Cayman Islands (Offshore)
    )

    # Dispose >365 days later in PT
    matches = engine.process_disposal(
        disposal_id="disp_1",
        asset_symbol="BTC",
        disposal_date=date(2025, 6, 1),
        quantity=1.0,
        unit_price=60_000.0,
        country_code="PT",
    )

    assert len(matches) == 1
    match = matches[0]
    # Under Art. 10.º n.º 1 al. k) CIRS, offshore crypto is NOT exempt even if held >365 days
    assert match.is_exempt_crypto is False
    assert match.tax_rate_pct == 35.0
    assert match.net_gain_loss == 40_000.0
    assert match.estimated_tax == pytest.approx(14_000.0)  # 40000 * 35%


def test_bug_r6_tax_02_autonomous_preserves_35pct_and_englobamento_records_reference():
    """BUG-R6-TAX-02: Under autonomous regime, global tax properly combines Anexo G (28%)
    and Anexo J (35% offshore). Under englobamento, total_tax_autonomous is 0.0.
    """
    engine = PortugueseTaxEngine()
    # Lot 1: Domestic/Standard (Anexo G)
    engine.add_acquisition(
        lot_id="g1",
        asset_symbol="STOCK_PT",
        acquisition_date=date(2025, 1, 10),
        quantity=10.0,
        unit_price=10.0,
        country_code="PT",
    )
    # Lot 2: Offshore (Anexo J, 35%)
    engine.add_acquisition(
        lot_id="j1",
        asset_symbol="OFFSHORE_FUND",
        acquisition_date=date(2025, 1, 10),
        quantity=10.0,
        unit_price=10.0,
        country_code="KY",
    )

    m1 = engine.process_disposal(
        disposal_id="d1",
        asset_symbol="STOCK_PT",
        disposal_date=date(2025, 6, 1),
        quantity=10.0,
        unit_price=20.0,
        country_code="PT",
    )
    m2 = engine.process_disposal(
        disposal_id="d2",
        asset_symbol="OFFSHORE_FUND",
        disposal_date=date(2025, 6, 1),
        quantity=10.0,
        unit_price=20.0,
        country_code="KY",
    )
    all_matches = m1 + m2

    # Autonomous regime:
    rep_auto = engine.generate_annual_tax_report(all_matches, 2025, opt_for_englobamento=False)
    # G1: gain 100 * 28% = 28.0; J1: gain 100 * 35% = 35.0; Total = 63.0
    assert rep_auto["anexo_g"]["estimated_tax_autonomous_28pct"] == pytest.approx(28.0)
    assert rep_auto["anexo_j"]["estimated_tax_autonomous_28pct"] == pytest.approx(35.0)
    assert rep_auto["global_estimated_tax_28pct"] == pytest.approx(63.0)
    assert rep_auto["total_tax_autonomous"] == pytest.approx(63.0)

    # Englobamento:
    rep_englob = engine.generate_annual_tax_report(all_matches, 2025, opt_for_englobamento=True)
    assert rep_englob["total_tax_autonomous"] == 0.0
    assert rep_englob["final_taxable_base"] == pytest.approx(200.0)


def test_bug_r6_tax_03_crypto_swap_rollover_holding_period_and_cost():
    """BUG-R6-TAX-03: Under Art. 10.º n.º 19 CIRS, a crypto-to-crypto swap rolls over
    the original acquisition date and cost basis to the received crypto asset.
    """
    engine = PortugueseTaxEngine()
    engine.add_acquisition(
        lot_id="btc_initial",
        asset_symbol="BTC",
        acquisition_date=date(2024, 1, 1),
        quantity=1.0,
        unit_price=30_000.0,
        is_crypto=True,
    )

    # Swap 1.0 BTC for 15.0 ETH on 2024-06-01 (less than 365d from BTC acquisition)
    matches, new_lots = engine.process_crypto_swap(
        swap_id="swap_1",
        from_asset_symbol="BTC",
        to_asset_symbol="ETH",
        swap_date=date(2024, 6, 1),
        quantity_disposed=1.0,
        quantity_acquired=15.0,
    )

    assert len(matches) == 1
    assert matches[0].is_crypto_to_crypto_swap is True  # Swap is tax neutral under Art. 10.º n.º 19
    assert matches[0].estimated_tax == 0.0
    assert len(new_lots) == 1

    rolled_eth = new_lots[0]
    # The rolled ETH keeps original acquisition date of the BTC (2024-01-01) and original cost (30,000 / 15 = 2,000)
    assert rolled_eth.acquisition_date == date(2024, 1, 1)
    assert rolled_eth.quantity == 15.0
    assert rolled_eth.unit_price == pytest.approx(2_000.0)

    # Now register the rolled ETH lot and sell it on 2025-01-15 (>365d after BTC buy date 2024-01-01)
    engine.add_lot(rolled_eth)
    eth_matches = engine.process_disposal(
        disposal_id="disp_eth",
        asset_symbol="ETH",
        disposal_date=date(2025, 1, 15),
        quantity=15.0,
        unit_price=3_500.0,
        is_crypto=True,
        country_code="PT",
    )
    assert len(eth_matches) == 1
    # Holding period from 2024-01-01 to 2025-01-15 is 380 days (>365d) -> EXEMPT!
    assert eth_matches[0].holding_days == 380
    assert eth_matches[0].is_exempt_crypto is True
    assert eth_matches[0].estimated_tax == 0.0
    assert eth_matches[0].estimated_tax == 0.0


def test_bug_r6_tax_04_foreign_dividend_excess_domestic_withholding_refund():
    """BUG-R6-TAX-04: Excess domestic withholding on foreign dividends must be flagged as a refund due."""
    payment = ForeignDividendPayment(
        id="div_us_1",
        asset_symbol="AAPL",
        payment_date=date(2025, 4, 15),
        gross_amount_eur=1_000.0,
        foreign_tax_withheld_eur=150.0,
        country_code="US",
        domestic_tax_withheld_eur=280.0,
    )
    calc = calculate_foreign_dividend_tax(payment, domestic_tax_withheld=payment.domestic_tax_withheld_eur)
    assert calc.portuguese_autonomous_tax_28pct == 280.0
    assert calc.tax_credit_applied_art81 == 150.0
    assert calc.net_portuguese_tax_payable == 0.0
    assert calc.domestic_tax_withheld == 280.0
    assert calc.tax_refund_due == pytest.approx(150.0)

    summary = summarize_foreign_dividends([payment])
    assert summary["total_domestic_tax_withheld"] == 280.0
    assert summary["total_tax_refund_due"] == pytest.approx(150.0)
    assert summary["total_net_portuguese_tax_payable"] == 0.0


def test_bug_r6_math_01_calmar_ratio_zero_drawdown_and_bounds():
    """BUG-R6-MATH-01: Calmar ratio with zero drawdown returns sentinel +-99.99, and clamps to [-99.99, 99.99]."""
    # Zero drawdown with positive CAGR
    assert calmar_ratio(cagr_pct=12.0, max_drawdown_pct=0.0) == 99.99
    # Zero drawdown with negative CAGR
    assert calmar_ratio(cagr_pct=-5.0, max_drawdown_pct=0.0) == -99.99
    # Zero drawdown with 0 CAGR
    assert calmar_ratio(cagr_pct=0.0, max_drawdown_pct=0.0) == 0.0
    # Huge CAGR / small drawdown clamped to 99.99
    assert calmar_ratio(cagr_pct=500.0, max_drawdown_pct=0.1) == 99.99
    # Non-finite values return 0.0
    assert calmar_ratio(cagr_pct=float("nan"), max_drawdown_pct=10.0) == 0.0


def test_bug_r6_math_02_sharpe_sortino_zero_volatility_bounded():
    """BUG-R6-MATH-02: Sharpe and Sortino ratios return within [-99.99, 99.99] bounds."""
    # Positive excess with zero vol returns +99.99
    assert sharpe_ratio([5.0] * 12, risk_free_rate_annual_pct=2.0, periods_per_year=12) == 99.99
    assert sortino_ratio([5.0] * 12, risk_free_rate_annual_pct=2.0, periods_per_year=12) == 99.99
    # Negative excess with zero vol returns 0.0 (non-positive sentinel)
    assert sharpe_ratio([-1.0] * 12, risk_free_rate_annual_pct=2.0, periods_per_year=12) == 0.0
    assert sortino_ratio([0.0] * 12, risk_free_rate_annual_pct=2.0, periods_per_year=12) == 0.0


def test_bug_r6_math_03_portfolio_attribution_market_value_weights():
    """BUG-R6-MATH-03: portfolio_weight_pct and market_weight_pct use current market value,
    while cost_weight_pct tracks cost basis.
    """
    holdings = [
        {
            "id": 1,
            "name": "Big Winner",
            "value": 8_000.0,  # 80% of current value
            "cost_basis": 1_000.0,  # 50% of cost basis
            "fees": 0.0,
            "cost_basis_known": 1,
        },
        {
            "id": 2,
            "name": "Laggard",
            "value": 2_000.0,  # 20% of current value
            "cost_basis": 1_000.0,  # 50% of cost basis
            "fees": 0.0,
            "cost_basis_known": 1,
        },
    ]
    res = portfolio_performance_attribution(holdings)
    row1 = next(r for r in res["rows"] if r["id"] == 1)
    row2 = next(r for r in res["rows"] if r["id"] == 2)

    assert row1["portfolio_weight_pct"] == pytest.approx(80.0)
    assert row1["market_weight_pct"] == pytest.approx(80.0)
    assert row1["cost_weight_pct"] == pytest.approx(50.0)

    assert row2["portfolio_weight_pct"] == pytest.approx(20.0)
    assert row2["market_weight_pct"] == pytest.approx(20.0)
    assert row2["cost_weight_pct"] == pytest.approx(50.0)


def test_bug_r6_fire_01_fire_milestones_zero_savings_compound_growth():
    """BUG-R6-FIRE-01: calc_years calculates compound growth time when annual savings is zero
    provided invested > 0, r > 0, and target > invested.
    """
    milestones = FirePlanningEngine.calculate_fire_milestones(
        current_invested=100_000.0,
        annual_expenses=20_000.0,
        annual_savings=0.0,  # Coast FIRE: zero additional contributions
        real_growth_rate_pct=7.0,  # 7% real compound growth
        swr_pct=4.0,
    )
    std = milestones["standard"]
    assert std.years_to_goal is not None
    expected_years = math.log(500_000.0 / 100_000.0) / math.log(1.07)
    assert std.years_to_goal == pytest.approx(expected_years, abs=0.05)


def test_bug_r6_fire_02_guyton_klinger_year1_no_inflation_and_floor_distinction():
    """BUG-R6-FIRE-02: Year 1 does not compound inflation prematurely, and floor activation
    is distinguished from a standard 10% preservation cut.
    """
    hist = FirePlanningEngine.simulate_guyton_klinger_drawdown(
        initial_capital=100_000.0,
        initial_annual_withdrawal=5_000.0,
        annual_returns=[-10.0] * 30,
        annual_inflation_rates=[5.0] * 30,
        initial_swr_pct=5.0,  # A10: the anchor must match actual initial spending.
        min_floor_pct_of_initial=0.95,
    )
    assert hist[0]["withdrawal"] == 5000.0
    assert hist[0]["applied_inflation_pct"] == 0.0
    binding = [row for row in hist if "Piso Mínimo Ativado" in row["rule_triggered"]]
    assert binding
    assert binding[0]["withdrawal"] == pytest.approx(4750.0 * 1.05 ** (binding[0]["year"] - 1))


def test_bug_r6_port_01_crisis_drawdown_manager_deducts_and_prevents_reexecution():
    """BUG-R6-PORT-01: CrisisDrawdownManager tracks executed tranches and prevents duplicate execution."""
    mgr = CrisisDrawdownManager(total_reserve_cash=10_000.0, base_monthly_dca=500.0)

    preview = mgr.calculate_deployment_plan(-0.25, mark_executed=False)
    assert len(preview) == 2
    assert mgr.current_reserve == 10_000.0
    assert len(mgr.executed_thresholds) == 0

    order = mgr.execute_tranche(-0.10)
    assert order is not None
    assert order.deployed_amount == 1_500.0
    assert mgr.current_reserve == 8_500.0
    assert -0.10 in mgr.executed_thresholds

    assert mgr.execute_tranche(-0.10) is None

    plan2 = mgr.calculate_deployment_plan(-0.25, mark_executed=True)
    assert len(plan2) == 1
    assert plan2[0].drawdown_threshold == -0.20
    assert plan2[0].deployed_amount == 2_500.0
    assert mgr.current_reserve == 6_000.0

    mgr.reset()
    assert mgr.current_reserve == 10_000.0
    assert len(mgr.executed_thresholds) == 0
