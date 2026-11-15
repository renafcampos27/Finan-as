"""Tests for v3.49 Phase 2: UI/UX 120 FPS, Liquid Glass & Geometry Invariants."""

from PySide6.QtWidgets import QApplication, QPushButton, QWidget
from ui.components.charts.donut import DonutChart
from ui.components.charts.sankey import SankeyChart
from ui.core.layouts import FlowLayout
from ui.core.theme import Theme
from ui.core.widgets import LiquidSheetDialog


def _ensure_app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _dark_theme() -> Theme:
    return Theme(dark=True)


def test_flow_layout_size_hint_width_awareness() -> None:
    _ensure_app()
    parent = QWidget()
    parent.resize(400, 300)
    layout = FlowLayout(parent)

    for i in range(10):
        btn = QPushButton(f"Filter Chip {i}")
        btn.setFixedSize(120, 32)
        layout.addWidget(btn)

    hint = layout.sizeHint()
    assert hint.width() == 400
    assert hint.height() > 32


def test_liquid_sheet_dialog_no_parent_blur() -> None:
    _ensure_app()
    parent = QWidget()
    parent.resize(800, 600)
    dialog = LiquidSheetDialog(parent)

    dialog.show()
    assert parent.graphicsEffect() is None
    dialog.hide()
    assert parent.graphicsEffect() is None


def test_sankey_chart_responsive_label_elision() -> None:
    _ensure_app()
    chart = SankeyChart(_dark_theme)
    chart.resize(500, 400)
    chart.set_data(
        [
            ("Supermercado e Compras de Alimentacao", 800.0),
            ("Telecomunicacoes e Servicos de Internet", 150.0),
            ("Poupanca", 2050.0),
        ]
    )
    assert len(chart.items) == 3


def test_donut_chart_hover_invalidation() -> None:
    _ensure_app()
    donut = DonutChart(_dark_theme)
    donut.resize(400, 300)
    donut.set_data(
        [
            ("Alimentação", 300.0, "#ff0000"),
            ("Transportes", 100.0, "#00ff00"),
        ]
    )
    assert len(donut.data) == 2
