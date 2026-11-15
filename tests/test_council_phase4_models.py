"""A10: explicit units, chronology and guardrail variant contracts."""

import math
from unittest.mock import MagicMock

import pytest

from services.fire import FirePlanningEngine
from services.financial_intelligence import monte_carlo_projection
from services.investment_projection import build_investment_projection


def _gk(returns, inflation=None, **overrides):
    values = dict(
        initial_capital=100_000,
        initial_annual_withdrawal=4_000,
        annual_returns=returns,
        annual_inflation_rates=inflation or [0] * len(returns),
        min_floor_pct_of_initial=0,
    )
    values.update(overrides)
    return FirePlanningEngine.simulate_guyton_klinger_drawdown(**values)


def test_capital_preservation_expires_only_in_final_fifteen_years():
    result = _gk([-50] + [0] * 29)
    assert any("Preservação" in row["rule_triggered"] for row in result[:15])
    assert all("Preservação" not in row["rule_triggered"] for row in result[15:])
    assert result[14]["capital_preservation_active"] is True
    assert result[15]["capital_preservation_active"] is False


def test_prosperity_remains_active_during_final_fifteen_years():
    result = _gk([100] * 30)
    assert all("Prosperidade" in row["rule_triggered"] for row in result[15:])


def test_withdrawal_uses_prior_year_inflation_not_future_current_year():
    result = _gk([10, 0], [7, 50])
    assert result[0]["withdrawal"] == 4_000
    assert result[1]["withdrawal"] == pytest.approx(4_280)
    assert result[1]["applied_inflation_pct"] == pytest.approx(7)


def test_negative_prior_return_does_not_freeze_below_initial_rate():
    result = _gk([100, -1, 0], [10, 5, 0])
    assert result[2]["applied_inflation_pct"] == 5
    assert "Congelada" not in result[2]["inflation_status"]


def test_negative_prior_return_freezes_only_a_proposed_increase():
    result = _gk([-10, 0], [5, 50])
    assert result[1]["withdrawal"] == 4_000
    assert result[1]["applied_inflation_pct"] == 0
    assert "Congelada" in result[1]["inflation_status"]


def test_actual_initial_rate_is_stable_anchor_and_conflicts_are_rejected():
    result = _gk([0, 0], initial_annual_withdrawal=5_000)
    assert result[0]["initial_withdrawal_rate_pct"] == 5
    assert result[1]["initial_withdrawal_rate_pct"] == 5
    with pytest.raises(ValueError, match="inicial"):
        _gk([0, 0], initial_annual_withdrawal=5_000, initial_swr_pct=4)


def test_floor_uses_initial_purchasing_power_without_advancing_first_year():
    result = _gk([0, 0], [10, 20], min_floor_pct_of_initial=0.8)
    assert result[0]["withdrawal_floor_nominal"] == 3_200
    assert result[1]["withdrawal_floor_nominal"] == pytest.approx(3_520)


def test_mismatched_return_and_inflation_sequences_do_not_silently_truncate():
    with pytest.raises(ValueError, match="mesmo"):
        _gk([1, 2, 3], [2])


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_models_reject_nonfinite_assumptions(bad):
    with pytest.raises(ValueError):
        _gk([bad])
    with pytest.raises(ValueError):
        FirePlanningEngine.simulate_sequence_of_returns_risk(
            initial_capital=100_000,
            initial_annual_withdrawal=4_000,
            average_real_return_pct=bad,
        )
    with pytest.raises(ValueError):
        monte_carlo_projection(100, annual_return_pct=bad)
    with pytest.raises(ValueError):
        build_investment_projection(100, annual_rate_pct=bad)


def test_srr_uses_real_shocks_and_only_terminal_nominal_conversion():
    values = dict(
        initial_capital=500_000,
        initial_annual_withdrawal=20_000,
        duration_years=10,
        average_real_return_pct=5,
        early_crash_scenario="dotcom_2000",
    )
    zero = FirePlanningEngine.simulate_sequence_of_returns_risk(
        **values, annual_inflation_pct=0
    )
    inflated = FirePlanningEngine.simulate_sequence_of_returns_risk(
        **values, annual_inflation_pct=5
    )
    assert zero.final_capital_real == inflated.final_capital_real
    assert zero.yearly_balances == inflated.yearly_balances
    assert inflated.final_capital_nominal == pytest.approx(
        inflated.final_capital_real * 1.05**10, abs=0.02
    )
    assert inflated.return_basis == "real"
    assert inflated.scenario_source == "illustrative_real_stress"


