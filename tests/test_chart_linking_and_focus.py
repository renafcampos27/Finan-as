from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget

from ui.components.charts import (
    BalanceChart,
    CumulativeCashflowChart,
    SavingsEfficiencyChart,
)
from ui.core.theme import Theme


def test_temporal_hover_links_exact_dates_and_monthly_blocks() -> None:
    app = QApplication.instance() or QApplication([])
    theme = Theme()
    balance = BalanceChart(lambda: theme)
    cashflow = CumulativeCashflowChart(lambda: theme)
    savings = SavingsEfficiencyChart(lambda: theme)

    balance.set_points(
        [
            {"date": "2026-07-01", "balance": 1_000, "resolution": "daily"},
            {"date": "2026-07-15", "balance": 1_200, "resolution": "daily"},
            {"date": "2026-08-05", "balance": 1_350, "resolution": "daily"},
            {"date": "2026-08-28", "balance": 1_500, "resolution": "daily"},
        ]
    )
    cashflow.set_rows(
        [
            {
                "date": "2026-07-01",
                "resolution": "daily",
                "income": 2_000,
                "expenses": 800,
            },
            {
                "date": "2026-07-15",
                "resolution": "daily",
                "income": 0,
                "expenses": 200,
            },
            {
                "date": "2026-08-05",
                "resolution": "daily",
                "income": 2_000,
                "expenses": 900,
            },
        ]
    )
    savings.set_rows(
        [
            {
                "month": "2026-07",
                "income": 2_000,
                "expenses": 1_000,
                "savings": 1_000,
            },
            {
                "month": "2026-08",
                "income": 2_000,
                "expenses": 900,
                "savings": 1_100,
            },
        ]
    )

    charts = (balance, cashflow, savings)

    def link(period: str) -> None:
        for chart in charts:
            chart.set_highlighted_period(period)

    for chart in charts:
        chart.period_hovered.connect(link)

    balance.emit_period_for_hover(1)
    assert balance.highlighted_period == "2026-07-15"
    assert balance.hover_index == 1
    assert cashflow.hover_index == 1
    assert savings.hover_index == 0

    savings.emit_period_for_hover(1)
    assert savings.highlighted_period == "2026-08"
    assert balance.hover_index == 3
    assert cashflow.hover_index == 2
    assert all(chart.highlighted_period == "2026-08" for chart in charts)

    balance.close()
    cashflow.close()
    savings.close()
    assert app is not None


def test_chart_focus_mode_restores_original_layout_and_constraints() -> None:
    app = QApplication.instance() or QApplication([])
    theme = Theme()
    host = QWidget()
    layout = QVBoxLayout(host)
    chart = BalanceChart(lambda: theme)
    chart.setAccessibleName("Saldo histórico e previsão")
    chart.setMinimumHeight(280)
    chart.setMaximumHeight(420)
    layout.addWidget(chart)
    host.resize(680, 460)
    host.show()
    app.processEvents()

    original_parent = chart.parentWidget()
    original_minimum = chart.minimumSize()
    original_maximum = chart.maximumSize()
    original_index = layout.indexOf(chart)

    dialog = chart.open_focus_view()
    app.processEvents()

    assert dialog.isVisible()
    assert chart.parentWidget() is dialog
    assert chart.maximumHeight() == 16_777_215
    assert chart._focus_dialog is dialog
    assert "Saldo histórico" in dialog.windowTitle()

    QTest.keyClick(dialog, Qt.Key_Escape)
    app.processEvents()

    assert chart.parentWidget() is original_parent
    assert layout.indexOf(chart) == original_index
    assert chart.minimumSize() == original_minimum
    assert chart.maximumSize() == original_maximum
    assert chart._focus_dialog is None

    host.close()
