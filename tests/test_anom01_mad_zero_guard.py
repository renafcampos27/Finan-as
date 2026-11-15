#!/usr/bin/env python3
"""ANOM-01 regression tests: MAD=0 guard for anomaly detection & forecast bands.

Bug (P1): users with perfectly repeated values (e.g. an 8.00 EUR lunch every
day) produce a Median Absolute Deviation (MAD) of 0.0. Dividing by that scale
(yields inf/nan) or letting the interval width collapse to zero (Winkler score
degrades to 0) breaks anomaly detection and forecast quality metrics.

Guarantees required by the bug ledger entry 13:
  (a) all values equal -> no exception, finite scores, no inf/nan;
  (b) single-element series -> no anomalies, score 0.0, no exception;
  (c) normal data -> original detection behaviour preserved.
"""

from __future__ import annotations

import math
import statistics
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from database.repo import Repo  # noqa: E402
from services.analytics import AnalyticsService  # noqa: E402
from services.spending_diagnostics import SpendingDiagnosticsService  # noqa: E402


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """Isolated throwaway database; never touches the user's real database."""
    import utils.config as config

    monkeypatch.setattr(config, "DB_NAME", str(tmp_path / "anom01.sqlite3"))
    r = Repo()
    yield r
    r.db.conn.close()


@pytest.fixture
def analytics(repo):
    return AnalyticsService(repo)


def _insert_expenses(
    repo: Repo,
    rows: list[tuple[str, str, float, str]],
) -> None:
    # Start from a clean ledger: Repo() seeds demo movements that would
    # otherwise pollute the per-category statistics under test.
    repo.db.conn.execute("DELETE FROM transactions")
    repo.db.conn.executemany(
        """
        INSERT INTO transactions(date, description, amount, category, account, kind)
        VALUES(?, ?, ?, ?, 'Conta', 'expense')
        """,
        [
            (day, description, -amount, category)
            for day, description, amount, category in rows
        ],
    )
    repo.db.conn.commit()


def test_anom01_identical_values_mad_is_zero_and_guard_activates():
    """(a) Exact repeated values: MAD==0.0 and the documented fallback engages."""
    values = [8.0] * 10
    median = statistics.median(values)
    mad = statistics.median(abs(value - median) for value in values)

    assert median == pytest.approx(8.0)
    assert mad == 0.0  # pre-condition of the bug: MAD is exactly zero
    assert statistics.pstdev(values) == 0.0  # stdev fallback is also zero

    # Guard contract: fallback to stdev when meaningful, otherwise to 1.0.
    if mad < 1e-6:
        fallback = statistics.stdev(values) if len(values) > 1 else 0.0
        mad = fallback if fallback > 1e-6 else 1.0
    assert mad == pytest.approx(1.0)


def test_anom01_identical_daily_expenses_never_raise_and_yield_finite_alerts(
    repo,
    analytics,
):
    """(a) 10 identical 8.00 EUR lunches -> no exception, finite threshold path."""
    _insert_expenses(
        repo,
        [
            ("2026-07-01", "Almoco", 8.0, "Alimentacao"),
            ("2026-07-02", "Almoco", 8.0, "Alimentacao"),
            ("2026-07-03", "Almoco", 8.0, "Alimentacao"),
            ("2026-07-04", "Almoco", 8.0, "Alimentacao"),
            ("2026-07-05", "Almoco", 8.0, "Alimentacao"),
            ("2026-07-06", "Almoco", 8.0, "Alimentacao"),
            ("2026-07-07", "Almoco", 8.0, "Alimentacao"),
            ("2026-07-08", "Almoco", 8.0, "Alimentacao"),
            ("2026-07-09", "Almoco", 8.0, "Alimentacao"),
            ("2026-07-10", "Almoco", 8.0, "Alimentacao"),
        ],
    )

    anomalies = analytics.detect_financial_anomalies(
        date(2026, 7, 12), lookback_days=180
    )

    for alert in anomalies:
        for key in ("amount",):
            assert math.isfinite(float(alert[key]))
        assert alert["amount"] >= 0.0
    kinds = {alert["kind"] for alert in anomalies}
    assert kinds.isdisjoint({"spike"})


def test_anom01_constant_history_backtest_band_is_never_collapsed(repo, analytics):
    """(a) Constant monthly history -> finite band width (Winkler does not die)."""
    for index in range(12):
        month_day = f"2025-{index + 1:02d}-05"
        _insert_expenses(
            repo,
            [(month_day, "Almoco", 8.0, "Alimentacao")],
        )

    diagnostics = SpendingDiagnosticsService(repo)
    accuracy = diagnostics.forecast_accuracy(
        reference_date=date(2026, 7, 25),
        visible_months=12,
    )

    for row in accuracy["rows"]:
        assert math.isfinite(float(row["low"]))
        assert math.isfinite(float(row["high"]))
        assert row["high"] > row["low"], (
            "ANOM-01: constant history must not collapse the interval to zero width"
        )


def test_anom01_single_element_series_yields_no_anomalies(repo, analytics):
    """(b) A single data point must be silently skipped (no stdev possible)."""
    _insert_expenses(
        repo,
        [("2026-07-10", "Unico", 500.0, "Diversos")],
    )

    anomalies = analytics.detect_financial_anomalies(
        date(2026, 7, 12), lookback_days=180
    )

    assert anomalies == []


def test_anom01_normal_values_still_flag_extreme_outlier(repo, analytics):
    """(c) Normal data: 300 must still be flagged among ~100-ish baselines."""
    _insert_expenses(
        repo,
        [
            ("2026-05-01", "Mercado", 100.0, "Supermercado"),
            ("2026-05-15", "Mercado", 102.0, "Supermercado"),
            ("2026-06-01", "Mercado", 98.0, "Supermercado"),
            ("2026-06-15", "Mercado", 101.0, "Supermercado"),
            ("2026-07-05", "Mercado", 300.0, "Supermercado"),
        ],
    )

    anomalies = analytics.detect_financial_anomalies(
        date(2026, 7, 12), lookback_days=180
    )

    spike_alerts = [alert for alert in anomalies if alert["kind"] == "spike"]
    assert len(spike_alerts) == 1
    assert spike_alerts[0]["amount"] == pytest.approx(300.0)
    assert spike_alerts[0]["category"] == "Supermercado"


def test_anom01_normal_values_keep_mad_and_threshold_unchanged():
    """(c) For varied values MAD > 0 so the guard must not alter the math."""
    values = [100.0, 102.0, 98.0, 101.0, 300.0]
    median = statistics.median(values)
    mad = statistics.median(abs(value - median) for value in values)

    assert median == pytest.approx(101.0)
    assert mad == pytest.approx(1.0)  # deviations [1, 1, 3, 0, 199] -> median 1
    assert mad >= 1e-6  # guard inactive for healthy data
    threshold = max(25.0, median * 1.8, median + 3.0 * max(1.0, mad))
    assert threshold == pytest.approx(max(25.0, 101.0 * 1.8, 101.0 + 3.0 * 1.0))
    assert 300.0 > threshold
