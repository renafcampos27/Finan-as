"""Automated unit and integration tests for Phase 15 Batch 7 (P1 math & FIRE simulation defects).

Defects covered:
1. AUD-MATH-02: Total capital loss (-100.0%) return in calculate_holding_xirr and calculate_portfolio_xirr.
2. AUD-MATH-04: Periodic scaling of target_downside_pct (MAR) and numerical stability capping in sortino_ratio.
3. AUD-SIM-01: Fisher exact rate conversion (nominal_to_real_rate / real_to_nominal_rate) and distinct real vs nominal capital in SRR.
4. AUD-SIM-02: Minimum survival withdrawal floor (min_floor_pct_of_initial) and non-truncated complete series in Guyton-Klinger.
"""

from __future__ import annotations

from datetime import date

import pytest

from services.finance_math import (
    nominal_to_real_rate,
    real_to_nominal_rate,
    sortino_ratio,
)
from services.fire import FirePlanningEngine, FireService
from services.investment_performance import (
    calculate_holding_xirr,
    calculate_portfolio_xirr,
)


# ============================================================================
# 1. AUD-MATH-02: Total Loss (-100.0%) in XIRR
# ============================================================================


def test_aud_math_02_holding_xirr_total_loss_returns_minus_100() -> None:
    # Asset bought for 1000€ and now valued at 0€ with no dividends
    acq_date = date(2025, 1, 15)
    today = date(2026, 9, 4)
    xirr_val = calculate_holding_xirr(
        acquisition_date=acq_date,
        cost_basis=1000.0,
        current_date=today,
        current_value=0.0,
    )
    assert xirr_val == -100.0, f"Expected -100.0 for total wipeout, got {xirr_val}"

    # Asset with zero dividends should still return -100.0
    xirr_zero_divs = calculate_holding_xirr(
        acquisition_date=acq_date,
        cost_basis=1500.0,
        current_date=today,
        current_value=0.0,
        dividends=[(date(2025, 6, 1), 0.0)],
        fees=20.0,
    )
    assert xirr_zero_divs == -100.0

    # Normal holding with positive value still returns standard calculation
    xirr_normal = calculate_holding_xirr(
        acquisition_date=acq_date,
        cost_basis=1000.0,
        current_date=today,
        current_value=1200.0,
    )
    assert xirr_normal is not None
    assert xirr_normal > 0.0

    # Holding with unknown cost basis (0.0) returns None
    assert calculate_holding_xirr(acq_date, 0.0, today, 100.0) is None


def test_aud_math_02_portfolio_xirr_total_loss_returns_minus_100() -> None:
    # Portfolio cash flows with capital invested but current total value 0.0
    today = date(2026, 9, 4)
    flows = [
        (date(2025, 1, 1), -10000.0),
        (date(2025, 6, 1), -5000.0),
    ]
    port_xirr = calculate_portfolio_xirr(
        cash_flows=flows,
        current_date=today,
        current_portfolio_value=0.0,
    )
    assert port_xirr == -100.0, f"Expected -100.0 for portfolio wipeout, got {port_xirr}"

    # Portfolio with positive value computes standard positive/negative XIRR
    port_positive = calculate_portfolio_xirr(
        cash_flows=flows,
        current_date=today,
        current_portfolio_value=20000.0,
    )
    assert port_positive is not None
    assert port_positive > 0.0


# ============================================================================
# 2. AUD-MATH-04: Periodic Scaling and Capping in Sortino Ratio
# ============================================================================


def test_aud_math_04_sortino_ratio_periodic_target_scaling() -> None:
    # Monthly returns: 1.0% each month = 12% annualized
    # Target: 6% annualized MAR
    # With 12 periods/year, target periodic should be 6.0 / 12 = 0.5% per month.
    # Since every month is 1.0% > 0.5%, there is ZERO downside deviation against MAR.
    monthly_returns = [1.0] * 12
    sortino_monthly = sortino_ratio(
        returns_pct=monthly_returns,
        risk_free_rate_annual_pct=2.0,
        target_downside_pct=6.0,
        periods_per_year=12,
    )
    # Excess return is positive (1.0% - 2/12% > 0) and downside dev is zero -> capped at 99.99
    assert sortino_monthly == 99.99

    # Now simulate mixed returns with target downside
    mixed_returns = [2.0, -1.0, 3.0, -0.5, 1.5, 2.5, -2.0, 1.0, 0.5, 3.0, -1.5, 2.0]
    sortino_mixed = sortino_ratio(
        returns_pct=mixed_returns,
        risk_free_rate_annual_pct=2.5,
        target_downside_pct=4.0,
        periods_per_year=12,
    )
    assert -99.99 <= sortino_mixed <= 99.99
    assert sortino_mixed > 0.0


