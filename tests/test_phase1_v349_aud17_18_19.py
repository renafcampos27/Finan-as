"""Unit and integration tests for AUD-17, AUD-18, and AUD-19 fixes in v3.49.0."""

from __future__ import annotations
import time
import pathlib
from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication
from services.finance_math import cornish_fisher_var
from ui.workers import AsyncTask

APP = None

def _drain_qt_events(timeout_ms: int = 150) -> None:
    global APP
    if APP is None:
        APP = QApplication.instance() or QApplication([])
    loop = QEventLoop()
    QTimer.singleShot(timeout_ms, loop.quit)
    loop.exec()


def test_aud17_application_sources_compile_on_supported_python():
    """Use the supported interpreter; Python 3.14 permits unparenthesized exception tuples."""
    base = pathlib.Path(__file__).resolve().parent.parent
    sources = [base / "main.py", *base.glob("*.pyw")]
    for directory in ("database", "domain", "services", "ui", "utils", "scripts", "motores_matematicos_simulacao"):
        sources.extend((base / directory).rglob("*.py"))
    assert sources
    for source in sources:
        compile(source.read_bytes(), str(source), "exec")


def test_aud18_cornish_fisher_var_negative_skewness_increases_tail_risk():
    """Verify that negative skewness (crash-prone series) strictly elevates tail loss in Cornish-Fisher VaR."""
    # Symmetric series (mean 0.5%, std 2.0%, skew ~0)
    sym_series = [-3.5, -2.0, -1.0, 0.0, 1.0, 2.0, 3.5, 4.0]
    
    # Left-tail crash series (negative skewness)
    crash_series = [-25.0, -18.0, -10.0, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0]
    
    # Right-tail positive skew series
    positive_skew_series = [-3.5, -2.5, -1.5, 0.0, 1.0, 1.5, 2.0, 10.0, 18.0, 25.0]
    
    var_sym = cornish_fisher_var(sym_series, confidence=0.95)
    var_crash = cornish_fisher_var(crash_series, confidence=0.95)
    var_pos = cornish_fisher_var(positive_skew_series, confidence=0.95)
    
    assert var_crash > var_sym, (
        f"Crash series VaR ({var_crash:.2f}%) must be strictly greater than symmetric series VaR ({var_sym:.2f}%)"
    )
    assert var_crash > var_pos, (
        f"Crash series VaR ({var_crash:.2f}%) must be strictly greater than positive skew series VaR ({var_pos:.2f}%)"
    )
    assert var_sym > 0.0
    assert var_crash > 15.0  # Significant double-digit tail loss for -25% crash


def test_aud19_async_task_is_running_reflects_actual_lifecycle():
    """Verify AsyncTask.is_running() accurately returns False when unstarted and after finishing."""
    _drain_qt_events(10)
    task = AsyncTask(label="TestTaskLifecycle")
    
    # 1. Unstarted task must not be running
    assert not task.is_running()
    
    # 2. Start a short task
    finished_events = []
    task.finished.connect(lambda cancelled: finished_events.append(cancelled))
    
    def quick_worker():
        time.sleep(0.04)
        return "result_ok"
        
    started = task.start(quick_worker)
    assert started is True
    
    # 3. Wait for execution and drain Qt events
    for _ in range(30):
        _drain_qt_events(20)
        if finished_events:
            break
        time.sleep(0.02)
        
    assert len(finished_events) == 1
    # 4. After completion, is_running must return False
    assert not task.is_running()
