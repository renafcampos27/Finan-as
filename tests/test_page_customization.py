from __future__ import annotations

import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QEvent
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import QApplication, QLabel, QPushButton

from ui.card_customization import PageCardCustomizationDialog
from ui.components import BalanceChart, GlassCard
from ui.core import BasePage, Theme


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


class _StatusBar:
    def showMessage(self, *_args) -> None:
        pass


class _App:
    def __init__(self) -> None:
        self.repo = _Repo()
        self.analytics_service = object()
        self.payments_service = object()
        self.import_service = object()
        self.theme = Theme()
        self._status = _StatusBar()

    def statusBar(self):
        return self._status

    def sync_content_size(self) -> None:
        pass


class _CustomizablePage(BasePage):
    def __init__(self, app: _App) -> None:
        super().__init__(app, "Página de teste")
        self.filters_reset = 0
        self.first = GlassCard("📈 Primeiro gráfico", strong=True)
        self.first.setProperty("pageCardKey", "first")
        self.chart = BalanceChart(lambda: self.app.theme)
        self.first.v.addWidget(self.chart)
        self.second = GlassCard("💡 Segunda informação")
        self.second.setProperty("pageCardKey", "second")
        self.body.addWidget(self.first)
        self.body.addWidget(self.second)

    def _reset_page_filters(self) -> None:
        self.filters_reset += 1


def test_every_base_page_can_hide_disable_restore_cards_and_reset_zoom(qapp):
    app = _App()
    page = _CustomizablePage(app)
    app.repo.set_setting(
        page._card_setting_key(),
        json.dumps({"hidden": ["first"], "permanent": ["second"]}),
    )

    page.apply_card_preferences()
    assert page.first.isHidden()
    assert page.second.isHidden()
    assert not page.second.updatesEnabled()
    assert page.should_compute_card(page.first)
    assert not page.should_compute_card(page.second)
    assert (
        page.reset_view_btn.toolTip()
        == "Repor zoom, filtros e comparações fixadas desta página"
    )
    assert page.customize_cards_btn.isEnabled()

    page.chart.zoom_factor = 2.4
    page.chart.pan_offset = 120.0
    page.chart.pin_detail_item({"date": "2026-07-01", "balance": 500})
    page.reset_page_view()
    assert page.filters_reset == 1
    assert page.chart.zoom_factor == pytest.approx(1.0)
    assert page.chart.pan_offset == pytest.approx(0.0)
    assert page.chart._pinned_detail_item is None

    app.repo.set_setting(
        page._card_setting_key(),
        json.dumps({"hidden": [], "permanent": []}),
    )
    page.apply_card_preferences()
    assert not page.first.isHidden()
    assert not page.second.isHidden()
    assert page.second.updatesEnabled()


def test_card_scan_ignores_qt_wrappers_with_deleted_title_labels(qapp):
    app = _App()
    page = _CustomizablePage(app)
    stale = GlassCard("Card antigo")
    live = GlassCard("Card atual")
    page.body.addWidget(stale)
    page.body.addWidget(live)

    stale_title = stale.title_label
    assert stale_title is not None
    stale_title.deleteLater()
    QApplication.sendPostedEvents(stale_title, QEvent.DeferredDelete)

    # This is the short lifecycle window that previously raised
    # "Internal C++ object already deleted" during navigation.
    cards = page.page_cards()
    titles = {title for _key, title, _card in cards}
    page.apply_card_preferences()

    assert "Card antigo" not in titles
    assert "Card atual" in titles


@pytest.mark.parametrize("dark", [False, True])
def test_card_customization_dialog_has_explicit_readable_theme(qapp, dark):
    app = _App()
    app.theme = Theme(dark=dark)
    page = _CustomizablePage(app)
    dialog = PageCardCustomizationDialog(
        "Dashboard",
        [
            {"key": "balance", "title": "📈 Saldo e previsão", "visible": True},
            {
                "key": "categories",
                "title": "🧩 Peso das categorias",
                "visible": False,
                "permanent": False,
            },
        ],
        page,
    )
    dialog.show()
    qapp.processEvents()

    background = dialog.palette().color(QPalette.Window)
    text = dialog.palette().color(QPalette.WindowText)
    brightness_gap = (
        abs(
            (text.red() * 299 + text.green() * 587 + text.blue() * 114)
            - (
                background.red() * 299
                + background.green() * 587
                + background.blue() * 114
            )
        )
        / 1000
    )

    assert brightness_gap > 140
    assert "CardCustomizationTable" in dialog.styleSheet()
    assert any(
        label.objectName() == "CardCustomizationHeading"
        and "Personalizar Dashboard" in label.text()
        for label in dialog.findChildren(QLabel)
    )
    assert {button.text() for button in dialog.findChildren(QPushButton)} >= {
        "Guardar",
        "Cancelar",
        "Restaurar todos",
    }
    assert not dialog.grab().isNull()
    dialog.close()


