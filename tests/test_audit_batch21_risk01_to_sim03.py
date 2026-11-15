#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Regression tests for audit batch 21 (risk & simulation defects):

- BUG-RISK-01 (P1): income shock must accept the full 0-100% range — the
  engine clamped to 95% and the UI slider to 80%, making a total income
  loss impossible and inconsistent between layers.
- BUG-RISK-02 (P1): runway must report the deficit absorption time when the
  emergency expense pushes cash negative but the monthly flow is positive
  (two-phase burn), never an unconditional 0 months.
- BUG-RISK-03 (P1): zero income with outstanding debt payments must yield a
  critical debt score (0), never a perfect 100.
- BUG-SIM-01 (P1): the Guyton-Klinger engine must be invoked by the FIRE UI
  panel and the real-return spinbox must accept negative rates ([-10, 15]).
- BUG-SIM-02 (P2): FIRE spinboxes must debounce recalculations behind a
  single-shot QTimer (250 ms) instead of recomputing synchronously per tick.
- BUG-SIM-03 (P1): the SRR engine must report 0% survival (immediate
  depletion) for zero/negative portfolios, regardless of withdrawal.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from database.engine import FinanceDB  # noqa: E402
from database.repo import Repo  # noqa: E402
from services.financial_health import FinancialHealthService  # noqa: E402
from services.fire import FirePlanningEngine  # noqa: E402


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.delenv("FINANCAS_PESSOAIS_DEMO_DATA", raising=False)
    db = FinanceDB(tmp_path / "batch21.sqlite3")
    yield Repo(db)
    try:
        db.conn.close()
    except sqlite3.Error:
        pass


def _seed_three_complete_months(repo: Repo, salary: float, expenses: float) -> None:
    """Five complete historical months (Apr–Aug 2026), 3 income + 3 expense
    movements per month, all before the Sep 2026 reference date."""
    for month in (4, 5, 6, 7, 8):
        for day in (5, 15, 25):
            repo.add_transaction(
                f"2026-{month:02d}-{day:02d}",
                "Salário",
                salary / 3.0,
                "Rendimento",
                "Conta Principal",
            )
            repo.add_transaction(
                f"2026-{month:02d}-{day:02d}",
                "Despesa fixa",
                -expenses / 3.0,
                "Casa",
                "Conta Principal",
            )


def _seed_reference_account(repo: Repo, balance: float) -> None:
    repo.db.conn.execute(
        "UPDATE financial_accounts SET reference_balance=?,reference_date=?,"
        "balance_known=1,is_active=1 WHERE name='Conta Principal'",
        (balance, "2026-09-01"),
    )
    repo.db.conn.commit()


# ---------------------------------------------------------------------------
# BUG-RISK-01: full 0-100% income shock range (engine + UI slider)
# ---------------------------------------------------------------------------


