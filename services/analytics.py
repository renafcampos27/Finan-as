"""Financial analytics and presentation-ready metric orchestration."""

from __future__ import annotations

import math
import statistics
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any, Optional

from database.validation import (
    sql_external_financial_flow,
    sql_finite_number,
    sql_iso_date,
)
from services.cashflow_forecast import CashflowForecastService
from services.financial_intelligence import (
    FinancialIntelligenceService,
    monte_carlo_projection as build_monte_carlo_projection,
)
from services.investment_projection import build_investment_projection
from services.spending_diagnostics import SpendingDiagnosticsService
from services.habit_personalization import (
    build_habit_audit_rows,
    build_habit_calibration,
)
from services.habit_simulations import HABIT_PRESETS_BY_KEY
from utils.app_logging import log_exception
from utils.config import parse_date
from utils.helpers import (
    BLUE,
    GREEN,
    ORANGE,
    PINK,
    PT_MONTHS,
    PURPLE,
    TEAL,
    YELLOW,
    add_months,
    eur,
    iso,
    rolling_month_window,
    row_get,
    safe_float,
)

if TYPE_CHECKING:
    from database.repo import Repo
    from services.payments import PaymentsService


_FINANCIAL_DATE_SQL = "COALESCE(NULLIF(m.payment_date,''),t.date)"
_VALID_TRANSACTION_AMOUNT_SQL = sql_finite_number("t.amount", non_zero=True)
_VALID_FINANCIAL_DATE_SQL = sql_iso_date(_FINANCIAL_DATE_SQL)
_EXTERNAL_FLOW_SQL = sql_external_financial_flow("t.category")


