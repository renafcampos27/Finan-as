#!/usr/bin/env python3
"""Read-only smoke checks for financial analytics orchestration."""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from database.repo import Repo
from services.analytics import AnalyticsService


def main() -> int:
    repo = Repo()
    analytics = AnalyticsService(repo)
    today = date.today()
    start = today.replace(day=1).isoformat()
    end = today.isoformat()
    month_key = today.strftime("%Y-%m")

    totals = analytics.totals_for_period(start, end)
    assert {
        "income",
        "expenses",
        "savings",
        "investments",
        "investments_available",
        "investment_as_of",
        "investment_source",
        "investment_cost",
        "investment_gain",
        "investment_return_pct",
    } <= set(totals)
    assert totals["savings"] == totals["income"] - totals["expenses"]

    categories = analytics.category_totals(start, end, 8)
    assert all(len(row) == 3 for row in categories)

    balance = analytics.balance_series(30)
    assert len(balance) == 30
    assert all(
        set(point) == {"date", "balance", "delta", "income", "expense"}
        for point in balance
    )
    assert [point["date"] for point in balance] == sorted(
        point["date"] for point in balance
    )

    budgets = analytics.monthly_category_budget_rows(month_key)
    assert all("status" in row and "utilisation" in row for row in budgets)

    payment_methods = analytics.payment_method_totals(start, end)
    assert all(len(row) == 3 for row in payment_methods)

    monthly_evolution = analytics.monthly_evolution(today.year)
    assert len(monthly_evolution) == 12

    review = analytics.monthly_review_data(month_key)
    assert {
        "start",
        "end",
        "totals",
        "prev_totals",
        "categories",
        "uncategorized",
        "no_attachment",
        "duplicates",
        "invalid_dates",
        "invalid_amounts",
    } <= set(review)

    score = analytics.detailed_financial_score(month_key)
    assert 0 <= int(score["score"]) <= 100
    assert len(score["criteria"]) == 7

    progress, current, status = analytics.monthly_goal_progress(
        {
            "month": month_key,
            "goal_type": "Poupar pelo menos",
            "category": "",
            "target_value": 100.0,
        }
    )
    assert progress >= 0
    assert current.endswith("€")
    assert status in {"Concluído", "Em progresso"}

    summaries = analytics.monthly_summary(3)
    assert len(summaries) <= 3
    assert [row["month"] for row in summaries] == sorted(
        row["month"] for row in summaries
    )

    repo.db.conn.close()
    print("Analytics service smoke OK")
    print("balance_points", len(balance))
    print("monthly_summaries", len(summaries))
    print("score_criteria", len(score["criteria"]))
    print("repo_analytics_methods_removed", True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
