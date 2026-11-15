"""Automated verification suite for Audit Batch 32 defects.

Defects covered:
1. BUG-CHART-BASE-01: HiDPI permanent cache invalidation fix in StableChart.paintEvent
   comparing self._cached_pixmap.size() == self.size() instead of physical target_pixel_size.
2. BUG-EXP-02: _render_on_gui_thread uses Qt.ConnectionType.QueuedConnection to prevent worker
   thread freeze in native C++ and ensure finished.wait(timeout=60) functions as a real timeout.
3. BUG-REPO-ANA-01: save_monthly_snapshot supports *, commit: bool = True, avoids committing
   before SAVEPOINT, and rolls back savepoint without calling connection.rollback().
4. BUG-REPO-PLAN-01: Ambient transaction preservation in _planning_write, create_subscription_charge,
   delete_scheduled_payment, and create_scheduled_payment_charge.
5. BUG-DB-RO-01: CancellableReadSnapshot.__enter__ prevents connection/FD leak on failure or cancel
   by closing connection and clearing _connection in try...except.
6. BUG-UDF-01: AppStateStore thread-safety with threading.RLock(), subscriber snapshot iteration
   with error isolation, and unsubscribe method.
7. BUG-UDF-02: app_reducer clamps DEPLOY_DRY_POWDER_TRANCHE amount to available cash,
   preventing "free money" creation and preserving total portfolio value.
"""

from __future__ import annotations

import math
import os
import sqlite3
import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import QApplication

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from database.engine import FinanceDB
from database.read_only import CancellableReadSnapshot
from database.repo import Repo
from motores_matematicos_simulacao.crisis_backtester_and_architecture_engine import (
    Action,
    AppState,
    AppStateStore,
    app_reducer,
)
from services.export_documents import _render_on_gui_thread
from ui.components.charts.base import StableChart
from ui.core.theme import Theme


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.delenv("FINANCAS_PESSOAIS_DEMO_DATA", raising=False)
    db = FinanceDB(tmp_path / "batch32_test.sqlite3")
    r = Repo(db)
    yield r
    try:
        db.conn.close()
    except sqlite3.Error:
        pass


# ============================================================================
# 1. BUG-CHART-BASE-01: HiDPI Cache Reuse in StableChart.paintEvent
# ============================================================================

def test_c_chart_base_01_source_code_contract():
    """Verify StableChart compares physical pixel dimensions target_pixel_size and DPR."""
    source = (ROOT / "ui" / "components" / "charts" / "base.py").read_text(encoding="utf-8")
    assert "target_pixel_size = QSize(pixel_width, pixel_height)" in source
    assert "self._cached_pixmap.size() == target_pixel_size" in source
    assert "devicePixelRatio()" in source


def test_c_chart_base_01_hidpi_cache_reused_on_scaling_factors(qapp):
    """StableChart must reuse cached pixmap on HiDPI displays (125%, 150%, 200%)."""
    class TestHiDPIChart(StableChart):
        def __init__(self, dpr: float):
            super().__init__(lambda: Theme())
            self._mock_dpr = dpr
            self.draw_count = 0

        def devicePixelRatioF(self) -> float:
            return self._mock_dpr

        def render_chart(self, painter: QPainter) -> None:
            self.draw_count += 1
            painter.fillRect(self.rect(), Qt.GlobalColor.blue)

    for scale in (1.25, 1.5, 2.0):
        chart = TestHiDPIChart(dpr=scale)
        chart.resize(200, 160)
        chart.show()
        qapp.processEvents()
        assert chart._cached_pixmap is not None
        assert chart.draw_count == 1
        first_pixmap = chart._cached_pixmap

        # Pixmap physical size matches target_pixel_size
        expected_w = int(math.ceil(200 * scale))
        expected_h = int(math.ceil(160 * scale))
        assert chart._cached_pixmap.size() == QSize(expected_w, expected_h)
        assert math.isclose(first_pixmap.devicePixelRatio(), scale, abs_tol=1e-3)

        # Subsequent repaint without data change must reuse cached pixmap
        chart.update()
        qapp.processEvents()
        assert chart.draw_count == 1
        assert chart._cached_pixmap is first_pixmap
        chart.close()


