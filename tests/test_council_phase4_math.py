"""A09 small, manual reconciliation examples and input boundaries."""

from datetime import date
from types import SimpleNamespace

import pytest

from database.engine import FinanceDB
from database.repo import Repo
from services.cashflow_forecast import CashflowForecastService
from services.financial_health import FinancialHealthService
from services.forecast_validation import (
    measure_interval_coverage,
    percentile,
    walk_forward_calibration,
)
from services.investment_performance import portfolio_performance_attribution
from services.payments import PaymentsService
from services.spending_diagnostics import SpendingDiagnosticsService


@pytest.fixture
def repo(tmp_path, empty_profile):
    instance = Repo(FinanceDB(tmp_path / "math.sqlite3"))
    yield instance
    instance.db.conn.close()


def test_pareto_counts_real_merchants_before_others_aggregation():
    repo = SimpleNamespace(
        q=lambda *args: [
            {"merchant": f"M{i}", "total": 50.0, "movements": 1} for i in range(20)
        ]
    )
    result = SpendingDiagnosticsService(repo).merchant_pareto(
        "2026-01-01", "2026-01-31", limit=12
    )
    assert result["concentration_80_count"] == 16
    assert sum(r["total"] for r in result["rows"]) == result["total"] == 1000


def test_residual_band_nominal_coverage_matches_its_quantiles():
    result = walk_forward_calibration(
        [
            {"predicted_raw": 100, "actual": value}
            for value in [100, 100, 100, 90, 110, 95, 105, 85, 115, 100]
        ]
    )
    assert result["nominal_coverage_pct"] == 60
    assert result["residual_quantiles"] == (0.2, 0.8)
    assert result["band_sample_size"] == len(result["validation_rows"])


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), -float("inf")])
def test_forecast_calibration_rejects_nonfinite_input(invalid):
    with pytest.raises(ValueError):
        walk_forward_calibration([{"predicted_raw": invalid, "actual": 100}])
    with pytest.raises(ValueError):
        percentile([1, invalid], 0.2)
    with pytest.raises(ValueError):
        measure_interval_coverage([{"actual": invalid, "low": 0, "high": 100}])


def test_effective_scheduled_date_matches_forecast_and_posting(repo):
    repo.add_scheduled_payment(
        "Instalment", "Diversos", "", 40, "Conta principal", "", "2026-10-24", 24, 3
    )
    payments = PaymentsService(repo)
    events = CashflowForecastService(repo, payments)._scheduled_forecast_events(
        date(2026, 10, 20), date(2026, 12, 31)
    )
    assert [event["date"] for event in events] == [
        "2026-10-26",
        "2026-11-24",
        "2026-12-24",
    ]
    assert payments.generate_due_scheduled_payments(date(2026, 10, 24)) == 0
    assert payments.generate_due_scheduled_payments(date(2026, 10, 26)) == 1
    assert repo.q("SELECT date FROM transactions")[0]["date"] == events[0]["date"]


def test_forecast_suppresses_business_day_occurrence_with_stale_progress(repo):
    hid = repo.add_scheduled_payment(
        "Instalment", "Diversos", "", 40, "Conta principal", "", "2026-10-24", 24, 3
    )
    payments = PaymentsService(repo)
    payments.generate_due_scheduled_payments(date(2026, 10, 26))
    repo.db.conn.execute(
        "UPDATE scheduled_payments SET months_paid=0 WHERE id=?", (hid,)
    )
    repo.db.conn.commit()
    events = CashflowForecastService(repo, payments)._scheduled_forecast_events(
        date(2026, 10, 20), date(2026, 12, 31)
    )
    assert [event["date"] for event in events] == ["2026-11-24", "2026-12-24"]


def test_fee_metrics_keep_their_denominators_explicit():
    result = portfolio_performance_attribution(
        [{"id": 1, "value": 1100, "cost_basis": 1000, "fees": 50}]
    )
    assert result["fees_pct_of_cost"] == 5
    assert result["fees_pct_of_cash"] == pytest.approx(50 / 1050 * 100)
    assert (
        result["return_gap_pp"]
        == result["fee_impact_pp"]
        == pytest.approx(10 - 50 / 1050 * 100)
    )


