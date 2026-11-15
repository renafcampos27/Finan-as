from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from ui.components import FixedCommitmentsChart, NetWorthBridgeChart
from ui.core import Theme


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_net_worth_bridge_chart_renders_and_exposes_details(qapp):
    chart = NetWorthBridgeChart(lambda: Theme(dark=False))
    chart.resize(1_000, 360)
    chart.set_data(
        {
            "change": 1_600,
            "coverage_pct": 100,
            "steps": [
                {
                    "key": "opening",
                    "label": "Património inicial",
                    "value": 5_700,
                    "start": 0,
                    "end": 5_700,
                    "role": "total",
                },
                {
                    "key": "income",
                    "label": "Receitas",
                    "value": 2_000,
                    "start": 5_700,
                    "end": 7_700,
                    "before": 5_700,
                    "after": 7_700,
                    "role": "change",
                    "tone": "positive",
                },
                {
                    "key": "expenses",
                    "label": "Despesas",
                    "value": -1_300,
                    "start": 7_700,
                    "end": 6_400,
                    "before": 7_700,
                    "after": 6_400,
                    "role": "change",
                    "tone": "negative",
                },
                {
                    "key": "closing",
                    "label": "Património final",
                    "value": 6_400,
                    "start": 0,
                    "end": 6_400,
                    "role": "total",
                },
            ],
        }
    )
    chart.show()
    qapp.processEvents()

    image = chart.grab().toImage()
    payload = chart.detail_payload(chart.rows[1])

    assert not image.isNull()
    assert chart._paint_error == ""
    assert payload["subtitle"] == "Receitas"
    assert chart.accessibleName() == "Ponte interativa do património"
    chart.close()


def test_fixed_commitments_chart_renders_stacked_months(qapp):
    chart = FixedCommitmentsChart(lambda: Theme(dark=True))
    chart.resize(1_000, 350)
    chart.set_data(
        {
            "monthly_average": 130,
            "rows": [
                {
                    "month": f"2026-{month:02d}",
                    "label": f"{month:02d}/26",
                    "subscriptions": 30,
                    "scheduled": 100 if month <= 3 else 0,
                    "total": 130 if month <= 3 else 30,
                    "event_count": 2 if month <= 3 else 1,
                    "events": [],
                }
                for month in range(1, 13)
            ],
        }
    )
    chart.show()
    qapp.processEvents()

    image = chart.grab().toImage()
    payload = chart.detail_payload(chart.rows[0])

    assert not image.isNull()
    assert chart._paint_error == ""
    assert payload["subtitle"] == "01/26"
    assert len(chart._bar_hits) == 12
    assert chart.accessibleName() == "Evolução dos compromissos fixos"
    chart.close()
