#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Finanças Pessoais – Liquid Glass
Nova interface PySide6 para a app local de finanças pessoais.
Mantém a base de dados e a app clássica intactas; esta é apenas uma camada visual moderna.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any

from utils.qt_runtime import configure_qt_runtime

configure_qt_runtime()

try:
    from PySide6.QtCore import QPoint, Qt, QTimer
    from PySide6.QtGui import QColor
    from PySide6.QtWidgets import (
        QButtonGroup,
        QComboBox,
        QFrame,
        QGridLayout,
        QHBoxLayout,
        QInputDialog,
        QLabel,
        QLayout,
        QPushButton,
        QSizePolicy,
        QVBoxLayout,
        QWidget,
    )
except (
    Exception
) as exc:  # pragma: no cover - only used when PySide6 is missing on the user's PC.
    print("PySide6 não está instalado. Instala com: pip install PySide6")
    print(exc)
    sys.exit(1)

from ui.components import (
    BalanceChart,
    BudgetForecastChart,
    CashFlowWaterfallChart,
    CashflowTimelineChart,
    ChipButton,
    CumulativeCashflowChart,
    DecisionBriefingCard,
    DonutChart,
    ExpenseWorkTimeCard,
    GlassCard,
    MetricCard,
    ProgressPill,
    SavingsEfficiencyChart,
)
from ui.core import (
    BasePage,
    EdgePropagatingScrollArea,
    FlowLayout,
    ResponsivePolicy,
)
from ui.dialogs import DashboardConfigDialog
from ui.pages.dashboard_layout import (
    DASHBOARD_CARD_SPECS,
    DASHBOARD_CARD_SPEC_BY_KEY,
    arrange_dashboard_companions,
    dashboard_column_stretches,
    dashboard_grid_spacing,
    order_card_entries,
    plan_dashboard_layout,
    primary_metric_columns,
    uses_balanced_feature_columns,
)
from services.dashboard_metrics import DEFAULT_WEEKLY_WORK_HOURS
from ui.dashboard_worker import DashboardWorker
from utils.app_logging import log_exception
from utils.helpers import (
    BLUE,
    GREEN,
    ORANGE,
    PINK,
    PURPLE,
    RED,
    eur,
    format_iso_date,
    iso,
    percent,
    rolling_month_window,
    safe_float,
    to_qdate,
)

if TYPE_CHECKING:
    from main import LiquidFinanceApp


