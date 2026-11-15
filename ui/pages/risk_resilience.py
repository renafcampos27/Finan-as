from __future__ import annotations

from typing import TYPE_CHECKING, Any

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QComboBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)

from ui.components import (
    GlassCard,
    MetricCard,
    ResilienceScenarioChart,
    ScoreBreakdownChart,
    ScoreGauge,
)
from ui.core import BasePage, EuroSpinBox, ScrollPassthroughSlider
from utils.helpers import BLUE, GREEN, ORANGE, PURPLE, RED, eur, percent, safe_float

if TYPE_CHECKING:
    from main import LiquidFinanceApp


class RiskResiliencePage(BasePage):
    """Interactive household stress-test and resilience cockpit."""

    def __init__(self, app: "LiquidFinanceApp") -> None:
        super().__init__(
            app,
            "Risco e Resiliência",
            "Testa choques de rendimento, despesas inesperadas e quedas de mercado",
        )
        self._data: dict[str, Any] = {}
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(140)
        self._refresh_timer.timeout.connect(self.refresh)

        self.controls_card = GlassCard("🧪 Laboratório de stress", strong=True)
        presets = QHBoxLayout()
        presets.setSpacing(8)
        presets.addWidget(QLabel("Cenário rápido"))
        for label, values in (
            ("🌤️ Moderado", (20, 3, 1_000, 10)),
            ("🌧️ Severo", (40, 6, 5_000, 30)),
            ("⛈️ Extremo", (60, 12, 10_000, 50)),
        ):
            button = QPushButton(label)
            button.setObjectName("Secondary")
            button.clicked.connect(
                lambda _checked=False, selected=values: self._set_scenario(*selected)
            )
            presets.addWidget(button)
        presets.addStretch(1)
        self.controls_card.v.addLayout(presets)

        fields = QGridLayout()
        fields.setHorizontalSpacing(18)
        fields.setVerticalSpacing(8)
        self.income_cut_label = QLabel()
        self.income_cut_label.setObjectName("SectionTitle")
        self.income_cut_slider = ScrollPassthroughSlider(Qt.Horizontal)
        # BUG-RISK-01: allow simulating a total income loss (100%).
        self.income_cut_slider.setRange(0, 100)
        self.income_cut_slider.setValue(30)
        self.income_cut_slider.setTickInterval(10)

        self.cut_months_label = QLabel("⏱️ Duração da redução")
        self.cut_months_label.setObjectName("SectionTitle")
        self.cut_months = QSpinBox()
        self.cut_months.setRange(1, 24)
        self.cut_months.setValue(6)
        self.cut_months.setSuffix(" meses")

        self.emergency_label = QLabel("🧾 Despesa inesperada")
        self.emergency_label.setObjectName("SectionTitle")
        self.emergency = EuroSpinBox()
        self.emergency.setRange(0, 250_000)
        self.emergency.setDecimals(0)
        self.emergency.setSingleStep(250)
        self.emergency.setSuffix(" €")
        self.emergency.setValue(2_000)

        self.market_drop_label = QLabel()
        self.market_drop_label.setObjectName("SectionTitle")
        self.market_drop_slider = ScrollPassthroughSlider(Qt.Horizontal)
        self.market_drop_slider.setRange(0, 70)
        self.market_drop_slider.setValue(20)
        self.market_drop_slider.setTickInterval(10)

        self.horizon_label = QLabel("🗓️ Horizonte")
        self.horizon_label.setObjectName("SectionTitle")
        self.horizon = QComboBox()
        self.horizon.addItem("6 meses", 6)
        self.horizon.addItem("12 meses", 12)
        self.horizon.addItem("24 meses", 24)
        self.horizon.setCurrentIndex(1)

        self.reset_scenario_btn = QPushButton("↺ Repor cenário")
        self.reset_scenario_btn.setObjectName("Primary")
        self.reset_scenario_btn.clicked.connect(self._reset_page_filters)

        self._add_field(fields, 0, 0, self.income_cut_label, self.income_cut_slider)
        self._add_field(fields, 0, 1, self.cut_months_label, self.cut_months)
        self._add_field(fields, 0, 2, self.emergency_label, self.emergency)
        self._add_field(fields, 1, 0, self.market_drop_label, self.market_drop_slider)
        self._add_field(fields, 1, 1, self.horizon_label, self.horizon)
        reset_box = QVBoxLayout()
        reset_box.addWidget(QLabel("Ações"))
        reset_box.addWidget(self.reset_scenario_btn)
        fields.addLayout(reset_box, 1, 2)
        for column in range(3):
            fields.setColumnStretch(column, 1)
        self.controls_card.v.addLayout(fields)
        self.register_filter_panel(self.controls_card)
        self.body.addWidget(self.controls_card)

        self.summary = QGridLayout()
        self.summary.setSpacing(10)
        self.register_responsive_grid(self.summary)
        self.body.addLayout(self.summary)

        self.scenario_chart = ResilienceScenarioChart(lambda: self.app.theme)
        self.scenario_chart.configure_details(
            lambda _item: {
                "title": "Trajetória de liquidez",
                "explanation": (
                    "Compara o saldo base com os cenários otimista e de stress "
                    "no mês selecionado."
                ),
            },
            action_label="Selecionar mês",
        )
        self.scenario_chart.point_clicked.connect(self._show_point_detail)
        self.scenario_detail = QLabel()
        self.scenario_detail.setObjectName("Subtle")
        self.scenario_detail.setWordWrap(True)
        self.scenario_card = GlassCard("📉 Trajetórias de liquidez", strong=True)
        self.scenario_card.set_confidence_badge("Confiança · a calcular", "warning")
        self.scenario_card.v.addWidget(self.scenario_detail)
        self.scenario_card.v.addWidget(self.scenario_chart)
        self.body.addWidget(self.scenario_card)

        self.lower_grid = QGridLayout()
        self.lower_grid.setSpacing(12)
        self.register_responsive_grid(
            self.lower_grid,
            max_columns=2,
            min_column_width=500,
        )
        self.breakdown_card = GlassCard("🛡️ Componentes da resiliência", strong=True)
        self.resilience_gauge_box = QHBoxLayout()
        self.resilience_breakdown = ScoreBreakdownChart(lambda: self.app.theme)
        self.resilience_breakdown.configure_details(
            lambda _item: {
                "title": "Componente de risco e resiliência",
                "explanation": (
                    "A pontuação combina cobertura de emergência, liquidez, "
                    "dívida, estabilidade do rendimento e concentração."
                ),
            },
            action_label="Analisar componente",
        )
        self.resilience_breakdown.dimension_clicked.connect(self._show_component_detail)
        self.component_detail = QLabel(
            "Seleciona um componente para compreender a leitura."
        )
        self.component_detail.setWordWrap(True)
        self.component_detail.setObjectName("Subtle")
        self.breakdown_card.v.addLayout(self.resilience_gauge_box)
        self.breakdown_card.v.addWidget(self.resilience_breakdown)
        self.breakdown_card.v.addWidget(self.component_detail)

        self.actions_card = GlassCard("🚦 Plano de resposta", strong=True)
        self.actions_card.set_compact_header()
        self.actions_content = QVBoxLayout()
        self.actions_content.setContentsMargins(0, 0, 0, 0)
        self.actions_content.setSpacing(8)
        self.method_hint = QLabel()
        self.method_hint.setWordWrap(True)
        self.method_hint.setObjectName("Subtle")
        self.actions_card.v.addLayout(self.actions_content)
        self.actions_card.v.addWidget(self.method_hint)
        self.lower_grid.addWidget(self.breakdown_card, 0, 0)
        self.lower_grid.addWidget(self.actions_card, 0, 1)
        self.body.addLayout(self.lower_grid)

        for control in (
            self.income_cut_slider,
            self.cut_months,
            self.emergency,
            self.market_drop_slider,
            self.horizon,
        ):
            if isinstance(control, QComboBox):
                control.currentIndexChanged.connect(self._queue_refresh)
            else:
                control.valueChanged.connect(self._queue_refresh)
        self._update_control_labels()
        self.refresh()

    @staticmethod
    def _add_field(
        grid: QGridLayout, row: int, column: int, label: QLabel, control
    ) -> None:
        layout = QVBoxLayout()
        layout.setSpacing(5)
        layout.addWidget(label)
        layout.addWidget(control)
        grid.addLayout(layout, row, column)

    def _queue_refresh(self, *_args) -> None:
        self._update_control_labels()
        self._refresh_timer.start()

    def _update_control_labels(self) -> None:
        self.income_cut_label.setText(
            f"📉 Redução do rendimento · {self.income_cut_slider.value()}%"
        )
        self.market_drop_label.setText(
            f"📊 Queda dos investimentos · {self.market_drop_slider.value()}%"
        )

    def _set_scenario(
        self, income_cut: int, duration: int, emergency: float, market_drop: int
    ) -> None:
        self.income_cut_slider.setValue(income_cut)
        self.cut_months.setValue(duration)
        self.emergency.setValue(emergency)
        self.market_drop_slider.setValue(market_drop)
        self._refresh_timer.stop()
        self._update_control_labels()
        self.refresh()

    def _reset_page_filters(self) -> None:
        self.horizon.setCurrentIndex(1)
        self._set_scenario(30, 6, 2_000, 20)

    @staticmethod
    def _clear_layout(layout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            if item.widget():
                widget = item.widget()
                widget.hide()
                widget.setParent(None)
                widget.deleteLater()
            elif item.layout():
                RiskResiliencePage._clear_layout(item.layout())

    @staticmethod
    def _tone_colour(tone: str) -> str:
        return {
            "positive": GREEN,
            "warning": ORANGE,
            "negative": RED,
            "neutral": BLUE,
        }.get(str(tone), BLUE)

    def _summary_cards(self, data: dict[str, Any]) -> None:
        self._clear_layout(self.summary)
        probability = safe_float(data.get("probability_negative"))
        runway = safe_float(data.get("runway_months"))
        minimum = safe_float(data.get("minimum_stressed"))
        score_available = bool(data.get("score_available", True))
        score = safe_float(data.get("score"))
        confidence_score = safe_float(data.get("confidence_score"))
        confidence = str(data.get("confidence") or "insufficient")
        confidence_tone = {
            "high": "positive",
            "medium": "neutral",
            "low": "warning",
            "insufficient": "warning",
        }.get(confidence, "neutral")
        self.scenario_card.set_confidence_badge(
            f"Confiança · {data.get('confidence_label', 'Dados insuficientes')}",
            confidence_tone,
        )
        cards = [
            MetricCard(
                "Resiliência",
                (
                    f"{score:.0f}/100 · {data.get('grade', '—')}"
                    if score_available
                    else "Dados insuficientes"
                ),
                f"{data.get('marker', '')} {data.get('status', '')}",
                self._tone_colour(str(data.get("tone"))),
                "🛡️",
                status=(
                    "Score do cenário atual"
                    if score_available
                    else "São necessários pelo menos 3 meses completos"
                ),
                tone=str(data.get("tone") or "neutral"),
                progress=score if score_available else None,
            ),
            MetricCard(
                "Risco de saldo negativo",
                percent(probability) if score_available else "—",
                (
                    "em trajetórias simuladas"
                    if score_available
                    else "Aguardando histórico suficiente"
                ),
                (
                    RED
                    if score_available and probability >= 40
                    else ORANGE
                    if not score_available or probability >= 15
                    else GREEN
                ),
                (
                    "🚨"
                    if score_available and probability >= 40
                    else "⚠️"
                    if not score_available or probability >= 15
                    else "✅"
                ),
                status=(
                    "Dados insuficientes"
                    if not score_available
                    else "Risco elevado"
                    if probability >= 40
                    else "Sob observação"
                    if probability >= 15
                    else "Risco baixo"
                ),
                tone=(
                    "warning"
                    if not score_available
                    else "negative"
                    if probability >= 40
                    else "warning"
                    if probability >= 15
                    else "positive"
                ),
                progress=probability if score_available else None,
            ),
            MetricCard(
                "Autonomia sob stress",
                (
                    "24+ meses"
                    if score_available and runway >= 24
                    else f"{runway:.1f} meses".replace(".", ",")
                    if score_available
                    else "—"
                ),
                (
                    "após a despesa inesperada"
                    if score_available
                    else "Aguardando histórico suficiente"
                ),
                GREEN if score_available and runway >= 6 else ORANGE,
                "⏳",
                status=(
                    "Reserva disponível" if score_available else "Dados insuficientes"
                ),
                tone=(
                    "positive"
                    if score_available and runway >= 6
                    else "warning"
                    if not score_available or runway >= 3
                    else "negative"
                ),
                progress=(
                    min(100.0, runway / 12.0 * 100.0) if score_available else None
                ),
            ),
            MetricCard(
                "Mínimo no cenário",
                eur(minimum, self.app.theme.privacy) if score_available else "—",
                (
                    f"saldo negativo no mês {data.get('negative_month')}"
                    if score_available and data.get("negative_month")
                    else "sem rutura no horizonte"
                    if score_available
                    else "Aguardando histórico suficiente"
                ),
                RED if score_available and minimum < 0 else PURPLE,
                "📍",
                status=(
                    "Pior ponto estimado" if score_available else "Dados insuficientes"
                ),
                tone=(
                    "negative"
                    if score_available and minimum < 0
                    else "positive"
                    if score_available
                    else "neutral"
                ),
            ),
            MetricCard(
                "Confiança da análise",
                str(data.get("confidence_label") or "Dados insuficientes"),
                (
                    f"{int(data.get('data_months', 0) or 0)}/12 meses · "
                    f"{safe_float(data.get('data_completeness_pct')):.0f}% completos"
                ),
                self._tone_colour(confidence_tone),
                "🧪",
                status="Qualidade da base histórica",
                tone=confidence_tone,
                progress=confidence_score,
            ),
        ]
        for index, card in enumerate(cards):
            self.summary.addWidget(card, 0, index)
            self.summary.setColumnStretch(index, 1)
        self.reflow_responsive_grid(self.summary)

    def _fill_actions(self, data: dict[str, Any]) -> None:
        self._clear_layout(self.actions_content)
        if bool(data.get("score_available", True)):
            intro_text = (
                f"{data.get('marker', '')} <b>{data.get('status', '')}</b> · "
                f"património estimado após stress: "
                f"<b>{eur(data.get('stressed_final_net_worth', 0), self.app.theme.privacy)}</b>"
            )
        else:
            intro_text = (
                "🧪 <b>Dados insuficientes</b> · a app não apresenta "
                "património, risco ou autonomia simulados como se fossem uma "
                "estimativa fiável."
            )
        intro = QLabel(intro_text)
        intro.setWordWrap(True)
        self.actions_content.addWidget(intro)
        for action in data.get("actions", []):
            button = QPushButton(
                f"{action.get('icon', '•')}  {action.get('title', '')}\n"
                f"     {action.get('message', '')}  →"
            )
            button.setObjectName("Secondary")
            button.setMinimumHeight(62)
            button.setCursor(Qt.PointingHandCursor)
            button.setStyleSheet("text-align:left; padding:9px 12px;")
            target_page = str(action.get("page") or "dashboard")
            button.clicked.connect(
                lambda _checked=False, page=target_page: self.app.goto(page)
            )

            self.actions_content.addWidget(button)
        if bool(data.get("score_available", True)):
            market_loss = safe_float(data.get("market_loss"))
            market = QLabel(
                f"📉 Impacto direto da queda de mercado: "
                f"<b>{eur(-market_loss, self.app.theme.privacy)}</b>. "
                f"Recuperação da liquidez: "
                f"<b>{'fora do horizonte' if not data.get('recovery_month') else 'mês ' + str(data.get('recovery_month'))}</b>."
            )
            market.setWordWrap(True)
            self.actions_content.addWidget(market)
        sensitivity = list(data.get("sensitivity") or [])
        if sensitivity:
            minimum_values = [
                safe_float(row.get("minimum_cash")) for row in sensitivity
            ]
            sensitivity_text = (
                "Sensibilidade: o saldo mínimo varia entre "
                f"{eur(min(minimum_values), self.app.theme.privacy)} e "
                f"{eur(max(minimum_values), self.app.theme.privacy)} nos cenários "
                "mais severo e mais favorável. "
            )
        else:
            sensitivity_text = ""
        self.method_hint.setText(sensitivity_text + str(data.get("method_label") or ""))

    def _show_point_detail(self, row: dict[str, Any]) -> None:
        label = str(row.get("label") or "")
        self.scenario_detail.setText(
            f"📍 {label}: base {eur(row.get('base', 0), self.app.theme.privacy)} · "
            f"otimista {eur(row.get('optimistic', 0), self.app.theme.privacy)} · "
            f"stress {eur(row.get('stressed', 0), self.app.theme.privacy)}. "
            "Passa o rato para comparar o intervalo provável."
        )

    def _show_component_detail(self, key: str) -> None:
        component = next(
            (row for row in self._data.get("components", []) if row.get("key") == key),
            None,
        )
        if not component:
            return
        readings = {
            "reserve": f"A reserva cobre {safe_float(self._data.get('emergency_months')):.1f} meses de despesas.",
            "margin": f"A margem mensal média é {eur(self._data.get('monthly_margin', 0), self.app.theme.privacy)}.",
            "stability": "Compara a variação observada de rendimentos e despesas nos últimos meses.",
            "debt": (
                f"As prestações representam {percent(self._data.get('debt_service_ratio', 0), self.app.theme.privacy)} do rendimento médio."
                if self._data.get("debt_service_available", True)
                else "Sem estado histórico de dívidas; relação prestação/rendimento indisponível."
            ),
            "scenario": f"O saldo mínimo do stress é {eur(self._data.get('minimum_stressed', 0), self.app.theme.privacy)}.",
        }
        self.component_detail.setText(
            f"{component.get('marker', '')} <b>{component.get('name')}</b> · "
            f"{safe_float(component.get('score')):.0f}/100 · {component.get('status')}. "
            f"{readings.get(key, '')}"
        )
        self.component_detail.setTextFormat(Qt.RichText)

    def refresh(self) -> None:
        self._refresh_timer.stop()
        data = self.analytics_service.risk_resilience_analysis(
            horizon_months=int(self.horizon.currentData() or 12),
            income_reduction_pct=self.income_cut_slider.value(),
            income_reduction_months=self.cut_months.value(),
            emergency_expense=self.emergency.value(),
            market_drawdown_pct=self.market_drop_slider.value(),
        )
        self._data = data
        self._summary_cards(data)
        self.scenario_chart.set_data(list(data.get("rows") or []))
        self._show_point_detail((data.get("rows") or [{}])[-1])
        while self.resilience_gauge_box.count():
            item = self.resilience_gauge_box.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.resilience_gauge_box.addStretch(1)
        if bool(data.get("score_available", True)):
            self.resilience_gauge_box.addWidget(
                ScoreGauge(
                    lambda: self.app.theme,
                    int(safe_float(data.get("score"))),
                ),
                alignment=Qt.AlignCenter,
            )
        else:
            unavailable = QLabel(
                "🧪  Dados insuficientes\n"
                "Adiciona pelo menos 3 meses com rendimentos e despesas."
            )
            unavailable.setAlignment(Qt.AlignCenter)
            unavailable.setWordWrap(True)
            unavailable.setObjectName("Subtle")
            unavailable.setMinimumHeight(140)
            self.resilience_gauge_box.addWidget(
                unavailable,
                alignment=Qt.AlignCenter,
            )
        self.resilience_gauge_box.addStretch(1)
        self.resilience_breakdown.set_data(list(data.get("components") or []))
        self._fill_actions(data)
