#!/usr/bin/env python3
"""Exercise timer shutdown and background backup in a fresh Qt process."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import threading
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ.setdefault("FINANCAS_PESSOAIS_TESTING", "1")
created_state = ""
if not os.environ.get("FINANCAS_PESSOAIS_STATE_DIR", "").strip():
    created_state = tempfile.mkdtemp(prefix="financas-runtime-probe-")
    os.environ["FINANCAS_PESSOAIS_STATE_DIR"] = created_state


def _new_window():
    from PySide6.QtWidgets import QApplication

    from main import LiquidFinanceApp

    app = QApplication.instance() or QApplication([])
    # The probe deliberately does not show its top-level window.  Prevent Qt
    # from treating that headless state as a request to quit the nested event
    # loop before the heartbeat and background task have been observed.
    app.setQuitOnLastWindowClosed(False)
    window = LiquidFinanceApp()
    window.repo.set_setting("backup_on_close", "0")
    window.backup_service.run_backup_on_close = lambda silent=True: None
    window.auto_backup_timer.stop()
    return app, window


def _dispose(app, window) -> None:
    try:
        window.auto_backup_timer.stop()
        window.subscription_reminder_timer.stop()
        window.pages["dashboard"].shutdown_workers()
        window.close()
        app.processEvents()
    finally:
        try:
            window.repo.db.conn.close()
        except Exception:
            pass


def timer_probe() -> dict[str, object]:
    from PySide6.QtCore import QEventLoop, QTimer

    app, window = _new_window()
    fired: list[bool] = []
    window.subscription_reminder_timer.stop()
    window.subscription_reminder_timer.timeout.disconnect()
    window.subscription_reminder_timer.timeout.connect(lambda: fired.append(True))
    window.subscription_reminder_timer.start(40)
    try:
        window.close()
        observation_loop = QEventLoop()
        QTimer.singleShot(100, observation_loop.quit)
        observation_loop.exec()
        return {
            "fired_count": len(fired),
            "timer_active": window.subscription_reminder_timer.isActive(),
            "closing": window._closing,
        }
    finally:
        _dispose(app, window)


def background_backup_probe() -> dict[str, object]:
    from PySide6.QtCore import QEventLoop, QTimer

    from services.backup import BackupService

    app, window = _new_window()
    # Measure the backup in isolation from the deferred first layout/style
    # pass queued by window construction.  In normal use that pass completes
    # when the window appears, long before the 60-second backup timer fires.
    for _ in range(3):
        app.processEvents()
    original = BackupService.run_scheduled_backup_for_database
    worker_release = threading.Event()

    def slow_backup(_database_file, *, silent=True):
        # Keep work active until the GUI has demonstrably processed heartbeats.
        # A fixed 350ms/500ms race fails under unrelated build/CI scheduling load.
        worker_release.wait(10)
        return None

    BackupService.run_scheduled_backup_for_database = staticmethod(slow_backup)
    pulses = 0
    heartbeat = QTimer(window)
    heartbeat.setInterval(20)

    def pulse() -> None:
        nonlocal pulses
        if window.auto_backup_task.is_running() and not worker_release.is_set():
            pulses += 1
            if pulses >= 8:
                worker_release.set()

    heartbeat.timeout.connect(pulse)
    heartbeat.start()
    observation_loop = QEventLoop()
    window.auto_backup_task.finished.connect(observation_loop.quit)
    watchdog = QTimer(window)
    watchdog.setSingleShot(True)
    watchdog.timeout.connect(observation_loop.quit)
    try:
        window._start_auto_backup_check()
        started = window.auto_backup_task.is_running()
        watchdog.start(5000)
        observation_loop.exec()
        return {
            "task_started": started,
            "event_loop_pulses": pulses,
            "task_running_after_wait": window.auto_backup_task.is_running(),
        }
    finally:
        heartbeat.stop()
        watchdog.stop()
        worker_release.set()
        BackupService.run_scheduled_backup_for_database = original
        _dispose(app, window)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("probe", choices=("timer", "background-backup"))
    args = parser.parse_args()
    try:
        result = timer_probe() if args.probe == "timer" else background_backup_probe()
        print(json.dumps(result))
        return 0
    finally:
        try:
            from utils.app_logging import shutdown_logging

            shutdown_logging()
        except Exception:
            pass
        if created_state:
            shutil.rmtree(created_state, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
