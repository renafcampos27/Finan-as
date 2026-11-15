"""Update UI uses workers and cannot install before safe database shutdown."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMainWindow, QMessageBox

from ui.app_updates import AppUpdateController
from ui.workers import BackgroundTaskRegistry


class FakeRepo:
    def __init__(self):
        self.values = {}

    def setting(self, key, default):
        return self.values.get(key, default)

    def set_setting(self, key, value):
        self.values[key] = value

    def commit(self):
        pass


@pytest.fixture
def controller():
    qapp = QApplication.instance() or QApplication([])
    app = QMainWindow()
    app.background_task_registry = BackgroundTaskRegistry(app)
    app.repo = FakeRepo()
    app.goto = lambda name: None
    app._database_closed = False
    updater = AppUpdateController(app)
    yield updater
    updater.task.cancel()
    deadline = time.monotonic() + 3
    while updater.task.is_running() and time.monotonic() < deadline:
        qapp.processEvents()
        QTest.qWait(5)
    assert not updater.task.is_running()
    app.close()


def wait_finished(controller):
    deadline = time.monotonic() + 3
    while controller.task.is_running() and time.monotonic() < deadline:
        QApplication.processEvents()
        QTest.qWait(5)
    assert not controller.task.is_running()
    QApplication.processEvents()


def test_check_runs_outside_gui_and_does_not_install(controller, monkeypatch):
    gui_thread = threading.get_ident()
    threads = []
    release = SimpleNamespace(version="99.0.0", html_url="https://github.com/renafcampos27/Finan-as/releases/tag/v99.0.0")

    def check(version):
        threads.append(threading.get_ident())
        return release

    monkeypatch.setattr("ui.app_updates.updates.check_for_update", check)
    controller.check()
    wait_finished(controller)
    assert threads and threads[0] != gui_thread
    assert controller.release is release
    assert "99.0.0" in controller.status
    assert controller.pending is None


def test_install_requires_confirmation(controller, monkeypatch, tmp_path):
    controller.release = SimpleNamespace(version="99.0.0")
    monkeypatch.setattr("ui.app_updates.updates.managed_install_root", lambda: tmp_path)
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.No)
    monkeypatch.setattr("ui.app_updates.updates.download_update", lambda *a, **kw: pytest.fail("download without approval"))
    controller.install()
    assert not controller.task.is_running()
    assert controller.pending is None


def test_no_helper_without_completed_database_shutdown(controller, monkeypatch):
    monkeypatch.setattr("ui.app_updates.updates.launch_windows_update", lambda *a: pytest.fail("unsafe installation"))
    controller.pending = object()
    controller.launch_pending_after_exit()
    assert controller.pending is None


def test_cancelled_shutdown_cannot_install_on_later_exit(controller, monkeypatch):
    monkeypatch.setattr("ui.app_updates.updates.launch_windows_update", lambda *a: pytest.fail("cancelled installation"))
    controller.pending = object()
    controller.cancel_pending()
    controller.app._database_closed = True
    controller.launch_pending_after_exit()
    assert controller.pending is None


def test_safe_exit_launches_prepared_update_once(controller, monkeypatch):
    prepared = object()
    launched = []
    monkeypatch.setattr("ui.app_updates.updates.launch_windows_update", launched.append)
    controller.pending = prepared
    controller.app._database_closed = True
    controller.launch_pending_after_exit()
    controller.launch_pending_after_exit()
    assert launched == [prepared]


def test_failed_download_keeps_app_open(controller):
    controller._operation = "download"
    controller._error("synthetic failure")
    controller._finished(False)
    assert controller.pending is None
    assert not controller.app._database_closed
    assert "preservada" in controller.status


def test_offline_checks_are_disabled_in_test_profiles(controller, monkeypatch, tmp_path):
    monkeypatch.setenv("FINANCAS_PESSOAIS_TESTING", "1")
    monkeypatch.setattr("ui.app_updates.updates.managed_install_root", lambda: tmp_path)
    monkeypatch.setattr("ui.app_updates.QTimer.singleShot", lambda *args: pytest.fail("network startup in test profile"))
    controller.schedule_startup_check()


def test_card_preserves_user_auto_check_preference(controller):
    from PySide6.QtWidgets import QCheckBox

    card = controller.make_card()
    automatic = card.findChild(QCheckBox)
    automatic.setChecked(False)
    assert controller.app.repo.values["check_app_updates"] == "0"
    card.deleteLater()
