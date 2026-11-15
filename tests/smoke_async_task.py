#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Smoke checks for responsive and cancellable Qt background tasks."""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QCoreApplication, QEventLoop, QTimer  # noqa: E402

from ui.workers import AsyncTask  # noqa: E402


def execute(callback: Any, *, cancel_after_ms: int = 0) -> dict[str, Any]:
    loop = QEventLoop()
    task = AsyncTask()
    state: dict[str, Any] = {"ticks": 0, "result": None, "error": "", "cancelled": None}

    heartbeat = QTimer()
    heartbeat.setInterval(5)
    heartbeat.timeout.connect(lambda: state.__setitem__("ticks", state["ticks"] + 1))
    heartbeat.start()

    timeout = QTimer()
    timeout.setSingleShot(True)
    timeout.timeout.connect(
        lambda: (state.__setitem__("error", "timeout"), loop.quit())
    )
    timeout.start(3000)

    task.result.connect(lambda value: state.__setitem__("result", value))
    task.error.connect(lambda message: state.__setitem__("error", message))
    task.finished.connect(
        lambda cancelled: (
            state.__setitem__("cancelled", cancelled),
            loop.quit(),
        )
    )

    if not task.start(callback):
        raise AssertionError("AsyncTask refused its first callback")
    if cancel_after_ms:
        QTimer.singleShot(cancel_after_ms, task.cancel)
    loop.exec()
    heartbeat.stop()
    timeout.stop()
    return state


def main() -> int:
    app = QCoreApplication.instance() or QCoreApplication([])

    completed = execute(lambda: (time.sleep(0.12), 42)[1])
    if completed["result"] != 42 or completed["error"]:
        raise AssertionError(f"Unexpected async result: {completed}")
    if completed["ticks"] < 5:
        raise AssertionError("Qt event loop was blocked by the background task")
    if completed["cancelled"] is not False:
        raise AssertionError("Completed task was reported as cancelled")

    cancelled = execute(
        lambda: (time.sleep(0.12), "late result")[1], cancel_after_ms=10
    )
    if cancelled["cancelled"] is not True or cancelled["result"] is not None:
        raise AssertionError(f"Cancellation did not suppress the result: {cancelled}")

    failed = execute(lambda: (_ for _ in ()).throw(ValueError("expected failure")))
    if "expected failure" not in failed["error"]:
        raise AssertionError(f"Worker error was not propagated: {failed}")

    print("Async task smoke OK")
    print("responsive_ticks", completed["ticks"])
    print("cancelled_result_suppressed", cancelled["result"] is None)
    app.quit()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
