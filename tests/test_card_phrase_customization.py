from __future__ import annotations

import json

from PySide6.QtGui import QPalette
from PySide6.QtWidgets import QApplication, QPushButton, QWidget
import pytest

from ui.card_customization import PageCardCustomizationDialog
from ui.card_phrase_dialog import CardPhraseCustomizationDialog
from ui.components.cards import GlassCard, MetricCard
from ui.components.expense_work_time import ExpenseWorkTimeCard
from ui.core.theme import Theme


@pytest.fixture
def qapp():
    return QApplication.instance() or QApplication([])


class _MockRepo:
    def __init__(self) -> None:
        self._settings: dict[str, str] = {}

    def setting(self, key: str, default: str = "") -> str:
        return self._settings.get(key, default)

    def set_setting(self, key: str, value: str) -> None:
        self._settings[key] = str(value)


class _MockParent(QWidget):
    def __init__(self, repo: _MockRepo, dark: bool = False) -> None:
        super().__init__()
        self.repo = repo
        self.theme = Theme(dark=dark)
        self.customization_opened = False

    def open_card_customization(self) -> None:
        self.customization_opened = True


@pytest.mark.parametrize("dark", [False, True])
def test_card_phrase_dialog_theme_and_state(qapp, dark: bool):
    theme_parent = QWidget()
    theme_parent.theme = Theme(dark=dark)

    dialog = CardPhraseCustomizationDialog(
        card_title="Despesas em tempo de trabalho",
        original_description="Quanto tempo de trabalho foi necessário.",
        current_description="Texto personalizado pelo utilizador.",
        preferences={
            "show_description": True,
            "show_comparison": True,
            "show_footer": False,
        },
        has_comparison=True,
        has_footer=True,
        parent=theme_parent,
    )
    dialog.show()
    qapp.processEvents()

    # Verify readable theme contrast
    bg = dialog.palette().color(QPalette.Window)
    text = dialog.palette().color(QPalette.WindowText)
    gap = abs(
        (text.red() * 299 + text.green() * 587 + text.blue() * 114)
        - (bg.red() * 299 + bg.green() * 587 + bg.blue() * 114)
    ) / 1000
    assert gap > 140

    # Verify initial controls
    assert dialog.show_description_check.isChecked() is True
    assert dialog.show_comparison_check is not None
    assert dialog.show_comparison_check.isChecked() is True
    assert dialog.show_footer_check is not None
    assert dialog.show_footer_check.isChecked() is False
    assert dialog.text_edit.toPlainText() == "Texto personalizado pelo utilizador."

    # Test state retrieval
    state = dialog.state()
    assert state["show_description"] is True
    assert state["show_comparison"] is True
    assert state["show_footer"] is False
    assert state["custom_description"] == "Texto personalizado pelo utilizador."

    # Test reset to original
    dialog._reset_to_original()
    assert dialog.text_edit.toPlainText() == "Quanto tempo de trabalho foi necessário."
    assert dialog.show_description_check.isChecked() is True
    assert dialog.show_comparison_check.isChecked() is True
    assert dialog.show_footer_check.isChecked() is True

    # State after reset should reflect empty custom text because it matches original
    assert dialog.state()["custom_description"] == ""

    dialog.close()
    theme_parent.close()


