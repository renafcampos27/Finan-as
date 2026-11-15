from __future__ import annotations

import json
import re

import pytest
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QComboBox,
    QHBoxLayout,
    QPushButton,
    QWidget,
)

from ui.components.cards import GlassCard
from ui.components.charts import BalanceChart, PeriodComparisonChart
from ui.components.page_tools import PageToolsBar
from ui.core import BasePage, Theme, apply_combo_theme
from utils.helpers import format_period_variation
from utils.output_security import privacy_export_row


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


class _Repo:
    def __init__(self) -> None:
        self.settings: dict[str, str] = {}

    def setting(self, key: str, default: str = "") -> str:
        return self.settings.get(key, default)

    def set_setting(self, key: str, value: str) -> None:
        self.settings[key] = value


class _Status:
    def showMessage(self, *_args) -> None:
        pass


class _App:
    def __init__(self) -> None:
        self.repo = _Repo()
        self.analytics_service = object()
        self.payments_service = object()
        self.import_service = object()
        self.theme = Theme()
        self._status = _Status()

    def statusBar(self):
        return self._status

    def sync_content_size(self) -> None:
        pass


class _ViewPage(BasePage):
    def __init__(self, app: _App) -> None:
        super().__init__(app, "Vista persistente")
        self.filters = GlassCard("Filtros", strong=True)
        row = QHBoxLayout()
        self.period = QComboBox()
        self.period.addItem("Mês", "month")
        self.period.addItem("Ano", "year")
        row.addWidget(self.period)
        self.range_group = QButtonGroup(self)
        for label, value in (("30 dias", 30), ("90 dias", 90)):
            button = QPushButton(label)
            button.setCheckable(True)
            button.setProperty("filterValue", value)
            button.setChecked(value == 30)
            self.range_group.addButton(button)
            row.addWidget(button)
        self.filters.v.addLayout(row)
        self.register_filter_panel(self.filters, self.filters.setVisible)
        self.body.addWidget(self.filters)
        self.chart = BalanceChart(lambda: self.app.theme)
        self.chart.set_points(
            [
                {"date": "2026-07-01", "balance": 100.0},
                {"date": "2026-07-02", "balance": 250.0},
            ]
        )
        self.chart_card = GlassCard("Gráfico")
        self.chart_card.v.addWidget(self.chart)
        self.body.addWidget(self.chart_card)


def test_huge_variation_uses_multiplier_previous_value_and_explanation() -> None:
    result = format_period_variation(8_721.0, 100.0)

    assert result["huge"] is True
    assert result["display"] == "87,2×"
    assert result["percentage"] == pytest.approx(8_621.0)
    assert result["context"] == "anterior: 100 €"
    assert "base anterior era muito pequena" in result["explanation"]

    no_base = format_period_variation(500.0, 0.0)
    assert no_base["display"] == "sem base anterior"
    assert no_base["percentage"] is None


def test_privacy_export_masks_money_and_sensitive_text() -> None:
    values = privacy_export_row(
        ["Jantar especial", "Conta principal", -72.5, "Restaurantes"],
        ["description", "account", "amount", "category"],
        True,
    )

    assert values == ["Dados ocultos", "Dados ocultos", "••••• €", "Restaurantes"]
    assert privacy_export_row(["=2+2"], ["category"], False) == ["'=2+2"]


def test_theme_has_solid_controls_status_and_larger_scrollbars(qapp) -> None:
    dark = Theme(dark=True)
    css = dark.stylesheet()
    assert re.search(r"QScrollBar:vertical\s*\{[^}]*width:\s*14px", css, re.S)
    assert "QStatusBar#AppStatusBar" in css
    assert "QLineEdit#GlobalSearch" in css
    assert "QCheckBox::indicator:checked" in css
    assert "QToolButton#ChartActionButton" in css
    assert not re.search(r"@[a-z_]+@", css)

    combo = QComboBox()
    combo.addItems(["Primeiro", "Segundo"])
    apply_combo_theme(combo, dark)
    assert combo.palette().color(QPalette.Base).name().upper() == "#172033"
    assert combo.view().palette().color(QPalette.Base).name().upper() == "#111827"


