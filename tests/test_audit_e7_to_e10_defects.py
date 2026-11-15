"""
Regression tests for defects E7, E8, E9, E10 (C33-C36).
E7: Historical outliers suppress valid recurring-payment detection (services/analytics.py)
E8: Dynamic Analysis double-counts split parents for Todos and Saldo (database/repo/analytics.py)
E9: Heatmap drill-down opens the whole dashboard period (ui/components/charts/heatmap.py / ui/pages/dashboard.py)
E10: Cashflow tooltip activates outside the plot (ui/components/charts/cashflow.py)
"""
from datetime import date, timedelta
import pytest
from PySide6.QtCore import QPointF, QRectF
from PySide6.QtWidgets import QApplication

from database.repo import Repo
from services.analytics import AnalyticsService
from ui.components.charts.cashflow import CashflowTimelineChart
from ui.components.charts.heatmap import HeatmapChart
from ui.core.theme import Theme
import utils.config as config


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance() or QApplication([])
    return app


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_NAME", str(tmp_path / "test_e7_e10.sqlite3"))
    r = Repo()
    yield r
    r.db.conn.close()


@pytest.fixture
def dummy_theme():
    return Theme()


# ============================================================================
# E7 TESTS: Historical outliers suppress valid recurring-payment detection
# ============================================================================

def test_e7_recent_monthly_pair_plus_old_outlier_is_detected(repo):
    """E7: Payments with an old outlier (e.g. 150 days ago) must not suppress
    a valid monthly recurring payment pattern in the recent window.
    """
    today = date.today()
    desc = "Netflix Subscription Test"
    repo.add_transaction((today - timedelta(days=150)).isoformat(), desc, -15.99, "Desejos", "Conta principal")
    repo.add_transaction((today - timedelta(days=60)).isoformat(), desc, -15.99, "Desejos", "Conta principal")
    repo.add_transaction((today - timedelta(days=30)).isoformat(), desc, -15.99, "Desejos", "Conta principal")
    repo.add_transaction(today.isoformat(), desc, -15.99, "Desejos", "Conta principal")

    service = AnalyticsService(repo)
    suspects = service.detect_recurring_subscriptions()
    names = [s["name"] for s in suspects]
    assert desc in names, f"Expected '{desc}' to be detected as recurring subscription despite 150d outlier"
    match = next(s for s in suspects if s["name"] == desc)
    assert match["occurrences"] == 3
    assert abs(match["amount"] - 15.99) < 0.01


def test_e7_old_only_history_does_not_create_candidate(repo):
    """E7: Transactions that occurred exclusively > 90 days ago must not be flagged."""
    today = date.today()
    desc = "Old Cancelled Gym"
    repo.add_transaction((today - timedelta(days=180)).isoformat(), desc, -30.00, "Desejos", "Conta principal")
    repo.add_transaction((today - timedelta(days=150)).isoformat(), desc, -30.00, "Desejos", "Conta principal")

    service = AnalyticsService(repo)
    suspects = service.detect_recurring_subscriptions()
    names = [s["name"] for s in suspects]
    assert desc not in names


def test_e7_irregular_cadence_within_window_rejected(repo):
    """E7: Two payments 75 days apart are not monthly and must be rejected."""
    today = date.today()
    desc = "Irregular Hardware Store"
    repo.add_transaction((today - timedelta(days=75)).isoformat(), desc, -50.00, "Desejos", "Conta principal")
    repo.add_transaction(today.isoformat(), desc, -50.00, "Desejos", "Conta principal")

    service = AnalyticsService(repo)
    suspects = service.detect_recurring_subscriptions()
    names = [s["name"] for s in suspects]
    assert desc not in names


def test_e7_has_monthly_cadence_trailing_matching():
    """E7: Unit test on _has_monthly_cadence with trailing monthly sequence."""
    today = date.today()
    dates = [
        today - timedelta(days=105),
        today - timedelta(days=60),
        today - timedelta(days=30),
        today,
    ]
    assert AnalyticsService._has_monthly_cadence(dates) is True

    irregular = [
        today - timedelta(days=107),
        today - timedelta(days=67),
        today - timedelta(days=12),
        today,
    ]
    assert AnalyticsService._has_monthly_cadence(irregular) is False


