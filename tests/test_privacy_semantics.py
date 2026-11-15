from __future__ import annotations

import pytest
from PySide6.QtWidgets import (
    QApplication,
    QDoubleSpinBox,
    QLineEdit,
    QWidget,
)

from ui.components.charts import MonteCarloChart, ResilienceScenarioChart
from ui.core import MoneyInputPrivacyManager, Theme
from utils.helpers import eur


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _metric_values(payload: dict) -> dict[str, str]:
    return {
        str(metric.get("label") or ""): str(metric.get("value") or "")
        for metric in payload.get("metrics", [])
    }


def test_chart_detail_schema_masks_monte_carlo_money(qapp):
    theme = Theme(privacy=True)
    chart = MonteCarloChart(lambda: theme)
    row = {
        "year": 5,
        "invested": 15_000.0,
        "p10": 13_000.0,
        "p50": 18_000.0,
        "p90": 24_000.0,
        "probability_target": 72.5,
    }
    chart.set_data({"rows": [row], "target": 20_000})

    values = _metric_values(chart.detail_payload(row))

    assert values["Investido"] == eur(15_000, True)
    assert values["Percentil 10"] == eur(13_000, True)
    assert values["Mediana"] == eur(18_000, True)
    assert values["Percentil 90"] == eur(24_000, True)
    assert values["Probabilidade do objetivo"] == "72,5%"


def test_chart_detail_schema_masks_resilience_scenarios(qapp):
    theme = Theme(privacy=True)
    chart = ResilienceScenarioChart(lambda: theme)
    row = {
        "month": 6,
        "base": 12_000.0,
        "optimistic": 15_000.0,
        "stressed": 7_000.0,
        "p10": 6_000.0,
        "p50": 11_000.0,
        "p90": 16_000.0,
    }
    chart.set_data([row])

    values = _metric_values(chart.detail_payload(row))

    for label in (
        "Cenário base",
        "Cenário otimista",
        "Cenário de stress",
        "Percentil 10",
        "Mediana",
        "Percentil 90",
    ):
        assert values[label] == eur(1, True)


def test_money_inputs_are_masked_only_outside_active_editing(qapp):
    owner = QWidget()
    manager = MoneyInputPrivacyManager(owner)
    qapp.installEventFilter(manager)
    money = QDoubleSpinBox(owner)
    money.setSuffix(" €")
    money.setValue(12_345.67)
    percent = QDoubleSpinBox(owner)
    percent.setSuffix(" %")
    owner.show()
    qapp.processEvents()

    try:
        manager.set_privacy(True)
        money.clearFocus()
        qapp.processEvents()
        assert money.lineEdit().echoMode() == QLineEdit.EchoMode.Password
        assert bool(money.property("privacyMasked")) is True
        assert percent.lineEdit().echoMode() == QLineEdit.EchoMode.Normal

        money.setFocus()
        qapp.processEvents()
        assert money.lineEdit().echoMode() == QLineEdit.EchoMode.Normal

        manager.set_privacy(False)
        assert money.lineEdit().echoMode() == QLineEdit.EchoMode.Normal
    finally:
        manager.set_privacy(False)
        qapp.removeEventFilter(manager)
        owner.close()