def test_glass_card_phrase_preferences_and_persistence(qapp):
    repo = _MockRepo()
    parent = _MockParent(repo)
    card = GlassCard("Fluxo de Caixa", parent)
    card.setProperty("pageCardKey", "cash_flow_card")
    card.set_description("Visão mensal de entradas e saídas.")
    card.set_footer("Fonte: movimentos bancários confirmados.")
    parent.show()
    qapp.processEvents()

    assert card.description_label is not None
    assert card.description_label.text() == "Visão mensal de entradas e saídas."
    assert card.description_label.isVisible() is True
    assert card.footer_label is not None
    assert card.footer_label.text() == "Fonte: movimentos bancários confirmados."
    assert card.footer_label.isVisible() is True

    # Save preference to hide description and customize footer
    prefs = {
        "show_description": False,
        "custom_description": "",
        "show_comparison": True,
        "show_footer": True,
        "custom_footer": "Nota metodológica customizada.",
    }
    card.save_phrase_preferences(prefs)
    card.apply_phrase_preferences()
    qapp.processEvents()

    # In repo, setting was stored as JSON
    raw = repo.setting("card_phrases:cash_flow_card")
    assert raw
    saved = json.loads(raw)
    assert saved["show_description"] is False
    assert saved["custom_footer"] == "Nota metodológica customizada."

    # In card, description is hidden and footer has custom text
    assert card.description_label.isVisible() is False
    assert card.footer_label.isVisible() is True
    assert card.footer_label.text() == "Nota metodológica customizada."

    # Now set a custom description and show it
    card.save_phrase_preferences(
        {
            "show_description": True,
            "custom_description": "Acompanha o teu fluxo diário com rigor.",
            "show_comparison": True,
            "show_footer": False,
            "custom_footer": "",
        }
    )
    card.apply_phrase_preferences()
    qapp.processEvents()
    assert card.description_label.isVisible() is True
    assert card.description_label.text() == "Acompanha o teu fluxo diário com rigor."
    assert card.footer_label.isVisible() is False

    parent.close()


def test_glass_card_context_menu(qapp):
    repo = _MockRepo()
    parent = _MockParent(repo)
    card = GlassCard("Previsão Orçamental", parent)
    card.setProperty("pageCardKey", "budget_forecast")
    card.set_description("Previsão a 12 meses.")
    card.set_footer("Metodologia Monte Carlo.")
    parent.show()
    qapp.processEvents()

    menu = card.create_context_menu()
    actions_seen = [act.text() for act in menu.actions()]

    assert any("Frases · Previsão Orçamental" in a for a in actions_seen)
    assert any("Mostrar frase explicativa" in a for a in actions_seen)
    assert any("Mostrar rodapé / metodologia" in a for a in actions_seen)
    assert any("Personalizar texto da frase…" in a for a in actions_seen)
    assert any("Restaurar frase original" in a for a in actions_seen)
    assert any("Personalizar cards desta página…" in a for a in actions_seen)

    # Test toggling description action from menu
    toggle_desc = [a for a in menu.actions() if "Mostrar frase explicativa" in a.text()][0]
    assert toggle_desc.isChecked() is True
    toggle_desc.trigger()
    qapp.processEvents()

    assert card.phrase_preferences()["show_description"] is False
    assert card.description_label.isVisible() is False

    # Test restore original text action
    reset_action = [a for a in menu.actions() if "Restaurar frase original" in a.text()][0]
    reset_action.trigger()
    qapp.processEvents()

    assert card.phrase_preferences()["show_description"] is True
    assert card.description_label.isVisible() is True

    # Test open page cards customization action
    page_action = [a for a in menu.actions() if "Personalizar cards desta página…" in a.text()][0]
    page_action.trigger()
    assert parent.customization_opened is True

    parent.close()


