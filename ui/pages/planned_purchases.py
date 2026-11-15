"""Planned-purchase workspace extracted from the general planning module."""

from __future__ import annotations

import sys
from datetime import date
from typing import TYPE_CHECKING, Optional

from utils.qt_runtime import configure_qt_runtime

configure_qt_runtime()

try:
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QColor
    from PySide6.QtWidgets import (
        QAbstractItemView,
        QDialog,
        QGridLayout,
        QHBoxLayout,
        QHeaderView,
        QLabel,
        QMessageBox,
        QPushButton,
        QTableWidget,
    )
except Exception as exc:  # pragma: no cover - PySide6 missing on the user's PC.
    print("PySide6 não está instalado. Instala com: pip install PySide6")
    print(exc)
    sys.exit(1)

from ui.components import GlassCard, MetricCard
from ui.core import BasePage, clear_layout_recursive
from ui.core.smart_table import SmartTableWidgetItem as QTableWidgetItem
from ui.dialogs import PlannedPurchaseDialog
from utils.config import parse_date
from utils.helpers import (
    APP_TITLE,
    BLUE,
    GREEN,
    ORANGE,
    PURPLE,
    RED,
    eur,
    percent,
    safe_float,
)

if TYPE_CHECKING:
    from main import LiquidFinanceApp


