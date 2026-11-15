#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Finanças Pessoais – Liquid Glass
Nova interface PySide6 para a app local de finanças pessoais.
Mantém a base de dados e a app clássica intactas; esta é apenas uma camada visual moderna.
"""

from __future__ import annotations

import json
import html
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from utils.qt_runtime import configure_qt_runtime

configure_qt_runtime()

try:
    from PySide6.QtCore import QDate, Qt, Signal
    from PySide6.QtGui import QColor, QFont, QTextCharFormat
    from PySide6.QtWidgets import (
        QCalendarWidget,
        QComboBox,
        QFrame,
        QGridLayout,
        QHBoxLayout,
        QHeaderView,
        QLabel,
        QLineEdit,
        QMessageBox,
        QProgressBar,
        QPushButton,
        QSizePolicy,
        QTableWidget,
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
    BudgetVarianceHeatmapChart,
    CashflowTimelineChart,
    CashRunwayProbabilityChart,
    FixedCommitmentsChart,
    GlassCard,
    RecurringCommitmentsTimelineChart,
)
from ui.chart_detail_support import movement_detail_payload
from ui.document_preview import AttachmentPreviewDialog
from ui.workers import AsyncTask
from ui.core import (
    BasePage,
    EdgePropagatingScrollArea,
    FlowLayout,
    apply_calendar_theme,
)
from ui.core.smart_table import SmartTableWidgetItem as QTableWidgetItem
from utils.app_logging import log_exception
from utils.helpers import BLUE, GREEN, ORANGE, RED, eur, open_path, safe_float
from services.documents import (
    AttachmentIntegrityError,
    AttachmentValidationError,
)
from services.product_review import ProductReviewService

if TYPE_CHECKING:
    from main import LiquidFinanceApp


class FinanceCenterPage(BasePage):
    def __init__(self, app: "LiquidFinanceApp"):
        super().__init__(app, "Centro financeiro", "")
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        self._cashflow_marked_dates: set[str] = set()
        self._cashflow_events_by_date: dict[str, list[dict]] = {}
        self.layout().setSpacing(2)
        self.body.setSpacing(6)
        self.body.setAlignment(Qt.AlignTop)
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(8)
        grid.setAlignment(Qt.AlignTop)
        self.calendar = QCalendarWidget()
        self.calendar.setGridVisible(False)
        self.calendar.setFixedHeight(320)
        self.calendar.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.calendar.setHorizontalHeaderFormat(QCalendarWidget.ShortDayNames)
        self.calendar.setNavigationBarVisible(True)
        self.calendar.setObjectName("FinanceCalendar")
        self.calendar.setAutoFillBackground(True)
        self.calendar.setAttribute(Qt.WA_StyledBackground, True)
        apply_calendar_theme(self.calendar, self.app.theme)
        self.calendar.clicked.connect(self.on_calendar_clicked)
        self.calendar_card = GlassCard("🗓️ Calendário financeiro", strong=True)
        self.calendar_card.v.setContentsMargins(15, 10, 15, 10)
        self.calendar_card.v.setSpacing(5)
        self.calendar_card.v.setAlignment(Qt.AlignTop)
        self.calendar_card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.calendar_card.setMaximumHeight(378)
        self.calendar_card.v.addWidget(self.calendar)
        self.forecast = GlassCard("🛟 Tesouraria a 60 dias")
        self.forecast.v.setContentsMargins(15, 10, 15, 10)
        self.forecast.v.setSpacing(5)
        self.forecast.v.setAlignment(Qt.AlignTop)
        self.forecast.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        self.forecast.setMaximumHeight(260)
        if self.forecast.title_label is not None:
            self.forecast.title_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            self.forecast.title_label.setSizePolicy(
                QSizePolicy.Expanding, QSizePolicy.Fixed
            )
        self.alerts = GlassCard("🚦 Centro de alertas", strong=True)
        self.recurring = GlassCard("🔔 Próximos eventos")
        self.recurring.v.setAlignment(Qt.AlignTop)
        self.recurring.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        self.recurring.setMaximumHeight(620)
        self.recurring_summary = QLabel()
        self.recurring_summary.setObjectName("Subtle")
        self.recurring_summary.setWordWrap(True)
        self.recurring_scroll = EdgePropagatingScrollArea()
        self.recurring_scroll.setObjectName("UpcomingEventsScroll")
        self.recurring_scroll.setWidgetResizable(True)
        self.recurring_scroll.setFrameShape(QFrame.NoFrame)
        self.recurring_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.recurring_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.recurring_scroll.setMinimumHeight(240)
        self.recurring_scroll.setMaximumHeight(540)
        self.recurring_scroll.setSizePolicy(
            QSizePolicy.Expanding, QSizePolicy.Expanding
        )
        self.recurring_events_host = QWidget()
        self.recurring_events_host.setObjectName("UpcomingEventsContent")
        self.recurring_events_layout = QVBoxLayout(self.recurring_events_host)
        self.recurring_events_layout.setContentsMargins(0, 0, 3, 0)
        self.recurring_events_layout.setSpacing(7)
        self.recurring_events_layout.setAlignment(Qt.AlignTop)
        self.recurring_scroll.setWidget(self.recurring_events_host)
        self.recurring.v.addWidget(self.recurring_summary)
        self.recurring.v.addWidget(self.recurring_scroll, 1)
        alert_controls_wrap = QWidget()
        alert_controls_wrap.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        alert_controls = FlowLayout(
            alert_controls_wrap, margin=0, hspacing=6, vspacing=6
        )
        self.alert_filter = QComboBox()
        self.alert_filter.addItem("Todos", "all")
        self.alert_filter.addItem("🚨 Críticos", "critical")
        self.alert_filter.addItem("⚠️ Atenção", "warning")
        self.alert_filter.addItem("ℹ️ Informação", "info")
        self.alert_open_btn = QPushButton("Abrir detalhe")
        self.alert_open_btn.setObjectName("Primary")
        self.alert_dismiss_btn = QPushButton("Ocultar selecionado")
        self.alert_restore_btn = QPushButton("Repor ocultos")
        self.alert_snooze_btn = QPushButton("Adiar 7 dias")
        self.alert_snooze_btn.setAccessibleName("Adiar o alerta selecionado por sete dias")
        self.quality_check_btn = QPushButton("Qualidade e origem")
        self.quality_check_btn.setAccessibleName("Abrir checklist opcional de qualidade e origem dos números")
        alert_controls.addWidget(QLabel("Prioridade"))
        alert_controls.addWidget(self.alert_filter)
        alert_controls.addWidget(self.alert_restore_btn)
        alert_controls.addWidget(self.quality_check_btn)
        alert_controls.addWidget(self.alert_snooze_btn)
        alert_controls.addWidget(self.alert_dismiss_btn)
        alert_controls.addWidget(self.alert_open_btn)
        self.alert_table = QTableWidget(0, 5)
        self.alert_table.setProperty("columnPersistenceId", "finance_center.alerts")
        self.alert_table.setHorizontalHeaderLabels(
            ["Prioridade", "Data", "Alerta", "Categoria", "Valor"]
        )
        self.alert_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        for column in (0, 1, 3, 4):
            self.alert_table.horizontalHeader().setSectionResizeMode(
                column, QHeaderView.ResizeToContents
            )
        self.alert_table.setAlternatingRowColors(True)
        self.alert_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.alert_table.verticalHeader().setVisible(False)
        self.alert_table.setMinimumHeight(270)
        self.alerts.v.addWidget(alert_controls_wrap)
        self.alerts.v.addWidget(self.alert_table)
        self.alerts.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        self.summary_left = QWidget()
        self.summary_left.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        summary_left_layout = QVBoxLayout(self.summary_left)
        summary_left_layout.setContentsMargins(0, 0, 0, 0)
        summary_left_layout.setSpacing(8)
        summary_left_layout.setAlignment(Qt.AlignTop)
        summary_left_layout.addWidget(self.calendar_card, 0, Qt.AlignTop)
        summary_left_layout.addWidget(self.forecast, 0, Qt.AlignTop)

        self.summary_grid = grid
        grid.addWidget(self.summary_left, 0, 0, Qt.AlignTop)
        grid.addWidget(self.recurring, 0, 1, Qt.AlignTop)
        grid.setColumnStretch(0, 3)
        grid.setColumnStretch(1, 2)
        grid.setRowStretch(0, 0)
        self.body.addLayout(grid, 0)
        self.register_responsive_grid(grid, max_columns=2, min_column_width=440)
        self.body.addWidget(self.alerts, 0)

        self.cashflow_card = GlassCard(
            "📈 Evolução prevista do saldo · 60 dias", strong=True
        )
        self.cashflow_hint = QLabel()
        self.cashflow_hint.setObjectName("Subtle")
        self.cashflow_hint.setWordWrap(True)
        self.cashflow_chart = CashflowTimelineChart(lambda: self.app.theme)
        self.cashflow_chart.configure_details(
            self._cashflow_detail_payload,
            action_label="Abrir origem",
        )
        self.cashflow_chart.event_clicked.connect(self._on_cashflow_event)
        self.cashflow_card.v.addWidget(self.cashflow_hint)
        self.cashflow_card.v.addWidget(self.cashflow_chart)
        self.body.addWidget(self.cashflow_card, 0)

        self.commitments_card = GlassCard(
            "🔒 Evolução dos compromissos fixos · 12 meses",
            strong=True,
        )
        if self.commitments_card.title_label is not None:
            self.commitments_card.title_label.setMinimumWidth(0)
            self.commitments_card.title_label.setWordWrap(True)
            self.commitments_card.title_label.setSizePolicy(
                QSizePolicy.Ignored,
                QSizePolicy.Preferred,
            )
        self.commitments_summary = QLabel()
        self.commitments_summary.setObjectName("Subtle")
        self.commitments_summary.setWordWrap(True)
        self.commitments_chart = FixedCommitmentsChart(lambda: self.app.theme)
        self.commitments_chart.configure_details(
            self._commitment_detail_payload,
        )
        self.commitments_card.v.addWidget(self.commitments_summary)
        self.commitments_card.v.addWidget(self.commitments_chart)
        self.body.addWidget(self.commitments_card, 0)

        self.operational_grid = QGridLayout()
        self.operational_grid.setContentsMargins(0, 0, 0, 0)
        self.operational_grid.setSpacing(8)

        self.runway_summary = QLabel()
        self.runway_summary.setObjectName("Subtle")
        self.runway_summary.setWordWrap(True)
        self.runway_chart = CashRunwayProbabilityChart(lambda: self.app.theme)
        self.runway_chart.configure_details(self._runway_detail_payload)
        self.runway_card = GlassCard(
            "🛟 Saldo positivo no fim de cada mês · 12 meses",
            strong=True,
        )
        self.runway_card.setProperty("pageCardKey", "cash_runway_probability")
        self.runway_card.v.addWidget(self.runway_summary)
        self.runway_card.v.addWidget(self.runway_chart)

        self.recurring_timeline_summary = QLabel()
        self.recurring_timeline_summary.setObjectName("Subtle")
        self.recurring_timeline_summary.setWordWrap(True)
        self.recurring_timeline_chart = RecurringCommitmentsTimelineChart(
            lambda: self.app.theme
        )
        self.recurring_timeline_chart.configure_details(
            self._recurring_timeline_detail_payload,
            action_label="Abrir subscrições",
            action_handler=lambda _item: self.app.goto("subscricoes"),
        )
        self.recurring_timeline_card = GlassCard(
            "🧭 Compromissos recorrentes · confiança",
            strong=True,
        )
        self.recurring_timeline_card.setProperty(
            "pageCardKey", "recurring_commitment_timeline"
        )
        self.recurring_timeline_card.v.addWidget(self.recurring_timeline_summary)
        self.recurring_timeline_card.v.addWidget(self.recurring_timeline_chart)

        self.budget_variance_summary = QLabel()
        self.budget_variance_summary.setObjectName("Subtle")
        self.budget_variance_summary.setWordWrap(True)
        self.budget_variance_chart = BudgetVarianceHeatmapChart(lambda: self.app.theme)
        self.budget_variance_chart.configure_details(
            self._budget_variance_detail_payload,
            action_label="Abrir orçamentos",
            action_handler=lambda _item: self.app.goto("orcamentos"),
        )
        self.budget_variance_card = GlassCard(
            "🧩 Mapa de desvios do orçamento",
            strong=True,
        )
        self.budget_variance_card.setProperty("pageCardKey", "budget_variance_heatmap")
        self.budget_variance_card.v.addWidget(self.budget_variance_summary)
        self.budget_variance_card.v.addWidget(self.budget_variance_chart)

        # Long operational titles must not become the minimum width of the
        # whole page.  At narrow widths they wrap inside a single-column card;
        # at wider widths the grid still presents the intended two columns.
        for card, summary in (
            (self.runway_card, self.runway_summary),
            (self.recurring_timeline_card, self.recurring_timeline_summary),
            (self.budget_variance_card, self.budget_variance_summary),
        ):
            if card.title_label is not None:
                card.title_label.setMinimumWidth(0)
                card.title_label.setWordWrap(True)
                card.title_label.setSizePolicy(
                    QSizePolicy.Ignored,
                    QSizePolicy.Preferred,
                )
            summary.setMinimumWidth(0)
            summary.setSizePolicy(
                QSizePolicy.Ignored,
                QSizePolicy.Preferred,
            )

        self.operational_grid.addWidget(self.runway_card, 0, 0)
        self.operational_grid.addWidget(self.recurring_timeline_card, 0, 1)
        self.operational_grid.addWidget(self.budget_variance_card, 1, 0)
        self.body.addLayout(self.operational_grid)
        self.register_responsive_grid(
            self.operational_grid,
            max_columns=2,
            min_column_width=520,
        )
        self.alert_filter.currentIndexChanged.connect(self._fill_alert_table)
        self.alert_open_btn.clicked.connect(self._open_selected_alert)
        self.alert_dismiss_btn.clicked.connect(self._dismiss_selected_alert)
        self.alert_restore_btn.clicked.connect(self._restore_alerts)
        self.alert_snooze_btn.clicked.connect(self._snooze_selected_alert)
        self.quality_check_btn.clicked.connect(self._open_quality_checklist)
        self.alert_table.cellDoubleClicked.connect(
            lambda _row, _column: self._open_selected_alert()
        )
        self._alerts_data: list[dict] = []
        self.refresh()

    def reflow_responsive_grids(self) -> None:
        """Keep the calendar/event composition intentional at every width."""
        super().reflow_responsive_grids()
        grid = getattr(self, "summary_grid", None)
        recurring = getattr(self, "recurring", None)
        if grid is None or recurring is None:
            return
        if self._responsive_column_count(2, 440) >= 2:
            grid.setColumnStretch(0, 3)
            grid.setColumnStretch(1, 2)
            recurring.setMaximumWidth(520)
            self.recurring_scroll.setMaximumHeight(540)
        else:
            grid.setColumnStretch(0, 1)
            grid.setColumnStretch(1, 0)
            recurring.setMaximumWidth(16_777_215)
            self.recurring_scroll.setMaximumHeight(420)
        grid.invalidate()

    def wipe(self, card: GlassCard) -> None:
        while card.v.count() > 1:
            item = card.v.takeAt(1)
            widget = item.widget()
            if widget:
                if widget is card.data_coverage_label:
                    card.data_coverage_label = None
                widget.deleteLater()

    def _clear_event_rows(self) -> None:
        while self.recurring_events_layout.count():
            item = self.recurring_events_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def on_calendar_clicked(self, qdate: QDate) -> None:
        selected_date = qdate.toString("yyyy-MM-dd")
        predicted_events = self._cashflow_events_by_date.get(selected_date, [])
        if predicted_events:
            self._on_cashflow_event(predicted_events[0])
            return
        self._open_movements_date(qdate)

    def _open_movements_date(self, qdate: QDate) -> None:
        mov_page = self.app.pages.get("movimentos")
        if not mov_page:
            return
        mov_page.period.setCurrentText("Personalizado")
        mov_page.start.blockSignals(True)
        mov_page.end.blockSignals(True)
        mov_page.start.setDate(qdate)
        mov_page.end.setDate(qdate)
        mov_page.start.blockSignals(False)
        mov_page.end.blockSignals(False)
        mov_page.refresh()
        self.app.goto("movimentos")

    def _on_cashflow_event(self, event: dict) -> None:
        dashboard = self.app.pages.get("dashboard")
        if dashboard and hasattr(dashboard, "_on_cashflow_event"):
            dashboard._on_cashflow_event(dict(event))
            return
        event_date = QDate.fromString(str(event.get("date") or "")[:10], "yyyy-MM-dd")
        if event_date.isValid():
            self._open_movements_date(event_date)

    def _cashflow_detail_payload(self, event: dict) -> dict:
        event_date = str(event.get("date") or "")[:10]
        source = str(event.get("source") or "")
        source_labels = {
            "recorded": "Movimento já registado",
            "subscription": "Subscrição prevista",
            "scheduled": "Pagamento planeado",
            "recurring_income": "Receita recorrente estimada",
        }
        payload = movement_detail_payload(
            self.repo,
            self.app.theme,
            title="Evento da previsão de tesouraria",
            item=event,
            start=event_date,
            end=event_date,
            category=str(event.get("category") or ""),
            kind=str(event.get("kind") or ""),
            search_text=str(event.get("name") or ""),
            subtitle=(
                f"{event.get('name') or 'Evento'} · "
                f"{source_labels.get(source, 'Evento previsto')}"
            ),
            explanation=(
                "Este marcador altera o saldo previsto na data indicada. "
                "A evidência aparece abaixo apenas quando já existe um "
                "movimento correspondente registado."
            ),
            rows_title="Movimento correspondente já registado",
            limit=6,
        )
        payload["icon"] = "📅"
        return payload

    def _commitment_detail_payload(self, item: dict) -> dict:
        events = [
            dict(event)
            for event in list(item.get("events") or [])
            if isinstance(event, dict)
        ]
        rows = []
        for event in events[:20]:
            source = str(event.get("source") or "")
            actual_date = self._display_date(event.get("date"))
            due_date = self._display_date(
                event.get("due_date")
                or event.get("linked_due_date")
                or event.get("date")
            )
            due_suffix = (
                f" · vencimento {due_date}"
                if due_date and due_date != actual_date
                else ""
            )
            rows.append(
                {
                    "date": actual_date,
                    "title": str(event.get("name") or "Compromisso"),
                    "subtitle": (
                        "Subscrição"
                        if source == "subscription"
                        else "Pagamento agendado"
                    )
                    + f" · {event.get('category') or 'Sem categoria'}"
                    + due_suffix,
                    "value": eur(
                        abs(safe_float(event.get("amount"))),
                        self.app.theme.privacy,
                    ),
                    "tone": "negative",
                }
            )
        return {
            "icon": "🔒",
            "title": "Compromissos fixos do mês",
            "subtitle": (
                f"{item.get('label') or item.get('month') or ''}"
                + (" · mês parcial" if bool(item.get("partial")) else "")
            ),
            "explanation": (
                "Valores confirmados em subscrições ativas e pagamentos "
                "agendados. Não inclui despesas recorrentes inferidas."
            ),
            "metrics": [
                {
                    "label": "Subscrições",
                    "value": eur(
                        item.get("subscriptions", 0),
                        self.app.theme.privacy,
                    ),
                    "tone": "warning",
                },
                {
                    "label": "Pagamentos agendados",
                    "value": eur(
                        item.get("scheduled", 0),
                        self.app.theme.privacy,
                    ),
                    "tone": "negative",
                },
                {
                    "label": "Total",
                    "value": eur(item.get("total", 0), self.app.theme.privacy),
                    "tone": "negative",
                },
                {
                    "label": "Eventos",
                    "value": str(int(item.get("event_count", 0) or 0)),
                    "tone": "neutral",
                },
            ],
            "rows_title": "Compromissos incluídos",
            "rows": rows,
            "show_empty_rows": True,
            "empty_rows_text": "Sem compromissos explícitos neste mês.",
        }

    def _runway_detail_payload(self, item: dict) -> dict:
        probability = safe_float(item.get("probability_positive"))
        tone = (
            "positive"
            if probability >= 80
            else "warning"
            if probability >= 50
            else "negative"
        )
        return {
            "icon": "🛟",
            "title": "Cenário mensal da tesouraria",
            "subtitle": str(item.get("month") or item.get("label") or ""),
            "explanation": (
                "A linha base usa a margem média recente ponderada. A faixa P10–P90 "
                "alarga com a variabilidade observada. A percentagem indica apenas "
                "a hipótese de este mês terminar acima de zero; não significa que o "
                "saldo ficou positivo durante todo o percurso anterior."
            ),
            "metrics": [
                {
                    "label": "Saldo base",
                    "value": eur(item.get("base", 0), self.app.theme.privacy),
                    "tone": "neutral",
                },
                {
                    "label": "Faixa provável",
                    "value": (
                        f"{eur(item.get('p10', 0), self.app.theme.privacy)} – "
                        f"{eur(item.get('p90', 0), self.app.theme.privacy)}"
                    ),
                    "tone": "neutral",
                },
                {
                    "label": "Este mês termina acima de 0 €",
                    "value": f"{probability:.1f}%".replace(".", ","),
                    "tone": tone,
                },
            ],
        }

    def _recurring_timeline_detail_payload(self, item: dict) -> dict:
        events = [
            dict(event)
            for event in list(item.get("events") or [])
            if isinstance(event, dict)
        ]
        rows = [
            {
                "date": str(event.get("date") or "")[:10],
                "title": str(event.get("name") or "Compromisso"),
                "subtitle": (
                    f"{event.get('status') or 'Sem estado'} · "
                    f"{event.get('category') or 'Sem categoria'}"
                ),
                "value": eur(event.get("amount", 0), self.app.theme.privacy),
                "tone": (
                    "positive"
                    if event.get("status_key") == "confirmed"
                    else "warning"
                    if event.get("status_key") == "probable"
                    else "negative"
                ),
            }
            for event in events[:18]
        ]
        return {
            "icon": "🧭",
            "title": "Compromissos recorrentes do mês",
            "subtitle": str(item.get("month") or item.get("label") or ""),
            "explanation": (
                "Só a coluna confirmada representa obrigações explícitas. Os padrões "
                "prováveis e os valores por associar exigem validação do utilizador."
            ),
            "metrics": [
                {
                    "label": "Confirmado",
                    "value": eur(item.get("confirmed", 0), self.app.theme.privacy),
                    "tone": "positive",
                },
                {
                    "label": "Provável",
                    "value": eur(item.get("probable", 0), self.app.theme.privacy),
                    "tone": "warning",
                },
                {
                    "label": "Por associar",
                    "value": eur(item.get("unlinked", 0), self.app.theme.privacy),
                    "tone": "negative",
                },
            ],
            "rows_title": "Eventos incluídos",
            "rows": rows,
            "show_empty_rows": True,
            "empty_rows_text": "Sem eventos neste mês.",
        }

    def _budget_variance_detail_payload(self, item: dict) -> dict:
        variance = safe_float(item.get("variance"))
        return {
            "icon": "🧩",
            "title": str(item.get("category") or "Categoria"),
            "subtitle": str(item.get("month_label") or item.get("month") or ""),
            "explanation": (
                "Compara o valor realizado com o orçamento específico desse mês; "
                "quando não existe limite, a despesa fica explicitamente assinalada."
            ),
            "metrics": [
                {
                    "label": "Realizado",
                    "value": eur(item.get("spent", 0), self.app.theme.privacy),
                    "tone": "negative",
                },
                {
                    "label": "Orçamento",
                    "value": eur(item.get("budget", 0), self.app.theme.privacy),
                    "tone": "neutral",
                },
                {
                    "label": "Desvio",
                    "value": eur(variance, self.app.theme.privacy),
                    "tone": "negative" if variance > 0 else "positive",
                },
                {
                    "label": "Estado",
                    "value": str(item.get("status") or "—"),
                    "tone": str(item.get("tone") or "neutral"),
                },
            ],
        }

    @staticmethod
    def _rgba(color_value: str, alpha: int) -> str:
        color = QColor(color_value)
        return (
            f"rgba({color.red()},{color.green()},{color.blue()},"
            f"{max(0, min(255, alpha))})"
        )

    @staticmethod
    def _event_accent(events: list[dict]) -> str:
        if any(bool(event.get("overdue")) for event in events):
            return RED
        if any(str(event.get("source")) == "scheduled" for event in events):
            return RED
        if any(safe_float(event.get("amount")) > 0 for event in events):
            return GREEN
        if any(str(event.get("source")) == "subscription" for event in events):
            return ORANGE
        return BLUE

    def _mark_cashflow_dates(self, events: list[dict]) -> None:
        for date_text in self._cashflow_marked_dates:
            previous = QDate.fromString(date_text, "yyyy-MM-dd")
            if previous.isValid():
                self.calendar.setDateTextFormat(previous, QTextCharFormat())

        grouped: dict[str, list[dict]] = {}
        for event in events:
            date_text = str(event.get("date") or "")[:10]
            qdate = QDate.fromString(date_text, "yyyy-MM-dd")
            if qdate.isValid():
                grouped.setdefault(date_text, []).append(dict(event))

        for date_text, day_events in grouped.items():
            accent = QColor(self._event_accent(day_events))
            background = QColor(accent)
            background.setAlpha(30)
            date_format = QTextCharFormat()
            date_format.setForeground(accent)
            date_format.setBackground(background)
            date_format.setFontUnderline(True)
            date_format.setUnderlineColor(accent)
            font = date_format.font()
            font.setWeight(QFont.Weight.DemiBold)
            date_format.setFont(font)
            self.calendar.setDateTextFormat(
                QDate.fromString(date_text, "yyyy-MM-dd"), date_format
            )

        self._cashflow_marked_dates = set(grouped)
        self._cashflow_events_by_date = grouped

    @staticmethod
    def _display_date(value: object) -> str:
        try:
            return date.fromisoformat(str(value)[:10]).strftime("%d/%m")
        except (TypeError, ValueError):
            return str(value or "")

    def _add_event_button(self, event: dict) -> None:
        source = str(event.get("source") or "")
        source_info = {
            "recurring_income": ("💶", "Receita prevista", GREEN),
            "subscription": ("🔁", "Subscrição", ORANGE),
            "scheduled": ("🧾", "Pagamento agendado", RED),
            "recorded": ("✅", "Já registado", BLUE),
        }
        icon, source_label, accent = source_info.get(source, ("📌", "Evento", BLUE))
        amount = safe_float(event.get("amount"))
        amount_label = (
            f"{'+' if amount > 0 else '−'}{eur(abs(amount), self.app.theme.privacy)}"
        )
        event_date = self._display_date(event.get("date"))
        name = str(event.get("name") or source_label)
        overdue_label = " · ⚠️ vencido" if bool(event.get("overdue")) else ""
        row = QFrame()
        row.setObjectName("CashflowEventRow")
        row.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        row.setAccessibleName(f"{source_label}, {name}, {event_date}, {amount_label}")
        row.setToolTip(
            "Consulta a data, o tipo e o valor; usa Abrir para ver detalhes."
        )
        row.setStyleSheet(
            "QFrame#CashflowEventRow { border: none; border-left: 3px solid "
            f"{accent}; border-radius: 10px; background: {self._rgba(accent, 22)}; }}"
        )
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(10, 8, 8, 8)
        row_layout.setSpacing(9)

        icon_label = QLabel(icon)
        icon_label.setAlignment(Qt.AlignCenter)
        icon_label.setFixedSize(32, 32)
        icon_label.setStyleSheet(
            f"background: {self._rgba(accent, 38)}; border-radius: 9px;"
        )
        row_layout.addWidget(icon_label, 0, Qt.AlignVCenter)

        details_layout = QVBoxLayout()
        details_layout.setContentsMargins(0, 0, 0, 0)
        details_layout.setSpacing(2)
        name_label = QLabel(name)
        name_label.setWordWrap(True)
        name_label.setStyleSheet("font-weight: 700;")
        meta_label = QLabel(f"{event_date} · {source_label}{overdue_label}")
        meta_label.setObjectName("Subtle")
        meta_label.setWordWrap(True)
        details_layout.addWidget(name_label)
        details_layout.addWidget(meta_label)
        row_layout.addLayout(details_layout, 1)

        action_layout = QVBoxLayout()
        action_layout.setContentsMargins(0, 0, 0, 0)
        action_layout.setSpacing(3)
        amount = QLabel(amount_label)
        amount.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        amount.setStyleSheet(f"font-weight: 800; color: {accent};")
        action = QPushButton("Abrir")
        action.setObjectName("Secondary")
        action.setCursor(Qt.PointingHandCursor)
        action.setFixedWidth(72)
        action.setToolTip("Abrir detalhes deste evento")
        action_layout.addWidget(amount, 0, Qt.AlignRight)
        action_layout.addWidget(action, 0, Qt.AlignRight)
        row_layout.addLayout(action_layout)

        event_payload = dict(event)
        action.clicked.connect(
            lambda _checked=False, selected=event_payload: self._on_cashflow_event(
                selected
            )
        )
        self.recurring_events_layout.addWidget(row)

    def _dismissed_alerts(self) -> set[str]:
        try:
            values = json.loads(self.repo.setting("dismissed_financial_alerts", "[]"))
        except (TypeError, ValueError, json.JSONDecodeError):
            values = []
        return {str(value) for value in values}

    def _product_review(self):
        injected = getattr(self.app, "product_review_service", None)
        return injected if injected is not None else ProductReviewService(self.repo)

    def _fill_alert_table(self, _index: int = -1) -> None:
        selected_filter = str(self.alert_filter.currentData() or "all")
        dismissed = self._dismissed_alerts()
        self._visible_alerts = [
            alert
            for alert in self._product_review().visible_alerts(self._alerts_data)
            if str(alert.get("key")) not in dismissed
            and (
                selected_filter == "all"
                or str(alert.get("severity")) == selected_filter
            )
        ]
        self.alert_table.clearContents()
        self.alert_table.setRowCount(len(self._visible_alerts))
        severity_info = {
            "critical": ("🚨 Crítico", RED),
            "warning": ("⚠️ Atenção", ORANGE),
            "info": ("ℹ️ Informação", BLUE),
        }
        for row, alert in enumerate(self._visible_alerts):
            label, color = severity_info.get(
                str(alert.get("severity")), ("ℹ️ Informação", BLUE)
            )
            values = [
                label,
                str(alert.get("date") or "—")[:10],
                ("Dados ocultos" if self.app.theme.privacy else
                 f"{alert.get('origin')} · {alert.get('title')} · {alert.get('message')}"),
                ("•••" if self.app.theme.privacy else alert.get("category") or "—"),
                eur(alert.get("amount", 0), self.app.theme.privacy),
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if column == 2:
                    item.setToolTip("Dados ocultos" if self.app.theme.privacy else html.escape(
                        f"Origem: {alert.get('origin')}\nPrazo: {alert.get('deadline') or 'Sem prazo definido'}"
                    ))
                if column == 0:
                    item.setForeground(QColor(color))
                if column == 4:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self.alert_table.setItem(row, column, item)

    def _selected_alert(self) -> Optional[dict]:
        row = self.alert_table.currentRow()
        if 0 <= row < len(getattr(self, "_visible_alerts", [])):
            return self._visible_alerts[row]
        return None

    def _open_selected_alert(self) -> None:
        alert = self._selected_alert()
        if alert is None:
            return
        page = str(alert.get("action_page") or "centro")
        action = alert.get("action")
        if action in {"reimbursements", "subscription_radar"}:
            self.app.goto(page)
            target = self.app.pages.get(page)
            method = "open_reimbursements" if action == "reimbursements" else "open_price_radar"
            callback = getattr(target, method, None)
            if callable(callback):
                callback()
            return
        if page == "movimentos":
            movements = self.app.pages.get("movimentos")
            if movements is not None:
                movements.search.setText(str(alert.get("search") or ""))
                qdate = QDate.fromString(
                    str(alert.get("start") or alert.get("date") or "")[:10], "yyyy-MM-dd"
                )
                if qdate.isValid():
                    movements.period.setCurrentText("Personalizado")
                    movements.start.setDate(qdate)
                    end_date = QDate.fromString(str(alert.get("end") or alert.get("date") or "")[:10], "yyyy-MM-dd")
                    movements.end.setDate(end_date if end_date.isValid() else qdate)
                if action == "missing_documents":
                    movements.attach_filter.setCurrentText("Sem anexos")
                    movements.kind.setCurrentText("Despesas")
                movements.refresh()
        self.app.goto(page)

    def _dismiss_selected_alert(self) -> None:
        alert = self._selected_alert()
        if alert is None:
            return
        self._product_review().dismiss(str(alert.get("key")))
        self._fill_alert_table()

    def _restore_alerts(self) -> None:
        self._product_review().reset_choices()
        self._fill_alert_table()

    def _snooze_selected_alert(self) -> None:
        alert = self._selected_alert()
        if alert is not None:
            self._product_review().snooze(str(alert["key"]), date.today() + timedelta(days=7))
            self._fill_alert_table()

    def _open_quality_checklist(self) -> None:
        from ui.product_review import QualityChecklistDialog

        dialog = QualityChecklistDialog(self.app, self)
        self._quality_dialog = dialog
        try:
            dialog.exec()
        finally:
            self._quality_dialog = None
            dialog.deleteLater()

    def update_export_privacy_labels(self) -> None:
        if hasattr(self, "_alerts_data"):
            self._fill_alert_table()
        dialog = getattr(self, "_quality_dialog", None)
        if dialog is not None:
            dialog.update_privacy()

    def refresh(self) -> None:
        self.wipe(self.forecast)
        self._clear_event_rows()
        today = date.today()
        cashflow = self.analytics_service.cashflow_calendar_forecast(90, today)
        cashflow_coverage = dict(cashflow.get("data_coverage") or {})
        cashflow_coverage_text = self.data_coverage_text(cashflow_coverage)
        cashflow_coverage_tone = (
            "positive"
            if int(cashflow_coverage.get("active_months", 0) or 0) >= 6
            else "warning"
        )
        self.forecast.set_data_coverage(
            cashflow_coverage_text,
            cashflow_coverage_tone,
        )
        self.cashflow_card.set_data_coverage(
            cashflow_coverage_text,
            cashflow_coverage_tone,
        )
        summary = (cashflow.get("summaries") or {}).get("60", {})
        calibration = dict(cashflow.get("calibration") or {})
        starting = safe_float(cashflow.get("starting_balance"))
        ending = safe_float(summary.get("ending_balance"))
        minimum = safe_float(summary.get("minimum_balance"))
        minimum_date = self._display_date(summary.get("minimum_date"))
        tone = str(summary.get("tone") or "neutral")
        tone_info = {
            "positive": ("✅", GREEN),
            "warning": ("⚠️", ORANGE),
            "negative": ("🚨", RED),
            "neutral": ("🧭", BLUE),
        }
        status_icon, status_color = tone_info.get(tone, tone_info["neutral"])

        apply_calendar_theme(self.calendar, self.app.theme)
        self._mark_cashflow_dates(list(cashflow.get("events") or []))
        self.cashflow_chart.set_data(cashflow, 60)
        self.cashflow_hint.setText(
            f"{cashflow.get('method_label', '')} "
            "Passa o rato pelo gráfico e seleciona um marcador para abrir os detalhes."
        )
        if self.should_compute_card(self.commitments_card) and hasattr(
            self.analytics_service, "fixed_commitments_evolution"
        ):
            commitments = self.analytics_service.fixed_commitments_evolution(
                12,
                today,
            )
        else:
            commitments = {
                "rows": [],
                "monthly_average": 0.0,
                "peak_month": "",
                "peak_total": 0.0,
                "method_label": "",
            }
        self.commitments_chart.set_data(commitments)
        commitments_coverage = dict(commitments.get("data_coverage") or {})
        if commitments_coverage:
            self.commitments_card.set_data_coverage(
                self.data_coverage_text(commitments_coverage),
                "positive"
                if int(commitments_coverage.get("active_months", 0) or 0) >= 6
                else "warning",
            )
        peak_month = str(commitments.get("peak_month") or "")
        try:
            peak_label = date.fromisoformat(f"{peak_month}-01").strftime("%m/%Y")
        except (TypeError, ValueError):
            peak_label = "—"
        trend = commitments.get("trend_pct")
        trend_label = (
            (
                f"{safe_float(trend):+.1f}% entre os primeiros e últimos "
                "meses completos"
            ).replace(".", ",")
            if trend is not None
            else "tendência ainda sem base comparável"
        )
        partial_total = safe_float(commitments.get("partial_month_total"))
        partial_label = (
            f" · restante do mês atual "
            f"<b>{eur(partial_total, self.app.theme.privacy)}</b>"
            if any(bool(row.get("partial")) for row in commitments.get("rows", []))
            else ""
        )
        complete_months = int(commitments.get("complete_months", 0) or 0)
        self.commitments_summary.setText(
            f"Média dos {complete_months} meses completos "
            f"<b>{eur(commitments.get('monthly_average', 0), self.app.theme.privacy)}</b>"
            f"{partial_label} · pico "
            f"<b>{eur(commitments.get('peak_total', 0), self.app.theme.privacy)}</b> "
            f"em {peak_label} · {trend_label}. "
            f"{commitments.get('method_label', '')}"
        )

        runway_provider = getattr(
            self.analytics_service,
            "cash_runway_scenarios",
            None,
        )
        runway = (
            runway_provider(12, today)
            if self.should_compute_card(self.runway_card) and callable(runway_provider)
            else {"rows": [], "method_label": ""}
        )
        self.runway_chart.set_projection(runway)
        runway_coverage = dict(runway.get("data_coverage") or {})
        if runway_coverage:
            self.runway_card.set_data_coverage(
                self.data_coverage_text(runway_coverage),
                "positive"
                if int(runway_coverage.get("active_months", 0) or 0) >= 6
                else "warning",
            )
        probability = safe_float(
            runway.get(
                "final_month_probability_positive",
                runway.get("ending_probability_positive"),
            )
        )
        breach_month = runway.get("breach_month")
        breach_label = (
            f" · saldo base cruza zero no mês {int(breach_month)}"
            if breach_month is not None
            else " · saldo base não cruza zero neste horizonte"
        )
        self.runway_summary.setText(
            f"<b>{probability:.0f}%</b> de probabilidade estimada de saldo positivo "
            f"no fim do mês 12{breach_label}. Não significa que o saldo se manteve "
            f"positivo em todos os meses anteriores. {runway.get('method_label', '')}"
        )

        recurring_provider = getattr(
            self.analytics_service,
            "recurring_commitment_timeline",
            None,
        )
        recurring_timeline = (
            recurring_provider(6, today)
            if self.should_compute_card(self.recurring_timeline_card)
            and callable(recurring_provider)
            else {"rows": [], "method_label": ""}
        )
        self.recurring_timeline_chart.set_data(recurring_timeline)
        self.recurring_timeline_summary.setText(
            f"Confirmado <b>{eur(recurring_timeline.get('confirmed_total', 0), self.app.theme.privacy)}</b> · "
            f"provável <b>{eur(recurring_timeline.get('probable_total', 0), self.app.theme.privacy)}</b> · "
            f"por associar <b>{eur(recurring_timeline.get('unlinked_total', 0), self.app.theme.privacy)}</b>. "
            f"{recurring_timeline.get('method_label', '')}"
        )

        variance_provider = getattr(
            self.analytics_service,
            "budget_variance_heatmap",
            None,
        )
        budget_variance = (
            variance_provider(12, today)
            if self.should_compute_card(self.budget_variance_card)
            and callable(variance_provider)
            else {"months": [], "categories": [], "cells": []}
        )
        self.budget_variance_chart.set_data(budget_variance)
        self.budget_variance_summary.setText(
            f"<b>{int(budget_variance.get('category_count', 0) or 0)}</b> categorias · "
            f"<b>{int(budget_variance.get('over_count', 0) or 0)}</b> células ultrapassadas · "
            f"<b>{int(budget_variance.get('warning_count', 0) or 0)}</b> perto do limite. "
            "Seleciona uma célula para ver valores e abrir os orçamentos."
        )

        forecast_value = QLabel(
            f"<span style='font-size:26px;font-weight:800'>{eur(ending, self.app.theme.privacy)}</span>"
            f"<br><span style='color:{self.app.theme.muted}'>saldo previsto dentro de 60 dias</span>"
        )
        forecast_value.setWordWrap(True)
        self.forecast.v.addWidget(forecast_value)
        status = QLabel(
            f"{status_icon} {summary.get('status', 'Sem previsão disponível')}"
        )
        status.setWordWrap(True)
        status.setStyleSheet(
            "font-weight: 700; padding: 7px 10px; "
            f"background: {self._rgba(status_color, 28)}; "
            f"border: 1px solid {self._rgba(status_color, 72)}; "
            "border-radius: 10px;"
        )
        self.forecast.v.addWidget(status)
        if bool(calibration.get("enabled")):
            learned = int(calibration.get("sample_months", 0) or 0)
            validated = int(calibration.get("validation_months", 0) or 0)
            learning_detail = f"<br>🧠 Aprendizagem: <b>{learned} meses</b>"
            if bool(calibration.get("accuracy_available")):
                learning_detail += (
                    f" · <b>{validated} validados sem antecipar o resultado · "
                    f"{safe_float(calibration.get('accuracy_pct')):.0f}% "
                    "dentro das faixas previstas</b>"
                )
            else:
                learning_detail += " · <b>cobertura das faixas ainda em validação</b>"
        else:
            learning_detail = (
                f"<br>🧠 Aprendizagem: <b>"
                f"{int(calibration.get('sample_months', 0) or 0)}/"
                f"{int(calibration.get('required_months', 3) or 3)} meses</b>"
            )
        forecast_details = QLabel(
            f"💰 Hoje: <b>{eur(starting, self.app.theme.privacy)}</b><br>"
            f"🛟 Mínimo: <b>{eur(minimum, self.app.theme.privacy)}</b> em {minimum_date}<br>"
            f"📥 Entradas: <b>{eur(summary.get('expected_income', 0), self.app.theme.privacy)}</b> · "
            f"📤 Saídas: <b>{eur(summary.get('total_outflow', 0), self.app.theme.privacy)}</b>"
            f"{learning_detail}"
        )
        forecast_details.setWordWrap(True)
        self.forecast.v.addWidget(forecast_details)

        self._alerts_data = (
            self.analytics_service.financial_alert_center(today)
            if self.should_compute_card(self.alerts)
            else []
        )
        if self.should_compute_card(self.alerts):
            self._alerts_data.extend(self._product_review().administrative_items(as_of=today))
        overdue_count = int(summary.get("overdue_count", 0) or 0)
        if overdue_count:
            self._alerts_data.insert(
                0,
                {
                    "key": f"cashflow-overdue:{today.isoformat()}",
                    "severity": "critical",
                    "title": "Pagamentos vencidos previstos",
                    "message": f"Existem {overdue_count} compromissos vencidos na previsão.",
                    "date": today.isoformat(),
                    "amount": summary.get("known_commitments", 0),
                    "category": "Tesouraria",
                    "action_page": "dashboard",
                },
            )
        self._fill_alert_table()

        events = list(cashflow.get("events") or [])
        if not events:
            self.recurring_summary.setText(
                "Sem compromissos conhecidos neste horizonte."
            )
            empty = QLabel("✨ Sem eventos conhecidos nos próximos 90 dias.")
            empty.setObjectName("Subtle")
            empty.setWordWrap(True)
            self.recurring_events_layout.addWidget(empty)
        else:
            self.recurring_summary.setText(
                f"{len(events)} evento(s) nos próximos 90 dias · "
                "desloca para consultar todos."
            )
        for event in events:
            self._add_event_button(dict(event))


class DocumentsPage(BasePage):
    audit_progress = Signal(int, int, str)

    def __init__(self, app: "LiquidFinanceApp"):
        super().__init__(
            app,
            "Documentos e faturas",
            "Anexos associados aos movimentos e faturas arquivadas",
        )
        top = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Pesquisar ficheiro, fornecedor, descrição...")
        self.search.setMinimumWidth(260)
        self.open_folder = QPushButton("Abrir pasta de anexos")
        self.refresh_btn = QPushButton("Atualizar")
        top.addWidget(self.search, 1)
        top.addWidget(self.refresh_btn)
        top.addWidget(self.open_folder)
        self.storage_card = GlassCard("🗂️ Integridade do arquivo", strong=True)
        self.storage_status = QLabel()
        self.storage_status.setWordWrap(True)
        self.storage_status.setObjectName("DocumentStorageStatus")
        storage_actions_wrap = QWidget()
        storage_actions = FlowLayout(
            storage_actions_wrap, margin=0, hspacing=6, vspacing=6
        )
        self._storage_audit: dict = {}
        self._pending_audit = False
        self._pending_force_audit = False
        self.audit_task = AsyncTask(
            self,
            registry=app.background_task_registry,
            label="Verificação do arquivo de documentos",
        )
        self.audit_task.result.connect(self._on_audit_result)
        self.audit_task.error.connect(self._on_audit_error)
        self.audit_task.finished.connect(self._on_audit_finished)
        self.audit_progress.connect(self._on_audit_progress)
        self.audit_storage_btn = QPushButton("🔄 Verificar agora")
        self.audit_storage_btn.setObjectName("Secondary")
        self.cleanup_orphans_btn = QPushButton("🧹 Limpar ficheiros órfãos")
        self.cleanup_missing_btn = QPushButton("🩹 Remover referências em falta")
        self.cancel_audit_btn = QPushButton("Cancelar verificação")
        self.cancel_audit_btn.setObjectName("Secondary")
        self.cancel_audit_btn.setVisible(False)
        storage_actions.addWidget(self.audit_storage_btn)
        storage_actions.addWidget(self.cleanup_orphans_btn)
        storage_actions.addWidget(self.cleanup_missing_btn)
        storage_actions.addWidget(self.cancel_audit_btn)
        self.audit_progress_bar = QProgressBar()
        self.audit_progress_bar.setRange(0, 100)
        self.audit_progress_bar.setTextVisible(True)
        self.audit_progress_bar.setVisible(False)
        self.storage_card.v.addWidget(self.storage_status)
        self.storage_card.v.addWidget(self.audit_progress_bar)
        self.storage_card.v.addWidget(storage_actions_wrap)

        self.table = QTableWidget(0, 8)
        self.table.setProperty("columnPersistenceId", "documents.list")
        self.table.setHorizontalHeaderLabels(
            [
                "ID",
                "Data",
                "Movimento",
                "Ficheiro",
                "Tipo",
                "Descrição",
                "Estado",
                "Caminho",
            ]
        )
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.table.setColumnHidden(0, True)
        self.table.setColumnHidden(7, True)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        card = GlassCard("Arquivo", strong=True)
        card.v.addWidget(self.table)
        self.documents_table_status = QLabel()
        self.documents_table_status.setObjectName("Subtle")
        self.documents_table_status.setWordWrap(True)
        self.documents_load_more_btn = QPushButton("Carregar mais")
        self.documents_load_more_btn.setObjectName("Secondary")
        self.documents_load_more_btn.setVisible(False)
        archive_status = QHBoxLayout()
        archive_status.setContentsMargins(0, 0, 0, 0)
        archive_status.setSpacing(12)
        archive_status.addWidget(self.documents_table_status, 0, Qt.AlignVCenter)
        archive_status.addWidget(self.documents_load_more_btn, 0, Qt.AlignVCenter)
        archive_status.addStretch(1)
        card.v.addLayout(archive_status)

        self.body.addLayout(top)
        self.body.addWidget(self.storage_card)
        self.body.addWidget(card)
        self.refresh_btn.clicked.connect(lambda: self.refresh())
        self.audit_storage_btn.clicked.connect(
            lambda: self._start_storage_audit(force=True)
        )
        self.cancel_audit_btn.clicked.connect(self.audit_task.cancel)
        self.cleanup_orphans_btn.clicked.connect(self.cleanup_orphans)
        self.cleanup_missing_btn.clicked.connect(self.cleanup_missing_references)
        self.documents_load_more_btn.clicked.connect(self.load_more_documents)
        self.open_folder.clicked.connect(
            lambda: open_path(self.app.document_service.attachments_directory)
        )
        self.search.returnPressed.connect(lambda: self.refresh())
        self.table.doubleClicked.connect(self.open_selected)
        self._documents_page_size = 200
        self._documents_loaded_limit = self._documents_page_size
        self._documents_total = 0
        self.refresh()

    @staticmethod
    def _size_label(value: object) -> str:
        size = max(0, int(value or 0))
        if size < 1024:
            return f"{size} B"
        if size < 1024 * 1024:
            return f"{size / 1024:.1f} KB"
        if size < 1024 * 1024 * 1024:
            return f"{size / (1024 * 1024):.1f} MB"
        return f"{size / (1024 * 1024 * 1024):.1f} GB"

    @staticmethod
    def _status_background(color_value: str, alpha: int) -> str:
        color = QColor(color_value)
        return (
            f"rgba({color.red()},{color.green()},{color.blue()},"
            f"{max(0, min(255, alpha))})"
        )

    def _apply_storage_status(self, audit: dict) -> None:
        missing = int(audit.get("missing_records", 0) or 0)
        outside = int(audit.get("outside_records", 0) or 0)
        orphans = int(audit.get("orphan_count", 0) or 0)
        available = int(audit.get("available_records", 0) or 0)
        verified = int(audit.get("verified_records", 0) or 0)
        altered = int(audit.get("altered_records", 0) or 0)
        unverified = int(audit.get("unverified_records", 0) or 0)
        total = int(audit.get("total_records", 0) or 0)
        size = self._size_label(audit.get("managed_bytes", 0))
        healthy = bool(audit.get("healthy", False))
        accent = GREEN if healthy else ORANGE
        if healthy:
            text = (
                f"✅ Arquivo consistente · {verified}/{total} assinatura(s) "
                f"verificada(s) · {available}/{total} disponível(is) · "
                f"{size} geridos pela aplicação."
            )
        else:
            text = (
                f"⚠️ Revisão necessária · {available}/{total} disponíveis · "
                f"{missing} referência(s) sem ficheiro · {orphans} ficheiro(s) órfão(s) "
                f"· {outside} caminho(s) fora do arquivo · {altered} alterado(s) "
                f"· {unverified} por verificar."
            )
        self.storage_status.setText(text)
        self.storage_status.setAccessibleName(text)
        self.storage_status.setStyleSheet(
            "font-weight: 700; padding: 10px 12px; border-radius: 10px; "
            f"background: {self._status_background(accent, 24)}; "
            f"border: 1px solid {self._status_background(accent, 82)};"
        )
        self.cleanup_orphans_btn.setEnabled(orphans > 0)
        self.cleanup_missing_btn.setEnabled(missing > 0)

    def refresh(
        self,
        *_args,
        reset_limit: bool = True,
        force_audit: bool = False,
        start_audit: bool = True,
    ) -> None:
        if reset_limit:
            self._documents_loaded_limit = self._documents_page_size
        cached = self.app.document_service.cached_storage_audit()
        if cached is not None and not force_audit:
            self._storage_audit = cached
        audit = self._storage_audit
        if audit:
            self._apply_storage_status(audit)
        else:
            self.storage_status.setText(
                "🔄 A lista já está disponível; a integridade é verificada em segundo plano."
            )
        missing_ids = {int(value) for value in audit.get("missing_record_ids", [])}
        outside_ids = {int(value) for value in audit.get("outside_record_ids", [])}
        altered_ids = {int(value) for value in audit.get("altered_record_ids", [])}
        unverified_ids = {
            int(value) for value in audit.get("unverified_record_ids", [])
        }
        verified_ids = {int(value) for value in audit.get("verified_record_ids", [])}
        audit_ready = bool(audit)
        search = self.search.text()
        total = self.repo.document_attachment_count(search)
        rows = self.repo.document_attachments(
            search, limit=self._documents_loaded_limit
        )
        self._documents_total = total
        self.table.clearContents()
        self.table.setRowCount(len(rows))
        for row, r in enumerate(rows):
            attachment_id = int(r["id"])
            if attachment_id in altered_ids:
                status_text, status_color = "⛔ Alterado", RED
            elif attachment_id in outside_ids:
                status_text, status_color = "🔒 Fora do arquivo", ORANGE
            elif attachment_id in missing_ids:
                status_text, status_color = "⚠️ Em falta", RED
            elif attachment_id in unverified_ids:
                status_text, status_color = "⚠️ Não verificado", ORANGE
            elif audit_ready and attachment_id in verified_ids:
                status_text, status_color = "✅ Verificado", GREEN
            else:
                status_text, status_color = "⏳ Por verificar", ORANGE
            vals = [
                attachment_id,
                r["tx_date"] or r["created_at"][:10],
                r["tx_desc"] or f"Movimento {r['transaction_id']}",
                r["file_name"],
                r["file_type"],
                r["description"],
                status_text,
                r["stored_path"],
            ]
            for col, val in enumerate(vals):
                item = QTableWidgetItem(str(val))
                if col == 6:
                    item.setForeground(QColor(status_color))
                    item.setToolTip(
                        "Disponível no arquivo gerido pela app"
                        if status_text.startswith("✅")
                        else status_text[2:]
                    )
                self.table.setItem(row, col, item)
        loaded = len(rows)
        remaining = max(0, total - loaded)
        self.documents_load_more_btn.setVisible(remaining > 0)
        if remaining > 0:
            next_count = min(self._documents_page_size, remaining)
            self.documents_load_more_btn.setText(f"Carregar mais {next_count}")
            self.documents_table_status.setText(
                f"A mostrar {loaded} de {total} documentos."
            )
        else:
            self.documents_table_status.setText(
                f"{total} documento(s) no arquivo."
                if total
                else "Sem documentos para esta pesquisa."
            )
        if start_audit and (force_audit or cached is None):
            self._start_storage_audit(force=force_audit)

    def _start_storage_audit(self, *, force: bool) -> None:
        if self.audit_task.is_running():
            self._pending_audit = True
            self._pending_force_audit = self._pending_force_audit or bool(force)
            return
        operation = self.app.document_service.audit_storage_operation(
            force=force,
            progress_callback=self.audit_progress.emit,
        )
        self.audit_storage_btn.setEnabled(False)
        self.cancel_audit_btn.setVisible(True)
        self.audit_progress_bar.setVisible(True)
        self.audit_progress_bar.setRange(0, 0)
        self.audit_progress_bar.setFormat("A preparar a verificação…")
        self.storage_status.setText(
            "🔍 A verificar apenas os documentos novos ou alterados…"
            if not force
            else "🔍 Verificação completa em curso…"
        )
        if not self.audit_task.start(operation):
            self._on_audit_error("Não foi possível iniciar a verificação.")
            self._on_audit_finished(False)

    def _on_audit_progress(self, current: int, total: int, detail: str) -> None:
        if total > 0:
            self.audit_progress_bar.setRange(0, total)
            self.audit_progress_bar.setValue(max(0, min(int(current), int(total))))
            self.audit_progress_bar.setFormat(f"%v/%m · {detail}")
        else:
            self.audit_progress_bar.setRange(0, 0)
            self.audit_progress_bar.setFormat(detail or "A verificar…")

    def _on_audit_result(self, result: object) -> None:
        self._storage_audit = dict(result or {})
        if bool(self._storage_audit.get("stale")):
            self._pending_audit = True
        self._apply_storage_status(self._storage_audit)
        self.refresh(reset_limit=False, start_audit=False)

    def _on_audit_error(self, message: str) -> None:
        self.storage_status.setText(
            "⚠️ A verificação não foi concluída. A lista de documentos permanece "
            f"disponível. Detalhe: {message}"
        )

    def _on_audit_finished(self, cancelled: bool) -> None:
        self.audit_storage_btn.setEnabled(True)
        self.cancel_audit_btn.setVisible(False)
        self.audit_progress_bar.setVisible(False)
        pending_audit = self._pending_audit
        pending_force = self._pending_force_audit
        self._pending_audit = False
        self._pending_force_audit = False
        if cancelled and not pending_audit:
            self.storage_status.setText(
                "Verificação cancelada; nenhum ficheiro do arquivo foi alterado."
            )
        if pending_audit:
            self._start_storage_audit(force=pending_force)

    def load_more_documents(self) -> None:
        if self._documents_loaded_limit >= self._documents_total:
            return
        self._documents_loaded_limit = min(
            self._documents_total,
            self._documents_loaded_limit + self._documents_page_size,
        )
        self.refresh(reset_limit=False)

    def cleanup_orphans(self) -> None:
        count = int(self._storage_audit.get("orphan_count", 0) or 0)
        if count <= 0:
            QMessageBox.information(
                self, "Arquivo de documentos", "Não existem ficheiros órfãos."
            )
            return
        reply = QMessageBox.question(
            self,
            "Arquivo de documentos",
            f"Eliminar {count} ficheiro(s) que não estão associados a nenhum movimento?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        try:
            removed = self.app.document_service.remove_orphan_files()
        except Exception as exc:
            log_exception("Failed to clean orphan document files", exc)
            QMessageBox.critical(
                self,
                "Arquivo de documentos",
                "Não foi possível concluir a limpeza do arquivo.",
            )
            return
        self.refresh(force_audit=True)
        QMessageBox.information(
            self,
            "Arquivo de documentos",
            f"Foram removidos {removed} ficheiro(s) órfão(s).",
        )

    def cleanup_missing_references(self) -> None:
        count = int(self._storage_audit.get("missing_records", 0) or 0)
        if count <= 0:
            QMessageBox.information(
                self, "Arquivo de documentos", "Não existem referências em falta."
            )
            return
        reply = QMessageBox.question(
            self,
            "Arquivo de documentos",
            f"Remover {count} referência(s) cujo ficheiro já não existe?\n\n"
            "Os movimentos financeiros não serão eliminados.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        try:
            removed = self.app.document_service.remove_missing_references()
        except Exception as exc:
            log_exception("Failed to clean missing document references", exc)
            QMessageBox.critical(
                self,
                "Arquivo de documentos",
                "Não foi possível remover as referências em falta.",
            )
            return
        self.refresh(force_audit=True)
        QMessageBox.information(
            self,
            "Arquivo de documentos",
            f"Foram removidas {removed} referência(s) em falta.",
        )

    def open_selected(self) -> None:
        row = self.table.currentRow()
        if row < 0:
            return
        id_item = self.table.item(row, 0)
        attachment_id = int(id_item.text()) if id_item is not None else 0
        record = self.repo.transaction_attachment(attachment_id)
        if record is None:
            QMessageBox.warning(
                self,
                "Arquivo de documentos",
                "O registo deste anexo já não existe.",
            )
            self.refresh(reset_limit=False, force_audit=True)
            return
        try:
            verified = self.app.document_service.verify_attachment_record(record)
        except FileNotFoundError:
            message = "O ficheiro já não existe."
        except AttachmentIntegrityError as exc:
            message = f"O anexo foi bloqueado porque mudou depois de guardado:\n\n{exc}"
        except (AttachmentValidationError, OSError) as exc:
            message = f"O anexo não passou a validação de segurança:\n\n{exc}"
        else:
            path = Path(verified["path"])
            if bool(verified.get("internal_preview")):
                AttachmentPreviewDialog(path, self).exec()
                self.refresh(reset_limit=False)
                return
            if bool(verified.get("active_warning")):
                answer = QMessageBox.question(
                    self,
                    "Abrir documento externo",
                    "Este formato será aberto por outra aplicação e pode conter "
                    "fórmulas, ligações ou conteúdo ativo. Abre apenas se confiares "
                    "na origem.\n\nContinuar?",
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.No,
                )
                if answer != QMessageBox.Yes:
                    return
            open_path(path)
            self.refresh(reset_limit=False)
            return
        QMessageBox.warning(self, "Arquivo de documentos", message)
        self.refresh(reset_limit=False, force_audit=True)
