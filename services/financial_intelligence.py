"""Advanced local analytics for planning, risk and period comparison."""

from __future__ import annotations

import math
import statistics
from calendar import monthrange
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any, Optional

from database.validation import (
    sql_external_financial_flow,
    sql_finite_number,
    sql_iso_date,
)
from services.finance_math import periodic_discount_factor
from services.investment_performance import (
    portfolio_performance_attribution,
)
from utils.config import parse_date
from utils.helpers import add_months, eur, iso, row_get, safe_float, strict_float

if TYPE_CHECKING:
    from database.repo import Repo


_FINANCIAL_DATE_SQL = "COALESCE(NULLIF(m.payment_date,''),t.date)"
_VALID_TRANSACTION_AMOUNT_SQL = sql_finite_number("t.amount", non_zero=True)
_VALID_FINANCIAL_DATE_SQL = sql_iso_date(_FINANCIAL_DATE_SQL)
_EXTERNAL_FLOW_SQL = sql_external_financial_flow("t.category")


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = max(0.0, min(1.0, percentile)) * (len(ordered) - 1)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _percentiles_10_50_90(values: list[float]) -> tuple[float, float, float]:
    """Calculate P10, P50 and P90 percentiles with a single sort operation."""
    if not values:
        return 0.0, 0.0, 0.0
    ordered = sorted(values)
    n = len(ordered)
    if n == 1:
        v = float(ordered[0])
        return v, v, v

    def _calc_at(p: float) -> float:
        pos = p * (n - 1)
        low = int(math.floor(pos))
        up = int(math.ceil(pos))
        if low == up:
            return ordered[low]
        w = pos - low
        return ordered[low] * (1.0 - w) + ordered[up] * w

    return _calc_at(0.10), _calc_at(0.50), _calc_at(0.90)


