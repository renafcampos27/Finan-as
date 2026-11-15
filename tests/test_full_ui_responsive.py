from __future__ import annotations

import os
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest
from shiboken6 import isValid

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent, QPointF, Qt
from PySide6.QtGui import QFont, QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication,
    QHeaderView,
    QLabel,
    QPushButton,
    QSizePolicy,
    QTableView,
)
from utils.version import APP_VERSION


VISUAL_STATES = tuple(
    (width, dark, privacy)
    for width in (960, 1280, 1600, 1920, 2560)
    for dark, privacy in (
        ((False, False),)
        if width >= 1920
        else ((False, False), (False, True), (True, False), (True, True))
    )
)
STATE_IDS = [
    f"{width}-{'dark' if dark else 'light'}-{'private' if privacy else 'visible'}"
    for width, dark, privacy in VISUAL_STATES
]


def test_visual_state_inventory_is_complete():
    assert len(VISUAL_STATES) == 14
    assert len(set(VISUAL_STATES)) == 14
    assert set(VISUAL_STATES) == {
        (width, dark, privacy)
        for width in (960, 1280, 1600)
        for dark in (False, True)
        for privacy in (False, True)
    } | {(1920, False, False), (2560, False, False)}


@pytest.mark.timeout(300)
@pytest.mark.parametrize(
    "visual_state", (None, *VISUAL_STATES), ids=("common", *STATE_IDS)
)
def test_full_app_layout_at_requested_viewports_themes_and_privacy(
    tmp_path, monkeypatch, request, visual_state
):
    """Each state owns a process, QApplication and temporary data profile."""
    node_id = request.node.nodeid
    if os.environ.get("FINANCAS_UI_STATE_WORKER") == node_id:
        _exercise_full_app_state(tmp_path, monkeypatch, visual_state)
        return
    environment = os.environ.copy()
    environment.pop("PYTHONOPTIMIZE", None)
    environment.update(
        FINANCAS_UI_STATE_WORKER=node_id,
        FINANCAS_PESSOAIS_STATE_DIR=str(tmp_path / "profile"),
        FINANCAS_PESSOAIS_TESTING="1",
        FINANCAS_PESSOAIS_DEMO_DATA="1",
        QT_QPA_PLATFORM="offscreen",
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONUTF8="1",
    )
    # Allow sufficient headroom for rendering 30 complex pages offscreen
    deadline = 240.0

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            node_id,
            "-q",
            "-p",
            "no:cacheprovider",
            "--tb=short",
            f"--timeout={max(1.0, deadline - 10.0)}",
        ],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=deadline,
        check=False,
    )

    assert result.returncode == 0, (
        f"Isolated state {visual_state} failed ({result.returncode}).\n"
        f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )


