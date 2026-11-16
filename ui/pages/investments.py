#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Finanças Pessoais – Liquid Glass
Nova interface PySide6 para a app local de finanças pessoais.
Mantém a base de dados e a app clássica intactas; esta é apenas uma camada visual moderna.
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any, Optional


from utils.qt_runtime import configure_qt_runtime

configure_qt_runtime()

try:
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtGui import QColor
    from PySide6.QtWidgets import (
        QAbstractItemView,
        QComboBox,
        QDialog,
        QDoubleSpinBox,
        QGridLayout,
        QHBoxLayout,
        QHeaderView,
        QLabel,
        QMessageBox,
        QProgressBar,
        QPushButton,
        QSizePolicy,
        QSlider,
        QTableWidget,
    )
except (
    Exception
) as exc:  # pragma: no cover - only used when PySide6 is missing on the user's PC.
    print("PySide6 não está instalado. Instala com: pip install PySide6")
    print(exc)
    sys.exit(1)

from ui.workers import AsyncTask
from ui.components import (
    DonutChart,
    GlassCard,
    InvestmentEvolutionChart,
    InvestmentProjectionChart,
    MetricCard,
    MonteCarloChart,
    NetWorthBridgeChart,
    NetWorthChart,
    PortfolioContributionChart,
)
from ui.core import BasePage, EuroSpinBox, ScrollPassthroughSlider
from ui.core.smart_table import SmartTableWidgetItem as QTableWidgetItem
from ui.dialogs import (
    AccountBalanceDialog,
    HoldingDialog,
    LiabilityDialog,
    PortugueseTaxDialog,
)
from services.investment_performance import (
    portfolio_performance_attribution,
)
from services.investment_projection import build_financial_independence_outlook
from utils.helpers import (
    APP_TITLE,
    BLUE,
    GREEN,
    MINT,
    ORANGE,
    PINK,
    PURPLE,
    RED,
    TEAL,
    YELLOW,
    add_months,
    eur,
    format_period_variation,
    iso,
    percent,
    row_get,
    safe_float,
)
from utils.app_logging import log_exception

if TYPE_CHECKING:
    from main import LiquidFinanceApp