def test_quant_engine_exports_resolve_without_losing_legacy_names():
    from motores_matematicos_simulacao import (
        portfolio_stress_and_tax_optimizer as engine,
    )

    assert not [name for name in engine.__all__ if not hasattr(engine, name)]
    assert engine.DrawdownTrancheOrder is engine.CrisisDeploymentOrder
    assert engine.CrisisAlphaDeployer is engine.CrisisDrawdownManager


def _score_service(rows):
    service = FinancialHealthService.__new__(FinancialHealthService)
    service._snapshot_cache = {}
    service.repo = SimpleNamespace(
        month_period=lambda *_: ("2026-10-01", "2026-10-31", "2026-10"),
        q=lambda *_: [{"data_as_of": "2026-10-20"}],
        top_categories_payment_period=lambda *_: [],
    )
    service._monthly_rows = lambda *_: rows
    service._totals = lambda *_: {"income": 2000, "expenses": 60, "savings": 1940}
    service._net_worth = lambda *_: {
        "liquid_assets": 6000,
        "net_worth": 10000,
        "liabilities_available": True,
        "total_liabilities": 0,
        "subscriptions_available": True,
        "subscription_rows": [],
    }
    service._budget_metrics = lambda *_: (50, "Test budget", {})
    service._quality_metrics = lambda *_: (
        50,
        "Test quality",
        {"transaction_count": 20, "categorized_pct": 100},
    )
    return service


def test_health_history_excludes_current_partial_month(monkeypatch):
    from services import financial_health

    class FixedDate(date):
        @classmethod
        def today(cls):
            return cls(2026, 10, 20)

    monkeypatch.setattr(financial_health, "date", FixedDate)
    rows = [
        {
            "month": f"2026-{month:02d}",
            "income": 2000,
            "expenses": 1000,
            "savings": 1000,
            "savings_rate": 50,
        }
        for month in range(5, 10)
    ]
    rows.append(
        {
            "month": "2026-10",
            "income": 2000,
            "expenses": 60,
            "savings": 1940,
            "savings_rate": 97,
        }
    )
    result = _score_service(rows)._score_snapshot("2026-10")
    assert result["average_expenses"] == 1000
    assert result["savings_rate"] == 50
    assert result["emergency_months"] == 6
    assert result["baseline_months"] == 5


def test_health_without_completed_month_is_unavailable_not_zero(monkeypatch):
    from services import financial_health

    class FixedDate(date):
        @classmethod
        def today(cls):
            return cls(2026, 10, 20)

    monkeypatch.setattr(financial_health, "date", FixedDate)
    rows = [
        {
            "month": "2026-10",
            "income": 2000,
            "expenses": 60,
            "savings": 1940,
            "savings_rate": 97,
        }
    ]
    result = _score_service(rows)._score_snapshot("2026-10")
    assert result["baseline_available"] is False
    assert result["average_expenses"] is None and result["emergency_months"] is None


def test_pareto_long_tail_and_empty_cases_reconcile():
    raw = [{"merchant": f"Large{i}", "total": 60.0, "movements": 1} for i in range(12)]
    raw += [{"merchant": f"Small{i}", "total": 7.4, "movements": 1} for i in range(100)]
    result = SpendingDiagnosticsService(
        SimpleNamespace(q=lambda *_: raw)
    ).merchant_pareto("2026-01-01", "2026-01-31")
    assert result["concentration_80_count"] == 73
    assert sum(r["total"] for r in result["rows"]) == pytest.approx(result["total"])
    assert result["rows"][-1]["cumulative_pct"] == pytest.approx(100)
    empty = SpendingDiagnosticsService(
        SimpleNamespace(q=lambda *_: [])
    ).merchant_pareto("2026-01-01", "2026-01-31")
    assert empty["concentration_80_count"] == 0 and empty["rows"] == []


