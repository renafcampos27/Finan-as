"""Creation and measured follow-up of habit-change plans."""

from __future__ import annotations

import calendar
import json
import re
from datetime import date
from typing import Any, Mapping, Optional

from services.habit_personalization import (
    build_habit_audit_rows,
    normalise_habit_audit_rules,
)
from services.habit_simulations import HABIT_PRESETS_BY_KEY
from utils.helpers import safe_float


SCENARIO_LABELS = {
    "reduce_25": "−25% das ocorrências",
    "half": "Metade das ocorrências",
    "alternative": "Alternativa realista",
    "eliminate": "Eliminar o hábito",
}


def _value(row: Any, key: str, default: Any = "") -> Any:
    if isinstance(row, Mapping):
        return row.get(key, default)
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return default


def scenario_plan_values(
    simulation: dict[str, Any],
    scenario_key: str,
) -> dict[str, float]:
    """Extract one monthly scenario without recalculating the simulation."""
    selected = str(scenario_key or "")
    if selected not in SCENARIO_LABELS:
        raise ValueError(f"Unsupported habit plan scenario: {scenario_key}")
    monthly = dict((simulation.get("monthly_series") or [{}])[0])
    inputs = simulation.get("inputs") or {}
    frequencies = simulation.get("scenario_frequencies") or {}
    baseline_cost = max(0.0, safe_float(monthly.get("cost_base")))
    baseline_frequency = max(0.0, safe_float(inputs.get("frequency")))
    if selected == "reduce_25":
        planned_cost = max(0.0, safe_float(monthly.get("cost_reduce_25")))
        target_frequency = max(0.0, safe_float(frequencies.get("reduce_25")))
    elif selected == "half":
        planned_cost = max(0.0, safe_float(monthly.get("cost_half")))
        target_frequency = max(0.0, safe_float(frequencies.get("half")))
    elif selected == "alternative":
        planned_cost = max(0.0, safe_float(monthly.get("cost_alternative")))
        target_frequency = baseline_frequency
    else:
        planned_cost = 0.0
        target_frequency = 0.0
    return {
        "baseline_monthly_cost": baseline_cost,
        "planned_monthly_cost": planned_cost,
        "planned_monthly_savings": max(0.0, baseline_cost - planned_cost),
        "target_frequency": target_frequency,
    }


def build_habit_plan_payload(
    *,
    preset_key: str,
    simulation: dict[str, Any],
    scenario_key: str,
    name: str,
    start_date: str,
    goal_id: Optional[int],
    audit_rules: Optional[Mapping[str, Any]] = None,
    notes: str = "",
) -> dict[str, Any]:
    preset = HABIT_PRESETS_BY_KEY.get(str(preset_key or ""))
    if preset is None:
        raise ValueError(f"Unknown habit preset: {preset_key}")
    scenario = scenario_plan_values(simulation, scenario_key)
    inputs = simulation.get("inputs") or {}
    return {
        "name": str(name or "").strip()
        or f"{preset.name} · {SCENARIO_LABELS[scenario_key]}",
        "preset_key": preset.key,
        "habit_name": preset.name,
        "scenario_key": scenario_key,
        "baseline_unit_cost": max(0.0, safe_float(inputs.get("unit_cost"))),
        "baseline_frequency": max(0.0, safe_float(inputs.get("frequency"))),
        "frequency_unit": str(inputs.get("frequency_unit") or "month"),
        "alternative_unit_cost": max(
            0.0,
            safe_float(inputs.get("alternative_unit_cost")),
        ),
        "target_frequency": scenario["target_frequency"],
        "baseline_monthly_cost": scenario["baseline_monthly_cost"],
        "planned_monthly_cost": scenario["planned_monthly_cost"],
        "planned_monthly_savings": scenario["planned_monthly_savings"],
        "goal_id": int(goal_id) if goal_id else None,
        "start_date": str(start_date or date.today().isoformat())[:10],
        "audit_rules_json": json.dumps(
            normalise_habit_audit_rules(audit_rules),
            ensure_ascii=False,
            sort_keys=True,
        ),
        "notes": str(notes or "").strip(),
    }


