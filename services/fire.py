"""Financial Independence, Retire Early (FIRE) & Decumulation Planning Engine.

Provides Trinity Study Safe Withdrawal Rate (SWR) calculations, Guyton-Klinger
dynamic withdrawal guardrails, Coast/Barista/Lean/Fat FIRE milestones, and
Sequence of Returns Risk (SRR) early-drawdown stress simulations.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

from utils.helpers import safe_float, strict_float


@dataclass(slots=True)
class FireMilestone:
    name: str
    target_capital: float
    annual_spending: float
    withdrawal_rate_pct: float
    progress_pct: float
    years_to_goal: Optional[float]
    description: str


@dataclass(slots=True)
class SrrSimulationResult:
    scenario_name: str
    starting_capital: float
    annual_withdrawal: float
    duration_years: int
    portfolio_survived: bool
    final_capital_nominal: float
    final_capital_real: float
    depletion_year: Optional[int]
    yearly_balances: list[float]
    return_basis: str = "real"
    withdrawal_basis: str = "constant_today_euros"
    balance_basis: str = "today_euros"
    scenario_source: str = "illustrative_real_stress"
    scenario_returns_pct: list[float] = field(default_factory=list)


class FirePlanningEngine:
    """Engine for FIRE calculations, dynamic guardrails, and decumulation simulations."""

    @staticmethod
    def calculate_fire_milestones(
        *,
        current_invested: float,
        annual_expenses: float,
        annual_savings: float,
        real_growth_rate_pct: float = 4.0,
        swr_pct: float = 4.0,
        years_to_retirement: float = 25.0,
    ) -> dict[str, FireMilestone]:
        """Calculate LeanFIRE, StandardFIRE, FatFIRE, BaristaFIRE, and CoastFIRE milestones."""
        for name, value in (
            ("capital atual", current_invested), ("despesas anuais", annual_expenses),
            ("poupança anual", annual_savings), ("crescimento real", real_growth_rate_pct),
            ("taxa objetivo", swr_pct), ("anos até à reforma", years_to_retirement),
        ):
            strict_float(value, field_name=name)
        invested = max(0.0, safe_float(current_invested))
        expenses = max(0.0, safe_float(annual_expenses))
        savings = max(0.0, safe_float(annual_savings))
        swr = max(1.0, min(10.0, safe_float(swr_pct, 4.0)))
        r = safe_float(real_growth_rate_pct) / 100.0

        def calc_years(target: float) -> Optional[float]:
            if invested >= target:
                return 0.0
            if savings <= 0:
                if invested > 0 and r > 0 and target > invested:
                    try:
                        return max(0.0, math.log(target / invested) / math.log(1.0 + r))
                    except (ValueError, ZeroDivisionError):
                        return None
                return None
            # N = ln((Target*r + Savings) / (Invested*r + Savings)) / ln(1+r)
            if abs(r) < 1e-6:
                return (target - invested) / savings
            try:
                top = target * r + savings
                bottom = invested * r + savings
                if top <= 0 or bottom <= 0:
                    return None
                return max(0.0, math.log(top / bottom) / math.log(1.0 + r))
            except (ValueError, ZeroDivisionError):
                return None

        def _calc_progress(target: float) -> float:
            if target <= 0.0:
                return 100.0 if invested >= 0.0 else 0.0
            return round(invested / target * 100.0, 2)

        # 1. Standard FIRE: 100% of current annual expenses
        std_target = round(expenses / (swr / 100.0), 2) if swr else 0.0
        standard = FireMilestone(
            name="Standard FIRE",
            target_capital=std_target,
            annual_spending=round(expenses, 2),
            withdrawal_rate_pct=swr,
            progress_pct=_calc_progress(std_target),
            years_to_goal=calc_years(std_target),
            description="Cobre 100% do teu padrão de despesas atual em segurança.",
        )

        # 2. Lean FIRE: 75% of current expenses (essentials only)
        lean_spending = round(expenses * 0.75, 2)
        lean_target = round(lean_spending / (swr / 100.0), 2) if swr else 0.0
        lean = FireMilestone(
            name="Lean FIRE",
            target_capital=lean_target,
            annual_spending=lean_spending,
            withdrawal_rate_pct=swr,
            progress_pct=_calc_progress(lean_target),
            years_to_goal=calc_years(lean_target),
            description="Cobre 75% dos gastos atuais (orçamento essencial e frugal).",
        )

        # 3. Fat FIRE: 135% of current expenses (lifestyle inflation / abundance)
        fat_spending = round(expenses * 1.35, 2)
        fat_target = round(fat_spending / (swr / 100.0), 2) if swr else 0.0
        fat = FireMilestone(
            name="Fat FIRE",
            target_capital=fat_target,
            annual_spending=fat_spending,
            withdrawal_rate_pct=swr,
            progress_pct=_calc_progress(fat_target),
            years_to_goal=calc_years(fat_target),
            description="Cobre 135% dos gastos atuais com margem para viagens e luxos.",
        )

        # 4. Barista FIRE: 50% from portfolio + 50% from part-time work
        barista_spending = round(expenses * 0.50, 2)
        barista_target = round(barista_spending / (swr / 100.0), 2) if swr else 0.0
        barista = FireMilestone(
            name="Barista FIRE",
            target_capital=barista_target,
            annual_spending=barista_spending,
            withdrawal_rate_pct=swr,
            progress_pct=_calc_progress(barista_target),
            years_to_goal=calc_years(barista_target),
            description="50% dos gastos cobertos por investimentos; restante por trabalho parcial.",
        )

        # 5. Coast FIRE: existing capital will grow to target by retirement age without new contributions
        horizon_years = max(0.0, safe_float(years_to_retirement, 25.0))
        discount = math.pow(1.0 + r, horizon_years) if (1.0 + r) > 0 else 1.0
        coast_target = round(std_target / discount, 2) if discount > 0 else std_target
        coast = FireMilestone(
            name="Coast FIRE",
            target_capital=coast_target,
            annual_spending=round(expenses, 2),
            withdrawal_rate_pct=swr,
            progress_pct=_calc_progress(coast_target),
            years_to_goal=calc_years(coast_target),
            description=f"Capital necessário hoje para atingir o objetivo em {horizon_years:g} anos sem novos reforços.",
        )


        return {
            "standard": standard,
            "lean": lean,
            "fat": fat,
            "barista": barista,
            "coast": coast,
        }

    @classmethod
    def simulate_guyton_klinger_drawdown(
        cls,
        *,
        initial_capital: float,
        initial_annual_withdrawal: float,
        annual_returns: Sequence[float],
        annual_inflation_rates: Sequence[float],
        initial_swr_pct: float | None = None,
        min_floor_pct_of_initial: float = 0.80,
    ) -> list[dict[str, Any]]:
        """Apply the documented GK-2006 spending variant to one nominal portfolio.

        Withdraw at each year's start; apply that year's nominal return afterward.
        Year two onwards uses prior-year inflation, conditional negative-return
        freezes, a fixed actual initial-rate anchor, and 20%/10% guardrails.
        Only preservation expires in the final fifteen planned years. The optional
        real-spending floor is an application extension, not a paper result.
        """
        capital = strict_float(initial_capital, field_name="capital inicial")
        withdrawal = strict_float(initial_annual_withdrawal, field_name="levantamento inicial")
        floor_pct = strict_float(min_floor_pct_of_initial, field_name="piso do levantamento")
        if capital < 0 or withdrawal < 0 or not 0 <= floor_pct <= 1:
            raise ValueError("Capital, levantamento e piso devem ser não negativos; o piso fica entre 0 e 1.")
        returns = [strict_float(value, field_name="retorno nominal") for value in annual_returns]
        inflation_rates = [strict_float(value, field_name="inflação anual") for value in annual_inflation_rates]
        if len(returns) != len(inflation_rates):
            raise ValueError("Retornos e inflação devem ter o mesmo número de anos.")
        if any(value < -100 for value in returns) or any(value <= -100 for value in inflation_rates):
            raise ValueError("Retornos não podem ser inferiores a -100% e a inflação deve exceder -100%.")
        initial_rate = strict_float(withdrawal / capital * 100, field_name="taxa inicial calculada") if capital else 0.0
        if initial_swr_pct is not None:
            supplied_rate = strict_float(initial_swr_pct, field_name="taxa de levantamento inicial")
            if supplied_rate < 0 or (capital > 0 and not math.isclose(supplied_rate, initial_rate, rel_tol=1e-9, abs_tol=1e-9)):
                raise ValueError("A taxa inicial deve corresponder ao levantamento inicial dividido pelo capital inicial.")
        base_floor = withdrawal * floor_pct
        cumulative_inflation = 1.0
        horizon = len(returns)
        history: list[dict[str, Any]] = []
        for year_idx, nominal_return_pct in enumerate(returns):
            previous_inflation = inflation_rates[year_idx - 1] / 100 if year_idx else 0.0
            if year_idx:
                cumulative_inflation *= 1.0 + previous_inflation
                proposed = withdrawal * (1.0 + previous_inflation)
                proposed_rate = proposed / capital * 100 if capital > 0 else float("inf")
                freeze = (
                    returns[year_idx - 1] < 0
                    and previous_inflation > 0
                    and proposed_rate > initial_rate
                )
                applied_inf = 0.0 if freeze else previous_inflation
                inf_status = "Congelada (Retorno Negativo Anterior e Taxa Elevada)" if freeze else "Normal"
            else:
                applied_inf = 0.0
                inf_status = "Ano Inicial (Sem Ajuste)"
            withdrawal *= 1.0 + applied_inf
            floor_amount = strict_float(base_floor * cumulative_inflation, field_name="piso nominal calculado")
            current_rate = withdrawal / capital * 100 if capital > 0 else 0.0
            preservation_active = horizon - year_idx > 15
            rule_triggered = f"Inflação {inf_status}"
            if capital > 0 and initial_rate > 0:
                if preservation_active and current_rate > initial_rate * 1.2:
                    cut_amount = withdrawal * 0.90
                    withdrawal = max(cut_amount, floor_amount)
                    rule_triggered = (
                        "Preservação de Capital (-10%)" if cut_amount >= floor_amount
                        else "Preservação de Capital (Piso Mínimo Ativado)"
                    )
                elif current_rate < initial_rate * 0.8:
                    withdrawal *= 1.10
                    rule_triggered = "Prosperidade (+10%)"
            withdrawal = strict_float(withdrawal, field_name="levantamento calculado")
            capital_before_withdrawal = capital
            actual_withdrawal = min(capital, withdrawal)
            capital = strict_float(
                (capital - actual_withdrawal) * (1.0 + nominal_return_pct / 100),
                field_name="capital nominal calculado",
            )
            history.append({
                "year": year_idx + 1,
                "return_pct": nominal_return_pct,
                "return_basis": "nominal",
                "inflation_pct": inflation_rates[year_idx],
                "prior_year_inflation_pct": previous_inflation * 100,
                "applied_inflation_pct": applied_inf * 100,
                "inflation_status": inf_status,
                "withdrawal": actual_withdrawal,
                "requested_withdrawal": withdrawal,
                "unmet_withdrawal": max(0.0, withdrawal - actual_withdrawal),
                "withdrawal_rate_pct": actual_withdrawal / capital_before_withdrawal * 100 if capital_before_withdrawal > 0 else 0.0,
                "initial_withdrawal_rate_pct": initial_rate,
                "withdrawal_floor_nominal": floor_amount,
                "capital_preservation_active": preservation_active,
                "remaining_years_including_current": horizon - year_idx,
                "ending_capital": capital,
                "ending_capital_basis": "nominal_euros",
                "model_variant": "gk2006_single_portfolio_real_floor_v1",
                "rule_triggered": rule_triggered if capital_before_withdrawal > 0 else "Capital Esgotado",
            })
        return history

    @staticmethod
    def simulate_sequence_of_returns_risk(
        *,
        initial_capital: float,
        initial_annual_withdrawal: float,
        duration_years: int = 30,
        average_real_return_pct: float = 5.0,
        early_crash_scenario: str = "dotcom_2000",
        annual_inflation_pct: float = 2.5,
    ) -> SrrSimulationResult:
        """Stress-test in constant purchasing-power euros.

        Named shocks are illustrative real-return assumptions inspired by crisis
        episodes, not an audited historical nominal-return/CPI dataset. Withdrawals
        and balances remain real; inflation converts only the terminal balance.
        """
        capital = max(0.0, strict_float(initial_capital, field_name="capital inicial"))
        withdrawal = max(0.0, strict_float(initial_annual_withdrawal, field_name="levantamento inicial"))
        raw_years = strict_float(duration_years, field_name="horizonte")
        avg_ret = strict_float(average_real_return_pct, field_name="retorno real médio")
        inflation_pct = strict_float(annual_inflation_pct, field_name="inflação anual")
        if capital < 0 or withdrawal < 0 or raw_years != int(raw_years) or not 1 <= raw_years <= 50:
            raise ValueError("Capital e levantamento devem ser não negativos e o horizonte inteiro deve ficar entre 1 e 50 anos.")
        if avg_ret < -100 or inflation_pct <= -100:
            raise ValueError("Retorno real deve ser pelo menos -100% e inflação deve exceder -100%.")
        years = int(raw_years)
        scenarios_patterns: dict[str, list[float]] = {
            "dotcom_2000": [-10.0, -12.0, -22.0, 28.0, 10.0, 4.0, 15.0],
            "gfc_2008": [-37.0, 26.0, 15.0, 2.0, 16.0, 32.0, 13.0],
            "stagflation_1973": [-15.0, -25.0, 35.0, 20.0, -7.0, 6.0, 18.0],
            "baseline_steady": [avg_ret] * 7,
        }

        if early_crash_scenario not in scenarios_patterns:
            raise ValueError("Cenário de stress desconhecido.")
        pattern = scenarios_patterns[early_crash_scenario]
        scenario_source = "constant_real_assumption" if early_crash_scenario == "baseline_steady" else "illustrative_real_stress"
        modeled_returns = [pattern[y] if y < len(pattern) else avg_ret for y in range(years)]
        yearly_balances: list[float] = [capital]
        depleted_year: Optional[int] = None

        if capital <= 0:
            # BUG-SIM-03: a zero/negative portfolio is immediately depleted —
            # survival must be 0%, never 100%, regardless of the withdrawal.
            return SrrSimulationResult(
                scenario_name=early_crash_scenario,
                starting_capital=capital,
                annual_withdrawal=initial_annual_withdrawal,
                duration_years=years,
                portfolio_survived=False,
                final_capital_nominal=0.0,
                final_capital_real=0.0,
                depletion_year=1,
                yearly_balances=[0.0] * (years + 1),
                scenario_source=scenario_source,
                scenario_returns_pct=modeled_returns,
            )

        for y in range(1, years + 1):
            if capital <= 0:
                if depleted_year is None:
                    depleted_year = y
                yearly_balances.append(0.0)
                continue

            # Annual withdrawal (real terms)
            capital -= withdrawal
            if capital <= 0:
                capital = 0.0
                if depleted_year is None:
                    depleted_year = y
            else:
                # Return rate for current year
                if y <= len(pattern):
                    r = pattern[y - 1] / 100.0
                else:
                    r = avg_ret / 100.0
                capital = strict_float(capital * max(0.0, 1.0 + r), field_name="capital real calculado")
                if capital <= 0 and depleted_year is None:
                    depleted_year = y

            yearly_balances.append(capital)

        survived = capital > 0 and depleted_year is None
        infl = inflation_pct / 100.0
        try:
            cum_infl = math.pow(1.0 + infl, years)
        except OverflowError as exc:
            raise ValueError("A inflação e o horizonte excedem a capacidade numérica do modelo.") from exc
        final_real = round(capital, 2)
        final_nominal = round(strict_float(capital * cum_infl, field_name="capital nominal final"), 2) if capital > 0 else 0.0

        return SrrSimulationResult(
            scenario_name=early_crash_scenario,
            starting_capital=initial_capital,
            annual_withdrawal=initial_annual_withdrawal,
            duration_years=years,
            portfolio_survived=survived,
            final_capital_nominal=final_nominal,
            final_capital_real=final_real,
            depletion_year=depleted_year,
            yearly_balances=yearly_balances,
            scenario_source=scenario_source,
            scenario_returns_pct=modeled_returns,
        )

    @classmethod
    def simulate_guyton_klinger_sensitivity_grid(
        cls,
        *,
        initial_capital: float,
        initial_annual_withdrawal: float,
        annual_returns: Sequence[float],
        annual_inflation_rates: Sequence[float],
        swr_range_pct: Sequence[float] = (3.0, 3.5, 4.0, 4.5, 5.0, 5.5),
    ) -> dict[float, list[dict[str, Any]]]:
        """Vary actual initial spending by the SWR axis, retaining the baseline as context."""
        strict_float(initial_annual_withdrawal, field_name="levantamento de referência")
        capital = strict_float(initial_capital, field_name="capital inicial")
        return {
            swr: cls.simulate_guyton_klinger_drawdown(
                initial_capital=initial_capital,
                initial_annual_withdrawal=capital * strict_float(swr, field_name="taxa inicial") / 100.0,
                annual_returns=annual_returns,
                annual_inflation_rates=annual_inflation_rates,
                initial_swr_pct=swr,
            )
            for swr in swr_range_pct
        }

    @classmethod
    def simulate_sequence_of_returns_batch(
        cls,
        *,
        initial_capital: float,
        initial_annual_withdrawal: float,
        duration_years: int = 30,
        average_real_return_pct: float = 5.0,
        scenarios: Sequence[str] = (
            "dotcom_2000",
            "gfc_2008",
            "stagflation_1973",
            "baseline_steady",
        ),
    ) -> dict[str, SrrSimulationResult]:
        """Execute all early retirement crash stress scenarios in a single batch."""
        return {
            scenario: cls.simulate_sequence_of_returns_risk(
                initial_capital=initial_capital,
                initial_annual_withdrawal=initial_annual_withdrawal,
                duration_years=duration_years,
                average_real_return_pct=average_real_return_pct,
                early_crash_scenario=scenario,
            )
            for scenario in scenarios
        }


FireService = FirePlanningEngine

__all__ = [
    "FireMilestone",
    "FirePlanningEngine",
    "FireService",
    "SrrSimulationResult",
]
