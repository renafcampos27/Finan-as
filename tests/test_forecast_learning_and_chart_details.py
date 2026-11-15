from __future__ import annotations

from datetime import date

import pytest
from PySide6.QtWidgets import QApplication, QLabel

from database.repo import Repo
from services.analytics import AnalyticsService
from services.cashflow_forecast import CashflowForecastService
from services.forecast_validation import walk_forward_calibration
from services.payments import PaymentsService
from ui.components import (
    CategoryForecastAccuracyChart,
    ChartDetailDialog,
    DonutChart,
    ForecastErrorCausesChart,
)
from ui.components.charts.financial_pulse import CumulativeCashflowChart
from ui.core import Theme
from utils.helpers import add_months


@pytest.fixture
def repo(tmp_path, monkeypatch):
    import utils.config as config

    monkeypatch.setattr(config, "DB_NAME", str(tmp_path / "learning.sqlite3"))
    result = Repo()
    yield result
    result.db.conn.close()


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _clear_movements(repo: Repo) -> None:
    repo.db.conn.execute("DELETE FROM subscription_occurrences")
    repo.db.conn.execute("DELETE FROM scheduled_payment_occurrences")
    repo.db.conn.execute("DELETE FROM transaction_splits")
    repo.db.conn.execute("DELETE FROM transaction_meta")
    repo.db.conn.execute("DELETE FROM transactions")
    repo.db.conn.commit()


def test_cashflow_forecast_learns_recent_bias_without_changing_movements(repo):
    _clear_movements(repo)
    first_month = date(2025, 1, 1)
    for index in range(18):
        month = add_months(first_month, index, 1)
        amount = 420.0 + index * 55.0
        repo.add_transaction(
            month.replace(day=15).isoformat(),
            f"Despesa variável {index + 1}",
            -amount,
            "Diversos",
            "Conta principal",
        )
    before = repo.scalar("SELECT COUNT(*) FROM transactions")

    forecast = AnalyticsService(repo).cashflow_calendar_forecast(
        90,
        date(2026, 7, 18),
    )
    calibration = forecast["calibration"]

    assert calibration["enabled"] is True
    assert calibration["sample_months"] >= 9
    assert calibration["correction_factor"] > 1.0
    assert 0 <= calibration["accuracy_pct"] <= 100
    assert calibration["confidence"] == "high"
    assert calibration["optimistic_multiplier"] < 1.0
    assert calibration["pessimistic_multiplier"] > 1.0
    assert forecast["variable_daily_expense"] > forecast["raw_variable_daily_expense"]
    assert (
        forecast["scenario_spend_multipliers"]["optimistic"]
        == calibration["optimistic_multiplier"]
    )
    assert (
        forecast["scenario_spend_multipliers"]["pessimistic"]
        == calibration["pessimistic_multiplier"]
    )
    assert "recalibradas pelos erros" in forecast["method_label"]
    assert repo.scalar("SELECT COUNT(*) FROM transactions") == before


def test_calibrated_accuracy_uses_only_unseen_validation_months():
    rows = [
        {"month": f"2026-{index + 1:02d}", "predicted_raw": 100.0, "actual": actual}
        for index, actual in enumerate((50.0, 200.0, 50.0, 100.0, 100.0, 100.0))
    ]

    result = walk_forward_calibration(rows)
    validation = result["validation_rows"]
    expected_mape = sum(row["error_pct"] for row in validation) / len(validation)
    expected_hits = sum(
        1 for row in validation if row["low"] <= row["actual"] <= row["high"]
    )
    expected_coverage = expected_hits / len(validation) * 100.0

    assert result["sample_months"] == 6
    assert result["validation_months"] == 3
    assert all(row["validation_order"] > 0 for row in validation)
    assert result["mape_pct"] == pytest.approx(expected_mape)
    assert result["error_accuracy_pct"] == pytest.approx(
        max(0.0, 100.0 - min(100.0, expected_mape))
    )
    assert result["accuracy_method"] == "composite_interval_quality"
    assert result["interval_hits"] == expected_hits
    assert result["interval_total"] == len(validation)
    assert result["interval_coverage_pct"] == pytest.approx(expected_coverage)
    assert result["accuracy_pct"] == pytest.approx(result["forecast_quality_pct"])
    assert all(0.0 <= row["accuracy_pct"] <= 100.0 for row in validation)
    assert all(row["band_width_pct"] >= 0.0 for row in validation)