# ============================================================================
# 2. BUG-EXP-02: Export Documents QueuedConnection & Timeout Sentinel
# ============================================================================

def test_c_exp_02_source_uses_queued_connection():
    """_render_on_gui_thread must use QueuedConnection, not BlockingQueuedConnection."""
    source = (ROOT / "services" / "export_documents.py").read_text(encoding="utf-8")
    assert "Qt.ConnectionType.QueuedConnection" in source
    assert "BlockingQueuedConnection" not in source


def test_c_exp_02_render_on_gui_thread_timeout_raises_runtime_error(qapp, monkeypatch):
    """When GUI thread fails to process work within timeout, a typed RuntimeError is raised."""
    # Simulate being on a worker thread
    monkeypatch.setattr("services.export_documents._on_gui_thread", lambda: False)

    # Monkeypatch finished.wait to simulate 60s timeout expiration
    with patch("threading.Event.wait", return_value=False):
        with pytest.raises(RuntimeError) as exc_info:
            _render_on_gui_thread(lambda: "never_reached")
        assert "A geração do PDF bloqueou" in str(exc_info.value)


def test_c_exp_02_render_on_gui_thread_propagates_worker_exception(qapp, monkeypatch):
    """Exceptions in work() must be captured and re-raised on the waiting thread."""
    monkeypatch.setattr("services.export_documents._on_gui_thread", lambda: False)

    def faulty_work():
        raise ValueError("simulated export failure")

    worker_error: list[BaseException] = []

    def run_worker():
        try:
            _render_on_gui_thread(faulty_work)
        except BaseException as exc:
            worker_error.append(exc)

    t = threading.Thread(target=run_worker)
    t.start()

    # Spin event loop on main thread until worker finishes
    for _ in range(50):
        qapp.processEvents()
        t.join(timeout=0.05)
        if not t.is_alive():
            break

    assert len(worker_error) == 1
    assert isinstance(worker_error[0], ValueError)
    assert "simulated export failure" in str(worker_error[0])


def test_c_exp_02_render_on_gui_thread_success_from_worker(qapp, monkeypatch):
    """Successful work() on worker thread correctly returns result to caller."""
    monkeypatch.setattr("services.export_documents._on_gui_thread", lambda: False)

    worker_result: list[str] = []

    def run_worker():
        res = _render_on_gui_thread(lambda: "pdf_bytes_ok")
        worker_result.append(res)

    t = threading.Thread(target=run_worker)
    t.start()

    for _ in range(50):
        qapp.processEvents()
        t.join(timeout=0.05)
        if not t.is_alive():
            break

    assert worker_result == ["pdf_bytes_ok"]


# ============================================================================
# 3. BUG-REPO-ANA-01: save_monthly_snapshot Transaction Isolation
# ============================================================================

def test_c_repo_ana_01_save_monthly_snapshot_commit_parameter_and_outer_tx(repo: Repo):
    """save_monthly_snapshot with commit=False does not commit outer transaction."""
    conn = repo.db.conn
    conn.execute("SAVEPOINT outer_ana_tx")
    conn.execute(
        "INSERT INTO settings(key, value) VALUES('ambient_marker_ana', 'initial')"
    )

    repo.save_monthly_snapshot(
        month="2025-01",
        start="2025-01-01",
        end="2025-01-31",
        notes="Teste isolamento",
        commit=False,
    )

    # Outer transaction is still active and can be rolled back cleanly
    conn.execute("ROLLBACK TO SAVEPOINT outer_ana_tx")
    conn.execute("RELEASE SAVEPOINT outer_ana_tx")

    row = repo.q("SELECT value FROM settings WHERE key='ambient_marker_ana'")
    assert not row


