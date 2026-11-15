from __future__ import annotations

from statistics import median

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication, QLabel, QMessageBox, QWidget

from ui.core.theme import Theme, apply_tooltip_theme
from ui.pages.settings import SettingsPage


def _force_hostile_native_palette(app: QApplication, *, dark: bool) -> None:
    """Recreate a native palette that conflicts with the selected app theme."""
    palette = QPalette(app.palette())
    background = QColor("#050505" if not dark else "#FFFFFF")
    foreground = QColor("#142035" if not dark else "#F8FAFC")
    for group in (
        QPalette.ColorGroup.Active,
        QPalette.ColorGroup.Inactive,
        QPalette.ColorGroup.Disabled,
    ):
        for role in (
            QPalette.ColorRole.Window,
            QPalette.ColorRole.Base,
            QPalette.ColorRole.Button,
        ):
            palette.setColor(group, role, background)
        for role in (
            QPalette.ColorRole.WindowText,
            QPalette.ColorRole.Text,
            QPalette.ColorRole.ButtonText,
        ):
            palette.setColor(group, role, foreground)
    app.setPalette(palette)


def _median_lightness(message_box: QMessageBox) -> float:
    image = message_box.grab().toImage()
    assert not image.isNull()
    step_x = max(2, image.width() // 80)
    step_y = max(2, image.height() // 50)
    samples = [
        image.pixelColor(x, y).lightness()
        for y in range(0, image.height(), step_y)
        for x in range(0, image.width(), step_x)
        if image.pixelColor(x, y).alpha() > 220
    ]
    return float(median(samples))


@pytest.mark.parametrize("dark", [False, True])
def test_static_warning_survives_a_late_conflicting_native_palette(
    dark: bool,
) -> None:
    """Exercise the same static QMessageBox path that failed in Settings."""
    app = QApplication.instance() or QApplication([])
    previous_palette = QPalette(app.palette())
    previous_stylesheet = app.styleSheet()
    previous_filter = getattr(app, "_finance_message_box_theme_filter", None)
    previous_filter_theme = getattr(previous_filter, "theme", None)
    theme = Theme(dark=dark)
    parent = QWidget()
    parent.setStyleSheet(theme.stylesheet())
    parent.resize(720, 480)
    parent.show()
    captured: dict[str, object] = {}

    apply_tooltip_theme(theme)
    # Simulate a late native/third-party reset. The per-popup guard must still
    # restore the active theme when QMessageBox.warning constructs its dialog.
    app.setStyleSheet("")
    _force_hostile_native_palette(app, dark=dark)

    def inspect_and_close() -> None:
        try:
            boxes = [
                widget
                for widget in app.topLevelWidgets()
                if isinstance(widget, QMessageBox) and widget.isVisible()
            ]
            if not boxes:
                captured["error"] = "No visible QMessageBox was created."
                return
            message = boxes[0]
            captured["surface"] = message.palette().color(QPalette.Window).name()
            captured["text"] = message.palette().color(QPalette.WindowText).name()
            captured["stylesheet"] = message.styleSheet()
            captured["native_disabled"] = message.testOption(
                QMessageBox.Option.DontUseNativeDialog
            )
            captured["wrapped_text"] = "\n" in message.text()
            captured["lightness"] = _median_lightness(message)
            labels = [
                label
                for label in message.findChildren(QLabel)
                if label.objectName() != "qt_msgboxex_icon_label"
            ]
            captured["labels_wrapped"] = bool(labels) and all(
                label.wordWrap() for label in labels
            )
            no_button = message.button(QMessageBox.No)
            if no_button is not None:
                no_button.click()
            else:
                message.reject()
        except Exception as exc:  # pragma: no cover - keeps modal test releasable.
            captured["error"] = repr(exc)
            for widget in app.topLevelWidgets():
                if isinstance(widget, QMessageBox):
                    widget.reject()

    QTimer.singleShot(80, inspect_and_close)
    try:
        answer = QMessageBox.warning(
            parent,
            "Apagar todos os movimentos",
            "Serão apagados 94 movimentos e todos os metadados, divisões e "
            "anexos ligados. Primeiro será criado e validado um backup completo.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        assert answer == QMessageBox.No
        assert "error" not in captured, captured.get("error")
        assert captured["surface"] == QColor(theme.message_surface).name()
        assert captured["text"] == QColor(theme.message_text).name()
        assert theme.message_surface in str(captured["stylesheet"])
        assert theme.message_text in str(captured["stylesheet"])
        assert captured["native_disabled"] is True
        assert captured["wrapped_text"] is True
        assert captured["labels_wrapped"] is True
        if dark:
            assert float(captured["lightness"]) < 90
        else:
            assert float(captured["lightness"]) > 205
    finally:
        parent.close()
        app.setPalette(previous_palette)
        app.setStyleSheet(previous_stylesheet)
        active_filter = getattr(app, "_finance_message_box_theme_filter", None)
        if previous_filter is None and active_filter is not None:
            app.removeEventFilter(active_filter)
            delattr(app, "_finance_message_box_theme_filter")
        elif previous_filter is not None and previous_filter_theme is not None:
            previous_filter.set_theme(previous_filter_theme)


def test_clear_all_movements_uses_the_explicit_readable_dialog() -> None:
    """Run the actual Settings method without touching a real repository."""
    app = QApplication.instance() or QApplication([])
    previous_palette = QPalette(app.palette())
    previous_stylesheet = app.styleSheet()
    previous_filter = getattr(app, "_finance_message_box_theme_filter", None)
    previous_filter_theme = getattr(previous_filter, "theme", None)
    theme = Theme(dark=False)

    class RepoStub:
        @staticmethod
        def movement_count() -> int:
            return 94

    parent = QWidget()
    parent.repo = RepoStub()
    parent.app = type("AppStub", (), {"theme": theme})()
    captured: dict[str, object] = {}
    apply_tooltip_theme(theme)
    app.setStyleSheet("")
    _force_hostile_native_palette(app, dark=False)

    def inspect_and_cancel() -> None:
        boxes = [
            widget
            for widget in app.topLevelWidgets()
            if isinstance(widget, QMessageBox) and widget.isVisible()
        ]
        if not boxes:
            captured["error"] = "No Settings confirmation was shown."
            return
        message = boxes[0]
        captured["surface"] = message.palette().color(QPalette.Window).name()
        captured["text"] = message.text()
        captured["detail"] = message.informativeText()
        captured["buttons"] = {
            button.text().replace("&", "") for button in message.buttons()
        }
        captured["lightness"] = _median_lightness(message)
        cancel = message.button(QMessageBox.No)
        if cancel is not None:
            cancel.click()
        else:
            message.reject()

    QTimer.singleShot(80, inspect_and_cancel)
    try:
        SettingsPage.clear_all_movements(parent)
        assert "error" not in captured, captured.get("error")
        assert captured["surface"] == "#f8faff"
        assert float(captured["lightness"]) > 205
        assert captured["buttons"] == {"Continuar", "Cancelar"}
        assert "94 movimentos" in str(captured["text"])
        assert "\n" in str(captured["text"])
        assert "backup completo" in str(captured["detail"]).replace("\n", " ")
    finally:
        parent.close()
        app.setPalette(previous_palette)
        app.setStyleSheet(previous_stylesheet)
        active_filter = getattr(app, "_finance_message_box_theme_filter", None)
        if previous_filter is None and active_filter is not None:
            app.removeEventFilter(active_filter)
            delattr(app, "_finance_message_box_theme_filter")
        elif previous_filter is not None and previous_filter_theme is not None:
            previous_filter.set_theme(previous_filter_theme)
