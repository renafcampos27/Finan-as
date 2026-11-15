"""Dashboard data loader with a thread-owned database connection."""

from __future__ import annotations

import calendar
import sqlite3
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from PySide6.QtCore import QThread, Signal

from services.dashboard_metrics import (
    DEFAULT_WEEKLY_WORK_HOURS,
    emergency_reserve_snapshot,
    expense_work_time_snapshot,
    work_value_snapshot,
)
from services.decision_intelligence import build_decision_briefing
from services.investment_projection import build_financial_independence_outlook
from utils.app_logging import log_exception
from utils.helpers import safe_float


def _previous_year(value: date) -> date:
    try:
        return value.replace(year=value.year - 1)
    except ValueError:
        return value.replace(year=value.year - 1, day=28)


def _comparison_window(
    start_value: str,
    end_value: str,
    comparison_mode: str,
) -> tuple[date, date, str]:
    start_date = datetime.strptime(start_value, "%Y-%m-%d").date()
    end_date = datetime.strptime(end_value, "%Y-%m-%d").date()
    if comparison_mode == "year":
        return _previous_year(start_date), _previous_year(end_date), "ano anterior"
    window_days = max(1, (end_date - start_date).days + 1)
    previous_end = start_date - timedelta(days=1)
    previous_start = previous_end - timedelta(days=window_days - 1)
    return previous_start, previous_end, "período anterior"


def _average_monthly_expense(rows: list[dict[str, Any]], fallback: float) -> float:
    complete_months = [
        safe_float(row.get("expenses"))
        for row in rows
        if safe_float(row.get("expenses")) > 0 and not bool(row.get("partial"))
    ]
    positive_months = complete_months or [
        safe_float(row.get("expenses"))
        for row in rows
        if safe_float(row.get("expenses")) > 0
    ]
    return (
        sum(positive_months) / len(positive_months)
        if positive_months
        else safe_float(fallback)
    )


def _metric_trends(
    rows: list[dict[str, Any]],
    maximum_points: int = 24,
) -> dict[str, list[float]]:
    """Compact the selected period without changing its financial totals."""
    clean = list(rows or [])
    limit = max(2, int(maximum_points or 24))
    if len(clean) > limit:
        compacted: list[dict[str, float]] = []
        for index in range(limit):
            start = index * len(clean) // limit
            end = (index + 1) * len(clean) // limit
            bucket = clean[start:end]
            income = sum(safe_float(row.get("income")) for row in bucket)
            expenses = sum(safe_float(row.get("expenses")) for row in bucket)
            compacted.append(
                {
                    "income": income,
                    "expenses": expenses,
                    "net": income - expenses,
                }
            )
        clean = compacted
    return {
        "income": [safe_float(row.get("income")) for row in clean],
        "expenses": [safe_float(row.get("expenses")) for row in clean],
        "savings": [
            safe_float(
                row.get(
                    "net",
                    safe_float(row.get("income")) - safe_float(row.get("expenses")),
                )
            )
            for row in clean
        ],
    }