def test_quantiles_report_sample_without_guaranteeing_future_coverage():
    assert percentile(range(10), 0.2) == pytest.approx(1.8)
    with pytest.raises(ValueError):
        percentile([], 0.2)
    with pytest.raises(ValueError):
        percentile([1], float("nan"))
    rows = [
        {"predicted_raw": 100.0, "actual": v}
        for v in [100, 100, 100, 90, 110, 95, 105, 85, 115, 100]
    ]
    original = walk_forward_calibration(rows)
    altered = walk_forward_calibration(
        rows[:-1] + [{"predicted_raw": 100.0, "actual": 10000}]
    )
    assert (
        original["validation_rows"][-1]["low"] == altered["validation_rows"][-1]["low"]
    )
    assert (
        original["validation_rows"][-1]["high"]
        == altered["validation_rows"][-1]["high"]
    )
    assert [r["band_sample_size"] for r in original["validation_rows"]] == list(
        range(7)
    )
    assert original["coverage_guaranteed"] is False
    assert "60%" in original["band_note"] and "7 resíduo" in original["band_note"]


def test_scheduled_forecast_respects_effective_horizon_and_overdue_business_day(repo):
    repo.add_scheduled_payment(
        "Instalment", "Diversos", "", 40, "Conta principal", "", "2026-10-24", 24, 3
    )
    payments = PaymentsService(repo)
    forecast = CashflowForecastService(repo, payments)
    assert (
        forecast._scheduled_forecast_events(date(2026, 10, 20), date(2026, 10, 25))
        == []
    )
    overdue = forecast._scheduled_forecast_events(date(2026, 10, 30), date(2026, 11, 5))
    assert overdue[0]["date"] == "2026-11-02"
    assert (
        overdue[0]["due_date"] == "2026-10-26"
        and overdue[0]["anchor_date"] == "2026-10-24"
    )
    assert (
        forecast._scheduled_forecast_events(date(2026, 10, 30), date(2026, 10, 31))
        == []
    )


def test_legacy_raw_anchor_occurrence_is_not_posted_again(repo):
    hid = repo.add_scheduled_payment(
        "Instalment", "Diversos", "", 40, "Conta principal", "", "2026-10-24", 24, 3
    )
    repo.create_scheduled_payment_charge(
        hid, "2026-10-24", "Legacy", 40, "Diversos", "Conta principal"
    )
    payments = PaymentsService(repo)
    assert payments.generate_due_scheduled_payments(date(2026, 10, 26)) == 0
    assert repo.movement_count() == 1


@pytest.mark.parametrize("fees", [0, 50, 300])
def test_fee_rate_is_distinct_from_return_gap(fees):
    result = portfolio_performance_attribution(
        [{"id": 1, "value": 1100, "cost_basis": 1000, "fees": fees}]
    )
    assert result["fees_pct_of_cost"] == fees / 10
    assert result["fees_pct_of_cash"] == pytest.approx(fees / (1000 + fees) * 100)
    assert result["net_return_pct"] == pytest.approx((100 - fees) / (1000 + fees) * 100)


@pytest.mark.parametrize("field", ["value", "cost_basis", "fees", "exchange_rate"])
def test_portfolio_rejects_nonfinite_numeric_inputs(field):
    row = {"id": 1, "value": 1100, "cost_basis": 1000, "fees": 50}
    row[field] = float("inf")
    with pytest.raises(ValueError):
        portfolio_performance_attribution([row])


def test_stress_baseline_does_not_switch_to_partial_history_after_day_24(repo):
    for month in range(5, 10):
        repo.add_transaction(
            f"2026-{month:02d}-01", "Income", 2000, "Salário", "Conta principal"
        )
        repo.add_transaction(
            f"2026-{month:02d}-02", "Expense", -1000, "Diversos", "Conta principal"
        )
    repo.add_transaction("2026-10-01", "Income", 2000, "Salário", "Conta principal")
    repo.add_transaction(
        "2026-10-02", "Partial expense", -60, "Diversos", "Conta principal"
    )
    service = FinancialHealthService(repo, PaymentsService(repo))
    early = service.risk_resilience(as_of=date(2026, 10, 20), simulations=200)
    late = service.risk_resilience(as_of=date(2026, 10, 28), simulations=200)
    assert early["average_expenses"] == late["average_expenses"] == 1000
    assert early["data_period_end"] == late["data_period_end"] == "2026-09"