def monte_carlo_projection(
    contribution: float,
    annual_return_pct: float = 6.0,
    annual_volatility_pct: float = 12.0,
    years: int = 10,
    *,
    simulations: int = 1_000,
    target: float = 100_000.0,
    mode: str = "monthly",
    initial_capital: float = 0.0,
    inflation_pct: float = 2.0,
    tax_pct: float = 28.0,
    fee_pct: float = 0.0,
    seed: int = 42,
    correlation_pct: float = 40.0,
    asset_weights: Optional[list[float]] = None,
    history_points: int = 0,
) -> dict[str, Any]:
    """Run monthly geometric paths with separate hypothetical horizon liquidations."""
    for field, value in (
        ("reforço", contribution), ("capital inicial", initial_capital),
        ("retorno anual", annual_return_pct), ("volatilidade", annual_volatility_pct),
        ("inflação", inflation_pct), ("imposto", tax_pct), ("comissão", fee_pct),
        ("objetivo", target), ("correlação", correlation_pct),
    ):
        strict_float(value, field_name=field)
    for field, value in (("anos", years), ("simulações", simulations), ("semente", seed), ("pontos históricos", history_points)):
        count = strict_float(value, field_name=field)
        if count != int(count):
            raise ValueError(f"{field.capitalize()} deve ser um número inteiro.")
    for value in asset_weights or []:
        strict_float(value, field_name="peso do ativo")
    amount = max(0.0, safe_float(contribution))
    initial = max(0.0, safe_float(initial_capital))
    annual_return = max(-0.9999, min(5.0, safe_float(annual_return_pct) / 100.0))
    volatility = max(0.0, min(2.0, safe_float(annual_volatility_pct) / 100.0))
    inflation = max(-0.2, min(1.0, safe_float(inflation_pct) / 100.0))
    tax = max(0.0, min(1.0, safe_float(tax_pct) / 100.0))
    fee = max(0.0, min(1.0, safe_float(fee_pct) / 100.0))
    horizon = max(1, min(40, int(years or 10)))
    run_count = max(100, min(5_000, int(simulations or 1_000)))
    objective = max(0.0, safe_float(target))
    normalised_mode = "single" if str(mode or "monthly").strip().casefold() in {"single", "unique", "único", "unico"} else "monthly"
    weights = [max(0.0, safe_float(value)) for value in (asset_weights or [])]
    weight_scale = max(weights, default=0.0)
    if weight_scale <= 0:
        weights = [1.0]
    else:
        scaled_weights = [value / weight_scale for value in weights if value > 0]
        weight_total = math.fsum(scaled_weights)
        weights = [value / weight_total for value in scaled_weights]
    asset_count = len(weights)
    concentration = sum(value**2 for value in weights)
    minimum_correlation = -1.0 / (asset_count - 1) if asset_count > 1 else 1.0
    requested_correlation = max(
        -0.9999, min(0.9999, safe_float(correlation_pct) / 100.0)
    )
    correlation = (
        max(minimum_correlation, requested_correlation) if asset_count > 1 else 1.0
    )
    diversification_factor = math.sqrt(
        max(0.0, correlation + (1.0 - correlation) * concentration)
    )
    effective_volatility = volatility * diversification_factor
    yearly_real_contributions: list[float] = []
    yearly_nominal_contributions: list[float] = []

    monthly_volatility = effective_volatility / math.sqrt(12.0)
    # ``annual_return_pct`` is an effective arithmetic annual return. The log
    # drift below makes the expected twelve-month growth exactly 1 + annual.
    monthly_drift = math.log1p(annual_return) / 12.0 - 0.5 * monthly_volatility**2
    total_months = horizon * 12
    discount_factors = [
        max(1e-12, periodic_discount_factor(inflation * 100.0, month))
        for month in range(1, total_months + 1)
    ]

    for year in range(1, horizon + 1):
        months = year * 12
        nominal_contributed = initial + (
            amount if normalised_mode == "single" else amount * months
        )
        if normalised_mode == "single":
            real_contributed = initial + amount
        else:
            real_contributed = initial + sum(
                amount / discount_factors[month - 1] for month in range(1, months + 1)
            )
        yearly_nominal_contributions.append(nominal_contributed)
        yearly_real_contributions.append(real_contributed)

    import numpy as np

    np_rng = np.random.default_rng(int(seed))
    shocks = np_rng.normal(0.0, 1.0, size=(run_count, total_months))
    growths = np.exp(monthly_drift + monthly_volatility * shocks)

    single_fee = amount * fee if normalised_mode == "single" else 0.0
    val_init = initial + (amount - single_fee if normalised_mode == "single" else 0.0)
    values = np.full(run_count, val_init, dtype=np.float64)
    nom_contrib = np.full(
        run_count,
        initial + (amount if normalised_mode == "single" else 0.0),
        dtype=np.float64,
    )
    market_idx = np.ones(run_count, dtype=np.float64)
    peak_idx = np.ones(run_count, dtype=np.float64)
    max_dd = np.zeros(run_count, dtype=np.float64)
    monthly_deposit = amount * (1.0 - fee) if normalised_mode == "monthly" else 0.0

    yearly_nominal_mat = np.empty((horizon, run_count), dtype=np.float64)
    yearly_real_mat = np.empty((horizon, run_count), dtype=np.float64)

    # Turn numerical overflow into an explicit failed calculation, never a
    # plausible percentile report. The valid-path recurrence is unchanged.
    with np.errstate(over="ignore", invalid="ignore"):
        for month_idx in range(total_months):
            g = growths[:, month_idx]
            values = np.maximum(0.0, values * g)
            market_idx = np.maximum(0.0, market_idx * g)
            peak_idx = np.maximum(peak_idx, market_idx)
            dd = np.where(peak_idx > 0, (peak_idx - market_idx) / peak_idx, 0.0)
            max_dd = np.maximum(max_dd, dd)

            if normalised_mode == "monthly":
                values += monthly_deposit
                nom_contrib += amount

            month = month_idx + 1
            if month % 12 == 0:
                year_idx = (month // 12) - 1
                taxable_gain = np.maximum(0.0, values - nom_contrib)
                after_tax = values - taxable_gain * tax
                real_val = after_tax / discount_factors[month_idx]
                yearly_nominal_mat[year_idx] = after_tax
                yearly_real_mat[year_idx] = real_val

    if not (np.isfinite(yearly_nominal_mat).all() and np.isfinite(yearly_real_mat).all()
            and np.isfinite(yearly_nominal_contributions).all()
            and np.isfinite(yearly_real_contributions).all()):
        raise ValueError("As hipóteses excedem a capacidade numérica da simulação.")

    p10_r_arr, p50_r_arr, p90_r_arr = np.percentile(
        yearly_real_mat, [10, 50, 90], axis=1
    )
    p10_n_arr, p50_n_arr, p90_n_arr = np.percentile(
        yearly_nominal_mat, [10, 50, 90], axis=1
    )

    real_contrib_arr = np.array(yearly_real_contributions, dtype=np.float64)
    nom_contrib_arr = np.array(yearly_nominal_contributions, dtype=np.float64)

    prob_target_arr = (
        np.mean(yearly_real_mat >= objective, axis=1) * 100.0
        if objective > 0
        else np.zeros(horizon, dtype=np.float64)
    )
    prob_loss_real_arr = (
        np.mean(yearly_real_mat < real_contrib_arr[:, None], axis=1) * 100.0
    )
    prob_loss_nom_arr = (
        np.mean(yearly_nominal_mat < nom_contrib_arr[:, None], axis=1) * 100.0
    )

    rows: list[dict[str, Any]] = []
    for y in range(horizon):
        year = y + 1
        nominal_invested = float(nom_contrib_arr[y])
        real_invested = float(real_contrib_arr[y])
        p10_r = float(p10_r_arr[y])
        p50_r = float(p50_r_arr[y])
        p90_r = float(p90_r_arr[y])
        p10_n = float(p10_n_arr[y])
        p50_n = float(p50_n_arr[y])
        p90_n = float(p90_n_arr[y])
        rows.append(
            {
                "year": year,
                "invested": real_invested,
                "invested_real": real_invested,
                "invested_nominal": nominal_invested,
                "p10": p10_r,
                "p50": p50_r,
                "p90": p90_r,
                "p10_real": p10_r,
                "p50_real": p50_r,
                "p90_real": p90_r,
                "p10_nominal": p10_n,
                "p50_nominal": p50_n,
                "p90_nominal": p90_n,
                "probability_target": float(prob_target_arr[y]),
                "probability_loss_real": float(prob_loss_real_arr[y]),
                "probability_loss_nominal": float(prob_loss_nom_arr[y]),
            }
        )
    med_max_drawdown = float(np.percentile(max_dd * 100.0, 50))
    p90_max_drawdown = float(np.percentile(max_dd * 100.0, 90))
    final = rows[-1]
    points = max(0, int(history_points or 0))
    if points >= 24:
        confidence, confidence_label = "high", "Apoio histórico elevado"
    elif points >= 12:
        confidence, confidence_label = "medium", "Apoio histórico moderado"
    else:
        confidence, confidence_label = "low", "Cenário sobretudo manual"
    return {
        "mode": normalised_mode,
        "contribution": amount,
        "initial_capital": initial,
        "annual_return_pct": annual_return * 100.0,
        "annual_volatility_pct": volatility * 100.0,
        "effective_volatility_pct": effective_volatility * 100.0,
        "inflation_pct": inflation * 100.0,
        "tax_pct": tax * 100.0,
        "fee_pct": fee * 100.0,
        "simulations": run_count,
        "years": horizon,
        "target": objective,
        "probability_target": final["probability_target"],
        "probability_loss_real": final["probability_loss_real"],
        "probability_loss_nominal": final["probability_loss_nominal"],
        "median_max_drawdown_pct": med_max_drawdown,
        "p90_max_drawdown_pct": p90_max_drawdown,
        "seed": int(seed),
        "asset_count": asset_count,
        "correlation_pct": correlation * 100.0,
        "requested_correlation_pct": requested_correlation * 100.0,
        "history_points": points,
        "confidence": confidence,
        "confidence_label": confidence_label,
        "annual_return_convention": "effective_annual",
        "tax_timing": "hypothetical_liquidation_each_horizon",
        "tax_reduces_reinvested_state": False,
        "contribution_timing": "month_end",
        "return_basis": "nominal",
        "target_value_basis": "today_euros",
        "model_type": "one_factor_volatility_adjustment",
        "model_version": "1.1-scalar-approx",
        "is_multi_asset_covariance": False,
        "model_description": (
            "Ajuste escalar de volatilidade de um fator (aproximação ponderada por concentração "
            "e correlação paritária, sem matriz de covariância multi-ativo completa nem rebalanceamento periódico)."
        ),
        "assumptions": {
            "annual_return_pct": safe_float(annual_return_pct),
            "annual_volatility_pct": safe_float(annual_volatility_pct),
            "effective_volatility_pct": effective_volatility * 100.0,
            "diversification_factor": diversification_factor,
            "concentration_hhi": concentration,
            "inflation_pct": safe_float(inflation_pct),
            "tax_pct": safe_float(tax_pct),
            "fee_pct": safe_float(fee_pct),
            "weights": weights,
            "asset_count": asset_count,
            "correlation_pct": correlation * 100.0,
        },
        "rows": rows,
        "data_warning": (
            "A rentabilidade, volatilidade e correlação são hipóteses manuais; "
            "existem poucos pontos históricos da carteira para as confrontar."
            if points < 12
            else "As hipóteses continuam manuais e devem ser revistas, mesmo com histórico disponível."
        ),
        "method_label": (
            f"{run_count} trajetórias · semente {int(seed)} · taxa anual efetiva · "
            f"modelo de 1 fator ({asset_count} ativo(s), correlação paritária {correlation * 100.0:.0f}%, sem covariância multi-ativo) · "
            "comissão aplicada quando cada reforço entra, imposto estimado numa "
            "liquidação em cada horizonte e inflação aplicada a cada fluxo. "
            "Objetivo e P10/P50/P90 estão em euros de hoje; não são garantias."
        ),
    }


class FinancialIntelligenceService:
    def __init__(self, repo: "Repo") -> None:
        self.repo = repo

    def net_worth_bridge(
        self,
        start: str,
        end: str,
    ) -> dict[str, Any]:
        """Reconcile the change in net worth between two real dates.

        Cash movements, portfolio cost/value snapshots and recorded liabilities
        are kept separate so transfers into an investment are not mistaken for
        market gains.  When a historical non-cash snapshot is unavailable the
        result remains visible, but its coverage is explicitly reduced.
        """

        start_day = parse_date(start)
        end_day = min(parse_date(end), date.today())
        if start_day > end_day:
            raise ValueError("O início da ponte patrimonial é posterior ao fim.")
        opening_day = start_day - timedelta(days=1)

        def cash_balance(reference: date) -> float:
            return safe_float(self.repo.cash_balance(iso(reference)))

        flow_row = self.repo.q(
            f"""
            SELECT
                COALESCE(SUM(
                    CASE WHEN t.amount>0
                              AND {_EXTERNAL_FLOW_SQL}
                         THEN t.amount ELSE 0 END
                ),0) AS income,
                COALESCE(SUM(
                    CASE WHEN t.amount<0
                              AND {_EXTERNAL_FLOW_SQL}
                         THEN ABS(t.amount) ELSE 0 END
                ),0) AS expenses,
                COALESCE(SUM(
                    CASE WHEN t.category='Objetivos e Poupança'
                         THEN t.amount ELSE 0 END
                ),0) AS goal_cash_flow
            FROM transactions t
            LEFT JOIN transaction_meta m ON m.transaction_id=t.id
            WHERE COALESCE(NULLIF(m.payment_date,''),t.date)>=?
              AND COALESCE(NULLIF(m.payment_date,''),t.date)<=?
              AND {_VALID_TRANSACTION_AMOUNT_SQL}
              AND {_VALID_FINANCIAL_DATE_SQL}
            """,
            (iso(start_day), iso(end_day)),
        )
        income = safe_float(row_get(flow_row[0], "income")) if flow_row else 0.0
        expenses = safe_float(row_get(flow_row[0], "expenses")) if flow_row else 0.0
        goal_cash_flow = (
            safe_float(row_get(flow_row[0], "goal_cash_flow")) if flow_row else 0.0
        )

        opening_cash = cash_balance(opening_day)
        closing_cash = cash_balance(end_day)
        opening_investments = self.repo.investment_state_as_of(iso(opening_day))
        closing_investments = self.repo.investment_state_as_of(iso(end_day))
        opening_state = self.repo.financial_state_as_of(iso(opening_day))
        closing_state = self.repo.financial_state_as_of(iso(end_day))

        def liability_total(state: dict[str, Any]) -> float:
            return sum(
                max(0.0, safe_float(row_get(row, "balance")))
                for row in list(state.get("liabilities") or [])
            )

        def goal_total(state: dict[str, Any]) -> float:
            return sum(
                max(0.0, safe_float(row_get(row, "saved_amount")))
                for row in list(state.get("goals") or [])
            )

        opening_investment_value = safe_float(opening_investments.get("total"))
        closing_investment_value = safe_float(closing_investments.get("total"))
        opening_cost_basis = safe_float(opening_investments.get("invested"))
        closing_cost_basis = safe_float(closing_investments.get("invested"))
        opening_liabilities = liability_total(opening_state)
        closing_liabilities = liability_total(closing_state)
        opening_goal_assets = goal_total(opening_state)
        closing_goal_assets = goal_total(closing_state)

        opening_net_worth = (
            opening_cash
            + opening_investment_value
            + opening_goal_assets
            - opening_liabilities
        )
        closing_net_worth = (
            closing_cash
            + closing_investment_value
            + closing_goal_assets
            - closing_liabilities
        )
        raw_invested_capital_change = closing_cost_basis - opening_cost_basis
        raw_market_result = (
            closing_investment_value
            - opening_investment_value
            - raw_invested_capital_change
        )
        raw_liability_reduction = opening_liabilities - closing_liabilities
        observed_cash_change = closing_cash - opening_cash
        classified_cash_change = income - expenses + goal_cash_flow
        cash_adjustment = observed_cash_change - classified_cash_change

        current_holdings = list(self.repo.holdings())
        current_liabilities = list(self.repo.liabilities())
        current_goals = list(self.repo.goals())
        investment_history = list(self.repo.investment_evolution())
        investment_history_required = bool(current_holdings or investment_history)
        liability_history_required = bool(current_liabilities)
        goal_history_required = bool(current_goals or abs(goal_cash_flow) >= 0.005)
        opening_investments_known = (
            bool(opening_investments.get("available"))
            or not investment_history_required
        )
        closing_investments_known = (
            bool(closing_investments.get("available"))
            or not investment_history_required
        )
        opening_liabilities_known = (
            bool(opening_state.get("liabilities_available"))
            or not liability_history_required
        )
        closing_liabilities_known = (
            bool(closing_state.get("liabilities_available"))
            or not liability_history_required
        )
        opening_goals_known = (
            bool(opening_state.get("goals_available")) or not goal_history_required
        )
        closing_goals_known = (
            bool(closing_state.get("goals_available")) or not goal_history_required
        )
        investment_change_known = (
            opening_investments_known and closing_investments_known
        )
        liability_change_known = opening_liabilities_known and closing_liabilities_known
        goal_change_known = opening_goals_known and closing_goals_known
        invested_capital_change = (
            raw_invested_capital_change if investment_change_known else 0.0
        )
        market_result = raw_market_result if investment_change_known else 0.0
        unclassified_investment_change = (
            0.0
            if investment_change_known
            else closing_investment_value - opening_investment_value
        )
        liability_reduction = raw_liability_reduction if liability_change_known else 0.0
        unclassified_liability_change = (
            0.0 if liability_change_known else raw_liability_reduction
        )
        raw_goal_assets_change = closing_goal_assets - opening_goal_assets
        goal_assets_change = raw_goal_assets_change if goal_change_known else 0.0
        unclassified_goal_change = 0.0 if goal_change_known else raw_goal_assets_change

        coverage_components = [
            {
                "key": "cash",
                "label": "Saldo e movimentos",
                "required": True,
                "opening_known": True,
                "closing_known": True,
                "complete": True,
            },
            {
                "key": "investments",
                "label": "Carteira",
                "required": investment_history_required,
                "opening_known": opening_investments_known,
                "closing_known": closing_investments_known,
                "complete": investment_change_known,
            },
            {
                "key": "liabilities",
                "label": "Passivos",
                "required": liability_history_required,
                "opening_known": opening_liabilities_known,
                "closing_known": closing_liabilities_known,
                "complete": liability_change_known,
            },
            {
                "key": "goals",
                "label": "Objetivos e poupança",
                "required": goal_history_required,
                "opening_known": opening_goals_known,
                "closing_known": closing_goals_known,
                "complete": goal_change_known,
            },
        ]
        history_complete = all(
            bool(component["complete"])
            for component in coverage_components
            if bool(component["required"])
        )
        known_change_magnitude = abs(income) + abs(expenses) + abs(goal_cash_flow)
        if investment_change_known:
            known_change_magnitude += abs(invested_capital_change) + abs(market_result)
        if liability_change_known:
            known_change_magnitude += abs(liability_reduction)
        if goal_change_known:
            known_change_magnitude += abs(goal_assets_change)
        unknown_change_magnitude = (
            abs(unclassified_investment_change)
            + abs(unclassified_liability_change)
            + abs(unclassified_goal_change)
            + abs(cash_adjustment)
        )
        total_change_magnitude = known_change_magnitude + unknown_change_magnitude
        if total_change_magnitude >= 0.005:
            coverage_pct = known_change_magnitude / total_change_magnitude * 100.0
        else:
            coverage_pct = 100.0 if history_complete else 0.0
        coverage_pct = max(0.0, min(100.0, coverage_pct))
        complete = history_complete and unknown_change_magnitude < 0.005

        changes = [
            {
                "key": "income",
                "label": "Receitas",
                "value": income,
                "role": "change",
                "tone": "positive",
                "explanation": "Entradas registadas no período.",
            },
            {
                "key": "expenses",
                "label": "Despesas",
                "value": -expenses,
                "role": "change",
                "tone": "negative",
                "explanation": "Saídas registadas no período.",
            },
            {
                "key": "invested_capital",
                "label": "Capital investido",
                "value": invested_capital_change,
                "role": "change",
                "tone": "positive" if invested_capital_change >= 0 else "negative",
                "explanation": (
                    "Variação do custo de aquisição da carteira; separa reforços "
                    "e retiradas do desempenho do mercado."
                ),
            },
            {
                "key": "market_result",
                "label": "Resultado do mercado",
                "value": market_result,
                "role": "change",
                "tone": "positive" if market_result >= 0 else "negative",
                "explanation": (
                    "Variação do valor da carteira depois de retirar o capital "
                    "investido ou levantado."
                ),
            },
        ]
        if abs(goal_cash_flow) >= 0.005:
            changes.append(
                {
                    "key": "goal_cash_flow",
                    "label": "Conta ↔ objetivos",
                    "value": goal_cash_flow,
                    "role": "change",
                    "tone": "positive" if goal_cash_flow >= 0 else "neutral",
                    "explanation": (
                        "Movimento de caixa para dentro ou para fora dos objetivos; "
                        "não é receita nem despesa."
                    ),
                }
            )
        if goal_change_known and abs(goal_assets_change) >= 0.005:
            changes.append(
                {
                    "key": "goal_assets",
                    "label": "Valor nos objetivos",
                    "value": goal_assets_change,
                    "role": "change",
                    "tone": "positive" if goal_assets_change >= 0 else "neutral",
                    "explanation": (
                        "Variação do dinheiro guardado nos objetivos, contado como "
                        "parte do património."
                    ),
                }
            )
        elif abs(unclassified_goal_change) >= 0.005:
            changes.append(
                {
                    "key": "unclassified_goals",
                    "label": "Objetivos sem histórico",
                    "value": unclassified_goal_change,
                    "role": "change",
                    "tone": "warning",
                    "explanation": (
                        "Falta um estado histórico dos objetivos; a variação fica "
                        "visível sem ser apresentada como receita ou despesa."
                    ),
                }
            )
        if not investment_change_known and abs(unclassified_investment_change) >= 0.005:
            changes = [
                change
                for change in changes
                if change["key"] not in {"invested_capital", "market_result"}
            ]
            changes.append(
                {
                    "key": "unclassified_investments",
                    "label": "Carteira sem histórico",
                    "value": unclassified_investment_change,
                    "role": "change",
                    "tone": "warning",
                    "explanation": (
                        "Não existe um estado histórico suficiente para separar "
                        "capital aplicado de valorização. O valor aparece como "
                        "variação não classificada, sem fingir rentabilidade."
                    ),
                }
            )
        if liability_change_known:
            changes.append(
                {
                    "key": "liability_reduction",
                    "label": "Redução de dívida",
                    "value": liability_reduction,
                    "role": "change",
                    "tone": ("positive" if liability_reduction >= 0 else "negative"),
                    "explanation": (
                        "Impacto da redução de passivos; um aumento de dívida "
                        "surge como valor negativo."
                    ),
                }
            )
        elif abs(unclassified_liability_change) >= 0.005:
            changes.append(
                {
                    "key": "unclassified_liabilities",
                    "label": "Passivos sem histórico",
                    "value": unclassified_liability_change,
                    "role": "change",
                    "tone": "warning",
                    "explanation": (
                        "A variação dos passivos não pode ser atribuída com "
                        "segurança porque falta um dos estados históricos."
                    ),
                }
            )
        if abs(cash_adjustment) >= 0.005:
            changes.append(
                {
                    "key": "cash_adjustment",
                    "label": "Outros movimentos",
                    "value": cash_adjustment,
                    "role": "change",
                    "tone": "positive" if cash_adjustment >= 0 else "negative",
                    "explanation": (
                        "Diferença entre a variação do saldo e as receitas/despesas "
                        "classificadas e os movimentos de objetivos, incluindo "
                        "transferências internas entre contas."
                    ),
                }
            )

        running = opening_net_worth
        steps: list[dict[str, Any]] = [
            {
                "key": "opening",
                "label": "Património inicial",
                "value": opening_net_worth,
                "start": 0.0,
                "end": opening_net_worth,
                "role": "total",
                "tone": "positive" if opening_net_worth >= 0 else "negative",
                "explanation": (
                    "Saldo de contas + investimentos + objetivos − passivos "
                    "antes do período."
                ),
            }
        ]
        for change in changes:
            before = running
            running += safe_float(change.get("value"))
            steps.append(
                {
                    **change,
                    "start": before,
                    "end": running,
                    "before": before,
                    "after": running,
                }
            )

        reconciliation = closing_net_worth - running
        if abs(reconciliation) >= 0.005:
            before = running
            running += reconciliation
            steps.append(
                {
                    "key": "reconciliation",
                    "label": "Reconciliação",
                    "value": reconciliation,
                    "start": before,
                    "end": running,
                    "before": before,
                    "after": running,
                    "role": "change",
                    "tone": "warning",
                    "explanation": (
                        "Ajuste necessário para ligar os estados inicial e final. "
                        "Pode resultar de histórico patrimonial incompleto."
                    ),
                }
            )
        steps.append(
            {
                "key": "closing",
                "label": "Património final",
                "value": closing_net_worth,
                "start": 0.0,
                "end": closing_net_worth,
                "role": "total",
                "tone": "positive" if closing_net_worth >= 0 else "negative",
                "explanation": (
                    "Saldo de contas + investimentos + objetivos − passivos "
                    "no fim do período."
                ),
            }
        )

        change = closing_net_worth - opening_net_worth
        return {
            "start": iso(start_day),
            "end": iso(end_day),
            "opening_as_of": iso(opening_day),
            "opening_net_worth": opening_net_worth,
            "closing_net_worth": closing_net_worth,
            "change": change,
            "change_pct": (
                change / abs(opening_net_worth) * 100.0 if opening_net_worth else None
            ),
            "income": income,
            "expenses": expenses,
            "goal_cash_flow": goal_cash_flow,
            "goal_assets_change": goal_assets_change,
            "invested_capital_change": invested_capital_change,
            "market_result": market_result,
            "liability_reduction": liability_reduction,
            "unclassified_investment_change": unclassified_investment_change,
            "unclassified_liability_change": unclassified_liability_change,
            "unclassified_goal_change": unclassified_goal_change,
            "cash_adjustment": cash_adjustment,
            "coverage_pct": coverage_pct,
            "explained_change_pct": coverage_pct,
            "known_change_magnitude": known_change_magnitude,
            "unknown_change_magnitude": unknown_change_magnitude,
            "coverage_basis": "absolute_bridge_movements",
            "coverage_components": coverage_components,
            "history_complete": history_complete,
            "complete": complete,
            "opening_state_source": str(opening_state.get("source") or "unavailable"),
            "closing_state_source": str(closing_state.get("source") or "unavailable"),
            "opening_investment_source": str(
                opening_investments.get("source") or "unavailable"
            ),
            "closing_investment_source": str(
                closing_investments.get("source") or "unavailable"
            ),
            "steps": steps,
            "method_label": (
                "Reconciliação de saldo, objetivos, carteira e passivos com dados históricos "
                "registados. A percentagem representa a parcela do valor absoluto "
                "dos movimentos cuja origem foi identificada; não é uma estimativa "
                "estatística de confiança. "
                + (
                    "Toda a variação ficou explicada."
                    if complete
                    else (
                        "A parte restante está visível como carteira, passivos ou "
                        "outros movimentos sem classificação suficiente."
                    )
                )
            ),
        }

    def net_worth_snapshot(self, as_of: Optional[date] = None) -> dict[str, Any]:
        reference = min(as_of or date.today(), date.today())
        state = self.repo.financial_state_as_of(reference.isoformat())
        holdings = list(state.get("holdings") or [])
        liabilities = list(state.get("liabilities") or [])
        subscriptions = list(state.get("subscriptions") or [])
        goals = list(state.get("goals") or [])
        investment_state = self.repo.investment_state_as_of(reference.isoformat())
        holdings_detail_available = bool(holdings)
        if not holdings and bool(investment_state.get("available")):
            holdings = [
                {
                    "name": "Carteira histórica",
                    "asset_class": "Sem detalhe histórico",
                    "value": safe_float(investment_state.get("total")),
                    "cost_basis": safe_float(investment_state.get("invested")),
                }
            ]
        configured_rows = list(self.repo.financial_accounts())
        active_account_names = {
            str(row_get(row, "name") or "Conta principal").strip().casefold()
            for row in configured_rows
        }
        active_account_ids = {
            str(row_get(row, "stable_id", "") or "").strip()
            for row in configured_rows
            if str(row_get(row, "stable_id", "") or "").strip()
        }
        account_rows = list(self.repo.account_balances(reference.isoformat()))
        unknown_accounts = [
            str(row_get(row, "account") or "Conta principal")
            for row in account_rows
            if not bool(row_get(row, "balance_known", False))
        ]

        def holding_link_is_valid(row: Any) -> bool:
            linked_id = str(row_get(row, "linked_account_id", "") or "").strip()
            linked_name = str(row_get(row, "linked_account", "") or "").strip()
            return linked_id in active_account_ids or (
                bool(linked_name) and linked_name.casefold() in active_account_names
            )

        asset_groups: dict[str, float] = {}
        liquid_assets = 0.0
        overdrafts = 0.0
        for row in account_rows:
            balance = safe_float(row_get(row, "balance"))
            account = str(row_get(row, "account") or "Conta principal")
            if balance >= 0:
                asset_groups[f"Conta · {account}"] = balance
                liquid_assets += balance
            else:
                overdrafts += abs(balance)
        investment_assets = 0.0
        for row in holdings:
            rate = safe_float(row_get(row, "exchange_rate", 1.0) or 1.0)
            rate = rate if rate > 0 else 1.0
            value = max(0.0, safe_float(row_get(row, "value"))) * rate
            asset_class = str(row_get(row, "asset_class") or "Investimentos")
            if holding_link_is_valid(row):
                continue
            asset_groups[asset_class] = asset_groups.get(asset_class, 0.0) + value
            investment_assets += value
        goal_assets = 0.0
        for row in goals:
            value = max(0.0, safe_float(row_get(row, "saved_amount")))
            if value <= 0:
                continue
            # PERS-02: Goals are virtual allocations within cash, not autonomous assets
            goal_assets += value

        linked_holdings = [
            str(row_get(row, "name") or row_get(row, "asset_class") or "Investimento")
            for row in holdings
            if holding_link_is_valid(row)
        ]
        broken_holding_links = [
            str(row_get(row, "name") or row_get(row, "asset_class") or "Investimento")
            for row in holdings
            if str(row_get(row, "linked_account", "") or "").strip()
            and not holding_link_is_valid(row)
        ]
        monetary_classes = {
            "depósito",
            "deposito",
            "dinheiro",
            "conta à ordem",
            "conta a ordem",
            "cash",
        }
        unlinked_monetary_holdings = [
            str(
                row_get(row, "name") or row_get(row, "asset_class") or "Ativo monetário"
            )
            for row in holdings
            if str(row_get(row, "asset_class") or "").strip().casefold()
            in monetary_classes
            and not str(row_get(row, "linked_account", "") or "").strip()
        ]

        liability_groups: dict[str, float] = {}
        monthly_debt_payment = 0.0
        weighted_interest = 0.0
        for row in liabilities:
            balance = max(0.0, safe_float(row_get(row, "balance")))
            kind = str(row_get(row, "liability_type") or "Outro")
            liability_groups[kind] = liability_groups.get(kind, 0.0) + balance
            monthly_debt_payment += max(
                0.0, safe_float(row_get(row, "monthly_payment"))
            )
            weighted_interest += balance * max(
                0.0, safe_float(row_get(row, "interest_rate"))
            )
        if overdrafts:
            liability_groups["Saldos negativos"] = overdrafts

        total_assets = sum(asset_groups.values())
        total_liabilities = sum(liability_groups.values())
        net_worth = total_assets - total_liabilities
        account_coverage_pct = (
            (len(account_rows) - len(unknown_accounts)) / len(account_rows) * 100.0
            if account_rows
            else 100.0
        )
        quality_score = max(
            0.0,
            account_coverage_pct
            - min(25.0, len(broken_holding_links) * 12.5)
            - min(25.0, len(unlinked_monetary_holdings) * 12.5),
        )
        quality_label = (
            "Completa"
            if quality_score >= 90
            else "Parcial"
            if quality_score >= 55
            else "A rever"
        )
        return {
            "total_assets": total_assets,
            "total_liabilities": total_liabilities,
            "net_worth": net_worth,
            "liquid_assets": liquid_assets,
            "investment_assets": investment_assets,
            "goal_assets": goal_assets,
            "monthly_debt_payment": monthly_debt_payment,
            "debt_ratio_pct": (
                total_liabilities / total_assets * 100.0 if total_assets else 0.0
            ),
            "weighted_interest_pct": (
                weighted_interest / total_liabilities if total_liabilities else 0.0
            ),
            "as_of": reference.isoformat(),
            "state_as_of": str(state.get("as_of") or ""),
            "state_source": str(state.get("source") or "unavailable"),
            "state_confidence": safe_float(state.get("confidence")),
            "liabilities_available": bool(state.get("liabilities_available")),
            "subscriptions_available": bool(state.get("subscriptions_available")),
            "goals_available": bool(state.get("goals_available")),
            "holdings_detail_available": holdings_detail_available,
            "holding_rows": holdings,
            "goal_rows": goals,
            "account_rows": account_rows,
            "data_quality": {
                "score_pct": quality_score,
                "label": quality_label,
                "account_coverage_pct": account_coverage_pct,
                "unknown_accounts": unknown_accounts,
                "linked_holdings": linked_holdings,
                "broken_holding_links": broken_holding_links,
                "unlinked_monetary_holdings": unlinked_monetary_holdings,
            },
            "subscription_rows": subscriptions,
            "assets": [
                {"label": label, "value": value, "kind": "asset"}
                for label, value in sorted(
                    asset_groups.items(), key=lambda item: item[1], reverse=True
                )
                if value > 0
            ],
            "liabilities": [
                {"label": label, "value": value, "kind": "liability"}
                for label, value in sorted(
                    liability_groups.items(), key=lambda item: item[1], reverse=True
                )
                if value > 0
            ],
        }

    @staticmethod
    def _month_bounds(month_key: str) -> tuple[date, date]:
        start = parse_date(f"{month_key}-01")
        end = start.replace(day=monthrange(start.year, start.month)[1])
        return start, end

    def smart_budget_analysis(
        self, month_key: str, as_of: Optional[date] = None
    ) -> dict[str, Any]:
        start, end = self._month_bounds(month_key)
        reference = as_of or date.today()
        effective_reference = min(max(reference, start), end)
        elapsed = max(1, (effective_reference - start).days + 1)
        total_days = (end - start).days + 1
        budgets = self.repo.category_budgets("expense", month_key)

        def spending(period_start: date, period_end: date) -> dict[str, float]:
            rows = self.repo.category_spend_with_splits(
                iso(period_start),
                iso(period_end + timedelta(days=1)),
                basis="Pagamento",
                end_exclusive=True,
            )
            return {
                str(row_get(row, "category") or "Outros"): safe_float(
                    row_get(row, "total")
                )
                for row in rows
            }

        current = spending(start, effective_reference)
        previous_start = add_months(start, -1)
        previous_end = previous_start.replace(
            day=monthrange(previous_start.year, previous_start.month)[1]
        )
        previous = spending(previous_start, previous_end)
        previous_month_key = previous_start.strftime("%Y-%m")
        previous_budgets = self.repo.category_budgets(
            "expense",
            previous_month_key,
        )
        history: list[dict[str, float]] = []
        for offset in range(1, 4):
            hist_start = add_months(start, -offset)
            hist_end = hist_start.replace(
                day=monthrange(hist_start.year, hist_start.month)[1]
            )
            history.append(spending(hist_start, hist_end))

        categories = sorted(set(budgets) | set(current) | set(previous))
        rows: list[dict[str, Any]] = []
        for category in categories:
            budget = max(0.0, safe_float(budgets.get(category)))
            spent = max(0.0, safe_float(current.get(category)))
            previous_spend = max(0.0, safe_float(previous.get(category)))
            historical_values = [safe_float(item.get(category)) for item in history]
            historical_average = sum(historical_values) / len(historical_values)
            completed = reference > end
            projection = spent if completed else spent / elapsed * total_days
            previous_budget = max(0.0, safe_float(previous_budgets.get(category)))
            rollover = (
                max(0.0, previous_budget - previous_spend)
                if previous_budget > 0
                else 0.0
            )
            effective_budget = max(0.0, budget + rollover)
            blended = (
                historical_average * 0.70 + projection * 0.30
                if historical_average > 0
                else projection
            )
            suggested = math.ceil(max(0.0, blended * 1.05) / 5.0) * 5.0
            pace = spent / elapsed
            breach_date = ""
            if pace > 0 and effective_budget > 0 and projection > effective_budget:
                breach_day = min(total_days, max(1, math.ceil(effective_budget / pace)))
                breach_date = iso(start + timedelta(days=breach_day - 1))
            # BUG-AUD-C102: Handle negative or zero budget envelopes gracefully so utilization
            # rates do not turn negative and pace projections/alerts do not produce distorted warnings.
            utilisation = (
                spent / effective_budget * 100.0 if effective_budget > 0 else 0.0
            )
            projected_utilisation = (
                projection / effective_budget * 100.0 if effective_budget > 0 else 0.0
            )
            if effective_budget > 0 and spent > effective_budget:
                status, tone = "Ultrapassado", "negative"
            elif effective_budget > 0 and projected_utilisation > 110:
                status, tone = "Rutura prevista", "negative"
            elif effective_budget > 0 and projected_utilisation > 90:
                status, tone = "Atenção", "warning"
            elif effective_budget > 0:
                status, tone = "Controlado", "positive"
            else:
                status, tone = "Sem limite", "neutral"
            rows.append(
                {
                    "category": category,
                    "spent": spent,
                    "budget": budget,
                    "previous_budget": previous_budget,
                    "rollover": rollover,
                    "effective_budget": effective_budget,
                    "historical_average": historical_average,
                    "projected": projection,
                    "suggested_budget": suggested,
                    "utilisation": utilisation,
                    "projected_utilisation": projected_utilisation,
                    "breach_date": breach_date,
                    "status": status,
                    "tone": tone,
                }
            )
        rows.sort(
            key=lambda row: (
                {"negative": 0, "warning": 1, "neutral": 2, "positive": 3}.get(
                    str(row["tone"]), 4
                ),
                -safe_float(row["projected"]),
            )
        )
        return {
            "month": month_key,
            "days_elapsed": elapsed,
            "days_in_month": total_days,
            "total_rollover": sum(safe_float(row["rollover"]) for row in rows),
            "total_suggested": sum(safe_float(row["suggested_budget"]) for row in rows),
            "risk_count": sum(1 for row in rows if row["tone"] == "negative"),
            "rows": rows,
            "method_label": (
                "Sugestão ponderada: 70% da média dos últimos 3 meses e 30% "
                "da projeção atual, com margem de 5%. O saldo transitado usa a folga do mês anterior."
            ),
        }

    @staticmethod
    def _normalise(value: Any) -> str:
        return " ".join(str(value or "").casefold().strip().split())

    def detect_anomalies(
        self,
        as_of: Optional[date] = None,
        lookback_days: int = 180,
        reporting_days: int = 45,
    ) -> list[dict[str, Any]]:
        reference = as_of or date.today()
        window_days = max(60, int(lookback_days))
        start = reference - timedelta(days=window_days - 1)
        rows = list(
            self.repo.q(
                f"""
                SELECT t.id,
                       COALESCE(NULLIF(m.payment_date,''),t.date) AS date,
                       t.description,
                       COALESCE(NULLIF(t.category,''),'Outros') AS category,
                       ABS(t.amount) AS amount
                FROM transactions t
                LEFT JOIN transaction_meta m ON m.transaction_id=t.id
                WHERE COALESCE(NULLIF(m.payment_date,''),t.date)>=?
                  AND COALESCE(NULLIF(m.payment_date,''),t.date)<=?
                  AND t.amount<0 AND {_EXTERNAL_FLOW_SQL}
                  AND {_VALID_TRANSACTION_AMOUNT_SQL}
                  AND {_VALID_FINANCIAL_DATE_SQL}
                ORDER BY date, t.id
                """,
                (iso(start), iso(reference)),
            )
        )
        alerts: list[dict[str, Any]] = []
        by_category: dict[str, list[Any]] = {}
        by_description: dict[str, list[Any]] = {}
        for row in rows:
            by_category.setdefault(
                str(row_get(row, "category") or "Outros"), []
            ).append(row)
            description_key = self._normalise(row_get(row, "description"))
            if description_key:
                by_description.setdefault(description_key, []).append(row)

        recent_cutoff = reference - timedelta(
            days=max(1, min(int(lookback_days), int(reporting_days)))
        )
        for category, category_rows in by_category.items():
            values = [safe_float(row_get(row, "amount")) for row in category_rows]
            if len(values) < 4:
                continue
            median = statistics.median(values)
            # ANOM-01: with repeated identical values (e.g. the same daily
            # lunch amount) the median absolute deviation is 0.0 and the
            # scale collapses. Fall back to classic stdev, then to 1.0, so
            # the spike threshold stays finite (no zero/inf/nan division).
            raw_mad = statistics.median(abs(value - median) for value in values)
            mad = raw_mad
            if mad < 1e-6:  # ANOM-01
                fallback = (
                    statistics.stdev(values) if len(values) > 1 else 0.0
                )
                mad = fallback if fallback > 1e-6 else 1.0
            # BUG-CASH-03: the static €25 floor neutralised anomaly detection
            # for small recurring expenses (zero-variance spend). The floor is
            # now relative to the median so a +100% deviation on small amounts
            # is still flagged, while the 1.8× multiple keeps noise rejection
            # for genuinely tiny categories. When the baseline variance is
            # zero (raw MAD collapses before the guard), the classic-stdev
            # fallback is contaminated by the very spike under evaluation,
            # so the relative multiple governs instead.
            if raw_mad < 1e-6:
                threshold = max(median * 1.8, median + 3.0)
            else:
                threshold = max(median * 1.8, median + 3.0 * max(1.0, mad))
            for row in category_rows:
                try:
                    transaction_date = parse_date(str(row_get(row, "date")))
                except (ValueError):
                    continue
                amount = safe_float(row_get(row, "amount"))
                if transaction_date >= recent_cutoff and amount > threshold:
                    alerts.append(
                        {
                            "key": f"spike:{row_get(row, 'id')}",
                            "severity": "warning"
                            if amount < threshold * 1.5
                            else "critical",
                            "kind": "spike",
                            "title": f"Gasto invulgar em {category}",
                            "message": (
                                f"{row_get(row, 'description')} ficou acima do padrão "
                                f"habitual desta categoria ({eur(median)})."
                            ),
                            "date": str(row_get(row, "date"))[:10],
                            "amount": amount,
                            "category": category,
                            "search": str(row_get(row, "description") or ""),
                            "action_page": "movimentos",
                        }
                    )

        duplicates: dict[tuple[str, str, int], list[Any]] = {}
        for row in rows:
            key = (
                str(row_get(row, "date"))[:10],
                self._normalise(row_get(row, "description")),
                int(round(safe_float(row_get(row, "amount")) * 100)),
            )
            duplicates.setdefault(key, []).append(row)
        for (day, description, _cents), duplicate_rows in duplicates.items():
            try:
                duplicate_date = parse_date(day)
            except (ValueError):
                continue
            if (
                description
                and len(duplicate_rows) > 1
                and duplicate_date >= recent_cutoff
            ):
                first = duplicate_rows[0]
                alerts.append(
                    {
                        "key": f"duplicate:{day}:{description}:{_cents}",
                        "severity": "critical",
                        "kind": "duplicate",
                        "title": "Possível cobrança duplicada",
                        "message": f"{len(duplicate_rows)} movimentos iguais: {row_get(first, 'description')}.",
                        "date": day,
                        "amount": safe_float(row_get(first, "amount")),
                        "category": str(row_get(first, "category") or "Outros"),
                        "search": str(row_get(first, "description") or ""),
                        "action_page": "movimentos",
                    }
                )

        for description, description_rows in by_description.items():
            if len(description_rows) < 2:
                continue
            previous, latest = description_rows[-2], description_rows[-1]
            previous_amount = safe_float(row_get(previous, "amount"))
            latest_amount = safe_float(row_get(latest, "amount"))
            try:
                latest_date = parse_date(str(row_get(latest, "date")))
            except (ValueError):
                continue
            if (
                latest_date >= recent_cutoff
                and previous_amount > 0
                and latest_amount > previous_amount * 1.10
                and latest_amount - previous_amount >= 2.0
            ):
                increase = (latest_amount / previous_amount - 1.0) * 100.0
                alerts.append(
                    {
                        "key": f"increase:{row_get(latest, 'id')}",
                        "severity": "warning",
                        "kind": "increase",
                        "title": "Preço recorrente aumentou",
                        "message": f"{row_get(latest, 'description')} subiu {increase:.1f}% face ao registo anterior.",
                        "date": str(row_get(latest, "date"))[:10],
                        "amount": latest_amount,
                        "category": str(row_get(latest, "category") or "Outros"),
                        "search": str(row_get(latest, "description") or description),
                        "action_page": "movimentos",
                    }
                )

        severity_order = {"critical": 0, "warning": 1, "info": 2}
        unique: dict[str, dict[str, Any]] = {}
        for alert in alerts:
            unique[str(alert["key"])] = alert
        return sorted(
            unique.values(),
            key=lambda alert: (
                severity_order.get(str(alert["severity"]), 3),
                str(alert.get("date") or ""),
            ),
            reverse=False,
        )

    def alert_center(self, as_of: Optional[date] = None) -> list[dict[str, Any]]:
        reference = as_of or date.today()
        alerts = list(self.detect_anomalies(reference))
        budgets = self.smart_budget_analysis(reference.strftime("%Y-%m"), reference)
        for row in budgets["rows"]:
            if row["tone"] not in {"negative", "warning"}:
                continue
            alerts.append(
                {
                    "key": f"budget:{reference:%Y-%m}:{row['category']}",
                    "severity": "critical" if row["tone"] == "negative" else "warning",
                    "kind": "budget",
                    "title": f"Orçamento · {row['category']}",
                    "message": (
                        f"Projeção {eur(row['projected'])}; "
                        f"limite efetivo {eur(row['effective_budget'])}."
                    ),
                    "date": str(row.get("breach_date") or iso(reference)),
                    "amount": safe_float(row["projected"]),
                    "category": str(row["category"]),
                    "action_page": "orcamentos",
                }
            )

        from services.payments import PaymentsService

        payments = PaymentsService(self.repo)
        for row in self.repo.subscriptions(active_only=True):
            candidate = payments.next_subscription_due_date(row, reference)
            days = (candidate - reference).days
            if days <= 7:
                alerts.append(
                    {
                        "key": f"subscription:{row_get(row, 'id')}:{candidate}",
                        "severity": "info" if days > 2 else "warning",
                        "kind": "subscription",
                        "title": "Renovação próxima",
                        "message": f"{row_get(row, 'name')} renova dentro de {days} dia(s).",
                        "date": iso(candidate),
                        "amount": safe_float(row_get(row, "amount")),
                        "category": str(row_get(row, "category") or "Subscrições"),
                        "action_page": "subscricoes",
                    }
                )

        for row in self.repo.liabilities():
            interest = safe_float(row_get(row, "interest_rate"))
            balance = safe_float(row_get(row, "balance"))
            if interest >= 8.0 and balance > 0:
                alerts.append(
                    {
                        "key": f"debt:{row_get(row, 'id')}",
                        "severity": "warning" if interest < 15 else "critical",
                        "kind": "debt",
                        "title": "Dívida com juro elevado",
                        "message": f"{row_get(row, 'name')} tem taxa de {interest:.2f}%.",
                        "date": iso(reference),
                        "amount": balance,
                        "category": "Dívidas",
                        "action_page": "carteira",
                    }
                )

        order = {"critical": 0, "warning": 1, "info": 2}
        return sorted(
            alerts,
            key=lambda alert: (
                order.get(str(alert.get("severity")), 3),
                str(alert.get("date") or ""),
            ),
        )

    def compare_periods(
        self,
        current_start: str,
        current_end: str,
        reference_start: str,
        reference_end: str,
    ) -> dict[str, Any]:
        def totals(start: str, end: str) -> dict[str, float]:
            income = safe_float(
                self.repo.scalar(
                    f"SELECT SUM(t.amount) FROM transactions t "
                    f"LEFT JOIN transaction_meta m ON m.transaction_id=t.id "
                    f"WHERE {_FINANCIAL_DATE_SQL}>=? "
                    f"AND {_FINANCIAL_DATE_SQL}<=? "
                    f"AND t.amount>0 AND {_VALID_TRANSACTION_AMOUNT_SQL} "
                    f"AND {_VALID_FINANCIAL_DATE_SQL} "
                    f"AND {_EXTERNAL_FLOW_SQL}",
                    (start, end),
                )
            )
            expenses = safe_float(
                self.repo.scalar(
                    f"SELECT SUM(ABS(t.amount)) FROM transactions t "
                    f"LEFT JOIN transaction_meta m ON m.transaction_id=t.id "
                    f"WHERE {_FINANCIAL_DATE_SQL}>=? "
                    f"AND {_FINANCIAL_DATE_SQL}<=? "
                    f"AND t.amount<0 AND {_VALID_TRANSACTION_AMOUNT_SQL} "
                    f"AND {_VALID_FINANCIAL_DATE_SQL} "
                    f"AND {_EXTERNAL_FLOW_SQL}",
                    (start, end),
                )
            )
            return {
                "income": income,
                "expenses": expenses,
                "savings": income - expenses,
            }

        def categories(start: str, end: str) -> dict[str, float]:
            try:
                end_exclusive = (
                    date.fromisoformat(str(end)[:10]) + timedelta(days=1)
                ).isoformat()
            except (TypeError, ValueError):
                end_exclusive = end
            rows = self.repo.category_spend_with_splits(
                start,
                end_exclusive,
                basis="Pagamento",
                end_exclusive=True,
            )
            return {
                str(row_get(row, "category") or "Outros"): safe_float(
                    row_get(row, "total")
                )
                for row in rows
            }

        current_totals = totals(current_start, current_end)
        reference_totals = totals(reference_start, reference_end)
        current_categories = categories(current_start, current_end)
        reference_categories = categories(reference_start, reference_end)
        configured_categories = {
            str(row_get(row, "name") or "").strip()
            for row in self.repo.category_rows("expense")
            if str(row_get(row, "name") or "").strip()
        }
        rows = []
        all_categories = (
            configured_categories | set(current_categories) | set(reference_categories)
        )
        for category in all_categories:
            current_value = safe_float(current_categories.get(category))
            reference_value = safe_float(reference_categories.get(category))
            delta = current_value - reference_value
            rows.append(
                {
                    "category": category,
                    "current": current_value,
                    "reference": reference_value,
                    "delta": delta,
                    "change_pct": (
                        delta / reference_value * 100.0 if reference_value else None
                    ),
                    "change_basis": (
                        "percentual"
                        if reference_value
                        else "novo"
                        if current_value
                        else "sem_variacao"
                    ),
                }
            )
        rows.sort(
            key=lambda row: (
                -max(row["current"], row["reference"]),
                str(row["category"]).casefold(),
            )
        )

        metrics: dict[str, dict[str, float]] = {}
        for key in ("income", "expenses", "savings"):
            current_value = current_totals[key]
            reference_value = reference_totals[key]
            delta = current_value - reference_value
            metrics[key] = {
                "current": current_value,
                "reference": reference_value,
                "delta": delta,
                "change_pct": (
                    delta / abs(reference_value) * 100.0
                    if reference_value
                    else 100.0
                    if current_value
                    else 0.0
                ),
            }
        return {
            "current": {"start": current_start, "end": current_end},
            "reference": {"start": reference_start, "end": reference_end},
            "metrics": metrics,
            "categories": rows,
        }

    def explain_period_variation(
        self, comparison: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Explain the main drivers without external AI or invented data."""
        metrics = comparison.get("metrics", {}) or {}
        expenses = metrics.get("expenses", {}) or {}
        income = metrics.get("income", {}) or {}
        savings = metrics.get("savings", {}) or {}
        expense_delta = safe_float(expenses.get("delta"))
        insights: list[dict[str, Any]] = [
            {
                "kind": "expenses",
                "title": (
                    "Despesas aumentaram"
                    if expense_delta > 0
                    else "Despesas diminuíram"
                    if expense_delta < 0
                    else "Despesas estáveis"
                ),
                "delta": expense_delta,
                "current": safe_float(expenses.get("current")),
                "reference": safe_float(expenses.get("reference")),
                "change_pct": safe_float(expenses.get("change_pct")),
                "tone": (
                    "negative"
                    if expense_delta > 0
                    else "positive"
                    if expense_delta < 0
                    else "neutral"
                ),
                "message": "Variação total das despesas face ao período de referência.",
            }
        ]

        category_rows = list(comparison.get("categories", []) or [])
        increases = sorted(
            (row for row in category_rows if safe_float(row.get("delta")) > 0),
            key=lambda row: safe_float(row.get("delta")),
            reverse=True,
        )
        decreases = sorted(
            (row for row in category_rows if safe_float(row.get("delta")) < 0),
            key=lambda row: safe_float(row.get("delta")),
        )
        for row in increases[:2]:
            insights.append(
                {
                    "kind": "category",
                    "title": f"Mais gasto em {row.get('category')}",
                    "category": str(row.get("category") or "Outros"),
                    "delta": safe_float(row.get("delta")),
                    "current": safe_float(row.get("current")),
                    "reference": safe_float(row.get("reference")),
                    "change_pct": safe_float(row.get("change_pct")),
                    "tone": "negative",
                    "message": "Esta categoria foi uma das principais causas do aumento.",
                }
            )
        if not increases and decreases:
            for row in decreases[:2]:
                insights.append(
                    {
                        "kind": "category",
                        "title": f"Poupança em {row.get('category')}",
                        "category": str(row.get("category") or "Outros"),
                        "delta": safe_float(row.get("delta")),
                        "current": safe_float(row.get("current")),
                        "reference": safe_float(row.get("reference")),
                        "change_pct": safe_float(row.get("change_pct")),
                        "tone": "positive",
                        "message": "Esta categoria foi uma das principais causas da redução.",
                    }
                )
        elif increases and decreases:
            # Show the largest reductions as well: hiding them made the
            # explanation look one-sided when both directions existed.
            for row in decreases[:2]:
                insights.append(
                    {
                        "kind": "category",
                        "title": f"Poupança em {row.get('category')}",
                        "category": str(row.get("category") or "Outros"),
                        "delta": safe_float(row.get("delta")),
                        "current": safe_float(row.get("current")),
                        "reference": safe_float(row.get("reference")),
                        "change_pct": safe_float(row.get("change_pct")),
                        "tone": "positive",
                        "message": "Esta categoria reduziu e compensa parte do aumento.",
                    }
                )
        # Reconcile the highlighted drivers with the total expense variation
        # so the listed causes never appear to explain more than the total.
        highlighted = insights[1:]  # skip the total-expense entry at index 0.
        highlighted_delta = math.fsum(
            safe_float(row.get("delta"))
            for row in highlighted
            if row.get("kind") == "category"
        )
        unexplained = expense_delta - highlighted_delta
        if abs(unexplained) >= 0.005:
            insights.append(
                {
                    "kind": "reconciliation",
                    "title": "Restantes categorias",
                    "delta": unexplained,
                    "current": safe_float(expenses.get("current")),
                    "reference": safe_float(expenses.get("reference")),
                    "change_pct": None,
                    "tone": (
                        "negative"
                        if unexplained > 0
                        else "positive"
                        if unexplained < 0
                        else "neutral"
                    ),
                    "message": (
                        "A soma das variações destacadas difere da variação total; "
                        "este valor fecha a diferença das categorias não destacadas."
                    ),
                }
            )

        income_delta = safe_float(income.get("delta"))
        if income_delta:
            insights.append(
                {
                    "kind": "income",
                    "title": (
                        "Receitas cresceram"
                        if income_delta > 0
                        else "Receitas diminuíram"
                    ),
                    "delta": income_delta,
                    "current": safe_float(income.get("current")),
                    "reference": safe_float(income.get("reference")),
                    "change_pct": safe_float(income.get("change_pct")),
                    "tone": "positive" if income_delta > 0 else "warning",
                    "message": "A alteração das receitas também influenciou a poupança.",
                }
            )

        savings_delta = safe_float(savings.get("delta"))
        insights.append(
            {
                "kind": "savings",
                "title": (
                    "Poupança melhorou"
                    if savings_delta > 0
                    else "Poupança recuou"
                    if savings_delta < 0
                    else "Poupança sem alteração"
                ),
                "delta": savings_delta,
                "current": safe_float(savings.get("current")),
                "reference": safe_float(savings.get("reference")),
                "change_pct": safe_float(savings.get("change_pct")),
                "tone": (
                    "positive"
                    if savings_delta > 0
                    else "negative"
                    if savings_delta < 0
                    else "neutral"
                ),
                "message": "Resultado combinado das alterações nas receitas e despesas.",
            }
        )
        return insights[:6]

    def annual_report_data(self, year: int) -> dict[str, Any]:
        selected_year = max(1900, min(2999, int(year)))
        start = f"{selected_year}-01-01"
        end = f"{selected_year}-12-31"
        previous_start = f"{selected_year - 1}-01-01"
        previous_end = f"{selected_year - 1}-12-31"
        comparison = self.compare_periods(start, end, previous_start, previous_end)
        monthly_rows = self.repo.q(
            f"""
            SELECT CAST(substr(COALESCE(NULLIF(m.payment_date,''),t.date), 6, 2) AS INTEGER) AS month,
                   COALESCE(SUM(CASE WHEN t.amount>0 THEN t.amount ELSE 0 END),0) AS income,
                   COALESCE(SUM(CASE WHEN t.amount<0 THEN ABS(t.amount) ELSE 0 END),0) AS expenses
            FROM transactions t
            LEFT JOIN transaction_meta m ON m.transaction_id=t.id
            WHERE COALESCE(NULLIF(m.payment_date,''),t.date)>=?
              AND COALESCE(NULLIF(m.payment_date,''),t.date)<=?
              AND {_EXTERNAL_FLOW_SQL}
              AND {_VALID_TRANSACTION_AMOUNT_SQL}
              AND {_VALID_FINANCIAL_DATE_SQL}
            GROUP BY CAST(substr(COALESCE(NULLIF(m.payment_date,''),t.date), 6, 2) AS INTEGER)
            """,
            (start, end),
        )
        totals_by_month = {
            int(row_get(row, "month")): {
                "income": safe_float(row_get(row, "income")),
                "expenses": safe_float(row_get(row, "expenses")),
            }
            for row in monthly_rows
        }
        monthly: list[dict[str, Any]] = []
        for month in range(1, 13):
            values = totals_by_month.get(month, {})
            income = safe_float(values.get("income"))
            expenses = safe_float(values.get("expenses"))
            monthly.append(
                {
                    "month": month,
                    "income": income,
                    "expenses": expenses,
                    "savings": income - expenses,
                }
            )

        investment_state = self.repo.investment_state_as_of(end)
        holdings = [dict(row) for row in investment_state.get("holdings", [])]
        investment_total = safe_float(investment_state.get("total"))
        investment_cost = safe_float(investment_state.get("invested"))
        investment_performance = portfolio_performance_attribution(holdings)
        if not holdings and bool(investment_state.get("available")):
            historical_gain = investment_total - investment_cost
            historical_return = (
                historical_gain / investment_cost * 100.0 if investment_cost else None
            )
            investment_performance.update(
                {
                    "total_value": investment_total,
                    "total_cost_basis": investment_cost,
                    "total_cash_invested": investment_cost,
                    "gross_gain": historical_gain,
                    "net_gain": historical_gain,
                    "gross_return_pct": historical_return,
                    "net_return_pct": historical_return,
                    "weighted_return_pct": historical_return,
                    "fee_impact_pp": 0.0,
                    "total_fees": 0.0,
                    "data_coverage_pct": 100.0 if investment_cost > 0 else 0.0,
                }
            )
        evolution = [
            dict(row)
            for row in self.repo.investment_evolution()
            if start <= str(row.get("date") or row.get("day") or "")[:10] <= end
        ]
        document_summary_rows = self.repo.q(
            f"""
            SELECT COUNT(a.id) AS document_count,
                   COALESCE(SUM(CASE WHEN {sql_finite_number("a.total_amount", non_negative=True)}
                                     THEN a.total_amount ELSE 0 END),0) AS documented_total,
                   COUNT(DISTINCT NULLIF(a.supplier,'')) AS supplier_count,
                   COUNT(DISTINCT a.transaction_id) AS documented_movement_count
            FROM transaction_attachments a
            LEFT JOIN transactions t ON t.id=a.transaction_id
            WHERE COALESCE(NULLIF(a.invoice_date,''), t.date)>=?
              AND COALESCE(NULLIF(a.invoice_date,''), t.date)<=?
              AND {sql_iso_date("COALESCE(NULLIF(a.invoice_date,''), t.date)")}
            """,
            (start, end),
        )
        # ``documented_total`` must count each movement once even when one
        # invoice has several attachments (photos, front and back pages).
        movement_total_rows = self.repo.q(
            f"""
            SELECT COALESCE(SUM(movement_total), 0) AS documented_total
            FROM (
                SELECT a.transaction_id,
                       MAX(CASE WHEN {sql_finite_number("a.total_amount", non_negative=True)}
                                THEN a.total_amount ELSE 0 END) AS movement_total
                FROM transaction_attachments a
                LEFT JOIN transactions t ON t.id=a.transaction_id
                WHERE COALESCE(NULLIF(a.invoice_date,''), t.date)>=?
                  AND COALESCE(NULLIF(a.invoice_date,''), t.date)<=?
                  AND {sql_iso_date("COALESCE(NULLIF(a.invoice_date,''), t.date)")}
                GROUP BY a.transaction_id
            )
            """,
            (start, end),
        )
        document_summary = (
            dict(document_summary_rows[0])
            if document_summary_rows
            else {
                "document_count": 0,
                "documented_total": 0.0,
                "supplier_count": 0,
                "documented_movement_count": 0,
            }
        )
        document_summary["documented_total"] = safe_float(
            movement_total_rows[0]["documented_total"] if movement_total_rows else 0.0
        )
        suppliers = [
            dict(row)
            for row in self.repo.q(
                f"""
                SELECT COALESCE(NULLIF(supplier,''),'Sem fornecedor') AS supplier,
                       COUNT(*) AS documents,
                       COALESCE(SUM(supplier_total), 0) AS total
                FROM (
                    SELECT a.supplier AS supplier,
                           a.transaction_id,
                           MAX(CASE WHEN {sql_finite_number("a.total_amount", non_negative=True)}
                                    THEN a.total_amount ELSE 0 END) AS supplier_total,
                           COUNT(*) AS documents
                    FROM transaction_attachments a
                    LEFT JOIN transactions t ON t.id=a.transaction_id
                    WHERE COALESCE(NULLIF(a.invoice_date,''), t.date)>=?
                      AND COALESCE(NULLIF(a.invoice_date,''), t.date)<=?
                      AND {sql_iso_date("COALESCE(NULLIF(a.invoice_date,''), t.date)")}
                    GROUP BY a.supplier, a.transaction_id
                )
                GROUP BY COALESCE(NULLIF(supplier,''),'Sem fornecedor')
                ORDER BY documents DESC, total DESC
                LIMIT 10
                """,
                (start, end),
            )
        ]
        return {
            "year": selected_year,
            "start": start,
            "end": end,
            "comparison": comparison,
            "explanations": self.explain_period_variation(comparison),
            "monthly": monthly,
            "holdings": holdings,
            "investment_total": investment_total,
            "investment_cost": investment_cost,
            "investment_gain": investment_total - investment_cost,
            "investment_fees": safe_float(investment_performance.get("total_fees")),
            "investment_net_gain": (
                investment_performance.get("net_gain")
                if investment_performance.get("net_return_pct") is not None
                else None
            ),
            "investment_gross_return_pct": investment_performance.get(
                "gross_return_pct"
            ),
            "investment_net_return_pct": investment_performance.get("net_return_pct"),
            "investment_data_coverage_pct": safe_float(
                investment_performance.get("data_coverage_pct")
            ),
            "investment_fee_impact_pp": safe_float(
                investment_performance.get("fee_impact_pp")
            ),
            "investment_attribution": list(investment_performance.get("rows", [])),
            "investment_available": bool(investment_state.get("available")),
            "investment_as_of": str(investment_state.get("as_of") or ""),
            "investment_source": str(investment_state.get("source") or "unavailable"),
            "investment_evolution": evolution,
            "documents": document_summary,
            "document_suppliers": suppliers,
        }
