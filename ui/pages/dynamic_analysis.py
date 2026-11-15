from __future__ import annotations

import csv
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDateEdit,
    QFileDialog,
    QGridLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
)

from ui.components import BarChart, GlassCard, MetricCard
from ui.chart_detail_support import (
    movement_detail_payload,
    open_movements_view,
    period_for_chart_item,
)
from ui.core import BasePage, clear_layout_recursive, prepare_date_edit
from ui.core.smart_table import SmartTableWidgetItem as QTableWidgetItem
from utils.output_security import privacy_export_row
from utils.app_logging import log_exception
from utils.helpers import (
    APP_TITLE,
    BLUE,
    GREEN,
    ORANGE,
    PINK,
    PURPLE,
    TEAL,
    YELLOW,
    eur,
    percent,
    safe_float,
    to_qdate,
)

if TYPE_CHECKING:
    from main import LiquidFinanceApp


class DynamicAnalysisPage(BasePage):
    def __init__(self, app: "LiquidFinanceApp"):
        super().__init__(
            app,
            "Análise dinâmica",
            "Filtros tipo Excel, agrupamentos e drill-down por categoria, subcategoria, comerciante ou forma de pagamento",
        )
        filters = GlassCard(strong=True)
        self.filters_card = filters
        grid = QGridLayout()
        grid.setSpacing(10)
        self.basis = QComboBox()
        self.basis.addItems(["Pagamento", "Compra"])
        self.group = QComboBox()
        self.group.addItems(
            [
                "Categoria",
                "Subcategoria",
                "Comerciante",
                "Forma de pagamento",
                "Conta",
                "Mês",
            ]
        )
        self.kind = QComboBox()
        self.kind.addItems(["Despesas", "Receitas", "Todos", "Saldo"])
        self.start = QDateEdit()
        prepare_date_edit(self.start, self.app.theme)
        self.start.setDate(to_qdate(date.today().replace(month=1, day=1)))
        self.end = QDateEdit()
        prepare_date_edit(self.end, self.app.theme)
        self.end.setDate(QDate.currentDate())
        self.refresh_btn = QPushButton("Atualizar análise")
        self.refresh_btn.setObjectName("Primary")
        self.export_btn = QPushButton("Exportar CSV")
        grid.addWidget(QLabel("Base"), 0, 0)
        grid.addWidget(self.basis, 0, 1)
        grid.addWidget(QLabel("Agrupar por"), 0, 2)
        grid.addWidget(self.group, 0, 3)
        grid.addWidget(QLabel("Tipo"), 0, 4)
        grid.addWidget(self.kind, 0, 5)
        grid.addWidget(QLabel("Início"), 1, 0)
        grid.addWidget(self.start, 1, 1)
        grid.addWidget(QLabel("Fim"), 1, 2)
        grid.addWidget(self.end, 1, 3)
        grid.addWidget(self.refresh_btn, 1, 4)
        grid.addWidget(self.export_btn, 1, 5)
        filters.v.addLayout(grid)
        self.summary = QGridLayout()
        self.summary.setSpacing(14)
        self.chart = BarChart(lambda: self.app.theme)
        self.chart.setAccessibleName("Resultados da análise dinâmica")
        self.chart.configure_details(
            self._group_detail_payload,
            action_label="Abrir movimentos relacionados",
            action_handler=self._open_group_movements,
        )
        self.results_card = GlassCard("Top resultados", strong=True)
        self.results_card.set_compact_header()
        self.results_card.v.addWidget(self.chart)
        self.table = QTableWidget(0, 4)
        self.table.setProperty("columnPersistenceId", "dynamic_analysis.results")
        self.table.setHorizontalHeaderLabels(["Grupo", "Total", "Movimentos", "Peso"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        table_card = GlassCard("Tabela dinâmica", strong=True)
        table_card.v.addWidget(self.table)
        body_grid = QGridLayout()
        body_grid.setSpacing(16)
        body_grid.addWidget(self.results_card, 0, 0)
        body_grid.addWidget(table_card, 0, 1)
        body_grid.setColumnStretch(0, 1)
        body_grid.setColumnStretch(1, 1)
        self.register_filter_panel(filters)
        self.body.addWidget(filters)
        self.body.addLayout(self.summary)
        self.body.addLayout(body_grid)
        self.refresh_btn.clicked.connect(self.refresh)
        self.export_btn.clicked.connect(self.export_csv)
        for w in [self.basis, self.group, self.kind, self.start, self.end]:
            text_changed = getattr(w, "currentTextChanged", None)
            if text_changed is not None:
                text_changed.connect(lambda *_: self.refresh())
            date_changed = getattr(w, "dateChanged", None)
            if date_changed is not None:
                date_changed.connect(lambda *_: self.refresh())
        self.refresh()

    def rows(self) -> list[dict[str, Any]]:
        return self.repo.dynamic_grouped(
            self.start.date().toPython().isoformat(),
            self.end.date().toPython().isoformat(),
            self.group.currentText(),
            self.basis.currentText(),
            self.kind.currentText(),
        )

    def _group_detail_scope(
        self,
        item: dict[str, Any],
    ) -> tuple[str, str, dict[str, Any]]:
        start = self.start.date().toPython().isoformat()
        end = self.end.date().toPython().isoformat()
        label = str(item.get("label") or "")
        group = self.group.currentText()
        aggregate = label.startswith("Outros (")
        options: dict[str, Any] = {
            "kind": self.kind.currentText(),
            "basis": self.basis.currentText(),
        }
        if group == "Mês" and not aggregate:
            start, end = period_for_chart_item(
                {"month": label, "resolution": "monthly"},
                fallback_start=start,
                fallback_end=end,
            )
        elif group == "Categoria" and not aggregate:
            options["category"] = label
        elif group == "Subcategoria" and not aggregate and label != "Sem subcategoria":
            options["subcategory"] = label
        elif group == "Comerciante" and not aggregate and label != "Não definido":
            options["search_text"] = label
        elif (
            group == "Forma de pagamento" and not aggregate and label != "Não definido"
        ):
            options["payment_method"] = label
        elif group == "Conta" and not aggregate:
            options["account"] = label
        return start, end, options

    def _group_detail_payload(self, item: dict[str, Any]) -> dict[str, Any]:
        start, end, options = self._group_detail_scope(item)
        label = str(item.get("label") or "Grupo")
        return movement_detail_payload(
            self.repo,
            self.app.theme,
            title="Detalhe da análise dinâmica",
            item=item,
            start=start,
            end=end,
            subtitle=f"{self.group.currentText()} · {label}",
            explanation=(
                "A lista usa os mesmos filtros, datas e base temporal da "
                "análise, incluindo divisões por categoria quando aplicável."
            ),
            **options,
        )

    def _open_group_movements(self, item: dict[str, Any]) -> None:
        start, end, options = self._group_detail_scope(item)
        kind = str(options.get("kind") or "")
        open_movements_view(
            self.app,
            start=start,
            end=end,
            kind=kind,
            category=str(options.get("category") or ""),
            search_text=str(options.get("search_text") or ""),
        )

    def _reset_page_filters(self) -> None:
        controls = [self.basis, self.group, self.kind, self.start, self.end]
        for control in controls:
            control.blockSignals(True)
        self.basis.setCurrentText("Pagamento")
        self.group.setCurrentText("Categoria")
        self.kind.setCurrentText("Despesas")
        self.start.setDate(to_qdate(date.today().replace(month=1, day=1)))
        self.end.setDate(QDate.currentDate())
        for control in controls:
            control.blockSignals(False)
        self.refresh()

    def clear_summary(self) -> None:
        clear_layout_recursive(self.summary)

    def refresh(self) -> None:
        rows = self.rows()
        total = sum(abs(safe_float(r["total"])) for r in rows)
        movements = sum(int(r["movements"]) for r in rows)
        self.clear_summary()
        metrics = [
            (
                "Total",
                eur(total, self.app.theme.privacy),
                self.kind.currentText(),
                BLUE,
                "Σ",
            ),
            ("Movimentos", str(movements), "no período", GREEN, "#"),
            ("Grupos", str(len(rows)), self.group.currentText(), PURPLE, "▥"),
        ]
        for i, m in enumerate(metrics):
            self.summary.addWidget(MetricCard(*m), 0, i)
        colors = [GREEN, BLUE, PURPLE, ORANGE, PINK, TEAL, YELLOW]
        self.chart.set_items(
            [
                (r["label"], abs(safe_float(r["total"])), colors[i % len(colors)])
                for i, r in enumerate(rows[:10])
            ]
        )
        self.table.clearContents()
        self.table.setRowCount(len(rows))
        for row, r in enumerate(rows):
            weight = abs(safe_float(r["total"])) / total * 100 if total else 0
            vals = [
                r["label"],
                eur(r["total"], self.app.theme.privacy),
                str(r["movements"]),
                percent(weight),
            ]
            for col, val in enumerate(vals):
                item = QTableWidgetItem(str(val))
                if col in (1, 2, 3):
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self.table.setItem(row, col, item)

    def export_csv(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Exportar análise dinâmica",
            str(Path.home() / "analise_dinamica.csv"),
            "CSV (*.csv)",
        )
        if not path:
            return
        try:
            rows = self.rows()
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f, delimiter=";")
                writer.writerow(["grupo", "total", "movimentos"])
                group_text = self.group.currentText()
                group_field = (
                    "merchant"
                    if group_text == "Comerciante"
                    else "account"
                    if group_text in ("Conta", "Forma de pagamento")
                    else "category"
                    if group_text in ("Categoria", "Subcategoria")
                    else "group"
                )
                for r in rows:
                    writer.writerow(
                        privacy_export_row(
                            [r["label"], r["total"], r["movements"]],
                            [group_field, "total", "count"],
                            self.app.theme.privacy,
                            mask_categories=self.app.theme.privacy,
                        )
                    )
            QMessageBox.information(self, APP_TITLE, f"Exportado para:\n{path}")
        except Exception as exc:
            log_exception(f"Falha ao exportar análise dinâmica para {path}", exc)
            QMessageBox.critical(self, APP_TITLE, f"Erro ao exportar CSV:\n{exc}")