def test_score_tags_calendar_completeness_and_keeps_current_totals(repo):
    service = FinancialHealthService(repo, PaymentsService(repo))
    rows = service._monthly_rows(date.today().strftime("%Y-%m"), 2)
    assert rows[0]["is_complete"] is True and rows[1]["is_complete"] is False


def test_weekend_payment_crossing_month_is_not_posted_before_effective_day(repo):
    repo.add_scheduled_payment(
        "Month end", "Diversos", "", 40, "Conta principal", "", "2026-01-31", 31, 3
    )
    payments = PaymentsService(repo)
    assert payments.generate_due_scheduled_payments(date(2026, 2, 1)) == 0
    assert repo.movement_count() == 0
    assert payments.generate_due_scheduled_payments(date(2026, 2, 2)) == 1
    assert repo.q("SELECT date FROM transactions")[0]["date"] == "2026-02-02"


def test_historical_debt_absence_is_not_zero_and_does_not_read_future_liability(repo):
    for month in (5, 6, 7, 8):
        repo.add_transaction(
            f"2026-{month:02d}-01", "Income", 3000, "Salário", "Conta principal"
        )
        repo.add_transaction(
            f"2026-{month:02d}-02", "Expense", -1000, "Diversos", "Conta principal"
        )
    repo.add_liability("New debt", "Crédito", "Conta principal", 5000, 4, 400)
    service = FinancialHealthService(repo, PaymentsService(repo))
    absent = service.risk_resilience(as_of=date(2026, 9, 4), simulations=200)
    assert absent["debt_service_ratio"] is None
    assert absent["debt_service_available"] is False
    repo.record_financial_state_snapshot("2026-08-31")
    known = service.risk_resilience(as_of=date(2026, 9, 4), simulations=200)
    assert known["debt_service_ratio"] == pytest.approx(400 / 3000 * 100)
    assert known["debt_service_available"] is True


def test_unknown_fee_denominator_is_unavailable_not_zero():
    result = portfolio_performance_attribution(
        [{"id": 1, "value": 500, "cost_basis": 0, "cost_basis_known": 0, "fees": 50}]
    )
    assert result["fees_pct_of_cost"] is None
    assert result["fees_pct_of_cash"] is None
    assert result["return_gap_pp"] is None
    assert result["fee_metrics_available"] is False


@pytest.mark.parametrize(
    "field", ["income_reduction_pct", "emergency_expense", "market_drawdown_pct"]
)
def test_stress_rejects_nonfinite_input_before_querying_profile(field):
    service = FinancialHealthService(SimpleNamespace(), SimpleNamespace())
    with pytest.raises(ValueError):
        service.risk_resilience(**{field: float("nan")})


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _paint_contribution_text(chart):
    from PySide6.QtGui import QImage, QPainter

    image = QImage(1100, 400, QImage.Format_ARGB32)
    painter = QPainter(image)
    text = []

    class RecordingPainter:
        def __getattr__(self, name):
            return getattr(painter, name)

        def drawText(self, *args):
            if args and isinstance(args[-1], str):
                text.append(args[-1])
            return painter.drawText(*args)

    try:
        chart.render_chart(RecordingPainter())
    finally:
        painter.end()
    assert not image.isNull()
    return "\n".join(text)


def _delete_chart(chart):
    from PySide6.QtCore import QCoreApplication, QEvent
    from shiboken6 import isValid

    chart.close()
    chart.deleteLater()
    QCoreApplication.sendPostedEvents(chart, QEvent.DeferredDelete)
    assert not isValid(chart)