def test_c_repo_ana_01_save_monthly_snapshot_error_preserves_ambient_transaction(repo: Repo, monkeypatch):
    """An error in save_monthly_snapshot only rolls back its own savepoint, not outer transaction."""
    conn = repo.db.conn
    conn.execute("SAVEPOINT outer_ana_err")
    conn.execute(
        "INSERT INTO settings(key, value) VALUES('keep_this_record', 'preserved')"
    )

    # Force record_financial_state_snapshot to fail
    def fail_snapshot(*args, **kwargs):
        raise RuntimeError("simulated snapshot crash")

    monkeypatch.setattr(repo, "record_financial_state_snapshot", fail_snapshot)

    with pytest.raises(RuntimeError, match="simulated snapshot crash"):
        repo.save_monthly_snapshot(
            month="2025-01",
            start="2025-01-01",
            end="2025-01-31",
            notes="Teste erro",
            commit=False,
        )

    # Ambient record must still exist in connection
    row = repo.q("SELECT value FROM settings WHERE key='keep_this_record'")
    assert row and row[0]["value"] == "preserved"
    conn.commit()


def test_c_repo_ana_01_save_monthly_snapshot_default_commits_to_disk(tmp_path, monkeypatch):
    """save_monthly_snapshot with default commit=True persists data to disk for new connections."""
    monkeypatch.delenv("FINANCAS_PESSOAIS_DEMO_DATA", raising=False)
    db_file = tmp_path / "ana_commit_test.sqlite3"
    db = FinanceDB(db_file)
    r = Repo(db)
    r.save_monthly_snapshot(
        month="2025-02",
        start="2025-02-01",
        end="2025-02-28",
        notes="Snapshot committed to disk",
    )
    db.conn.close()

    raw_conn = sqlite3.connect(str(db_file))
    cur = raw_conn.execute("SELECT month, notes FROM monthly_snapshots WHERE month='2025-02'")
    row = cur.fetchone()
    raw_conn.close()

    assert row is not None
    assert row[0] == "2025-02"
    assert "Snapshot committed to disk" in row[1]


# ============================================================================
# 4. BUG-REPO-PLAN-01: Planning Mutators Transaction Isolation
# ============================================================================

def test_c_repo_plan_01_planning_write_preserves_ambient_transaction(repo: Repo):
    """_planning_write failure only rolls back inner savepoint, keeping ambient transaction."""
    conn = repo.db.conn
    conn.execute("SAVEPOINT ambient_plan_1")
    conn.execute("INSERT INTO settings(key, value) VALUES('plan_ambient', 'ok')")

    with pytest.raises(ZeroDivisionError):
        with repo._planning_write("test_savepoint_failure", capture_state=False):
            conn.execute("INSERT INTO settings(key, value) VALUES('inner_plan', 'fail')")
            _ = 1 / 0

    # Ambient insert must be intact; inner insert must be rolled back
    assert repo.q("SELECT value FROM settings WHERE key='plan_ambient'")[0]["value"] == "ok"
    assert not repo.q("SELECT value FROM settings WHERE key='inner_plan'")
    conn.commit()


def test_c_repo_plan_01_delete_scheduled_payment_preserves_ambient_transaction(repo: Repo):
    """delete_scheduled_payment failure does not commit before savepoint or abort ambient transaction."""
    conn = repo.db.conn
    conn.execute("SAVEPOINT ambient_plan_2")
    conn.execute("INSERT INTO settings(key, value) VALUES('sched_del_ambient', 'alive')")

    with pytest.raises(ValueError, match="Scheduled payment not found"):
        repo.delete_scheduled_payment(999999)

    assert repo.q("SELECT value FROM settings WHERE key='sched_del_ambient'")[0]["value"] == "alive"
    conn.commit()


