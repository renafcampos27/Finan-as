"""Reusable Qt background task runner for blocking callables."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import threading
from typing import Any

from PySide6.QtCore import (
    QCoreApplication,
    QElapsedTimer,
    QEventLoop,
    QObject,
    QThread,
    Qt,
    Signal,
    Slot,
)

from utils.app_logging import log_exception

_JOBS_LOCK = threading.Lock()
_ACTIVE_JOBS: dict[QThread, QObject] = {}

# Service-level guard: analytics/repo callables may only execute one at a time
# app-wide.  Qt workers run on their own QThreads while the GUI thread keeps
# issuing queries on the same ``FinanceDB`` connection (opened with
# ``check_same_thread=False``); without this gate a worker query can interleave
# with a GUI query on that shared connection and corrupt its statement state.
_SQLITE_SHARED_CONNECTION_LOCK = threading.RLock()


@dataclass(frozen=True)
class _RegisteredTask:
    thread: QThread
    label: str
    cancel: Callable[[], Any] | None
    allow_during_shutdown: bool


class BackgroundTaskRegistry(QObject):
    """Track and stop every background thread owned by one app window."""

    active_changed = Signal(int)
    all_finished = Signal()

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._lock = threading.RLock()
        self._tasks: dict[QThread, _RegisteredTask] = {}
        self._accepting_tasks = True
        self._maintenance_active = False

    @property
    def is_shutting_down(self) -> bool:
        with self._lock:
            return not self._accepting_tasks

    def can_start(self, *, allow_during_shutdown: bool = False) -> bool:
        with self._lock:
            if self._maintenance_active:
                return False
            return self._accepting_tasks or bool(allow_during_shutdown)

    def register_thread(
        self,
        thread: QThread,
        *,
        label: str,
        cancel: Callable[[], Any] | None = None,
        allow_during_shutdown: bool = False,
    ) -> bool:
        """Register a QThread before it starts and remove it after it finishes."""
        with self._lock:
            if thread in self._tasks:
                return True
            if not self.can_start(allow_during_shutdown=allow_during_shutdown):
                return False
            entry = _RegisteredTask(
                thread=thread,
                label=str(label or "Tarefa em segundo plano").strip(),
                cancel=cancel,
                allow_during_shutdown=bool(allow_during_shutdown),
            )
            self._tasks[thread] = entry
            thread.finished.connect(
                self._release_finished_sender,
                type=Qt.QueuedConnection,
            )
            count = len(self._tasks)
        self.active_changed.emit(count)
        return True

    @Slot()
    def _release_thread(self, thread: QThread | None = None) -> None:
        if thread is None:
            return
        with self._lock:
            removed = self._tasks.pop(thread, None)
            if removed is None:
                return
            count = len(self._tasks)
        with _JOBS_LOCK:
            _ACTIVE_JOBS.pop(thread, None)
        self.active_changed.emit(count)
        if count == 0:
            self.all_finished.emit()

    @Slot()
    def _release_finished_sender(self) -> None:
        """Release a finished thread only in the registry's Qt thread."""

        sender = self.sender()
        if isinstance(sender, QThread):
            self._release_thread(sender)

    def _prune_finished(self) -> None:
        with self._lock:
            finished: list[QThread] = []
            for thread in list(self._tasks.keys()):
                try:
                    if thread.isFinished() and not thread.isRunning():
                        finished.append(thread)
                except RuntimeError:
                    # A Qt owner may already have processed deleteLater. Treat the
                    # invalid wrapper as finished instead of touching C++ again.
                    finished.append(thread)
            for thread in finished:
                self._tasks.pop(thread, None)

    def active_count(self) -> int:
        with self._lock:
            self._prune_finished()
            return len(self._tasks)

    def has_active_tasks(self) -> bool:
        return self.active_count() > 0

    def active_labels(self) -> tuple[str, ...]:
        with self._lock:
            self._prune_finished()
            labels: list[str] = []
            for entry in self._tasks.values():
                if entry.label not in labels:
                    labels.append(entry.label)
            return tuple(labels)

    def cancel_all(self, *, include_shutdown_tasks: bool = True) -> int:
        """Request cooperative cancellation without terminating any thread."""

        with self._lock:
            self._prune_finished()
            entries = tuple(self._tasks.values())

        cancelled = 0
        for entry in entries:
            if not include_shutdown_tasks and entry.allow_during_shutdown:
                continue
            try:
                if callable(entry.cancel):
                    entry.cancel()
                else:
                    entry.thread.requestInterruption()
                cancelled += 1
            except (RuntimeError, OSError) as exc:
                log_exception(
                    f"Failed to cancel background task: {entry.label}",
                    exc,
                )
        return cancelled

    def begin_shutdown(self) -> int:
        """Reject new ordinary jobs and cancel every job already in progress."""

        self._accepting_tasks = False
        self._maintenance_active = False
        return self.cancel_all(include_shutdown_tasks=False)

    def begin_maintenance(self) -> int:
        """Stop all data-bound jobs, including backup tasks, before a restore."""

        self._accepting_tasks = False
        self._maintenance_active = True
        return self.cancel_all(include_shutdown_tasks=True)

    def wait_for_idle(self, timeout_ms: int = 15_000) -> bool:
        """Wait for cooperative cancellation while keeping the GUI responsive."""

        timer = QElapsedTimer()
        timer.start()
        timeout = max(0, int(timeout_ms))
        while self.has_active_tasks():
            app = QCoreApplication.instance()
            if app is not None:
                app.processEvents(QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)
            if timer.elapsed() >= timeout:
                return False
            QThread.msleep(5)
        return True

    def end_maintenance(self) -> None:
        """Accept new work after a completed or cancelled maintenance action."""

        self._accepting_tasks = True
        self._maintenance_active = False

    def resume_after_failed_shutdown(self) -> None:
        """Allow work again when the final persistence/backup step failed."""

        if self.has_active_tasks():
            raise RuntimeError(
                "Não é possível retomar enquanto existem tarefas ativas."
            )
        self._accepting_tasks = True
        self._maintenance_active = False


