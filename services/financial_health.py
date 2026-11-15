"""Explainable financial health scoring and household stress testing.

The calculations in this module are deliberately local and deterministic.  They
turn data already stored by the application into presentation-ready scores,
causes and actions without inventing transactions or relying on an external AI.
"""

from __future__ import annotations

import math
import statistics
from calendar import monthrange
from collections import OrderedDict
from datetime import date
from typing import TYPE_CHECKING, Any, Optional, TypeVar

from database.validation import sql_external_financial_flow
from utils.helpers import add_months, eur, parse_month_key, row_get, safe_float, strict_float

if TYPE_CHECKING:
    from database.repo import Repo
    from services.payments import PaymentsService


KT = TypeVar("KT")
VT = TypeVar("VT")


class BoundedLRUCache(OrderedDict[KT, VT]):
    """Lightweight bounded LRU cache preventing unbounded memory growth."""

    def __init__(self, maxsize: int = 128) -> None:
        super().__init__()
        self.maxsize = max(8, int(maxsize))

    def __setitem__(self, key: KT, value: VT) -> None:
        if key in self:
            self.move_to_end(key)
        super().__setitem__(key, value)
        if len(self) > self.maxsize:
            self.popitem(last=False)

    def __getitem__(self, key: KT) -> VT:
        val = super().__getitem__(key)
        self.move_to_end(key)
        return val


_EXTERNAL_FLOW_SQL = sql_external_financial_flow("t.category")


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, safe_float(value)))


def _mean(values: list[float]) -> float:
    return statistics.fmean(values) if values else 0.0


def _weighted_mean(values: list[float]) -> float:
    """Weight recent monthly observations more heavily than older ones."""

    if not values:
        return 0.0
    weights = list(range(1, len(values) + 1))
    return sum(value * weight for value, weight in zip(values, weights)) / sum(weights)


def _correlation(left: list[float], right: list[float]) -> float:
    """Return a bounded Pearson correlation without failing on flat series."""

    size = min(len(left), len(right))
    if size < 3:
        return 0.0
    x_values = left[-size:]
    y_values = right[-size:]
    x_mean = _mean(x_values)
    y_mean = _mean(y_values)
    x_delta = [value - x_mean for value in x_values]
    y_delta = [value - y_mean for value in y_values]
    denominator = math.sqrt(
        sum(value * value for value in x_delta)
        * sum(value * value for value in y_delta)
    )
    if denominator <= 0:
        return 0.0
    return _clamp(
        sum(x * y for x, y in zip(x_delta, y_delta)) / denominator,
        -0.85,
        0.85,
    )


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = _clamp(percentile, 0.0, 1.0) * (len(ordered) - 1)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _grade(score: float) -> str:
    value = safe_float(score)
    if value >= 90:
        return "A+"
    if value >= 82:
        return "A"
    if value >= 75:
        return "B+"
    if value >= 68:
        return "B"
    if value >= 60:
        return "C+"
    if value >= 50:
        return "C"
    if value >= 40:
        return "D"
    return "E"


def _tone(score: float) -> tuple[str, str, str]:
    value = safe_float(score)
    if value >= 80:
        return "positive", "Forte", "🟢"
    if value >= 60:
        return "neutral", "Estável", "🔵"
    if value >= 40:
        return "warning", "Atenção", "🟡"
    return "negative", "Crítico", "🔴"


def _month_key(value: str) -> str:
    return parse_month_key(value)