def test_c_repo_plan_01_create_subscription_charge_preserves_ambient_transaction(repo: Repo, monkeypatch):
    """create_subscription_charge failure preserves ambient uncommitted changes."""
    conn = repo.db.conn
    conn.execute("SAVEPOINT ambient_plan_3")
    conn.execute("INSERT INTO settings(key, value) VALUES('sub_charge_ambient', 'alive')")

    # Monkeypatch _create_subscription_charge_uncommitted to fail
    def fail_sub(*args, **kwargs):
        raise RuntimeError("simulated sub failure")

    monkeypatch.setattr(repo, "_create_subscription_charge_uncommitted", fail_sub)

    with pytest.raises(RuntimeError, match="simulated sub failure"):
        repo.create_subscription_charge(
            subscription_id=1,
            due_date="2025-01-15",
            name="Netflix",
            amount=15.99,
            category="Subscrições",
        )

    assert repo.q("SELECT value FROM settings WHERE key='sub_charge_ambient'")[0]["value"] == "alive"
    conn.commit()


def test_c_repo_plan_01_create_scheduled_payment_charge_preserves_ambient_transaction(repo: Repo, monkeypatch):
    """create_scheduled_payment_charge failure preserves ambient uncommitted changes."""
    conn = repo.db.conn
    conn.execute("SAVEPOINT ambient_plan_4")
    conn.execute("INSERT INTO settings(key, value) VALUES('sched_charge_ambient', 'alive')")

    def fail_sched(*args, **kwargs):
        raise RuntimeError("simulated sched failure")

    monkeypatch.setattr(repo, "_create_scheduled_payment_charge_uncommitted", fail_sched)

    with pytest.raises(RuntimeError, match="simulated sched failure"):
        repo.create_scheduled_payment_charge(
            scheduled_id=1,
            due_date="2025-01-20",
            description="Ginásio",
            amount=35.0,
            category="Saúde",
            account="Conta Principal",
        )

    assert repo.q("SELECT value FROM settings WHERE key='sched_charge_ambient'")[0]["value"] == "alive"
    conn.commit()


def test_c_repo_plan_01_mutators_commit_on_success(tmp_path, monkeypatch):
    """Planning mutators commit on success and persist across connections."""
    monkeypatch.delenv("FINANCAS_PESSOAIS_DEMO_DATA", raising=False)
    db_file = tmp_path / "plan_commit_test.sqlite3"
    db = FinanceDB(db_file)
    r = Repo(db)

    r.db.conn.execute(
        """
        INSERT INTO scheduled_payments(id, name, category, amount, account, start_date, debit_day)
        VALUES(42, 'Clube de Ténis', 'Lazer', 50.0, 'Conta principal', '2025-01-01', 5)
        """
    )
    r.db.conn.commit()

    r.delete_scheduled_payment(42)
    db.conn.close()

    raw_conn = sqlite3.connect(str(db_file))
    cur = raw_conn.execute("SELECT id FROM scheduled_payments WHERE id=42")
    row = cur.fetchone()
    raw_conn.close()

    assert row is None


# ============================================================================
# 5. BUG-DB-RO-01: CancellableReadSnapshot Cleanup on Failure / Cancel
# ============================================================================

def test_c_db_ro_01_cancel_before_enter_raises_interrupted(tmp_path):
    """Cancelling before __enter__ raises OperationalError and creates no connection."""
    db_file = tmp_path / "ro_test.sqlite3"
    FinanceDB(db_file).conn.close()

    snap = CancellableReadSnapshot(db_file)
    snap.cancel()

    with pytest.raises(sqlite3.OperationalError, match="interrupted"):
        with snap:
            pass

    assert snap._connection is None


def test_c_db_ro_01_begin_failure_closes_connection_and_resets(tmp_path):
    """If connection.execute('BEGIN') fails, connection is closed and _connection is None."""
    db_file = tmp_path / "ro_begin_fail.sqlite3"
    FinanceDB(db_file).conn.close()

    snap = CancellableReadSnapshot(db_file)
    real_open = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
    proxy = MagicMock(wraps=real_open)
    proxy.execute.side_effect = sqlite3.OperationalError("database is locked")

    with patch("database.read_only.open_read_only_connection", return_value=proxy):
        with pytest.raises(sqlite3.OperationalError, match="database is locked"):
            with snap:
                pass

    assert snap._connection is None
    assert proxy.close.call_count == 1


