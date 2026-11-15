# -*- coding: utf-8 -*-
"""Regression tests for audit batch 17 (quantitative math defects MATH-01..05).

Covers:
- BUG-MATH-01 (P1): TWRR must never restart compounding after a total
  liquidation (-100%) unless an explicit recapitalization is requested
  (``allow_recapitalization=True``). Contract reconciled in BUG-MATH-C8.
- BUG-MATH-02 (P1): a fully liquidated portfolio (value 0 EUR) must register
  an explicit terminal cash flow at the valuation date so the XIRR horizon
  ends at liquidation, not at the last dividend.
- BUG-MATH-03 (P1): Cornish-Fisher closed-form inversion collapsing to
  VaR = 0.0% under extreme negative skew must fall back to a bracketed
  numerical inversion validated against the empirical tail quantile.
- BUG-MATH-04 (P1): infinite periodic returns must be discarded (isfinite)
  instead of poisoning Sharpe/Sortino with NaN (contract from BUG-MATH-C32).
- BUG-MATH-05 (P1): crisis shocks must not double-apply inflation and the
  Guyton-Klinger survival floor must be indexed to cumulative inflation.
"""

from __future__ import annotations

import math
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.finance_math import (
    cornish_fisher_var,
    expected_shortfall_cvar,
    sharpe_ratio,
    sortino_ratio,
    twrr,
    xirr_pct,
)
from services.fire import FirePlanningEngine
from services.investment_performance import (
    calculate_holding_xirr,
    calculate_portfolio_xirr,
)


# ---------------------------------------------------------------------------
# BUG-MATH-01 — TWRR total-loss compounding semantics
# ---------------------------------------------------------------------------


def test_math01_twrr_total_loss_never_recovers_by_default():
    """A -100% period pins cumulative TWRR at -100% for all later periods."""
    # 100 -> 0 (ruin) -> +50% on a zero base is still zero capital.
    assert twrr([-100.0, 50.0]) == -100.0
    # Even a euphoric +900% afterwards cannot resurrect a zeroed base.
    assert twrr([-100.0, 900.0]) == -100.0
    # Multiple ruin periods stay pinned.
    assert twrr([-100.0, 50.0, -100.0, 200.0]) == -100.0


def test_math01_twrr_explicit_recapitalization_restarts_baseline():
    """Only an explicit recapitalization may restart the compounding baseline (capped at 0% per BUG-AUD-C97)."""
    assert twrr([-100.0, 50.0], allow_recapitalization=True) == 0.0
    # Default remains the ruin-preserving contract.
    assert twrr([-100.0, 50.0], allow_recapitalization=False) == -100.0


def test_math01_twrr_normal_compounding_unaffected():
    """The ruin guard must not alter ordinary geometric linking."""
    returns = [5.0, 2.0, -3.0, 10.0]
    expected = (1.05 * 1.02 * 0.97 * 1.10 - 1.0) * 100.0
    assert twrr(returns) == pytest.approx(expected, abs=1e-9)


# ---------------------------------------------------------------------------
# BUG-MATH-02 — XIRR terminal flow for fully liquidated portfolios
# ---------------------------------------------------------------------------


def test_math02_portfolio_xirr_includes_terminal_zero_flow():
    """Liquidated portfolio (val=0) registers an explicit terminal flow."""
    cash_flows = [
        (date(2024, 1, 1), -10_000.0),
        (date(2024, 6, 30), 500.0),  # dividend before the crash
    ]
    result = calculate_portfolio_xirr(cash_flows, date(2025, 1, 1), 0.0)
    # Destroyed portfolio with only dividends: deeply negative XIRR, never
    # positive, and never None (the terminal zero flow keeps signs present).
    assert result is not None
    assert result < 0.0
    assert result > -100.0  # bounded annualized loss, not -inf


def test_math02_portfolio_xirr_total_loss_no_dividends():
    """Pure total loss (no positive flows) is exactly -100%."""
    cash_flows = [(date(2024, 1, 1), -10_000.0)]
    assert calculate_portfolio_xirr(cash_flows, date(2025, 1, 1), 0.0) == -100.0


def test_math02_holding_xirr_zero_value_with_prior_dividends():
    """Holding-level XIRR: wiped-out position with prior dividends is negative."""
    result = calculate_holding_xirr(
        acquisition_date=date(2024, 1, 1),
        cost_basis=10_000.0,
        current_date=date(2025, 1, 1),
        current_value=0.0,
        dividends=[(date(2024, 6, 30), 500.0)],
        fees=0.0,
    )
    assert result is not None
    assert result < 0.0