def test_fee_explanations_preserve_private_accessibility_and_paint(qapp):
    from ui.components.charts.investment import PortfolioContributionChart
    from ui.core.theme import Theme

    chart = PortfolioContributionChart(lambda: Theme(privacy=True))
    chart.resize(1100, 400)
    try:
        chart.set_data(
            {
                "rows": [
                    {
                        "name": "PRIVATE-ASSET",
                        "asset_class": "PRIVATE-CLASS",
                        "performance_available": True,
                        "contribution_pp": 87.6,
                        "net_gain": 987.6,
                        "fees": 23.4,
                    }
                ],
                "net_return_pct": 123.4,
                "fee_impact_pp": 56.7,
                "total_fees": 987.6,
            }
        )
        chart.hover_index = 0
        visible = _paint_contribution_text(chart)
        accessible = chart.accessibleDescription()
        for value in ("123", "56.7", "56,7", "987", "87,6", "PRIVATE-"):
            assert value not in visible and value not in accessible
        assert "bases distintas" in accessible and "•••" in accessible
    finally:
        _delete_chart(chart)


def test_unknown_fee_return_gap_stays_unavailable_in_chart(qapp):
    from ui.components.charts.investment import PortfolioContributionChart
    from ui.core.theme import Theme

    chart = PortfolioContributionChart(lambda: Theme())
    chart.resize(1100, 400)
    try:
        chart.set_data(
            portfolio_performance_attribution(
                [
                    {
                        "id": 1,
                        "name": "Unknown cost",
                        "value": 500,
                        "cost_basis": 0,
                        "cost_basis_known": 0,
                        "fees": 50,
                    },
                ]
            )
        )
        assert "não disponível" in chart.accessibleDescription()
        visible = _paint_contribution_text(chart)
        assert "diferença de retornos (bases distintas) N/D" in visible
        assert "0,0 p.p." not in visible
    finally:
        _delete_chart(chart)


@pytest.mark.parametrize("metadata_kind", ["mapping", "json"])
@pytest.mark.parametrize("key", ["historical_exchange_rate", "buy_exchange_rate"])
@pytest.mark.parametrize("invalid", [float("nan"), float("inf")])
def test_metadata_historical_fx_rejects_nonfinite_input(metadata_kind, key, invalid):
    import json

    metadata = {key: invalid}
    row = {
        "id": 1,
        "value": 1100,
        "cost_basis": 1000,
        "fees": 50,
        "currency": "USD",
        "exchange_rate": 0.9,
        "metadata": json.dumps(metadata) if metadata_kind == "json" else metadata,
    }
    with pytest.raises(ValueError, match="finito"):
        portfolio_performance_attribution([row])


def test_future_as_of_cannot_make_current_partial_history_complete(repo, monkeypatch):
    from services import financial_health

    class FixedDate(date):
        @classmethod
        def today(cls):
            return cls(2026, 10, 20)

    for month in range(5, 10):
        repo.add_transaction(
            f"2026-{month:02d}-01", "Income", 2000, "Salário", "Conta principal"
        )
        repo.add_transaction(
            f"2026-{month:02d}-02", "Expense", -1000, "Diversos", "Conta principal"
        )
    repo.add_transaction("2026-10-01", "Income", 2000, "Salário", "Conta principal")
    repo.add_transaction(
        "2026-10-02", "Partial expense", -60, "Diversos", "Conta principal"
    )
    monkeypatch.setattr(financial_health, "date", FixedDate)
    result = FinancialHealthService(repo, PaymentsService(repo)).risk_resilience(
        as_of=date(2026, 11, 4),
        simulations=200,
    )
    assert result["data_period_end"] == "2026-09"
    assert result["average_expenses"] == 1000


def test_missing_forecast_observation_is_not_imputed_as_zero():
    with pytest.raises(ValueError):
        measure_interval_coverage([{"low": 0, "high": 0}])
    with pytest.raises(ValueError):
        walk_forward_calibration([{"predicted_raw": 100}])
    observed_zero = measure_interval_coverage([
        {"actual": 0, "predicted": 0, "low": 0, "high": 0},
    ])
    assert observed_zero["interval_total"] == 1
    assert observed_zero["interval_hits"] == 1
    absent = measure_interval_coverage([])
    assert absent["interval_total"] == 0
