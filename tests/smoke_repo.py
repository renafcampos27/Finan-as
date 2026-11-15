#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Read-only smoke checks for the Repo facade used by the PySide UI."""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from database.repo import Repo  # noqa: E402


def require_type(name: str, value: Any, expected: type | tuple[type, ...]) -> None:
    if not isinstance(value, expected):
        raise AssertionError(f"{name}: expected {expected}, got {type(value).__name__}")


def main() -> int:
    repo = Repo()
    start, end, _ = repo.month_period(date.today().year, date.today().month)

    checks: dict[str, Any] = {
        "safe_commit": repo.safe_commit("smoke_repo safe_commit"),
        "ai_settings": repo.ai_settings(),
        "transaction_data_quality_counts": repo.transaction_data_quality_counts(
            start, end
        ),
        "transaction_context_sample": repo.transaction_context_sample(start, end, 5),
        "uncategorized_transactions": repo.uncategorized_transactions(5),
        "categories": repo.categories(),
        "category_budgets": repo.category_budgets(),
        "report_months": repo.report_months(),
        "goals": repo.goals(limit=3),
        "holdings": repo.holdings(),
        "subscriptions": repo.subscriptions(),
        "scheduled_payments": repo.scheduled_payments(),
        "planned_purchases": repo.planned_purchases(),
        "document_attachments": repo.document_attachments(""),
        "transaction_attachment_names": repo.transaction_attachment_names(0),
        "simple_duplicate_groups": repo.simple_duplicate_groups(limit=5),
        "possible_duplicates": repo.possible_duplicates(limit=5),
        "generic_category_count": repo.generic_category_count(),
    }

    require_type("safe_commit", checks["safe_commit"], bool)
    for key in [
        "categories",
        "transaction_context_sample",
        "uncategorized_transactions",
        "report_months",
        "goals",
        "holdings",
        "subscriptions",
        "scheduled_payments",
        "planned_purchases",
        "document_attachments",
        "transaction_attachment_names",
        "simple_duplicate_groups",
        "possible_duplicates",
    ]:
        require_type(key, checks[key], list)
    require_type("category_budgets", checks["category_budgets"], dict)
    require_type("ai_settings", checks["ai_settings"], dict)
    require_type(
        "transaction_data_quality_counts",
        checks["transaction_data_quality_counts"],
        dict,
    )
    require_type("generic_category_count", checks["generic_category_count"], int)

    removed_backup_methods = {
        "backup_preferences",
        "save_backup_preferences",
        "backup_sqlite",
        "export_backup_zip",
        "restore_backup",
        "run_auto_backup_if_due",
        "run_backup_on_close",
        "prune_backups",
        "scrub_ai_secret_from_backups",
        "attach_file",
        "infer_attachment_metadata",
        "totals_for_period",
        "category_totals",
        "balance_series",
        "monthly_category_budget_rows",
        "payment_method_totals",
        "monthly_evolution",
        "annual_evolution",
        "monthly_review_data",
        "detailed_financial_score",
        "monthly_goal_progress",
        "monthly_summary",
        "subscription_monthly_amount",
        "active_subscription_monthly_total",
        "due_date_for_payment",
        "next_due_for_payment",
        "generate_due_scheduled_payments",
        "likely_recurring_expenses",
        "category_payment_totals",
        "month_income_by_payment",
        "month_expenses_by_payment",
        "monthly_split_plan",
        "save_monthly_split_plan",
        "split_candidate_transactions",
        "clear_transaction_splits",
        "recent_transaction_splits",
    }
    remaining = sorted(name for name in removed_backup_methods if hasattr(repo, name))
    if remaining:
        raise AssertionError(f"Repo still exposes service orchestration: {remaining}")
    if not hasattr(repo, "add_transaction_attachment"):
        raise AssertionError("Repo is missing attachment-record persistence")
    if not hasattr(repo, "import_transactions_batch"):
        raise AssertionError("Repo is missing atomic import persistence")
    for method in [
        "scheduled_payment_occurrence_exists",
        "add_scheduled_payment_occurrence",
        "scheduled_payment_occurrence_count",
        "update_scheduled_payment_progress",
    ]:
        if not hasattr(repo, method):
            raise AssertionError(f"Repo is missing payment persistence: {method}")

    print("Repo smoke OK")
    for key, value in checks.items():
        if isinstance(value, (list, dict)):
            print(f"{key}: {len(value)}")
        else:
            print(f"{key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