class FinancialHealthService:
    """Single source of truth for Score Financeiro 2.0 and resilience metrics."""

    DIMENSION_META: dict[str, dict[str, str]] = {
        "liquidity": {
            "name": "Liquidez e reserva",
            "icon": "🛟",
            "action": "Reforçar o fundo de emergência até cobrir 3–6 meses de despesas.",
            "page": "centro",
        },
        "savings": {
            "name": "Poupança consistente",
            "icon": "🌱",
            "action": "Automatizar uma transferência mensal e estabilizar a taxa de poupança.",
            "page": "planeador",
        },
        "budget": {
            "name": "Disciplina orçamental",
            "icon": "🎯",
            "action": "Ajustar limites das categorias com maior desvio e cobrir gastos sem orçamento.",
            "page": "orcamentos",
        },
        "debt": {
            "name": "Endividamento",
            "icon": "⚖️",
            "action": "Priorizar dívida cara e reduzir a prestação mensal face ao rendimento.",
            "page": "carteira",
        },
        "fixed_costs": {
            "name": "Custos fixos",
            "icon": "🔁",
            "action": "Rever subscrições e encargos recorrentes com pouco valor de utilização.",
            "page": "subscricoes",
        },
        "diversification": {
            "name": "Diversificação",
            "icon": "🧺",
            "action": "Reduzir concentração e distribuir a carteira por classes adequadas ao teu risco.",
            "page": "carteira",
        },
        "data_quality": {
            "name": "Qualidade dos dados",
            "icon": "🧩",
            "action": "Corrigir movimentos sem categoria e confirmar possíveis duplicados.",
            "page": "movimentos",
        },
    }

    WEIGHTS = {
        "liquidity": 20.0,
        "savings": 20.0,
        "budget": 15.0,
        "debt": 15.0,
        "fixed_costs": 10.0,
        "diversification": 10.0,
        "data_quality": 10.0,
    }

    def __init__(self, repo: "Repo", payments_service: "PaymentsService") -> None:
        self.repo = repo
        self.payments_service = payments_service
        self._totals_cache = BoundedLRUCache[str, dict[str, float]](maxsize=128)
        self._net_worth_cache = BoundedLRUCache[str, dict[str, Any]](maxsize=128)
        self._budget_cache = BoundedLRUCache[
            tuple[str, float], tuple[float, str, dict[str, float]]
        ](maxsize=128)
        self._quality_cache = BoundedLRUCache[str, tuple[float, str, dict[str, float]]](
            maxsize=128
        )
        self._snapshot_cache = BoundedLRUCache[str, dict[str, Any]](maxsize=128)

    def clear_cache(self) -> None:
        """Invalidate all internal memoized calculations."""
        self._totals_cache.clear()
        self._net_worth_cache.clear()
        self._budget_cache.clear()
        self._quality_cache.clear()
        self._snapshot_cache.clear()

    @staticmethod
    def _month_keys(end_key: str, count: int) -> list[str]:
        key = _month_key(end_key)
        end = date(int(key[:4]), int(key[5:7]), 1)
        return [
            add_months(end, offset).strftime("%Y-%m")
            for offset in range(-(max(1, count) - 1), 1)
        ]

    def _totals(self, month_key: str) -> dict[str, float]:
        key = _month_key(month_key)
        if key in self._totals_cache:
            return self._totals_cache[key]
        start, end, _ = self.repo.month_period(int(key[:4]), int(key[5:7]))
        if key == date.today().strftime("%Y-%m"):
            end = min(end, date.today().isoformat())
        values = self.repo.totals_for_payment_period(start, end)
        res = {
            "income": safe_float(values.get("income")),
            "expenses": safe_float(values.get("expenses")),
            "savings": safe_float(values.get("savings")),
        }
        self._totals_cache[key] = res
        return res

    def _monthly_rows(
        self, end_key: str, count: int = 6
    ) -> list[dict[str, float | str]]:
        rows: list[dict[str, float | str]] = []
        for key in self._month_keys(end_key, count):
            totals = self._totals(key)
            income = totals["income"]
            rows.append(
                {
                    "month": key,
                    "is_complete": key < date.today().strftime("%Y-%m"),
                    **totals,
                    "savings_rate": (
                        totals["savings"] / income * 100.0 if income else 0.0
                    ),
                }
            )
        return rows

    def _net_worth(self, month_key: str) -> dict[str, Any]:
        key = _month_key(month_key)
        if key in self._net_worth_cache:
            return self._net_worth_cache[key]
        # Importing here avoids a circular dependency with AnalyticsService.
        from services.financial_intelligence import FinancialIntelligenceService

        last_day = monthrange(int(key[:4]), int(key[5:7]))[1]
        as_of = date(int(key[:4]), int(key[5:7]), last_day)
        res = FinancialIntelligenceService(self.repo).net_worth_snapshot(as_of)
        self._net_worth_cache[key] = res
        return res

    def _budget_metrics(
        self, month_key: str, expenses: float
    ) -> tuple[float, str, dict[str, float]]:

        key = _month_key(month_key)
        cache_key = (key, round(float(expenses), 2))
        if cache_key in self._budget_cache:
            return self._budget_cache[cache_key]
        start, end, _ = self.repo.month_period(int(key[:4]), int(key[5:7]))
        if key == date.today().strftime("%Y-%m"):
            end = min(end, date.today().isoformat())
        budgets = {
            str(name): max(0.0, safe_float(value))
            for name, value in self.repo.category_budgets("expense", key).items()
            if safe_float(value) > 0
        }
        spending = {
            str(name): safe_float(value)
            for name, value, _colour in self.repo.top_categories_payment_period(
                start, end
            )
        }
        total_budget = sum(budgets.values())
        budgeted_spend = sum(spending.get(name, 0.0) for name in budgets)
        coverage = budgeted_spend / expenses * 100.0 if expenses else 100.0
        utilisation = budgeted_spend / total_budget * 100.0 if total_budget else 0.0
        if total_budget <= 0:
            res = (
                42.0,
                "Ainda não existem limites por categoria; o score mantém uma leitura neutra.",
                {
                    "total_budget": 0.0,
                    "budget_coverage_pct": 0.0,
                    "budget_utilisation_pct": 0.0,
                },
            )
            self._budget_cache[cache_key] = res
            return res
        if utilisation <= 80:
            execution = 100.0
        elif utilisation <= 100:
            execution = 100.0 - (utilisation - 80.0) * 2.0
        else:
            execution = max(0.0, 60.0 - (utilisation - 100.0) * 3.0)
        raw = execution * 0.75 + _clamp(coverage) * 0.25
        reading = (
            f"{utilisation:.1f}% do orçamento usado; "
            f"{coverage:.1f}% das despesas estão cobertas por limites."
        ).replace(".", ",")
        res = (
            _clamp(raw),
            reading,
            {
                "total_budget": total_budget,
                "budget_coverage_pct": coverage,
                "budget_utilisation_pct": utilisation,
            },
        )
        self._budget_cache[cache_key] = res
        return res

    def _quality_metrics(self, end_key: str) -> tuple[float, str, dict[str, float]]:
        key = _month_key(end_key)
        if key in self._quality_cache:
            return self._quality_cache[key]
        keys = self._month_keys(key, 6)
        start = f"{keys[0]}-01"
        last_key = keys[-1]
        end = f"{last_key}-{monthrange(int(last_key[:4]), int(last_key[5:7]))[1]:02d}"
        if last_key == date.today().strftime("%Y-%m"):
            end = min(end, date.today().isoformat())
        counts = self.repo.q(
            """
            SELECT COUNT(*) AS total,

                   COALESCE(SUM(CASE WHEN TRIM(IFNULL(t.category,''))=''
                        OR LOWER(TRIM(t.category)) IN ('sem categoria','não definido','nao definido')
                        THEN 1 ELSE 0 END), 0) AS uncategorized
            FROM transactions t
            LEFT JOIN transaction_meta m ON m.transaction_id=t.id
            WHERE COALESCE(NULLIF(m.payment_date,''),t.date)>=?
              AND COALESCE(NULLIF(m.payment_date,''),t.date)<=?
            """,
            (start, end),
        )
        total = int(safe_float(counts[0]["total"])) if counts else 0
        uncategorized = int(safe_float(counts[0]["uncategorized"])) if counts else 0
        duplicate_groups = int(
            safe_float(
                self.repo.scalar(
                    """
                    SELECT COUNT(*) FROM (
                        SELECT COALESCE(NULLIF(m.payment_date,''),t.date) AS financial_date,
                               t.description, t.amount
                        FROM transactions t
                        LEFT JOIN transaction_meta m ON m.transaction_id=t.id
                        WHERE COALESCE(NULLIF(m.payment_date,''),t.date)>=?
                          AND COALESCE(NULLIF(m.payment_date,''),t.date)<=?
                        GROUP BY financial_date, t.description, t.amount
                        HAVING COUNT(*)>1
                    )
                    """,
                    (start, end),
                )
            )
        )
        if total <= 0:
            res = (
                25.0,
                "Sem movimentos suficientes para validar a qualidade.",
                {
                    "transaction_count": 0.0,
                    "categorized_pct": 0.0,
                    "duplicate_groups": 0.0,
                },
            )
            self._quality_cache[key] = res
            return res
        categorized_pct = (total - uncategorized) / total * 100.0
        duplicate_penalty = min(35.0, duplicate_groups / total * 180.0)
        raw = _clamp(categorized_pct - duplicate_penalty)
        reading = (
            f"{categorized_pct:.1f}% categorizados; "
            f"{duplicate_groups} grupo(s) potencialmente duplicado(s)."
        ).replace(".", ",")
        res = (
            raw,
            reading,
            {
                "transaction_count": float(total),
                "categorized_pct": categorized_pct,
                "duplicate_groups": float(duplicate_groups),
            },
        )
        self._quality_cache[key] = res
        return res

    def _score_snapshot(self, month_key: str) -> dict[str, Any]:
        key = _month_key(month_key)
        if key in self._snapshot_cache:
            return self._snapshot_cache[key]
        _month_start, month_end, _ = self.repo.month_period(
            int(key[:4]),
            int(key[5:7]),
        )

        data_cutoff = min(date.fromisoformat(month_end), date.today()).isoformat()
        data_rows = self.repo.q(
            f"""
            SELECT MAX(COALESCE(NULLIF(m.payment_date,''),t.date)) AS data_as_of
            FROM transactions t
            LEFT JOIN transaction_meta m ON m.transaction_id=t.id
            WHERE COALESCE(NULLIF(m.payment_date,''),t.date)<=?
              AND {_EXTERNAL_FLOW_SQL}
              AND DATE(COALESCE(NULLIF(m.payment_date,''),t.date)) IS NOT NULL
            """,
            (data_cutoff,),
        )
        data_as_of = str(data_rows[0]["data_as_of"] or "")[:10] if data_rows else ""
        totals = self._totals(key)
        rows = self._monthly_rows(key, 6)
        active_rows = [
            row
            for row in rows
            if safe_float(row["income"]) > 0 or safe_float(row["expenses"]) > 0
        ]
        baseline_rows = [
            row for row in active_rows
            if str(row.get("month", "")) < date.today().strftime("%Y-%m")
        ]
        income_values = [
            safe_float(row["income"])
            for row in baseline_rows
            if safe_float(row["income"]) > 0
        ]
        expense_values = [safe_float(row["expenses"]) for row in baseline_rows]
        saving_rates = [
            safe_float(row["savings_rate"])
            for row in baseline_rows
            if safe_float(row["income"]) > 0
        ]
        average_income = _mean(income_values)
        average_expenses = _mean(expense_values)
        average_rate = _mean(saving_rates)
        positive_ratio = (
            sum(1 for value in saving_rates if value > 0) / len(saving_rates)
            if saving_rates
            else 0.0
        )
        rate_stability = (
            max(0.0, 1.0 - statistics.pstdev(saving_rates) / 35.0)
            if len(saving_rates) > 1
            else (0.5 if saving_rates else 0.0)
        )

        net_worth = self._net_worth(key)
        liquid_assets = max(0.0, safe_float(net_worth.get("liquid_assets")))
        emergency_months = (
            liquid_assets / average_expenses if average_expenses > 0 else 0.0
        )
        liquidity_raw = _clamp(emergency_months / 6.0 * 100.0)
        liquidity_reading = (
            f"Reserva líquida para {emergency_months:.1f} meses; objetivo recomendado: 3–6."
        ).replace(".", ",")

        net_worth_value = safe_float(net_worth.get("net_worth"))
        annual_exp = average_expenses * 12.0
        coverage_years = (
            net_worth_value / annual_exp
            if annual_exp > 0
            else (10.0 if net_worth_value > 0 else 0.0)
        )

        if average_income <= 0 or not saving_rates:
            if coverage_years >= 5.0:
                savings_raw = 100.0
                savings_reading = (
                    f"Sem rendimento ativo registado; cobertura patrimonial de {coverage_years:.1f} anos "
                    f"(independência financeira / reforma assegurada)."
                ).replace(".", ",")
            elif coverage_years >= 1.0:
                savings_raw = _clamp(coverage_years / 5.0 * 100.0)
                savings_reading = (
                    f"Sem rendimento ativo registado; património cobre {coverage_years:.1f} anos "
                    f"de despesas ({savings_raw:.0f}/100)."
                ).replace(".", ",")
            else:
                savings_raw = 0.0
                savings_reading = "Sem rendimento ativo e cobertura patrimonial inferior a 1 ano."
        else:
            savings_raw = _clamp(
                _clamp(average_rate / 20.0 * 100.0) * 0.70
                + positive_ratio * 20.0
                + rate_stability * 10.0
            )
            savings_reading = (
                f"Taxa média {average_rate:.1f}%; poupança positiva em "
                f"{positive_ratio * 100:.0f}% dos meses com rendimento."
            ).replace(".", ",")

        budget_raw, budget_reading, budget_metrics = self._budget_metrics(
            key, totals["expenses"]
        )

        debt_payment = max(0.0, safe_float(net_worth.get("monthly_debt_payment")))
        debt_service_ratio = (
            debt_payment / average_income * 100.0 if average_income else (100.0 if debt_payment > 0 else 0.0)
        )
        debt_ratio = max(0.0, safe_float(net_worth.get("debt_ratio_pct")))
        total_liabilities = max(0.0, safe_float(net_worth.get("total_liabilities")))
        liabilities_available = bool(net_worth.get("liabilities_available"))
        if not liabilities_available:
            debt_raw = 45.0
        elif total_liabilities <= 0:
            # HEALTH-01: no debt at all is good even when net worth is
            # negative (e.g. low cash month) — keep the positive reading.
            debt_raw = 100.0
        elif net_worth_value <= 0.0:
            # HEALTH-01: insolvent user (liabilities >= assets) must score
            # 0, never inherit a partial score from ratio saturation.
            debt_raw = 0.0
        elif average_income <= 0:
            if coverage_years >= 5.0 and debt_ratio <= 30.0:
                debt_raw = _clamp(100.0 - debt_ratio * 1.5)
            else:
                debt_raw = 5.0
        else:
            debt_raw = _clamp(
                100.0
                - max(0.0, debt_service_ratio - 8.0) * 2.6
                - min(38.0, debt_ratio * 0.45)
            )
        if not liabilities_available:
            debt_reading = "Sem snapshot histórico de dívidas; dimensão mantida neutra."
        elif net_worth_value <= 0.0 and total_liabilities > 0:
            debt_reading = (
                "Passivos igualam ou excedem os ativos (património líquido "
                f"negativo: {net_worth_value:.2f} €). Prioridade: plano de "
                "redução de dívida."
            )
        else:
            debt_reading = (
                f"Prestações representam {debt_service_ratio:.1f}% do rendimento médio; "
                f"dívida equivale a {debt_ratio:.1f}% dos ativos."
            )
        debt_reading = debt_reading.replace(".", ",")

        subscriptions_available = bool(net_worth.get("subscriptions_available"))
        if subscriptions_available:
            subscriptions = sum(
                self.payments_service.subscription_monthly_amount(row)
                for row in net_worth.get("subscription_rows", [])
                if int(row_get(row, "active", 0) or 0) == 1
            )
        else:
            start, end, _ = self.repo.month_period(int(key[:4]), int(key[5:7]))
            subscription_spend = sum(
                safe_float(value)
                for name, value, _colour in self.repo.top_categories_payment_period(
                    start, end
                )
                if "subscr" in str(name or "").casefold()
            )
            subscriptions = subscription_spend
        subscription_ratio = (
            subscriptions / average_income * 100.0 if average_income else 0.0
        )
        if not subscriptions_available and subscriptions <= 0:
            fixed_raw = 50.0
        elif subscriptions <= 0:
            fixed_raw = 100.0
        elif average_income <= 0:
            sub_exp_ratio = (
                subscriptions / average_expenses * 100.0
                if average_expenses > 0
                else 0.0
            )
            if coverage_years >= 5.0:
                fixed_raw = _clamp(100.0 - max(0.0, sub_exp_ratio - 5.0) * 5.0)
            else:
                fixed_raw = 0.0
        else:
            fixed_raw = _clamp(100.0 - max(0.0, subscription_ratio - 5.0) * 5.0)
        subscription_ratio_text = f"{subscription_ratio:.1f}".replace(".", ",")
        fixed_reading = (
            (
                f"Subscrições mensais: {eur(subscriptions)} "
                f"({subscription_ratio_text}% do rendimento médio)."
            )
            if subscriptions_available
            else (
                "Estimativa por movimentos da categoria Subscrições: "
                f"{eur(subscriptions)}."
                if subscriptions > 0
                else "Sem snapshot histórico de subscrições; dimensão mantida neutra."
            )
        )

        holding_rows = list(net_worth.get("holding_rows") or [])
        classes: dict[str, float] = {}
        for holding in holding_rows:
            value = max(0.0, safe_float(row_get(holding, "value")))
            name = str(row_get(holding, "asset_class") or "Outros")
            classes[name] = classes.get(name, 0.0) + value
        investment_total = sum(classes.values())
        holdings_detail_available = bool(net_worth.get("holdings_detail_available"))
        if not holdings_detail_available:
            diversification_raw = 45.0
            diversification_reading = (
                "Sem detalhe histórico por classe; dimensão mantida neutra."
            )
            concentration_pct = 0.0
        elif investment_total <= 0:
            diversification_raw = 45.0
            diversification_reading = (
                "Sem carteira registada; dimensão mantida numa zona neutra de partida."
            )
            concentration_pct = 0.0
        else:
            shares = [
                value / investment_total for value in classes.values() if value > 0
            ]
            if not shares:
                diversification_raw = 45.0
                diversification_reading = (
                    "Sem posições ativas nas classes; dimensão mantida neutra."
                )
                concentration_pct = 0.0
            else:
                hhi = sum(share * share for share in shares)
                effective_classes = 1.0 / hhi if hhi > 1e-9 else 1.0
                max_share = max(shares)
                concentration_pct = max_share * 100.0
                class_score = _clamp(effective_classes / 4.0 * 100.0)
                concentration_score = _clamp((1.0 - max_share) / 0.75 * 100.0)
                diversification_raw = class_score * 0.55 + concentration_score * 0.45
                active_class_count = len([v for v in classes.values() if v > 0])
                diversification_reading = (
                    f"{active_class_count} classe(s); maior concentração: {concentration_pct:.1f}%."
                ).replace(".", ",")


        quality_raw, quality_reading, quality_metrics = self._quality_metrics(key)
        if not baseline_rows:
            liquidity_raw = savings_raw = fixed_raw = 45.0
            missing_history = "Sem meses completos observados; histórico ainda indisponível."
            liquidity_reading = savings_reading = fixed_reading = missing_history
            if not liabilities_available or total_liabilities > 0:
                debt_raw = 45.0
                debt_reading = missing_history
        raw_values = {
            "liquidity": liquidity_raw,
            "savings": savings_raw,
            "budget": budget_raw,
            "debt": debt_raw,
            "fixed_costs": fixed_raw,
            "diversification": diversification_raw,
            "data_quality": quality_raw,
        }
        readings = {
            "liquidity": liquidity_reading,
            "savings": savings_reading,
            "budget": budget_reading,
            "debt": debt_reading,
            "fixed_costs": fixed_reading,
            "diversification": diversification_reading,
            "data_quality": quality_reading,
        }
        dimensions: list[dict[str, Any]] = []
        for dimension_key, weight in self.WEIGHTS.items():
            raw = _clamp(raw_values[dimension_key])
            points = raw / 100.0 * weight
            tone, status, marker = _tone(raw)
            meta = self.DIMENSION_META[dimension_key]
            dimensions.append(
                {
                    "key": dimension_key,
                    "name": meta["name"],
                    "icon": meta["icon"],
                    "score": raw,
                    "points": points,
                    "max_points": weight,
                    "reading": readings[dimension_key],
                    "tone": tone,
                    "status": status,
                    "marker": marker,
                    "action": meta["action"],
                    "action_page": meta["page"],
                }
            )
        score = round(sum(safe_float(row["points"]) for row in dimensions))
        overall_tone, overall_status, marker = _tone(score)
        active_months = len(active_rows)
        transaction_count = safe_float(quality_metrics["transaction_count"])
        confidence = _clamp(
            active_months / 6.0 * 55.0
            + min(1.0, transaction_count / 60.0) * 25.0
            + safe_float(quality_metrics["categorized_pct"]) / 100.0 * 20.0
        )
        state_confidence = safe_float(net_worth.get("state_confidence"))
        if key < date.today().strftime("%Y-%m"):
            confidence = min(confidence, max(35.0, state_confidence))
        confidence_label = (
            "Elevada" if confidence >= 80 else "Média" if confidence >= 55 else "Baixa"
        )
        res = {
            "month": key,
            "score": score,
            "grade": _grade(score),
            "status": overall_status,
            "tone": overall_tone,
            "marker": marker,
            "confidence": round(confidence),
            "confidence_label": confidence_label,
            "data_as_of": data_as_of,
            "dimensions": dimensions,
            "criteria": [
                (
                    row["name"],
                    row["points"],
                    row["reading"],
                    row["max_points"],
                )
                for row in dimensions
            ],
            "baseline_available": bool(baseline_rows),
            "baseline_months": len(baseline_rows),
            "baseline_month_keys": [str(row["month"]) for row in baseline_rows],
            "baseline_method": "completed_observed_months",
            "savings_rate": average_rate if baseline_rows else None,
            "emergency_months": emergency_months if baseline_rows else None,
            "average_income": average_income if baseline_rows else None,
            "average_expenses": average_expenses if baseline_rows else None,
            "subscription_ratio": subscription_ratio if baseline_rows else None,
            "debt_service_available": bool(baseline_rows) and liabilities_available,
            "debt_service_ratio": debt_service_ratio if baseline_rows and liabilities_available else None,
            "concentration_pct": concentration_pct,
            "state_as_of": str(net_worth.get("state_as_of") or ""),
            "state_source": str(net_worth.get("state_source") or "unavailable"),
            "historical_state_confidence": state_confidence,
            "metrics": {**budget_metrics, **quality_metrics},
        }
        self._snapshot_cache[key] = res
        return res

    def financial_score(
        self, month_key: str, history_months: int = 12
    ) -> dict[str, Any]:
        """Return Score 2.0, its trend, causes and prioritized actions."""
        self.clear_cache()
        key = _month_key(month_key)

        current = self._score_snapshot(key)
        previous_key = add_months(date(int(key[:4]), int(key[5:7]), 1), -1).strftime(
            "%Y-%m"
        )
        previous = self._score_snapshot(previous_key)
        previous_by_key = {row["key"]: row for row in previous["dimensions"]}
        for row in current["dimensions"]:
            row["delta"] = safe_float(row["score"]) - safe_float(
                previous_by_key.get(row["key"], {}).get("score")
            )

        current["delta_1m"] = current["score"] - previous["score"]
        current["previous_score"] = previous["score"]
        current["trend"] = []
        for trend_key in self._month_keys(key, max(3, min(24, history_months))):
            snapshot = self._score_snapshot(trend_key)
            current["trend"].append(
                {
                    "month": trend_key,
                    "label": trend_key,
                    "score": snapshot["score"],
                    "grade": snapshot["grade"],
                    "status": snapshot["status"],
                    "tone": snapshot["tone"],
                }
            )

        changes = sorted(
            current["dimensions"],
            key=lambda row: abs(safe_float(row["delta"])),
            reverse=True,
        )
        current["causes"] = [
            {
                "key": row["key"],
                "title": row["name"],
                "delta": row["delta"],
                "message": (
                    f"{row['marker']} {row['name']} "
                    f"{'melhorou' if row['delta'] > 0 else 'recuou' if row['delta'] < 0 else 'manteve-se'} "
                    f"{abs(safe_float(row['delta'])):.1f} pontos na dimensão."
                ).replace(".", ","),
            }
            for row in changes[:3]
        ]
        weakest = sorted(
            current["dimensions"], key=lambda row: safe_float(row["score"])
        )
        actions: list[dict[str, Any]] = []
        running_score = safe_float(current["score"])
        for priority, row in enumerate(weakest[:3], start=1):
            target_raw = max(70.0, safe_float(row["score"]))
            possible = max(
                0.0,
                (target_raw - safe_float(row["score"]))
                / 100.0
                * safe_float(row["max_points"]),
            )
            running_score = min(100.0, running_score + possible)
            actions.append(
                {
                    "priority": priority,
                    "key": row["key"],
                    "icon": row["icon"],
                    "title": row["name"],
                    "message": row["action"],
                    "page": row["action_page"],
                    "impact_points": possible,
                    "projected_score": round(running_score),
                }
            )
        current["actions"] = actions
        current["method_label"] = (
            "Score explicável de 0–100: liquidez 20, poupança 20, orçamento 15, "
            "endividamento 15, custos fixos 10, diversificação 10 e qualidade dos dados 10."
        )
        return current

    def risk_resilience(
        self,
        *,
        as_of: Optional[date] = None,
        horizon_months: int = 12,
        income_reduction_pct: float = 30.0,
        income_reduction_months: int = 6,
        emergency_expense: float = 2_000.0,
        market_drawdown_pct: float = 20.0,
        simulations: int = 600,
    ) -> dict[str, Any]:
        """Stress household liquidity and net worth under an adjustable scenario."""
        self.clear_cache()
        reference = as_of or date.today()

        for value, name in (
            (horizon_months, "horizonte"), (income_reduction_pct, "redução de rendimento"),
            (income_reduction_months, "duração do choque"), (emergency_expense, "despesa extraordinária"),
            (market_drawdown_pct, "queda de mercado"), (simulations, "simulações"),
        ):
            strict_float(value, field_name=name)

        horizon = max(3, min(36, int(horizon_months or 12)))
        # BUG-RISK-01: a total income loss (100%) is a valid stress
        # scenario; the clamp only rejects out-of-range values.
        reduction = _clamp(income_reduction_pct, 0.0, 100.0) / 100.0
        reduction_months = max(1, min(horizon, int(income_reduction_months or 1)))
        emergency = max(0.0, safe_float(emergency_expense))
        drawdown = _clamp(market_drawdown_pct, 0.0, 90.0) / 100.0

        # Historical behaviour uses completed months, independently of the day.
        baseline_month = min(
            add_months(reference.replace(day=1), -1),
            add_months(date.today().replace(day=1), -1),
        )
        history = self._monthly_rows(baseline_month.strftime("%Y-%m"), 12)
        active = [
            row
            for row in history
            if safe_float(row["income"]) > 0 or safe_float(row["expenses"]) > 0
        ]
        recent = active[-12:] if active else []
        incomes = [safe_float(row["income"]) for row in recent]
        expenses = [safe_float(row["expenses"]) for row in recent]
        average_income = _weighted_mean(incomes)
        average_expenses = _weighted_mean(expenses)
        complete_months = sum(
            1
            for income, expense in zip(incomes, expenses)
            if income > 0 and expense > 0
        )
        data_months = len(recent)
        completeness_pct = complete_months / data_months * 100.0 if data_months else 0.0
        confidence_score = round(
            min(100.0, data_months / 12.0 * 100.0) * 0.70 + completeness_pct * 0.30
        )
        score_available = (
            data_months >= 3
            and complete_months >= 3
            and average_income > 0
            and average_expenses > 0
        )
        if not score_available:
            confidence = "insufficient"
            confidence_label = "Dados insuficientes"
        elif confidence_score >= 85:
            confidence = "high"
            confidence_label = "Confiança alta"
        elif confidence_score >= 60:
            confidence = "medium"
            confidence_label = "Confiança média"
        else:
            confidence = "low"
            confidence_label = "Confiança baixa"
        income_sigma = (
            statistics.pstdev(incomes) if len(incomes) > 1 else average_income * 0.08
        )
        expense_sigma = (
            statistics.pstdev(expenses)
            if len(expenses) > 1
            else average_expenses * 0.10
        )
        income_expense_correlation = _correlation(incomes, expenses)
        net_worth = self._net_worth(reference.strftime("%Y-%m"))
        starting_cash = max(0.0, safe_float(net_worth.get("liquid_assets")))
        investments = max(0.0, safe_float(net_worth.get("investment_assets")))
        starting_net_worth = safe_float(net_worth.get("net_worth"))
        debt_payment = max(0.0, safe_float(net_worth.get("monthly_debt_payment")))
        debt_service_available = bool(net_worth.get("liabilities_available", debt_payment > 0))

        base_surplus = average_income - average_expenses
        optimistic_surplus = average_income * 1.05 - average_expenses * 0.95
        stressed_income = average_income * (1.0 - reduction)
        stressed_expenses = average_expenses * 1.05
        stressed_surplus = stressed_income - stressed_expenses

        rows: list[dict[str, Any]] = [
            {
                "month": 0,
                "label": "Hoje",
                "base": starting_cash,
                "optimistic": starting_cash,
                "stressed": starting_cash,
                "p10": starting_cash,
                "p50": starting_cash,
                "p90": starting_cash,
            }
        ]
        base_cash = optimistic_cash = stressed_cash = starting_cash
        for month in range(1, horizon + 1):
            base_cash += base_surplus
            optimistic_cash += optimistic_surplus
            scenario_income = (
                stressed_income if month <= reduction_months else average_income
            )
            stressed_cash += scenario_income - stressed_expenses
            if month == 1:
                stressed_cash -= emergency
            rows.append(
                {
                    "month": month,
                    "label": f"M{month}",
                    "base": base_cash,
                    "optimistic": optimistic_cash,
                    "stressed": stressed_cash,
                }
            )

        run_count = max(200, min(2_000, int(simulations or 600)))
        seed = (
            7_031
            + int(reduction * 1_000)
            + int(emergency)
            + horizon * 17
            + reduction_months * 31
        )
        import numpy as np

        np_rng = np.random.default_rng(seed)
        income_shocks = np_rng.normal(0.0, 1.0, size=(run_count, horizon))
        independent_shocks = np_rng.normal(0.0, 1.0, size=(run_count, horizon))
        corr_factor = math.sqrt(max(0.0, 1.0 - income_expense_correlation**2))
        expense_shocks = (
            income_expense_correlation * income_shocks + corr_factor * independent_shocks
        )

        sampled_incomes = np.maximum(
            0.0,
            average_income + income_sigma * income_shocks,
        )
        sampled_expenses = np.maximum(
            0.0,
            average_expenses + expense_sigma * expense_shocks,
        )

        if reduction_months > 0:
            sampled_incomes[:, : min(horizon, reduction_months)] *= 1.0 - reduction
        sampled_expenses *= 1.05

        monthly_flows = sampled_incomes - sampled_expenses
        monthly_flows[:, 0] -= emergency

        cash_trajectories = starting_cash + np.cumsum(monthly_flows, axis=1)

        crossed_zero_mask = np.any(cash_trajectories < 0, axis=1)
        negative_runs = int(np.sum(crossed_zero_mask))

        p10_vals = np.percentile(cash_trajectories, 10, axis=0)
        p50_vals = np.percentile(cash_trajectories, 50, axis=0)
        p90_vals = np.percentile(cash_trajectories, 90, axis=0)

        for month in range(1, horizon + 1):
            rows[month]["p10"] = float(p10_vals[month - 1])
            rows[month]["p50"] = float(p50_vals[month - 1])
            rows[month]["p90"] = float(p90_vals[month - 1])

        post_emergency_cash = starting_cash - emergency
        # BUG-CASH-04: a temporary income shock must not be treated as a
        # perpetual deficit. During the shock window the burn rate is the
        # stressed deficit; once the shock ends, normal income resumes, so a
        # non-negative base surplus stops the depletion. Only a structural
        # deficit (base_surplus < 0) keeps burning cash after the shock.
        # BUG-CASH-04: a temporary income shock must not be treated as a
        # perpetual deficit. During the shock window the burn rate is the
        # stressed deficit; once the shock ends, normal income resumes, so a
        # non-negative base surplus stops the depletion. Only a structural
        # deficit (base_surplus < 0) keeps burning cash after the shock.
        # BUG-RISK-02: a negative cash position right after the emergency
        # expense is not an immediate depletion when the monthly flow is
        # positive — report the deficit absorption time instead of 0 months.
        shock_burn = max(0.0, -stressed_surplus)
        shock_months = min(reduction_months, horizon)
        if post_emergency_cash < 0:
            if stressed_surplus < 0 and base_surplus < 0:
                # Cash starts negative and every phase keeps burning it.
                runway = 0.0
            elif stressed_surplus < 0:
                # Burn through the shock window, then recover at base rate.
                hole = -post_emergency_cash + shock_burn * shock_months
                runway = shock_months + hole / base_surplus
            else:
                # Positive flow in every phase: absorb the hole at the
                # slowest positive monthly surplus (conservative estimate).
                rates = [s for s in (stressed_surplus, base_surplus) if s > 0]
                runway = -post_emergency_cash / min(rates) if rates else 0.0
        elif shock_burn > 0:
            covered = post_emergency_cash / shock_burn
            if covered >= shock_months and base_surplus >= 0:
                # Reserves outlast the temporary shock; income resumes.
                runway = 24.0
            elif base_surplus < 0:
                # Structural deficit continues after the shock at base rate.
                remaining = post_emergency_cash - shock_burn * shock_months
                runway = shock_months + max(0.0, remaining) / abs(base_surplus)
            else:
                runway = covered
        else:
            runway = 24.0
        negative_month = next(
            (int(row["month"]) for row in rows[1:] if safe_float(row["stressed"]) < 0),
            None,
        )
        recovery_month = next(
            (
                int(row["month"])
                for row in rows[1:]
                if int(row["month"]) > reduction_months
                and safe_float(row["stressed"]) >= starting_cash
            ),
            None,
        )
        minimum_stressed = min(safe_float(row["stressed"]) for row in rows)
        stressed_final_net_worth = (
            starting_net_worth
            + safe_float(rows[-1]["stressed"])
            - starting_cash
            - investments * drawdown
        )

        emergency_months = starting_cash / average_expenses if average_expenses else 0.0
        savings_rate = base_surplus / average_income * 100.0 if average_income else 0.0
        income_cv = income_sigma / average_income if average_income else 1.0
        expense_cv = expense_sigma / average_expenses if average_expenses else 1.0
        # BUG-RISK-03: with zero income the ratio must never evaluate to a
        # perfect score — outstanding debt payments with no income to cover
        # them are the critical case (ratio reported at 100%).
        if average_income > 0:
            debt_service_ratio = debt_payment / average_income * 100.0
        else:
            debt_service_ratio = 100.0 if debt_payment > 0 else 0.0
        liquidity_score = _clamp(emergency_months / 6.0 * 100.0)
        margin_score = _clamp(savings_rate / 20.0 * 100.0)
        stability_score = _clamp(100.0 - income_cv * 150.0 - expense_cv * 45.0)
        debt_score = _clamp(100.0 - max(0.0, debt_service_ratio - 8.0) * 3.2)
        if not debt_service_available:
            debt_score = 45.0
        scenario_score = _clamp(
            100.0
            - negative_runs / run_count * 80.0
            - (30.0 if minimum_stressed < 0 else 0.0)
        )
        component_specs = [
            ("reserve", "Reserva imediata", "🛟", liquidity_score, 30.0),
            ("margin", "Margem mensal", "💶", margin_score, 25.0),
            ("stability", "Estabilidade", "🧱", stability_score, 15.0),
            ("debt", "Capacidade de dívida", "⚖️", debt_score, 15.0),
            ("scenario", "Resistência ao choque", "🌦️", scenario_score, 15.0),
        ]
        components = []
        for key, name, icon, raw, weight in component_specs:
            tone, status, marker = _tone(raw)
            components.append(
                {
                    "key": key,
                    "name": name,
                    "icon": icon,
                    "score": raw,
                    "points": raw / 100.0 * weight,
                    "max_points": weight,
                    "tone": tone,
                    "status": status,
                    "marker": marker,
                    "available": debt_service_available if key == "debt" else score_available,
                }
            )
        calculated_score = round(sum(safe_float(row["points"]) for row in components))
        resilience_score: Optional[int] = calculated_score if score_available else None
        if score_available:
            overall_tone, overall_status, marker = _tone(calculated_score)
        else:
            overall_tone, overall_status, marker = (
                "neutral",
                "Dados insuficientes",
                "⚪",
            )
        actions: list[dict[str, str]] = []
        if score_available and emergency_months < 3:
            actions.append(
                {
                    "icon": "🛟",
                    "title": "Aumentar reserva",
                    "message": "Prioridade alta: aproximar a liquidez de três meses de despesas.",
                    "page": "centro",
                }
            )
        if score_available and debt_service_ratio > 20:
            actions.append(
                {
                    "icon": "⚖️",
                    "title": "Aliviar prestações",
                    "message": "A prestação mensal está a limitar a capacidade de absorver choques.",
                    "page": "carteira",
                }
            )
        if score_available and savings_rate < 15:
            actions.append(
                {
                    "icon": "🌱",
                    "title": "Criar margem",
                    "message": "Reforçar a poupança recorrente melhora todos os cenários de liquidez.",
                    "page": "orcamentos",
                }
            )
        if score_available and negative_runs / run_count >= 0.25:
            actions.append(
                {
                    "icon": "🚦",
                    "title": "Plano de contingência",
                    "message": "Reduzir custos flexíveis antes do choque baixa o risco de saldo negativo.",
                    "page": "planeador",
                }
            )
        if not actions:
            actions.append(
                {
                    "icon": "🧪" if not score_available else "✅",
                    "title": (
                        "Completar o histórico"
                        if not score_available
                        else "Estrutura resistente"
                    ),
                    "message": (
                        "São necessários pelo menos três meses completos de "
                        "rendimentos e despesas para avaliar a resiliência."
                        if not score_available
                        else "Mantém a reserva e testa novamente quando "
                        "rendimentos ou encargos mudarem."
                    ),
                    "page": "movimentos" if not score_available else "dashboard",
                }
            )

        def sensitivity_result(
            label: str,
            income_cut: float,
            emergency_multiplier: float,
        ) -> dict[str, Any]:
            cash = starting_cash
            minimum_cash = cash
            breach_month: Optional[int] = None
            for month in range(1, horizon + 1):
                month_income = (
                    average_income * (1.0 - income_cut)
                    if month <= reduction_months
                    else average_income
                )
                cash += month_income - average_expenses * 1.05
                if month == 1:
                    cash -= emergency * emergency_multiplier
                minimum_cash = min(minimum_cash, cash)
                if breach_month is None and cash < 0:
                    breach_month = month
            return {
                "label": label,
                "income_reduction_pct": income_cut * 100.0,
                "emergency_expense": emergency * emergency_multiplier,
                "minimum_cash": minimum_cash,
                "breach_month": breach_month,
            }

        sensitivity = (
            [
                sensitivity_result(
                    "Mais favorável",
                    max(0.0, reduction - 0.10),
                    0.5,
                ),
                sensitivity_result("Cenário atual", reduction, 1.0),
                sensitivity_result(
                    "Mais severo",
                    min(1.0, reduction + 0.15),
                    1.5,
                ),
            ]
            if score_available
            else []
        )
        availability_note = (
            ""
            if score_available
            else " O score e os indicadores simulados ficam ocultos até existirem pelo menos três meses completos."
        )
        return {
            "as_of": reference.isoformat(),
            "horizon_months": horizon,
            "score": resilience_score,
            "calculated_score": calculated_score,
            "score_available": score_available,
            "grade": _grade(calculated_score) if score_available else "—",
            "status": overall_status,
            "tone": overall_tone,
            "marker": marker,
            "rows": rows,
            "components": components,
            "actions": actions[:4],
            "average_income": average_income,
            "average_expenses": average_expenses,
            "monthly_margin": base_surplus,
            "starting_cash": starting_cash,
            "emergency_months": emergency_months,
            "runway_months": min(24.0, runway),
            "probability_negative": negative_runs / run_count * 100.0,
            "negative_month": negative_month,
            "recovery_month": recovery_month,
            "minimum_stressed": minimum_stressed,
            "starting_net_worth": starting_net_worth,
            "stressed_final_net_worth": stressed_final_net_worth,
            "market_loss": investments * drawdown,
            "debt_service_ratio": debt_service_ratio if debt_service_available else None,
            "debt_service_available": debt_service_available,
            "income_reduction_pct": reduction * 100.0,
            "income_reduction_months": reduction_months,
            "emergency_expense": emergency,
            "market_drawdown_pct": drawdown * 100.0,
            "simulations": run_count,
            "confidence_score": confidence_score,
            "confidence": confidence,
            "confidence_label": confidence_label,
            "data_months": data_months,
            "baseline_method": "completed_observed_months",
            "data_period_start": str(recent[0]["month"]) if recent else "",
            "data_period_end": str(recent[-1]["month"]) if recent else "",
            "data_completeness_pct": completeness_pct,
            "income_expense_correlation": income_expense_correlation,
            "sensitivity": sensitivity,
            "method_label": (
                f"{confidence_label}: {data_months}/12 meses com maior peso nos dados "
                f"recentes e {completeness_pct:.0f}% de meses completos. Cenário "
                f"determinístico e {run_count} trajetórias locais usam a volatilidade "
                f"observada e correlação rendimento/despesa de "
                f"{income_expense_correlation * 100:.0f}%. Inclui despesas anuais "
                "registadas, mas é um teste de esforço e não uma garantia."
                f"{availability_note}"
            ),
        }
