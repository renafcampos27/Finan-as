from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from services.finance_math import effective_annual_to_periodic_rate

WEEKS_PER_MONTH = 52.1429 / 12.0
DAYS_PER_MONTH = 365.2425 / 12.0
DEFAULT_COST_INFLATION_PCT = 0.0
DEFAULT_INVESTMENT_RATE_PCT = 5.0
HORIZONS: tuple[tuple[int, str], ...] = (
    (1, "1 mês"),
    (12, "1 ano"),
    (24, "2 anos"),
    (36, "3 anos"),
    (60, "5 anos"),
    (120, "10 anos"),
    (360, "30 anos"),
)


@dataclass(slots=True, frozen=True)
class HabitPreset:
    key: str
    name: str
    emoji: str
    unit_cost: float
    frequency: float
    frequency_unit: str
    description: str
    gambling: bool = False
    win_probability_pct: float = 0.0
    payout_multiplier: float = 1.0
    alternative_unit_cost: float = 0.0
    alternative_name: str = "Não consumir"

    @property
    def display_name(self) -> str:
        return f"{self.emoji} {self.name}"


HABIT_PRESETS: tuple[HabitPreset, ...] = (
    HabitPreset(
        "casino",
        "Casino",
        "🎰",
        30.0,
        1.0,
        "week",
        "Sessões de casino com retorno incerto e vantagem estrutural da casa.",
        True,
        30.0,
        1.50,
        0.0,
        "Não jogar",
    ),
    HabitPreset(
        "betting",
        "Apostas",
        "⚽",
        10.0,
        2.0,
        "week",
        "Apostas desportivas avaliadas por probabilidade e multiplicador médio.",
        True,
        38.0,
        1.80,
        0.0,
        "Não apostar",
    ),
    HabitPreset(
        "dinners",
        "Jantares fora",
        "🍽️",
        25.0,
        1.0,
        "week",
        "Refeições fora de casa que podem ser reduzidas sem desaparecer por completo.",
        alternative_unit_cost=6.0,
        alternative_name="Refeição em casa",
    ),
    HabitPreset(
        "beer",
        "Cervejas",
        "🍺",
        2.50,
        3.0,
        "week",
        "Consumo regular de cerveja fora ou em compras ocasionais.",
        alternative_unit_cost=0.90,
        alternative_name="Alternativa em casa",
    ),
    HabitPreset(
        "coffee",
        "Cafés",
        "☕",
        1.10,
        7.0,
        "week",
        "Pequena despesa recorrente cujo efeito aparece sobretudo no longo prazo.",
        alternative_unit_cost=0.25,
        alternative_name="Café em casa",
    ),
    HabitPreset(
        "snacks",
        "Lanches",
        "🥐",
        3.50,
        3.0,
        "week",
        "Lanches comprados fora de casa durante a semana.",
        alternative_unit_cost=1.0,
        alternative_name="Lanche preparado",
    ),
    HabitPreset(
        "sweets",
        "Doces",
        "🍫",
        1.75,
        4.0,
        "week",
        "Compras pequenas de doces e sobremesas.",
        alternative_unit_cost=0.0,
        alternative_name="Não comprar",
    ),
    HabitPreset(
        "canteen",
        "Cantina",
        "🍲",
        4.50,
        5.0,
        "week",
        "Almoços na cantina; útil para comparar com alternativas mais económicas.",
        alternative_unit_cost=2.50,
        alternative_name="Refeição preparada",
    ),
    HabitPreset(
        "subscriptions",
        "Subscrições",
        "📺",
        11.99,
        3.0,
        "month",
        "Serviços digitais, aplicações ou clubes pagos todos os meses.",
        alternative_unit_cost=0.0,
        alternative_name="Cancelar serviços",
    ),
    HabitPreset(
        "delivery",
        "Take-away",
        "🛵",
        17.50,
        1.0,
        "week",
        "Pedidos de comida e respetivas taxas de entrega.",
        alternative_unit_cost=6.0,
        alternative_name="Cozinhar em casa",
    ),
    HabitPreset(
        "impulse",
        "Compras impulso",
        "🛍️",
        35.0,
        2.0,
        "month",
        "Compras não planeadas que parecem pequenas quando vistas isoladamente.",
        alternative_unit_cost=0.0,
        alternative_name="Não comprar",
    ),
    HabitPreset(
        "ride_hailing",
        "TVDE / táxi",
        "🚕",
        8.0,
        4.0,
        "month",
        "Deslocações por aplicação ou táxi que poderiam ter alternativa.",
        alternative_unit_cost=2.0,
        alternative_name="Transporte público",
    ),
)