def test_cards_share_semantic_slots_and_restore_density(qapp) -> None:
    card = GlassCard("Previsão", strong=True)
    original = card.v.contentsMargins()
    original_spacing = card.v.spacing()
    card.set_description("O que este gráfico mede.")
    card.set_legend("Azul: base · Verde: otimista")
    card.set_footer("Método baseado apenas nos dados guardados.")
    card.set_confidence_badge("Confiança moderada", "warning")
    card.apply_density("compact")

    assert card.description_label.text().startswith("O que")
    assert card.legend_label.objectName() == "CardLegend"
    assert card.footer_label.objectName() == "CardFooter"
    assert card.confidence_badge.property("tone") == "warning"
    assert card.v.spacing() < original_spacing

    card.apply_density("comfortable")
    restored = card.v.contentsMargins()
    assert (restored.left(), restored.top(), restored.right(), restored.bottom()) == (
        original.left(),
        original.top(),
        original.right(),
        original.bottom(),
    )
    assert card.v.spacing() == original_spacing


def test_page_view_persists_density_filters_collapse_and_zoom(qapp) -> None:
    app = _App()
    first = _ViewPage(app)
    first.set_page_density("compact")
    first.period.setCurrentIndex(1)
    first.range_group.buttons()[1].click()
    first.set_filter_panel_expanded(False)
    first.chart.zoom_factor = 2.4
    first.chart.pan_offset = 82.0

    assert first.persist_page_view_state() is True
    stored = json.loads(app.repo.setting(first._view_state_setting_key(), "{}"))
    assert stored["filters_expanded"] is False
    assert stored["charts"]["chart"]["zoom"] == pytest.approx(2.4)

    second = _ViewPage(app)
    second.restore_page_view_state()
    assert second.page_density == "compact"
    assert second.period.currentData() == "year"
    assert second.range_group.checkedButton().property("filterValue") == 90
    assert second.filters.isHidden()
    assert second.chart.zoom_factor == pytest.approx(2.4)
    assert second.chart.pan_offset == pytest.approx(82.0)


def test_every_chart_can_open_a_representative_detail(qapp) -> None:
    chart = BalanceChart(lambda: Theme())
    chart.set_points(
        [
            {"date": "2026-07-01", "balance": 100.0},
            {"date": "2026-07-02", "balance": 250.0},
        ]
    )
    assert chart.open_selected_detail() is True
    assert chart._last_detail_payload["subtitle"] == "2026-07-02"

    comparison = PeriodComparisonChart(lambda: Theme())
    payload = comparison.detail_payload(
        {
            "category": "Subscrições",
            "current": 8_721.0,
            "reference": 100.0,
            "delta": 8_621.0,
            "change_pct": 8_621.0,
        }
    )
    variation = next(
        metric for metric in payload["metrics"] if metric["label"] == "Variação"
    )
    assert "87,2×" in variation["value"]
    assert "anterior:" in variation["value"]


def test_floating_page_tools_stay_inside_the_viewport(qapp) -> None:
    parent = QWidget()
    parent.resize(960, 640)
    calls: list[str] = []
    tools = PageToolsBar(
        parent,
        on_top=lambda: calls.append("top"),
        on_filters=lambda: calls.append("filters"),
        on_reset=lambda: calls.append("reset"),
    )
    tools.set_context(filter_available=True, scroll_available=True)
    parent.show()
    qapp.processEvents()
    tools.reposition()

    assert tools.isVisible()
    assert parent.rect().contains(tools.geometry())
    tools.filters_button.click()
    tools.reset_button.click()
    assert calls == ["filters", "reset"]
