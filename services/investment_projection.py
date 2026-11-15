"""Pure investment forecast calculations used by analytics and the UI."""

from __future__ import annotations

from typing import Any

from services.finance_math import (
    effective_annual_to_periodic_rate,
    periodic_discount_factor,
    present_value_of_contributions,
)
from utils.helpers import safe_float, strict_float


def build_financial_independence_outlook(
    monthly_rows: list[dict[str, Any]],
    *,
    investable_capital: float,
    annual_rate_pct: float = 5.0,
    inflation_pct: float = 2.0,
    tax_pct: float = 28.0,
    contribution_fee_pct: float = 0.0,
    withdrawal_rate_pct: float = 4.0,
) -> dict[str, Any]:
    """Estimate financial independence from observed complete months.

    The result is deliberately unavailable when fewer than three complete
    months with activity exist. This avoids manufacturing an expense baseline.
    """
    usable = [
        row
        for row in monthly_rows
        if not bool(row.get("partial"))
        and (
            safe_float(row.get("income")) > 0
            or safe_float(row.get("expenses", row.get("expense"))) > 0
        )
    ][-12:]
    if len(usable) < 3:
        return {
            "available": False,
            "reason": (
                "São necessários pelo menos 3 meses completos com movimentos "
                "para calcular uma média credível."
            ),
            "months_used": len(usable),
        }

    average_expenses = sum(
        max(0.0, safe_float(row.get("expenses", row.get("expense")))) for row in usable
    ) / len(usable)
    average_income = sum(
        max(0.0, safe_float(row.get("income"))) for row in usable
    ) / len(usable)
    average_savings = average_income - average_expenses
    if average_expenses <= 0:
        return {
            "available": False,
            "reason": "Os meses completos não contêm despesas válidas.",
            "months_used": len(usable),
        }

    withdrawal_rate = max(1.0, min(10.0, safe_float(withdrawal_rate_pct, 4.0)))
    target = average_expenses * 12.0 / (withdrawal_rate / 100.0)
    capital = max(0.0, safe_float(investable_capital))
    progress_pct = capital / target * 100.0 if target else 0.0

    nominal_rate = safe_float(annual_rate_pct)
    after_tax_rate = (
        nominal_rate
        - max(0.0, nominal_rate) * max(0.0, min(100.0, safe_float(tax_pct))) / 100.0
    )
    inflation = max(-20.0, min(100.0, safe_float(inflation_pct)))
    real_rate = (1.0 + after_tax_rate / 100.0) / max(
        1e-9, 1.0 + inflation / 100.0
    ) - 1.0
    monthly_rate = effective_annual_to_periodic_rate(real_rate * 100.0)

    months_to_target: int | None = 0 if capital >= target else None
    if capital < target and average_savings > 0:
        projected = capital
        net_monthly_savings = average_savings * (
            1.0 - max(0.0, min(100.0, safe_float(contribution_fee_pct))) / 100.0
        )
        for month_index in range(1, 100 * 12 + 1):
            projected = projected * (1.0 + monthly_rate) + net_monthly_savings
            if projected >= target:
                months_to_target = month_index
                break

    first_month = str(usable[0].get("month") or "")
    last_month = str(usable[-1].get("month") or "")
    confidence = (
        "alta" if len(usable) >= 12 else "média" if len(usable) >= 6 else "baixa"
    )
    return {
        "available": True,
        "months_used": len(usable),
        "period_start": first_month,
        "period_end": last_month,
        "confidence": confidence,
        "average_expenses": average_expenses,
        "average_income": average_income,
        "average_savings": average_savings,
        "investable_capital": capital,
        "withdrawal_rate_pct": withdrawal_rate,
        "target": target,
        "progress_pct": progress_pct,
        "annual_real_rate_pct": real_rate * 100.0,
        "months_to_target": months_to_target,
    }