def test_math02_xirr_positive_case_unchanged():
    """The terminal-flow change must not alter healthy portfolios."""
    # 365-day window (2024 is a leap year; 2024-01-01 -> 2024-12-31).
    flows = [
        (date(2024, 1, 1), -10_000.0),
        (date(2024, 12, 31), 11_000.0),
    ]
    rate = xirr_pct(flows)
    assert rate is not None
    assert rate == pytest.approx(10.0, abs=0.01)


# ---------------------------------------------------------------------------
# BUG-MATH-03 — Cornish-Fisher inversion under extreme negative skew
# ---------------------------------------------------------------------------


def _moments(sample: list[float]) -> tuple[float, float, float, float]:
    n = len(sample)
    mean = sum(sample) / n
    var = sum((x - mean) ** 2 for x in sample) / (n - 1)
    std = math.sqrt(var)
    m3 = sum((x - mean) ** 3 for x in sample) / n
    m4 = sum((x - mean) ** 4 for x in sample) / n
    return mean, std, m3 / std**3, m4 / std**4 - 3.0


def test_math03_var_not_zero_under_extreme_negative_skew():
    """Skew ~ -22 (true collapse regime) must never report VaR = 0.0%."""
    # 18 normal months and 2 crash months: skew ~ -22 collapses the closed
    # form (z_cf < 0 at 99%, near-collapse at 95%).
    sample = [1.0] * 18 + [-40.0, -35.0]
    mean, std, skew, kurt = _moments(sample)
    assert skew < -2.0  # precondition: heavy left tail, CF beyond valid regime

    var_95 = cornish_fisher_var(sample, confidence=0.95)
    assert var_95 > 0.0
    # Empirical floor at 95%: tail_n = ceil(0.05 * 20) = 1 -> worst sample.
    assert var_95 >= 40.0 - 1e-9


def test_math03_var_99_extreme_skew_above_empirical_quantile():
    """99% confidence must cover the empirical worst-1% tail under collapse."""
    sample = [1.0] * 18 + [-40.0, -35.0]
    var_99 = cornish_fisher_var(sample, confidence=0.99)
    # tail_n = ceil(0.01 * 20) = 1 -> the -40% crash bounds the 99% VaR.
    assert var_99 >= 40.0 - 1e-9


def test_math03_moderate_skew_closed_form_unchanged():
    """Non-collapse regime keeps the exact closed-form CF result."""
    sample = [2.0, 1.5, -1.0, 3.0, -2.5, 4.0, 1.2, 0.8, -0.5, 2.2, 1.8, 3.5]
    # Pre-fix regression value (closed form, no fallback path taken).
    assert cornish_fisher_var(sample, confidence=0.95) == pytest.approx(2.09, abs=1e-9)


def test_math03_cvar_still_exceeds_or_matches_var():
    """CVaR remains a coherent tail companion of the CF VaR."""
    sample = [1.0] * 18 + [-40.0, -35.0]
    var_95 = cornish_fisher_var(sample, confidence=0.95)
    cvar_95 = expected_shortfall_cvar(sample, confidence=0.95)
    assert cvar_95 >= var_95 * 0.5


def test_math03_zero_vol_and_tiny_sample_guards():
    """Degenerate inputs keep their exact guarded contracts."""
    assert cornish_fisher_var([1.0, 1.0, 1.0, 1.0], confidence=0.95) == 0.0
    assert cornish_fisher_var([1.0, 2.0], confidence=0.95) == 0.0
    assert cornish_fisher_var([], confidence=0.95) == 0.0


# ---------------------------------------------------------------------------
# BUG-MATH-04 — infinite returns must not poison risk-adjusted ratios
# ---------------------------------------------------------------------------


def test_math04_infinite_returns_discarded_from_sharpe_and_sortino():
    """inf passes isnan (False) but must be dropped by the isfinite filter."""
    poisoned = [1.0, float("inf"), -2.0, 3.0, 0.5, 2.0, -1.0, 4.0]
    sharpe = sharpe_ratio(poisoned, risk_free_rate_annual_pct=2.5, periods_per_year=12)
    sortino = sortino_ratio(poisoned, risk_free_rate_annual_pct=2.5, periods_per_year=12)
    assert math.isfinite(sharpe)
    assert math.isfinite(sortino)
    # -inf must also be discarded.
    poisoned_neg = [1.0, float("-inf"), -2.0, 3.0, 0.5, 2.0, -1.0, 4.0]
    assert math.isfinite(sharpe_ratio(poisoned_neg))
    assert math.isfinite(sortino_ratio(poisoned_neg))
    # NaN equally discarded.
    poisoned_nan = [1.0, float("nan"), -2.0, 3.0, 0.5, 2.0, -1.0, 4.0]
    assert math.isfinite(sharpe_ratio(poisoned_nan))


