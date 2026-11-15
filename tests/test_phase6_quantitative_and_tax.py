#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Test suite for Phase 6: Quantitative Finance (XIRR/MWRR/TWRR), Statistical Stability, and CIRS Tax Engine."""

from __future__ import annotations

from datetime import date
import pytest

from services.finance_math import (
    cagr,
    modified_dietz,
    twrr,
    xirr,
    xirr_pct,
)
from services.tax import (
    COEFICIENTES_DESVALORIZACAO,
    PortugueseTaxEngine,
)
from services.fire import (
    FirePlanningEngine,
)


def test_xirr_standard_and_irregular_cashflows():
    """Verify XIRR converges accurately on standard and irregular date series."""
    # Simple 1-year 10% return: -1000 on Jan 1, +1100 on Dec 31
    flows_simple = [
        (date(2025, 1, 1), -1000.0),
        (date(2026, 1, 1), 1100.0),
    ]
    rate = xirr_pct(flows_simple)
    assert rate is not None
    assert pytest.approx(rate, abs=0.01) == 10.0

    # Irregular cash flows with multiple deposits and final value
    flows_irregular = [
        (date(2025, 1, 1), -10000.0),
        (date(2025, 4, 1), -5000.0),
        (date(2025, 7, 1), -5000.0),
        (date(2025, 10, 1), 2000.0),  # Partial withdrawal / dividend
        (date(2026, 1, 1), 21000.0),  # Portfolio end value
    ]
    rate_irreg = xirr_pct(flows_irregular)
    assert rate_irreg is not None
    assert 10.0 < rate_irreg < 35.0


def test_xirr_edge_cases_same_day_and_invalid_signs():
    """Verify XIRR handles same-day flows and fails safely when signs do not alternate."""
    # Same day flows combined
    flows_same_day = [
        (date(2025, 1, 1), -500.0),
        (date(2025, 1, 1), -500.0),
        (date(2026, 1, 1), 1200.0),
    ]
    rate = xirr_pct(flows_same_day)
    assert rate is not None
    assert pytest.approx(rate, abs=0.01) == 20.0

    # All positive flows (no IRR solution)
    assert xirr([(date(2025, 1, 1), 100.0), (date(2026, 1, 1), 200.0)]) is None
    # All negative flows
    assert xirr([(date(2025, 1, 1), -100.0), (date(2026, 1, 1), -200.0)]) is None
    # Empty
    assert xirr([]) is None


def test_twrr_and_modified_dietz_and_cagr():
    """Verify TWRR, Modified Dietz, and CAGR calculate correct compounding and weighted returns."""
    # TWRR: 4 quarterly returns of +5%, +2%, -3%, +10%
    sub_returns = [5.0, 2.0, -3.0, 10.0]
    expected_twrr = (1.05 * 1.02 * 0.97 * 1.10 - 1.0) * 100.0
    assert pytest.approx(twrr(sub_returns), abs=0.001) == expected_twrr

    # Modified Dietz: Started with 10k, added 5k halfway through the year, ended with 17k
    flows = [(date(2025, 7, 2), 5000.0)]
    md_return = modified_dietz(
        start_value=10000.0,
        end_value=17000.0,
        cash_flows=flows,
        start_date=date(2025, 1, 1),
        end_date=date(2025, 12, 31),
    )
    # Gain = 17000 - 10000 - 5000 = 2000. Denominator = 10000 + 5000 * 0.5 = 12500. Return = 2000/12500 = 16%
    assert 15.5 <= md_return <= 16.5

    # CAGR: from 10k to 20k in 7.2 years (~10% CAGR)
    cagr_res = cagr(10000.0, 20000.0, 7.2)
    assert pytest.approx(cagr_res, abs=0.2) == 10.07


def test_portuguese_tax_engine_fifo_and_anexo_g():
    """Verify FIFO tax lot matching for national equities (Anexo G) with partial lot sales."""
    engine = PortugueseTaxEngine()
    # Lot 1: 100 shares at 10€ on 2024-01-10 with 5€ fee
    engine.add_acquisition(
        lot_id="LOT-1",
        asset_symbol="BCP.LS",
        asset_name="Banco Comercial Português",
        acquisition_date="2024-01-10",
        quantity=100.0,
        unit_price=10.0,
        fees=5.0,
        is_foreign=False,
    )
    # Lot 2: 50 shares at 12€ on 2024-06-15 with 5€ fee
    engine.add_acquisition(
        lot_id="LOT-2",
        asset_symbol="BCP.LS",
        asset_name="Banco Comercial Português",
        acquisition_date="2024-06-15",
        quantity=50.0,
        unit_price=12.0,
        fees=5.0,
        is_foreign=False,
    )

    # Disposal 1: Sell 120 shares at 15€ on 2024-11-20 with 10€ sale fee
    matches = engine.process_disposal(
        disposal_id="DISP-1",
        asset_symbol="BCP.LS",
        disposal_date="2024-11-20",
        quantity=120.0,
        unit_price=15.0,
        fees=10.0,
        is_foreign=False,
    )

    assert len(matches) == 2
    # Match 1: 100 shares from LOT-1
    assert matches[0].acquisition_lot_id == "LOT-1"
    assert matches[0].matched_quantity == 100.0
    assert matches[0].tax_schedule == "Anexo G"
    # Match 2: 20 shares from LOT-2
    assert matches[1].acquisition_lot_id == "LOT-2"
    assert matches[1].matched_quantity == 20.0
    assert matches[1].tax_schedule == "Anexo G"

    report = engine.generate_annual_tax_report(matches, 2024)
    assert report["anexo_g"]["transaction_count"] == 2
    assert report["global_net_capital_gain_loss"] > 0
    assert report["global_estimated_tax_28pct"] == pytest.approx(
        report["global_net_capital_gain_loss"] * 0.28, abs=0.01
    )


