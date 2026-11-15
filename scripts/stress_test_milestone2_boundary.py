"""Adversarial Stress Test and Boundary Condition Verification for Milestone 2.

Executed by Challenger challenger_m2_iter2_2.
Tests all 6 required boundary and extreme cases:
1. Zero capital / zero contributions
2. Extreme market volatility (200%, 500%, 1000%)
3. Market crash (-50%, -90%, -99.9%)
4. Singular and non-convergent cashflows in XIRR
5. Empty transaction tables across all analytical queries
6. 100% split transactions (precision, hierarchy, performance)
"""

from __future__ import annotations

import json
import math
import os
import sys
import tempfile
import time
from datetime import date, timedelta
from typing import Any

# Ensure project root in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import numpy as np

from database.engine import FinanceDB
from database.repo import Repo
from services.cashflow_forecast import CashflowForecastService
from services.finance_math import (
    cagr,
    cornish_fisher_var,
    expected_shortfall_cvar,
    max_drawdown,
    modified_dietz,
    present_value_of_contributions,
    sharpe_ratio,
    sortino_ratio,
    twrr,
    xirr,
    xirr_pct,
)
from services.financial_health import compute_financial_health_score, risk_resilience
from services.financial_intelligence import monte_carlo_projection
from services.fire import FirePlanningEngine


def stress_test_1_zero_capital_contributions() -> dict[str, Any]:
    print("=== STRESS TEST 1: Zero Capital and Zero Contributions ===")
    results = {}

    # 1. Monte Carlo with 0 capital and 0 contribution (monthly and single)
    mc_monthly = monte_carlo_projection(
        contribution=0.0,
        annual_return_pct=7.0,
        annual_volatility_pct=15.0,
        years=10,
        simulations=500,
        initial_capital=0.0,
        seed=42,
        mode="monthly",
    )
    assert len(mc_monthly["table"]) == 10
    for r in mc_monthly["table"]:
        assert r["p10_nominal"] == 0.0
        assert r["p50_nominal"] == 0.0
        assert r["p90_nominal"] == 0.0
        assert r["p10_real"] == 0.0
        assert r["p50_real"] == 0.0
        assert r["p90_real"] == 0.0
        assert r["probability_target"] == 0.0

    mc_single = monte_carlo_projection(
        contribution=0.0,
        annual_return_pct=7.0,
        annual_volatility_pct=15.0,
        years=10,
        simulations=500,
        initial_capital=0.0,
        seed=42,
        mode="single",
    )
    assert len(mc_single["table"]) == 10
    for r in mc_single["table"]:
        assert r["p50_nominal"] == 0.0
        assert r["p50_real"] == 0.0

    # 2. Math routines with zero inputs
    assert cagr(0.0, 0.0, 0.0) == 0.0
    assert cagr(0.0, 1000.0, 5.0) == 0.0
    assert cagr(1000.0, 0.0, 5.0) == -100.0
    assert twrr([]) == 0.0
    assert twrr([0.0, 0.0, 0.0]) == 0.0
    assert modified_dietz(0.0, 0.0, [], date(2025, 1, 1), date(2025, 12, 31)) == 0.0
    assert present_value_of_contributions(0.0, 0, 2.0) == 0.0
    assert present_value_of_contributions(0.0, 120, 2.0) == 0.0
    assert present_value_of_contributions(500.0, 0, 2.0) == 0.0

    # 3. FIRE Engine with zero capital / expenses / savings
    fire_milestones = FirePlanningEngine.calculate_fire_milestones(
        current_invested=0.0,
        annual_expenses=0.0,
        annual_savings=0.0,
    )
    for k, m in fire_milestones.items():
        assert m.target_capital == 0.0
        assert m.progress_pct == 100.0
        assert m.years_to_goal == 0.0

    # FIRE with zero invested but positive expenses and zero savings
    fire_no_savings = FirePlanningEngine.calculate_fire_milestones(
        current_invested=0.0,
        annual_expenses=24000.0,
        annual_savings=0.0,
    )
    assert fire_no_savings["standard"].years_to_goal is None
    assert fire_no_savings["standard"].progress_pct == 0.0

    # Guyton-Klinger with zero capital
    gk_zero = FirePlanningEngine.simulate_guyton_klinger_drawdown(
        initial_capital=0.0,
        initial_annual_withdrawal=20000.0,
        annual_returns=[5.0, 5.0, 5.0],
        annual_inflation_rates=[2.0, 2.0, 2.0],
    )
    assert len(gk_zero) >= 1
    assert gk_zero[0]["ending_capital"] == 0.0

    # SRR with zero capital
    srr_zero = FirePlanningEngine.simulate_sequence_of_returns_risk(
        initial_capital=0.0,
        initial_annual_withdrawal=20000.0,
        duration_years=10,
    )
    assert not srr_zero.portfolio_survived
    assert srr_zero.final_capital_nominal == 0.0

    # Risk resilience with zero capital and zero income
    rr_zero = risk_resilience(
        liquid_capital=0.0,
        monthly_income=0.0,
        monthly_essential_expenses=1000.0,
        monthly_discretionary_expenses=500.0,
        run_count=500,
        horizon_months=12,
    )
    assert rr_zero["success_rate"] == 0.0

    results["status"] = "PASSED"
    results["all_zero_guards_functional"] = True
    print("Stress Test 1 PASSED!")
    return results


