"""Milestone 2 Empirical Benchmark and Adversarial Stress Harness.

Executed by Challenger challenger_m2_1.
"""

from __future__ import annotations

import gc
import json
import os
import statistics
import sys
import tempfile
import time
from datetime import date, timedelta
from typing import Any

# Ensure project root in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import numpy as np

from database.db import Database
from database.repo import Repo
from services.finance_math import xirr
from services.financial_health import risk_resilience
from services.financial_intelligence import monte_carlo_projection
from services.fire import simulate_guyton_klinger_sensitivity_grid


def benchmark_monte_carlo() -> dict[str, Any]:
    print("=== 1. Benchmarking Monte Carlo Projection ===")
    runs = 5_000
    years = 30
    trials = 10
    timings = []

    # Warmup
    _ = monte_carlo_projection(
        contribution=500.0,
        annual_return_pct=7.0,
        annual_volatility_pct=15.0,
        years=years,
        simulations=runs,
        initial_capital=10_000.0,
        seed=42,
    )

    gc.disable()
    for _ in range(trials):
        t0 = time.perf_counter()
        res = monte_carlo_projection(
            contribution=500.0,
            annual_return_pct=7.0,
            annual_volatility_pct=15.0,
            years=years,
            simulations=runs,
            initial_capital=10_000.0,
            seed=42,
        )
        t1 = time.perf_counter()
        timings.append((t1 - t0) * 1000.0)
    gc.enable()

    mean_ms = statistics.mean(timings)
    median_ms = statistics.median(timings)
    min_ms = min(timings)
    max_ms = max(timings)
    stdev_ms = statistics.stdev(timings)

    # Invariant checks
    rows = res["table"]
    assert len(rows) == 30, f"Expected 30 year rows, got {len(rows)}"
    invariants_passed = True
    for r in rows:
        # Check percentile ordering
        if not (r["p10_real"] <= r["p50_real"] <= r["p90_real"]):
            invariants_passed = False
        if not (r["p10_nominal"] <= r["p50_nominal"] <= r["p90_nominal"]):
            invariants_passed = False
        if not (0.0 <= r["probability_target"] <= 100.0):
            invariants_passed = False
        if not (0.0 <= r["probability_loss_real"] <= 100.0):
            invariants_passed = False
        if not (0.0 <= r["probability_loss_nominal"] <= 100.0):
            invariants_passed = False

    # Check reproducibility
    res2 = monte_carlo_projection(
        contribution=500.0,
        annual_return_pct=7.0,
        annual_volatility_pct=15.0,
        years=years,
        simulations=runs,
        initial_capital=10_000.0,
        seed=42,
    )
    exact_reproducibility = res["table"] == res2["table"]

    # Total simulated month-steps: 5000 runs * 30 years * 12 months = 1,800,000
    monthly_steps = runs * years * 12
    steps_per_sec = (monthly_steps / (mean_ms / 1000.0))

    result = {
        "runs": runs,
        "years": years,
        "trials": trials,
        "mean_ms": round(mean_ms, 3),
        "median_ms": round(median_ms, 3),
        "min_ms": round(min_ms, 3),
        "max_ms": round(max_ms, 3),
        "stdev_ms": round(stdev_ms, 3),
        "steps_per_sec": round(steps_per_sec, 0),
        "invariants_passed": invariants_passed,
        "exact_reproducibility": exact_reproducibility,
    }
    print(f"Monte Carlo Results: {json.dumps(result, indent=2)}")
    return result