def test_expense_work_time_card_phrase_isolation_and_customization(qapp):
    repo = _MockRepo()
    parent = _MockParent(repo)
    card = ExpenseWorkTimeCard(parent=parent, theme_provider=lambda: parent.theme)
    parent.show()
    qapp.processEvents()

    # Initial state: default intro
    assert card.intro.isVisible() is True
    assert (
        card.intro.text()
        == "Quanto tempo do teu trabalho líquido foi necessário para pagar cada categoria."
    )

    # Add comparison hint
    card.set_comparison_hint("+12% vs mês anterior")
    assert (
        card.intro.text()
        == "Quanto tempo do teu trabalho líquido foi necessário para pagar cada categoria. · +12% vs mês anterior"
    )

    # 1. Hide only the main explanation, keep comparison note
    card.save_phrase_preferences(
        {
            "show_description": False,
            "custom_description": "",
            "show_comparison": True,
            "show_footer": True,
        }
    )
    card.apply_phrase_preferences()
    assert card.intro.isVisible() is True
    assert card.intro.text() == "+12% vs mês anterior"

    # 2. Hide both explanation and comparison note
    card.save_phrase_preferences(
        {
            "show_description": False,
            "custom_description": "",
            "show_comparison": False,
            "show_footer": True,
        }
    )
    card.apply_phrase_preferences()
    assert card.intro.isVisible() is False

    # 3. Custom text + comparison note
    card.save_phrase_preferences(
        {
            "show_description": True,
            "custom_description": "Horas investidas no mês.",
            "show_comparison": True,
            "show_footer": True,
        }
    )
    card.apply_phrase_preferences()
    assert card.intro.isVisible() is True
    assert card.intro.text() == "Horas investidas no mês. · +12% vs mês anterior"

    # 4. Hide method / footer
    card.set_data(
        {
            "available": True,
            "total_amount": 100.0,
            "total_hours": 10.0,
            "total_days": 1.25,
            "hourly_value": 10.0,
            "rows": [],
        },
        period_label="Março 2026",
        income_reference="Salário",
        context_months=3,
        weekly_hours=40.0,
    )
    assert card.method.isVisible() is True

    card.save_phrase_preferences(
        {
            "show_description": True,
            "custom_description": "",
            "show_comparison": True,
            "show_footer": False,
        }
    )
    card.apply_phrase_preferences()
    assert card.method.isVisible() is False

    # Re-running set_data keeps footer hidden when show_footer is False
    card.set_data(
        {
            "available": True,
            "total_amount": 120.0,
            "total_hours": 12.0,
            "total_days": 1.5,
            "hourly_value": 10.0,
            "rows": [],
        },
        period_label="Março 2026",
        income_reference="Salário",
        context_months=3,
        weekly_hours=40.0,
    )
    assert card.method.isVisible() is False

    parent.close()


def test_page_card_customization_dialog_includes_phrases_button(qapp):
    app_parent = QWidget()
    app_parent.theme = Theme(dark=False)

    card1 = GlassCard("Despesas", app_parent)
    card1.setProperty("pageCardKey", "expenses_card")

    entries = [
        {"key": "expenses", "title": "Despesas", "visible": True, "card": card1},
        {"key": "income", "title": "Receitas", "visible": True},
    ]

    dialog = PageCardCustomizationDialog("Dashboard", entries, app_parent)
    dialog.show()
    qapp.processEvents()

    # Table has 4 columns: Title, Show, Disable, Elementos
    assert dialog.table.columnCount() == 4
    header_labels = [
        dialog.table.horizontalHeaderItem(col).text() for col in range(4)
    ]
    assert any(h in ("Elementos", "Frases") for h in header_labels)

    # Verify buttons exist in column 3
    element_buttons = [
        btn
        for btn in dialog.findChildren(QPushButton)
        if btn.objectName() == "CardPhraseTableBtn"
    ]
    assert len(element_buttons) == 2
    assert all(
        ("Elementos" in btn.text() or "Frases" in btn.text()) for btn in element_buttons
    )

    dialog.close()
    app_parent.close()