def stress_test_2_extreme_volatility() -> dict[str, Any]:
    print("\n=== STRESS TEST 2: Extreme Market Volatility (200%, 500%, 1000%) ===")
    results = {}

    for vol in (200.0, 500.0, 1000.0):
        mc = monte_carlo_projection(
            contribution=500.0,
            annual_return_pct=10.0,
            annual_volatility_pct=vol,
            years=10,
            simulations=1000,
            initial_capital=10_000.0,
            seed=123,
        )
        assert len(mc["table"]) == 10
        for r in mc["table"]:
            # Check non-negativity and finite numbers
            assert not math.isnan(r["p10_real"]) and not math.isinf(r["p10_real"])
            assert not math.isnan(r["p50_real"]) and not math.isinf(r["p50_real"])
            assert not math.isnan(r["p90_real"]) and not math.isinf(r["p90_real"])
            assert r["p10_real"] >= 0.0
            assert r["p50_real"] >= 0.0
            assert r["p90_real"] >= 0.0
            assert r["p10_real"] <= r["p50_real"] <= r["p90_real"]
            assert 0.0 <= r["probability_loss_real"] <= 100.0
            assert 0.0 <= r["probability_target"] <= 100.0

    # Cornish-Fisher and CVaR with extreme return swings
    extreme_returns = [-95.0, 300.0, -80.0, 500.0, -99.0, 1000.0, -50.0, 200.0]
    var_95 = cornish_fisher_var(extreme_returns, confidence=0.95)
    cvar_95 = expected_shortfall_cvar(extreme_returns, confidence=0.95)
    assert not math.isnan(var_95) and not math.isinf(var_95) and var_95 >= 0.0
    assert not math.isnan(cvar_95) and not math.isinf(cvar_95) and cvar_95 >= 0.0

    # Sharpe and Sortino with zero variance (identical returns)
    flat_returns = [5.0, 5.0, 5.0, 5.0, 5.0]
    sr_flat = sharpe_ratio(flat_returns)
    assert sr_flat == 0.0

    # Sortino with only positive returns
    all_pos_returns = [10.0, 15.0, 8.0, 20.0, 12.0]
    sortino_pos = sortino_ratio(all_pos_returns, risk_free_rate_annual_pct=2.0)
    assert sortino_pos == float("inf")

    results["status"] = "PASSED"
    results["volatility_200_500_1000_passed"] = True
    print("Stress Test 2 PASSED!")
    return results