# ============================================================================
# E8 TESTS: Dynamic Analysis split parent counts for Todos and Saldo
# ============================================================================

def test_e8_multiple_splits_in_same_category_does_not_double_count(repo):
    """E8: A single parent transaction split into two parts under the SAME category
    must report exactly 1 movement, not 2, across Despesas, Todos, and Saldo.
    """
    tx_id = repo.add_transaction("2028-05-10", "Compra Desejos", -100.0, "Desejos", "Conta principal")
    repo.save_transaction_splits(tx_id, [("Saúde", 60.0), ("Saúde", 40.0)])

    res_desp = repo.dynamic_grouped("2028-05-01", "2028-05-31", group_by="Categoria", kind="Despesas")
    assert len(res_desp) == 1
    assert res_desp[0]["label"] == "Saúde"
    assert abs(res_desp[0]["total"] - 100.0) < 0.01
    assert res_desp[0]["movements"] == 1, f"Expected 1 movement for Despesas, got {res_desp[0]['movements']}"

    res_todos = repo.dynamic_grouped("2028-05-01", "2028-05-31", group_by="Categoria", kind="Todos")
    assert len(res_todos) == 1
    assert res_todos[0]["label"] == "Saúde"
    assert abs(res_todos[0]["total"] - 100.0) < 0.01
    assert res_todos[0]["movements"] == 1, f"Expected 1 movement for Todos, got {res_todos[0]['movements']}"

    res_saldo = repo.dynamic_grouped("2028-05-01", "2028-05-31", group_by="Categoria", kind="Saldo")
    assert len(res_saldo) == 1
    assert res_saldo[0]["label"] == "Saúde"
    assert abs(res_saldo[0]["total"] - (-100.0)) < 0.01
    assert res_saldo[0]["movements"] == 1, f"Expected 1 movement for Saldo, got {res_saldo[0]['movements']}"


def test_e8_unsplit_and_split_reconcile_counts_across_views(repo):
    """E8: 1 unsplit transaction in Saúde (50€) and 1 split transaction
    with 60€ in Saúde and 40€ in Transporte:
    Saúde must have 2 movements (1 unsplit + 1 split)
    Transporte must have 1 movement (1 split).
    """
    repo.add_transaction("2028-06-05", "Farmacia A", -50.0, "Saúde", "Conta principal")
    comp_id = repo.add_transaction("2028-06-15", "Loja B", -100.0, "Desejos", "Conta principal")
    repo.save_transaction_splits(comp_id, [("Saúde", 60.0), ("Transporte", 40.0)])

    for kind in ("Despesas", "Todos"):
        rows = {
            str(r["label"]): r
            for r in repo.dynamic_grouped("2028-06-01", "2028-06-30", group_by="Categoria", kind=kind)
        }
        assert "Saúde" in rows
        assert "Transporte" in rows
        assert abs(rows["Saúde"]["total"] - 110.0) < 0.01
        assert rows["Saúde"]["movements"] == 2
        assert abs(rows["Transporte"]["total"] - 40.0) < 0.01
        assert rows["Transporte"]["movements"] == 1


# ============================================================================
# E9 TESTS: Heatmap drill-down opens exact selected day
# ============================================================================

def test_e9_heatmap_set_data_includes_date_and_resolution(qapp, dummy_theme):
    """E9: HeatmapChart.set_data must store 'date' and 'resolution': 'daily'."""
    chart = HeatmapChart(lambda: dummy_theme)
    raw_data = [
        {"date": "2026-06-25", "expense": 45.50},
        {"date": "2026-06-26", "expense": 12.00},
    ]
    chart.set_data(raw_data)
    assert len(chart.points) == 2
    p0 = chart.points[0]
    assert p0["date"] == "2026-06-25"
    assert p0["date_str"] == "2026-06-25"
    assert p0["resolution"] == "daily"
    assert abs(p0["expense"] - 45.50) < 0.01