def test_latest_real_value_cannot_change_its_own_calibrated_prediction():
    rows = [
        {"month": f"2026-{index + 1:02d}", "predicted_raw": 100.0, "actual": actual}
        for index, actual in enumerate((90.0, 100.0, 110.0, 120.0, 130.0))
    ]
    changed = [dict(row) for row in rows]
    changed[-1]["actual"] = 9_999.0

    first = walk_forward_calibration(rows)
    second = walk_forward_calibration(changed)

    assert first["rows"][-1]["is_validation"] is True
    assert first["rows"][-1]["predicted"] == second["rows"][-1]["predicted"]
    assert (
        first["rows"][-1]["validation_correction_factor"]
        == second["rows"][-1]["validation_correction_factor"]
    )
    assert first["rows"][-1]["low"] == second["rows"][-1]["low"]
    assert first["rows"][-1]["high"] == second["rows"][-1]["high"]


def test_generic_subscription_name_does_not_hide_unrelated_expense(repo):
    _clear_movements(repo)
    repo.db.conn.execute("DELETE FROM subscriptions")
    repo.db.conn.commit()
    repo.add_subscription("Bar", 12.0, "Lazer", 1, "Mensal")
    repo.add_transaction(
        "2026-07-01",
        "Barbearia",
        -12.0,
        "Lazer",
        "Conta principal",
    )
    service = CashflowForecastService(repo, PaymentsService(repo))

    daily = service._variable_spend_by_day(
        date(2026, 7, 1),
        date(2026, 7, 1),
    )

    assert daily[date(2026, 7, 1)] == pytest.approx(12.0)


def test_exact_subscription_occurrence_is_removed_from_variable_baseline(repo):
    _clear_movements(repo)
    repo.db.conn.execute("DELETE FROM subscriptions")
    repo.db.conn.commit()
    subscription_id = repo.add_subscription(
        "Streaming",
        12.0,
        "Subscrições",
        1,
        "Mensal",
    )
    transaction_id = repo.add_transaction(
        "2026-07-01",
        "Streaming (Subscrição)",
        -12.0,
        "Subscrições",
        "Conta principal",
    )
    repo.add_subscription_occurrence(
        subscription_id,
        "2026-07-01",
        transaction_id,
    )
    service = CashflowForecastService(repo, PaymentsService(repo))

    daily = service._variable_spend_by_day(
        date(2026, 7, 1),
        date(2026, 7, 1),
    )

    assert daily == {}


def test_subscription_deduplication_requires_amount_or_exact_link():
    generated = {
        "date": "2026-08-01",
        "name": "Streaming",
        "amount": -12.0,
        "kind": "expense",
        "source": "subscription",
        "source_id": 7,
    }
    different_amount = {
        "date": "2026-08-01",
        "name": "Streaming",
        "amount": -35.0,
        "kind": "expense",
        "source": "recorded",
        "subscription_id": None,
    }
    linked = {
        **different_amount,
        "name": "Descrição alterada",
        "subscription_id": 7,
    }

    assert (
        CashflowForecastService._same_forecast_event(
            generated,
            different_amount,
        )
        is False
    )
    assert CashflowForecastService._same_forecast_event(generated, linked) is True


def test_category_precision_and_error_causes_reconcile_without_future_data(repo):
    _clear_movements(repo)
    for index in range(12):
        month = add_months(date(2025, 1, 1), index, 1)
        repo.add_transaction(
            month.replace(day=5).isoformat(),
            f"Categoria estável {index}",
            -100.0,
            "Estável",
            "Conta principal",
        )
        if index % 2 == 0:
            repo.add_transaction(
                month.replace(day=18).isoformat(),
                f"Categoria volátil {index}",
                -400.0,
                "Volátil",
                "Conta principal",
            )
    analytics = AnalyticsService(repo)

    overall = analytics.forecast_accuracy(
        reference_date=date(2026, 1, 15),
        visible_months=9,
    )
    categories = analytics.category_forecast_accuracy(
        reference_date=date(2026, 1, 15),
        visible_months=9,
    )
    by_category = {row["category"]: row for row in categories["rows"]}
    explained = next(row for row in reversed(overall["rows"]) if row.get("causes"))

    assert (
        by_category["Estável"]["accuracy_pct"] >= by_category["Volátil"]["accuracy_pct"]
    )
    assert (
        by_category["Estável"]["error_accuracy_pct"]
        > by_category["Volátil"]["error_accuracy_pct"]
    )
    assert by_category["Estável"]["status"] == "Previsível"
    assert by_category["Volátil"]["status"] == "Volátil"
    assert overall["interval_coverage_pct"] == pytest.approx(
        overall["interval_hits"] / overall["interval_total"] * 100.0
    )
    assert overall["accuracy_pct"] == pytest.approx(overall["forecast_quality_pct"])
    assert overall["accuracy_pct"] < overall["interval_coverage_pct"]
    assert overall["mean_band_width_pct"] > 0
    assert all(
        row["band_status"] in {"Dentro da faixa", "Fora da faixa"}
        for row in overall["rows"]
    )
    assert sum(cause["delta"] for cause in explained["causes"]) == pytest.approx(
        explained["actual_delta"]
    )
    assert explained["cause_headline"]


