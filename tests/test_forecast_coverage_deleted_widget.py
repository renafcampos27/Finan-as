"""Tests verifying that deleted Qt objects/labels never cause Shiboken RuntimeError crashes."""


from unittest.mock import MagicMock

import pytest
from PySide6.QtWidgets import QApplication, QLabel
from shiboken6 import delete

from ui.components.cards import GlassCard, MetricCard
from ui.core.page import BasePage, _is_qt_object_valid


class DummyPage(BasePage):
    """Minimal BasePage subclass for testing card coverage and freshness tracking."""

    def __init__(self, app=None):
        mock_app = app or MagicMock()
        mock_app.repo = MagicMock()
        mock_app.repo.setting.return_value = "comfortable"
        mock_app.analytics_service = MagicMock()
        del mock_app.analytics_service.data_coverage_label
        mock_app.payments_service = MagicMock()
        mock_app.import_service = MagicMock()
        mock_app.theme = MagicMock()
        mock_app.theme.privacy = False
        super().__init__(mock_app, "Dummy Page")


@pytest.fixture
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_is_qt_object_valid_helper(qapp):
    """Test _is_qt_object_valid correctly detects valid, None, and deleted objects."""
    assert not _is_qt_object_valid(None)
    assert _is_qt_object_valid(MagicMock()) is True

    lbl = QLabel("test")
    assert _is_qt_object_valid(lbl)

    delete(lbl)
    assert not _is_qt_object_valid(lbl)
    assert not BasePage._qt_object_is_valid(lbl)


def test_glass_card_card_title_and_key_with_deleted_label(qapp):
    """Test card_title and card_key safely handle deleted title_label."""
    card = GlassCard("📈 Previsão de Tesouraria")
    assert card.card_title() == "📈 Previsão de Tesouraria"
    assert card.card_key() == "previsao_de_tesouraria"

    # Delete the underlying QLabel
    assert card.title_label is not None
    delete(card.title_label)

    # After deletion, accessing card_title and card_key must NOT raise RuntimeError
    assert card.card_title() == ""
    assert card.card_key() == ""


def test_glass_card_set_data_coverage_with_deleted_internal_labels(qapp):
    """Test set_data_coverage and set_confidence_badge when labels were deleted."""
    card = GlassCard("Previsão")
    card.set_footer("Nota metodológica")
    assert card.footer_label is not None

    # Delete footer_label
    delete(card.footer_label)

    # set_data_coverage should safely handle deleted footer_label
    card.set_data_coverage("Cobertura real: 12 meses", "positive")
    assert card.data_coverage_label is not None
    assert card.data_coverage_label.text() == "Cobertura real: 12 meses"

    # Delete data_coverage_label and re-apply
    delete(card.data_coverage_label)
    card.set_data_coverage("Cobertura real: 24 meses", "positive")
    assert card.data_coverage_label is not None
    assert card.data_coverage_label.text() == "Cobertura real: 24 meses"


def test_glass_card_set_context_badge_with_deleted_title_label(qapp):
    """Test set_context_badge when title_label was deleted."""
    card = GlassCard("Projeção")
    assert card.title_label is not None
    delete(card.title_label)

    # Calling set_context_badge must not crash on deleted title_label
    card.set_context_badge("Mês atual", "warning")
    assert card.context_badge is not None
    assert card.context_badge.text() == "Mês atual"


def test_glass_card_apply_phrase_preferences_with_deleted_labels(qapp):
    """Test apply_phrase_preferences does not crash on deleted child widgets."""
    card = GlassCard("Previsão")
    card.set_description("Descrição inicial")
    card.set_footer("Rodapé inicial")
    card.set_confidence_badge("Alta")
    card.set_legend("Legenda inicial")

    # Delete all internal labels
    delete(card.description_label)
    delete(card.footer_label)
    delete(card.confidence_badge)
    delete(card.legend_label)
    delete(card.title_label)

    # apply_phrase_preferences must not raise RuntimeError
    card.apply_phrase_preferences()


