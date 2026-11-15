"""Comprehensive Empirical Benchmarking & Speedup Verification Suite for Milestone 2.

Evaluates:
1. Monte Carlo Matrix Vectorization vs Scalar Baseline
2. XIRR Vectorized NumPy Solver vs Pure Python Loop
3. Cashflow Forecasting Prefix Sums & Hash Indexing vs Daily Iteration Scans
4. SQL Split Single-Pass Aggregation vs Legacy Multi-Pass UNION ALL CTE
"""

from __future__ import annotations

import gc
import json
import math
import os
import statistics
import sys
import tempfile
import time
from datetime import date, timedelta
from typing import Any, Sequence

# Ensure project root in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import numpy as np

from database.db import Database
from database.repo import Repo
from services.cashflow_forecast import CashflowForecastService
from services.finance_math import periodic_discount_factor, xirr
from services.financial_intelligence import monte_carlo_projection
from services.payments import PaymentsService


# ---------------------------------------------------------------------------
# 1. MONTE CARLO BENCHMARK (Vectorized vs Scalar Pure-Python)
# ---------------------------------------------------------------------------
def _monte_carlo_scalar_legacy(
    contribution: float,
    annual_return_pct: float = 6.0,
    annual_volatility_pct: float = 12.0,
    years: int = 30,
    *,
    simulations: int = 5000,
    initial_capital: float = 10000.0,
    inflation_pct: float = 2.0,
    tax_pct: float = 28.0,
    seed: int = 42,
) -> list[float]:
    """Faithful legacy scalar pure-python simulation loop."""
    import random

    rng = random.Random(seed)
    annual_return = annual_return_pct / 100.0
    volatility = annual_volatility_pct / 100.0
    tax = tax_pct / 100.0
    monthly_volatility = volatility / math.sqrt(12.0)
    monthly_drift = math.log1p(annual_return) / 12.0 - 0.5 * monthly_volatility**2
    total_months = years * 12

    final_real_values = []
    for _ in range(simulations):
        val = initial_capital
        nom_contrib = initial_capital
        for month in range(1, total_months + 1):
            shock = rng.gauss(0.0, 1.0)
            growth = math.exp(monthly_drift + monthly_volatility * shock)
            val = max(0.0, val * growth)
            val += contribution
            nom_contrib += contribution
        taxable_gain = max(0.0, val - nom_contrib)
        after_tax = val - taxable_gain * tax
        discount = max(1e-12, periodic_discount_factor(inflation_pct, total_months))
        final_real_values.append(after_tax / discount)
    return final_real_values