def test_new_forecast_diagnostic_charts_render_all_rows(qapp):
    theme = Theme(dark=False)
    category_chart = CategoryForecastAccuracyChart(lambda: theme)
    category_chart.resize(980, 360)
    category_chart.set_data(
        {
            "rows": [
                {
                    "category": "Supermercado",
                    "accuracy_pct": 86.0,
                    "wape_pct": 14.0,
                    "mae": 42.0,
                    "regularity_pct": 100.0,
                    "active_months": 9,
                    "status": "Previsível",
                    "tone": "positive",
                    "total": 2_000.0,
                },
                {
                    "category": "Lazer muito variável",
                    "accuracy_pct": 48.0,
                    "wape_pct": 52.0,
                    "mae": 95.0,
                    "regularity_pct": 55.0,
                    "active_months": 5,
                    "status": "Volátil",
                    "tone": "negative",
                    "total": 900.0,
                },
            ],
            "best": {"category": "Supermercado"},
            "hardest": {"category": "Lazer muito variável"},
        }
    )
    causes_chart = ForecastErrorCausesChart(lambda: theme)
    causes_chart.resize(980, 340)
    causes_chart.set_data(
        {
            "month": "2025-12",
            "predicted": 1_000.0,
            "actual": 1_180.0,
            "causes": [
                {
                    "category": "Viagem",
                    "predicted": 0.0,
                    "actual": 250.0,
                    "delta": 250.0,
                    "reason": "Sem valor previsto para esta categoria",
                    "contribution_pct": 72.0,
                },
                {
                    "category": "Supermercado",
                    "predicted": 500.0,
                    "actual": 430.0,
                    "delta": -70.0,
                    "reason": "Despesa abaixo do padrão recente",
                    "contribution_pct": 28.0,
                },
            ],
        }
    )

    for chart in (category_chart, causes_chart):
        chart.show()
        qapp.processEvents()
        assert not chart.grab().isNull()
        assert chart._paint_error == ""
        assert len(chart._row_rects) == len(chart.rows)
        chart.close()


def test_budget_forecast_applies_learned_error_band(repo):
    _clear_movements(repo)
    first_month = date(2025, 1, 1)
    for index in range(18):
        month = add_months(first_month, index, 1)
        repo.add_transaction(
            month.replace(day=14).isoformat(),
            f"Histórico crescente {index + 1}",
            -(350.0 + index * 45.0),
            "Diversos",
            "Conta principal",
        )
    repo.add_transaction(
        "2026-07-08",
        "Despesa atual",
        -420.0,
        "Diversos",
        "Conta principal",
    )
    repo.set_category_budget("Diversos", 2_500.0, "2026-07")

    forecast = AnalyticsService(repo).monthly_budget_forecast(
        "2026-07",
        date(2026, 7, 18),
    )
    learning = forecast["forecast_learning"]

    assert learning["enabled"] is True
    assert learning["correction_factor"] > 1.0
    assert forecast["total_projected_optimistic"] < forecast["total_projected"]
    assert forecast["total_projected"] < forecast["total_projected_pessimistic"]
    assert "corrigiu o ritmo pelos erros" in forecast["method_label"]
    assert all(
        row["projected_optimistic"] <= row["projected"] <= row["projected_pessimistic"]
        for row in forecast["rows"]
    )


def test_chart_movement_details_reconcile_split_categories(repo):
    _clear_movements(repo)
    transaction_id = repo.add_transaction(
        "2026-07-12",
        "Compra dividida",
        -100.0,
        "Diversos",
        "Conta principal",
    )
    repo.save_transaction_splits(
        transaction_id,
        [("Alimentação", 60.0), ("Lazer", 40.0)],
    )

    food = repo.chart_movement_details(
        "2026-07-01",
        "2026-07-31",
        category="Alimentação",
        kind="Despesas",
    )
    leisure = repo.chart_movement_details(
        "2026-07-01",
        "2026-07-31",
        category="Lazer",
        kind="Despesas",
    )

    assert len(food) == len(leisure) == 1
    assert float(food[0]["amount"]) == pytest.approx(-60.0)
    assert float(leisure[0]["amount"]) == pytest.approx(-40.0)
    assert food[0]["effective_category"] == "Supermercado"
    assert food[0]["split_contribution"] == 1