def test_e9_dashboard_chart_detail_period_single_day():
    """E9: _chart_detail_period must return (day, day) for a daily heatmap item."""
    from ui.pages.dashboard import DashboardPage

    class DummyDashboard:
        start_cache = "2026-01-01"
        end_cache = "2026-12-31"
        _chart_detail_period = DashboardPage._chart_detail_period

    dummy = DummyDashboard()

    # 1. Normal daily point with 'date'
    item = {"date": "2026-06-25", "resolution": "daily", "expense": 50.0}
    start, end = dummy._chart_detail_period(item)
    assert start == "2026-06-25"
    assert end == "2026-06-25"

    # 2. Point with only 'date_str'
    item_str = {"date_str": "2026-06-25", "expense": 50.0}
    start, end = dummy._chart_detail_period(item_str)
    assert start == "2026-06-25"
    assert end == "2026-06-25"

    # 3. Leap day (2024-02-29)
    item_leap = {"date": "2024-02-29", "resolution": "daily"}
    start, end = dummy._chart_detail_period(item_leap)
    assert start == "2024-02-29"
    assert end == "2024-02-29"

    # 4. Month boundaries
    item_first = {"date": "2026-01-01", "resolution": "daily"}
    start, end = dummy._chart_detail_period(item_first)
    assert start == "2026-01-01"
    assert end == "2026-01-01"

    item_last = {"date": "2026-12-31", "resolution": "daily"}
    start, end = dummy._chart_detail_period(item_last)
    assert start == "2026-12-31"
    assert end == "2026-12-31"

    # 5. Malformed item falls back to start_cache, end_cache
    item_bad = {"date": "not-a-date", "month": 6}
    start, end = dummy._chart_detail_period(item_bad)
    assert start == "2026-01-01"
    assert end == "2026-12-31"


# ============================================================================
# E10 TESTS: Cashflow tooltip activates only inside the plot rectangle
# ============================================================================

def test_e10_cashflow_point_index_outside_plot_returns_none(qapp, dummy_theme):
    """E10: Points outside the plot rectangle must return None, preventing tooltip activation."""
    chart = CashflowTimelineChart(lambda: dummy_theme)
    chart.resize(800, 400)
    chart.points = [
        {"date": f"2026-06-{d:02d}", "balance": 1000.0 + d * 10}
        for d in range(1, 31)
    ]
    plot = chart._plot_rect()
    assert plot.left() == 72.0
    assert plot.top() == 48.0

    # 1. Left axis margin (e.g. x=71 when plot starts at x=72)
    assert chart._point_index_at(71.0) is None
    assert chart._point_index_at(10.0) is None
    assert chart._point_index_at(0.0) is None

    # 2. Right margin (x > plot.right() + 0.5)
    assert chart._point_index_at(plot.right() + 5.0) is None

    # 3. Top margin / Title bar (y < plot.top() - 0.5)
    assert chart._point_index_at(plot.center().x(), y=20.0) is None

    # 4. Bottom margin (y > plot.bottom() + 0.5)
    assert chart._point_index_at(plot.center().x(), y=plot.bottom() + 10.0) is None

    # 5. In-plot coordinates select valid point
    idx_start = chart._point_index_at(plot.left() + 0.1, y=plot.center().y())
    assert idx_start == 0

    idx_end = chart._point_index_at(plot.right() - 0.1, y=plot.center().y())
    assert idx_end == len(chart.points) - 1

    idx_mid = chart._point_index_at(plot.center().x(), y=plot.center().y())
    assert idx_mid is not None
    assert 0 < idx_mid < len(chart.points) - 1


def test_e10_cashflow_event_markers_remain_selectable(qapp, dummy_theme):
    """E10: Event markers must remain selectable through _event_at."""
    chart = CashflowTimelineChart(lambda: dummy_theme)
    marker_event = {"name": "Salário", "amount": 2000.0, "source": "recurring_income", "date": "2026-06-05"}
    marker_rect = QRectF(150.0, 100.0, 18.0, 18.0)
    chart._event_hits = [(marker_event, marker_rect)]

    # Point directly inside marker rect
    hit = chart._event_at(QPointF(155.0, 105.0))
    assert hit is marker_event

    # Point outside marker rect
    miss = chart._event_at(QPointF(200.0, 200.0))
    assert miss is None
