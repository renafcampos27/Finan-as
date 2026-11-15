from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from ui.components.charts.diagnostics import (
    ForecastAccuracyChart,
    MerchantParetoChart,
    SpendingSeasonalityChart,
)
from ui.core.theme import Theme


class HoverEvent:
    def __init__(self, position) -> None:
        self._position = position

    def position(self):
        return self._position


@pytest.mark.parametrize(("dark", "privacy"), [(False, False), (True, True)])
def test_diagnostic_charts_render_and_keep_hover_interactive(
    dark: bool,
    privacy: bool,
) -> None:
    app = QApplication.instance() or QApplication([])
    theme = Theme(dark=dark, privacy=privacy)

    forecast = ForecastAccuracyChart(lambda: theme)
    forecast.set_data(
        {
            "accuracy_pct": 84,
            "mae": 42,
            "bias": -8,
            "rows": [
                {
                    "month": f"2026-{month:02d}",
                    "actual": 700 + month * 22,
                    "predicted": 680 + month * 25,
                    "low": 590 + month * 20,
                    "high": 790 + month * 30,
                    "accuracy_pct": 80 + month,
                }
                for month in range(1, 7)
            ],
        }
    )

    seasonality = SpendingSeasonalityChart(lambda: theme)
    seasonality.set_data(
        {
            "total": 4200,
            "maximum": 55,
            "cells": [
                {
                    "weekday": weekday,
                    "weekday_label": ("Seg", "Ter", "Qua", "Qui", "Sex", "Sáb", "Dom")[
                        weekday
                    ],
                    "month": month,
                    "month_label": str(month),
                    "value": float((weekday + month) % 8 * 7),
                    "total": float((weekday + month) % 8 * 70),
                    "movements": weekday + month,
                }
                for weekday in range(7)
                for month in range(1, 13)
            ],
        }
    )

    pareto = MerchantParetoChart(lambda: theme)
    pareto.set_data(
        {
            "rows": [
                {
                    "merchant": f"Comerciante {index}",
                    "total": 600 - index * 80,
                    "share_pct": 30 - index * 3,
                    "cumulative_pct": min(100, 30 + index * 18),
                    "movements": 9 - index,
                }
                for index in range(5)
            ]
        }
    )

    for chart in (forecast, seasonality, pareto):
        chart.resize(960, max(420, chart.minimumHeight()))
        chart.show()
        app.processEvents()
        pixmap = chart.grab()
        assert not pixmap.isNull()
        assert not chart._paint_error

    forecast.mouseMoveEvent(HoverEvent(forecast._point(1, 744, forecast._maximum())))
    assert forecast.hover_index is not None
    seasonality.mouseMoveEvent(HoverEvent(seasonality._cell_rects[0].center()))
    assert seasonality.hover_index is not None
    pareto.mouseMoveEvent(HoverEvent(pareto._row_rects[0].center()))
    assert pareto.hover_index is not None

    for chart in (forecast, seasonality, pareto):
        chart.close()