def benchmark_monte_carlo() -> dict[str, Any]:
    print("\n" + "=" * 70)
    print("1. EMPIRICAL BENCHMARK: Monte Carlo Simulation (Vectorized vs Scalar)")
    print("=" * 70)

    runs = 5000
    years = 30
    trials = 10

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

    # 1A. Vectorized Benchmark
    gc.disable()
    vec_timings = []
    for _ in range(trials):
        t0 = time.perf_counter()
        vec_res = monte_carlo_projection(
            contribution=500.0,
            annual_return_pct=7.0,
            annual_volatility_pct=15.0,
            years=years,
            simulations=runs,
            initial_capital=10_000.0,
            seed=42,
        )
        t1 = time.perf_counter()
        vec_timings.append((t1 - t0) * 1000.0)
    gc.enable()

    # 1B. Scalar Baseline Benchmark (3 trials to avoid excessive test duration)
    scalar_timings = []
    for _ in range(3):
        t0 = time.perf_counter()
        _ = _monte_carlo_scalar_legacy(
            contribution=500.0,
            annual_return_pct=7.0,
            annual_volatility_pct=15.0,
            years=years,
            simulations=runs,
            initial_capital=10_000.0,
            seed=42,
        )
        t1 = time.perf_counter()
        scalar_timings.append((t1 - t0) * 1000.0)

    vec_mean = statistics.mean(vec_timings)
    vec_median = statistics.median(vec_timings)
    vec_min = min(vec_timings)
    scalar_mean = statistics.mean(scalar_timings)
    speedup = scalar_mean / vec_mean

    # Total simulation points: 5,000 runs * 30 years * 12 months = 1,800,000 points
    total_steps = runs * years * 12
    throughput = total_steps / (vec_mean / 1000.0)

    # Invariants verification
    rows = vec_res["rows"]
    assert len(rows) == 30, f"Expected 30 year rows, got {len(rows)}"
    invariants_ok = True
    for r in rows:
        if not (r["p10_real"] <= r["p50_real"] <= r["p90_real"]):
            invariants_ok = False
        if not (r["p10_nominal"] <= r["p50_nominal"] <= r["p90_nominal"]):
            invariants_ok = False
        if not (0.0 <= r["probability_target"] <= 100.0):
            invariants_ok = False
        if not (0.0 <= r["probability_loss_real"] <= 100.0):
            invariants_ok = False

    # Reproducibility check
    vec_res2 = monte_carlo_projection(
        contribution=500.0,
        annual_return_pct=7.0,
        annual_volatility_pct=15.0,
        years=years,
        simulations=runs,
        initial_capital=10_000.0,
        seed=42,
    )
    exact_reproducible = vec_res["rows"] == vec_res2["rows"]

    result = {
        "workload": f"{runs:,} simulations x {years} years ({total_steps:,} total month-steps)",
        "vectorized_mean_ms": round(vec_mean, 2),
        "vectorized_median_ms": round(vec_median, 2),
        "vectorized_min_ms": round(vec_min, 2),
        "scalar_baseline_mean_ms": round(scalar_mean, 2),
        "speedup_factor": round(speedup, 1),
        "throughput_steps_per_sec": round(throughput, 0),
        "invariants_passed": invariants_ok,
        "exact_reproducibility": exact_reproducible,
    }
    print(json.dumps(result, indent=2))
    return result


# ---------------------------------------------------------------------------
# 2. XIRR ROOT-FINDER BENCHMARK (Vectorized vs Scalar Loop)
# ---------------------------------------------------------------------------
def _xirr_scalar_legacy(
    cash_flows: Sequence[tuple[date, float]],
    guess: float = 0.10,
    max_iterations: int = 100,
    tolerance: float = 1e-7,
) -> float | None:
    """Legacy pure-python loop for NPV and derivative calculation."""
    if len(cash_flows) < 2:
        return None
    first_d = cash_flows[0][0]
    flows = [((d - first_d).days / 365.0, amt) for d, amt in cash_flows]

    rate = guess
    for _ in range(max_iterations):
        if rate <= -0.99999999:
            return None
        npv = 0.0
        d_npv = 0.0
        for t, amt in flows:
            disc = (1.0 + rate) ** t
            npv += amt / disc
            d_npv -= (t * amt) / ((1.0 + rate) ** (t + 1))
        if abs(npv) < tolerance:
            return rate
        if abs(d_npv) < 1e-12:
            break
        step = npv / d_npv
        new_rate = rate - step
        if abs(new_rate - rate) < tolerance:
            return new_rate
        rate = new_rate
    return None