class TestBugRisk01FullShockRange:
    def test_engine_accepts_total_income_loss(self, repo: Repo) -> None:
        """100% reduction must be applied verbatim: stressed income is 0 and
        the reported income_reduction_pct equals 100.0 (old clamp: 95.0)."""
        _seed_three_complete_months(repo, salary=3000.0, expenses=2000.0)
        _seed_reference_account(repo, 9000.0)
        service = FinancialHealthService(repo, MagicMock())
        result = service.risk_resilience(
            as_of=date(2026, 9, 4),
            horizon_months=12,
            income_reduction_pct=100.0,
            income_reduction_months=3,
            emergency_expense=0.0,
        )
        assert result["income_reduction_pct"] == 100.0
        # Stressed income = average_income * (1 - 1.0) = 0.
        stressed_deficit = result["average_expenses"] * 1.05
        # Month 1 deterministic stressed balance: 9000 - 2000.01*1.05.
        assert result["rows"][1]["stressed"] == pytest.approx(
            9000.0 - stressed_deficit, abs=1e-6
        )

    def test_engine_rejects_above_range(self, repo: Repo) -> None:
        """Out-of-range values are still clamped to the [0, 100] window."""
        _seed_three_complete_months(repo, salary=3000.0, expenses=2000.0)
        _seed_reference_account(repo, 9000.0)
        service = FinancialHealthService(repo, MagicMock())
        result = service.risk_resilience(
            as_of=date(2026, 9, 4),
            income_reduction_pct=250.0,
        )
        assert result["income_reduction_pct"] == 100.0
        result_low = service.risk_resilience(
            as_of=date(2026, 9, 4),
            income_reduction_pct=-30.0,
        )
        assert result_low["income_reduction_pct"] == 0.0

    def test_severe_sensitivity_variant_never_milder_than_current(
        self, repo: Repo
    ) -> None:
        """The 'Mais severo' sensitivity row must cut at least as much income
        as the current scenario even at 100% (old cap: min(0.95, ...))."""
        _seed_three_complete_months(repo, salary=3000.0, expenses=2000.0)
        _seed_reference_account(repo, 9000.0)
        service = FinancialHealthService(repo, MagicMock())
        result = service.risk_resilience(
            as_of=date(2026, 9, 4),
            horizon_months=12,
            income_reduction_pct=100.0,
            income_reduction_months=3,
            emergency_expense=0.0,
        )
        sensitivity = {row["label"]: row for row in result["sensitivity"]}
        assert sensitivity["Cenário atual"]["income_reduction_pct"] == 100.0
        assert (
            sensitivity["Mais severo"]["income_reduction_pct"] == 100.0
        )
        assert (
            sensitivity["Mais severo"]["minimum_cash"]
            <= sensitivity["Cenário atual"]["minimum_cash"]
        )

    def test_ui_slider_spans_full_range(self) -> None:
        """Static source guard: the income-cut slider must span 0..100."""
        source = (ROOT / "ui" / "pages" / "risk_resilience.py").read_text(
            encoding="utf-8"
        )
        assert "self.income_cut_slider.setRange(0, 100)" in source
        assert "self.income_cut_slider.setRange(0, 80)" not in source


# ---------------------------------------------------------------------------
# BUG-RISK-02: runway absorbs a post-emergency cash hole
# ---------------------------------------------------------------------------


class TestBugRisk02RunwayAbsorption:
    def test_negative_post_emergency_cash_with_positive_flow_is_finite(
        self, repo: Repo
    ) -> None:
        """Reserves 5000, emergency 8000, shock deficit ~0.01/month for 6
        months, base surplus ~1000/month: the 3000.11 hole is absorbed in
        ~3.0 months after the shock ends => runway = 6 + 3000.11/999.99,
        never the old unconditional 0.0."""
        _seed_three_complete_months(repo, salary=3000.0, expenses=2000.0)
        _seed_reference_account(repo, 5000.0)
        service = FinancialHealthService(repo, MagicMock())
        result = service.risk_resilience(
            as_of=date(2026, 9, 4),
            horizon_months=12,
            income_reduction_pct=30.0,
            income_reduction_months=6,
            emergency_expense=8000.0,
        )
        assert result["score_available"] is True
        shock_burn = result["average_expenses"] * 1.05 - result[
            "average_income"
        ] * 0.7
        hole = (8000.0 - result["starting_cash"]) + shock_burn * 6
        base_surplus = result["average_income"] - result["average_expenses"]
        expected = 6 + hole / base_surplus
        assert result["runway_months"] == pytest.approx(expected, abs=1e-6)
        assert result["runway_months"] > 0.0

    def test_structural_deficit_with_negative_start_stays_zero(
        self, repo: Repo
    ) -> None:
        """Cash negative after the emergency AND a structural deficit
        (expenses permanently above income) => immediate depletion, 0 months."""
        # Income 1000, expenses 2000: base_surplus = -1000 (structural).
        _seed_three_complete_months(repo, salary=1000.0, expenses=2000.0)
        _seed_reference_account(repo, 1000.0)
        service = FinancialHealthService(repo, MagicMock())
        result = service.risk_resilience(
            as_of=date(2026, 9, 4),
            horizon_months=12,
            income_reduction_pct=30.0,
            income_reduction_months=6,
            emergency_expense=3000.0,
        )
        assert result["runway_months"] == 0.0

    def test_positive_flow_absorbs_hole_without_shock_burn(
        self, repo: Repo
    ) -> None:
        """With no shock (reduction 0), a negative post-emergency position is
        absorbed at the deterministic monthly flow rate. The stress model
        applies the 5% expense inflation in every month, so the absorption
        rate is the stressed surplus (income - expenses*1.05)."""
        _seed_three_complete_months(repo, salary=3000.0, expenses=2000.0)
        _seed_reference_account(repo, 1000.0)
        service = FinancialHealthService(repo, MagicMock())
        result = service.risk_resilience(
            as_of=date(2026, 9, 4),
            horizon_months=12,
            income_reduction_pct=0.0,
            income_reduction_months=1,
            emergency_expense=4000.0,
        )
        hole = 4000.0 - result["starting_cash"]
        stressed_surplus = result["average_income"] - result[
            "average_expenses"
        ] * 1.05
        expected = hole / stressed_surplus
        assert result["runway_months"] == pytest.approx(expected, abs=1e-6)

    def test_batch18_temporary_shock_contract_preserved(self, repo: Repo) -> None:
        """Regression guard for BUG-CASH-04: ample reserves + temporary shock
        + positive base surplus still yields the full 24-month horizon."""
        _seed_three_complete_months(repo, salary=3000.0, expenses=2000.0)
        _seed_reference_account(repo, 9000.0)
        service = FinancialHealthService(repo, MagicMock())
        result = service.risk_resilience(
            as_of=date(2026, 9, 4),
            horizon_months=12,
            income_reduction_pct=100.0,
            income_reduction_months=3,
            emergency_expense=0.0,
        )
        assert result["runway_months"] == 24.0

    def test_batch18_structural_deficit_contract_preserved(self, repo: Repo) -> None:
        """Regression guard for BUG-CASH-04: a structural deficit burning
        through reserves yields the finite two-phase runway."""
        _seed_three_complete_months(repo, salary=3000.0, expenses=2000.0)
        _seed_reference_account(repo, 6000.0)
        service = FinancialHealthService(repo, MagicMock())
        result = service.risk_resilience(
            as_of=date(2026, 9, 4),
            horizon_months=24,
            income_reduction_pct=100.0,
            income_reduction_months=24,
            emergency_expense=0.0,
        )
        # BUG-RISK-01: 100% is honoured, so the structural deficit is the
        # full stressed expenses (income contribution is exactly zero).
        stressed_deficit = result["average_expenses"] * 1.05
        expected = 6000.0 / stressed_deficit
        assert abs(result["runway_months"] - expected) < 1e-6
        assert result["runway_months"] < 24.0


