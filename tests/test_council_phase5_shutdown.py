"""Council A13: user-controlled shutdown and one exception-hook owner."""

from __future__ import annotations

import sys
import subprocess
import sqlite3
import threading
import time
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QApplication, QMainWindow, QWidget

import main
from utils.qt_runtime import install_global_exception_hook


@pytest.fixture
def shell(monkeypatch):
    """Exercise actual closeEvent without opening any financial profile."""
    qapp = QApplication.instance() or QApplication([])
    window = main.LiquidFinanceApp.__new__(main.LiquidFinanceApp)
    QMainWindow.__init__(window)
    window._closing = True
    window._shutdown_started = True
    window._shutdown_started_at = time.monotonic() - 100
    window._shutdown_warning_shown = False
    window._shutdown_wait_dialog = None
    window._shutdown_cancel_requested = False
    window._shutdown_state_saved = True
    window._shutdown_backup_started = True
    window._shutdown_backup_finished = True
    window._shutdown_backup_error = ""
    window._shutdown_close_scheduled = False
    window._shutdown_integrity_error = False
    window._database_closed = False
    window._shutdown_poll_timer = QTimer(window)
    window._shutdown_poll_timer.setSingleShot(True)
    state = SimpleNamespace(active=False, cancelled=0, finalized=0, aborted=[])
    window.background_task_registry = SimpleNamespace(
        has_active_tasks=lambda: state.active,
        active_labels=lambda: ("Escrita de documentos",) if state.active else (),
        cancel_all=lambda **kwargs: setattr(state, "cancelled", state.cancelled + 1),
    )
    window.close_backup_task = SimpleNamespace(is_running=lambda: False)
    monkeypatch.setattr(window, "_save_shutdown_state", lambda: (True, ""))

    def finalize():
        state.finalized += 1
        window._database_closed = True
        return True, ""

    monkeypatch.setattr(window, "_finalize_database_shutdown", finalize)

    def abort(detail, **kwargs):
        state.aborted.append(detail)
        window._closing = False
        window._shutdown_poll_timer.stop()

    monkeypatch.setattr(window, "_abort_shutdown", abort)
    yield window, state
    window._closing = False
    window._shutdown_poll_timer.stop()
    dialog = getattr(window, "_shutdown_wait_dialog", None)
    if dialog is not None:
        dialog.hide()
        dialog.deleteLater()
    window.deleteLater()
    qapp.processEvents()


def test_pending_work_warning_keeps_database_open_and_exposes_control(shell):
    window, state = shell
    state.active = True
    window._continue_shutdown_when_ready()
    assert window._shutdown_wait_dialog is not None
    assert state.finalized == 0
    assert window._shutdown_warning_shown


def test_failed_backup_can_skip_only_this_backup_and_finish_durable_close(
    shell, monkeypatch
):
    window, state = shell
    window._shutdown_backup_error = "disco indisponível"
    choices = []
    monkeypatch.setattr(
        window,
        "_choose_close_backup_failure",
        lambda detail: choices.append(detail) or "skip",
        raising=False,
    )
    window.closeEvent(QCloseEvent())
    QApplication.processEvents()
    assert choices == ["disco indisponível"]
    assert state.finalized == 1
    assert not state.aborted


def test_exception_hook_installation_is_idempotent(monkeypatch):
    monkeypatch.setattr(sys, "excepthook", sys.__excepthook__)
    install_global_exception_hook()
    first = sys.excepthook
    install_global_exception_hook()
    assert sys.excepthook is first


def test_warning_does_not_open_duplicate_controls_or_force_close(shell):
    window, state = shell
    state.active = True
    window._continue_shutdown_when_ready()
    first = window._shutdown_wait_dialog
    for _ in range(5):
        window._continue_shutdown_when_ready()
    assert window._shutdown_wait_dialog is first
    assert first.isEnabled()
    assert "Escrita de documentos" in first.pending.text()
    assert state.finalized == 0


def test_cancel_shutdown_waits_for_writer_then_resumes_instead_of_closing(shell):
    window, state = shell
    state.active = True
    window._continue_shutdown_when_ready()
    window._cancel_shutdown_when_idle()
    assert window._shutdown_cancel_requested
    assert not state.aborted
    assert state.finalized == 0
    assert not window._shutdown_wait_dialog.cancel_close.isEnabled()
    state.active = False
    window._continue_shutdown_when_ready()
    assert state.aborted == ["Encerramento cancelado pelo utilizador."]
    assert state.finalized == 0