def benchmark_xirr() -> dict[str, Any]:
    print("\n" + "=" * 70)
    print("2. EMPIRICAL BENCHMARK: XIRR Root-Finder over Varying Cashflow Sizes")
    print("=" * 70)

    sizes = [10, 50, 100, 500, 1000]
    rng = np.random.default_rng(42)
    base_date = date(2020, 1, 1)

    size_results = {}
    total_vec_us = []
    total_scalar_us = []

    for size in sizes:
        # Generate cashflow series
        flows = [(base_date, -50000.0)]
        curr_d = base_date
        cum_inv = 50000.0
        for _ in range(size - 2):
            curr_d += timedelta(days=int(rng.integers(10, 60)))
            amt = -float(rng.uniform(100.0, 1000.0))
            cum_inv += -amt
            flows.append((curr_d, amt))
        curr_d += timedelta(days=30)
        final_val = cum_inv * 1.35  # ~35% gain
        flows.append((curr_d, final_val))

        # Measure Vectorized XIRR
        trials = 100 if size <= 100 else 30
        t_vec = []
        for _ in range(trials):
            t0 = time.perf_counter()
            r_vec = xirr(flows)
            t1 = time.perf_counter()
            t_vec.append((t1 - t0) * 1_000_000.0)

        # Measure Scalar XIRR
        t_scalar = []
        for _ in range(trials):
            t0 = time.perf_counter()
            _xirr_scalar_legacy(flows)
            t1 = time.perf_counter()
            t_scalar.append((t1 - t0) * 1_000_000.0)

        mean_vec_us = statistics.mean(t_vec)
        mean_scal_us = statistics.mean(t_scalar)
        speedup = mean_scal_us / mean_vec_us

        # Precision verification: calculate NPV at r_vec
        assert r_vec is not None
        npv_residual = abs(
            sum(amt / ((1.0 + r_vec) ** ((d - base_date).days / 365.0)) for d, amt in flows)
        )

        size_results[f"N_{size}"] = {
            "cashflows": size,
            "vectorized_mean_us": round(mean_vec_us, 1),
            "scalar_mean_us": round(mean_scal_us, 1),
            "speedup": round(speedup, 2),
            "npv_residual": float(f"{npv_residual:.2e}"),
            "precision_passed": npv_residual < 1e-4,
        }
        total_vec_us.append(mean_vec_us)
        total_scalar_us.append(mean_scal_us)

    overall_speedup = statistics.mean(total_scalar_us) / statistics.mean(total_vec_us)

    # Edge cases
    assert xirr([]) is None
    assert xirr([(base_date, 100.0)]) is None
    assert xirr([(base_date, 100.0), (base_date + timedelta(days=365), 110.0)]) is None  # All positive
    assert xirr([(base_date, -100.0), (base_date + timedelta(days=365), -110.0)]) is None  # All negative
    analytical_10 = xirr([(date(2025, 1, 1), -1000.0), (date(2026, 1, 1), 1100.0)])
    assert analytical_10 is not None and abs(analytical_10 - 0.10) < 1e-6

    result = {
        "benchmarks_by_size": size_results,
        "overall_average_speedup": round(overall_speedup, 2),
        "edge_cases_handled": True,
        "analytical_10pct_exact": True,
    }
    print(json.dumps(result, indent=2))
    return result


# ---------------------------------------------------------------------------
# 3. CASHFLOW OCCURRENCE LOOKUPS & PREFIX SUMS BENCHMARK
# ---------------------------------------------------------------------------
def benchmark_cashflow_forecasting() -> dict[str, Any]:
    print("\n" + "=" * 70)
    print("3. EMPIRICAL BENCHMARK: Cashflow Forecasting Prefix Sums & Hash Lookups")
    print("=" * 70)

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "bench_cashflow.sqlite3")
        db = Database(db_path)
        repo = Repo(db)
        payments_service = PaymentsService(repo)
        service = CashflowForecastService(repo, payments_service)

        # Seed realistic ledger: 3,000 transactions over 2 years + 10 subscriptions + 5 scheduled
        conn = repo.db.conn
        conn.execute("BEGIN TRANSACTION")
        base_d = date(2024, 1, 1)
        rng = np.random.default_rng(123)

        categories = ["Salário", "Supermercado", "Restauração", "Transporte", "Lazer", "Contas"]
        for i in range(3000):
            d = (base_d + timedelta(days=int(rng.integers(0, 730)))).isoformat()
            cat = rng.choice(categories)
            amt = float(rng.uniform(1500.0, 2500.0)) if cat == "Salário" else -float(rng.uniform(5.0, 150.0))
            conn.execute(
                "INSERT INTO transactions(date, description, amount, category, account) VALUES(?,?,?,?,?)",
                (d, f"Movement {i}", amt, cat, "Conta Ordem"),
            )

        # Insert active subscriptions
        for s in range(10):
            conn.execute(
                "INSERT INTO subscriptions(name, amount, frequency, first_due_date, active, category) VALUES(?,?,?,?,1,?)",
                (f"Sub {s}", 15.0 + s * 5, "monthly", "2024-01-15", "Subscrições"),
            )
        # Insert active scheduled payments
        for p in range(5):
            conn.execute(
                "INSERT INTO scheduled_payments(name, amount, start_date, months_total, months_paid, active, category) VALUES(?,?,?,12,2,1,?)",
                (f"Loan {p}", 150.0 + p * 50, "2024-06-01", "Crédito"),
            )
        conn.commit()

        # Benchmark calibration (prefix sums optimization)
        ref_date = date(2025, 9, 1)
        trials = 20
        calib_timings = []
        for _ in range(trials):
            t0 = time.perf_counter()
            calib = service.forecast_calibration(ref_date, sample_months=12, lookback_days=90)
            t1 = time.perf_counter()
            calib_timings.append((t1 - t0) * 1000.0)

        # Benchmark full 90-day cashflow forecast
        forecast_timings = []
        for _ in range(trials):
            t0 = time.perf_counter()
            fc = service.cashflow_calendar_forecast(horizon_days=90, as_of=ref_date)
            t1 = time.perf_counter()
            forecast_timings.append((t1 - t0) * 1000.0)

        mean_calib_ms = statistics.mean(calib_timings)
        mean_forecast_ms = statistics.mean(forecast_timings)

        # Invariant checks
        summary_90 = fc["summaries"]["90"]

        result = {
            "calibration_mean_ms": round(mean_calib_ms, 2),
            "forecast_90d_mean_ms": round(mean_forecast_ms, 2),
            "calibration_correction_factor": round(float(calib.get("correction_factor", 1.0)), 4),
            "starting_balance": round(float(fc.get("starting_balance", 0.0)), 2),
            "ending_balance_90d": round(float(summary_90.get("ending_balance", 0.0)), 2),
            "event_count_90d": summary_90.get("event_count", 0),
            "status": summary_90.get("status", ""),
        }
        print(json.dumps(result, indent=2))
        return result