# ---------------------------------------------------------------------------
# BUG-RISK-03: zero income with debt => critical score, not perfect
# ---------------------------------------------------------------------------


class TestBugRisk03ZeroIncomeDebtScore:
    def _seed_expense_only_history(self, repo: Repo) -> None:
        for month in (4, 5, 6, 7, 8):
            for day in (5, 15, 25):
                repo.add_transaction(
                    f"2026-{month:02d}-{day:02d}",
                    "Despesa fixa",
                    -666.66,
                    "Casa",
                    "Conta Principal",
                )

    def test_zero_income_with_debt_payment_scores_zero(self, repo: Repo) -> None:
        """Income 0 + monthly payment 300 => debt_service_ratio 100 and the
        debt component scores 0 (old behaviour: ratio 0.0, score 100)."""
        self._seed_expense_only_history(repo)
        repo.add_liability(
            "Crédito Pessoal", "Crédito", "Conta Principal", 5000.0, 5.0, 300.0
        )
        # Supply the historical evidence required by this retrospective query.
        repo.record_financial_state_snapshot("2026-08-31")
        service = FinancialHealthService(repo, MagicMock())
        result = service.risk_resilience(as_of=date(2026, 9, 4), horizon_months=12)
        assert result["debt_service_ratio"] == 100.0
        debt_component = next(
            c for c in result["components"] if c["key"] == "debt"
        )
        assert debt_component["score"] == 0.0

    def test_zero_income_without_debt_keeps_neutral_ratio(self, repo: Repo) -> None:
        """Income 0 and no debt payments => ratio 0 (nothing to service);
        the score must not be worse than the neutral data path."""
        self._seed_expense_only_history(repo)
        repo.record_financial_state_snapshot("2026-08-31")
        service = FinancialHealthService(repo, MagicMock())
        result = service.risk_resilience(as_of=date(2026, 9, 4), horizon_months=12)
        assert result["debt_service_ratio"] == 0.0

    def test_positive_income_ratio_unchanged(self, repo: Repo) -> None:
        """Normal path is untouched: 400 payment / 3000 income => 13.33%."""
        _seed_three_complete_months(repo, salary=3000.0, expenses=2000.0)
        repo.add_liability(
            "Crédito", "Crédito", "Conta Principal", 8000.0, 4.0, 400.0
        )
        repo.record_financial_state_snapshot("2026-08-31")
        service = FinancialHealthService(repo, MagicMock())
        result = service.risk_resilience(as_of=date(2026, 9, 4), horizon_months=12)
        assert result["debt_service_ratio"] == pytest.approx(
            400.0 / 3000.0 * 100.0, abs=1e-9
        )


