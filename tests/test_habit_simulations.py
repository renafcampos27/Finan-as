from __future__ import annotations

import pytest

from services.habit_simulations import (
    HABIT_PRESETS,
    HABIT_PRESETS_BY_KEY,
    HabitSimulationInputs,
    occurrences_per_month,
    simulate_habit,
)


def test_requested_habits_and_extra_scenarios_are_available() -> None:
    names = {preset.name for preset in HABIT_PRESETS}

    assert {
        "Casino",
        "Apostas",
        "Jantares fora",
        "Cervejas",
        "Cafés",
        "Lanches",
        "Doces",
        "Cantina",
        "Subscrições",
    }.issubset(names)
    assert len(HABIT_PRESETS) >= 12
    assert HABIT_PRESETS_BY_KEY["coffee"].alternative_name == "Café em casa"
    assert HABIT_PRESETS_BY_KEY["coffee"].alternative_unit_cost == pytest.approx(0.25)


def test_daily_habit_projects_all_requested_horizons() -> None:
    preset = HABIT_PRESETS_BY_KEY["coffee"]
    result = simulate_habit(
        preset,
        HabitSimulationInputs(
            unit_cost=1.0,
            frequency=1.0,
            frequency_unit="day",
            annual_inflation_pct=0.0,
            annual_investment_rate_pct=5.0,
            monthly_income=2_000.0,
        ),
    )

    assert [row["months"] for row in result["rows"]] == [1, 12, 24, 36, 60, 120, 360]
    assert result["monthly_cost"] == pytest.approx(365.2425 / 12.0)
    assert result["annual_cost"] == pytest.approx(365.2425)
    assert result["rows"][-1]["opportunity_value"] > result["rows"][-1]["cost_base"]
    assert 1.0 < result["income_share_pct"] < 2.0
    assert result["occurrence_income_share_pct"] == pytest.approx(0.05)
    assert result["annual_income_share_pct"] == pytest.approx(
        result["annual_cost"] / 24_000.0 * 100.0
    )


def test_default_projection_keeps_the_selected_price_constant() -> None:
    preset = HABIT_PRESETS_BY_KEY["subscriptions"]
    result = simulate_habit(
        preset,
        HabitSimulationInputs(
            unit_cost=12.0,
            frequency=1.0,
            frequency_unit="month",
            monthly_income=2_000.0,
        ),
    )

    assert result["monthly_cost"] == pytest.approx(12.0)
    assert result["annual_cost"] == pytest.approx(144.0)
    assert result["income_share_pct"] == pytest.approx(0.6)
    assert result["annual_income_share_pct"] == pytest.approx(0.6)


def test_monthly_series_compares_integer_reductions_and_real_alternative() -> None:
    preset = HABIT_PRESETS_BY_KEY["subscriptions"]
    result = simulate_habit(
        preset,
        HabitSimulationInputs(
            unit_cost=10.0,
            alternative_unit_cost=2.0,
            frequency=4.0,
            frequency_unit="month",
            monthly_income=2_000.0,
        ),
    )

    assert len(result["monthly_series"]) == 360
    assert [row["months"] for row in result["monthly_series"][:3]] == [1.0, 2.0, 3.0]
    assert result["scenario_frequencies"] == {
        "current": 4.0,
        "reduce_25": 3.0,
        "half": 2.0,
        "alternative": 0.0,
    }
    first = result["monthly_series"][0]
    assert first["cost_base"] == pytest.approx(40.0)
    assert first["cost_reduce_25"] == pytest.approx(32.0)
    assert first["cost_half"] == pytest.approx(24.0)
    assert first["cost_alternative"] == pytest.approx(8.0)
    assert first["savings_alternative"] == pytest.approx(32.0)
    assert result["quarter_reduction_monthly"] == pytest.approx(8.0)
    assert result["annual_savings_potential"] == pytest.approx(384.0)
    assert (
        result["rows"][-1]["opportunity_value"]
        > result["rows"][-1]["savings_alternative"]
    )


def test_alternative_cost_never_exceeds_current_cost() -> None:
    result = simulate_habit(
        HABIT_PRESETS_BY_KEY["coffee"],
        HabitSimulationInputs(
            unit_cost=1.0,
            alternative_unit_cost=5.0,
            frequency=1.0,
            frequency_unit="month",
        ),
    )

    assert result["monthly_alternative_cost"] == pytest.approx(1.0)
    assert result["monthly_savings_potential"] == pytest.approx(0.0)


def test_gambling_uses_probability_below_half_and_expected_value() -> None:
    preset = HABIT_PRESETS_BY_KEY["betting"]
    inputs = HabitSimulationInputs(
        unit_cost=10.0,
        frequency=2.0,
        frequency_unit="week",
        annual_inflation_pct=0.0,
        annual_investment_rate_pct=0.0,
        gambling=True,
        win_probability_pct=40.0,
        payout_multiplier=1.8,
    )
    result = simulate_habit(preset, inputs)
    gross_monthly = 10.0 * occurrences_per_month(2.0, "week")

    assert result["expected_return_ratio"] == pytest.approx(0.72)
    assert result["expected_loss_ratio"] == pytest.approx(0.28)
    assert result["monthly_cost"] == pytest.approx(gross_monthly * 0.28)
    assert result["rows"][0]["expected_return"] == pytest.approx(gross_monthly * 0.72)
    assert result["rows"][0]["cost_low"] < result["rows"][0]["cost_base"]
    assert result["rows"][0]["cost_high"] > result["rows"][0]["cost_base"]


def test_zero_frequency_produces_safe_zero_projection() -> None:
    preset = HABIT_PRESETS_BY_KEY["subscriptions"]
    result = simulate_habit(
        preset,
        HabitSimulationInputs(
            unit_cost=15.0,
            frequency=0.0,
            frequency_unit="month",
        ),
    )

    assert result["monthly_cost"] == 0.0
    assert result["thirty_year_opportunity"] == 0.0
    assert all(row["cost_base"] == 0.0 for row in result["rows"])
