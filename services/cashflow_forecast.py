"""Cash-calendar event detection and scenario forecasting."""

from __future__ import annotations

import functools
import re
import unicodedata
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any, Optional

from database.validation import (
    sql_external_financial_flow,
    sql_finite_number,
    sql_iso_date,
)
from services.forecast_validation import walk_forward_calibration
from services.payments import next_business_day
from utils.config import parse_date
from utils.helpers import (
    add_months,
    iso,
    parse_flexible_date,
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


@functools.lru_cache(maxsize=4096)
def _normalise_description_cached(raw_str: str) -> str:
    text = unicodedata.normalize(
        "NFKD",
        raw_str.casefold().strip(),
    )
    text = "".join(
        character for character in text if not unicodedata.combining(character)
    )
    for suffix in ("(subscrição)", "(subscricao)"):
        text = text.replace(suffix, "")
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


class CashflowForecastService:
    def __init__(self, repo: "Repo", payments_service: "PaymentsService") -> None:
        self.repo = repo
        self.payments_service = payments_service

    @staticmethod
    def _normalised_description(value: Any) -> str:
        return _normalise_description_cached(str(value or ""))

    @staticmethod
    def _amounts_match(
        first: Any,
        second: Any,
        *,
        relative_tolerance: float = 0.025,
        absolute_tolerance: float = 0.05,
    ) -> bool:
        left = abs(safe_float(first))
        right = abs(safe_float(second))
        if left <= 0 or right <= 0:
            return False
        tolerance = max(
            float(absolute_tolerance),
            max(left, right) * max(0.0, float(relative_tolerance)),
        )
        return abs(left - right) <= tolerance

    @classmethod
    def _description_matches(
        cls,
        description: Any,
        expected_name: Any,
        *,
        allow_phrase: bool,
    ) -> bool:
        description_key = cls._normalised_description(description)
        expected_key = cls._normalised_description(expected_name)
        if not description_key or not expected_key:
            return False
        if description_key == expected_key:
            return True
        if not allow_phrase:
            return False
        # Word boundaries prevent short/generic names from matching an
        # unrelated word (for example "Bar" inside "Barbearia").
        if len(expected_key) < 5:
            return False
        return f" {expected_key} " in f" {description_key} "

    @classmethod
    def _event_matches_signature(
        cls,
        *,
        description: Any,
        amount: Any,
        category: Any,
        signature: dict[str, Any],
    ) -> bool:
        category_key = cls._normalised_description(category)
        signature_category = cls._normalised_description(signature.get("category"))
        if category_key and signature_category and category_key != signature_category:
            return False
        exact_name = cls._description_matches(
            description,
            signature.get("name"),
            allow_phrase=False,
        )
        if exact_name:
            return cls._amounts_match(
                amount,
                signature.get("amount"),
                relative_tolerance=0.35,
                absolute_tolerance=1.0,
            )
        return cls._description_matches(
            description,
            signature.get("name"),
            allow_phrase=True,
        ) and cls._amounts_match(amount, signature.get("amount"))

    def _recurring_income_events(
        self,
        reference: date,
        end_day: date,
    ) -> list[dict[str, Any]]:
        start = reference - timedelta(days=210)
        rows = self.repo.q(
            f"""
            SELECT COALESCE(NULLIF(m.payment_date,''),t.date) AS date,
                   t.description, t.category, t.amount
            FROM transactions t
            LEFT JOIN transaction_meta m ON m.transaction_id=t.id
            WHERE COALESCE(NULLIF(m.payment_date,''),t.date)>=?
              AND COALESCE(NULLIF(m.payment_date,''),t.date)<=?
              AND t.amount>0 AND {_EXTERNAL_FLOW_SQL}
              AND {_VALID_TRANSACTION_AMOUNT_SQL}
              AND {_VALID_FINANCIAL_DATE_SQL}
            ORDER BY date
            """,
            (iso(start), iso(reference)),
        )
        groups: dict[str, list[Any]] = {}
        for row in rows:
            key = self._normalised_description(row_get(row, "description"))
            if not key:
                key = self._normalised_description(row_get(row, "category"))
            if key:
                groups.setdefault(key, []).append(row)

        events: list[dict[str, Any]] = []
        for grouped_rows in groups.values():
            dates = sorted(
                parsed
                for parsed in (
                    parse_flexible_date(row_get(row, "date")) for row in grouped_rows
                )
                if parsed is not None
            )
            distinct_months = {(value.year, value.month) for value in dates}
            if len(dates) < 2 or len(distinct_months) < 2:
                continue

            amounts = [safe_float(row_get(row, "amount")) for row in grouped_rows]
            average = sum(amounts) / len(amounts)
            if average <= 0 or (max(amounts) - min(amounts)) / average > 0.35:
                continue

            month_indexes = sorted(year * 12 + month for year, month in distinct_months)
            month_gaps = [
                right - left for left, right in zip(month_indexes, month_indexes[1:])
            ]
            if month_gaps and sorted(month_gaps)[len(month_gaps) // 2] > 2:
                continue

            day_values = sorted(value.day for value in dates)
            typical_day = day_values[len(day_values) // 2]
            last_date = dates[-1]
            candidate = add_months(last_date, 1, typical_day)
            while candidate <= reference:
                candidate = add_months(candidate, 1, typical_day)

            latest = grouped_rows[-1]
            name = str(
                row_get(latest, "description", "Receita recorrente")
                or "Receita recorrente"
            )
            category = str(row_get(latest, "category", "Rendimentos") or "Rendimentos")
            while candidate <= end_day:
                events.append(
                    {
                        "date": iso(candidate),
                        "name": name,
                        "amount": average,
                        "source": "recurring_income",
                        "kind": "income",
                        "category": category,
                        "recorded": False,
                        "overdue": False,
                    }
                )
                candidate = add_months(candidate, 1, typical_day)
        return events

    def _subscription_forecast_events(
        self,
        reference: date,
        end_day: date,
    ) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        existing_occurrences = {
            (int(r["subscription_id"]), str(r["due_date"])[:10])
            for r in self.repo.q(
                "SELECT subscription_id, due_date FROM subscription_occurrences "
                "WHERE due_date >= ? AND due_date <= ?",
                (iso(reference), iso(end_day)),
            )
            if r["subscription_id"] is not None and r["due_date"]
        }
        for row in self.repo.subscriptions(active_only=True):
            subscription_id = int(row_get(row, "id", 0) or 0)
            candidate = self.payments_service.next_subscription_due_date(
                row,
                reference + timedelta(days=1),
            )

            while candidate <= end_day:
                candidate_iso = iso(candidate)
                if (subscription_id, candidate_iso) not in existing_occurrences:
                    events.append(
                        {
                            "date": candidate_iso,
                            "name": str(
                                row_get(row, "name", "Subscrição") or "Subscrição"
                            ),
                            "amount": -abs(safe_float(row_get(row, "amount"))),
                            "source": "subscription",
                            "kind": "expense",
                            "category": str(
                                row_get(row, "category", "Subscrições") or "Subscrições"
                            ),
                            "recorded": False,
                            "overdue": False,
                            "source_id": subscription_id,
                        }
                    )
                candidate = self.payments_service.advance_subscription_due(
                    row,
                    candidate,
                )
        return events

    def _scheduled_forecast_events(
        self,
        reference: date,
        end_day: date,
    ) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        tomorrow = reference + timedelta(days=1)
        existing_occurrences = {
            (int(r["scheduled_id"]), str(r["due_date"])[:10])
            for r in self.repo.q(
                "SELECT scheduled_id, due_date FROM scheduled_payment_occurrences "
                "WHERE due_date <= ?",
                (iso(end_day),),
            )
            if r["scheduled_id"] is not None and r["due_date"]
        }
        for row in self.repo.scheduled_payments():
            if int(row_get(row, "active", 1) or 0) != 1:
                continue
            total = max(0, int(row_get(row, "months_total", 1) or 0))
            index = max(0, int(row_get(row, "months_paid", 0) or 0))
            for _ in range(240):
                if total > 0 and index >= total:
                    break
                anchor = self.payments_service.due_date_for_payment(row, index)
                due = self.payments_service.effective_due_date_for_payment(row, index)
                if due > end_day:
                    break
                scheduled_id = int(row_get(row, "id", 0) or 0)
                due_iso = iso(due)
                if not any(
                    (scheduled_id, value) in existing_occurrences
                    for value in (due_iso, iso(anchor))
                ):
                    overdue = due <= reference
                    forecast_day = next_business_day(tomorrow) if overdue else due
                    if forecast_day > end_day:
                        index += 1
                        continue
                    events.append(
                        {
                            "date": iso(forecast_day),
                            "due_date": due_iso,
                            "anchor_date": iso(anchor),
                            "name": str(
                                row_get(row, "name", "Pagamento") or "Pagamento"
                            ),
                            "amount": -abs(safe_float(row_get(row, "amount"))),
                            "source": "scheduled",
                            "kind": "expense",
                            "category": str(
                                row_get(row, "category", "Outros") or "Outros"
                            ),
                            "recorded": False,
                            "overdue": overdue,
                            "source_id": scheduled_id,
                        }
                    )
                index += 1
        return events

    def _recorded_future_events(
        self,
        reference: date,
        end_day: date,
    ) -> list[dict[str, Any]]:
        rows = self.repo.q(
            f"""
            SELECT t.id, COALESCE(NULLIF(m.payment_date,''),t.date) AS date,
                   t.description, t.category, t.amount,
                   (
                       SELECT o.subscription_id
                       FROM subscription_occurrences o
                       WHERE o.transaction_id=t.id
                       ORDER BY o.id DESC
                       LIMIT 1
                   ) AS subscription_id,
                   (
                       SELECT o.due_date
                       FROM subscription_occurrences o
                       WHERE o.transaction_id=t.id
                       ORDER BY o.id DESC
                       LIMIT 1
                   ) AS subscription_due_date,
                   (
                       SELECT o.scheduled_id
                       FROM scheduled_payment_occurrences o
                       WHERE o.transaction_id=t.id
                       ORDER BY o.id DESC
                       LIMIT 1
                   ) AS scheduled_id,
                   (
                       SELECT o.due_date
                       FROM scheduled_payment_occurrences o
                       WHERE o.transaction_id=t.id
                       ORDER BY o.id DESC
                       LIMIT 1
                   ) AS scheduled_due_date
            FROM transactions t
            LEFT JOIN transaction_meta m ON m.transaction_id=t.id
            WHERE COALESCE(NULLIF(m.payment_date,''),t.date)>?
              AND COALESCE(NULLIF(m.payment_date,''),t.date)<=?
              AND t.amount!=0 AND {_EXTERNAL_FLOW_SQL}
              AND {_VALID_TRANSACTION_AMOUNT_SQL}
              AND {_VALID_FINANCIAL_DATE_SQL}
            ORDER BY date, t.id
            """,
            (iso(reference), iso(end_day)),
        )
        return [
            {
                "date": str(row_get(row, "date"))[:10],
                "name": str(
                    row_get(row, "description", "Movimento futuro")
                    or "Movimento futuro"
                ),
                "amount": safe_float(row_get(row, "amount")),
                "source": "recorded",
                "kind": "income"
                if safe_float(row_get(row, "amount")) > 0
                else "expense",
                "category": str(row_get(row, "category", "Outros") or "Outros"),
                "recorded": True,
                "overdue": False,
                "source_id": int(row_get(row, "id", 0) or 0),
                "subscription_id": (
                    int(row_get(row, "subscription_id", 0) or 0) or None
                ),
                "scheduled_id": (int(row_get(row, "scheduled_id", 0) or 0) or None),
                "linked_due_date": str(
                    row_get(row, "subscription_due_date")
                    or row_get(row, "scheduled_due_date")
                    or ""
                )[:10],
            }
            for row in rows
        ]

    def _explicit_spend_identifiers(
        self,
    ) -> tuple[list[dict[str, Any]], set[int]]:
        """Return safe signatures and exact links excluded from the baseline."""

        signatures = [
            {
                "name": str(row_get(row, "name") or ""),
                "amount": abs(safe_float(row_get(row, "amount"))),
                "category": str(row_get(row, "category") or ""),
            }
            for row in (
                list(self.repo.subscriptions(active_only=False))
                + list(self.repo.scheduled_payments())
            )
            if self._normalised_description(row_get(row, "name"))
            and abs(safe_float(row_get(row, "amount"))) > 0
        ]
        occurrence_ids = {
            int(row_get(row, "transaction_id", 0) or 0)
            for row in self.repo.q(
                """
                SELECT transaction_id
                FROM scheduled_payment_occurrences
                WHERE transaction_id IS NOT NULL
                UNION
                SELECT transaction_id
                FROM subscription_occurrences
                WHERE transaction_id IS NOT NULL
                """
            )
        }
        return signatures, occurrence_ids

    def _variable_spend_by_day(
        self,
        start: date,
        end: date,
    ) -> dict[date, float]:
        """Load variable expenses once so calibration does not repeat SQL queries."""

        rows = self.repo.q(
            f"""
            SELECT t.id, COALESCE(NULLIF(m.payment_date,''),t.date) AS date,
                   t.description, t.category, t.amount
            FROM transactions t
            LEFT JOIN transaction_meta m ON m.transaction_id=t.id
            WHERE COALESCE(NULLIF(m.payment_date,''),t.date)>=?
              AND COALESCE(NULLIF(m.payment_date,''),t.date)<=?
              AND t.amount<0 AND {_EXTERNAL_FLOW_SQL}
              AND {_VALID_TRANSACTION_AMOUNT_SQL}
              AND {_VALID_FINANCIAL_DATE_SQL}
            """,
            (iso(start), iso(end)),
        )
        signatures, occurrence_ids = self._explicit_spend_identifiers()
        exact_buckets: dict[str, list[dict[str, Any]]] = {}
        phrase_signatures: list[dict[str, Any]] = []
        for sig in signatures:
            norm_name = self._normalised_description(sig.get("name"))
            if norm_name:
                exact_buckets.setdefault(norm_name, []).append(sig)
                if len(norm_name) >= 5:
                    phrase_signatures.append(sig)

        daily: dict[date, float] = {}
        for row in rows:
            movement_date = parse_flexible_date(row_get(row, "date"))
            if movement_date is None:
                continue
            row_id = int(row_get(row, "id", 0) or 0)
            if row_id in occurrence_ids:
                continue

            desc = row_get(row, "description")
            amt = row_get(row, "amount")
            cat = row_get(row, "category")
            norm_desc = self._normalised_description(desc)

            matched = False
            if norm_desc in exact_buckets:
                for sig in exact_buckets[norm_desc]:
                    if self._event_matches_signature(
                        description=desc,
                        amount=amt,
                        category=cat,
                        signature=sig,
                    ):
                        matched = True
                        break

            if not matched and phrase_signatures:
                matched = any(
                    self._event_matches_signature(
                        description=desc,
                        amount=amt,
                        category=cat,
                        signature=sig,
                    )
                    for sig in phrase_signatures
                )

            if not matched:
                daily[movement_date] = daily.get(movement_date, 0.0) + abs(
                    safe_float(amt)
                )
        return daily

    def _determine_coverage_start(
        self,
        first_tx_date: date | None = None,
    ) -> date | None:
        """Determine observation coverage start independently of expense occurrence."""
        if first_tx_date is not None:
            return first_tx_date
        try:
            rows = self.repo.q(
                f"""
                SELECT MIN(d) AS min_d FROM (
                    SELECT MIN({_FINANCIAL_DATE_SQL}) AS d
                    FROM transactions t
                    LEFT JOIN transaction_meta m ON m.transaction_id=t.id
                    WHERE t.date IS NOT NULL AND t.date != ''
                      AND {_VALID_FINANCIAL_DATE_SQL}
                    UNION ALL
                    SELECT MIN(reference_date) AS d
                    FROM financial_accounts
                    WHERE reference_date IS NOT NULL AND reference_date != ''
                )
                """
            )
            if rows and row_get(rows[0], "min_d"):
                parsed = parse_flexible_date(row_get(rows[0], "min_d"))
                if parsed is not None:
                    return parsed
        except Exception:
            pass
        return None

    def _variable_daily_spend(
        self,
        reference: date,
        lookback_days: int = 90,
        first_tx_date: date | None = None,
        last_tx_date: date | None = None,
    ) -> float:
        if lookback_days is not None and int(lookback_days) <= 0:
            days = 1
        else:
            days = max(1, int(lookback_days or 90))
        start = reference - timedelta(days=days - 1)
        daily = self._variable_spend_by_day(start, reference)

        coverage_start = self._determine_coverage_start(first_tx_date)
        if coverage_start is None:
            coverage_start = min(daily.keys()) if daily else start

        obs_start = max(start, coverage_start)
        obs_end = (
            min(reference, last_tx_date) if last_tx_date is not None else reference
        )
        if obs_end < obs_start:
            history_days = 1
        else:
            history_days = max(1, min(days, (obs_end - obs_start).days + 1))

        if not daily:
            return 0.0
        total_expenses = sum(daily.values())
        return total_expenses / float(history_days)

    def forecast_calibration(
        self,
        reference: date,
        *,
        sample_months: int = 12,
        lookback_days: int = 90,
    ) -> dict[str, Any]:
        """Back-test the variable-spend forecast using only completed months.

        Each historical month is predicted from the days that preceded it.
        Recent errors receive more weight, allowing the current baseline and
        scenario band to adapt without storing or modifying financial records.
        """

        months = max(3, min(24, int(sample_months or 12)))
        history_days = max(30, min(365, int(lookback_days or 90)))
        current_month = reference.replace(day=1)
        first_target = add_months(current_month, -months)
        series_start = first_target - timedelta(days=history_days)
        last_complete_day = current_month - timedelta(days=1)
        daily = self._variable_spend_by_day(series_start, last_complete_day)

        total_days = max(0, (last_complete_day - series_start).days + 1)
        prefix_sum = [0.0] * (total_days + 1)
        running = 0.0
        for i in range(total_days):
            d = series_start + timedelta(days=i)
            running += daily.get(d, 0.0)
            prefix_sum[i + 1] = running

        def range_sum(start_d: date, end_d: date) -> float:
            if end_d < start_d or start_d > last_complete_day or end_d < series_start:
                return 0.0
            s_idx = max(0, min(total_days, (start_d - series_start).days))
            e_idx = max(0, min(total_days, (end_d - series_start).days + 1))
            if e_idx <= s_idx:
                return 0.0
            return prefix_sum[e_idx] - prefix_sum[s_idx]

        coverage_start = self._determine_coverage_start()
        rows: list[dict[str, Any]] = []
        for index in range(months):
            target_start = add_months(first_target, index)
            target_end = add_months(target_start, 1) - timedelta(days=1)
            cutoff = target_start - timedelta(days=1)
            history_start = cutoff - timedelta(days=history_days - 1)

            if coverage_start is not None and coverage_start > cutoff:
                continue
            effective_start = (
                max(history_start, coverage_start)
                if coverage_start
                else history_start
            )
            effective_days = max(
                1, min(history_days, (cutoff - effective_start).days + 1)
            )

            predicted_daily = range_sum(effective_start, cutoff) / float(effective_days)
            predicted = predicted_daily * ((target_end - target_start).days + 1)
            actual = range_sum(target_start, target_end)
            if predicted <= 0 or actual <= 0:
                continue
            raw_ratio = actual / predicted
            rows.append(
                {
                    "month": target_start.strftime("%Y-%m"),
                    "predicted_raw": predicted,
                    "actual": actual,
                    "raw_ratio": raw_ratio,
                    "raw_error_pct": abs(predicted - actual) / actual * 100.0,
                }
            )

        result = walk_forward_calibration(
            rows,
            prediction_key="predicted_raw",
            actual_key="actual",
            minimum_training_months=3,
            minimum_band_months=3,
        )
        result["last_evaluated_month"] = rows[-1]["month"] if rows else ""
        result["method"] = "walk_forward"
        return result

    @classmethod
    def _same_forecast_event(
        cls,
        generated: dict[str, Any],
        recorded: dict[str, Any],
    ) -> bool:
        """Match a generated event to a real one without broad substrings."""

        source = str(generated.get("source") or "")
        source_id = int(generated.get("source_id", 0) or 0)
        recorded_match_date = str(
            recorded.get("linked_due_date") or recorded.get("date") or ""
        )
        if str(generated.get("date") or "") != recorded_match_date:
            return False
        if str(generated.get("kind") or "") != str(recorded.get("kind") or ""):
            return False
        if (
            source == "subscription"
            and source_id
            and int(recorded.get("subscription_id", 0) or 0) == source_id
        ):
            return True
        if (
            source == "scheduled"
            and source_id
            and int(recorded.get("scheduled_id", 0) or 0) == source_id
        ):
            return True
        if not cls._amounts_match(
            generated.get("amount"),
            recorded.get("amount"),
        ):
            return False
        return cls._description_matches(
            recorded.get("name"),
            generated.get("name"),
            allow_phrase=True,
        )

    def cashflow_calendar_forecast(
        self,
        horizon_days: int = 90,
        as_of: Optional[date] = None,
    ) -> dict[str, Any]:
        """Build a read-only 30/60/90-day cash calendar and balance path."""
        reference = as_of or date.today()
        horizon = max(1, min(365, int(horizon_days or 90)))
        end_day = reference + timedelta(days=horizon)
        # Keep day zero identical to the Dashboard and patrimony definition,
        # including confirmed account reference balances and goal transfers.
        starting_balance = safe_float(self.repo.cash_balance(iso(reference)))

        recorded = self._recorded_future_events(reference, end_day)
        generated = (
            self._recurring_income_events(reference, end_day)
            + self._subscription_forecast_events(reference, end_day)
            + self._scheduled_forecast_events(reference, end_day)
        )
        recorded_keys: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for event in recorded:
            key = (str(event["date"]), str(event["kind"]))
            recorded_keys.setdefault(key, []).append(event)

        events = list(recorded)
        for event in generated:
            key = (str(event["date"]), str(event["kind"]))
            duplicates = recorded_keys.get(key, [])
            if any(
                self._same_forecast_event(event, existing) for existing in duplicates
            ):
                continue
            events.append(event)
        events.sort(
            key=lambda event: (str(event["date"]), -safe_float(event["amount"]))
        )

        coverage_start = self._determine_coverage_start()
        raw_variable_daily = self._variable_daily_spend(reference)
        calibration = self.forecast_calibration(reference)
        correction_factor = safe_float(
            calibration.get("correction_factor"),
            1.0,
        )
        variable_daily = raw_variable_daily * correction_factor
        optimistic_multiplier = safe_float(
            calibration.get("optimistic_multiplier"),
            0.8,
        )
        pessimistic_multiplier = safe_float(
            calibration.get("pessimistic_multiplier"),
            1.2,
        )
        events_by_date: dict[str, list[dict[str, Any]]] = {}
        for event in events:
            events_by_date.setdefault(str(event["date"]), []).append(event)

        points: list[dict[str, Any]] = [
            {
                "date": iso(reference),
                "balance": starting_balance,
                "balance_base": starting_balance,
                "balance_optimistic": starting_balance,
                "balance_pessimistic": starting_balance,
                "income": 0.0,
                "expense": 0.0,
                "baseline_expense": 0.0,
                "event_count": 0,
            }
        ]
        balance = starting_balance
        optimistic_balance = starting_balance
        pessimistic_balance = starting_balance
        for offset in range(1, horizon + 1):
            current = reference + timedelta(days=offset)
            day_events = events_by_date.get(iso(current), [])
            income = sum(
                safe_float(event.get("amount"))
                for event in day_events
                if safe_float(event.get("amount")) > 0
            )
            explicit_expense = sum(
                abs(safe_float(event.get("amount")))
                for event in day_events
                if safe_float(event.get("amount")) < 0
            )
            balance += income - explicit_expense - variable_daily
            optimistic_balance += (
                income - explicit_expense - variable_daily * optimistic_multiplier
            )
            pessimistic_balance += (
                income - explicit_expense - variable_daily * pessimistic_multiplier
            )
            points.append(
                {
                    "date": iso(current),
                    "balance": balance,
                    "balance_base": balance,
                    "balance_optimistic": optimistic_balance,
                    "balance_pessimistic": pessimistic_balance,
                    "income": income,
                    "expense": explicit_expense + variable_daily,
                    "baseline_expense": variable_daily,
                    "event_count": len(day_events),
                }
            )

        def summary(days: int) -> dict[str, Any]:
            used_days = max(1, min(horizon, int(days)))
            subset = points[: used_days + 1]
            cutoff = reference + timedelta(days=used_days)
            used_events = [
                event
                for event in events
                if parse_date(str(event["date"])[:10]) <= cutoff
            ]
            minimum = min(subset, key=lambda point: safe_float(point["balance"]))
            risk_point = next(
                (point for point in subset if safe_float(point.get("balance")) < 0),
                None,
            )
            expected_income = sum(
                safe_float(event.get("amount"))
                for event in used_events
                if safe_float(event.get("amount")) > 0
            )
            commitments = sum(
                abs(safe_float(event.get("amount")))
                for event in used_events
                if safe_float(event.get("amount")) < 0
            )
            baseline_total = variable_daily * used_days
            ending = safe_float(subset[-1]["balance"])
            ending_optimistic = safe_float(subset[-1]["balance_optimistic"])
            ending_pessimistic = safe_float(subset[-1]["balance_pessimistic"])
            minimum_balance = safe_float(minimum["balance"])
            # BUG-CASH-02: zero/negative balance without any projected inflow
            # is a liquidity-critical state, not "stable". The pessimistic
            # scenario (stress band) gates the stable verdict so a fragile
            # base path is never labelled positive.
            pessimistic_ending = safe_float(subset[-1]["balance_pessimistic"])
            pessimistic_negative = any(
                safe_float(point.get("balance_pessimistic")) < 0 for point in subset
            )
            if risk_point is not None or starting_balance < 0:
                status = "Saldo negativo previsto"
                tone = "negative"
            elif starting_balance <= 0 and expected_income <= 0:
                status = "Sem margem de tesouraria"
                tone = "negative"
            elif starting_balance > 0 and (
                minimum_balance / starting_balance < 0.2
                or ending / starting_balance < 0.4
                or pessimistic_negative
                or pessimistic_ending < 0
            ):
                status = "Margem de segurança reduzida"
                tone = "warning"
            else:
                status = "Tesouraria estável"
                tone = "positive"
            return {
                "days": used_days,
                "ending_balance": ending,
                "ending_balance_base": ending,
                "ending_balance_optimistic": ending_optimistic,
                "ending_balance_pessimistic": ending_pessimistic,
                "minimum_balance": minimum_balance,
                "minimum_date": str(minimum["date"]),
                "expected_income": expected_income,
                "known_commitments": commitments,
                "baseline_expenses": baseline_total,
                "total_outflow": commitments + baseline_total,
                "event_count": len(used_events),
                "overdue_count": sum(
                    1 for event in used_events if bool(event.get("overdue"))
                ),
                "risk_date": str(risk_point["date"]) if risk_point else "",
                "buffer_days": (
                    (parse_date(str(risk_point["date"])) - reference).days
                    if risk_point
                    else used_days
                ),
                "status": status,
                "tone": tone,
            }

        summary_days = sorted({min(horizon, days) for days in (30, 60, 90, horizon)})
        summaries = {str(days): summary(days) for days in summary_days}
        return {
            "as_of": iso(reference),
            "horizon_days": horizon,
            "starting_balance": starting_balance,
            "raw_variable_daily_expense": raw_variable_daily,
            "variable_daily_expense": variable_daily,
            "observation_days": (
                max(
                    1,
                    min(
                        90,
                        (
                            reference
                            - max(reference - timedelta(days=89), coverage_start)
                        ).days
                        + 1,
                    ),
                )
                if coverage_start
                else 0
            ),
            "coverage_start": iso(coverage_start) if coverage_start else "",
            "coverage_sufficient": bool(
                coverage_start is not None
                and (reference - coverage_start).days + 1 >= 30
            ),
            "calibration": calibration,
            "detected_income_count": sum(
                1 for event in events if event.get("source") == "recurring_income"
            ),
            "events": events,
            "points": points,
            "summaries": summaries,
            "scenario_spend_multipliers": {
                "optimistic": optimistic_multiplier,
                "base": 1.0,
                "pessimistic": pessimistic_multiplier,
            },
            "method_label": (
                "Saldo até hoje + eventos registados/agendados + receitas mensais "
                "detetadas − média diária das restantes despesas. "
                + (
                    "As estimativas foram recalibradas pelos erros observados nos "
                    f"últimos {int(calibration.get('sample_months', 0) or 0)} "
                    "meses completos. "
                    + (
                        "A faixa usa apenas os erros dos "
                        f"{int(calibration.get('validation_months', 0) or 0)} "
                        "meses previstos antes de o resultado ser conhecido."
                        if bool(calibration.get("band_learned"))
                        else "A faixa mantém ±20% até existirem três meses "
                        "validados fora da amostra."
                    )
                    if bool(calibration.get("enabled"))
                    else "A faixa inicial usa ±20% até existirem pelo menos "
                    f"{int(calibration.get('required_months', 3) or 3)} "
                    "meses completos para aprender."
                )
            ) + " " + str(calibration.get("band_note", "")),
        }

    def fixed_commitments_evolution(
        self,
        months: int = 12,
        as_of: Optional[date] = None,
    ) -> dict[str, Any]:
        """Return explicit subscription and scheduled-payment commitments.

        This deliberately excludes inferred recurring expenses.  Future
        transactions are included only when linked to a subscription or a
        scheduled-payment occurrence, preserving a one-to-one audit trail.
        """

        reference = as_of or date.today()
        horizon = max(1, min(36, int(months or 12)))
        first_month = reference.replace(day=1)
        end_day = add_months(first_month, horizon) - timedelta(days=1)

        recorded: list[dict[str, Any]] = []
        for source_event in self._recorded_future_events(reference, end_day):
            event = dict(source_event)
            if safe_float(event.get("amount")) >= 0:
                continue
            if event.get("subscription_id"):
                event["source"] = "subscription"
            elif event.get("scheduled_id"):
                event["source"] = "scheduled"
            else:
                continue
            recorded.append(event)

        generated = self._subscription_forecast_events(
            reference,
            end_day,
        ) + self._scheduled_forecast_events(reference, end_day)
        events = list(recorded)
        for event in generated:
            if any(self._same_forecast_event(event, existing) for existing in recorded):
                continue
            events.append(dict(event))
        events.sort(
            key=lambda event: (
                str(event.get("date") or ""),
                str(event.get("source") or ""),
                str(event.get("name") or ""),
            )
        )

        rows: list[dict[str, Any]] = []
        for offset in range(horizon):
            month_start = add_months(first_month, offset, 1)
            month_end = add_months(month_start, 1, 1) - timedelta(days=1)
            month_events = [
                dict(event)
                for event in events
                if month_start
                <= parse_date(str(event.get("date") or "")[:10])
                <= month_end
            ]
            subscription_total = sum(
                abs(safe_float(event.get("amount")))
                for event in month_events
                if str(event.get("source") or "") == "subscription"
            )
            scheduled_total = sum(
                abs(safe_float(event.get("amount")))
                for event in month_events
                if str(event.get("source") or "") == "scheduled"
            )
            rows.append(
                {
                    "month": month_start.strftime("%Y-%m"),
                    "label": month_start.strftime("%m/%y"),
                    "subscriptions": subscription_total,
                    "scheduled": scheduled_total,
                    "total": subscription_total + scheduled_total,
                    "event_count": len(month_events),
                    "partial": offset == 0 and reference.day > 1,
                    "events": month_events,
                }
            )

        def window_summary(size: int) -> dict[str, Any]:
            selected = rows[: max(1, min(horizon, int(size)))]
            total = sum(safe_float(row.get("total")) for row in selected)
            complete = [row for row in selected if not bool(row.get("partial"))]
            complete_total = sum(safe_float(row.get("total")) for row in complete)
            partial_total = sum(
                safe_float(row.get("total"))
                for row in selected
                if bool(row.get("partial"))
            )
            return {
                "months": len(selected),
                "total": total,
                "complete_months": len(complete),
                "monthly_average": (
                    complete_total / len(complete) if complete else 0.0
                ),
                "partial_month_total": partial_total,
                "event_count": sum(
                    int(row.get("event_count", 0) or 0) for row in selected
                ),
            }

        non_empty = [row for row in rows if safe_float(row.get("total")) > 0]
        peak = (
            max(non_empty, key=lambda row: safe_float(row.get("total")))
            if non_empty
            else {}
        )
        comparable_rows = [row for row in rows if not bool(row.get("partial"))]
        first_block = comparable_rows[: min(3, len(comparable_rows))]
        last_block = comparable_rows[-min(3, len(comparable_rows)) :]
        first_average = (
            sum(safe_float(row.get("total")) for row in first_block) / len(first_block)
            if first_block
            else 0.0
        )
        last_average = (
            sum(safe_float(row.get("total")) for row in last_block) / len(last_block)
            if last_block
            else 0.0
        )
        trend_pct = (
            (last_average - first_average) / first_average * 100.0
            if first_average
            else None
        )
        return {
            "as_of": iso(reference),
            "months": horizon,
            "rows": rows,
            "events": events,
            "summaries": {
                str(size): window_summary(size)
                for size in sorted({min(horizon, size) for size in (3, 6, 12, horizon)})
            },
            "monthly_average": (
                sum(safe_float(row.get("total")) for row in comparable_rows)
                / len(comparable_rows)
                if comparable_rows
                else 0.0
            ),
            "complete_months": len(comparable_rows),
            "partial_month_total": sum(
                safe_float(row.get("total")) for row in rows if bool(row.get("partial"))
            ),
            "peak_month": str(peak.get("month") or ""),
            "peak_total": safe_float(peak.get("total")),
            "trend_pct": trend_pct,
            "subscription_total": sum(
                safe_float(row.get("subscriptions")) for row in rows
            ),
            "scheduled_total": sum(safe_float(row.get("scheduled")) for row in rows),
            "method_label": (
                "Inclui apenas subscrições ativas e pagamentos agendados com "
                "datas explícitas. Movimentos futuros já ligados substituem a "
                "ocorrência prevista, sem duplicação; despesas recorrentes "
                "inferidas ficam excluídas. A média usa apenas meses completos."
            ),
        }