# ---------------------------------------------------------------------------
# BUG-SIM-01: GK wired into the UI + negative real returns accepted
# ---------------------------------------------------------------------------


class TestBugSim01GkWiringAndNegativeReturns:
    def test_fire_panel_source_invokes_guyton_klinger(self) -> None:
        """Static source guard: _update_fire_calculations must call the GK
        engine (it previously only rendered static rule text)."""
        source = (ROOT / "ui" / "pages" / "simulations.py").read_text(
            encoding="utf-8"
        )
        start = source.index("def _update_fire_calculations")
        end = source.index("def _plan_count_changed")
        block = source[start:end]
        assert "simulate_guyton_klinger_drawdown" in block

    def test_gk_engine_supports_negative_real_returns(self) -> None:
        """The engine itself must handle negative returns without producing
        negative capital or NaN balances (stagflation path)."""
        history = FirePlanningEngine.simulate_guyton_klinger_drawdown(
            initial_capital=500_000.0,
            initial_annual_withdrawal=20_000.0,
            annual_returns=[-5.0] * 30,
            annual_inflation_rates=[2.0] * 30,
            initial_swr_pct=4.0,
        )
        assert len(history) == 30
        for row in history:
            assert row["ending_capital"] >= 0.0

    def test_fire_page_growth_spin_accepts_negative_rates(self, qapp, tmp_path) -> None:
        """The real-return spinbox must span [-10, 15] (stagflation)."""
        from ui.pages.simulations import HabitSimulationsPage

        mock_app = _mock_app(tmp_path)
        with patch.object(HabitSimulationsPage, "refresh", lambda self: None):
            page = HabitSimulationsPage(mock_app)
        assert page.fire_growth_spin.minimum() == -10.0
        assert page.fire_growth_spin.maximum() == 15.0
        page.fire_growth_spin.setValue(-3.0)
        assert page.fire_growth_spin.value() == -3.0

    def test_fire_page_renders_gk_simulation_result(self, qapp, tmp_path) -> None:
        """After a recalculation the GK panel must show the simulated outcome
        (survival/depletion), not only the static rule descriptions."""
        from ui.pages.simulations import HabitSimulationsPage

        mock_app = _mock_app(tmp_path)
        with patch.object(HabitSimulationsPage, "refresh", lambda self: None):
            page = HabitSimulationsPage(mock_app)
        page.fire_capital_spin.setValue(500_000.0)
        page.fire_expenses_spin.setValue(20_000.0)
        page._update_fire_calculations()
        text = page.fire_gk_label.text()
        assert "Simulação Guyton-Klinger" in text
        assert ("✅ Sobreviveu 30 anos" in text) or ("Esgotou" in text)


# ---------------------------------------------------------------------------
# BUG-SIM-02: debounced FIRE recalculations
# ---------------------------------------------------------------------------