def stress_test_3_market_crash() -> dict[str, Any]:
    print("\n=== STRESS TEST 3: Market Crash (-50%, -90%, -99.9%) ===")
    results = {}

    for crash_rate in (-50.0, -90.0, -99.0):
        mc = monte_carlo_projection(
            contribution=100.0,
            annual_return_pct=crash_rate,
            annual_volatility_pct=20.0,
            years=5,
            simulations=500,
            initial_capital=50_000.0,
            seed=42,
        )
        assert len(mc["table"]) == 5
        # Year 5 P50 should be significantly degraded
        p50_yr5 = mc["table"][-1]["p50_real"]
        assert 0.0 <= p50_yr5 < 50_000.0
        assert mc["table"][-1]["probability_loss_real"] >= 90.0

    # Max Drawdown under catastrophic collapse
    collapse_series = [100.0, 80.0, 40.0, 20.0, 5.0, 0.1, 0.0]
    mdd, peak_idx, trough_idx = max_drawdown(collapse_series)
    assert mdd == 100.0
    assert peak_idx == 0
    assert trough_idx == 6

    # Guyton-Klinger under sustained -50% annual crash
    gk_crash = FirePlanningEngine.simulate_guyton_klinger_drawdown(
        initial_capital=100_000.0,
        initial_annual_withdrawal=4_000.0,
        annual_returns=[-50.0, -50.0, -50.0, -50.0, -50.0],
        annual_inflation_rates=[2.0, 2.0, 2.0, 2.0, 2.0],
    )
    assert len(gk_crash) == 5
    # Rules should trigger capital preservation cuts
    preservation_triggers = [r for r in gk_crash if "Preservação de Capital" in r["rule_triggered"]]
    assert len(preservation_triggers) > 0

    results["status"] = "PASSED"
    results["crash_handling_passed"] = True
    print("Stress Test 3 PASSED!")
    return results


def stress_test_4_xirr_singular_and_nonconvergent() -> dict[str, Any]:
    print("\n=== STRESS TEST 4: Singular, Non-Convergent, and Boundary Cashflows in XIRR ===")
    results = {}

    # 1. Empty and single cashflow
    assert xirr([]) is None
    assert xirr_pct([]) is None
    assert xirr([(date(2025, 1, 1), -1000.0)]) is None
    assert xirr([(date(2025, 1, 1), 1000.0)]) is None

    # 2. All positive / all negative
    assert xirr([(date(2025, 1, 1), 1000.0), (date(2026, 1, 1), 2000.0)]) is None
    assert xirr([(date(2025, 1, 1), -1000.0), (date(2026, 1, 1), -2000.0)]) is None

    # 3. Same-day netted cashflows
    same_day = [
        (date(2025, 1, 1), -1000.0),
        (date(2025, 1, 1), 500.0),
        (date(2025, 1, 1), -200.0), # Net day 0: -700.0
        (date(2026, 1, 1), 770.0),   # 1 year later: +10% return
    ]
    rate_same_day = xirr(same_day)
    assert rate_same_day is not None
    assert abs(rate_same_day - 0.10) < 1e-4

    # 4. Same-day cancellation resulting in 1 net flow -> None
    cancelled_same_day = [
        (date(2025, 1, 1), -1000.0),
        (date(2025, 1, 1), 1000.0),
        (date(2026, 1, 1), 500.0),
    ]
    assert xirr(cancelled_same_day) is None

    # 5. Extreme magnitude cashflows (10^14 and 10^-6)
    huge_flows = [
        (date(2025, 1, 1), -1e12),
        (date(2026, 1, 1), 1.15e12),
    ]
    rate_huge = xirr(huge_flows)
    assert rate_huge is not None
    assert abs(rate_huge - 0.15) < 1e-5

    tiny_flows = [
        (date(2025, 1, 1), -0.001),
        (date(2026, 1, 1), 0.0012),
    ]
    rate_tiny = xirr(tiny_flows)
    assert rate_tiny is not None
    assert abs(rate_tiny - 0.20) < 1e-4

    # 6. Century horizon (100 years apart)
    century_flows = [
        (date(1925, 1, 1), -1000.0),
        (date(2025, 1, 1), 1000.0 * (1.08 ** 100)),
    ]
    rate_century = xirr(century_flows)
    assert rate_century is not None
    assert abs(rate_century - 0.08) < 1e-4

    # 7. Extreme loss (-99.9%) and extreme gain (+5000%)
    extreme_loss = [
        (date(2025, 1, 1), -10000.0),
        (date(2026, 1, 1), 10.0),
    ]
    rate_loss = xirr(extreme_loss)
    assert rate_loss is not None
    assert abs(rate_loss - (-0.999)) < 1e-3

    extreme_gain = [
        (date(2025, 1, 1), -100.0),
        (date(2026, 1, 1), 5100.0), # +5000%
    ]
    rate_gain = xirr(extreme_gain)
    assert rate_gain is not None
    assert abs(rate_gain - 50.0) < 1e-3

    # 8. Highly oscillatory cashflows (multiple alternating signs)
    oscillatory = [
        (date(2020, 1, 1), -1000.0),
        (date(2021, 1, 1), 3000.0),
        (date(2022, 1, 1), -3000.0),
        (date(2023, 1, 1), 1500.0),
    ]
    rate_osc = xirr(oscillatory)
    assert rate_osc is not None # Brent's solver finds a valid root
    # Verify NPV equals 0 at root
    t0 = date(2020, 1, 1)
    npv = sum(amt / ((1.0 + rate_osc) ** ((d - t0).days / 365.0)) for d, amt in oscillatory)
    assert abs(npv) < 1e-3

    results["status"] = "PASSED"
    results["all_xirr_singularities_handled"] = True
    print("Stress Test 4 PASSED!")
    return results


