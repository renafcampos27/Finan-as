"""LGBK3: the QMessageBox theme filter must survive nested Polish events.

apply_message_box_theme() mutates the message box stylesheet and palette,
which makes Qt emit a nested PolishRequest while the eventFilter is still
running. Without a re-entrancy guard the filter recurses until the process
hangs (observed as a pytest timeout at 90% of the full suite whenever any
test left a QMessageBox behind and a later test called
app.setStyleSheet()).
"""

from __future__ import annotations

import pytest
from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import QApplication, QMessageBox

from ui.core.theme import (
    Theme,
    _THEME_MARKER_PROPERTY,
    _install_message_box_theme_filter,
    _message_box_stylesheet,
)


@pytest.fixture()
def app() -> QApplication:
    instance = QApplication.instance() or QApplication([])
    previous_palette = QPalette(instance.palette())
    previous_stylesheet = instance.styleSheet()
    previous_filter = getattr(instance, "_finance_message_box_theme_filter", None)
    previous_filter_theme = getattr(previous_filter, "theme", None)
    yield instance
    active_filter = getattr(instance, "_finance_message_box_theme_filter", None)
    if previous_filter is None and active_filter is not None:
        instance.removeEventFilter(active_filter)
        delattr(instance, "_finance_message_box_theme_filter")
    elif previous_filter is not None and previous_filter_theme is not None:
        previous_filter.set_theme(previous_filter_theme)
    instance.setPalette(previous_palette)
    instance.setStyleSheet(previous_stylesheet)


def _screenless_box() -> QMessageBox:
    box = QMessageBox()
    box.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    return box


def test_nested_polish_request_does_not_recurse(app: QApplication) -> None:
    """A Polish event handled by the filter must not re-enter itself."""
    theme = Theme(dark=False)
    _install_message_box_theme_filter(app, theme)
    box = _screenless_box()
    box.show()
    try:
        # Direct nested delivery: this is the exact path that livelocked.
        app.sendEvent(box, QEvent(QEvent.Type.Polish))
        app.sendEvent(box, QEvent(QEvent.Type.PolishRequest))
        manager = app._finance_message_box_theme_filter
        assert manager._applying is False
        assert (
            box.palette().color(
                QPalette.ColorGroup.Active,
                QPalette.ColorRole.Window,
            )
            == theme.message_surface
        )
        assert theme.message_surface in box.styleSheet()
    finally:
        box.hide()
        box.deleteLater()


def test_guard_resets_and_future_boxes_still_themed(app: QApplication) -> None:
    """The guard is per-delivery, never disables the filter afterwards."""
    theme = Theme(dark=True)
    _install_message_box_theme_filter(app, theme)
    first = _screenless_box()
    first.show()
    try:
        app.sendEvent(first, QEvent(QEvent.Type.Polish))
        manager = app._finance_message_box_theme_filter
        assert manager._applying is False

        second = _screenless_box()
        second.show()
        try:
            app.sendEvent(second, QEvent(QEvent.Type.Show))
            assert theme.message_surface in second.styleSheet()
            assert manager._applying is False
        finally:
            second.hide()
            second.deleteLater()
    finally:
        first.hide()
        first.deleteLater()


def test_repeated_delivery_is_short_circuited(app: QApplication) -> None:
    """The marker short-circuit must skip widget mutation (no repolish)."""
    theme = Theme(dark=False)
    _install_message_box_theme_filter(app, theme)
    box = _screenless_box()
    box.show()
    try:
        expected = _message_box_stylesheet(theme)
        assert box.property(_THEME_MARKER_PROPERTY) == expected
        # Direct re-delivery of Polish events after the first application:
        # without the marker gate each delivery would mutate the widget and
        # re-queue another PolishRequest forever.
        for _ in range(10):
            app.sendEvent(box, QEvent(QEvent.Type.Polish))
            app.sendEvent(box, QEvent(QEvent.Type.PolishRequest))
        assert box.property(_THEME_MARKER_PROPERTY) == expected
        assert box.styleSheet() == expected
        assert manager_present(app)._applying is False
    finally:
        box.hide()
        box.deleteLater()


def manager_present(app: QApplication) -> object:
    """Fetch the installed filter manager (helper for assertions)."""
    manager = getattr(app, "_finance_message_box_theme_filter", None)
    assert manager is not None
    return manager


def test_global_repolish_with_residual_message_box_completes(
    app: QApplication,
) -> None:
    """app.setStyleSheet() with a visible box must terminate (suite hang)."""
    theme = Theme(dark=False)
    _install_message_box_theme_filter(app, theme)
    box = _screenless_box()
    box.show()
    try:
        app.setStyleSheet("/* lgbk3 repolish */")
        app.processEvents()
        app.setStyleSheet("")
        app.processEvents()
        assert (
            box.palette().color(
                QPalette.ColorGroup.Active,
                QPalette.ColorRole.Window,
            )
            == theme.message_surface
        )
    finally:
        box.hide()
        box.deleteLater()