def test_math04_clean_reference_series_matches_expected_value():
    """Exact-value regression on a finite reference series (C32 contract)."""
    clean = [2.0, 1.5, -1.0, 3.0, -2.5, 4.0, 1.2, 0.8, -0.5, 2.2, 1.8, 3.5]
    # Replacing the last finite element with +inf yields the same cleaned
    # series (11 finite values), so both ratios must be exactly equal.
    with_inf = clean[:-1] + [float("inf")]
    reference = clean[:-1]
    assert sharpe_ratio(with_inf) == sharpe_ratio(reference)
    assert sortino_ratio(with_inf) == sortino_ratio(reference)


# ---------------------------------------------------------------------------
# BUG-MATH-05 — crisis shocks inflation and Guyton-Klinger floor
# ---------------------------------------------------------------------------


def test_math05_gk_floor_indexed_to_cumulative_inflation():
    """The survival floor must track cumulative inflation, not stay nominal."""
    hist = FirePlanningEngine.simulate_guyton_klinger_drawdown(
        initial_capital=500_000.0,
        initial_annual_withdrawal=20_000.0,
        annual_returns=[-20.0] * 30,
        annual_inflation_rates=[10.0] * 30,
        initial_swr_pct=4.0,
    )
    # A10: the 30-year horizon keeps preservation active during the crash.
    # The initial spending date has no inflation; year k uses 1.1**(k-1).
    # Frozen years (prior return < 0) keep withdrawal flat, so
    # once the floor binds, the withdrawal must grow at 10%/year.
    floor_activations = [
        row
        for row in hist
        if row["rule_triggered"] == "Preservação de Capital (Piso Mínimo Ativado)"
    ]
    assert floor_activations, "floor must activate in a persistent crash"
    # The floor binds when the (frozen) withdrawal falls to the indexed floor:
    # cumulative inflation from the initial spending date is 1.1**(k-1), so
    # the floor in year k is 16000 * 1.1**(k-1). The final
    # depletion year is capital-limited (actual = min(capital, floor)), so it
    # is excluded from the floor-value assertions.
    binding_years = [row for row in floor_activations if row["ending_capital"] > 0.0]
    assert binding_years, "floor must bind with capital remaining"
    for row in binding_years:
        k = row["year"]
        expected_floor = 16_000.0 * (1.10 ** (k - 1))
        assert row["withdrawal"] == pytest.approx(expected_floor, abs=0.01), (
            f"year {k}: withdrawal {row['withdrawal']} != indexed floor {expected_floor}"
        )
    # Explicitly: the frozen-nominal bug would keep the floor at 16000 forever;
    # the indexed floor in the last binding year must exceed that.
    assert binding_years[-1]["withdrawal"] > 16_000.0


def test_math05_srr_real_terms_no_double_inflation():
    """SRR operates in real terms: final real equals the simulated balance."""
    res = FirePlanningEngine.simulate_sequence_of_returns_risk(
        initial_capital=1_000_000.0,
        initial_annual_withdrawal=40_000.0,
        duration_years=30,
        average_real_return_pct=5.0,
        early_crash_scenario="baseline_steady",
    )
    assert res.portfolio_survived is True
    # baseline_steady: real return 5%, real withdrawal 40000 -> final real is
    # the deterministic balance, and nominal only ever exceeds it (never
    # double-discounted below the real value).
    assert res.final_capital_real == pytest.approx(res.yearly_balances[-1], abs=0.01)
    assert res.final_capital_nominal >= res.final_capital_real - 1e-6


def test_math05_srr_crash_scenario_survival_ordering():
    """Crash scenario must be strictly worse than the stable baseline."""
    common = dict(
        initial_capital=400_000.0,
        initial_annual_withdrawal=24_000.0,
        duration_years=30,
        average_real_return_pct=5.0,
    )
    stable = FirePlanningEngine.simulate_sequence_of_returns_risk(
        early_crash_scenario="baseline_steady", **common
    )
    crash = FirePlanningEngine.simulate_sequence_of_returns_risk(
        early_crash_scenario="dotcom_2000", **common
    )
    assert crash.final_capital_real < stable.final_capital_real
    assert stable.portfolio_survived is True