class AnalyticsService:
    """Build financial metrics from raw repository queries."""

    def __init__(
        self,
        repo: "Repo",
        payments_service: Optional["PaymentsService"] = None,
    ) -> None:
        self.repo = repo
        if payments_service is None:
            from services.payments import PaymentsService

            payments_service = PaymentsService(repo)
        self.payments_service = payments_service
        self.spending_diagnostics = SpendingDiagnosticsService(repo)
        self._health_service: Optional[Any] = None
        self._intel_service: Optional[FinancialIntelligenceService] = None
        self._coverage_cache: dict[tuple[str, str], dict[str, Any]] = {}
        self._monthly_summary_cache: dict[tuple[str, str], list[dict[str, Any]]] = {}

    @property
    def health_service(self) -> Any:
        if self._health_service is None:
            from services.financial_health import FinancialHealthService

            self._health_service = FinancialHealthService(
                self.repo, self.payments_service
            )
        return self._health_service

    @property
    def intel_service(self) -> FinancialIntelligenceService:
        if self._intel_service is None:
            self._intel_service = FinancialIntelligenceService(self.repo)
        return self._intel_service

    def invalidate_caches(self) -> None:
        """Clear all in-memory metrics caches across analytics subsystems."""
        self._coverage_cache.clear()
        self._monthly_summary_cache.clear()
        if hasattr(self.spending_diagnostics, "invalidate_caches"):
            self.spending_diagnostics.invalidate_caches()
        if self._health_service is not None:
            if hasattr(self._health_service, "clear_cache"):
                self._health_service.clear_cache()
            if hasattr(self._health_service, "invalidate_caches"):
                self._health_service.invalidate_caches()
        if self._intel_service is not None and hasattr(
            self._intel_service, "invalidate_caches"
        ):
            self._intel_service.invalidate_caches()

    def forecast_accuracy(
        self,
        *,
        reference_date: Optional[date] = None,
        visible_months: int = 12,
    ) -> dict[str, Any]:
        result = self.spending_diagnostics.forecast_accuracy(
            reference_date=reference_date,
            visible_months=visible_months,
        )

        reference = reference_date or date.today()
        start = add_months(reference.replace(day=1), -max(1, visible_months), 1)
        result["data_coverage"] = self.data_coverage_snapshot(
            iso(start),
            iso(reference),
        )
        return result

    def category_forecast_accuracy(
        self,
        *,
        reference_date: Optional[date] = None,
        visible_months: int = 12,
    ) -> dict[str, Any]:
        result = self.spending_diagnostics.category_forecast_accuracy(
            reference_date=reference_date,
            visible_months=visible_months,
        )
        reference = reference_date or date.today()
        start = add_months(reference.replace(day=1), -max(1, visible_months), 1)
        result["data_coverage"] = self.data_coverage_snapshot(
            iso(start),
            iso(reference),
        )
        return result

    def spending_seasonality(
        self,
        start: str,
        end: str,
        *,
        category: str = "",
    ) -> dict[str, Any]:
        return self.spending_diagnostics.spending_seasonality(
            start,
            end,
            category=category,
        )

    def merchant_pareto(
        self,
        start: str,
        end: str,
        *,
        limit: int = 12,
    ) -> dict[str, Any]:
        return self.spending_diagnostics.merchant_pareto(
            start,
            end,
            limit=limit,
        )

    def personal_income_context(self, months: int = 6) -> dict[str, Any]:
        """Return a stable salary/income reference for personal simulations."""
        rows = list(self.monthly_summary(max(1, int(months or 1))) or [])
        complete = [
            row
            for row in rows
            if not bool(row.get("partial")) and safe_float(row.get("income")) > 0
        ]
        usable = complete[-3:]
        if not usable:
            usable = [row for row in rows if safe_float(row.get("income")) > 0][-3:]
        average_savings = (
            sum(safe_float(row.get("savings")) for row in usable) / len(usable)
            if usable
            else 0.0
        )

        salary_months: list[Any] = []
        if rows:
            salary_months = self.repo.q(
                f"""
                SELECT SUBSTR(COALESCE(NULLIF(m.payment_date,''),t.date),1,7) AS month,
                       SUM(t.amount) AS salary
                FROM transactions t
                LEFT JOIN transaction_meta m ON m.transaction_id=t.id
                WHERE COALESCE(NULLIF(m.payment_date,''),t.date)>=?
                  AND COALESCE(NULLIF(m.payment_date,''),t.date)<=?
                  AND t.amount>0
                  AND {_VALID_TRANSACTION_AMOUNT_SQL}
                  AND {_VALID_FINANCIAL_DATE_SQL}
                  AND {_EXTERNAL_FLOW_SQL}
                  AND (
                      LOWER(t.description) LIKE '%salário%'
                      OR LOWER(t.description) LIKE '%salario%'
                      OR LOWER(t.description) LIKE '%vencimento%'
                      OR LOWER(t.description) LIKE '%ordenado%'
                  )
                GROUP BY month
                ORDER BY month
                """,
                (
                    str(rows[0].get("period_start") or ""),
                    str(rows[-1].get("period_end") or ""),
                ),
            )
        salary_values = [
            safe_float(row["salary"])
            for row in salary_months[-3:]
            if safe_float(row["salary"]) > 0
        ]
        if salary_values:
            average_income = sum(salary_values) / len(salary_values)
            reference_name = "salário"
            context_months = len(salary_values)
        elif usable:
            average_income = sum(safe_float(row.get("income")) for row in usable) / len(
                usable
            )
            reference_name = "rendimento"
            context_months = len(usable)
        else:
            average_income = 0.0
            reference_name = "salário"
            context_months = 0
        return {
            "average_income": average_income,
            "average_savings": average_savings,
            "reference_name": reference_name,
            "context_months": context_months,
        }

    def habit_calibration(
        self,
        preset_key: str,
        months: int = 6,
        *,
        reference_date: Optional[date] = None,
        audit_rules: Optional[dict[str, Any]] = None,
        transaction_context: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Calibrate one habit from explainable matches in recent expenses."""
        preset = HABIT_PRESETS_BY_KEY.get(str(preset_key or ""))
        if preset is None:
            return {"available": False, "preset_key": str(preset_key or "")}
        context = transaction_context or self.habit_transaction_context(
            months,
            reference_date=reference_date,
        )
        return build_habit_calibration(
            preset,
            context.get("transactions") or [],
            period_start=str(context.get("period_start") or ""),
            period_end=str(context.get("period_end") or ""),
            coverage_months=int(context.get("coverage_months", 0) or 0),
            audit_rules=audit_rules,
        )

    def habit_transaction_context(
        self,
        months: int = 6,
        *,
        reference_date: Optional[date] = None,
    ) -> dict[str, Any]:
        end = reference_date or date.today()
        start, end, _days = rolling_month_window(end, max(1, int(months or 1)))
        return self.habit_transactions_for_period(start.isoformat(), end.isoformat())

    def habit_transactions_for_period(
        self,
        start: str,
        end: str,
    ) -> dict[str, Any]:
        period_start = str(start or "1900-01-01")[:10]
        period_end = str(end or "2999-12-31")[:10]
        transactions = self.repo.q(
            f"""
            SELECT t.id,
                   COALESCE(NULLIF(m.payment_date,''),t.date) AS financial_date,
                   t.description,
                   ABS(t.amount) AS amount,
                   t.category,
                   IFNULL(m.subcategory,'') AS subcategory,
                   IFNULL(m.merchant,'') AS merchant
            FROM transactions t
            LEFT JOIN transaction_meta m ON m.transaction_id=t.id
            WHERE COALESCE(NULLIF(m.payment_date,''),t.date)>=?
              AND COALESCE(NULLIF(m.payment_date,''),t.date)<=?
              AND t.amount<0
              AND {_VALID_TRANSACTION_AMOUNT_SQL}
              AND {_VALID_FINANCIAL_DATE_SQL}
              AND {_EXTERNAL_FLOW_SQL}
            ORDER BY financial_date
            """,
            (period_start, period_end),
        )
        coverage_months = len(
            {
                str(row["financial_date"] or "")[:7]
                for row in transactions
                if str(row["financial_date"] or "")[:7]
            }
        )
        return {
            "period_start": period_start,
            "period_end": period_end,
            "coverage_months": coverage_months,
            "transactions": [dict(row) for row in transactions],
        }

    def habit_audit_rows(
        self,
        preset_key: str,
        months: int = 6,
        *,
        reference_date: Optional[date] = None,
        audit_rules: Optional[dict[str, Any]] = None,
        transaction_context: Optional[dict[str, Any]] = None,
    ) -> list[dict[str, Any]]:
        preset = HABIT_PRESETS_BY_KEY.get(str(preset_key or ""))
        if preset is None:
            return []
        context = transaction_context or self.habit_transaction_context(
            months,
            reference_date=reference_date,
        )
        return build_habit_audit_rows(
            preset,
            context.get("transactions") or [],
            audit_rules,
        )

    def totals_for_period(self, start: str, end: str) -> dict[str, Any]:
        start = start or "1900-01-01"
        end = end or "2999-12-31"
        row = self.repo.q(
            f"""
            SELECT
                COALESCE(SUM(CASE WHEN t.amount>0 THEN t.amount ELSE 0 END), 0.0) AS income,
                COALESCE(SUM(CASE WHEN t.amount<0 THEN ABS(t.amount) ELSE 0.0 END), 0.0) AS expenses
            FROM transactions t
            LEFT JOIN transaction_meta m ON m.transaction_id=t.id
            WHERE {_FINANCIAL_DATE_SQL}>=?
              AND {_FINANCIAL_DATE_SQL}<=?
              AND {_VALID_TRANSACTION_AMOUNT_SQL}
              AND {_VALID_FINANCIAL_DATE_SQL}
              AND {_EXTERNAL_FLOW_SQL}
            """,
            (start, end),
        )
        income = safe_float(row[0]["income"]) if row else 0.0
        expenses = safe_float(row[0]["expenses"]) if row else 0.0
        investment_state = self.repo.investment_state_as_of(end)
        investments = safe_float(investment_state.get("total"))
        return {
            "income": float(income),
            "expenses": float(expenses),
            "savings": float(income - expenses),
            "investments": float(investments),
            "investments_available": bool(investment_state.get("available")),
            "investment_as_of": str(investment_state.get("as_of") or ""),
            "investment_source": str(investment_state.get("source") or "unavailable"),
            "investment_cost": safe_float(investment_state.get("invested")),
            "investment_gain": safe_float(investment_state.get("gain")),
            "investment_return_pct": safe_float(investment_state.get("return_pct")),
        }

    def data_coverage_snapshot(self, start: str, end: str) -> dict[str, Any]:
        """Explain how much real history supports a score or forecast."""

        cache_key = (str(start or ""), str(end or ""))
        if cache_key in self._coverage_cache:
            return dict(self._coverage_cache[cache_key])

        try:
            start_day = date.fromisoformat(str(start or "")[:10])

            end_day = date.fromisoformat(str(end or "")[:10])
        except (TypeError, ValueError):
            return {
                "data_as_of": "",
                "first_data_date": "",
                "movement_count": 0,
                "active_months": 0,
                "expected_months": 0,
                "coverage_pct": 0.0,
                "confidence": "initial",
                "confidence_label": "Confiança inicial",
                "initial_balances_known": 0,
                "initial_balances_total": 0,
                "investment_cost_known": 0,
                "investment_cost_total": 0,
                "investment_cost_coverage_pct": 0.0,
                "invalid_transactions_excluded": 0,
                "invalid_dates_excluded": 0,
                "invalid_amounts_excluded": 0,
            }
        if start_day > end_day:
            start_day, end_day = end_day, start_day

        rows = self.repo.q(
            f"""
            WITH financial_transactions AS (
                SELECT COALESCE(NULLIF(m.payment_date,''),t.date) AS financial_date
                FROM transactions t
                LEFT JOIN transaction_meta m ON m.transaction_id=t.id
                WHERE COALESCE(NULLIF(m.payment_date,''),t.date)>=?
                  AND COALESCE(NULLIF(m.payment_date,''),t.date)<=?
                  AND {_EXTERNAL_FLOW_SQL}
                  AND {_VALID_TRANSACTION_AMOUNT_SQL}
                  AND {_VALID_FINANCIAL_DATE_SQL}
            )
            SELECT MIN(financial_date) AS first_date,
                   MAX(financial_date) AS latest_date,
                   COUNT(*) AS movement_count,
                   COUNT(DISTINCT SUBSTR(financial_date,1,7)) AS active_months
            FROM financial_transactions
            """,
            (start_day.isoformat(), end_day.isoformat()),
        )
        row = rows[0] if rows else {}
        first_date = str(row["first_date"] or "")[:10] if row else ""
        latest_date = str(row["latest_date"] or "")[:10] if row else ""
        movement_count = max(
            0,
            int(row["movement_count"] or 0) if row else 0,
        )
        active_months = max(
            0,
            int(row["active_months"] or 0) if row else 0,
        )

        effective_start = start_day
        if start_day.year <= 1900 and first_date:
            try:
                effective_start = date.fromisoformat(first_date)
            except ValueError:
                effective_start = start_day
        expected_months = (
            (end_day.year - effective_start.year) * 12
            + end_day.month
            - effective_start.month
            + 1
            if movement_count
            else 0
        )
        expected_months = max(0, expected_months)
        coverage_pct = (
            min(100.0, active_months / expected_months * 100.0)
            if expected_months
            else 0.0
        )
        if movement_count >= 12 and active_months >= 3 and coverage_pct >= 70:
            confidence = "high"
            confidence_label = "Confiança alta"
        elif movement_count >= 4 and active_months >= 2 and coverage_pct >= 40:
            confidence = "medium"
            confidence_label = "Confiança média"
        else:
            confidence = "initial"
            confidence_label = "Confiança inicial"
        account_quality = self.repo.q(
            """
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN balance_known=1
                                  AND COALESCE(balance_source,'')<>'merged_derived'
                            THEN 1 ELSE 0 END) AS known,
                   SUM(CASE WHEN balance_known=1 THEN 1 ELSE 0 END) AS usable
            FROM financial_accounts
            WHERE is_active=1
            """
        )
        account_row = account_quality[0] if account_quality else {}
        initial_balances_total = int(account_row["total"] or 0) if account_row else 0
        initial_balances_known = int(account_row["known"] or 0) if account_row else 0
        initial_balances_usable = int(account_row["usable"] or 0) if account_row else 0

        holding_quality = self.repo.q(
            f"""
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN cost_basis_known=1 AND cost_basis>0
                                  AND {sql_finite_number("cost_basis", non_zero=True)}
                            THEN 1 ELSE 0 END) AS known,
                   COALESCE(SUM(CASE WHEN {sql_finite_number("value", non_negative=True)}
                                     THEN value ELSE 0 END),0) AS total_value,
                   COALESCE(SUM(CASE WHEN cost_basis_known=1 AND cost_basis>0
                                          AND {sql_finite_number("cost_basis", non_zero=True)}
                                          AND {sql_finite_number("value", non_negative=True)}
                                     THEN value ELSE 0 END),0) AS known_value
            FROM holdings
            """
        )
        holding_row = holding_quality[0] if holding_quality else {}
        investment_cost_total = int(holding_row["total"] or 0) if holding_row else 0
        investment_cost_known = int(holding_row["known"] or 0) if holding_row else 0
        investment_total_value = safe_float(
            holding_row["total_value"] if holding_row else 0
        )
        investment_known_value = safe_float(
            holding_row["known_value"] if holding_row else 0
        )
        investment_cost_coverage_pct = (
            investment_known_value / investment_total_value * 100.0
            if investment_total_value > 0
            else (100.0 if investment_cost_total == 0 else 0.0)
        )

        invalid_quality = self.repo.q(
            f"""
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN NOT ({_VALID_FINANCIAL_DATE_SQL})
                            THEN 1 ELSE 0 END) AS invalid_dates,
                   SUM(CASE WHEN NOT ({_VALID_TRANSACTION_AMOUNT_SQL})
                            THEN 1 ELSE 0 END) AS invalid_amounts,
                   SUM(CASE WHEN NOT (
                                    {_VALID_FINANCIAL_DATE_SQL}
                                    AND {_VALID_TRANSACTION_AMOUNT_SQL}
                                ) THEN 1 ELSE 0 END) AS invalid_total
            FROM transactions t
            LEFT JOIN transaction_meta m ON m.transaction_id=t.id
            """
        )
        invalid_row = invalid_quality[0] if invalid_quality else {}
        invalid_dates = int(invalid_row["invalid_dates"] or 0) if invalid_row else 0
        invalid_amounts = int(invalid_row["invalid_amounts"] or 0) if invalid_row else 0
        invalid_total = int(invalid_row["invalid_total"] or 0) if invalid_row else 0
        snapshot = {
            "data_as_of": latest_date,
            "first_data_date": first_date,
            "movement_count": movement_count,
            "active_months": active_months,
            "expected_months": expected_months,
            "coverage_pct": coverage_pct,
            "confidence": confidence,
            "confidence_label": confidence_label,
            "initial_balances_known": initial_balances_known,
            "initial_balances_usable": initial_balances_usable,
            "initial_balances_total": initial_balances_total,
            "investment_cost_known": investment_cost_known,
            "investment_cost_total": investment_cost_total,
            "investment_cost_coverage_pct": investment_cost_coverage_pct,
            "invalid_transactions_excluded": invalid_total,
            "invalid_dates_excluded": invalid_dates,
            "invalid_amounts_excluded": invalid_amounts,
        }
        self._coverage_cache[cache_key] = snapshot
        return snapshot

    @staticmethod
    def data_coverage_label(snapshot: dict[str, Any] | None) -> str:
        """Render one honest, compact data-basis label for predictive cards."""

        data = dict(snapshot or {})
        months = int(data.get("active_months", 0) or 0)
        balances_known = int(data.get("initial_balances_known", 0) or 0)
        balances_total = int(data.get("initial_balances_total", 0) or 0)
        costs_known = int(data.get("investment_cost_known", 0) or 0)
        costs_total = int(data.get("investment_cost_total", 0) or 0)
        invalid = int(data.get("invalid_transactions_excluded", 0) or 0)
        return (
            f"📐 Base real: {months} mês(es) com movimentos · "
            f"saldos iniciais {balances_known}/{balances_total} · "
            f"custos de investimento {costs_known}/{costs_total} · "
            f"{invalid} transação(ões) inválida(s) excluída(s)"
        )

    def category_totals(
        self,
        start: str,
        end: str,
        limit: Optional[int] = None,
    ) -> list[tuple[str, float, str]]:
        return self.repo.top_categories_payment_period(start, end, limit)

    def cashflow_series(
        self,
        start: str,
        end: str,
        resolution: str = "daily",
    ) -> list[dict[str, Any]]:
        """Return real income and expenses grouped for cumulative-flow charts."""
        try:
            start_day = date.fromisoformat(str(start or "")[:10])
            end_day = date.fromisoformat(str(end or "")[:10])
        except (TypeError, ValueError):
            return []
        if start_day > end_day:
            return []

        rows = self.repo.q(
            f"""
            SELECT COALESCE(NULLIF(m.payment_date,''),t.date) AS financial_date,
                   SUM(CASE WHEN t.amount>0 THEN t.amount ELSE 0 END) AS income,
                   SUM(CASE WHEN t.amount<0 THEN ABS(t.amount) ELSE 0 END) AS expenses
            FROM transactions t
            LEFT JOIN transaction_meta m ON m.transaction_id=t.id
            WHERE COALESCE(NULLIF(m.payment_date,''),t.date)>=?
              AND COALESCE(NULLIF(m.payment_date,''),t.date)<=?
              AND {_EXTERNAL_FLOW_SQL}
              AND {_VALID_TRANSACTION_AMOUNT_SQL}
              AND {_VALID_FINANCIAL_DATE_SQL}
            GROUP BY financial_date
            ORDER BY financial_date
            """,
            (start_day.isoformat(), end_day.isoformat()),
        )

        daily: dict[date, dict[str, float]] = {}
        for row in rows:
            try:
                financial_day = date.fromisoformat(
                    str(row["financial_date"] or "")[:10]
                )
            except (TypeError, ValueError):
                continue
            daily[financial_day] = {
                "income": max(0.0, safe_float(row["income"])),
                "expenses": max(0.0, safe_float(row["expenses"])),
            }
        if not daily:
            return []

        selected_resolution = (
            resolution if resolution in {"daily", "weekly", "monthly"} else "daily"
        )
        full_history = start_day.year <= 1900
        if selected_resolution == "daily":
            # Only "Tudo" may skip its artificial 1900 prefix. Every explicit
            # filter keeps empty leading days so the horizontal axis still
            # represents the exact selected window.
            current = min(daily) if full_history else start_day
            result: list[dict[str, Any]] = []
            while current <= end_day:
                values = daily.get(current, {})
                income = safe_float(values.get("income"))
                expenses = safe_float(values.get("expenses"))
                result.append(
                    {
                        "date": current.isoformat(),
                        "resolution": "daily",
                        "income": income,
                        "expenses": expenses,
                        "net": income - expenses,
                    }
                )
                current += timedelta(days=1)
            return result

        grouped: dict[date, dict[str, float]] = {}
        for financial_day, values in daily.items():
            if selected_resolution == "weekly":
                bucket = financial_day - timedelta(days=financial_day.weekday())
            else:
                bucket = financial_day.replace(day=1)
            aggregate = grouped.setdefault(
                bucket,
                {"income": 0.0, "expenses": 0.0},
            )
            aggregate["income"] += safe_float(values.get("income"))
            aggregate["expenses"] += safe_float(values.get("expenses"))

        first_day = min(daily) if full_history else start_day
        if selected_resolution == "weekly":
            current_bucket = first_day - timedelta(days=first_day.weekday())
            final_bucket = end_day - timedelta(days=end_day.weekday())
        else:
            current_bucket = first_day.replace(day=1)
            final_bucket = end_day.replace(day=1)

        complete_grouped: dict[date, dict[str, float]] = {}
        while current_bucket <= final_bucket:
            complete_grouped[current_bucket] = grouped.get(
                current_bucket,
                {"income": 0.0, "expenses": 0.0},
            )
            current_bucket = (
                current_bucket + timedelta(days=7)
                if selected_resolution == "weekly"
                else add_months(current_bucket, 1, 1)
            )

        return [
            {
                "date": bucket.isoformat(),
                "resolution": selected_resolution,
                "income": values["income"],
                "expenses": values["expenses"],
                "net": values["income"] - values["expenses"],
            }
            for bucket, values in sorted(complete_grouped.items())
        ]

    def budget_rule_503020(self, start: str, end: str) -> dict[str, Any]:
        """Compare real split-aware spending with the configurable 50/30/20 rule."""
        totals = self.totals_for_period(start, end)
        income = safe_float(totals.get("income"))
        try:
            end_exclusive = iso(date.fromisoformat(end[:10]) + timedelta(days=1))
        except (TypeError, ValueError):
            end_exclusive = end
        mapping = self.repo.category_budget_buckets()
        actual = {"Necessidades": 0.0, "Desejos": 0.0, "Poupança": 0.0}
        for row in self.repo.category_spend_with_splits(
            start,
            end_exclusive,
            end_exclusive=True,
        ):
            category = str(row["category"] or "")
            bucket = mapping.get(category, "Desejos")
            actual[bucket] = actual.get(bucket, 0.0) + safe_float(row["total"])
        # Money invested through a savings category and unspent cash both count
        # towards the savings pillar, without double counting either amount.
        actual["Poupança"] = income - actual["Necessidades"] - actual["Desejos"]
        targets = {"Necessidades": 0.50, "Desejos": 0.30, "Poupança": 0.20}
        rows = []
        for bucket in ("Necessidades", "Desejos", "Poupança"):
            value = actual[bucket]
            target_value = income * targets[bucket]
            rows.append(
                {
                    "bucket": bucket,
                    "actual": value,
                    "actual_pct": value / income * 100 if income else 0.0,
                    "target": target_value,
                    "target_pct": targets[bucket] * 100,
                    "variance": value - target_value,
                }
            )
        return {"income": income, "rows": rows, "mapping": mapping}

    def balance_forecast_series(
        self,
        historical_days: Optional[int] = 90,
        forecast_days: int = 30,
        resolution: str = "daily",
    ) -> list[dict[str, Any]]:
        # ``None`` deliberately represents the Dashboard's "Tudo" filter.
        # Keep that meaning all the way to ``balance_series`` instead of
        # forcing it through integer-only date arithmetic.
        history_window: Optional[int]
        if historical_days is None:
            history_window = None
        else:
            try:
                history_window = max(1, int(historical_days))
            except (TypeError, ValueError):
                history_window = 90

        # Get historical balance points up to today
        historical = self.balance_series(days=history_window)
        if not historical:
            return []

        today = date.today()
        first_history_date = str(historical[0].get("date") or "")[:10]
        try:
            history_start = date.fromisoformat(first_history_date).isoformat()
        except (TypeError, ValueError):
            fallback_days = history_window or 365
            history_start = iso(today - timedelta(days=max(1, fallback_days) - 1))
        forecast_coverage = self.data_coverage_snapshot(
            history_start,
            iso(today),
        )
        current_balance = historical[-1]["balance"]

        # Calculate daily burn rate (average expense over last 30 days)
        recent_window = historical[-30:]
        # Average over calendar days, including no-spend days. Dividing only by
        # days that contain a transaction substantially overstates the forecast.
        recent_expenses = sum(safe_float(p.get("expense")) for p in recent_window)
        linked_subscription_expenses = 0.0
        quality_warnings: list[str] = []
        if recent_window:
            try:
                linked_rows = self.repo.q(
                    f"""
                    SELECT COALESCE(SUM(ABS(t.amount)), 0) AS total
                    FROM subscription_occurrences o
                    JOIN transactions t ON t.id=o.transaction_id
                    WHERE o.due_date>=? AND o.due_date<=?
                      AND {_VALID_TRANSACTION_AMOUNT_SQL}
                    """,
                    (
                        str(recent_window[0].get("date") or "")[:10],
                        str(recent_window[-1].get("date") or "")[:10],
                    ),
                )
                if linked_rows:
                    linked_subscription_expenses = safe_float(linked_rows[0]["total"])
            except Exception as exc:
                log_exception(
                    "Não foi possível excluir subscrições já agendadas da previsão",
                    exc,
                )
                quality_warnings.append(
                    "As subscrições associadas não puderam ser separadas do ritmo diário."
                )
                linked_subscription_expenses = 0.0
        # Linked subscription charges are scheduled explicitly below. Remove
        # them from the generic burn rate so they are not deducted twice.
        daily_burn = (
            max(0.0, recent_expenses - linked_subscription_expenses)
            / len(recent_window)
            if recent_window
            else 0.0
        )

        # We will create fake points for the future
        forecast: list[dict[str, Any]] = []
        current = today + timedelta(days=1)
        simulated_balance = current_balance
        optimistic_balance = current_balance
        pessimistic_balance = current_balance

        # Build actual recurrence dates (monthly, quarterly, semiannual or annual).
        subscriptions_by_day: dict[str, list[Any]] = {}
        forecast_end = today + timedelta(days=max(0, forecast_days))
        subscription_loader = getattr(self.repo, "subscriptions", None)
        subscriptions = (
            subscription_loader(active_only=True)
            if callable(subscription_loader)
            else []
        )
        for sub in subscriptions:
            candidate = self.payments_service.next_subscription_due_date(
                sub,
                today + timedelta(days=1),
            )
            while candidate <= forecast_end:
                subscriptions_by_day.setdefault(iso(candidate), []).append(sub)
                candidate = self.payments_service.advance_subscription_due(
                    sub,
                    candidate,
                )

        for _ in range(forecast_days):
            key = current.strftime("%Y-%m-%d")
            # Apply daily burn
            simulated_balance -= daily_burn
            optimistic_balance -= daily_burn * 0.8
            pessimistic_balance -= daily_burn * 1.2

            # Apply subscriptions
            scheduled_expense = 0.0
            for sub in subscriptions_by_day.get(key, []):
                subscription_amount = abs(float(sub["amount"] or 0))
                scheduled_expense += subscription_amount
                simulated_balance -= subscription_amount
                optimistic_balance -= subscription_amount
                pessimistic_balance -= subscription_amount

            forecast.append(
                {
                    "date": key,
                    "balance": simulated_balance,
                    "balance_base": simulated_balance,
                    "balance_optimistic": optimistic_balance,
                    "balance_pessimistic": pessimistic_balance,
                    "delta": -(daily_burn + scheduled_expense),
                    "income": 0.0,
                    "expense": daily_burn + scheduled_expense,
                    "daily_baseline": daily_burn,
                    "scheduled_expense": scheduled_expense,
                    "is_forecast": True,
                    "quality_warnings": tuple(quality_warnings),
                    "data_coverage": forecast_coverage,
                }
            )
            current += timedelta(days=1)

        points = historical + forecast
        if resolution in ("monthly", "weekly"):
            grouped: dict[str, dict[str, Any]] = {}
            for p in points:
                dt = parse_date(p["date"])
                if resolution == "monthly":
                    key = dt.strftime("%Y-%m")
                    display_date = f"{key}-01"
                else:
                    iso_year, iso_week, _ = dt.isocalendar()
                    key = f"{iso_year}-W{iso_week:02d}"
                    monday = dt - timedelta(days=dt.weekday())
                    display_date = iso(monday)

                if key not in grouped:
                    grouped[key] = {
                        "date": display_date,
                        "balance": p["balance"],
                        "balance_base": p.get("balance_base", p["balance"]),
                        "balance_optimistic": p.get("balance_optimistic", p["balance"]),
                        "balance_pessimistic": p.get(
                            "balance_pessimistic", p["balance"]
                        ),
                        "delta": 0.0,
                        "income": 0.0,
                        "expense": 0.0,
                        "is_forecast": p.get("is_forecast", False),
                        "resolution": resolution,
                        "data_coverage": forecast_coverage,
                    }
                # A weekly/monthly point represents the close of the bucket,
                # not its opening value. Forecast state follows that closing
                # point as well, so mixed current buckets are visibly projected.
                grouped[key]["balance"] = p["balance"]
                grouped[key]["balance_base"] = p.get("balance_base", p["balance"])
                grouped[key]["balance_optimistic"] = p.get(
                    "balance_optimistic", p["balance"]
                )
                grouped[key]["balance_pessimistic"] = p.get(
                    "balance_pessimistic", p["balance"]
                )
                grouped[key]["is_forecast"] = p.get("is_forecast", False)
                grouped[key]["delta"] += p.get("delta", 0.0)
                grouped[key]["income"] += p.get("income", 0.0)
                grouped[key]["expense"] += p.get("expense", 0.0)
            return list(grouped.values())
        else:
            for p in points:
                p["resolution"] = "daily"
            return points

    def detect_recurring_subscriptions(self) -> list[dict[str, Any]]:
        try:
            window_days = 90
            window_start = (date.today() - timedelta(days=window_days)).isoformat()
            rows = self.repo.q(f"""
                SELECT t.description, AVG(t.amount) as avg_amount, COUNT(*) as count,
                       MIN(COALESCE(NULLIF(m.payment_date,''),t.date)) AS first_date,
                       MAX(COALESCE(NULLIF(m.payment_date,''),t.date)) AS last_date
                FROM transactions t
                LEFT JOIN transaction_meta m ON m.transaction_id=t.id
                WHERE t.amount < 0
                  AND {_EXTERNAL_FLOW_SQL}
                  AND {_VALID_TRANSACTION_AMOUNT_SQL}
                  AND {_VALID_FINANCIAL_DATE_SQL}
                  AND COALESCE(NULLIF(m.payment_date,''),t.date) >= date('now', '-90 days')
                GROUP BY t.description
                HAVING count >= 2
            """)

            # Filter against existing subscriptions using set and fast checks
            existing_names = [
                str(r["name"] or "").strip().lower() for r in self.repo.q("SELECT name FROM subscriptions")
            ]
            existing_set = {name for name in existing_names if name}

            suspects = []
            for r in rows:
                desc = str(r["description"] or "").strip().lower()
                if not desc or desc in existing_set:
                    continue
                if any(ext in desc or desc in ext for ext in existing_set):
                    continue
                # RECUR-01: require a regular cadence before flagging a
                # merchant as a forgotten subscription. Two same-amount
                # purchases 75 days apart are NOT a monthly subscription.
                occurrences = self._occurrence_dates(
                    str(r["description"] or ""),
                    since_date=window_start,
                )
                if not self._has_monthly_cadence(occurrences):
                    continue
                suspects.append(
                    {
                        "name": r["description"],
                        "amount": abs(float(r["avg_amount"])),
                        "occurrences": int(r["count"]),
                    }
                )
            return suspects
        except Exception as e:
            log_exception("Failed to detect subscriptions", e)
            return []

    def _occurrence_dates(
        self,
        description: str,
        *,
        since_date: Optional[str] = None,
    ) -> list[date]:
        """Payment dates of every movement with this exact description."""

        where_extra = ""
        params: list[Any] = [description]
        if since_date:
            where_extra = " AND COALESCE(NULLIF(m.payment_date,''),t.date) >= ?"
            params.append(str(since_date)[:10])

        rows = self.repo.q(
            f"""
            SELECT COALESCE(NULLIF(m.payment_date,''),t.date) AS pay_date
            FROM transactions t
            LEFT JOIN transaction_meta m ON m.transaction_id=t.id
            WHERE t.description = ?
              AND t.amount < 0
              AND {_EXTERNAL_FLOW_SQL}
              AND {_VALID_TRANSACTION_AMOUNT_SQL}
              AND {_VALID_FINANCIAL_DATE_SQL}
              {where_extra}
            ORDER BY pay_date
            """,
            params,
        )
        dates: list[date] = []
        for row in rows:
            try:
                dates.append(date.fromisoformat(str(row["pay_date"])[:10]))
            except (TypeError, ValueError):
                continue
        return dates

    @classmethod
    def _is_cadence_match(cls, dates: list[date]) -> bool:
        if len(dates) < 2:
            return False
        gaps = [
            (later - earlier).days
            for earlier, later in zip(dates, dates[1:])
            if later > earlier
        ]
        gaps = [gap for gap in gaps if gap > 0]
        if not gaps:
            return False
        monthly = [gap for gap in gaps if 28 <= gap <= 31]
        if len(monthly) != len(gaps):
            return False
        if len(gaps) == 1:
            return True
        mean = sum(gaps) / len(gaps)
        variance = sum((gap - mean) ** 2 for gap in gaps) / (len(gaps) - 1)
        return variance**0.5 <= 3.0

    @classmethod
    def _has_monthly_cadence(cls, dates: list[date]) -> bool:
        """RECUR-01: monthly cadence = intervals of 28-31 days, stdev <= 3.

        With a single interval (two occurrences) the gap itself must be
        monthly-sized; with more occurrences the intervals must be both
        monthly-sized AND consistent (standard deviation <= 3 days).
        If an earlier occurrence in the sequence was an outlier, trailing
        sub-sequences of length >= 2 are also checked.
        """
        if len(dates) < 2:
            return False
        if cls._is_cadence_match(dates):
            return True
        for start_idx in range(1, len(dates) - 1):
            if cls._is_cadence_match(dates[start_idx:]):
                return True
        return False

    def balance_series(self, days: Optional[int] = 90) -> list[dict[str, Any]]:
        today = date.today()
        start = today - timedelta(days=days - 1) if days else None
        start_iso = iso(start) if start else "1900-01-01"
        opening_day = (start - timedelta(days=1)) if start else None
        balance = safe_float(
            self.repo.cash_balance(iso(opening_day)) if opening_day else 0.0
        )
        reference_days = {
            str(row["reference_date"] or "")[:10]
            for row in self.repo.q(
                "SELECT reference_date FROM financial_accounts "
                "WHERE is_active=1 AND balance_known=1 AND reference_date<>'' "
                "AND reference_date>=? AND reference_date<=?",
                (start_iso, iso(today)),
            )
        }
        rows = self.repo.q(
            f"""
            SELECT COALESCE(NULLIF(m.payment_date,''),t.date) AS financial_date,
                   SUM(t.amount) AS delta,
                   SUM(CASE WHEN t.amount>0 AND {_EXTERNAL_FLOW_SQL}
                            THEN t.amount ELSE 0 END) AS income,
                   SUM(CASE WHEN t.amount<0 AND {_EXTERNAL_FLOW_SQL}
                            THEN ABS(t.amount) ELSE 0 END) AS expense
            FROM transactions t
            LEFT JOIN transaction_meta m ON m.transaction_id=t.id
            WHERE COALESCE(NULLIF(m.payment_date,''),t.date)>=?
              AND COALESCE(NULLIF(m.payment_date,''),t.date)<=?
              AND {_VALID_TRANSACTION_AMOUNT_SQL}
              AND {_VALID_FINANCIAL_DATE_SQL}
            GROUP BY financial_date
            ORDER BY financial_date ASC
            """,
            (start_iso, iso(today)),
        )
        by_day = {str(row["financial_date"]): row for row in rows}
        current = start or (
            parse_date(str(rows[0]["financial_date"]))
            if rows
            else today - timedelta(days=30)
        )
        points: list[dict[str, Any]] = []
        while current <= today:
            key = iso(current)
            row = by_day.get(key)
            delta = float(row["delta"] or 0) if row else 0.0
            income = float(row["income"] or 0) if row else 0.0
            expense = float(row["expense"] or 0) if row else 0.0
            if key in reference_days:
                balance = safe_float(self.repo.cash_balance(key))
            else:
                balance += delta
            points.append(
                {
                    "date": key,
                    "balance": balance,
                    "delta": delta,
                    "income": income,
                    "expense": expense,
                }
            )
            current += timedelta(days=1)
        return points

    def monthly_category_budget_rows(
        self,
        month_key: str,
        through_date: Optional[date] = None,
    ) -> list[dict[str, Any]]:
        start, end = self.repo.month_bounds_from_key(month_key)
        effective_end = end
        if through_date is not None:
            start_day = parse_date(start)
            end_day = parse_date(end)
            cutoff = through_date + timedelta(days=1)
            effective_end = iso(min(end_day, max(start_day, cutoff)))
        previous_start, previous_end = self.repo.month_bounds_from_key(
            self.repo.previous_month_key(month_key)
        )
        rows = self.repo.category_spend_with_splits(
            start,
            effective_end,
            end_exclusive=True,
        )
        previous_rows = self.repo.category_spend_with_splits(
            previous_start,
            previous_end,
            end_exclusive=True,
        )
        spent = {
            str(row["category"] or "Sem categoria"): safe_float(row["total"])
            for row in rows
        }
        previous = {
            str(row["category"] or "Sem categoria"): safe_float(row["total"])
            for row in previous_rows
        }
        budgets = self.repo.category_budgets("expense", month_key)
        result: list[dict[str, Any]] = []
        for category in sorted(set(budgets) | set(spent)):
            current_spend = float(spent.get(category, 0.0))
            budget = max(0.0, float(budgets.get(category, 0.0)))
            previous_spend = float(previous.get(category, 0.0))
            utilisation = current_spend / budget * 100 if budget > 0 else 0.0
            if budget <= 0:
                status = "Sem orçamento"
            elif current_spend <= budget * 0.8:
                status = "Controlado"
            elif current_spend <= budget:
                status = "Perto do limite"
            else:
                status = "Ultrapassado"
            if current_spend > 0 or budget > 0:
                result.append(
                    {
                        "category": category,
                        "spent": current_spend,
                        "budget": budget,
                        "utilisation": utilisation,
                        "saved_vs_budget": budget - current_spend if budget > 0 else 0.0,
                        "previous_spend": previous_spend,
                        "delta_vs_previous": current_spend - previous_spend,
                        "status": status,
                    }
                )
        result.sort(key=lambda row: safe_float(row.get("spent")), reverse=True)
        return result

    def monthly_budget_forecast(
        self,
        month_key: Optional[str] = None,
        as_of: Optional[date] = None,
    ) -> dict[str, Any]:
        """Forecast category budgets using current spend and last month's pattern."""
        reference = as_of or date.today()
        key = str(month_key or reference.strftime("%Y-%m"))[:7]
        start_iso, end_iso = self.repo.month_bounds_from_key(key)
        start_day = parse_date(start_iso)
        end_day = parse_date(end_iso)
        days_in_month = max(1, (end_day - start_day).days)
        if reference < start_day:
            elapsed_days = 0
        elif reference >= end_day:
            elapsed_days = days_in_month
        else:
            elapsed_days = (reference - start_day).days + 1
        remaining_days = max(0, days_in_month - elapsed_days)
        remaining_fraction = remaining_days / days_in_month
        completed = elapsed_days >= days_in_month
        accuracy = self.forecast_accuracy(
            reference_date=reference,
            visible_months=12,
        )
        forecast_learning = dict(accuracy.get("learning") or {})
        learning_enabled = bool(forecast_learning.get("enabled")) and not completed
        correction_factor = (
            safe_float(forecast_learning.get("correction_factor"), 1.0)
            if learning_enabled
            else 1.0
        )
        optimistic_multiplier = (
            safe_float(forecast_learning.get("optimistic_multiplier"), 0.8)
            if learning_enabled
            else 0.8
        )
        pessimistic_multiplier = (
            safe_float(forecast_learning.get("pessimistic_multiplier"), 1.2)
            if learning_enabled
            else 1.2
        )

        rows = self.monthly_category_budget_rows(key, through_date=reference)
        forecast_rows: list[dict[str, Any]] = []
        used_history = False
        for source in rows:
            row = dict(source)
            spent = safe_float(row.get("spent"))
            budget = max(0.0, safe_float(row.get("budget")))
            previous_spend = safe_float(row.get("previous_spend"))
            if completed:
                projected = spent
                projection_basis = "final"
            elif previous_spend > 0:
                projected = (
                    spent + previous_spend * remaining_fraction * correction_factor
                )
                projection_basis = "mês anterior"
                used_history = True
            elif elapsed_days > 0:
                current_daily = spent / elapsed_days
                projected = spent + current_daily * remaining_days * correction_factor
                projection_basis = "ritmo atual"
            else:
                projected = previous_spend * correction_factor
                projection_basis = "histórico"
            projected = max(spent, projected)
            remaining_projection = max(0.0, projected - spent)
            projected_optimistic = spent + (
                remaining_projection * optimistic_multiplier
            )
            projected_pessimistic = spent + (
                remaining_projection * pessimistic_multiplier
            )
            projected_utilisation = projected / budget * 100 if budget > 0 else 0.0
            projected_margin = budget - projected if budget > 0 else -projected

            if budget <= 0:
                forecast_status = "Sem orçamento"
            elif spent > budget:
                forecast_status = "Ultrapassado"
            elif projected > budget:
                forecast_status = "Em risco"
            elif projected >= budget * 0.9:
                forecast_status = "Atenção"
            else:
                forecast_status = "Controlado"

            row.update(
                {
                    "projected": projected,
                    "projected_optimistic": projected_optimistic,
                    "projected_pessimistic": projected_pessimistic,
                    "projected_utilisation": projected_utilisation,
                    "projected_margin": projected_margin,
                    "forecast_status": forecast_status,
                    "projection_basis": projection_basis,
                }
            )
            forecast_rows.append(row)

        priority = {
            "Ultrapassado": 0,
            "Em risco": 1,
            "Sem orçamento": 2,
            "Atenção": 3,
            "Controlado": 4,
        }
        forecast_rows.sort(
            key=lambda row: (
                priority.get(str(row.get("forecast_status")), 9),
                -safe_float(row.get("projected_utilisation")),
                -safe_float(row.get("spent")),
            )
        )

        total_budget = sum(safe_float(row.get("budget")) for row in forecast_rows)
        total_spent = sum(safe_float(row.get("spent")) for row in forecast_rows)
        total_projected = sum(safe_float(row.get("projected")) for row in forecast_rows)
        total_projected_optimistic = sum(
            safe_float(row.get("projected_optimistic")) for row in forecast_rows
        )
        total_projected_pessimistic = sum(
            safe_float(row.get("projected_pessimistic")) for row in forecast_rows
        )
        budgeted_spend = sum(
            safe_float(row.get("spent"))
            for row in forecast_rows
            if safe_float(row.get("budget")) > 0
        )
        unbudgeted_spend = max(0.0, total_spent - budgeted_spend)
        coverage_pct = budgeted_spend / total_spent * 100 if total_spent else 100.0
        projected_margin = total_budget - total_projected
        over_count = sum(
            1 for row in forecast_rows if row.get("forecast_status") == "Ultrapassado"
        )
        risk_count = sum(
            1 for row in forecast_rows if row.get("forecast_status") == "Em risco"
        )
        attention_count = sum(
            1 for row in forecast_rows if row.get("forecast_status") == "Atenção"
        )

        if total_budget <= 0:
            overall_status = "Sem orçamentos definidos"
            overall_tone = "neutral"
        elif over_count or total_spent > total_budget:
            overall_status = "Orçamento já ultrapassado"
            overall_tone = "negative"
        elif risk_count or projected_margin < 0:
            overall_status = "Mês em risco"
            overall_tone = "warning"
        elif attention_count or unbudgeted_spend > 0:
            overall_status = "Atenção ao ritmo"
            overall_tone = "warning"
        else:
            overall_status = "Dentro do plano"
            overall_tone = "positive"

        if completed:
            method_label = "Mês concluído: valores finais registados."
        elif used_history:
            method_label = (
                "Previsão: gasto atual + padrão do mês anterior nos dias restantes."
            )
        else:
            method_label = "Previsão baseada no ritmo de gasto atual."
        if learning_enabled:
            method_label += (
                " O histórico corrigiu o ritmo pelos erros de "
                f"{int(forecast_learning.get('sample_months', 0) or 0)} "
                "meses completos."
            )
            if bool(forecast_learning.get("accuracy_available")):
                method_label += (
                    " Cobertura observada em "
                    f"{int(forecast_learning.get('validation_months', 0) or 0)} "
                    "meses previstos sem conhecer o resultado: "
                    f"{safe_float(forecast_learning.get('accuracy_pct')):.0f}% "
                    "dos resultados dentro das faixas previstas."
                )
            else:
                method_label += " A cobertura das faixas ainda está em validação."

        return {
            "month": key,
            "month_label": f"{PT_MONTHS[start_day.month - 1].capitalize()} {start_day.year}",
            "as_of": iso(min(max(reference, start_day), end_day - timedelta(days=1))),
            "days_elapsed": elapsed_days,
            "days_in_month": days_in_month,
            "days_remaining": remaining_days,
            "is_complete": completed,
            "expected_utilisation": elapsed_days / days_in_month * 100,
            "total_budget": total_budget,
            "total_spent": total_spent,
            "total_projected": total_projected,
            "total_projected_optimistic": total_projected_optimistic,
            "total_projected_pessimistic": total_projected_pessimistic,
            "projected_margin": projected_margin,
            "unbudgeted_spend": unbudgeted_spend,
            "coverage_pct": coverage_pct,
            "over_count": over_count,
            "risk_count": risk_count,
            "attention_count": attention_count,
            "overall_status": overall_status,
            "overall_tone": overall_tone,
            "method_label": method_label,
            "forecast_learning": forecast_learning,
            "data_coverage": self.data_coverage_snapshot(
                iso(add_months(start_day, -12, 1)),
                iso(min(reference, end_day - timedelta(days=1))),
            ),
            "rows": forecast_rows,
        }

    def cashflow_calendar_forecast(
        self,
        horizon_days: int = 90,
        as_of: Optional[date] = None,
    ) -> dict[str, Any]:
        reference = as_of or date.today()
        result = CashflowForecastService(
            self.repo,
            self.payments_service,
        ).cashflow_calendar_forecast(horizon_days, as_of)
        result["data_coverage"] = self.data_coverage_snapshot(
            iso(add_months(reference.replace(day=1), -12, 1)),
            iso(reference),
        )
        return result

    def fixed_commitments_evolution(
        self,
        months: int = 12,
        as_of: Optional[date] = None,
    ) -> dict[str, Any]:
        reference = as_of or date.today()
        result = CashflowForecastService(
            self.repo,
            self.payments_service,
        ).fixed_commitments_evolution(months, as_of)
        result["data_coverage"] = self.data_coverage_snapshot(
            iso(add_months(reference.replace(day=1), -max(3, int(months)), 1)),
            iso(reference),
        )
        return result

    def cash_runway_scenarios(
        self,
        months: int = 12,
        as_of: Optional[date] = None,
    ) -> dict[str, Any]:
        """Project monthly cash and each month's probability of ending above zero.

        The estimate uses completed historical months only. It is intentionally
        separate from the interactive stress test: this is an operational
        baseline, not a promise of future performance.  ``probability_positive``
        is a marginal probability for the row's month; it does not mean that
        every preceding month in the projection also stayed above zero.
        """

        reference = as_of or date.today()
        horizon = max(3, min(24, int(months or 12)))
        current_month = reference.replace(day=1)
        history_start = add_months(current_month, -12, 1)
        history_end = current_month - timedelta(days=1)
        history = [
            row
            for row in self.monthly_summary_for_period(
                iso(history_start),
                iso(history_end),
            )
            if int(row.get("movement_count", 0) or 0) > 0
        ]
        nets = [
            safe_float(row.get("income")) - safe_float(row.get("expenses"))
            for row in history
        ]
        if nets:
            weights = list(range(1, len(nets) + 1))
            weighted_net = sum(
                value * weight for value, weight in zip(nets, weights)
            ) / sum(weights)
            volatility = statistics.pstdev(nets) if len(nets) > 1 else 0.0
        else:
            weighted_net = 0.0
            volatility = 0.0
        starting_balance = safe_float(self.repo.cash_balance(iso(reference)))
        rows: list[dict[str, Any]] = []
        for index in range(horizon + 1):
            month_date = add_months(current_month, index, 1)
            uncertainty = volatility * math.sqrt(index)
            base = starting_balance + weighted_net * index
            p10 = base - 1.2815515655 * uncertainty
            p90 = base + 1.2815515655 * uncertainty
            pessimistic = starting_balance + (weighted_net - volatility) * index
            optimistic = starting_balance + (weighted_net + volatility * 0.65) * index
            if index == 0:
                probability_positive = 100.0 if starting_balance >= 0 else 0.0
            elif uncertainty > 0:
                probability_positive = (
                    0.5 * (1.0 + math.erf(base / uncertainty / math.sqrt(2.0))) * 100.0
                )
            else:
                probability_positive = 100.0 if base >= 0 else 0.0
            rows.append(
                {
                    "month": month_date.strftime("%Y-%m"),
                    "label": "Hoje" if index == 0 else f"M{index}",
                    "month_index": index,
                    "base": round(base, 2),
                    "optimistic": round(optimistic, 2),
                    "stressed": round(pessimistic, 2),
                    "p10": round(p10, 2),
                    "p50": round(base, 2),
                    "p90": round(p90, 2),
                    "probability_positive": round(
                        max(0.0, min(100.0, probability_positive)),
                        1,
                    ),
                    "probability_scope": "month_end",
                    "probability_label": "Este mês termina acima de 0 €",
                }
            )
        breach_month = next(
            (
                int(row["month_index"])
                for row in rows[1:]
                if safe_float(row.get("base")) < 0
            ),
            None,
        )
        sample_months = len(history)
        if sample_months >= 9:
            confidence, confidence_label = "high", "Confiança alta"
        elif sample_months >= 4:
            confidence, confidence_label = "medium", "Confiança moderada"
        else:
            confidence, confidence_label = "initial", "Confiança inicial"
        return {
            "rows": rows,
            "starting_balance": starting_balance,
            "monthly_net": weighted_net,
            "monthly_volatility": volatility,
            "sample_months": sample_months,
            "confidence": confidence,
            "confidence_label": confidence_label,
            "breach_month": breach_month,
            "final_month_probability_positive": rows[-1]["probability_positive"],
            # Compatibility alias for stored UI state and older callers.
            "ending_probability_positive": rows[-1]["probability_positive"],
            "probability_scope": "month_end",
            "probability_definition": (
                "Probabilidade de o saldo projetado de cada mês terminar acima "
                "de 0 €; não mede se o saldo permaneceu positivo em todos os "
                "meses anteriores."
            ),
            "method_label": (
                f"{confidence_label}: média recente ponderada de {sample_months} "
                "mês(es) completos; a faixa P10–P90 aumenta com a variabilidade "
                "observada. A percentagem é calculada separadamente para o fim "
                "de cada mês, não para todo o percurso até esse mês."
            ),
            "data_coverage": self.data_coverage_snapshot(
                iso(history_start),
                iso(history_end),
            ),
        }

    def budget_variance_heatmap(
        self,
        months: int = 12,
        as_of: Optional[date] = None,
    ) -> dict[str, Any]:
        """Return every category/month budget deviation for an interactive grid."""

        reference = as_of or date.today()
        visible = max(3, min(24, int(months or 12)))
        first = add_months(reference.replace(day=1), -(visible - 1), 1)
        month_rows: list[dict[str, Any]] = []
        cells: list[dict[str, Any]] = []
        categories: set[str] = set()
        for offset in range(visible):
            month_start = add_months(first, offset, 1)
            month_key = month_start.strftime("%Y-%m")
            month_rows.append(
                {
                    "month": month_key,
                    "label": f"{PT_MONTHS[month_start.month - 1][:3].capitalize()} "
                    f"{str(month_start.year)[2:]}",
                }
            )
            cutoff = reference if month_key == reference.strftime("%Y-%m") else None
            for row in self.monthly_category_budget_rows(month_key, cutoff):
                category = str(row.get("category") or "Sem categoria")
                spent = max(0.0, safe_float(row.get("spent")))
                budget = max(0.0, safe_float(row.get("budget")))
                if spent <= 0 and budget <= 0:
                    continue
                categories.add(category)
                variance = spent - budget
                if budget > 0:
                    utilisation = spent / budget * 100.0
                    variance_pct = variance / budget * 100.0
                else:
                    utilisation = 0.0
                    variance_pct = 150.0 if spent > 0 else 0.0
                if budget <= 0 and spent > 0:
                    status, tone = "Sem orçamento", "negative"
                elif utilisation > 100:
                    status, tone = "Ultrapassado", "negative"
                elif utilisation >= 80:
                    status, tone = "Perto do limite", "warning"
                else:
                    status, tone = "Controlado", "positive"
                cells.append(
                    {
                        "month": month_key,
                        "month_label": month_rows[-1]["label"],
                        "category": category,
                        "spent": spent,
                        "budget": budget,
                        "variance": variance,
                        "variance_pct": variance_pct,
                        "utilisation": utilisation,
                        "status": status,
                        "tone": tone,
                    }
                )
        category_totals = {
            category: sum(
                safe_float(cell.get("spent"))
                for cell in cells
                if cell.get("category") == category
            )
            for category in categories
        }
        ordered_categories = sorted(
            categories,
            key=lambda value: (-category_totals[value], value.casefold()),
        )
        return {
            "months": month_rows,
            "categories": ordered_categories,
            "cells": cells,
            "category_count": len(ordered_categories),
            "over_count": sum(1 for cell in cells if cell["tone"] == "negative"),
            "warning_count": sum(1 for cell in cells if cell["tone"] == "warning"),
        }

    def recurring_commitment_timeline(
        self,
        months: int = 6,
        as_of: Optional[date] = None,
    ) -> dict[str, Any]:
        """Separate explicit commitments from inferred and unlinked expenses."""

        from services.subscription_matching import (
            normalized_subscription_text,
            subscription_match_terms,
        )

        reference = as_of or date.today()
        horizon = max(3, min(12, int(months or 6)))
        end = add_months(reference, horizon, reference.day)
        events: list[dict[str, Any]] = []
        subscriptions = list(self.repo.subscriptions(active_only=True))
        exact_terms = {
            term
            for subscription in subscriptions
            for term in subscription_match_terms(subscription)
        }
        explicit_terms = set(exact_terms)
        for subscription in subscriptions:
            due = self.payments_service.next_subscription_due_date(
                subscription,
                reference,
            )
            for _iteration in range(24):
                if due > end:
                    break
                events.append(
                    {
                        "date": due.isoformat(),
                        "name": str(row_get(subscription, "name", "Subscrição")),
                        "category": str(
                            row_get(subscription, "category", "Subscrições")
                        ),
                        "amount": abs(safe_float(row_get(subscription, "amount", 0))),
                        "status_key": "confirmed",
                        "status": "Confirmado",
                        "source": "subscription",
                    }
                )
                due = self.payments_service.advance_subscription_due(
                    subscription,
                    due,
                )
        for payment in self.repo.scheduled_payments():
            if int(row_get(payment, "active", 1) or 0) != 1:
                continue
            explicit_terms.add(
                normalized_subscription_text(row_get(payment, "name", ""))
            )
            paid = max(0, int(row_get(payment, "months_paid", 0) or 0))
            total = self.payments_service._months_total(payment)
            limit = total if total > 0 else paid + horizon + 1
            for index in range(paid, limit):
                due = self.payments_service.due_date_for_payment(payment, index)
                if due > end:
                    break
                if due < reference:
                    continue
                events.append(
                    {
                        "date": due.isoformat(),
                        "name": str(row_get(payment, "name", "Pagamento")),
                        "category": str(row_get(payment, "category", "Outros")),
                        "amount": abs(safe_float(row_get(payment, "amount", 0))),
                        "status_key": "confirmed",
                        "status": "Confirmado",
                        "source": "scheduled",
                    }
                )
        probable_keys: set[str] = set()
        for recurring in self.payments_service.likely_recurring_expenses(20):
            key = normalized_subscription_text(recurring.get("description"))
            if not key or key in explicit_terms:
                continue
            probable_keys.add(key)
            try:
                due = add_months(
                    parse_date(str(recurring.get("last_date") or "")[:10]),
                    1,
                )
            except (TypeError, ValueError):
                continue
            while due < reference:
                due = add_months(due, 1)
            while due <= end:
                events.append(
                    {
                        "date": due.isoformat(),
                        "name": str(
                            recurring.get("description") or "Recorrente provável"
                        ),
                        "category": str(recurring.get("category") or "Outros"),
                        "amount": abs(safe_float(recurring.get("amount"))),
                        "status_key": "probable",
                        "status": "Provável · confirmar",
                        "source": "inferred",
                        "confidence": str(recurring.get("confidence") or ""),
                    }
                )
                due = add_months(due, 1)
        recent_start = add_months(reference, -6).isoformat()
        unlinked_rows = self.repo.q(
            f"""
            SELECT LOWER(TRIM(t.description)) AS description_key,
                   MIN(t.description) AS description,
                   MIN(t.category) AS category,
                   ROUND(AVG(ABS(t.amount)),2) AS amount,
                   MAX({_FINANCIAL_DATE_SQL}) AS last_date
            FROM transactions t
            LEFT JOIN transaction_meta m ON m.transaction_id=t.id
            LEFT JOIN subscription_occurrences o ON o.transaction_id=t.id
            WHERE t.amount<0 AND o.transaction_id IS NULL
              AND t.category='Subscrições'
              AND {_FINANCIAL_DATE_SQL}>=?
              AND {_VALID_TRANSACTION_AMOUNT_SQL}
              AND {_VALID_FINANCIAL_DATE_SQL}
            GROUP BY description_key, LOWER(TRIM(t.account))
            """,
            (recent_start,),
        )
        for candidate in unlinked_rows:
            key = normalized_subscription_text(candidate["description_key"])
            if not key or key in explicit_terms or key in probable_keys:
                continue
            try:
                due = add_months(parse_date(str(candidate["last_date"])[:10]), 1)
            except (TypeError, ValueError):
                continue
            while due < reference:
                due = add_months(due, 1)
            while due <= end:
                events.append(
                    {
                        "date": due.isoformat(),
                        "name": str(
                            candidate["description"] or "Subscrição por associar"
                        ),
                        "category": str(candidate["category"] or "Subscrições"),
                        "amount": abs(safe_float(candidate["amount"])),
                        "status_key": "unlinked",
                        "status": "Por associar",
                        "source": "unlinked",
                    }
                )
                due = add_months(due, 1)
        month_rows: list[dict[str, Any]] = []
        for offset in range(horizon + 1):
            month_start = add_months(reference.replace(day=1), offset, 1)
            key = month_start.strftime("%Y-%m")
            selected = [event for event in events if str(event["date"]).startswith(key)]
            confirmed = sum(
                safe_float(event["amount"])
                for event in selected
                if event["status_key"] == "confirmed"
            )
            probable = sum(
                safe_float(event["amount"])
                for event in selected
                if event["status_key"] == "probable"
            )
            unlinked = sum(
                safe_float(event["amount"])
                for event in selected
                if event["status_key"] == "unlinked"
            )
            month_rows.append(
                {
                    "month": key,
                    "label": f"{PT_MONTHS[month_start.month - 1][:3].capitalize()} "
                    f"{str(month_start.year)[2:]}",
                    "confirmed": confirmed,
                    "probable": probable,
                    "unlinked": unlinked,
                    "total": confirmed + probable + unlinked,
                    "event_count": len(selected),
                    "events": selected,
                }
            )
        return {
            "rows": month_rows,
            "events": sorted(events, key=lambda event: (event["date"], event["name"])),
            "confirmed_total": sum(row["confirmed"] for row in month_rows),
            "probable_total": sum(row["probable"] for row in month_rows),
            "unlinked_total": sum(row["unlinked"] for row in month_rows),
            "method_label": (
                "Confirmado = subscrições e pagamentos registados; provável = padrão "
                "recorrente por confirmar; por associar = despesa de subscrição sem ligação."
            ),
        }

    def monte_carlo_projection(
        self,
        contribution: float,
        annual_return_pct: float = 6.0,
        annual_volatility_pct: float = 12.0,
        years: int = 10,
        **options: Any,
    ) -> dict[str, Any]:
        result = build_monte_carlo_projection(
            contribution,
            annual_return_pct,
            annual_volatility_pct,
            years,
            **options,
        )
        reference = date.today()
        result["data_coverage"] = self.data_coverage_snapshot(
            iso(add_months(reference.replace(day=1), -24, 1)),
            iso(reference),
        )
        return result

    def net_worth_snapshot(self, as_of: Optional[date] = None) -> dict[str, Any]:
        return self.intel_service.net_worth_snapshot(as_of)

    def net_worth_bridge(self, start: str, end: str) -> dict[str, Any]:
        return self.intel_service.net_worth_bridge(start, end)

    def smart_budget_analysis(
        self, month_key: str, as_of: Optional[date] = None
    ) -> dict[str, Any]:
        return self.intel_service.smart_budget_analysis(month_key, as_of)

    def detect_financial_anomalies(
        self,
        as_of: Optional[date] = None,
        lookback_days: int = 180,
        reporting_days: int = 45,
    ) -> list[dict[str, Any]]:
        return self.intel_service.detect_anomalies(as_of, lookback_days, reporting_days)

    def financial_alert_center(
        self, as_of: Optional[date] = None
    ) -> list[dict[str, Any]]:
        return self.intel_service.alert_center(as_of)

    def compare_periods(
        self,
        current_start: str,
        current_end: str,
        reference_start: str,
        reference_end: str,
    ) -> dict[str, Any]:
        return self.intel_service.compare_periods(
            current_start,
            current_end,
            reference_start,
            reference_end,
        )

    def explain_period_variation(
        self, comparison: dict[str, Any]
    ) -> list[dict[str, Any]]:
        return self.intel_service.explain_period_variation(comparison)

    def annual_report_data(self, year: int) -> dict[str, Any]:
        return self.intel_service.annual_report_data(year)

    def investment_projection(
        self,
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
        result = build_investment_projection(
            contribution,
            annual_rate_pct,
            years,
            mode=mode,
            initial_capital=initial_capital,
            inflation_pct=inflation_pct,
            tax_pct=tax_pct,
            fee_pct=fee_pct,
            scenario_spread_pct=scenario_spread_pct,
        )
        reference = date.today()
        result["data_coverage"] = self.data_coverage_snapshot(
            iso(add_months(reference.replace(day=1), -24, 1)),
            iso(reference),
        )
        return result

    def payment_method_totals(
        self,
        start: str,
        end: str,
    ) -> list[tuple[str, float, str]]:
        rows = self.repo.q(
            f"""
            SELECT COALESCE(NULLIF(m.payment_method,''),'Não definido') AS method,
                   SUM(ABS(t.amount)) AS total
            FROM transactions t
            LEFT JOIN transaction_meta m ON m.transaction_id=t.id
            WHERE COALESCE(NULLIF(m.payment_date,''), t.date)>=?
              AND COALESCE(NULLIF(m.payment_date,''), t.date)<=?
              AND t.amount<0
              AND {_EXTERNAL_FLOW_SQL}
              AND {_VALID_TRANSACTION_AMOUNT_SQL}
              AND {_VALID_FINANCIAL_DATE_SQL}
            GROUP BY method
            ORDER BY total DESC
            """,
            (start, end),
        )
        colours = [GREEN, BLUE, PURPLE, ORANGE, PINK, TEAL, YELLOW]
        return [
            (
                str(row["method"]),
                float(row["total"] or 0),
                colours[index % len(colours)],
            )
            for index, row in enumerate(rows)
        ]

    def monthly_evolution(self, year: int) -> list[dict[str, Any]]:
        curr_map = self.repo.monthly_totals_grouped_for_year(year)
        prev_map = self.repo.monthly_totals_grouped_for_year(year - 1)
        result: list[dict[str, Any]] = []
        for month in range(1, 13):
            month_key = f"{year:04d}-{month:02d}"
            prev_key = f"{year - 1:04d}-{month:02d}"
            curr_data = curr_map.get(
                month_key, {"income": 0.0, "expenses": 0.0, "savings": 0.0}
            )
            prev_data = prev_map.get(
                prev_key, {"income": 0.0, "expenses": 0.0, "savings": 0.0}
            )
            result.append(
                {
                    "month": month_key,
                    "label": PT_MONTHS[month - 1][:3].capitalize(),
                    "prev_income": prev_data["income"],
                    "prev_expenses": prev_data["expenses"],
                    "income": curr_data["income"],
                    "expenses": curr_data["expenses"],
                    "savings": curr_data["savings"],
                    "investments": 0.0,
                    "investments_available": False,
                    "investment_as_of": "",
                    "investment_source": "unavailable",
                }
            )
        return result

    def monthly_review_data(self, month_key: str) -> dict[str, Any]:
        year, month = int(month_key[:4]), int(month_key[5:7])
        start, end, _ = self.repo.month_period(year, month)
        totals = self.repo.totals_for_payment_period(start, end)
        previous = add_months(date(year, month, 1), -1)
        previous_start, previous_end, _ = self.repo.month_period(
            previous.year,
            previous.month,
        )
        previous_totals = self.repo.totals_for_payment_period(
            previous_start,
            previous_end,
        )
        categories = self.repo.top_categories_payment_period(start, end, 10)
        uncategorized = int(
            self.repo.scalar(
                "SELECT COUNT(*) FROM transactions t "
                "LEFT JOIN transaction_meta m ON m.transaction_id=t.id "
                "WHERE COALESCE(NULLIF(m.payment_date,''),t.date)>=? "
                "AND COALESCE(NULLIF(m.payment_date,''),t.date)<=? "
                "AND (t.category='' OR t.category IN ('Outros','Diversos'))",
                (start, end),
            )
        )
        no_attachment = int(
            self.repo.scalar(
                f"""
                SELECT COUNT(*) FROM transactions t
                LEFT JOIN transaction_meta m ON m.transaction_id=t.id
                LEFT JOIN (
                    SELECT transaction_id, COUNT(*) cnt
                    FROM transaction_attachments
                    GROUP BY transaction_id
                ) a ON a.transaction_id=t.id
                WHERE COALESCE(NULLIF(m.payment_date,''),t.date)>=?
                  AND COALESCE(NULLIF(m.payment_date,''),t.date)<=?
                  AND t.amount<0
                  AND {_EXTERNAL_FLOW_SQL}
                  AND {_VALID_TRANSACTION_AMOUNT_SQL}
                  AND {_VALID_FINANCIAL_DATE_SQL}
                  AND ABS(t.amount)>=100
                  AND IFNULL(a.cnt,0)=0
                """,
                (start, end),
            )
        )
        duplicates = self.repo.q(
            """
            SELECT COALESCE(NULLIF(m.payment_date,''),t.date) AS d,
                   t.description, t.amount, COUNT(*) c
            FROM transactions t
            LEFT JOIN transaction_meta m ON m.transaction_id=t.id
            WHERE COALESCE(NULLIF(m.payment_date,''),t.date)>=?
              AND COALESCE(NULLIF(m.payment_date,''),t.date)<=?
            GROUP BY d,t.description,t.amount
            HAVING c>1
            ORDER BY d DESC, t.description
            """,
            (start, end),
        )
        quality = self.repo.transaction_data_quality_counts(start, end)
        return {
            "start": start,
            "end": end,
            "totals": totals,
            "prev_totals": previous_totals,
            "categories": categories,
            "uncategorized": uncategorized,
            "no_attachment": no_attachment,
            "duplicates": duplicates,
            "invalid_dates": int(quality.get("invalid_dates", 0)),
            "invalid_amounts": int(quality.get("invalid_amounts", 0)),
        }

    def detailed_financial_score(self, month_key: str) -> dict[str, Any]:
        return self.health_service.financial_score(month_key)

    def risk_resilience_analysis(
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
        return self.health_service.risk_resilience(
            as_of=as_of,
            horizon_months=horizon_months,
            income_reduction_pct=income_reduction_pct,
            income_reduction_months=income_reduction_months,
            emergency_expense=emergency_expense,
            market_drawdown_pct=market_drawdown_pct,
            simulations=simulations,
        )

    def monthly_goal_progress(
        self, row: Any, privacy: bool = False
    ) -> tuple[float, str, str]:
        month_key = str(row["month"])
        year, month = int(month_key[:4]), int(month_key[5:7])
        start, end, _ = self.repo.month_period(year, month)
        goal_type = str(row["goal_type"])
        category = str(row["category"] or "")
        target = safe_float(row["target_value"])
        denom = max(1.0, target) if target > 0 else 1.0
        if goal_type == "Poupar pelo menos":
            current = safe_float(
                self.repo.totals_for_payment_period(start, end).get("savings", 0.0)
            )
            return (
                min(100.0, max(0.0, current / denom * 100.0)),
                eur(current, privacy),
                "Concluído" if current >= target else "Em progresso",
            )
        if goal_type == "Investir pelo menos":
            current_raw = self.repo.scalar(
                f"SELECT SUM(ABS(t.amount)) FROM transactions t "
                f"LEFT JOIN transaction_meta m ON m.transaction_id=t.id "
                f"WHERE {_FINANCIAL_DATE_SQL}>=? "
                f"AND {_FINANCIAL_DATE_SQL}<=? AND t.amount<0 "
                f"AND {_VALID_TRANSACTION_AMOUNT_SQL} "
                f"AND {_VALID_FINANCIAL_DATE_SQL} "
                "AND LOWER(t.category) LIKE '%invest%'",
                (start, end),
            )
            current = safe_float(current_raw)
            return (
                min(100.0, max(0.0, current / denom * 100.0)),
                eur(current, privacy),
                "Concluído" if current >= target else "Em progresso",
            )
        current_raw = self.repo.scalar(
            f"SELECT SUM(ABS(t.amount)) FROM transactions t "
            f"LEFT JOIN transaction_meta m ON m.transaction_id=t.id "
            f"WHERE {_FINANCIAL_DATE_SQL}>=? "
            f"AND {_FINANCIAL_DATE_SQL}<=? "
            f"AND t.amount<0 AND {_VALID_TRANSACTION_AMOUNT_SQL} "
            f"AND {_VALID_FINANCIAL_DATE_SQL} "
            f"AND {_EXTERNAL_FLOW_SQL} "
            "AND (?='' OR t.category=?)",
            (start, end, category, category),
        )
        current = safe_float(current_raw)
        percentage = min(999.0, max(0.0, current / denom * 100.0))
        return (
            percentage,
            eur(current, privacy),
            "Dentro do limite" if current <= target else "Ultrapassado",
        )

    def monthly_summary(self, months: int = 12) -> list[dict[str, Any]]:
        months = max(1, int(months or 1))
        today = date.today()
        start = add_months(today.replace(day=1), -(months - 1), 1)
        return self.monthly_summary_for_period(iso(start), iso(today))

    def monthly_summary_for_period(self, start: str, end: str) -> list[dict[str, Any]]:
        """Return monthly buckets clipped to one exact inclusive date window."""
        cache_key = (str(start or ""), str(end or ""))
        if cache_key in self._monthly_summary_cache:
            return [dict(x) for x in self._monthly_summary_cache[cache_key]]

        start_day = parse_date(start)

        end_day = parse_date(end)
        if start_day > end_day:
            return []

        rows = self.repo.q(
            f"""
            SELECT SUBSTR({_FINANCIAL_DATE_SQL},1,7) AS month_key,
                   MIN({_FINANCIAL_DATE_SQL}) AS first_date,
                   SUM(CASE WHEN t.amount>0 THEN t.amount ELSE 0 END) AS income,
                   SUM(CASE WHEN t.amount<0 THEN ABS(t.amount) ELSE 0 END) AS expenses,
                   COUNT(*) AS movement_count
            FROM transactions t
            LEFT JOIN transaction_meta m ON m.transaction_id=t.id
            WHERE {_FINANCIAL_DATE_SQL}>=?
              AND {_FINANCIAL_DATE_SQL}<=?
              AND {_EXTERNAL_FLOW_SQL}
              AND {_VALID_TRANSACTION_AMOUNT_SQL}
              AND {_VALID_FINANCIAL_DATE_SQL}
            GROUP BY 1
            ORDER BY month_key
            """,
            (iso(start_day), iso(end_day)),
        )

        monthly: dict[str, dict[str, float | int]] = {}
        first_dates: list[date] = []
        for row in rows:
            month_key = str(row["month_key"] or "")[:7]
            try:
                date.fromisoformat(f"{month_key}-01")
            except (TypeError, ValueError):
                continue
            try:
                first_dates.append(
                    date.fromisoformat(str(row["first_date"] or "")[:10])
                )
            except (TypeError, ValueError):
                pass
            monthly[month_key] = {
                "income": max(0.0, safe_float(row["income"])),
                "expenses": max(0.0, safe_float(row["expenses"])),
                "movement_count": max(0, int(row["movement_count"] or 0)),
            }

        # An interval without any real financial movement is missing data, not
        # a sequence of zero-income/zero-expense months.
        if not monthly:
            return []
        if start_day.year <= 1900:
            if not first_dates:
                return []
            start_day = max(start_day, min(first_dates))

        result: list[dict[str, Any]] = []
        month_start = start_day.replace(day=1)
        inclusive_end = end_day + timedelta(days=1)
        while month_start <= end_day:
            next_month = add_months(month_start, 1, 1)
            bucket_start = max(start_day, month_start)
            bucket_end = min(inclusive_end, next_month)
            values = monthly.get(month_start.strftime("%Y-%m"), {})
            income = safe_float(values.get("income"))
            expenses = safe_float(values.get("expenses"))
            savings = income - expenses
            result.append(
                {
                    "month": month_start.strftime("%Y-%m"),
                    "label": f"{PT_MONTHS[month_start.month - 1][:3].capitalize()} "
                    f"{month_start.year}",
                    "period_start": iso(bucket_start),
                    "period_end": iso(bucket_end - timedelta(days=1)),
                    "partial": bucket_start != month_start or bucket_end != next_month,
                    "movement_count": int(values.get("movement_count", 0) or 0),
                    "income": income,
                    "expenses": expenses,
                    "savings": savings,
                    "savings_rate": savings / income * 100 if income else 0.0,
                }
            )
            month_start = next_month
        self._monthly_summary_cache[cache_key] = result
        return result
