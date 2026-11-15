from __future__ import annotations

from datetime import date

import pytest

from database.repo import Repo
from services.habit_personalization import (
    build_habit_audit_rows,
    build_habit_calibration,
)
from services.habit_plans import (
    build_habit_plan_payload,
    build_habit_plan_tracking,
    scenario_plan_values,
)
from services.habit_simulations import (
    HABIT_PRESETS_BY_KEY,
    HabitSimulationInputs,
    simulate_habit,
)


def _expense(
    identifier: int,
    description: str,
    amount: float,
    financial_date: str,
    category: str = "Diversos",
) -> dict[str, object]:
    return {
        "id": identifier,
        "description": description,
        "amount": amount,
        "financial_date": financial_date,
        "category": category,
        "subcategory": "",
        "merchant": "",
    }


@pytest.fixture
def repo(tmp_path, monkeypatch):
    import database.repo.base as repo_base

    monkeypatch.setattr(repo_base, "db_path", lambda: tmp_path / "plans.sqlite3")
    instance = Repo()
    yield instance
    instance.db.conn.close()


def test_auditor_can_exclude_automatic_and_include_manual_movements() -> None:
    rows = build_habit_audit_rows(
        HABIT_PRESETS_BY_KEY["coffee"],
        [
            _expense(1, "Café na pastelaria", 1.2, "2026-07-10"),
            _expense(2, "Máquina do escritório", 0.8, "2026-07-11"),
            _expense(3, "Supermercado", 42.0, "2026-07-12", "Supermercado"),
        ],
        {
            "included_transaction_ids": [2],
            "excluded_transaction_ids": [1],
        },
    )

    by_id = {row["id"]: row for row in rows}
    assert by_id[1]["selected"] is False
    assert by_id[1]["match_kind"] == "excluded"
    assert by_id[2]["selected"] is True
    assert by_id[2]["match_kind"] == "manual"
    assert by_id[3]["selected"] is False

    calibration = build_habit_calibration(
        HABIT_PRESETS_BY_KEY["coffee"],
        rows,
        period_start="2026-07-01",
        period_end="2026-07-31",
        coverage_months=1,
        audit_rules={
            "included_transaction_ids": [2],
            "excluded_transaction_ids": [1],
        },
    )
    assert calibration["transaction_count"] == 1
    assert calibration["average_unit_cost"] == pytest.approx(0.8)


def test_plan_payload_and_tracking_compare_prorated_plan_with_reality() -> None:
    simulation = simulate_habit(
        HABIT_PRESETS_BY_KEY["coffee"],
        HabitSimulationInputs(
            unit_cost=2.0,
            alternative_unit_cost=0.5,
            frequency=10,
            frequency_unit="month",
        ),
    )
    scenario = scenario_plan_values(simulation, "half")
    assert scenario["baseline_monthly_cost"] == pytest.approx(20.0)
    assert scenario["planned_monthly_cost"] == pytest.approx(12.5)
    payload = build_habit_plan_payload(
        preset_key="coffee",
        simulation=simulation,
        scenario_key="half",
        name="Reduzir cafés",
        start_date="2026-05-15",
        goal_id=None,
        audit_rules={},
    )
    plan = {
        "id": 7,
        "status": "active",
        "goal_name": "",
        **payload,
    }
    tracking = build_habit_plan_tracking(
        plan,
        [
            _expense(1, "Café", 3.0, "2026-05-20"),
            _expense(2, "Café", 10.0, "2026-06-02"),
            _expense(3, "Supermercado", 50.0, "2026-06-03", "Supermercado"),
        ],
        reference_date=date(2026, 6, 30),
    )

    assert len(tracking["rows"]) == 2
    assert tracking["planned_total"] == pytest.approx(12.5 * 17 / 31 + 12.5)
    assert tracking["actual_total"] == pytest.approx(13.0)
    assert tracking["transaction_count"] == 2
    assert tracking["status"] == "No caminho"
    assert tracking["actual_savings_total"] > 0


def test_saved_plan_repository_lifecycle(repo: Repo) -> None:
    simulation = simulate_habit(
        HABIT_PRESETS_BY_KEY["subscriptions"],
        HabitSimulationInputs(
            unit_cost=15.0,
            alternative_unit_cost=5.0,
            frequency=3,
            frequency_unit="month",
        ),
    )
    payload = build_habit_plan_payload(
        preset_key="subscriptions",
        simulation=simulation,
        scenario_key="alternative",
        name="Limpar subscrições",
        start_date="2026-07-19",
        goal_id=None,
        audit_rules={"keywords": ["streaming pessoal"]},
        notes="Rever no fim do mês",
    )
    plan_id = repo.add_habit_change_plan(**payload)

    saved = dict(repo.habit_change_plan_by_id(plan_id))
    assert saved["name"] == "Limpar subscrições"
    assert saved["scenario_key"] == "alternative"
    assert "streaming pessoal" in saved["audit_rules_json"]
    assert len(repo.habit_change_plans(active_only=True)) == 1

    repo.set_habit_change_plan_status(plan_id, "paused")
    assert dict(repo.habit_change_plan_by_id(plan_id))["status"] == "paused"
    assert repo.habit_change_plans(active_only=True) == []

    repo.delete_habit_change_plan(plan_id)
    assert repo.habit_change_plan_by_id(plan_id) is None