@pytest.mark.parametrize("action", ["escape", "close"])
def test_warning_escape_and_close_cancel_without_hiding_pending_write(shell, action):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    window, state = shell
    state.active = True
    window._continue_shutdown_when_ready()
    dialog = window._shutdown_wait_dialog
    if action == "escape":
        QTest.keyClick(dialog, Qt.Key_Escape)
    else:
        dialog.close()
    assert window._shutdown_cancel_requested
    assert dialog.isVisible()
    assert state.finalized == 0
    assert not window._shutdown_wait_dialog.cancel_close.isEnabled()
    state.active = False
    window._continue_shutdown_when_ready()
    assert state.aborted == ["Encerramento cancelado pelo utilizador."]
    assert state.finalized == 0


def test_pending_control_requests_only_safe_ordinary_task_cancellation(
    shell, monkeypatch
):
    window, _state = shell
    calls = []
    monkeypatch.setattr(
        window.background_task_registry,
        "cancel_all",
        lambda **kwargs: calls.append(kwargs),
    )
    window._request_shutdown_task_cancellation()
    assert calls == [{"include_shutdown_tasks": False}]


def test_short_work_completion_needs_no_warning(shell):
    window, state = shell
    window._shutdown_started_at = time.monotonic()
    state.active = True
    window._continue_shutdown_when_ready()
    assert window._shutdown_wait_dialog is None
    state.active = False
    window._continue_shutdown_when_ready()
    QApplication.processEvents()
    assert state.finalized == 1


@pytest.mark.parametrize("choice", ["retry", "cancel"])
def test_failed_backup_retry_or_cancel_is_an_explicit_choice(
    shell, monkeypatch, choice
):
    window, state = shell
    window._shutdown_backup_error = "cópia indisponível"
    monkeypatch.setattr(window, "_choose_close_backup_failure", lambda _detail: choice)
    retries = []
    if choice == "retry":
        monkeypatch.setattr(
            window, "_continue_shutdown_when_ready", lambda: retries.append(True)
        )
    window.closeEvent(QCloseEvent())
    if choice == "retry":
        assert retries == [True]
        assert not window._shutdown_backup_started
        assert not window._shutdown_backup_finished
        assert not state.aborted
    else:
        assert len(state.aborted) == 1
    assert state.finalized == 0


def test_skip_does_not_bypass_actual_database_finalization_error(shell, monkeypatch):
    window, state = shell
    window._shutdown_backup_error = "backup recusado"
    monkeypatch.setattr(window, "_choose_close_backup_failure", lambda _detail: "skip")
    monkeypatch.setattr(
        window, "_finalize_database_shutdown", lambda: (False, "erro de integridade")
    )
    window.closeEvent(QCloseEvent())
    QApplication.processEvents()
    assert state.aborted == ["erro de integridade"]
    assert not window._database_closed


def test_work_must_drain_even_when_backup_is_reported_failed(shell, monkeypatch):
    window, state = shell
    state.active = True
    window._shutdown_backup_error = "erro ainda em limpeza"
    choices = []
    monkeypatch.setattr(
        window, "_choose_close_backup_failure", lambda detail: choices.append(detail)
    )
    window.closeEvent(QCloseEvent())
    assert not choices
    assert state.finalized == 0


def test_pending_cancel_is_respected_by_a_direct_new_close_event(shell):
    window, state = shell
    window._shutdown_cancel_requested = True
    window.closeEvent(QCloseEvent())
    assert state.aborted == ["Encerramento cancelado pelo utilizador."]
    assert state.finalized == 0


def test_hook_logs_once_and_nested_notifier_exception_uses_primitive_fallback(
    monkeypatch,
):
    from unittest.mock import Mock
    from utils import app_logging, error_handler

    logger = Mock()
    fallback = Mock()
    notifications = []
    monkeypatch.setattr(app_logging, "get_logger", lambda: logger)
    monkeypatch.setattr(sys, "__excepthook__", fallback)

    def notify(_kind, _value, _tb):
        notifications.append(True)
        error_handler.handle_unhandled_exception(ValueError, ValueError("nested"), None)
        return True

    monkeypatch.setattr(error_handler, "_notifier", notify)
    error_handler.handle_unhandled_exception(
        RuntimeError, RuntimeError("original"), None
    )
    assert logger.critical.call_count == 1
    assert notifications == [True]
    assert fallback.call_count == 1


