from __future__ import annotations

from datetime import date

from ui.core.responsive import ResponsivePolicy
from ui.navigation import (
    NAVIGATION_GROUPS,
    WORKSPACES,
    all_navigation_items,
    contextual_navigation_label,
    navigation_groups,
    navigation_key_for_page,
)
from ui.pages.dashboard_layout import (
    DASHBOARD_CARD_SPECS,
    arrange_dashboard_companions,
    dashboard_column_stretches,
    metric_band_columns,
    migrate_saved_order,
    plan_dashboard_layout,
    primary_metric_columns,
    uses_balanced_feature_columns,
)
from ui.dashboard_worker import _comparison_window


def test_responsive_policy_boundaries() -> None:
    assert ResponsivePolicy.dashboard_columns(759) == 1
    assert ResponsivePolicy.dashboard_columns(760) == 2
    assert ResponsivePolicy.dashboard_columns(1179) == 2
    assert ResponsivePolicy.dashboard_columns(1180) == 3
    assert ResponsivePolicy.dashboard_columns(1920) == 3
    assert ResponsivePolicy.dashboard_columns(2560) == 3

    assert ResponsivePolicy.metric_columns(429) == 1
    assert ResponsivePolicy.metric_columns(430) == 2
    assert ResponsivePolicy.metric_columns(779) == 2
    assert ResponsivePolicy.metric_columns(780) == 3


def test_dashboard_card_registry_is_complete_and_unique() -> None:
    keys = [spec.key for spec in DASHBOARD_CARD_SPECS]
    setting_keys = [spec.setting_key for spec in DASHBOARD_CARD_SPECS]

    assert len(keys) == len(set(keys)) == 15
    assert len(setting_keys) == len(set(setting_keys))
    assert {
        "chart",
        "cashflow_pulse",
        "savings_efficiency",
        "budget_forecast",
        "donut",
        "goals",
        "insights",
    } <= set(keys)


def test_saved_dashboard_order_migrates_new_cards() -> None:
    migrated = migrate_saved_order(["show_chart", "show_donut", "show_goals"])

    assert migrated.index("show_cashflow_pulse") == migrated.index("show_chart") + 1
    assert (
        migrated.index("show_savings_efficiency")
        == migrated.index("show_cashflow_pulse") + 1
    )
    assert (
        migrated.index("show_waterfall")
        == migrated.index("show_savings_efficiency") + 1
    )
    assert (
        migrated.index("show_budget_forecast") == migrated.index("show_waterfall") + 1
    )
    assert (
        migrated.index("show_cashflow_calendar")
        == migrated.index("show_budget_forecast") + 1
    )
    assert migrated.index("show_expense_work_time") == migrated.index("show_donut") + 1


def test_wide_dashboard_companions_share_balanced_rows() -> None:
    marker = object()
    entries = [
        ("cashflow_pulse", marker, 2),
        ("bubble", marker, 3),
        ("savings_efficiency", marker, 1),
        ("budget_forecast", marker, 2),
        ("waterfall", marker, 3),
        ("goals", marker, 1),
        ("donut", marker, 2),
        ("insights", marker, 1),
    ]

    arranged = arrange_dashboard_companions(entries, 3)
    arranged_keys = [key for key, _widget, _span in arranged]
    assert (
        arranged_keys.index("savings_efficiency")
        == arranged_keys.index("cashflow_pulse") + 1
    )
    assert arranged_keys.index("goals") == arranged_keys.index("budget_forecast") + 1
    assert arranged_keys.index("insights") == arranged_keys.index("donut") + 1

    balanced = uses_balanced_feature_columns(arranged, 3)
    placements = {
        placement.key: placement
        for placement in plan_dashboard_layout(
            arranged,
            3,
            balanced_feature_columns=balanced,
        )
    }
    assert placements["budget_forecast"].row == placements["goals"].row
    assert placements["donut"].row == placements["insights"].row
    assert placements["cashflow_pulse"].row == placements["savings_efficiency"].row
    assert placements["cashflow_pulse"].column_span == 2
    assert placements["savings_efficiency"].column_span == 1
    assert placements["donut"].column_span == 1
    assert placements["insights"].column_span == 2
    assert dashboard_column_stretches(3, balanced_feature_columns=True) == (
        106,
        57,
        87,
    )