def test_data_coverage_phrase_persistence_and_asynchronous_updates(qapp):
    """Verify that 'Base real: X meses com movimento' can be dynamically hidden and stays hidden across async worker reloads."""
    repo = _MockRepo()
    parent = _MockParent(repo)
    card = GlassCard("Previsão Orçamental", parent)
    card.setProperty("pageCardKey", "budget_forecast")

    # Initial async load from worker
    card.set_data_coverage("Base real: 9 meses com movimento (amostra robusta)", "positive")
    parent.show()
    qapp.processEvents()

    assert card.data_coverage_label is not None
    assert card.data_coverage_label.isVisible() is True
    assert "Base real: 9 meses com movimento" in card.data_coverage_label.text()

    # Dynamic elements inspection returns data_coverage
    elements = card.available_customization_elements()
    assert "data_coverage" in elements
    assert elements["data_coverage"]["enabled"] is True
    assert "9 meses" in elements["data_coverage"]["preview"]

    # Context menu has data coverage toggle action
    menu = card.create_context_menu()
    actions = {act.text(): act for act in menu.actions()}
    coverage_action = next((act for text, act in actions.items() if "base real" in text.lower()), None)
    assert coverage_action is not None
    assert coverage_action.isChecked() is True

    # User toggles it OFF
    coverage_action.trigger()
    qapp.processEvents()

    assert card.phrase_preferences()["show_data_coverage"] is False
    assert card.data_coverage_label.isVisible() is False

    # A background worker completes a new calculation and updates data coverage text
    card.set_data_coverage("Base real: 10 meses com movimento (amostra recalculada)", "positive")
    qapp.processEvents()

    # CRITICAL: The label MUST remain hidden because the user explicitly hid it!
    assert card.data_coverage_label.isVisible() is False
    assert card.data_coverage_label.text() == "Base real: 10 meses com movimento (amostra recalculada)"

    # Now restore/turn it back ON
    card.save_phrase_preferences({"show_data_coverage": True})
    card.apply_phrase_preferences()
    qapp.processEvents()

    assert card.data_coverage_label.isVisible() is True
    assert "10 meses" in card.data_coverage_label.text()

    parent.close()


def test_metric_card_dynamic_elements_customization(qapp):
    """Verify MetricCard exposes independent delta, sparkline, and status toggles that survive set_data()."""
    repo = _MockRepo()
    parent = _MockParent(repo)
    card = MetricCard(
        title="Poupança Mensal",
        value="850,00 €",
        delta="+15% vs mês anterior",
        accent="#10B981",
        icon="💰",
        status="Meta de poupança atingida",
        trend=[700.0, 750.0, 800.0, 850.0],
        trend_label="4 meses",
        parent=parent,
    )
    card.setProperty("pageCardKey", "monthly_savings_metric")
    parent.show()
    qapp.processEvents()

    # Initial state
    assert card.delta_label.isVisible() is True
    assert card.sparkline.isVisible() is True
    assert card.status_label.isVisible() is True

    # Introspection returns all 3 elements
    elements = card.available_customization_elements()
    assert "delta" in elements
    assert "sparkline" in elements
    assert "metric_status" in elements

    # 1. Hide delta only
    card.save_phrase_preferences({"show_delta": False})
    card.apply_phrase_preferences()
    qapp.processEvents()
    assert card.delta_label.isVisible() is False
    assert card.sparkline.isVisible() is True
    assert card.status_label.isVisible() is True

    # 2. Hide sparkline and status as well
    card.save_phrase_preferences({"show_sparkline": False, "show_status": False})
    card.apply_phrase_preferences()
    qapp.processEvents()
    assert card.delta_label.isVisible() is False
    assert card.sparkline.isVisible() is False
    assert card.status_label.isVisible() is False

    # 3. Subsequent worker update calling set_data() respects preferences
    card.set_data(
        value="900,00 €",
        delta="+20% vs mês anterior",
        status="Nova meta batida",
        trend=[750.0, 800.0, 850.0, 900.0],
    )
    qapp.processEvents()

    assert card.value_label.text() == "900,00 €"
    assert card.delta_label.isVisible() is False
    assert card.sparkline.isVisible() is False
    assert card.status_label.isVisible() is False

    # 4. Restore all
    card.reset_phrase_preferences()
    qapp.processEvents()
    assert card.delta_label.isVisible() is True
    assert card.sparkline.isVisible() is True
    assert card.status_label.isVisible() is True

    parent.close()