class TestBugSim02FireDebounce:
    def test_spinboxes_do_not_recalculate_synchronously(self, qapp, tmp_path) -> None:
        """A spinbox change must NOT rebuild the milestone grid synchronously;
        it queues the 250 ms single-shot timer instead."""
        from ui.pages.simulations import HabitSimulationsPage

        mock_app = _mock_app(tmp_path)
        with patch.object(HabitSimulationsPage, "refresh", lambda self: None):
            page = HabitSimulationsPage(mock_app)
        assert page.fire_milestones_grid.count() == 0
        page.fire_capital_spin.setValue(123_456.0)
        assert page.fire_milestones_grid.count() == 0
        assert page._fire_recalc_timer.isActive()

    def test_timer_fires_exactly_one_recalculation(self, qapp, tmp_path) -> None:
        """Several rapid ticks coalesce into a single recalculation."""
        from ui.pages.simulations import HabitSimulationsPage

        mock_app = _mock_app(tmp_path)
        with patch.object(HabitSimulationsPage, "refresh", lambda self: None):
            page = HabitSimulationsPage(mock_app)
        calls: list[int] = []
        original = page._update_fire_calculations

        def counting() -> None:
            calls.append(1)
            original()

        page._update_fire_calculations = counting  # type: ignore[method-assign]
        page.fire_capital_spin.setValue(10_000.0)
        page.fire_capital_spin.setValue(20_000.0)
        page.fire_expenses_spin.setValue(19_000.0)
        QTest.qWait(400)
        assert len(calls) == 1
        assert page.fire_milestones_grid.count() == 4

    def test_direct_call_still_recalculates_immediately(
        self, qapp, tmp_path
    ) -> None:
        """Explicit _update_fire_calculations() stays synchronous (existing
        tests and programmatic refreshes rely on it)."""
        from ui.pages.simulations import HabitSimulationsPage

        mock_app = _mock_app(tmp_path)
        with patch.object(HabitSimulationsPage, "refresh", lambda self: None):
            page = HabitSimulationsPage(mock_app)
        page._update_fire_calculations()
        assert page.fire_milestones_grid.count() == 4
        assert not page._fire_recalc_timer.isActive()

    def test_debounce_interval_is_250ms(self, qapp, tmp_path) -> None:
        from ui.pages.simulations import HabitSimulationsPage

        mock_app = _mock_app(tmp_path)
        with patch.object(HabitSimulationsPage, "refresh", lambda self: None):
            page = HabitSimulationsPage(mock_app)
        assert page._fire_recalc_timer.interval() == 250
        assert page._fire_recalc_timer.isSingleShot()


# ---------------------------------------------------------------------------
# BUG-SIM-03: SRR zero/negative portfolio => 0% survival
# ---------------------------------------------------------------------------


class TestBugSim03SrrZeroCapital:
    @pytest.mark.parametrize("capital", [0.0, -1.0, -5_000.0])
    def test_zero_or_negative_capital_never_survives(self, capital: float) -> None:
        """A depleted portfolio reports survival 0% and immediate depletion,
        even with zero withdrawals (old behaviour: survived=True)."""
        for withdrawal in (0.0, 12_000.0):
            res = FirePlanningEngine.simulate_sequence_of_returns_risk(
                initial_capital=capital,
                initial_annual_withdrawal=withdrawal,
                duration_years=30,
                average_real_return_pct=5.0,
                early_crash_scenario="baseline_steady",
            )
            assert res.portfolio_survived is False
            assert res.depletion_year == 1
            assert res.final_capital_real == 0.0
            assert res.final_capital_nominal == 0.0
            assert res.yearly_balances == [0.0] * 31

    def test_positive_capital_unchanged(self) -> None:
        """Guard: a healthy portfolio still survives the baseline scenario."""
        res = FirePlanningEngine.simulate_sequence_of_returns_risk(
            initial_capital=1_000_000.0,
            initial_annual_withdrawal=30_000.0,
            duration_years=30,
            average_real_return_pct=5.0,
            early_crash_scenario="baseline_steady",
        )
        assert res.portfolio_survived is True
        assert res.depletion_year is None


# ---------------------------------------------------------------------------
# Shared UI test helpers
# ---------------------------------------------------------------------------


def _mock_app(tmp_path: Path) -> MagicMock:
    class DummyTheme:
        dark = True
        privacy = False
        muted = "#8b949e"
        text = "#c9d1d9"
        background = "#0d1117"
        card_bg = "#161b22"
        border = "#30363d"

    mock_app = MagicMock()
    mock_app.theme = DummyTheme()
    mock_app.page_density = "normal"
    mock_app.content_scroll.viewport().width.return_value = 1280
    mock_app.content_scroll.verticalScrollBar().value.return_value = 0
    mock_app.repo.db.path = tmp_path / "test.db"
    mock_app.repo.setting.side_effect = lambda k, default="": str(default)
    mock_app.repo.saved_filters.return_value = []
    mock_app.repo.monthly_snapshot_notes.return_value = ""
    mock_app.repo.month_period.return_value = ("2026-01-01", "2026-01-31", "2026-01")
    mock_app.repo.available_years.return_value = [2024, 2025, 2026]
    return mock_app


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


from PySide6.QtTest import QTest  # noqa: E402  (needed for qWait above)