class PortfolioPage(BasePage):
    def __init__(self, app: "LiquidFinanceApp"):
        super().__init__(
            app,
            "Investimentos",
            "Investimentos registados manualmente, sem sincronização externa",
        )
        top = QHBoxLayout()
        self.add_btn = QPushButton("+ Novo investimento")
        self.add_btn.setObjectName("CardAction")
        self.edit_btn = QPushButton("Editar investimento")
        self.close_position_btn = QPushButton("Fechar posição / Vender")
        self.close_position_btn.setObjectName("Secondary")
        self.delete_btn = QPushButton("Eliminar")
        self.accounts_btn = QPushButton("🏦 Saldos das contas")
        self.accounts_btn.setObjectName("Secondary")
        self.tax_btn = QPushButton("🏛️ Fiscalidade IRS")
        self.tax_btn.setObjectName("Secondary")
        top.addStretch()
        top.addWidget(self.accounts_btn)
        top.addWidget(self.tax_btn)
        top.addWidget(self.edit_btn)
        top.addWidget(self.close_position_btn)
        top.addWidget(self.delete_btn)
        top.addWidget(self.add_btn)

        self.summary = QGridLayout()
        self.summary.setSpacing(14)
        self.investment_donut = DonutChart(lambda: self.app.theme)
        self.investment_donut.empty_message = "Sem investimentos registados na carteira"
        self.investment_donut.setMinimumHeight(260)
        self.investment_donut.configure_details(
            self._holding_detail_payload,
            action_label="Selecionar investimento",
            action_handler=self._select_holding_item,
        )
        self.table = QTableWidget(0, 10)
        self.table.setProperty("columnPersistenceId", "portfolio.holdings")
        self.table.setHorizontalHeaderLabels(
            [
                "ID",
                "Ativo",
                "Tipo",
                "Conta",
                "Valor atual",
                "Capital investido",
                "Comissões",
                "Ganho líquido",
                "Rentab. líquida",
                "Contribuição",
            ]
        )
        self.table.setColumnHidden(0, True)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeToContents
        )
        self.table.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.ResizeToContents
        )
        self.table.horizontalHeader().setSectionResizeMode(
            4, QHeaderView.ResizeToContents
        )
        self.table.horizontalHeader().setSectionResizeMode(
            5, QHeaderView.ResizeToContents
        )
        self.table.horizontalHeader().setSectionResizeMode(
            6, QHeaderView.ResizeToContents
        )
        for column in range(7, 10):
            self.table.horizontalHeader().setSectionResizeMode(
                column,
                QHeaderView.ResizeToContents,
            )
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.verticalHeader().setVisible(False)
        self.table.setMinimumHeight(250)

        chart_card = GlassCard("Distribuição dos investimentos", strong=True)
        chart_card.v.addWidget(self.investment_donut)
        self.holdings_summary = QLabel()
        self.holdings_summary.setObjectName("Subtle")
        self.holdings_summary.setWordWrap(True)
        table_card = GlassCard("Ativos · resultado líquido", strong=True)
        table_card.v.addWidget(self.holdings_summary)
        table_card.v.addWidget(self.table)

        self.portfolio_contribution_chart = PortfolioContributionChart(
            lambda: self.app.theme
        )
        self.portfolio_contribution_chart.configure_details(
            self._portfolio_contribution_detail_payload,
            action_label="Selecionar investimento",
            action_handler=self._select_contribution_holding,
        )
        contribution_hint = QLabel(
            "Cada barra mostra quantos pontos percentuais esse ativo acrescenta "
            "ou retira à rentabilidade líquida total. As comissões são deduzidas."
        )
        contribution_hint.setObjectName("Subtle")
        contribution_hint.setWordWrap(True)
        self.portfolio_contribution_card = GlassCard(
            "🧭 Rentabilidade e contribuição por ativo",
            strong=True,
        )
        self.portfolio_contribution_card.setProperty(
            "pageCardKey",
            "portfolio_contribution",
        )
        self.portfolio_contribution_card.v.addWidget(contribution_hint)
        self.portfolio_contribution_card.v.addWidget(self.portfolio_contribution_chart)

        self.net_worth_chart = NetWorthChart(lambda: self.app.theme)
        self.net_worth_chart.configure_details(
            self._net_worth_detail_payload,
        )
        self.net_worth_status = QLabel()
        self.net_worth_status.setWordWrap(True)
        self.net_worth_status.setTextFormat(Qt.RichText)
        self.liability_table = QTableWidget(0, 7)
        self.liability_table.setProperty("columnPersistenceId", "portfolio.liabilities")
        self.liability_table.setHorizontalHeaderLabels(
            [
                "ID",
                "Dívida",
                "Tipo",
                "Entidade",
                "Capital",
                "Juro",
                "Prestação",
            ]
        )
        self.liability_table.setColumnHidden(0, True)
        self.liability_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.Stretch
        )
        for column in range(2, 7):
            self.liability_table.horizontalHeader().setSectionResizeMode(
                column, QHeaderView.ResizeToContents
            )
        self.liability_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.liability_table.setAlternatingRowColors(True)
        self.liability_table.verticalHeader().setVisible(False)
        self.liability_table.setMinimumHeight(190)
        liability_actions = QHBoxLayout()
        self.add_liability_btn = QPushButton("+ Nova dívida")
        self.add_liability_btn.setObjectName("CardAction")
        self.edit_liability_btn = QPushButton("Editar dívida")
        self.delete_liability_btn = QPushButton("Eliminar dívida")
        liability_actions.addStretch(1)
        liability_actions.addWidget(self.edit_liability_btn)
        liability_actions.addWidget(self.delete_liability_btn)
        liability_actions.addWidget(self.add_liability_btn)
        self.net_worth_card = GlassCard(
            "💎 Património completo · ativos e passivos", strong=True
        )
        self.net_worth_card.v.addWidget(self.net_worth_status)
        self.net_worth_card.v.addWidget(self.net_worth_chart)
        self.net_worth_card.v.addLayout(liability_actions)
        self.net_worth_card.v.addWidget(self.liability_table)

        self.bridge_period = QComboBox()
        for label, months in (
            ("3 meses", 3),
            ("6 meses", 6),
            ("12 meses", 12),
            ("24 meses", 24),
        ):
            self.bridge_period.addItem(label, months)
        self.bridge_period.setCurrentIndex(2)
        self.bridge_period.setAccessibleName("Período da ponte do património")
        self.bridge_period.setMaximumWidth(180)
        self.bridge_period.setFixedHeight(36)
        bridge_controls = QHBoxLayout()
        bridge_controls.setContentsMargins(0, 0, 0, 0)
        bridge_controls.addWidget(QLabel("Período"))
        bridge_controls.addWidget(self.bridge_period)
        bridge_controls.addStretch(1)
        self.net_worth_bridge_summary = QLabel()
        self.net_worth_bridge_summary.setObjectName("Subtle")
        self.net_worth_bridge_summary.setWordWrap(True)
        self.net_worth_bridge_chart = NetWorthBridgeChart(lambda: self.app.theme)
        self.net_worth_bridge_chart.configure_details(
            self._net_worth_bridge_detail_payload,
        )
        self.net_worth_bridge_card = GlassCard(
            "🌉 Ponte do património · o que mudou", strong=True
        )
        if self.net_worth_bridge_card.title_label is not None:
            self.net_worth_bridge_card.title_label.setMinimumWidth(0)
            self.net_worth_bridge_card.title_label.setWordWrap(True)
            self.net_worth_bridge_card.title_label.setSizePolicy(
                QSizePolicy.Ignored,
                QSizePolicy.Preferred,
            )
        self.net_worth_bridge_card.v.addLayout(bridge_controls)
        self.net_worth_bridge_card.v.addWidget(self.net_worth_bridge_summary)
        self.net_worth_bridge_card.v.addWidget(self.net_worth_bridge_chart)

        self.investment_evolution = InvestmentEvolutionChart(lambda: self.app.theme)
        self.investment_evolution.configure_details(
            lambda _item: {
                "title": "Evolução da carteira",
                "explanation": (
                    "Compara o capital aplicado com o valor total registado na "
                    "data selecionada. A diferença corresponde ao ganho ou perda."
                ),
            }
        )
        self.investment_evolution.setMinimumHeight(360)
        evolution_card = GlassCard(
            "Evolução · investido face ao valor total", strong=True
        )
        evolution_hint = QLabel(
            "Capital aplicado e valor atual da carteira ao longo do tempo."
        )
        evolution_hint.setObjectName("Subtle")
        evolution_card.v.addWidget(evolution_hint)
        evolution_card.v.addWidget(self.investment_evolution)

        self._projection_average_income = 0.0
        self._projection_average_savings = 0.0
        self._projection_context_months = 0
        self._projection_mode_cache = "monthly"
        self._projection_monthly_amount = 250
        self._projection_single_amount = 5_000
        self._monte_carlo_history_points = 0
        self._monte_carlo_asset_weights: list[float] = []
        self.projection_chart = InvestmentProjectionChart(lambda: self.app.theme)
        self.projection_chart.configure_details(
            lambda _item: {
                "title": "Simulação de investimento",
                "explanation": (
                    "Mostra o capital aplicado e os três cenários líquidos de "
                    "inflação, impostos e comissões no horizonte selecionado."
                ),
            }
        )

        self.projection_mode = QComboBox()
        self.projection_mode.addItem("Mensal", "monthly")
        self.projection_mode.addItem("Único", "single")
        self.projection_mode.setAccessibleName("Modo do investimento")
        self.projection_initial = EuroSpinBox()
        self.projection_initial.setRange(0.0, 10_000_000.0)
        self.projection_initial.setDecimals(2)
        self.projection_initial.setSingleStep(500.0)
        self.projection_initial.setSuffix(" €")
        self.projection_initial.setAccessibleName("Capital inicial")

        self.projection_slider = ScrollPassthroughSlider(Qt.Horizontal)
        self.projection_slider.setRange(0, 5_000)
        self.projection_slider.setSingleStep(25)
        self.projection_slider.setPageStep(250)
        self.projection_slider.setTickInterval(500)
        self.projection_slider.setTickPosition(QSlider.TicksBelow)
        self.projection_slider.setValue(250)
        self.projection_slider.setAccessibleName("Reforço mensal a investir")
        self.projection_slider.setAccessibleDescription(
            "Escolhe entre zero e cinco mil euros por mês."
        )

        self.projection_amount_label = QLabel()
        self.projection_amount_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.projection_amount_label.setAccessibleName("Valor do investimento")
        self.projection_rate = QDoubleSpinBox()
        self.projection_rate.setRange(0.0, 25.0)
        self.projection_rate.setDecimals(1)
        self.projection_rate.setSingleStep(0.5)
        self.projection_rate.setSuffix(" % / ano")
        self.projection_rate.setValue(5.0)
        self.projection_rate.setKeyboardTracking(False)
        self.projection_rate.setAccessibleName("Rentabilidade anual estimada")

        self.projection_inflation = QDoubleSpinBox()
        self.projection_inflation.setRange(-5.0, 20.0)
        self.projection_inflation.setDecimals(1)
        self.projection_inflation.setSingleStep(0.5)
        self.projection_inflation.setSuffix(" % / ano")
        self.projection_inflation.setValue(2.0)
        self.projection_inflation.setKeyboardTracking(False)
        self.projection_inflation.setAccessibleName("Inflação anual estimada")
        self.projection_tax = QDoubleSpinBox()
        self.projection_tax.setRange(0.0, 100.0)
        self.projection_tax.setDecimals(1)
        self.projection_tax.setSingleStep(1.0)
        self.projection_tax.setSuffix(" %")
        self.projection_tax.setValue(28.0)
        self.projection_tax.setKeyboardTracking(False)
        self.projection_tax.setAccessibleName("Imposto sobre ganhos")
        self.projection_fee = QDoubleSpinBox()
        self.projection_fee.setRange(0.0, 20.0)
        self.projection_fee.setDecimals(2)
        self.projection_fee.setSingleStep(0.1)
        self.projection_fee.setSuffix(" %")
        self.projection_fee.setValue(0.0)
        self.projection_fee.setKeyboardTracking(False)
        self.projection_fee.setAccessibleName("Comissão sobre cada novo reforço")
        self.projection_reset_btn = QPushButton("↺ Repor simulação")
        self.projection_reset_btn.setObjectName("CardAction")
        self.projection_reset_btn.setToolTip("Repõe todos os parâmetros recomendados")

        projection_controls = QGridLayout()
        projection_controls.setContentsMargins(0, 0, 0, 0)
        projection_controls.setHorizontalSpacing(12)
        projection_controls.setVerticalSpacing(7)
        projection_controls.addWidget(QLabel("Modo"), 0, 0)
        projection_controls.addWidget(self.projection_mode, 0, 1)
        projection_controls.addWidget(QLabel("Capital inicial"), 0, 2)
        projection_controls.addWidget(self.projection_initial, 0, 3)
        self.projection_amount_title = QLabel("Reforço mensal pretendido")
        projection_controls.addWidget(self.projection_amount_title, 1, 0)
        projection_controls.addWidget(self.projection_amount_label, 1, 1)
        projection_controls.addWidget(QLabel("Rentabilidade base"), 1, 2)
        projection_controls.addWidget(self.projection_rate, 1, 3)
        projection_controls.addWidget(self.projection_slider, 2, 0, 1, 4)
        projection_controls.addWidget(QLabel("Inflação"), 3, 0)
        projection_controls.addWidget(self.projection_inflation, 3, 1)
        projection_controls.addWidget(QLabel("Imposto sobre ganhos"), 3, 2)
        projection_controls.addWidget(self.projection_tax, 3, 3)
        projection_controls.addWidget(QLabel("Comissão por reforço"), 4, 0)
        projection_controls.addWidget(self.projection_fee, 4, 1)
        projection_controls.addWidget(self.projection_reset_btn, 4, 3)
        projection_controls.setColumnStretch(0, 1)
        projection_controls.setColumnStretch(1, 1)
        projection_controls.setColumnStretch(2, 1)

        self.projection_effort_label = QLabel()
        self.projection_effort_label.setWordWrap(True)
        self.projection_effort_label.setAccessibleName(
            "Esforço financeiro do reforço mensal"
        )
        self.projection_effort = QProgressBar()
        self.projection_effort.setRange(0, 100)
        self.projection_effort.setTextVisible(True)
        self.projection_effort.setMinimumHeight(23)
        self.projection_effort.setAccessibleName(
            "Percentagem do rendimento mensal usada no investimento"
        )
        projection_controls.addWidget(self.projection_effort_label, 5, 0, 1, 4)
        projection_controls.addWidget(self.projection_effort, 6, 0, 1, 4)

        self.projection_outcome = QLabel()
        self.projection_outcome.setWordWrap(True)
        self.projection_outcome.setAccessibleName("Resultado projetado a cinco anos")
        projection_controls.addWidget(self.projection_outcome, 7, 0, 1, 4)

        self.projection_card = GlassCard(
            "🎚️ Simulador de investimento · 1 a 5 anos", strong=True
        )
        self.projection_card.set_confidence_badge(
            "Confiança inicial · a calcular histórico",
            "warning",
        )
        projection_hint = QLabel(
            "Compara cenários pessimista, base e otimista (±2 p.p.). "
            "Separa o capital nominal dos euros de hoje; cada reforço é "
            "corrigido apenas pelo tempo em que esteve investido."
        )
        projection_hint.setObjectName("Subtle")
        projection_hint.setWordWrap(True)
        self.projection_card.v.addWidget(projection_hint)
        self.projection_card.v.addLayout(projection_controls)
        self.projection_card.v.addWidget(self.projection_chart)
        self.register_filter_panel(self.projection_card)

        self.monte_carlo_chart = MonteCarloChart(lambda: self.app.theme)
        self.monte_carlo_chart.configure_details(
            lambda _item: {
                "title": "Simulação Monte Carlo",
                "explanation": (
                    "P10, P50 e P90 representam, respetivamente, um cenário "
                    "desfavorável, a mediana e um cenário favorável das simulações."
                ),
            }
        )
        self.monte_carlo_years = QComboBox()
        for years in (5, 10, 20, 30):
            self.monte_carlo_years.addItem(f"{years} anos", years)
        self.monte_carlo_years.setCurrentIndex(1)
        self.monte_carlo_volatility = QDoubleSpinBox()
        self.monte_carlo_volatility.setRange(0.0, 80.0)
        self.monte_carlo_volatility.setDecimals(1)
        self.monte_carlo_volatility.setValue(12.0)
        self.monte_carlo_volatility.setSuffix(" % / ano")
        self.monte_carlo_target = EuroSpinBox()
        self.monte_carlo_target.setRange(0.0, 100_000_000.0)
        self.monte_carlo_target.setDecimals(0)
        self.monte_carlo_target.setSingleStep(5_000.0)
        self.monte_carlo_target.setValue(100_000.0)
        self.monte_carlo_target.setSuffix(" €")
        self.monte_carlo_simulations = QComboBox()
        for simulations in (500, 1_000, 2_000):
            self.monte_carlo_simulations.addItem(
                f"{simulations:,}".replace(",", " "), simulations
            )
        self.monte_carlo_simulations.setCurrentIndex(1)
        self.monte_carlo_correlation = QDoubleSpinBox()
        self.monte_carlo_correlation.setRange(-20.0, 100.0)
        self.monte_carlo_correlation.setDecimals(0)
        self.monte_carlo_correlation.setSingleStep(5.0)
        self.monte_carlo_correlation.setValue(40.0)
        self.monte_carlo_correlation.setSuffix(" %")
        self.monte_carlo_correlation.setAccessibleName(
            "Correlação estimada entre os investimentos"
        )
        monte_controls = QGridLayout()
        monte_controls.setHorizontalSpacing(12)
        monte_controls.setVerticalSpacing(8)
        monte_controls.addWidget(QLabel("Horizonte"), 0, 0)
        monte_controls.addWidget(self.monte_carlo_years, 0, 1)
        monte_controls.addWidget(QLabel("Volatilidade"), 0, 2)
        monte_controls.addWidget(self.monte_carlo_volatility, 0, 3)
        monte_controls.addWidget(QLabel("Objetivo"), 1, 0)
        monte_controls.addWidget(self.monte_carlo_target, 1, 1)
        monte_controls.addWidget(QLabel("Simulações"), 1, 2)
        monte_controls.addWidget(self.monte_carlo_simulations, 1, 3)
        monte_controls.addWidget(QLabel("Correlação entre ativos"), 2, 0)
        monte_controls.addWidget(self.monte_carlo_correlation, 2, 1)
        for column in range(4):
            monte_controls.setColumnStretch(column, 1)
        self.monte_carlo_result = QLabel()
        self.monte_carlo_result.setWordWrap(True)
        self.monte_carlo_result.setAccessibleName("Resultado da simulação Monte Carlo")
        self.monte_carlo_method = QLabel()
        self.monte_carlo_method.setObjectName("Subtle")
        self.monte_carlo_method.setWordWrap(True)
        self.monte_carlo_card = GlassCard(
            "🎲 Simulação Monte Carlo · probabilidade e risco", strong=True
        )
        self.monte_carlo_card.set_confidence_badge(
            "Confiança inicial · a calcular histórico",
            "warning",
        )
        self.monte_carlo_card.v.addLayout(monte_controls)
        self._monte_carlo_pending = False
        self._monte_carlo_task = AsyncTask(
            self,
            registry=self.app.background_task_registry,
            label="Simulação Monte Carlo",
        )
        self._monte_carlo_task.result.connect(self._on_monte_carlo_result)
        self._monte_carlo_task.error.connect(self._on_monte_carlo_error)
        self._monte_carlo_task.finished.connect(self._on_monte_carlo_finished)
        self._monte_carlo_timer = QTimer(self)
        self._monte_carlo_timer.setSingleShot(True)
        self._monte_carlo_timer.setInterval(180)
        self._monte_carlo_timer.timeout.connect(self._update_monte_carlo)
        self.monte_carlo_card.v.addWidget(self.monte_carlo_result)

        self.monte_carlo_card.v.addWidget(self.monte_carlo_method)
        self.monte_carlo_card.v.addWidget(self.monte_carlo_chart)

        self.fire_card = GlassCard(
            "Caminho para a independência financeira 🔥", strong=True
        )
        self.fire_lbl = QLabel()
        self.fire_lbl.setWordWrap(True)
        self.fire_lbl.setTextFormat(Qt.RichText)
        self.fire_lbl.setStyleSheet(f"color: {self.app.theme.text}; font-size: 14px;")

        self.fire_progress = QProgressBar()
        self.fire_progress.setTextVisible(True)
        self.fire_progress.setMinimumHeight(24)

        self.fire_card.v.addWidget(self.fire_lbl)
        self.fire_card.v.addWidget(self.fire_progress)

        grid = QGridLayout()
        grid.setSpacing(16)
        grid.addWidget(self.net_worth_card, 0, 0, 1, 2)
        grid.addWidget(self.net_worth_bridge_card, 1, 0, 1, 2)
        grid.addWidget(chart_card, 2, 0)
        grid.addWidget(table_card, 2, 1)
        grid.addWidget(self.portfolio_contribution_card, 3, 0, 1, 2)
        grid.addWidget(evolution_card, 4, 0, 1, 2)
        grid.addWidget(self.projection_card, 5, 0, 1, 2)
        grid.addWidget(self.monte_carlo_card, 6, 0, 1, 2)
        grid.addWidget(self.fire_card, 7, 0, 1, 2)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 2)

        self.body.addLayout(top)
        self.body.addLayout(self.summary)
        self.body.addLayout(grid)
        self.add_btn.clicked.connect(self.add_holding)
        self.edit_btn.clicked.connect(self.edit_selected)
        self.close_position_btn.clicked.connect(self.close_selected_position)
        self.delete_btn.clicked.connect(self.delete_selected)
        self.accounts_btn.clicked.connect(self.edit_account_balance)
        self.tax_btn.clicked.connect(self._open_tax_dialog)
        self.table.itemDoubleClicked.connect(lambda *_: self.edit_selected())
        self.add_liability_btn.clicked.connect(self.add_liability)
        self.edit_liability_btn.clicked.connect(self.edit_liability)
        self.delete_liability_btn.clicked.connect(self.delete_liability)
        self.liability_table.itemDoubleClicked.connect(lambda *_: self.edit_liability())
        self.bridge_period.currentIndexChanged.connect(
            lambda _index: self._refresh_net_worth_bridge()
        )
        self.projection_slider.valueChanged.connect(
            lambda _value: self._update_projection()
        )
        self.projection_mode.currentIndexChanged.connect(
            lambda _index: self._change_projection_mode()
        )
        self.projection_initial.valueChanged.connect(
            lambda _value: self._update_projection()
        )
        self.projection_rate.valueChanged.connect(
            lambda _value: self._update_projection()
        )
        self.projection_inflation.valueChanged.connect(
            lambda _value: self._update_projection()
        )
        self.projection_tax.valueChanged.connect(
            lambda _value: self._update_projection()
        )
        self.projection_fee.valueChanged.connect(
            lambda _value: self._update_projection()
        )
        self.projection_reset_btn.clicked.connect(self._reset_projection)
        for control in (
            self.monte_carlo_years,
            self.monte_carlo_volatility,
            self.monte_carlo_target,
            self.monte_carlo_simulations,
            self.monte_carlo_correlation,
        ):
            try:
                control.currentIndexChanged.connect(
                    lambda *_: self._schedule_monte_carlo()
                )
            except AttributeError:
                control.valueChanged.connect(lambda *_: self._schedule_monte_carlo())
        self.refresh()

    def clear_summary(self):
        while self.summary.count():
            item = self.summary.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def _refresh_net_worth_bridge(self) -> None:
        if not self.should_compute_card(self.net_worth_bridge_card):
            return
        reference = date.today()
        months = int(self.bridge_period.currentData() or 12)
        start = add_months(reference, -months) + timedelta(days=1)
        data = self.analytics_service.net_worth_bridge(
            start.isoformat(),
            reference.isoformat(),
        )
        self._net_worth_bridge_data = dict(data)
        self.net_worth_bridge_chart.set_data(data)
        change = safe_float(data.get("change"))
        direction = "aumentou" if change >= 0 else "diminuiu"
        variation = format_period_variation(
            safe_float(data.get("closing_net_worth")),
            safe_float(data.get("opening_net_worth")),
            privacy=self.app.theme.privacy,
        )
        percent_text = f" ({variation['display']}"
        if variation["huge"]:
            percent_text += f"; {variation['context']}"
        percent_text += ")"
        coverage = safe_float(
            data.get("explained_change_pct", data.get("coverage_pct"))
        )
        components = list(data.get("coverage_components") or [])
        missing_components = [
            str(component.get("label") or "")
            for component in components
            if bool(component.get("required")) and not bool(component.get("complete"))
        ]
        history_label = (
            "histórico completo"
            if bool(data.get("history_complete"))
            else (
                "histórico parcial"
                + (f" ({', '.join(missing_components)})" if missing_components else "")
            )
        )
        coverage_label = f"{coverage:.0f}% da variação explicada · {history_label}"
        self.net_worth_bridge_summary.setText(
            f"Entre {start.strftime('%d/%m/%Y')} e "
            f"{reference.strftime('%d/%m/%Y')}, o património {direction} "
            f"<b>{eur(abs(change), self.app.theme.privacy)}</b>{percent_text} · "
            f"{coverage_label}. {data.get('method_label', '')}"
        )

    def _net_worth_bridge_detail_payload(self, item: dict) -> dict:
        value = safe_float(item.get("value"))
        role = str(item.get("role") or "")
        metrics = [
            {
                "label": "Valor" if role == "total" else "Impacto",
                "value": eur(value, self.app.theme.privacy),
                "tone": ("positive" if value >= 0 else "negative"),
            }
        ]
        if role != "total":
            metrics.extend(
                [
                    {
                        "label": "Antes",
                        "value": eur(
                            item.get("before", item.get("start", 0)),
                            self.app.theme.privacy,
                        ),
                        "tone": "neutral",
                    },
                    {
                        "label": "Depois",
                        "value": eur(
                            item.get("after", item.get("end", 0)),
                            self.app.theme.privacy,
                        ),
                        "tone": "neutral",
                    },
                ]
            )
        data = getattr(self, "_net_worth_bridge_data", {})
        metrics.append(
            {
                "label": "Variação explicada",
                "value": percent(
                    data.get(
                        "explained_change_pct",
                        data.get("coverage_pct", 0),
                    )
                ),
                "tone": "positive" if data.get("complete") else "warning",
            }
        )
        metrics.append(
            {
                "label": "Histórico dos estados",
                "value": ("Completo" if data.get("history_complete") else "Parcial"),
                "tone": ("positive" if data.get("history_complete") else "warning"),
            }
        )
        return {
            "icon": "🌉",
            "title": "Ponte do património",
            "subtitle": str(item.get("label") or "Etapa"),
            "explanation": str(item.get("explanation") or ""),
            "metrics": metrics,
        }

    def _open_tax_dialog(self) -> None:
        dialog = PortugueseTaxDialog(self.repo, self)
        try:
            dialog.exec()
        finally:
            dialog.deleteLater()

    @staticmethod
    def _rgba(color_value: str, alpha: int) -> str:
        color = QColor(str(color_value or BLUE))
        if not color.isValid():
            color = QColor(BLUE)
        return (
            f"rgba({color.red()},{color.green()},{color.blue()},"
            f"{max(0, min(255, int(alpha)))})"
        )

    def _refresh_projection_context(self) -> None:
        history = list(self.analytics_service.monthly_summary(4))
        completed = history[:-1] if len(history) > 1 else history
        usable = [row for row in completed[-3:] if safe_float(row.get("income")) > 0]
        if not usable:
            usable = [row for row in history if safe_float(row.get("income")) > 0][-3:]
        self._projection_context_months = len(usable)
        if not usable:
            self._projection_average_income = 0.0
            self._projection_average_savings = 0.0
            return
        self._projection_average_income = sum(
            safe_float(row.get("income")) for row in usable
        ) / len(usable)
        self._projection_average_savings = sum(
            safe_float(row.get("savings")) for row in usable
        ) / len(usable)

    def _projection_mode(self) -> str:
        return str(self.projection_mode.currentData() or "monthly")

    def _change_projection_mode(self) -> None:
        previous = self._projection_mode_cache
        if previous == "monthly":
            self._projection_monthly_amount = self.projection_slider.value()
        else:
            self._projection_single_amount = self.projection_slider.value()
        mode = self._projection_mode()
        self._projection_mode_cache = mode
        blocked = self.projection_slider.blockSignals(True)
        try:
            if mode == "single":
                self.projection_slider.setRange(0, 100_000)
                self.projection_slider.setSingleStep(250)
                self.projection_slider.setPageStep(2_500)
                self.projection_slider.setTickInterval(10_000)
                self.projection_slider.setValue(self._projection_single_amount)
                self.projection_amount_title.setText("Investimento único pretendido")
                self.projection_slider.setAccessibleDescription(
                    "Escolhe um investimento único entre zero e cem mil euros."
                )
            else:
                self.projection_slider.setRange(0, 5_000)
                self.projection_slider.setSingleStep(25)
                self.projection_slider.setPageStep(250)
                self.projection_slider.setTickInterval(500)
                self.projection_slider.setValue(self._projection_monthly_amount)
                self.projection_amount_title.setText("Reforço mensal pretendido")
                self.projection_slider.setAccessibleDescription(
                    "Escolhe entre zero e cinco mil euros por mês."
                )
        finally:
            self.projection_slider.blockSignals(blocked)
        self._update_projection()

    def _reset_projection(self) -> None:
        widgets = (
            self.projection_mode,
            self.projection_slider,
            self.projection_initial,
            self.projection_rate,
            self.projection_inflation,
            self.projection_tax,
            self.projection_fee,
        )
        previous = [widget.blockSignals(True) for widget in widgets]
        try:
            self._projection_mode_cache = "monthly"
            self._projection_monthly_amount = 250
            self._projection_single_amount = 5_000
            self.projection_mode.setCurrentIndex(0)
            self.projection_slider.setRange(0, 5_000)
            self.projection_slider.setSingleStep(25)
            self.projection_slider.setPageStep(250)
            self.projection_slider.setTickInterval(500)
            self.projection_slider.setValue(250)
            self.projection_initial.setValue(0.0)
            self.projection_rate.setValue(5.0)
            self.projection_inflation.setValue(2.0)
            self.projection_tax.setValue(28.0)
            self.projection_fee.setValue(0.0)
            self.projection_amount_title.setText("Reforço mensal pretendido")
        finally:
            for widget, blocked in zip(widgets, previous):
                widget.blockSignals(blocked)
        self._update_projection()

    def _apply_projection_styles(self, effort_color: str) -> None:
        theme = self.app.theme
        primary = str(theme.primary_color)
        secondary = str(theme.secondary_color)
        self.projection_slider.setStyleSheet(
            "QSlider::groove:horizontal { height: 8px; "
            f"background: {theme.scroll_handle}; border-radius: 4px; }}"
            "QSlider::sub-page:horizontal { "
            "background: qlineargradient(x1:0,y1:0,x2:1,y2:0, "
            f"stop:0 {secondary}, stop:1 {primary}); border-radius: 4px; }}"
            "QSlider::add-page:horizontal { "
            f"background: {theme.scroll_handle}; border-radius: 4px; }}"
            "QSlider::handle:horizontal { "
            f"background: {theme.input_popup}; border: 2px solid {primary}; "
            "width: 20px; height: 20px; margin: -7px 0; border-radius: 10px; }"
        )
        self.projection_amount_label.setStyleSheet(
            f"color: {primary}; font-size: 20px; font-weight: 800;"
        )
        input_style = (
            "QDoubleSpinBox, QComboBox { "
            f"background: {theme.input_popup}; color: {theme.input_text}; "
            f"border: 1.2px solid {theme.input_border}; }}"
        )
        for input_widget in (
            self.projection_mode,
            self.projection_initial,
            self.projection_rate,
            self.projection_inflation,
            self.projection_tax,
            self.projection_fee,
        ):
            input_widget.setStyleSheet(input_style)
        self.projection_effort_label.setStyleSheet(
            "font-weight: 700; padding: 8px 11px; "
            f"background: {self._rgba(effort_color, 24)}; "
            f"border: 1px solid {self._rgba(effort_color, 68)}; "
            "border-radius: 10px;"
        )
        self.projection_effort.setStyleSheet(
            "QProgressBar { "
            f"border: 1px solid {theme.border}; border-radius: 8px; "
            f"background: {theme.scroll_handle}; color: {theme.text}; "
            "text-align: center; font-weight: 700; } "
            "QProgressBar::chunk { "
            f"background: {effort_color}; border-radius: 7px; }}"
        )
        self.projection_outcome.setStyleSheet(
            "font-weight: 700; padding: 8px 11px; "
            f"background: {self._rgba(GREEN, 20)}; "
            f"border: 1px solid {self._rgba(GREEN, 58)}; "
            "border-radius: 10px;"
        )

    def _update_projection_effort(self, amount: float) -> None:
        income = self._projection_average_income
        savings = self._projection_average_savings
        mode = self._projection_mode()
        income_pct = amount / income * 100.0 if income > 0 else 0.0
        savings_pct = amount / savings * 100.0 if savings > 0 else 0.0
        savings_months = amount / savings if savings > 0 else 0.0

        if amount <= 0:
            status, effort_color = "🧭 Define um valor de investimento", BLUE
        elif income <= 0:
            status, effort_color = "⚪ Sem histórico de rendimento suficiente", BLUE
        elif savings <= 0:
            status, effort_color = "🚨 Sem poupança média positiva disponível", RED
        elif mode == "single" and savings_months > 6:
            status, effort_color = "🚨 Exige mais de seis meses de poupança", RED
        elif mode == "single" and savings_months > 3:
            status, effort_color = "⚠️ Esforço único elevado", ORANGE
        elif mode == "single" and savings_months <= 1:
            status, effort_color = "✅ Esforço único confortável", GREEN
        elif mode == "single":
            status, effort_color = "🟦 Esforço único moderado", BLUE
        elif amount > savings * 1.25:
            status, effort_color = "🚨 Esforço acima da poupança disponível", RED
        elif amount > savings or income_pct > 25:
            status, effort_color = "⚠️ Esforço financeiro elevado", ORANGE
        elif amount <= savings * 0.5 and income_pct <= 10:
            status, effort_color = "✅ Esforço financeiro confortável", GREEN
        else:
            status, effort_color = "🟦 Esforço financeiro moderado", BLUE

        details: list[str] = []
        confidence_tone = (
            "positive"
            if self._projection_context_months >= 12
            else "neutral"
            if self._projection_context_months >= 6
            else "warning"
        )
        confidence_label = (
            "elevada"
            if self._projection_context_months >= 12
            else "moderada"
            if self._projection_context_months >= 6
            else "limitada"
        )
        self.projection_card.set_confidence_badge(
            f"Confiança {confidence_label} · "
            f"{self._projection_context_months} meses completos",
            confidence_tone,
        )
        if mode == "single" and savings > 0:
            details.append(f"{savings_months:.1f} meses de poupança".replace(".", ","))
        elif income > 0:
            details.append(f"{percent(income_pct)} do rendimento médio")
        if mode == "monthly" and savings > 0:
            details.append(f"{percent(savings_pct)} da poupança média")
        headline = status + (" · " + " · ".join(details) if details else "")

        if self._projection_context_months:
            months_label = (
                "mês completo"
                if self._projection_context_months == 1
                else "meses completos"
            )
            capacity = (
                f"Base: {self._projection_context_months} {months_label} · "
                f"rendimento {eur(income, self.app.theme.privacy)} · "
                f"poupança {eur(savings, self.app.theme.privacy)} · "
                + (
                    f"equivale a {savings_months:.1f} meses de poupança".replace(
                        ".", ","
                    )
                    if mode == "single" and savings > 0
                    else f"folga após reforço {eur(savings - amount, self.app.theme.privacy)}"
                )
            )
        else:
            capacity = (
                "Regista pelo menos um mês com rendimento para calcular o esforço."
            )
        self.projection_effort_label.setText(f"{headline}\n{capacity}")
        self.projection_effort_label.setAccessibleDescription(capacity)
        if mode == "single" and savings > 0:
            self.projection_effort.setValue(
                max(0, min(100, int(round(savings_months / 12.0 * 100))))
            )
            self.projection_effort.setFormat(
                f"{savings_months:.1f} meses de poupança".replace(".", ",")
            )
        else:
            self.projection_effort.setValue(
                max(0, min(100, int(round(income_pct)))) if income > 0 else 0
            )
            self.projection_effort.setFormat(
                f"{percent(income_pct)} do rendimento médio"
                if income > 0
                else "Sem base de rendimento"
            )
        self._apply_projection_styles(effort_color)

    def _update_projection(self) -> None:
        amount = float(self.projection_slider.value())
        annual_rate = self.projection_rate.value()
        mode = self._projection_mode()
        projection = self.analytics_service.investment_projection(
            amount,
            annual_rate,
            5,
            mode=mode,
            initial_capital=self.projection_initial.value(),
            inflation_pct=self.projection_inflation.value(),
            tax_pct=self.projection_tax.value(),
            fee_pct=self.projection_fee.value(),
        )
        projection_coverage = dict(projection.get("data_coverage") or {})
        self.projection_card.set_data_coverage(
            self.data_coverage_text(projection_coverage),
            "positive"
            if int(projection_coverage.get("active_months", 0) or 0) >= 6
            else "warning",
        )
        amount_suffix = " / mês" if mode == "monthly" else " · único"
        self.projection_amount_label.setText(
            f"{eur(amount, self.app.theme.privacy)}{amount_suffix}"
        )
        self.projection_slider.setToolTip(
            ("Reforço mensal: " if mode == "monthly" else "Investimento único: ")
            + eur(amount, self.app.theme.privacy)
        )
        self.projection_chart.set_projection(projection)
        rows = list(projection.get("rows") or [])
        if rows:
            last = rows[-1]
            real_gain = safe_float(last.get("gain_real", last.get("gain")))
            real_gain_prefix = "+" if real_gain > 0 else ""
            real_return = safe_float(
                last.get("return_real_pct", last.get("return_pct"))
            )
            real_return_prefix = "+" if real_return > 0 else ""
            nominal_gain = safe_float(last.get("gain_nominal"))
            nominal_prefix = "+" if nominal_gain > 0 else ""
            self.projection_outcome.setText(
                f"🎯 Em {int(last.get('year', 5))} anos · euros de hoje: "
                f"{eur(last.get('projected_real', last.get('projected_base', 0)), self.app.theme.privacy)} · "
                f"contribuições equivalentes {eur(last.get('invested_real', 0), self.app.theme.privacy)} · "
                f"ganho real {real_gain_prefix}{eur(real_gain, self.app.theme.privacy)} "
                f"({real_return_prefix}{percent(real_return)})\n"
                f"Nominal: capital {eur(last.get('projected_nominal', 0), self.app.theme.privacy)} · "
                f"total entregue {eur(last.get('invested_nominal', last.get('invested', 0)), self.app.theme.privacy)} · "
                f"ganho {nominal_prefix}{eur(nominal_gain, self.app.theme.privacy)} "
                f"({percent(last.get('return_nominal_pct', 0))}).\n"
                f"Faixa: {eur(last.get('projected_pessimistic', 0), self.app.theme.privacy)} "
                f"a {eur(last.get('projected_optimistic', 0), self.app.theme.privacy)} · "
                f"custos acumulados {eur(last.get('fees_paid', 0), self.app.theme.privacy)} · "
                f"imposto estimado {eur(last.get('taxes_paid', 0), self.app.theme.privacy)}."
            )
        else:
            self.projection_outcome.setText("Sem projeção disponível.")
        self._update_projection_effort(amount)
        self._schedule_monte_carlo()

    def _schedule_monte_carlo(self) -> None:
        if hasattr(self, "_monte_carlo_timer"):
            self._monte_carlo_timer.start()

    def _update_monte_carlo(self) -> None:
        if not self.should_compute_card(self.monte_carlo_card):
            return
        amount = float(self.projection_slider.value())
        annual_rate = self.projection_rate.value()
        volatility = self.monte_carlo_volatility.value()
        years = int(self.monte_carlo_years.currentData() or 10)
        simulations = int(self.monte_carlo_simulations.currentData() or 1_000)
        target = self.monte_carlo_target.value()
        mode = self._projection_mode()
        initial_capital = self.projection_initial.value()
        inflation_pct = self.projection_inflation.value()
        tax_pct = self.projection_tax.value()
        fee_pct = self.projection_fee.value()
        correlation_pct = self.monte_carlo_correlation.value()
        weights = list(self._monte_carlo_asset_weights)
        points = self._monte_carlo_history_points

        if self._monte_carlo_task.is_running():
            self._monte_carlo_pending = True
            self._monte_carlo_task.cancel()
            return

        self._monte_carlo_pending = False

        # ``data_coverage_snapshot`` runs SQL, so it must never execute on the
        # worker thread: the analytics service holds the GUI-thread connection
        # (opened with ``check_same_thread=False``).  Capture the coverage on
        # this (GUI) thread and let the worker run the pure Monte Carlo math.
        from services.financial_intelligence import (
            monte_carlo_projection as build_monte_carlo_projection,
        )

        coverage = dict(
            self.analytics_service.data_coverage_snapshot(
                iso(add_months(date.today().replace(day=1), -24, 1)),
                iso(date.today()),
            )
        )

        def calc_fn():
            return build_monte_carlo_projection(
                amount,
                annual_rate,
                volatility,
                years,
                simulations=simulations,
                target=target,
                mode=mode,
                initial_capital=initial_capital,
                inflation_pct=inflation_pct,
                tax_pct=tax_pct,
                fee_pct=fee_pct,
                correlation_pct=correlation_pct,
                asset_weights=weights,
                history_points=points,
            ) | {"data_coverage": coverage}

        calc_fn.requires_sqlite_lock = False
        self._monte_carlo_task.start(calc_fn)

    def _on_monte_carlo_finished(self, _cancelled: bool) -> None:
        if getattr(self, "_monte_carlo_pending", False):
            self._monte_carlo_pending = False
            self._update_monte_carlo()

    def _on_monte_carlo_result(self, result: dict[str, Any]) -> None:
        monte_carlo_coverage = dict(result.get("data_coverage") or {})
        self.monte_carlo_card.set_data_coverage(
            self.data_coverage_text(monte_carlo_coverage),
            "positive"
            if int(monte_carlo_coverage.get("active_months", 0) or 0) >= 6
            else "warning",
        )
        self.monte_carlo_chart.set_data(result)
        confidence_label = str(result.get("confidence_label") or "Dados limitados")
        confidence_key = str(result.get("confidence") or "").casefold()
        self.monte_carlo_card.set_confidence_badge(
            f"Confiança · {confidence_label}",
            "positive"
            if "elev" in confidence_key or "high" in confidence_key
            else "neutral"
            if "moder" in confidence_key or "medium" in confidence_key
            else "warning",
        )
        rows = list(result.get("rows") or [])
        if not rows:
            self.monte_carlo_result.setText("Sem simulações disponíveis.")
            return
        final = rows[-1]
        probability = safe_float(result.get("probability_target"))
        tone = GREEN if probability >= 70 else ORANGE if probability >= 40 else RED
        self.monte_carlo_result.setStyleSheet(
            "font-weight: 700; padding: 8px 11px; "
            f"background: {self._rgba(tone, 24)}; "
            f"border: 1px solid {self._rgba(tone, 70)}; border-radius: 10px;"
        )
        self.monte_carlo_result.setText(
            f"🎯 Probabilidade de atingir {eur(result.get('target', 0), self.app.theme.privacy)}: "
            f"{percent(probability)} em {int(final.get('year', 0))} anos. "
            f"Mediana {eur(final.get('p50', 0), self.app.theme.privacy)} · "
            f"faixa P10–P90 {eur(final.get('p10', 0), self.app.theme.privacy)} a "
            f"{eur(final.get('p90', 0), self.app.theme.privacy)}.\n"
            f"Risco de terminar abaixo das contribuições reais: "
            f"{percent(result.get('probability_loss_real', 0))} · "
            f"drawdown mediano {percent(result.get('median_max_drawdown_pct', 0))} · "
            f"{result.get('confidence_label', '')}."
        )
        self.monte_carlo_method.setText(
            f"{result.get('data_warning', '')}\n{result.get('method_label', '')}"
        )

    def _on_monte_carlo_error(self, err: str) -> None:
        self.monte_carlo_result.setText(
            "Não foi possível calcular a simulação Monte Carlo."
        )

    def refresh(self):
        rows = self.repo.holdings()
        self._holding_rows = list(rows)
        self._holdings_by_id = {
            int(row_get(row, "id") or 0): row
            for row in rows
            if int(row_get(row, "id") or 0) > 0
        }
        self._holdings_by_stable_id = {
            str(row_get(row, "stable_id") or ""): row
            for row in rows
            if str(row_get(row, "stable_id") or "").strip()
        }
        liabilities = self.repo.liabilities()
        net_worth = self.analytics_service.net_worth_snapshot()
        performance = portfolio_performance_attribution(rows)
        self._portfolio_performance = dict(performance)
        attribution_by_id = {
            int(item.get("id", 0) or 0): item for item in performance.get("rows", [])
        }
        cost = safe_float(performance.get("total_cost_basis"))
        fees = safe_float(performance.get("total_fees"))
        gross_gain = safe_float(performance.get("gross_gain"))
        gain = safe_float(performance.get("net_gain"))
        gross_return_value = performance.get("gross_return_pct")
        return_value = performance.get("net_return_pct")
        gross_ret = safe_float(gross_return_value)
        ret = safe_float(return_value)
        performance_available = return_value is not None
        performance_coverage = safe_float(performance.get("data_coverage_pct"))
        self.clear_summary()
        cards = [
            (
                "Património líquido",
                eur(net_worth.get("net_worth", 0), self.app.theme.privacy),
                "ativos − passivos",
                GREEN if safe_float(net_worth.get("net_worth")) >= 0 else RED,
                "💎",
            ),
            (
                "Ativos totais",
                eur(net_worth.get("total_assets", 0), self.app.theme.privacy),
                "contas + investimentos",
                BLUE,
                "📈",
            ),
            (
                "Passivos",
                eur(net_worth.get("total_liabilities", 0), self.app.theme.privacy),
                f"rácio {percent(net_worth.get('debt_ratio_pct', 0), self.app.theme.privacy)}",
                RED if safe_float(net_worth.get("total_liabilities")) > 0 else GREEN,
                "🧾",
            ),
            (
                "Ganho líquido da carteira",
                eur(gain, self.app.theme.privacy) if performance_available else "N/D",
                (
                    f"{percent(ret, self.app.theme.privacy)} ponderados · cobertura {percent(performance_coverage, self.app.theme.privacy)}"
                    if performance_available
                    else "Confirma o capital investido e o câmbio de aquisição para calcular"
                ),
                GREEN
                if performance_available and gain >= 0
                else RED
                if performance_available
                else BLUE,
                "↕",
            ),
        ]
        for i, m in enumerate(cards):
            self.summary.addWidget(MetricCard(*m), 0, i)

        # Financial independence uses up to 12 complete observed months. The
        # current partial month is excluded and no synthetic expense is inserted.
        complete_months = self.analytics_service.monthly_summary(13)
        investable_capital = max(
            0.0,
            safe_float(net_worth.get("liquid_assets"))
            + safe_float(net_worth.get("investment_assets"))
            - safe_float(net_worth.get("total_liabilities")),
        )
        fire = build_financial_independence_outlook(
            complete_months,
            investable_capital=investable_capital,
            annual_rate_pct=self.projection_rate.value(),
            inflation_pct=self.projection_inflation.value(),
            tax_pct=self.projection_tax.value(),
            contribution_fee_pct=self.projection_fee.value(),
        )
        prog_pct = (
            min(100, max(0, round(safe_float(fire.get("progress_pct")))))
            if fire.get("available")
            else 0
        )
        self.fire_progress.setValue(prog_pct)
        self.fire_progress.setFormat(
            f"{prog_pct}% do objetivo"
            if fire.get("available")
            else "Dados insuficientes"
        )
        self.fire_progress.setStyleSheet(
            f"QProgressBar {{ border: 1px solid {self.app.theme.border}; border-radius: 12px; background: {self.app.theme.bg1}; text-align: center; color: {self.app.theme.text}; font-weight: bold; }} QProgressBar::chunk {{ background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {ORANGE}, stop:1 {YELLOW}); border-radius: 11px; }}"
        )

        if not fire.get("available"):
            self.fire_lbl.setText(
                "<h3 style='margin-bottom: 4px;'>Estimativa ainda indisponível</h3>"
                f"<p style='margin-top: 0;'>{fire.get('reason')} "
                f"Meses completos encontrados: <b>{int(fire.get('months_used', 0))}</b>. "
                "Não foi criado qualquer valor de despesas artificial.</p>"
            )
        elif prog_pct < 25:
            badge = "🥉"
        elif prog_pct < 50:
            badge = "🥈"
        elif prog_pct < 90:
            badge = "🥇"
        else:
            badge = "💎"

        if fire.get("available") and investable_capital >= safe_float(
            fire.get("target")
        ):
            txt = (
                f"<h3 style='color: {GREEN};'>Atingiste o objetivo estimado de independência financeira! 💎🏆</h3>"
                f"<p>O capital investível líquido de <b>{eur(investable_capital, self.app.theme.privacy)}</b> "
                f"cobre o objetivo de {eur(fire.get('target', 0), self.app.theme.privacy)}, "
                f"calculado com uma retirada anual de {percent(fire.get('withdrawal_rate_pct', 4))}.</p>"
            )
            if not getattr(self, "_confetti_shown", False):
                self.app.show_confetti()
                self._confetti_shown = True
        elif fire.get("available"):
            months_to_target = fire.get("months_to_target")
            if months_to_target is not None:
                years = int(months_to_target) // 12
                remaining_months = int(months_to_target) % 12
                horizon = (
                    f"{years} ano(s) e {remaining_months} mês(es)"
                    if remaining_months
                    else f"{years} ano(s)"
                )
                projection_text = f"projeção aproximada: <b>{horizon}</b>"
            else:
                projection_text = "sem data projetável enquanto a poupança média mensal não for positiva"
            txt = (
                "<h3 style='margin-bottom: 4px;'>Objetivo estimado: "
                f"<span style='color: #F59E0B;'>{eur(fire.get('target', 0), self.app.theme.privacy)}</span> {badge}</h3>"
                f"<p style='margin-top: 0;'>Capital investível líquido atual: "
                f"<b>{eur(investable_capital, self.app.theme.privacy)}</b> · {projection_text}. "
                f"Base: {int(fire.get('months_used', 0))} meses completos "
                f"({fire.get('period_start')} a {fire.get('period_end')}, confiança {fire.get('confidence')}), "
                f"despesa média {eur(fire.get('average_expenses', 0), self.app.theme.privacy)}/mês, "
                f"poupança média {eur(fire.get('average_savings', 0), self.app.theme.privacy)}/mês "
                f"e retorno real líquido estimado de {percent(fire.get('annual_real_rate_pct', 0))}/ano.</p>"
            )
        if fire.get("available"):
            self.fire_lbl.setText(txt)

        colours = [GREEN, BLUE, PURPLE, ORANGE, PINK, TEAL, YELLOW, MINT]
        donut_data = []
        holding_name_counts: dict[str, int] = {}
        for holding_row in rows:
            base_name = str(
                row_get(holding_row, "name")
                or row_get(holding_row, "asset_class")
                or "Investimento"
            )
            holding_name_counts[base_name] = holding_name_counts.get(base_name, 0) + 1
        for i, r in enumerate(rows):
            rate = safe_float(row_get(r, "exchange_rate", 1.0), 1.0)
            if rate <= 0:
                rate = 1.0
            value = safe_float(r["value"]) * rate
            if value > 0:
                label = str(r["name"] or r["asset_class"] or "Investimento")
                if holding_name_counts.get(label, 0) > 1:
                    label = (
                        f"{label} · {str(row_get(r, 'account') or 'Manual')} "
                        f"#{int(row_get(r, 'id') or 0)}"
                    )
                donut_data.append(
                    {
                        "id": int(row_get(r, "id") or 0),
                        "entity_id": str(row_get(r, "stable_id") or ""),
                        "label": label,
                        "value": value,
                        "color": colours[i % len(colours)],
                    }
                )
        self.investment_donut.set_data(donut_data)
        fee_impact = safe_float(performance.get("fee_impact_pp"))
        fee_impact_text = (
            "••• p.p."
            if self.app.theme.privacy
            else f"−{abs(fee_impact):.1f}".replace(".", ",") + " p.p."
        )
        if performance_available:
            performance_text = (
                f"Capital conhecido {eur(cost, self.app.theme.privacy)} + comissões "
                f"{eur(fees, self.app.theme.privacy)} · ganho bruto "
                f"{eur(gross_gain, self.app.theme.privacy)} ({percent(gross_ret, self.app.theme.privacy)}) · "
                f"ganho líquido {eur(gain, self.app.theme.privacy)} ({percent(ret, self.app.theme.privacy)}) · "
                f"diferença bruto/líquido (bases distintas) {fee_impact_text} · "
                f"comissões/custo {percent(performance.get('fees_pct_of_cost', 0), self.app.theme.privacy)}"
            )
        else:
            performance_text = (
                "Rentabilidade não disponível: nenhum ativo tem capital investido "
                "conhecido e superior a zero."
            )
        self.holdings_summary.setText(
            f"📊 Cobertura do custo de aquisição: {percent(performance_coverage, self.app.theme.privacy)} · "
            f"{performance_text}"
        )
        self.portfolio_contribution_chart.set_data(performance)
        self.net_worth_chart.set_data(net_worth)
        quality = dict(net_worth.get("data_quality") or {})
        unknown_count = len(quality.get("unknown_accounts") or [])
        linked_count = len(quality.get("linked_holdings") or [])
        self.net_worth_status.setText(
            f"<b>Liquidez:</b> {eur(net_worth.get('liquid_assets', 0), self.app.theme.privacy)} · "
            f"<b>Investimentos:</b> {eur(net_worth.get('investment_assets', 0), self.app.theme.privacy)} · "
            f"<b>Objetivos:</b> {eur(net_worth.get('goal_assets', 0), self.app.theme.privacy)} · "
            f"<b>Prestação mensal das dívidas:</b> "
            f"{eur(net_worth.get('monthly_debt_payment', 0), self.app.theme.privacy)} · "
            f"<b>Juro médio ponderado:</b> {percent(net_worth.get('weighted_interest_pct', 0), self.app.theme.privacy)}<br>"
            f"<b>Qualidade do património:</b> {quality.get('label', 'Parcial')} "
            f"({percent(quality.get('score_pct', 0), self.app.theme.privacy)}) · {'•••' if self.app.theme.privacy else unknown_count} conta(s) sem "
            f"saldo de referência · {'•••' if self.app.theme.privacy else linked_count} ativo(s) sem dupla contagem."
        )
        self.liability_table.clearContents()
        self.liability_table.setRowCount(len(liabilities))
        for liability_row, liability in enumerate(liabilities):
            values = [
                row_get(liability, "id"),
                row_get(liability, "name"),
                row_get(liability, "liability_type"),
                row_get(liability, "account"),
                eur(row_get(liability, "balance"), self.app.theme.privacy),
                percent(row_get(liability, "interest_rate")),
                eur(row_get(liability, "monthly_payment"), self.app.theme.privacy),
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if column >= 4:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                if column in (4, 5):
                    item.setForeground(QColor(RED))
                self.liability_table.setItem(liability_row, column, item)
        self._refresh_net_worth_bridge()
        evolution_rows = self.repo.investment_evolution()
        self._monte_carlo_history_points = len(evolution_rows)
        def _rate_val(item: Any) -> float:
            try:
                r = float(row_get(item, "exchange_rate", 1.0) or 1.0)
                return r if r > 0 else 1.0
            except (TypeError, ValueError):
                return 1.0

        self._monte_carlo_asset_weights = [
            max(0.0, safe_float(row_get(row, "value")) * _rate_val(row))
            for row in rows
            if safe_float(row_get(row, "value")) > 0
        ]
        self.investment_evolution.set_data(evolution_rows, gross_ret)
        self._refresh_projection_context()
        self._update_projection()

        self.table.clearContents()
        self.table.setRowCount(len(rows))
        for row, r in enumerate(rows):
            attribution = attribution_by_id.get(int(r["id"]), {})
            row_available = bool(attribution.get("performance_available"))
            row_gain = safe_float(attribution.get("net_gain"))
            row_return = safe_float(attribution.get("net_return_pct"))
            contribution = safe_float(attribution.get("contribution_pp"))
            curr = str(row_get(r, "currency", "EUR") or "EUR").strip().upper()
            rate = _rate_val(r)
            val_eur = safe_float(attribution.get("current_value", safe_float(r["value"]) * rate))
            cost_eur = safe_float(attribution.get("cost_basis", safe_float(r["cost_basis"]) * rate))
            fees_eur = safe_float(attribution.get("fees", safe_float(row_get(r, "fees")) * rate))
            vals = [
                r["id"],
                "Dados ocultos" if self.app.theme.privacy else r["name"],
                "Dados ocultos" if self.app.theme.privacy else r["asset_class"],
                "Dados ocultos" if self.app.theme.privacy else r["account"],
                eur(val_eur, self.app.theme.privacy),
                eur(cost_eur, self.app.theme.privacy)
                if row_available
                else "N/D",
                eur(fees_eur, self.app.theme.privacy),
                eur(row_gain, self.app.theme.privacy) if row_available else "N/D",
                percent(row_return, self.app.theme.privacy) if row_available else "N/D",
                (
                    "••• p.p."
                    if self.app.theme.privacy
                    else f"{contribution:+.1f} p.p.".replace(".", ",")
                )
                if row_available
                else "N/D",
            ]
            for col, val in enumerate(vals):
                item = QTableWidgetItem(str(val))
                if col >= 4:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                if col in (7, 8, 9) and row_available:
                    item.setForeground(QColor(GREEN if row_gain >= 0 else RED))
                if col == 4 and curr != "EUR":
                    # BUG-AUD-C100: Mask financial balances in tooltips under Privacy Mode
                    privacy_active = bool(getattr(getattr(self.app, "theme", None), "privacy", False))
                    val_str = "••••" if privacy_active else f"{safe_float(r['value']):,.2f}"
                    item.setToolTip(f"{val_str} {curr} (câmbio: {rate:.4f})")
                self.table.setItem(row, col, item)

    def selected_id(self) -> Optional[int]:
        row = self.table.currentRow()
        item = self.table.item(row, 0) if row >= 0 else None
        # BUG-UI-03: seleção vazia ou texto não numérico devolve None em vez
        # de crashar com ValueError (os callers já verificam None).
        try:
            return int(item.text()) if item else None
        except (AttributeError, TypeError, ValueError):
            return None

    def _holding_detail_payload(self, item: dict) -> dict:
        name = str(item.get("label") or item.get("name") or "Investimento")
        holding_id = int(item.get("id", 0) or 0)
        holding = getattr(self, "_holdings_by_id", {}).get(holding_id)
        if holding is None:
            return {
                "title": "Distribuição dos investimentos",
                "subtitle": name,
            }
        attribution = next(
            (
                dict(row)
                for row in getattr(
                    self,
                    "_portfolio_performance",
                    {},
                ).get("rows", [])
                if int(row.get("id", 0) or 0) == holding_id
            ),
            {},
        )
        value = safe_float(attribution.get("current_value", row_get(holding, "value")))
        cost = safe_float(attribution.get("cost_basis", row_get(holding, "cost_basis")))
        fees = safe_float(attribution.get("fees", row_get(holding, "fees")))
        performance_available = bool(attribution.get("performance_available"))
        gross_gain = (
            safe_float(attribution.get("gross_gain"), value - cost)
            if performance_available
            else None
        )
        gain = (
            safe_float(attribution.get("net_gain"), value - cost - fees)
            if performance_available
            else None
        )
        net_return = safe_float(attribution.get("net_return_pct"))
        contribution = safe_float(attribution.get("contribution_pp"))
        return {
            "icon": "📈",
            "title": "Detalhe do investimento",
            "subtitle": name,
            "explanation": (
                "O ganho bruto compara valor atual e capital investido. O ganho "
                "líquido também deduz as comissões registadas; a contribuição "
                "mostra o impacto deste ativo na rentabilidade total."
                if performance_available
                else "A rentabilidade fica indisponível porque o custo de aquisição "
                "não foi confirmado. O valor atual continua incluído na carteira."
            ),
            "metrics": [
                {
                    "label": "Valor atual",
                    "value": eur(value, self.app.theme.privacy),
                    "tone": (
                        "positive"
                        if performance_available and safe_float(gain) >= 0
                        else "negative"
                        if performance_available
                        else "neutral"
                    ),
                },
                {
                    "label": "Capital investido",
                    "value": (
                        eur(cost, self.app.theme.privacy)
                        if performance_available
                        else "N/D"
                    ),
                    "tone": "neutral",
                },
                {
                    "label": "Comissões",
                    "value": eur(fees, self.app.theme.privacy),
                    "tone": "warning" if fees > 0 else "neutral",
                },
                {
                    "label": "Ganho bruto",
                    "value": (
                        eur(gross_gain, self.app.theme.privacy)
                        if performance_available
                        else "N/D"
                    ),
                    "tone": (
                        "positive"
                        if performance_available and safe_float(gross_gain) >= 0
                        else "negative"
                        if performance_available
                        else "neutral"
                    ),
                },
                {
                    "label": "Ganho líquido",
                    "value": (
                        eur(gain, self.app.theme.privacy)
                        if performance_available
                        else "N/D"
                    ),
                    "tone": (
                        "positive"
                        if performance_available and safe_float(gain) >= 0
                        else "negative"
                        if performance_available
                        else "neutral"
                    ),
                },
                {
                    "label": "Rentabilidade líquida",
                    "value": percent(net_return) if performance_available else "N/D",
                    "tone": (
                        "positive"
                        if performance_available and safe_float(gain) >= 0
                        else "negative"
                        if performance_available
                        else "neutral"
                    ),
                },
                {
                    "label": "Contribuição",
                    "value": (
                        f"{contribution:+.1f} p.p.".replace(".", ",")
                        if performance_available
                        else "N/D"
                    ),
                    "tone": (
                        "positive"
                        if performance_available and contribution >= 0
                        else "negative"
                        if performance_available
                        else "neutral"
                    ),
                },
            ],
            "rows_title": "Classificação",
            "rows": [
                {
                    "date": "Ativo",
                    "title": str(row_get(holding, "asset_class") or "Sem tipo"),
                    "subtitle": str(row_get(holding, "account") or "Sem conta"),
                    "value": percent(
                        value
                        / max(
                            1.0,
                            sum(
                                safe_float(row_get(row, "value"))
                                for row in getattr(self, "_holding_rows", [])
                            ),
                        )
                        * 100
                    ),
                    "tone": "neutral",
                }
            ],
        }

    def _portfolio_contribution_detail_payload(
        self,
        item: dict,
    ) -> dict:
        name = str(item.get("name") or "Investimento")
        performance_available = bool(item.get("performance_available"))
        net_gain = safe_float(item.get("net_gain"))
        contribution = safe_float(item.get("contribution_pp"))
        return {
            "icon": "🧭",
            "title": "Contribuição para a carteira",
            "subtitle": (
                f"{name} · {item.get('asset_class', 'Outro')} · "
                f"{item.get('account', 'Manual')}"
            ),
            "explanation": (
                "A contribuição em pontos percentuais é o ganho líquido deste "
                "ativo dividido pelo capital total comprometido na carteira. "
                "A soma de todos os ativos coincide com a rentabilidade líquida "
                "ponderada."
                if performance_available
                else "O valor atual é conhecido, mas não existe um custo de aquisição "
                "válido. Este ativo fica fora da rentabilidade percentual."
            ),
            "metrics": [
                {
                    "label": "Valor atual",
                    "value": eur(
                        item.get("current_value", 0),
                        self.app.theme.privacy,
                    ),
                    "tone": "neutral",
                },
                {
                    "label": "Capital + comissões",
                    "value": (
                        eur(item.get("cash_invested", 0), self.app.theme.privacy)
                        if performance_available
                        else "N/D"
                    ),
                    "tone": "neutral",
                },
                {
                    "label": "Comissões",
                    "value": eur(
                        item.get("fees", 0),
                        self.app.theme.privacy,
                    ),
                    "tone": (
                        "warning" if safe_float(item.get("fees")) > 0 else "neutral"
                    ),
                },
                {
                    "label": "Ganho líquido",
                    "value": (
                        eur(net_gain, self.app.theme.privacy)
                        if performance_available
                        else "N/D"
                    ),
                    "tone": (
                        "positive"
                        if performance_available and net_gain >= 0
                        else "negative"
                        if performance_available
                        else "neutral"
                    ),
                },
                {
                    "label": "Rentabilidade do ativo",
                    "value": (
                        percent(item.get("net_return_pct", 0))
                        if performance_available
                        else "N/D"
                    ),
                    "tone": (
                        "positive"
                        if performance_available and net_gain >= 0
                        else "negative"
                        if performance_available
                        else "neutral"
                    ),
                },
                {
                    "label": "Contribuição total",
                    "value": (
                        f"{contribution:+.1f} p.p.".replace(".", ",")
                        if performance_available
                        else "N/D"
                    ),
                    "tone": (
                        "positive"
                        if performance_available and contribution >= 0
                        else "negative"
                        if performance_available
                        else "neutral"
                    ),
                },
            ],
        }

    def _select_contribution_holding(self, item: dict) -> None:
        holding_id = int(item.get("id", 0) or 0)
        if holding_id:
            self._select_id(holding_id)

    def _select_holding_item(self, item: dict) -> None:
        holding_id = int(item.get("id", 0) or 0)
        if holding_id:
            self._select_id(holding_id)

    def _net_worth_detail_payload(self, _item: dict) -> dict:
        data = self.analytics_service.net_worth_snapshot()
        net = safe_float(data.get("net_worth"))
        quality = dict(data.get("data_quality") or {})
        return {
            "icon": "💎",
            "title": "Património completo",
            "subtitle": "Ativos, passivos e património líquido",
            "explanation": (
                "O património líquido resulta dos ativos disponíveis menos "
                "todas as dívidas registadas. Os saldos de referência e as ligações "
                "a ativos monetários determinam a qualidade do total."
            ),
            "metrics": [
                {
                    "label": "Ativos",
                    "value": eur(data.get("total_assets", 0), self.app.theme.privacy),
                    "tone": "positive",
                },
                {
                    "label": "Passivos",
                    "value": eur(
                        data.get("total_liabilities", 0),
                        self.app.theme.privacy,
                    ),
                    "tone": "negative",
                },
                {
                    "label": "Património líquido",
                    "value": eur(net, self.app.theme.privacy),
                    "tone": "positive" if net >= 0 else "negative",
                },
                {
                    "label": "Rácio de dívida",
                    "value": percent(data.get("debt_ratio_pct", 0)),
                    "tone": "warning",
                },
                {
                    "label": "Qualidade dos dados",
                    "value": (
                        f"{quality.get('label', 'Parcial')} · "
                        f"{percent(quality.get('score_pct', 0))}"
                    ),
                    "tone": (
                        "positive"
                        if safe_float(quality.get("score_pct")) >= 90
                        else "warning"
                    ),
                },
            ],
        }

    def _select_id(self, hid: int) -> None:
        for r in range(self.table.rowCount()):
            item = self.table.item(r, 0)
            if item and item.text() == str(hid):
                self.table.selectRow(r)
                self.table.scrollToItem(item, QAbstractItemView.PositionAtCenter)
                break

    def add_holding(self):
        d = HoldingDialog(self.app)
        if d.exec() == QDialog.Accepted:
            data = d.get_data()
            if not data["name"]:
                QMessageBox.warning(self, APP_TITLE, "Indica o nome do investimento.")
                return
            if data["cost_basis_known"] and data["cost"] <= 0:
                QMessageBox.warning(
                    self,
                    APP_TITLE,
                    "Se conheces o capital investido, indica um valor superior a zero. "
                    "Caso contrário, desmarca essa opção.",
                )
                return
            try:
                self.repo.add_holding(
                    data["name"],
                    data["asset_class"],
                    data["account"],
                    data["value"],
                    data["cost"],
                    data["notes"],
                    fees=data["fees"],
                    cost_basis_known=data["cost_basis_known"],
                    linked_account=data["linked_account"],
                    currency=data["currency"],
                    exchange_rate=data["exchange_rate"],
                    acquisition_date=data.get("acquisition_date", ""),
                    jurisdiction=data.get("jurisdiction", "PT"),
                )
                self.app.notify_data_changed("Investimento adicionado.")
            except Exception as exc:
                log_exception("PortfolioPage.add_holding", exc)
                QMessageBox.critical(
                    self, APP_TITLE, f"Erro ao adicionar investimento: {exc}"
                )

    def edit_selected(self):
        hid = self.selected_id()
        if not hid:
            QMessageBox.information(self, APP_TITLE, "Seleciona um investimento.")
            return
        holding = self.repo.holding_by_id(hid)
        if not holding:
            QMessageBox.warning(
                self, APP_TITLE, "Não encontrei o investimento selecionado."
            )
            self.refresh()
            return
        d = HoldingDialog(self.app, holding)
        if d.exec() == QDialog.Accepted:
            data = d.get_data()
            if not data["name"]:
                QMessageBox.warning(self, APP_TITLE, "Indica o nome do investimento.")
                return
            if data["cost_basis_known"] and data["cost"] <= 0:
                QMessageBox.warning(
                    self,
                    APP_TITLE,
                    "Se conheces o capital investido, indica um valor superior a zero. "
                    "Caso contrário, desmarca essa opção.",
                )
                return
            try:
                self.repo.update_holding(
                    hid,
                    data["name"],
                    data["asset_class"],
                    data["account"],
                    data["value"],
                    data["cost"],
                    data["notes"],
                    fees=data["fees"],
                    cost_basis_known=data["cost_basis_known"],
                    linked_account=data["linked_account"],
                    currency=data["currency"],
                    exchange_rate=data["exchange_rate"],
                    acquisition_date=data.get("acquisition_date", ""),
                    jurisdiction=data.get("jurisdiction", "PT"),
                )
                self.app.notify_data_changed("Investimento atualizado.")
                self._select_id(hid)
            except Exception as exc:
                log_exception("PortfolioPage.edit_selected", exc)
                QMessageBox.critical(
                    self, APP_TITLE, f"Erro ao atualizar investimento: {exc}"
                )

    def edit_account_balance(self) -> None:
        dialog = AccountBalanceDialog(self.repo, self.app)
        if dialog.exec() != QDialog.Accepted:
            return
        data = dialog.values()
        try:
            self.repo.set_financial_account_reference(
                data["name"],
                data["balance"],
                data["date"],
                balance_known=bool(data["known"]),
            )
        except (RuntimeError, ValueError) as exc:
            QMessageBox.warning(self, APP_TITLE, str(exc))
            return
        self.app.notify_data_changed(
            "Saldo de referência guardado; o património foi recalculado."
        )

    def close_selected_position(self) -> None:
        """BUG-INV-01: Dedicated flow to close/sell an investment holding."""
        hid = self.selected_id()
        if not hid:
            QMessageBox.information(
                self, APP_TITLE, "Seleciona um investimento para fechar a posição."
            )
            return
        holding = self.repo.holding_by_id(hid)
        if not holding:
            QMessageBox.warning(
                self, APP_TITLE, "Não encontrei o investimento selecionado."
            )
            self.refresh()
            return

        from ui.dialogs import ClosePositionDialog

        dlg = ClosePositionDialog(self.app, holding)
        if dlg.exec() != QDialog.Accepted:
            return

        data = dlg.get_data()
        holding_name = str(row_get(holding, "name") or "Investimento")
        try:
            self.repo.record_investment_disposal(
                hid,
                sale_date=data["sale_date"],
                sale_value_eur=data["sale_value"],
                sale_fees_eur=data["sale_fees"],
                account=data["account"],
                create_transaction=data["create_transaction"],
                archive_holding=data["archive_holding"],
                acquisition_exchange_rate=data.get("acquisition_exchange_rate"),
                journal=getattr(self.app, "change_journal_service", None),
            )
            msg = f"Posição em «{holding_name}» encerrada; comprovativo da venda preservado."
            self.app.notify_data_changed(msg)
            if hasattr(self.app, "refresh_undo_actions"):
                self.app.refresh_undo_actions()
            self.refresh()
        except Exception as exc:
            log_exception("PortfolioPage.close_selected_position", exc)
            QMessageBox.critical(
                self, APP_TITLE, f"Erro ao fechar posição de investimento: {exc}"
            )

    def delete_selected(self):
        hid = self.selected_id()
        if not hid:
            QMessageBox.information(self, APP_TITLE, "Seleciona um investimento.")
            return
        holding = self.repo.holding_by_id(hid)
        name = (
            str(row_get(holding, "name") or "")
            if holding
            else (
                self.table.item(self.table.currentRow(), 1).text()
                if self.table.currentRow() >= 0 and self.table.item(self.table.currentRow(), 1)
                else "este investimento"
            )
        )
        linked = str(row_get(holding, "linked_account") or "").strip() if holding else ""
        linked_warning = (
            f"\n\n⚠️ Atenção: Este investimento está associado à conta «{linked}».\n"
            "A sua eliminação afeta o património, mas o saldo da conta permanece inalterado."
            if linked
            else ""
        )
        msg = (
            f"Queres mesmo eliminar «{name}»?{linked_warning}\n\n"
            "Esta ação pode ser desfeita mais tarde com Ctrl+Z."
        )
        if QMessageBox.question(self, APP_TITLE, msg) == QMessageBox.Yes:
            try:
                journal_svc = getattr(self.app, "change_journal_service", None)
                if journal_svc and hasattr(journal_svc, "delete_holding"):
                    journal_svc.delete_holding(
                        hid, label=f"Eliminar investimento · {name}"
                    )
                else:
                    self.repo.delete_holding(hid)
                self.app.notify_data_changed("Investimento eliminado.")
                if hasattr(self.app, "refresh_undo_actions"):
                    self.app.refresh_undo_actions()
            except Exception as exc:
                log_exception("PortfolioPage.delete_selected", exc)
                QMessageBox.critical(
                    self, APP_TITLE, f"Erro ao eliminar investimento: {exc}"
                )

    def selected_liability_id(self) -> Optional[int]:
        try:
            row = self.liability_table.currentRow()
            if row < 0:
                return None
            item = self.liability_table.item(row, 0)
            if not item:
                return None
            txt = (item.text() or "").strip()
            return int(txt) if txt.isdigit() else None
        except Exception:
            return None

    def add_liability(self) -> None:
        try:
            dialog = LiabilityDialog(self.app)
            if dialog.exec() != QDialog.Accepted:
                return
            data = dialog.get_data()
            if not data["name"]:
                QMessageBox.warning(self, APP_TITLE, "Indica o nome da dívida.")
                return
            self.repo.add_liability(
                data["name"],
                data["liability_type"],
                data["account"],
                data["balance"],
                data["interest_rate"],
                data["monthly_payment"],
                data["notes"],
            )
            self.app.notify_data_changed("Dívida adicionada ao património.")
        except Exception as exc:
            log_exception("PortfolioPage.add_liability", exc)
            QMessageBox.critical(
                self, APP_TITLE, f"Erro ao adicionar dívida: {exc}"
            )

    def edit_liability(self) -> None:
        liability_id = self.selected_liability_id()
        if not liability_id:
            QMessageBox.information(self, APP_TITLE, "Seleciona uma dívida.")
            return
        try:
            liability = self.repo.liability_by_id(liability_id)
            if liability is None:
                self.refresh()
                return
            dialog = LiabilityDialog(self.app, liability)
            if dialog.exec() != QDialog.Accepted:
                return
            data = dialog.get_data()
            if not data["name"]:
                QMessageBox.warning(
                    self,
                    APP_TITLE,
                    "O nome da dívida não pode ficar em branco. "
                    "A atualização foi cancelada.",
                )
                return
            self.repo.update_liability(
                liability_id,
                data["name"],
                data["liability_type"],
                data["account"],
                data["balance"],
                data["interest_rate"],
                data["monthly_payment"],
                data["notes"],
            )
            self.app.notify_data_changed("Dívida atualizada.")
        except Exception as exc:
            log_exception("PortfolioPage.edit_liability", exc)
            QMessageBox.critical(
                self, APP_TITLE, f"Erro ao atualizar dívida: {exc}"
            )

    def delete_liability(self) -> None:
        liability_id = self.selected_liability_id()
        if not liability_id:
            QMessageBox.information(self, APP_TITLE, "Seleciona uma dívida.")
            return
        try:
            row = self.liability_table.currentRow()
            item = self.liability_table.item(row, 1) if row >= 0 else None
            name = (
                item.text().strip()
                if (item and item.text() and item.text().strip())
                else "esta dívida"
            )
            if (
                QMessageBox.question(self, APP_TITLE, f"Eliminar a dívida {name}?")
                != QMessageBox.Yes
            ):
                return
            self.repo.delete_liability(liability_id)
            self.app.notify_data_changed("Dívida eliminada.")
        except Exception as exc:
            log_exception("PortfolioPage.delete_liability", exc)
            QMessageBox.critical(
                self, APP_TITLE, f"Erro ao eliminar dívida: {exc}"
            )


from .subscriptions import SubscriptionsPage as SubscriptionsPage

InvestmentsPage = PortfolioPage

__all__ = ["PortfolioPage", "InvestmentsPage"]