def build_investment_projection(
    contribution: float,
    annual_rate_pct: float = 5.0,
    years: int = 5,
    *,
    mode: str = "monthly",
    initial_capital: float = 0.0,
    inflation_pct: float = 0.0,
    tax_pct: float = 0.0,
    fee_pct: float = 0.0,
    scenario_spread_pct: float = 2.0,
) -> dict[str, Any]:
    """Project nominal and present-day values without mixing their bases."""
    for field, value in (
        ("reforço", contribution), ("capital inicial", initial_capital),
        ("retorno anual", annual_rate_pct), ("inflação", inflation_pct),
        ("imposto", tax_pct), ("comissão", fee_pct),
        ("amplitude do cenário", scenario_spread_pct),
    ):
        strict_float(value, field_name=field)
    count = strict_float(years, field_name="anos")
    if count != int(count):
        raise ValueError("Anos deve ser um número inteiro.")
    amount = max(0.0, safe_float(contribution))
    initial = max(0.0, safe_float(initial_capital))
    annual_rate = max(-99.0, min(1_000.0, safe_float(annual_rate_pct)))
    inflation = max(-20.0, min(100.0, safe_float(inflation_pct)))
    tax = max(0.0, min(100.0, safe_float(tax_pct)))
    fee = max(0.0, min(100.0, safe_float(fee_pct)))
    spread = max(0.0, min(50.0, safe_float(scenario_spread_pct)))
    horizon = max(1, min(50, int(years or 5)))
    normalised_mode = (
        "single"
        if str(mode or "monthly").strip().casefold()
        in {"single", "unique", "único", "unico"}
        else "monthly"
    )

    def projected_value(rate_pct: float, months: int) -> dict[str, float]:
        monthly_rate = effective_annual_to_periodic_rate(max(-99.0, rate_pct))
        growth = (1.0 + monthly_rate) ** months
        if normalised_mode == "single":
            fees_paid = amount * fee / 100.0
            nominal_contributions = initial + amount
            real_contributions = nominal_contributions
            before_tax = (initial + amount - fees_paid) * growth
            real_fees_paid = fees_paid
        else:
            fees_paid = amount * fee / 100.0 * months
            nominal_contributions = initial + amount * months
            real_contributions = initial + present_value_of_contributions(
                amount,
                months,
                inflation,
            )
            net_monthly = amount * (1.0 - fee / 100.0)
            if abs(monthly_rate) < 1e-12:
                annuity = net_monthly * months
            else:
                annuity = net_monthly * (growth - 1.0) / monthly_rate
            before_tax = initial * growth + annuity
            real_fees_paid = present_value_of_contributions(
                amount * fee / 100.0,
                months,
                inflation,
            )
        for field, value in (
            ("capital projetado", before_tax), ("capital contribuído", nominal_contributions),
            ("contribuições reais", real_contributions), ("comissões", fees_paid),
        ):
            strict_float(value, field_name=field)
        taxable_gain = max(0.0, before_tax - nominal_contributions)
        taxes_paid = taxable_gain * tax / 100.0
        nominal_after_tax = before_tax - taxes_paid
        inflation_factor = periodic_discount_factor(inflation, months)
        real_after_tax = strict_float(
            nominal_after_tax / max(1e-12, inflation_factor),
            field_name="capital real projetado",
        )
        nominal_gain = nominal_after_tax - nominal_contributions
        real_gain = real_after_tax - real_contributions
        return {
            "invested": nominal_contributions,
            "invested_nominal": nominal_contributions,
            "invested_real": real_contributions,
            "nominal_contributions": nominal_contributions,
            "real_contributions": real_contributions,
            "projected": real_after_tax,
            "projected_real": real_after_tax,
            "nominal": nominal_after_tax,
            "projected_nominal": nominal_after_tax,
            "gain": real_gain,
            "gain_real": real_gain,
            "gain_nominal": nominal_gain,
            "return_pct": (
                real_gain / real_contributions * 100.0 if real_contributions else 0.0
            ),
            "return_real_pct": (
                real_gain / real_contributions * 100.0 if real_contributions else 0.0
            ),
            "return_nominal_pct": (
                nominal_gain / nominal_contributions * 100.0
                if nominal_contributions
                else 0.0
            ),
            "fees_paid": fees_paid,
            "fees_paid_nominal": fees_paid,
            "fees_paid_real": real_fees_paid,
            "taxes_paid": taxes_paid,
            "taxes_paid_real": taxes_paid / max(1e-12, inflation_factor),
        }

    rows: list[dict[str, Any]] = []
    for year in range(1, horizon + 1):
        months = year * 12
        pessimistic = projected_value(annual_rate - spread, months)
        base = projected_value(annual_rate, months)
        optimistic = projected_value(annual_rate + spread, months)
        rows.append(
            {
                "year": year,
                "months": months,
                "invested": base["invested"],
                "invested_nominal": base["invested_nominal"],
                "invested_real": base["invested_real"],
                "nominal_contributions": base["nominal_contributions"],
                "real_contributions": base["real_contributions"],
                "projected": base["projected"],
                "projected_base": base["projected"],
                "projected_pessimistic": pessimistic["projected"],
                "projected_optimistic": optimistic["projected"],
                "nominal_base": base["nominal"],
                "projected_nominal": base["projected_nominal"],
                "projected_real": base["projected_real"],
                "gain": base["gain"],
                "gain_real": base["gain_real"],
                "gain_nominal": base["gain_nominal"],
                "return_pct": base["return_pct"],
                "return_real_pct": base["return_real_pct"],
                "return_nominal_pct": base["return_nominal_pct"],
                "return_pct_pessimistic": pessimistic["return_pct"],
                "return_pct_optimistic": optimistic["return_pct"],
                "fees_paid": base["fees_paid"],
                "fees_paid_nominal": base["fees_paid_nominal"],
                "fees_paid_real": base["fees_paid_real"],
                "taxes_paid": base["taxes_paid"],
                "taxes_paid_real": base["taxes_paid_real"],
            }
        )
    return {
        "mode": normalised_mode,
        "contribution": amount,
        "monthly_contribution": amount if normalised_mode == "monthly" else 0.0,
        "initial_capital": initial,
        "annual_rate_pct": annual_rate,
        "inflation_pct": inflation,
        "tax_pct": tax,
        "fee_pct": fee,
        "scenario_spread_pct": spread,
        "years": horizon,
        "tax_timing": "hypothetical_liquidation_each_horizon",
        "tax_reduces_reinvested_state": False,
        "contribution_timing": "month_end",
        "return_basis": "nominal",
        "rows": rows,
        "method_label": (
            "Taxa anual efetiva convertida por capitalização composta; cenários a ±"
            f"{spread:.1f} p.p. O capital nominal e os euros de hoje são "
            "apresentados separadamente; cada reforço é corrigido pela inflação "
            "apenas desde o mês em que entra; impostos estimados na liquidação. "
            "Simulação ilustrativa."
        ),
    }
