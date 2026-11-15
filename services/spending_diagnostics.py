"""Explainable spending diagnostics used by the analysis workspace."""

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
from services.forecast_validation import (
    measure_interval_coverage,
    walk_forward_calibration,
)
from utils.config import parse_date
from utils.helpers import PT_MONTHS, add_months, safe_float

if TYPE_CHECKING:
    from database.repo import Repo


WEEKDAYS_PT = ("Seg", "Ter", "Qua", "Qui", "Sex", "Sáb", "Dom")
_EXTERNAL_FLOW_SQL = sql_external_financial_flow("t.category")


class SpendingDiagnosticsService:
    def __init__(self, repo: "Repo") -> None:
        self.repo = repo

    @staticmethod
    def _financial_date_sql() -> str:
        return "COALESCE(NULLIF(m.payment_date,''),t.date)"

    @staticmethod
    def _empty_learning(sample_months: int = 0) -> dict[str, Any]:
        return {
            "enabled": False,
            "sample_months": max(0, int(sample_months)),
            "training_months": max(0, int(sample_months)),
            "required_months": 3,
            "validation_months": 0,
            "minimum_band_months": 3,
            "band_learned": False,
            "accuracy_available": False,
            "correction_factor": 1.0,
            "optimistic_multiplier": 0.8,
            "pessimistic_multiplier": 1.2,
            "accuracy_pct": 0.0,
            "forecast_quality_pct": 0.0,
            "interval_coverage_pct": 0.0,
            "interval_hits": 0,
            "interval_misses": 0,
            "interval_total": 0,
            "accuracy_method": "composite_interval_quality",
            "error_accuracy_pct": 0.0,
            "mean_band_width_pct": 0.0,
            "mean_interval_score_pct": 0.0,
            "calibration_score": 0.0,
            "sharpness_score": 0.0,
            "interval_quality_score": 0.0,
            "mape_pct": 0.0,
            "bias_pct": 0.0,
            "confidence": "initial",
            "confidence_label": "Aprendizagem em validação",
            "method": "walk_forward",
        }

    def _monthly_category_expenses(
        self,
        start: date,
        end_exclusive: date,
    ) -> dict[str, dict[str, float]]:
        """Return split-aware expense totals by month and effective category."""

        rows = self.repo.q(
            f"""
            WITH base AS (
                SELECT t.id,
                       SUBSTR({self._financial_date_sql()},1,7) AS month,
                       t.category,
                       t.amount,
                       CASE WHEN EXISTS (
                           SELECT 1
                           FROM transaction_splits present
                           WHERE present.transaction_id=t.id
                       ) THEN 1 ELSE 0 END AS has_split
                FROM transactions t
                LEFT JOIN transaction_meta m ON m.transaction_id=t.id
                WHERE {self._financial_date_sql()}>=?
                  AND {self._financial_date_sql()}<?
                  AND t.amount<0
                  AND {_EXTERNAL_FLOW_SQL}
                  AND {sql_finite_number("t.amount")}
                  AND {sql_iso_date(self._financial_date_sql())}
            ),
            expanded AS (
                SELECT base.month,
                       COALESCE(NULLIF(TRIM(split.category),''),'Outros') AS category,
                       ABS(split.amount) AS amount
                FROM base
                JOIN transaction_splits split ON split.transaction_id=base.id
                WHERE {sql_finite_number("split.amount", non_negative=True)}
                UNION ALL
                SELECT base.month,
                       COALESCE(NULLIF(TRIM(base.category),''),'Outros') AS category,
                       ABS(base.amount) AS amount
                FROM base
                WHERE base.has_split=0
            )
            SELECT month, category, SUM(amount) AS actual
            FROM expanded
            GROUP BY month, category
            ORDER BY month, category
            """,
            (start.isoformat(), end_exclusive.isoformat()),
        )
        result: dict[str, dict[str, float]] = {}
        for row in rows:
            month = str(row["month"] or "")
            category = str(row["category"] or "Outros")
            if month:
                result.setdefault(month, {})[category] = safe_float(row["actual"])
        return result

    @staticmethod
    def _calendar_months(first: str, last: str) -> list[str]:
        try:
            start = date.fromisoformat(f"{str(first)[:7]}-01")
            end = date.fromisoformat(f"{str(last)[:7]}-01")
        except (TypeError, ValueError):
            return []
        count = (end.year - start.year) * 12 + end.month - start.month + 1
        return [
            add_months(start, index).strftime("%Y-%m") for index in range(max(0, count))
        ]

    @classmethod
    def _monthly_backtest(
        cls,
        actual_by_month: dict[str, float],
        *,
        target_months: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        if not actual_by_month:
            return []
        selected_targets = target_months or set(actual_by_month)
        months = cls._calendar_months(
            min(selected_targets),
            max(selected_targets),
        )
        series = [
            {
                "month": month,
                "actual": safe_float(actual_by_month.get(month)),
            }
            for month in months
        ]
        backtest: list[dict[str, Any]] = []
        for index in range(3, len(series)):
            month = str(series[index]["month"])
            if month not in selected_targets:
                continue
            history = [
                safe_float(row["actual"]) for row in series[max(0, index - 6) : index]
            ]
            if len(history) < 3:
                continue
            weights = list(range(1, len(history) + 1))
            predicted = sum(
                value * weight for value, weight in zip(history, weights)
            ) / sum(weights)
            # ANOM-01: constant history => pstdev 0.0 collapses the band to a
            # zero-width interval (Winkler score degrades to 0). Fall back to
            # a 1.0 scale so the band keeps a minimal sane width.
            spread = statistics.pstdev(history) if len(history) > 1 else 0.0
            if spread < 1e-6:
                spread = 1.0  # ANOM-01
            actual = safe_float(series[index]["actual"])
            error = predicted - actual
            error_pct = (
                abs(error) / actual * 100
                if actual > 0
                else 100.0
                if predicted > 0
                else 0.0
            )
            backtest.append(
                {
                    "month": month,
                    "predicted_raw": predicted,
                    "predicted": predicted,
                    "actual": actual,
                    "low_raw": max(0.0, predicted - 1.28 * spread),
                    "high_raw": predicted + 1.28 * spread,
                    "low": max(0.0, predicted - 1.28 * spread),
                    "high": predicted + 1.28 * spread,
                    "error": error,
                    "error_pct": error_pct,
                    "accuracy_pct": max(0.0, 100.0 - min(100.0, error_pct)),
                }
            )
        return backtest

    def _analyse_backtest(
        self,
        backtest: list[dict[str, Any]],
        *,
        visible_months: int,
    ) -> dict[str, Any]:
        if not backtest:
            return {
                "rows": [],
                "mae": 0.0,
                "rmse": 0.0,
                "mape": 0.0,
                "bias": 0.0,
                "accuracy_pct": 0.0,
                "forecast_quality_pct": 0.0,
                "interval_coverage_pct": 0.0,
                "interval_hits": 0,
                "interval_misses": 0,
                "interval_total": 0,
                "accuracy_method": "composite_interval_quality",
                "error_accuracy_pct": 0.0,
                "mean_band_width_pct": 0.0,
                "mean_interval_score_pct": 0.0,
                "calibration_score": 0.0,
                "sharpness_score": 0.0,
                "interval_quality_score": 0.0,
                "accuracy_kind": "insufficient",
                "sample_months": 0,
                "validation_months": 0,
                "learning": self._empty_learning(),
            }

        learning = walk_forward_calibration(
            backtest,
            prediction_key="predicted_raw",
            actual_key="actual",
            minimum_training_months=3,
            minimum_band_months=3,
        )
        prepared_full = [dict(row) for row in learning.get("rows", [])]
        for row in prepared_full:
            correction = safe_float(
                row.get("validation_correction_factor"),
                1.0,
            )
            row["low"] = safe_float(row.get("low_raw")) * correction
            row["high"] = safe_float(row.get("high_raw")) * correction
            row["error"] = safe_float(row.get("predicted")) - safe_float(
                row.get("actual")
            )

        coverage = measure_interval_coverage(
            prepared_full[-max(3, int(visible_months)) :],
            prefer_validation=True,
        )
        visible = list(coverage["rows"])
        raw_errors = [
            safe_float(row.get("predicted_raw")) - safe_float(row.get("actual"))
            for row in visible
        ]
        raw_mae = (
            sum(abs(value) for value in raw_errors) / len(raw_errors)
            if raw_errors
            else 0.0
        )
        raw_rmse = (
            math.sqrt(sum(value**2 for value in raw_errors) / len(raw_errors))
            if raw_errors
            else 0.0
        )
        raw_percent_errors = [
            (
                abs(
                    safe_float(row.get("predicted_raw")) - safe_float(row.get("actual"))
                )
                / safe_float(row.get("actual"))
                * 100.0
                if safe_float(row.get("actual")) > 0
                else 100.0
                if safe_float(row.get("predicted_raw")) > 0
                else 0.0
            )
            for row in visible
        ]
        raw_mape = (
            sum(raw_percent_errors) / len(raw_percent_errors)
            if raw_percent_errors
            else 0.0
        )
        raw_bias = sum(raw_errors) / len(raw_errors) if raw_errors else 0.0

        validation_rows = [row for row in visible if bool(row.get("is_validation"))]
        evaluated = validation_rows or visible
        errors = [safe_float(row.get("error")) for row in evaluated]
        percent_errors = [safe_float(row.get("error_pct")) for row in evaluated]
        mae = sum(abs(value) for value in errors) / len(errors) if errors else 0.0
        rmse = (
            math.sqrt(sum(value**2 for value in errors) / len(errors))
            if errors
            else 0.0
        )
        bias = sum(errors) / len(errors) if errors else 0.0
        mape = sum(percent_errors) / len(percent_errors) if percent_errors else 0.0
        error_accuracy = max(0.0, 100.0 - min(100.0, mape))
        accuracy = safe_float(coverage.get("accuracy_pct"))
        interval_coverage = safe_float(coverage.get("interval_coverage_pct"))
        accuracy_kind = (
            "forecast_quality_validated" if validation_rows else "forecast_quality_base"
        )
        learning.update(
            {
                "method": "walk_forward",
                "accuracy_pct": accuracy if validation_rows else 0.0,
                "forecast_quality_pct": (accuracy if validation_rows else 0.0),
                "interval_coverage_pct": (
                    interval_coverage if validation_rows else 0.0
                ),
                "interval_hits": (
                    int(coverage.get("interval_hits", 0) or 0) if validation_rows else 0
                ),
                "interval_misses": int(coverage.get("interval_misses", 0) or 0)
                if validation_rows
                else 0,
                "interval_total": int(coverage.get("interval_total", 0) or 0)
                if validation_rows
                else 0,
                "accuracy_available": bool(validation_rows),
                "accuracy_method": "composite_interval_quality",
                "error_accuracy_pct": error_accuracy,
                "mean_band_width_pct": safe_float(coverage.get("mean_band_width_pct")),
                "mean_interval_score_pct": safe_float(
                    coverage.get("mean_interval_score_pct")
                ),
                "calibration_score": safe_float(coverage.get("calibration_score")),
                "sharpness_score": safe_float(coverage.get("sharpness_score")),
                "interval_quality_score": safe_float(
                    coverage.get("interval_quality_score")
                ),
                "mape_pct": mape,
                "bias_pct": bias,
            }
        )
        return {
            "rows": visible,
            "mae": mae,
            "rmse": rmse,
            "mape": mape,
            "bias": bias,
            "accuracy_pct": accuracy,
            "forecast_quality_pct": accuracy,
            "interval_coverage_pct": interval_coverage,
            "interval_hits": int(coverage.get("interval_hits", 0) or 0),
            "interval_misses": int(coverage.get("interval_misses", 0) or 0),
            "interval_total": int(coverage.get("interval_total", 0) or 0),
            "nominal_coverage_pct": coverage["nominal_coverage_pct"],
            "band_method": "normal_approximation_from_historical_spread",
            "accuracy_method": "composite_interval_quality",
            "error_accuracy_pct": error_accuracy,
            "mean_band_width_pct": safe_float(coverage.get("mean_band_width_pct")),
            "mean_interval_score_pct": safe_float(
                coverage.get("mean_interval_score_pct")
            ),
            "calibration_score": safe_float(coverage.get("calibration_score")),
            "sharpness_score": safe_float(coverage.get("sharpness_score")),
            "interval_quality_score": safe_float(
                coverage.get("interval_quality_score")
            ),
            "accuracy_kind": accuracy_kind,
            "sample_months": len(visible),
            "validation_months": len(validation_rows),
            "raw_mae": raw_mae,
            "raw_rmse": raw_rmse,
            "raw_mape": raw_mape,
            "raw_bias": raw_bias,
            "raw_accuracy_pct": max(
                0.0,
                100.0 - min(100.0, raw_mape),
            ),
            "learning": learning,
        }

    @classmethod
    def _category_predictions_for_month(
        cls,
        month: str,
        monthly_categories: dict[str, dict[str, float]],
    ) -> dict[str, float]:
        try:
            target = date.fromisoformat(f"{month}-01")
        except ValueError:
            return {}
        categories = {
            category for values in monthly_categories.values() for category in values
        }
        predictions: dict[str, float] = {}
        history_months = [
            add_months(target, offset).strftime("%Y-%m") for offset in range(-6, 0)
        ]
        for category in categories:
            history = [
                safe_float(monthly_categories.get(key, {}).get(category))
                for key in history_months
            ]
            if len(history) < 3:
                continue
            weights = list(range(1, len(history) + 1))
            predictions[category] = sum(
                value * weight for value, weight in zip(history, weights)
            ) / sum(weights)
        return predictions

    def _attach_error_causes(
        self,
        result: dict[str, Any],
        monthly_categories: dict[str, dict[str, float]],
    ) -> None:
        for row in list(result.get("rows") or []):
            month = str(row.get("month") or "")
            actual_values = dict(monthly_categories.get(month, {}))
            predicted_values = self._category_predictions_for_month(
                month,
                monthly_categories,
            )
            predicted_total = sum(predicted_values.values())
            target_prediction = safe_float(row.get("predicted"))
            scale = target_prediction / predicted_total if predicted_total > 0 else 0.0
            if predicted_total <= 0 and target_prediction > 0:
                predicted_values["Estimativa geral"] = target_prediction
            categories = set(actual_values) | set(predicted_values)
            causes: list[dict[str, Any]] = []
            for category in categories:
                actual = safe_float(actual_values.get(category))
                predicted = safe_float(predicted_values.get(category))
                if predicted_total > 0:
                    predicted *= scale
                delta = actual - predicted
                if abs(delta) < 0.01:
                    continue
                if predicted <= 0 and actual > 0:
                    cause_type = "new"
                    reason = "Sem valor previsto para esta categoria"
                elif actual <= 0 and predicted > 0:
                    cause_type = "stopped"
                    reason = "A despesa esperada não se repetiu"
                elif actual > predicted * 1.5:
                    cause_type = "spike"
                    reason = "Despesa muito acima do padrão recente"
                elif actual < predicted * 0.5:
                    cause_type = "drop"
                    reason = "Despesa muito abaixo do padrão recente"
                else:
                    cause_type = "shift"
                    reason = "Variação face ao padrão recente"
                causes.append(
                    {
                        "category": category,
                        "predicted": predicted,
                        "actual": actual,
                        "delta": delta,
                        "cause_type": cause_type,
                        "reason": reason,
                        "tone": "negative" if delta > 0 else "positive",
                    }
                )
            causes.sort(
                key=lambda cause: abs(safe_float(cause.get("delta"))),
                reverse=True,
            )
            total_absolute = sum(
                abs(safe_float(cause.get("delta"))) for cause in causes
            )
            for cause in causes:
                cause["contribution_pct"] = (
                    abs(safe_float(cause.get("delta"))) / total_absolute * 100.0
                    if total_absolute
                    else 0.0
                )
            actual_delta = safe_float(row.get("actual")) - target_prediction
            row["actual_delta"] = actual_delta
            row["causes"] = causes[:8]
            if not causes:
                row["cause_headline"] = "Sem variação material por categoria."
            else:
                lead = causes[0]
                direction = "acima" if safe_float(lead.get("delta")) > 0 else "abaixo"
                row["cause_headline"] = (
                    f"{lead.get('category')} foi o principal fator, "
                    f"{direction} do valor esperado."
                )

    def forecast_accuracy(
        self,
        *,
        reference_date: Optional[date] = None,
        history_months: int = 30,
        visible_months: int = 12,
    ) -> dict[str, Any]:
        """Back-test monthly expense forecasts using only prior-month data."""

        today = reference_date or date.today()
        current_month = today.replace(day=1)
        start_month = add_months(current_month, -max(8, int(history_months)))
        monthly_categories = self._monthly_category_expenses(
            start_month,
            current_month,
        )
        actual_by_month = {
            month: sum(values.values()) for month, values in monthly_categories.items()
        }
        if not actual_by_month:
            return self._analyse_backtest([], visible_months=visible_months)
        backtest = self._monthly_backtest(actual_by_month)
        result = self._analyse_backtest(
            backtest,
            visible_months=visible_months,
        )
        self._attach_error_causes(result, monthly_categories)
        return result

    def category_forecast_accuracy(
        self,
        *,
        reference_date: Optional[date] = None,
        history_months: int = 30,
        visible_months: int = 12,
    ) -> dict[str, Any]:
        """Measure how predictable each effective expense category is."""

        today = reference_date or date.today()
        current_month = today.replace(day=1)
        start_month = add_months(current_month, -max(8, int(history_months)))
        monthly_categories = self._monthly_category_expenses(
            start_month,
            current_month,
        )
        if not monthly_categories:
            return {"rows": [], "best": {}, "hardest": {}}
        months = self._calendar_months(
            min(monthly_categories),
            max(monthly_categories),
        )
        target_months = set(months)
        categories = sorted(
            {category for values in monthly_categories.values() for category in values},
            key=str.casefold,
        )
        rows: list[dict[str, Any]] = []
        for category in categories:
            actual_by_month = {
                month: safe_float(monthly_categories.get(month, {}).get(category))
                for month in months
            }
            total = sum(actual_by_month.values())
            if total <= 0:
                continue
            backtest = self._monthly_backtest(
                actual_by_month,
                target_months=target_months,
            )
            analysis = self._analyse_backtest(
                backtest,
                visible_months=visible_months,
            )
            evaluated = [
                row
                for row in analysis.get("rows", [])
                if bool(row.get("is_validation"))
            ] or list(analysis.get("rows", []))
            absolute_error = sum(
                abs(safe_float(row.get("predicted")) - safe_float(row.get("actual")))
                for row in evaluated
            )
            actual_total = sum(safe_float(row.get("actual")) for row in evaluated)
            if not evaluated or actual_total <= 0:
                wape = 0.0 if not absolute_error else 100.0
                error_accuracy = (
                    0.0 if not evaluated else (100.0 if not absolute_error else 0.0)
                )
            else:
                wape = absolute_error / actual_total * 100.0
                error_accuracy = max(0.0, 100.0 - min(100.0, wape))

            accuracy = safe_float(analysis.get("accuracy_pct"))
            interval_coverage = safe_float(analysis.get("interval_coverage_pct"))
            interval_total = int(analysis.get("interval_total", len(evaluated)) or 0)
            active_months = sum(
                1
                for month in months[-max(3, int(visible_months)) :]
                if safe_float(actual_by_month.get(month)) > 0
            )
            visible_count = min(
                len(months),
                max(3, int(visible_months)),
            )
            regularity = active_months / visible_count * 100.0 if visible_count else 0.0
            if interval_total < 3:
                status = "Dados insuficientes"
                tone = "neutral"
            elif accuracy >= 80 and error_accuracy >= 70:
                status = "Previsível"
                tone = "positive"
            elif accuracy >= 60 and error_accuracy >= 40:
                status = "Moderada"
                tone = "warning"
            else:
                status = "Volátil"
                tone = "negative"
            errors = [
                safe_float(row.get("predicted")) - safe_float(row.get("actual"))
                for row in evaluated
            ]
            rows.append(
                {
                    "category": category,
                    "accuracy_pct": accuracy,
                    "forecast_quality_pct": accuracy,
                    "interval_coverage_pct": interval_coverage,
                    "interval_hits": int(analysis.get("interval_hits", 0) or 0),
                    "interval_misses": int(analysis.get("interval_misses", 0) or 0),
                    "interval_total": interval_total,
                    "accuracy_method": "composite_interval_quality",
                    "error_accuracy_pct": error_accuracy,
                    "mean_band_width_pct": safe_float(
                        analysis.get("mean_band_width_pct")
                    ),
                    "mean_interval_score_pct": safe_float(
                        analysis.get("mean_interval_score_pct")
                    ),
                    "calibration_score": safe_float(analysis.get("calibration_score")),
                    "sharpness_score": safe_float(analysis.get("sharpness_score")),
                    "interval_quality_score": safe_float(
                        analysis.get("interval_quality_score")
                    ),
                    "wape_pct": wape,
                    "mae": (
                        sum(abs(value) for value in errors) / len(errors)
                        if errors
                        else 0.0
                    ),
                    "bias": (sum(errors) / len(errors) if errors else 0.0),
                    "regularity_pct": regularity,
                    "active_months": active_months,
                    "sample_months": len(evaluated),
                    "validation_months": sum(
                        1 for row in evaluated if bool(row.get("is_validation"))
                    ),
                    "total": total,
                    "status": status,
                    "tone": tone,
                }
            )
        rows.sort(
            key=lambda row: (
                -safe_float(row.get("total")),
                str(row.get("category") or "").casefold(),
            )
        )
        qualified = [row for row in rows if int(row.get("sample_months", 0) or 0) >= 3]
        return {
            "rows": rows,
            "best": (
                dict(
                    max(
                        qualified,
                        key=lambda row: (
                            safe_float(row["accuracy_pct"]),
                            safe_float(row["error_accuracy_pct"]),
                        ),
                    )
                )
                if qualified
                else {}
            ),
            "hardest": (
                dict(
                    min(
                        qualified,
                        key=lambda row: (
                            safe_float(row["accuracy_pct"]),
                            safe_float(row["error_accuracy_pct"]),
                        ),
                    )
                )
                if qualified
                else {}
            ),
            "period_start": start_month.isoformat(),
            "period_end": (current_month - timedelta(days=1)).isoformat(),
        }

    def spending_seasonality(
        self,
        start: str,
        end: str,
        *,
        category: str = "",
    ) -> dict[str, Any]:
        """Return a month-by-weekday average that includes zero-spend days."""

        start_date = parse_date(str(start)[:10])
        end_date = parse_date(str(end)[:10])
        if end_date < start_date:
            start_date, end_date = end_date, start_date
        params: list[Any] = [start_date.isoformat(), end_date.isoformat()]
        category_clause = ""
        if category:
            category_clause = " AND t.category=?"
            params.append(str(category))
        rows = self.repo.q(
            f"""
            SELECT {self._financial_date_sql()} AS financial_date,
                   ABS(t.amount) AS amount
            FROM transactions t
            LEFT JOIN transaction_meta m ON m.transaction_id=t.id
            WHERE {self._financial_date_sql()}>=?
              AND {self._financial_date_sql()}<=?
              AND t.amount<0
              AND {_EXTERNAL_FLOW_SQL}
              AND {sql_finite_number("t.amount")}
              AND {sql_iso_date(self._financial_date_sql())}
              {category_clause}
            """,
            tuple(params),
        )

        totals: dict[tuple[int, int], float] = {}
        movements: dict[tuple[int, int], int] = {}
        for row in rows:
            try:
                movement_day = parse_date(str(row["financial_date"])[:10])
            except (TypeError, ValueError):
                continue
            key = (movement_day.weekday(), movement_day.month)
            totals[key] = totals.get(key, 0.0) + safe_float(row["amount"])
            movements[key] = movements.get(key, 0) + 1

        day_counts: dict[tuple[int, int], int] = {}
        cursor = start_date
        while cursor <= end_date:
            key = (cursor.weekday(), cursor.month)
            day_counts[key] = day_counts.get(key, 0) + 1
            cursor += timedelta(days=1)

        cells: list[dict[str, Any]] = []
        for weekday in range(7):
            for month in range(1, 13):
                key = (weekday, month)
                days = day_counts.get(key, 0)
                total = totals.get(key, 0.0)
                cells.append(
                    {
                        "weekday": weekday,
                        "weekday_label": WEEKDAYS_PT[weekday],
                        "month": month,
                        "month_label": PT_MONTHS[month - 1],
                        "days": days,
                        "total": total,
                        "movements": movements.get(key, 0),
                        "value": total / days if days else 0.0,
                    }
                )
        active = [cell for cell in cells if safe_float(cell["value"]) > 0]
        peak = (
            max(active, key=lambda cell: safe_float(cell["value"])) if active else None
        )
        return {
            "cells": cells,
            "peak": peak,
            "maximum": max(
                (safe_float(cell["value"]) for cell in active),
                default=0.0,
            ),
            "total": sum(totals.values()),
            "category": category,
            "period_start": start_date.isoformat(),
            "period_end": end_date.isoformat(),
        }

    def merchant_pareto(
        self,
        start: str,
        end: str,
        *,
        limit: int = 12,
    ) -> dict[str, Any]:
        """Rank merchants and calculate their cumulative share of spending."""

        merchant_sql = (
            "COALESCE(NULLIF(TRIM(m.merchant),''),"
            "NULLIF(TRIM(t.description),''),'Sem comerciante')"
        )
        raw = self.repo.q(
            f"""
            SELECT {merchant_sql} AS merchant,
                   SUM(ABS(t.amount)) AS total,
                   COUNT(*) AS movements
            FROM transactions t
            LEFT JOIN transaction_meta m ON m.transaction_id=t.id
            WHERE {self._financial_date_sql()}>=?
              AND {self._financial_date_sql()}<=?
              AND t.amount<0
              AND {_EXTERNAL_FLOW_SQL}
              AND {sql_finite_number("t.amount")}
              AND {sql_iso_date(self._financial_date_sql())}
            GROUP BY merchant
            HAVING SUM(ABS(t.amount))>0
            ORDER BY total DESC, merchant
            """,
            (str(start)[:10], str(end)[:10]),
        )
        all_rows = [
            {
                "merchant": str(row["merchant"] or "Sem comerciante"),
                "total": safe_float(row["total"]),
                "movements": int(row["movements"] or 0),
            }
            for row in raw
        ]
        grand_total = sum(safe_float(row["total"]) for row in all_rows)
        row_limit = max(4, min(20, int(limit)))
        visible = all_rows[:row_limit]
        remainder = all_rows[row_limit:]
        if remainder:
            visible.append(
                {
                    "merchant": f"Outros ({len(remainder)})",
                    "total": sum(safe_float(row["total"]) for row in remainder),
                    "movements": sum(int(row["movements"]) for row in remainder),
                }
            )
        cumulative = 0.0
        for row in visible:
            cumulative += safe_float(row["total"])
            row["share_pct"] = (
                safe_float(row["total"]) / grand_total * 100 if grand_total else 0.0
            )
            row["cumulative_pct"] = (
                min(100.0, max(0.0, cumulative / grand_total * 100.0))
                if grand_total
                else 0.0
            )

        # Concentration counts actual merchants, never the presentation bucket.
        concentration_80 = 0
        full_cumulative = 0.0
        for index, row in enumerate(all_rows):
            full_cumulative += row["total"]
            if grand_total > 0 and full_cumulative >= grand_total * 0.8:
                concentration_80 = index + 1
                break
        return {
            "rows": visible,
            "total": grand_total,
            "merchants_count": len(all_rows),
            "concentration_80_count": concentration_80,
            "concentration_80_method": "full_merchant_population",
        }