def test_c_db_ro_01_cancel_during_begin_closes_connection(tmp_path):
    """If cancelled during __enter__, connection is closed immediately and _connection is None."""
    db_file = tmp_path / "ro_cancel_begin.sqlite3"
    FinanceDB(db_file).conn.close()

    snap = CancellableReadSnapshot(db_file)
    real_open = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
    proxy = MagicMock(wraps=real_open)

    def interrupt_and_fail(*args, **kwargs):
        snap.cancel()
        raise sqlite3.OperationalError("interrupted")

    proxy.execute.side_effect = interrupt_and_fail

    with patch("database.read_only.open_read_only_connection", return_value=proxy):
        with pytest.raises(sqlite3.OperationalError, match="interrupted"):
            with snap:
                pass

    assert snap._connection is None
    assert proxy.close.call_count == 1


def test_c_db_ro_01_happy_path_closes_cleanly(tmp_path):
    """Successful snapshot use closes cleanly in __exit__ and clears _connection."""
    db_file = tmp_path / "ro_happy.sqlite3"
    FinanceDB(db_file).conn.close()

    snap = CancellableReadSnapshot(db_file)
    with snap as conn:
        assert conn is not None
        assert snap._connection is conn
        cur = conn.execute("SELECT 42")
        assert cur.fetchone()[0] == 42

    assert snap._connection is None


# ============================================================================
# 6. BUG-UDF-01: AppStateStore Thread-Safety & Resilience
# ============================================================================

def test_c_udf_01_store_has_rlock():
    """AppStateStore must hold a threading.RLock for thread safety."""
    store = AppStateStore(AppState())
    assert hasattr(store, "_lock")
    assert isinstance(store._lock, type(threading.RLock()))


def test_c_udf_01_subscribe_and_unsubscribe():
    """AppStateStore supports subscribe and unsubscribe without duplicate calls."""
    store = AppStateStore(AppState())
    notifications = []

    def on_change(state: AppState):
        notifications.append(state.portfolio_equity)

    store.subscribe(on_change)
    store.dispatch(Action("UPDATE_MARKET_TICK", {"new_equity": 80000.0}))
    assert notifications == [80000.0]

    store.unsubscribe(on_change)
    store.dispatch(Action("UPDATE_MARKET_TICK", {"new_equity": 85000.0}))
    # Unsubscribed listener was not invoked
    assert notifications == [80000.0]


def test_c_udf_01_failing_subscriber_isolated():
    """A failing subscriber must not stop subsequent subscribers from receiving notification."""
    store = AppStateStore(AppState())
    successful = []

    def bad_subscriber(state: AppState):
        raise ValueError("Listener exploded")

    def good_subscriber(state: AppState):
        successful.append(state.portfolio_equity)

    store.subscribe(bad_subscriber)
    store.subscribe(good_subscriber)

    store.dispatch(Action("UPDATE_MARKET_TICK", {"new_equity": 95000.0}))
    assert successful == [95000.0]


def test_c_udf_01_reentrant_dispatch():
    """A subscriber dispatching a secondary action must not deadlock due to RLock."""
    store = AppStateStore(AppState(portfolio_equity=50000.0))
    trace = []

    def cascading_listener(state: AppState):
        trace.append(state.portfolio_equity)
        if state.portfolio_equity == 60000.0:
            store.dispatch(Action("UPDATE_MARKET_TICK", {"new_equity": 70000.0}))

    store.subscribe(cascading_listener)
    store.dispatch(Action("UPDATE_MARKET_TICK", {"new_equity": 60000.0}))
    assert trace == [60000.0, 70000.0]
    assert store.state.portfolio_equity == 70000.0


def test_c_udf_01_multithreaded_concurrent_dispatches():
    """Concurrent dispatches from multiple threads execute safely without race conditions."""
    store = AppStateStore(AppState(portfolio_cash=10000.0, portfolio_equity=50000.0))
    errors = []

    def worker(worker_id: int):
        try:
            for _ in range(10):
                store.dispatch(Action("UPDATE_MARKET_TICK", {"new_equity": 50000.0 + worker_id}))
                _ = store.state
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors


