from __future__ import annotations

import pytest

from services.dashboard_metrics import (
    emergency_reserve_snapshot,
    expense_work_time_snapshot,
    work_value_snapshot,
)


def test_emergency_reserve_prefers_explicit_goal_and_reports_surplus() -> None:
    snapshot = emergency_reserve_snapshot(
        [
            {
                "id": 7,
                "name": "Fundo de emergência",
                "saved_amount": 7_500,
                "target_amount": 6_000,
                "notes": "",
                "goal_type": "emergency_fund",
            }
        ],
        cash_balance=12_000,
        monthly_expense=1_000,
    )

    assert snapshot["source"] == "goal"
    assert snapshot["goal_id"] == 7
    assert snapshot["current"] == 7_500
    assert snapshot["target"] == 6_000
    assert snapshot["above"] == 1_500
    assert snapshot["missing"] == 0
    assert snapshot["status_key"] == "above"


def test_emergency_reserve_does_not_infer_type_from_free_form_notes() -> None:
    snapshot = emergency_reserve_snapshot(
        [
            {
                "id": 8,
                "name": "Comprar casa",
                "saved_amount": 42_000,
                "target_amount": 160_000,
                "notes": "Entrada, obras e fundo de emergência",
                "goal_type": "standard",
            }
        ],
        cash_balance=3_000,
        monthly_expense=1_000,
    )

    assert snapshot["source"] == "automatic"
    assert snapshot["goal_id"] is None
    assert snapshot["current"] == 3_000
    assert snapshot["target"] == 6_000


def test_emergency_reserve_uses_six_month_liquid_reference_without_goal() -> None:
    snapshot = emergency_reserve_snapshot(
        [],
        cash_balance=2_400,
        monthly_expense=800,
    )

    assert snapshot["source"] == "automatic"
    assert snapshot["current"] == 2_400
    assert snapshot["target"] == 4_800
    assert snapshot["missing"] == 2_400
    assert snapshot["coverage_months"] == 3
    assert snapshot["status_key"] == "base"


def test_work_value_uses_net_monthly_income_and_configurable_weekly_hours() -> None:
    snapshot = work_value_snapshot(2_750, weekly_hours=40)

    assert snapshot["available"] is True
    assert snapshot["monthly_hours"] == pytest.approx(173.333333)
    assert snapshot["hourly_value"] == pytest.approx(15.8653846)
    assert snapshot["daily_value"] == pytest.approx(126.9230769)


def test_expense_work_time_keeps_every_category_and_totals_match() -> None:
    snapshot = expense_work_time_snapshot(
        [
            ("Casa", 1_000, "#0A84FF"),
            ("Café", 25, "#FF9F0A"),
            ("Casa", 200, "#0A84FF"),
            ("Transportes", 160, "#30D158"),
        ],
        hourly_value=20,
        daily_hours=8,
    )

    assert [row["category"] for row in snapshot["rows"]] == [
        "Casa",
        "Transportes",
        "Café",
    ]
    assert snapshot["total_amount"] == 1_385
    assert snapshot["total_hours"] == pytest.approx(69.25)
    assert snapshot["total_days"] == pytest.approx(8.65625)
    assert snapshot["rows"][0]["hours"] == 60
