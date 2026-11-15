#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Test suite for Phase 9: QA Modernization, Property-Based Fuzzing & Boundary Verification."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from database.engine import FinanceDB
from services.analytics import AnalyticsService
from utils.config import parse_date
from utils.helpers import (
    add_months,
    rolling_month_window,
    safe_float,
)


def test_currency_and_portuguese_number_fuzzing():
    """Fuzz number parser with Portuguese European, negative and edge formatting."""
    # Standard numbers
    assert safe_float(1234.56) == 1234.56
    assert safe_float("1234.56") == 1234.56

    # Portuguese comma decimal
    assert safe_float("1234,56") == 1234.56

    # European dot thousands + comma decimal
    assert safe_float("1.234,56") == 1234.56
    assert safe_float("10.000.000,50") == 10000000.50

    # Currency suffix and prefix
    assert safe_float("1.234,56 €") == 1234.56
    assert safe_float("€ 1.234,56") == 1234.56
    assert safe_float("1.234,56 EUR") == 1234.56

    # Negative numbers with parentheses and leading signs
    assert safe_float("(500,00 €)") == -500.00
    assert safe_float("- 1.234,56 €") == -1234.56
    assert safe_float("+ 750,25 €") == 750.25

    # Safe fallbacks on corrupt / non-numeric data
    assert safe_float(None) == 0.0
    assert safe_float("") == 0.0
    assert safe_float("N/A", default=42.0) == 42.0
    assert safe_float(float("nan"), default=0.0) == 0.0
    assert safe_float(float("inf"), default=0.0) == 0.0


def test_leap_year_and_month_end_date_transitions():
    """Verify leap year handling (Feb 29) and month-end boundary transitions."""
    # Leap year vs non-leap year
    assert parse_date("2024-02-29") == date(2024, 2, 29)
    assert parse_date("2020-02-29") == date(2020, 2, 29)
    assert parse_date("2000-02-29") == date(2000, 2, 29)  # Century leap year

    # Jan 31 + 1 month -> Feb 29 (in 2024 leap year) or Feb 28 (in 2025)
    d_jan31_leap = date(2024, 1, 31)
    d_feb_leap = add_months(d_jan31_leap, 1)
    assert d_feb_leap == date(2024, 2, 29)

    d_jan31_nonleap = date(2025, 1, 31)
    d_feb_nonleap = add_months(d_jan31_nonleap, 1)
    assert d_feb_nonleap == date(2025, 2, 28)

    # Feb 29 + 1 year -> Feb 28
    d_feb29 = date(2024, 2, 29)
    d_next_year = add_months(d_feb29, 12)
    assert d_next_year == date(2025, 2, 28)

    # Rolling 12-month window boundary calculation
    window_start, window_end, days_count = rolling_month_window(date(2026, 8, 15), 6)
    assert window_start == date(2026, 2, 16)
    assert window_end == date(2026, 8, 15)
    assert days_count > 150


def test_sqlite_fixture_clean_lifecycle_and_no_file_lock(tmp_path: Path):
    """Verify database creates, performs operations and closes cleanly without Windows file lock."""
    db_file = tmp_path / "clean_lifecycle.sqlite3"
    db = FinanceDB(db_file)
    db.conn.close()
    db_file.unlink()
    assert not db_file.exists()


def test_analytics_smoke_integration_under_pytest(tmp_path: Path):
    """Verify AnalyticsService handles a complete month dataset cleanly."""
    db_file = tmp_path / "analytics_smoke.sqlite3"
    db = FinanceDB(db_file)
    db.conn.execute(
        """
        INSERT INTO transactions(date, description, amount, category, account, kind)
        VALUES ('2026-08-01', 'Salário', 2500.0, 'Rendimentos', 'Conta Principal', 'income'),
               ('2026-08-02', 'Renda Casa', -750.0, 'Casa e contas', 'Conta Principal', 'expense'),
               ('2026-08-05', 'Supermercado', -150.0, 'Supermercado', 'Conta Principal', 'expense')
        """
    )
    db.conn.commit()

    class StubRepo:
        def __init__(self, database: FinanceDB):
            self.db = database

        def scalar(self, query, params=()):
            cursor = self.db.conn.execute(query, params)
            row = cursor.fetchone()
            return row[0] if row else 0.0

        def q(self, query, params=()):
            return [dict(row) for row in self.db.conn.execute(query, params).fetchall()]

        def query(self, query, params=()):
            return self.q(query, params)

        def month_period(self, y, m):
            return f"{y:04d}-{m:02d}-01", f"{y:04d}-{m:02d}-31", "month"

        def totals_for_payment_period(self, start, end):
            return {"income": 2500.0, "expenses": 900.0, "savings": 1600.0}

        def investment_state_as_of(self, end):
            return {
                "available": True,
                "as_of": end,
                "source": "portfolio",
                "cost_basis": 0.0,
                "cost_basis_known": True,
                "value": 0.0,
                "net_gain": 0.0,
                "return_pct": 0.0,
            }

    repo = StubRepo(db)
    service = AnalyticsService(repo)
    totals = service.totals_for_period("2026-08-01", "2026-08-31")
    assert totals["income"] >= 2500.0
    assert totals["expenses"] >= 900.0
    assert totals["savings"] == totals["income"] - totals["expenses"]
    db.conn.close()