@pytest.mark.parametrize("mode", ["single", "monthly"])
def test_zero_volatility_matches_deterministic_at_every_horizon(mode):
    options = dict(
        mode=mode, initial_capital=5_000, inflation_pct=2.5, tax_pct=28, fee_pct=1
    )
    mc = monte_carlo_projection(300, 6, 0, 15, simulations=100, **options)
    deterministic = build_investment_projection(300, 6, 15, **options)
    for stochastic, fixed in zip(mc["rows"], deterministic["rows"], strict=True):
        assert stochastic["p50_nominal"] == pytest.approx(
            fixed["projected_nominal"], rel=1e-11
        )
        assert stochastic["p50_real"] == pytest.approx(
            fixed["projected_real"], rel=1e-11
        )
    assert mc["tax_timing"] == "hypothetical_liquidation_each_horizon"
    assert mc["tax_reduces_reinvested_state"] is False


def test_srr_honors_short_requested_horizon_without_silent_five_year_extension():
    result = FirePlanningEngine.simulate_sequence_of_returns_risk(
        initial_capital=100_000,
        initial_annual_withdrawal=4_000,
        duration_years=1,
        early_crash_scenario="baseline_steady",
        average_real_return_pct=0,
    )
    assert result.duration_years == 1 and result.yearly_balances == [100_000, 96_000]


def test_frozen_inflation_is_not_made_up_in_a_later_positive_year():
    result = _gk([-10, 10, 0], [5, 20, 0])
    assert result[1]["withdrawal"] == 4_000
    assert result[2]["withdrawal"] == pytest.approx(4_800)


def test_inflation_above_six_percent_is_not_capped_in_the_chosen_variant():
    result = _gk([0, 0], [10, 0])
    assert result[1]["applied_inflation_pct"] == 10
    assert result[1]["withdrawal"] == pytest.approx(4_400)


def test_sensitivity_axis_changes_initial_cash_instead_of_a_false_anchor():
    result = FirePlanningEngine.simulate_guyton_klinger_sensitivity_grid(
        initial_capital=100_000,
        initial_annual_withdrawal=4_000,
        annual_returns=[0] * 30,
        annual_inflation_rates=[0] * 30,
        swr_range_pct=[3, 5],
    )
    assert result[3][0]["withdrawal"] == 3_000
    assert result[5][0]["withdrawal"] == 5_000
    assert result[3][0]["initial_withdrawal_rate_pct"] == 3
    assert result[5][0]["initial_withdrawal_rate_pct"] == 5


def test_depletion_in_final_return_is_recorded_in_that_year():
    result = FirePlanningEngine.simulate_sequence_of_returns_risk(
        initial_capital=100_000,
        initial_annual_withdrawal=0,
        duration_years=1,
        early_crash_scenario="baseline_steady",
        average_real_return_pct=-100,
    )
    assert not result.portfolio_survived and result.depletion_year == 1
    assert result.yearly_balances == [100_000, 0]


@pytest.mark.parametrize(
    "field,value",
    [
        ("annual_inflation_pct", -100),
        ("average_real_return_pct", -101),
        ("duration_years", 1.5),
        ("early_crash_scenario", "invented"),
    ],
)
def test_srr_rejects_invalid_units_and_unidentified_scenario(field, value):
    with pytest.raises(ValueError):
        FirePlanningEngine.simulate_sequence_of_returns_risk(
            initial_capital=100_000,
            initial_annual_withdrawal=4_000,
            **{field: value},
        )


def test_gk_deflation_is_not_frozen_after_a_negative_return():
    result = _gk([-10, 0], [-5, 0])
    assert result[1]["withdrawal"] == pytest.approx(3_800)
    assert result[1]["applied_inflation_pct"] == -5


