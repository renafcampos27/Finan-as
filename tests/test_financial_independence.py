from __future__ import annotations

import pytest

from services.investment_projection import build_financial_independence_outlook


def _month(
    key: str,
    *,
    income: float = 2_500.0,
    expenses: float = 1_500.0,
    partial: bool = False,
) -> dict:
    return {
        "month": key,
        "income": income,
        "expenses": expenses,
        "savings": income - expenses,
        "partial": partial,
    }


def test_financial_independence_never_invents_an_expense_baseline() -> None:
    result = build_financial_independence_outlook(
        [_month("2026-06"), _month("2026-07", partial=True)],
        investable_capital=50_000,
    )

    assert result["available"] is False
    assert result["months_used"] == 1
    assert "3 meses completos" in result["reason"]
    assert "target" not in result


def test_financial_independence_uses_complete_months_and_investable_capital() -> None:
    rows = [
        _month("2026-03", expenses=1_200),
        _month("2026-04", expenses=1_500),
        _month("2026-05", expenses=1_800),
        _month("2026-06", expenses=1_500),
        _month("2026-07", expenses=9_999, partial=True),
    ]

    result = build_financial_independence_outlook(
        rows,
        investable_capital=75_000,
        annual_rate_pct=5,
        inflation_pct=2,
        tax_pct=28,
    )

    assert result["available"] is True
    assert result["months_used"] == 4
    assert result["average_expenses"] == pytest.approx(1_500)
    assert result["target"] == pytest.approx(450_000)
    assert result["progress_pct"] == pytest.approx(75_000 / 450_000 * 100)
    assert result["period_end"] == "2026-06"
    assert result["annual_real_rate_pct"] < 5
    assert result["months_to_target"] is not None
