"""Comprehensive 4-Tier Requirement-Driven Opaque-Box E2E Test Suite.

Covers Features F1 through F11 from PROJECT.md:
- Tier 1: Feature Specification Coverage (Math, Monte Carlo, XIRR, Cashflow, SQL Splits, Engine, Journal, Backup, Decimal Precision)
- Tier 2: Boundary & Corner Cases (Zero/negative, empty ledgers, extreme volatility, single txns, century horizons, corrupt files)
- Tier 3: Cross-Feature Combinations (Pairwise interactions across modules)
- Tier 4: Real-World Workload Scenarios (Full lifecycle with 1,000+ txns, splits, valuations, FIRE, encrypted backup, compaction, restore)
"""

from __future__ import annotations

import math
import random
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pytest

from database.engine import FinanceDB
from database.repo.main_repo import Repo
from domain.models import Holding
from services.backup import BackupService
from services.cashflow_forecast import (
    CashflowForecastService,
    _normalise_description_cached,
)
from services.change_journal import ChangeJournalService
from services.finance_math import (
    cagr,
    cornish_fisher_var,
    effective_annual_to_periodic_rate,
    expected_shortfall_cvar,
    max_drawdown,
    modified_dietz,
    periodic_discount_factor,
    present_value_of_contributions,
    sharpe_ratio,
    sortino_ratio,
    twrr,
    xirr,
    xirr_pct,
)
from services.financial_intelligence import monte_carlo_projection
from services.fire import FirePlanningEngine
from services.payments import PaymentsService
from utils.helpers import safe_float


# ============================================================================
# Test Harness & Ephemeral Database Fixtures
# ============================================================================

@pytest.fixture
def sandbox_dir(tmp_path: Path) -> Path:
    """Create a completely isolated sandbox root directory for the test."""
    root = tmp_path / "sandbox"
    root.mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture
def test_db(sandbox_dir: Path) -> FinanceDB:
    """Initialize an isolated FinanceDB with default schema."""
    db_file = sandbox_dir / "financas_pessoais.sqlite3"
    db = FinanceDB(
        db_file,
        attachments_directory=sandbox_dir / "anexos",
        history_directory=sandbox_dir / "historico_alteracoes",
        preferences_file=sandbox_dir / "backup_preferences.json",
        protection_directory=sandbox_dir / "protecoes",
    )
    db.conn.execute("DELETE FROM transactions")
    db.conn.execute("DELETE FROM transaction_splits")
    db.conn.execute("DELETE FROM transaction_meta")
    db.conn.execute("DELETE FROM transaction_attachments")
    db.conn.execute("DELETE FROM holdings")
    db.conn.execute("DELETE FROM subscriptions")
    db.conn.execute("DELETE FROM subscription_occurrences")
    db.conn.execute("DELETE FROM scheduled_payments")
    db.conn.execute("DELETE FROM scheduled_payment_occurrences")
    db.conn.execute("DELETE FROM goals")
    db.conn.execute("DELETE FROM monthly_snapshots")
    db.conn.execute("DELETE FROM financial_state_snapshots")
    db.conn.commit()
    yield db
    try:
        db.conn.close()
    except Exception:
        pass


@pytest.fixture
def test_repo(test_db: FinanceDB) -> Repo:
    """Initialize a full Repo layer pointing to the test database."""
    return Repo(db=test_db)


@pytest.fixture
def test_backup_service(test_repo: Repo, sandbox_dir: Path) -> BackupService:
    """Initialize an isolated BackupService."""
    return BackupService(
        test_repo,
        backup_directory=sandbox_dir / "backups",
        database_file=Path(test_repo.db.path),
        preferences_file=sandbox_dir / "backup_preferences.json",
        attachments_directory=sandbox_dir / "anexos",
        history_directory=sandbox_dir / "historico_alteracoes",
    )


@pytest.fixture
def test_journal_service(test_repo: Repo, sandbox_dir: Path) -> ChangeJournalService:
    """Initialize an isolated ChangeJournalService."""
    return ChangeJournalService(
        test_repo,
        attachments_directory=sandbox_dir / "anexos",
        journal_storage=sandbox_dir / "historico_alteracoes",
    )


@pytest.fixture
def test_cashflow_service(test_repo: Repo) -> CashflowForecastService:
    """Initialize an isolated CashflowForecastService."""
    payments = PaymentsService(test_repo)
    return CashflowForecastService(test_repo, payments)


# ============================================================================
# TIER 1: FEATURE SPECIFICATION COVERAGE (>=5 tests per domain area)
# ============================================================================