def test_fire_ui_converts_real_to_nominal_once_and_uses_actual_anchor(
    tmp_path, monkeypatch
):
    from PySide6.QtCore import QCoreApplication, QEvent
    from PySide6.QtWidgets import QApplication
    from ui.core.theme import Theme
    from ui.pages.simulations import HabitSimulationsPage

    app = QApplication.instance() or QApplication([])
    owner = MagicMock()
    owner.theme = Theme()
    owner.page_density = "normal"
    owner.content_scroll.viewport().width.return_value = 1280
    owner.repo.db.path = tmp_path / "unused.sqlite3"
    owner.repo.setting.side_effect = lambda key, default="": str(default)
    monkeypatch.setattr(HabitSimulationsPage, "refresh", lambda self: None)
    real_gk = FirePlanningEngine.simulate_guyton_klinger_drawdown
    observed = []

    def capture(**values):
        observed.append(values)
        return real_gk(**values)

    monkeypatch.setattr(FirePlanningEngine, "simulate_guyton_klinger_drawdown", capture)
    page = HabitSimulationsPage(owner)
    try:
        page.fire_capital_spin.setValue(500_000)
        page.fire_expenses_spin.setValue(20_000)
        page.fire_growth_spin.setValue(5)
        page.fire_swr_spin.setValue(
            8
        )  # Target SWR is deliberately not the actual 4% anchor.
        page._update_fire_calculations()
        assert observed[-1]["annual_returns"][0] == pytest.approx(7.1)
        assert "initial_swr_pct" not in observed[-1]
        assert "nominal" in page.fire_gk_label.text()
        assert "-10%, -12%, -22%" in page.fire_srr_label.text()
        assert "reais ilustrativos" in page.fire_srr_label.text()
    finally:
        page._fire_recalc_timer.stop()
        page.close()
        page.deleteLater()
        QCoreApplication.sendPostedEvents(page, QEvent.DeferredDelete)
        app.processEvents()


def test_page_destruction_cancels_queued_content_height_callbacks(
    tmp_path, monkeypatch
):
    from PySide6.QtCore import QCoreApplication, QEvent
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    from shiboken6 import isValid
    from ui.core.theme import Theme
    from ui.pages.simulations import HabitSimulationsPage

    app = QApplication.instance() or QApplication([])
    owner = MagicMock()
    owner.theme = Theme()
    owner.page_density = "normal"
    owner.content_scroll.viewport().width.return_value = 1280
    owner.repo.db.path = tmp_path / "unused.sqlite3"
    owner.repo.setting.side_effect = lambda key, default="": str(default)
    monkeypatch.setattr(HabitSimulationsPage, "refresh", lambda self: None)
    calls = []
    monkeypatch.setattr(
        HabitSimulationsPage,
        "_sync_content_height",
        lambda self: calls.append(isValid(self)),
    )
    page = HabitSimulationsPage(owner)
    page._fire_recalc_timer.stop()
    page.close()
    page.deleteLater()
    QCoreApplication.sendPostedEvents(page, QEvent.DeferredDelete)
    assert not isValid(page)
    # Deliver past the delayed 80ms boundary after destruction, not before it.
    QTest.qWait(100)
    app.processEvents()
    assert calls == []


def test_large_finite_weights_normalize_without_overflowing_the_sum():
    result = monte_carlo_projection(
        100, 5, 0, 1, simulations=100, asset_weights=[1e308, 1e308]
    )
    assert result["assumptions"]["weights"] == [0.5, 0.5]


def test_projection_state_overflow_is_rejected_instead_of_returning_nan():
    with pytest.raises(ValueError):
        monte_carlo_projection(1e308, 5, 0, 5, simulations=100)
    with pytest.raises(ValueError):
        build_investment_projection(1e308, 5, 5)
    with pytest.raises(ValueError):
        build_investment_projection(0, 0, 50, initial_capital=1e308, inflation_pct=-20)


@pytest.mark.parametrize(
    "field", ["target", "fee_pct", "seed", "years", "history_points"]
)
def test_monte_carlo_rejects_nonfinite_auxiliary_inputs(field):
    with pytest.raises(ValueError):
        monte_carlo_projection(100, **{field: math.nan})


@pytest.mark.parametrize(
    "field",
    [
        "current_invested",
        "annual_expenses",
        "annual_savings",
        "real_growth_rate_pct",
        "swr_pct",
        "years_to_retirement",
    ],
)
def test_milestones_do_not_replace_unknown_assumptions_with_zero(field):
    values = {
        "current_invested": 100_000,
        "annual_expenses": 20_000,
        "annual_savings": 5_000,
        field: math.nan,
    }
    with pytest.raises(ValueError):
        FirePlanningEngine.calculate_fire_milestones(**values)


def test_sensitivity_rejects_nonfinite_reference_spending():
    with pytest.raises(ValueError):
        FirePlanningEngine.simulate_guyton_klinger_sensitivity_grid(
            initial_capital=100_000,
            initial_annual_withdrawal=math.inf,
            annual_returns=[0] * 30,
            annual_inflation_rates=[0] * 30,
        )
