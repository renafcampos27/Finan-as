#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Finanças Pessoais – Liquid Glass
Nova interface PySide6 para a app local de finanças pessoais.
Mantém a base de dados e a app clássica intactas; esta é apenas uma camada visual moderna.
"""

from __future__ import annotations


import html as html_lib


import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from utils.qt_runtime import configure_qt_runtime

configure_qt_runtime()

try:
    from PySide6.QtCore import QDate, Qt
    from PySide6.QtGui import QColor
    from PySide6.QtWidgets import (
        QComboBox,
        QDateEdit,
        QFileDialog,
        QFrame,
        QGridLayout,
        QHBoxLayout,
        QHeaderView,
        QLabel,
        QMessageBox,
        QPushButton,
        QSizePolicy,
        QTableWidget,
        QTextEdit,
        QVBoxLayout,
    )
except (
    Exception
) as exc:  # pragma: no cover - only used when PySide6 is missing on the user's PC.
    print("PySide6 não está instalado. Instala com: pip install PySide6")
    print(exc)
    sys.exit(1)

from ui.components import (
    BalanceChart,
    CategoryForecastAccuracyChart,
    DataQualityTrendChart,
    DonutChart,
    ForecastAccuracyChart,
    ForecastErrorCausesChart,
    GlassCard,
    MerchantParetoChart,
    MetricCard,
    PeriodComparisonChart,
    ScoreBreakdownChart,
    ScoreGauge,
    ScoreTrendChart,
    SpendingSeasonalityChart,
)
from ui.chart_detail_support import (
    movement_detail_payload,
    movement_evidence_rows,
    open_movements_view,
    period_for_chart_item,
)
from services.data_health import DataHealthService
from services.product_review import ProductReviewService
from services.backup import BackupService
from ui.workers import AsyncTask

from utils.output_security import html_text, safe_hex_colour, privacy_export_cell
from utils.app_logging import log_exception
from ui.core import (
    BasePage,
    EdgePropagatingScrollArea,
    FlowLayout,
    prepare_date_edit,
)
from ui.core.smart_table import SmartTableWidgetItem as QTableWidgetItem
from utils.helpers import (
    APP_TITLE,
    BLUE,
    GREEN,
    ORANGE,
    PT_MONTHS,
    PURPLE,
    RED,
    eur,
    format_period_variation,
    format_iso_date,
    percent,
    rolling_month_window,
    safe_float,
    safe_year_from_combo,
)

if TYPE_CHECKING:
    from main import LiquidFinanceApp


def _private_count(value: Any, privacy: bool) -> str:
    return "•••" if privacy else str(value)


def _private_text(value: Any, privacy: bool) -> str:
    return str(privacy_export_cell(value, "description", privacy))


class MonthlyReviewPage(BasePage):
    def __init__(self, app: "LiquidFinanceApp"):
        super().__init__(
            app, "Revisão mensal", "Assistente de fecho do mês e resumo automático"
        )
        top = QHBoxLayout()
        self.year = QComboBox()
        self.year.addItems([str(y) for y in self.repo.available_years()])
        self.month = QComboBox()
        self.month.addItems([f"{i:02d} — {PT_MONTHS[i - 1]}" for i in range(1, 13)])
        self.month.setCurrentIndex(date.today().month - 1)
        self.gen = QPushButton("Rever mês")
        self.gen.setObjectName("CardAction")
        self.export_pdf = QPushButton("Exportar PDF")
        top.addWidget(QLabel("Ano"))
        top.addWidget(self.year)
        top.addWidget(QLabel("Mês"))
        top.addWidget(self.month)
        top.addStretch()
        top.addWidget(self.gen)
        top.addWidget(self.export_pdf)
        self.cards = QGridLayout()
        self.cards.setSpacing(10)
        self.register_responsive_grid(self.cards)
        self.text = QTextEdit()
        self.text.setReadOnly(True)
        self.text.setMinimumHeight(520)
        card = GlassCard("Relatório inteligente do mês", strong=True)
        card.v.addWidget(self.text)
        self.body.addLayout(top)
        self.body.addLayout(self.cards)
        self.body.addWidget(card)
        self.gen.clicked.connect(self.refresh)
        self.export_pdf.clicked.connect(self.export_month_pdf)
        self.refresh()

    def ym(self) -> str:
        year = safe_year_from_combo(self.year)
        month = max(1, min(12, self.month.currentIndex() + 1))
        return f"{year:04d}-{month:02d}"

    def _clear_cards(self):
        while self.cards.count():
            item = self.cards.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def refresh(self):
        ym = self.ym()
        data = self.analytics_service.monthly_review_data(ym)
        totals = data["totals"]
        prev = data["prev_totals"]
        income, expenses, savings = (
            totals["income"],
            totals["expenses"],
            totals["savings"],
        )
        rate = savings / income * 100 if income else 0
        self._clear_cards()
        dif_exp = expenses - prev.get("expenses", 0)
        dif_sav = savings - prev.get("savings", 0)
        metrics = [
            ("Receitas", eur(income, self.app.theme.privacy), ym, GREEN, "↓"),
            (
                "Despesas",
                eur(expenses, self.app.theme.privacy),
                f"vs mês ant.: {eur(dif_exp, self.app.theme.privacy)}",
                RED,
                "↑",
            ),
            (
                "Poupança",
                eur(savings, self.app.theme.privacy),
                f"vs mês ant.: {eur(dif_sav, self.app.theme.privacy)}",
                BLUE,
                "=",
            ),
            ("Taxa de poupança", percent(rate, self.app.theme.privacy), "meta saudável: 20%+", PURPLE, "%"),
        ]
        for i, m in enumerate(metrics):
            self.cards.addWidget(MetricCard(*m), 0, i)
        self.reflow_responsive_grid(self.cards)
        lines = []
        lines.append(f"REVISÃO DE {ym}\n")
        lines.append(ProductReviewService(self.repo).period_summary_text(
            data["start"], data["end"], privacy=self.app.theme.privacy
        ) + "\n")
        lines.append(
            f"Recebeste {eur(income, self.app.theme.privacy)}, gastaste {eur(expenses, self.app.theme.privacy)} e poupaste {eur(savings, self.app.theme.privacy)} ({percent(rate, self.app.theme.privacy)})."
        )
        if savings > prev.get("savings", 0):
            lines.append(
                f"A tua poupança subiu {eur(dif_sav, self.app.theme.privacy)} face ao mês anterior."
            )
        elif prev.get("savings", 0):
            lines.append(
                f"A tua poupança desceu {eur(abs(dif_sav), self.app.theme.privacy)} face ao mês anterior."
            )
        lines.append("\nTOP CATEGORIAS")
        for name, total, _ in data["categories"][:6]:
            lines.append(f"- {_private_text(name, self.app.theme.privacy)}: {eur(total, self.app.theme.privacy)}")
        lines.append("\nPONTOS A REVER")
        if data["uncategorized"]:
            lines.append(
                f"- {_private_count(data['uncategorized'], self.app.theme.privacy)} movimentos em categorias genéricas ou sem categoria."
            )
        if data["no_attachment"]:
            lines.append(
                f"- {_private_count(data['no_attachment'], self.app.theme.privacy)} despesas relevantes sem fatura/anexo."
            )
        if data["duplicates"]:
            lines.append(f"- {_private_count(len(data['duplicates']), self.app.theme.privacy)} possíveis duplicados detetados.")
        if data.get("invalid_dates"):
            lines.append(
                f"- {_private_count(data['invalid_dates'], self.app.theme.privacy)} movimento(s) com datas inválidas na base de dados."
            )
        if data.get("invalid_amounts"):
            lines.append(
                f"- {_private_count(data['invalid_amounts'], self.app.theme.privacy)} movimento(s) com valores inválidos na base de dados."
            )
        if not any(
            [
                data["uncategorized"],
                data["no_attachment"],
                data["duplicates"],
                data.get("invalid_dates"),
                data.get("invalid_amounts"),
            ]
        ):
            lines.append("- Sem alertas críticos encontrados neste mês.")
        lines.append("\nRECOMENDAÇÕES")
        if rate < 10:
            lines.append("- Prioridade: reduzir gastos variáveis ou rever subscrições.")
        elif rate < 20:
            lines.append(
                "- Boa base, mas ainda abaixo da meta clássica de 20% de poupança."
            )
        else:
            lines.append(
                "- Mês positivo; mantém o ritmo e canaliza excedente para objetivos/cofres/investimentos manuais."
            )
        self.text.setPlainText("\n".join(lines))

    def export_month_pdf(self):
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Exportar revisão mensal",
            str(Path.home() / f"revisao_{self.ym()}.pdf"),
            "PDF (*.pdf);;HTML (*.html)",
        )
        if not path:
            return
        escaped = html_lib.escape(self.text.toPlainText(), quote=True)
        html_payload = (
            "<h1>Revisão mensal</h1><pre style='font-family:Segoe UI,Arial;font-size:12pt;white-space:pre-wrap;word-wrap:break-word'>"
            + escaped
            + "</pre>"
        )

        try:
            if path.lower().endswith(".html"):
                Path(path).parent.mkdir(parents=True, exist_ok=True)
                Path(path).write_text(html_payload, encoding="utf-8")
            else:
                from services.export_documents import render_html_to_pdf

                render_html_to_pdf(html_payload, path)

            QMessageBox.information(
                self, APP_TITLE, f"Relatório exportado para:\n{path}"
            )
        except PermissionError as exc:
            log_exception(f"Ficheiro de relatório bloqueado em {path}", exc)
            QMessageBox.critical(
                self,
                "Ficheiro em Uso",
                f"Não foi possível gravar '{Path(path).name}'. O ficheiro está aberto noutro programa. Feche o ficheiro e tente novamente.",
            )
        except Exception as exc:
            QMessageBox.warning(self, APP_TITLE, f"Não foi possível exportar:\n{exc}")


class ScoreDetailsPage(BasePage):
    def __init__(self, app: "LiquidFinanceApp"):
        super().__init__(
            app,
            "Score Financeiro 2.0",
            "Nota explicável, confiança dos dados, evolução e ações prioritárias",
        )
        self._data: dict = {}
        self._selected_dimension_key = ""
        self._selected_action_page = "dashboard"
        top = QHBoxLayout()
        self.year = QComboBox()
        self.year.addItems([str(y) for y in self.repo.available_years()])
        self.month = QComboBox()
        self.month.addItems([f"{i:02d} — {PT_MONTHS[i - 1]}" for i in range(1, 13)])
        self.month.setCurrentIndex(date.today().month - 1)
        top.addWidget(QLabel("Ano"))
        top.addWidget(self.year)
        top.addWidget(QLabel("Mês"))
        top.addWidget(self.month)
        top.addStretch()

        self.summary = QGridLayout()
        self.summary.setSpacing(10)
        self.register_responsive_grid(self.summary)

        self.visual_grid = QGridLayout()
        self.visual_grid.setSpacing(12)
        self.register_responsive_grid(
            self.visual_grid,
            max_columns=2,
            min_column_width=500,
        )
        self.breakdown_card = GlassCard("🧭 Anatomia do score", strong=True)
        self.gauge_container = QHBoxLayout()
        self.gauge_container.setAlignment(Qt.AlignCenter)
        self.score_status = QLabel()
        self.score_status.setWordWrap(True)
        self.score_status.setAlignment(Qt.AlignCenter)
        self.breakdown = ScoreBreakdownChart(lambda: self.app.theme)
        self.breakdown.configure_details(
            lambda _item: {
                "title": "Componente do Score Financeiro 2.0",
                "explanation": (
                    "Mostra a pontuação, o peso e o estado do componente "
                    "selecionado no mês em análise."
                ),
            },
            action_label="Analisar componente",
        )
        self.breakdown.dimension_clicked.connect(self._select_dimension)
        self.breakdown_card.v.addLayout(self.gauge_container)
        self.breakdown_card.v.addWidget(self.score_status)
        self.breakdown_card.v.addWidget(self.breakdown)

        self.trend_card = GlassCard("📈 Evolução nos últimos 12 meses", strong=True)
        self.trend_card.set_compact_header()
        self.trend = ScoreTrendChart(lambda: self.app.theme)
        self.trend.configure_details(
            lambda _item: {
                "title": "Evolução do Score Financeiro 2.0",
                "explanation": (
                    "Cada ponto é recalculado apenas com os dados disponíveis "
                    "até ao final do respetivo mês."
                ),
            },
            action_label="Abrir mês",
        )
        self.trend.month_clicked.connect(self._open_trend_month)
        self.causes_label = QLabel()
        self.causes_label.setWordWrap(True)
        self.causes_label.setObjectName("Subtle")
        self.trend_card.v.addWidget(self.trend)
        self.trend_card.v.addWidget(self.causes_label)
        self.visual_grid.addWidget(self.breakdown_card, 0, 0)
        self.visual_grid.addWidget(self.trend_card, 0, 1)

        self.dimension_card = GlassCard("🔎 Leitura selecionada", strong=True)
        dimension_row = QHBoxLayout()
        self.dimension_detail = QLabel()
        self.dimension_detail.setWordWrap(True)
        self.dimension_open_btn = QPushButton("Abrir área relacionada →")
        self.dimension_open_btn.setObjectName("Primary")
        self.dimension_open_btn.clicked.connect(self._open_dimension_action)
        dimension_row.addWidget(self.dimension_detail, 1)
        dimension_row.addWidget(self.dimension_open_btn)
        self.dimension_card.v.addLayout(dimension_row)

        self.table = QTableWidget(0, 6)
        self.table.setProperty("columnPersistenceId", "score.components")
        self.table.setHorizontalHeaderLabels(
            ["Dimensão", "Estado", "Nota", "Pontos", "Máximo", "Leitura"]
        )
        self.table.horizontalHeader().setSectionResizeMode(5, QHeaderView.Stretch)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setMinimumHeight(300)
        self.table.cellClicked.connect(self._select_table_dimension)
        self.exact_card = GlassCard("📋 Cálculo detalhado", strong=True)
        self.exact_card.v.addWidget(self.table)

        self.actions_card = GlassCard("🚀 Próximas melhores ações", strong=True)
        self.actions_content = QVBoxLayout()
        self.actions_content.setContentsMargins(0, 0, 0, 0)
        self.actions_content.setSpacing(9)
        self.method_hint = QLabel()
        self.method_hint.setObjectName("Subtle")
        self.method_hint.setWordWrap(True)
        self.actions_card.v.addLayout(self.actions_content)
        self.actions_card.v.addWidget(self.method_hint)

        self.body.addLayout(top)
        self.body.addLayout(self.summary)
        self.body.addLayout(self.visual_grid)
        self.body.addWidget(self.dimension_card)
        self.body.addWidget(self.actions_card)
        self.body.addWidget(self.exact_card)
        self.year.currentIndexChanged.connect(self.refresh)
        self.month.currentIndexChanged.connect(self.refresh)
        self.refresh()

    def ym(self):
        year = safe_year_from_combo(self.year)
        month = max(1, min(12, self.month.currentIndex() + 1))
        return f"{year:04d}-{month:02d}"

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
                ScoreDetailsPage._clear_layout(item.layout())

    @staticmethod
    def _tone_colour(tone: str) -> str:
        return {
            "positive": GREEN,
            "warning": ORANGE,
            "negative": RED,
            "neutral": BLUE,
        }.get(str(tone), BLUE)

    def _open_trend_month(self, month_key: str) -> None:
        try:
            year, month = int(month_key[:4]), int(month_key[5:7])
        except (TypeError, ValueError):
            return
        self.year.blockSignals(True)
        self.month.blockSignals(True)
        if self.year.findText(str(year)) < 0:
            self.year.addItem(str(year))
        self.year.setCurrentText(str(year))
        self.month.setCurrentIndex(month - 1)
        self.year.blockSignals(False)
        self.month.blockSignals(False)
        self.refresh()

    def _select_table_dimension(self, row: int, _column: int) -> None:
        dimensions = list(self._data.get("dimensions") or [])
        if 0 <= row < len(dimensions):
            self._select_dimension(str(dimensions[row].get("key") or ""))

    def _select_dimension(self, key: str) -> None:
        dimension = next(
            (
                row
                for row in self._data.get("dimensions", [])
                if str(row.get("key")) == str(key)
            ),
            None,
        )
        if not dimension:
            return
        self._selected_dimension_key = str(key)
        self._selected_action_page = str(dimension.get("action_page") or "dashboard")
        self.breakdown.set_data(
            list(self._data.get("dimensions") or []), self._selected_dimension_key
        )
        delta = safe_float(dimension.get("delta"))
        self.dimension_detail.setText(
            f"{dimension.get('marker', '')} <b>{dimension.get('icon', '')} "
            f"{dimension.get('name', '')}</b> · "
            f"<b>{safe_float(dimension.get('score')):.0f}/100</b> · "
            f"{dimension.get('status', '')} · "
            f"{'+' if delta > 0 else ''}{delta:.1f} face ao mês anterior.<br>"
            f"{dimension.get('reading', '')}<br>"
            f"<b>Ação:</b> {dimension.get('action', '')}"
        )
        self.dimension_detail.setTextFormat(Qt.RichText)

    def _open_dimension_action(self) -> None:
        self.app.goto(self._selected_action_page)

    def _fill_summary(self, data: dict) -> None:
        self._clear_layout(self.summary)
        delta = safe_float(data.get("delta_1m"))
        confidence = safe_float(data.get("confidence"))
        emergency = safe_float(data.get("emergency_months"))
        data_date = format_iso_date(data.get("data_as_of"), "sem movimentos")
        cards = [
            MetricCard(
                "Score financeiro",
                f"{data.get('score', 0)}/100 · {data.get('grade', '—')}",
                f"{data.get('marker', '')} {data.get('status', '')}",
                self._tone_colour(str(data.get("tone"))),
                "🩺",
                status="Score 2.0",
                tone=str(data.get("tone") or "neutral"),
                progress=safe_float(data.get("score")),
                trend=[safe_float(row.get("score")) for row in data.get("trend", [])],
            ),
            MetricCard(
                "Variação mensal",
                f"{'+' if delta > 0 else ''}{delta:.0f} pontos",
                f"anterior: {data.get('previous_score', 0)}/100",
                GREEN if delta > 0 else RED if delta < 0 else BLUE,
                "📈" if delta > 0 else "📉" if delta < 0 else "➡️",
                status="Evolução do score",
                tone="positive"
                if delta > 0
                else "negative"
                if delta < 0
                else "neutral",
            ),
            MetricCard(
                "Confiança dos dados",
                f"{confidence:.0f}% · {data.get('confidence_label', '')}",
                "histórico, volume e categorização",
                GREEN if confidence >= 80 else ORANGE if confidence >= 55 else RED,
                "🔬",
                status="Fiabilidade da leitura",
                tone="positive"
                if confidence >= 80
                else "warning"
                if confidence >= 55
                else "negative",
                progress=confidence,
            ),
            MetricCard(
                "Reserva financeira",
                f"{emergency:.1f} meses".replace(".", ","),
                "objetivo recomendado: 3–6",
                GREEN if emergency >= 6 else ORANGE if emergency >= 3 else RED,
                "🛟",
                status="Autonomia estimada",
                tone="positive"
                if emergency >= 6
                else "warning"
                if emergency >= 3
                else "negative",
                progress=min(100.0, emergency / 6.0 * 100.0),
            ),
        ]
        contexts = (
            (f"Mês analisado · {self.ym()}", "neutral"),
            ("Mês atual vs. anterior", "neutral"),
            (
                f"Dados até {data_date}",
                (
                    "positive"
                    if confidence >= 80
                    else "warning"
                    if confidence >= 55
                    else "negative"
                ),
            ),
            (f"Estado até {data_date}", "neutral"),
        )
        for index, card in enumerate(cards):
            card.set_context_badge(*contexts[index])
            self.summary.addWidget(card, 0, index)
            self.summary.setColumnStretch(index, 1)
        self.reflow_responsive_grid(self.summary)

    def _fill_actions(self, data: dict) -> None:
        self._clear_layout(self.actions_content)
        actions_layout = QGridLayout()
        actions_layout.setSpacing(9)
        for index, action in enumerate(data.get("actions", [])):
            impact = safe_float(action.get("impact_points"))
            button = QPushButton(
                f"{action.get('priority', index + 1)}. {action.get('icon', '')}  "
                f"{action.get('title', '')}\n"
                f"{action.get('message', '')}\n"
                f"Potencial: +{impact:.1f} pts · score ≈ {action.get('projected_score', 0)}  →"
            )
            button.setObjectName("Secondary")
            button.setMinimumHeight(88)
            button.setCursor(Qt.PointingHandCursor)
            button.setStyleSheet("text-align:left; padding:10px 12px;")
            target_page = str(action.get("page") or "dashboard")
            button.clicked.connect(
                lambda _checked=False, page=target_page: self.app.goto(page)
            )

            actions_layout.addWidget(button, 0, index)
            actions_layout.setColumnStretch(index, 1)
        self.actions_content.addLayout(actions_layout)
        self.method_hint.setText(str(data.get("method_label") or ""))

    def refresh(self):
        data = self.analytics_service.detailed_financial_score(self.ym())
        self._data = data
        self._fill_summary(data)

        self._clear_layout(self.gauge_container)

        gauge = ScoreGauge(lambda: self.app.theme, data["score"])
        self.gauge_container.addWidget(gauge, alignment=Qt.AlignCenter)
        self.score_status.setText(
            f"{data.get('marker', '')} <b>{data.get('status', '')}</b> · "
            f"nota <b>{data.get('grade', '—')}</b> · confiança "
            f"<b>{data.get('confidence', 0)}% ({data.get('confidence_label', '')})</b>"
            f" · dados até <b>{format_iso_date(data.get('data_as_of'), 'sem movimentos')}</b>"
        )
        self.score_status.setTextFormat(Qt.RichText)
        data_date = format_iso_date(data.get("data_as_of"), "sem movimentos")
        confidence = safe_float(data.get("confidence"))
        data_tone = (
            "positive"
            if confidence >= 80
            else "warning"
            if confidence >= 55
            else "negative"
        )
        self.breakdown_card.set_context_badge(f"Dados até {data_date}", data_tone)
        self.trend_card.set_context_badge("Últimos 12 meses")
        self.dimension_card.set_context_badge("Dimensão selecionada")
        self.actions_card.set_context_badge(f"Mês analisado · {self.ym()}")
        self.exact_card.set_context_badge(f"Mês analisado · {self.ym()}")
        dimensions = list(data.get("dimensions") or [])
        if not self._selected_dimension_key or not any(
            row.get("key") == self._selected_dimension_key for row in dimensions
        ):
            weakest = min(
                dimensions, key=lambda row: safe_float(row.get("score")), default={}
            )
            self._selected_dimension_key = str(weakest.get("key") or "")
        self.breakdown.set_data(dimensions, self._selected_dimension_key)
        self.trend.set_data(list(data.get("trend") or []), self.ym())
        causes = data.get("causes", [])
        self.causes_label.setText(
            "<b>Principais causas face ao mês anterior</b><br>"
            + "<br>".join(str(row.get("message") or "") for row in causes)
        )
        self.causes_label.setTextFormat(Qt.RichText)

        rows = dimensions
        self.table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            score = safe_float(row.get("points"))
            maxv = safe_float(row.get("max_points"))
            vals = [
                f"{row.get('icon', '')} {row.get('name', '')}",
                f"{row.get('marker', '')} {row.get('status', '')}",
                f"{safe_float(row.get('score')):.0f}/100",
                f"{score:.1f}".replace(".", ","),
                f"{maxv:.0f}",
                str(row.get("reading") or ""),
            ]
            for c, v in enumerate(vals):
                item = QTableWidgetItem(str(v))
                if c in (2, 3, 4):
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                if c in (1, 2, 5):
                    item.setForeground(
                        QColor(self._tone_colour(str(row.get("tone") or "neutral")))
                    )
                self.table.setItem(r, c, item)
        self._fill_actions(data)
        self._select_dimension(self._selected_dimension_key)


class PdfReportsPage(BasePage):
    def __init__(self, app: "LiquidFinanceApp"):
        super().__init__(app, "Relatórios PDF", "Relatório mensal/anual exportável")
        top = QHBoxLayout()
        self.year = QComboBox()
        self.year.addItems([str(y) for y in self.repo.available_years()])
        self.month = QComboBox()
        self.month.addItem("Ano completo")
        self.month.addItems([f"{i:02d} — {PT_MONTHS[i - 1]}" for i in range(1, 13)])
        self.export = QPushButton("Exportar PDF")
        self.export.setObjectName("CardAction")
        top.addWidget(QLabel("Ano"))
        top.addWidget(self.year)
        top.addWidget(QLabel("Período"))
        top.addWidget(self.month)
        top.addStretch()
        top.addWidget(self.export)
        self.preview = QTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setMinimumHeight(620)
        card = GlassCard("Pré-visualização", strong=True)
        card.v.addWidget(self.preview)
        self.body.addLayout(top)
        self.body.addWidget(card)
        self.year.currentIndexChanged.connect(self.refresh)
        self.month.currentIndexChanged.connect(self.refresh)
        self.export.clicked.connect(self.export_pdf)
        self.refresh()

    def html(self):
        y = safe_year_from_combo(self.year)
        privacy = self.app.theme.privacy

        def money(value: Any) -> str:
            return eur(value, privacy)

        is_year = self.month.currentIndex() == 0
        if is_year:
            start, end = f"{y}-01-01", f"{y}-12-31"
            title = f"Relatório anual {y}"
            period_key = f"{y}"
        else:
            m = self.month.currentIndex()
            start, end, ym = self.repo.month_period(y, m)
            title = f"Relatório mensal {ym}"
            period_key = ym
        totals = self.repo.totals_for_payment_period(start, end)
        cats = self.repo.top_categories_payment_period(start, end)
        income = safe_float(totals["income"])
        expenses = safe_float(totals["expenses"])
        savings = safe_float(totals["savings"])
        rate = (savings / income * 100) if income else 0
        score = (
            self.analytics_service.detailed_financial_score(start[:7])["score"]
            if not is_year
            else "—"
        )
        review = (
            self.analytics_service.monthly_review_data(period_key)
            if not is_year
            else {"uncategorized": 0, "no_attachment": 0, "duplicates": []}
        )
        alerts = []
        if review.get("uncategorized"):
            alerts.append(f"{_private_count(review['uncategorized'], privacy)} movimentos sem categoria clara")
        if review.get("no_attachment"):
            alerts.append(f"{_private_count(review['no_attachment'], privacy)} despesas relevantes sem anexo")
        if review.get("duplicates"):
            alerts.append(f"{_private_count(len(review['duplicates']), privacy)} possíveis duplicados")
        if review.get("invalid_dates"):
            alerts.append(f"{_private_count(review['invalid_dates'], privacy)} datas inválidas na base")
        if review.get("invalid_amounts"):
            alerts.append(f"{_private_count(review['invalid_amounts'], privacy)} valores inválidos na base")
        if not alerts:
            alerts.append("Sem alertas críticos no período")
        recommendation = (
            "Mantém o ritmo e canaliza excedente para objetivos/investimentos."
            if rate >= 20
            else (
                "Revê categorias variáveis e subscrições para aproximar a poupança de 20%."
                if rate >= 10
                else "Prioridade: reduzir despesas variáveis, rever pagamentos recorrentes e criar margem mensal."
            )
        )
        cat_rows = "".join(
            f"<tr><td><span style='display:inline-block;width:10px;height:10px;background:{safe_hex_colour(colour)};border-radius:5px;margin-right:7px'></span>{html_text(_private_text(c, privacy))}</td><td style='text-align:right'>{money(v)}</td><td style='text-align:right'>{percent((safe_float(v) / expenses * 100) if expenses else 0, self.app.theme.privacy)}</td></tr>"
            for c, v, colour in cats
        )
        alert_rows = "".join(f"<li>{html_text(a)}</li>" for a in alerts)
        annual_sections = ""
        if is_year:
            annual = self.analytics_service.annual_report_data(y)
            month_rows = "".join(
                "<tr>"
                f"<td>{PT_MONTHS[int(row['month']) - 1].capitalize()}</td>"
                f"<td style='text-align:right'>{money(row['income'])}</td>"
                f"<td style='text-align:right'>{money(row['expenses'])}</td>"
                f"<td style='text-align:right'>{money(row['savings'])}</td>"
                "</tr>"
                for row in annual.get("monthly", [])
            )
            investment_available = bool(annual.get("investment_available"))
            investment_performance_available = (
                annual.get("investment_net_return_pct") is not None
            )
            holding_rows = "".join(
                "<tr>"
                f"<td>{html_lib.escape(_private_text(row.get('name') or 'Ativo', privacy))}</td>"
                f"<td>{html_lib.escape(str(row.get('asset_class') or 'Outro'))}</td>"
                f"<td style='text-align:right'>{money(row.get('cost_basis', 0)) if row.get('performance_available') else 'N/D'}</td>"
                f"<td style='text-align:right'>{money(row.get('fees', 0))}</td>"
                f"<td style='text-align:right'>{money(row.get('current_value', 0))}</td>"
                f"<td style='text-align:right'>{money(row.get('net_gain', 0)) if row.get('performance_available') else 'N/D'}</td>"
                f"<td style='text-align:right'>{('••• p.p.' if privacy else format(safe_float(row.get('contribution_pp')), '+.1f') + ' p.p.') if row.get('performance_available') else 'N/D'}</td>"
                "</tr>"
                for row in annual.get("investment_attribution", [])
            ) or (
                "<tr><td colspan='7'>Sem detalhe histórico da carteira.</td></tr>"
                if investment_available
                else "<tr><td colspan='7'>Sem snapshot histórico para este ano.</td></tr>"
            )
            supplier_rows = (
                "".join(
                    "<tr>"
                    f"<td>{html_lib.escape(_private_text(row.get('supplier') or 'Sem fornecedor', privacy))}</td>"
                    f"<td style='text-align:right'>{_private_count(int(row.get('documents', 0) or 0), privacy)}</td>"
                    f"<td style='text-align:right'>{money(row.get('total', 0))}</td>"
                    "</tr>"
                    for row in annual.get("document_suppliers", [])
                )
                or "<tr><td colspan='3'>Sem documentos associados neste ano.</td></tr>"
            )
            explanation_rows = "".join(
                "<li>"
                f"<b>{html_lib.escape(str(row.get('title') or 'Variação'))}:</b> "
                f"{money(row.get('delta', 0))} ({percent(row.get('change_pct', 0), self.app.theme.privacy)}). "
                f"{html_lib.escape(_private_text(row.get('message') or '', privacy))}</li>"
                for row in annual.get("explanations", [])
            )
            documents = annual.get("documents", {}) or {}
            annual_sections = f"""
            <h2>Evolução mensal</h2>
            <table width='100%' cellspacing='0' cellpadding='7' style='border-collapse:collapse'>
            <tr style='background:#F8FAFC'><th align='left'>Mês</th><th align='right'>Receitas</th><th align='right'>Despesas</th><th align='right'>Poupança</th></tr>
            {month_rows}</table>
            <h2>Investimentos</h2>
            <p><b>Valor no período:</b> {money(annual.get("investment_total", 0)) if investment_available else "Sem snapshot"} ·
            <b>Capital aplicado:</b> {money(annual.get("investment_cost", 0)) if investment_available else "—"} ·
            <b>Comissões:</b> {money(annual.get("investment_fees", 0)) if investment_available else "—"} ·
            <b>Ganho líquido:</b> {money(annual.get("investment_net_gain", 0)) if investment_performance_available else "N/D"} ·
            <b>Rentabilidade líquida ponderada:</b> {percent(annual.get("investment_net_return_pct", 0), self.app.theme.privacy) if investment_performance_available else "N/D"} ·
            <b>Cobertura do custo:</b> {percent(annual.get("investment_data_coverage_pct", 0), self.app.theme.privacy) if investment_available else "—"} ·
            <b>Diferença bruto/líquido (bases distintas):</b> {("•••" if privacy else format(safe_float(annual.get("investment_fee_impact_pp")), ".1f")) + " p.p." if investment_performance_available else "N/D"} ·
            <b>Data do estado:</b> {annual.get("investment_as_of") or "—"}</p>
            <table width='100%' cellspacing='0' cellpadding='7' style='border-collapse:collapse'>
            <tr style='background:#F8FAFC'><th align='left'>Ativo</th><th align='left'>Tipo</th><th align='right'>Investido</th><th align='right'>Comissões</th><th align='right'>Valor atual</th><th align='right'>Ganho líquido</th><th align='right'>Contribuição</th></tr>
            {holding_rows}</table>
            <h2>Documentos e faturas</h2>
            <p><b>Documentos:</b> {_private_count(int(documents.get('document_count', 0) or 0), privacy)} ·
            <b>Fornecedores:</b> {_private_count(int(documents.get('supplier_count', 0) or 0), privacy)} ·
            <b>Total documentado:</b> {money(documents.get("documented_total", 0))}</p>
            <table width='100%' cellspacing='0' cellpadding='7' style='border-collapse:collapse'>
            <tr style='background:#F8FAFC'><th align='left'>Fornecedor</th><th align='right'>Documentos</th><th align='right'>Total</th></tr>
            {supplier_rows}</table>
            <h2>Explicação face a {y - 1}</h2><ul>{explanation_rows}</ul>
            """
        scope_summary = html_lib.escape(ProductReviewService(self.repo).period_summary_text(
            start, end, privacy=privacy
        ))
        return f"""<html><body style='font-family:Segoe UI,Arial;color:#172033;margin:28px'>
        <h1 style='margin-bottom:4px'>{title}</h1>
        <p style='color:#64748B'>Gerado em {datetime.now().strftime("%d/%m/%Y %H:%M")}</p>
        <p style='white-space:pre-wrap'>{scope_summary}</p>
        <table width='100%' cellspacing='0' cellpadding='10' style='border-collapse:collapse;margin:18px 0'>
        <tr style='background:#F1F5F9'><th align='left'>Receitas</th><th align='left'>Despesas</th><th align='left'>Poupança</th><th align='left'>Taxa</th><th align='left'>Score</th></tr>
        <tr><td><b>{money(income)}</b></td><td><b>{money(expenses)}</b></td><td><b>{money(savings)}</b></td><td><b>{percent(rate, self.app.theme.privacy)}</b></td><td><b>{score}</b></td></tr>
        </table>
        <h2>Categorias</h2>
        <table width='100%' cellspacing='0' cellpadding='8' style='border-collapse:collapse'>
        <tr style='background:#F8FAFC'><th align='left'>Categoria</th><th align='right'>Total</th><th align='right'>Peso</th></tr>{cat_rows}</table>
        {annual_sections}
        <h2>Alertas</h2><ul>{alert_rows}</ul>
        <h2>Recomendação</h2><p>{recommendation}</p>
        </body></html>"""

    def refresh(self):
        self.preview.setHtml(self.html())
        self.export.setText(
            "Exportar relatório anual PDF"
            if self.month.currentIndex() == 0
            else "Exportar relatório mensal PDF"
        )

    def export_pdf(self):
        y = safe_year_from_combo(self.year)
        period = (
            f"{y:04d}"
            if self.month.currentIndex() == 0
            else f"{y:04d}-{self.month.currentIndex():02d}"
        )
        self._on_export_html_ready(self.html(), period)

    def _on_export_html_ready(self, html_content: str, ym: str):
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Exportar PDF",
            str(Path.home() / f"relatorio_{ym}.pdf"),
            "PDF (*.pdf)",
        )
        if not path:
            return
        try:
            from services.export_documents import render_html_to_pdf

            render_html_to_pdf(html_content, path)
            self.app.show_toast("Relatório PDF exportado com sucesso!", "✅")
        except PermissionError as exc:
            log_exception(f"Ficheiro PDF bloqueado em {path}", exc)
            self.app.show_toast(f"Ficheiro '{Path(path).name}' em uso. Feche-o e tente novamente.", "❌", tone="danger")
        except Exception as exc:
            log_exception(f"Falha ao exportar PDF para {path}", exc)
            self.app.show_toast(f"Erro ao exportar PDF: {exc}", "❌", tone="danger")


class HealthPage(BasePage):
    def __init__(self, app: "LiquidFinanceApp"):
        super().__init__(
            app, "Saúde financeira", "Score, autonomia, rácios e alertas inteligentes"
        )
        self.summary = QGridLayout()
        self.summary.setSpacing(14)
        self.register_responsive_grid(self.summary)
        self.data_health_service = DataHealthService(
            self.repo,
            backup_service=self.app.backup_service,
        )
        self.backup_health_task = AsyncTask(
            self,
            registry=app.background_task_registry,
            label="Validação real do backup na Saúde financeira",
        )
        self.backup_health_task.result.connect(self._on_backup_health_result)
        self.backup_health_task.error.connect(self._on_backup_health_error)
        self._backup_health_fingerprint = None
        self._backup_health_result: dict[str, Any] = {}
        self.data_health_grid = QGridLayout()
        self.data_health_grid.setContentsMargins(0, 0, 0, 0)
        self.data_health_grid.setSpacing(10)
        self.register_responsive_grid(
            self.data_health_grid,
            max_columns=2,
            min_column_width=430,
        )

        self.data_health_status = QLabel()
        self.data_health_status.setObjectName("MetricStatus")
        self.data_health_status.setWordWrap(True)
        self.data_health_details = QLabel()
        self.data_health_details.setObjectName("Subtle")
        self.data_health_details.setWordWrap(True)
        self.demo_review_btn = QPushButton("Rever dados de demonstração")
        self.demo_review_btn.setObjectName("Secondary")
        self.demo_review_btn.clicked.connect(self._show_demo_review)
        self.data_health_card = GlassCard("🧬 Saúde dos dados", strong=True)
        self.data_health_card.setProperty("pageCardKey", "data_health")
        self.data_health_card.v.addWidget(self.data_health_status)
        self.data_health_card.v.addWidget(self.data_health_details)
        self.data_health_card.v.addWidget(self.demo_review_btn, 0, Qt.AlignLeft)

        self.protection_status = QLabel()
        self.protection_status.setObjectName("MetricStatus")
        self.protection_status.setWordWrap(True)
        self.protection_details = QLabel()
        self.protection_details.setObjectName("Subtle")
        self.protection_details.setWordWrap(True)
        self.open_protection_btn = QPushButton("Abrir proteção e backups")
        self.open_protection_btn.setObjectName("Secondary")
        self.open_protection_btn.clicked.connect(
            lambda _checked=False: self.app.goto("definicoes")
        )
        self.protection_card = GlassCard("🛡️ Proteção e recuperação", strong=True)
        self.protection_card.setProperty("pageCardKey", "data_protection_health")
        self.protection_card.v.addWidget(self.protection_status)
        self.protection_card.v.addWidget(self.protection_details)
        self.protection_card.v.addWidget(self.open_protection_btn, 0, Qt.AlignLeft)
        self.data_health_grid.addWidget(self.data_health_card, 0, 0)
        self.data_health_grid.addWidget(self.protection_card, 0, 1)

        self.data_quality_chart = DataQualityTrendChart(lambda: self.app.theme)
        self.data_quality_chart.configure_details(
            self._data_quality_detail_payload,
            action_label="Abrir movimentos",
            action_handler=lambda _item: self.app.goto("movimentos"),
        )
        self.data_quality_hint = QLabel(
            "O score penaliza categorias genéricas, duplicados candidatos e "
            "descrições insuficientes; meses sem movimentos não são inventados."
        )
        self.data_quality_hint.setObjectName("Subtle")
        self.data_quality_hint.setWordWrap(True)
        self.data_quality_card = GlassCard(
            "📈 Tendência da qualidade dos dados",
            strong=True,
        )
        self.data_quality_card.setProperty("pageCardKey", "data_quality_trend")
        self.data_quality_card.v.addWidget(self.data_quality_hint)
        self.data_quality_card.v.addWidget(self.data_quality_chart)
        self.alerts = GlassCard("Alertas e recomendações", strong=True)
        self.table = QTableWidget(0, 3)
        self.table.setProperty("columnPersistenceId", "health.metrics")
        self.table.setHorizontalHeaderLabels(["Indicador", "Valor", "Leitura"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Interactive)
        self.table.setColumnWidth(0, 180)
        self.table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeToContents
        )
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setMinimumHeight(280)
        card = GlassCard("Indicadores", strong=True)
        card.v.addWidget(self.table)
        self.body.addLayout(self.summary)
        self.body.addLayout(self.data_health_grid)
        self.body.addWidget(self.data_quality_card)
        self.body.addWidget(self.alerts)
        self.body.addWidget(card)
        self.refresh()

    def clear(self, layout):
        while layout.count():
            item = layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    @staticmethod
    def _set_status_tone(label: QLabel, tone: str) -> None:
        label.setProperty("tone", tone)
        style = label.style()
        if style is not None:
            style.unpolish(label)
            style.polish(label)

    def _data_quality_detail_payload(self, item: dict[str, Any]) -> dict[str, Any]:
        score = safe_float(item.get("score"))
        return {
            "icon": "🧬",
            "title": "Qualidade dos movimentos",
            "subtitle": str(item.get("month") or ""),
            "explanation": (
                "O indicador usa apenas meses com movimentos válidos. Categorias "
                "genéricas, duplicados candidatos e descrições vazias reduzem a nota."
            ),
            "metrics": [
                {
                    "label": "Qualidade",
                    "value": f"{score:.1f}/100".replace(".", ","),
                    "tone": str(item.get("tone") or "neutral"),
                },
                {
                    "label": "Movimentos",
                    "value": str(int(item.get("total", 0) or 0)),
                    "tone": "neutral",
                    "sensitive": True,
                },
                {
                    "label": "Categorias genéricas",
                    "value": str(int(item.get("generic", 0) or 0)),
                    "tone": "warning",
                    "sensitive": True,
                },
                {
                    "label": "Duplicados candidatos",
                    "value": str(int(item.get("duplicates", 0) or 0)),
                    "tone": "negative",
                    "sensitive": True,
                },
            ],
        }

    def _show_demo_review(self) -> None:
        review = dict(getattr(self, "_demo_review", {}) or {})
        if not review:
            return
        candidate_total = sum(
            int(review.get(key, 0) or 0)
            for key in (
                "transaction_candidates",
                "subscription_candidates",
                "holding_candidates",
                "goal_candidates",
            )
        )
        confidence = (
            "Perfil completo identificado com confiança elevada."
            if bool(review.get("safe_to_offer_removal"))
            else "Perfil parcial ou misturado: revisão manual obrigatória."
        )
        QMessageBox.information(
            self,
            "Revisão segura dos dados de demonstração",
            f"{review.get('message', '')}\n\n"
            f"{confidence}\n"
            f"Registos candidatos: {candidate_total} · "
            f"adicionais: {int(review.get('extra_financial_records', 0) or 0)}.\n\n"
            "Esta janela nunca apaga dados. Uma eventual remoção exige primeiro "
            "um backup completo verificado, nova confirmação da assinatura e "
            "confirmação explícita do utilizador.",
        )

    def _refresh_data_health(self, today: date) -> None:
        if self.data_health_service.repo is not self.repo:
            self.data_health_service = DataHealthService(
                self.repo,
                backup_service=self.app.backup_service,
            )
        snapshot = self.data_health_service.snapshot(
            reference_date=today,
            months=12,
        )
        self._data_health_snapshot = snapshot
        self._demo_review = dict(snapshot.get("demo_review") or {})
        score = int(snapshot.get("score", 0) or 0)
        tone = str(snapshot.get("tone") or "neutral")
        self._set_status_tone(self.data_health_status, tone)
        self.data_health_status.setText(
            f"{snapshot.get('marker', '')} Qualidade e integridade: "
            f"{score}/100 · {snapshot.get('status', '')}"
        )
        database_label = (
            "base verificada no arranque"
            if bool(snapshot.get("database_verified"))
            else "verificação da base por confirmar"
        )
        self.data_health_details.setText(
            f"🗃️ {_private_count(int(snapshot.get('transactions', 0) or 0), self.app.theme.privacy)} movimentos · "
            f"{database_label} · estrutura "
            f"{int(snapshot.get('schema_version', 0) or 0)}/"
            f"{int(snapshot.get('supported_schema_version', 0) or 0)}.<br>"
            f"⚠️ {_private_count(int(snapshot.get('invalid', 0) or 0), self.app.theme.privacy)} inválidos · "
            f"{_private_count(int(snapshot.get('category_inconsistencies', 0) or 0), self.app.theme.privacy)} categoria(s) incompatível(eis) · "
            f"{_private_count(int(snapshot.get('generic', 0) or 0), self.app.theme.privacy)} genéricos · "
            f"{_private_count(int(snapshot.get('duplicate_candidates', 0) or 0), self.app.theme.privacy)} duplicados candidatos · "
            f"{_private_count(int(snapshot.get('attachments_unverified', 0) or 0), self.app.theme.privacy)} anexos sem assinatura completa."
        )
        candidate_total = sum(
            int(self._demo_review.get(key, 0) or 0)
            for key in (
                "transaction_candidates",
                "subscription_candidates",
                "holding_candidates",
                "goal_candidates",
            )
        )
        can_review = (
            bool(self._demo_review.get("marker_present")) or candidate_total >= 20
        )
        self.demo_review_btn.setEnabled(can_review)
        self.demo_review_btn.setText(
            "Rever perfil de demonstração"
            if can_review
            else "Sem perfil de demonstração detetado"
        )
        self.data_quality_chart.set_data(list(snapshot.get("trend") or []))

        protection = dict(snapshot.get("protection") or {})
        fingerprint = self.app.backup_service.latest_backup_fingerprint()
        if (
            self._backup_health_result
            and fingerprint == self._backup_health_fingerprint
        ):
            self._render_protection(self._backup_health_result, current=True)
        else:
            self._render_protection(protection, current=False)
            if (
                fingerprint is not None
                and fingerprint != self._backup_health_fingerprint
                and not self.backup_health_task.is_running()
            ):
                self.protection_details.setText(
                    self.protection_details.text()
                    + "<br>🔄 Validação integral em curso, sem bloquear a aplicação."
                )
                db_path = self.app.repo.db.path
                self.backup_health_task.start(
                    lambda: BackupService.run_verify_latest_backup_for_database(db_path)
                )

    def _render_protection(
        self,
        protection: dict[str, Any],
        *,
        current: bool,
    ) -> None:
        protection_score = int(protection.get("score", 0) or 0)
        protection_tone = (
            "positive"
            if protection_score >= 80
            else "warning"
            if protection_score >= 40
            else "negative"
        )
        self._set_status_tone(self.protection_status, protection_tone)
        self.protection_status.setText(
            f"🛡️ {'Proteção confirmada agora' if current else 'Proteção registada'}: "
            f"{protection_score}/100 · "
            f"{protection.get('message', 'Sem informação')}"
        )
        verified_at = str(
            (
                protection.get("verified_at")
                if current
                else protection.get("last_verified_at")
            )
            or ""
        )
        try:
            verified_at = datetime.fromisoformat(verified_at).strftime(
                "%d/%m/%Y às %H:%M:%S"
            )
        except ValueError:
            pass
        backup_flags = [
            (
                f"verificado realmente em {verified_at}"
                if current and protection.get("verified") and verified_at
                else f"verificação registada em {verified_at}"
                if verified_at
                else "por verificar"
            ),
            "recente" if protection.get("recent") else "antigo ou inexistente",
            "cifrado" if protection.get("encrypted") else "sem cifragem",
            "segunda cópia"
            if protection.get("mirrored")
            else "sem segunda cópia confirmada",
            "recuperação testada"
            if protection.get("drill_recent")
            else "teste de recuperação em falta",
        ]
        self.protection_details.setText(
            " · ".join(backup_flags)
            + (
                ".<br>Assinatura e conteúdo do ficheiro foram recalculados nesta sessão."
                if current
                else ".<br>Resumo rápido baseado no último registo; ainda não afirma que o ficheiro atual está verificado."
            )
        )

    def _on_backup_health_result(self, result: Any) -> None:
        self._backup_health_fingerprint = (
            self.app.backup_service.latest_backup_fingerprint()
        )
        self._backup_health_result = dict(result or {})
        self._render_protection(self._backup_health_result, current=True)

    def _on_backup_health_error(self, message: str) -> None:
        self._set_status_tone(self.protection_status, "negative")
        self.protection_status.setText(
            "🛡️ Não foi possível confirmar agora o backup mais recente."
        )
        self.protection_details.setText(str(message))

    def refresh(self):
        today = date.today()
        self._refresh_data_health(today)
        start = today.replace(day=1).isoformat()
        end = today.isoformat()
        totals = self.analytics_service.totals_for_period(start, end)
        income = totals["income"]
        expenses = totals["expenses"]
        savings = totals["savings"]
        fixed = self.payments_service.active_subscription_monthly_total()
        score_data = self.analytics_service.detailed_financial_score(
            today.strftime("%Y-%m")
        )
        score = safe_float(score_data.get("score"))
        savings_rate = safe_float(score_data.get("savings_rate"))
        autonomy = safe_float(score_data.get("emergency_months"))
        subs_ratio = safe_float(score_data.get("subscription_ratio"))
        average_income = safe_float(score_data.get("average_income"))
        average_expenses = safe_float(score_data.get("average_expenses"))
        score_color = {
            "positive": GREEN,
            "warning": ORANGE,
            "negative": RED,
            "neutral": BLUE,
        }.get(str(score_data.get("tone") or "neutral"), BLUE)
        self.clear(self.summary)
        for i, m in enumerate(
            [
                (
                    "Score Financeiro 2.0",
                    f"{score:.0f}/100 · {score_data.get('grade', '—')}",
                    f"{score_data.get('marker', '')} {score_data.get('status', '')}",
                    score_color,
                    "🩺",
                ),
                (
                    "Taxa de poupança",
                    percent(savings_rate, self.app.theme.privacy),
                    "média histórica do Score 2.0",
                    GREEN if savings_rate >= 15 else ORANGE,
                    "%",
                ),
                (
                    "Autonomia",
                    "••• meses" if self.app.theme.privacy else f"{autonomy:.1f} meses".replace(".", ","),
                    "liquidez/despesas médias",
                    BLUE,
                    "🛟",
                ),
                (
                    "Subscrições",
                    percent(subs_ratio, self.app.theme.privacy),
                    "do rendimento médio",
                    PURPLE,
                    "🔁",
                ),
            ]
        ):
            self.summary.addWidget(MetricCard(*m), 0, i)
        self.reflow_responsive_grid(self.summary)
        while self.alerts.v.count() > 1:
            item = self.alerts.v.takeAt(1)
            if item.widget():
                item.widget().deleteLater()
        msgs = []
        if savings_rate < 10:
            msgs.append(
                (
                    "Aumentar poupança",
                    "A taxa média de poupança usada pelo Score 2.0 está baixa.",
                    ORANGE,
                )
            )
        else:
            msgs.append(
                (
                    "Bom controlo",
                    "A tua poupança média está num nível saudável.",
                    GREEN,
                )
            )
        if autonomy < 3:
            msgs.append(
                (
                    "Fundo de emergência",
                    "Tenta chegar pelo menos a 3 meses de autonomia.",
                    RED,
                )
            )
        if subs_ratio > 8:
            msgs.append(
                (
                    "Subscrições",
                    "O peso das subscrições está elevado face ao rendimento.",
                    PURPLE,
                )
            )
        uncategorized = self.repo.generic_category_count()
        if uncategorized:
            msgs.append(
                (
                    "Categorias por rever",
                    f"Tens {_private_count(int(uncategorized), self.app.theme.privacy)} movimentos genéricos.",
                    BLUE,
                )
            )
        for title, msg, color in msgs:
            lbl = QLabel(
                f"<b>{title}</b><br><span style='color:{self.app.theme.muted}'>{msg}</span>"
            )
            lbl.setWordWrap(True)
            lbl.setStyleSheet(f"background:{color}18;border-radius:16px;padding:12px;")
            self.alerts.v.addWidget(lbl)
        indicators = [
            (
                "Receitas do mês",
                eur(income, self.app.theme.privacy),
                "mês atual até hoje",
            ),
            (
                "Despesas do mês",
                eur(expenses, self.app.theme.privacy),
                "saídas registadas",
            ),
            (
                "Poupança líquida",
                eur(savings, self.app.theme.privacy),
                "mês atual até hoje",
            ),
            (
                "Rendimento médio",
                eur(average_income, self.app.theme.privacy),
                "base do Score 2.0",
            ),
            (
                "Despesa média",
                eur(average_expenses, self.app.theme.privacy),
                "base da autonomia",
            ),
            (
                "Gastos fixos/subscrições",
                eur(fixed, self.app.theme.privacy),
                "mensal estimado",
            ),
            (
                "Património líquido",
                eur(self.repo.net_worth(), self.app.theme.privacy),
                "contas + carteira − passivos",
            ),
        ]
        self.table.setRowCount(len(indicators))
        for row, (a, b, c) in enumerate(indicators):
            for col, val in enumerate([a, b, c]):
                item = QTableWidgetItem(str(val))
                if col == 1:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self.table.setItem(row, col, item)


class ReportsPage(BasePage):
    def __init__(self, app: "LiquidFinanceApp"):
        super().__init__(
            app, "Análises", "Relatórios, evolução mensal e distribuição por categoria"
        )
        top = QHBoxLayout()
        self.range = QComboBox()
        self.range.addItems(["3 meses", "6 meses", "12 meses"])
        self.export_btn = QPushButton("Exportar resumo TXT")
        top.addWidget(QLabel("Período"))
        top.addWidget(self.range)
        top.addStretch()
        top.addWidget(self.export_btn)
        self.summary = QGridLayout()
        self.summary.setSpacing(14)
        self.register_responsive_grid(self.summary)

        comparison_controls = QGridLayout()
        comparison_controls.setHorizontalSpacing(10)
        comparison_controls.setVerticalSpacing(8)
        today = date.today()
        current_start, current_end, _days = rolling_month_window(
            today,
            self.months(),
        )
        previous_end = current_start - timedelta(days=1)
        previous_start, _previous_end, _previous_days = rolling_month_window(
            previous_end,
            self.months(),
        )
        self.compare_current_start = QDateEdit()
        self.compare_current_end = QDateEdit()
        self.compare_reference_start = QDateEdit()
        self.compare_reference_end = QDateEdit()
        for editor in (
            self.compare_current_start,
            self.compare_current_end,
            self.compare_reference_start,
            self.compare_reference_end,
        ):
            prepare_date_edit(editor, self.app.theme)
        self.compare_current_start.setDate(
            QDate(current_start.year, current_start.month, current_start.day)
        )
        self.compare_current_end.setDate(
            QDate(current_end.year, current_end.month, current_end.day)
        )
        self.compare_reference_start.setDate(
            QDate(previous_start.year, previous_start.month, previous_start.day)
        )
        self.compare_reference_end.setDate(
            QDate(previous_end.year, previous_end.month, previous_end.day)
        )
        self.compare_btn = QPushButton("Comparar períodos")
        self.compare_btn.setObjectName("Primary")
        self.compare_sync_btn = QPushButton(f"↻ Usar {self.months()} meses")
        self.compare_sync_btn.setObjectName("Secondary")
        self.compare_sync_btn.setToolTip(
            "Usar o período selecionado no topo e comparar com os meses imediatamente anteriores"
        )
        comparison_controls.addWidget(QLabel("Período atual"), 0, 0)
        comparison_controls.addWidget(self.compare_current_start, 0, 1)
        comparison_controls.addWidget(QLabel("até"), 0, 2)
        comparison_controls.addWidget(self.compare_current_end, 0, 3)
        comparison_controls.addWidget(QLabel("Referência"), 1, 0)
        comparison_controls.addWidget(self.compare_reference_start, 1, 1)
        comparison_controls.addWidget(QLabel("até"), 1, 2)
        comparison_controls.addWidget(self.compare_reference_end, 1, 3)
        comparison_controls.addWidget(self.compare_sync_btn, 0, 4)
        comparison_controls.addWidget(self.compare_btn, 1, 4)
        self.comparison_summary = QGridLayout()
        self.comparison_summary.setSpacing(12)
        self.register_responsive_grid(self.comparison_summary)
        self.period_comparison_chart = PeriodComparisonChart(lambda: self.app.theme)
        self.period_comparison_chart.configure_details(
            self._comparison_detail_payload,
            action_label="Abrir período atual",
            action_handler=self._open_comparison_current,
        )
        self.comparison_scope_hint = QLabel()
        self.comparison_scope_hint.setObjectName("Subtle")
        self.comparison_scope_hint.setWordWrap(True)
        self.period_comparison_scroll = EdgePropagatingScrollArea()
        self.period_comparison_scroll.setObjectName("ComparisonCategoryScroll")
        self.period_comparison_scroll.setWidgetResizable(True)
        self.period_comparison_scroll.setFrameShape(QFrame.NoFrame)
        self.period_comparison_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarAlwaysOff
        )
        self.period_comparison_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.period_comparison_scroll.setMinimumHeight(430)
        self.period_comparison_scroll.setMaximumHeight(510)
        self.period_comparison_scroll.setSizePolicy(
            QSizePolicy.Expanding,
            QSizePolicy.Fixed,
        )
        self.period_comparison_scroll.setWidget(self.period_comparison_chart)
        self.comparison_card = GlassCard("↔ Comparador de períodos", strong=True)
        self.comparison_card.v.addLayout(comparison_controls)
        self.comparison_card.v.addLayout(self.comparison_summary)
        self.comparison_card.v.addWidget(self.comparison_scope_hint)
        self.comparison_card.v.addWidget(self.period_comparison_scroll)
        self.comparison_explanations_card = GlassCard(
            "💡 Explicação das variações", strong=True
        )

        self.anomaly_table = QTableWidget(0, 5)
        self.anomaly_table.setProperty("columnPersistenceId", "reports.anomalies")
        self.anomaly_table.setHorizontalHeaderLabels(
            ["Prioridade", "Data", "Anomalia", "Categoria", "Valor"]
        )
        self.anomaly_table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.Stretch
        )
        for column in (0, 1, 3, 4):
            self.anomaly_table.horizontalHeader().setSectionResizeMode(
                column, QHeaderView.ResizeToContents
            )
        self.anomaly_table.setAlternatingRowColors(True)
        self.anomaly_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.anomaly_table.verticalHeader().setVisible(False)
        self.anomaly_table.setMinimumHeight(260)
        anomaly_hint = QLabel(
            "Deteta valores fora do padrão da categoria, possíveis duplicados e aumentos recorrentes. "
            "Faz duplo clique para abrir os movimentos relacionados."
        )
        anomaly_hint.setObjectName("Subtle")
        anomaly_hint.setWordWrap(True)
        self.anomaly_card = GlassCard("🔎 Deteção de anomalias", strong=True)
        self.anomaly_card.setProperty("pageCardKey", "anomalies_period")
        self.anomaly_card.v.addWidget(anomaly_hint)
        self.anomaly_card.v.addWidget(self.anomaly_table)

        self.forecast_accuracy_chart = ForecastAccuracyChart(lambda: self.app.theme)
        self.forecast_accuracy_chart.configure_details(
            self._forecast_accuracy_detail_payload,
            action_label="Abrir despesas do mês",
            action_handler=self._open_forecast_month,
        )
        forecast_hint = QLabel(
            "Teste histórico honesto: compara previsão e despesa real sem usar "
            "dados futuros. A precisão é a percentagem de resultados que ficou "
            "dentro da faixa prevista; o erro pontual aparece como diagnóstico."
        )
        forecast_hint.setObjectName("Subtle")
        forecast_hint.setWordWrap(True)
        self.forecast_accuracy_card = GlassCard(
            "🎯 Previsão vs. realidade",
            strong=True,
        )
        self.forecast_accuracy_card.setProperty(
            "pageCardKey",
            "forecast_accuracy",
        )
        self.forecast_accuracy_card.v.addWidget(forecast_hint)
        self.forecast_accuracy_card.v.addWidget(self.forecast_accuracy_chart)

        self.forecast_causes_month = QComboBox()
        self.forecast_causes_month.setMinimumWidth(180)
        causes_controls = FlowLayout(hspacing=8, vspacing=8)
        causes_controls.addWidget(QLabel("Mês explicado"))
        causes_controls.addWidget(self.forecast_causes_month)
        self.forecast_causes_summary = QLabel()
        self.forecast_causes_summary.setObjectName("Subtle")
        self.forecast_causes_summary.setWordWrap(True)
        self.forecast_causes_chart = ForecastErrorCausesChart(lambda: self.app.theme)
        self.forecast_causes_chart.configure_details(
            self._forecast_cause_detail_payload,
            action_label="Abrir categoria no mês",
            action_handler=self._open_forecast_cause,
        )
        self.forecast_causes_card = GlassCard(
            "🧭 Porque falhou a previsão?",
            strong=True,
        )
        self.forecast_causes_card.setProperty(
            "pageCardKey",
            "forecast_error_causes",
        )
        self.forecast_causes_card.v.addLayout(causes_controls)
        self.forecast_causes_card.v.addWidget(self.forecast_causes_summary)
        self.forecast_causes_card.v.addWidget(self.forecast_causes_chart)

        self.category_accuracy_summary = QLabel()
        self.category_accuracy_summary.setObjectName("Subtle")
        self.category_accuracy_summary.setWordWrap(True)
        self.category_accuracy_chart = CategoryForecastAccuracyChart(
            lambda: self.app.theme
        )
        self.category_accuracy_chart.configure_details(
            self._category_accuracy_detail_payload,
            action_label="Abrir histórico da categoria",
            action_handler=self._open_category_accuracy,
        )
        self.category_accuracy_card = GlassCard(
            "🧩 Cobertura das faixas por categoria",
            strong=True,
        )
        self.category_accuracy_card.setProperty(
            "pageCardKey",
            "forecast_category_accuracy",
        )
        self.category_accuracy_card.v.addWidget(self.category_accuracy_summary)
        self.category_accuracy_card.v.addWidget(self.category_accuracy_chart)

        self.seasonality_category = QComboBox()
        self.seasonality_category.addItem("Todas as categorias", "")
        for category in self.repo.categories("expense"):
            self.seasonality_category.addItem(category, category)
        self.seasonality_category.setMinimumWidth(220)
        seasonality_controls = FlowLayout(hspacing=8, vspacing=8)
        seasonality_controls.addWidget(QLabel("Categoria"))
        seasonality_controls.addWidget(self.seasonality_category)
        self.seasonality_chart = SpendingSeasonalityChart(lambda: self.app.theme)
        self.seasonality_chart.configure_details(
            self._seasonality_detail_payload,
            action_label="Abrir período analisado",
            action_handler=self._open_seasonality_period,
        )
        seasonality_hint = QLabel(
            "Média diária por dia da semana e mês, incluindo os dias sem despesa. "
            "Passa o cursor por cada célula para ver total e movimentos."
        )
        seasonality_hint.setObjectName("Subtle")
        seasonality_hint.setWordWrap(True)
        self.seasonality_card = GlassCard(
            "🗓 Sazonalidade das despesas",
            strong=True,
        )
        self.seasonality_card.setProperty(
            "pageCardKey",
            "spending_seasonality",
        )
        self.seasonality_card.v.addLayout(seasonality_controls)
        self.seasonality_card.v.addWidget(seasonality_hint)
        self.seasonality_card.v.addWidget(self.seasonality_chart)

        self.merchant_limit = QComboBox()
        for label, value in (("Top 8", 8), ("Top 12", 12), ("Top 16", 16)):
            self.merchant_limit.addItem(label, value)
        self.merchant_limit.setCurrentIndex(1)
        merchant_controls = FlowLayout(hspacing=8, vspacing=8)
        merchant_controls.addWidget(QLabel("Detalhe"))
        merchant_controls.addWidget(self.merchant_limit)
        self.merchant_pareto_summary = QLabel()
        self.merchant_pareto_summary.setObjectName("Subtle")
        self.merchant_pareto_summary.setWordWrap(True)
        self.merchant_pareto_chart = MerchantParetoChart(lambda: self.app.theme)
        self.merchant_pareto_chart.configure_details(
            self._merchant_detail_payload,
            action_label="Abrir comerciante",
            action_handler=self._open_merchant_movements,
        )
        self.merchant_pareto_card = GlassCard(
            "🏪 Concentração 80/20 por comerciante",
            strong=True,
        )
        self.merchant_pareto_card.setProperty(
            "pageCardKey",
            "merchant_pareto",
        )
        self.merchant_pareto_card.v.addLayout(merchant_controls)
        self.merchant_pareto_card.v.addWidget(self.merchant_pareto_summary)
        self.merchant_pareto_card.v.addWidget(self.merchant_pareto_chart)

        self.balance_chart = BalanceChart(lambda: self.app.theme)
        self.balance_chart.configure_details(
            self._balance_detail_payload,
            action_label="Abrir movimentos do ponto",
            action_handler=self._open_balance_movements,
        )
        self.balance_chart.setMinimumHeight(450)
        self.category_chart = DonutChart(lambda: self.app.theme)
        self.category_chart.configure_details(
            self._category_detail_payload,
            action_label="Abrir categoria",
        )
        self.category_chart.category_clicked.connect(self._open_report_category)
        self.category_chart.setMinimumHeight(260)
        self.balance_card = GlassCard("📈 Evolução do saldo no período", strong=True)
        self.balance_card.setProperty("pageCardKey", "balance_period")
        self.balance_card.v.addWidget(self.balance_chart)
        self.category_card = GlassCard("🍩 Categorias do período", strong=True)
        # Keep the customization preference stable while the visible title is
        # updated with the selected 3/6/12-month range.
        self.category_card.setProperty("pageCardKey", "categories_period")
        self.category_card.v.addWidget(self.category_chart)
        self.table = QTableWidget(0, 5)
        self.table.setProperty("columnPersistenceId", "reports.period_comparison")
        self.table.setHorizontalHeaderLabels(
            ["Mês", "Receitas", "Despesas", "Poupança", "Taxa"]
        )
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setMinimumHeight(450)
        self.table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.history_card = GlassCard("📅 Histórico do período por mês", strong=True)
        self.history_card.setProperty("pageCardKey", "history_period")
        self.history_card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.history_card.v.addWidget(self.table)
        self.body.addLayout(top)
        self.body.addLayout(self.summary)
        self.register_filter_panel(self.comparison_card)
        self.body.addWidget(self.comparison_card)
        self.body.addWidget(self.comparison_explanations_card)
        self.body.addWidget(self.anomaly_card)
        self.body.addWidget(self.forecast_accuracy_card)
        self.body.addWidget(self.forecast_causes_card)
        self.body.addWidget(self.category_accuracy_card)
        self.body.addWidget(self.seasonality_card)
        self.body.addWidget(self.merchant_pareto_card)
        self.body.addWidget(self.balance_card)
        self.body.addWidget(self.category_card)
        self.body.addWidget(self.history_card)

        self.range.currentIndexChanged.connect(self._on_report_range_changed)
        self.export_btn.clicked.connect(self.export_txt)
        self.compare_btn.clicked.connect(self._refresh_period_comparison)
        self.compare_sync_btn.clicked.connect(self._sync_comparison_to_selected_range)
        self.anomaly_table.cellDoubleClicked.connect(self._open_anomaly)
        self.forecast_causes_month.currentIndexChanged.connect(
            self._refresh_forecast_causes
        )
        self.seasonality_category.currentIndexChanged.connect(
            lambda *_: self._refresh_spending_diagnostics()
        )
        self.merchant_limit.currentIndexChanged.connect(
            lambda *_: self._refresh_spending_diagnostics()
        )
        self.refresh()

    def clear_summary(self):
        while self.summary.count():
            item = self.summary.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    @staticmethod
    def _clear_metric_layout(layout: QGridLayout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def _sync_comparison_to_selected_range(
        self,
        _checked: bool = False,
        *,
        refresh: bool = True,
    ) -> None:
        months = self.months()
        current_start, current_end, _days = rolling_month_window(
            date.today(),
            months,
        )
        reference_end = current_start - timedelta(days=1)
        reference_start, _end, _reference_days = rolling_month_window(
            reference_end,
            months,
        )
        for editor, value in (
            (self.compare_current_start, current_start),
            (self.compare_current_end, current_end),
            (self.compare_reference_start, reference_start),
            (self.compare_reference_end, reference_end),
        ):
            editor.setDate(QDate(value.year, value.month, value.day))
        self.compare_sync_btn.setText(f"↻ Usar {months} meses")
        if refresh:
            self._refresh_period_comparison()

    def _on_report_range_changed(self, _index: int) -> None:
        self._sync_comparison_to_selected_range(refresh=False)
        self.refresh()

    def _refresh_period_comparison(self) -> None:
        current_start = self.compare_current_start.date().toPython()
        current_end = self.compare_current_end.date().toPython()
        reference_start = self.compare_reference_start.date().toPython()
        reference_end = self.compare_reference_end.date().toPython()
        if current_start > current_end or reference_start > reference_end:
            QMessageBox.warning(
                self, APP_TITLE, "A data inicial não pode ser posterior à data final."
            )
            return
        comparison = self.analytics_service.compare_periods(
            current_start.isoformat(),
            current_end.isoformat(),
            reference_start.isoformat(),
            reference_end.isoformat(),
        )
        self._comparison_data = comparison
        self.period_comparison_chart.set_data(comparison)
        category_rows = list(comparison.get("categories", []) or [])
        active_categories = sum(
            1
            for row in category_rows
            if safe_float(row.get("current")) or safe_float(row.get("reference"))
        )
        inactive_categories = max(0, len(category_rows) - active_categories)
        self.comparison_scope_hint.setText(
            f"{_private_count(len(category_rows), self.app.theme.privacy)} categorias no comparador · "
            f"{_private_count(active_categories, self.app.theme.privacy)} com movimentos nos intervalos escolhidos"
            + (
                f" · {_private_count(inactive_categories, self.app.theme.privacy)} sem movimentos aparecem a zero."
                if inactive_categories
                else "."
            )
        )
        self._clear_metric_layout(self.comparison_summary)
        definitions = (
            ("income", "Receitas", GREEN, "📥", True),
            ("expenses", "Despesas", RED, "📤", False),
            ("savings", "Poupança", BLUE, "💰", True),
        )
        for index, (key, title, color, icon, increase_is_good) in enumerate(
            definitions
        ):
            metric = comparison.get("metrics", {}).get(key, {})
            current = safe_float(metric.get("current"))
            reference = safe_float(metric.get("reference"))
            delta = safe_float(metric.get("delta"))
            variation = format_period_variation(
                current,
                reference,
                privacy=self.app.theme.privacy,
            )
            good = delta >= 0 if increase_is_good else delta <= 0
            self.comparison_summary.addWidget(
                MetricCard(
                    title,
                    eur(current, self.app.theme.privacy),
                    (
                        ("+" if delta > 0 else "")
                        + eur(delta, self.app.theme.privacy)
                        + f" · {variation['display']}"
                        + (f" ({variation['context']})" if variation["huge"] else "")
                    ),
                    color,
                    icon,
                    status=("Melhorou" if good else "Piorou") if delta else "Estável",
                    tone="positive"
                    if good and delta
                    else "negative"
                    if delta
                    else "neutral",
                ),
                0,
                index,
            )
        self.reflow_responsive_grid(self.comparison_summary)
        if self.should_compute_card(self.comparison_explanations_card):
            while self.comparison_explanations_card.v.count() > 1:
                item = self.comparison_explanations_card.v.takeAt(1)
                if item.widget():
                    item.widget().deleteLater()
            tone_colors = {
                "positive": GREEN,
                "negative": RED,
                "warning": ORANGE,
                "neutral": BLUE,
            }
            explanations = self.analytics_service.explain_period_variation(comparison)
            for explanation in explanations:
                color = QColor(tone_colors.get(str(explanation.get("tone")), BLUE))
                background = f"rgba({color.red()},{color.green()},{color.blue()},28)"
                border = f"rgba({color.red()},{color.green()},{color.blue()},82)"
                delta = safe_float(explanation.get("delta"))
                delta_text = ("+" if delta > 0 else "") + eur(
                    delta, self.app.theme.privacy
                )
                variation = format_period_variation(
                    safe_float(explanation.get("current")),
                    safe_float(explanation.get("reference")),
                    privacy=self.app.theme.privacy,
                )
                change_text = str(variation["display"])
                context_text = f" · {variation['context']}" if variation["huge"] else ""
                label = QLabel(
                    f"<b>{html_text(_private_text(explanation.get('title'), self.app.theme.privacy))}</b> · {delta_text} "
                    f"({change_text}){context_text}<br>"
                    f"<span style='color:{self.app.theme.text}'>"
                    f"{html_text(_private_text(explanation.get('message'), self.app.theme.privacy))} "
                    f"{variation['explanation'] if variation['huge'] else ''}</span>"
                )
                label.setWordWrap(True)
                label.setStyleSheet(
                    f"color: {self.app.theme.text}; background: {background}; "
                    f"border: 1px solid {border}; border-radius: 12px; padding: 10px;"
                )
                self.comparison_explanations_card.v.addWidget(label)

    def _refresh_anomalies(self) -> None:
        _start, end, days, period_label = self._period_window(self.months())
        self._anomalies = self.analytics_service.detect_financial_anomalies(
            date.fromisoformat(end), max(180, days + 120), days
        )
        if self.anomaly_card.title_label is not None:
            self.anomaly_card.title_label.setText(
                f"🔎 Deteção de anomalias · {period_label}"
            )
        self.anomaly_table.clearContents()
        self.anomaly_table.setRowCount(len(self._anomalies))
        severity_labels = {
            "critical": ("🚨 Crítica", RED),
            "warning": ("⚠️ Atenção", ORANGE),
            "info": ("ℹ️ Informação", BLUE),
        }
        for row, anomaly in enumerate(self._anomalies):
            label, color = severity_labels.get(
                str(anomaly.get("severity")), ("ℹ️ Informação", BLUE)
            )
            values = [
                label,
                anomaly.get("date") or "—",
                _private_text(f"{anomaly.get('title')} · {anomaly.get('message')}", self.app.theme.privacy),
                _private_text(anomaly.get("category") or "—", self.app.theme.privacy),
                eur(anomaly.get("amount", 0), self.app.theme.privacy),
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if column == 0:
                    item.setForeground(QColor(color))
                if column == 4:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self.anomaly_table.setItem(row, column, item)

    def _open_anomaly(self, row: int, _column: int = 0) -> None:
        if not (0 <= row < len(getattr(self, "_anomalies", []))):
            return
        anomaly = self._anomalies[row]
        movements = self.app.pages.get("movimentos")
        if movements is None:
            return
        day = QDate.fromString(str(anomaly.get("date") or "")[:10], "yyyy-MM-dd")
        movements.search.setText(str(anomaly.get("search") or ""))
        if day.isValid():
            movements.period.setCurrentText("Personalizado")
            movements.start.setDate(day)
            movements.end.setDate(day)
        movements.refresh()
        self.app.goto("movimentos")

    def _reset_page_filters(self) -> None:
        self.range.setCurrentText("3 meses")
        self.seasonality_category.setCurrentIndex(0)
        self.merchant_limit.setCurrentIndex(1)
        today = date.today()
        current_start = today.replace(day=1)
        previous_end = current_start - timedelta(days=1)
        previous_start = previous_end.replace(day=1)
        self.compare_current_start.setDate(
            QDate(current_start.year, current_start.month, current_start.day)
        )
        self.compare_current_end.setDate(QDate(today.year, today.month, today.day))
        self.compare_reference_start.setDate(
            QDate(previous_start.year, previous_start.month, previous_start.day)
        )
        self.compare_reference_end.setDate(
            QDate(previous_end.year, previous_end.month, previous_end.day)
        )
        self.refresh()

    def _refresh_spending_diagnostics(self) -> None:
        start, end, _days, period_label = self._period_window(self.months())
        if any(
            self.should_compute_card(card)
            for card in (
                self.forecast_accuracy_card,
                self.forecast_causes_card,
            )
        ):
            forecast = self.analytics_service.forecast_accuracy(visible_months=12)
            self._forecast_accuracy_data = dict(forecast)
            self.forecast_accuracy_chart.set_data(forecast)
            forecast_coverage = dict(forecast.get("data_coverage") or {})
            coverage_text = self.data_coverage_text(forecast_coverage)
            coverage_tone = (
                "positive"
                if int(forecast_coverage.get("active_months", 0) or 0) >= 6
                else "warning"
            )
            self.forecast_accuracy_card.set_data_coverage(
                coverage_text,
                coverage_tone,
            )
            self.forecast_causes_card.set_data_coverage(
                coverage_text,
                coverage_tone,
            )
            previous_month = str(self.forecast_causes_month.currentData() or "")
            forecast_rows = list(forecast.get("rows", []) or [])
            self.forecast_causes_month.blockSignals(True)
            self.forecast_causes_month.clear()
            for row in forecast_rows:
                month = str(row.get("month") or "")
                self.forecast_causes_month.addItem(month, month)
            selected_index = self.forecast_causes_month.findData(previous_month)
            if selected_index < 0 and forecast_rows:
                worst = max(
                    forecast_rows,
                    key=lambda row: abs(safe_float(row.get("actual_delta"))),
                )
                selected_index = self.forecast_causes_month.findData(
                    str(worst.get("month") or "")
                )
            if selected_index >= 0:
                self.forecast_causes_month.setCurrentIndex(selected_index)
            self.forecast_causes_month.blockSignals(False)
            self._refresh_forecast_causes()
            if self.forecast_accuracy_card.title_label is not None:
                accuracy = safe_float(forecast.get("accuracy_pct"))
                status = "🟢" if accuracy >= 80 else "🟠" if accuracy >= 60 else "🔴"
                self.forecast_accuracy_card.title_label.setText(
                    f"{status} Previsão vs. realidade"
                )

        if self.should_compute_card(self.category_accuracy_card):
            category_accuracy = self.analytics_service.category_forecast_accuracy(
                visible_months=12
            )
            self._category_accuracy_data = dict(category_accuracy)
            self.category_accuracy_chart.set_data(category_accuracy)
            category_coverage = dict(category_accuracy.get("data_coverage") or {})
            self.category_accuracy_card.set_data_coverage(
                self.data_coverage_text(category_coverage),
                "positive"
                if int(category_coverage.get("active_months", 0) or 0) >= 6
                else "warning",
            )
            best = dict(category_accuracy.get("best") or {})
            hardest = dict(category_accuracy.get("hardest") or {})
            if best and hardest:
                self.category_accuracy_summary.setText(
                    f"Maior qualidade: {best.get('category')} "
                    f"({percent(best.get('accuracy_pct', 0), self.app.theme.privacy)}; "
                    f"cobertura {percent(best.get('interval_coverage_pct', 0), self.app.theme.privacy)}). "
                    f"Menor qualidade: {hardest.get('category')} "
                    f"({percent(hardest.get('accuracy_pct', 0), self.app.theme.privacy)}). "
                    "A nota penaliza erro e faixas demasiado largas."
                )
            else:
                self.category_accuracy_summary.setText(
                    "Ainda não existem meses suficientes para comparar categorias."
                )

        if self.should_compute_card(self.seasonality_card):
            category = str(self.seasonality_category.currentData() or "")
            seasonality = self.analytics_service.spending_seasonality(
                start,
                end,
                category=category,
            )
            self.seasonality_chart.set_data(seasonality)
            if self.seasonality_card.title_label is not None:
                scope = category or "todas as categorias"
                self.seasonality_card.title_label.setText(
                    f"🗓 Sazonalidade · {scope} · {period_label}"
                )

        if self.should_compute_card(self.merchant_pareto_card):
            pareto = self.analytics_service.merchant_pareto(
                start,
                end,
                limit=int(self.merchant_limit.currentData() or 12),
            )
            self.merchant_pareto_chart.set_data(pareto)
            merchant_count = int(pareto.get("merchants_count", 0) or 0)
            concentration = int(pareto.get("concentration_80_count", 0) or 0)
            self.merchant_pareto_summary.setText(
                (
                    f"{merchant_count} comerciante(s) no período · "
                    f"os primeiros {concentration} concentram cerca de 80% da despesa."
                )
                if merchant_count
                else "Sem despesas com comerciante ou descrição no período."
            )
            if self.merchant_pareto_card.title_label is not None:
                self.merchant_pareto_card.title_label.setText(
                    f"🏪 Concentração 80/20 por comerciante · {period_label}"
                )

    def months(self) -> int:
        # BUG-UI-05: an empty combo must not crash the page; fall back to the
        # last 6 months so every caller keeps a sane rolling window.
        text = self.range.currentText() if self.range.count() else ""
        if not text:
            return 6
        try:
            return int(text.split()[0])
        except (ValueError, IndexError):
            return 6

    @staticmethod
    def _period_window(
        months: int, reference: date | None = None
    ) -> tuple[str, str, int, str]:
        """Return the rolling window represented by the period selector."""
        selected_months = max(1, int(months or 1))
        end = reference or date.today()
        start, end, days = rolling_month_window(end, selected_months)
        label = f"últimos {selected_months} meses"
        return start.isoformat(), end.isoformat(), days, label

    def _report_period(self) -> tuple[str, str]:
        start = str(getattr(self, "_report_start", "") or "")
        end = str(getattr(self, "_report_end", "") or "")
        if start and end:
            return start, end
        start, end, _days, _label = self._period_window(self.months())
        return start, end

    def _comparison_detail_payload(self, item: dict[str, Any]) -> dict[str, Any]:
        comparison = dict(getattr(self, "_comparison_data", {}) or {})
        current = dict(comparison.get("current") or {})
        reference = dict(comparison.get("reference") or {})
        category = str(item.get("category") or "Categoria")
        current_rows = movement_evidence_rows(
            self.repo,
            self.app.theme,
            str(current.get("start") or ""),
            str(current.get("end") or ""),
            category=category,
            kind="Despesas",
            limit=6,
        )
        reference_rows = movement_evidence_rows(
            self.repo,
            self.app.theme,
            str(reference.get("start") or ""),
            str(reference.get("end") or ""),
            category=category,
            kind="Despesas",
            limit=6,
        )
        for row in current_rows:
            row["subtitle"] = f"Atual · {row.get('subtitle', '')}"
        for row in reference_rows:
            row["subtitle"] = f"Referência · {row.get('subtitle', '')}"
        delta = safe_float(item.get("delta"))
        return {
            "icon": "↔",
            "title": "Comparação de períodos",
            "subtitle": category,
            "explanation": (
                "Compara a mesma categoria nos dois intervalos escolhidos. "
                "A evidência identifica de que período vem cada movimento."
            ),
            "metrics": [
                {
                    "label": "Período atual",
                    "value": eur(item.get("current", 0), self.app.theme.privacy),
                    "tone": "neutral",
                },
                {
                    "label": "Referência",
                    "value": eur(item.get("reference", 0), self.app.theme.privacy),
                    "tone": "neutral",
                },
                {
                    "label": "Diferença",
                    "value": eur(delta, self.app.theme.privacy),
                    "tone": "negative" if delta > 0 else "positive",
                },
                {
                    "label": "Variação",
                    "value": percent(item.get("change_pct", 0), self.app.theme.privacy),
                    "tone": "negative" if delta > 0 else "positive",
                },
            ],
            "rows_title": "Movimentos nos dois períodos",
            "rows": current_rows + reference_rows,
            "show_empty_rows": True,
            "empty_rows_text": "Não existem movimentos nesta categoria.",
        }

    def _open_comparison_current(self, item: dict[str, Any]) -> None:
        comparison = dict(getattr(self, "_comparison_data", {}) or {})
        current = dict(comparison.get("current") or {})
        open_movements_view(
            self.app,
            start=str(current.get("start") or ""),
            end=str(current.get("end") or ""),
            kind="Despesas",
            category=str(item.get("category") or ""),
        )

    def _forecast_month_period(
        self,
        item: dict[str, Any],
    ) -> tuple[str, str]:
        start, end = self._report_period()
        return period_for_chart_item(
            item,
            fallback_start=start,
            fallback_end=end,
            clip_to_fallback=False,
        )

    def _forecast_accuracy_detail_payload(
        self,
        item: dict[str, Any],
    ) -> dict[str, Any]:
        start, end = self._forecast_month_period(item)
        payload = movement_detail_payload(
            self.repo,
            self.app.theme,
            title="Previsão vs. realidade",
            item=item,
            start=start,
            end=end,
            kind="Despesas",
            explanation=(
                "A previsão foi calculada apenas com meses anteriores. "
                "Os movimentos abaixo formam o valor real usado na comparação."
            ),
            rows_title="Despesas reais do mês",
        )
        payload["icon"] = "🎯"
        return payload

    def _open_forecast_month(self, item: dict[str, Any]) -> None:
        start, end = self._forecast_month_period(item)
        open_movements_view(
            self.app,
            start=start,
            end=end,
            kind="Despesas",
        )

    def _selected_forecast_row(self) -> dict[str, Any]:
        month = str(self.forecast_causes_month.currentData() or "")
        rows = list(getattr(self, "_forecast_accuracy_data", {}).get("rows", []))
        return next(
            (dict(row) for row in rows if str(row.get("month") or "") == month),
            {},
        )

    def _refresh_forecast_causes(self, _index: int = -1) -> None:
        row = self._selected_forecast_row()
        self.forecast_causes_chart.set_data(row)
        if not row:
            self.forecast_causes_summary.setText(
                "Ainda não existe um mês completo com erro explicável."
            )
            return
        difference = safe_float(row.get("actual")) - safe_float(row.get("predicted"))
        direction = (
            "acima do previsto"
            if difference > 0
            else "abaixo do previsto"
            if difference < 0
            else "igual ao previsto"
        )
        self.forecast_causes_summary.setText(
            f"{_private_text(row.get('cause_headline', ''), self.app.theme.privacy)} O total real ficou "
            f"{eur(abs(difference), self.app.theme.privacy)} {direction}. "
            "As barras reconciliam as categorias que empurraram a despesa "
            "para cima ou para baixo."
        )

    def _forecast_cause_detail_payload(
        self,
        item: dict[str, Any],
    ) -> dict[str, Any]:
        row = self._selected_forecast_row()
        start, end = self._forecast_month_period(row)
        category = str(item.get("category") or "")
        effective_category = "" if category == "Estimativa geral" else category
        payload = movement_detail_payload(
            self.repo,
            self.app.theme,
            title="Causa do erro da previsão",
            item=item,
            start=start,
            end=end,
            category=effective_category,
            kind="Despesas",
            subtitle=f"{category} · {row.get('month', '')}",
            explanation=str(item.get("reason") or "Variação face ao padrão recente."),
            rows_title="Despesas reais que explicam a categoria",
        )
        payload["icon"] = "🧭"
        return payload

    def _open_forecast_cause(self, item: dict[str, Any]) -> None:
        row = self._selected_forecast_row()
        start, end = self._forecast_month_period(row)
        category = str(item.get("category") or "")
        open_movements_view(
            self.app,
            start=start,
            end=end,
            kind="Despesas",
            category="" if category == "Estimativa geral" else category,
        )

    def _category_accuracy_period(self) -> tuple[str, str]:
        data = dict(getattr(self, "_category_accuracy_data", {}) or {})
        start = str(data.get("period_start") or "")
        end = str(data.get("period_end") or "")
        return (start, end) if start and end else self._report_period()

    def _category_accuracy_detail_payload(
        self,
        item: dict[str, Any],
    ) -> dict[str, Any]:
        start, end = self._category_accuracy_period()
        category = str(item.get("category") or "")
        payload = movement_detail_payload(
            self.repo,
            self.app.theme,
            title="Qualidade prevista da categoria",
            item=item,
            start=start,
            end=end,
            category=category,
            kind="Despesas",
            subtitle=f"{category} · {item.get('status', '')}",
            explanation=(
                "A qualidade combina erro pontual, resultados dentro da faixa, "
                "calibração e largura do intervalo sem usar dados futuros. "
                "Assim, uma faixa demasiado larga deixa de parecer precisa só "
                "porque inclui todos os resultados."
            ),
            rows_title="Maiores movimentos no histórico avaliado",
        )
        payload["icon"] = "🧩"
        return payload

    def _open_category_accuracy(self, item: dict[str, Any]) -> None:
        start, end = self._category_accuracy_period()
        open_movements_view(
            self.app,
            start=start,
            end=end,
            kind="Despesas",
            category=str(item.get("category") or ""),
        )

    def _seasonality_detail_payload(
        self,
        item: dict[str, Any],
    ) -> dict[str, Any]:
        start, end = self._report_period()
        category = str(self.seasonality_category.currentData() or "")
        return movement_detail_payload(
            self.repo,
            self.app.theme,
            title="Sazonalidade das despesas",
            item=item,
            start=start,
            end=end,
            category=category,
            kind="Despesas",
            month_number=int(item.get("month", 1) or 1),
            weekday=int(item.get("weekday", 0) or 0),
            subtitle=(
                f"{item.get('weekday_label') or ''} · {item.get('month_label') or ''}"
            ),
            explanation=(
                "A célula agrega apenas este dia da semana e este mês do ano "
                "dentro do período analisado; os dias sem despesa entram na média."
            ),
            rows_title="Movimentos exatos desta célula",
        )

    def _open_seasonality_period(self, _item: dict[str, Any]) -> None:
        start, end = self._report_period()
        open_movements_view(
            self.app,
            start=start,
            end=end,
            kind="Despesas",
            category=str(self.seasonality_category.currentData() or ""),
        )

    @staticmethod
    def _is_aggregate_merchant(merchant: str) -> bool:
        return merchant.startswith("Outros (") and merchant.endswith(")")

    def _merchant_detail_payload(self, item: dict[str, Any]) -> dict[str, Any]:
        start, end = self._report_period()
        merchant = str(item.get("merchant") or "Sem comerciante")
        aggregate = self._is_aggregate_merchant(merchant)
        return movement_detail_payload(
            self.repo,
            self.app.theme,
            title="Concentração por comerciante",
            item=item,
            start=start,
            end=end,
            kind="Despesas",
            search_text="" if aggregate else merchant,
            subtitle=merchant,
            explanation=(
                "Este ponto reúne os comerciantes fora do limite visível; "
                "a lista mostra as maiores despesas do período."
                if aggregate
                else (
                    "A barra soma os movimentos cuja descrição ou comerciante "
                    "corresponde à seleção."
                )
            ),
            rows_title="Movimentos que formam este total",
        )

    def _open_merchant_movements(self, item: dict[str, Any]) -> None:
        start, end = self._report_period()
        merchant = str(item.get("merchant") or "")
        open_movements_view(
            self.app,
            start=start,
            end=end,
            kind="Despesas",
            search_text="" if self._is_aggregate_merchant(merchant) else merchant,
        )

    def _balance_point_period(self, item: dict[str, Any]) -> tuple[str, str]:
        start, end = self._report_period()
        return period_for_chart_item(
            item,
            fallback_start=start,
            fallback_end=end,
        )

    def _balance_detail_payload(self, item: dict[str, Any]) -> dict[str, Any]:
        start, end = self._balance_point_period(item)
        return movement_detail_payload(
            self.repo,
            self.app.theme,
            title="Evolução do saldo",
            item=item,
            start=start,
            end=end,
            explanation=(
                "Mostra o saldo no ponto selecionado e os movimentos financeiros "
                "que ocorreram nesse intervalo."
            ),
        )

    def _open_balance_movements(self, item: dict[str, Any]) -> None:
        start, end = self._balance_point_period(item)
        open_movements_view(self.app, start=start, end=end)

    def _category_detail_payload(self, item: dict[str, Any]) -> dict[str, Any]:
        start, end = self._report_period()
        category = str(item.get("label") or item.get("category") or "")
        return movement_detail_payload(
            self.repo,
            self.app.theme,
            title="Categorias do período",
            item=item,
            start=start,
            end=end,
            category=category,
            kind="Despesas",
            subtitle=category,
        )

    def _open_report_category(self, category: str) -> None:
        start, end = self._report_period()
        open_movements_view(
            self.app,
            start=start,
            end=end,
            kind="Despesas",
            category=category,
        )

    def refresh(self):
        months = self.months()
        start, end, days, period_label = self._period_window(months)
        self._report_start = start
        self._report_end = end
        balance_points = self.analytics_service.balance_forecast_series(
            historical_days=days,
            forecast_days=30,
        )
        self.balance_chart.set_points(balance_points)
        balance_coverage = next(
            (
                dict(point.get("data_coverage") or {})
                for point in balance_points
                if point.get("data_coverage")
            ),
            {},
        )
        if balance_coverage:
            self.balance_card.set_data_coverage(
                self.data_coverage_text(balance_coverage),
                "positive"
                if int(balance_coverage.get("active_months", 0) or 0) >= 6
                else "warning",
            )
        self.category_chart.set_data(self.analytics_service.category_totals(start, end))
        if self.category_card.title_label is not None:
            self.category_card.title_label.setText(
                f"🍩 Categorias do período · {period_label}"
            )
        self.category_chart.setAccessibleDescription(
            f"Distribuição das despesas por categoria nos {period_label}, "
            f"de {start} a {end}."
        )
        if self.balance_card.title_label is not None:
            self.balance_card.title_label.setText(
                f"📈 Evolução do saldo · {period_label}"
            )
        if self.history_card.title_label is not None:
            self.history_card.title_label.setText(
                f"📅 Histórico mensal · {period_label}"
            )

        summaries = self.analytics_service.monthly_summary_for_period(start, end)
        income = sum(safe_float(r.get("income", 0)) for r in summaries)
        expenses = sum(
            safe_float(r.get("expenses", r.get("expense", 0))) for r in summaries
        )
        savings = income - expenses
        rate = (savings / income * 100) if income else 0
        self.clear_summary()
        for i, m in enumerate(
            [
                (
                    "Receitas",
                    eur(income, self.app.theme.privacy),
                    f"últimos {months} meses",
                    GREEN,
                    "↓",
                ),
                (
                    "Despesas",
                    eur(expenses, self.app.theme.privacy),
                    f"últimos {months} meses",
                    RED,
                    "↑",
                ),
                (
                    "Poupança",
                    eur(savings, self.app.theme.privacy),
                    percent(rate, self.app.theme.privacy),
                    BLUE,
                    "=",
                ),
                (
                    "Média mensal",
                    eur(savings / max(1, months), self.app.theme.privacy),
                    "poupança",
                    PURPLE,
                    "≈",
                ),
            ]
        ):
            self.summary.addWidget(MetricCard(*m), 0, i)
        self.reflow_responsive_grid(self.summary)
        self.table.setRowCount(len(summaries))
        for row, r in enumerate(summaries):
            inc = safe_float(r.get("income", 0))
            exp = safe_float(r.get("expenses", r.get("expense", 0)))
            sav = safe_float(r.get("savings", inc - exp))
            sr = (sav / inc * 100) if inc else safe_float(r.get("savings_rate", 0))
            vals = [
                r.get("month", ""),
                eur(inc, self.app.theme.privacy),
                eur(exp, self.app.theme.privacy),
                eur(sav, self.app.theme.privacy),
                percent(sr, self.app.theme.privacy),
            ]
            for col, val in enumerate(vals):
                item = QTableWidgetItem(str(val))
                if col > 0:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self.table.setItem(row, col, item)
        if self.should_compute_card(self.comparison_card) or self.should_compute_card(
            self.comparison_explanations_card
        ):
            self._refresh_period_comparison()
        if self.should_compute_card(self.anomaly_card):
            self._refresh_anomalies()
        if any(
            self.should_compute_card(card)
            for card in (
                self.forecast_accuracy_card,
                self.forecast_causes_card,
                self.category_accuracy_card,
                self.seasonality_card,
                self.merchant_pareto_card,
            )
        ):
            self._refresh_spending_diagnostics()

    def export_txt(self):
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Exportar análise",
            str(Path.home() / "analise_financeira.txt"),
            "Texto (*.txt)",
        )
        if not path:
            return
        try:
            start, end, _days, _label = self._period_window(self.months())
            summaries = self.analytics_service.monthly_summary_for_period(start, end)
            privacy = self.app.theme.privacy
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write("Análise financeira\n\n")
                for r in summaries:
                    inc = safe_float(r.get("income", 0))
                    exp = safe_float(r.get("expenses", r.get("expense", 0)))
                    sav = safe_float(r.get("savings", inc - exp))
                    sr = (sav / inc * 100) if inc else safe_float(r.get("savings_rate", 0))
                    rate_str = "•••%" if privacy else f"{sr:.1f}%"
                    f.write(
                        f"{r.get('month', '')}: receitas {eur(inc, privacy)}, "
                        f"despesas {eur(exp, privacy)}, poupança {eur(sav, privacy)}, "
                        f"taxa {rate_str}\n"
                    )
            QMessageBox.information(self, APP_TITLE, f"Exportado para:\n{path}")
        except PermissionError as exc:
            log_exception(f"Ficheiro de análise bloqueado em {path}", exc)
            QMessageBox.critical(
                self,
                "Ficheiro em Uso",
                f"Não foi possível gravar '{Path(path).name}'. O ficheiro está aberto noutro programa. Feche o ficheiro e tente novamente.",
            )
        except Exception as exc:
            log_exception(f"Falha ao exportar análise para {path}", exc)
            QMessageBox.critical(self, APP_TITLE, f"Erro ao exportar análise:\n{exc}")


from .history import HistoryPage as HistoryPage