# ============================================================================
# 7. BUG-UDF-02: App Reducer Clamping DEPLOY_DRY_POWDER_TRANCHE
# ============================================================================

def test_c_udf_02_clamp_to_available_cash_preserves_total_wealth():
    """DEPLOY_DRY_POWDER_TRANCHE clamps actual deployment to available cash (no free money)."""
    initial = AppState(portfolio_cash=25000.0, portfolio_equity=75000.0, tax_lots_count=5)
    total_wealth_before = initial.portfolio_cash + initial.portfolio_equity

    # Case A: Normal deploy (amount <= cash)
    s1 = app_reducer(initial, Action("DEPLOY_DRY_POWDER_TRANCHE", {"amount": 10000.0}))
    assert s1.portfolio_cash == 15000.0
    assert s1.portfolio_equity == 85000.0
    assert s1.portfolio_cash + s1.portfolio_equity == total_wealth_before
    assert s1.tax_lots_count == 6

    # Case B: Overdraft deploy (amount > cash) -> MUST clamp to available cash 25000.0
    s2 = app_reducer(initial, Action("DEPLOY_DRY_POWDER_TRANCHE", {"amount": 60000.0}))
    assert s2.portfolio_cash == 0.0
    assert s2.portfolio_equity == 100000.0  # NOT 75000 + 60000 = 135000!
    assert s2.portfolio_cash + s2.portfolio_equity == total_wealth_before
    assert s2.tax_lots_count == 6

    # Case C: Zero cash available -> zero deployed, no tax lot created
    zero_cash_state = AppState(portfolio_cash=0.0, portfolio_equity=100000.0, tax_lots_count=5)
    s3 = app_reducer(zero_cash_state, Action("DEPLOY_DRY_POWDER_TRANCHE", {"amount": 5000.0}))
    assert s3.portfolio_cash == 0.0
    assert s3.portfolio_equity == 100000.0
    assert s3.tax_lots_count == 5

    # Case D: Negative amount requested -> zero deployed, cash and equity conserved
    s4 = app_reducer(initial, Action("DEPLOY_DRY_POWDER_TRANCHE", {"amount": -5000.0}))
    assert s4.portfolio_cash == 25000.0
    assert s4.portfolio_equity == 75000.0
    assert s4.tax_lots_count == 5


def test_c_udf_02_edge_cases_none_and_invalid_payload():
    """app_reducer gracefully handles None, non-numeric, or missing amounts."""
    initial = AppState(portfolio_cash=20000.0, portfolio_equity=80000.0, tax_lots_count=3)
    total_wealth = 100000.0

    # None amount
    s1 = app_reducer(initial, Action("DEPLOY_DRY_POWDER_TRANCHE", {"amount": None}))
    assert s1.portfolio_cash == 20000.0
    assert s1.portfolio_equity == 80000.0
    assert s1.tax_lots_count == 3
    assert s1.portfolio_cash + s1.portfolio_equity == total_wealth

    # Non-numeric string amount
    s2 = app_reducer(initial, Action("DEPLOY_DRY_POWDER_TRANCHE", {"amount": "not_a_number"}))
    assert s2.portfolio_cash == 20000.0
    assert s2.portfolio_equity == 80000.0
    assert s2.tax_lots_count == 3

    # Empty payload
    s3 = app_reducer(initial, Action("DEPLOY_DRY_POWDER_TRANCHE", {}))
    assert s3.portfolio_cash == 20000.0
    assert s3.portfolio_equity == 80000.0
    assert s3.tax_lots_count == 3

    # UPDATE_MARKET_TICK with None
    s4 = app_reducer(initial, Action("UPDATE_MARKET_TICK", {"new_equity": None, "drawdown": None}))
    assert s4.portfolio_equity == 80000.0
    assert s4.market_drawdown == 0.0

