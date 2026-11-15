from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

from database.repo import Repo
from services.analytics import AnalyticsService
from services.data_health import DataHealthService
from services.payments import PaymentsService
from ui.components import (
    BudgetVarianceHeatmapChart,
    CashRunwayProbabilityChart,
    DataQualityTrendChart,
    RecurringCommitmentsTimelineChart,
)
from ui.core.theme import Theme


@pytest.fixture
def repo(tmp_path: Path, monkeypatch):
    import utils.config as config

    monkeypatch.setattr(config, "DB_NAME", str(tmp_path / "operational.sqlite3"))
    repository = Repo()
    yield repository
    repository.db.conn.close()


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_demo_review_is_safe_only_for_the_untouched_complete_profile(
    repo: Repo,
) -> None:
    service = DataHealthService(repo)

    initial = service.demo_data_review()

    assert initial.complete_demo_profile is True
    assert initial.safe_to_offer_removal is True
    assert initial.transaction_candidates == 90

    repo.add_transaction(
        date.today().isoformat(),
        "Movimento real do utilizador",
        -15.0,
        "Lazer",
        "Conta principal",
    )
    mixed = service.demo_data_review()

    assert mixed.complete_demo_profile is True
    assert mixed.extra_financial_records == 1
    assert mixed.safe_to_offer_removal is False
    assert "não permite remoção automática" in mixed.message


def test_data_health_snapshot_is_read_only_and_keeps_active_months(repo: Repo) -> None:
    service = DataHealthService(repo)
    before = int(repo.scalar("SELECT COUNT(*) FROM transactions"))

    snapshot = service.snapshot(reference_date=date.today(), months=12)

    assert int(repo.scalar("SELECT COUNT(*) FROM transactions")) == before
    assert snapshot["database_verified"] is True
    assert snapshot["schema_version"] == snapshot["supported_schema_version"]
    assert 0 <= snapshot["score"] <= 100
    assert snapshot["trend"]
    assert all(row["total"] > 0 for row in snapshot["trend"])


def test_cash_runway_probability_is_bounded_and_uses_completed_history(
    repo: Repo,
) -> None:
    analytics = AnalyticsService(repo, PaymentsService(repo))

    result = analytics.cash_runway_scenarios(12, date.today())

    assert len(result["rows"]) == 13
    assert result["rows"][0]["label"] == "Hoje"
    assert result["sample_months"] >= 4
    assert all(0 <= row["probability_positive"] <= 100 for row in result["rows"])
    assert result["rows"][0]["p10"] == result["starting_balance"]
    assert "mês(es) completos" in result["method_label"]
    assert result["probability_scope"] == "month_end"
    assert (
        result["final_month_probability_positive"]
        == result["rows"][-1]["probability_positive"]
    )
    assert all(row["probability_scope"] == "month_end" for row in result["rows"])
    assert "não mede se o saldo permaneceu positivo" in result["probability_definition"]


def test_budget_heatmap_preserves_every_visible_category(repo: Repo) -> None:
    analytics = AnalyticsService(repo, PaymentsService(repo))

    result = analytics.budget_variance_heatmap(12, date.today())

    cell_categories = {row["category"] for row in result["cells"]}
    assert set(result["categories"]) == cell_categories
    assert "Casa e contas" in cell_categories
    assert "Supermercado" in cell_categories
    assert all(row["month"] for row in result["cells"])
    assert all(row["status"] for row in result["cells"])


def test_recurring_timeline_keeps_confidence_levels_separate(repo: Repo) -> None:
    analytics = AnalyticsService(repo, PaymentsService(repo))

    result = analytics.recurring_commitment_timeline(6, date.today())

    assert len(result["rows"]) == 7
    assert result["confirmed_total"] > 0
    assert all(
        event["status_key"] in {"confirmed", "probable", "unlinked"}
        for event in result["events"]
    )
    assert all(
        row["total"]
        == pytest.approx(row["confirmed"] + row["probable"] + row["unlinked"])
        for row in result["rows"]
    )


@pytest.mark.parametrize(
    ("dark", "privacy"),
    [(False, False), (True, False), (False, True)],
)
def test_operational_charts_render_in_all_visual_modes(
    repo: Repo,
    qapp: QApplication,
    dark: bool,
    privacy: bool,
) -> None:
    theme = Theme(dark=dark, privacy=privacy)
    analytics = AnalyticsService(repo, PaymentsService(repo))
    health = DataHealthService(repo)
    charts = [
        CashRunwayProbabilityChart(lambda: theme),
        BudgetVarianceHeatmapChart(lambda: theme),
        RecurringCommitmentsTimelineChart(lambda: theme),
        DataQualityTrendChart(lambda: theme),
    ]
    charts[0].set_projection(analytics.cash_runway_scenarios(12, date.today()))
    assert "mês final terminar acima de zero" in charts[0].accessibleDescription()
    assert "percurso anterior" in charts[0].accessibleDescription()
    charts[1].set_data(analytics.budget_variance_heatmap(12, date.today()))
    charts[2].set_data(analytics.recurring_commitment_timeline(6, date.today()))
    charts[3].set_data(health.monthly_quality_trend(12, date.today()))

    for chart in charts:
        chart.resize(720, max(340, chart.minimumHeight()))
        chart.show()
        qapp.processEvents()
        pixmap = chart.grab()
        assert not pixmap.isNull()
        assert not chart._paint_error
        assert chart.accessibleName()
        assert chart.has_detail_data()
        chart.close()