def test_two_column_dashboard_gives_explanatory_cards_a_complete_row() -> None:
    marker = object()
    entries = [
        ("savings_efficiency", marker, 1),
        ("goals", marker, 1),
        ("insights", marker, 1),
    ]

    placements = plan_dashboard_layout(
        entries,
        2,
        balanced_feature_columns=False,
    )

    assert [placement.column_span for placement in placements] == [2, 2, 2]
    assert [placement.column for placement in placements] == [0, 0, 0]
    assert [placement.row for placement in placements] == [0, 1, 2]


def test_dashboard_comparison_windows() -> None:
    assert _comparison_window("2026-07-01", "2026-07-31", "previous") == (
        date(2026, 5, 31),
        date(2026, 6, 30),
        "período anterior",
    )
    assert _comparison_window("2024-02-29", "2024-03-02", "year") == (
        date(2023, 2, 28),
        date(2023, 3, 2),
        "ano anterior",
    )


def test_navigation_registry_has_unique_page_keys() -> None:
    keys = [item.key for group in NAVIGATION_GROUPS for item in group.items]
    all_keys = [item.key for item in all_navigation_items()]
    legacy_groups = navigation_groups()

    # Phase 4 consolidation: 14 primary sidebar destinations across 5 thematic families
    assert len(keys) == len(set(keys)) == 14
    # All 29 routes remain reachable and indexed in all_navigation_items()
    assert len(all_keys) == len(set(all_keys)) == 29
    assert len(legacy_groups) == len(NAVIGATION_GROUPS) == 5
    assert legacy_groups[0][0] in {"Início", "Visão geral"}
    assert len(WORKSPACES) == 8
    assert navigation_key_for_page("risco") == "saude"
    assert navigation_key_for_page("juros") == "simulacoes"
    assert navigation_key_for_page("centro") == "panorama"
    assert navigation_key_for_page("importacao") == "movimentos"
    assert navigation_key_for_page("objetivos") == "orcamentos"
    assert navigation_key_for_page("metas_mensais") == "planeador"
    contextual = [contextual_navigation_label(key) for key in keys]
    assert all(contextual)
    assert max(map(len, contextual)) <= 18
    assert contextual_navigation_label("panorama") == "🧭 Panorama"
    assert contextual_navigation_label("analises") == "📊 Análises"


def test_dashboard_period_buttons_and_total_visibility() -> None:
    from PySide6.QtWidgets import QApplication
    from main import LiquidFinanceApp

    app = QApplication.instance() or QApplication([])
    win = LiquidFinanceApp()
    try:
        dash = win.pages["dashboard"]
        buttons = dash.range_group.buttons()
        labels = [b.text() for b in buttons]

        # Verify all 8 period chips exist, including 'Total' with 0 days
        assert "Total" in labels
        total_chip = buttons[labels.index("Total")]
        assert total_chip.property("filterValue") == 0

        # Verify layout in expanded mode
        win.show()
        dash.show()
        dash.set_dashboard_filters_expanded(True)
        dash._update_filter_group_widths()
        for _ in range(5):
            app.processEvents()

        # Check that every button is unhidden and within group bounds
        assert not total_chip.isHidden()
        group_h = dash.period_filter_group.height()
        assert total_chip.height() < 50
        assert group_h >= total_chip.pos().y() + total_chip.height()
    finally:
        win.close()


def test_primary_metric_columns_breakpoints() -> None:
    # 4 columns on large displays (>= 1600 px)
    assert primary_metric_columns(1600) == 4
    assert primary_metric_columns(1920) == 4
    assert primary_metric_columns(2560) == 4
    assert metric_band_columns(1600) == 4

    # 2 columns on medium / standard laptops (960 to 1599 px)
    assert primary_metric_columns(1599) == 2
    assert primary_metric_columns(1280) == 2
    assert primary_metric_columns(960) == 2
    assert metric_band_columns(960) == 2

    # 1 column on compact viewports (< 960 px)
    assert primary_metric_columns(959) == 1
    assert primary_metric_columns(800) == 1
    assert primary_metric_columns(0) == 1
    assert primary_metric_columns(-50) == 1


