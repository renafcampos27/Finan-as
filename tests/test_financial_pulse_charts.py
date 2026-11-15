from __future__ import annotations

from datetime import date, timedelta

import pytest
from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication

from ui.components import ChipButton
from ui.components.charts import CumulativeCashflowChart, SavingsEfficiencyChart
from ui.core.theme import Theme
from utils.helpers import add_months


def test_chip_uses_shared_theme_instead_of_fixed_inline_colours() -> None:
    app = QApplication.instance() or QApplication([])
    chip = ChipButton("3 meses", checked=True)

    assert app is not None
    assert chip.objectName() == "Chip"
    assert chip.styleSheet() == ""
    assert chip.isChecked()

    chip.close()


@pytest.mark.parametrize(("dark", "privacy"), [(False, False), (True, True)])
def test_financial_pulse_charts_render_and_keep_hover_readable(
    dark: bool,
    privacy: bool,
) -> None:
    app = QApplication.instance() or QApplication([])
    theme = Theme(dark=dark, privacy=privacy)
    start = date(2026, 7, 1)

    cumulative = CumulativeCashflowChart(lambda: theme)
    cumulative.set_rows(
        [
            {
                "date": (start + timedelta(days=index)).isoformat(),
                "resolution": "daily",
                "income": 1_500 if index in {0, 14} else 0,
                "expenses": 40 + index * 3 if index % 2 == 0 else 0,
            }
            for index in range(30)
        ]
    )
    cumulative.set_comparison_rows(
        [
            {
                "date": (
                    start - timedelta(days=30) + timedelta(days=index)
                ).isoformat(),
                "resolution": "daily",
                "income": 1_200 if index in {0, 14} else 0,
                "expenses": 35 + index * 2 if index % 2 == 0 else 0,
            }
            for index in range(30)
        ],
        "período anterior",
    )

    efficiency = SavingsEfficiencyChart(lambda: theme)
    efficiency.set_rows(
        [
            {
                "month": f"2026-{month:02d}",
                "label": (
                    "Janeiro",
                    "Fevereiro",
                    "Março",
                    "Abril",
                    "Maio",
                    "Junho",
                    "Julho",
                    "Agosto",
                )[month - 1],
                "income": 2_500,
                "expenses": 1_700 + month * 90,
                "savings": 800 - month * 90,
                "savings_rate": (800 - month * 90) / 2_500 * 100,
                "partial": month == 8,
            }
            for month in range(1, 9)
        ]
    )
    efficiency.set_comparison_rows(
        [
            {
                "month": f"2025-{month:02d}",
                "label": f"Mês anterior {month}",
                "income": 2_300,
                "expenses": 1_650 + month * 80,
                "savings": 650 - month * 80,
                "savings_rate": (650 - month * 80) / 2_300 * 100,
            }
            for month in range(1, 9)
        ],
        "ano anterior",
    )

    cumulative.resize(920, 380)
    efficiency.resize(560, 380)
    for chart in (cumulative, efficiency):
        chart.show()
    app.processEvents()

    assert len(cumulative.rows) == 30
    assert cumulative.rows[-1]["cumulative_income"] == pytest.approx(3_000)
    assert len(cumulative.comparison_rows) == 30
    assert len(efficiency.rows) == 8
    assert len(efficiency.comparison_rows) == 8

    for chart in (cumulative, efficiency):
        pixmap = chart.grab()
        assert not pixmap.isNull()
        assert not chart._paint_error

    income_point = cumulative._point_positions[12][0]
    cumulative.mouseMoveEvent(
        QMouseEvent(
            QEvent.Type.MouseMove,
            QPointF(income_point),
            QPointF(income_point),
            Qt.MouseButton.NoButton,
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
        )
    )
    bar_center = efficiency._bar_rects[5].center()
    efficiency.mouseMoveEvent(
        QMouseEvent(
            QEvent.Type.MouseMove,
            QPointF(bar_center),
            QPointF(bar_center),
            Qt.MouseButton.NoButton,
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
        )
    )
    app.processEvents()

    assert cumulative.hover_index is not None
    assert efficiency.hover_index is not None
    assert efficiency._comparison_rate_points
    assert "entradas acumuladas" in cumulative.accessibleDescription().casefold()
    assert "taxa média" in efficiency.accessibleDescription().casefold()
    assert not cumulative.grab().isNull()
    assert not efficiency.grab().isNull()

    cumulative.close()
    efficiency.close()


def test_savings_efficiency_keeps_every_month_from_the_filter() -> None:
    app = QApplication.instance() or QApplication([])
    theme = Theme()
    chart = SavingsEfficiencyChart(lambda: theme)
    start = date(2025, 1, 1)
    rows = []
    for index in range(18):
        current = add_months(start, index, 1)
        rows.append(
            {
                "month": current.strftime("%Y-%m"),
                "label": current.strftime("%b %Y"),
                "income": 2_000,
                "expenses": 1_400 + index * 20,
                "savings": 600 - index * 20,
                "savings_rate": (600 - index * 20) / 2_000 * 100,
                "partial": index in {0, 17},
            }
        )

    chart.set_rows(rows)
    chart.resize(460, 380)
    chart.show()
    app.processEvents()
    chart.grab()

    assert len(chart.rows) == 18
    assert len(chart._bar_rects) == 18
    assert all(rect.width() < 20 for rect in chart._bar_rects)
    assert not chart._paint_error
    chart.close()


def test_savings_efficiency_average_ignores_blocks_without_income() -> None:
    app = QApplication.instance() or QApplication([])
    chart = SavingsEfficiencyChart(lambda: Theme())
    chart.set_rows(
        [
            {
                "month": "2026-01",
                "label": "Jan 2026",
                "income": 0,
                "expenses": 0,
                "savings": 0,
                "savings_rate": 0,
                "movement_count": 0,
            },
            {
                "month": "2026-02",
                "label": "Fev 2026",
                "income": 2_000,
                "expenses": 1_000,
                "savings": 1_000,
                "savings_rate": 50,
                "movement_count": 2,
            },
        ]
    )

    assert app is not None
    assert "50.0%" in chart.accessibleDescription()
    assert "1 mês com receita" in chart.accessibleDescription()
    assert "2 blocos apresentados" in chart.accessibleDescription()
    chart.close()


def test_financial_pulse_empty_state_replaces_previous_accessible_values() -> None:
    app = QApplication.instance() or QApplication([])
    cumulative = CumulativeCashflowChart(lambda: Theme())
    efficiency = SavingsEfficiencyChart(lambda: Theme())

    cumulative.set_rows(
        [
            {
                "date": "2026-07-01",
                "income": 2_000,
                "expenses": 900,
            }
        ]
    )
    efficiency.set_rows(
        [
            {
                "month": "2026-07",
                "income": 2_000,
                "expenses": 900,
                "savings": 1_100,
                "savings_rate": 55,
            }
        ]
    )
    cumulative.set_rows([])
    efficiency.set_rows([])

    assert app is not None
    assert "sem movimentos" in cumulative.accessibleDescription().casefold()
    assert "sem movimentos" in efficiency.accessibleDescription().casefold()
    cumulative.close()
    efficiency.close()
