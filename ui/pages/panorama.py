from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFileDialog,
    QGridLayout,
    QLabel,
    QMessageBox,
    QPushButton,
)

from ui.components import (
    CompactProgressPill,
    GlassCard,
    MetricCard,
    MonthlyEvolutionChart,
    ProgressPill,
    ScoreGauge,
    StackedExpenseChart,
)
from ui.components.charts import SankeyChart
from ui.chart_detail_support import (
    movement_detail_payload,
    open_movements_view,
    period_for_chart_item,
)
from ui.core import BasePage, EuroSpinBox, FlowLayout
from ui.dialogs import BudgetBucketDialog
from utils.output_security import html_text, safe_hex_colour
from utils.app_logging import log_exception
from utils.helpers import (
    APP_TITLE,
    BLUE,
    GREEN,
    ORANGE,
    PT_MONTHS,
    PURPLE,
    RED,
    eur,
    format_iso_date,
    percent,
    safe_year_from_combo,
)

if TYPE_CHECKING:
    from main import LiquidFinanceApp


class PanoramaPage(BasePage):
    def __init__(self, app: "LiquidFinanceApp"):
        super().__init__(
            app,
            "Panorama financeiro",
            "Diagnóstico mensal, metas, análise 50/30/20 e composição de despesas",
        )
        controls = GlassCard(strong=True)
        self.filters_card = controls
        row = FlowLayout(hspacing=10, vspacing=8)
        self.month = QComboBox()
        self.month.addItems([m.capitalize() for m in PT_MONTHS])
        self.month.setCurrentIndex(date.today().month - 1)
        self.year = QComboBox()
        self.year.addItems([str(y) for y in self.repo.available_years()])
        self.year.setCurrentText(str(date.today().year))
        self.expense_target = EuroSpinBox()
        self.expense_target.setRange(0, 100_000_000)
        self.expense_target.setDecimals(2)
        self.expense_target.setSuffix(" €")
        self.save_target_btn = QPushButton("Guardar meta")
        self.save_target_btn.setObjectName("Primary")
        self.export_btn = QPushButton("Exportar relatório")
        row.addWidget(QLabel("Mês"))
        row.addWidget(self.month)
        row.addWidget(QLabel("Ano"))
        row.addWidget(self.year)
        row.addWidget(QLabel("Meta de gastos"))
        row.addWidget(self.expense_target)
        row.addWidget(self.save_target_btn)
        row.addWidget(self.export_btn)
        controls.v.addLayout(row)

        self.metrics = QGridLayout()
        self.metrics.setSpacing(14)
        self.register_responsive_grid(self.metrics)
        self.diagnostic = GlassCard("Diagnóstico do mês", strong=True)
        self.fifty = GlassCard("Análise 50/30/20", strong=True)
        self.payment = GlassCard("Distribuição por forma de pagamento", strong=True)
        self.diagnostic.set_compact_header()
        self.payment.set_compact_header()
        self.evolution = MonthlyEvolutionChart(lambda: self.app.theme)
        self.composition = StackedExpenseChart(lambda: self.app.theme)
        self._period_highlight_timer = QTimer(self)
        self._period_highlight_timer.setSingleShot(True)
        self._period_highlight_timer.timeout.connect(self._clear_period_highlight)
        self._highlighted_period = ""
        self.evolution.period_hovered.connect(self._on_period_hovered)
        self.composition.period_hovered.connect(self._on_period_hovered)
        self.sankey = SankeyChart(lambda: self.app.theme)
        self.evolution.setAccessibleName("Evolução mensal do ano selecionado")
        self.composition.setAccessibleName("Composição mensal das despesas")
        self.sankey.setAccessibleName("Fluxo das despesas por categoria")
        self.evolution.configure_details(
            self._evolution_detail_payload,
            action_label="Abrir movimentos do mês",
            action_handler=self._open_evolution_movements,
        )
        self.composition.configure_details(
            self._composition_detail_payload,
            action_label="Abrir despesas do mês",
            action_handler=self._open_composition_movements,
        )
        self.sankey.configure_details(
            self._sankey_detail_payload,
            action_label="Abrir categoria",
        )
        self.sankey.category_clicked.connect(self._open_sankey_category)
        self.evo_card = GlassCard("Evolução mensal do ano selecionado", strong=True)
        self.evo_card.v.addWidget(self.evolution)
        self.comp_card = GlassCard("Composição de despesas por mês", strong=True)
        self.comp_card.v.addWidget(self.composition)
        self.sankey_card = GlassCard("Fluxo de Dinheiro (Sankey)", strong=True)
        self.sankey_card.v.addWidget(self.sankey)
        self.content_grid = QGridLayout()
        self.content_grid.setSpacing(16)
        self.register_filter_panel(controls)
        self.body.addWidget(controls)
        self.body.addLayout(self.metrics)
        self.body.addLayout(self.content_grid)
        self._layout_content_cards()
        self.month.currentIndexChanged.connect(lambda *_: self.refresh())
        self.year.currentTextChanged.connect(lambda *_: self.refresh())
        self.save_target_btn.clicked.connect(self.save_target)
        self.export_btn.clicked.connect(self.export_html)
        self.refresh()

    def _layout_content_cards(self) -> None:
        viewport_w = (
            self.app.content_scroll.viewport().width()
            if hasattr(self.app, "content_scroll")
            else 0
        )
        width = max(self.width(), viewport_w, 0)
        columns = 3 if width >= 900 else (2 if width >= 620 else 1)
        if getattr(self, "_last_layout_columns", None) == columns:
            return
        self._last_layout_columns = columns

        cards = (
            self.diagnostic,
            self.fifty,
            self.payment,
            self.evo_card,
            self.comp_card,
            self.sankey_card,
        )
        for card in cards:
            self.content_grid.removeWidget(card)
        if columns == 3:
            self.content_grid.addWidget(self.diagnostic, 0, 0, 1, 2)
            self.content_grid.addWidget(self.fifty, 0, 2)
            self.content_grid.addWidget(self.evo_card, 1, 0, 1, 2)
            self.content_grid.addWidget(self.payment, 1, 2)
            self.content_grid.addWidget(self.comp_card, 2, 0, 1, 3)
            self.content_grid.addWidget(self.sankey_card, 3, 0, 1, 3)
        elif columns == 2:
            self.content_grid.addWidget(self.diagnostic, 0, 0, 1, 2)
            self.content_grid.addWidget(self.fifty, 1, 0)
            self.content_grid.addWidget(self.payment, 1, 1)
            self.content_grid.addWidget(self.evo_card, 2, 0, 1, 2)
            self.content_grid.addWidget(self.comp_card, 3, 0, 1, 2)
            self.content_grid.addWidget(self.sankey_card, 4, 0, 1, 2)
        else:
            for row, card in enumerate(cards):
                self.content_grid.addWidget(card, row, 0)
        for column in range(3):
            self.content_grid.setColumnStretch(column, 1 if column < columns else 0)
        self.content_grid.invalidate()

    def reflow_responsive_grids(self) -> None:
        super().reflow_responsive_grids()
        if hasattr(self, "content_grid"):
            self._layout_content_cards()

    def showEvent(self, event):  # type: ignore[override]
        super().showEvent(event)
        if hasattr(self, "content_grid"):
            self._layout_content_cards()
        self._schedule_responsive_reflow()

    def resizeEvent(self, event):  # type: ignore[override]
        super().resizeEvent(event)
        if hasattr(self, "content_grid"):
            self._layout_content_cards()

    def _set_period_highlight(self, period: str) -> None:
        selected = str(period or "").strip()
        self._period_highlight_timer.stop()
        self._highlighted_period = selected
        for chart in (self.evolution, self.composition):
            chart.set_highlighted_period(selected)

    def _on_period_hovered(self, period: str) -> None:
        selected = str(period or "").strip()
        if selected:
            self._set_period_highlight(selected)
        elif self._highlighted_period:
            self._period_highlight_timer.start(650)

    def _clear_period_highlight(self) -> None:
        self._set_period_highlight("")

    def _selected_year(self) -> int:
        return safe_year_from_combo(self.year)

    def selected_period(self) -> tuple[str, str, str, int, int]:
        y = self._selected_year()
        m = self.month.currentIndex() + 1
        s, e, ym = self.repo.month_period(y, m)
        if ym == date.today().strftime("%Y-%m"):
            e = min(e, date.today().isoformat())
        return s, e, ym, y, m

    def _annual_period(self) -> tuple[str, str]:
        year = self._selected_year()
        end = f"{year}-12-31"
        if year == date.today().year:
            end = date.today().isoformat()
        return f"{year}-01-01", end

    def _detail_month_period(self, item: dict[str, Any]) -> tuple[str, str]:
        start, end = self._annual_period()
        return period_for_chart_item(
            item,
            fallback_start=start,
            fallback_end=end,
        )

    def _evolution_detail_payload(self, item: dict[str, Any]) -> dict[str, Any]:
        start, end = self._detail_month_period(item)
        return movement_detail_payload(
            self.repo,
            self.app.theme,
            title="Evolução mensal",
            item=item,
            start=start,
            end=end,
            explanation=(
                "Receitas, despesas e saldo do mês selecionado, acompanhados "
                "pelos movimentos de maior impacto."
            ),
        )

    def _composition_detail_payload(self, item: dict[str, Any]) -> dict[str, Any]:
        start, end = self._detail_month_period(item)
        return movement_detail_payload(
            self.repo,
            self.app.theme,
            title="Composição das despesas",
            item=item,
            start=start,
            end=end,
            kind="Despesas",
            explanation=(
                "A barra agrega todas as categorias de despesa do mês; a lista "
                "mostra os movimentos que mais contribuíram para o total."
            ),
        )

    def _sankey_detail_payload(self, item: dict[str, Any]) -> dict[str, Any]:
        start, end, _ym, _year, _month = self.selected_period()
        category = str(item.get("label") or item.get("category") or "")
        return movement_detail_payload(
            self.repo,
            self.app.theme,
            title="Fluxo de dinheiro por categoria",
            item=item,
            start=start,
            end=end,
            category=category,
            kind="Despesas",
            subtitle=category,
        )

    def _open_evolution_movements(self, item: dict[str, Any]) -> None:
        start, end = self._detail_month_period(item)
        open_movements_view(self.app, start=start, end=end)

    def _open_composition_movements(self, item: dict[str, Any]) -> None:
        start, end = self._detail_month_period(item)
        open_movements_view(
            self.app,
            start=start,
            end=end,
            kind="Despesas",
        )

    def _open_sankey_category(self, category: str) -> None:
        start, end, _ym, _year, _month = self.selected_period()
        open_movements_view(
            self.app,
            start=start,
            end=end,
            kind="Despesas",
            category=category,
        )

    def _reset_page_filters(self) -> None:
        self.month.blockSignals(True)
        self.year.blockSignals(True)
        self.month.setCurrentIndex(date.today().month - 1)
        self.year.setCurrentText(str(date.today().year))
        self.month.blockSignals(False)
        self.year.blockSignals(False)
        self.refresh()

    def wipe_card(self, card: GlassCard) -> None:
        def clear_layout(layout):
            while layout.count():
                item = layout.takeAt(0)
                if item.widget():
                    item.widget().deleteLater()
                elif item.layout():
                    clear_layout(item.layout())
                    item.layout().deleteLater()

        while card.v.count() > 1:
            item = card.v.takeAt(1)
            if item.widget():
                item.widget().deleteLater()
            elif item.layout():
                clear_layout(item.layout())
                item.layout().deleteLater()

    def clear_metrics(self) -> None:
        while self.metrics.count():
            item = self.metrics.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def refresh(self) -> None:
        self._clear_period_highlight()
        s, e, ym, year, month = self.selected_period()
        targets = self.repo.month_target(ym)
        self.expense_target.blockSignals(True)
        self.expense_target.setValue(targets.get("expense_target", 0.0))
        self.expense_target.blockSignals(False)
        totals = self.repo.totals_for_payment_period(s, e)
        income = totals["income"]
        expenses = totals["expenses"]
        savings = totals["savings"]
        # BUG-PAN-01: without income but with expenses, the month is in
        # deficit; showing 0% would hide it. Meta 0 configured is distinct
        # from no meta at all.
        if income:
            rate = savings / income * 100
        elif expenses > 0:
            rate = -100.0
        else:
            rate = 0.0
        target = targets.get("expense_target", 0.0)
        target_used = expenses / target * 100 if target else 0
        self.clear_metrics()
        cards = [
            (
                "Receitas",
                eur(income, self.app.theme.privacy),
                "mês de pagamento",
                GREEN,
                "↓",
            ),
            (
                "Despesas",
                eur(expenses, self.app.theme.privacy),
                "mês de pagamento",
                RED,
                "↑",
            ),
            (
                "Saldo",
                eur(savings, self.app.theme.privacy),
                "receitas - despesas",
                BLUE,
                "✓",
            ),
            ("Taxa de poupança", percent(rate), "resultado do mês", PURPLE, "◎"),
        ]
        if target:
            cards.append(
                (
                    "Meta de gastos",
                    percent(target_used),
                    f"{eur(expenses, self.app.theme.privacy)} de {eur(target, self.app.theme.privacy)}",
                    ORANGE if target_used < 95 else RED,
                    "◌",
                )
            )
        elif targets.get("has_row"):
            # BUG-PAN-01: a meta explicitly configured as 0 € is a conscious
            # decision and must be distinguishable from "no meta configured".
            cards.append(
                (
                    "Meta de gastos",
                    "Sem limite",
                    "Meta configurada a 0,00 €",
                    ORANGE,
                    "◌",
                )
            )
        for i, c in enumerate(cards):
            self.metrics.addWidget(MetricCard(*c), 0, i)
        self.reflow_responsive_grid(self.metrics)
        if self.should_compute_card(self.diagnostic):
            self.refresh_diagnostic(s, e, ym, totals, target)
        if self.should_compute_card(self.fifty):
            self.refresh_503020(s, e, totals)
        if self.should_compute_card(self.payment):
            self.refresh_payment(s, e)
        if self.should_compute_card(self.evo_card):
            self.evolution.set_rows(self.analytics_service.monthly_evolution(year))
        if self.should_compute_card(self.comp_card):
            self.refresh_composition(year)
        if self.should_compute_card(self.sankey_card):
            self.sankey.set_data(self.analytics_service.category_totals(s, e))
        self.apply_card_preferences()

    def card_preferences_changed(self) -> None:
        self.refresh()

    def refresh_diagnostic(
        self, s: str, e: str, ym: str, totals: dict[str, float], target: float
    ) -> None:
        self.wipe_card(self.diagnostic)
        income, expenses, savings = (
            totals["income"],
            totals["expenses"],
            totals["savings"],
        )
        # BUG-PAN-01: same deficit rule as the summary cards.
        if income:
            rate = savings / income * 100
        elif expenses > 0:
            rate = -100.0
        else:
            rate = 0.0
        score_data = self.analytics_service.detailed_financial_score(ym)
        score = int(score_data.get("score", 0) or 0)
        score_confidence = int(score_data.get("confidence", 0) or 0)
        data_date = format_iso_date(
            score_data.get("data_as_of"),
            "sem movimentos",
        )
        self.diagnostic.set_context_badge(
            f"Dados até {data_date}",
            (
                "positive"
                if score_confidence >= 80
                else "warning"
                if score_confidence >= 55
                else "negative"
            ),
        )
        top = self.repo.top_categories_payment_period(s, e, 6)

        gauge = ScoreGauge(lambda: self.app.theme, score)
        self.diagnostic.v.addWidget(gauge, alignment=Qt.AlignCenter)
        summary = QLabel(
            f"{score_data.get('marker', '')} Score 2.0: <b>{score}/100 · "
            f"{score_data.get('grade', '—')} · {score_data.get('status', '')}</b> · "
            f"Confiança: <b>{score_confidence}% "
            f"({score_data.get('confidence_label', '')})</b> · "
            f"dados até <b>{data_date}</b><br>"
            f"Custo do mês: <b>{eur(expenses, self.app.theme.privacy)}</b> · "
            f"Valor poupado: <b>{eur(savings, self.app.theme.privacy)}</b> · "
            f"Taxa: <b>{percent(rate)}</b>"
        )
        summary.setWordWrap(True)
        self.diagnostic.v.addWidget(summary)
        if top:
            positive_vals = [float(v) for _, v, _ in top if v > 0]
            if positive_vals:
                maxv = max(positive_vals)
            else:
                maxv = max(abs(float(v)) for _, v, _ in top)
            maxv = max(1.0, maxv)
            cat_grid = QGridLayout()
            cat_grid.setContentsMargins(0, 6, 0, 0)
            cat_grid.setHorizontalSpacing(10)
            cat_grid.setVerticalSpacing(8)
            cols = 3
            for idx, (name, val, color) in enumerate(top):
                f_val = float(val)
                progress = max(0.0, min(1.0, f_val / maxv)) if f_val > 0 else 0.0
                pill = CompactProgressPill(
                    name,
                    eur(val, self.app.theme.privacy),
                    progress,
                    color,
                    lambda: self.app.theme,
                )
                cat_grid.addWidget(pill, idx // cols, idx % cols)
            for col in range(cols):
                cat_grid.setColumnStretch(col, 1)
            self.diagnostic.v.addLayout(cat_grid)
        else:
            self.diagnostic.v.addWidget(QLabel("Sem gastos registados neste mês."))

    def refresh_503020(
        self,
        start: str,
        end: str,
        totals: dict[str, float],
    ) -> None:
        self.wipe_card(self.fifty)
        analysis = self.analytics_service.budget_rule_503020(start, end)
        income = float(analysis.get("income") or 0)
        self.fifty.v.addWidget(
            QLabel(f"Base: <b>{eur(income, self.app.theme.privacy)}</b>")
        )
        colors = {"Necessidades": GREEN, "Desejos": BLUE, "Poupança": PURPLE}
        for row in analysis.get("rows", []):
            bucket = str(row.get("bucket") or "Desejos")
            value = float(row.get("actual") or 0)
            share = float(row.get("actual_pct") or 0) / 100.0
            target_pct = float(row.get("target_pct") or 0)
            variance = float(row.get("variance") or 0)
            self.fifty.v.addWidget(
                ProgressPill(
                    f"{bucket} · real {percent(share * 100)} / meta {percent(target_pct)}",
                    eur(value, self.app.theme.privacy),
                    max(0.0, min(1.0, share)),
                    colors[bucket],
                    lambda: self.app.theme,
                )
            )
            variance_label = QLabel(
                f"Diferença: <b>{eur(variance, self.app.theme.privacy)}</b> face à meta."
            )
            variance_label.setObjectName("Subtle")
            self.fifty.v.addWidget(variance_label)
        classify = QPushButton("⚙ Classificar categorias")
        classify.setObjectName("Secondary")
        classify.clicked.connect(self.edit_budget_buckets)
        self.fifty.v.addWidget(classify, alignment=Qt.AlignRight)

    def edit_budget_buckets(self) -> None:
        dialog = BudgetBucketDialog(self.repo, self)
        if dialog.exec() != QDialog.Accepted:
            return
        for category, bucket in dialog.values():
            self.repo.set_category_budget_bucket(category, bucket)
        self.app.notify_data_changed("Grupos 50/30/20 atualizados.")
        self.refresh()

    def refresh_payment(self, s: str, e: str) -> None:
        self.wipe_card(self.payment)
        rows = self.analytics_service.payment_method_totals(s, e)
        if not rows:
            self.payment.v.addWidget(QLabel("Sem formas de pagamento neste período."))
            return
        maxv = max(v for _, v, _ in rows) or 1
        for method, total, color in rows:
            self.payment.v.addWidget(
                ProgressPill(
                    method,
                    eur(total, self.app.theme.privacy),
                    total / maxv,
                    color,
                    lambda: self.app.theme,
                )
            )

    def refresh_composition(self, year: int) -> None:
        yearly_cats = self.repo.top_categories_payment_period(
            f"{year}-01-01", f"{year}-12-31"
        )
        primary = yearly_cats[:6]
        remainder_names = {name for name, _value, _color in yearly_cats[6:]}
        cats = [(name, safe_hex_colour(color)) for name, _value, color in primary]
        if remainder_names:
            cats.append((f"Outros ({len(remainder_names)})", "#94A3B8"))
        rows: list[dict[str, Any]] = []
        for m in range(1, 13):
            s, e, ym = self.repo.month_period(year, m)
            row = {
                "month": ym,
                "label": PT_MONTHS[m - 1][:3].capitalize(),
            }
            vals = {
                name: val
                for name, val, _ in self.repo.top_categories_payment_period(s, e)
            }
            for name, _color in cats[: len(primary)]:
                row[name] = vals.get(name, 0.0)
            if remainder_names:
                row[cats[-1][0]] = sum(
                    value for name, value in vals.items() if name in remainder_names
                )
            rows.append(row)
        self.composition.set_data(rows, cats)

    def save_target(self) -> None:
        _s, _e, ym, _year, _month = self.selected_period()
        self.repo.set_month_target(ym, self.expense_target.value())
        QMessageBox.information(self, APP_TITLE, f"Meta de gastos guardada para {ym}.")
        self.refresh()

    def export_html(self) -> None:
        s, e, ym, _year, _month = self.selected_period()
        totals = self.repo.totals_for_payment_period(s, e)
        cats = self.repo.top_categories_payment_period(s, e)
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Exportar panorama",
            str(Path.home() / f"panorama_{ym}.html"),
            "HTML (*.html)",
        )
        if not path:
            return
        privacy = self.app.theme.privacy
        rows = "".join(
            f"<tr><td>{html_text(name)}</td><td>{html_text(eur(val, privacy))}</td></tr>"
            for name, val, _ in cats
        )
        safe_month = html_text(ym)
        html = f"""<!doctype html><meta charset='utf-8'><title>Panorama {safe_month}</title><style>body{{font-family:Segoe UI,Arial;padding:32px;color:#172033}}.card{{border:1px solid #e5e7eb;border-radius:18px;padding:18px;margin:14px 0}}td,th{{padding:9px 14px;border-bottom:1px solid #eef2f7}}</style><h1>Panorama financeiro — {safe_month}</h1><div class='card'><b>Receitas:</b> {html_text(eur(totals["income"], privacy))}<br><b>Despesas:</b> {html_text(eur(totals["expenses"], privacy))}<br><b>Saldo:</b> {html_text(eur(totals["savings"], privacy))}<br><b>Taxa de poupança:</b> {html_text(percent(totals["savings"] / totals["income"] * 100 if totals["income"] else (-100.0 if totals["expenses"] > 0 else 0.0)))}</div><h2>Categorias</h2><table><tr><th>Categoria</th><th>Total</th></tr>{rows}</table>"""
        try:
            Path(path).write_text(html, encoding="utf-8")
            QMessageBox.information(self, APP_TITLE, f"Relatório exportado para:\n{path}")
        except Exception as exc:
            log_exception(f"Falha ao exportar panorama para {path}", exc)
            QMessageBox.critical(self, APP_TITLE, f"Erro ao exportar panorama:\n{exc}")