def test_portuguese_tax_engine_crypto_exemption_over_365_days():
    """Verify CIRS Art. 10.º n.º 1 k) & n.º 19: Crypto held >= 365 days is 100% tax exempt."""
    engine = PortugueseTaxEngine()
    # Crypto Lot 1: 1 BTC bought on 2023-01-01 at 20,000€
    engine.add_acquisition(
        lot_id="BTC-LOT-1",
        asset_symbol="BTC",
        acquisition_date="2023-01-01",
        quantity=1.0,
        unit_price=20000.0,
        is_crypto=True,
    )
    # Crypto Lot 2: 1 BTC bought on 2024-06-01 at 60,000€
    engine.add_acquisition(
        lot_id="BTC-LOT-2",
        asset_symbol="BTC",
        acquisition_date="2024-06-01",
        quantity=1.0,
        unit_price=60000.0,
        is_crypto=True,
    )

    # Sale 1: 1 BTC on 2024-08-01 at 65,000€ (Held > 365 days -> EXEMPT)
    matches_exempt = engine.process_disposal(
        disposal_id="BTC-DISP-1",
        asset_symbol="BTC",
        disposal_date="2024-08-01",
        quantity=1.0,
        unit_price=65000.0,
        is_crypto=True,
    )
    assert len(matches_exempt) == 1
    assert matches_exempt[0].is_exempt_crypto is True
    assert matches_exempt[0].tax_rate_pct == 0.0
    assert matches_exempt[0].estimated_tax == 0.0

    # Sale 2: 1 BTC on 2024-09-01 at 65,000€ (Held < 365 days -> TAXABLE at 28%)
    matches_taxable = engine.process_disposal(
        disposal_id="BTC-DISP-2",
        asset_symbol="BTC",
        disposal_date="2024-09-01",
        quantity=1.0,
        unit_price=65000.0,
        is_crypto=True,
    )
    assert len(matches_taxable) == 1
    assert matches_taxable[0].is_exempt_crypto is False
    assert matches_taxable[0].tax_rate_pct == 28.0
    assert matches_taxable[0].net_gain_loss == pytest.approx(5000.0)
    assert matches_taxable[0].estimated_tax == pytest.approx(5000.0 * 0.28)


def test_portuguese_tax_engine_currency_devaluation_coefficient():
    """Art. 50.º CIRS: since the 2014 IRS reform the devaluation
    coefficient applies exclusively to real estate; quoted securities and
    crypto keep coef = 1.0."""
    engine = PortugueseTaxEngine()
    # Bought 100 shares of VWCE in 2020 at 80€ each
    engine.add_acquisition(
        lot_id="VWCE-2020",
        asset_symbol="VWCE.DE",
        acquisition_date="2020-03-15",
        quantity=100.0,
        unit_price=80.0,
        is_foreign=True,
    )
    # Sold in 2024 at 120€ each (Holding period ~ 4 years > 24 months)
    matches = engine.process_disposal(
        disposal_id="VWCE-DISP-2024",
        asset_symbol="VWCE.DE",
        disposal_date="2024-05-10",
        quantity=100.0,
        unit_price=120.0,
        is_foreign=True,
    )
    assert len(matches) == 1
    m = matches[0]
    assert m.tax_schedule == "Anexo J"
    # Quoted securities keep coef = 1.0 (Art. 50.º applies to real estate only).
    assert m.devaluation_coefficient == 1.0
    assert m.adjusted_acquisition_cost == pytest.approx(100.0 * 80.0)
    assert m.net_gain_loss == pytest.approx(12000.0 - 8000.0)