def test_chart_detail_payload_can_be_enriched_and_runs_related_action(qapp):
    theme = Theme(dark=False, privacy=False)
    selected: list[dict] = []
    chart = CumulativeCashflowChart(lambda: theme)
    chart.set_rows(
        [
            {
                "date": "2026-07-12",
                "resolution": "daily",
                "income": 2_000.0,
                "expenses": 350.0,
                "net": 1_650.0,
            }
        ]
    )
    chart.configure_details(
        lambda item: {
            "title": "Entradas vs. saídas acumuladas",
            "rows": [
                {
                    "date": "12/07/2026",
                    "title": "Supermercado",
                    "subtitle": "Alimentação",
                    "value": "-350,00 €",
                    "tone": "negative",
                }
            ],
            "show_empty_rows": True,
        },
        action_label="Abrir movimentos",
        action_handler=selected.append,
    )

    assert chart.open_detail(chart.rows[0]) is True
    payload = chart._last_detail_payload
    assert payload["title"] == "Entradas vs. saídas acumuladas"
    assert payload["rows"][0]["title"] == "Supermercado"
    assert selected and selected[0]["date"] == "2026-07-12"


@pytest.mark.parametrize("dark", [False, True])
def test_chart_detail_dialog_is_bounded_and_readable(qapp, dark):
    dialog = ChartDetailDialog(
        {
            "title": "Despesas por categoria",
            "subtitle": "Alimentação · julho de 2026",
            "explanation": "Movimentos que formam o valor selecionado.",
            "metrics": [
                {"label": "Total", "value": "850,40 €", "tone": "negative"},
                {"label": "Movimentos", "value": "12", "tone": "neutral"},
            ],
            "rows": [
                {
                    "date": "12/07/2026",
                    "title": "Supermercado com uma descrição bastante longa",
                    "subtitle": "Alimentação · Conta principal",
                    "value": "-95,30 €",
                    "tone": "negative",
                }
            ],
        },
        Theme(dark=dark),
        action_label="Abrir movimentos",
    )
    dialog.show()
    qapp.processEvents()

    assert dialog.width() <= 780
    assert dialog.height() <= 720
    assert dialog.title_label.text() == "Despesas por categoria"
    assert dialog.action_button is not None
    assert dialog.action_button.text() == "Abrir movimentos"
    assert dialog.rows_scroll.isVisible()
    assert any(
        label.text() == "Supermercado com uma descrição bastante longa"
        for label in dialog.findChildren(QLabel)
    )
    assert "#000000" not in dialog.styleSheet().casefold()
    dialog.close()


def test_chart_detail_without_real_action_has_no_decorative_button(qapp):
    dialog = ChartDetailDialog(
        {
            "title": "Detalhe apenas informativo",
            "metrics": [{"label": "Valor", "value": "10,00 €"}],
        },
        Theme(dark=False),
    )

    assert dialog.action_button is None
    dialog.close()


def test_chart_point_can_be_pinned_and_compared_with_another_point(qapp):
    chart = CumulativeCashflowChart(lambda: Theme(dark=False))
    first = {
        "date": "2026-06-30",
        "income": 2_000.0,
        "expenses": 1_500.0,
        "net": 500.0,
    }
    second = {
        "date": "2026-07-31",
        "income": 2_200.0,
        "expenses": 1_650.0,
        "net": 550.0,
    }

    assert chart.pin_detail_item(first) is True
    payload = chart.detail_payload(second)
    comparison = payload["comparison"]

    assert comparison["pinned_label"] == "2026-06-30"
    assert comparison["same_item"] is False
    assert any(
        row["key"] == "income"
        and row["delta"] == pytest.approx(200)
        and row["change_pct"] == pytest.approx(10)
        for row in comparison["metrics"]
    )
    assert "comparado com 2026-06-30" in payload["subtitle"]
    assert not chart._pinned_badge.isHidden()

    chart.clear_pinned_detail()
    assert "comparison" not in chart.detail_payload(second)
    assert chart._pinned_badge.isHidden()


def test_chart_detail_dialog_offers_real_pin_action(qapp):
    dialog = ChartDetailDialog(
        {
            "title": "Ponto comparável",
            "metrics": [{"label": "Valor", "value": "10,00 €"}],
        },
        Theme(dark=False),
        pin_label="Fixar para comparar",
    )

    assert dialog.pin_button is not None
    assert dialog.pin_button.text() == "Fixar para comparar"
    dialog._request_pin()
    assert dialog.pin_requested is True
    dialog.close()


def test_generic_chart_details_respect_privacy(qapp):
    chart = DonutChart(lambda: Theme(privacy=True))
    payload = chart.detail_payload(("Alimentação", 523.45))
    metric_values = [str(metric.get("value") or "") for metric in payload["metrics"]]

    assert any("••••" in value for value in metric_values)