def benchmark_xirr() -> dict[str, Any]:
    print("\n=== 2. Benchmarking XIRR Solver ===")
    # Generate 100 realistic random cashflow series
    rng = np.random.default_rng(12345)
    series_list = []
    base_date = date(2020, 1, 1)

    for i in range(100):
        n_flows = int(rng.integers(5, 30))
        # First flow is negative (initial investment)
        initial_inv = -float(rng.uniform(1000.0, 50000.0))
        flows = [(base_date, initial_inv)]
        curr_d = base_date
        cum_invested = -initial_inv
        
        for _ in range(n_flows - 2):
            curr_d = curr_d + timedelta(days=int(rng.integers(15, 90)))
            # Either a contribution (-), dividend (+), or partial withdrawal (+)
            flow_type = rng.choice(["contrib", "div"], p=[0.7, 0.3])
            if flow_type == "contrib":
                amt = -float(rng.uniform(100.0, 2000.0))
                cum_invested += -amt
            else:
                amt = float(rng.uniform(10.0, 500.0))
            flows.append((curr_d, amt))
            
        # Final terminal value (positive)
        curr_d = curr_d + timedelta(days=int(rng.integers(30, 180)))
        # Target CAGR between -15% and +35%
        elapsed_years = (curr_d - base_date).days / 365.25
        target_mult = (1.0 + rng.uniform(-0.15, 0.35)) ** elapsed_years
        final_val = max(100.0, cum_invested * target_mult)
        flows.append((curr_d, float(final_val)))
        series_list.append(flows)

    # Warmup
    for s in series_list[:5]:
        _ = xirr(s)

    # Benchmark latency
    timings = []
    results = []
    npv_residuals = []

    for s in series_list:
        t0 = time.perf_counter()
        rate = xirr(s)
        t1 = time.perf_counter()
        timings.append((t1 - t0) * 1_000_000.0) # microseconds
        results.append(rate)

        # Check precision: compute NPV at rate
        assert rate is not None, f"XIRR failed to converge on series: {s}"
        first_d = s[0][0]
        npv = sum(amt / ((1.0 + rate) ** ((d - first_d).days / 365.0)) for d, amt in s)
        npv_residuals.append(abs(npv))

    total_time_ms = sum(timings) / 1000.0
    mean_us = statistics.mean(timings)
    median_us = statistics.median(timings)
    min_us = min(timings)
    max_us = max(timings)
    max_npv_error = max(npv_residuals)
    mean_npv_error = statistics.mean(npv_residuals)

    # Analytical test case: exactly 10% annual return
    test_10pct = [(date(2025, 1, 1), -1000.0), (date(2026, 1, 1), 1100.0)]
    rate_10pct = xirr(test_10pct)
    assert rate_10pct is not None and abs(rate_10pct - 0.10) < 1e-6

    # Edge cases
    # All positive -> None
    assert xirr([(date(2025, 1, 1), 1000.0), (date(2026, 1, 1), 1100.0)]) is None
    # All negative -> None
    assert xirr([(date(2025, 1, 1), -1000.0), (date(2026, 1, 1), -1100.0)]) is None
    # Empty -> None
    assert xirr([]) is None
    # Single item -> None
    assert xirr([(date(2025, 1, 1), -1000.0)]) is None

    result = {
        "series_count": len(series_list),
        "total_time_ms": round(total_time_ms, 3),
        "mean_us": round(mean_us, 2),
        "median_us": round(median_us, 2),
        "min_us": round(min_us, 2),
        "max_us": round(max_us, 2),
        "max_npv_residual": float(f"{max_npv_error:.2e}"),
        "mean_npv_residual": float(f"{mean_npv_error:.2e}"),
        "precision_invariant_passed": max_npv_error < 1e-4,
        "analytical_10pct_passed": abs(rate_10pct - 0.10) < 1e-6,
    }
    print(f"XIRR Results: {json.dumps(result, indent=2)}")
    return result