def test_apply_default_forecast_coverage_with_deleted_card_title_label(qapp):
    """Recreate the exact user traceback: card with deleted title_label during page refresh."""
    page = DummyPage()
    card1 = GlassCard("📈 Saldo histórico e previsão", parent=page)
    _card2 = GlassCard("Outro cartão normal", parent=page)

    page.analytics_service.data_coverage_snapshot.return_value = {
        "active_months": 12,
        "expected_months": 24,
        "invalid_transactions_excluded": 0,
    }

    # Simulate C++ deletion of card1's title_label (e.g. from dynamic re-layout)
    assert card1.title_label is not None
    delete(card1.title_label)

    # Now apply_default_forecast_coverage must execute cleanly without RuntimeError
    page.apply_default_forecast_coverage()


def test_apply_default_forecast_coverage_with_deleted_card(qapp):
    """Test apply_default_forecast_coverage when an entire card was deleted via deleteLater/delete."""
    page = DummyPage()
    card = GlassCard("Previsão futura", parent=page)
    delete(card)

    page.analytics_service.data_coverage_snapshot.return_value = {
        "active_months": 12,
        "expected_months": 24,
        "invalid_transactions_excluded": 0,
    }

    # Must not crash when encountering the deleted card
    page.apply_default_forecast_coverage()


def test_apply_default_forecast_coverage_success(qapp):
    """Test apply_default_forecast_coverage successfully attaches badge to intact card."""
    page = DummyPage()
    card = GlassCard("📈 Previsão de Saldo", parent=page)

    page.analytics_service.data_coverage_snapshot.return_value = {
        "active_months": 12,
        "expected_months": 24,
        "invalid_transactions_excluded": 0,
    }

    page.apply_default_forecast_coverage()
    assert card.data_coverage_label is not None
    assert "12/24 meses" in card.data_coverage_label.text()


def test_page_cards_tolerates_deleted_title_label(qapp):
    """Test page_cards ignores or handles cards with deleted title labels."""
    page = DummyPage()
    card = GlassCard("Cartão com título", parent=page)
    assert card.title_label is not None
    delete(card.title_label)

    # Should not crash, and should skip the card whose title cannot be retrieved
    cards = page.page_cards()
    assert len(cards) == 0


def test_apply_chart_calculation_contexts_tolerates_deleted_chart(qapp):
    """Test apply_chart_calculation_contexts ignores deleted StableChart objects."""
    from ui.components.charts.base import StableChart

    page = DummyPage()
    chart = StableChart(lambda: page.app.theme, parent=page)
    delete(chart)

    # Must execute cleanly without RuntimeError
    page.apply_chart_calculation_contexts()


def test_metric_card_set_data_with_deleted_labels(qapp):
    """Test MetricCard.set_data handles deleted labels without RuntimeError."""
    card = MetricCard("Despesas", "100 €", "+5 €", "#0A84FF", "💶")
    assert card.title_label is not None
    delete(card.title_label)
    delete(card.delta_label)

    # set_data must not crash on deleted labels
    card.set_data(value="120 €")


def test_set_card_title_and_set_compact_header_with_deleted_title(qapp):
    """Test set_card_title and set_compact_header safely handle deleted title_label."""
    card = GlassCard("Cartão Inicial")
    assert card.title_label is not None
    delete(card.title_label)

    # Calling set_card_title when title_label was deleted must not raise RuntimeError
    card.set_card_title("Novo Título")
    assert card._original_title == "Novo Título"

    # Calling set_compact_header when title_label was deleted must not raise RuntimeError
    card.set_compact_header(word_wrap=True, fit_context_header=True)


def test_fit_label_height_and_fit_widget_height_with_deleted_widgets(qapp):
    """Test fit_label_height and fit_widget_height safely tolerate deleted widgets."""
    from PySide6.QtWidgets import QWidget

    card = GlassCard("Cartão")
    lbl = QLabel("Rótulo", card)
    widget = QWidget(card)

    delete(lbl)
    delete(widget)

    # Must not raise RuntimeError
    card.fit_label_height(lbl)
    card.fit_widget_height(widget)


def test_repolish_with_deleted_widget(qapp):
    """Test _repolish safely handles deleted Qt widgets."""
    lbl = QLabel("teste")
    delete(lbl)
    # Must not raise RuntimeError
    BasePage._repolish(lbl)


