from __future__ import annotations

from statistics import median

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox, QPushButton

from ui.core.theme import Theme, apply_tooltip_theme


def _sampled_lightness(message_box: QMessageBox) -> float:
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
    assert samples
    return float(median(samples))


@pytest.mark.parametrize("dark", [False, True])
def test_message_boxes_follow_the_active_theme_without_native_black_fallback(
    dark: bool,
) -> None:
    app = QApplication.instance() or QApplication([])
    previous_stylesheet = app.styleSheet()
    theme = Theme(dark=dark)
    apply_tooltip_theme(theme)
    message = QMessageBox()
    message.setWindowTitle("Apagar todos os movimentos")
    message.setIcon(QMessageBox.Warning)
    message.setText("Serão apagados todos os movimentos.")
    message.setInformativeText("Primeiro será criado e validado um backup completo.")
    message.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
    message.setDefaultButton(QMessageBox.No)
    message.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)

    try:
        message.show()
        QTest.qWait(40)
        app.processEvents()

        stylesheet = app.styleSheet()
        assert "QMessageBox" in stylesheet
        assert f"background-color: {theme.message_surface}" in stylesheet
        assert f"color: {theme.message_text}" in stylesheet
        assert len(message.findChildren(QPushButton)) == 2
        lightness = _sampled_lightness(message)
        if dark:
            assert lightness < 90
        else:
            assert lightness > 205
    finally:
        message.close()
        message.deleteLater()
        app.processEvents()
        app.setStyleSheet(previous_stylesheet)
        app.processEvents()