def test_expense_work_time_dynamic_metrics_and_status(qapp):
    """Verify ExpenseWorkTimeCard allows toggling work metrics and status message independently."""
    repo = _MockRepo()
    parent = _MockParent(repo)
    card = ExpenseWorkTimeCard(parent=parent, theme_provider=lambda: parent.theme)
    card.setProperty("pageCardKey", "work_time_card")
    parent.show()
    qapp.processEvents()

    # Feed data
    card.set_data(
        {
            "available": True,
            "total_amount": 300.0,
            "total_hours": 20.0,
            "total_days": 2.5,
            "hourly_value": 15.0,
            "rows": [{"category": "Alimentação", "amount": 300.0, "hours": 20.0, "days": 2.5}],
        },
        period_label="Março 2026",
        income_reference="Salário",
        context_months=6,
        weekly_hours=40.0,
    )
    qapp.processEvents()

    # Both metrics and status are visible initially
    assert card.status.isVisible() is True
    for i in range(card.metrics.count()):
        item = card.metrics.itemAt(i)
        assert item.widget().isVisible() is True

    # Elements introspection contains work_metrics and metric_status
    elements = card.available_customization_elements()
    assert "work_metrics" in elements
    assert "metric_status" in elements

    # Hide work_metrics block
    card.save_phrase_preferences({"show_work_metrics": False})
    card.apply_phrase_preferences()
    qapp.processEvents()

    for i in range(card.metrics.count()):
        item = card.metrics.itemAt(i)
        assert item.widget().isVisible() is False
    assert card.status.isVisible() is True

    # Hide status as well
    card.save_phrase_preferences({"show_status": False})
    card.apply_phrase_preferences()
    qapp.processEvents()
    assert card.status.isVisible() is False

    # Call set_data again; ensure preferences remain enforced
    card.set_data(
        {
            "available": True,
            "total_amount": 350.0,
            "total_hours": 23.3,
            "total_days": 2.9,
            "hourly_value": 15.0,
            "rows": [{"category": "Alimentação", "amount": 350.0, "hours": 23.3, "days": 2.9}],
        },
        period_label="Março 2026",
        income_reference="Salário",
        context_months=6,
        weekly_hours=40.0,
    )
    qapp.processEvents()

    for i in range(card.metrics.count()):
        item = card.metrics.itemAt(i)
        assert item.widget().isVisible() is False
    assert card.status.isVisible() is False

    parent.close()


def test_card_phrase_dialog_dynamic_element_rendering(qapp):
    """Verify CardPhraseCustomizationDialog dynamically renders checkboxes for bespoke elements."""
    parent = QWidget()
    parent.theme = Theme(dark=True)

    bespoke_elements = {
        "data_coverage": {
            "key": "show_data_coverage",
            "title": "Base real dos dados",
            "preview": "Base real: 9 meses",
            "icon": "📐",
            "enabled": True,
            "supports_text": False,
        },
        "description": {
            "key": "show_description",
            "title": "Frase explicativa",
            "preview": "Texto original",
            "custom_key": "custom_description",
            "custom_text": "Meu texto custom",
            "icon": "💬",
            "enabled": True,
            "supports_text": True,
        },
        "sparkline": {
            "key": "show_sparkline",
            "title": "Mini-gráfico de tendência",
            "preview": "Tendência a 4 meses",
            "icon": "📈",
            "enabled": False,
            "supports_text": False,
        },
    }

    dialog = CardPhraseCustomizationDialog(
        card_title="Teste Dinâmico",
        elements=bespoke_elements,
        parent=parent,
    )
    dialog.show()
    qapp.processEvents()

    assert dialog._checkboxes["show_data_coverage"].isChecked() is True
    assert dialog._checkboxes["show_description"].isChecked() is True
    assert dialog._checkboxes["show_sparkline"].isChecked() is False
    assert dialog.text_edit.toPlainText() == "Meu texto custom"

    # Toggle sparkline to ON and data_coverage to OFF
    dialog._checkboxes["show_sparkline"].setChecked(True)
    dialog._checkboxes["show_data_coverage"].setChecked(False)

    st = dialog.state()
    assert st["show_sparkline"] is True
    assert st["show_data_coverage"] is False
    assert st["custom_description"] == "Meu texto custom"

    dialog.close()
    parent.close()

