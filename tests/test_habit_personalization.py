from __future__ import annotations

from datetime import date

import pytest

from services.habit_personalization import (
    build_goal_impact,
    build_habit_calibration,
    habit_match_kind,
)
from services.habit_simulations import (
    HABIT_PRESETS_BY_KEY,
    HabitSimulationInputs,
    simulate_habit,
)


def _expense(
    description: str,
    amount: float,
    financial_date: str,
    category: str = "Diversos",
) -> dict[str, object]:
    return {
        "description": description,
        "amount": amount,
        "financial_date": financial_date,
        "category": category,
        "subcategory": "",
        "merchant": "",
    }


def test_habit_matching_is_accent_insensitive_and_respects_exclusions() -> None:
    cafe = _expense("Café depois do almoço", 1.2, "2026-07-01")
    delivery = _expense("Uber Eats", 18.0, "2026-07-02")

    assert habit_match_kind("coffee", cafe) == "keyword"
    assert habit_match_kind("delivery", delivery) == "keyword"
    assert habit_match_kind("ride_hailing", delivery) == ""


def test_calibration_exposes_sample_quality_and_integer_suggestion() -> None:
    rows = [
        _expense(
            "Café habitual",
            1.0 + (index % 3) * 0.25,
            f"2026-{(index % 4) + 1:02d}-{(index % 24) + 1:02d}",
        )
        for index in range(16)
    ]
    result = build_habit_calibration(
        HABIT_PRESETS_BY_KEY["coffee"],
        rows,
        period_start="2026-01-01",
        period_end="2026-04-30",
        coverage_months=4,
    )

    assert result["available"] is True
    assert result["transaction_count"] == 16
    assert result["active_months"] == 4
    assert result["confidence"] == "high"
    assert result["average_unit_cost"] == pytest.approx(1.234375)
    assert isinstance(result["suggested_frequency"], int)
    assert result["suggested_frequency"] >= 1
    assert result["suggested_frequency_unit"] in {"day", "week", "month"}


def test_calibration_with_no_matches_keeps_manual_scenario_available() -> None:
    result = build_habit_calibration(
        HABIT_PRESETS_BY_KEY["betting"],
        [_expense("Supermercado", 45.0, "2026-07-01", "Supermercado")],
        period_start="2026-01-01",
        period_end="2026-07-01",
        coverage_months=6,
    )

    assert result["available"] is False
    assert result["transaction_count"] == 0
    assert result["confidence"] == "none"


def test_goal_impact_compares_all_savings_scenarios() -> None:
    simulation = simulate_habit(
        HABIT_PRESETS_BY_KEY["subscriptions"],
        HabitSimulationInputs(
            unit_cost=25.0,
            alternative_unit_cost=5.0,
            frequency=4,
            frequency_unit="month",
        ),
    )
    result = build_goal_impact(
        {
            "id": 7,
            "name": "Fundo de emergência",
            "target_amount": 1_200.0,
            "saved_amount": 0.0,
            "monthly_contribution": 100.0,
        },
        simulation,
        reference_date=date(2026, 7, 19),
    )

    scenarios = result["scenarios"]
    assert result["baseline_months"] == 12
    assert scenarios["current"]["months"] == 12
    assert scenarios["reduce_25"]["months"] < 12
    assert scenarios["half"]["months"] < scenarios["reduce_25"]["months"]
    assert scenarios["alternative"]["months"] <= scenarios["half"]["months"]
    assert scenarios["eliminate"]["months_gained"] > 0
    assert scenarios["alternative"]["completion_date"].startswith("2027-")


def test_goal_without_contribution_gets_a_plan_from_savings() -> None:
    simulation = simulate_habit(
        HABIT_PRESETS_BY_KEY["coffee"],
        HabitSimulationInputs(
            unit_cost=2.0,
            alternative_unit_cost=0.25,
            frequency=7,
            frequency_unit="week",
        ),
    )
    result = build_goal_impact(
        {
            "id": 1,
            "name": "Viagem",
            "target_amount": 2_000.0,
            "saved_amount": 200.0,
            "monthly_contribution": 0.0,
        },
        simulation,
        reference_date=date(2026, 7, 19),
    )

    assert result["scenarios"]["current"]["months"] is None
    assert result["scenarios"]["alternative"]["creates_plan"] is True
    assert result["scenarios"]["alternative"]["months"] is not None