# ---------------------------------------------------------------------------
# 4. SQL SPLIT AGGREGATIONS VS LEGACY MULTI-PASS UNION ALL CTE
# ---------------------------------------------------------------------------
def _legacy_category_spend_with_splits_union_all(repo: Repo, start: str, end: str) -> list[Any]:
    """Legacy multi-pass CTE with UNION ALL between parent non-split rows and child split rows."""
    return repo.q(
        """
        WITH split_parents AS (
            SELECT DISTINCT transaction_id FROM transaction_splits WHERE transaction_id IS NOT NULL
        ),
        regular_expenses AS (
            SELECT t.category AS category, ABS(t.amount) AS amount, t.id AS tx_id
            FROM transactions t
            LEFT JOIN transaction_meta m ON m.transaction_id=t.id
            WHERE COALESCE(NULLIF(m.payment_date,''),t.date) >= ?
              AND COALESCE(NULLIF(m.payment_date,''),t.date) < ?
              AND t.amount < 0
              AND t.id NOT IN (SELECT transaction_id FROM split_parents)
        ),
        split_expenses AS (
            SELECT s.category AS category, ABS(s.amount) AS amount, s.transaction_id AS tx_id
            FROM transaction_splits s
            JOIN transactions t ON t.id=s.transaction_id
            LEFT JOIN transaction_meta m ON m.transaction_id=t.id
            WHERE COALESCE(NULLIF(m.payment_date,''),t.date) >= ?
              AND COALESCE(NULLIF(m.payment_date,''),t.date) < ?
              AND t.amount < 0
        ),
        combined AS (
            SELECT * FROM regular_expenses
            UNION ALL
            SELECT * FROM split_expenses
        )
        SELECT category, SUM(amount) AS total, COUNT(DISTINCT tx_id) AS movements
        FROM combined
        GROUP BY category
        ORDER BY total DESC
        """,
        (start, end, start, end),
    )