_STANDALONE_TASK_REGISTRY = BackgroundTaskRegistry()


class _TaskWorker(QObject):
    result = Signal(object)
    error = Signal(str)
    done = Signal()

    def __init__(self, callback: Callable[[], Any]):
        super().__init__()
        self.callback = callback

    @Slot()
    def run(self) -> None:
        thread = QThread.currentThread()
        try:
            if thread.isInterruptionRequested():
                return

            needs_lock = bool(getattr(self.callback, "requires_sqlite_lock", True))
            if needs_lock:
                # Bounded lock acquisition: poll every 50ms checking for thread
                # interruption so cancellation is never blocked by SQLite lock contention.
                acquired = False
                while not acquired:
                    if thread.isInterruptionRequested():
                        return
                    acquired = _SQLITE_SHARED_CONNECTION_LOCK.acquire(timeout=0.05)
                try:
                    if thread.isInterruptionRequested():
                        return
                    value = self.callback()
                finally:
                    _SQLITE_SHARED_CONNECTION_LOCK.release()
            else:
                if thread.isInterruptionRequested():
                    return
                value = self.callback()

            if not thread.isInterruptionRequested():
                self.result.emit(value)
        except Exception as exc:
            if not thread.isInterruptionRequested():
                log_exception("Background task execution failed", exc)
                self.error.emit(str(exc))
        finally:
            self.done.emit()


class AsyncTask(QObject):
    """Run one blocking callable at a time without blocking the Qt UI thread."""

    result = Signal(object)
    error = Signal(str)
    finished = Signal(bool)

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        registry: BackgroundTaskRegistry | None = None,
        label: str = "Tarefa em segundo plano",
        allow_during_shutdown: bool = False,
    ):
        super().__init__(parent)
        self._thread: QThread | None = None
        self._worker: _TaskWorker | None = None
        self._cancel_requested = False
        self._cancel_callback: Callable[[], Any] | None = None
        self._registry = registry or _STANDALONE_TASK_REGISTRY
        self._label = str(label or "Tarefa em segundo plano").strip()
        self._allow_during_shutdown = bool(allow_during_shutdown)

    def is_running(self) -> bool:
        return self._thread is not None


    def start(self, callback: Callable[[], Any]) -> bool:
        if self.is_running():
            return False
        if not self._registry.can_start(
            allow_during_shutdown=self._allow_during_shutdown
        ):
            return False

        self._cancel_requested = False
        self._cancel_callback = getattr(callback, "cancel", None)
        thread = QThread()
        worker = _TaskWorker(callback)
        worker.moveToThread(thread)

        thread.started.connect(worker.run)
        worker.result.connect(self._relay_result)
        worker.error.connect(self._relay_error)
        worker.done.connect(worker.deleteLater)
        worker.done.connect(thread.quit)
        self._thread = thread
        self._worker = worker
        if not self._registry.register_thread(
            thread,
            label=self._label,
            cancel=self.cancel,
            allow_during_shutdown=self._allow_during_shutdown,
        ):
            self._thread = None
            self._worker = None
            self._cancel_callback = None
            worker.deleteLater()
            thread.deleteLater()
            return False
        thread.finished.connect(
            self._thread_finished,
            type=Qt.QueuedConnection,
        )
        with _JOBS_LOCK:
            _ACTIVE_JOBS[thread] = worker
        thread.start()
        return True

    def cancel(self) -> bool:
        if not self.is_running() or self._thread is None:
            return False
        self._cancel_requested = True
        self._thread.requestInterruption()
        if callable(self._cancel_callback):
            try:
                self._cancel_callback()
            except Exception as exc:
                # Cancellation callbacks belong to external services; a
                # callback failure must not prevent the Qt thread interruption.
                log_exception("Background task cancellation callback failed", exc)
        return True

    @Slot(object)
    def _relay_result(self, value: Any) -> None:
        if not self._cancel_requested:
            self.result.emit(value)

    @Slot(str)
    def _relay_error(self, message: str) -> None:
        if not self._cancel_requested:
            self.error.emit(message)

    @Slot()
    def _thread_finished(self) -> None:
        cancelled = self._cancel_requested
        sender = self.sender()
        thread = sender if isinstance(sender, QThread) else self._thread
        if thread is not None:
            # This slot is queued to the AsyncTask's (normally GUI) thread.
            # The finished signal can precede the final native-thread return;
            # wait for that return, then release every Python owner before
            # scheduling C++ deletion. This avoids shiboken destruction races.
            thread.wait()
            self._registry._release_thread(thread)
            with _JOBS_LOCK:
                _ACTIVE_JOBS.pop(thread, None)
            thread.deleteLater()

        if thread is not self._thread:
            # A stale completion must never clear or wait on a replacement
            # task. ``start`` now closes this window, but retaining the guard
            # makes cleanup robust to direct/external signal delivery too.
            return
        self._thread = None
        self._worker = None
        self._cancel_callback = None
        self.finished.emit(cancelled)