def test_metric_card_variants() -> None:
    from PySide6.QtWidgets import QApplication
    from ui.components.cards import MetricCard

    _ = QApplication.instance() or QApplication([])

    card_primary = MetricCard("Saldo", "1 000 €", "+5%", "#00B0FF", "💰", variant="primary")
    assert card_primary.variant == "primary"
    assert card_primary.property("metricVariant") == "primary"

    card_context = MetricCard("Reserva", "3 000 €", "3 meses", "#30D158", "🛡️", variant="context")
    assert card_context.variant == "context"
    assert card_context.property("metricVariant") == "context"


def test_dashboard_secondary_metrics_drawer_interactive() -> None:
    from PySide6.QtWidgets import QApplication
    from main import LiquidFinanceApp

    _app = QApplication.instance() or QApplication([])
    win = LiquidFinanceApp()
    try:
        dash = win.pages["dashboard"]
        mock_data = {
            "totals": {"income": 2500.0, "expenses": 1200.0, "savings": 1300.0},
            "totals_prev": {},
            "savings_rate": 52.0,
            "balance": 4500.0,
            "net": 4500.0,
            "monthly_expense": 1200.0,
            "expense_structure": {},
            "emergency_reserve": {"available": True, "months": 3.75},
            "work_value": {"available": False},
            "income_context": {},
            "categories": [],
            "previous_categories": [],
            "forecast_data_context": {},
            "data_context": {},
            "forecast": [],
            "cashflow_pulse": [],
            "cashflow_pulse_previous": [],
            "savings_efficiency": [],
            "savings_efficiency_previous": [],
        }
        dash._on_data_loaded(dash._dashboard_request_id, mock_data)

        # Inital layout has 8 metric widgets: 4 primary and 4 in the secondary drawer
        assert len(dash.metric_widgets) == 8
        assert dash.secondary_metrics_toggle_btn is not None
        assert dash.secondary_metrics_container is not None

        # Verify variants
        assert [getattr(w, "variant", "") for w in dash.metric_widgets] == [
            "primary", "primary", "primary", "primary",
            "context", "context", "context", "context",
        ]

        # Initially drawer is collapsed
        assert not dash._secondary_metrics_expanded
        assert dash.secondary_metrics_container.isHidden()
        assert "Mostrar" in dash.secondary_metrics_toggle_btn.text()

        initial_host = dash._secondary_metrics_host
        assert initial_host is not None

        # Click to expand
        dash.toggle_secondary_metrics()
        assert dash._secondary_metrics_expanded is True
        assert not dash.secondary_metrics_container.isHidden()
        assert "Ocultar" in dash.secondary_metrics_toggle_btn.text()

        # Simulate layout pass (e.g. window resize) - host must be reused, not recreated
        dash.layout_metric_cards()
        assert dash._secondary_metrics_host is initial_host
        assert not dash.secondary_metrics_container.isHidden()
        assert "Ocultar" in dash.secondary_metrics_toggle_btn.text()

        # Click to collapse again
        dash.toggle_secondary_metrics()
        assert not dash._secondary_metrics_expanded
        assert dash.secondary_metrics_container.isHidden()
        assert "Mostrar" in dash.secondary_metrics_toggle_btn.text()
    finally:
        win.close()


def test_page_header_widget_contract() -> None:
    from PySide6.QtWidgets import QApplication
    from ui.components.page_header import PageHeaderWidget

    _ = QApplication.instance() or QApplication([])

    header = PageHeaderWidget(
        title="Movimentos",
        subtitle="Registo histórico",
        show_period_selector=True,
    )
    assert header.title_label.text() == "Movimentos"
    assert header.sub_label.text() == "Registo histórico"
    assert header.sub_label.objectName() == "Subtle"
    assert not header.period_container.isHidden()

    clicked = []
    action_btn = header.add_action_button(
        "Novo",
        icon="➕",
        callback=lambda: clicked.append(True),
        variant="Primary",
    )
    assert action_btn.text() == "➕ Novo"
    assert action_btn.objectName() == "Primary"
    action_btn.click()
    assert clicked == [True]

    header.set_title("Novo Título")
    assert header.title_label.text() == "Novo Título"

    header.set_subtitle("")
    assert header.sub_label.isHidden()


