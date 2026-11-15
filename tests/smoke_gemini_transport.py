#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Offline checks for Gemini cancellation, retries and global deadlines."""

from __future__ import annotations

import json
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QEventLoop, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from services.ai import GeminiClient, PreparedGeminiCall  # noqa: E402
from ui.workers import AsyncTask  # noqa: E402


class FakeResponse:
    def __init__(
        self,
        status: int,
        payload: dict[str, Any],
        headers: dict[str, str] | None = None,
    ):
        self.status = status
        self._body = json.dumps(payload).encode("utf-8")
        self._read = False
        self._headers = headers or {}

    def read(self, size: int = -1) -> bytes:
        if self._read:
            return b""
        self._read = True
        return self._body

    def getheader(self, name: str) -> str | None:
        return self._headers.get(name)


class FakeConnection:
    def __init__(self, response: FakeResponse):
        self.response = response
        self.closed = False
        self.request_data: tuple[Any, ...] | None = None
        self.sock = None

    def request(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.request_data = (method, path, body, headers)

    def getresponse(self) -> FakeResponse:
        return self.response

    def close(self) -> None:
        self.closed = True


class BlockingConnection:
    def __init__(self):
        self.closed = threading.Event()
        self.sock = None

    def request(self, *args: Any, **kwargs: Any) -> None:
        return None

    def getresponse(self) -> FakeResponse:
        self.closed.wait(5)
        raise OSError("connection closed")

    def close(self) -> None:
        self.closed.set()


class SlowTimeoutConnection:
    sock = None

    def request(self, *args: Any, **kwargs: Any) -> None:
        return None

    def getresponse(self) -> FakeResponse:
        time.sleep(0.22)
        raise socket.timeout("simulated timeout")

    def close(self) -> None:
        return None


def check_retry() -> int:
    responses = [
        FakeResponse(503, {"error": {"message": "temporarily unavailable"}}),
        FakeResponse(
            200,
            {"candidates": [{"content": {"parts": [{"text": "resposta após retry"}]}}]},
        ),
    ]
    connections: list[FakeConnection] = []

    def factory(host: str, timeout: float) -> FakeConnection:
        connection = FakeConnection(responses[len(connections)])
        connections.append(connection)
        return connection

    client = GeminiClient(
        "secret-key",
        "gemini-test",
        connection_factory=factory,
        request_timeout=2,
        max_attempts=3,
        retry_base_delay=0.01,
    )
    result = client.generate("teste")
    if result != "resposta após retry" or len(connections) != 2:
        raise AssertionError("Transient HTTP failure was not retried correctly")
    if not all(connection.closed for connection in connections):
        raise AssertionError("Retry connections were not closed")
    return len(connections)


def check_hard_cancel() -> float:
    # The smoke also runs in the widget suite: a QCoreApplication singleton
    # would prevent every later QWidget from being constructed safely.
    app = QApplication.instance() or QApplication([])
    created: list[BlockingConnection] = []

    def factory(host: str, timeout: float) -> BlockingConnection:
        connection = BlockingConnection()
        created.append(connection)
        return connection

    client = GeminiClient(
        "secret-key",
        "gemini-test",
        connection_factory=factory,
        request_timeout=30,
        max_attempts=1,
    )
    call = PreparedGeminiCall(client, lambda: client.generate("teste"))
    task = AsyncTask()
    loop = QEventLoop()
    state = {"cancelled": None, "timeout": False}
    task.finished.connect(
        lambda cancelled: (
            state.__setitem__("cancelled", cancelled),
            loop.quit(),
        )
    )
    timeout = QTimer()
    timeout.setSingleShot(True)
    timeout.timeout.connect(
        lambda: (
            state.__setitem__("timeout", True),
            loop.quit(),
        )
    )
    timeout.start(2000)

    started = time.monotonic()
    task.start(call)
    QTimer.singleShot(40, task.cancel)
    loop.exec()
    elapsed = time.monotonic() - started
    timeout.stop()

    if state["timeout"] or state["cancelled"] is not True:
        raise AssertionError(f"Hard cancellation failed: {state}")
    if elapsed >= 1.0:
        raise AssertionError(f"Connection cancellation took too long: {elapsed:.2f}s")
    if not created or not created[0].closed.is_set():
        raise AssertionError("Active HTTP connection was not closed")
    app.processEvents()
    return elapsed


def check_global_timeout() -> float:
    client = GeminiClient(
        "secret-key",
        "gemini-test",
        connection_factory=lambda host, timeout: SlowTimeoutConnection(),
        request_timeout=1,
        max_attempts=20,
        retry_base_delay=0,
    )
    started = time.monotonic()
    try:
        client.generate("teste")
    except RuntimeError as exc:
        message = str(exc)
    else:
        raise AssertionError("Global timeout did not fail")
    elapsed = time.monotonic() - started
    if "limite global" not in message:
        raise AssertionError(f"Unexpected timeout message: {message}")
    if elapsed > 2.0:
        raise AssertionError(f"Global timeout exceeded its bound: {elapsed:.2f}s")
    return elapsed


def test_retry() -> None:
    check_retry()


def test_hard_cancel() -> None:
    check_hard_cancel()
    assert isinstance(QApplication.instance(), QApplication)


def test_global_timeout() -> None:
    check_global_timeout()


def main() -> int:
    attempts = check_retry()
    cancel_elapsed = check_hard_cancel()
    timeout_elapsed = check_global_timeout()
    print("Gemini transport smoke OK")
    print("retry_attempts", attempts)
    print("hard_cancel_seconds", f"{cancel_elapsed:.3f}")
    print("global_timeout_seconds", f"{timeout_elapsed:.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