def test_failed_logging_does_not_lose_error_presentation(monkeypatch):
    from unittest.mock import Mock
    from utils import app_logging, error_handler

    logger = Mock()
    logger.critical.side_effect = OSError("sink failed")
    notifier = Mock(return_value=True)
    monkeypatch.setattr(app_logging, "get_logger", lambda: logger)
    monkeypatch.setattr(error_handler, "_notifier", notifier)
    monkeypatch.setattr(sys, "__excepthook__", Mock())
    error_handler.handle_unhandled_exception(ValueError, ValueError("top"), None)
    assert notifier.call_count == 1


@pytest.mark.parametrize("kind", [KeyboardInterrupt, SystemExit])
def test_control_exceptions_bypass_ui_and_application_error_log(monkeypatch, kind):
    from unittest.mock import Mock
    from utils import app_logging, error_handler

    logger, notifier, fallback = Mock(), Mock(), Mock()
    monkeypatch.setattr(app_logging, "get_logger", lambda: logger)
    monkeypatch.setattr(error_handler, "_notifier", notifier)
    monkeypatch.setattr(sys, "__excepthook__", fallback)
    error_handler.handle_unhandled_exception(kind, kind(), None)
    assert fallback.call_count == 1
    assert not logger.critical.called and not notifier.called


def test_logging_setup_cannot_replace_the_registered_ui_owner(monkeypatch):
    from utils.app_logging import configure_logging
    from utils.error_handler import handle_unhandled_exception

    monkeypatch.setattr(sys, "excepthook", sys.__excepthook__)
    install_global_exception_hook()
    configure_logging()
    assert sys.excepthook is handle_unhandled_exception


@pytest.fixture
def activation(tmp_path):
    from PySide6.QtCore import QLockFile
    from utils.single_instance import InstanceActivationServer

    qapp = QApplication.instance() or QApplication([])
    lock_path = tmp_path / "profile.lock"
    lock = QLockFile(str(lock_path))
    assert lock.tryLock(0)
    channel = InstanceActivationServer(lock_path)
    window = QWidget()
    calls = []
    window.activateWindow = lambda: calls.append("activate")
    channel.set_window(window)
    yield channel, lock_path, window, calls
    channel.server.close()
    channel.deleteLater()
    window.hide()
    window.deleteLater()
    lock.unlock()
    qapp.processEvents()


