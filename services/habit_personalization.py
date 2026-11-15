"""Personal evidence and goal impact for habit simulations.

The UI deliberately receives presentation-ready dictionaries from this module:
matching rules, confidence and date calculations stay testable and independent
from Qt and SQLite.
"""

from __future__ import annotations

import math
import unicodedata
from dataclasses import dataclass
from datetime import date
from typing import Any, Iterable, Mapping, Optional

from services.habit_simulations import (
    DAYS_PER_MONTH,
    WEEKS_PER_MONTH,
    HabitPreset,
    occurrences_per_month,
)
from utils.helpers import add_months, safe_float


@dataclass(slots=True, frozen=True)
class HabitMatchRule:
    keywords: tuple[str, ...]
    categories: tuple[str, ...] = ()
    exclusions: tuple[str, ...] = ()


HABIT_MATCH_RULES: dict[str, HabitMatchRule] = {
    "casino": HabitMatchRule(
        ("casino", "slot", "roleta", "blackjack", "jogo online"),
        ("casino", "jogo"),
    ),
    "betting": HabitMatchRule(
        (
            "aposta",
            "betano",
            "betclic",
            "betway",
            "bwin",
            "placard",
            "solverde",
        ),
        ("apostas",),
    ),
    "dinners": HabitMatchRule(
        ("jantar", "restaurante", "pizzaria", "churrasqueira", "tasca"),
        ("restaurantes", "restaurante"),
        ("supermercado", "cantina"),
    ),
    "beer": HabitMatchRule(
        ("cerveja", "beer", "cervejaria", "bar ", " pub", "pub "),
        ("bares", "cervejas"),
    ),
    "coffee": HabitMatchRule(
        ("cafe", "coffee", "cafeteria", "cafetaria", "starbucks"),
        ("cafes", "cafetarias"),
        ("supermercado",),
    ),
    "snacks": HabitMatchRule(
        ("lanche", "snack", "pastelaria", "sandes", "tosta"),
        ("lanches", "pastelaria"),
    ),
    "sweets": HabitMatchRule(
        ("doce", "chocolate", "gelado", "sobremesa", "confeitaria"),
        ("doces", "sobremesas"),
    ),
    "canteen": HabitMatchRule(
        ("cantina", "refeitorio", "refeicao escolar"),
        ("cantina", "refeitorio"),
    ),
    "subscriptions": HabitMatchRule(
        (
            "subscri",
            "subscription",
            "mensalidade",
            "streaming",
            "netflix",
            "spotify",
            "disney+",
            "hbo",
        ),
        ("subscricoes", "assinaturas"),
    ),
    "delivery": HabitMatchRule(
        (
            "take-away",
            "take away",
            "takeaway",
            "uber eats",
            "glovo",
            "bolt food",
            "just eat",
            "delivery",
        ),
        ("take-away", "delivery"),
    ),
    "impulse": HabitMatchRule(
        (
            "compra impulso",
            "compra ocasional",
            "shopping",
            "loja online",
            "marketplace",
        ),
        ("compras impulso",),
    ),
    "ride_hailing": HabitMatchRule(
        ("tvde", "taxi", "uber", "bolt", "cabify"),
        ("tvde", "taxi"),
        ("uber eats", "bolt food"),
    ),
}


def _fold(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or "").casefold())
    return " ".join("".join(ch for ch in text if not unicodedata.combining(ch)).split())


def _row_value(row: Any, key: str, default: Any = "") -> Any:
    if isinstance(row, Mapping):
        return row.get(key, default)
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return default


def habit_match_kind(preset_key: str, row: Any) -> str:
    """Return ``keyword``, ``category`` or an empty string for one expense."""
    rule = HABIT_MATCH_RULES.get(str(preset_key or ""))
    if rule is None:
        return ""
    category = _fold(_row_value(row, "category"))
    text = _fold(
        " ".join(
            str(_row_value(row, field) or "")
            for field in ("description", "merchant", "subcategory", "category")
        )
    )
    exclusions = tuple(_fold(value) for value in rule.exclusions)
    if any(value and value in text for value in exclusions):
        return ""
    keywords = tuple(_fold(value) for value in rule.keywords)
    if any(value and value in text for value in keywords):
        return "keyword"
    categories = {_fold(value) for value in rule.categories}
    return "category" if category in categories else ""


