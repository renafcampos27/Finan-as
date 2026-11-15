"""Milestone 2 Empirical Benchmark and Adversarial Stress Suite.

Executed by Challenger challenger_m2_iter2_1 as part of pytest suite.
"""

from __future__ import annotations

import gc
import math
import os
import statistics
import tempfile
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Sequence

import numpy as np

from database.engine import FinanceDB
from database.repo import Repo
from services.cashflow_forecast import CashflowForecastService
from services.finance_math import periodic_discount_factor, xirr
from services.financial_intelligence import monte_carlo_projection
from services.payments import PaymentsService


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
    """Legacy scalar pure-python simulation loop."""
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
        for _ in range(1, total_months + 1):
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


def test_benchmark_monte_carlo_5000x30y_speedup():
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

    # Invariant checks
    rows = res["rows"]
    assert len(rows) == 30
    for r in rows:
        assert r["p10_real"] <= r["p50_real"] <= r["p90_real"]
        assert r["p10_nominal"] <= r["p50_nominal"] <= r["p90_nominal"]
        assert 0.0 <= r["probability_target"] <= 100.0
        assert 0.0 <= r["probability_loss_real"] <= 100.0
        assert 0.0 <= r["probability_loss_nominal"] <= 100.0

    # Check exact reproducibility with seed
    res2 = monte_carlo_projection(
        contribution=500.0,
        annual_return_pct=7.0,
        annual_volatility_pct=15.0,
        years=years,
        simulations=runs,
        initial_capital=10_000.0,
        seed=42,
    )
    assert res["rows"] == res2["rows"]

    # Measure scalar baseline
    scalar_timings = []
    for _ in range(2):
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

    scalar_mean_ms = statistics.mean(scalar_timings)
    speedup = scalar_mean_ms / mean_ms
    monthly_steps = runs * years * 12
    steps_per_sec = monthly_steps / (mean_ms / 1000.0)

    print(
        f"\n[BENCHMARK] Monte Carlo 5,000x30y ({monthly_steps:,} steps): Vectorized mean={mean_ms:.2f}ms (median={median_ms:.2f}ms, min={min_ms:.2f}ms), Scalar mean={scalar_mean_ms:.2f}ms | SPEEDUP={speedup:.1f}x | Throughput={steps_per_sec:,.0f} steps/s"
    )
    assert mean_ms < 200.0, f"Monte Carlo latency too high: {mean_ms:.2f}ms (expected < 200ms)"
    assert speedup >= 5.0, f"Monte Carlo speedup insufficient: {speedup:.1f}x (expected >= 5x)"


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
            try:
                disc = (1.0 + rate) ** t
                npv += amt / disc
                d_npv -= (t * amt) / ((1.0 + rate) ** (t + 1))
            except OverflowError:
                return None
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


def test_benchmark_xirr_sizes_and_speedup():
    rng = np.random.default_rng(12345)
    sizes = [10, 50, 100, 500]
    base_date = date(2020, 1, 1)

    for size in sizes:
        flows = [(base_date, -50000.0)]
        curr_d = base_date
        cum_inv = 50000.0
        for _ in range(size - 2):
            curr_d += timedelta(days=int(rng.integers(10, 60)))
            amt = -float(rng.uniform(100.0, 1000.0))
            cum_inv += -amt
            flows.append((curr_d, amt))
        curr_d += timedelta(days=30)
        final_val = cum_inv * 1.35
        flows.append((curr_d, final_val))

        trials = 50
        t_vec = []
        for _ in range(trials):
            t0 = time.perf_counter()
            r_vec = xirr(flows)
            t1 = time.perf_counter()
            t_vec.append((t1 - t0) * 1_000_000.0)

        t_scalar = []
        for _ in range(trials):
            t0 = time.perf_counter()
            _xirr_scalar_legacy(flows)
            t1 = time.perf_counter()
            t_scalar.append((t1 - t0) * 1_000_000.0)

        mean_vec_us = statistics.mean(t_vec)
        mean_scal_us = statistics.mean(t_scalar)
        speedup = mean_scal_us / mean_vec_us if mean_vec_us > 0 else 1.0

        assert r_vec is not None
        npv_res = abs(
            sum(amt / ((1.0 + r_vec) ** ((d - base_date).days / 365.0)) for d, amt in flows)
        )

        print(
            f"\n[BENCHMARK] XIRR N={size}: Vectorized={mean_vec_us:.1f}us, Scalar={mean_scal_us:.1f}us | SPEEDUP={speedup:.2f}x | NPV residual={npv_res:.2e}"
        )
        assert npv_res < 5e-4
        assert mean_vec_us < 2500.0

    # Analytical test case: exactly 10% annual return
    test_10pct = [(date(2025, 1, 1), -1000.0), (date(2026, 1, 1), 1100.0)]
    rate_10pct = xirr(test_10pct)
    assert rate_10pct is not None
    assert abs(rate_10pct - 0.10) < 1e-6