HABIT_PRESETS_BY_KEY = {preset.key: preset for preset in HABIT_PRESETS}


@dataclass(slots=True, frozen=True)
class HabitSimulationInputs:
    unit_cost: float
    frequency: float
    frequency_unit: str
    alternative_unit_cost: float = 0.0
    annual_inflation_pct: float = DEFAULT_COST_INFLATION_PCT
    annual_investment_rate_pct: float = DEFAULT_INVESTMENT_RATE_PCT
    gambling: bool = False
    win_probability_pct: float = 0.0
    payout_multiplier: float = 1.0
    monthly_income: float = 0.0


def occurrences_per_month(frequency: float, frequency_unit: str) -> float:
    value = max(0.0, float(frequency or 0.0))
    unit = str(frequency_unit or "week").strip().lower()
    if unit == "day":
        return value * DAYS_PER_MONTH
    if unit == "month":
        return value
    return value * WEEKS_PER_MONTH


def _return_ratio(
    inputs: HabitSimulationInputs, probability_delta: float = 0.0
) -> float:
    if not inputs.gambling:
        return 0.0
    probability = max(
        0.0,
        min(49.0, float(inputs.win_probability_pct) + float(probability_delta)),
    )
    multiplier = max(1.0, min(2.0, float(inputs.payout_multiplier)))
    return min(0.99, probability / 100.0 * multiplier)


def _scenario_frequency(frequency: float, factor: float) -> float:
    current = max(0.0, float(frequency or 0.0))
    # BUG-HABIT-01: keep fractional frequencies (1x/month reduced by 25% is
    # 0.75x/month, not 0) so unit-frequency habits survive reduction scenarios.
    return max(0.0, current * max(0.0, factor))