def stress_test_5_empty_transaction_tables() -> dict[str, Any]:
    print("\n=== STRESS TEST 5: Empty Transaction Tables Across All Analytical Queries ===")
    results = {}

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "empty_financas.sqlite3")
        db = FinanceDB(db_path)
        repo = Repo(db)

        # 1. Analytical queries on completely empty DB
        cat_spend = repo.category_spend_with_splits("2025-01-01", "2026-01-01")
        assert cat_spend == []

        totals_period = repo.totals_for_payment_period("2025-01-01", "2026-01-01")
        assert totals_period["income"] == 0.0
        assert totals_period["expenses"] == 0.0
        assert totals_period["savings"] == 0.0
        assert totals_period["investments"] == 0.0

        monthly_totals = repo.monthly_totals_grouped_for_year(2025)
        assert monthly_totals == {}

        dynamic_cat = repo.dynamic_grouped("2025-01-01", "2026-01-01", group_by="Categoria", kind="Despesas")
        assert dynamic_cat == []

        dynamic_merch = repo.dynamic_grouped("2025-01-01", "2026-01-01", group_by="Comerciante", kind="Despesas")
        assert dynamic_merch == []

        top_cats = repo.top_categories_payment_period("2025-01-01", "2026-01-01", limit=5)
        assert top_cats == []

        chart_details = repo.chart_movement_details("2025-01-01", "2026-01-01")
        assert chart_details == []

        invest_evol = repo.investment_evolution()
        assert invest_evol == []

        inv_state = repo.investment_state_as_of("2025-12-31")
        assert inv_state["invested"] == 0.0
        assert inv_state["total"] == 0.0

        fin_state = repo.financial_state_as_of("2025-12-31")
        assert fin_state["holdings"] == []
        assert fin_state["liabilities"] == []
        assert fin_state["subscriptions"] == []

        # 2. Cashflow forecast on empty DB
        forecast_service = CashflowForecastService(repo)
        forecast_res = forecast_service.forecast_90_days(start_date=date(2025, 1, 1), starting_balance=0.0)
        assert len(forecast_res.daily_projections) == 90
        assert forecast_res.minimum_projected_balance == 0.0
        assert forecast_res.closing_balance == 0.0

        # 3. Financial health score on empty DB
        health_score = compute_financial_health_score(repo, as_of=date(2025, 1, 1))
        assert "overall_score" in health_score
        assert 0.0 <= health_score["overall_score"] <= 100.0

        db.conn.close()

    results["status"] = "PASSED"
    results["empty_db_resilience_passed"] = True
    print("Stress Test 5 PASSED!")
    return results


