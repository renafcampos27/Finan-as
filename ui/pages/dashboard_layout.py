"""Declarative dashboard card registry and pure layout planning helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence


@dataclass(frozen=True, slots=True)
class DashboardCardSpec:
    key: str
    setting_key: str
    widget_attribute: str
    preferred_span: int
    default_visible: bool = True
    compact_visible: bool = True


@dataclass(frozen=True, slots=True)
class DashboardCardPlacement:
    key: str
    row: int
    column: int
    column_span: int


DASHBOARD_CARD_SPECS = (
    DashboardCardSpec("chart", "show_chart", "chart_card", 99),
    DashboardCardSpec(
        "cashflow_pulse",
        "show_cashflow_pulse",
        "cashflow_pulse_card",
        2,
    ),
    DashboardCardSpec(
        "savings_efficiency",
        "show_savings_efficiency",
        "savings_efficiency_card",
        1,
    ),
    DashboardCardSpec(
        "waterfall",
        "show_waterfall",
        "waterfall_card",
        99,
        compact_visible=False,
    ),
    DashboardCardSpec(
        "budget_forecast",
        "show_budget_forecast",
        "budget_card",
        2,
    ),
    DashboardCardSpec(
        "cashflow_calendar",
        "show_cashflow_calendar",
        "cashflow_card",
        99,
        compact_visible=False,
    ),
    DashboardCardSpec("donut", "show_donut", "donut_card", 2),
    DashboardCardSpec(
        "expense_work_time",
        "show_expense_work_time",
        "expense_work_time_card",
        99,
    ),
    DashboardCardSpec("goals", "show_goals", "goals_card", 1),
    DashboardCardSpec("insights", "show_insights", "insights", 1),
    DashboardCardSpec(
        "radar",
        "show_radar",
        "radar_card",
        99,
        default_visible=False,
        compact_visible=False,
    ),
    DashboardCardSpec(
        "treemap",
        "show_treemap",
        "treemap_card",
        99,
        default_visible=False,
        compact_visible=False,
    ),
    DashboardCardSpec(
        "bubble",
        "show_bubble",
        "bubble_card",
        99,
        default_visible=False,
        compact_visible=False,
    ),
    DashboardCardSpec(
        "sankey",
        "show_sankey",
        "sankey_card",
        99,
        default_visible=False,
        compact_visible=False,
    ),
    DashboardCardSpec(
        "heatmap",
        "show_heatmap",
        "heatmap_card",
        99,
        default_visible=False,
        compact_visible=False,
    ),
)

DASHBOARD_CARD_SPEC_BY_KEY = {spec.key: spec for spec in DASHBOARD_CARD_SPECS}

_ORDER_MIGRATIONS = (
    ("show_waterfall", "show_chart"),
    ("show_cashflow_pulse", "show_chart"),
    ("show_savings_efficiency", "show_cashflow_pulse"),
    ("show_budget_forecast", "show_waterfall"),
    ("show_cashflow_calendar", "show_budget_forecast"),
    ("show_expense_work_time", "show_donut"),
)


def migrate_saved_order(saved_order: Sequence[str] | None) -> list[str]:
    """Add cards introduced after an older dashboard order was saved."""

    result = [str(value) for value in (saved_order or ()) if str(value)]
    if not result:
        return result
    for setting_key, predecessor in _ORDER_MIGRATIONS:
        if setting_key in result:
            continue
        position = (
            result.index(predecessor) + 1 if predecessor in result else len(result)
        )
        result.insert(position, setting_key)
    return result


def order_card_entries(
    entries: Sequence[tuple[str, str, Any, int]],
    saved_order: Sequence[str] | None,
) -> list[tuple[str, str, Any, int]]:
    migrated_order = migrate_saved_order(saved_order)
    if not migrated_order:
        return list(entries)
    rank = {setting_key: index for index, setting_key in enumerate(migrated_order)}
    return sorted(entries, key=lambda entry: rank.get(entry[1], len(rank) + 1))


def arrange_dashboard_companions(
    entries: Sequence[tuple[str, Any, int]],
    columns: int,
) -> list[tuple[str, Any, int]]:
    """Keep feature cards beside their intended companion on wide layouts."""

    arranged = list(entries)
    if columns != 3:
        return arranged

    def place_companion(feature_key: str, companion_keys: frozenset[str]) -> None:
        feature_index = next(
            (index for index, entry in enumerate(arranged) if entry[0] == feature_key),
            -1,
        )
        if feature_index < 0:
            return
        companion_index = next(
            (
                index
                for index in range(feature_index + 1, len(arranged))
                if arranged[index][0] in companion_keys
            ),
            -1,
        )
        if companion_index > feature_index + 1:
            companion = arranged.pop(companion_index)
            arranged.insert(feature_index + 1, companion)

    place_companion("budget_forecast", frozenset({"goals"}))
    place_companion("donut", frozenset({"insights"}))
    place_companion("cashflow_pulse", frozenset({"savings_efficiency"}))
    return arranged


def uses_balanced_feature_columns(
    entries: Sequence[tuple[str, Any, int]],
    columns: int,
) -> bool:
    return columns == 3 and any(
        key in {"budget_forecast", "donut"} for key, _widget, _span in entries
    )


def plan_dashboard_layout(
    entries: Sequence[tuple[str, Any, int]],
    columns: int,
    *,
    balanced_feature_columns: bool,
) -> tuple[DashboardCardPlacement, ...]:
    """Calculate row/column placement without touching Qt widgets."""

    safe_columns = max(1, min(3, int(columns)))
    row = 0
    column = 0
    placements: list[DashboardCardPlacement] = []

    for index, (key, _widget, preferred_span) in enumerate(entries):
        remaining = len(entries) - index
        span = 1 if safe_columns == 1 else min(int(preferred_span), safe_columns)
        if safe_columns == 2 and key in {
            "savings_efficiency",
            "goals",
            "insights",
        }:
            # These cards contain full explanatory sentences.  A half-width
            # card at the two-column breakpoint creates tall, clipped-looking
            # text and an unused neighbouring slot, so use the complete row.
            span = 2
        elif safe_columns == 3 and key == "donut":
            span = 1
        elif (
            safe_columns == 3
            and index > 0
            and entries[index - 1][0] == "donut"
            and key == "insights"
        ):
            span = 2
        if (
            balanced_feature_columns
            and key in {"budget_forecast", "donut"}
            and column != 0
        ):
            row += 1
            column = 0
        if span > safe_columns - column:
            row += 1
            column = 0
        if remaining == 1:
            span = safe_columns if column == 0 else max(span, safe_columns - column)
        placements.append(DashboardCardPlacement(key, row, column, span))
        column += span
        if column >= safe_columns:
            row += 1
            column = 0

    return tuple(placements)


def dashboard_column_stretches(
    columns: int,
    *,
    balanced_feature_columns: bool,
) -> tuple[int, ...]:
    safe_columns = max(1, min(3, int(columns)))
    if balanced_feature_columns and safe_columns == 3:
        return (106, 57, 87)
    return tuple(1 for _ in range(safe_columns))


def dashboard_grid_spacing(columns: int) -> int:
    return 10 if columns == 1 else 14 if columns == 2 else 16


def primary_metric_columns(width: int) -> int:
    """Return the responsive column count for the primary 4 KPI dashboard cards."""
    safe_width = max(0, int(width))
    if safe_width >= 1600:
        return 4
    if safe_width >= 960:
        return 2
    return 1


metric_band_columns = primary_metric_columns