def _build_monthly_series(
    inputs: HabitSimulationInputs,
    total_months: int = 360,
) -> tuple[list[dict[str, float | str]], dict[str, float]]:
    count = max(1, int(total_months))
    current_frequency = max(0.0, float(inputs.frequency or 0.0))
    reduced_25_frequency = _scenario_frequency(current_frequency, 0.75)
    half_frequency = _scenario_frequency(current_frequency, 0.50)
    current_occurrences = occurrences_per_month(
        current_frequency,
        inputs.frequency_unit,
    )
    reduced_25_occurrences = occurrences_per_month(
        reduced_25_frequency,
        inputs.frequency_unit,
    )
    half_occurrences = occurrences_per_month(
        half_frequency,
        inputs.frequency_unit,
    )
    unit_cost = max(0.0, float(inputs.unit_cost))
    alternative_unit_cost = (
        0.0
        if inputs.gambling
        else max(0.0, min(unit_cost, float(inputs.alternative_unit_cost or 0.0)))
    )
    inflation = max(-0.95, float(inputs.annual_inflation_pct) / 100.0)
    monthly_rate = effective_annual_to_periodic_rate(
        max(-99.0, float(inputs.annual_investment_rate_pct))
    )
    base_return_ratio = _return_ratio(inputs)
    best_return_ratio = _return_ratio(inputs, 8.0)
    worst_return_ratio = _return_ratio(inputs, -8.0)

    gross_total = 0.0
    expected_return_total = 0.0
    cost_current_total = 0.0
    cost_reduced_25_total = 0.0
    cost_half_total = 0.0
    cost_alternative_total = 0.0
    cost_low_total = 0.0
    cost_high_total = 0.0
    savings_reduced_25_total = 0.0
    savings_half_total = 0.0
    savings_alternative_total = 0.0
    opportunity_value = 0.0
    opportunity_reduced_25 = 0.0
    opportunity_half = 0.0
    series: list[dict[str, float | str]] = []

    for month_index in range(count):
        price_factor = (1.0 + inflation) ** (month_index / 12.0)
        gross = unit_cost * current_occurrences * price_factor
        expected_return = gross * base_return_ratio
        current_cost = max(0.0, gross - expected_return)
        alternative_all = alternative_unit_cost * current_occurrences * price_factor

        reduced_25_gross = unit_cost * reduced_25_occurrences * price_factor
        reduced_25_current_cost = max(
            0.0,
            reduced_25_gross - reduced_25_gross * base_return_ratio,
        )
        reduced_25_alternative_cost = (
            alternative_unit_cost
            * max(0.0, current_occurrences - reduced_25_occurrences)
            * price_factor
        )
        reduced_25_cost = reduced_25_current_cost + reduced_25_alternative_cost

        half_gross = unit_cost * half_occurrences * price_factor
        half_current_cost = max(0.0, half_gross - half_gross * base_return_ratio)
        half_alternative_cost = (
            alternative_unit_cost
            * max(0.0, current_occurrences - half_occurrences)
            * price_factor
        )
        half_cost = half_current_cost + half_alternative_cost

        if inputs.gambling:
            lower_cost = max(0.0, gross * (1.0 - best_return_ratio))
            higher_cost = max(0.0, gross * (1.0 - worst_return_ratio))
        else:
            lower_cost = gross * 0.85
            higher_cost = gross * 1.15

        saved_reduced_25 = max(0.0, current_cost - reduced_25_cost)
        saved_half = max(0.0, current_cost - half_cost)
        saved_alternative = max(0.0, current_cost - alternative_all)

        gross_total += gross
        expected_return_total += expected_return
        cost_current_total += current_cost
        cost_reduced_25_total += reduced_25_cost
        cost_half_total += half_cost
        cost_alternative_total += alternative_all
        cost_low_total += lower_cost
        cost_high_total += higher_cost
        savings_reduced_25_total += saved_reduced_25
        savings_half_total += saved_half
        savings_alternative_total += saved_alternative
        opportunity_value = opportunity_value * (1.0 + monthly_rate) + saved_alternative
        opportunity_reduced_25 = (
            opportunity_reduced_25 * (1.0 + monthly_rate) + saved_reduced_25
        )
        opportunity_half = opportunity_half * (1.0 + monthly_rate) + saved_half
        months = month_index + 1
        series.append(
            {
                "months": float(months),
                "label": f"Mês {months}",
                "gross_spend": gross_total,
                "expected_return": expected_return_total,
                "cost_base": cost_current_total,
                "cost_current": cost_current_total,
                "cost_reduce_25": cost_reduced_25_total,
                "cost_half": cost_half_total,
                "cost_alternative": cost_alternative_total,
                "cost_low": min(cost_low_total, cost_high_total),
                "cost_high": max(cost_low_total, cost_high_total),
                "savings_reduce_25": savings_reduced_25_total,
                "savings_half": savings_half_total,
                "savings_alternative": savings_alternative_total,
                "opportunity_value": opportunity_value,
                "opportunity_reduce_25": opportunity_reduced_25,
                "opportunity_half": opportunity_half,
                "investment_gain": opportunity_value - savings_alternative_total,
            }
        )

    return series, {
        "current": current_frequency,
        "reduce_25": reduced_25_frequency,
        "half": half_frequency,
        "alternative": 0.0,
    }