def test_aud_math_04_sortino_ratio_negative_excess_with_zero_downside() -> None:
    # Returns flat at 0.0% when target is 0.0% and risk free is 2.0%
    # Downside deviation is 0.0, but mean excess is negative (-2.0/12% < 0)
    flat_returns = [0.0, 0.0, 0.0, 0.0]
    sortino_flat = sortino_ratio(
        returns_pct=flat_returns,
        risk_free_rate_annual_pct=2.0,
        target_downside_pct=0.0,
        periods_per_year=12,
    )
    assert sortino_flat == 0.0


# ============================================================================
# 3. AUD-SIM-01: Fisher Exact Equation and SRR Real vs Nominal Separation
# ============================================================================


def test_aud_sim_01_fisher_exact_conversion() -> None:
    # Nominal 7.0% with 2.5% inflation
    # Exact real rate = (1 + 0.07) / (1 + 0.025) - 1 = 1.07 / 1.025 - 1 = 4.3902439%
    real = nominal_to_real_rate(7.0, 2.5)
    assert real == pytest.approx(4.3902, abs=0.0001)

    # Invert back to nominal: (1 + 0.043902) * (1 + 0.025) - 1 = 0.06999955 = 7.0%
    nominal = real_to_nominal_rate(real, 2.5)
    assert nominal == pytest.approx(7.0, abs=0.001)


def test_aud_sim_01_srr_nominal_and_real_divergence() -> None:
    # 30-year simulation with constant positive return and safe withdrawal
    res = FirePlanningEngine.simulate_sequence_of_returns_risk(
        initial_capital=1_000_000.0,
        initial_annual_withdrawal=30_000.0,  # 3% safe SWR
        duration_years=30,
        average_real_return_pct=5.0,
        early_crash_scenario="baseline_steady",
        annual_inflation_pct=2.5,
    )
    assert res.portfolio_survived is True
    assert res.final_capital_real > 0.0

    # At 2.5% annual inflation over 30 years, nominal capital must be strictly higher than real capital
    # Compound inflation multiplier = (1 + 0.025)^30 ~= 2.097567
    expected_nominal = res.final_capital_real * (1.025 ** 30)
    assert res.final_capital_nominal > res.final_capital_real
    assert res.final_capital_nominal == pytest.approx(expected_nominal, rel=1e-2)


# ============================================================================
# 4. AUD-SIM-02: Guyton-Klinger Withdrawal Floor and Series Length
# ============================================================================


def test_aud_sim_02_guyton_klinger_withdrawal_floor() -> None:
    # Prolonged crash: 8 years of severe negative returns (-30% per year)
    # With 10% preservation cuts every year, withdrawal would drop to 0.9^8 = 43% without a floor.
    # With min_floor_pct_of_initial = 0.80 (80%), withdrawal must NEVER drop below 80% of initial.
    initial_withdrawal = 10_000.0
    history = FireService.simulate_guyton_klinger_drawdown(
        initial_capital=200_000.0,
        initial_annual_withdrawal=initial_withdrawal,
        annual_returns=[-30.0] * 8,
        annual_inflation_rates=[2.0] * 8,
        initial_swr_pct=5.0,
        min_floor_pct_of_initial=0.80,
    )
    min_floor = initial_withdrawal * 0.80
    for row in history:
        if row["ending_capital"] > 0:
            assert row["withdrawal"] >= min_floor, (
                f"Year {row['year']} withdrawal {row['withdrawal']} dropped below floor {min_floor}"
            )


def test_aud_sim_02_guyton_klinger_preserves_full_series_when_depleted() -> None:
    # 5-year simulation where capital depletes rapidly in year 2
    history = FirePlanningEngine.simulate_guyton_klinger_drawdown(
        initial_capital=10_000.0,
        initial_annual_withdrawal=8_000.0,
        annual_returns=[-50.0, -50.0, -50.0, -50.0, -50.0],
        annual_inflation_rates=[2.0, 2.0, 2.0, 2.0, 2.0],
        initial_swr_pct=80.0,  # Actual initial cash withdrawal / initial capital.
    )
    # Must contain exactly 5 years without early truncation
    assert len(history) == 5, f"Expected full length 5, got {len(history)}"
    # Depleted years have ending_capital 0.0 and withdrawal 0.0
    assert history[-1]["ending_capital"] == 0.0
    assert history[-1]["withdrawal"] == 0.0
    assert history[-1]["rule_triggered"] == "Capital Esgotado"