def test_card_customization_dialog_is_clamped_to_the_available_screen(qapp):
    app = _App()
    page = _CustomizablePage(app)
    page.resize(960, 640)
    page.show()
    entries = [
        {
            "key": f"card_{index}",
            "title": f"Card financeiro {index + 1}",
            "visible": True,
            "permanent": False,
        }
        for index in range(30)
    ]
    dialog = PageCardCustomizationDialog("Dashboard", entries, page)
    dialog.show()
    qapp.processEvents()

    available = dialog.screen().availableGeometry()
    assert dialog.width() <= max(320, available.width() - 48)
    assert dialog.height() <= max(320, available.height() - 72)
    assert dialog.table.rowCount() == 30
    assert dialog.table.verticalScrollBar().maximum() > 0
    assert not dialog.grab().isNull()

    dialog.close()
    page.close()


def test_is_qt_object_valid_helper(qapp):
    from unittest.mock import MagicMock
    from shiboken6 import delete
    from ui.core.page import _is_qt_object_valid

    assert not _is_qt_object_valid(None)
    assert _is_qt_object_valid(MagicMock()) is True

    lbl = QLabel("test")
    assert _is_qt_object_valid(lbl)

    delete(lbl)
    assert not _is_qt_object_valid(lbl)
    assert not BasePage._qt_object_is_valid(lbl)


def test_glass_card_card_title_and_key_with_deleted_label(qapp):
    from shiboken6 import delete

    card = GlassCard("📈 Previsão de Tesouraria")
    assert card.card_title() == "📈 Previsão de Tesouraria"
    assert card.card_key() == "previsao_de_tesouraria"

    assert card.title_label is not None
    delete(card.title_label)

    assert card.card_title() == ""
    assert card.card_key() == ""


def test_glass_card_set_data_coverage_with_deleted_internal_labels(qapp):
    from shiboken6 import delete

    card = GlassCard("Previsão")
    card.set_footer("Nota metodológica")
    assert card.footer_label is not None

    delete(card.footer_label)

    card.set_data_coverage("Cobertura real: 12 meses", "positive")
    assert card.data_coverage_label is not None
    assert card.data_coverage_label.text() == "Cobertura real: 12 meses"

    delete(card.data_coverage_label)
    card.set_data_coverage("Cobertura real: 24 meses", "positive")
    assert card.data_coverage_label is not None
    assert card.data_coverage_label.text() == "Cobertura real: 24 meses"


def test_glass_card_set_context_badge_with_deleted_title_label(qapp):
    from shiboken6 import delete

    card = GlassCard("Projeção")
    assert card.title_label is not None
    delete(card.title_label)

    card.set_context_badge("Mês atual", "warning")
    assert card.context_badge is not None
    assert card.context_badge.text() == "Mês atual"


def test_glass_card_apply_phrase_preferences_with_deleted_labels(qapp):
    from shiboken6 import delete

    card = GlassCard("Previsão")
    card.set_description("Descrição inicial")
    card.set_footer("Rodapé inicial")
    card.set_confidence_badge("Alta")
    card.set_legend("Legenda inicial")

    delete(card.description_label)
    delete(card.footer_label)
    delete(card.confidence_badge)
    delete(card.legend_label)
    delete(card.title_label)

    card.apply_phrase_preferences()


def test_apply_default_forecast_coverage_with_deleted_card_title_label(qapp):
    from unittest.mock import MagicMock
    from shiboken6 import delete

    app = _App()
    app.analytics_service = MagicMock()
    app.analytics_service.data_coverage_snapshot.return_value = {
        "active_months": 12,
        "expected_months": 24,
        "invalid_transactions_excluded": 0,
    }

    page = _CustomizablePage(app)
    predictive_card = GlassCard("📈 Previsão de Saldo", parent=page)
    page.body.addWidget(predictive_card)

    assert predictive_card.title_label is not None
    delete(predictive_card.title_label)

    # Recreating the user scenario: refresh calling apply_default_forecast_coverage
    # This must NOT raise RuntimeError: libshiboken: Internal C++ object (PySide6.QtWidgets.QLabel) already deleted.
    page.apply_default_forecast_coverage()


def test_apply_default_forecast_coverage_with_deleted_card(qapp):
    from unittest.mock import MagicMock
    from shiboken6 import delete

    app = _App()
    app.analytics_service = MagicMock()
    app.analytics_service.data_coverage_snapshot.return_value = {
        "active_months": 12,
        "expected_months": 24,
        "invalid_transactions_excluded": 0,
    }

    page = _CustomizablePage(app)
    predictive_card = GlassCard("Previsão futura", parent=page)
    page.body.addWidget(predictive_card)

    delete(predictive_card)

    page.apply_default_forecast_coverage()


