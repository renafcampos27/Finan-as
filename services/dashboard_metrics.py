"""Explainable dashboard metrics for emergency reserves and work time."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from utils.helpers import safe_float
from utils.output_security import safe_hex_colour


DEFAULT_WEEKLY_WORK_HOURS = 40.0
DEFAULT_EMERGENCY_TARGET_MONTHS = 6.0


def _row_value(row: Any, key: str, default: Any = "") -> Any:
    if isinstance(row, Mapping):
        return row.get(key, default)
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return default


def emergency_reserve_snapshot(
    goals: Iterable[Any],
    *,
    cash_balance: float,
    monthly_expense: float,
    target_months: float = DEFAULT_EMERGENCY_TARGET_MONTHS,
) -> dict[str, Any]:
    """Prefer an explicit emergency goal, otherwise derive a liquid reference."""
    explicit = next(
        (
            goal
            for goal in goals or []
            if str(_row_value(goal, "goal_type") or "").strip() == "emergency_fund"
        ),
        None,
    )
    expense = max(0.0, safe_float(monthly_expense))
    months_target = max(1.0, min(24.0, safe_float(target_months, 6.0)))

    if explicit is not None:
        goal = explicit
        current = max(0.0, safe_float(_row_value(goal, "saved_amount")))
        target = max(0.0, safe_float(_row_value(goal, "target_amount")))
        source = "goal"
        goal_id = int(safe_float(_row_value(goal, "id"))) or None
        goal_name = str(_row_value(goal, "name") or "Reserva de emergência")
    else:
        current = max(0.0, safe_float(cash_balance))
        target = expense * months_target
        source = "automatic"
        goal_id = None
        goal_name = "Referência automática"

    coverage_months = current / expense if expense > 0 else 0.0
    missing = max(0.0, target - current)
    above = max(0.0, current - target)
    progress = current / target if target > 0 else 0.0
    if target <= 0:
        status_key, tone = "unavailable", "neutral"
    elif current > target + 0.01:
        status_key, tone = "above", "positive"
    elif current >= target - 0.01:
        status_key, tone = "reached", "positive"
    elif coverage_months >= 3.0:
        status_key, tone = "base", "neutral"
    elif coverage_months >= 1.0:
        status_key, tone = "building", "warning"
    else:
        status_key, tone = "critical", "negative"

    return {
        "available": target > 0,
        "source": source,
        "goal_id": goal_id,
        "goal_name": goal_name,
        "current": current,
        "target": target,
        "missing": missing,
        "above": above,
        "progress": progress,
        "coverage_months": coverage_months,
        "target_months": months_target,
        "monthly_expense": expense,
        "status_key": status_key,
        "tone": tone,
    }


def work_value_snapshot(
    monthly_income: float,
    *,
    weekly_hours: float = DEFAULT_WEEKLY_WORK_HOURS,
) -> dict[str, float | bool]:
    """Convert net monthly salary into transparent hourly and daily values."""
    income = max(0.0, safe_float(monthly_income))
    hours_week = max(1.0, min(100.0, safe_float(weekly_hours, 40.0)))
    hours_month = hours_week * 52.0 / 12.0
    hours_day = hours_week / 5.0
    hourly_value = income / hours_month if hours_month > 0 else 0.0
    daily_value = hourly_value * hours_day
    return {
        "available": income > 0,
        "monthly_income": income,
        "weekly_hours": hours_week,
        "monthly_hours": hours_month,
        "daily_hours": hours_day,
        "hourly_value": hourly_value,
        "daily_value": daily_value,
        "minutes_per_euro": 60.0 / hourly_value if hourly_value > 0 else 0.0,
    }


def expense_work_time_snapshot(
    categories: Iterable[Any],
    *,
    hourly_value: float,
    daily_hours: float = 8.0,
) -> dict[str, Any]:
    """Convert every visible category amount into equivalent paid work time."""
    per_hour = max(0.0, safe_float(hourly_value))
    hours_day = max(1.0, min(24.0, safe_float(daily_hours, 8.0)))
    grouped: dict[str, tuple[float, str]] = {}
    for row in categories or []:
        if isinstance(row, Mapping):
            name = str(row.get("category") or row.get("name") or "Sem categoria")
            amount = abs(safe_float(row.get("amount", row.get("value", 0))))
            colour = safe_hex_colour(row.get("colour") or row.get("color"))
        else:
            try:
                name, raw_amount, colour = row
            except (TypeError, ValueError):
                continue
            name = str(name or "Sem categoria")
            amount = abs(safe_float(raw_amount))
            colour = safe_hex_colour(colour)
        if amount <= 0:
            continue
        previous, previous_colour = grouped.get(name, (0.0, colour))
        grouped[name] = (previous + amount, previous_colour)

    rows = []
    for name, (amount, colour) in sorted(
        grouped.items(), key=lambda item: item[1][0], reverse=True
    ):
        hours = amount / per_hour if per_hour > 0 else 0.0
        rows.append(
            {
                "category": name,
                "amount": amount,
                "hours": hours,
                "days": hours / hours_day if hours_day > 0 else 0.0,
                "colour": colour,
            }
        )
    total_amount = sum(safe_float(row["amount"]) for row in rows)
    total_hours = sum(safe_float(row["hours"]) for row in rows)
    return {
        "available": per_hour > 0,
        "hourly_value": per_hour,
        "daily_hours": hours_day,
        "rows": rows,
        "total_amount": total_amount,
        "total_hours": total_hours,
        "total_days": total_hours / hours_day if hours_day > 0 else 0.0,
    }


__all__ = [
    "DEFAULT_EMERGENCY_TARGET_MONTHS",
    "DEFAULT_WEEKLY_WORK_HOURS",
    "emergency_reserve_snapshot",
    "expense_work_time_snapshot",
    "work_value_snapshot",
]