def benchmark_category_spend_with_splits() -> dict[str, Any]:
    print("\n=== 3. Benchmarking Category Spend with Splits ===")
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "bench_financas.sqlite3")
        db = Database(db_path)
        repo = Repo(db)

        # Populate with 10,000 transactions, 2,000 split transactions
        categories = ["Alimentação", "Habitação", "Transporte", "Saúde", "Lazer", "Tecnologia", "Vestuário", "Educação"]
        for cat in categories:
            repo.db.conn.execute("INSERT OR IGNORE INTO categories(name, colour) VALUES(?, '#ffffff')", (cat,))

        conn = repo.db.conn
        conn.execute("BEGIN TRANSACTION")
        
        base_d = date(2025, 1, 1)
        rng = np.random.default_rng(999)
        
        total_unsplits_amount = 0.0
        total_splits_amount = 0.0
        
        # 8,000 normal expense transactions
        for i in range(8_000):
            d = (base_d + timedelta(days=int(rng.integers(0, 365)))).isoformat()
            cat = rng.choice(categories)
            amt = -round(float(rng.uniform(5.0, 200.0)), 2)
            total_unsplits_amount += abs(amt)
            conn.execute(
                "INSERT INTO transactions(date, description, amount, category, account) VALUES(?,?,?,?,?)",
                (d, f"Tx {i}", amt, cat, "Conta Ordem")
            )
            
        # 2,000 split expense transactions
        for i in range(8000, 10_000):
            d = (base_d + timedelta(days=int(rng.integers(0, 365)))).isoformat()
            # Parent transaction has broad category "Geral"
            # 3 splits of specific categories
            split_cats = rng.choice(categories, size=3, replace=False)
            split_amounts = [round(float(rng.uniform(10.0, 50.0)), 2) for _ in range(3)]
            parent_amt = -round(sum(split_amounts), 2)
            
            cur = conn.execute(
                "INSERT INTO transactions(date, description, amount, category, account) VALUES(?,?,?,?,?)",
                (d, f"Split Tx {i}", parent_amt, "Geral", "Cartão")
            )
            tx_id = cur.lastrowid
            
            for sc, sa in zip(split_cats, split_amounts):
                total_splits_amount += sa
                conn.execute(
                    "INSERT INTO transaction_splits(transaction_id, category, amount) VALUES(?,?,?)",
                    (tx_id, sc, sa)
                )

        conn.commit()

        # Warmup
        _ = repo.category_spend_with_splits("2025-01-01", "2026-01-01")

        # Benchmark 50 runs
        trials = 50
        timings = []
        for _ in range(trials):
            t0 = time.perf_counter()
            rows = repo.category_spend_with_splits("2025-01-01", "2026-01-01")
            t1 = time.perf_counter()
            timings.append((t1 - t0) * 1000.0)

        mean_ms = statistics.mean(timings)
        median_ms = statistics.median(timings)
        min_ms = min(timings)
        max_ms = max(timings)

        # Integrity Check: Sum of category totals must equal total_unsplits_amount + total_splits_amount
        sum_query_total = sum(float(r["total"] or 0) for r in rows)
        expected_total = total_unsplits_amount + total_splits_amount
        diff = abs(sum_query_total - expected_total)
        reconciliation_passed = diff < 0.05

        result = {
            "total_transactions": 10_000,
            "split_transactions": 2_000,
            "trials": trials,
            "mean_ms": round(mean_ms, 3),
            "median_ms": round(median_ms, 3),
            "min_ms": round(min_ms, 3),
            "max_ms": round(max_ms, 3),
            "sum_query_total": round(sum_query_total, 2),
            "expected_total": round(expected_total, 2),
            "reconciliation_diff": round(diff, 4),
            "reconciliation_passed": reconciliation_passed,
        }
        print(f"Category Spend with Splits Results: {json.dumps(result, indent=2)}")
        return result


def benchmark_financial_health_and_fire() -> dict[str, Any]:
    print("\n=== 4. Benchmarking Financial Health & FIRE Vectorizations ===")
    # 1. Risk resilience (2000 runs, 24 months)
    timings_rr = []
    for _ in range(10):
        t0 = time.perf_counter()
        rr = risk_resilience(
            liquid_capital=10_000.0,
            monthly_income=2_500.0,
            monthly_essential_expenses=1_500.0,
            monthly_discretionary_expenses=500.0,
            run_count=2000,
            horizon_months=24,
            seed=42,
        )
        t1 = time.perf_counter()
        timings_rr.append((t1 - t0) * 1000.0)

    # 2. FIRE sensitivity grid
    timings_fire = []
    for _ in range(10):
        t0 = time.perf_counter()
        _ = simulate_guyton_klinger_sensitivity_grid(
            initial_portfolio=500_000.0,
            initial_withdrawal=20_000.0,
            years=30,
            simulations=500,
        )
        t1 = time.perf_counter()
        timings_fire.append((t1 - t0) * 1000.0)

    result = {
        "risk_resilience_mean_ms": round(statistics.mean(timings_rr), 3),
        "risk_resilience_median_ms": round(statistics.median(timings_rr), 3),
        "fire_sensitivity_mean_ms": round(statistics.mean(timings_fire), 3),
        "fire_sensitivity_median_ms": round(statistics.median(timings_fire), 3),
        "risk_resilience_success_rate": rr["success_rate"],
    }
    print(f"Health & FIRE Results: {json.dumps(result, indent=2)}")
    return result


if __name__ == "__main__":
    print("STARTING CHALLENGER M2 BENCHMARK SUITE\n")
    mc_res = benchmark_monte_carlo()
    xirr_res = benchmark_xirr()
    split_res = benchmark_category_spend_with_splits()
    health_fire_res = benchmark_financial_health_and_fire()
    print("\nALL BENCHMARKS COMPLETED SUCCESSFULLY!")
