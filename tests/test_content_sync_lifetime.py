"""Queued geometry callbacks cannot access native widgets deleted after close."""

from __future__ import annotations

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QTimer
from PySide6.QtWidgets import QApplication, QLabel, QMainWindow, QScrollArea, QVBoxLayout, QWidget
from shiboken6 import delete, isValid

from main import CurrentPageStack, LiquidFinanceApp


class _GeometryWindow(QMainWindow):
    sync_content_size = LiquidFinanceApp.sync_content_size

    def __init__(self):
        super().__init__()
        self._closing = False
        self.content_scroll = QScrollArea(self)
        self.content_scroll.resize(600, 400)
        self.stack = CurrentPageStack(self.content_scroll)
        self.page = QWidget()
        layout = QVBoxLayout(self.page)
        layout.addWidget(QLabel("Synthetic geometry content"))
        self.page.setMinimumHeight(520)
        self.stack.addWidget(self.page)


@pytest.fixture
def window():
    qapp = QApplication.instance() or QApplication([])
    current = _GeometryWindow()
    yield current
    if isValid(current):
        current.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qapp.processEvents()


def test_live_content_keeps_existing_viewport_and_size_hint_behavior(window):
    window.sync_content_size()
    assert window.page.maximumWidth() == window.content_scroll.viewport().width()
    expected = max(
        window.page.sizeHint().height(), window.page.minimumSizeHint().height(),
        window.page.minimumHeight(), window.content_scroll.viewport().height(),
    )
    assert window.stack.minimumHeight() == expected
    assert window.stack.maximumHeight() == expected


@pytest.mark.parametrize("target", ["stack", "scroll", "window", "closing"])
def test_late_sync_ignores_closed_or_deleted_native_owners(window, monkeypatch, target):
    def unexpected_geometry_error(*_args):
        pytest.fail("A stale geometry callback touched deleted native widgets")

    monkeypatch.setattr("main.log_exception", unexpected_geometry_error)
    if target == "closing":
        window._closing = True
        delete(window.stack)
    elif target == "window":
        delete(window)
    elif target == "scroll":
        delete(window.content_scroll)
    else:
        delete(window.stack)
    window.sync_content_size()


def test_queued_sync_after_stack_deferred_delete_has_no_error(window, monkeypatch):
    errors = []
    monkeypatch.setattr("main.log_exception", lambda *args: errors.append(args))
    window.stack.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert not isValid(window.stack)
    QTimer.singleShot(0, window.sync_content_size)
    QApplication.processEvents()
    assert errors == []
