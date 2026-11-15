from __future__ import annotations

import ast
from pathlib import Path

import pytest
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication,
    QScrollArea,
    QStyle,
    QStyleOptionSlider,
    QWidget,
)

from ui.core import ScrollPassthroughSlider


@pytest.fixture
def qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


def _wheel_event(delta: int = -120) -> QWheelEvent:
    return QWheelEvent(
        QPointF(30, 10),
        QPointF(30, 10),
        QPoint(),
        QPoint(0, delta),
        Qt.NoButton,
        Qt.NoModifier,
        Qt.ScrollUpdate,
        False,
    )


def test_slider_ignores_wheel_without_changing_value(qapp) -> None:
    slider = ScrollPassthroughSlider(Qt.Horizontal)
    slider.setRange(0, 100)
    slider.setValue(50)
    wheel = _wheel_event()

    slider.wheelEvent(wheel)

    assert slider.value() == 50
    assert wheel.isAccepted() is False


def test_ignored_slider_wheel_propagates_to_page_scroll(qapp) -> None:
    page = QWidget()
    page.setFixedSize(240, 900)
    slider = ScrollPassthroughSlider(Qt.Horizontal, page)
    slider.setGeometry(20, 20, 180, 30)
    slider.setRange(0, 100)
    slider.setValue(50)
    scroll = QScrollArea()
    scroll.setWidget(page)
    scroll.resize(240, 180)
    scroll.show()
    qapp.processEvents()
    wheel = _wheel_event()

    qapp.sendEvent(slider, wheel)
    qapp.processEvents()

    assert slider.value() == 50
    assert scroll.verticalScrollBar().value() > 0


def test_slider_keeps_keyboard_interaction(qapp) -> None:
    slider = ScrollPassthroughSlider(Qt.Horizontal)
    slider.setRange(0, 100)
    slider.setSingleStep(5)
    slider.setValue(50)

    slider.show()
    slider.setFocus()
    QTest.keyClick(slider, Qt.Key.Key_Right)

    assert slider.value() == 55


def test_slider_keeps_native_drag_interaction(qapp) -> None:
    slider = ScrollPassthroughSlider(Qt.Horizontal)
    slider.setRange(0, 100)
    slider.setValue(50)
    slider.resize(240, 32)
    slider.show()
    qapp.processEvents()
    option = QStyleOptionSlider()
    slider.initStyleOption(option)
    handle = slider.style().subControlRect(
        QStyle.ComplexControl.CC_Slider,
        option,
        QStyle.SubControl.SC_SliderHandle,
        slider,
    )

    QTest.mousePress(slider, Qt.LeftButton, Qt.NoModifier, handle.center())
    assert slider.isSliderDown() is True
    QTest.mouseMove(slider, slider.rect().topRight())
    QTest.mouseRelease(slider, Qt.LeftButton, Qt.NoModifier, slider.rect().topRight())

    assert slider.isSliderDown() is False
    assert slider.value() == 100


def test_ui_pages_do_not_construct_wheel_sensitive_qsliders() -> None:
    ui_root = Path(__file__).resolve().parents[1] / "ui"
    violations: list[str] = []
    for path in sorted(ui_root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "QSlider"
            ):
                violations.append(f"{path.relative_to(ui_root)}:{node.lineno}")

    assert violations == []