def test_apply_default_forecast_coverage_success(qapp):
    from unittest.mock import MagicMock

    app = _App()
    app.analytics_service = MagicMock()
    del app.analytics_service.data_coverage_label
    app.analytics_service.data_coverage_snapshot.return_value = {
        "active_months": 12,
        "expected_months": 24,
        "invalid_transactions_excluded": 0,
    }

    page = _CustomizablePage(app)
    predictive_card = GlassCard("📈 Previsão de Saldo", parent=page)
    page.body.addWidget(predictive_card)

    page.apply_default_forecast_coverage()
    assert predictive_card.data_coverage_label is not None
    assert "12/24 meses" in predictive_card.data_coverage_label.text()


def test_page_cards_tolerates_deleted_title_label(qapp):
    from shiboken6 import delete

    app = _App()
    page = _CustomizablePage(app)
    card = GlassCard("Novo Cartão", parent=page)
    page.body.addWidget(card)
    assert card.title_label is not None
    delete(card.title_label)

    # Should not crash
    cards = page.page_cards()
    assert not any(c is card for _, _, c in cards)


def test_metric_card_set_data_with_deleted_labels(qapp):
    from shiboken6 import delete
    from ui.components.cards import MetricCard

    card = MetricCard("Despesas", "100 €", "+5 €", "#0A84FF", "💶")
    assert card.title_label is not None
    delete(card.title_label)
    delete(card.delta_label)

    card.set_data(value="120 €")


def test_metric_card_with_deleted_child_widgets(qapp):
    from shiboken6 import delete
    from ui.components.cards import MetricCard, _apply_tone

    card = MetricCard("Receitas", "200 €", "-10 €", "#30D158", "💰")
    delete(card.title_label)
    delete(card.delta_label)
    delete(card.status_label)
    delete(card.sparkline)
    delete(card.progress_bar)

    card.set_data(value="250 €", delta="-5 €", status="Normal", progress=50)
    elems = card.available_customization_elements()
    assert isinstance(elems, dict)
    card.set_clickable(True, "Ver detalhes")
    _apply_tone(card.status_label, "positive")


def test_apply_chart_calculation_contexts_tolerates_deleted_chart(qapp):
    from shiboken6 import delete
    from ui.components.charts.base import StableChart

    app = _App()
    page = _CustomizablePage(app)
    chart = StableChart(lambda: app.theme, parent=page)
    delete(chart)

    page.apply_chart_calculation_contexts()


def test_set_card_title_and_set_compact_header_with_deleted_title(qapp):
    from shiboken6 import delete

    card = GlassCard("Cartão Inicial")
    assert card.title_label is not None
    delete(card.title_label)

    # Calling set_card_title when title_label was deleted must not raise RuntimeError
    card.set_card_title("Novo Título")
    assert card._original_title == "Novo Título"

    # Calling set_compact_header when title_label was deleted must not raise RuntimeError
    card.set_compact_header(word_wrap=True, fit_context_header=True)


def test_fit_label_height_and_fit_widget_height_with_deleted_widgets(qapp):
    from PySide6.QtWidgets import QWidget
    from shiboken6 import delete

    card = GlassCard("Cartão")
    lbl = QLabel("Rótulo", card)
    widget = QWidget(card)

    delete(lbl)
    delete(widget)

    # Must not raise RuntimeError
    card.fit_label_height(lbl)
    card.fit_widget_height(widget)


def test_repolish_with_deleted_widget(qapp):
    from shiboken6 import delete

    lbl = QLabel("teste")
    delete(lbl)
    # Must not raise RuntimeError
    BasePage._repolish(lbl)


def test_page_refresh_indicators_with_deleted_data_updated_label(qapp):
    from shiboken6 import delete

    app = _App()
    page = _CustomizablePage(app)
    assert page.data_updated_label is not None
    delete(page.data_updated_label)

    # Must not raise RuntimeError when data_updated_label was deleted
    page.begin_data_refresh()
    page.mark_data_updated()
    page.mark_data_stale()
    page.mark_data_refresh_failed()


def test_card_helpers_with_deleted_layout(qapp):
    from shiboken6 import delete

    card = GlassCard("Cartão")
    delete(card.v)

    # All helper methods must tolerate deleted self.v
    card.set_description("Nova descrição")
    card.set_legend("Nova legenda")
    card.set_footer("Novo rodapé")
    card.set_data_coverage("Cobertura real 12 meses")
    card.set_confidence_badge("Alta")
    card.apply_density("compact")
    card.set_compact_header()