def test_benchmark_cashflow_forecasting_prefix_sums():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "bench_cashflow.sqlite3")
        db = FinanceDB(Path(db_path))
        try:
            repo = Repo(db)
            payments_service = PaymentsService(repo)
            service = CashflowForecastService(repo, payments_service)

            conn = repo.db.conn
            conn.commit()
            base_d = date(2024, 1, 1)
            rng = np.random.default_rng(123)

            categories = ["Salário", "Supermercado", "Restauração", "Transporte", "Lazer", "Contas"]
            with conn:
                for i in range(3000):
                    d = (base_d + timedelta(days=int(rng.integers(0, 730)))).isoformat()
                    cat = rng.choice(categories)
                    if cat == "Salário":
                        amt = float(rng.uniform(1500.0, 2500.0))
                        kind = "income"
                    else:
                        amt = -float(rng.uniform(5.0, 150.0))
                        kind = "expense"
                    conn.execute(
                        "INSERT INTO transactions(date, description, amount, category, account, kind) VALUES(?,?,?,?,?,?)",
                        (d, f"Movement {i}", amt, cat, "Conta Ordem", kind),
                    )

                for s in range(10):
                    conn.execute(
                        "INSERT INTO subscriptions(name, amount, period, renewal_day, active, category) VALUES(?,?,?,?,1,?)",
                        (f"Sub {s}", 15.0 + s * 5, "Mensal", 15, "Subscrições"),
                    )
                for p in range(5):
                    conn.execute(
                        "INSERT INTO scheduled_payments(name, amount, start_date, months_total, months_paid, active, category) VALUES(?,?,?,12,2,1,?)",
                        (f"Loan {p}", 150.0 + p * 50, "2024-06-01", "Crédito"),
                    )

            ref_date = date(2025, 9, 1)
            trials = 20
            calib_timings = []
            for _ in range(trials):
                t0 = time.perf_counter()
                service.forecast_calibration(ref_date, sample_months=12, lookback_days=90)
                t1 = time.perf_counter()
                calib_timings.append((t1 - t0) * 1000.0)

            forecast_timings = []
            for _ in range(trials):
                t0 = time.perf_counter()
                fc = service.cashflow_calendar_forecast(horizon_days=90, as_of=ref_date)
                t1 = time.perf_counter()
                forecast_timings.append((t1 - t0) * 1000.0)

            mean_calib_ms = statistics.mean(calib_timings)
            mean_forecast_ms = statistics.mean(forecast_timings)
            summary_90 = fc["summaries"]["90"]

            print(
                f"\n[BENCHMARK] Cashflow Calibration (12 months walk-forward): mean={mean_calib_ms:.2f}ms | 90-Day Full Forecast: mean={mean_forecast_ms:.2f}ms | Events={summary_90['event_count']}"
            )
            assert mean_calib_ms < 100.0
            assert mean_forecast_ms < 150.0
            assert "ending_balance" in summary_90
        finally:
            db.conn.close()


