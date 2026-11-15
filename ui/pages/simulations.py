from __future__ import annotations

import json
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QComboBox,
    QDoubleSpinBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSlider,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)

from services.habit_simulations import (
    DEFAULT_COST_INFLATION_PCT,
    DEFAULT_INVESTMENT_RATE_PCT,
    HABIT_PRESETS,
    HABIT_PRESETS_BY_KEY,
    HabitPreset,
    HabitSimulationInputs,
    occurrences_per_month,
    simulate_habit,
)
from services.habit_personalization import (
    build_goal_impact,
    normalise_habit_audit_rules,
)
from services.habit_plans import build_habit_plan_payload, scenario_plan_values
from services.fire import FirePlanningEngine
from ui.components import GlassCard, HabitImpactChart, MetricCard
from ui.components.habit_audit import HabitAuditDialog
from ui.components.habit_plans import HabitPlanDialog, HabitPlansCard
from ui.core import (
    BasePage,
    EuroSpinBox,
    FlowLayout,
    ScrollPassthroughSlider,
    clear_layout_recursive,
)
from ui.core.smart_table import SmartTableWidgetItem as QTableWidgetItem
from utils.helpers import BLUE, GREEN, ORANGE, PURPLE, eur, percent, safe_float
from utils.app_logging import log_exception


if TYPE_CHECKING:
    from main import LiquidFinanceApp