def test_page_refresh_indicators_with_deleted_data_updated_label(qapp):
    """Test BasePage refresh state methods safely handle deleted data_updated_label."""
    page = DummyPage()
    assert page.data_updated_label is not None
    delete(page.data_updated_label)

    # Must not raise RuntimeError when data_updated_label was deleted
    page.begin_data_refresh()
    page.mark_data_updated()
    page.mark_data_stale()
    page.mark_data_refresh_failed()


def test_card_helpers_with_deleted_layout(qapp):
    """Test GlassCard helpers gracefully tolerate deleted self.v."""
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

    # Geometry queries and resizeEvent must not crash on deleted self.v
    assert card.minimumSizeHint() is not None
    assert card.sizeHint() is not None
    card.resize(250, 250)


def test_card_sync_compact_label_heights_with_deleted_card(qapp):
    """Test _sync_compact_label_heights safely tolerates deleted card."""
    card = GlassCard("Cartão")
    card._compact_height_sync_pending = True
    delete(card)

    # Direct invocation on deleted object must safely return without crash
    card._sync_compact_label_heights()


def test_table_state_with_deleted_parent_title_label(qapp):
    """Test table state resolution handles parent card with deleted title_label."""
    from PySide6.QtWidgets import QTableView
    from ui.core.table_state import TableColumnWidthManager

    card = GlassCard("Tabela Cartão")
    table = QTableView(card)
    assert card.title_label is not None
    delete(card.title_label)

    manager = TableColumnWidthManager(MagicMock())
    identity = manager._legacy_state_key(table)
    assert identity is not None
    assert len(identity) > 0


def test_page_card_customization_dialog_phrase_with_deleted_card(qapp, monkeypatch):
    """Test PageCardCustomizationDialog._edit_card_phrase with deleted card."""
    from ui.card_customization import PageCardCustomizationDialog
    from ui.card_phrase_dialog import CardPhraseCustomizationDialog

    card = GlassCard("Meu Cartão")
    delete(card)

    dialog = PageCardCustomizationDialog(
        "Teste",
        [{"key": "card_key", "title": "Meu Cartão", "card": card}],
    )
    monkeypatch.setattr(CardPhraseCustomizationDialog, "exec", lambda self: 0)
    # Must not raise RuntimeError when clicking phrase button on deleted card
    dialog._edit_card_phrase({"key": "card_key", "title": "Meu Cartão", "card": card})
    dialog.close()


def test_dashboard_render_charts_with_deleted_pulse_and_savings_title_labels(qapp):
    """Test DashboardPage._on_data_loaded tolerates deleted title labels on pulse/savings cards."""
    from ui.pages.dashboard import DashboardPage

    mock_app = MagicMock()
    mock_app.repo = MagicMock()
    mock_app.repo.setting.return_value = "comfortable"
    mock_app.analytics_service = MagicMock()
    del mock_app.analytics_service.data_coverage_label
    mock_app.payments_service = MagicMock()
    mock_app.import_service = MagicMock()
    mock_app.theme = MagicMock()
    mock_app.theme.privacy = False
    mock_app.theme.dark = False

    page = DashboardPage(mock_app)
    assert page.cashflow_pulse_card.title_label is not None
    assert page.savings_efficiency_card.title_label is not None
    delete(page.cashflow_pulse_card.title_label)
    delete(page.savings_efficiency_card.title_label)

    data = {
        "totals": {"income": 1000.0, "expenses": 500.0, "savings": 500.0},
        "totals_prev": {},
        "savings_rate": 50.0,
        "balance": 1000.0,
        "net": 1000.0,
        "monthly_expense": 500.0,
        "expense_structure": {},
        "emergency_reserve": {"available": False},
        "work_value": {"available": False},
        "income_context": {},
        "categories": [],
        "previous_categories": [],
        "forecast_data_context": {},
        "data_context": {},
        "forecast": [],
        "cashflow_pulse": [{"date": "2026-09-01", "inflow": 100, "outflow": 50}],
        "cashflow_pulse_previous": [],
        "savings_efficiency": [{"month": "2026-09", "margin": 50, "rate": 0.5}],
        "savings_efficiency_previous": [],
    }
    # _on_data_loaded updates pulse and savings cards; must not raise RuntimeError
    page._on_data_loaded(page._dashboard_request_id, data)