def _parse_rules(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return normalise_habit_audit_rules(value)
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        parsed = {}
    return normalise_habit_audit_rules(parsed if isinstance(parsed, Mapping) else {})


def _parse_date(value: Any, fallback: date) -> date:
    text = str(value or "")[:10]
    try:
        return date.fromisoformat(text)
    except (TypeError, ValueError):
        # BUG-HABIT-04: 29 February in a non-leap year must clamp to 28 Feb
        # instead of silently falling back to today, which would erase the
        # tracking history of the plan.
        match = re.fullmatch(r"(\d{4})-02-29", text)
        if match:
            return date(int(match.group(1)), 2, 28)
        return fallback


def build_habit_plan_tracking(
    plan: Any,
    transactions: list[Any],
    *,
    reference_date: Optional[date] = None,
) -> dict[str, Any]:
    """Compare the time-adjusted plan with matched real spending by month."""
    today = reference_date or date.today()
    start = _parse_date(_value(plan, "start_date"), today)
    preset_key = str(_value(plan, "preset_key") or "")
    preset = HABIT_PRESETS_BY_KEY.get(preset_key)
    if preset is None:
        return {
            "rows": [],
            "status": "Sem categoria",
            "tone": "warning",
            "message": "O hábito deste plano já não está disponível.",
        }
    rules = _parse_rules(_value(plan, "audit_rules_json", "{}"))
    audit_rows = build_habit_audit_rows(preset, transactions, rules)
    selected_transactions = [row for row in audit_rows if bool(row.get("selected"))]
    baseline_monthly = max(0.0, safe_float(_value(plan, "baseline_monthly_cost")))
    planned_monthly = max(0.0, safe_float(_value(plan, "planned_monthly_cost")))

    rows: list[dict[str, Any]] = []
    if start <= today:
        cursor = start.replace(day=1)
        final_month = today.replace(day=1)
        cumulative_planned = 0.0
        cumulative_actual = 0.0
        cumulative_baseline = 0.0
        while cursor <= final_month:
            days_in_month = calendar.monthrange(cursor.year, cursor.month)[1]
            month_end = cursor.replace(day=days_in_month)
            effective_start = max(start, cursor)
            effective_end = min(today, month_end)
            active_days = max(0, (effective_end - effective_start).days + 1)
            factor = active_days / days_in_month if active_days else 0.0
            actual = sum(
                safe_float(row.get("amount"))
                for row in selected_transactions
                if effective_start.isoformat()
                <= str(row.get("financial_date") or "")[:10]
                <= effective_end.isoformat()
            )
            transaction_count = sum(
                1
                for row in selected_transactions
                if effective_start.isoformat()
                <= str(row.get("financial_date") or "")[:10]
                <= effective_end.isoformat()
            )
            planned = planned_monthly * factor
            baseline = baseline_monthly * factor
            cumulative_planned += planned
            cumulative_actual += actual
            cumulative_baseline += baseline
            rows.append(
                {
                    "month": cursor.isoformat()[:7],
                    "label": f"{cursor.month:02d}/{cursor.year}",
                    "days_observed": active_days,
                    "days_in_month": days_in_month,
                    "partial": active_days < days_in_month,
                    "planned_cost": planned,
                    "actual_cost": actual,
                    "variance": actual - planned,
                    "planned_savings": baseline - planned,
                    "actual_savings": baseline - actual,
                    "cumulative_planned": cumulative_planned,
                    "cumulative_actual": cumulative_actual,
                    "cumulative_baseline": cumulative_baseline,
                    "transaction_count": transaction_count,
                }
            )
            if cursor.month == 12:
                cursor = date(cursor.year + 1, 1, 1)
            else:
                cursor = date(cursor.year, cursor.month + 1, 1)

    planned_total = sum(safe_float(row.get("planned_cost")) for row in rows)
    actual_total = sum(safe_float(row.get("actual_cost")) for row in rows)
    baseline_total = sum(
        safe_float(row.get("planned_cost")) + safe_float(row.get("planned_savings"))
        for row in rows
    )
    status_value = str(_value(plan, "status") or "active")
    if start > today:
        status, tone = "Agendado", "neutral"
    elif status_value == "paused":
        status, tone = "Pausado", "warning"
    elif status_value == "completed":
        status, tone = "Concluído", "positive"
    elif planned_total <= 0:
        # BUG-HABIT-03: with zero planned cost there is no denominator to
        # divide by; judge adherence against the historical baseline spend
        # instead of a rigid binary rule.
        if baseline_total <= 0:
            status, tone = (
                ("No caminho", "positive")
                if actual_total <= 0
                else ("Fora do plano", "negative")
            )
        elif actual_total <= baseline_total * 0.90:
            status, tone = "No caminho", "positive"
        elif actual_total <= baseline_total:
            status, tone = "Atenção", "warning"
        else:
            status, tone = "Fora do plano", "negative"
    elif actual_total <= planned_total * 1.05:
        status, tone = "No caminho", "positive"
    elif actual_total <= planned_total * 1.20:
        status, tone = "Atenção", "warning"
    else:
        status, tone = "Fora do plano", "negative"

    return {
        "plan_id": int(safe_float(_value(plan, "id"))),
        "plan_name": str(_value(plan, "name") or "Plano"),
        "preset_key": preset_key,
        "habit_name": str(_value(plan, "habit_name") or preset.name),
        "scenario_key": str(_value(plan, "scenario_key") or ""),
        "scenario_label": SCENARIO_LABELS.get(
            str(_value(plan, "scenario_key") or ""),
            "Plano personalizado",
        ),
        "start_date": start.isoformat(),
        "status": status,
        "tone": tone,
        "rows": rows,
        "planned_total": planned_total,
        "actual_total": actual_total,
        "variance_total": actual_total - planned_total,
        "baseline_total": baseline_total,
        "planned_savings_total": baseline_total - planned_total,
        "actual_savings_total": baseline_total - actual_total,
        "transaction_count": sum(int(row.get("transaction_count", 0)) for row in rows),
        "goal_id": int(safe_float(_value(plan, "goal_id"))) or None,
        "goal_name": str(_value(plan, "goal_name") or ""),
    }


__all__ = [
    "SCENARIO_LABELS",
    "build_habit_plan_payload",
    "build_habit_plan_tracking",
    "scenario_plan_values",
]