def _exercise_full_app_state(tmp_path, monkeypatch, visual_state):
    """Retain every shared interaction/assertion and run one matrix state."""
    import database.repo.base as repo_base
    import services.documents as document_service_module
    import utils.app_logging as app_logging

    database_path = tmp_path / "financas_pessoais.sqlite3"
    monkeypatch.setattr(repo_base, "db_path", lambda: database_path)
    monkeypatch.setattr(
        document_service_module, "attachments_dir", lambda: tmp_path / "anexos"
    )
    monkeypatch.setattr(app_logging, "configure_logging", lambda *_a, **_k: None)

    from main import LiquidFinanceApp

    qapp = QApplication.instance() or QApplication([])
    original_font = QFont(qapp.font())
    window = None
    try:
        window = LiquidFinanceApp()
        window.repo.set_setting("backup_on_close", "0")
        window.backup_service.run_backup_on_close = lambda silent=True: None
        window.check_subscription_reminders = lambda: None
        window.show()
        window.pages["dashboard"].start_initial_load()
        QTest.qWait(350)
        if visual_state is None:
            # Startup must stay light: only the dashboard is materialised and a
            # global refresh must not eagerly construct the remaining 26 pages.
            assert set(window.pages.keys()) == {"dashboard"}
            assert window.pages["dashboard"]._dashboard_request_id == 1
            window.refresh_all()
            assert set(window.pages.keys()) == {"dashboard"}
            # Phase 4 consolidation: 5 thematic navigation families
            assert len(window.nav_group_headers) == 5
            assert (
                window.repo.scalar("SELECT COUNT(*) FROM financial_state_snapshots")
                == 1
            )

            dashboard = window.pages["dashboard"]
            movements = window.pages["movimentos"]
            portfolio = window.pages["carteira"]
            budgets = window.pages["orcamentos"]
            simulations = window.pages["simulacoes"]
            reports = window.pages["analises"]
            panorama = window.pages["panorama"]
            score_page = window.pages["score"]
            risk_page = window.pages["risco"]
            health_page = window.pages["saude"]
            finance_center = window.pages["centro"]
            documents = window.pages["documentos"]
            pdf_reports = window.pages["relatorios_pdf"]
            settings_page = window.pages["definicoes"]
            all_pages = {key: window.pages[key] for key in window.page_factories}
            # Lazily-created pages may finish their debounced chart calculations
            # on the next event-loop ticks.
            QTest.qWait(250)
            managed_tables = [
                table
                for page in all_pages.values()
                for table in page.findChildren(QTableView)
                if bool(table.property("columnPersistenceRegistered"))
            ]
            assert len(managed_tables) >= 20
            for _attempt in range(10):
                if all(table.isSortingEnabled() for table in managed_tables):
                    break
                QTest.qWait(25)
            assert all(table.isSortingEnabled() for table in managed_tables)
            from ui.dialogs import BudgetBucketDialog

            window.resize(960, 640)
            bucket_dialog = BudgetBucketDialog(window.repo, panorama)
            bucket_dialog.show()
            QTest.qWait(500)
            assert bucket_dialog.parentWidget() is window
            assert bucket_dialog.size() == window.size()
            assert bucket_dialog.rect().contains(bucket_dialog.sheet.geometry())
            assert bucket_dialog.sheet.width() < bucket_dialog.width()
            assert bucket_dialog.sheet.height() < bucket_dialog.height()
            assert bucket_dialog.heading_label.isVisibleTo(bucket_dialog)
            assert bucket_dialog.table.isVisibleTo(bucket_dialog)
            assert bucket_dialog.table.rowCount() > 0
            assert bucket_dialog._selectors[0][1].isVisibleTo(bucket_dialog)
            bucket_dialog.close()
            QTest.qWait(30)
            assert window.graphicsEffect() is None

            # These cards rebuild dynamic content. Repeated refreshes after the
            # deferred-deletion queue runs must preserve their persistent labels.
            score_page.refresh()
            risk_page.refresh()
            qapp.processEvents()
            score_page.refresh()
            risk_page.refresh()
            assert score_page.method_hint.text()
            assert risk_page.method_hint.text()

            window.repo.set_setting(
                panorama._card_setting_key(),
                json.dumps({"hidden": ["receitas"], "permanent": []}),
            )
            panorama.refresh()
            QTest.qWait(20)
            hidden_revenue_cards = [
                card for key, _title, card in panorama.page_cards() if key == "receitas"
            ]
            assert hidden_revenue_cards
            assert all(card.isHidden() for card in hidden_revenue_cards)
            window.repo.set_setting(panorama._card_setting_key(), "{}")
            panorama.refresh()
            QTest.qWait(20)
            visible_revenue_cards = [
                card for key, _title, card in panorama.page_cards() if key == "receitas"
            ]
            assert visible_revenue_cards
            assert all(not card.isHidden() for card in visible_revenue_cards)
            assert set(all_pages) == set(window.page_factories)
            # Phase 4 consolidation: 14 primary sidebar destinations (folded pages accessed via workspace tabs)
            assert len(window.nav_buttons) == 14
            assert window.version_badge.text() == f"v{APP_VERSION}"
            assert window._sidebar_contextual is True
            assert any(
                len(button.text().replace("★", "").strip()) > 2
                for button in window.nav_buttons.values()
            )
            assert f"v{APP_VERSION}" in window.version_badge.toolTip()
            assert f"v{APP_VERSION}" in window.windowTitle()
            assert qapp.applicationVersion() == APP_VERSION
            navigation_focus = window.cycle_focus_zone(1)
            assert window._is_descendant(navigation_focus, window.sidebar)
            search_focus = window.cycle_focus_zone(1)
            assert search_focus is window.search
            page_focus = window.cycle_focus_zone(1)
            assert window._is_descendant(page_focus, dashboard)
            accessibility_audit = window.accessibility_manager.audit_tree(window)
            assert accessibility_audit.unnamed == ()
            location_cards = [
                card
                for _key, title, card in settings_page.page_cards()
                if title == "Localização dos dados"
            ]
            assert len(location_cards) == 1
            location_buttons = {
                button.text() for button in location_cards[0].findChildren(QPushButton)
            }
            assert location_buttons == {
                "Abrir pasta da base de dados",
                "Abrir proteções de versão",
            }
            location_text = "\n".join(
                label.text() for label in location_cards[0].findChildren(QLabel)
            )
            assert str(database_path) in location_text
            assert str(tmp_path / "anexos") in location_text
            assert "Proteção de versões ativa" in location_text
            assert "protecoes_de_versao" in location_text
            assert not hasattr(settings_page, "open_classic")
            assert settings_page.primary_backup_path.isReadOnly()
            assert settings_page.primary_backup_path.text()
            assert settings_page.clear_all_movements_btn.text().startswith(
                "Apagar todos"
            )
            assert all(
                "app clássica" not in label.text().lower()
                for label in settings_page.findChildren(QLabel)
            )
            assert dashboard.category_limit == 8
            assert dashboard.donut.minimumHeight() == round(230 * 1.20)
            assert dashboard.donut_card.minimumHeight() >= 320
            assert movements.table_card.minimumHeight() == 480
            assert movements.table.minimumHeight() == 420
            assert dashboard.category_limit_combo.currentData() == 8
            assert "risco" not in window.nav_buttons
            assert reports._workspace_switcher is not None
            assert risk_page._workspace_switcher is not None
            assert set(risk_page._workspace_switcher.buttons) == {
                "saude",
                "score",
                "risco",
            }
            assert not risk_page.workspace_host.isHidden()
            window.goto("risco")
            assert window.nav_buttons["saude"].property("active") == "true"
            assert all(
                hasattr(page, "reset_view_btn") and hasattr(page, "customize_cards_btn")
                for page in all_pages.values()
            )
            assert portfolio.monte_carlo_chart.rows
            assert len(portfolio.net_worth_bridge_chart.rows) >= 3
            assert (
                "variação explicada"
                in portfolio.net_worth_bridge_summary.text().casefold()
            )
            assert len(finance_center.commitments_chart.rows) == 12
            assert "Média dos" in finance_center.commitments_summary.text()
            assert "meses completos" in finance_center.commitments_summary.text()
            assert len(finance_center.runway_chart.rows) == 13
            assert (
                0 <= finance_center.runway_chart.rows[-1]["probability_positive"] <= 100
            )
            assert len(finance_center.recurring_timeline_chart.rows) == 7
            assert finance_center.budget_variance_chart.categories
            assert health_page.data_quality_chart.rows
            assert "Proteção registada" in health_page.protection_status.text()
            assert len(simulations.habit_buttons) >= 12
            assert len(simulations.chart.rows) == 7
            assert len(simulations.chart.series) == 360
            assert simulations.results_table.rowCount() == 7
            assert simulations.results_table.columnCount() == 7
            assert (
                simulations.results_table.verticalScrollBarPolicy()
                == Qt.ScrollBarAlwaysOff
            )
            assert simulations.gambling_panel.isVisibleTo(simulations)
            assert "Casino" in simulations.preset_description.text()
            assert simulations.price_basis_value.text() == "Preço atual constante"
            assert not hasattr(simulations, "inflation")
            assert not hasattr(simulations, "investment_rate")
            assert simulations._income_reference_name == "salário"
            assert simulations._average_income == 2_750
            assert [
                simulations.results_table.item(row, 0).text()
                for row in range(simulations.results_table.rowCount())
            ] == ["1 mês", "1 ano", "2 anos", "3 anos", "5 anos", "10 anos", "30 anos"]
            simulations.habit_buttons["coffee"].click()
            assert simulations._selected_key == "coffee"
            assert not simulations.gambling_panel.isVisibleTo(simulations)
            assert simulations.alternative_name_label.text() == "Café em casa"
            assert simulations.alternative_spin.isEnabled()
            assert simulations.alternative_spin.value() == 0.25
            assert simulations.frequency_slider.maximum() == 60
            simulations._habit_states.setdefault("coffee", {})["frequency"] = 2.5
            simulations._select_habit("coffee", capture_current=False, persist=False)
            assert simulations.frequency_slider.value() == 3
            assert simulations._inputs().frequency == 3
            simulations.amount_slider.setValue(250)
            simulations.frequency_slider.setValue(14)
            assert simulations.amount_spin.value() == 2.50
            assert simulations.alternative_spin.maximum() == 2.50
            simulations.alternative_spin.setValue(0.50)
            assert simulations._inputs().alternative_unit_cost == 0.50
            assert simulations._inputs().frequency == 14
            assert simulations.frequency_value_label.text() == "14 por semana"
            assert simulations._current_data["monthly_cost"] > 0
            assert simulations.reduce_25_btn.text() == "Comparar −25%"
            assert simulations.reduce_50_btn.text() == "Comparar metade"
            assert simulations.goal_combo.count() == 1
            assert "Comprar casa" in simulations.goal_title.text()
            assert "Atual:" in simulations.goal_detail.text()
            simulations.frequency_slider.setValue(4)
            simulations.reduce_25_btn.click()
            assert simulations.frequency_slider.value() == 4
            assert simulations._preview_kind == "reduce_25"
            assert simulations.chart.active_scenario == "reduce_25"
            assert simulations.apply_preview_btn.isVisibleTo(simulations)
            assert "Nada foi alterado" in simulations.preview_detail.text()
            simulations.apply_preview_btn.click()
            assert simulations.frequency_slider.value() == 3
            assert simulations._preview_kind == "current"
            simulations.frequency_slider.setValue(4)
            simulations.reduce_50_btn.click()
            assert simulations.frequency_slider.value() == 4
            assert simulations._preview_kind == "half"
            simulations.apply_preview_btn.click()
            assert simulations.frequency_slider.value() == 2
            simulations.frequency_slider.setValue(1)
            simulations.reduce_25_btn.click()
            assert simulations.frequency_slider.value() == 1
            simulations.apply_preview_btn.click()
            assert simulations.frequency_slider.value() == 0
            simulations.frequency_slider.setValue(1)
            simulations.reduce_50_btn.click()
            assert simulations.frequency_slider.value() == 1
            simulations.apply_preview_btn.click()
            assert simulations.frequency_slider.value() == 0
            simulations.habit_buttons["dinners"].click()
            assert simulations._habit_calibration["available"] is True
            manual_cost = simulations.amount_spin.value()
            simulations.real_data_preview_btn.click()
            assert simulations._preview_kind == "real"
            assert simulations.amount_spin.value() == manual_cost
            assert "continuam guardados" in simulations.preview_detail.text()
            simulations.apply_preview_btn.click()
            assert simulations._preview_kind == "current"
            assert (
                simulations.amount_spin.value()
                == simulations._habit_calibration["average_unit_cost"]
            )
            simulations.habit_buttons["coffee"].click()
            simulations.frequency_slider.setValue(14)
            if simulations._average_income > 0:
                assert "% / mês" in simulations.salary_share_value.text()
                assert "Cálculo automático" in simulations.salary_share_formula.text()
                assert simulations._current_data["income_share_pct"] > 0
            simulations.habit_buttons["casino"].click()
            assert simulations.gambling_panel.isVisibleTo(simulations)
            assert simulations.win_probability.maximum() == 49
            assert not simulations.alternative_spin.isEnabled()
            assert simulations.alternative_spin.value() == 0
            assert simulations.audit_movements_btn.isEnabled()
            simulations.habit_buttons["coffee"].click()
            simulations.alternative_preview_btn.click()
            assert simulations._preview_kind == "alternative"
            assert simulations.chart.active_scenario == "alternative"
            assert simulations.save_plan_btn.isVisibleTo(simulations)
            assert not simulations.apply_preview_btn.isVisibleTo(simulations)
            from services.habit_plans import build_habit_plan_payload

            plan_payload = build_habit_plan_payload(
                preset_key="coffee",
                simulation=simulations._base_data,
                scenario_key="alternative",
                name="Plano visual de cafés",
                start_date="2026-05-01",
                goal_id=None,
                audit_rules=simulations._habit_audit_rules.get("coffee", {}),
            )
            plan_id = window.repo.add_habit_change_plan(**plan_payload)
            simulations.plans_card.refresh(select_id=plan_id)
            simulations._set_internal_view("plans")
            qapp.processEvents()
            assert simulations.plans_card.isVisibleTo(simulations)
            assert simulations.plans_card.plan_count() == 1
            assert simulations.plans_card.chart.rows
            assert simulations.plans_card.table.rowCount() >= 1
            assert simulations.plans_card.status_badge.text() != "Sem planos"
            simulations._set_internal_view("simulate")
            assert hasattr(portfolio, "liability_table")
            assert hasattr(budgets, "smart_budget_table")
            assert budgets.smart_budget_table.objectName() == "SmartBudgetTable"
            assert window.repo.setting("smart_budget_columns_v303", "0") == "1"
            assert hasattr(reports, "period_comparison_chart")
            assert len(reports.period_comparison_chart.rows) == len(
                window.repo.categories("expense")
            )
            assert "categorias no comparador" in reports.comparison_scope_hint.text()
            assert hasattr(reports, "period_comparison_scroll")
            assert hasattr(reports, "anomaly_table")
            assert hasattr(finance_center, "alert_table")
            assert hasattr(movements, "load_more_btn")
            assert hasattr(documents, "storage_status")
            assert "Arquivo consistente" in documents.storage_status.text()
            assert documents.table.columnCount() == 8
            assert hasattr(documents, "documents_load_more_btn")
            assert hasattr(score_page, "breakdown") and hasattr(score_page, "trend")
            assert hasattr(risk_page, "scenario_chart")
            assert hasattr(risk_page, "resilience_breakdown")
            metric_titles = [
                card.title_label.text()
                for card in dashboard.metric_widgets
                if hasattr(card, "title_label") and card.title_label is not None
            ]
            window.goto("dashboard")
            QTest.qWait(80)
            assert len(dashboard.metric_widgets) == 8
            assert "Reserva de emergência" in metric_titles
            assert "Valor do teu tempo" in metric_titles
            assert all(
                card.context_badge is not None and card.context_badge.text()
                for card in dashboard.metric_widgets
            )
            assert all(
                card.context_badge is not None and card.context_badge.text()
                for _key, _setting_key, card, _span in dashboard.dashboard_cards
            )
            assert "Dados até" in dashboard.chart_data_badge.text()
            assert "confiança" in dashboard.chart_data_badge.text()
            assert "Dados até" in dashboard.budget_data_badge.text()
            assert any(
                phrase in dashboard.budget_data_badge.text()
                for phrase in ("coberto por orçamentos", "ainda sem despesas")
            )
            assert "dados até" in score_page.score_status.text().casefold()
            assert score_page.breakdown_card.context_badge is not None
            assert "Dados até" in score_page.breakdown_card.context_badge.text()
            focus_category = dashboard.donut.data[0][0]
            dashboard._set_category_highlight(focus_category)
            assert all(
                getattr(chart, "highlighted_category", focus_category) == focus_category
                for chart in (
                    dashboard.donut,
                    dashboard.waterfall,
                    dashboard.budget_chart,
                    dashboard.radar,
                    dashboard.treemap,
                    dashboard.bubble,
                    dashboard.sankey,
                )
            )
            assert dashboard.expense_work_time_card.table.currentRow() >= 0
            dashboard._clear_category_highlight()
            assert dashboard.donut.highlighted_category == ""
            dashboard_scroll = window.content_scroll.verticalScrollBar()
            dashboard_scroll.setValue(0)
            qapp.processEvents()
            assert not window.dashboard_sticky_host.isVisible()
            dashboard_scroll.setValue(dashboard_scroll.maximum())
            QTest.qWait(30)
            assert window.dashboard_sticky_host.isVisible()
            assert (
                window.dashboard_sticky_summary.text()
                == dashboard.filters_summary.text()
            )
            dashboard_scroll.setValue(0)
            qapp.processEvents()
            assert not window.dashboard_sticky_host.isVisible()
            assert dashboard.expense_work_time_card.table.rowCount() == len(
                window.analytics_service.category_totals(
                    dashboard.start_cache,
                    dashboard.end_cache,
                )
            )
            assert (
                dashboard.expense_work_time_card.table.verticalScrollBar().maximum()
                == 0
            )
            assert "€/hora" in next(
                card.value_label.fullText()
                for card in dashboard.metric_widgets
                if card.title_label.text() == "Valor do teu tempo"
            )
            assert "€" in dashboard.donut.accessibleDescription()
            assert "%" in dashboard.donut.accessibleDescription()
            assert dashboard.cashflow_pulse.rows
            assert dashboard.savings_efficiency.rows
            assert dashboard.cashflow_pulse.comparison_rows
            assert dashboard.savings_efficiency.comparison_rows
            assert dashboard.donut._comparison_values
            assert dashboard.donut_comparison_hint.isVisibleTo(dashboard)
            assert "Vs." in dashboard.donut_comparison_hint.text()
            for card in dashboard.metric_widgets:
                if card.title_label.text() in {"Receitas", "Despesas", "Poupança"}:
                    assert dashboard.period_label_cache in card.sparkline.trend_label
                    assert "8 meses" not in card.sparkline.trend_label
            assert dashboard.period_label_cache in (
                dashboard.cashflow_pulse_card.title_label.text()
            )
            assert dashboard.period_label_cache in (
                dashboard.savings_efficiency_card.title_label.text()
            )
            assert len(dashboard.savings_efficiency.rows) == len(
                window.analytics_service.monthly_summary_for_period(
                    dashboard.start_cache,
                    dashboard.end_cache,
                )
            )
            assert "8 meses" not in dashboard.savings_efficiency_card.title_label.text()
            assert "entradas acumuladas" in (
                dashboard.cashflow_pulse.accessibleDescription().casefold()
            )
            assert "taxa média" in (
                dashboard.savings_efficiency.accessibleDescription().casefold()
            )
            assert dashboard.goals_card.title_label is not None
            assert (
                dashboard.goals_card.title_label.sizePolicy().verticalPolicy()
                == QSizePolicy.Fixed
            )
            assert dashboard.insights.priority_rows
            assert dashboard.insights.summary_panel.objectName() == "DecisionSummary"
            assert all(
                row.objectName() == "DecisionPriorityRow"
                and row.action_button.objectName() == "DecisionAction"
                and row.metrics_label.wordWrap()
                for row in dashboard.insights.priority_rows
            )
            pdf_reports.month.setCurrentIndex(0)
            annual_html = pdf_reports.html()
            assert "Evolução mensal" in annual_html
            assert "Investimentos" in annual_html
            assert "Documentos e faturas" in annual_html
            assert "Explicação face a" in annual_html
            assert hasattr(window, "table_column_manager")
            all_tables = [
                table
                for page in all_pages.values()
                for table in page.findChildren(QTableView)
            ]
            managed_tables = [
                table
                for table in all_tables
                if not bool(table.property("columnPersistenceExcluded"))
            ]
            excluded_tables = [
                table
                for table in all_tables
                if bool(table.property("columnPersistenceExcluded"))
            ]
            assert all(
                bool(table.property("columnPersistenceRegistered"))
                for table in managed_tables
            )
            stable_table_ids = [
                str(table.property("columnPersistenceId") or "")
                for table in managed_tables
            ]
            assert all(stable_table_ids)
            assert len(stable_table_ids) == len(set(stable_table_ids))
            assert all(
                len(table.property("columnPersistenceColumnIds") or [])
                == table.model().columnCount()
                and not bool(table.property("columnPersistenceUsesFallbackIds"))
                and table.horizontalHeader().sectionsMovable()
                for table in managed_tables
            )
            assert all(
                str(table.property("columnPersistenceKey"))
                == f"table_columns:v2:{stable_id}"
                for table, stable_id in zip(managed_tables, stable_table_ids)
            )
            assert all(
                table.horizontalHeader().sectionResizeMode(column)
                == QHeaderView.ResizeMode.Interactive
                for table in managed_tables
                for column in range(table.model().columnCount())
            )
            assert excluded_tables
            assert all(
                table.objectName() == "qt_calendar_calendarview"
                and not bool(table.property("columnPersistenceRegistered"))
                and all(
                    table.horizontalHeader().sectionResizeMode(column)
                    == QHeaderView.ResizeMode.Stretch
                    for column in range(table.model().columnCount())
                )
                for table in excluded_tables
            )
            movements_header = movements.table.horizontalHeader()
            assert all(
                movements_header.sectionResizeMode(column)
                == QHeaderView.ResizeMode.Interactive
                for column in range(movements.movements_model.columnCount())
            )
            movements_header.resizeSection(3, 337)
            movements.refresh()
            qapp.processEvents()
            assert movements_header.sectionSize(3) == 337
            window.goto("movimentos")
            QTest.qWait(120)
            movements.period.setCurrentText("Tudo")
            movements.refresh()
            for _ in range(100):
                qapp.processEvents()
                if (
                    not movements._loader_task.is_running()
                    and not movements._pending_load
                ):
                    break
                QTest.qWait(20)
            first_movement = movements.movements_model.index(0, 1)
            assert first_movement.isValid()
            movements.table.scrollTo(first_movement)
            qapp.processEvents()
            viewport = movements.table.viewport()
            hover_position = movements.table.visualRect(first_movement).center()
            assert movements.table.indexAt(hover_position).isValid()
            mouse_event = QMouseEvent(
                QEvent.Type.MouseMove,
                QPointF(hover_position),
                QPointF(viewport.mapToGlobal(hover_position)),
                Qt.MouseButton.NoButton,
                Qt.MouseButton.NoButton,
                Qt.KeyboardModifier.NoModifier,
            )
            QApplication.sendEvent(viewport, mouse_event)
            qapp.processEvents()
            assert movements._movement_hover_card.isVisible()
            assert not movements._movement_hover_card.isWindow()
            hover_image = movements._movement_hover_card.grab().toImage()
            hover_background = hover_image.pixelColor(
                movements._movement_hover_card.width() - 18,
                movements._movement_hover_card.height() - 18,
            )
            assert hover_background.lightness() > 235
            movement_scroll = window.content_scroll.verticalScrollBar()
            movement_scroll.setValue(movement_scroll.maximum())
            saved_movement_scroll = movement_scroll.value()
            window.goto("carteira")
            QTest.qWait(180)
            assert window.content_scroll.verticalScrollBar().value() == 0
            portfolio_scroll = window.content_scroll.verticalScrollBar()
            portfolio_position = min(240, portfolio_scroll.maximum())
            portfolio_scroll.setValue(portfolio_position)
            window.goto("movimentos")
            QTest.qWait(40)
            assert window.content_scroll.verticalScrollBar().value() == min(
                saved_movement_scroll,
                window.content_scroll.verticalScrollBar().maximum(),
            )
            window.goto("carteira")
            QTest.qWait(40)
            assert (
                window.content_scroll.verticalScrollBar().value() == portfolio_position
            )

            # Regression: category-heavy budget cards must grow with their data
            # instead of clipping the last rows inside a fixed-height card.
            budget_rows = [
                {
                    "category": f"Categoria {index:02d}",
                    "spent": 35.0 + index,
                    "budget": 100.0 + index * 5,
                    "projected": 70.0 + index * 2,
                    "projected_margin": 30.0 + index * 3,
                    "utilisation": 35.0,
                    "previous_spend": 28.0 + index,
                    "forecast_status": "Controlado",
                }
                for index in range(18)
            ]
            budget_forecast = {
                "rows": budget_rows,
                "total_budget": sum(row["budget"] for row in budget_rows),
                "total_spent": sum(row["spent"] for row in budget_rows),
                "total_projected": sum(row["projected"] for row in budget_rows),
                "projected_margin": sum(row["projected_margin"] for row in budget_rows),
                "coverage_pct": 100.0,
                "overall_tone": "positive",
                "overall_status": "Controlado",
                "days_elapsed": 18,
                "days_in_month": 31,
                "month_label": "Julho 2026",
                "method_label": "Cenário visual de teste com todas as categorias.",
                "over_count": 0,
                "risk_count": 0,
                "unbudgeted_spend": 0.0,
            }
            smart_rows = [
                {
                    "category": row["category"],
                    "rollover": 10.0,
                    "effective_budget": row["budget"] + 10.0,
                    "projected": row["projected"],
                    "suggested_budget": row["budget"],
                    "breach_date": "",
                    "status": "Controlado",
                    "tone": "positive",
                }
                for row in budget_rows
            ]
            smart_budget = {
                "rows": smart_rows,
                "method_label": "Rollover e previsão de rutura.",
                "total_rollover": 180.0,
                "risk_count": 0,
            }
            monkeypatch.setattr(budgets, "_forecast", lambda: budget_forecast)
            monkeypatch.setattr(budgets, "_smart_budget", lambda: smart_budget)
            budgets.refresh()
            qapp.processEvents()

            assert budgets.forecast_chart.row_height == 48
            assert budgets.forecast_chart.minimumHeight() == 108 + 18 * 48
            assert budgets.table.rowCount() == 18
            assert budgets.smart_budget_table.rowCount() == 18
            assert budgets.table.verticalScrollBarPolicy() == Qt.ScrollBarAlwaysOff
            assert (
                budgets.smart_budget_table.verticalScrollBarPolicy()
                == Qt.ScrollBarAlwaysOff
            )

        else:
            dashboard = window.pages["dashboard"]
            movements = window.pages["movimentos"]
            portfolio = window.pages["carteira"]
            budgets = window.pages["orcamentos"]
            simulations = window.pages["simulacoes"]
            reports = window.pages["analises"]
            panorama = window.pages["panorama"]
            score_page = window.pages["score"]
            risk_page = window.pages["risco"]
            health_page = window.pages["saude"]
            finance_center = window.pages["centro"]
            documents = window.pages["documentos"]
            pdf_reports = window.pages["relatorios_pdf"]
            settings_page = window.pages["definicoes"]
            all_pages = {key: window.pages[key] for key in window.page_factories}

            QTest.qWait(250)
            pdf_reports.month.setCurrentIndex(0)
            simulations.habit_buttons["coffee"].click()
            assert simulations._selected_key == "coffee"
            assert not simulations.gambling_panel.isVisibleTo(simulations)
            assert simulations.alternative_name_label.text() == "Café em casa"
            assert simulations.alternative_spin.isEnabled()
            assert simulations.alternative_spin.value() == 0.25
            assert simulations.frequency_slider.maximum() == 60
            simulations._habit_states.setdefault("coffee", {})["frequency"] = 2.5
            simulations._select_habit("coffee", capture_current=False, persist=False)
            assert simulations.frequency_slider.value() == 3
            assert simulations._inputs().frequency == 3
            simulations.amount_slider.setValue(250)
            simulations.frequency_slider.setValue(14)
            assert simulations.amount_spin.value() == 2.50
            assert simulations.alternative_spin.maximum() == 2.50
            simulations.alternative_spin.setValue(0.50)
            assert simulations._inputs().alternative_unit_cost == 0.50
            assert simulations._inputs().frequency == 14
            assert simulations.frequency_value_label.text() == "14 por semana"
            assert simulations._current_data["monthly_cost"] > 0
            assert simulations.reduce_25_btn.text() == "Comparar −25%"
            assert simulations.reduce_50_btn.text() == "Comparar metade"
            assert simulations.goal_combo.count() == 1
            assert "Comprar casa" in simulations.goal_title.text()
            assert "Atual:" in simulations.goal_detail.text()
            simulations.frequency_slider.setValue(4)
            simulations.reduce_25_btn.click()
            assert simulations.frequency_slider.value() == 4
            assert simulations._preview_kind == "reduce_25"
            assert simulations.chart.active_scenario == "reduce_25"
            assert simulations.apply_preview_btn.isVisibleTo(simulations)
            assert "Nada foi alterado" in simulations.preview_detail.text()
            simulations.apply_preview_btn.click()
            assert simulations.frequency_slider.value() == 3
            assert simulations._preview_kind == "current"
            simulations.frequency_slider.setValue(4)
            simulations.reduce_50_btn.click()
            assert simulations.frequency_slider.value() == 4
            assert simulations._preview_kind == "half"
            simulations.apply_preview_btn.click()
            assert simulations.frequency_slider.value() == 2
            simulations.frequency_slider.setValue(1)
            simulations.reduce_25_btn.click()
            assert simulations.frequency_slider.value() == 1
            simulations.apply_preview_btn.click()
            assert simulations.frequency_slider.value() == 0
            simulations.frequency_slider.setValue(1)
            simulations.reduce_50_btn.click()
            assert simulations.frequency_slider.value() == 1
            simulations.apply_preview_btn.click()
            assert simulations.frequency_slider.value() == 0
            simulations.habit_buttons["dinners"].click()
            assert simulations._habit_calibration["available"] is True
            manual_cost = simulations.amount_spin.value()
            simulations.real_data_preview_btn.click()
            assert simulations._preview_kind == "real"
            assert simulations.amount_spin.value() == manual_cost
            assert "continuam guardados" in simulations.preview_detail.text()
            simulations.apply_preview_btn.click()
            assert simulations._preview_kind == "current"
            assert (
                simulations.amount_spin.value()
                == simulations._habit_calibration["average_unit_cost"]
            )
            simulations.habit_buttons["coffee"].click()
            simulations.frequency_slider.setValue(14)
            if simulations._average_income > 0:
                assert "% / mês" in simulations.salary_share_value.text()
                assert "Cálculo automático" in simulations.salary_share_formula.text()
                assert simulations._current_data["income_share_pct"] > 0
            simulations.habit_buttons["casino"].click()
            assert simulations.gambling_panel.isVisibleTo(simulations)
            assert simulations.win_probability.maximum() == 49
            assert not simulations.alternative_spin.isEnabled()
            assert simulations.alternative_spin.value() == 0
            assert simulations.audit_movements_btn.isEnabled()
            simulations.habit_buttons["coffee"].click()
            simulations.alternative_preview_btn.click()
            assert simulations._preview_kind == "alternative"
            assert simulations.chart.active_scenario == "alternative"
            assert simulations.save_plan_btn.isVisibleTo(simulations)
            assert not simulations.apply_preview_btn.isVisibleTo(simulations)
            from services.habit_plans import build_habit_plan_payload

            plan_payload = build_habit_plan_payload(
                preset_key="coffee",
                simulation=simulations._base_data,
                scenario_key="alternative",
                name="Plano visual de cafés",
                start_date="2026-05-01",
                goal_id=None,
                audit_rules=simulations._habit_audit_rules.get("coffee", {}),
            )
            plan_id = window.repo.add_habit_change_plan(**plan_payload)
            simulations.plans_card.refresh(select_id=plan_id)
            simulations._set_internal_view("plans")
            qapp.processEvents()
            assert simulations.plans_card.isVisibleTo(simulations)
            assert simulations.plans_card.plan_count() == 1
            assert simulations.plans_card.chart.rows
            assert simulations.plans_card.table.rowCount() >= 1
            assert simulations.plans_card.status_badge.text() != "Sem planos"
            simulations._set_internal_view("simulate")
            # Regression: category-heavy budget cards must grow with their data
            # instead of clipping the last rows inside a fixed-height card.
            budget_rows = [
                {
                    "category": f"Categoria {index:02d}",
                    "spent": 35.0 + index,
                    "budget": 100.0 + index * 5,
                    "projected": 70.0 + index * 2,
                    "projected_margin": 30.0 + index * 3,
                    "utilisation": 35.0,
                    "previous_spend": 28.0 + index,
                    "forecast_status": "Controlado",
                }
                for index in range(18)
            ]
            budget_forecast = {
                "rows": budget_rows,
                "total_budget": sum(row["budget"] for row in budget_rows),
                "total_spent": sum(row["spent"] for row in budget_rows),
                "total_projected": sum(row["projected"] for row in budget_rows),
                "projected_margin": sum(row["projected_margin"] for row in budget_rows),
                "coverage_pct": 100.0,
                "overall_tone": "positive",
                "overall_status": "Controlado",
                "days_elapsed": 18,
                "days_in_month": 31,
                "month_label": "Julho 2026",
                "method_label": "Cenário visual de teste com todas as categorias.",
                "over_count": 0,
                "risk_count": 0,
                "unbudgeted_spend": 0.0,
            }
            smart_rows = [
                {
                    "category": row["category"],
                    "rollover": 10.0,
                    "effective_budget": row["budget"] + 10.0,
                    "projected": row["projected"],
                    "suggested_budget": row["budget"],
                    "breach_date": "",
                    "status": "Controlado",
                    "tone": "positive",
                }
                for row in budget_rows
            ]
            smart_budget = {
                "rows": smart_rows,
                "method_label": "Rollover e previsão de rutura.",
                "total_rollover": 180.0,
                "risk_count": 0,
            }
            monkeypatch.setattr(budgets, "_forecast", lambda: budget_forecast)
            monkeypatch.setattr(budgets, "_smart_budget", lambda: smart_budget)
            budgets.refresh()
            qapp.processEvents()

            assert budgets.forecast_chart.row_height == 48
            assert budgets.forecast_chart.minimumHeight() == 108 + 18 * 48
            assert budgets.table.rowCount() == 18
            assert budgets.smart_budget_table.rowCount() == 18
            assert budgets.table.verticalScrollBarPolicy() == Qt.ScrollBarAlwaysOff
            assert (
                budgets.smart_budget_table.verticalScrollBarPolicy()
                == Qt.ScrollBarAlwaysOff
            )

        if visual_state is not None:
            width, dark, privacy = visual_state
            window.resize(width, 1080 if width == 1920 else 900)
            window.repo.set_setting("liquid_dark", "1" if dark else "0")
            window.repo.set_setting("liquid_privacy", "1" if privacy else "0")
            window.apply_theme(first=True)
            assert panorama.composition.theme_provider() is window.theme
            assert panorama.sankey.theme_provider() is window.theme
            if privacy:
                assert "••••• €" in pdf_reports.html()
            for page in all_pages.values():
                window.stack.setCurrentWidget(page)
                qapp.processEvents()
                assert page.reset_view_btn.geometry().right() <= page.width()
                assert page.customize_cards_btn.geometry().right() <= page.width()
                # Render every page at all three requested widths in the
                # baseline state. Complex pages are additionally
                # rendered for every theme/privacy combination.
                if not dark and not privacy:
                    assert not page.grab().isNull()
            window.stack.setCurrentWidget(budgets)
            qapp.processEvents()
            assert not budgets.forecast_chart.grab().isNull()
            assert len(budgets.forecast_chart._hit_rects) == 18
            assert (
                budgets.forecast_chart._hit_rects[-1][1].bottom()
                <= budgets.forecast_chart.height()
            )
            assert budgets.table.verticalScrollBar().maximum() == 0
            assert budgets.smart_budget_table.verticalScrollBar().maximum() == 0
            smart_header = budgets.smart_budget_table.horizontalHeader()
            assert smart_header.offset() == 0
            assert smart_header.sectionSize(0) >= 170
            assert budgets.smart_budget_table.geometry().left() >= 0
            window.stack.setCurrentWidget(simulations)
            simulations.refresh()
            QTest.qWait(90)
            assert not simulations.chart.grab().isNull()
            assert len(simulations.chart.series) == 360
            assert len(simulations.chart._point_rects) == 7
            assert simulations.results_table.verticalScrollBar().maximum() == 0
            assert (
                simulations.chart.geometry().bottom()
                < simulations.chart_detail.geometry().top()
            )
            assert (
                simulations.chart_detail.geometry().bottom()
                < simulations.results_table.geometry().top()
            )
            assert (
                simulations.results_table.geometry().bottom()
                < simulations.insight_banner.geometry().top()
            )
            assert all(
                button.geometry().right() <= button.parentWidget().width()
                for button in simulations.habit_buttons.values()
            )
            simulations._set_internal_view("plans")
            qapp.processEvents()
            assert simulations.plans_card.isVisibleTo(simulations)
            assert not simulations.plans_card.grab().isNull()
            assert not simulations.plans_card.chart.grab().isNull()
            assert simulations.plans_card.table.verticalScrollBar().maximum() == 0
            simulations._set_internal_view("simulate")
            for page in (dashboard, portfolio, score_page, risk_page):
                window.stack.setCurrentWidget(page)
                qapp.processEvents()
                assert not page.grab().isNull()
            window.goto("dashboard")
            qapp.processEvents()
            window.sync_content_size()
            donut_width_ratio = dashboard.donut_card.width() / max(
                1,
                dashboard.dashboard_grid.geometry().width(),
            )
            if dashboard.dashboard_column_count() == 3:
                assert 0.41 <= donut_width_ratio <= 0.44
                pulse_width_ratio = dashboard.cashflow_pulse_card.width() / max(
                    1,
                    dashboard.dashboard_grid.geometry().width(),
                )
                savings_width_ratio = dashboard.savings_efficiency_card.width() / max(
                    1,
                    dashboard.dashboard_grid.geometry().width(),
                )
                assert 0.64 <= pulse_width_ratio <= 0.66
                assert 0.33 <= savings_width_ratio <= 0.35
                assert (
                    dashboard.cashflow_pulse_card.geometry().top()
                    == dashboard.savings_efficiency_card.geometry().top()
                )
                budget_width_ratio = dashboard.budget_card.width() / max(
                    1,
                    dashboard.dashboard_grid.geometry().width(),
                )
                assert 0.64 <= budget_width_ratio <= 0.66
                goals_width_ratio = dashboard.goals_card.width() / max(
                    1,
                    dashboard.dashboard_grid.geometry().width(),
                )
                insights_width_ratio = dashboard.insights.width() / max(
                    1,
                    dashboard.dashboard_grid.geometry().width(),
                )
                assert 0.33 <= goals_width_ratio <= 0.36
                assert 0.56 <= insights_width_ratio <= 0.59
                assert (
                    dashboard.donut_card.geometry().top()
                    == dashboard.insights.geometry().top()
                )
                assert (
                    dashboard.budget_card.geometry().top()
                    == dashboard.goals_card.geometry().top()
                )
            else:
                assert donut_width_ratio >= 0.95
                assert dashboard.budget_card.width() >= int(
                    dashboard.dashboard_grid.geometry().width() * 0.95
                )
            assert (
                dashboard.expense_work_time_card.table.verticalScrollBar().maximum()
                == 0
            )
            assert (
                dashboard.expense_work_time_card.geometry().bottom()
                <= dashboard.height()
            )
            assert (
                dashboard.budget_scroll.geometry().bottom()
                <= dashboard.budget_card.contentsRect().bottom()
            )
            budget_gap = (
                dashboard.budget_scroll.geometry().top()
                - dashboard.budget_hint.geometry().bottom()
                - 1
            )
            # QLabel baselines can introduce up to two logical pixels of
            # rounding on scaled Windows displays while the layout itself
            # still retains the intended compact four-pixel spacing.
            assert 0 <= budget_gap <= dashboard.budget_card.v.spacing() + 2
            assert dashboard.budget_card.v.spacing() == 4
            assert (
                dashboard.budget_hint.height()
                == dashboard.budget_hint.heightForWidth(dashboard.budget_hint.width())
            )
            donut_gap = (
                dashboard.donut.geometry().top()
                - (
                    dashboard.donut_comparison_hint.geometry().bottom()
                    if dashboard.donut_comparison_hint.isVisible()
                    else dashboard.donut_card.title_label.geometry().bottom()
                )
                - 1
            )
            assert 0 <= donut_gap <= dashboard.donut_card.v.spacing()
            assert dashboard.donut_card.v.spacing() == 3
            assert (
                dashboard.budget_chart.minimumHeight()
                == dashboard.budget_chart.required_height_for_rows(
                    len(dashboard.budget_chart.rows)
                )
            )
        if visual_state is None:
            for compact_height in (640, 720):
                window.resize(960, compact_height)
                window.goto("dashboard")
                qapp.processEvents()
                assert window.content_scroll.verticalScrollBar().maximum() > 0
                assert window.content_scroll.horizontalScrollBar().maximum() == 0
                assert not dashboard.grab().isNull()
            window.repo.set_setting("global_font_scale", "Muito Grande (125%)")
            window.apply_font_scale()
            window.resize(960, 640)
            qapp.processEvents()
            assert window.content_scroll.horizontalScrollBar().maximum() == 0
            # Flow layouts have a width-dependent height. ``sizeHint()`` uses the
            # narrow minimum width and therefore overestimates the real height at
            # this viewport; validate the rendered width instead.
            required_filter_height = dashboard.filters_card.heightForWidth(
                dashboard.filters_card.width()
            )
            assert required_filter_height <= dashboard.filters_card.height()
            window.repo.set_setting("global_font_scale", "Normal (100%)")
            window.apply_font_scale()
            qapp.processEvents()
            assert all(
                table.horizontalHeader().sectionResizeMode(column)
                == QHeaderView.ResizeMode.Interactive
                for table in managed_tables
                for column in range(table.model().columnCount())
            )
            assert all(
                table.horizontalHeader().sectionResizeMode(column)
                == QHeaderView.ResizeMode.Stretch
                for table in excluded_tables
                for column in range(table.model().columnCount())
            )
            assert window.content_scroll.horizontalScrollBar().maximum() == 0
    finally:
        try:
            if window is not None and isValid(window):
                # Let the real close path stop every page worker and detach
                # global filters before the database and widget tree disappear.
                window.close()
                deadline = time.monotonic() + 5.0
                while not window._database_closed and time.monotonic() < deadline:
                    QTest.qWait(20)
                assert window._database_closed, "Application shutdown did not finish"
                assert not window.background_task_registry.has_active_tasks()
                window.deleteLater()
                QCoreApplication.sendPostedEvents(window, QEvent.Type.DeferredDelete)
                assert not isValid(window)
        finally:
            qapp.setFont(original_font)
            assert qapp.font() == original_font
