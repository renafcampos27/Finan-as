from __future__ import annotations

import threading
from unittest.mock import MagicMock
from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication

from services.ai import PreparedGeminiCall
from ui.workers import AsyncTask, BackgroundTaskRegistry, _SQLITE_SHARED_CONNECTION_LOCK


APP = None


def _drain_qt_events(timeout_ms: int = 200) -> None:
    global APP
    if APP is None:
        APP = QApplication.instance() or QApplication([])
    loop = QEventLoop()
    QTimer.singleShot(timeout_ms, loop.quit)
    loop.exec()


def test_ai_prepared_call_has_requires_sqlite_lock_false() -> None:
    dummy_client = MagicMock()
    call = PreparedGeminiCall(dummy_client, lambda: "OK")
    assert hasattr(call, "requires_sqlite_lock")
    assert call.requires_sqlite_lock is False


def test_ai_task_executes_and_cancels_without_sqlite_lock_blockage() -> None:
    global APP
    if APP is None:
        APP = QApplication.instance() or QApplication([])

    registry = BackgroundTaskRegistry()
    task = AsyncTask(registry=registry, label="AI-Test")

    lock_held = threading.Event()
    release_lock = threading.Event()

    def _lock_holder():
        with _SQLITE_SHARED_CONNECTION_LOCK:
            lock_held.set()
            release_lock.wait(timeout=2.0)

    holder_thread = threading.Thread(target=_lock_holder, daemon=True)
    holder_thread.start()
    assert lock_held.wait(timeout=1.0)

    try:
        ai_executed = threading.Event()

        class DummyAICall:
            requires_sqlite_lock = False

            def __init__(self):
                self._cancelled = False

            def __call__(self):
                ai_executed.set()
                return "AI_ANSWER"

            def cancel(self):
                self._cancelled = True

        call = DummyAICall()
        results: list[str] = []
        task.result.connect(results.append)
        started = task.start(call)
        assert started is True
        assert ai_executed.wait(timeout=1.0)
        _drain_qt_events(100)
        assert results == ["AI_ANSWER"]
    finally:
        release_lock.set()
        holder_thread.join(timeout=1.0)


def test_async_task_cancellation_during_lock_contention() -> None:
    global APP
    if APP is None:
        APP = QApplication.instance() or QApplication([])
    registry = BackgroundTaskRegistry()
    task = AsyncTask(registry=registry, label="Contended-Lock")

    lock_held = threading.Event()
    release_lock = threading.Event()

    def _lock_holder():
        with _SQLITE_SHARED_CONNECTION_LOCK:
            lock_held.set()
            release_lock.wait(timeout=2.0)

    holder_thread = threading.Thread(target=_lock_holder, daemon=True)
    holder_thread.start()
    assert lock_held.wait(timeout=1.0)

    try:
        task_started = threading.Event()

        def _db_heavy_task():
            task_started.set()
            return "DONE"

        started = task.start(_db_heavy_task)
        assert started is True
        assert task.is_running() is True
        cancelled = task.cancel()
        assert cancelled is True
        _drain_qt_events(200)
        assert not task_started.is_set()
    finally:
        release_lock.set()
        holder_thread.join(timeout=1.0)


def test_monte_carlo_pending_requeue_mechanism() -> None:
    class DummyInvestmentsPage:
        def __init__(self):
            self._monte_carlo_pending = False
            self.recalc_count = 0

        def _update_monte_carlo(self):
            self.recalc_count += 1

        def _on_monte_carlo_finished(self, _cancelled: bool):
            if getattr(self, "_monte_carlo_pending", False):
                self._monte_carlo_pending = False
                self._update_monte_carlo()

    page = DummyInvestmentsPage()
    page._monte_carlo_pending = True
    page._on_monte_carlo_finished(_cancelled=True)
    assert page.recalc_count == 1
    assert page._monte_carlo_pending is False