def _request_in_process(script, *args):
    # A separate process reproduces the second application and avoids coupling
    # the server's GUI loop to a blocking Python client's GIL behavior.
    process = subprocess.Popen(
        [sys.executable, "-c", script, *map(str, args)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 10
    while process.poll() is None and time.monotonic() < deadline:
        QApplication.processEvents()
        time.sleep(0.005)
    stdout, stderr = process.communicate(timeout=2)
    assert process.returncode in (0, 1), (stdout, stderr)
    return process.returncode == 0


_ACTIVATION_CLIENT = """
import sys
from pathlib import Path
from PySide6.QtCore import QCoreApplication
from utils.single_instance import request_instance_activation
app = QCoreApplication([])
raise SystemExit(0 if request_instance_activation(Path(sys.argv[1]), 1500) else 1)
"""


def test_profile_scoped_local_channel_activates_original_window(activation):
    from PySide6.QtNetwork import QLocalServer

    channel, lock_path, _window, calls = activation
    assert channel.server.socketOptions() == QLocalServer.UserAccessOption
    result = _request_in_process(_ACTIVATION_CLIENT, lock_path)
    assert result, (calls, channel.server.isListening(), channel.server.errorString())
    assert calls == ["activate"]


def test_other_profile_cannot_target_this_channel(activation, tmp_path):
    _channel, _lock_path, _window, calls = activation
    assert not _request_in_process(_ACTIVATION_CLIENT, tmp_path / "other.lock")
    assert not calls


def test_channel_rejects_any_command_except_activation(activation):
    _channel, lock_path, _window, calls = activation
    script = """
import sys
from pathlib import Path
from PySide6.QtCore import QCoreApplication
from PySide6.QtNetwork import QLocalSocket
from utils.single_instance import activation_endpoint
app = QCoreApplication([])
client = QLocalSocket()
client.connectToServer(activation_endpoint(Path(sys.argv[1])))
assert client.waitForConnected(1500)
client.write(b'RESTORE data\\n')
client.flush()
client.waitForReadyRead(1000)
result = bytes(client.readAll())
client.disconnectFromServer()
raise SystemExit(0 if result == b'' else 1)
"""
    assert _request_in_process(script, lock_path)
    assert not calls


def _wait_until(predicate, seconds=5):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        QApplication.processEvents()
        if predicate():
            return True
        time.sleep(0.005)
    return bool(predicate())


@pytest.mark.parametrize("choice", ["retry", "skip"])
def test_actual_failed_backup_worker_choice_completes_close_without_preference_change(
    tmp_path, empty_profile, monkeypatch, choice
):
    import database.repo.base as repo_base
    from services.backup import BackupService

    QApplication.instance() or QApplication([])
    database = tmp_path / "close.sqlite3"
    monkeypatch.setattr(repo_base, "db_path", lambda: database)
    calls, choices = [], []

    def backup(_database, *, silent):
        calls.append(True)
        if len(calls) == 1:
            raise OSError("destino temporariamente indisponível")
        return tmp_path / "synthetic-success.zip"

    monkeypatch.setattr(
        BackupService, "run_close_backup_for_database", staticmethod(backup)
    )
    window = main.LiquidFinanceApp()
    window.repo.set_setting("backup_on_close", "1")
    monkeypatch.setattr(
        window,
        "_choose_close_backup_failure",
        lambda detail: choices.append(detail) or choice,
    )
    window.show()
    window.close()
    assert _wait_until(lambda: window._database_closed)
    assert len(calls) == (2 if choice == "retry" else 1)
    assert choices == ["destino temporariamente indisponível"]
    assert not window.isVisible()
    with sqlite3.connect(database) as connection:
        assert (
            connection.execute(
                "SELECT value FROM settings WHERE key='backup_on_close'"
            ).fetchone()[0]
            == "1"
        )


def test_actual_writer_can_cancel_close_and_resume_only_after_finishing(
    tmp_path, empty_profile, monkeypatch
):
    import database.repo.base as repo_base
    from ui.workers import AsyncTask

    QApplication.instance() or QApplication([])
    monkeypatch.setattr(repo_base, "db_path", lambda: tmp_path / "writer.sqlite3")
    monkeypatch.setattr(main.LiquidFinanceApp, "SHUTDOWN_WARNING_SECONDS", 0.01)
    window = main.LiquidFinanceApp()
    window.repo.set_setting("backup_on_close", "0")
    release, started = threading.Event(), threading.Event()
    marker = tmp_path / "attachment-evidence.bin"
    task = AsyncTask(
        window, registry=window.background_task_registry, label="Escrever anexo"
    )

    def writer():
        started.set()
        release.wait(5)
        marker.write_bytes(b"completed evidence")
        return True

    assert task.start(writer)
    assert started.wait(1)
    try:
        window.show()
        window.close()
        assert _wait_until(lambda: window._shutdown_wait_dialog is not None)
        dialog = window._shutdown_wait_dialog
        assert dialog.isEnabled() and not window.isEnabled()
        dialog.cancel_close.click()
        assert window._shutdown_cancel_requested
        assert not window._database_closed
        assert window.background_task_registry.is_shutting_down
        release.set()
        assert _wait_until(lambda: not window._closing)
        assert marker.read_bytes() == b"completed evidence"
        assert window.isVisible() and window.isEnabled()
        assert not window._database_closed
        assert not window.background_task_registry.is_shutting_down
        assert window._shutdown_started_at == 0
        window.close()
        assert _wait_until(lambda: window._database_closed)
    finally:
        release.set()
        _wait_until(lambda: not window.background_task_registry.has_active_tasks())


def test_error_dialog_renders_untrusted_error_text_as_plain_text(monkeypatch):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QMessageBox
    from utils import qt_runtime

    QApplication.instance() or QApplication([])
    captured = []
    monkeypatch.setattr(
        QMessageBox,
        "exec",
        lambda self: (
            captured.append((self.textFormat(), self.text())) or QMessageBox.Ok
        ),
    )
    with monkeypatch.context() as isolated:
        isolated.delitem(sys.modules, "pytest")
        isolated.delenv("PYTEST_CURRENT_TEST", raising=False)
        assert qt_runtime._present_unhandled_exception(
            ValueError, ValueError("<b>literal</b>"), None
        )
    assert captured[0][0] == Qt.PlainText
    assert "<b>literal</b>" in captured[0][1]