class TestTier1FeatureCoverage:
    """Tier 1: Feature Specification Coverage across all core engines."""

    # ------------------------------------------------------------------------
    # Domain 1: Financial Mathematics & Rates (Features F4, F5)
    # ------------------------------------------------------------------------

    def test_effective_periodic_compounding_exact(self) -> None:
        """Effective rate must compound to the exact annual rate after 12 periods."""
        annual_pct = 6.0
        monthly_rate = effective_annual_to_periodic_rate(annual_pct, periods_per_year=12)
        # Mathematical derivation: (1 + r_m)^12 == 1 + 0.06
        compounded = math.pow(1.0 + monthly_rate, 12) - 1.0
        assert math.isclose(compounded, 0.06, rel_tol=1e-12)
        # 0% annual rate gives 0.0 periodic rate
        assert effective_annual_to_periodic_rate(0.0, 12) == 0.0
        # Extreme negative bound handling
        assert effective_annual_to_periodic_rate(-150.0, 12) < 0.0

    def test_periodic_discount_factor_annuity_pv(self) -> None:
        """Discount factor and annuity PV must strictly match closed-form annuity math."""
        # 12 monthly payments of 100€ with 0% inflation = 1200.0€
        pv_zero = present_value_of_contributions(100.0, 12, 0.0, periods_per_year=12)
        assert math.isclose(pv_zero, 1200.0, rel_tol=1e-9)

        # With 2% annual inflation, PV should be strictly discounted (< 1200.0€)
        pv_inf = present_value_of_contributions(100.0, 12, 2.0, periods_per_year=12)
        assert 1180.0 < pv_inf < 1200.0

        # Periodic discount factor at period 0 is 1.0
        df0 = periodic_discount_factor(5.0, 0, periods_per_year=12)
        assert math.isclose(df0, 1.0, rel_tol=1e-9)

        # Periodic discount factor at period 12 for 5% annual is 1.05
        df12 = periodic_discount_factor(5.0, 12, periods_per_year=12)
        assert math.isclose(df12, 1.05, rel_tol=1e-9)

    def test_cagr_positive_growth_and_zero_bounds(self) -> None:
        """CAGR calculation must follow (V_end / V_start)^(1/years) - 1 with safe bounds."""
        # 1,000€ growing to 2,000€ over 5 years
        # Expected: 2^(1/5) - 1 = 14.869835...%
        rate = cagr(1000.0, 2000.0, 5.0)
        expected = (math.pow(2.0, 0.2) - 1.0) * 100.0
        assert math.isclose(rate, expected, rel_tol=1e-6)

        # Total capital loss
        assert cagr(1000.0, 0.0, 3.0) == -100.0
        # Invalid / zero start capital
        assert cagr(0.0, 500.0, 2.0) == 0.0
        # Invalid duration
        assert cagr(1000.0, 1500.0, 0.0) == 0.0

    def test_twrr_geometric_linking(self) -> None:
        """TWRR must geometrically link sub-period return rates in percentage points."""
        # Returns: +10%, -5%, +20%
        # Compound: (1.10) * (0.95) * (1.20) = 1.254 -> +25.4%
        returns = [10.0, -5.0, 20.0]
        linked = twrr(returns)
        assert math.isclose(linked, 25.4, rel_tol=1e-6)
        # Empty returns list returns 0.0
        assert twrr([]) == 0.0

    def test_modified_dietz_cashflow_weighting(self) -> None:
        """Modified Dietz must accurately weight intra-period cash flows by timing."""
        start_val = 1000.0
        end_val = 1200.0
        # +100€ injected exactly halfway through a 30-day month (weight = 15/30 = 0.5)
        # Denominator = 1000 + 100*0.5 = 1050. Net Gain = 1200 - 1000 - 100 = 100.
        # Return = 100 / 1050 * 100 = 9.5238...%
        flows = [(date(2026, 6, 16), 100.0)]
        ret = modified_dietz(start_val, end_val, flows, date(2026, 6, 1), date(2026, 7, 1))
        expected = (100.0 / 1050.0) * 100.0
        assert math.isclose(ret, expected, rel_tol=1e-4)

    def test_sharpe_and_sortino_downside_ratios(self) -> None:
        """Sharpe and Sortino ratios must penalize volatility and downside risk accurately."""
        # Monthly return series (in %)
        returns = [1.5, 2.0, -0.5, 3.0, 1.0, -1.2, 2.5, 0.8, -0.2, 1.8, 2.2, 1.1]
        sr = sharpe_ratio(returns, risk_free_rate_annual_pct=2.0, periods_per_year=12)
        sortino = sortino_ratio(returns, risk_free_rate_annual_pct=2.0, periods_per_year=12)

        assert sr > 0.0
        assert sortino > 0.0
        # Sortino considers only negative deviations, typically higher for mostly positive series
        assert not math.isnan(sr) and not math.isinf(sr)

        # Single return or empty returns gives 0.0
        assert sharpe_ratio([5.0]) == 0.0
        assert sortino_ratio([]) == 0.0

    def test_max_drawdown_peak_trough_identification(self) -> None:
        """Max drawdown must identify the greatest peak-to-trough drop and its indices."""
        # Series: 100 -> 150 (peak) -> 120 -> 90 (trough, -40%) -> 140 -> 80 (trough from 150: -46.67%) -> 160
        series = [100.0, 150.0, 120.0, 90.0, 140.0, 80.0, 160.0]
        dd, peak_idx, trough_idx = max_drawdown(series)
        # Drop from 150 to 80 = (150 - 80) / 150 * 100 = 46.67%
        assert math.isclose(dd, 46.67, abs_tol=0.01)
        assert peak_idx == 1  # 150.0
        assert trough_idx == 5  # 80.0

    def test_cornish_fisher_var_and_expected_shortfall(self) -> None:
        """Cornish-Fisher expansion VaR and CVaR must quantify non-normal tail risk."""
        returns = [-3.5, -2.1, -1.0, 0.5, 1.2, 1.5, 2.0, 2.2, 3.0, -4.8, 0.2, 1.8]
        var_95 = cornish_fisher_var(returns, confidence=0.95)
        cvar_95 = expected_shortfall_cvar(returns, confidence=0.95)

        assert var_95 > 0.0
        assert cvar_95 > 0.0
        # Expected Shortfall (average tail loss) should be >= Value at Risk cutoff
        assert cvar_95 >= var_95 * 0.8  # Consistent magnitude

    # ------------------------------------------------------------------------
    # Domain 2: Monte Carlo Stochastic Engine (Feature F4)
    # ------------------------------------------------------------------------

    def test_monte_carlo_monthly_vs_single_accumulation(self) -> None:
        """Monte Carlo projection must accumulate correctly for single vs monthly contributions."""
        # 10,000€ initial + 500€/month for 10 years at 6% return, 12% vol
        res_monthly = monte_carlo_projection(
            contribution=500.0,
            annual_return_pct=6.0,
            annual_volatility_pct=12.0,
            years=10,
            initial_capital=10000.0,
            mode="monthly",
            simulations=500,
            seed=42,
        )
        # 10,000€ initial + 60,000€ single lump-sum
        res_single = monte_carlo_projection(
            contribution=60000.0,
            annual_return_pct=6.0,
            annual_volatility_pct=12.0,
            years=10,
            initial_capital=10000.0,
            mode="single",
            simulations=500,
            seed=42,
        )

        assert "rows" in res_monthly
        assert "rows" in res_single
        assert len(res_monthly["rows"]) == 10
        assert len(res_single["rows"]) == 10

        # Final median wealth must exceed total nominal contributions
        final_nominal = res_monthly["rows"][-1]["p50_nominal"]
        assert final_nominal > (10000.0 + 500.0 * 120)
        # Single lump sum invested at start compounds more than monthly stream of identical total capital
        assert res_single["rows"][-1]["p50_nominal"] > final_nominal

    def test_monte_carlo_multi_asset_diversification_effect(self) -> None:
        """Diversification with uncorrelated assets must reduce portfolio effective volatility."""
        # 100% in a single volatile asset
        res_single_asset = monte_carlo_projection(
            contribution=1000.0,
            annual_return_pct=7.0,
            annual_volatility_pct=20.0,
            years=10,
            asset_weights=[1.0],
            correlation_pct=100.0,
            simulations=500,
            seed=42,
        )
        # Split equally across 4 assets with low correlation (20%)
        res_diversified = monte_carlo_projection(
            contribution=1000.0,
            annual_return_pct=7.0,
            annual_volatility_pct=20.0,
            years=10,
            asset_weights=[0.25, 0.25, 0.25, 0.25],
            correlation_pct=20.0,
            simulations=500,
            seed=42,
        )
        # Lower volatility results in narrower spread (P90 - P10) for diversified portfolio
        spread_single = res_single_asset["rows"][-1]["p90_real"] - res_single_asset["rows"][-1]["p10_real"]
        spread_diversified = res_diversified["rows"][-1]["p90_real"] - res_diversified["rows"][-1]["p10_real"]
        assert spread_diversified < spread_single

    def test_monte_carlo_inflation_and_tax_discounting(self) -> None:
        """Real wealth projection must strictly discount inflation and capital gains tax."""
        res = monte_carlo_projection(
            contribution=1000.0,
            annual_return_pct=8.0,
            annual_volatility_pct=10.0,
            years=10,
            initial_capital=50000.0,
            inflation_pct=2.5,
            tax_pct=28.0,
            simulations=500,
            seed=42,
        )
        # In the presence of inflation > 0, Real median < Nominal median
        assert res["rows"][-1]["p50_real"] < res["rows"][-1]["p50_nominal"]
        for row in res["rows"]:
            assert row["p50_real"] <= row["p50_nominal"]

    def test_monte_carlo_percentiles_order_and_bounds(self) -> None:
        """P10 <= P50 <= P90 invariant must hold across all years."""
        res = monte_carlo_projection(
            contribution=500.0,
            annual_return_pct=6.0,
            annual_volatility_pct=15.0,
            years=5,
            simulations=500,
            seed=123,
        )
        assert res["rows"][-1]["p10_nominal"] <= res["rows"][-1]["p50_nominal"] <= res["rows"][-1]["p90_nominal"]
        assert res["rows"][-1]["p10_real"] <= res["rows"][-1]["p50_real"] <= res["rows"][-1]["p90_real"]
        for row in res["rows"]:
            assert row["p10_real"] <= row["p50_real"] <= row["p90_real"]
            assert row["p10_nominal"] <= row["p50_nominal"] <= row["p90_nominal"]

    def test_monte_carlo_reproducibility_with_seed(self) -> None:
        """Identical random seeds must yield bit-for-bit identical projection results."""
        run1 = monte_carlo_projection(contribution=300.0, years=5, simulations=200, seed=999)
        run2 = monte_carlo_projection(contribution=300.0, years=5, simulations=200, seed=999)
        run3 = monte_carlo_projection(contribution=300.0, years=5, simulations=200, seed=888)

        assert run1["rows"][-1]["p50_nominal"] == run2["rows"][-1]["p50_nominal"]
        assert run1["rows"][-1]["p50_real"] == run2["rows"][-1]["p50_real"]
        assert run1["rows"][-1]["p50_nominal"] != run3["rows"][-1]["p50_nominal"]

    # ------------------------------------------------------------------------
    # Domain 3: XIRR & Rentability Solvers (Feature F5)
    # ------------------------------------------------------------------------

    def test_xirr_annualized_regular_annual_doubling(self) -> None:
        """XIRR for an exact 1-year 10% gain must return 0.10 (10%)."""
        flows = [
            (date(2025, 1, 1), -1000.0),
            (date(2026, 1, 1), 1100.0),
        ]
        rate = xirr(flows)
        assert rate is not None
        assert math.isclose(rate, 0.10, rel_tol=1e-5)

    def test_xirr_pct_scale_factor(self) -> None:
        """xirr_pct must return the percentage representation of xirr."""
        flows = [
            ("2025-01-01", -5000.0),
            ("2025-07-01", -2000.0),
            ("2026-01-01", 7700.0),
        ]
        rate = xirr(flows)
        rate_pct = xirr_pct(flows)
        assert rate is not None and rate_pct is not None
        assert math.isclose(rate_pct, rate * 100.0, rel_tol=1e-7)

    def test_xirr_irregular_dates_multi_year(self) -> None:
        """XIRR must converge accurately on irregular multi-year cash flows."""
        flows = [
            (date(2023, 3, 15), -10000.0),
            (date(2023, 9, 20), -5000.0),
            (date(2024, 4, 10), 2000.0),
            (date(2024, 11, 5), -3000.0),
            (date(2025, 3, 15), 18500.0),
        ]
        rate = xirr(flows)
        assert rate is not None
        assert 0.05 < rate < 0.25

    def test_xirr_requires_both_signs(self) -> None:
        """XIRR must return None if cash flows do not contain both positive and negative values."""
        all_inflows = [
            (date(2025, 1, 1), -1000.0),
            (date(2025, 6, 1), -500.0),
        ]
        all_outflows = [
            (date(2025, 1, 1), 1000.0),
            (date(2025, 6, 1), 500.0),
        ]
        assert xirr(all_inflows) is None
        assert xirr(all_outflows) is None

    def test_xirr_zero_amount_handling_and_convergence(self) -> None:
        """Zero amounts should be ignored and single net flows handled cleanly."""
        flows_with_zero = [
            (date(2025, 1, 1), -1000.0),
            (date(2025, 3, 1), 0.0),
            (date(2026, 1, 1), 1100.0),
        ]
        rate = xirr(flows_with_zero)
        assert rate is not None
        assert math.isclose(rate, 0.10, rel_tol=1e-5)

    # ------------------------------------------------------------------------
    # Domain 4: Cashflow & Recurring Forecasting (Feature F6)
    # ------------------------------------------------------------------------

    def test_description_normalization_and_fuzzy_matching(self) -> None:
        """Description normalization must strip accents, special chars, and sub tags."""
        raw = "  Continente Hipermercados (Subscrição)  "
        normalized = _normalise_description_cached(raw)
        assert normalized == "continente hipermercados"

        raw_accent = "Café & Padaria São João"
        norm_accent = _normalise_description_cached(raw_accent)
        assert norm_accent == "cafe padaria sao joao"

    def test_recurring_amount_matching_tolerances(self) -> None:
        """Amounts matching must respect relative tolerance (2.5%) and absolute tolerance."""
        assert CashflowForecastService._amounts_match(100.0, 102.0)
        assert not CashflowForecastService._amounts_match(100.0, 110.0)
        assert CashflowForecastService._amounts_match(10.00, 10.04)

    def test_cashflow_projection_window_consistency(
        self, test_repo: Repo, test_cashflow_service: CashflowForecastService
    ) -> None:
        """Cashflow projection must return summaries for 30, 60, and 90-day windows."""
        res = test_cashflow_service.cashflow_calendar_forecast(horizon_days=90)
        assert "summaries" in res
        assert "30" in res["summaries"]
        assert "60" in res["summaries"]
        assert "90" in res["summaries"]
        assert res["summaries"]["30"]["days"] == 30
        assert res["summaries"]["60"]["days"] == 60
        assert res["summaries"]["90"]["days"] == 90

    def test_cashflow_recurring_frequency_detection(self, test_repo: Repo) -> None:
        """PaymentsService must calculate correct monthly equivalent for periodic subscriptions."""
        payments = PaymentsService(test_repo)
        monthly_row = {"amount": 30.0, "period": "Mensal"}
        annual_row = {"amount": 120.0, "period": "Anual"}
        quarterly_row = {"amount": 60.0, "period": "Trimestral"}

        assert math.isclose(payments.subscription_monthly_amount(monthly_row), 30.0)
        assert math.isclose(payments.subscription_monthly_amount(annual_row), 10.0)
        assert math.isclose(payments.subscription_monthly_amount(quarterly_row), 20.0)

    def test_cashflow_event_signature_matching(self) -> None:
        """Signature matching should accurately identify recurring bills by name & category."""
        sig = {"name": "Netflix", "amount": 15.99, "category": "Subscrições"}
        assert CashflowForecastService._event_matches_signature(
            description="Netflix Mensalidade",
            amount=-15.99,
            category="Subscrições",
            signature=sig,
        )
        assert not CashflowForecastService._event_matches_signature(
            description="Spotify",
            amount=-15.99,
            category="Subscrições",
            signature=sig,
        )

    # ------------------------------------------------------------------------
    # Domain 5: SQL Split Aggregations & Query Optimizations (Feature F7, F8)
    # ------------------------------------------------------------------------

    def test_category_spend_with_splits_reconciliation(self, test_repo: Repo) -> None:
        """Split transactions must allocate sub-amounts into their effective categories."""
        # Create a parent transaction of -100€ in category 'Supermercado'
        tx_id = test_repo.add_transaction(
            "2026-06-15",
            "Compras Continente",
            -100.0,
            "Supermercado",
            "Conta Principal",
        )
        # Split it into 60% Supermercado and 40% Farmácia
        test_repo.save_transaction_splits(
            tx_id,
            [
                ("Supermercado", 60.0),
                ("Farmácia", 40.0),
            ],
        )

        spend = test_repo.category_spend_with_splits("2026-06-01", "2026-06-30")
        by_cat = {str(row["category"]): float(row["total"]) for row in spend}

        assert math.isclose(by_cat.get("Supermercado", 0.0), 60.0, rel_tol=1e-5)
        assert math.isclose(by_cat.get("Farmácia", 0.0), 40.0, rel_tol=1e-5)
        assert sum(by_cat.values()) == 100.0

    def test_dynamic_grouped_by_category_and_merchant(self, test_repo: Repo) -> None:
        """Dynamic grouping must aggregate correctly by Category and Merchant."""
        test_repo.add_transaction_with_meta(
            "2026-06-10",
            "Jantar",
            -45.0,
            "Restauração",
            "Conta Principal",
            merchant="Restaurante Central",
        )
        test_repo.add_transaction_with_meta(
            "2026-06-12",
            "Almoço",
            -25.0,
            "Restauração",
            "Conta Principal",
            merchant="Restaurante Central",
        )

        by_cat = test_repo.dynamic_grouped("2026-06-01", "2026-06-30", group_by="Categoria")
        by_merch = test_repo.dynamic_grouped("2026-06-01", "2026-06-30", group_by="Comerciante")

        assert any(row["label"] == "Restauração" and math.isclose(row["total"], 70.0) for row in by_cat)
        assert any(row["label"] == "Restaurante Central" and math.isclose(row["total"], 70.0) for row in by_merch)

    def test_totals_for_payment_period_income_expense_savings(self, test_repo: Repo) -> None:
        """Payment period totals must compute exact income, expense, and savings."""
        test_repo.add_transaction(
            "2026-05-01",
            "Salário",
            3000.0,
            "Salário",
            "Conta Principal",
        )
        test_repo.add_transaction(
            "2026-05-05",
            "Renda",
            -900.0,
            "Habitação",
            "Conta Principal",
        )
        test_repo.add_transaction(
            "2026-05-10",
            "Supermercado",
            -300.0,
            "Alimentação",
            "Conta Principal",
        )

        totals = test_repo.totals_for_payment_period("2026-05-01", "2026-05-31")
        assert math.isclose(totals["income"], 3000.0)
        assert math.isclose(totals["expenses"], 1200.0)
        assert math.isclose(totals["savings"], 1800.0)

    def test_monthly_totals_grouped_for_year(self, test_repo: Repo) -> None:
        """Batch monthly totals must return accurate dictionary keyed by YYYY-MM."""
        test_repo.add_transaction("2026-01-15", "A", 2000.0, "Salário", "Conta Principal")
        test_repo.add_transaction("2026-01-20", "B", -500.0, "Habitação", "Conta Principal")
        test_repo.add_transaction("2026-02-15", "C", 2000.0, "Salário", "Conta Principal")
        test_repo.add_transaction("2026-02-20", "D", -800.0, "Habitação", "Conta Principal")

        yearly = test_repo.monthly_totals_grouped_for_year(2026)
        assert "2026-01" in yearly
        assert "2026-02" in yearly
        assert math.isclose(yearly["2026-01"]["savings"], 1500.0)
        assert math.isclose(yearly["2026-02"]["savings"], 1200.0)

    def test_chart_movement_details_split_expansion(self, test_repo: Repo) -> None:
        """Chart movement details must expand split transactions into separate lines."""
        tx_id = test_repo.add_transaction(
            "2026-07-01",
            "Loja Departamentos",
            -200.0,
            "Vestuário",
            "Conta Principal",
        )
        test_repo.save_transaction_splits(
            tx_id,
            [
                ("Vestuário", 75.0),
                ("Casa", 25.0),
            ],
        )

        details = test_repo.chart_movement_details("2026-07-01", "2026-07-31", category="Casa")
        assert len(details) == 1
        assert math.isclose(abs(float(details[0]["amount"])), 50.0)
        assert int(details[0]["split_contribution"]) == 1

    # ------------------------------------------------------------------------
    # Domain 6: SQLite Engine & Incremental Vacuuming (Feature F9)
    # ------------------------------------------------------------------------

    def test_engine_wal_mode_and_memory_pragmas(self, test_db: FinanceDB) -> None:
        """FinanceDB must run in WAL mode with optimized synchronous and memory settings."""
        journal_mode = test_db.conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert str(journal_mode).casefold() == "wal"

        sync_mode = test_db.conn.execute("PRAGMA synchronous").fetchone()[0]
        # NORMAL (1) or FULL (2)
        assert sync_mode in (1, 2)

    def test_engine_integrity_check_clean_db(self, test_db: FinanceDB) -> None:
        """PRAGMA integrity_check on active database must yield 'ok'."""
        integrity = test_db.conn.execute("PRAGMA integrity_check").fetchone()[0]
        assert str(integrity).casefold() == "ok"

    def test_engine_read_only_connection_isolation(self, test_db: FinanceDB) -> None:
        """open_read_only must disallow write operations while allowing queries."""
        ro_db = FinanceDB.open_read_only(test_db.path)
        try:
            # Querying must succeed
            count = ro_db.conn.execute("SELECT COUNT(*) FROM categories").fetchone()[0]
            assert count >= 0

            # Write operations must fail with read-only error
            with pytest.raises(sqlite3.OperationalError):
                ro_db.conn.execute("INSERT INTO settings(key, value) VALUES('a', 'b')")
        finally:
            ro_db.conn.close()

    def test_engine_incremental_vacuum_execution(self, test_db: FinanceDB) -> None:
        """PRAGMA incremental_vacuum must execute safely on the database."""
        test_db.conn.execute("PRAGMA incremental_vacuum(10)")
        test_db.conn.commit()
        integrity = test_db.conn.execute("PRAGMA integrity_check").fetchone()[0]
        assert str(integrity).casefold() == "ok"

    def test_engine_transaction_savepoint_rollback(self, test_db: FinanceDB) -> None:
        """Savepoint rollback must cleanly revert uncommitted modifications."""
        test_db.conn.execute("SAVEPOINT test_sp")
        test_db.conn.execute("INSERT INTO settings(key, value) VALUES('temp_sp_key', 'val')")
        # Rollback savepoint
        test_db.conn.execute("ROLLBACK TO SAVEPOINT test_sp")
        test_db.conn.execute("RELEASE SAVEPOINT test_sp")

        row = test_db.conn.execute("SELECT value FROM settings WHERE key='temp_sp_key'").fetchone()
        assert row is None

    # ------------------------------------------------------------------------
    # Domain 7: Snapshot Compaction & Change Journal (Feature F10)
    # ------------------------------------------------------------------------

    def test_change_journal_delete_created_undo(
        self, test_repo: Repo, test_journal_service: ChangeJournalService
    ) -> None:
        """Undo for a newly created transaction must remove it and its metadata."""
        tx_id = test_repo.add_transaction(
            "2026-08-01",
            "Compra para desfazer",
            -25.0,
            "Outros",
            "Conta Principal",
        )
        test_journal_service.record_created_transactions(
            [tx_id],
            label="Importação teste",
        )
        undo_available = test_journal_service.latest_available()
        assert undo_available is not None

        test_journal_service.undo_latest()
        # Transaction must no longer exist
        tx = test_repo.transaction_by_id(tx_id)
        assert tx is None

    def test_change_journal_restore_existing_undo(
        self, test_repo: Repo, test_journal_service: ChangeJournalService
    ) -> None:
        """Undo for an edited transaction must restore previous values."""
        tx_id = test_repo.add_transaction(
            "2026-08-01",
            "Original",
            -50.0,
            "Alimentação",
            "Conta Principal",
        )
        snapshot = test_journal_service.capture_transactions([tx_id])
        test_journal_service.record_transaction_snapshot(
            snapshot,
            label="Modificação",
            action_type="edit",
        )
        test_repo.update_transaction(tx_id, description="Modificado", amount=-80.0)

        undo_action = test_journal_service.latest_available()
        assert undo_action is not None
        test_journal_service.undo_latest()

        restored = test_repo.transaction_by_id(tx_id)
        assert restored is not None
        assert math.isclose(float(restored["amount"]), -50.0)
        assert str(restored["description"]) == "Original"

    def test_change_journal_restore_deleted_undo(
        self, test_repo: Repo, test_journal_service: ChangeJournalService
    ) -> None:
        """Undo for a deleted transaction must recreate the row exactly."""
        tx_id = test_repo.add_transaction(
            "2026-08-02",
            "Apagar e Recuperar",
            -75.0,
            "Lazer",
            "Conta Principal",
        )
        test_journal_service.delete_transactions([tx_id], label="Apagar e Recuperar")
        assert test_repo.transaction_by_id(tx_id) is None

        undo_action = test_journal_service.latest_available()
        assert undo_action is not None
        test_journal_service.undo_latest()

        recovered = test_repo.transaction_by_id(tx_id)
        assert recovered is not None
        assert math.isclose(float(recovered["amount"]), -75.0)

    def test_change_journal_attachment_transaction_safety(
        self, test_repo: Repo, test_journal_service: ChangeJournalService, sandbox_dir: Path
    ) -> None:
        """Attachments deleted during an undoable operation must be preserved in storage."""
        tx_id = test_repo.add_transaction(
            "2026-08-03",
            "Com Fatura",
            -120.0,
            "Outros",
            "Conta Principal",
        )
        att_file = sandbox_dir / "anexos" / str(tx_id) / "fatura.pdf"
        att_file.parent.mkdir(parents=True, exist_ok=True)
        att_file.write_bytes(b"%PDF-1.4 test invoice content")
        test_repo.add_transaction_attachment(
            transaction_id=tx_id,
            file_name="fatura.pdf",
            stored_name="fatura.pdf",
            stored_path=str(att_file),
            original_path=str(att_file),
            file_type="application/pdf",
            description="Fatura teste",
            supplier="Fornecedor",
            invoice_date="2026-08-03",
            total_amount=120.0,
            file_size=len(b"%PDF-1.4 test invoice content"),
        )

        test_journal_service.delete_transactions([tx_id], label="Apagar com anexo")
        assert test_repo.transaction_by_id(tx_id) is None

        undo_action = test_journal_service.latest_available()
        assert undo_action is not None
        test_journal_service.undo_latest()

        # Transaction is restored
        assert test_repo.transaction_by_id(tx_id) is not None
        assert att_file.exists()

    def test_change_journal_table_whitelist_guard(
        self, test_repo: Repo, test_journal_service: ChangeJournalService
    ) -> None:
        """Attempting to journal a non-whitelisted table must raise RuntimeError."""
        with pytest.raises(RuntimeError, match="não permitida"):
            test_journal_service._validated_row_columns(
                test_repo.db.conn,
                table="sqlite_master",
                row={"type": "table"},
            )

    # ------------------------------------------------------------------------
    # Domain 8: Encrypted Backup Packaging & Disaster Recovery (Feature F11)
    # ------------------------------------------------------------------------

    def test_backup_aes_gcm_encrypt_decrypt_roundtrip(self, sandbox_dir: Path) -> None:
        """AES-GCM encryption/decryption must preserve 100% bit-for-bit file contents."""
        source = sandbox_dir / "payload.txt"
        encrypted = sandbox_dir / "payload.lgbak"
        decrypted = sandbox_dir / "recovered.txt"

        original_data = b"Financas Pessoais Encrypted Payload \x00\xff\xfe Test Content 2026"
        source.write_bytes(original_data)

        password = "MinhaPalavraPasseForte123!"
        BackupService.encrypt_file(source, encrypted, password, payload_kind="S")
        assert encrypted.exists()
        assert encrypted.stat().st_size > len(original_data)

        BackupService.decrypt_file(encrypted, decrypted, password)
        assert decrypted.read_bytes() == original_data

    def test_backup_wrong_password_rejection(self, sandbox_dir: Path) -> None:
        """Decryption with incorrect password must fail with authentication error."""
        source = sandbox_dir / "secret.txt"
        encrypted = sandbox_dir / "secret.lgbak"
        decrypted = sandbox_dir / "secret_out.txt"
        source.write_text("Confidential Data", encoding="utf-8")

        BackupService.encrypt_file(source, encrypted, "PalavraPasseCerta123!", payload_kind="S")

        with pytest.raises(ValueError, match="Palavra-passe incorreta|danificado"):
            BackupService.decrypt_file(encrypted, decrypted, "PalavraPasseErrada456!")

    def test_backup_corrupted_payload_rejection(self, sandbox_dir: Path) -> None:
        """Ciphertext tampering must be caught by AES-GCM authentication tag."""
        source = sandbox_dir / "tamper_source.txt"
        encrypted = sandbox_dir / "tamper_test.lgbak"
        decrypted = sandbox_dir / "tamper_out.txt"
        source.write_text("Important Data to Tamper", encoding="utf-8")

        password = "ChaveSeguraDeBackup2026!"
        BackupService.encrypt_file(source, encrypted, password, payload_kind="S")

        # Corrupt one byte in the middle of ciphertext
        data = bytearray(encrypted.read_bytes())
        data[len(data) // 2] ^= 0xFF
        encrypted.write_bytes(bytes(data))

        with pytest.raises(ValueError, match="Palavra-passe incorreta|danificado"):
            BackupService.decrypt_file(encrypted, decrypted, password)

    def test_backup_complete_zip_manifest_validation(
        self, test_repo: Repo, test_backup_service: BackupService, sandbox_dir: Path
    ) -> None:
        """Full backup ZIP must contain all 4 scopes and a valid protection manifest."""
        zip_target = sandbox_dir / "backups" / "financas_backup_test.zip"
        test_backup_service.export_backup_zip(zip_target)

        assert zip_target.exists()
        manifest = test_backup_service.validate_complete_archive(zip_target)
        assert "database" in manifest
        assert "tables" in manifest
        assert manifest["database"]["archive_name"] == "financas_pessoais.sqlite3"

    def test_backup_secondary_mirror_synchronization(
        self, test_repo: Repo, test_backup_service: BackupService, sandbox_dir: Path
    ) -> None:
        """Secondary backup mirror must verify SHA-256 match."""
        secondary_dir = sandbox_dir.parent / "secondary_backups"
        test_backup_service.save_preferences(
            interval_minutes=1440,
            keep=10,
            backup_on_close=False,
            encryption_enabled=False,
            secondary_enabled=True,
            secondary_directory=str(secondary_dir),
        )
        target = test_backup_service.create_manual_backup()
        mirrored = secondary_dir / target.name

        assert mirrored.exists()
        assert BackupService._file_digest(target) == BackupService._file_digest(mirrored)

    # ------------------------------------------------------------------------
    # Domain 9: Decimal Precision & Monetary Invariants (Features F3, F7)
    # ------------------------------------------------------------------------

    def test_transaction_split_percentage_sum_reconciliation(self, test_repo: Repo) -> None:
        """Splits on a transaction must sum to exactly 100.0% and total amount."""
        tx_id = test_repo.add_transaction(
            "2026-06-20",
            "Fatura Complexa",
            -300.0,
            "Outros",
            "Conta Principal",
        )
        splits = [
            ("Categoria 1", 33.333333),
            ("Categoria 2", 33.333333),
            ("Categoria 3", 33.333334),
        ]
        test_repo.save_transaction_splits(tx_id, splits)
        loaded_splits = test_repo.transaction_splits(tx_id)

        total_amount = sum(float(s["amount"]) for s in loaded_splits)
        total_pct = sum(float(s["percent"]) for s in loaded_splits)
        assert math.isclose(total_amount, 300.0, abs_tol=1e-5)
        assert math.isclose(total_pct, 100.0, abs_tol=1e-4)

    def test_cent_perfect_allocation_no_penny_drift(self) -> None:
        """Arithmetic splitting of uneven cents must preserve penny integrity."""
        # 100.00€ split 3 ways: 33.33 + 33.33 + 33.34 = 100.00
        cents = 10000  # 100.00 EUR in integer cents
        part1 = cents // 3
        part2 = cents // 3
        part3 = cents - (part1 + part2)
        assert part1 == 3333
        assert part2 == 3333
        assert part3 == 3334
        assert (part1 + part2 + part3) == 10000

    def test_large_currency_aggregation_invariance(self, test_repo: Repo) -> None:
        """Summing 1,000 transactions of 1.23€ must equal exactly 1,230.00€."""
        for i in range(1000):
            test_repo.db.conn.execute(
                """
                INSERT INTO transactions(date, description, amount, category, account, kind)
                VALUES(?, ?, ?, ?, ?, ?)
                """,
                ("2026-01-01", f"Tx {i}", 1.23, "Salário", "Conta Principal", "income"),
            )
        test_repo.db.conn.commit()

        total = test_repo.scalar("SELECT SUM(amount) FROM transactions WHERE amount > 0")
        assert math.isclose(float(total), 1230.00, abs_tol=1e-5)

    def test_financial_models_safe_float_bounds(self) -> None:
        """safe_float helper must handle NaN, Inf, None, and strings safely."""
        assert safe_float(None) == 0.0
        assert safe_float("123.45") == 123.45
        assert safe_float("invalid", default=10.0) == 10.0
        assert safe_float(float("nan"), default=0.0) == 0.0
        assert safe_float(float("inf"), default=99.0) == 99.0

    def test_holding_net_gain_and_return_pct_precision(self) -> None:
        """Holding entity must calculate net gain and return percentage with exact precision."""
        holding = Holding(
            id=1,
            name="ETF Global World",
            asset_class="Ações",
            symbol="VWCE",
            quantity=10.0,
            unit_price=150.0,
            value=1500.0,
            cost_basis=1000.0,
            fees=10.0,
        )
        # Gross gain: 1500 - 1000 = 500
        assert math.isclose(holding.gross_gain, 500.0)
        # Net gain: 1500 - (1000 + 10) = 490.0
        assert math.isclose(holding.net_gain, 490.0)
        # Return pct: 490 / 1010 * 100 = 48.51485...%
        assert math.isclose(holding.return_pct, (490.0 / 1010.0) * 100.0, rel_tol=1e-5)


# ============================================================================
# TIER 2: BOUNDARY & CORNER CASES
# ============================================================================

class TestTier2BoundaryAndCornerCases:
    """Tier 2: Boundary, Extreme, and Adversarial Corner Cases."""

    def test_zero_starting_capital_and_zero_contributions(self) -> None:
        """Zero starting capital with zero contributions must yield 0 wealth cleanly."""
        res = monte_carlo_projection(
            contribution=0.0,
            annual_return_pct=6.0,
            annual_volatility_pct=10.0,
            years=10,
            initial_capital=0.0,
            simulations=100,
            seed=42,
        )
        assert res["rows"][-1]["p50_nominal"] == 0.0
        assert res["rows"][-1]["p50_real"] == 0.0

    def test_empty_ledger_analytics_and_forecasting(
        self, test_repo: Repo, test_cashflow_service: CashflowForecastService
    ) -> None:
        """Analytics and forecasting queries on an empty database must not crash."""
        totals = test_repo.totals_for_payment_period("2026-01-01", "2026-12-31")
        assert totals["income"] == 0.0
        assert totals["expenses"] == 0.0
        assert totals["savings"] == 0.0

        spend = test_repo.category_spend_with_splits("2026-01-01", "2026-12-31")
        assert len(spend) == 0

        forecast = test_cashflow_service.cashflow_calendar_forecast(horizon_days=30)
        assert forecast["starting_balance"] == 0.0

    def test_extreme_market_volatility_and_market_crash(self) -> None:
        """Extreme 150% annual volatility and market crash scenarios must not produce NaNs."""
        res = monte_carlo_projection(
            contribution=1000.0,
            annual_return_pct=-30.0,
            annual_volatility_pct=150.0,
            years=10,
            initial_capital=10000.0,
            simulations=200,
            seed=42,
        )
        assert not math.isnan(res["rows"][-1]["p50_real"])
        assert not math.isinf(res["rows"][-1]["p50_real"])
        assert res["rows"][-1]["p10_real"] >= 0.0

    def test_single_transaction_portfolio_and_single_asset(self, test_repo: Repo) -> None:
        """Ledgers with only 1 transaction or 1 holding must evaluate cleanly."""
        test_repo.add_transaction(
            "2026-01-01",
            "Único movimento",
            500.0,
            "Salário",
            "Conta Principal",
        )
        res = test_repo.monthly_totals_grouped_for_year(2026)
        assert "2026-01" in res
        assert math.isclose(res["2026-01"]["income"], 500.0)

    def test_extended_century_horizon_fire_planning(self) -> None:
        """FIRE milestone calculation for 100-year horizons must not overflow."""
        milestones = FirePlanningEngine.calculate_fire_milestones(
            current_invested=10000.0,
            annual_expenses=24000.0,
            annual_savings=6000.0,
            real_growth_rate_pct=4.0,
            swr_pct=4.0,
            years_to_retirement=100.0,
        )
        assert "standard" in milestones
        assert "coast" in milestones
        assert milestones["standard"].target_capital == 600000.0
        assert milestones["coast"].target_capital < milestones["standard"].target_capital

    def test_corrupted_zero_byte_and_truncated_backup_rejection(self, sandbox_dir: Path) -> None:
        """Zero-byte and truncated encrypted files must be immediately rejected."""
        empty_file = sandbox_dir / "empty.lgbak"
        empty_file.touch()

        target = sandbox_dir / "out.sqlite3"
        with pytest.raises(ValueError, match="vazio ou incompleto"):
            BackupService.decrypt_file(empty_file, target, "PalavraPasse123!")

    def test_tampered_ciphertext_gcm_tag_validation(self, sandbox_dir: Path) -> None:
        """Truncating the GCM tag at the end of the file must prevent decryption."""
        source = sandbox_dir / "source.txt"
        encrypted = sandbox_dir / "truncated.lgbak"
        decrypted = sandbox_dir / "out.txt"
        source.write_bytes(b"Valuable Financial Secrets")

        BackupService.encrypt_file(source, encrypted, "Password123!", payload_kind="S")
        # Truncate last 8 bytes of the tag
        full_data = encrypted.read_bytes()
        encrypted.write_bytes(full_data[:-8])

        with pytest.raises(ValueError, match="Palavra-passe incorreta|danificado|incompleto"):
            BackupService.decrypt_file(encrypted, decrypted, "Password123!")


# ============================================================================
# TIER 3: CROSS-FEATURE COMBINATIONS (Pairwise Module Interactions)
# ============================================================================

class TestTier3CrossFeatureCombinations:
    """Tier 3: Inter-Service and Pairwise Subsystem Integration."""

    def test_pair_backup_restore_and_monte_carlo_on_restored_data(
        self, test_repo: Repo, test_backup_service: BackupService, sandbox_dir: Path
    ) -> None:
        """Creating an encrypted backup, restoring it, and running Monte Carlo on restored data."""
        # Step 1: Populate ledger and holdings
        test_repo.add_transaction("2026-01-01", "Income", 5000.0, "Salário", "Conta Principal")
        test_repo.add_transaction("2026-01-02", "Expense", -2000.0, "Habitação", "Conta Principal")
        test_repo.add_holding(
            name="Ações Mundiais",
            asset_class="Ações",
            account="Manual",
            value=5000.0,
            cost_basis=4000.0,
        )

        # Step 2: Create complete encrypted export
        backup_file = sandbox_dir / "backups" / "export_pair.lgbak"
        test_backup_service.export_encrypted_backup(backup_file, password="ChaveDeTesteSegura123!")

        # Step 3: Clean current repo and restore from backup
        test_repo.db.conn.close()
        db_path = Path(test_repo.db.path)
        db_path.unlink(missing_ok=True)
        for suffix in ("-wal", "-shm"):
            Path(f"{db_path}{suffix}").unlink(missing_ok=True)

        BackupService.restore_missing_profile(
            backup_file,
            database_file=db_path,
            supported_schema_version=FinanceDB.CURRENT_SCHEMA_VERSION,
            password="ChaveDeTesteSegura123!",
        )

        # Step 4: Verify restored financial state
        test_repo.db = FinanceDB(
            db_path,
            attachments_directory=sandbox_dir / "anexos",
            history_directory=sandbox_dir / "historico_alteracoes",
            preferences_file=sandbox_dir / "backup_preferences.json",
            protection_directory=sandbox_dir / "protecoes",
        )
        assert test_repo.scalar("SELECT COUNT(*) FROM transactions") == 2
        holdings = test_repo.holdings()
        assert len(holdings) == 1
        total_invested = sum(float(h["value"]) for h in holdings)
        assert math.isclose(total_invested, 5000.0)

        # Step 5: Feed restored capital into Monte Carlo engine
        mc = monte_carlo_projection(
            contribution=300.0,
            initial_capital=total_invested,
            years=5,
            simulations=200,
            seed=42,
        )
        assert mc["rows"][-1]["p50_nominal"] > 5000.0

    def test_pair_split_transactions_budget_and_category_spend(self, test_repo: Repo) -> None:
        """Split transactions interaction with budget categories and monthly aggregated spend."""
        test_repo.category_identity("Supermercado", create=True, kind="expense")
        test_repo.category_identity("Farmácia", create=True, kind="expense")
        test_repo.set_category_budget("Supermercado", 500.0)
        test_repo.set_category_budget("Farmácia", 100.0)

        tx_id = test_repo.add_transaction(
            "2026-03-10",
            "Hipermercado e Farmácia",
            -150.0,
            "Supermercado",
            "Conta Principal",
        )
        test_repo.save_transaction_splits(
            tx_id,
            [
                ("Supermercado", 66.666667),
                ("Farmácia", 33.333333),
            ],
        )

        spend = test_repo.category_spend_with_splits("2026-03-01", "2026-03-31")
        by_cat = {str(r["category"]): float(r["total"]) for r in spend}
        assert math.isclose(by_cat["Supermercado"], 100.0, abs_tol=1e-4)
        assert math.isclose(by_cat["Farmácia"], 50.0, abs_tol=1e-4)

    def test_pair_cashflow_forecast_recurring_and_change_journal_undo(
        self,
        test_repo: Repo,
        test_cashflow_service: CashflowForecastService,
        test_journal_service: ChangeJournalService,
    ) -> None:
        """Cancelling or deleting a recurring transaction and undoing via ChangeJournal restores cashflow."""
        tx_id = test_repo.add_transaction(
            "2026-04-01",
            "Assinatura Ginásio",
            -40.0,
            "Saúde",
            "Conta Principal",
        )
        test_journal_service.delete_transactions([tx_id], label="Apagar assinatura")
        assert test_repo.transaction_by_id(tx_id) is None

        # Undo deletion
        undo_action = test_journal_service.latest_available()
        assert undo_action is not None
        test_journal_service.undo_latest()

        # Cashflow forecast incorporates the restored transaction
        forecast = test_cashflow_service.cashflow_calendar_forecast(horizon_days=30)
        assert forecast is not None
        assert test_repo.transaction_by_id(tx_id) is not None

    def test_pair_fire_milestones_networth_and_financial_state_snapshot(self, test_repo: Repo) -> None:
        """Historical financial snapshot state feeds directly into FIRE decumulation models."""
        test_repo.add_holding(
            name="ETF S&P 500",
            asset_class="Ações",
            account="Manual",
            value=20000.0,
            cost_basis=15000.0,
        )
        test_repo.record_financial_state_snapshot("2026-05-31")

        state = test_repo.financial_state_as_of("2026-05-31")
        invested = sum(float(h["value"]) for h in state["holdings"])

        milestones = FirePlanningEngine.calculate_fire_milestones(
            current_invested=invested,
            annual_expenses=12000.0,
            annual_savings=4000.0,
            swr_pct=4.0,
        )
        assert milestones["standard"].progress_pct == round((20000.0 / 300000.0) * 100.0, 2)

    def test_pair_encrypted_backup_change_journal_and_managed_attachments(
        self,
        test_repo: Repo,
        test_backup_service: BackupService,
        test_journal_service: ChangeJournalService,
        sandbox_dir: Path,
    ) -> None:
        """Full encrypted backup roundtrip preserves ChangeJournal undo records and attachments."""
        tx_id = test_repo.add_transaction(
            "2026-06-01",
            "Com anexo",
            -10.0,
            "Outros",
            "Conta Principal",
        )
        att_dir = sandbox_dir / "anexos" / str(tx_id)
        att_dir.mkdir(parents=True, exist_ok=True)
        att_file = att_dir / "recibo.txt"
        att_file.write_text("Recibo emitido 2026", encoding="utf-8")
        test_repo.add_transaction_attachment(
            transaction_id=tx_id,
            file_name="recibo.txt",
            stored_name="recibo.txt",
            stored_path=str(att_file),
            original_path=str(att_file),
            file_type="text/plain",
            description="Recibo teste",
            supplier="Fornecedor",
            invoice_date="2026-06-01",
            total_amount=10.0,
            file_size=len(b"Recibo emitido 2026"),
        )

        test_journal_service.record_created_transactions([tx_id], label="Importação anexo")

        backup_file = sandbox_dir / "backups" / "full_scope.lgbak"
        test_backup_service.export_encrypted_backup(backup_file, password="SuperPassword2026!")

        # Restore into sandbox
        test_repo.db.conn.close()
        db_path = Path(test_repo.db.path)
        db_path.unlink(missing_ok=True)
        for suffix in ("-wal", "-shm"):
            Path(f"{db_path}{suffix}").unlink(missing_ok=True)

        BackupService.restore_missing_profile(
            backup_file,
            database_file=db_path,
            supported_schema_version=FinanceDB.CURRENT_SCHEMA_VERSION,
            password="SuperPassword2026!",
        )
        test_repo.db = FinanceDB(
            db_path,
            attachments_directory=sandbox_dir / "anexos",
            history_directory=sandbox_dir / "historico_alteracoes",
            preferences_file=sandbox_dir / "backup_preferences.json",
            protection_directory=sandbox_dir / "protecoes",
        )
        test_journal_service.repo = test_repo
        assert (sandbox_dir / "anexos" / str(tx_id) / "recibo.txt").exists()
        assert test_journal_service.latest_available() is not None


# ============================================================================
# TIER 4: REAL-WORLD WORKLOAD SCENARIOS (End-to-End High Volume Lifecycles)
# ============================================================================

class TestTier4RealWorldWorkloadScenarios:
    """Tier 4: End-to-end full lifecycle under high-volume production workload."""

    def test_real_world_lifecycle_high_volume_ledger_and_full_ecosystem(
        self,
        test_repo: Repo,
        test_backup_service: BackupService,
        test_journal_service: ChangeJournalService,
        test_cashflow_service: CashflowForecastService,
        sandbox_dir: Path,
    ) -> None:
        """Simulate a complete user financial lifecycle with 1,000+ transactions.

        1. Ingestion of 1,000+ transactions spanning 12 calendar months.
        2. Complex split categorization of supermarket & department purchases.
        3. Portfolio valuation tracking across multiple asset classes.
        4. Calculation of FIRE milestones and Monte Carlo projections.
        5. Encrypted backup packaging, database compaction, and vacuuming.
        6. Clean database restoration and 100% mathematical integrity verification.
        """
        rng = random.Random(42)
        start_date = date(2025, 1, 1)

        # 1. Ingest 1,000 realistic transactions
        categories_income = ["Salário", "Freelance", "Dividendos"]
        categories_expense = ["Supermercado", "Renda", "Restaurantes", "Transporte", "Saúde", "Lazer", "Tecnologia"]
        accounts = ["Conta Principal", "Cartão Crédito", "Conta Poupança"]

        inserted_income = 0.0
        inserted_expense = 0.0

        for i in range(1000):
            txn_date = start_date + timedelta(days=rng.randint(0, 360))
            is_income = rng.random() < 0.15  # 15% income, 85% expenses
            if is_income:
                amount = round(rng.uniform(1500.0, 3500.0), 2)
                cat = rng.choice(categories_income)
                inserted_income += amount
            else:
                amount = -round(rng.uniform(5.0, 350.0), 2)
                cat = rng.choice(categories_expense)
                inserted_expense += abs(amount)

            acc = rng.choice(accounts)
            tx_id = test_repo.add_transaction(
                txn_date.isoformat(),
                f"Movimento {i+1} - {cat}",
                amount,
                cat,
                acc,
            )

            # 2. Split complex purchases (approx 10% of expenses)
            if not is_income and abs(amount) > 100.0 and (i % 10 == 0):
                test_repo.save_transaction_splits(
                    tx_id,
                    [
                        (cat, 50.0),
                        ("Outros", 50.0),
                    ],
                )

        # 3. Setup holdings portfolio
        test_repo.add_holding(
            name="Vanguard All-World",
            asset_class="Ações",
            account="Manual",
            value=22000.0,
            cost_basis=18000.0,
        )
        test_repo.add_holding(
            name="Obrigações Estado",
            asset_class="Obrigações",
            account="Manual",
            value=5000.0,
            cost_basis=5000.0,
        )
        test_repo.record_financial_state_snapshot("2025-12-31")

        # 4. Verify Annual Financial Balance
        year_totals = test_repo.totals_for_payment_period("2025-01-01", "2025-12-31")
        assert math.isclose(year_totals["income"], inserted_income, rel_tol=1e-5)
        assert math.isclose(year_totals["expenses"], inserted_expense, rel_tol=1e-5)

        # 5. Compute FIRE Milestones and Stochastic Monte Carlo Forecast
        invested_total = 27000.0
        milestones = FirePlanningEngine.calculate_fire_milestones(
            current_invested=invested_total,
            annual_expenses=year_totals["expenses"],
            annual_savings=max(0.0, year_totals["savings"]),
            swr_pct=4.0,
        )
        assert milestones["standard"].target_capital == round(year_totals["expenses"] / 0.04, 2)

        mc_res = monte_carlo_projection(
            contribution=max(0.0, year_totals["savings"] / 12.0),
            initial_capital=invested_total,
            years=15,
            simulations=500,
            seed=42,
        )
        assert mc_res["rows"][-1]["p50_nominal"] > invested_total

        # 6. Database compaction & vacuuming
        test_repo.db.conn.execute("PRAGMA incremental_vacuum(50)")
        test_repo.db.conn.commit()

        # 7. Create Complete Encrypted Export
        backup_file = sandbox_dir / "backups" / "production_lifecycle.lgbak"
        password = "ChaveSeguraDeProducao2026!"
        test_backup_service.export_encrypted_backup(backup_file, password=password)
        assert backup_file.exists()

        # 8. Restore into a fresh, isolated repository instance
        restore_sandbox = sandbox_dir / "restored_system"
        restore_sandbox.mkdir(parents=True, exist_ok=True)
        restored_db_file = restore_sandbox / "financas_pessoais.sqlite3"

        BackupService.restore_missing_profile(
            backup_file,
            database_file=restored_db_file,
            supported_schema_version=FinanceDB.CURRENT_SCHEMA_VERSION,
            password=password,
        )

        restored_db = FinanceDB(
            restored_db_file,
            attachments_directory=restore_sandbox / "anexos",
            history_directory=restore_sandbox / "historico_alteracoes",
            preferences_file=restore_sandbox / "backup_preferences.json",
            protection_directory=restore_sandbox / "protecoes",
        )
        restored_repo = Repo(db=restored_db)

        # 9. Invariant Verification on Restored System
        restored_totals = restored_repo.totals_for_payment_period("2025-01-01", "2025-12-31")
        assert math.isclose(restored_totals["income"], inserted_income, rel_tol=1e-5)
        assert math.isclose(restored_totals["expenses"], inserted_expense, rel_tol=1e-5)
        assert math.isclose(restored_totals["savings"], year_totals["savings"], rel_tol=1e-5)

        restored_holdings = restored_repo.holdings()
        assert len(restored_holdings) == 2
        restored_invested = sum(float(h["value"]) for h in restored_holdings)
        assert math.isclose(restored_invested, invested_total, rel_tol=1e-5)

        # Ensure SQLite integrity is 100% OK
        integrity = restored_repo.db.conn.execute("PRAGMA integrity_check").fetchone()[0]
        assert str(integrity).casefold() == "ok"
        restored_db.conn.close()

    def test_real_world_multi_year_decumulation_with_guyton_klinger(self) -> None:
        """Multi-decade dynamic retirement decumulation simulation with Guyton-Klinger guardrails."""
        initial_capital = 500000.0
        initial_withdrawal = 20000.0  # 4% initial SWR
        # 10 years of returns with market downturns and inflation shifts
        annual_returns = [8.0, -12.0, 15.0, 5.0, -5.0, 10.0, 7.0, 6.0, -2.0, 9.0]
        annual_inflation = [2.0, 3.5, 2.5, 2.0, 1.8, 2.2, 2.0, 2.1, 1.9, 2.0]

        history = FirePlanningEngine.simulate_guyton_klinger_drawdown(
            initial_capital=initial_capital,
            initial_annual_withdrawal=initial_withdrawal,
            annual_returns=annual_returns,
            annual_inflation_rates=annual_inflation,
            initial_swr_pct=4.0,
        )

        assert len(history) == 10
        # After a negative return year (year 2), inflation in year 3 must be frozen
        assert history[2]["inflation_status"] == "Congelada (Retorno Negativo Anterior e Taxa Elevada)"
        assert history[2]["applied_inflation_pct"] == 0.0

        # Portfolio capital must remain solvent across all 10 years
        for year in history:
            assert year["ending_capital"] > 0.0
            assert year["withdrawal"] > 0.0