class HabitSimulationsPage(BasePage):
    SETTINGS_KEY = "habit_simulations_state_v1"
    AUDIT_SETTINGS_KEY = "habit_audit_rules_v1"

    def __init__(self, app: "LiquidFinanceApp") -> None:
        super().__init__(
            app,
            "Simulações",
            "Explora o custo real de hábitos e o que esse dinheiro poderia construir",
        )
        self._updating_controls = True
        self._selected_key = HABIT_PRESETS[0].key
        self._habit_states: dict[str, dict[str, Any]] = {}
        self._average_income = 0.0
        self._average_savings = 0.0
        self._income_reference_name = "salário"
        self._context_months = 0
        self._habit_calibration: dict[str, Any] = {}
        self._goals_by_id: dict[int, dict[str, Any]] = {}
        self._selected_goal_id = 0
        self._preview_kind = "current"
        self._base_data: dict[str, Any] = {}
        self._current_data: dict[str, Any] = {}
        self._selected_horizon_months = 120
        self._habit_transaction_context: dict[str, Any] = {}

        saved = self._saved_state()
        self._current_view = (
            str(saved.get("view") or "simulate")
            if str(saved.get("view") or "simulate") in {"simulate", "plans"}
            else "simulate"
        )
        self._habit_audit_rules = self._saved_audit_rules()
        selected = str(saved.get("selected") or self._selected_key)
        if selected in HABIT_PRESETS_BY_KEY:
            self._selected_key = selected
        stored_habits = saved.get("habits", {})
        if isinstance(stored_habits, dict):
            self._habit_states = {
                str(key): dict(value)
                for key, value in stored_habits.items()
                if isinstance(value, dict)
            }
        self._selected_goal_id = int(safe_float(saved.get("selected_goal_id")))

        self.view_bar = QFrame()
        self.view_bar.setObjectName("StatusBanner")
        self.view_bar.setProperty("tone", "neutral")
        view_layout = QHBoxLayout(self.view_bar)
        view_layout.setContentsMargins(10, 7, 10, 7)
        view_layout.setSpacing(8)
        view_hint = QLabel("Vista")
        view_hint.setObjectName("Subtle")
        view_layout.addWidget(view_hint)
        self.view_group = QButtonGroup(self)
        self.view_group.setExclusive(True)
        self.simulate_view_btn = QPushButton("🧪 Simular Hábitos")
        self.simulate_view_btn.setCheckable(True)
        self.simulate_view_btn.setProperty("viewKey", "simulate")
        self.simulate_view_btn.setObjectName("Secondary")
        self.plans_view_btn = QPushButton("📈 Planos (0)")
        self.plans_view_btn.setCheckable(True)
        self.plans_view_btn.setProperty("viewKey", "plans")
        self.plans_view_btn.setObjectName("Secondary")
        self.fire_view_btn = QPushButton("🔥 Independência FIRE")
        self.fire_view_btn.setCheckable(True)
        self.fire_view_btn.setProperty("viewKey", "fire")
        self.fire_view_btn.setObjectName("Secondary")
        for button in (self.simulate_view_btn, self.plans_view_btn, self.fire_view_btn):
            self.view_group.addButton(button)
            view_layout.addWidget(button)
        view_layout.addStretch(1)

        self.main_card = GlassCard(
            "🧪 Laboratório de impacto financeiro · hábitos e escolhas",
            strong=True,
            accent="#BF5AF2",
        )
        self.main_card.setProperty("pageCardKey", "habit_simulation_lab")
        self.main_card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        self.main_card.set_compact_header(word_wrap=True)

        self.intro_wrap = QWidget()
        intro_row = QHBoxLayout(self.intro_wrap)
        intro_row.setContentsMargins(0, 0, 0, 0)
        intro = QLabel(
            "Escolhe um hábito, ajusta o valor e a frequência e observa o impacto "
            "entre um mês e trinta anos."
        )
        intro.setObjectName("Subtle")
        intro.setWordWrap(True)
        intro_row.addWidget(intro, 1)
        self.impact_badge = QLabel("Cenário em tempo real")
        self.impact_badge.setObjectName("MetricStatus")
        self.impact_badge.setProperty("tone", "neutral")
        intro_row.addWidget(self.impact_badge, 0, Qt.AlignTop)
        self.main_card.fit_widget_height(self.intro_wrap)
        self.main_card.v.addWidget(self.intro_wrap)

        self.choose_title = QLabel("1 · O que queres simular?")
        self.choose_title.setObjectName("SimulationSectionTitle")
        self.main_card.fit_label_height(self.choose_title)
        self.main_card.v.addWidget(self.choose_title)

        self.habit_group = QButtonGroup(self)
        self.habit_group.setExclusive(True)
        self.habit_buttons: dict[str, QPushButton] = {}
        self.habit_grid = QGridLayout()
        self.habit_grid.setContentsMargins(0, 0, 0, 0)
        self.habit_grid.setHorizontalSpacing(8)
        self.habit_grid.setVerticalSpacing(8)
        for index, preset in enumerate(HABIT_PRESETS):
            button = QPushButton(preset.display_name)
            button.setObjectName("HabitTile")
            button.setCheckable(True)
            button.setCursor(Qt.PointingHandCursor)
            button.setProperty("habitKey", preset.key)
            button.setToolTip(preset.description)
            button.setAccessibleName(f"Simular {preset.name}")
            self.habit_group.addButton(button)
            self.habit_buttons[preset.key] = button
            self.habit_grid.addWidget(button, index // 4, index % 4)
        self.main_card.v.addLayout(self.habit_grid)
        self.register_responsive_grid(
            self.habit_grid, max_columns=4, min_column_width=155
        )

        self.preset_description = QLabel()
        self.preset_description.setObjectName("Subtle")
        self.preset_description.setWordWrap(True)
        self.main_card.fit_label_height(
            self.preset_description,
            word_wrap=True,
        )
        self.main_card.v.addWidget(self.preset_description)

        self.calibration_panel = QFrame()
        self.calibration_panel.setObjectName("StatusBanner")
        self.calibration_panel.setProperty("tone", "neutral")
        calibration_layout = QGridLayout(self.calibration_panel)
        calibration_layout.setContentsMargins(13, 10, 13, 10)
        calibration_layout.setHorizontalSpacing(10)
        calibration_layout.setVerticalSpacing(3)
        self.calibration_icon = QLabel("📚")
        self.calibration_icon.setObjectName("StatusBannerIcon")
        self.calibration_icon.setAlignment(Qt.AlignCenter)
        self.calibration_icon.setFixedSize(42, 42)
        self.calibration_title = QLabel("A procurar padrões nos teus movimentos…")
        self.calibration_title.setObjectName("StatusBannerTitle")
        self.calibration_confidence = QLabel("A analisar")
        self.calibration_confidence.setObjectName("MetricStatus")
        self.calibration_confidence.setProperty("tone", "neutral")
        self.calibration_detail = QLabel()
        self.calibration_detail.setObjectName("StatusBannerDescription")
        self.calibration_detail.setWordWrap(True)
        self.real_data_preview_btn = QPushButton("Comparar média real")
        self.real_data_preview_btn.setObjectName("CardAction")
        self.real_data_preview_btn.setCheckable(True)
        self.real_data_preview_btn.setAccessibleName(
            "Pré-visualizar valores calculados a partir dos meus movimentos"
        )
        self.audit_movements_btn = QPushButton("🔍 Rever movimentos")
        self.audit_movements_btn.setObjectName("CardAction")
        self.audit_movements_btn.setToolTip(
            "Escolher os movimentos usados para calcular a média real deste hábito"
        )
        calibration_actions = QWidget()
        calibration_actions_layout = FlowLayout(
            calibration_actions,
            margin=0,
            hspacing=6,
            vspacing=5,
        )
        calibration_actions_layout.addWidget(self.real_data_preview_btn)
        calibration_actions_layout.addWidget(self.audit_movements_btn)
        calibration_layout.addWidget(self.calibration_icon, 0, 0, 2, 1)
        calibration_layout.addWidget(self.calibration_title, 0, 1)
        calibration_layout.addWidget(self.calibration_confidence, 0, 2)
        calibration_layout.addWidget(calibration_actions, 0, 3)
        calibration_layout.addWidget(self.calibration_detail, 1, 1, 1, 3)
        calibration_layout.setColumnStretch(1, 1)
        self.main_card.v.addWidget(self.calibration_panel)
        self.main_card.v.addWidget(self._divider())

        self.controls_title = QLabel("2 · Define o teu cenário")
        self.controls_title.setObjectName("SimulationSectionTitle")
        self.main_card.fit_label_height(self.controls_title)
        self.main_card.v.addWidget(self.controls_title)

        self.amount_slider = ScrollPassthroughSlider(Qt.Horizontal)
        self.amount_slider.setRange(0, 100_000)
        self.amount_slider.setSingleStep(10)
        self.amount_slider.setPageStep(500)
        self.amount_slider.setTickInterval(10_000)
        self.amount_slider.setTickPosition(QSlider.TicksBelow)
        self.amount_slider.setAccessibleName("Valor de cada ocorrência")
        self.amount_spin = EuroSpinBox()
        self.amount_spin.setRange(0.0, 1_000.0)
        self.amount_spin.setDecimals(2)
        self.amount_spin.setSingleStep(0.50)
        self.amount_spin.setSuffix(" €")
        self.amount_spin.setKeyboardTracking(False)
        self.amount_spin.setAccessibleName("Valor exato de cada ocorrência")

        self.amount_title = QLabel("Valor por ocorrência")
        self.amount_value_label = QLabel()
        self.amount_value_label.setObjectName("SimulationControlValue")
        self.amount_value_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

        self.frequency_slider = ScrollPassthroughSlider(Qt.Horizontal)
        self.frequency_slider.setRange(0, 60)
        self.frequency_slider.setSingleStep(1)
        self.frequency_slider.setPageStep(5)
        self.frequency_slider.setTickInterval(10)
        self.frequency_slider.setTickPosition(QSlider.TicksBelow)
        self.frequency_slider.setAccessibleName(
            "Frequência inteira do hábito, sem meios valores"
        )
        self.frequency_unit = QComboBox()
        self.frequency_unit.addItem("por dia", "day")
        self.frequency_unit.addItem("por semana", "week")
        self.frequency_unit.addItem("por mês", "month")
        self.frequency_unit.setAccessibleName("Unidade da frequência")
        self.frequency_title = QLabel("Frequência")
        self.frequency_value_label = QLabel()
        self.frequency_value_label.setObjectName("SimulationControlValue")
        self.frequency_value_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

        self.alternative_name_label = QLabel()
        self.alternative_name_label.setObjectName("SimulationControlValue")
        self.alternative_name_label.setWordWrap(True)
        self.alternative_spin = EuroSpinBox()
        self.alternative_spin.setRange(0.0, 1_000.0)
        self.alternative_spin.setDecimals(2)
        self.alternative_spin.setSingleStep(0.25)
        self.alternative_spin.setSuffix(" €")
        self.alternative_spin.setKeyboardTracking(False)
        self.alternative_spin.setAccessibleName(
            "Custo por ocorrência da alternativa mais económica"
        )
        self.alternative_hint = QLabel()
        self.alternative_hint.setObjectName("Subtle")
        self.alternative_hint.setWordWrap(True)

        self.price_basis_value = QLabel("Preço atual constante")
        self.price_basis_value.setObjectName("SimulationControlValue")
        self.price_basis_value.setAccessibleName(
            "O preço definido é mantido constante em todos os prazos"
        )
        self.salary_share_value = QLabel("A calcular…")
        self.salary_share_value.setObjectName("SimulationControlValue")
        self.salary_share_value.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.salary_share_value.setAccessibleName(
            "Percentagem automática do salário usada por este hábito"
        )
        self.salary_share_formula = QLabel()
        self.salary_share_formula.setObjectName("Subtle")
        self.salary_share_formula.setWordWrap(True)

        controls = QGridLayout()
        controls.setContentsMargins(0, 0, 0, 0)
        controls.setHorizontalSpacing(12)
        controls.setVerticalSpacing(7)
        controls.addWidget(self.amount_title, 0, 0)
        controls.addWidget(self.amount_value_label, 0, 1)
        controls.addWidget(QLabel("Valor exato"), 0, 2)
        controls.addWidget(self.amount_spin, 0, 3)
        controls.addWidget(self.amount_slider, 1, 0, 1, 4)
        controls.addWidget(self.frequency_title, 2, 0)
        controls.addWidget(self.frequency_value_label, 2, 1)
        controls.addWidget(QLabel("Unidade"), 2, 2)
        controls.addWidget(self.frequency_unit, 2, 3)
        controls.addWidget(self.frequency_slider, 3, 0, 1, 4)
        controls.addWidget(QLabel("Alternativa realista"), 4, 0)
        controls.addWidget(self.alternative_name_label, 4, 1)
        controls.addWidget(QLabel("Custo por ocorrência"), 4, 2)
        controls.addWidget(self.alternative_spin, 4, 3)
        controls.addWidget(self.alternative_hint, 5, 0, 1, 4)
        controls.addWidget(QLabel("Base do custo"), 6, 0)
        controls.addWidget(self.price_basis_value, 6, 1)
        controls.addWidget(QLabel("Peso no salário"), 6, 2)
        controls.addWidget(self.salary_share_value, 6, 3)
        controls.addWidget(self.salary_share_formula, 7, 0, 1, 4)
        controls.setColumnStretch(0, 1)
        controls.setColumnStretch(1, 1)
        controls.setColumnStretch(2, 1)
        controls.setColumnStretch(3, 1)
        self.main_card.v.addLayout(controls)

        self.quick_actions_wrap = QWidget()
        quick_actions = FlowLayout(
            self.quick_actions_wrap,
            margin=0,
            hspacing=8,
            vspacing=8,
        )
        self.preview_group = QButtonGroup(self)
        self.preview_group.setExclusive(True)
        self.current_preview_btn = QPushButton("Cenário atual")
        self.current_preview_btn.setCheckable(True)
        self.current_preview_btn.setChecked(True)
        self.current_preview_btn.setProperty("previewKind", "current")
        self.reduce_25_btn = QPushButton("Comparar −25%")
        self.reduce_25_btn.setToolTip(
            "Mostra o resultado com menos 25% de ocorrências sem alterar o cenário."
        )
        self.reduce_25_btn.setAccessibleName(
            "Comparar menos vinte e cinco por cento de ocorrências"
        )
        self.reduce_25_btn.setCheckable(True)
        self.reduce_25_btn.setProperty("previewKind", "reduce_25")
        self.reduce_50_btn = QPushButton("Comparar metade")
        self.reduce_50_btn.setToolTip(
            "Mostra o resultado com metade das ocorrências sem alterar o cenário."
        )
        self.reduce_50_btn.setAccessibleName("Comparar metade das ocorrências")
        self.reduce_50_btn.setCheckable(True)
        self.reduce_50_btn.setProperty("previewKind", "half")
        self.alternative_preview_btn = QPushButton("Comparar alternativa")
        self.alternative_preview_btn.setToolTip(
            "Mostra o custo da alternativa indicada sem alterar os valores atuais."
        )
        self.alternative_preview_btn.setCheckable(True)
        self.alternative_preview_btn.setProperty("previewKind", "alternative")
        self.eliminate_btn = QPushButton("Comparar eliminar")
        self.eliminate_btn.setCheckable(True)
        self.eliminate_btn.setProperty("previewKind", "eliminate")
        self.apply_preview_btn = QPushButton("Aplicar ao cenário")
        self.apply_preview_btn.setObjectName("Primary")
        self.apply_preview_btn.hide()
        self.save_plan_btn = QPushButton("🎯 Guardar como plano")
        self.save_plan_btn.setObjectName("Primary")
        self.save_plan_btn.setToolTip(
            "Guardar este cenário e acompanhar o custo planeado face ao custo real"
        )
        self.save_plan_btn.hide()
        self.reset_preset_btn = QPushButton("↺ Repor hábito")
        self.reset_preset_btn.setObjectName("CardAction")
        for button in (
            self.current_preview_btn,
            self.reduce_25_btn,
            self.reduce_50_btn,
            self.alternative_preview_btn,
            self.eliminate_btn,
        ):
            self.preview_group.addButton(button)
        for button in (
            self.current_preview_btn,
            self.reduce_25_btn,
            self.reduce_50_btn,
            self.alternative_preview_btn,
            self.eliminate_btn,
            self.apply_preview_btn,
            self.save_plan_btn,
            self.reset_preset_btn,
        ):
            quick_actions.addWidget(button)
        self.main_card.fit_widget_height(self.quick_actions_wrap)
        self.main_card.v.addWidget(self.quick_actions_wrap)

        self.preview_detail = QLabel()
        self.preview_detail.setObjectName("SimulationChartDetail")
        self.preview_detail.setWordWrap(True)
        self.preview_detail.hide()
        self.main_card.v.addWidget(self.preview_detail)

        self.gambling_panel = QFrame()
        self.gambling_panel.setObjectName("GamblingPanel")
        gambling_layout = QGridLayout(self.gambling_panel)
        gambling_layout.setContentsMargins(12, 10, 12, 10)
        gambling_layout.setHorizontalSpacing(12)
        gambling_layout.setVerticalSpacing(6)
        self.win_probability = ScrollPassthroughSlider(Qt.Horizontal)
        self.win_probability.setRange(1, 49)
        self.win_probability.setSingleStep(1)
        self.win_probability.setTickInterval(8)
        self.win_probability.setTickPosition(QSlider.TicksBelow)
        self.win_probability.setAccessibleName("Probabilidade estimada de ganhar")
        self.win_probability_label = QLabel()
        self.win_probability_label.setObjectName("SimulationControlValue")
        self.payout_multiplier = QDoubleSpinBox()
        self.payout_multiplier.setRange(1.0, 2.0)
        self.payout_multiplier.setDecimals(2)
        self.payout_multiplier.setSingleStep(0.05)
        self.payout_multiplier.setSuffix(" ×")
        self.payout_multiplier.setKeyboardTracking(False)
        self.payout_multiplier.setAccessibleName("Multiplicador médio quando ganha")
        gambling_layout.addWidget(QLabel("🎲 Hipótese de vitória"), 0, 0)
        gambling_layout.addWidget(self.win_probability_label, 0, 1)
        gambling_layout.addWidget(QLabel("Retorno quando ganha"), 0, 2)
        gambling_layout.addWidget(self.payout_multiplier, 0, 3)
        gambling_layout.addWidget(self.win_probability, 1, 0, 1, 4)
        self.gambling_formula = QLabel()
        self.gambling_formula.setObjectName("Subtle")
        self.gambling_formula.setWordWrap(True)
        gambling_layout.addWidget(self.gambling_formula, 2, 0, 1, 4)
        self.main_card.v.addWidget(self.gambling_panel)

        self.gambling_warning = QLabel(
            "⚠️ A probabilidade e o retorno são hipóteses editáveis. O valor esperado "
            "não prevê uma sessão individual e não transforma jogo em rendimento seguro."
        )
        self.gambling_warning.setObjectName("SimulationWarning")
        self.gambling_warning.setWordWrap(True)
        self.main_card.v.addWidget(self.gambling_warning)
        self.main_card.v.addWidget(self._divider())

        self.result_title = QLabel("3 · Resultado vivo")
        self.result_title.setObjectName("SimulationSectionTitle")
        self.main_card.fit_label_height(self.result_title)
        self.main_card.v.addWidget(self.result_title)

        self.stats_grid = QGridLayout()
        self.stats_grid.setContentsMargins(0, 0, 0, 0)
        self.stats_grid.setHorizontalSpacing(8)
        self.stats_grid.setVerticalSpacing(8)
        self.stat_widgets: dict[str, tuple[QFrame, QLabel, QLabel]] = {}
        for index, (key, title, tone) in enumerate(
            (
                ("month", "Custo por mês", "neutral"),
                ("year", "Custo por ano", "warning"),
                ("ten", "Custo em 10 anos", "negative"),
                ("thirty", "Potencial em 30 anos", "positive"),
            )
        ):
            frame, value, detail = self._stat_widget(title, tone)
            self.stat_widgets[key] = (frame, value, detail)
            self.stats_grid.addWidget(frame, index // 4, index % 4)
        self.main_card.v.addLayout(self.stats_grid)
        self.register_responsive_grid(
            self.stats_grid, max_columns=4, min_column_width=160
        )

        self.income_effort_label = QLabel()
        self.income_effort_label.setWordWrap(True)
        self.income_effort_label.setAccessibleName(
            "Peso do hábito no rendimento mensal"
        )
        self.main_card.fit_label_height(
            self.income_effort_label,
            word_wrap=True,
        )
        self.income_effort = QProgressBar()
        self.income_effort.setObjectName("MetricProgress")
        self.income_effort.setRange(0, 100)
        self.income_effort.setTextVisible(False)
        self.income_effort.setAccessibleName(
            "Percentagem do rendimento usada pelo hábito"
        )
        self.main_card.v.addWidget(self.income_effort_label)
        self.main_card.v.addWidget(self.income_effort)

        self.chart = HabitImpactChart(lambda: self.app.theme)
        self.chart.configure_details(
            lambda _item: {
                "title": "Impacto acumulado do hábito",
                "explanation": (
                    "Compara o custo de manter o hábito com a redução de 25%, "
                    "metade da frequência e a alternativa escolhida."
                ),
            },
            action_label="Selecionar horizonte",
        )
        self.chart.horizon_selected.connect(self.select_horizon)
        self.main_card.v.addWidget(self.chart)
        self.chart_detail = QLabel()
        self.chart_detail.setObjectName("SimulationChartDetail")
        self.chart_detail.setWordWrap(True)
        self.main_card.fit_label_height(self.chart_detail, word_wrap=True)
        self.main_card.v.addWidget(self.chart_detail)

        self.goal_panel = QFrame()
        self.goal_panel.setObjectName("StatusBanner")
        self.goal_panel.setProperty("tone", "positive")
        goal_layout = QGridLayout(self.goal_panel)
        goal_layout.setContentsMargins(13, 10, 13, 10)
        goal_layout.setHorizontalSpacing(10)
        goal_layout.setVerticalSpacing(4)
        self.goal_icon = QLabel("🎯")
        self.goal_icon.setObjectName("StatusBannerIcon")
        self.goal_icon.setProperty("tone", "positive")
        self.goal_icon.setAlignment(Qt.AlignCenter)
        self.goal_icon.setFixedSize(42, 42)
        self.goal_title = QLabel("Impacto num objetivo")
        self.goal_title.setObjectName("StatusBannerTitle")
        self.goal_combo = QComboBox()
        self.goal_combo.setMinimumWidth(190)
        self.goal_combo.setAccessibleName("Objetivo usado nesta simulação")
        self.goal_detail = QLabel()
        self.goal_detail.setObjectName("StatusBannerDescription")
        self.goal_detail.setWordWrap(True)
        self.open_goals_btn = QPushButton("Abrir objetivos")
        self.open_goals_btn.setObjectName("CardAction")
        goal_layout.addWidget(self.goal_icon, 0, 0, 2, 1)
        goal_layout.addWidget(self.goal_title, 0, 1)
        goal_layout.addWidget(self.goal_combo, 0, 2)
        goal_layout.addWidget(self.open_goals_btn, 0, 3)
        goal_layout.addWidget(self.goal_detail, 1, 1, 1, 3)
        goal_layout.setColumnStretch(1, 1)
        self.main_card.v.addWidget(self.goal_panel)

        self.results_table = QTableWidget(0, 7)
        self.results_table.setProperty("columnPersistenceId", "simulations.results")
        self.results_table.setObjectName("HabitSimulationTable")
        self.results_table.setHorizontalHeaderLabels(
            [
                "Prazo",
                "Atual",
                "−25% ocorrências",
                "Metade",
                "Alternativa",
                "Poupança real",
                "Se poupado e investido",
            ]
        )
        self.results_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.results_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.results_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.results_table.setAlternatingRowColors(True)
        self.results_table.setWordWrap(False)
        self.results_table.verticalHeader().setVisible(False)
        self.results_table.verticalHeader().setDefaultSectionSize(42)
        self.results_table.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        header = self.results_table.horizontalHeader()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        for column in range(1, 6):
            header.setSectionResizeMode(column, QHeaderView.Interactive)
        header.setSectionResizeMode(6, QHeaderView.Stretch)
        self.results_table.setColumnWidth(0, 100)
        self.results_table.setColumnWidth(1, 145)
        self.results_table.setColumnWidth(2, 155)
        self.results_table.setColumnWidth(3, 145)
        self.results_table.setColumnWidth(4, 155)
        self.results_table.setColumnWidth(5, 160)
        self.results_table.setColumnWidth(6, 205)
        self.results_table.setMinimumHeight(365)
        self.results_table.setMaximumHeight(365)
        self.main_card.v.addWidget(self.results_table)

        self.insight_banner = QFrame()
        self.insight_banner.setObjectName("StatusBanner")
        insight_layout = QHBoxLayout(self.insight_banner)
        insight_layout.setContentsMargins(13, 10, 13, 10)
        insight_layout.setSpacing(10)
        self.insight_icon = QLabel("💡")
        self.insight_icon.setObjectName("StatusBannerIcon")
        self.insight_icon.setAlignment(Qt.AlignCenter)
        self.insight_icon.setFixedSize(42, 42)
        insight_layout.addWidget(self.insight_icon)
        insight_text = QVBoxLayout()
        insight_text.setContentsMargins(0, 0, 0, 0)
        insight_text.setSpacing(2)
        self.insight_title = QLabel("Ajuste inteligente")
        self.insight_title.setObjectName("StatusBannerTitle")
        self.insight_description = QLabel()
        self.insight_description.setObjectName("StatusBannerDescription")
        self.insight_description.setWordWrap(True)
        insight_text.addWidget(self.insight_title)
        insight_text.addWidget(self.insight_description)
        insight_layout.addLayout(insight_text, 1)
        self.main_card.v.addWidget(self.insight_banner)

        methodology = QLabel(
            "Metodologia: o preço escolhido mantém-se constante, em euros de hoje. "
            "O gráfico usa 360 meses numa escala temporal real e compara o cenário atual, "
            "−25%, metade e uma alternativa editável. Só a diferença de custo é tratada "
            "como poupança. A projeção investida usa uma hipótese técnica fixa de 5%/ano; "
            "a faixa provável é ±15% ou ±8 p.p. no jogo. A média real usa correspondências "
            "explicáveis dos últimos seis meses e indica a qualidade da amostra. O impacto "
            "nos objetivos soma a poupança à contribuição mensal, sem assumir rentabilidade. "
            "Valores indicativos, não garantias."
        )
        methodology.setObjectName("Subtle")
        methodology.setWordWrap(True)
        self.main_card.v.addWidget(methodology)
        self.register_filter_panel(self.main_card)
        self.body.addWidget(self.view_bar)
        self.body.addWidget(self.main_card)
        self.plans_card = HabitPlansCard(self.app)
        self.body.addWidget(self.plans_card)

        # -------------------------------------------------------------
        # FIRE Planning Card
        # -------------------------------------------------------------
        self.fire_card = GlassCard(
            "🔥 Planeador de Independência Financeira (FIRE) & Guardrails Guyton-Klinger",
            strong=True,
            accent="#FF9500",
        )
        self.fire_card.setProperty("pageCardKey", "fire_planning")
        fire_intro = QLabel(
            "Modela a tua data de independência financeira (FIRE), calcula os marcos de despesas "
            "(Lean, Standard, Fat, Coast), testa a resiliência a quedas de mercado nos primeiros anos "
            "e simula a taxa de levantamento dinâmico com regras de Guyton-Klinger."
        )
        fire_intro.setObjectName("Subtle")
        fire_intro.setWordWrap(True)
        self.fire_card.v.addWidget(fire_intro)

        fire_form = QGridLayout()
        fire_form.setHorizontalSpacing(14)
        fire_form.setVerticalSpacing(8)

        self.fire_capital_spin = EuroSpinBox(self)
        self.fire_capital_spin.setRange(0.0, 50_000_000.0)
        self.fire_capital_spin.setSingleStep(5_000.0)
        self.fire_capital_spin.setValue(100_000.0)

        self.fire_expenses_spin = EuroSpinBox(self)
        self.fire_expenses_spin.setRange(0.0, 1_000_000.0)
        self.fire_expenses_spin.setSingleStep(1_000.0)
        self.fire_expenses_spin.setValue(18_000.0)

        self.fire_savings_spin = EuroSpinBox(self)
        self.fire_savings_spin.setRange(0.0, 1_000_000.0)
        self.fire_savings_spin.setSingleStep(1_000.0)
        self.fire_savings_spin.setValue(6_000.0)

        self.fire_growth_spin = QDoubleSpinBox(self)
        # BUG-SIM-01: negative real returns (stagflation) must be simulable.
        self.fire_growth_spin.setRange(-10.0, 15.0)
        self.fire_growth_spin.setSingleStep(0.5)
        self.fire_growth_spin.setValue(4.5)
        self.fire_growth_spin.setSuffix(" %/ano")

        self.fire_swr_spin = QDoubleSpinBox(self)
        self.fire_swr_spin.setRange(1.0, 10.0)
        self.fire_swr_spin.setSingleStep(0.25)
        self.fire_swr_spin.setValue(4.0)
        self.fire_swr_spin.setSuffix(" %/ano")

        fire_form.addWidget(QLabel("Capital Investido Atual:"), 0, 0)
        fire_form.addWidget(self.fire_capital_spin, 0, 1)
        fire_form.addWidget(QLabel("Despesa Anual Pretendida:"), 0, 2)
        fire_form.addWidget(self.fire_expenses_spin, 0, 3)

        fire_form.addWidget(QLabel("Poupança Anual Prevista:"), 1, 0)
        fire_form.addWidget(self.fire_savings_spin, 1, 1)
        fire_form.addWidget(QLabel("Retorno Real Anual (%):"), 1, 2)
        fire_form.addWidget(self.fire_growth_spin, 1, 3)

        fire_form.addWidget(QLabel("Taxa Segura de Retirada (SWR):"), 2, 0)
        fire_form.addWidget(self.fire_swr_spin, 2, 1)

        self.fire_card.v.addLayout(fire_form)

        # Milestones grid
        self.fire_milestones_grid = QGridLayout()
        self.fire_milestones_grid.setContentsMargins(0, 0, 0, 0)
        self.fire_milestones_grid.setSpacing(8)
        self.fire_card.v.addLayout(self.fire_milestones_grid)
        self.register_responsive_grid(
            self.fire_milestones_grid, max_columns=4, min_column_width=180
        )

        # GK Guardrails Panel
        self.fire_gk_card = GlassCard(
            "🛡️ Guardrails Dinâmicos de Guyton-Klinger", strong=False
        )
        self.fire_gk_label = QLabel()
        self.fire_gk_label.setWordWrap(True)
        self.fire_gk_card.v.addWidget(self.fire_gk_label)
        self.fire_card.v.addWidget(self.fire_gk_card)

        # SRR Stress Test Panel
        self.fire_srr_card = GlassCard(
            "💥 Stress Test: Risco de Sequência de Retornos (SRR - 30 anos)",
            strong=False,
        )
        self.fire_srr_label = QLabel()
        self.fire_srr_label.setWordWrap(True)
        self.fire_srr_card.v.addWidget(self.fire_srr_label)
        self.fire_card.v.addWidget(self.fire_srr_card)

        self.body.addWidget(self.fire_card)

        for spin in (
            self.fire_capital_spin,
            self.fire_expenses_spin,
            self.fire_savings_spin,
            self.fire_growth_spin,
            self.fire_swr_spin,
        ):
            # BUG-SIM-02: the spinboxes queue a 250 ms single-shot timer
            # instead of recomputing milestones and both 30-year simulations
            # on every tick (main-thread lock-up).
            spin.valueChanged.connect(self._queue_fire_recalc)

        self._fire_recalc_timer = QTimer(self)
        self._fire_recalc_timer.setSingleShot(True)
        self._fire_recalc_timer.setInterval(250)
        self._fire_recalc_timer.timeout.connect(self._update_fire_calculations)

        self._persist_timer = QTimer(self)
        self._persist_timer.setSingleShot(True)
        self._persist_timer.setInterval(280)
        self._persist_timer.timeout.connect(self._persist_state_now)

        self.habit_group.buttonClicked.connect(self._habit_button_clicked)
        self.amount_slider.valueChanged.connect(self._amount_slider_changed)
        self.amount_spin.valueChanged.connect(self._amount_spin_changed)
        self.frequency_slider.valueChanged.connect(self._control_changed)
        self.frequency_unit.currentIndexChanged.connect(self._control_changed)
        self.alternative_spin.valueChanged.connect(self._control_changed)
        self.win_probability.valueChanged.connect(self._control_changed)
        self.payout_multiplier.valueChanged.connect(self._control_changed)
        self.preview_group.buttonClicked.connect(self._preview_button_clicked)
        self.real_data_preview_btn.clicked.connect(
            lambda checked=False: self._set_preview("real" if checked else "current")
        )
        self.audit_movements_btn.clicked.connect(self._open_habit_audit)
        self.apply_preview_btn.clicked.connect(self._apply_preview)
        self.save_plan_btn.clicked.connect(self._save_current_preview_as_plan)
        self.reset_preset_btn.clicked.connect(self.reset_current_preset)
        self.goal_combo.currentIndexChanged.connect(self._goal_changed)
        self.open_goals_btn.clicked.connect(lambda: self.app.goto("objetivos"))
        self.view_group.buttonClicked.connect(
            lambda button: self._set_internal_view(
                str(button.property("viewKey") or "simulate")
            )
        )
        self.plans_card.plans_changed.connect(self._plan_count_changed)

        self._updating_controls = False
        self._select_habit(self._selected_key, capture_current=False, persist=False)
        self.refresh()
        self._sync_internal_view()

    @staticmethod
    def _divider() -> QFrame:
        divider = QFrame()
        divider.setObjectName("SimulationDivider")
        divider.setFixedHeight(1)
        return divider

    @staticmethod
    def _stat_widget(title: str, tone: str) -> tuple[QFrame, QLabel, QLabel]:
        frame = QFrame()
        frame.setObjectName("SimulationStat")
        frame.setProperty("tone", tone)
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(3)
        title_label = QLabel(title)
        title_label.setObjectName("SimulationStatTitle")
        value = QLabel("—")
        value.setObjectName("SimulationStatValue")
        value.setWordWrap(True)
        detail = QLabel()
        detail.setObjectName("SimulationStatDetail")
        detail.setWordWrap(True)
        layout.addWidget(title_label)
        layout.addWidget(value)
        layout.addWidget(detail)
        return frame, value, detail

    def _saved_state(self) -> dict[str, Any]:
        try:
            parsed = json.loads(self.repo.setting(self.SETTINGS_KEY, "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def _saved_audit_rules(self) -> dict[str, dict[str, Any]]:
        try:
            parsed = json.loads(self.repo.setting(self.AUDIT_SETTINGS_KEY, "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            parsed = {}
        if not isinstance(parsed, dict):
            return {}
        return {
            str(key): normalise_habit_audit_rules(value)
            for key, value in parsed.items()
            if str(key) in HABIT_PRESETS_BY_KEY and isinstance(value, dict)
        }

    def _persist_audit_rules(self) -> None:
        self.repo.set_setting(
            self.AUDIT_SETTINGS_KEY,
            json.dumps(self._habit_audit_rules, ensure_ascii=False, sort_keys=True),
        )

    def _current_preset(self) -> HabitPreset:
        return HABIT_PRESETS_BY_KEY.get(self._selected_key, HABIT_PRESETS[0])

    def _capture_current_state(self) -> None:
        if self._updating_controls or self._selected_key not in HABIT_PRESETS_BY_KEY:
            return
        self._habit_states[self._selected_key] = {
            "unit_cost": self.amount_spin.value(),
            "alternative_unit_cost": self.alternative_spin.value(),
            "frequency": self.frequency_slider.value(),
            "frequency_unit": self.frequency_unit.currentData() or "week",
            "win_probability_pct": self.win_probability.value(),
            "payout_multiplier": self.payout_multiplier.value(),
        }

    def _persist_state_now(self) -> None:
        self._capture_current_state()
        self.repo.set_setting(
            self.SETTINGS_KEY,
            json.dumps(
                {
                    "selected": self._selected_key,
                    "selected_goal_id": self._selected_goal_id,
                    "view": self._current_view,
                    "habits": self._habit_states,
                },
                ensure_ascii=False,
            ),
        )

    def flush_pending_state(self) -> bool:
        """Persist the latest slider state even when its debounce is pending."""

        if self._persist_timer.isActive():
            self._persist_timer.stop()
            self._persist_state_now()
        return True

    def _schedule_persist(self) -> None:
        if not self._updating_controls:
            self._persist_timer.start()

    def _set_internal_view(self, view_key: str) -> None:
        selected = str(view_key or "simulate")
        if selected not in {"simulate", "plans", "fire"}:
            selected = "simulate"
        self._current_view = selected
        if selected == "plans":
            self.plans_card.refresh()
        elif selected == "fire":
            self._update_fire_calculations()
        self._sync_internal_view()
        self._schedule_persist()

    def _sync_internal_view(self) -> None:
        simulate_available = self.card_should_show(self.main_card)
        plans_available = self.card_should_show(self.plans_card)
        fire_available = self.card_should_show(self.fire_card)
        if (
            self._current_view == "simulate"
            and not simulate_available
            and plans_available
        ):
            self._current_view = "plans"
        elif (
            self._current_view == "plans" and not plans_available and simulate_available
        ):
            self._current_view = "simulate"
        self.main_card.setVisible(
            simulate_available and self._current_view == "simulate"
        )
        self.plans_card.setVisible(plans_available and self._current_view == "plans")
        self.fire_card.setVisible(fire_available and self._current_view == "fire")
        self.simulate_view_btn.setEnabled(simulate_available)
        self.plans_view_btn.setEnabled(plans_available)
        self.fire_view_btn.setEnabled(fire_available)
        for key, button in (
            ("simulate", self.simulate_view_btn),
            ("plans", self.plans_view_btn),
            ("fire", self.fire_view_btn),
        ):
            button.blockSignals(True)
            button.setChecked(self._current_view == key)
            button.blockSignals(False)
        self.updateGeometry()
        sync = getattr(self.app, "sync_content_size", None)
        if callable(sync):
            QTimer.singleShot(0, sync)

    def _queue_fire_recalc(self, *_args) -> None:
        self._fire_recalc_timer.start()

    def _update_fire_calculations(self) -> None:
        self._fire_recalc_timer.stop()
        invested = self.fire_capital_spin.value()
        expenses = self.fire_expenses_spin.value()
        savings = self.fire_savings_spin.value()
        growth = self.fire_growth_spin.value()
        swr = self.fire_swr_spin.value()

        milestones = FirePlanningEngine.calculate_fire_milestones(
            current_invested=invested,
            annual_expenses=expenses,
            annual_savings=savings,
            real_growth_rate_pct=growth,
            swr_pct=swr,
        )

        clear_layout_recursive(self.fire_milestones_grid)

        keys_and_meta = (
            ("standard", BLUE, "🎯"),
            ("lean", GREEN, "🥗"),
            ("fat", PURPLE, "💎"),
            ("coast", ORANGE, "⛵"),
        )
        for idx, (m_key, color, icon) in enumerate(keys_and_meta):
            m = milestones.get(m_key)

            if m:
                if expenses <= 0:
                    if invested > 0:
                        years_text = "✅ Objetivo Atingido"
                        sub = "100% · ✅ Objetivo Atingido"
                    else:
                        years_text = "Indica as despesas"
                        sub = "0% · Indica as despesas"
                else:
                    if m.years_to_goal is not None:
                        if m.years_to_goal == 0:
                            years_text = "✅ Atingido!"
                        else:
                            years_text = f"{round(m.years_to_goal, 1)} anos"
                    else:
                        years_text = (
                            "✅ Atingido!"
                            if invested >= m.target_capital and m.target_capital > 0
                            else "Inatingível (sem poupança)"
                        )
                    sub = f"{m.progress_pct}% · {years_text}"
                card = MetricCard(
                    m.name,
                    eur(m.target_capital, self.app.theme.privacy),
                    sub,
                    color,
                    icon,
                )
                self.fire_milestones_grid.addWidget(card, 0, idx)

        initial_withdrawal = expenses
        upper_limit = round(initial_withdrawal * 1.10, 2)
        lower_limit = round(initial_withdrawal * 0.90, 2)
        guardrail_anchor = expenses / invested * 100 if invested > 0 else None
        swr_upper_trigger = guardrail_anchor * 1.20 if guardrail_anchor is not None else None
        swr_lower_trigger = guardrail_anchor * 0.80 if guardrail_anchor is not None else None
        fire_privacy = self.app.theme.privacy
        # BUG-SIM-01: the Guyton-Klinger engine was never invoked by the UI;
        # run the real drawdown simulation so the guardrails panel shows
        # computed results instead of static rule descriptions only.
        # The control supplies a real return. GK uses nominal cash/returns, so
        # convert once; SRR below stays entirely in constant purchasing power.
        assumed_inflation_pct = 2.0
        nominal_growth = ((1.0 + growth / 100) * (1.0 + assumed_inflation_pct / 100) - 1.0) * 100
        gk_history = FirePlanningEngine.simulate_guyton_klinger_drawdown(
            initial_capital=invested,
            initial_annual_withdrawal=expenses,
            annual_returns=[nominal_growth] * 30,
            annual_inflation_rates=[assumed_inflation_pct] * 30,
        )
        gk_depleted_year = next(
            (row["year"] for row in gk_history if row["ending_capital"] <= 0.0),
            None,
        )
        gk_status = (
            f"⚠️ Esgotou no ano {gk_depleted_year}"
            if gk_depleted_year is not None
            else "✅ Sobreviveu 30 anos"
        )
        gk_final = gk_history[-1]["ending_capital"] if gk_history else invested
        gk_activations = sum(
            1
            for row in gk_history
            if row["rule_triggered"].startswith(("Preservação", "Prosperidade"))
        )
        anchor_text = percent(guardrail_anchor, fire_privacy) if guardrail_anchor is not None else "N/D"
        upper_text = percent(swr_upper_trigger, fire_privacy) if swr_upper_trigger is not None else "N/D"
        lower_text = percent(swr_lower_trigger, fire_privacy) if swr_lower_trigger is not None else "N/D"
        activation_text = "•••" if fire_privacy else str(gk_activations)
        self.fire_gk_label.setText(
            f"<b>Levantamento Base:</b> {eur(initial_withdrawal, fire_privacy)}/ano "
            f"(taxa inicial efetiva {anchor_text}; SWR escolhido serve o objetivo FIRE).<br><br>"
            f"• <b>Preservação:</b> Acima de {upper_text}, reduz o orçamento em 10% "
            f"(exemplo inicial {eur(lower_limit, fire_privacy)}/ano). Esta regra termina nos últimos 15 anos.<br>"
            f"• <b>Prosperidade:</b> Abaixo de {lower_text}, aumenta o orçamento em 10% "
            f"(exemplo inicial {eur(upper_limit, fire_privacy)}/ano), incluindo os últimos 15 anos.<br>"
            "• <b>Inflação:</b> Aplica a inflação do ano anterior; após retorno negativo, "
            "omite o aumento apenas se a taxa proposta exceder a inicial. Sem recuperação posterior.<br>"
            "Variante ilustrativa de uma carteira, com piso real de 80% para cortes; "
            "não reproduz a gestão multiativo do estudo.<br>"
            f"<b>Simulação Guyton-Klinger (30 anos, retorno real {growth}%/ano, "
            f"nominal {nominal_growth:.2f}% e inflação {assumed_inflation_pct}%):</b> "
            f"{gk_status} · Saldo nominal final: {eur(gk_final, fire_privacy)} · "
            f"Guardrails acionados em {activation_text} ano(s)."
        )

        srr_stable = FirePlanningEngine.simulate_sequence_of_returns_risk(
            initial_capital=invested,
            initial_annual_withdrawal=expenses,
            duration_years=30,
            average_real_return_pct=growth,
            early_crash_scenario="baseline_steady",
        )
        srr_crash = FirePlanningEngine.simulate_sequence_of_returns_risk(
            initial_capital=invested,
            initial_annual_withdrawal=expenses,
            duration_years=30,
            average_real_return_pct=growth,
            early_crash_scenario="dotcom_2000",
        )

        stable_status = (
            "✅ Sobreviveu 30 anos"
            if srr_stable.portfolio_survived
            else f"⚠️ Esgotou no ano {srr_stable.depletion_year}"
        )
        crash_status = (
            "✅ Sobreviveu 30 anos"
            if srr_crash.portfolio_survived
            else f"⚠️ Esgotou no ano {srr_crash.depletion_year}"
        )
        shock_text = ", ".join(f"{value:g}%" for value in srr_crash.scenario_returns_pct[:3])
        self.fire_srr_label.setText(
            f"<b>Cenário A (Retorno real estável a {growth}%/ano):</b> {stable_status} · "
            f"Saldo real aos 30 anos: <b>{eur(srr_stable.final_capital_real, fire_privacy)}</b>.<br><br>"
            f"<b>Cenário B (Choques reais ilustrativos: {shock_text}):</b> {crash_status} · "
            f"Saldo real residual: <b>{eur(srr_crash.final_capital_real, fire_privacy)}</b>.<br>"
            "Levantamentos e saldos em euros de hoje; os choques inspiram-se em crises, "
            "não são uma série histórica nominal auditada. A inflação converte apenas o saldo nominal final. "
            "Uma reserva de liquidez pode reduzir vendas forçadas; a simulação não garante esse resultado."
        )

    def _plan_count_changed(self, count: int) -> None:
        self.plans_view_btn.setText(f"📈 Planos ({max(0, int(count))})")

    def apply_card_preferences(self) -> None:
        super().apply_card_preferences()
        self._sync_internal_view()

    def _habit_button_clicked(self, button: QPushButton) -> None:
        self._select_habit(str(button.property("habitKey") or ""))

    def _select_habit(
        self,
        key: str,
        *,
        capture_current: bool = True,
        persist: bool = True,
    ) -> None:
        if key not in HABIT_PRESETS_BY_KEY:
            return
        if capture_current:
            self._capture_current_state()
        self._preview_kind = "current"
        self._selected_key = key
        preset = self._current_preset()
        state = self._habit_states.get(key, {})
        self._updating_controls = True
        try:
            self.habit_buttons[key].setChecked(True)
            cost = safe_float(state.get("unit_cost"), preset.unit_cost)
            alternative_cost = safe_float(
                state.get("alternative_unit_cost"),
                preset.alternative_unit_cost,
            )
            frequency = safe_float(state.get("frequency"), preset.frequency)
            unit = str(state.get("frequency_unit") or preset.frequency_unit)
            win_probability = int(
                round(
                    safe_float(
                        state.get("win_probability_pct"),
                        preset.win_probability_pct or 30.0,
                    )
                )
            )
            payout = safe_float(
                state.get("payout_multiplier"),
                preset.payout_multiplier,
            )
            self.amount_spin.setValue(max(0.0, min(1_000.0, cost)))
            self.amount_slider.setValue(round(self.amount_spin.value() * 100.0))
            self.alternative_spin.setMaximum(self.amount_spin.value())
            self.alternative_spin.setValue(
                0.0
                if preset.gambling
                else max(0.0, min(self.amount_spin.value(), alternative_cost))
            )
            self.alternative_spin.setEnabled(not preset.gambling)
            self.alternative_name_label.setText(preset.alternative_name)
            self.frequency_slider.setValue(self._integer_frequency(frequency))
            unit_index = self.frequency_unit.findData(unit)
            self.frequency_unit.setCurrentIndex(max(0, unit_index))
            self.win_probability.setValue(max(1, min(49, win_probability)))
            self.payout_multiplier.setValue(max(1.0, min(2.0, payout)))
            self.gambling_panel.setVisible(preset.gambling)
            self.gambling_warning.setVisible(preset.gambling)
            self.preset_description.setText(
                f"{preset.display_name} · {preset.description} · Alternativa: "
                f"{preset.alternative_name}."
            )
        finally:
            self._updating_controls = False
        self._sync_preview_controls()
        self._load_habit_calibration()
        self._recalculate(persist=persist)

    def _amount_slider_changed(self, value: int) -> None:
        if self._updating_controls:
            return
        self._clear_real_preview()
        self._updating_controls = True
        try:
            self.amount_spin.setValue(value / 100.0)
            self._sync_alternative_limit()
        finally:
            self._updating_controls = False
        self._recalculate()

    def _amount_spin_changed(self, value: float) -> None:
        if self._updating_controls:
            return
        self._clear_real_preview()
        self._updating_controls = True
        try:
            self.amount_slider.setValue(round(value * 100.0))
            self._sync_alternative_limit()
        finally:
            self._updating_controls = False
        self._recalculate()

    def _control_changed(self, *_args) -> None:
        if not self._updating_controls:
            self._clear_real_preview()
            self._recalculate()

    def _sync_alternative_limit(self) -> None:
        maximum = max(0.0, self.amount_spin.value())
        self.alternative_spin.setMaximum(maximum)
        if self.alternative_spin.value() > maximum:
            self.alternative_spin.setValue(maximum)

    def _preview_button_clicked(self, button: QPushButton) -> None:
        self._set_preview(str(button.property("previewKind") or "current"))

    def _clear_real_preview(self) -> None:
        if self._preview_kind == "real":
            self._preview_kind = "current"
            self._sync_preview_controls()

    def _sync_preview_controls(self) -> None:
        buttons = {
            "current": self.current_preview_btn,
            "reduce_25": self.reduce_25_btn,
            "half": self.reduce_50_btn,
            "alternative": self.alternative_preview_btn,
            "eliminate": self.eliminate_btn,
        }
        self.preview_group.setExclusive(False)
        for key, button in buttons.items():
            button.setChecked(self._preview_kind == key)
        self.preview_group.setExclusive(True)
        self.real_data_preview_btn.blockSignals(True)
        self.real_data_preview_btn.setChecked(self._preview_kind == "real")
        self.real_data_preview_btn.blockSignals(False)
        self.apply_preview_btn.setVisible(
            self._preview_kind not in {"current", "alternative"}
        )
        self.save_plan_btn.setVisible(
            self._preview_kind in {"reduce_25", "half", "alternative", "eliminate"}
        )
        self.preview_detail.setVisible(self._preview_kind != "current")

    def _set_preview(self, kind: str) -> None:
        selected = str(kind or "current")
        if selected not in {
            "current",
            "real",
            "reduce_25",
            "half",
            "alternative",
            "eliminate",
        }:
            selected = "current"
        if selected == "real" and not bool(self._habit_calibration.get("available")):
            selected = "current"
        self._preview_kind = selected
        self._sync_preview_controls()
        self._recalculate(persist=False)

    def _calibration_inputs(self, base: HabitSimulationInputs) -> HabitSimulationInputs:
        calibration = self._habit_calibration
        if not bool(calibration.get("available")):
            return base
        unit_cost = max(0.0, safe_float(calibration.get("average_unit_cost")))
        return replace(
            base,
            unit_cost=unit_cost,
            alternative_unit_cost=min(base.alternative_unit_cost, unit_cost),
            frequency=float(
                max(0, int(calibration.get("suggested_frequency", 0) or 0))
            ),
            frequency_unit=str(calibration.get("suggested_frequency_unit") or "month"),
        )

    def _apply_preview(self) -> None:
        kind = self._preview_kind
        if kind == "current":
            return
        self._updating_controls = True
        try:
            if kind == "real" and bool(self._habit_calibration.get("available")):
                cost = max(
                    0.0,
                    min(
                        1_000.0,
                        safe_float(self._habit_calibration.get("average_unit_cost")),
                    ),
                )
                frequency = max(
                    0,
                    min(
                        60,
                        int(self._habit_calibration.get("suggested_frequency", 0) or 0),
                    ),
                )
                unit = str(
                    self._habit_calibration.get("suggested_frequency_unit") or "month"
                )
                self.amount_spin.setValue(cost)
                self.amount_slider.setValue(round(cost * 100.0))
                self.frequency_slider.setValue(frequency)
                unit_index = self.frequency_unit.findData(unit)
                self.frequency_unit.setCurrentIndex(max(0, unit_index))
                self._sync_alternative_limit()
            elif kind in {"reduce_25", "half"}:
                frequencies = self._base_data.get("scenario_frequencies") or {}
                self.frequency_slider.setValue(
                    max(0, min(60, int(safe_float(frequencies.get(kind)))))
                )
            elif kind == "eliminate":
                self.frequency_slider.setValue(0)
        finally:
            self._updating_controls = False
        self._preview_kind = "current"
        self._sync_preview_controls()
        self._recalculate(persist=True)

    def _save_current_preview_as_plan(self) -> None:
        scenario_key = self._preview_kind
        if scenario_key not in {"reduce_25", "half", "alternative", "eliminate"}:
            return
        scenario = scenario_plan_values(self._base_data, scenario_key)
        dialog = HabitPlanDialog(
            habit_name=self._current_preset().name,
            scenario_key=scenario_key,
            planned_monthly_savings=scenario["planned_monthly_savings"],
            goals=list(self._goals_by_id.values()),
            selected_goal_id=self._selected_goal_id,
            parent=self,
        )
        if not dialog.exec():
            return
        values = dialog.values()
        payload = build_habit_plan_payload(
            preset_key=self._selected_key,
            simulation=self._base_data,
            scenario_key=scenario_key,
            name=str(values.get("name") or ""),
            start_date=str(values.get("start_date") or ""),
            goal_id=values.get("goal_id"),
            audit_rules=self._habit_audit_rules.get(self._selected_key, {}),
            notes=str(values.get("notes") or ""),
        )
        try:
            plan_id = self.repo.add_habit_change_plan(**payload)
            self.plans_card.refresh(select_id=plan_id)
            self._set_internal_view("plans")
            self.app.show_toast(
                "Já podes acompanhar o limite planeado e o custo real mês a mês.",
                "🎯",
                title="Plano de mudança guardado",
                tone="success",
            )
        except Exception as exc:
            log_exception("Falha ao guardar plano de mudança de hábitos", exc)
            self.app.show_toast(
                f"Erro ao guardar o plano: {exc}",
                "❌",
                title="Erro ao guardar plano",
                tone="danger",
            )

    @staticmethod
    def _month_span_label(value: str) -> str:
        text = str(value or "")
        return f"{text[5:7]}/{text[:4]}" if len(text) >= 7 else "—"

    def _load_habit_calibration(self) -> None:
        self._habit_calibration = self.analytics_service.habit_calibration(
            self._selected_key,
            6,
            audit_rules=self._habit_audit_rules.get(self._selected_key, {}),
            transaction_context=self._habit_transaction_context or None,
        )
        self._refresh_calibration_panel()

    def _open_habit_audit(self) -> None:
        if not self._habit_transaction_context:
            self._habit_transaction_context = (
                self.analytics_service.habit_transaction_context(6)
            )
        rules = self._habit_audit_rules.get(self._selected_key, {})
        rows = self.analytics_service.habit_audit_rows(
            self._selected_key,
            6,
            audit_rules=rules,
            transaction_context=self._habit_transaction_context,
        )
        dialog = HabitAuditDialog(
            habit_name=self._current_preset().name,
            rows=rows,
            audit_rules=rules,
            privacy=self.app.theme.privacy,
            parent=self,
        )
        if not dialog.exec():
            return
        self._habit_audit_rules[self._selected_key] = dialog.rules()
        self._persist_audit_rules()
        self._load_habit_calibration()
        self._set_preview("current")
        self.plans_card.refresh()
        self.app.show_toast(
            "A média e a frequência foram recalculadas sem alterar movimentos.",
            "🔍",
            title="Seleção guardada",
            tone="success",
        )

    def _refresh_calibration_panel(self) -> None:
        data = self._habit_calibration
        available = bool(data.get("available"))
        tone = str(data.get("tone") or "neutral")
        self.real_data_preview_btn.setEnabled(available)
        if available:
            count = int(data.get("transaction_count", 0) or 0)
            self.audit_movements_btn.setText(f"🔍 Rever {count} usados")
            active = int(data.get("active_months", 0) or 0)
            coverage = int(data.get("coverage_months", 0) or 0)
            frequency = safe_float(data.get("suggested_frequency"))
            unit = str(data.get("suggested_frequency_unit") or "month")
            manual = int(data.get("manual_matches", 0) or 0)
            custom = int(data.get("custom_matches", 0) or 0)
            excluded = int(data.get("excluded_matches", 0) or 0)
            revised = (
                f" · revisão pessoal: {manual + custom} incluído(s), "
                f"{excluded} excluído(s)"
                if manual or custom or excluded
                else ""
            )
            self.calibration_title.setText(
                f"A tua média real para {self._current_preset().name}"
            )
            self.calibration_confidence.setText(
                str(data.get("confidence_label") or "Confiança inicial")
            )
            self.calibration_detail.setText(
                f"{count} movimento(s) compatível(is) em {active} de {coverage} mês(es) "
                f"com dados · preço médio {eur(data.get('average_unit_cost', 0), self.app.theme.privacy)} "
                f"· gasto observado {eur(data.get('average_monthly_spend', 0), self.app.theme.privacy)}/mês "
                f"· sugestão {self._frequency_text(frequency, unit)} · "
                f"{self._month_span_label(str(data.get('period_start') or ''))}–"
                f"{self._month_span_label(str(data.get('period_end') or ''))}{revised}."
            )
        else:
            self.audit_movements_btn.setText("🔍 Rever movimentos")
            coverage = int(data.get("coverage_months", 0) or 0)
            self.calibration_title.setText("Ainda não encontrei um padrão fiável")
            self.calibration_confidence.setText("Sem correspondências")
            self.calibration_detail.setText(
                f"Analisei {coverage} mês(es) com despesas. Quando existirem movimentos "
                f"identificáveis de {self._current_preset().name.lower()}, a app calcula "
                "o preço e a frequência sem substituir os teus valores manuais."
            )
        for widget in (
            self.calibration_panel,
            self.calibration_icon,
            self.calibration_confidence,
        ):
            self._set_tone(widget, tone)

    def reset_current_preset(self) -> None:
        self._preview_kind = "current"
        self._habit_states.pop(self._selected_key, None)
        self._select_habit(
            self._selected_key,
            capture_current=False,
            persist=True,
        )

    def _reset_page_filters(self) -> None:
        self._preview_kind = "current"
        self._current_view = "simulate"
        self._habit_states.pop(HABIT_PRESETS[0].key, None)
        self._select_habit(
            HABIT_PRESETS[0].key,
            capture_current=False,
            persist=True,
        )
        self._sync_internal_view()

    def _load_financial_context(self) -> None:
        context = self.analytics_service.personal_income_context(6)
        self._average_income = safe_float(context.get("average_income"))
        self._average_savings = safe_float(context.get("average_savings"))
        self._income_reference_name = str(context.get("reference_name") or "salário")
        self._context_months = int(context.get("context_months", 0) or 0)

    def _load_goals(self) -> None:
        rows = [dict(row) for row in self.repo.goals()]
        self._goals_by_id = {
            int(safe_float(row.get("id"))): row
            for row in rows
            if int(safe_float(row.get("id"))) > 0
        }
        self.goal_combo.blockSignals(True)
        self.goal_combo.clear()
        if not rows:
            self.goal_combo.addItem("Sem objetivos registados", 0)
            self._selected_goal_id = 0
        else:
            for row in rows:
                goal_id = int(safe_float(row.get("id")))
                self.goal_combo.addItem(str(row.get("name") or "Objetivo"), goal_id)
            index = self.goal_combo.findData(self._selected_goal_id)
            if index < 0:
                index = 0
            self.goal_combo.setCurrentIndex(index)
            self._selected_goal_id = int(safe_float(self.goal_combo.currentData()))
        self.goal_combo.blockSignals(False)
        self._update_goal_panel()

    def _goal_changed(self, _index: int = -1) -> None:
        self._selected_goal_id = int(safe_float(self.goal_combo.currentData()))
        self._update_goal_panel()
        self._update_preview_detail()
        self._schedule_persist()

    @staticmethod
    def _goal_date_label(value: str) -> str:
        text = str(value or "")
        return f"{text[5:7]}/{text[:4]}" if len(text) >= 7 else "—"

    def _goal_scenario_text(self, scenario: dict[str, Any]) -> str:
        months = scenario.get("months")
        if months is None:
            return "sem contribuição"
        if int(months) <= 0:
            return "já atingido"
        completion = self._goal_date_label(str(scenario.get("completion_date") or ""))
        gained = scenario.get("months_gained")
        if bool(scenario.get("creates_plan")):
            return f"{completion} · cria plano de {int(months)} meses"
        if gained is not None and int(gained) > 0:
            return f"{completion} · {int(gained)} meses antes"
        return completion

    def _goal_impact(self) -> dict[str, Any]:
        goal = self._goals_by_id.get(self._selected_goal_id)
        if goal is None or not self._current_data:
            return {}
        return build_goal_impact(goal, self._current_data)

    def _update_goal_panel(self) -> None:
        goal = self._goals_by_id.get(self._selected_goal_id)
        if goal is None:
            self.goal_title.setText("Liga a simulação a um objetivo")
            self.goal_detail.setText(
                "Cria um objetivo na área Objetivos para veres automaticamente "
                "quantos meses cada alternativa pode antecipar."
            )
            self.goal_combo.setEnabled(False)
            self._set_tone(self.goal_panel, "neutral")
            self._set_tone(self.goal_icon, "neutral")
            return
        self.goal_combo.setEnabled(True)
        impact = self._goal_impact()
        if not impact:
            return
        scenarios = impact.get("scenarios") or {}
        self.goal_title.setText(f"Impacto em «{impact.get('goal_name', 'Objetivo')}»")
        self.goal_detail.setText(
            f"Faltam {eur(impact.get('missing_amount', 0), self.app.theme.privacy)} · "
            f"contribuição atual {eur(impact.get('base_contribution', 0), self.app.theme.privacy)}/mês. "
            f"Atual: {self._goal_scenario_text(scenarios.get('current') or {})} · "
            f"−25%: {self._goal_scenario_text(scenarios.get('reduce_25') or {})} · "
            f"metade: {self._goal_scenario_text(scenarios.get('half') or {})} · "
            f"alternativa: {self._goal_scenario_text(scenarios.get('alternative') or {})}."
        )
        tone = "positive" if safe_float(impact.get("missing_amount")) > 0 else "neutral"
        self._set_tone(self.goal_panel, tone)
        self._set_tone(self.goal_icon, tone)

    def _inputs(self) -> HabitSimulationInputs:
        preset = self._current_preset()
        return HabitSimulationInputs(
            unit_cost=self.amount_spin.value(),
            frequency=float(self.frequency_slider.value()),
            frequency_unit=str(self.frequency_unit.currentData() or "week"),
            alternative_unit_cost=self.alternative_spin.value(),
            annual_inflation_pct=DEFAULT_COST_INFLATION_PCT,
            annual_investment_rate_pct=DEFAULT_INVESTMENT_RATE_PCT,
            gambling=preset.gambling,
            win_probability_pct=float(self.win_probability.value()),
            payout_multiplier=self.payout_multiplier.value(),
            monthly_income=self._average_income,
        )

    @staticmethod
    def _integer_frequency(value: float) -> int:
        return max(0, min(60, int(max(0.0, safe_float(value)) + 0.5)))

    @classmethod
    def _frequency_text(cls, value: float, unit: str) -> str:
        integer_value = cls._integer_frequency(value)
        if integer_value == 0:
            return "0 · eliminado"
        labels = {"day": "por dia", "week": "por semana", "month": "por mês"}
        return f"{integer_value} {labels.get(unit, 'por semana')}"

    @staticmethod
    def _decimal(value: float, decimals: int = 1) -> str:
        return f"{float(value):.{max(0, int(decimals))}f}".replace(".", ",")

    @staticmethod
    def _set_tone(widget: QWidget, tone: str) -> None:
        widget.setProperty("tone", tone)
        style = widget.style()
        if style is not None:
            style.unpolish(widget)
            style.polish(widget)
        widget.update()

    def _apply_slider_theme(self) -> None:
        theme = self.app.theme
        slider_style = (
            "QSlider::groove:horizontal { height: 8px; border-radius: 4px; "
            f"background: {theme.input_border}; }} "
            "QSlider::sub-page:horizontal { border-radius: 4px; "
            f"background: {theme.primary_color}; }} "
            "QSlider::add-page:horizontal { border-radius: 4px; "
            f"background: {theme.input}; }} "
            "QSlider::handle:horizontal { width: 22px; height: 22px; "
            "margin: -8px 0; border-radius: 11px; "
            f"background: {theme.input_popup}; border: 3px solid {theme.primary_color}; }}"
        )
        for slider in (
            self.amount_slider,
            self.frequency_slider,
            self.win_probability,
        ):
            slider.setStyleSheet(slider_style)

    def refresh(self) -> None:
        self._load_financial_context()
        self._habit_transaction_context = (
            self.analytics_service.habit_transaction_context(6)
        )
        self._load_habit_calibration()
        self._load_goals()
        self._apply_slider_theme()
        self._recalculate(persist=False)
        self.plans_card.refresh()
        self._sync_internal_view()

    def _sync_content_height(self) -> None:
        """Keep the large laboratory card tall enough for every visible section."""
        layout = self.main_card.v
        margins = layout.contentsMargins()
        required_height = margins.top() + margins.bottom()
        available_width = max(
            1,
            self.main_card.width() - margins.left() - margins.right(),
        )
        visible_items = 0
        for index in range(layout.count()):
            item = layout.itemAt(index)
            widget = item.widget() if item is not None else None
            if widget is not None and widget.isHidden():
                continue
            item_height = max(
                0,
                item.minimumSize().height(),
                item.sizeHint().height(),
            )
            if item.hasHeightForWidth():
                item_height = max(item_height, item.heightForWidth(available_width))
            required_height += item_height
            visible_items += 1
        required_height += max(0, visible_items - 1) * layout.spacing()
        required_height = max(1_200, required_height + 24)
        self.main_card.setMinimumHeight(required_height)
        self.main_card.updateGeometry()
        self.updateGeometry()
        sync = getattr(self.app, "sync_content_size", None)
        if callable(sync):
            sync()

    def _recalculate(self, *, persist: bool = True) -> None:
        preset = self._current_preset()
        base_inputs = self._inputs()
        base_data = simulate_habit(preset, base_inputs)
        self._base_data = base_data
        inputs = (
            self._calibration_inputs(base_inputs)
            if self._preview_kind == "real"
            else base_inputs
        )
        data = (
            simulate_habit(preset, inputs)
            if self._preview_kind == "real"
            else base_data
        )
        self._current_data = data
        privacy = self.app.theme.privacy
        occurrences = occurrences_per_month(inputs.frequency, inputs.frequency_unit)
        self.amount_value_label.setText(eur(inputs.unit_cost, privacy))
        self.frequency_value_label.setText(
            self._frequency_text(inputs.frequency, inputs.frequency_unit)
        )
        self.amount_slider.setAccessibleDescription(
            f"Valor atual {eur(inputs.unit_cost, privacy)} por ocorrência."
        )
        self.frequency_slider.setAccessibleDescription(
            f"Frequência atual {self.frequency_value_label.text()}."
        )

        expected_return_pct = safe_float(data.get("expected_return_ratio")) * 100.0
        expected_loss_pct = safe_float(data.get("expected_loss_ratio")) * 100.0
        self.win_probability_label.setText(f"{self.win_probability.value()}%")
        self.gambling_formula.setText(
            f"Hipótese: {self.win_probability.value()}% × "
            f"{self._decimal(self.payout_multiplier.value(), 2)} = retorno médio de "
            f"{self._decimal(expected_return_pct)}% do valor apostado; perda esperada "
            f"{self._decimal(expected_loss_pct)}%."
        )

        monthly = safe_float(data.get("monthly_cost"))
        monthly_alternative = safe_float(data.get("monthly_alternative_cost"))
        monthly_savings = safe_float(data.get("monthly_savings_potential"))
        annual = safe_float(data.get("annual_cost"))
        annual_savings = safe_float(data.get("annual_savings_potential"))
        ten_year = safe_float(data.get("ten_year_cost"))
        thirty = safe_float(data.get("thirty_year_opportunity"))
        if preset.gambling:
            self.alternative_hint.setText(
                f"{preset.alternative_name}: custo alternativo {eur(0, privacy)}. "
                "A poupança usa a perda esperada, não o valor total apostado."
            )
        else:
            self.alternative_hint.setText(
                f"{preset.alternative_name}: {eur(inputs.alternative_unit_cost, privacy)} "
                f"por ocorrência. A poupança real é apenas a diferença para "
                f"{eur(inputs.unit_cost, privacy)}."
            )
        share = safe_float(data.get("income_share_pct"))
        annual_share = safe_float(data.get("annual_income_share_pct"))
        occurrence_share = safe_float(data.get("occurrence_income_share_pct"))
        if self._average_income > 0:
            self.salary_share_value.setText(f"{self._decimal(share, 2)}% / mês")
            cost_label = "perda mensal esperada" if preset.gambling else "custo mensal"
            self.salary_share_formula.setText(
                f"Cálculo automático: {cost_label} {eur(monthly, privacy)} ÷ "
                f"{self._income_reference_name} mensal médio "
                f"{eur(self._average_income, privacy)} = "
                f"{self._decimal(share, 2)}%. Cada ocorrência corresponde a "
                f"{self._decimal(occurrence_share, 2)}% do rendimento; no ano, "
                f"o hábito representa {self._decimal(annual_share, 2)}% do rendimento anual."
            )
            self.salary_share_value.setAccessibleDescription(
                f"Este hábito usa {self._decimal(share, 2)} por cento do "
                f"{self._income_reference_name} mensal médio."
            )
        else:
            self.salary_share_value.setText("Sem rendimento registado")
            self.salary_share_formula.setText(
                "Regista o salário nos movimentos para esta percentagem ser calculada "
                "automaticamente."
            )
            self.salary_share_value.setAccessibleDescription(
                "Não há rendimento mensal registado para calcular a percentagem."
            )
        stat_values = {
            "month": (
                eur(monthly, privacy),
                f"{self._decimal(occurrences)} ocorrências/mês",
            ),
            "year": (
                eur(annual, privacy),
                f"{eur(annual_savings, privacy)} poupáveis com a alternativa",
            ),
            "ten": (
                eur(ten_year, privacy),
                f"alternativa: {eur(data['rows'][5]['cost_alternative'], privacy)}",
            ),
            "thirty": (
                eur(thirty, privacy),
                f"hipótese fixa de {self._decimal(inputs.annual_investment_rate_pct)}%/ano",
            ),
        }
        for key, (value_text, detail_text) in stat_values.items():
            _frame, value, detail = self.stat_widgets[key]
            value.setText(value_text)
            detail.setText(detail_text)

        tone = str(data.get("impact_tone") or "neutral")
        label = str(data.get("impact_label") or "Impacto moderado")
        preview_labels = {
            "real": "Pré-visualização · média real",
            "reduce_25": "Pré-visualização · −25%",
            "half": "Pré-visualização · metade",
            "alternative": "Pré-visualização · alternativa",
            "eliminate": "Pré-visualização · eliminar",
        }
        self.impact_badge.setText(preview_labels.get(self._preview_kind, label))
        self._set_tone(self.impact_badge, tone)
        self._set_tone(self.income_effort, tone)
        self._set_tone(self.insight_banner, tone)
        self._set_tone(self.insight_icon, tone)
        if self._average_income > 0:
            self.income_effort.setValue(max(0, min(100, round(share))))
            savings_context = (
                f" Representa também "
                f"{self._decimal(monthly / self._average_savings * 100.0)}% "
                "da poupança mensal média."
                if self._average_savings > 0
                else ""
            )
            self.income_effort_label.setText(
                f"💼 Este hábito representa {self._decimal(share)}% do "
                f"{self._income_reference_name} mensal médio."
                f"{savings_context} Equivale a cerca de "
                f"{self._decimal(safe_float(data.get('income_days')))} dia(s) de rendimento. "
                f"Referência calculada com {self._context_months} mês(es) da app."
            )
        else:
            self.income_effort.setValue(0)
            self.income_effort_label.setText(
                "💼 Regista o salário ou outros rendimentos mensais para comparar "
                "este hábito com o teu esforço financeiro real."
            )

        self.chart.set_data(data)
        self.chart.set_active_scenario(
            self._preview_kind
            if self._preview_kind in {"reduce_25", "half", "alternative"}
            else "current"
        )
        self._fill_table(data)
        self.select_horizon(self._selected_horizon_months)
        self._update_goal_panel()
        self._update_preview_detail()

        quarter_monthly = safe_float(data.get("quarter_reduction_monthly"))
        quarter_five = safe_float(data.get("quarter_reduction_five_year_value"))
        reduced_frequency = safe_float(
            (data.get("scenario_frequencies") or {}).get("reduce_25")
        )
        if preset.gambling:
            opening = (
                f"Neste cenário, o retorno médio estimado é "
                f"{self._decimal(expected_return_pct)}% e a perda esperada é "
                f"{self._decimal(expected_loss_pct)}% do valor jogado. Não jogar "
                f"libertaria cerca de {eur(monthly_savings, privacy)}/mês. "
            )
        else:
            opening = (
                f"Trocar todas as ocorrências por «{preset.alternative_name}» reduz "
                f"o custo de {eur(monthly, privacy)} para "
                f"{eur(monthly_alternative, privacy)}/mês e liberta "
                f"{eur(monthly_savings, privacy)}. "
            )
        self.insight_description.setText(
            opening
            + f"O cenário −25% fica em {self._frequency_text(reduced_frequency, inputs.frequency_unit)}: "
            f"poupa {eur(quarter_monthly, privacy)}/mês e poderia acumular "
            f"{eur(quarter_five, privacy)} em cinco anos."
        )
        self.insight_description.setAccessibleDescription(
            self.insight_description.text()
        )
        if persist:
            self._capture_current_state()
            self._schedule_persist()
        # Bind queued work to the native page; destruction cancels both calls.
        QTimer.singleShot(0, self, self._sync_content_height)
        QTimer.singleShot(80, self, self._sync_content_height)

    def _update_preview_detail(self) -> None:
        kind = self._preview_kind
        if kind == "current":
            self.preview_detail.clear()
            self.preview_detail.hide()
            self.apply_preview_btn.hide()
            self.save_plan_btn.hide()
            return
        self.preview_detail.show()
        self.apply_preview_btn.setVisible(kind not in {"alternative"})
        self.save_plan_btn.setVisible(
            kind in {"reduce_25", "half", "alternative", "eliminate"}
        )
        privacy = self.app.theme.privacy
        goal_suffix = ""
        impact = self._goal_impact()
        if impact:
            scenario = (impact.get("scenarios") or {}).get(kind) or {}
            if kind == "real":
                scenario = (impact.get("scenarios") or {}).get("current") or {}
            goal_suffix = (
                f" No objetivo «{impact.get('goal_name', 'Objetivo')}», a previsão fica em "
                f"{self._goal_scenario_text(scenario)}."
            )

        if kind == "real":
            calibration = self._habit_calibration
            monthly = safe_float(self._current_data.get("monthly_cost"))
            self.apply_preview_btn.setText("Usar estes valores")
            self.preview_detail.setText(
                f"📚 Média real em pré-visualização: "
                f"{eur(calibration.get('average_unit_cost', 0), privacy)} por ocorrência · "
                f"{self._frequency_text(calibration.get('suggested_frequency', 0), str(calibration.get('suggested_frequency_unit') or 'month'))} "
                f"· custo projetado {eur(monthly, privacy)}/mês. "
                "Os teus valores manuais continuam guardados até escolheres «Usar estes valores»."
                + goal_suffix
            )
            return

        monthly = dict((self._base_data.get("monthly_series") or [{}])[0])
        labels = {
            "reduce_25": "−25% das ocorrências",
            "half": "metade das ocorrências",
            "alternative": "alternativa realista",
            "eliminate": "eliminação do hábito",
        }
        cost_keys = {
            "reduce_25": "cost_reduce_25",
            "half": "cost_half",
            "alternative": "cost_alternative",
        }
        savings_keys = {
            "reduce_25": "savings_reduce_25",
            "half": "savings_half",
            "alternative": "savings_alternative",
        }
        if kind == "eliminate":
            monthly_cost = 0.0
            monthly_savings = safe_float(monthly.get("cost_base"))
            target_frequency = 0.0
        else:
            monthly_cost = safe_float(monthly.get(cost_keys[kind]))
            monthly_savings = safe_float(monthly.get(savings_keys[kind]))
            target_frequency = (
                safe_float((self._base_data.get("inputs") or {}).get("frequency"))
                if kind == "alternative"
                else safe_float(
                    (self._base_data.get("scenario_frequencies") or {}).get(kind)
                )
            )
        self.apply_preview_btn.setText("Aplicar ao cenário")
        self.preview_detail.setText(
            f"👁 {labels[kind]}: "
            f"{self._frequency_text(target_frequency, str(self.frequency_unit.currentData() or 'week'))} "
            f"· custo {eur(monthly_cost, privacy)}/mês · liberta "
            f"{eur(monthly_savings, privacy)}/mês. Nada foi alterado; aplica apenas se quiseres."
            + goal_suffix
        )

    def _fill_table(self, data: dict[str, Any]) -> None:
        rows = list(data.get("rows") or [])
        privacy = self.app.theme.privacy
        self.results_table.clearContents()
        self.results_table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            values = [
                row.get("label"),
                eur(row.get("cost_base", 0), privacy),
                eur(row.get("cost_reduce_25", 0), privacy),
                eur(row.get("cost_half", 0), privacy),
                eur(row.get("cost_alternative", 0), privacy),
                eur(row.get("savings_alternative", 0), privacy),
                eur(row.get("opportunity_value", 0), privacy),
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setData(Qt.UserRole, int(row.get("months", 0) or 0))
                if column > 0:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                if column == 1:
                    item.setForeground(
                        QColor("#D92D20" if not self.app.theme.dark else "#FF6961")
                    )
                elif column in (4, 5, 6):
                    item.setForeground(
                        QColor("#16803A" if not self.app.theme.dark else "#30D158")
                    )
                self.results_table.setItem(row_index, column, item)
            self.results_table.setRowHeight(row_index, 42)

    def select_horizon(self, months: int) -> None:
        rows = list(self._current_data.get("rows") or [])
        if not rows:
            self.chart_detail.clear()
            return
        index = next(
            (
                current
                for current, row in enumerate(rows)
                if int(row.get("months", 0) or 0) == int(months)
            ),
            min(len(rows) - 1, 5),
        )
        row = rows[index]
        self._selected_horizon_months = int(row.get("months", 0) or 0)
        self.results_table.selectRow(index)
        self.chart_detail.setText(
            f"📍 {row.get('label')}: atual "
            f"{eur(row.get('cost_base', 0), self.app.theme.privacy)} · −25% "
            f"{eur(row.get('cost_reduce_25', 0), self.app.theme.privacy)} · metade "
            f"{eur(row.get('cost_half', 0), self.app.theme.privacy)} · alternativa "
            f"{eur(row.get('cost_alternative', 0), self.app.theme.privacy)} · "
            f"poupança real {eur(row.get('savings_alternative', 0), self.app.theme.privacy)}."
        )


__all__ = ["HabitSimulationsPage"]
