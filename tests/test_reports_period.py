from __future__ import annotations

import os
from datetime import date

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from ui.pages.reports import ReportsPage
from ui.pages.movements import MovementsPage
from ui.pages.planning import BudgetsPage
from utils.helpers import rolling_month_window


def test_reports_period_window_matches_selector() -> None:
    reference = date(2026, 7, 13)

    for months in (3, 6, 12):
        start, end, days, label = ReportsPage._period_window(months, reference)
        expected_start, expected_end, expected_days = rolling_month_window(
            reference, months
        )

        assert days == expected_days
        assert start == expected_start.isoformat()
        assert end == expected_end.isoformat()
        assert label == f"últimos {months} meses"


def test_budget_month_options_are_consecutive() -> None:
    options = BudgetsPage._month_options(date(2026, 7, 13), 6)

    assert [value.strftime("%Y-%m") for value in options] == [
        "2026-07",
        "2026-06",
        "2026-05",
        "2026-04",
        "2026-03",
        "2026-02",
    ]


def test_movements_all_period_uses_the_real_financial_span() -> None:
    rows = [
        {"date": "2026-01-01", "payment_date": "2026-01-03"},
        {"date": "2026-01-20", "payment_date": "2026-01-12"},
    ]

    assert MovementsPage._filtered_day_count(rows) == 10
    assert MovementsPage._filtered_day_count(rows, "2026-01-01", "2026-01-31") == 31
