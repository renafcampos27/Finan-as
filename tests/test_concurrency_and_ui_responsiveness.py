from __future__ import annotations

import time

from PySide6.QtCore import QThread
from PySide6.QtWidgets import QApplication

from services.financial_intelligence import monte_carlo_projection
from ui.workers import AsyncTask, BackgroundTaskRegistry


def test_async_task_is_running_and_completion():
    # QApplication (not QCoreApplication): the rest of the suite creates a
    # QApplication, and mixing app instance types across tests destroys and
    # recreates the Qt application around live QThreads, which crashes the
    # process natively (0xC0000409) on PySide6/Python 3.14.
    app = QApplication.instance() or QApplication([])
    registry = BackgroundTaskRegistry()
    task = AsyncTask(registry=registry)

    assert not task.is_running()

    results = []
    task.result.connect(lambda v: results.append(v))

    def compute_work():
        time.sleep(0.05)
        return {"value": 42}

    started = task.start(compute_work)
    assert started
    assert task.is_running()

    # Wait for completion
    timeout = time.time() + 2.0
    while task.is_running() and time.time() < timeout:
        app.processEvents()
        QThread.msleep(10)

    assert not task.is_running()
    assert len(results) == 1
    assert results[0]["value"] == 42


def test_background_task_registry_cancellation():
    registry = BackgroundTaskRegistry()

    cancelled = []

    def cancel_hook():
        cancelled.append(True)

    thread = QThread()
    registry.register_thread(thread, label="Test Thread", cancel=cancel_hook)

    assert registry.has_active_tasks()
    assert registry.active_count() == 1
    assert "Test Thread" in registry.active_labels()

    # Cancel all
    count = registry.cancel_all()
    assert count == 1
    assert len(cancelled) == 1

    # Prune/release
    registry._release_thread(thread)
    assert registry.active_count() == 0


def test_monte_carlo_deterministic_simulation():
    res1 = monte_carlo_projection(
        contribution=200.0,
        annual_return_pct=7.0,
        annual_volatility_pct=15.0,
        years=10,
        simulations=500,
        seed=1234,
    )
    res2 = monte_carlo_projection(
        contribution=200.0,
        annual_return_pct=7.0,
        annual_volatility_pct=15.0,
        years=10,
        simulations=500,
        seed=1234,
    )

    final1 = res1["rows"][-1]
    final2 = res2["rows"][-1]
    assert final1["p50"] == final2["p50"]
    assert final1["p10"] == final2["p10"]
    assert final1["p90"] == final2["p90"]
    assert final1["p10"] <= final1["p50"] <= final1["p90"]
    assert len(res1["rows"]) == 10