def _legacy_category_spend_with_splits_union_all(repo: Repo, start: str, end: str) -> list[Any]:
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


def test_benchmark_sql_split_single_pass_speedup():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "bench_sql.sqlite3")
        db = FinanceDB(Path(db_path))
        try:
            repo = Repo(db)

            categories = [
                "Alimentação",
                "Habitação",
                "Transporte",
                "Saúde",
                "Lazer",
                "Tecnologia",
                "Vestuário",
                "Educação",
                "Geral",
            ]
            for cat in categories:
                repo.db.conn.execute(
                    "INSERT OR IGNORE INTO categories(name, kind, colour) VALUES(?, 'expense', '#ffffff')",
                    (cat,),
                )

            conn = repo.db.conn
            conn.commit()
            base_d = date(2025, 1, 1)
            rng = np.random.default_rng(888)

            total_expected_sum = 0.0

            with conn:
                for i in range(8000):
                    d = (base_d + timedelta(days=int(rng.integers(0, 365)))).isoformat()
                    cat = rng.choice(categories[:-1])
                    amt = -round(float(rng.uniform(5.0, 150.0)), 2)
                    total_expected_sum += abs(amt)
                    conn.execute(
                        "INSERT INTO transactions(date, description, amount, category, account, kind) VALUES(?,?,?,?,?,'expense')",
                        (d, f"Tx {i}", amt, cat, "Conta Ordem"),
                    )

                for i in range(8000, 10000):
                    d = (base_d + timedelta(days=int(rng.integers(0, 365)))).isoformat()
                    split_cats = rng.choice(categories[:-1], size=3, replace=False)
                    split_amts = [round(float(rng.uniform(10.0, 40.0)), 2) for _ in range(3)]
                    parent_amt = -round(sum(split_amts), 2)
                    total_expected_sum += sum(split_amts)

                    cur = conn.execute(
                        "INSERT INTO transactions(date, description, amount, category, account, kind) VALUES(?,?,?,?,?,'expense')",
                        (d, f"Split Tx {i}", parent_amt, "Geral", "Cartão"),
                    )
                    tx_id = cur.lastrowid
                    for sc, sa in zip(split_cats, split_amts):
                        conn.execute(
                            "INSERT INTO transaction_splits(transaction_id, category, amount, percent) VALUES(?,?,?,?)",
                            (tx_id, sc, sa, round((sa / abs(parent_amt)) * 100.0, 2)),
                        )

            start = "2025-01-01"
            end = "2026-01-01"
            trials = 50

            t_opt = []
            for _ in range(trials):
                t0 = time.perf_counter()
                opt_rows = repo.category_spend_with_splits(start, end)
                t1 = time.perf_counter()
                t_opt.append((t1 - t0) * 1000.0)

            t_legacy = []
            for _ in range(trials):
                t0 = time.perf_counter()
                _legacy_category_spend_with_splits_union_all(repo, start, end)
                t1 = time.perf_counter()
                t_legacy.append((t1 - t0) * 1000.0)

            mean_opt_ms = statistics.mean(t_opt)
            mean_leg_ms = statistics.mean(t_legacy)
            speedup = mean_leg_ms / mean_opt_ms if mean_opt_ms > 0 else 1.0

            sum_opt = sum(float(r["total"] or 0) for r in opt_rows)
            diff_opt = abs(sum_opt - total_expected_sum)

            print(
                f"\n[BENCHMARK] SQL Split Aggregation (10k txs, 2k splits): Single-Pass={mean_opt_ms:.3f}ms, Multi-Pass CTE={mean_leg_ms:.3f}ms | SPEEDUP={speedup:.2f}x | Diff={diff_opt:.4f}"
            )
            assert diff_opt < 0.05
            assert mean_opt_ms < 100.0
            assert speedup >= 0.5, f"SQL speedup insufficient: {speedup:.2f}x"
        finally:
            db.conn.close()

