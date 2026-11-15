"""RECUR-01: subscription cadence + day-31 anchor regressions.

Part 1: two same-amount purchases far apart must NOT be reported as a
forgotten subscription; a regular monthly cadence still is.
Part 2: the day-31 anchor must survive short months — February lands on
28/29 but March/April return to 31 (30 for 30-day months).  The current
helpers already implement ``min(anchor_day, days_in_month)`` via
``add_months`` and ``due_date_for_payment`` computes every occurrence
from the stored anchor; these tests pin that behaviour so a future
"advance from last occurrence" refactor cannot reintroduce the drift.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest

from database.engine import FinanceDB
from database.repo.main_repo import Repo
from services.analytics import AnalyticsService
from services.change_journal import ChangeJournalService
from services.payments import PaymentsService
from utils.helpers import add_months


@pytest.fixture()
def env(tmp_path: Path):
    database = FinanceDB(tmp_path / "recur01.sqlite3")
    repo = Repo(database)
    repo.db.seed_if_empty()
    try:
        for table in (
            "transaction_splits",
            "transactions",
            "subscriptions",
            "scheduled_payments",
            "financial_accounts",
        ):
            repo.db.conn.execute(f"DELETE FROM {table}")
        repo.db.conn.commit()
        journal = ChangeJournalService(repo)
        payments = PaymentsService(repo, journal)
        analytics = AnalyticsService(repo, payments)
        yield repo, analytics, database
    finally:
        database.close()


def _add_expense(repo: Repo, d: str, desc: str, amount: float) -> None:
    repo.add_transaction(d, desc, -amount, "Assinaturas", "Conta principal")


def test_two_isolated_same_amount_purchases_are_not_subscriptions(env) -> None:
    """50€ twice, 75 days apart: not a monthly cadence -> not a suspect."""

    repo, analytics, _db = env
    today = date.today()
    _add_expense(repo, (today - timedelta(days=80)).isoformat(), "Loja A", 50.0)
    _add_expense(repo, (today - timedelta(days=5)).isoformat(), "Loja A", 50.0)  # 75d gap

    suspects = analytics.detect_recurring_subscriptions()
    names = {str(s["name"]) for s in suspects}
    assert "Loja A" not in names, f"false positive: {names}"


def test_regular_monthly_cadence_is_still_detected(env) -> None:
    """Three regular ~30-day gaps with stdev <= 3 remain a suspect.

    Dates are anchored to *today* minus ~75 days so the rows fall inside
    the detector's 90-day SQL window regardless of when the suite runs.
    """

    repo, analytics, _db = env
    today = date.today()
    d0 = today - timedelta(days=75)
    d1 = today - timedelta(days=45)
    d2 = today - timedelta(days=15)
    _add_expense(repo, d0.isoformat(), "Gym Premium", 35.0)
    _add_expense(repo, d1.isoformat(), "Gym Premium", 35.0)
    _add_expense(repo, d2.isoformat(), "Gym Premium", 35.0)

    suspects = analytics.detect_recurring_subscriptions()
    names = {str(s["name"]) for s in suspects}
    assert "Gym Premium" in names, f"regular cadence missed: {names}"


def test_scheduled_payment_day31_anchor_survives_short_months() -> None:
    """Jan 31 anchor: Feb yields 28, March back to 31, April to 30."""

    row = {
        "id": 1,
        "name": "Renda 31",
        "start_date": "2026-01-31",
        "debit_day": 31,
        "active": 1,
        "months_paid": 0,
    }
    dues = [PaymentsService.due_date_for_payment(row, i) for i in range(4)]
    assert dues == [
        date(2026, 1, 31),
        date(2026, 2, 28),
        date(2026, 3, 31),
        date(2026, 4, 30),
    ]


def test_subscription_advance_from_short_month_restores_anchor() -> None:
    """add_months from a Feb 28 occurrence with anchor 31 gives Mar 31."""

    assert add_months(date(2026, 2, 28), 1, 31) == date(2026, 3, 31)
    assert add_months(date(2026, 3, 31), 1, 31) == date(2026, 4, 30)
    assert add_months(date(2026, 4, 30), 1, 31) == date(2026, 5, 31)


def test_subscription_window_without_renewal_day_restores_day31() -> None:
    """RECUR-01: rows without renewal_day must derive the anchor from the
    stored start_date, never from ``reference.day`` (which changes on every
    run) nor accumulate from a short-month clamp (Feb 28 -> stuck at 28)."""

    row = {
        "id": 9,
        "name": "Net fibra",
        "amount": 45.0,
        "period": "Mensal",
        "start_date": "2026-01-31",
        "last_used_date": "2026-01-31",
        "next_renewal_date": "",
    }
    reference = date(2026, 3, 15)

    # Jan 31 + 1 month clamps to Feb 28 for the overdue charge...
    assert PaymentsService.subscription_scheduled_due(row, reference) == date(
        2026, 2, 28
    )
    # ...but the window's next charge restores the day-31 anchor in March.
    window = PaymentsService.subscription_due_window(row, reference)
    assert window == (date(2026, 2, 28), date(2026, 3, 31), 0)
    assert PaymentsService.advance_subscription_due(row, date(2026, 2, 28)) == date(
        2026, 3, 31
    )


def test_subscription_window_repairs_already_drifted_date() -> None:
    """RECUR-01: a drifted stored date (28) with anchor 31 must not keep
    compounding the drift: the latest due charge returns to day 31."""

    row = {
        "id": 10,
        "name": "Renda",
        "amount": 750.0,
        "period": "Mensal",
        "renewal_day": 31,
        "next_renewal_date": "2025-11-28",
        "last_used_date": "",
    }

    latest, next_due, skipped = PaymentsService.subscription_due_window(
        row, date(2026, 4, 15)
    )

    assert latest == date(2026, 3, 31), f"drift persisted: {latest}"
    assert next_due == date(2026, 4, 30)
    assert skipped == 4


def test_subscription_scheduled_due_uses_stored_anchor(env) -> None:
    """The stored renewal_day (31) wins over any short-month drift."""

    repo, _analytics, _db = env
    repo.add_subscription(
        "Renda mensal",
        750.0,
        "Casa e contas",
        31,  # renewal_day anchor
        "Mensal",
        last_used_date="2026-02-28",
    )
    rows = repo.subscriptions(active_only=True)
    assert rows, "subscription must be persisted"
    due = PaymentsService.subscription_scheduled_due(rows[0], date(2026, 3, 10))
    assert due == date(2026, 3, 31), f"anchor lost: {due}"