def test_devaluation_coefficient_applies_to_real_estate_after_24_months():
    """Art. 50.º CIRS: real estate held >24 months uses the official table."""
    engine = PortugueseTaxEngine()
    engine.add_acquisition(
        lot_id="APT-2020",
        asset_symbol="Apartamento Lisboa",
        acquisition_date="2020-03-15",
        quantity=1.0,
        unit_price=300000.0,
        is_real_estate=True,
    )
    matches = engine.process_disposal(
        disposal_id="APT-DISP-2024",
        asset_symbol="Apartamento Lisboa",
        disposal_date="2024-05-10",
        quantity=1.0,
        unit_price=400000.0,
    )
    assert len(matches) == 1
    m = matches[0]
    assert m.devaluation_coefficient == COEFICIENTES_DESVALORIZACAO[2020]
    assert m.adjusted_acquisition_cost == pytest.approx(
        300000.0 * COEFICIENTES_DESVALORIZACAO[2020]
    )


def test_fire_milestones_and_guyton_klinger():
    """Verify FIRE milestones (Lean, Standard, Fat, Coast) and Guyton-Klinger dynamic guardrails."""
    milestones = FirePlanningEngine.calculate_fire_milestones(
        current_invested=100000.0,
        annual_expenses=24000.0,
        annual_savings=12000.0,
        swr_pct=4.0,
    )
    assert "standard" in milestones
    assert milestones["standard"].target_capital == 600000.0  # 24000 / 0.04
    assert milestones["lean"].target_capital == 450000.0  # 18000 / 0.04
    assert milestones["standard"].progress_pct == pytest.approx(
        100000.0 / 600000.0 * 100.0, abs=0.02
    )

    # Guyton-Klinger drawdown simulation over 5 years
    drawdown = FirePlanningEngine.simulate_guyton_klinger_drawdown(
        initial_capital=500000.0,
        initial_annual_withdrawal=20000.0,
        annual_returns=[-15.0, 20.0, 10.0, -5.0, 12.0],
        annual_inflation_rates=[3.0, 2.5, 2.0, 2.0, 2.0],
        initial_swr_pct=4.0,
    )
    assert len(drawdown) == 5
    assert all(row["ending_capital"] > 0 for row in drawdown)


def test_sequence_of_returns_risk_simulation():
    """Verify SRR simulation detects depletion under severe early market crashes."""
    res_crash = FirePlanningEngine.simulate_sequence_of_returns_risk(
        initial_capital=300000.0,
        initial_annual_withdrawal=25000.0,  # ~8.3% withdrawal (high risk)
        duration_years=25,
        early_crash_scenario="dotcom_2000",
    )
    assert len(res_crash.yearly_balances) == 26
    # An aggressive withdrawal rate with an early market crash should deplete before 25 years
    assert res_crash.portfolio_survived is False
    assert res_crash.depletion_year is not None

    res_safe = FirePlanningEngine.simulate_sequence_of_returns_risk(
        initial_capital=1000000.0,
        initial_annual_withdrawal=30000.0,  # 3% SWR (very safe)
        duration_years=30,
        early_crash_scenario="dotcom_2000",
    )
    assert res_safe.portfolio_survived is True


def test_quantitative_risk_metrics_sharpe_sortino_drawdown_var():
    """Verify Sharpe ratio, Sortino ratio, Max Drawdown, and Cornish-Fisher VaR & CVaR."""
    from services.finance_math import (
        cornish_fisher_var,
        expected_shortfall_cvar,
        max_drawdown,
        sharpe_ratio,
        sortino_ratio,
    )

    # Monthly returns series: positive trend with some pullbacks
    returns = [2.0, 1.5, -1.0, 3.0, -2.5, 4.0, 1.2, 0.8, -0.5, 2.2, 1.8, 3.5]
    sharpe = sharpe_ratio(returns, risk_free_rate_annual_pct=2.5, periods_per_year=12)
    assert sharpe > 1.0  # Strong annualized risk-adjusted return

    sortino = sortino_ratio(returns, risk_free_rate_annual_pct=2.5, periods_per_year=12)
    assert (
        sortino > sharpe
    )  # Sortino typically higher because only downside volatility is penalized

    # Cumulative series with a known drawdown: 100 -> 120 -> 90 -> 130
    # Peak is 120, trough is 90 -> Drawdown = (120 - 90)/120 = 25%
    series = [100.0, 110.0, 120.0, 105.0, 90.0, 95.0, 115.0, 130.0]
    dd_pct, peak_i, trough_i = max_drawdown(series)
    assert dd_pct == 25.0
    assert peak_i == 2
    assert trough_i == 4

    # Cornish-Fisher VaR & CVaR (Expected Shortfall)
    # Simulated returns with negative skew (fat left tail)
    skewed_returns = [
        1.5,
        2.0,
        0.8,
        1.2,
        -8.0,
        2.5,
        -0.5,
        1.0,
        1.8,
        -5.0,
        2.2,
        1.1,
        0.9,
        1.4,
        -6.5,
        2.0,
        1.7,
        0.5,
        -4.0,
        1.9,
    ]
    var_95 = cornish_fisher_var(skewed_returns, confidence=0.95)
    cvar_95 = expected_shortfall_cvar(skewed_returns, confidence=0.95)

    assert var_95 > 0.0
    assert cvar_95 >= var_95 * 0.8  # CVaR captures average tail loss
