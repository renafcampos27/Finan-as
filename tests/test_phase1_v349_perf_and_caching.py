"""Tests for v3.49 Phase 1: Computational Performance, Caching & Database Acceleration."""

from datetime import date
from database.engine import FinanceDB
from database.repo import Repo
from services.analytics import AnalyticsService
from services.finance_math import cagr, xirr, xirr_pct
from services.financial_intelligence import (
    _percentiles_10_50_90,
    monte_carlo_projection,
)
from services.cashflow_forecast import _normalise_description_cached


def test_cagr_total_loss_and_valid_returns():
    # Regular positive return
    assert round(cagr(100.0, 121.0, 2.0), 2) == 10.0
    # Total loss of capital (-100%)
    assert cagr(100.0, 0.0, 3.0) == -100.0
    # Zero duration or non-positive start
    assert cagr(0.0, 100.0, 2.0) == 0.0
    assert cagr(100.0, 100.0, 0.0) == 0.0


def test_xirr_brent_fast_convergence():
    # Standard cash flow: -1000 at t0, +1100 at t1 (exactly 365 days)
    flows = [
        (date(2023, 1, 1), -1000.0),
        (date(2024, 1, 1), 1100.0),
    ]
    rate = xirr(flows)
    assert rate is not None
    assert round(rate, 4) == 0.1000
    assert round(xirr_pct(flows) or 0.0, 2) == 10.0

    # Multi-step cash flow
    flows_multi = [
        (date(2023, 1, 1), -10000.0),
        (date(2023, 7, 1), -5000.0),
        (date(2024, 1, 1), 16500.0),
    ]
    rate_multi = xirr(flows_multi)
    assert rate_multi is not None
    assert rate_multi > 0.05


def test_monte_carlo_single_sort_percentiles():
    data = [10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0, 90.0, 100.0]
    p10, p50, p90 = _percentiles_10_50_90(data)
    assert p10 < p50 < p90
    assert round(p50, 1) == 55.0

    proj = monte_carlo_projection(
        contribution=500.0,
        annual_return_pct=7.0,
        annual_volatility_pct=15.0,
        years=5,
        simulations=200,
    )
    assert len(proj["rows"]) == 5
    assert proj["rows"][0]["p50_real"] > 0


def test_cashflow_description_memoization():
    desc1 = _normalise_description_cached("Continente Bom Dia (subscrição)")
    desc2 = _normalise_description_cached("Continente Bom Dia (subscrição)")
    assert desc1 == desc2 == "continente bom dia"


def test_analytics_persistent_services_and_monthly_evolution(tmp_path):
    db_path = tmp_path / "test_perf.sqlite3"
    db = FinanceDB(str(db_path))
    db.init_schema()
    db.ensure_performance_indexes()
    repo = Repo(db)
    analytics = AnalyticsService(repo)

    # Validate persistent service properties
    h1 = analytics.health_service
    h2 = analytics.health_service
    assert h1 is h2

    i1 = analytics.intel_service
    i2 = analytics.intel_service
    assert i1 is i2

    # Insert sample movements with matching kind
    db.conn.execute(
        "INSERT INTO categories (name, kind, colour) VALUES ('Salário', 'income', '#00ff00'), ('Alimentação', 'expense', '#ff0000')"
    )
    db.conn.execute(
        "INSERT INTO transactions (date, description, amount, category, kind) VALUES ('2026-01-15', 'Salario', 2500.0, 'Salário', 'income'), ('2026-01-20', 'Supermercado', -300.0, 'Alimentação', 'expense')"
    )
    db.conn.commit()

    # Single-query monthly evolution
    evo = analytics.monthly_evolution(2026)
    assert len(evo) == 12
    assert evo[0]["month"] == "2026-01"
    assert evo[0]["income"] == 2500.0
    assert evo[0]["expenses"] == 300.0
    assert evo[0]["savings"] == 2200.0

    # Cache invalidation clears without error
    analytics.invalidate_caches()
    db.close()