class DashboardWorker(QThread):
    finished_data = Signal(int, dict)
    failed = Signal(int, str)

    def __init__(
        self,
        request_id,
        repo,
        analytics,
        start,
        end,
        range_days,
        resolution,
        comparison_mode,
        enabled_modules=None,
        parent=None,
        period_label="",
    ):
        super().__init__(parent)
        self.request_id = int(request_id)
        self._repo_type = type(repo)
        self._analytics_type = type(analytics)
        self._payments_type = type(analytics.payments_service)
        self._database_path = getattr(getattr(repo, "db", None), "path", None)
        self._start_date = start
        self._end_date = end
        self.range_days = range_days
        self.resolution = resolution
        self.comparison_mode = str(comparison_mode or "previous")
        self.enabled_modules = None if enabled_modules is None else set(enabled_modules)
        self.period_label = str(period_label or "").strip()
        self._database_lock = threading.Lock()
        self._local_database = None
        self._is_closing = False
        self._interruption_requested = False

    def cancel(self) -> None:
        """Cancel cooperatively and interrupt the currently running SQLite query."""
        self._interruption_requested = True
        self.requestInterruption()
        with self._database_lock:
            if getattr(self, "_is_closing", False):
                return
            database = self._local_database
            if database is None:
                return
            connection = getattr(database, "conn", None)
            if connection is not None:
                try:
                    connection.interrupt()
                except (sqlite3.ProgrammingError, sqlite3.OperationalError):
                    pass
                except Exception as exc:
                    log_exception("Failed to interrupt dashboard worker connection", exc)

    def requestInterruption(self) -> None:
        self._interruption_requested = True
        super().requestInterruption()

    def isInterruptionRequested(self) -> bool:
        return self._interruption_requested or super().isInterruptionRequested()

    def module_enabled(self, key: str) -> bool:
        return self.enabled_modules is None or key in self.enabled_modules

    def _local_services(self):
        from database.engine import FinanceDB
        from database.repo import Repo
        from services.analytics import AnalyticsService

        if self._database_path is not None:
            local_repo = self._repo_type.__new__(self._repo_type)
            local_repo.db = FinanceDB.open_read_only(Path(self._database_path))
            with self._database_lock:
                if self.isInterruptionRequested() or self._is_closing:
                    try:
                        local_repo.db.conn.close()
                    except Exception:
                        pass
                    return None, None
                self._local_database = local_repo.db
        else:
            local_repo = Repo()
            with self._database_lock:
                if self.isInterruptionRequested() or self._is_closing:
                    try:
                        db = getattr(local_repo, "db", None)
                        if db is not None and hasattr(db, "conn"):
                            db.conn.close()
                    except Exception:
                        pass
                    return None, None
                self._local_database = getattr(local_repo, "db", None)
        try:
            local_payments = self._payments_type(local_repo)
            local_analytics = self._analytics_type(
                local_repo,
                payments_service=local_payments,
            )
        except (AttributeError, TypeError) as exc:
            log_exception("Dashboard worker service recreation failed", exc)
            local_analytics = AnalyticsService(local_repo)
        return local_repo, local_analytics

    def _comparison_totals(self, analytics):
        if self.range_days is None or self.comparison_mode == "none":
            return None, "", "", ""
        try:
            previous_start, previous_end, comparison_label = _comparison_window(
                self._start_date,
                self._end_date,
                self.comparison_mode,
            )
        except (TypeError, ValueError) as exc:
            log_exception("Invalid dashboard comparison period", exc)
            return None, "", "", ""
        totals = analytics.totals_for_period(
            previous_start.isoformat(),
            previous_end.isoformat(),
        )
        return (
            totals,
            comparison_label,
            previous_start.isoformat(),
            previous_end.isoformat(),
        )

    def _load_data(self, repo, analytics) -> dict[str, Any] | None:
        today = date.today()
        totals = analytics.totals_for_period(self._start_date, self._end_date)
        if self.isInterruptionRequested():
            return None

        (
            totals_prev,
            comparison_label,
            comparison_start,
            comparison_end,
        ) = self._comparison_totals(analytics)
        balance = repo.cash_balance()
        net = repo.net_worth()

        actual_history_days = (
            None
            if self.range_days is None
            else max(
                1,
                (
                    datetime.strptime(self._end_date, "%Y-%m-%d").date()
                    - datetime.strptime(self._start_date, "%Y-%m-%d").date()
                ).days
                + 1,
            )
        )
        forecast = (
            analytics.balance_forecast_series(
                historical_days=actual_history_days,
                forecast_days=30,
                resolution=self.resolution,
            )
            if self.module_enabled("chart")
            else []
        )
        period_flow = analytics.cashflow_series(
            self._start_date,
            self._end_date,
            self.resolution,
        )
        data_context = analytics.data_coverage_snapshot(
            self._start_date,
            min(self._end_date, today.isoformat()),
        )
        forecast_data_context = analytics.data_coverage_snapshot(
            (today - timedelta(days=364)).isoformat(),
            today.isoformat(),
        )
        cashflow_pulse = period_flow if self.module_enabled("cashflow_pulse") else []
        cashflow_pulse_previous = (
            analytics.cashflow_series(
                comparison_start,
                comparison_end,
                self.resolution,
            )
            if comparison_start and self.module_enabled("cashflow_pulse")
            else []
        )
        metric_trends = _metric_trends(period_flow)
        if self.isInterruptionRequested():
            return None

        category_modules = {
            "waterfall",
            "donut",
            "expense_work_time",
            "insights",
            "radar",
            "treemap",
            "bubble",
            "sankey",
        }
        categories = (
            analytics.category_totals(self._start_date, self._end_date)
            if any(self.module_enabled(key) for key in category_modules)
            else []
        )
        previous_categories = (
            analytics.category_totals(comparison_start, comparison_end)
            if comparison_start
            and any(self.module_enabled(key) for key in category_modules)
            else []
        )
        financial_independence_months = analytics.monthly_summary(13)
        monthly_summary = (
            financial_independence_months[-8:]
            if len(financial_independence_months) >= 8
            else financial_independence_months
        )
        savings_efficiency = (
            analytics.monthly_summary_for_period(
                self._start_date,
                self._end_date,
            )
            if self.module_enabled("savings_efficiency")
            else []
        )
        savings_efficiency_previous = (
            analytics.monthly_summary_for_period(
                comparison_start,
                comparison_end,
            )
            if comparison_start and self.module_enabled("savings_efficiency")
            else []
        )

        complete_financial_months = [
            row
            for row in financial_independence_months
            if not bool(row.get("partial"))
            and (
                safe_float(row.get("income")) > 0 or safe_float(row.get("expenses")) > 0
            )
        ]
        monthly_expense = (
            sum(safe_float(row.get("expenses")) for row in complete_financial_months)
            / len(complete_financial_months)
            if complete_financial_months
            else 0.0
        )
        monthly_saving = (
            sum(safe_float(row.get("savings")) for row in complete_financial_months)
            / len(complete_financial_months)
            if complete_financial_months
            else 0.0
        )
        net_worth_snapshot = analytics.net_worth_snapshot()
        investable_capital = max(
            0.0,
            safe_float(net_worth_snapshot.get("liquid_assets"))
            + safe_float(net_worth_snapshot.get("investment_assets"))
            - safe_float(net_worth_snapshot.get("total_liabilities")),
        )
        financial_independence = build_financial_independence_outlook(
            financial_independence_months,
            investable_capital=investable_capital,
        )
        goals = [dict(row) for row in repo.goals()]
        income_context = analytics.personal_income_context(6)
        weekly_hours = safe_float(
            repo.setting(
                "work_hours_per_week",
                str(DEFAULT_WEEKLY_WORK_HOURS),
            ),
            DEFAULT_WEEKLY_WORK_HOURS,
        )
        work_value = work_value_snapshot(
            income_context.get("average_income", 0),
            weekly_hours=weekly_hours,
        )
        work_value["income_context_months"] = int(
            income_context.get("context_months", 0) or 0
        )
        work_value["income_reference_name"] = str(
            income_context.get("reference_name") or "rendimento"
        )
        reserve_expense_rows = [
            row
            for row in monthly_summary
            if safe_float(row.get("expenses")) > 0 and not bool(row.get("partial"))
        ]
        if not reserve_expense_rows:
            reserve_expense_rows = [
                row for row in monthly_summary if safe_float(row.get("expenses")) > 0
            ]
        reserve_expense_fallback = not bool(reserve_expense_rows)
        reserve_expense_months = (
            len(complete_financial_months)
            if reserve_expense_fallback
            else len(reserve_expense_rows)
        )
        emergency_reserve = emergency_reserve_snapshot(
            goals,
            cash_balance=balance,
            monthly_expense=_average_monthly_expense(
                monthly_summary,
                monthly_expense,
            ),
        )
        emergency_reserve["expense_history_months"] = reserve_expense_months
        emergency_reserve["expense_lookup_months"] = (
            13 if reserve_expense_fallback else 8
        )
        expense_work_time = expense_work_time_snapshot(
            categories,
            hourly_value=work_value.get("hourly_value", 0),
            daily_hours=work_value.get("daily_hours", 8),
        )

        heatmap_series = (
            analytics.balance_series(365) if self.module_enabled("heatmap") else []
        )
        subscription_total = (
            analytics.payments_service.active_subscription_monthly_total()
            if self.module_enabled("insights")
            else 0.0
        )
        subscription_suspects = (
            analytics.detect_recurring_subscriptions()
            if self.module_enabled("insights")
            else []
        )
        anomalies = (
            analytics.detect_financial_anomalies(today)
            if self.module_enabled("insights")
            else []
        )
        data_quality = repo.transaction_data_quality_counts(
            self._start_date,
            self._end_date,
        )

        days_in_month = calendar.monthrange(today.year, today.month)[1]
        days_passed = today.day
        budget_forecast = (
            analytics.monthly_budget_forecast(today.strftime("%Y-%m"), today)
            if self.module_enabled("budget_forecast") or self.module_enabled("insights")
            else {}
        )
        cashflow_forecast = (
            analytics.cashflow_calendar_forecast(90, today)
            if self.module_enabled("cashflow_calendar")
            or self.module_enabled("insights")
            else {}
        )
        current_month_key = today.strftime("%Y-%m")
        if (
            financial_independence_months
            and financial_independence_months[-1].get("month") == current_month_key
        ):
            current_month_totals = {
                "income": safe_float(financial_independence_months[-1].get("income")),
                "expenses": safe_float(financial_independence_months[-1].get("expenses")),
                "savings": safe_float(financial_independence_months[-1].get("savings")),
            }
        else:
            current_month_totals = analytics.totals_for_period(
                today.replace(day=1).strftime("%Y-%m-%d"),
                today.isoformat(),
            )
        burn_rate = (
            current_month_totals["expenses"] / days_passed if days_passed > 0 else 0
        )
        decision_briefing = (
            build_decision_briefing(
                totals=totals,
                previous_totals=totals_prev,
                categories=categories,
                previous_categories=previous_categories,
                monthly_summary=monthly_summary,
                budget_forecast=budget_forecast,
                cashflow_forecast=cashflow_forecast,
                emergency_reserve=emergency_reserve,
                subscription_total=subscription_total,
                subscription_suspects=subscription_suspects,
                anomalies=anomalies,
                work_value=work_value,
                data_quality=data_quality,
                start=self._start_date,
                end=self._end_date,
                period_label=self.period_label,
                comparison_label=comparison_label,
                as_of=today,
            )
            if self.module_enabled("insights")
            else {}
        )

        if self.isInterruptionRequested():
            return None
        investment_evolution = [
            row
            for row in repo.investment_evolution()
            if self._start_date <= str(row.get("date") or "")[:10] <= self._end_date
        ]
        return {
            "totals": totals,
            "totals_prev": totals_prev,
            "comparison_label": comparison_label,
            "comparison_start": comparison_start,
            "comparison_end": comparison_end,
            "balance": balance,
            "net": net,
            "fire_number": safe_float(financial_independence.get("target")),
            "financial_independence": financial_independence,
            "monthly_expense": monthly_expense,
            "monthly_saving": monthly_saving,
            "forecast": forecast,
            "cashflow_pulse": cashflow_pulse,
            "cashflow_pulse_previous": cashflow_pulse_previous,
            "metric_trends": metric_trends,
            "data_context": data_context,
            "forecast_data_context": forecast_data_context,
            "savings_efficiency": savings_efficiency,
            "savings_efficiency_previous": savings_efficiency_previous,
            "categories": categories,
            "previous_categories": previous_categories,
            "monthly_summary": monthly_summary,
            "goals": goals,
            "income_context": income_context,
            "weekly_hours": work_value.get("weekly_hours", weekly_hours),
            "work_value": work_value,
            "emergency_reserve": emergency_reserve,
            "expense_work_time": expense_work_time,
            "investment_evolution": investment_evolution,
            "heatmap": heatmap_series,
            "subscription_total": subscription_total,
            "subscription_suspects": subscription_suspects,
            "anomalies": anomalies,
            "data_quality": data_quality,
            "decision_briefing": decision_briefing,
            "budget_forecast": budget_forecast,
            "cashflow_forecast": cashflow_forecast,
            "burn_rate": burn_rate,
            "forecast_end_month": burn_rate * days_in_month,
            "days_passed": days_passed,
            "days_in_month": days_in_month,
        }

    def run(self) -> None:
        local_repo = None
        try:
            local_repo, local_analytics = self._local_services()
            if self.isInterruptionRequested() or local_repo is None:
                return
            conn = getattr(getattr(local_repo, "db", None), "conn", None)
            if conn is None:
                return
            conn.execute("BEGIN")
            data = self._load_data(local_repo, local_analytics)
            if self.isInterruptionRequested():
                return
            if data is not None and not self.isInterruptionRequested():
                self.finished_data.emit(self.request_id, data)
        except Exception as exc:
            # A worker thread must turn unexpected failures into a UI signal;
            # letting an exception cross the Qt thread boundary terminates it.
            if self.isInterruptionRequested() or (
                isinstance(exc, sqlite3.OperationalError)
                and "interrupt" in str(exc).casefold()
            ):
                return
            log_exception("DASHBOARD WORKER CRASHED", exc)
            if not self.isInterruptionRequested():
                self.failed.emit(self.request_id, str(exc))
        finally:
            with self._database_lock:
                self._is_closing = True
                db_to_close = self._local_database
                self._local_database = None

            db = getattr(local_repo, "db", None) or db_to_close
            if db is not None:
                try:
                    conn = getattr(db, "conn", None)
                    if conn is not None:
                        try:
                            if getattr(conn, "in_transaction", False):
                                conn.rollback()
                        except (sqlite3.ProgrammingError, sqlite3.OperationalError):
                            pass
                        conn.close()
                except (sqlite3.ProgrammingError, sqlite3.OperationalError):
                    pass
                except Exception as exc:
                    log_exception("Failed to close dashboard worker database", exc)
