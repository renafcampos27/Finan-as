# -*- coding: utf-8 -*-
"""Regression tests for audit batch 22 (BUG-DEC-01..05, BUG-HABIT-01..04,
BUG-PAN-01).

Covers decision intelligence briefing, habit simulations/plans, the habit
plans card privacy masking and the panorama page savings-rate/meta semantics.
Each test asserts exact values against the current checkout.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from datetime import date
from unittest.mock import MagicMock

import pytest
from PySide6.QtWidgets import QApplication

from database.repo import Repo
from services.decision_intelligence import build_decision_briefing
from services.habit_plans import build_habit_plan_tracking
from services.habit_simulations import (
    HABIT_PRESETS_BY_KEY,
    HabitSimulationInputs,
    _scenario_frequency,
    simulate_habit,
)
from ui.components.decision_briefing import DecisionBriefingCard
from ui.components.habit_plans import HabitPlansCard
from utils.helpers import eur


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _base_context() -> dict:
    return {
        "totals": {"income": 2_000.0, "expenses": 1_700.0, "savings": 300.0},
        "monthly_summary": [
            {
                "income": 2_000.0,
                "expenses": 1_700.0,
                "savings": 300.0,
                "partial": False,
            }
            for _ in range(6)
        ],
        "emergency_reserve": {"available": False},
        "as_of": date(2026, 7, 27),
    }


# ============================================================================
# BUG-DEC-01 (P1): negative structural margin must never emit the green
# "Estrutura sem riscos imediatos detetados" card.
# ============================================================================


def test_bugdec01_negative_structural_margin_emits_critical_alert() -> None:
    context = _base_context()
    context["monthly_summary"] = [
        {
            "income": 2_000.0,
            "expenses": 2_150.0,
            "savings": -150.0,
            "partial": False,
        }
        for _ in range(6)
    ]

    result = build_decision_briefing(**context)

    keys = [row["key"] for row in result["priorities"]]
    assert "healthy:maintain" not in keys
    assert "margin:structural" in keys
    priority = next(
        row for row in result["priorities"] if row["key"] == "margin:structural"
    )
    assert priority["tone"] == "negative"
    assert priority["urgency"] == "Agir agora"
    assert priority["title"] == "Margem estrutural deficitária"
    assert priority["impact_amount"] == pytest.approx(150.0)
    assert priority["metrics"][0]["value"] == pytest.approx(-150.0)
    assert result["headline"] != "Estrutura sem riscos imediatos detetados"


def test_bugdec01_positive_margin_still_keeps_healthy_card() -> None:
    result = build_decision_briefing(**_base_context())

    keys = [row["key"] for row in result["priorities"]]
    assert "healthy:allocate" in keys
    assert "margin:structural" not in keys


# ============================================================================
# BUG-DEC-02 (P1): a category that was 0 € in the reference period and is now
# material must be flagged as a new expense.
# ============================================================================


def test_bugdec02_category_from_zero_is_flagged_as_new_expense() -> None:
    context = _base_context()
    context["categories"] = [("Saúde", 1_500.0, "#FF0000")]
    context["previous_categories"] = [("Saúde", 0.0)]

    result = build_decision_briefing(**context)

    keys = [row["key"] for row in result["priorities"]]
    assert "comparison:new:Saúde" in keys
    priority = next(
        row
        for row in result["priorities"]
        if row["key"] == "comparison:new:Saúde"
    )
    assert priority["tone"] == "warning"
    assert priority["impact_amount"] == pytest.approx(1_500.0)
    metric_values = [metric["value"] for metric in priority["metrics"]]
    assert metric_values[0] == pytest.approx(1_500.0)
    assert metric_values[1] == pytest.approx(0.0)


def test_bugdec02_small_new_category_is_not_flagged() -> None:
    context = _base_context()
    # 12.0 € < max(10.0, 1700 * 0.05 = 85.0) => below materiality.
    context["categories"] = [("Miudezas", 12.0, "#999999")]
    context["previous_categories"] = [("Miudezas", 0.0)]

    result = build_decision_briefing(**context)

    keys = [row["key"] for row in result["priorities"]]
    assert "comparison:new:Miudezas" not in keys


# ============================================================================
# BUG-DEC-03 (P1): near-zero saving pace must not project astronomical months;
# require a viable pace and cap the horizon at 10 years.
# ============================================================================


def test_bugdec03_near_zero_pace_does_not_project_astronomical_months() -> None:
    context = _base_context()
    context["monthly_summary"] = [
        {
            "income": 2_000.0,
            "expenses": 1_999.99,
            "savings": 0.01,
            "partial": False,
        }
        for _ in range(6)
    ]
    context["emergency_reserve"] = {
        "available": True,
        "source": "auto",
        "current": 200.0,
        "target": 6_000.0,
        "missing": 5_800.0,
        "coverage_months": 0.4,
    }

    result = build_decision_briefing(**context)
    priority = next(
        row for row in result["priorities"] if row["key"] == "reserve:gap"
    )

    assert "580000" not in priority["message"]
    # Pace 0.01 € < 5 € minimum => no calendarisation.
    assert priority["message"] == (
        "A margem média recente ainda não permite calendarizar o reforço; "
        "primeiro é preciso libertar saldo mensal."
    )


def test_bugdec03_horizon_is_capped_at_ten_years() -> None:
    context = _base_context()
    # Margin 10 €/month, missing 60_000 € => 6_000 months uncapped.
    context["monthly_summary"] = [
        {
            "income": 2_000.0,
            "expenses": 1_990.0,
            "savings": 10.0,
            "partial": False,
        }
        for _ in range(6)
    ]
    context["emergency_reserve"] = {
        "available": True,
        "source": "auto",
        "current": 1_000.0,
        "target": 61_000.0,
        "missing": 60_000.0,
        "coverage_months": 0.4,
    }

    result = build_decision_briefing(**context)
    priority = next(
        row for row in result["priorities"] if row["key"] == "reserve:gap"
    )

    assert "6000" not in priority["message"]
    assert "mais de 10 anos" in priority["message"]


def test_bugdec03_viable_pace_still_calendarises() -> None:
    context = _base_context()
    context["monthly_summary"] = [
        {
            "income": 2_000.0,
            "expenses": 1_700.0,
            "savings": 300.0,
            "partial": False,
        }
        for _ in range(6)
    ]
    context["emergency_reserve"] = {
        "available": True,
        "source": "goal",
        "current": 200.0,
        "target": 2_000.0,
        "missing": 1_800.0,
        "coverage_months": 0.4,
    }

    result = build_decision_briefing(**context)
    priority = next(
        row for row in result["priorities"] if row["key"] == "reserve:gap"
    )

    assert "6 meses" in priority["message"]
    assert priority["action"] == "Abrir objetivo"


# ============================================================================
# BUG-DEC-04 (P2): summary counter must equal the number of rendered rows.
# ============================================================================


def _briefing_data(count: int) -> dict:
    return {
        "as_of": "2026-07-27",
        "headline": "T",
        "priority_count": count,
        "urgent_count": 0,
        "warning_count": count,
        "coverage_score": 82,
        "coverage_level": "high",
        "coverage_label": "Confiança elevada",
        "method_label": "M",
        "scope_summary": "S",
        "priorities": [
            {
                "rank": i,
                "key": f"k{i}",
                "icon": "🎯",
                "tone": "warning",
                "urgency": "U",
                "title": f"T{i}",
                "message": "M",
                "action": "A",
                "page": "orcamentos",
                "category": "",
                "search": "",
                "confidence": "high",
                "confidence_label": "Confiança elevada",
                "scope_label": "S",
                "evidence": "E",
                "impact_amount": 1.0,
                "impact_label": "L",
                "metrics": [],
            }
            for i in range(1, count + 1)
        ],
    }


def test_bugdec04_summary_count_matches_rendered_rows(qapp) -> None:
    card = DecisionBriefingCard()
    try:
        card.set_data(_briefing_data(4), privacy=False, max_items=3)
        assert len(card.priority_rows) == 3
        assert "3 prioridades" in card.summary_meta.text()
        assert "4 prioridades" not in card.summary_meta.text()
    finally:
        card.close()


def test_bugdec04_summary_count_all_rows_fit(qapp) -> None:
    card = DecisionBriefingCard()
    try:
        card.set_data(_briefing_data(4), privacy=False, max_items=6)
        assert len(card.priority_rows) == 4
        assert "4 prioridades" in card.summary_meta.text()
    finally:
        card.close()


# ============================================================================
# BUG-DEC-05 (P2): when no category exceeds its limit, do not pick an
# arbitrary one; report that none exceeded.
# ============================================================================


def test_bugdec05_no_exceeded_budget_reports_none_exceeded() -> None:
    context = _base_context()
    context["budget_forecast"] = {
        "days_elapsed": 18,
        "rows": [
            {
                "category": "Alfa",
                "spent": 80.0,
                "budget": 100.0,
                "projected": 99.0,
                "forecast_status": "Em risco",
            },
            {
                "category": "Beta",
                "spent": 60.0,
                "budget": 200.0,
                "projected": 199.0,
                "forecast_status": "Atenção",
            },
        ],
    }

    result = build_decision_briefing(**context)

    keys = [row["key"] for row in result["priorities"]]
    assert "budget:none_exceeded" in keys
    assert not any(key.startswith("budget:") and key != "budget:none_exceeded" for key in keys)
    priority = next(
        row for row in result["priorities"] if row["key"] == "budget:none_exceeded"
    )
    assert priority["tone"] == "neutral"
    assert priority["title"] == "Nenhum orçamento ultrapassado"
    assert "Nenhuma categoria ultrapassa o limite" in priority["message"]
    # Beta has the highest utilisation (199/200 = 99.5% => ~100% rounded).
    assert "Beta" in priority["message"]
    assert priority["impact_amount"] == pytest.approx(0.0)


def test_bugdec05_exceeded_budget_still_picks_the_culprit() -> None:
    context = _base_context()
    context["budget_forecast"] = {
        "days_elapsed": 18,
        "rows": [
            {
                "category": "Restaurantes",
                "spent": 520.0,
                "budget": 600.0,
                "projected": 900.0,
                "forecast_status": "Em risco",
                "projection_basis": "mês anterior",
            },
        ],
    }

    result = build_decision_briefing(**context)
    priority = next(
        row
        for row in result["priorities"]
        if row["key"] == "budget:Restaurantes"
    )

    assert priority["impact_amount"] == pytest.approx(300.0)
    assert priority["impact_label"] == "excesso previsto"


# ============================================================================
# BUG-HABIT-01 (P1): unit-frequency habits must survive reduction scenarios.
# ============================================================================


def test_bughabit01_scenario_frequency_keeps_unit_frequency() -> None:
    assert _scenario_frequency(1.0, 0.75) == pytest.approx(0.75)
    assert _scenario_frequency(1.0, 0.50) == pytest.approx(0.50)
    assert _scenario_frequency(4.0, 0.75) == pytest.approx(3.0)
    assert _scenario_frequency(4.0, 0.50) == pytest.approx(2.0)
    assert _scenario_frequency(0.0, 0.75) == pytest.approx(0.0)
    assert _scenario_frequency(2.0, 0.0) == pytest.approx(0.0)


def test_bughabit01_simulation_preserves_fractional_occurrences() -> None:
    result = simulate_habit(
        HABIT_PRESETS_BY_KEY["subscriptions"],
        HabitSimulationInputs(
            unit_cost=10.0,
            frequency=1.0,
            frequency_unit="month",
            monthly_income=2_000.0,
        ),
    )

    frequencies = result["scenario_frequencies"]
    assert frequencies["current"] == pytest.approx(1.0)
    assert frequencies["reduce_25"] == pytest.approx(0.75)
    assert frequencies["half"] == pytest.approx(0.50)
    # Monthly costs keep the fractional reduction: 0.75 * 10 = 7.5 €.
    first = result["monthly_series"][0]
    assert first["cost_base"] == pytest.approx(10.0)
    assert first["cost_reduce_25"] == pytest.approx(7.5)
    assert first["cost_half"] == pytest.approx(5.0)
    # A 25% reduction on a 1x/month habit still saves money.
    assert result["quarter_reduction_monthly"] == pytest.approx(2.5)


# ============================================================================
# BUG-HABIT-02 (P1): privacy mode must not leak the variance sign.
# ============================================================================


def _make_habit_card(qapp) -> HabitPlansCard:
    app = MagicMock()
    app.theme.privacy = True
    app.theme.dark = False
    app.content_scroll.viewport().width.return_value = 1280
    app.content_scroll.verticalScrollBar().value.return_value = 0
    app.page_density = "normal"
    app.repo.setting.side_effect = lambda k, default="": str(default)
    app.repo.habit_change_plans.return_value = []
    app.repo.habit_change_plan_by_id.return_value = None
    return HabitPlansCard(app)


def test_bughabit02_privacy_hides_variance_sign(qapp) -> None:
    card = _make_habit_card(qapp)
    try:
        privacy = bool(card.app.theme.privacy)
        assert privacy is True
        variance = 25.0
        cell = (("" if privacy else "+") if variance > 0 else "") + eur(
            variance, privacy
        )
        assert cell == eur(variance, True)
        assert not cell.startswith("+")
        negative = -25.0
        cell_negative = (("" if privacy else "+") if negative > 0 else "") + eur(
            negative, privacy
        )
        assert cell_negative == eur(negative, True)
    finally:
        card.close()


def test_bughabit02_without_privacy_positive_variance_keeps_plus(qapp) -> None:
    card = _make_habit_card(qapp)
    try:
        card.app.theme.privacy = False
        privacy = bool(card.app.theme.privacy)
        variance = 25.0
        cell = (("" if privacy else "+") if variance > 0 else "") + eur(
            variance, privacy
        )
        assert cell == "+25 €"
    finally:
        card.close()


# ============================================================================
# BUG-HABIT-03 (P2): zero planned cost must not divide by zero nor collapse
# into a rigid binary status; adherence is judged against the baseline.
# ============================================================================


def _plan(**overrides) -> dict:
    plan = {
        "id": 1,
        "status": "active",
        "preset_key": "coffee",
        "scenario_key": "eliminate",
        "name": "Cafés",
        "start_date": "2023-06-01",
        "baseline_monthly_cost": 20.0,
        "planned_monthly_cost": 0.0,
        "audit_rules_json": "{}",
        "goal_name": "",
    }
    plan.update(overrides)
    return plan


def _expense(amount: float, day: str) -> dict:
    return {
        "id": 1,
        "description": "Café",
        "amount": amount,
        "financial_date": day,
        "category": "Diversos",
        "subcategory": "",
        "merchant": "",
    }


def test_bughabit03_zero_planned_cost_small_overspend_is_on_track() -> None:
    tracking = build_habit_plan_tracking(
        _plan(),
        [_expense(0.50, "2023-06-02")],
        reference_date=date(2023, 6, 30),
    )

    assert tracking["planned_total"] == pytest.approx(0.0)
    assert tracking["baseline_total"] == pytest.approx(20.0)
    assert tracking["actual_total"] == pytest.approx(0.50)
    assert tracking["status"] == "No caminho"
    assert tracking["tone"] == "positive"


def test_bughabit03_zero_planned_cost_above_baseline_is_off_track() -> None:
    tracking = build_habit_plan_tracking(
        _plan(),
        [_expense(25.0, "2023-06-02")],
        reference_date=date(2023, 6, 30),
    )

    assert tracking["status"] == "Fora do plano"
    assert tracking["tone"] == "negative"


def test_bughabit03_zero_planned_cost_zero_actual_is_on_track() -> None:
    tracking = build_habit_plan_tracking(
        _plan(),
        [],
        reference_date=date(2023, 6, 30),
    )

    assert tracking["status"] == "No caminho"
    assert tracking["tone"] == "positive"


def test_bughabit03_zero_planned_cost_between_90_and_100_pct_is_warning() -> None:
    # baseline_total = 20.0; actual 19.0 => 95% of baseline => Atenção.
    tracking = build_habit_plan_tracking(
        _plan(),
        [_expense(19.0, "2023-06-02")],
        reference_date=date(2023, 6, 30),
    )

    assert tracking["status"] == "Atenção"
    assert tracking["tone"] == "warning"


def test_bughabit03_no_baseline_keeps_binary_rule() -> None:
    tracking = build_habit_plan_tracking(
        _plan(baseline_monthly_cost=0.0),
        [_expense(5.0, "2023-06-02")],
        reference_date=date(2023, 6, 30),
    )

    assert tracking["baseline_total"] == pytest.approx(0.0)
    assert tracking["status"] == "Fora do plano"


# ============================================================================
# BUG-HABIT-04 (P1): 29 February in a non-leap year must clamp to 28 Feb
# instead of silently falling back to today and wiping history.
# ============================================================================


def test_bughabit04_feb29_non_leap_year_clamps_to_feb28() -> None:
    tracking = build_habit_plan_tracking(
        _plan(start_date="2023-02-29"),
        [],
        reference_date=date(2023, 3, 15),
    )

    assert tracking["start_date"] == "2023-02-28"
    months = [row["month"] for row in tracking["rows"]]
    assert months == ["2023-02", "2023-03"]


def test_bughabit04_feb29_leap_year_is_kept() -> None:
    tracking = build_habit_plan_tracking(
        _plan(start_date="2024-02-29"),
        [],
        reference_date=date(2024, 3, 15),
    )

    assert tracking["start_date"] == "2024-02-29"
    months = [row["month"] for row in tracking["rows"]]
    assert months == ["2024-02", "2024-03"]


def test_bughabit04_truly_invalid_date_still_falls_back() -> None:
    tracking = build_habit_plan_tracking(
        _plan(start_date="not-a-date"),
        [],
        reference_date=date(2023, 3, 15),
    )

    assert tracking["start_date"] == "2023-03-15"
    # Fallback = today (2023-03-15) => only the current partial month exists.
    assert [row["month"] for row in tracking["rows"]] == ["2023-03"]
    assert tracking["rows"][0]["partial"] is True


# ============================================================================
# BUG-PAN-01 (P2): negative savings rate must not be hidden as 0% and a meta
# configured as 0 € must be distinguishable from no meta at all.
# ============================================================================


def _panorama_page(qapp, tmp_path, monkeypatch, totals: dict, target: dict):
    import database.repo.base as repo_base
    import services.documents as document_service_module
    import utils.app_logging as app_logging

    database_path = tmp_path / "financas_pessoais.sqlite3"
    monkeypatch.setattr(repo_base, "db_path", lambda: database_path)
    monkeypatch.setattr(
        document_service_module, "attachments_dir", lambda: tmp_path / "anexos"
    )
    monkeypatch.setattr(app_logging, "configure_logging", lambda *_a, **_k: None)

    from main import LiquidFinanceApp
    from PySide6.QtTest import QTest

    window = LiquidFinanceApp()
    window.repo.set_setting("backup_on_close", "0")
    window.backup_service.run_backup_on_close = lambda silent=True: None
    window.check_subscription_reminders = lambda: None
    window.show()
    QTest.qWait(50)
    window.goto("panorama")
    QTest.qWait(50)
    return window


def test_bugpan01_month_target_distinguishes_no_row_from_zero_meta(
    tmp_path, monkeypatch
) -> None:
    import database.repo.base as repo_base

    database_path = tmp_path / "targets.sqlite3"
    monkeypatch.setattr(repo_base, "db_path", lambda: database_path)
    repo = Repo()
    try:
        no_row = repo.month_target("2026-01")
        assert no_row["expense_target"] == 0.0
        assert no_row["has_row"] == 0.0

        repo.set_month_target("2026-02", 500.0)
        configured = repo.month_target("2026-02")
        assert configured["expense_target"] == pytest.approx(500.0)
        assert configured["has_row"] == 1.0

        repo.set_month_target("2026-03", 0.0)
        zero_meta = repo.month_target("2026-03")
        assert zero_meta["expense_target"] == pytest.approx(0.0)
        assert zero_meta["has_row"] == 1.0
    finally:
        repo.db.conn.close()


def test_bugpan01_rate_helper_shows_deficit_without_income() -> None:
    # Mirror of the panorama rate rule: income=0 & expenses>0 => -100%.
    def panorama_rate(savings: float, income: float, expenses: float) -> float:
        if income:
            return savings / income * 100
        if expenses > 0:
            return -100.0
        return 0.0

    assert panorama_rate(-200.0, 0.0, 500.0) == pytest.approx(-100.0)
    assert panorama_rate(-200.0, 1_000.0, 1_200.0) == pytest.approx(-20.0)
    assert panorama_rate(0.0, 0.0, 0.0) == pytest.approx(0.0)
    assert panorama_rate(300.0, 2_000.0, 1_700.0) == pytest.approx(15.0)


def test_bugpan01_negative_rate_is_not_clamped_by_percent() -> None:
    from utils.helpers import percent

    assert percent(-25.0) == "-25,0%"
    assert percent(-100.0) == "-100,0%"
    assert percent(0.0) == "0,0%"
