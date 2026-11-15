#!/usr/bin/env python3
"""Measure a clean application launch in a fresh Python/Qt process."""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from time import perf_counter


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

os.environ["QT_QPA_PLATFORM"] = "offscreen"
created_state = ""
if not os.environ.get("FINANCAS_PESSOAIS_STATE_DIR", "").strip():
    created_state = tempfile.mkdtemp(prefix="financas-startup-probe-")
    os.environ["FINANCAS_PESSOAIS_STATE_DIR"] = created_state


def main() -> int:
    import_started = perf_counter()
    from PySide6.QtCore import QEventLoop, QTimer
    from PySide6.QtWidgets import QApplication

    from main import LiquidFinanceApp
    from ui.pages.dashboard import DashboardPage

    import_seconds = perf_counter() - import_started
    calls = {"dashboard_resize": 0, "budget_sync": 0, "content_sync": 0}
    original_resize = DashboardPage.resizeEvent
    original_budget_sync = DashboardPage._sync_budget_card_height
    original_content_sync = LiquidFinanceApp.sync_content_size

    def counted_resize(self, event):
        calls["dashboard_resize"] += 1
        return original_resize(self, event)

    def counted_budget_sync(self):
        calls["budget_sync"] += 1
        return original_budget_sync(self)

    def counted_content_sync(self):
        calls["content_sync"] += 1
        return original_content_sync(self)

    DashboardPage.resizeEvent = counted_resize
    DashboardPage._sync_budget_card_height = counted_budget_sync
    LiquidFinanceApp.sync_content_size = counted_content_sync

    app = QApplication.instance() or QApplication([])
    window = None
    try:
        created_at = perf_counter()
        window = LiquidFinanceApp()
        constructor_seconds = perf_counter() - created_at
        window.repo.set_setting("backup_on_close", "0")
        window.backup_service.run_backup_on_close = lambda silent=True: None
        window.resize(1280, 900)
        window.show()

        pulses = 0
        pulse_times: list[float] = []
        heartbeat = QTimer(window)
        heartbeat.setInterval(25)

        def record_pulse() -> None:
            nonlocal pulses
            pulses += 1
            pulse_times.append(perf_counter())

        heartbeat.timeout.connect(record_pulse)
        heartbeat.start()
        observation_started = perf_counter()
        observation_loop = QEventLoop()
        QTimer.singleShot(900, observation_loop.quit)
        observation_loop.exec()
        observation_finished = perf_counter()
        observation_seconds = observation_finished - observation_started
        heartbeat.stop()
        heartbeat_points = [observation_started, *pulse_times, observation_finished]
        max_event_loop_gap_seconds = max(
            right - left for left, right in zip(heartbeat_points, heartbeat_points[1:])
        )

        dashboard = window.pages["dashboard"]
        result = {
            "import_seconds": import_seconds,
            "constructor_seconds": constructor_seconds,
            "observation_seconds": observation_seconds,
            "event_loop_pulses": pulses,
            "max_event_loop_gap_seconds": max_event_loop_gap_seconds,
            "dashboard_resize_calls": calls["dashboard_resize"],
            "budget_sync_calls": calls["budget_sync"],
            "content_sync_calls": calls["content_sync"],
            "dashboard_height": dashboard.height(),
            "stack_height": window.stack.height(),
        }
        print(json.dumps(result))
        return 0
    finally:
        if window is not None:
            try:
                window.auto_backup_timer.stop()
                window.subscription_reminder_timer.stop()
                window.pages["dashboard"].shutdown_workers()
                window.close()
                app.processEvents()
                window.repo.db.conn.close()
            except Exception:
                pass
        try:
            from utils.app_logging import shutdown_logging

            shutdown_logging()
        except Exception:
            pass
        if created_state:
            shutil.rmtree(created_state, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