class DashboardPage(BasePage):
    _refresh_completes_asynchronously = True

    STARTUP_LOAD_DELAY_MS = 1000

    def __init__(self, app: "LiquidFinanceApp"):
        super().__init__(app, "🏠 Dashboard", "Visão geral das tuas finanças pessoais")
        # The Dashboard already has the richer Compacta/Detalhada selector in
        # its period panel.  Avoid presenting a second density control with a
        # subtly different meaning in the page header.
        self.density_btn.hide()
        self._dashboard_reflow_timer = QTimer(self)
        self._dashboard_reflow_timer.setSingleShot(True)
        self._dashboard_reflow_timer.setInterval(0)
        self._dashboard_reflow_timer.timeout.connect(self.reflow_dashboard)
        self._dashboard_height_timer = QTimer(self)
        self._dashboard_height_timer.setSingleShot(True)
        self._dashboard_height_timer.setInterval(0)
        self._dashboard_height_timer.timeout.connect(self._sync_budget_card_height)
        self._startup_load_timer = QTimer(self)
        self._startup_load_timer.setSingleShot(True)
        self._startup_load_timer.timeout.connect(self.start_initial_load)
        self._initial_load_pending = True
        self._secondary_metrics_expanded: bool = False
        self._secondary_metrics_host: QWidget | None = None
        self.secondary_metrics_toggle_btn: QPushButton | None = None
        self.secondary_metrics_container: QWidget | None = None
        self.secondary_metrics_grid: QGridLayout | None = None
        self.range_days = 90
        self.resolution = "daily"
        self.comparison_mode = "previous"
        self._comparison_before_all = "previous"
        self.dashboard_density = str(
            self.repo.setting("dashboard_density", "compact") or "compact"
        )
        if self.dashboard_density not in {"compact", "detailed"}:
            self.dashboard_density = "compact"
        height_migration_key = "dashboard_compact_chart_height_v302"
        if str(self.repo.setting(height_migration_key, "0") or "0") != "1":
            self.repo.set_setting("dashboard_category_limit", "8")
            self.repo.set_setting(height_migration_key, "1")
        stored_category_limit = str(
            self.repo.setting("dashboard_category_limit", "8") or "8"
        )
        self.category_limit: int | None = (
            int(stored_category_limit) if stored_category_limit in {"8", "16"} else None
        )
        self._last_categories: list[tuple[str, float, str]] = []
        self.cashflow_horizon = 60
        self._cashflow_data: dict[str, Any] = {}
        self._data_context: dict[str, Any] = {}
        self._forecast_data_context: dict[str, Any] = {}
        self._dashboard_request_id = 0
        self._workers: dict[int, DashboardWorker] = {}
        self.worker: DashboardWorker | None = None
        self._data_access_suspended = False
        self._category_highlight_timer = QTimer(self)
        self._category_highlight_timer.setSingleShot(True)
        self._category_highlight_timer.timeout.connect(self._clear_category_highlight)
        self._highlighted_category = ""
        self._period_highlight_timer = QTimer(self)
        self._period_highlight_timer.setSingleShot(True)
        self._period_highlight_timer.timeout.connect(self._clear_period_highlight)
        self._highlighted_period = ""

        self.filters_card = GlassCard("🗓️ Período e resolução", strong=True)
        self.filters_card.set_compact_header(
            word_wrap=False,
            top_margin=8,
            bottom_margin=7,
            spacing=4,
        )
        filters_wrap = QWidget()
        filters_wrap.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.filters_grid = QGridLayout(filters_wrap)
        self.filters_grid.setContentsMargins(0, 0, 0, 0)
        self.filters_grid.setHorizontalSpacing(8)
        self.filters_grid.setVerticalSpacing(6)
        self.filters_grid.setSizeConstraint(QLayout.SetMinimumSize)
        self._filter_groups: list[QFrame] = []
        self._filter_layout_mode = ""
        self.filters_summary = QLabel()
        self.filters_summary.setObjectName("DashboardFilterSummary")
        self.filters_summary.setWordWrap(True)
        self.filters_summary.setSizePolicy(
            QSizePolicy.Expanding,
            QSizePolicy.Preferred,
        )
        filters_summary_row = QWidget()
        filters_summary_layout = QHBoxLayout(filters_summary_row)
        filters_summary_layout.setContentsMargins(0, 0, 0, 0)
        filters_summary_layout.setSpacing(8)
        filters_summary_layout.addWidget(self.filters_summary, 1)
        self.filters_disclosure_btn = QPushButton()
        self.filters_disclosure_btn.setObjectName("Secondary")
        self.filters_disclosure_btn.setAccessibleName(
            "Mostrar ou ocultar filtros avançados do Dashboard"
        )
        self.filters_disclosure_btn.clicked.connect(
            lambda: self.set_dashboard_filters_expanded(
                not bool(self.filters_card.property("filtersExpanded"))
            )
        )
        filters_summary_layout.addWidget(self.filters_disclosure_btn, 0, Qt.AlignTop)
        self.filters_card.v.addWidget(filters_summary_row)

        self.period_filter_group, period_flow = self._create_filter_group(
            "PERÍODO",
            control_hspacing=4,
        )
        self.range_group = QButtonGroup(self)
        self.range_group.setExclusive(True)
        for label, days, checked in [
            ("Diário", 1, False),
            ("7 dias", 7, False),
            ("15 dias", 15, False),
            ("1 mês", 31, False),
            ("3 meses", 90, True),
            ("6 meses", 183, False),
            ("Anual", 365, False),
            ("Total", 0, False),
        ]:
            chip = ChipButton(label, checked)
            chip.setProperty("filterValue", days)
            if days == 0:
                chip.setToolTip("Todo o histórico financeiro (de sempre)")
            chip.clicked.connect(lambda _=False, d=days: self.change_range(d))
            self.range_group.addButton(chip)
            period_flow.addWidget(chip)

        self.resolution_filter_group, resolution_flow = self._create_filter_group(
            "AGRUPAR",
            control_hspacing=4,
        )
        self.res_group = QButtonGroup(self)
        self.res_group.setExclusive(True)
        for label, res, checked in [
            ("Diário", "daily", True),
            ("Semanal", "weekly", False),
            ("Mensal", "monthly", False),
        ]:
            chip = ChipButton(label, checked)
            chip.setProperty("filterValue", res)
            chip.clicked.connect(lambda _=False, r=res: self.change_resolution(r))
            self.res_group.addButton(chip)
            resolution_flow.addWidget(chip)

        self.comparison_filter_group, comparison_flow = self._create_filter_group(
            "COMPARAR",
        )
        self.comparison = QComboBox()
        self.comparison.setMinimumWidth(174)
        self.comparison.setMaximumWidth(280)
        self.comparison.addItem("Período anterior", "previous")
        self.comparison.addItem("Ano anterior", "year")
        self.comparison.addItem("Sem comparação", "none")
        self.comparison.setAccessibleName("Comparação dos indicadores")
        self.comparison.currentIndexChanged.connect(self.change_comparison)
        comparison_flow.addWidget(self.comparison)

        self.density_filter_group, density_flow = self._create_filter_group(
            "VISTA",
        )
        self.density_combo = QComboBox()
        self.density_combo.addItem("Compacta", "compact")
        self.density_combo.addItem("Detalhada", "detailed")
        self.density_combo.setMinimumWidth(118)
        self.density_combo.setMaximumWidth(190)
        self.density_combo.setCurrentIndex(
            max(0, self.density_combo.findData(self.dashboard_density))
        )
        self.density_combo.setAccessibleName("Densidade visual do Dashboard")
        self.density_combo.currentIndexChanged.connect(
            lambda _index: self.change_density(
                str(self.density_combo.currentData() or "compact")
            )
        )
        density_flow.addWidget(self.density_combo)

        self.category_filter_group, category_flow = self._create_filter_group(
            "CATEGORIAS",
        )
        self.category_limit_combo = QComboBox()
        self.category_limit_combo.addItem("Todas", 0)
        self.category_limit_combo.addItem("Top 8", 8)
        self.category_limit_combo.addItem("Top 16", 16)
        self.category_limit_combo.setMinimumWidth(118)
        self.category_limit_combo.setMaximumWidth(190)
        self.category_limit_combo.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        selected_limit = self.category_limit or 0
        self.category_limit_combo.setCurrentIndex(
            max(0, self.category_limit_combo.findData(selected_limit))
        )
        self.category_limit_combo.setAccessibleName(
            "Quantidade de categorias visíveis nos gráficos"
        )
        self.category_limit_combo.currentIndexChanged.connect(
            self.change_category_limit
        )
        category_flow.addWidget(self.category_limit_combo)

        self.filters_card.v.addWidget(filters_wrap)
        self.filters_wrap = filters_wrap
        self.register_filter_panel(
            self.filters_card,
            self.set_dashboard_filters_expanded,
        )
        self.set_dashboard_filters_expanded(False)
        self._update_filter_group_widths()
        self._update_filter_summary()
        self.cards_grid = QGridLayout()
        self.cards_grid.setSpacing(14)
        self.cards_grid.setAlignment(Qt.AlignTop)
        self.metric_widgets: list[QWidget] = []
        self.chart = BalanceChart(lambda: self.app.theme)
        self.chart.setMinimumHeight(450)
        self.donut = DonutChart(lambda: self.app.theme)
        self.goals_card = GlassCard("🎯 Objetivos em destaque")
        self.goals_card.set_compact_header(
            word_wrap=True,
            top_margin=7,
            bottom_margin=10,
            spacing=5,
            fit_context_header=True,
        )
        self.goals_card.v.setAlignment(Qt.AlignTop)
        self.config_btn = QPushButton("↕ Ordem")
        self.config_btn.setObjectName("Secondary")
        self.config_btn.setCursor(Qt.PointingHandCursor)
        self.config_btn.setToolTip("Reordenar os módulos do Dashboard")
        self.config_btn.clicked.connect(self.open_config)
        self.header.addWidget(self.config_btn)

        self.retry_btn = QPushButton("↻ Tentar novamente")
        self.retry_btn.setObjectName("Primary")
        self.retry_btn.setCursor(Qt.PointingHandCursor)
        self.retry_btn.clicked.connect(self.refresh)
        self.retry_btn.hide()
        self.header.addWidget(self.retry_btn)
        self.body.addWidget(self.filters_card)
        self.body.addLayout(self.cards_grid)

        self.chart_card = GlassCard("📈 Saldo histórico e previsão", strong=True)
        self.chart_card.set_compact_header(
            word_wrap=True,
            top_margin=7,
            bottom_margin=10,
            spacing=5,
            fit_context_header=True,
        )
        self.chart_card.set_confidence_badge("Confiança · a calcular", "warning")
        self.chart.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.chart_card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.chart_data_badge = self._add_data_coverage_badge(self.chart_card)
        self.chart_card.v.addWidget(self.chart)

        self.cashflow_pulse = CumulativeCashflowChart(lambda: self.app.theme)
        self.cashflow_pulse_card = GlassCard(
            "📈 Entradas vs. saídas acumuladas",
            strong=True,
            accent=GREEN,
        )
        self.cashflow_pulse_card.set_compact_header(
            word_wrap=True,
            top_margin=5,
            bottom_margin=8,
            spacing=2,
            fit_context_header=True,
        )
        self.cashflow_pulse_card.setMinimumHeight(405)
        self.cashflow_pulse_hint = QLabel(
            "A distância entre as linhas mostra a margem criada no período."
        )
        self.cashflow_pulse_hint.setObjectName("Subtle")
        self.cashflow_pulse_hint.setWordWrap(True)
        self.cashflow_pulse_hint.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self.cashflow_pulse_card.fit_label_height(
            self.cashflow_pulse_hint,
            word_wrap=True,
            stable_measurement=True,
        )
        self.cashflow_pulse_card.v.addWidget(self.cashflow_pulse_hint)
        self.cashflow_pulse_card.v.addWidget(self.cashflow_pulse)

        self.savings_efficiency = SavingsEfficiencyChart(lambda: self.app.theme)
        self.savings_efficiency_card = GlassCard(
            "💚 Margem e taxa de poupança",
            strong=True,
            accent=BLUE,
        )
        self.savings_efficiency_card.set_compact_header(
            word_wrap=True,
            top_margin=5,
            bottom_margin=8,
            spacing=2,
            fit_context_header=True,
        )
        self.savings_efficiency_card.setMinimumHeight(405)
        self.savings_efficiency_hint = QLabel(
            "Barras: dinheiro que sobrou · Linha: percentagem poupada · Meta: 20%."
        )
        self.savings_efficiency_hint.setObjectName("Subtle")
        self.savings_efficiency_hint.setWordWrap(True)
        self.savings_efficiency_hint.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self.savings_efficiency_card.fit_label_height(
            self.savings_efficiency_hint,
            word_wrap=True,
            stable_measurement=True,
        )
        self.savings_efficiency_card.v.addWidget(self.savings_efficiency_hint)
        self.savings_efficiency_card.v.addWidget(self.savings_efficiency)

        self.waterfall = CashFlowWaterfallChart(lambda: self.app.theme)
        self.waterfall.filter_clicked.connect(self._on_waterfall_filter_clicked)
        self.waterfall_card = GlassCard("🌊 Fluxo do dinheiro no período", strong=True)
        self.waterfall_card.set_compact_header(
            word_wrap=True,
            top_margin=7,
            bottom_margin=10,
            spacing=5,
            fit_context_header=True,
        )
        self.waterfall_card.setMinimumHeight(415)
        self.waterfall_comparison_hint = self._add_category_comparison_hint(
            self.waterfall_card
        )
        self.waterfall_hint = QLabel(
            "Receitas → despesas por categoria → resultado líquido. "
            "Passa o rato para explorar e clica numa barra para abrir os movimentos."
        )
        self.waterfall_hint.setObjectName("Subtle")
        self.waterfall_hint.setWordWrap(True)
        self.waterfall_card.v.addWidget(self.waterfall_hint)
        self.waterfall_card.v.addWidget(self.waterfall)

        self.budget_chart = BudgetForecastChart(lambda: self.app.theme)
        self.budget_chart.category_clicked.connect(self._open_budget_category)
        self.budget_card = GlassCard(
            "🎯 Orçamento vs. realizado · mês atual", strong=True
        )
        self.budget_card.set_confidence_badge("Confiança · a calcular", "warning")
        self.budget_card.setMinimumHeight(390)
        self.budget_card.v.setSpacing(4)
        if self.budget_card.title_label is not None and self._qt_object_is_valid(
            self.budget_card.title_label
        ):
            try:
                self.budget_card.title_label.setWordWrap(True)
                self.budget_card.title_label.setMinimumWidth(0)
                self.budget_card.title_label.setSizePolicy(
                    QSizePolicy.Ignored,
                    QSizePolicy.Maximum,
                )
            except RuntimeError:
                pass
        self.budget_header_wrap = QWidget()
        self.budget_header_wrap.setSizePolicy(
            QSizePolicy.Expanding,
            QSizePolicy.Maximum,
        )
        self.budget_header_flow = FlowLayout(
            self.budget_header_wrap,
            margin=0,
            hspacing=10,
            vspacing=4,
        )
        self.budget_status_label = QLabel("A calcular o ritmo do mês…")
        self.budget_status_label.setWordWrap(True)
        self.budget_status_label.setMinimumWidth(0)
        self.budget_status_label.setSizePolicy(
            QSizePolicy.Ignored,
            QSizePolicy.Preferred,
        )
        self.budget_status_label.setAccessibleName("Estado geral do orçamento")
        self.budget_data_badge = self._data_coverage_badge()
        self.budget_open_btn = QPushButton("Editar orçamentos →")
        self.budget_open_btn.setObjectName("Secondary")
        self.budget_open_btn.setCursor(Qt.PointingHandCursor)
        self.budget_open_btn.clicked.connect(lambda: self.app.goto("orcamentos"))
        self.budget_header_flow.addWidget(self.budget_status_label)
        # Keep confidence visible without creating another vertical row.  This
        # card already has a compact, wrapping metadata header and its chart
        # height is deliberately sized to the number of budget categories.
        if self.budget_card.confidence_badge is not None:
            self.budget_card.v.removeWidget(self.budget_card.confidence_badge)
            self.budget_header_flow.addWidget(self.budget_card.confidence_badge)
        self.budget_header_flow.addWidget(self.budget_data_badge)
        self.budget_header_flow.addWidget(self.budget_open_btn)
        self.budget_card.v.addWidget(self.budget_header_wrap)

        self.budget_summary_wrap = QWidget()
        self.budget_summary_wrap.setSizePolicy(
            QSizePolicy.Expanding, QSizePolicy.Maximum
        )
        self.budget_summary_flow = FlowLayout(
            self.budget_summary_wrap,
            margin=0,
            hspacing=8,
            vspacing=4,
        )
        self.budget_summary_labels: list[QLabel] = []
        for _ in range(4):
            label = QLabel()
            label.setMinimumHeight(36)
            label.setAlignment(Qt.AlignCenter)
            label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            self.budget_summary_labels.append(label)
            self.budget_summary_flow.addWidget(label)
        self.budget_card.v.addWidget(self.budget_summary_wrap)

        self.budget_hint = QLabel(
            "A previsão combina o gasto atual com o padrão do mês anterior."
        )
        self.budget_hint.setObjectName("Subtle")
        self.budget_hint.setWordWrap(True)
        self.budget_hint.setMinimumWidth(0)
        self.budget_hint.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Maximum)
        self.budget_card.v.addWidget(self.budget_hint)
        self.budget_scroll = EdgePropagatingScrollArea()
        self.budget_scroll.setObjectName("BudgetChartScroll")
        self.budget_scroll.setFrameShape(QFrame.NoFrame)
        self.budget_scroll.setWidgetResizable(True)
        self.budget_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.budget_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.budget_scroll.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.budget_scroll.setAccessibleName(
            "Categorias de orçamento e valores realizados"
        )
        self.budget_scroll.setWidget(self.budget_chart)
        self.budget_scroll.setFixedHeight(360)
        self.budget_card.v.addWidget(self.budget_scroll)

        self.cashflow_chart = CashflowTimelineChart(lambda: self.app.theme)
        self.cashflow_chart.event_clicked.connect(self._on_cashflow_event)
        self.cashflow_card = GlassCard("🗓️ Tesouraria futura · 60 dias", strong=True)
        self.cashflow_card.set_compact_header(
            word_wrap=True,
            top_margin=7,
            bottom_margin=10,
            spacing=5,
            fit_context_header=True,
        )
        self.cashflow_card.set_confidence_badge("Confiança · a calcular", "warning")
        self.cashflow_card.setMinimumHeight(535)
        self.cashflow_status_label = QLabel("A calcular compromissos futuros…")
        self.cashflow_status_label.setAccessibleName("Estado da tesouraria futura")
        self.cashflow_status_label.setSizePolicy(
            QSizePolicy.Expanding, QSizePolicy.Preferred
        )
        self.cashflow_card.v.addWidget(self.cashflow_status_label)
        self.cashflow_data_badge = self._add_data_coverage_badge(self.cashflow_card)

        cashflow_controls_wrap = QWidget()
        cashflow_controls_wrap.setSizePolicy(
            QSizePolicy.Expanding, QSizePolicy.Preferred
        )
        cashflow_controls = FlowLayout(
            cashflow_controls_wrap,
            margin=0,
            hspacing=8,
            vspacing=8,
        )
        horizon_hint = QLabel("HORIZONTE")
        horizon_hint.setObjectName("Subtle")
        cashflow_controls.addWidget(horizon_hint)
        self.cashflow_range_group = QButtonGroup(self)
        self.cashflow_range_group.setExclusive(True)
        for days in (30, 60, 90):
            chip = ChipButton(f"{days} dias", days == self.cashflow_horizon)
            chip.setProperty("filterValue", days)
            chip.clicked.connect(
                lambda _checked=False, selected=days: self.change_cashflow_horizon(
                    selected
                )
            )
            self.cashflow_range_group.addButton(chip)
            cashflow_controls.addWidget(chip)
        self.cashflow_open_btn = QPushButton("Abrir Centro financeiro →")
        self.cashflow_open_btn.setObjectName("Secondary")
        self.cashflow_open_btn.setCursor(Qt.PointingHandCursor)
        self.cashflow_open_btn.clicked.connect(lambda: self.app.goto("centro"))
        cashflow_controls.addWidget(self.cashflow_open_btn)
        self.cashflow_card.v.addWidget(cashflow_controls_wrap)

        cashflow_summary_wrap = QWidget()
        cashflow_summary_wrap.setSizePolicy(
            QSizePolicy.Expanding, QSizePolicy.Preferred
        )
        self.cashflow_summary_flow = FlowLayout(
            cashflow_summary_wrap,
            margin=0,
            hspacing=8,
            vspacing=8,
        )
        self.cashflow_summary_labels: list[QLabel] = []
        for _ in range(3):
            label = QLabel()
            label.setMinimumHeight(36)
            label.setMinimumWidth(210)
            label.setMaximumWidth(340)
            label.setAlignment(Qt.AlignCenter)
            label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            self.cashflow_summary_labels.append(label)
            self.cashflow_summary_flow.addWidget(label)
        self.cashflow_card.v.addWidget(cashflow_summary_wrap)

        self.cashflow_hint = QLabel()
        self.cashflow_hint.setObjectName("Subtle")
        self.cashflow_hint.setWordWrap(True)
        self.cashflow_card.v.addWidget(self.cashflow_hint)
        self.cashflow_card.v.addWidget(self.cashflow_chart)

        self.donut_card = GlassCard("🍩 Despesas por categoria")
        self.donut.category_clicked.connect(self._on_treemap_clicked)
        # Give the permanent values/percentages and longer category names 20%
        # more vertical room while keeping the card responsive horizontally.
        self.donut.setMinimumHeight(
            round(self.donut.BASE_MINIMUM_HEIGHT * self.donut.DASHBOARD_HEIGHT_SCALE)
        )
        self.donut_card.v.setContentsMargins(15, 10, 15, 10)
        self.donut_card.v.setSpacing(3)
        if self.donut_card.title_label is not None and self._qt_object_is_valid(
            self.donut_card.title_label
        ):
            try:
                self.donut_card.title_label.setSizePolicy(
                    QSizePolicy.Expanding, QSizePolicy.Fixed
                )
            except RuntimeError:
                pass
        self.donut_comparison_hint = self._add_category_comparison_hint(self.donut_card)
        self.donut_card.v.addWidget(self.donut)
        donut_card_chrome = max(
            0,
            self.donut_card.minimumSizeHint().height() - self.donut.minimumHeight(),
        )
        self.donut_card.setMinimumHeight(
            round(
                (self.donut.BASE_MINIMUM_HEIGHT + donut_card_chrome)
                * self.donut.DASHBOARD_HEIGHT_SCALE
            )
        )

        self.expense_work_time_card = ExpenseWorkTimeCard(
            lambda: self.app.theme,
            self,
        )
        self.expense_work_time_comparison_hint = self._add_category_comparison_hint(
            self.expense_work_time_card,
        )
        self.expense_work_time_card.category_clicked.connect(self._on_treemap_clicked)

        self.insights = DecisionBriefingCard(self)
        self.insights.action_requested.connect(self._open_decision_action)

        from ui.components.charts import (
            BubbleChart,
            HeatmapChart,
            RadarChart,
            SankeyChart,
            TreemapChart,
        )

        self.radar = RadarChart(lambda: self.app.theme)
        self.radar_card = GlassCard("🧭 Perfil relativo das categorias", strong=True)
        self.radar_card.set_compact_header(
            word_wrap=True,
            top_margin=7,
            bottom_margin=10,
            spacing=5,
            fit_context_header=True,
        )
        self.radar_comparison_hint = self._add_category_comparison_hint(self.radar_card)
        self.radar_card.v.addWidget(self.radar)

        self.treemap = TreemapChart(lambda: self.app.theme)
        self.treemap.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        self.treemap.category_clicked.connect(self._on_treemap_clicked)
        self.treemap_card = GlassCard("🧩 Peso das categorias", strong=True)
        self.treemap_card.set_compact_header(
            word_wrap=True,
            top_margin=7,
            bottom_margin=10,
            spacing=5,
            fit_context_header=True,
        )
        self.treemap_card.setMinimumHeight(590)
        self.treemap_card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        self.treemap_comparison_hint = self._add_category_comparison_hint(
            self.treemap_card
        )
        self.treemap_card.v.addWidget(self.treemap)

        self.bubble = BubbleChart(lambda: self.app.theme)
        if hasattr(self.bubble, "category_clicked"):
            self.bubble.category_clicked.connect(self._on_treemap_clicked)
        self.bubble_card = GlassCard("🫧 Categorias por volume", strong=True)
        self.bubble_card.set_compact_header(
            word_wrap=True,
            top_margin=7,
            bottom_margin=10,
            spacing=5,
            fit_context_header=True,
        )
        self.bubble_card.setMinimumHeight(470)
        self.bubble_card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        self.bubble_comparison_hint = self._add_category_comparison_hint(
            self.bubble_card
        )
        self.bubble_card.v.addWidget(self.bubble)

        self.sankey = SankeyChart(lambda: self.app.theme)
        self.sankey.category_clicked.connect(self._on_treemap_clicked)
        self.sankey_card = GlassCard("🌊 Distribuição das despesas", strong=True)
        self.sankey_card.set_compact_header(
            word_wrap=True,
            top_margin=7,
            bottom_margin=10,
            spacing=5,
            fit_context_header=True,
        )
        self.sankey_comparison_hint = self._add_category_comparison_hint(
            self.sankey_card
        )
        self.sankey_card.v.addWidget(self.sankey)

        self.heatmap = HeatmapChart(lambda: self.app.theme)
        self.heatmap_card = GlassCard(
            "🗓️ Ritmo diário de gastos · 12 meses", strong=True
        )
        self.heatmap_card.set_compact_header(
            word_wrap=True,
            top_margin=7,
            bottom_margin=10,
            spacing=5,
            fit_context_header=True,
        )
        self.heatmap_card.v.addWidget(self.heatmap)
        self._wire_category_focus()
        self._wire_period_focus()
        self._configure_chart_details()

        self.dashboard_card_pool = QWidget(self)
        self.dashboard_card_pool.hide()
        self.dashboard_grid = QGridLayout()
        self.dashboard_grid.setSizeConstraint(QLayout.SetMinimumSize)
        self.dashboard_grid.setSpacing(16)
        self.body.addLayout(self.dashboard_grid)
        # QGridLayout can interpolate a preferred row above its reported
        # minimum around the three-column breakpoint. Reserve a small,
        # deterministic footer so the last card remains inside the scroll page
        # without deriving a new page height from live child geometries.
        self.body.addSpacing(48)
        self.dashboard_cards = [
            (
                spec.key,
                spec.setting_key,
                getattr(self, spec.widget_attribute),
                spec.preferred_span,
            )
            for spec in DASHBOARD_CARD_SPECS
        ]
        for key, _setting_key, card, _preferred_span in self.dashboard_cards:
            card.setProperty("pageCardKey", f"dashboard_{key}")
            card.setParent(self.dashboard_card_pool)
        self._update_card_context_badges()
        self._dashboard_default_visibility = {
            spec.setting_key: spec.default_visible for spec in DASHBOARD_CARD_SPECS
        }
        self._migrate_dashboard_card_preferences()
        self._dashboard_layout_signature: tuple[Any, ...] | None = None
        self._dashboard_row_count = 0

        self._apply_dashboard_density()
        self.startup_status_label = QLabel(
            "⏳ A preparar o Dashboard… A navegação já está disponível."
        )
        self.startup_status_label.setObjectName("Subtle")
        self.startup_status_label.setAlignment(Qt.AlignCenter)
        self.startup_status_label.setMinimumHeight(72)
        self.startup_status_label.setAccessibleName("Dashboard em preparação")
        self.metric_widgets = [self.startup_status_label]
        self.layout_metric_cards()

    def showEvent(self, event):  # type: ignore[override]
        super().showEvent(event)
        self._update_filter_group_widths()
        if (
            self._initial_load_pending
            and not self._startup_load_timer.isActive()
            and not bool(getattr(self.app, "_closing", False))
        ):
            self._startup_load_timer.start(self.STARTUP_LOAD_DELAY_MS)
        elif not self._initial_load_pending:
            self._dashboard_reflow_timer.start()

    def start_initial_load(self) -> None:
        """Reveal and populate the full dashboard after the first responsive paint."""

        if (
            not self._initial_load_pending
            or self._data_access_suspended
            or bool(getattr(self.app, "_closing", False))
        ):
            return
        stack = getattr(self.app, "stack", None)
        if stack is not None and stack.currentWidget() is not self:
            return
        self._initial_load_pending = False
        self._startup_load_timer.stop()
        self._dashboard_layout_signature = None
        self.rebuild_layout(force=True)
        self.refresh()

    def _add_category_comparison_hint(
        self,
        card: GlassCard,
        *,
        position: int | None = None,
    ) -> QLabel:
        if hasattr(card, "comparison_hint") and isinstance(
            getattr(card, "comparison_hint"), QLabel
        ):
            return getattr(card, "comparison_hint")
        label = QLabel()
        label.setObjectName("Subtle")
        label.setProperty("comparisonContext", True)
        label.setWordWrap(True)
        label.setMinimumWidth(0)
        label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Maximum)
        label.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        label.setAccessibleName("Comparação das categorias")
        label.hide()
        card.fit_label_height(
            label,
            word_wrap=True,
            stable_measurement=True,
        )
        if position is None:
            card.v.addWidget(label)
        else:
            card.v.insertWidget(max(0, int(position)), label)
        return label

    @staticmethod
    def _category_values(
        rows: list[tuple[str, float, str]],
    ) -> dict[str, float]:
        values: dict[str, float] = {}
        for row in rows or []:
            try:
                category, amount, _color = row
            except (TypeError, ValueError):
                continue
            name = str(category or "Sem categoria")
            values[name] = values.get(name, 0.0) + abs(safe_float(amount))
        return values

    def _category_comparison_text(
        self,
        current_rows: list[tuple[str, float, str]],
        previous_rows: list[tuple[str, float, str]],
        comparison_label: str,
        active: bool,
    ) -> str:
        if not active:
            return ""
        current = self._category_values(current_rows)
        previous = self._category_values(previous_rows)
        if not previous:
            return f"↔ Comparação com {comparison_label}: sem despesas de referência."

        current_total = sum(current.values())
        previous_total = sum(previous.values())
        if previous_total > 0.005:
            total_delta = (current_total - previous_total) / previous_total * 100.0
            total_arrow = (
                "↑" if total_delta > 0.5 else "↓" if total_delta < -0.5 else "＝"
            )
            total_text = f"total {total_arrow} {abs(total_delta):.0f}%"
        else:
            total_text = "total sem base comparável"

        changes = sorted(
            (
                (
                    abs(current.get(name, 0.0) - previous.get(name, 0.0)),
                    name,
                    current.get(name, 0.0),
                    previous.get(name, 0.0),
                )
                for name in set(current) | set(previous)
            ),
            reverse=True,
        )
        details: list[str] = []
        for _impact, name, current_value, previous_value in changes:
            if len(details) >= 3:
                break
            if previous_value > 0.005:
                delta = (current_value - previous_value) / previous_value * 100.0
                arrow = "↑" if delta > 0.5 else "↓" if delta < -0.5 else "＝"
                details.append(f"{name} {arrow} {abs(delta):.0f}%")
            elif current_value > 0.005:
                details.append(f"{name} · nova")
        suffix = " · ".join((total_text, *details))
        return f"↔ Vs. {comparison_label} · {suffix}"

    @staticmethod
    def _display_data_date(value: object) -> str:
        return format_iso_date(value, "sem movimentos")

    @staticmethod
    def _coverage_tone(context: dict[str, Any] | None) -> str:
        confidence = str((context or {}).get("confidence") or "initial")
        return {
            "high": "positive",
            "medium": "warning",
            "initial": "neutral",
        }.get(confidence, "neutral")

    def _coverage_badge_text(
        self,
        context: dict[str, Any] | None,
        *,
        include_coverage: bool = True,
    ) -> str:
        snapshot = dict(context or {})
        data_date = self._display_data_date(snapshot.get("data_as_of"))
        confidence = str(
            snapshot.get("confidence_label") or "Confiança inicial"
        ).casefold()
        if data_date == "sem movimentos":
            return f"Sem movimentos no intervalo · {confidence}"
        parts = [f"Dados até {data_date}"]
        if include_coverage:
            parts.append(f"cobertura {safe_float(snapshot.get('coverage_pct')):.0f}%")
        balances_total = int(snapshot.get("initial_balances_total", 0) or 0)
        costs_total = int(snapshot.get("investment_cost_total", 0) or 0)
        invalid = int(snapshot.get("invalid_transactions_excluded", 0) or 0)
        if balances_total:
            parts.append(
                "saldos "
                f"{int(snapshot.get('initial_balances_known', 0) or 0)}/"
                f"{balances_total}"
            )
        if costs_total:
            parts.append(
                "custos "
                f"{int(snapshot.get('investment_cost_known', 0) or 0)}/"
                f"{costs_total}"
            )
        parts.append(f"inválidos excluídos {invalid}")
        parts.append(confidence)
        return " · ".join(parts)

    @staticmethod
    def _data_coverage_badge() -> QLabel:
        label = QLabel()
        label.setObjectName("DataCoverageBadge")
        label.setProperty("tone", "neutral")
        label.setWordWrap(True)
        label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        label.setAccessibleName("Atualidade e confiança dos dados")
        label.hide()
        return label

    def _add_data_coverage_badge(
        self,
        card: GlassCard,
        *,
        position: int | None = None,
    ) -> QLabel:
        label = self._data_coverage_badge()
        card.fit_label_height(
            label,
            word_wrap=True,
            stable_measurement=True,
        )
        if position is None:
            card.v.addWidget(label)
        else:
            card.v.insertWidget(max(0, int(position)), label)
        return label

    @staticmethod
    def _set_data_coverage_badge(
        label: QLabel,
        text: str,
        tone: str = "neutral",
    ) -> None:
        value = str(text or "").strip()
        label.setText(value)
        label.setToolTip(value)
        label.setAccessibleDescription(value)
        label.setProperty("tone", str(tone or "neutral"))
        style = label.style()
        if style is not None:
            style.unpolish(label)
            style.polish(label)
        label.setVisible(bool(value))
        label.updateGeometry()
        card = label.parentWidget()
        refresh_heights = getattr(card, "refresh_compact_heights", None)
        if callable(refresh_heights):
            refresh_heights()

    def _update_card_context_badges(self) -> None:
        contexts = {
            "chart_card": ("Filtro atual · previsão 30 dias", "neutral"),
            "cashflow_pulse_card": ("Filtro atual", "neutral"),
            "savings_efficiency_card": ("Filtro atual", "neutral"),
            "waterfall_card": ("Filtro atual", "neutral"),
            "budget_card": ("Mês atual", "neutral"),
            "cashflow_card": (
                f"Previsão {self.cashflow_horizon} dias",
                "neutral",
            ),
            "donut_card": ("Filtro atual", "neutral"),
            "expense_work_time_card": ("Filtro atual", "neutral"),
            "goals_card": ("Estado atual", "positive"),
            "insights": ("Dados atuais", "neutral"),
            "radar_card": ("Filtro atual", "neutral"),
            "treemap_card": ("Filtro atual", "neutral"),
            "bubble_card": ("Filtro atual", "neutral"),
            "sankey_card": ("Filtro atual", "neutral"),
            "heatmap_card": ("Últimos 12 meses", "neutral"),
        }
        for attribute, (text, tone) in contexts.items():
            card = getattr(self, attribute, None)
            if card is not None and hasattr(card, "set_context_badge"):
                card.set_context_badge(text, tone)

    def _apply_metric_context_badges(
        self,
        *,
        income_context_months: int,
    ) -> None:
        contexts = {
            "Saldo atual": ("Estado atual", "positive"),
            "Receitas": ("Filtro atual", "neutral"),
            "Despesas": ("Filtro atual", "neutral"),
            "Poupança": ("Filtro atual", "neutral"),
            "Reserva de emergência": ("Estado atual", "positive"),
            "Valor do teu tempo": (
                (
                    f"Últimos {income_context_months} meses"
                    if income_context_months > 0
                    else "Média salarial"
                ),
                "neutral",
            ),
            "Projeção do mês": ("Mês atual", "warning"),
            "Património": ("Estado atual", "positive"),
        }
        for card in self.metric_widgets:
            if not self._qt_object_is_valid(card):
                continue
            card_title_fn = getattr(card, "card_title", None)
            title = ""
            try:
                if callable(card_title_fn):
                    title = card_title_fn()
                else:
                    title_label = getattr(card, "title_label", None)
                    if title_label is not None and self._qt_object_is_valid(title_label):
                        title = str(title_label.text() or "")
            except RuntimeError:
                continue
            text, tone = contexts.get(title, ("Filtro atual", "neutral"))
            if hasattr(card, "set_context_badge"):
                try:
                    card.set_context_badge(text, tone)
                except RuntimeError:
                    pass

    def _category_highlight_targets(self) -> tuple[QWidget, ...]:
        attributes = (
            "donut",
            "waterfall",
            "budget_chart",
            "expense_work_time_card",
            "radar",
            "treemap",
            "bubble",
            "sankey",
        )
        return tuple(
            widget
            for attribute in attributes
            if (widget := getattr(self, attribute, None)) is not None
        )

    def _wire_category_focus(self) -> None:
        for chart in self._category_highlight_targets():
            signal = getattr(chart, "category_hovered", None)
            if signal is not None:
                signal.connect(self._on_category_hovered)

    def _set_category_highlight(
        self,
        category: str,
        *,
        duration_ms: int = 0,
    ) -> None:
        selected = str(category or "").strip()
        self._category_highlight_timer.stop()
        self._highlighted_category = selected
        for chart in self._category_highlight_targets():
            setter = getattr(chart, "set_highlighted_category", None)
            if callable(setter):
                try:
                    setter(selected)
                except RuntimeError:
                    continue
        if selected and duration_ms > 0:
            self._category_highlight_timer.start(max(250, int(duration_ms)))

    def _on_category_hovered(self, category: str) -> None:
        selected = str(category or "").strip()
        if selected:
            self._set_category_highlight(selected)
        elif self._highlighted_category:
            # Keep the visual bridge alive while the pointer travels between
            # neighbouring charts, then clear it automatically.
            self._category_highlight_timer.start(650)

    def _clear_category_highlight(self) -> None:
        self._set_category_highlight("")

    def _period_highlight_targets(self) -> tuple[QWidget, ...]:
        return tuple(
            chart
            for attribute in ("chart", "cashflow_pulse", "savings_efficiency")
            if (chart := getattr(self, attribute, None)) is not None
        )

    def _wire_period_focus(self) -> None:
        for chart in self._period_highlight_targets():
            signal = getattr(chart, "period_hovered", None)
            if signal is not None:
                signal.connect(self._on_period_hovered)

    def _configure_chart_details(self) -> None:
        """Attach one consistent evidence panel to the Dashboard charts."""

        category_charts = {
            "categories": (self.donut, "Abrir movimentos"),
            "waterfall": (self.waterfall, "Abrir movimentos"),
            "budget": (self.budget_chart, "Abrir movimentos"),
            "treemap": (self.treemap, "Abrir movimentos"),
            "bubble": (self.bubble, "Abrir movimentos"),
            "sankey": (self.sankey, "Abrir movimentos"),
            "cashflow_future": (self.cashflow_chart, "Abrir origem"),
        }
        for key, (chart, action_label) in category_charts.items():
            chart.configure_details(
                lambda item, selected_key=key: self._chart_detail_payload(
                    selected_key,
                    item,
                ),
                action_label=action_label,
            )

        period_charts = {
            "balance": self.chart,
            "cashflow_pulse": self.cashflow_pulse,
            "savings_efficiency": self.savings_efficiency,
            "heatmap": self.heatmap,
        }
        for key, chart in period_charts.items():
            chart.configure_details(
                lambda item, selected_key=key: self._chart_detail_payload(
                    selected_key,
                    item,
                ),
                action_label="Abrir movimentos",
                action_handler=lambda item, selected_key=key: (
                    self._open_chart_detail_movements(selected_key, item)
                ),
            )

    def _chart_detail_period(self, item: dict[str, Any]) -> tuple[str, str]:
        raw = str(
            item.get("date") or item.get("date_str") or item.get("month") or ""
        )[:10]
        resolution = str(item.get("resolution") or "").casefold()
        try:
            selected = date.fromisoformat(raw)
        except (TypeError, ValueError):
            if len(raw) == 7:
                try:
                    selected = date.fromisoformat(f"{raw}-01")
                    resolution = "monthly"
                except ValueError:
                    return self.start_cache, self.end_cache
            else:
                return self.start_cache, self.end_cache
        if resolution == "monthly" or len(str(item.get("month") or "")) == 7:
            next_month = (
                date(selected.year + 1, 1, 1)
                if selected.month == 12
                else date(selected.year, selected.month + 1, 1)
            )
            start = selected.replace(day=1)
            end = next_month - timedelta(days=1)
        elif resolution == "weekly":
            start = selected
            end = selected + timedelta(days=6)
        else:
            start = selected
            end = selected
        try:
            selected_start = max(start, date.fromisoformat(self.start_cache))
            selected_end = min(end, date.fromisoformat(self.end_cache))
        except (TypeError, ValueError):
            selected_start, selected_end = start, end
        if selected_start > selected_end:
            selected_start, selected_end = start, end
        return selected_start.isoformat(), selected_end.isoformat()

    @staticmethod
    def _chart_detail_kind(key: str, item: dict[str, Any]) -> str:
        if key in {
            "categories",
            "budget",
            "treemap",
            "bubble",
            "sankey",
            "heatmap",
        }:
            return "Despesas"
        raw = str(item.get("kind") or "").casefold()
        if raw in {"expense", "despesas", "despesa"}:
            return "Despesas"
        if raw in {"income", "receitas", "receita"}:
            return "Receitas"
        return "Todos"

    @staticmethod
    def _chart_detail_category(key: str, item: dict[str, Any]) -> str:
        if key not in {
            "categories",
            "waterfall",
            "budget",
            "treemap",
            "bubble",
            "sankey",
            "cashflow_future",
        }:
            return ""
        category = str(item.get("category") or item.get("label") or "").strip()
        return "" if category.casefold().startswith("outros") else category

    def _chart_detail_payload(
        self,
        key: str,
        item: dict[str, Any],
    ) -> dict[str, Any]:
        start, end = self._chart_detail_period(item)
        category = self._chart_detail_category(key, item)
        kind = self._chart_detail_kind(key, item)
        rows = self.repo.chart_movement_details(
            start,
            end,
            category=category,
            kind=kind,
            limit=10,
        )
        movement_rows: list[dict[str, str]] = []
        for row in rows:
            row_amount = safe_float(row["amount"])
            row_category = str(row["effective_category"] or "Sem categoria")
            row_account = str(row["account"] or "")
            split_note = (
                " · parte de uma divisão" if bool(row["split_contribution"]) else ""
            )
            movement_rows.append(
                {
                    "date": format_iso_date(str(row["financial_date"] or "")[:10]),
                    "title": str(row["description"] or "Movimento"),
                    "subtitle": (
                        f"{row_category}"
                        + (f" · {row_account}" if row_account else "")
                        + split_note
                    ),
                    "value": eur(row_amount, self.app.theme.privacy),
                    "tone": "positive" if row_amount > 0 else "negative",
                }
            )

        titles = {
            "balance": "Evolução do saldo",
            "cashflow_pulse": "Entradas vs. saídas acumuladas",
            "savings_efficiency": "Margem e taxa de poupança",
            "categories": "Despesas por categoria",
            "waterfall": "Fluxo do dinheiro no período",
            "budget": "Orçamento vs. realizado",
            "treemap": "Peso das categorias",
            "bubble": "Categorias por volume",
            "sankey": "Distribuição das despesas",
            "cashflow_future": "Tesouraria futura",
            "heatmap": "Ritmo diário de gastos",
        }
        selected_label = str(
            item.get("category")
            or item.get("label")
            or item.get("name")
            or item.get("date")
            or item.get("date_str")
            or item.get("month")
            or ""
        )
        return {
            "icon": "🔎",
            "title": titles.get(key, "Detalhes do gráfico"),
            "subtitle": selected_label
            or f"{format_iso_date(start)} a {format_iso_date(end)}",
            "explanation": (
                "Os movimentos abaixo usam a mesma data financeira e as mesmas "
                "divisões por categoria do gráfico. Assim é possível confirmar "
                "exatamente de onde vem o valor."
            ),
            "rows_title": "Maiores movimentos que explicam o ponto",
            "rows": movement_rows,
            "show_empty_rows": True,
            "empty_rows_text": (
                "Não existem movimentos individuais neste ponto; pode tratar-se "
                "de uma previsão ou de um período sem atividade."
            ),
        }

    def _open_chart_detail_movements(
        self,
        key: str,
        item: dict[str, Any],
    ) -> None:
        start, end = self._chart_detail_period(item)
        self._open_movements_filter(
            self._chart_detail_kind(key, item),
            self._chart_detail_category(key, item),
            period_start=start,
            period_end=end,
        )

    def _set_period_highlight(self, period: str) -> None:
        selected = str(period or "").strip()
        self._period_highlight_timer.stop()
        self._highlighted_period = selected
        for chart in self._period_highlight_targets():
            setter = getattr(chart, "set_highlighted_period", None)
            if callable(setter):
                try:
                    setter(selected)
                except RuntimeError:
                    continue

    def _on_period_hovered(self, period: str) -> None:
        selected = str(period or "").strip()
        if selected:
            self._set_period_highlight(selected)
        elif self._highlighted_period:
            self._period_highlight_timer.start(650)

    def _clear_period_highlight(self) -> None:
        self._set_period_highlight("")

    def _on_waterfall_filter_clicked(self, kind: str, category: str) -> None:
        if category:
            self._set_category_highlight(category, duration_ms=2600)
        self._open_movements_filter(kind, category)

    @staticmethod
    def _sample_values(values: list[float], maximum_points: int = 24) -> list[float]:
        clean = [safe_float(value) for value in values]
        limit = max(2, int(maximum_points or 24))
        if len(clean) <= limit:
            return clean
        indices = {
            round(index * (len(clean) - 1) / (limit - 1)) for index in range(limit)
        }
        return [clean[index] for index in sorted(indices)]

    def _create_filter_group(
        self,
        title: str,
        *,
        control_hspacing: int = 6,
    ) -> tuple[QFrame, FlowLayout]:
        group = QFrame()
        group.setObjectName("DashboardFilterGroup")
        group.setProperty("filterGroup", True)
        group.setAccessibleName(f"Filtro {title.casefold()}")
        group.setMinimumWidth(0)
        group.setMaximumWidth(16_777_215)
        group.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)

        group_layout = QVBoxLayout(group)
        group_layout.setContentsMargins(8, 3, 8, 4)
        group_layout.setSpacing(2)
        hint = QLabel(title)
        hint.setObjectName("DashboardFilterLabel")
        group_layout.addWidget(hint)

        controls = QWidget()
        controls.setObjectName("DashboardFilterControls")
        controls.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        controls_flow = FlowLayout(
            controls,
            margin=0,
            hspacing=control_hspacing,
            vspacing=6,
        )
        group_layout.addWidget(controls)
        self._filter_groups.append(group)
        return group, controls_flow

    def set_dashboard_filters_expanded(self, expanded: bool) -> None:
        """Keep the common summary visible and collapse the advanced controls."""

        selected = bool(expanded)
        self.filters_card.setProperty("filtersExpanded", selected)
        self.filters_wrap.setVisible(selected)
        self.filters_disclosure_btn.setText(
            "\u25b4 Ocultar opções" if selected else "\u25be Mais opções"
        )
        self.filters_disclosure_btn.setToolTip(
            "Ocultar período, resolução, comparação e categorias"
            if selected
            else "Mostrar período, resolução, comparação e categorias"
        )
        if selected:
            self._update_filter_group_widths()
            self.filters_card.setMinimumHeight(0)
            self.filters_card.setMaximumHeight(16_777_215)
        else:
            self.filters_card.v.activate()
            self.filters_card.setFixedHeight(self.filters_card.sizeHint().height())
        self.filters_card.v.activate()
        self.filters_card.updateGeometry()
        self.body.activate()
        self.updateGeometry()
        sync = getattr(self.app, "sync_content_size", None)
        if callable(sync):
            QTimer.singleShot(0, sync)

    def _capture_page_filter_state(self) -> dict[str, object]:
        state = super()._capture_page_filter_state()
        state["cashflow_horizon"] = {
            "type": "number",
            "value": self.cashflow_horizon,
        }
        return state

    def _restore_page_filter_state(self, state: dict[str, object]) -> None:
        super()._restore_page_filter_state(state)
        horizon = state.get("cashflow_horizon")
        if isinstance(horizon, dict):
            try:
                selected = int(horizon.get("value", 60))
            except (TypeError, ValueError):
                selected = 60
            self._select_filter_value(self.cashflow_range_group, selected)
            self.change_cashflow_horizon(selected)

    def _update_filter_group_widths(self) -> None:
        if not hasattr(self, "period_filter_group"):
            return
        try:
            viewport_width = self.app.content_scroll.viewport().width()
        except (AttributeError, RuntimeError):
            viewport_width = self.width()
        wrap_width = self.filters_wrap.width() if hasattr(self, "filters_wrap") else 0
        available = max(260, int(wrap_width or viewport_width - 52))
        mode = (
            "wide" if available >= 1_220 else "medium" if available >= 700 else "narrow"
        )
        self.resolution_filter_group.setMinimumWidth(220 if mode == "wide" else 0)
        if mode != self._filter_layout_mode:
            while self.filters_grid.count():
                self.filters_grid.takeAt(0)
            for column in range(16):
                self.filters_grid.setColumnStretch(column, 0)

            if mode == "wide":
                placements = (
                    (self.period_filter_group, 0, 0, 1, 9),
                    (self.resolution_filter_group, 0, 9, 1, 3),
                    (self.comparison_filter_group, 0, 12, 1, 2),
                    (self.density_filter_group, 0, 14, 1, 1),
                    (self.category_filter_group, 0, 15, 1, 1),
                )
                column_count = 16
            elif mode == "medium":
                placements = (
                    (self.period_filter_group, 0, 0, 1, 9),
                    (self.resolution_filter_group, 0, 9, 1, 4),
                    (self.comparison_filter_group, 1, 0, 1, 7),
                    (self.density_filter_group, 1, 7, 1, 3),
                    (self.category_filter_group, 1, 10, 1, 3),
                )
                column_count = 13
            else:
                placements = (
                    (self.period_filter_group, 0, 0, 1, 2),
                    (self.resolution_filter_group, 1, 0, 1, 1),
                    (self.comparison_filter_group, 1, 1, 1, 1),
                    (self.density_filter_group, 2, 0, 1, 1),
                    (self.category_filter_group, 2, 1, 1, 1),
                )
                column_count = 2
            for group, row, column, row_span, column_span in placements:
                self.filters_grid.addWidget(
                    group,
                    row,
                    column,
                    row_span,
                    column_span,
                )
            for column in range(column_count):
                self.filters_grid.setColumnStretch(column, 1)
            self._filter_layout_mode = mode

        # Ensure filter groups dynamically adapt their minimum height so wrapped
        # button rows are never clipped regardless of window width.
        for group in self._filter_groups:
            layout = group.layout()
            if layout is not None and layout.count() >= 2:
                controls_item = layout.itemAt(1)
                controls_widget = controls_item.widget() if controls_item else None
                if controls_widget is not None and hasattr(controls_widget, "layout"):
                    widget_layout = controls_widget.layout()
                    if isinstance(widget_layout, FlowLayout):
                        group_w = group.width() if group.width() > 50 else (available // 2)
                        needed_w = max(100, group_w - 18)
                        req_h = widget_layout.heightForWidth(needed_w)
                        if req_h > 0:
                            group.setMinimumHeight(req_h + 26)

        self.filters_grid.activate()
        for group in self._filter_groups:
            group.updateGeometry()
        if hasattr(self, "filters_wrap"):
            self.filters_wrap.updateGeometry()

    def _update_filter_summary(self) -> None:
        if not hasattr(self, "filters_summary"):
            return

        def checked_text(group: QButtonGroup, fallback: str) -> str:
            button = group.checkedButton()
            return button.text() if button is not None else fallback

        period_text = checked_text(self.range_group, "3 meses")
        resolution_text = checked_text(self.res_group, "Diário")
        density_text = self.density_combo.currentText() or "Compacta"
        comparison_text = self.comparison.currentText() or "Sem comparação"
        category_text = self.category_limit_combo.currentText() or "Top 8"
        if category_text == "Todas":
            category_text = "Todas as categorias"
        else:
            category_text = f"{category_text} categorias"
        summary = (
            f"🔎 {period_text}  ·  {resolution_text}  ·  {comparison_text}"
            f"  ·  Vista {density_text.casefold()}  ·  {category_text}"
        )
        self.filters_summary.setText(summary)
        self.filters_card.setAccessibleDescription(summary)
        refresh_sticky = getattr(
            self.app,
            "refresh_dashboard_sticky_summary",
            None,
        )
        if callable(refresh_sticky):
            refresh_sticky()

    def _density_allows_card(self, key: str) -> bool:
        if self.dashboard_density == "detailed":
            return True
        spec = DASHBOARD_CARD_SPEC_BY_KEY.get(key)
        return bool(spec and spec.compact_visible)

    def _apply_dashboard_density(self) -> None:
        compact = self.dashboard_density == "compact"
        self.chart.setMinimumHeight(330 if compact else 450)
        self.chart_card.setMinimumHeight(370 if compact else 490)
        self.budget_card.setMaximumHeight(16_777_215)
        self.budget_card.setMinimumHeight(350 if compact else 390)
        self.budget_scroll.setFixedHeight(360 if compact else 420)
        self.filters_card.setProperty("compactDashboard", compact)
        self.filters_card.style().unpolish(self.filters_card)
        self.filters_card.style().polish(self.filters_card)

    def change_density(self, value: str) -> None:
        selected = value if value in {"compact", "detailed"} else "compact"
        if (
            hasattr(self, "density_combo")
            and self.density_combo.currentData() != selected
        ):
            self.density_combo.blockSignals(True)
            self.density_combo.setCurrentIndex(
                max(0, self.density_combo.findData(selected))
            )
            self.density_combo.blockSignals(False)
        if self.dashboard_density == selected:
            self._update_filter_summary()
            return
        self.dashboard_density = selected
        self.repo.set_setting("dashboard_density", selected)
        self._apply_dashboard_density()
        self._update_filter_summary()
        self._dashboard_layout_signature = None
        self.rebuild_layout(force=True)
        self.refresh()

    def change_category_limit(self, *_args) -> None:
        raw_limit = int(self.category_limit_combo.currentData() or 0)
        self.category_limit = raw_limit or None
        self.repo.set_setting(
            "dashboard_category_limit",
            str(raw_limit) if raw_limit else "all",
        )
        for chart_name in ("donut", "radar", "bubble", "sankey"):
            chart = getattr(self, chart_name, None)
            if chart is not None and hasattr(chart, "set_category_limit"):
                chart.set_category_limit(self.category_limit)
        self._update_filter_summary()
        self.updateGeometry()
        QTimer.singleShot(0, self.app.sync_content_size)

    def _migrate_dashboard_card_preferences(self) -> None:
        """Move the legacy show/hide flags into the page-wide card setting."""
        if str(self.repo.setting(self._card_setting_key(), "") or "").strip():
            return
        try:
            legacy = json.loads(self.repo.setting("dashboard_layout", "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            legacy = {}
        hidden = [
            f"dashboard_{key}"
            for key, setting_key, _card, _span in self.dashboard_cards
            if not legacy.get(
                setting_key,
                self._dashboard_default_visibility.get(setting_key, True),
            )
        ]
        self.repo.set_setting(
            self._card_setting_key(),
            json.dumps({"hidden": hidden, "permanent": []}, ensure_ascii=False),
        )

    def open_config(self):
        try:
            cfg_str = self.repo.setting("dashboard_layout", "{}")
            current_config = json.loads(cfg_str)
        except (TypeError, ValueError, json.JSONDecodeError):
            current_config = {}

        dlg = DashboardConfigDialog(current_config, self.app.theme, self.app)
        if dlg.exec():
            new_config = dlg.get_config()
            self.repo.set_setting("dashboard_layout", json.dumps(new_config))
            self.rebuild_layout(force=True)
            self.updateGeometry()
            try:
                self.app.sync_content_size()
            except Exception as exc:
                log_exception("Failed to sync dashboard size after layout config", exc)

    def resizeEvent(self, event):  # type: ignore[override]
        super().resizeEvent(event)
        self._dashboard_reflow_timer.start()
        self._dashboard_height_timer.start()

    def _on_treemap_clicked(self, category: str):
        self._set_category_highlight(category, duration_ms=2600)
        if str(category).casefold().startswith("outros") and self.category_limit:
            self.category_limit_combo.setCurrentIndex(
                max(0, self.category_limit_combo.findData(0))
            )
            return
        self._open_movements_filter(
            "Despesas",
            "" if category in {"Outros", "Outras despesas"} else category,
        )

    def _open_budget_category(self, category: str) -> None:
        self._set_category_highlight(category, duration_ms=2600)
        today = date.today()
        self._open_movements_filter(
            "Despesas",
            category,
            period_start=today.replace(day=1).isoformat(),
            period_end=today.isoformat(),
        )

    def _open_current_month_expenses(self) -> None:
        today = date.today()
        self._open_movements_filter(
            "Despesas",
            period_start=today.replace(day=1).isoformat(),
            period_end=today.isoformat(),
        )

    def _open_decision_action(
        self,
        page: str,
        category: str = "",
        search_text: str = "",
    ) -> None:
        """Open the exact area and context referenced by a financial priority."""
        if page == "movimentos":
            self._open_movements_filter(
                "Despesas",
                category,
                search_text=search_text,
            )
            return
        self.app.goto(page or "dashboard")

    def _edit_work_hours(self) -> None:
        current = safe_float(
            self.repo.setting(
                "work_hours_per_week",
                str(DEFAULT_WEEKLY_WORK_HOURS),
            ),
            DEFAULT_WEEKLY_WORK_HOURS,
        )
        hours, accepted = QInputDialog.getDouble(
            self,
            "Valor do teu tempo",
            "Quantas horas trabalhas por semana?",
            current,
            1.0,
            100.0,
            1,
        )
        if not accepted:
            return
        self.repo.set_setting("work_hours_per_week", f"{hours:.1f}")
        self.app.show_toast(
            f"Jornada atualizada para {hours:.1f} h por semana.",
            "⏱️",
            title="Valor do teu tempo",
            tone="success",
        )
        self.refresh()

    def _on_cashflow_event(self, event: dict[str, Any]) -> None:
        source = str(event.get("source") or "")
        source_id = int(event.get("source_id", 0) or 0)
        if source == "subscription":
            page = self.app.pages.get("subscricoes")
            if page:
                page.refresh()
                self.app.goto("subscricoes")
                if source_id and hasattr(page, "_select_id"):
                    page._select_id(source_id)
            return
        if source == "scheduled":
            page = self.app.pages.get("pagamentos")
            if page:
                page.refresh()
                self.app.goto("pagamentos")
                if source_id and hasattr(page, "_select_id"):
                    page._select_id(source_id)
            return

        event_date = str(event.get("date") or date.today().isoformat())[:10]
        category = str(event.get("category") or "")
        name = str(event.get("name") or "")
        if source == "recurring_income":
            today = date.today()
            self._open_movements_filter(
                "Receitas",
                category,
                period_start=(today - timedelta(days=210)).isoformat(),
                period_end=today.isoformat(),
                search_text=name,
            )
            return
        self._open_movements_filter(
            "Receitas" if event.get("kind") == "income" else "Despesas",
            category,
            period_start=event_date,
            period_end=event_date,
            search_text=name,
        )

    def _open_movements_filter(
        self,
        kind: str = "Todos",
        category: str = "",
        *,
        period_start: str = "",
        period_end: str = "",
        search_text: str = "",
    ) -> None:
        """Open Movimentos with the dashboard period and visual selection applied."""
        mov_page = self.app.pages.get("movimentos")
        if not mov_page:
            return

        period_blocked = mov_page.period.blockSignals(True)
        try:
            if period_start and period_end:
                custom_index = mov_page.period.findText("Personalizado")
                if custom_index >= 0:
                    mov_page.period.setCurrentIndex(custom_index)
                start = datetime.strptime(period_start, "%Y-%m-%d").date()
                end = datetime.strptime(period_end, "%Y-%m-%d").date()
                mov_page.start.setDate(to_qdate(start))
                mov_page.end.setDate(to_qdate(end))
            elif self.range_days is None:
                all_index = mov_page.period.findText("Tudo")
                if all_index >= 0:
                    mov_page.period.setCurrentIndex(all_index)
            else:
                custom_index = mov_page.period.findText("Personalizado")
                if custom_index >= 0:
                    mov_page.period.setCurrentIndex(custom_index)
                start = datetime.strptime(self.start_cache, "%Y-%m-%d").date()
                end = datetime.strptime(self.end_cache, "%Y-%m-%d").date()
                mov_page.start.setDate(to_qdate(start))
                mov_page.end.setDate(to_qdate(end))
        finally:
            mov_page.period.blockSignals(period_blocked)

        mov_page.search.setText(str(search_text or ""))
        attachment_index = mov_page.attach_filter.findText("Todos")
        if attachment_index >= 0:
            mov_page.attach_filter.setCurrentIndex(attachment_index)

        category_name = str(category or "").strip()
        category_index = (
            mov_page.category.findText(category_name) if category_name else 0
        )
        mov_page.category.setCurrentIndex(category_index if category_index >= 0 else 0)

        kind_name = kind if kind in {"Receitas", "Despesas"} else "Todos"
        kind_index = mov_page.kind.findText(kind_name)
        mov_page.kind.setCurrentIndex(kind_index if kind_index >= 0 else 0)
        self.app.goto("movimentos")
        mov_page.refresh()

    def reflow_dashboard(self) -> None:
        self._update_filter_group_widths()
        self.layout_metric_cards()
        # Keep the deliberately compact startup placeholder intact until the
        # first responsive paint has completed.  A resize event used to rebuild
        # and reveal every chart immediately, defeating ``STARTUP_LOAD_DELAY_MS``
        # and occasionally starving the GUI heartbeat on a cold Windows/Qt
        # launch.  ``start_initial_load`` performs the first forced rebuild.
        if not self._initial_load_pending:
            self.rebuild_layout()
        self.filters_card.v.invalidate()
        self.filters_card.v.activate()
        self.body.invalidate()
        self.body.activate()
        self.filters_card.updateGeometry()
        self.updateGeometry()
        sync_content_size = getattr(self.app, "sync_content_size", None)
        if callable(sync_content_size):
            sync_content_size()

    def dashboard_column_count(self) -> int:
        try:
            width = self.app.content_scroll.viewport().width()
        except (AttributeError, RuntimeError):
            width = self.width()
        return ResponsivePolicy.dashboard_columns(width)

    def metric_column_count(self) -> int:
        try:
            width = self.app.content_scroll.viewport().width()
        except (AttributeError, RuntimeError):
            width = self.width()
        return ResponsivePolicy.metric_columns(width)

    def clear_dashboard_layout(self) -> None:
        while self.dashboard_grid.count():
            self.dashboard_grid.takeAt(0)
        for row in range(self._dashboard_row_count + 2):
            self.dashboard_grid.setRowMinimumHeight(row, 0)
            self.dashboard_grid.setRowStretch(row, 0)
        self._dashboard_row_count = 0

    def _sync_dashboard_row_minimums(self) -> bool:
        """Refresh row minimums from intrinsic card sizes, never live geometry."""
        if not hasattr(self, "dashboard_grid"):
            return False
        row_minimums: dict[int, int] = {}
        for index in range(self.dashboard_grid.count()):
            item = self.dashboard_grid.itemAt(index)
            widget = item.widget()
            if widget is None or widget.isHidden():
                continue
            row, _column, row_span, _column_span = self.dashboard_grid.getItemPosition(
                index
            )
            if row_span != 1:
                continue
            row_minimums[row] = max(
                row_minimums.get(row, 0),
                widget.minimumHeight(),
                widget.minimumSizeHint().height(),
            )

        changed = False
        for row in range(self._dashboard_row_count):
            required = row_minimums.get(row, 0)
            if self.dashboard_grid.rowMinimumHeight(row) == required:
                continue
            self.dashboard_grid.setRowMinimumHeight(row, required)
            changed = True
        if changed:
            self.dashboard_grid.invalidate()
            self.dashboard_grid.activate()
            self.updateGeometry()
        return changed

    def _enforce_category_chart_separation(self, attempt: int = 0) -> None:
        """Grow the scroll page if Qt compresses any dynamic Dashboard row."""

        if not hasattr(self, "dashboard_grid"):
            return
        self.dashboard_grid.invalidate()
        self.dashboard_grid.activate()
        required_gap = max(10, self.dashboard_grid.verticalSpacing())
        row_bounds: dict[int, tuple[int, int]] = {}
        for index in range(self.dashboard_grid.count()):
            item = self.dashboard_grid.itemAt(index)
            widget = item.widget()
            if widget is None or widget.isHidden():
                continue
            row, _column, _row_span, _column_span = self.dashboard_grid.getItemPosition(
                index
            )
            top = widget.mapTo(self, QPoint()).y()
            bottom = top + widget.height()
            previous = row_bounds.get(row)
            row_bounds[row] = (
                min(previous[0], top) if previous else top,
                max(previous[1], bottom) if previous else bottom,
            )
        overlap = 0
        ordered_rows = sorted(row_bounds)
        for previous_row, next_row in zip(ordered_rows, ordered_rows[1:]):
            previous_bottom = row_bounds[previous_row][1]
            next_top = row_bounds[next_row][0]
            overlap = max(overlap, previous_bottom + required_gap - next_top)
        if overlap <= 0:
            return
        self.setMinimumHeight(max(self.minimumHeight(), self.height() + overlap + 2))
        self.updateGeometry()
        self.app.sync_content_size()
        if attempt < 2:
            QTimer.singleShot(
                60,
                lambda next_attempt=attempt + 1: (
                    self._enforce_category_chart_separation(next_attempt)
                ),
            )

    def primary_metric_column_count(self) -> int:
        try:
            viewport_width = self.app.content_scroll.viewport().width()
        except (AttributeError, RuntimeError):
            viewport_width = self.width()
        return primary_metric_columns(viewport_width)

    def toggle_secondary_metrics(self) -> None:
        self._secondary_metrics_expanded = not self._secondary_metrics_expanded
        if (
            getattr(self, "secondary_metrics_container", None) is not None
            and self._qt_object_is_valid(self.secondary_metrics_container)
        ):
            self.secondary_metrics_container.setVisible(self._secondary_metrics_expanded)
        if (
            getattr(self, "secondary_metrics_toggle_btn", None) is not None
            and self._qt_object_is_valid(self.secondary_metrics_toggle_btn)
        ):
            self.secondary_metrics_toggle_btn.setText(
                "▴ Ocultar indicadores adicionais"
                if self._secondary_metrics_expanded
                else "▾ Mostrar mais indicadores (4)"
            )
        self.updateGeometry()
        sync_size = getattr(self.app, "sync_content_size", None)
        if callable(sync_size):
            sync_size()

    def layout_metric_cards(self) -> None:
        if not getattr(self, "metric_widgets", None):
            return
        while self.cards_grid.count():
            self.cards_grid.takeAt(0)
        valid_widgets = []
        for widget in self.metric_widgets:
            try:
                widget.setMinimumWidth(180)
                widget.setMaximumWidth(16777215)
                widget.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
                valid_widgets.append(widget)
            except RuntimeError:
                pass

        if not valid_widgets:
            return

        cols = self.primary_metric_column_count()
        for col in range(4):
            self.cards_grid.setColumnStretch(col, 1 if col < cols else 0)
            self.cards_grid.setColumnMinimumWidth(col, 0)

        primary_widgets = [
            w for w in valid_widgets if getattr(w, "variant", "primary") == "primary"
        ]
        context_widgets = [
            w for w in valid_widgets if getattr(w, "variant", "primary") != "primary"
        ]
        if not primary_widgets and len(valid_widgets) > 4:
            primary_widgets = valid_widgets[:4]
            context_widgets = valid_widgets[4:]
        elif not primary_widgets:
            primary_widgets = valid_widgets

        for index, widget in enumerate(primary_widgets):
            self.cards_grid.addWidget(widget, index // cols, index % cols)

        if context_widgets:
            primary_rows = (len(primary_widgets) + cols - 1) // cols
            host = getattr(self, "_secondary_metrics_host", None)
            if (
                host is None
                or not self._qt_object_is_valid(host)
                or self.secondary_metrics_grid is None
            ):
                host = QWidget()
                host.setObjectName("SecondaryMetricsHost")
                host_layout = QVBoxLayout(host)
                host_layout.setContentsMargins(0, 4, 0, 0)
                host_layout.setSpacing(6)

                toggle_btn = QPushButton(
                    "▴ Ocultar indicadores adicionais"
                    if self._secondary_metrics_expanded
                    else f"▾ Mostrar mais indicadores ({len(context_widgets)})"
                )
                toggle_btn.setObjectName("Secondary")
                toggle_btn.setCursor(Qt.PointingHandCursor)
                toggle_btn.setAccessibleName(
                    "Mostrar ou ocultar indicadores adicionais do Dashboard"
                )
                toggle_btn.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
                toggle_btn.clicked.connect(self.toggle_secondary_metrics)
                self.secondary_metrics_toggle_btn = toggle_btn
                host_layout.addWidget(toggle_btn, 0, Qt.AlignLeft)

                container = QWidget()
                container.setObjectName("SecondaryMetricsContainer")
                container_grid = QGridLayout(container)
                container_grid.setContentsMargins(0, 0, 0, 0)
                container_grid.setSpacing(14)

                container.setVisible(self._secondary_metrics_expanded)
                self.secondary_metrics_container = container
                self.secondary_metrics_grid = container_grid
                host_layout.addWidget(container)
                self._secondary_metrics_host = host
            else:
                toggle_btn = self.secondary_metrics_toggle_btn
                if toggle_btn is not None and self._qt_object_is_valid(toggle_btn):
                    toggle_btn.setText(
                        "▴ Ocultar indicadores adicionais"
                        if self._secondary_metrics_expanded
                        else f"▾ Mostrar mais indicadores ({len(context_widgets)})"
                    )
                container = self.secondary_metrics_container
                container_grid = self.secondary_metrics_grid
                while container_grid.count():
                    container_grid.takeAt(0)
                if container is not None and self._qt_object_is_valid(container):
                    container.setVisible(self._secondary_metrics_expanded)

            for c_idx in range(4):
                container_grid.setColumnStretch(c_idx, 1 if c_idx < cols else 0)
                container_grid.setColumnMinimumWidth(c_idx, 0)

            for s_idx, s_widget in enumerate(context_widgets):
                container_grid.addWidget(s_widget, s_idx // cols, s_idx % cols)

            host.setVisible(True)
            self.cards_grid.addWidget(host, primary_rows, 0, 1, max(1, cols))
        else:
            host = getattr(self, "_secondary_metrics_host", None)
            if host is not None and self._qt_object_is_valid(host):
                host.setVisible(False)

    def card_preferences_changed(self) -> None:
        self._dashboard_layout_signature = None
        self.rebuild_layout(force=True)
        self.refresh()

    def rebuild_layout(self, force: bool = False):
        if self._initial_load_pending and not force:
            return
        try:
            cfg_str = self.repo.setting("dashboard_layout", "{}")
            config = json.loads(cfg_str)
        except (TypeError, ValueError, json.JSONDecodeError):
            config = {}

        self.dashboard_cards = order_card_entries(
            self.dashboard_cards,
            config.get("order", []),
        )

        cols = self.dashboard_column_count()
        visible = [
            (key, widget, preferred_span)
            for key, setting_key, widget, preferred_span in self.dashboard_cards
            if self.card_should_show(widget) and self._density_allows_card(key)
        ]
        visible = arrange_dashboard_companions(visible, cols)
        balanced_feature_columns = uses_balanced_feature_columns(visible, cols)
        signature = (
            cols,
            self.dashboard_density,
            tuple(key for key, _widget, _span in visible),
        )
        if not force and signature == self._dashboard_layout_signature:
            return
        self._dashboard_layout_signature = signature

        self.setMinimumHeight(0)
        self.clear_dashboard_layout()
        for _key, _setting_key, widget, _span in self.dashboard_cards:
            widget.setVisible(False)
        for col in range(3):
            self.dashboard_grid.setColumnStretch(col, 0)
            self.dashboard_grid.setColumnMinimumWidth(col, 0)

        if not visible:
            return

        placements = plan_dashboard_layout(
            visible,
            cols,
            balanced_feature_columns=balanced_feature_columns,
        )
        visible_widgets = {key: widget for key, widget, _span in visible}
        row_minimums: dict[int, int] = {}
        for placement in placements:
            widget = visible_widgets[placement.key]
            widget.setVisible(True)
            self.dashboard_grid.addWidget(
                widget,
                placement.row,
                placement.column,
                1,
                placement.column_span,
                Qt.AlignTop,
            )
            row_minimums[placement.row] = max(
                row_minimums.get(placement.row, 0),
                widget.minimumHeight(),
                widget.minimumSizeHint().height(),
            )

        self._dashboard_row_count = max(row_minimums, default=-1) + 1
        for layout_row, minimum_height in row_minimums.items():
            self.dashboard_grid.setRowMinimumHeight(layout_row, minimum_height)
            self.dashboard_grid.setRowStretch(layout_row, 0)

        column_stretches = dashboard_column_stretches(
            cols,
            balanced_feature_columns=balanced_feature_columns,
        )
        for grid_col, stretch in enumerate(column_stretches):
            self.dashboard_grid.setColumnStretch(grid_col, stretch)
        self.dashboard_grid.setSpacing(dashboard_grid_spacing(cols))
        self.dashboard_grid.invalidate()
        self.dashboard_grid.activate()
        self.updateGeometry()
        QTimer.singleShot(0, self.app.sync_content_size)
        QTimer.singleShot(60, self._enforce_category_chart_separation)

    def change_range(self, days: int) -> None:
        new_val = None if days == 0 else days
        if self.range_days != new_val:
            was_all = self.range_days is None
            self.range_days = new_val
            if new_val is None:
                if self.comparison_mode != "none":
                    self._comparison_before_all = self.comparison_mode
                self.comparison.blockSignals(True)
                self.comparison.setCurrentIndex(
                    max(0, self.comparison.findData("none"))
                )
                self.comparison.blockSignals(False)
                self.comparison_mode = "none"
                self.comparison.setEnabled(False)
                self.comparison.setToolTip(
                    "A comparação não se aplica quando está selecionado todo o histórico."
                )
            elif was_all:
                restored = self._comparison_before_all or "previous"
                self.comparison.setEnabled(True)
                self.comparison.setToolTip("")
                self.comparison.blockSignals(True)
                self.comparison.setCurrentIndex(
                    max(0, self.comparison.findData(restored))
                )
                self.comparison.blockSignals(False)
                self.comparison_mode = restored
            self._update_filter_summary()
            self.refresh()

    @staticmethod
    def _select_filter_value(group: QButtonGroup, value: Any) -> None:
        for button in group.buttons():
            button.setChecked(button.property("filterValue") == value)

    def _reset_page_filters(self) -> None:
        """Restore the Dashboard's useful defaults with a single refresh."""
        self.range_days = 90
        self.resolution = "daily"
        self.comparison_mode = "previous"
        self._comparison_before_all = "previous"
        self.cashflow_horizon = 60
        self._select_filter_value(self.range_group, 90)
        self._select_filter_value(self.res_group, "daily")
        self._select_filter_value(self.cashflow_range_group, 60)
        self.category_limit_combo.blockSignals(True)
        self.category_limit_combo.setCurrentIndex(
            max(0, self.category_limit_combo.findData(8))
        )
        self.category_limit_combo.blockSignals(False)
        self.category_limit = 8
        self.repo.set_setting("dashboard_category_limit", "8")
        for chart_name in ("donut", "radar", "bubble", "sankey"):
            chart = getattr(self, chart_name, None)
            if chart is not None and hasattr(chart, "set_category_limit"):
                chart.set_category_limit(8)
        self.comparison.blockSignals(True)
        self.comparison.setCurrentIndex(max(0, self.comparison.findData("previous")))
        self.comparison.blockSignals(False)
        self.comparison.setEnabled(True)
        self.comparison.setToolTip("")
        if self._cashflow_data:
            self.cashflow_chart.set_horizon(60)
            self._update_cashflow_summary(self._cashflow_data)
        self._update_filter_summary()
        self.refresh()

    def change_resolution(self, res: str) -> None:
        if self.resolution != res:
            self.resolution = res
            self._update_filter_summary()
            self.refresh()

    def change_comparison(self, _index: int = -1) -> None:
        mode = str(self.comparison.currentData() or "previous")
        if self.comparison_mode != mode:
            self.comparison_mode = mode
            self._update_filter_summary()
            self.refresh()

    def change_cashflow_horizon(self, days: int) -> None:
        selected = max(30, min(90, int(days or 60)))
        if self.cashflow_horizon == selected and self._cashflow_data:
            return
        self.cashflow_horizon = selected
        self._update_card_context_badges()
        if self._cashflow_data:
            self.cashflow_chart.set_horizon(selected)
            self._update_cashflow_summary(self._cashflow_data)

    def _refresh_cashflow_forecast(self, data: dict[str, Any]) -> None:
        self._cashflow_data = dict(data or {})
        self.cashflow_chart.set_data(self._cashflow_data, self.cashflow_horizon)
        self._update_cashflow_summary(self._cashflow_data)

    def _update_cashflow_summary(self, data: dict[str, Any]) -> None:
        summaries = data.get("summaries", {}) or {}
        summary = summaries.get(str(self.cashflow_horizon), {}) or {}
        if not summary and summaries:
            closest = min(
                summaries,
                key=lambda key: abs(int(key) - self.cashflow_horizon),
            )
            summary = summaries.get(closest, {}) or {}

        if hasattr(self.cashflow_card, "set_card_title"):
            self.cashflow_card.set_card_title(
                f"🗓️ Tesouraria futura · {self.cashflow_horizon} dias"
            )
        elif self.cashflow_card.title_label is not None and self._qt_object_is_valid(
            self.cashflow_card.title_label
        ):
            try:
                self.cashflow_card.title_label.setText(
                    f"🗓️ Tesouraria futura · {self.cashflow_horizon} dias"
                )
            except RuntimeError:
                pass
        self.cashflow_card.set_context_badge(f"Previsão {self.cashflow_horizon} dias")
        tone = str(summary.get("tone") or "neutral")
        tone_map = {
            "positive": ("✅", GREEN),
            "warning": ("⚠️", ORANGE),
            "negative": ("🚨", RED),
            "neutral": ("🧭", BLUE),
        }
        icon, tone_color = tone_map.get(tone, tone_map["neutral"])
        overdue = int(summary.get("overdue_count", 0) or 0)
        event_count = int(summary.get("event_count", 0) or 0)
        coverage_text = self._coverage_badge_text(
            self._forecast_data_context,
            include_coverage=True,
        )
        calibration = dict(data.get("calibration") or {})
        calibration_samples = int(calibration.get("sample_months", 0) or 0)
        validation_samples = int(calibration.get("validation_months", 0) or 0)
        if bool(calibration.get("enabled")):
            coverage_text += f" · 🧠 {calibration_samples} aprendidos"
            if bool(calibration.get("accuracy_available")):
                coverage_text += (
                    f" · {validation_samples} validados"
                    f" · dentro das faixas "
                    f"{safe_float(calibration.get('accuracy_pct')):.0f}%"
                )
            else:
                coverage_text += " · cobertura das faixas em validação"
        else:
            required_samples = int(calibration.get("required_months", 3) or 3)
            coverage_text += (
                f" · 🧠 aprendizagem {calibration_samples}/{required_samples}"
            )
        if event_count:
            coverage_text += f" · {event_count} evento{'s' if event_count != 1 else ''}"
        self._set_data_coverage_badge(
            self.cashflow_data_badge,
            coverage_text,
            self._coverage_tone(self._forecast_data_context),
        )
        suffix = f"{event_count} evento(s) previsto(s)"
        if overdue:
            suffix += f" · {overdue} vencido(s)"
        self.cashflow_status_label.setText(
            f"{icon} {summary.get('status', 'Sem previsão')} · {suffix}"
        )
        self.cashflow_status_label.setStyleSheet(
            "font-weight: 700; padding: 8px 12px; "
            f"background: {self._rgba_style(tone_color, 32)}; "
            f"border: 1px solid {self._rgba_style(tone_color, 78)}; "
            "border-radius: 12px;"
        )

        starting = safe_float(data.get("starting_balance"))
        ending = safe_float(summary.get("ending_balance"))
        minimum = safe_float(summary.get("minimum_balance"))
        minimum_date = str(summary.get("minimum_date") or "")
        try:
            minimum_label = datetime.strptime(minimum_date, "%Y-%m-%d").strftime(
                "%d/%m"
            )
        except (TypeError, ValueError):
            minimum_label = "período"
        chip_data = [
            ("💰 Saldo hoje", eur(starting, self.app.theme.privacy), BLUE),
            (
                f"📅 Saldo +{self.cashflow_horizon}d",
                eur(ending, self.app.theme.privacy),
                GREEN if ending >= starting else ORANGE if ending >= 0 else RED,
            ),
            (
                f"🛟 Mínimo · {minimum_label}"
                if minimum >= 0
                else f"🚨 Mínimo · {minimum_label}",
                eur(minimum, self.app.theme.privacy),
                GREEN if minimum >= 0 else RED,
            ),
        ]
        for label, (title, value, color) in zip(
            self.cashflow_summary_labels, chip_data
        ):
            label.setText(f"{title}  ·  {value}")
            label.setAccessibleName(f"{title}: {value}")
            label.setStyleSheet(
                "font-weight: 650; padding: 7px 11px; "
                f"background: {self._rgba_style(color, 24)}; "
                f"border: 1px solid {self._rgba_style(color, 62)}; "
                "border-radius: 11px;"
            )

        hint = str(data.get("method_label") or "")
        hint += (
            f" Entradas previstas: {eur(summary.get('expected_income', 0), self.app.theme.privacy)}."
            f" Compromissos: {eur(summary.get('known_commitments', 0), self.app.theme.privacy)}."
            f" Despesa variável estimada: {eur(summary.get('baseline_expenses', 0), self.app.theme.privacy)}."
        )
        optimistic = safe_float(summary.get("ending_balance_optimistic"))
        pessimistic = safe_float(summary.get("ending_balance_pessimistic"))
        if optimistic or pessimistic:
            hint += (
                " Faixa no horizonte: "
                f"{eur(pessimistic, self.app.theme.privacy)} a "
                f"{eur(optimistic, self.app.theme.privacy)}."
            )
        if bool(calibration.get("enabled")):
            correction_pct = (
                safe_float(calibration.get("correction_factor"), 1.0) - 1.0
            ) * 100.0
            direction = "acima" if correction_pct >= 0 else "abaixo"
            hint += (
                f" 🧠 O histórico corrigiu a despesa variável "
                f"{abs(correction_pct):.0f}% para {direction}."
            )
            if bool(calibration.get("accuracy_available")):
                hint += (
                    f" Nos {validation_samples} meses avaliados sem conhecer "
                    "o resultado, "
                    f"{safe_float(calibration.get('accuracy_pct')):.0f}% "
                    "ficaram dentro da faixa prevista e o erro médio pontual foi "
                    f"{safe_float(calibration.get('mape_pct')):.0f}%."
                )
            else:
                hint += (
                    " A cobertura só será mostrada depois de existir um mês "
                    "avaliado sem usar o próprio resultado."
                )
        risk_date = str(summary.get("risk_date") or "")
        if risk_date:
            hint += f" 🚨 Primeiro saldo negativo previsto em {risk_date}."
        elif overdue:
            hint += " ⚠️ Existem pagamentos vencidos incluídos no próximo dia."
        self.cashflow_hint.setText(hint.strip())
        self.cashflow_hint.setAccessibleDescription(hint.strip())

    @staticmethod
    def _rgba_style(color_value: str, alpha: int) -> str:
        color = QColor(str(color_value or BLUE))
        if not color.isValid():
            color = QColor(BLUE)
        return (
            f"rgba({color.red()},{color.green()},{color.blue()},"
            f"{max(0, min(255, int(alpha)))})"
        )

    def _refresh_budget_forecast(self, forecast: dict[str, Any]) -> None:
        rows = forecast.get("rows", []) or []
        month_label = str(forecast.get("month_label") or "Mês atual")
        self.budget_chart.set_rows(rows, month_label)
        self.budget_card.set_context_badge("Mês atual")
        if hasattr(self.budget_card, "set_card_title"):
            self.budget_card.set_card_title(
                f"🎯 Orçamento vs. realizado · {month_label}"
            )
        elif self.budget_card.title_label is not None and self._qt_object_is_valid(
            self.budget_card.title_label
        ):
            try:
                self.budget_card.title_label.setText(
                    f"🎯 Orçamento vs. realizado · {month_label}"
                )
            except RuntimeError:
                pass
        budget_date = self._display_data_date(forecast.get("as_of"))
        coverage = safe_float(forecast.get("coverage_pct"))
        total_spent = safe_float(forecast.get("total_spent"))
        if total_spent > 0.005:
            budget_context = (
                f"Dados até {budget_date} · {coverage:.0f}% do gasto "
                "coberto por orçamentos"
            )
            budget_context_tone = (
                "positive"
                if coverage >= 85
                else "warning"
                if coverage >= 55
                else "negative"
            )
        else:
            budget_context = f"Dados até {budget_date} · ainda sem despesas"
            budget_context_tone = "neutral"
        budget_learning = dict(forecast.get("forecast_learning") or {})
        if bool(budget_learning.get("enabled")):
            budget_context += (
                f" · 🧠 {int(budget_learning.get('sample_months', 0) or 0)} aprendidos"
            )
            if bool(budget_learning.get("accuracy_available")):
                budget_context += (
                    f" · {int(budget_learning.get('validation_months', 0) or 0)} "
                    "validados"
                    f" · dentro das faixas "
                    f"{safe_float(budget_learning.get('accuracy_pct')):.0f}%"
                )
            else:
                budget_context += " · cobertura das faixas em validação"
        self._set_data_coverage_badge(
            self.budget_data_badge,
            budget_context,
            budget_context_tone,
        )

        tone = str(forecast.get("overall_tone") or "neutral")
        tone_map = {
            "positive": ("✅", GREEN),
            "warning": ("⚠️", ORANGE),
            "negative": ("🚨", RED),
            "neutral": ("🧭", BLUE),
        }
        icon, tone_color = tone_map.get(tone, tone_map["neutral"])
        critical_count = int(forecast.get("risk_count", 0) or 0) + int(
            forecast.get("over_count", 0) or 0
        )
        critical_text = (
            f"{critical_count} categoria(s) crítica(s)"
            if critical_count
            else "sem categorias críticas"
        )
        self.budget_status_label.setText(
            f"{icon} {forecast.get('overall_status', 'Sem estado')} · {critical_text}"
        )
        self.budget_status_label.setStyleSheet(
            "font-weight: 700; padding: 8px 12px; "
            f"background: {self._rgba_style(tone_color, 32)}; "
            f"border: 1px solid {self._rgba_style(tone_color, 78)}; "
            "border-radius: 12px;"
        )

        margin = safe_float(forecast.get("projected_margin"))
        chip_data = [
            (
                "💶 Orçamento",
                eur(safe_float(forecast.get("total_budget")), self.app.theme.privacy),
                BLUE,
            ),
            (
                "📍 Realizado",
                eur(safe_float(forecast.get("total_spent")), self.app.theme.privacy),
                PINK,
            ),
            (
                "🔮 Previsto",
                eur(
                    safe_float(forecast.get("total_projected")),
                    self.app.theme.privacy,
                ),
                ORANGE,
            ),
            (
                "🛟 Margem prevista" if margin >= 0 else "🚨 Défice previsto",
                eur(margin, self.app.theme.privacy),
                GREEN if margin >= 0 else RED,
            ),
        ]
        for label, (title, value, color) in zip(self.budget_summary_labels, chip_data):
            label.setText(f"{title}  ·  {value}")
            label.setMinimumWidth(170)
            label.setMaximumWidth(300)
            label.setAccessibleName(f"{title}: {value}")
            label.setStyleSheet(
                "font-weight: 650; padding: 7px 11px; "
                f"background: {self._rgba_style(color, 24)}; "
                f"border: 1px solid {self._rgba_style(color, 62)}; "
                "border-radius: 11px;"
            )

        hint = str(forecast.get("method_label") or "")
        elapsed = int(forecast.get("days_elapsed", 0) or 0)
        month_days = int(forecast.get("days_in_month", 0) or 0)
        if month_days:
            hint += f" Dados até ao dia {elapsed} de {month_days}."
        unbudgeted = safe_float(forecast.get("unbudgeted_spend"))
        if unbudgeted > 0:
            coverage = safe_float(forecast.get("coverage_pct"))
            hint += (
                f" ⚠️ {eur(unbudgeted, self.app.theme.privacy)} sem orçamento "
                f"({percent(coverage)} do gasto está coberto)."
            )
        projected_low = safe_float(forecast.get("total_projected_optimistic"))
        projected_high = safe_float(forecast.get("total_projected_pessimistic"))
        if bool(budget_learning.get("enabled")) and (projected_low or projected_high):
            hint += (
                " Faixa aprendida para o fecho do mês: "
                f"{eur(projected_low, self.app.theme.privacy)} a "
                f"{eur(projected_high, self.app.theme.privacy)}."
            )
        self.budget_hint.setText(hint.strip())
        self.budget_hint.setAccessibleDescription(hint.strip())
        self._sync_budget_card_height()

    def _sync_budget_card_height(self) -> None:
        """Fit the budget card to its real width without distributing blank space."""
        if not hasattr(self, "budget_card"):
            return
        base_height = 350 if self.dashboard_density == "compact" else 390
        margins = self.budget_card.v.contentsMargins()
        content_width = max(
            1,
            self.budget_card.width() - margins.left() - margins.right(),
        )

        def required_widget_height(widget: QWidget) -> int:
            height = widget.heightForWidth(content_width)
            if height < 0:
                height = widget.sizeHint().height()
            return max(1, widget.minimumSizeHint().height(), int(height))

        title_lbl = getattr(self.budget_card, "title_label", None)
        title_lbl_valid = title_lbl is not None and self._qt_object_is_valid(title_lbl)
        try:
            title_height = (
                required_widget_height(title_lbl)
                if title_lbl_valid
                else 0
            )
        except RuntimeError:
            title_height = 0
            title_lbl_valid = False
        header_height = max(
            self.budget_header_flow.minimumSize().height(),
            self.budget_header_flow.heightForWidth(content_width),
        )
        summary_height = max(
            self.budget_summary_flow.minimumSize().height(),
            self.budget_summary_flow.heightForWidth(content_width),
        )
        hint_height = required_widget_height(self.budget_hint)

        if title_lbl_valid:
            try:
                title_lbl.setFixedHeight(title_height)
            except RuntimeError:
                title_lbl_valid = False
        self.budget_header_wrap.setFixedHeight(header_height)
        self.budget_summary_wrap.setFixedHeight(summary_height)
        self.budget_hint.setFixedHeight(hint_height)

        visible_sections = 5 if title_lbl_valid else 4
        required = (
            margins.top()
            + margins.bottom()
            + title_height
            + header_height
            + summary_height
            + hint_height
            + self.budget_scroll.height()
            + max(0, visible_sections - 1) * self.budget_card.v.spacing()
        )
        required = max(base_height, required)
        height_changed = (
            self.budget_card.minimumHeight() != required
            or self.budget_card.maximumHeight() != required
        )
        if height_changed:
            self.budget_card.setFixedHeight(required)
        self.budget_card.updateGeometry()
        row_minimums_changed = self._sync_dashboard_row_minimums()
        if (height_changed or row_minimums_changed) and hasattr(
            self,
            "dashboard_grid",
        ):
            self.dashboard_grid.invalidate()
            self.dashboard_grid.activate()
            self.updateGeometry()
            sync_content_size = getattr(self.app, "sync_content_size", None)
            if callable(sync_content_size):
                QTimer.singleShot(0, sync_content_size)
                QTimer.singleShot(60, sync_content_size)

    def selected_period(self) -> tuple[str, str, str]:
        today = date.today()
        if self.range_days is None:
            return "1900-01-01", iso(today), "todo o histórico"
        days = max(1, int(self.range_days or 1))
        if days == 1:
            return iso(today), iso(today), "hoje"
        calendar_months = {31: 1, 90: 3, 183: 6, 365: 12}.get(days)
        if calendar_months is not None:
            start, _end, _actual_days = rolling_month_window(
                today,
                calendar_months,
            )
        else:
            start = today - timedelta(days=days - 1)
        labels = {
            7: "últimos 7 dias",
            15: "últimos 15 dias",
            31: "último mês",
            90: "últimos 3 meses",
            183: "últimos 6 meses",
            365: "último ano",
        }
        return iso(start), iso(today), labels.get(days, f"últimos {days} dias")

    def clear_layout(self, layout: QLayout) -> None:  # type: ignore[name-defined]
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                # Deferred deletion leaves a briefly valid wrapper. A fast
                # worker result must not reparent new cards into that old host.
                if widget is self._secondary_metrics_host:
                    self._secondary_metrics_host = None
                    self.secondary_metrics_container = None
                    self.secondary_metrics_grid = None
                    self.secondary_metrics_toggle_btn = None
                widget.hide()
                widget.deleteLater()

    def shutdown_workers(self, timeout_ms: int = 2500) -> bool:
        """Stop accepting results and let active dashboard queries close cleanly."""
        self._data_access_suspended = True
        self._startup_load_timer.stop()
        self._dashboard_reflow_timer.stop()
        self._dashboard_height_timer.stop()
        self._category_highlight_timer.stop()
        self._period_highlight_timer.stop()
        self._initial_load_pending = False
        self._dashboard_request_id += 1
        workers = tuple(self._workers.values())
        for worker in workers:
            if worker.isRunning():
                worker.cancel()
        deadline = time.monotonic() + max(0, timeout_ms) / 1000.0
        active_workers: dict[int, DashboardWorker] = {}
        for request_id, worker in tuple(self._workers.items()):
            if worker.isRunning():
                remaining_ms = max(0, int((deadline - time.monotonic()) * 1000))
                if remaining_ms:
                    worker.wait(remaining_ms)
            if worker.isFinished():
                worker.deleteLater()
            elif worker.isRunning():
                active_workers[request_id] = worker
        self._workers = active_workers
        if self.worker not in active_workers.values():
            self.worker = None
        return not active_workers

    def resume_data_access(self) -> None:
        """Allow queries again after an aborted close or a completed restore."""

        if bool(getattr(self.app, "_closing", False)):
            return
        database = getattr(getattr(self.repo, "db", None), "conn", None)
        if database is None or bool(
            getattr(getattr(self.repo, "db", None), "_closed", False)
        ):
            return
        self._data_access_suspended = False

    def _release_worker(self, request_id: int, worker: DashboardWorker) -> None:
        if self._workers.get(request_id) is worker:
            self._workers.pop(request_id, None)
        # Keep the latest worker alive for the existing diagnostic smoke script;
        # superseded workers can be disposed as soon as their thread stops.
        if worker is not self.worker:
            worker.deleteLater()

    def refresh(self) -> None:
        if (
            self._data_access_suspended
            or bool(getattr(self.app, "_closing", False))
            or bool(getattr(self.app, "_database_closed", False))
            or bool(getattr(getattr(self.repo, "db", None), "_closed", False))
        ):
            return
        if self._initial_load_pending:
            self._initial_load_pending = False
            self._startup_load_timer.stop()
            self._dashboard_layout_signature = None
            self.rebuild_layout(force=True)
        startup_status = getattr(self, "startup_status_label", None)
        if startup_status is not None:
            try:
                startup_status.hide()
            except RuntimeError:
                pass
            self.startup_status_label = None
        start, end, period_label = self.selected_period()
        self.period_label_cache = period_label
        self.start_cache = start
        self.end_cache = end
        self._update_card_context_badges()
        self._dashboard_request_id += 1
        request_id = self._dashboard_request_id

        for old_worker in tuple(self._workers.values()):
            if old_worker.isRunning():
                old_worker.cancel()

        self.retry_btn.hide()

        self.clear_layout(self.cards_grid)
        from ui.components.animations import SkeletonWidget

        skels = []
        for _ in range(8):
            skel = SkeletonWidget()
            skel.setMinimumSize(220, 110)
            skels.append(skel)
        self.metric_widgets = skels
        self.layout_metric_cards()

        previous_worker = self.worker
        enabled_modules = {
            key
            for key, _setting_key, card, _span in self.dashboard_cards
            if self.should_compute_card(card) and self._density_allows_card(key)
        }
        self.worker = DashboardWorker(
            request_id,
            self.repo,
            self.analytics_service,
            start,
            end,
            self.range_days,
            self.resolution,
            self.comparison_mode,
            enabled_modules,
            self,
            period_label=period_label,
        )
        if previous_worker is not None and previous_worker.isFinished():
            previous_worker.deleteLater()
        self._workers[request_id] = self.worker
        self.worker.finished_data.connect(self._on_data_loaded)
        self.worker.failed.connect(self._on_data_error)
        self.worker.finished.connect(
            lambda rid=request_id, worker=self.worker: self._release_worker(rid, worker)
        )
        if not self.app.background_task_registry.register_thread(
            self.worker,
            label="Cálculos do Dashboard",
            cancel=self.worker.cancel,
        ):
            self._workers.pop(request_id, None)
            self.worker.deleteLater()
            self.worker = None
            return
        self.worker.start()

    def _on_data_error(self, request_id: int, error: str) -> None:
        if self._data_access_suspended or request_id != self._dashboard_request_id:
            return
        self.clear_layout(self.cards_grid)
        error_card = MetricCard(
            "Não foi possível atualizar",
            "Tenta novamente",
            "Os dados anteriores permanecem nos gráficos.",
            RED,
            "⚠️",
            status="Erro",
            tone="negative",
            progress=None,
            accessible_text="Erro ao atualizar o dashboard. Tenta novamente.",
        )
        error_card.setToolTip(str(error)[:300])
        self.metric_widgets = [error_card]
        self.layout_metric_cards()
        self.apply_card_preferences()
        self.retry_btn.show()
        self.mark_data_refresh_failed()

    def _on_data_loaded(self, request_id: int, data: dict) -> None:
        if self._data_access_suspended or request_id != self._dashboard_request_id:
            return
        self._clear_period_highlight()
        self.clear_layout(self.cards_grid)
        self._last_categories = list(data.get("categories") or [])
        for category_chart in (
            self.donut,
            getattr(self, "radar", None),
            getattr(self, "bubble", None),
            getattr(self, "sankey", None),
        ):
            if category_chart is not None and hasattr(
                category_chart, "set_category_limit"
            ):
                category_chart.set_category_limit(self.category_limit)

        totals = data["totals"]
        totals_prev = data.get("totals_prev") or {}
        comparison_label = str(data.get("comparison_label") or "período anterior")
        comparison_active = bool(data.get("comparison_start")) and (
            self.comparison_mode != "none"
        )
        current_categories = list(data.get("categories") or [])
        previous_categories = list(data.get("previous_categories") or [])
        self._data_context = dict(data.get("data_context") or {})
        self._forecast_data_context = dict(data.get("forecast_data_context") or {})
        # Keep the confidence chip intentionally short.  Full coverage belongs
        # to the dedicated wrapped disclosure below each forecast; duplicating
        # it here forced the first two Dashboard columns beyond the viewport.
        forecast_confidence = str(
            self._forecast_data_context.get("confidence_label") or "Inicial"
        ).strip()
        forecast_tone = self._coverage_tone(self._forecast_data_context)
        for predictive_card in (
            self.chart_card,
            self.budget_card,
            self.cashflow_card,
        ):
            predictive_card.set_confidence_badge(
                f"Confiança · {forecast_confidence}",
                forecast_tone,
            )
        balance = data["balance"]
        net = data["net"]
        period_label = getattr(self, "period_label_cache", "")

        savings_rate = (
            totals["savings"] / totals["income"] * 100 if totals["income"] else 0
        )
        burn_rate = data.get("burn_rate", 0)
        forecast_end_month = data.get("forecast_end_month", 0)
        monthly_expense = safe_float(data.get("monthly_expense"))
        financial_independence = data.get("financial_independence", {}) or {}

        def comparison(current, previous, increase_is_good: bool):
            if previous is None or abs(safe_float(previous)) < 0.01:
                if self.comparison_mode == "none":
                    return "", "Comparação desligada", "neutral"
                return "", f"Sem dados no {comparison_label}", "neutral"
            delta = (
                (safe_float(current) - safe_float(previous))
                / abs(safe_float(previous))
                * 100
            )
            if abs(delta) < 0.5:
                return " · =", f"＝ Estável vs {comparison_label}", "neutral"
            rising = delta > 0
            arrow = "↑" if rising else "↓"
            suffix = f" · {arrow} {abs(delta):.0f}%"
            good = rising if increase_is_good else not rising
            tone = "positive" if good else "negative"
            status = f"{arrow} {abs(delta):.0f}% vs {comparison_label}"
            return suffix, status, tone

        inc_delta, inc_status, inc_tone = comparison(
            totals["income"], totals_prev.get("income"), True
        )
        exp_delta, exp_status, exp_tone = comparison(
            totals["expenses"], totals_prev.get("expenses"), False
        )
        sav_delta, _sav_comparison, _sav_tone = comparison(
            totals["savings"], totals_prev.get("savings"), True
        )

        if balance < 0:
            balance_status, balance_tone, balance_progress = (
                "🚨 Saldo negativo",
                "negative",
                0.0,
            )
        elif abs(balance) < 1e-6 and monthly_expense <= 1e-6:
            balance_status, balance_tone, balance_progress = (
                "📭 Sem saldo registado",
                "neutral",
                0.0,
            )
        elif monthly_expense > 0:
            coverage = balance / monthly_expense
            balance_status = f"🛟 {max(0.0, coverage):.1f} meses de despesas"
            balance_tone = "positive" if coverage >= 1 else "warning"
            balance_progress = min(1.0, max(0.0, coverage / 3.0))
        elif balance > 0:
            balance_status, balance_tone, balance_progress = (
                "✅ Saldo positivo",
                "positive",
                1.0,
            )
        else:
            balance_status, balance_tone, balance_progress = (
                "📭 Sem saldo registado",
                "neutral",
                0.0,
            )

        if totals["income"] == 0 and totals["expenses"] == 0:
            savings_status, savings_tone = "📭 Sem movimentos", "neutral"
        elif savings_rate < 0:
            savings_status, savings_tone = "🚨 Período em défice", "negative"
        elif savings_rate < 10:
            savings_status, savings_tone = "⚠️ Poupança baixa", "warning"
        elif savings_rate < 20:
            savings_status, savings_tone = "🟦 Ritmo moderado", "neutral"
        else:
            savings_status, savings_tone = "🏆 Bom ritmo", "positive"

        if monthly_expense <= 0:
            burn_status, burn_tone, burn_progress = (
                "Sem média histórica",
                "neutral",
                None,
            )
        else:
            burn_ratio = forecast_end_month / monthly_expense
            burn_progress = min(1.0, max(0.0, burn_ratio))
            if burn_ratio <= 1:
                burn_status, burn_tone = "✅ Dentro da média", "positive"
            elif burn_ratio <= 1.15:
                burn_status, burn_tone = "⚠️ Acima da média", "warning"
            else:
                burn_status, burn_tone = "🔥 Ritmo elevado", "negative"

        fire_progress = (
            min(
                1.0,
                max(
                    0.0,
                    safe_float(financial_independence.get("progress_pct")) / 100.0,
                ),
            )
            if financial_independence.get("available")
            else None
        )
        fire_status = (
            f"🧭 {fire_progress * 100:.0f}% do objetivo de independência financeira"
            if fire_progress is not None
            else "Dados insuficientes para a independência financeira"
        )
        net_tone = "positive" if net >= 0 else "negative"

        income_previous = totals_prev.get("income")
        income_progress = (
            min(1.0, max(0.0, totals["income"] / safe_float(income_previous)))
            if income_previous and safe_float(income_previous) > 0
            else None
        )
        spending_progress = (
            min(1.0, max(0.0, totals["expenses"] / totals["income"]))
            if totals["income"] > 0
            else (1.0 if totals["expenses"] > 0 else 0.0)
        )

        metric_trends = data.get("metric_trends", {}) or {}
        income_trend = [safe_float(value) for value in metric_trends.get("income", [])]
        expense_trend = [
            safe_float(value) for value in metric_trends.get("expenses", [])
        ]
        savings_trend = [
            safe_float(value) for value in metric_trends.get("savings", [])
        ]
        historical_forecast = [
            point
            for point in data.get("forecast", [])
            if not bool(point.get("is_forecast"))
        ]
        balance_trend = self._sample_values(
            [safe_float(point.get("balance")) for point in historical_forecast]
        )
        portfolio_trend = self._sample_values(
            [
                safe_float(row.get("total"))
                for row in (data.get("investment_evolution", []) or [])
            ]
        )

        reserve = data.get("emergency_reserve", {}) or {}
        reserve_current = safe_float(reserve.get("current"))
        reserve_target = safe_float(reserve.get("target"))
        reserve_missing = safe_float(reserve.get("missing"))
        reserve_above = safe_float(reserve.get("above"))
        reserve_coverage = safe_float(reserve.get("coverage_months"))
        reserve_status_key = str(reserve.get("status_key") or "unavailable")
        reserve_statuses = {
            "above": f"🏆 {eur(reserve_above, self.app.theme.privacy)} acima do objetivo",
            "reached": "✅ Objetivo atingido",
            "base": (
                "🛟 Base de 3 meses atingida · faltam "
                f"{eur(reserve_missing, self.app.theme.privacy)}"
            ),
            "building": f"⚠️ Faltam {eur(reserve_missing, self.app.theme.privacy)}",
            "critical": f"🚨 Faltam {eur(reserve_missing, self.app.theme.privacy)}",
            "unavailable": "Sem despesas suficientes para definir a referência",
        }
        if reserve.get("source") == "goal":
            reserve_delta = (
                f"Meta: {eur(reserve_target, self.app.theme.privacy)}"
                f" · {reserve_coverage:.1f} meses cobertos"
            )
        else:
            reserve_delta = (
                f"Meta: {eur(reserve_target, self.app.theme.privacy)}"
                f" · {safe_float(reserve.get('target_months'), 6):.0f} meses de despesas"
            )
        reserve_value = (
            eur(reserve_current, self.app.theme.privacy)
            if reserve.get("available")
            else "Sem referência"
        )

        work_value = data.get("work_value", {}) or {}
        income_context = data.get("income_context", {}) or {}
        work_available = bool(work_value.get("available"))
        weekly_hours = safe_float(
            work_value.get("weekly_hours"),
            DEFAULT_WEEKLY_WORK_HOURS,
        )
        if work_available:
            hourly_value = (
                f"{eur(work_value.get('hourly_value'), self.app.theme.privacy)}/hora"
            )
            hourly_delta = (
                f"{eur(work_value.get('daily_value'), self.app.theme.privacy)}/dia útil"
                f" · {income_context.get('reference_name', 'salário')} líquido "
                f"{eur(work_value.get('monthly_income'), self.app.theme.privacy)}"
            )
            hourly_status = (
                f"🕒 Jornada de {weekly_hours:.1f} h/semana · clica para ajustar"
            )
        else:
            hourly_value = "Sem salário registado"
            hourly_delta = "Regista uma receita de salário para calcular este valor"
            hourly_status = "⚠️ Referência de tempo indisponível"

        cards = [
            (
                (
                    "Saldo atual",
                    eur(balance, self.app.theme.privacy),
                    "Disponível em contas",
                    GREEN,
                    "💰",
                ),
                {
                    "status": balance_status,
                    "tone": balance_tone,
                    "progress": balance_progress,
                    "trend": balance_trend,
                    "trend_label": "Evolução do saldo no período selecionado",
                    "clickable": True,
                    "action_hint": "Abrir todos os movimentos deste período",
                },
            ),
            (
                (
                    "Receitas",
                    eur(totals["income"], self.app.theme.privacy),
                    period_label + inc_delta,
                    BLUE,
                    "📈",
                ),
                {
                    "status": inc_status,
                    "tone": inc_tone,
                    "progress": income_progress,
                    "trend": income_trend,
                    "trend_label": f"Tendência das receitas em {period_label}",
                    "clickable": True,
                    "action_hint": "Abrir apenas as receitas deste período",
                },
            ),
            (
                (
                    "Despesas",
                    eur(totals["expenses"], self.app.theme.privacy),
                    period_label + exp_delta,
                    PINK,
                    "📉",
                ),
                {
                    "status": exp_status,
                    "tone": exp_tone,
                    "progress": spending_progress,
                    "trend": expense_trend,
                    "trend_label": f"Tendência das despesas em {period_label}",
                    "clickable": True,
                    "action_hint": "Abrir apenas as despesas deste período",
                },
            ),
            (
                (
                    "Poupança",
                    eur(totals["savings"], self.app.theme.privacy),
                    f"Taxa {percent(savings_rate, self.app.theme.privacy)}" + ("" if self.app.theme.privacy else sav_delta),
                    GREEN,
                    "🏦",
                ),
                {
                    "status": savings_status,
                    "tone": savings_tone,
                    "progress": min(1.0, max(0.0, savings_rate / 30.0)),
                    "trend": savings_trend,
                    "trend_label": f"Tendência da poupança em {period_label}",
                    "clickable": True,
                    "action_hint": "Abrir os movimentos que formam esta poupança",
                },
            ),
            (
                (
                    "Reserva de emergência",
                    reserve_value,
                    reserve_delta,
                    GREEN,
                    "🛟",
                ),
                {
                    "status": reserve_statuses.get(
                        reserve_status_key,
                        reserve_statuses["unavailable"],
                    ),
                    "tone": str(reserve.get("tone") or "neutral"),
                    "progress": reserve.get("progress")
                    if reserve.get("available")
                    else None,
                    "trend": balance_trend,
                    "trend_label": (
                        f"Saldo líquido em {period_label}, usado como referência"
                    ),
                    "clickable": True,
                    "action_hint": "Abrir os objetivos para configurar a reserva",
                },
            ),
            (
                (
                    "Valor do teu tempo",
                    hourly_value,
                    hourly_delta,
                    BLUE,
                    "⏱️",
                ),
                {
                    "status": hourly_status,
                    "tone": "neutral" if work_available else "warning",
                    "progress": None,
                    "trend": income_trend,
                    "trend_label": f"Receitas registadas em {period_label}",
                    "clickable": True,
                    "action_hint": (
                        "Ajustar as horas de trabalho por semana"
                        if work_available
                        else "Abrir as receitas para registar o salário"
                    ),
                },
            ),
            (
                (
                    "Projeção do mês",
                    eur(forecast_end_month, self.app.theme.privacy),
                    f"{eur(burn_rate, self.app.theme.privacy)} por dia",
                    ORANGE,
                    "🔥",
                ),
                {
                    "status": burn_status,
                    "tone": burn_tone,
                    "progress": burn_progress,
                    "trend": expense_trend,
                    "trend_label": f"Despesas registadas em {period_label}",
                    "clickable": True,
                    "action_hint": "Abrir as despesas do mês atual",
                },
            ),
            (
                (
                    "Património",
                    eur(net, self.app.theme.privacy),
                    "Líquido com investimentos",
                    PURPLE,
                    "💎",
                ),
                {
                    "status": fire_status,
                    "tone": net_tone,
                    "progress": fire_progress,
                    "trend": portfolio_trend,
                    "trend_label": (f"Evolução da carteira dentro de {period_label}"),
                    "clickable": True,
                    "action_hint": "Abrir a área de investimentos",
                },
            ),
        ]
        self.metric_widgets = [
            MetricCard(
                *args,
                **kwargs,
                variant="primary" if index < 4 else "context",
                accessible_text=f"{args[0]}: {args[1]}. {kwargs['status']}",
            )
            for index, (args, kwargs) in enumerate(cards)
        ]
        self._apply_metric_context_badges(
            income_context_months=int(income_context.get("context_months", 0) or 0)
        )
        card_actions = [
            lambda: self._open_movements_filter("Todos"),
            lambda: self._open_movements_filter("Receitas"),
            lambda: self._open_movements_filter("Despesas"),
            lambda: self._open_movements_filter("Todos"),
            lambda: self.app.goto("objetivos"),
            self._edit_work_hours
            if work_available
            else lambda: self._open_movements_filter("Receitas"),
            self._open_current_month_expenses,
            lambda: self.app.goto("carteira"),
        ]
        for card, action in zip(self.metric_widgets, card_actions):
            card.clicked.connect(action)

        if savings_rate > 30.0:
            if not hasattr(self, "_savings_toasted"):
                self.app.show_dashboard_notice(
                    "Excelente ritmo de poupança",
                    f"Parabéns! A tua taxa chegou aos {savings_rate:.0f}% em {period_label}.",
                    "🏆",
                    tone="success",
                    duration_ms=22500,
                )
                self._savings_toasted = True

        if self.should_compute_card(self.chart_card):
            self.chart.set_points(data["forecast"])
            chart_warning_count = int(self.chart.invalid_points) + len(
                self.chart.quality_warnings
            )
            if chart_warning_count:
                self.chart_card.set_confidence_badge(
                    f"Rever dados · {chart_warning_count} aviso(s)",
                    "warning",
                )
            self._set_data_coverage_badge(
                self.chart_data_badge,
                self._coverage_badge_text(self._forecast_data_context),
                self._coverage_tone(self._forecast_data_context),
            )
        if self.should_compute_card(self.cashflow_pulse_card):
            forecast_coverage_text = self.data_coverage_text(
                self._forecast_data_context
            )
            self.cashflow_pulse_card.set_data_coverage(
                forecast_coverage_text,
                self._coverage_tone(self._forecast_data_context),
            )
            cashflow_rows = data.get("cashflow_pulse", []) or []
            cashflow_previous_rows = data.get("cashflow_pulse_previous", []) or []
            self.cashflow_pulse.set_rows(cashflow_rows)
            self.cashflow_pulse.set_comparison_rows(
                cashflow_previous_rows if comparison_active else [],
                comparison_label,
            )
            if hasattr(self.cashflow_pulse_card, "set_card_title"):
                self.cashflow_pulse_card.set_card_title(
                    f"📈 Entradas vs. saídas acumuladas · {period_label}"
                )
            elif (
                self.cashflow_pulse_card.title_label is not None
                and self._qt_object_is_valid(self.cashflow_pulse_card.title_label)
            ):
                try:
                    self.cashflow_pulse_card.title_label.setText(
                        f"📈 Entradas vs. saídas acumuladas · {period_label}"
                    )
                except RuntimeError:
                    pass
            resolution_label = {
                "daily": "diários",
                "weekly": "semanais",
                "monthly": "mensais",
            }.get(self.resolution, "temporais")
            comparison_detail = (
                f" Linhas tracejadas: {comparison_label}."
                if comparison_active and cashflow_previous_rows
                else ""
            )
            self.cashflow_pulse_hint.setText(
                f"Filtro aplicado: {period_label} · {len(cashflow_rows)} pontos "
                f"{resolution_label}. A distância entre as linhas é a margem criada."
                f"{comparison_detail}"
            )
            self.cashflow_pulse_card.refresh_compact_heights()
        if self.should_compute_card(self.savings_efficiency_card):
            self.savings_efficiency_card.set_data_coverage(
                self.data_coverage_text(self._forecast_data_context),
                self._coverage_tone(self._forecast_data_context),
            )
            savings_rows = data.get("savings_efficiency", []) or []
            savings_previous_rows = data.get("savings_efficiency_previous", []) or []
            self.savings_efficiency.set_rows(savings_rows)
            self.savings_efficiency.set_comparison_rows(
                savings_previous_rows if comparison_active else [],
                comparison_label,
            )
            if hasattr(self.savings_efficiency_card, "set_card_title"):
                self.savings_efficiency_card.set_card_title(
                    f"💚 Margem e taxa de poupança · {period_label}"
                )
            elif (
                self.savings_efficiency_card.title_label is not None
                and self._qt_object_is_valid(self.savings_efficiency_card.title_label)
            ):
                try:
                    self.savings_efficiency_card.title_label.setText(
                        f"💚 Margem e taxa de poupança · {period_label}"
                    )
                except RuntimeError:
                    pass
            partial_count = sum(bool(row.get("partial")) for row in savings_rows)
            partial_detail = (
                f" · {partial_count} parcial{'is' if partial_count != 1 else ''}"
                if partial_count
                else ""
            )
            self.savings_efficiency_hint.setText(
                f"Filtro aplicado: {period_label} · {len(savings_rows)} blocos "
                f"mensais{partial_detail}. Barras: margem · Linha: taxa · Meta: 20%."
                + (
                    f" Tracejado: {comparison_label}."
                    if comparison_active and savings_previous_rows
                    else ""
                )
            )
            self.savings_efficiency_card.refresh_compact_heights()
        comparison_text = self._category_comparison_text(
            current_categories,
            previous_categories,
            comparison_label,
            comparison_active,
        )
        comparison_hints = (
            (
                self.waterfall_comparison_hint,
                "Contorno tracejado: valores de referência. ",
            ),
            (
                self.donut_comparison_hint,
                "Anel interior tracejado: distribuição de referência. ",
            ),
            (self.expense_work_time_comparison_hint, ""),
            (
                self.radar_comparison_hint,
                "Polígono tracejado: perfil de referência. ",
            ),
            (self.treemap_comparison_hint, ""),
            (self.bubble_comparison_hint, ""),
            (self.sankey_comparison_hint, ""),
        )
        visual_comparison = bool(
            comparison_active and previous_categories and comparison_text
        )
        for comparison_hint, prefix in comparison_hints:
            comparison_hint.setText(
                (prefix if visual_comparison else "") + comparison_text
            )
            comparison_hint.setVisible(bool(comparison_text))
        if self.should_compute_card(self.donut_card):
            self.donut.set_data(current_categories)
            self.donut.set_comparison_data(
                previous_categories if comparison_active else [],
                comparison_label,
            )
        if self.should_compute_card(self.expense_work_time_card):
            self.expense_work_time_card.set_data(
                data.get("expense_work_time", {}) or {},
                period_label=period_label,
                income_reference=str(income_context.get("reference_name") or "salário"),
                context_months=int(income_context.get("context_months", 0) or 0),
                weekly_hours=weekly_hours,
            )
        if self.should_compute_card(self.waterfall_card):
            self.waterfall.set_data(
                totals["income"],
                totals["expenses"],
                current_categories,
                period_label,
                previous_income=totals_prev.get("income", 0)
                if comparison_active
                else 0,
                previous_expenses=totals_prev.get("expenses", 0)
                if comparison_active
                else 0,
                previous_categories=previous_categories if comparison_active else [],
                comparison_label=comparison_label if comparison_active else "",
            )
        if self.should_compute_card(self.budget_card):
            self._refresh_budget_forecast(data.get("budget_forecast", {}) or {})
        if self.should_compute_card(self.cashflow_card):
            self._refresh_cashflow_forecast(data.get("cashflow_forecast", {}) or {})

        if hasattr(self, "radar"):
            if self.should_compute_card(self.radar_card):
                self.radar.set_data(current_categories)
                self.radar.set_comparison_data(
                    previous_categories if comparison_active else [],
                    comparison_label,
                )
            if self.should_compute_card(self.treemap_card):
                self.treemap.set_data(current_categories)
            if self.should_compute_card(self.bubble_card):
                self.bubble.set_data(current_categories)
            if self.should_compute_card(self.sankey_card):
                self.sankey.set_data(current_categories)
            if self.should_compute_card(self.heatmap_card):
                self.heatmap.set_data(data.get("heatmap", []))

        self.layout_metric_cards()
        self.rebuild_layout()

        if self.should_compute_card(self.goals_card):
            self.refresh_goals(data.get("goals", []))
        if self.should_compute_card(self.insights):
            self.refresh_insights(data.get("decision_briefing", {}) or {})
        self.apply_card_preferences()
        self.retry_btn.hide()
        # Metric/insight content changes the page minimum after the worker returns.
        # Re-sync twice so the scroll container cannot compress later chart rows.
        self._sync_dashboard_row_minimums()
        self.updateGeometry()
        QTimer.singleShot(0, self.app.sync_content_size)
        QTimer.singleShot(120, self.app.sync_content_size)
        QTimer.singleShot(180, self._enforce_category_chart_separation)
        self.mark_data_updated()

    def refresh_goals(self, rows: list[Any] | None = None) -> None:
        while self.goals_card.v.count() > 1:
            item = self.goals_card.v.takeAt(1)
            if item.widget():
                item.widget().deleteLater()
        rows = list(rows)[:4] if rows is not None else self.repo.goals(limit=4)
        if not rows:
            self.goals_card.v.addWidget(QLabel("🎯 Ainda não tens objetivos."))
            return
        for r in rows:
            target = safe_float(r["target_amount"])
            saved = safe_float(r["saved_amount"])
            progress = saved / target if target else 0
            self.goals_card.v.addWidget(
                ProgressPill(
                    r["name"],
                    f"{eur(saved, self.app.theme.privacy)} de {eur(target, self.app.theme.privacy)}",
                    progress,
                    GREEN,
                    lambda: self.app.theme,
                )
            )

    def refresh_insights(
        self,
        briefing: dict[str, Any],
    ) -> None:
        self.insights.set_data(
            briefing,
            privacy=self.app.theme.privacy,
            max_items=3 if self.dashboard_density == "compact" else 4,
        )