class PlanningPage(BasePage):
    def __init__(self, app: "LiquidFinanceApp"):
        super().__init__(app, "Planeador", "")
        self.layout().setSpacing(2)
        self.body.setSpacing(5)
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(6)
        self.add_btn = QPushButton("+ Nova compra")
        self.add_btn.setObjectName("CardAction")
        self.edit_btn = QPushButton("Editar")
        self.delete_btn = QPushButton("Eliminar")
        self.delete_btn.setObjectName("Danger")
        top.addStretch()
        top.addWidget(self.delete_btn)
        top.addWidget(self.edit_btn)
        top.addWidget(self.add_btn)
        self.summary = QGridLayout()
        self.summary.setContentsMargins(0, 0, 0, 0)
        self.summary.setSpacing(8)
        self.table = QTableWidget(0, 7)
        self.table.setProperty("columnPersistenceId", "planned_purchases.list")
        self.table.setHorizontalHeaderLabels(
            ["ID", "Compra", "Alvo", "Guardado", "Progresso", "Data", "Prioridade"]
        )
        self.table.setColumnHidden(0, True)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.verticalHeader().setVisible(False)
        self.scenarios = GlassCard("Cenários rápidos", strong=True)
        card = GlassCard("Compras planeadas", strong=True)
        card.v.addWidget(self.table)
        self.body.addLayout(top)
        self.body.addLayout(self.summary)
        self.body.addWidget(card)
        self.body.addWidget(self.scenarios)
        self.add_btn.clicked.connect(self.add_purchase)
        self.edit_btn.clicked.connect(self.edit_selected)
        self.delete_btn.clicked.connect(self.delete_selected)
        self.table.itemDoubleClicked.connect(lambda *_: self.edit_selected())
        self.refresh()

    @staticmethod
    def clear(layout) -> None:
        clear_layout_recursive(layout)

    def _monthly_savings(self) -> float:
        today = date.today()
        totals = self.analytics_service.totals_for_period(
            today.replace(day=1).isoformat(),
            today.isoformat(),
        )
        return max(0, safe_float(totals.get("savings")))

    def refresh(self) -> None:
        rows = self.repo.planned_purchases()
        missing = sum(
            max(0, safe_float(row["target_amount"]) - safe_float(row["saved_amount"]))
            for row in rows
        )
        today = date.today()
        # BUG-PLAN-01: expired desired dates must not freeze "Próxima data" —
        # only future purchases are eligible for the highlight.
        next_due = sorted(
            (
                (parse_date(row["desired_date"]), row)
                for row in rows
                if str(row["desired_date"] or "").strip()
                and parse_date(row["desired_date"]) >= today
            ),
            key=lambda item: item[0],
        )
        monthly_savings = self._monthly_savings()

        self.clear(self.summary)
        summary_metrics = [
            ("Compras planeadas", str(len(rows)), "ativas", BLUE, "□"),
            (
                "Valor em falta",
                eur(missing, self.app.theme.privacy),
                "para todas",
                ORANGE,
                "Σ",
            ),
            (
                "Próxima data",
                next_due[0][0].strftime("%d/%m/%Y") if next_due else "—",
                next_due[0][1]["name"] if next_due else "sem data",
                PURPLE,
                "→",
            ),
            (
                "Poupança média",
                eur(monthly_savings, self.app.theme.privacy),
                "mês atual",
                GREEN,
                "↓",
            ),
        ]
        for index, metric in enumerate(summary_metrics):
            self.summary.addWidget(MetricCard(*metric), 0, index)

        self.table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            target = safe_float(row["target_amount"])
            saved = safe_float(row["saved_amount"])
            progress = saved / target * 100 if target else 0
            values = [
                row["id"],
                row["name"],
                eur(target, self.app.theme.privacy),
                eur(saved, self.app.theme.privacy),
                percent(progress),
                row["desired_date"],
                row["priority"],
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if column in {2, 3, 4}:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                if column == 6:
                    item.setForeground(
                        QColor(
                            RED
                            if value == "Alta"
                            else ORANGE
                            if value == "Média"
                            else GREEN
                        )
                    )
                if column == 5:
                    # BUG-PLAN-01: ISO dates sort lexicographically, i.e.
                    # chronologically, instead of by locale-dependent text.
                    item.setData(Qt.InitialSortOrderRole, str(value))
                if column == 6:
                    # BUG-PLAN-01: alphabetical order puts "Baixa" before
                    # "Média"; expose a numeric weight for correct sorting.
                    item.setData(
                        Qt.InitialSortOrderRole,
                        {"Alta": 0, "Média": 1, "Baixa": 2}.get(str(value), 3),
                    )
                self.table.setItem(row_index, column, item)

        self.clear(self.scenarios.v)
        self.scenarios.v.addWidget(QLabel("Cenários rápidos"))
        for monthly_change in (25, 50, 100, 150):
            annual = (monthly_savings + monthly_change) * 12
            label = QLabel(
                f"Cortar/aumentar <b>{eur(monthly_change, self.app.theme.privacy)}</b> "
                f"por mês → potencial anual: "
                f"<b>{eur(annual, self.app.theme.privacy)}</b>"
            )
            label.setWordWrap(True)
            self.scenarios.v.addWidget(label)

    def add_purchase(self) -> None:
        dialog = PlannedPurchaseDialog(self.app)
        if dialog.exec() != QDialog.Accepted:
            return
        data = dialog.get_data()
        if not data["name"]:
            QMessageBox.warning(self, APP_TITLE, "Indica o nome da compra.")
            return
        purchase_id = self.repo.add_planned_purchase(
            data["name"],
            data["target"],
            data["saved"],
            data["date"],
            data["priority"],
            data["notes"],
        )
        self.app.notify_data_changed()
        self._select_id(purchase_id)

    def selected_id(self) -> Optional[int]:
        row = self.table.currentRow()
        item = self.table.item(row, 0) if row >= 0 else None
        try:
            return int(item.text()) if item else None
        except (AttributeError, TypeError, ValueError):
            return None

    def _select_id(self, purchase_id: int) -> None:
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item and item.text() == str(purchase_id):
                self.table.selectRow(row)
                self.table.scrollToItem(item, QAbstractItemView.PositionAtCenter)
                break

    def edit_selected(self) -> None:
        purchase_id = self.selected_id()
        if not purchase_id:
            QMessageBox.information(self, APP_TITLE, "Seleciona uma compra planeada.")
            return
        row = self.repo.planned_purchase_by_id(purchase_id)
        if not row:
            QMessageBox.warning(self, APP_TITLE, "Não encontrei a compra selecionada.")
            self.refresh()
            return
        dialog = PlannedPurchaseDialog(self.app, row)
        if dialog.exec() != QDialog.Accepted:
            return
        data = dialog.get_data()
        if not data["name"]:
            QMessageBox.warning(self, APP_TITLE, "Indica o nome da compra.")
            return
        self.repo.update_planned_purchase(
            purchase_id,
            data["name"],
            data["target"],
            data["saved"],
            data["date"],
            data["priority"],
            data["notes"],
        )
        self.app.notify_data_changed("Compra planeada atualizada.")
        self._select_id(purchase_id)

    def delete_selected(self) -> None:
        purchase_id = self.selected_id()
        if not purchase_id:
            QMessageBox.information(self, APP_TITLE, "Seleciona uma compra planeada.")
            return
        current_row = self.table.currentRow()
        name_item = self.table.item(current_row, 1) if current_row >= 0 else None
        name = name_item.text() if name_item else "esta compra"
        if (
            QMessageBox.question(self, APP_TITLE, f"Eliminar {name}?")
            != QMessageBox.Yes
        ):
            return
        self.repo.delete_planned_purchase(purchase_id)
        self.app.notify_data_changed("Compra planeada eliminada.")
