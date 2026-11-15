"""Filter persistence tolerates controls removed by responsive page rebuilds."""

from types import SimpleNamespace

from PySide6.QtWidgets import QApplication, QButtonGroup, QPushButton, QWidget
from shiboken6 import delete

from ui.core.page import BasePage


def test_deleted_filter_control_is_not_traversed():
    qapp = QApplication.instance() or QApplication([])
    panel = QWidget()
    button = QPushButton(panel)
    page = SimpleNamespace(_primary_filter_panel=panel)
    assert BasePage._is_inside_filter_panel(page, button)
    delete(button)
    assert not BasePage._is_inside_filter_panel(page, button)
    delete(panel)
    assert not BasePage._is_inside_filter_panel(page, button)
    qapp.processEvents()


def test_deleted_filter_button_group_is_not_enumerated():
    qapp = QApplication.instance() or QApplication([])
    panel = QWidget()
    group = QButtonGroup(panel)
    page = SimpleNamespace(_primary_filter_panel=panel, group=group)
    page._is_inside_filter_panel = lambda widget: BasePage._is_inside_filter_panel(page, widget)
    delete(group)
    assert BasePage._persistent_filter_bindings(page) == []
    delete(panel)
    qapp.processEvents()