def normalise_habit_audit_rules(rules: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    source = rules or {}

    def ids(key: str) -> list[int]:
        values = source.get(key, [])
        if not isinstance(values, (list, tuple, set)):
            return []
        parsed: set[int] = set()
        for value in values:
            try:
                identifier = int(value)
            except (TypeError, ValueError):
                continue
            if identifier > 0:
                parsed.add(identifier)
        return sorted(parsed)

    raw_keywords = source.get("keywords", [])
    if isinstance(raw_keywords, str):
        raw_keywords = raw_keywords.split(",")
    keywords: list[str] = []
    if isinstance(raw_keywords, (list, tuple, set)):
        seen: set[str] = set()
        for value in raw_keywords:
            text = str(value or "").strip()
            folded = _fold(text)
            if folded and folded not in seen:
                keywords.append(text)
                seen.add(folded)
    return {
        "included_transaction_ids": ids("included_transaction_ids"),
        "excluded_transaction_ids": ids("excluded_transaction_ids"),
        "keywords": keywords,
    }


def build_habit_audit_rows(
    preset: HabitPreset,
    transactions: Iterable[Any],
    audit_rules: Optional[Mapping[str, Any]] = None,
) -> list[dict[str, Any]]:
    """Return every candidate expense with its explainable selection state."""
    rules = normalise_habit_audit_rules(audit_rules)
    included = set(rules["included_transaction_ids"])
    excluded = set(rules["excluded_transaction_ids"])
    custom_keywords = tuple(_fold(value) for value in rules["keywords"])
    result: list[dict[str, Any]] = []
    for source in transactions:
        identifier = int(safe_float(_row_value(source, "id")))
        amount = abs(safe_float(_row_value(source, "amount")))
        if amount <= 0:
            continue
        built_in_kind = habit_match_kind(preset.key, source)
        searchable = _fold(
            " ".join(
                str(_row_value(source, field) or "")
                for field in ("description", "merchant", "subcategory", "category")
            )
        )
        custom_match = any(
            keyword and keyword in searchable for keyword in custom_keywords
        )
        automatic_match = bool(built_in_kind or custom_match)
        manually_included = identifier > 0 and identifier in included
        manually_excluded = identifier > 0 and identifier in excluded
        selected = (automatic_match or manually_included) and not manually_excluded
        if manually_excluded:
            match_kind = "excluded"
        elif manually_included and not automatic_match:
            match_kind = "manual"
        elif custom_match and not built_in_kind:
            match_kind = "custom"
        else:
            match_kind = built_in_kind
        result.append(
            {
                "id": identifier,
                "financial_date": str(_row_value(source, "financial_date") or "")[:10],
                "description": str(_row_value(source, "description") or ""),
                "amount": amount,
                "category": str(_row_value(source, "category") or ""),
                "subcategory": str(_row_value(source, "subcategory") or ""),
                "merchant": str(_row_value(source, "merchant") or ""),
                "built_in_match": bool(built_in_kind),
                "custom_match": custom_match,
                "automatic_match": automatic_match,
                "selected": selected,
                "match_kind": match_kind,
            }
        )
    result.sort(
        key=lambda row: (
            not bool(row["selected"]),
            not bool(row["automatic_match"]),
            str(row["financial_date"]),
            int(row["id"]),
        ),
        reverse=False,
    )
    # Within each relevance group, keep the newest movements first.
    grouped: list[dict[str, Any]] = []
    for selected, automatic in (
        (True, True),
        (True, False),
        (False, True),
        (False, False),
    ):
        group = [
            row
            for row in result
            if bool(row["selected"]) == selected
            and bool(row["automatic_match"]) == automatic
        ]
        grouped.extend(
            sorted(
                group,
                key=lambda row: (str(row["financial_date"]), int(row["id"])),
                reverse=True,
            )
        )
    return grouped


def _suggest_integer_frequency(
    average_occurrences_month: float,
    preferred_unit: str,
) -> tuple[int, str]:
    monthly = max(0.0, safe_float(average_occurrences_month))
    if monthly <= 0:
        return 0, preferred_unit if preferred_unit in {
            "day",
            "week",
            "month",
        } else "month"
    factors = {
        "day": DAYS_PER_MONTH,
        "week": WEEKS_PER_MONTH,
        "month": 1.0,
    }
    preference = preferred_unit if preferred_unit in factors else "month"
    candidates: list[tuple[float, int, str]] = []
    for unit, factor in factors.items():
        frequency = max(1, min(60, int(monthly / factor + 0.5)))
        represented = frequency * factor
        relative_error = abs(represented - monthly) / max(1.0, monthly)
        preference_penalty = 0.0 if unit == preference else 0.10
        candidates.append((relative_error + preference_penalty, frequency, unit))
    _score, frequency, unit = min(candidates, key=lambda item: item[0])
    return frequency, unit


def build_habit_calibration(
    preset: HabitPreset,
    transactions: Iterable[Any],
    *,
    period_start: str,
    period_end: str,
    coverage_months: int,
    audit_rules: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Infer a robust, explainable simulation input from recorded expenses."""
    audit_rows = build_habit_audit_rows(preset, transactions, audit_rules)
    matched = [row for row in audit_rows if bool(row.get("selected"))]

    months = max(0, int(coverage_months or 0))
    if not matched or months <= 0:
        return {
            "available": False,
            "preset_key": preset.key,
            "transaction_count": 0,
            "active_months": 0,
            "coverage_months": months,
            "period_start": str(period_start or ""),
            "period_end": str(period_end or ""),
            "confidence": "none",
            "confidence_label": "Sem correspondências",
            "tone": "neutral",
        }

    amounts = [abs(safe_float(row.get("amount"))) for row in matched]
    active_months = {
        str(row.get("financial_date") or "")[:7]
        for row in matched
        if str(row.get("financial_date") or "")[:7]
    }
    keyword_count = sum(1 for row in matched if row.get("match_kind") == "keyword")
    category_count = sum(1 for row in matched if row.get("match_kind") == "category")
    custom_count = sum(1 for row in matched if row.get("match_kind") == "custom")
    manual_count = sum(1 for row in matched if row.get("match_kind") == "manual")
    average_unit_cost = sum(amounts) / len(amounts)
    average_monthly_occurrences = len(matched) / months
    average_monthly_spend = sum(amounts) / months
    frequency, frequency_unit = _suggest_integer_frequency(
        average_monthly_occurrences,
        preset.frequency_unit,
    )

    if len(matched) >= 12 and len(active_months) >= 4:
        confidence, confidence_label, tone = "high", "Confiança alta", "positive"
    elif len(matched) >= 5 and len(active_months) >= 2:
        confidence, confidence_label, tone = "medium", "Confiança média", "warning"
    else:
        confidence, confidence_label, tone = "low", "Confiança inicial", "neutral"
    if keyword_count == 0 and confidence == "high":
        confidence, confidence_label, tone = "medium", "Confiança média", "warning"

    represented_occurrences = occurrences_per_month(frequency, frequency_unit)
    return {
        "available": True,
        "preset_key": preset.key,
        "transaction_count": len(matched),
        "active_months": len(active_months),
        "coverage_months": months,
        "period_start": str(period_start or ""),
        "period_end": str(period_end or ""),
        "average_unit_cost": average_unit_cost,
        "average_monthly_spend": average_monthly_spend,
        "average_monthly_occurrences": average_monthly_occurrences,
        "suggested_frequency": frequency,
        "suggested_frequency_unit": frequency_unit,
        "represented_monthly_occurrences": represented_occurrences,
        "keyword_matches": keyword_count,
        "category_matches": category_count,
        "custom_matches": custom_count,
        "manual_matches": manual_count,
        "excluded_matches": sum(
            1 for row in audit_rows if row.get("match_kind") == "excluded"
        ),
        "confidence": confidence,
        "confidence_label": confidence_label,
        "tone": tone,
    }


def _months_to_goal(missing: float, monthly_contribution: float) -> Optional[int]:
    if missing <= 0:
        return 0
    contribution = max(0.0, safe_float(monthly_contribution))
    if contribution <= 0:
        return None
    return max(1, int(math.ceil(missing / contribution)))


def build_goal_impact(
    goal: Any,
    simulation: dict[str, Any],
    *,
    reference_date: Optional[date] = None,
) -> dict[str, Any]:
    """Project how the simulation's monthly savings changes one goal date."""
    today = reference_date or date.today()
    target = max(0.0, safe_float(_row_value(goal, "target_amount")))
    saved = max(0.0, safe_float(_row_value(goal, "saved_amount")))
    missing = max(0.0, target - saved)
    base_contribution = max(
        0.0,
        safe_float(_row_value(goal, "monthly_contribution")),
    )
    monthly = dict((simulation.get("monthly_series") or [{}])[0])
    current_cost = max(0.0, safe_float(monthly.get("cost_base")))
    extras = {
        "current": 0.0,
        "reduce_25": max(0.0, safe_float(monthly.get("savings_reduce_25"))),
        "half": max(0.0, safe_float(monthly.get("savings_half"))),
        "alternative": max(0.0, safe_float(monthly.get("savings_alternative"))),
        "eliminate": current_cost,
    }
    baseline_months = _months_to_goal(missing, base_contribution)
    scenarios: dict[str, dict[str, Any]] = {}
    for key, extra in extras.items():
        total_contribution = base_contribution + extra
        months = _months_to_goal(missing, total_contribution)
        completion = add_months(today, months).isoformat() if months is not None else ""
        months_gained = (
            max(0, baseline_months - months)
            if baseline_months is not None and months is not None
            else None
        )
        scenarios[key] = {
            "monthly_extra": extra,
            "monthly_contribution": total_contribution,
            "months": months,
            "completion_date": completion,
            "months_gained": months_gained,
            "creates_plan": baseline_months is None and months is not None,
        }

    return {
        "goal_id": int(safe_float(_row_value(goal, "id"))),
        "goal_name": str(_row_value(goal, "name") or "Objetivo"),
        "target_amount": target,
        "saved_amount": saved,
        "missing_amount": missing,
        "base_contribution": base_contribution,
        "baseline_months": baseline_months,
        "scenarios": scenarios,
    }


__all__ = [
    "HABIT_MATCH_RULES",
    "HabitMatchRule",
    "build_goal_impact",
    "build_habit_audit_rows",
    "build_habit_calibration",
    "habit_match_kind",
    "normalise_habit_audit_rules",
]