def simulate_habit(
    preset: HabitPreset,
    inputs: HabitSimulationInputs,
) -> dict[str, Any]:
    """Build one monthly projection and snapshot it at the requested horizons."""
    monthly_series, scenario_frequencies = _build_monthly_series(
        inputs,
        HORIZONS[-1][0],
    )
    rows: list[dict[str, Any]] = []
    for months, label in HORIZONS:
        row = dict(monthly_series[months - 1])
        row["months"] = months
        row["label"] = label
        rows.append(row)

    monthly = rows[0]
    annual = rows[1]
    ten_year = next(row for row in rows if row["months"] == 120)
    thirty_year = rows[-1]
    monthly_income = max(0.0, float(inputs.monthly_income or 0.0))
    monthly_cost = float(monthly["cost_base"])
    income_share_pct = monthly_cost / monthly_income * 100.0 if monthly_income else 0.0
    occurrence_income_share_pct = (
        max(0.0, float(inputs.unit_cost)) / monthly_income * 100.0
        if monthly_income
        else 0.0
    )
    annual_income = monthly_income * 12.0
    annual_income_share_pct = (
        float(annual["cost_base"]) / annual_income * 100.0 if annual_income else 0.0
    )
    income_day_value = monthly_income / 22.0 if monthly_income else 0.0
    income_days = monthly_cost / income_day_value if income_day_value else 0.0
    expected_return_ratio = _return_ratio(inputs)

    if monthly_income:
        if income_share_pct < 1.0:
            impact_tone, impact_label = "positive", "Impacto leve"
        elif income_share_pct < 5.0:
            impact_tone, impact_label = "neutral", "Impacto moderado"
        elif income_share_pct < 10.0:
            impact_tone, impact_label = "warning", "Impacto elevado"
        else:
            impact_tone, impact_label = "negative", "Impacto crítico"
    elif monthly_cost < 25.0:
        impact_tone, impact_label = "positive", "Impacto leve"
    elif monthly_cost < 100.0:
        impact_tone, impact_label = "neutral", "Impacto moderado"
    elif monthly_cost < 250.0:
        impact_tone, impact_label = "warning", "Impacto elevado"
    else:
        impact_tone, impact_label = "negative", "Impacto crítico"

    return {
        "preset": asdict(preset),
        "inputs": asdict(inputs),
        "rows": rows,
        "monthly_series": monthly_series,
        "scenario_frequencies": scenario_frequencies,
        "monthly_cost": monthly_cost,
        "monthly_alternative_cost": float(monthly["cost_alternative"]),
        "monthly_savings_potential": float(monthly["savings_alternative"]),
        "annual_cost": float(annual["cost_base"]),
        "annual_savings_potential": float(annual["savings_alternative"]),
        "ten_year_cost": float(ten_year["cost_base"]),
        "thirty_year_opportunity": float(thirty_year["opportunity_value"]),
        "expected_return_ratio": expected_return_ratio,
        "expected_loss_ratio": max(0.0, 1.0 - expected_return_ratio),
        "income_share_pct": income_share_pct,
        "occurrence_income_share_pct": occurrence_income_share_pct,
        "annual_income_share_pct": annual_income_share_pct,
        "income_days": income_days,
        "impact_tone": impact_tone,
        "impact_label": impact_label,
        "quarter_reduction_monthly": float(monthly["savings_reduce_25"]),
        "quarter_reduction_five_year_value": next(
            row for row in rows if row["months"] == 60
        )["opportunity_reduce_25"],
    }


__all__ = [
    "DEFAULT_COST_INFLATION_PCT",
    "DEFAULT_INVESTMENT_RATE_PCT",
    "HABIT_PRESETS",
    "HABIT_PRESETS_BY_KEY",
    "HORIZONS",
    "HabitPreset",
    "HabitSimulationInputs",
    "occurrences_per_month",
    "simulate_habit",
]