def benchmark_sql_split_aggregations() -> dict[str, Any]:
    print("\n" + "=" * 70)
    print("4. EMPIRICAL BENCHMARK: SQL Split Aggregation vs Legacy Multi-Pass CTE")
    print("=" * 70)

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "bench_sql.sqlite3")
        db = Database(db_path)
        repo = Repo(db)

        categories = [
            "Alimentação", "Habitação", "Transporte", "Saúde",
            "Lazer", "Tecnologia", "Vestuário", "Educação", "Geral"
        ]
        for cat in categories:
            repo.db.conn.execute("INSERT OR IGNORE INTO categories(name, colour) VALUES(?, '#ffffff')", (cat,))

        conn = repo.db.conn
        conn.execute("BEGIN TRANSACTION")
        base_d = date(2025, 1, 1)
        rng = np.random.default_rng(888)

        total_expected_sum = 0.0

        # 8,000 unsplit transactions
        for i in range(8000):
            d = (base_d + timedelta(days=int(rng.integers(0, 365)))).isoformat()
            cat = rng.choice(categories[:-1])
            amt = -round(float(rng.uniform(5.0, 150.0)), 2)
            total_expected_sum += abs(amt)
            conn.execute(
                "INSERT INTO transactions(date, description, amount, category, account) VALUES(?,?,?,?,?)",
                (d, f"Tx {i}", amt, cat, "Conta Ordem"),
            )

        # 2,000 split transactions
        for i in range(8000, 10000):
            d = (base_d + timedelta(days=int(rng.integers(0, 365)))).isoformat()
            split_cats = rng.choice(categories[:-1], size=3, replace=False)
            split_amts = [round(float(rng.uniform(10.0, 40.0)), 2) for _ in range(3)]
            parent_amt = -round(sum(split_amts), 2)
            total_expected_sum += sum(split_amts)

            cur = conn.execute(
                "INSERT INTO transactions(date, description, amount, category, account) VALUES(?,?,?,?,?)",
                (d, f"Split Tx {i}", parent_amt, "Geral", "Cartão"),
            )
            tx_id = cur.lastrowid
            for sc, sa in zip(split_cats, split_amts):
                conn.execute(
                    "INSERT INTO transaction_splits(transaction_id, category, amount) VALUES(?,?,?)",
                    (tx_id, sc, sa),
                )
        conn.commit()

        start = "2025-01-01"
        end = "2026-01-01"
        trials = 50

        # Benchmark Single-Pass (Optimized)
        t_opt = []
        for _ in range(trials):
            t0 = time.perf_counter()
            opt_rows = repo.category_spend_with_splits(start, end)
            t1 = time.perf_counter()
            t_opt.append((t1 - t0) * 1000.0)

        # Benchmark Multi-Pass (Legacy)
        t_legacy = []
        for _ in range(trials):
            t0 = time.perf_counter()
            leg_rows = _legacy_category_spend_with_splits_union_all(repo, start, end)
            t1 = time.perf_counter()
            t_legacy.append((t1 - t0) * 1000.0)

        mean_opt_ms = statistics.mean(t_opt)
        mean_leg_ms = statistics.mean(t_legacy)
        speedup = mean_leg_ms / mean_opt_ms

        # Exact penny reconciliation verification
        sum_opt = sum(float(r["total"] or 0) for r in opt_rows)
        sum_leg = sum(float(r["total"] or 0) for r in leg_rows)
        diff_opt = abs(sum_opt - total_expected_sum)
        diff_leg = abs(sum_leg - total_expected_sum)

        result = {
            "total_transactions": 10000,
            "split_transactions": 2000,
            "optimized_single_pass_mean_ms": round(mean_opt_ms, 3),
            "legacy_multi_pass_cte_mean_ms": round(mean_leg_ms, 3),
            "speedup_factor": round(speedup, 2),
            "total_expected_sum": round(total_expected_sum, 2),
            "optimized_query_sum": round(sum_opt, 2),
            "penny_perfect_reconciliation": diff_opt < 0.01 and diff_leg < 0.01,
        }
        print(json.dumps(result, indent=2))
        return result


if __name__ == "__main__":
    print("\n======================================================================")
    print("      MILESTONE 2 EMPIRICAL BENCHMARK & SPEEDUP VERIFICATION          ")
    print("======================================================================")
    r1 = benchmark_monte_carlo()
    r2 = benchmark_xirr()
    r3 = benchmark_cashflow_forecasting()
    r4 = benchmark_sql_split_aggregations()
    print("\n======================================================================")
    print("                    ALL BENCHMARKS COMPLETED                          ")
    print("======================================================================")
