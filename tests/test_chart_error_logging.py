from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from ui.components.charts.base import StableChart
from ui.core import Theme


class _FailingChart(StableChart):
    def __init__(self):
        super().__init__(lambda: Theme(dark=False))
        self.should_fail = True
        self.points = [{"date": "2026-07-01"}]

    def render_chart(self, painter) -> None:
        if self.should_fail:
            raise RuntimeError("simulated chart rendering failure")


def test_chart_render_failures_are_logged_once_until_recovery(monkeypatch):
    app = QApplication.instance() or QApplication([])
    reported: list[tuple[str, BaseException | None]] = []
    monkeypatch.setattr(
        "ui.components.charts.base.log_exception",
        lambda context, exc=None: reported.append((context, exc)),
    )
    chart = _FailingChart()
    chart.resize(480, 280)
    chart.show()
    app.processEvents()
    chart.grab()

    assert chart._paint_error == "simulated chart rendering failure"
    assert len(reported) == 1
    assert "chart=_FailingChart" in reported[0][0]
    assert "items=1" in reported[0][0]

    chart.invalidate_cache()
    chart.grab()
    assert len(reported) == 1

    chart.should_fail = False
    chart.invalidate_cache()
    chart.grab()
    assert chart._paint_error == ""

    chart.should_fail = True
    chart.invalidate_cache()
    chart.grab()
    assert len(reported) == 2
    chart.close()