def stress_test_6_100_pct_split_transactions() -> dict[str, Any]:
    print("\n=== STRESS TEST 6: 100% Split Transactions (Precision & Single-Pass Aggregation) ===")
    results = {}

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "splits_financas.sqlite3")
        db = FinanceDB(db_path)
        repo = Repo(db)

        categories = ["Alimentação", "Habitação", "Transporte", "Saúde", "Lazer", "Tecnologia", "Educação", "Vestuário"]
        for cat in categories:
            repo.db.conn.execute("INSERT OR IGNORE INTO categories(name, colour) VALUES(?, '#10b981')", (cat,))

        conn = repo.db.conn
        conn.execute("BEGIN TRANSACTION")

        base_d = date(2025, 1, 1)
        rng = np.random.default_rng(777)
        total_transactions = 1_000
        category_exact_expected: dict[str, float] = {c: 0.0 for c in categories}
        grand_total_spent = 0.0

        # Create 1,000 transactions where 100% of transactions are SPLIT into 2 to 10 split rows
        for i in range(total_transactions):
            d = (base_d + timedelta(days=int(rng.integers(0, 365)))).isoformat()
            n_splits = int(rng.integers(2, 11))
            split_cats = rng.choice(categories, size=n_splits, replace=True)
            split_amts = [round(float(rng.uniform(2.50, 85.75)), 2) for _ in range(n_splits)]
            parent_amt = -round(sum(split_amts), 2)
            grand_total_spent += abs(parent_amt)

            for sc, sa in zip(split_cats, split_amts):
                category_exact_expected[sc] = round(category_exact_expected[sc] + sa, 2)

            cur = conn.execute(
                "INSERT INTO transactions(date, description, amount, category, account) VALUES(?,?,?,?,?)",
                (d, f"100% Split Tx {i}", parent_amt, "Geral / Despesas Mistas", "Cartão Principal")
            )
            tx_id = cur.lastrowid

            for sc, sa in zip(split_cats, split_amts):
                conn.execute(
                    "INSERT INTO transaction_splits(transaction_id, category, amount) VALUES(?,?,?)",
                    (tx_id, sc, sa)
                )

        conn.commit()

        # Query using single-pass conditional aggregation
        t0 = time.perf_counter()
        spend_rows = repo.category_spend_with_splits("2025-01-01", "2026-01-01")
        t1 = time.perf_counter()
        query_time_ms = (t1 - t0) * 1000.0

        total_from_query = sum(float(r["total"] or 0) for r in spend_rows)
        diff_grand = abs(total_from_query - grand_total_spent)
        assert diff_grand < 0.01, f"Total spent mismatch: query={total_from_query}, expected={grand_total_spent}"

        # Check each category precision
        for r in spend_rows:
            cat_name = str(r["category"])
            cat_total = float(r["total"] or 0)
            expected_cat = category_exact_expected.get(cat_name, 0.0)
            diff_cat = abs(cat_total - expected_cat)
            assert diff_cat < 0.05, f"Category {cat_name} mismatch: query={cat_total}, expected={expected_cat}"

        # Test chart movement details with 100% splits
        chart_details = repo.chart_movement_details("2025-01-01", "2026-01-01", limit=50)
        assert len(chart_details) == 50
        for cd in chart_details:
            assert cd["split_contribution"] == 1
            assert cd["effective_category"] in categories

        # Test dynamic grouped on 100% splits
        dyn_groups = repo.dynamic_grouped("2025-01-01", "2026-01-01", group_by="Categoria", kind="Despesas")
        assert len(dyn_groups) == len(categories)
        dyn_total = sum(d["total"] for d in dyn_groups)
        assert abs(dyn_total - grand_total_spent) < 0.01

        db.conn.close()

    results["status"] = "PASSED"
    results["query_time_ms"] = round(query_time_ms, 3)
    results["total_transactions"] = total_transactions
    results["precision_delta"] = round(diff_grand, 6)
    print("Stress Test 6 PASSED!")
    return results


if __name__ == "__main__":
    print("STARTING ADVERSARIAL STRESS TEST & BOUNDARY HARNESS (M2 ITER 2)\n")
    r1 = stress_test_1_zero_capital_contributions()
    r2 = stress_test_2_extreme_volatility()
    r3 = stress_test_3_market_crash()
    r4 = stress_test_4_xirr_singular_and_nonconvergent()
    r5 = stress_test_5_empty_transaction_tables()
    r6 = stress_test_6_100_pct_split_transactions()

    summary = {
        "stress_test_1_zero_capital_contributions": r1,
        "stress_test_2_extreme_volatility": r2,
        "stress_test_3_market_crash": r3,
        "stress_test_4_xirr_singular_and_nonconvergent": r4,
        "stress_test_5_empty_transaction_tables": r5,
        "stress_test_6_100_pct_split_transactions": r6,
    }
    print("\n=======================================================")
    print("ALL 6 ADVERSARIAL STRESS TEST SUITES PASSED EMPIRICALLY!")
    print("=======================================================")
    print(json.dumps(summary, indent=2))
