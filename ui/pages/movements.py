#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Finanças Pessoais – Liquid Glass
Nova interface PySide6 para a app local de finanças pessoais.
Mantém a base de dados e a app clássica intactas; esta é apenas uma camada visual moderna.
"""

from __future__ import annotations

import csv
import json
import sys
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Optional

from utils.qt_runtime import configure_qt_runtime, is_qt_valid
from utils.output_security import privacy_export_row

configure_qt_runtime()

try:
    from PySide6.QtCore import (
        QDate,
        QModelIndex,
        QPoint,
        QPointF,
        Qt,
        QTimer,
    )
    from PySide6.QtGui import QColor, QCursor
    from PySide6.QtWidgets import (
        QAbstractItemView,
        QApplication,
        QCheckBox,
        QComboBox,
        QDateEdit,
        QDialog,
        QDialogButtonBox,
        QFileDialog,
        QFormLayout,
        QGridLayout,
        QHBoxLayout,
        QHeaderView,
        QLabel,
        QLineEdit,
        QMenu,
        QMessageBox,
        QPushButton,
        QSizePolicy,
        QTableView,
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

from ui.components import GlassCard, MetricCard
from ui.components.movement_table import MovementHoverCard, MovementsTableView
from ui.core import (
    BasePage,
    FlowLayout,
    PortugueseMoneyDelegate,
    apply_menu_theme,
    prepare_date_edit,
)
from ui.core.smart_table import SmartTableWidgetItem as QTableWidgetItem
from ui.dialogs import TransactionDialog
from ui.models import MovementCategoryDelegate, MovementsTableModel
from services.export_documents import (
    AsyncPdfExporter,
    export_movements_to_pdf_file,
)
from utils.app_logging import log_exception
from utils.config import parse_date
from utils.helpers import (
    APP_TITLE,
    BLUE,
    GREEN,
    ORANGE,
    RED,
    eur,
    percent,
    row_get,
    safe_float,
    start_end_for_period,
    to_qdate,
)

if TYPE_CHECKING:
    from main import LiquidFinanceApp


class MovementsPage(BasePage):
    _refresh_completes_asynchronously = True

    def __init__(self, app: "LiquidFinanceApp"):
        super().__init__(
            app, "Movimentos", "Pesquisa, filtros avançados, anexos e exportação"
        )
        filters = GlassCard(strong=True)
        self.filters_card = filters
        grid = QGridLayout()
        grid.setSpacing(10)
        filters.v.addLayout(grid)
        self.search = QLineEdit()
        self.search.setPlaceholderText(
            "Pesquisa literal: descrição, categoria, conta, etiqueta..."
        )
        self.search.setToolTip(
            "Pesquisa imediatamente o texto escrito. Os sinais % e _ são tratados como texto normal."
        )
        self.period = QComboBox()
        self.period.addItems(
            [
                "Mês atual",
                "Hoje",
                "Ontem",
                "Semana atual",
                "Últimos 7 dias",
                "Últimos 30 dias",
                "Mês anterior",
                "Ano atual",
                "Personalizado",
                "Tudo",
            ]
        )
        self.start = QDateEdit()
        prepare_date_edit(self.start, self.app.theme)
        self.start.setDate(to_qdate(date.today().replace(day=1)))
        self.end = QDateEdit()
        prepare_date_edit(self.end, self.app.theme)
        self.end.setDate(QDate.currentDate())
        self.category = QComboBox()
        self.category.addItem("Todas")
        self.category.addItems(self.repo.categories())
        self.kind = QComboBox()
        self.kind.addItems(["Todos", "Receitas", "Despesas"])
        self.attach_filter = QComboBox()
        self.attach_filter.addItems(["Todos", "Com anexos", "Sem anexos"])
        self.view_mode = QComboBox()
        self.view_mode.addItems(["Compacta", "Normal", "Detalhada"])
        saved_view = self.repo.setting("movements_view_mode", "Normal")
        idx_view = self.view_mode.findText(saved_view)
        self.view_mode.setCurrentIndex(idx_view if idx_view >= 0 else 1)
        self.auto_refresh = QCheckBox("Atualização automática")
        self.auto_refresh.setChecked(
            self.repo.setting("movements_auto_refresh", "1") == "1"
        )
        self.saved_filter = QComboBox()
        self.reload_saved_filters()
        self.apply_btn = QPushButton("Aplicar filtros")
        self.apply_btn.setObjectName("Primary")
        self.export_btn = QPushButton("Exportar CSV")
        self.export_pdf_btn = QPushButton("Exportar PDF")
        self.new_btn = QPushButton("+ Novo movimento")
        self.new_btn.setObjectName("Primary")
        self.new_transfer_btn = QPushButton("Nova transferência")
        self.edit_btn = QPushButton("Editar")
        self.delete_btn = QPushButton("Eliminar")
        self.delete_btn.setObjectName("Danger")
        self.attach_btn = QPushButton("Anexar fatura")
        self.duplicate_btn = QPushButton("Duplicar")
        self.bulk_btn = QPushButton("Ações em lote")
        self.rules_btn = QPushButton("Aplicar regras")
        self.duplicates_btn = QPushButton("Duplicados")
        self.reimbursements_btn = QPushButton("Reembolsos")
        self.save_filter_btn = QPushButton("Guardar filtro")
        self.load_filter_btn = QPushButton("Carregar filtro")
        self.delete_filter_btn = QPushButton("Eliminar filtro")
        grid.addWidget(QLabel("Pesquisa"), 0, 0)
        grid.addWidget(self.search, 0, 1, 1, 3)
        grid.addWidget(QLabel("Período"), 1, 0)
        grid.addWidget(self.period, 1, 1)
        grid.addWidget(QLabel("Início"), 1, 2)
        grid.addWidget(self.start, 1, 3)
        grid.addWidget(QLabel("Fim"), 1, 4)
        grid.addWidget(self.end, 1, 5)
        grid.addWidget(QLabel("Categoria"), 2, 0)
        grid.addWidget(self.category, 2, 1)
        grid.addWidget(QLabel("Tipo"), 2, 2)
        grid.addWidget(self.kind, 2, 3)
        grid.addWidget(QLabel("Anexos"), 2, 4)
        grid.addWidget(self.attach_filter, 2, 5)
        grid.addWidget(QLabel("Vista"), 3, 0)
        grid.addWidget(self.view_mode, 3, 1)
        grid.addWidget(QLabel("Refresh"), 3, 2)
        grid.addWidget(self.auto_refresh, 3, 3, 1, 2)
        grid.addWidget(QLabel("Filtro guardado"), 4, 0)
        grid.addWidget(self.saved_filter, 4, 1, 1, 3)
        btns = FlowLayout(hspacing=8, vspacing=8)
        for button in [
            self.apply_btn,
            self.new_btn,
            self.new_transfer_btn,
            self.edit_btn,
            self.duplicate_btn,
            self.delete_btn,
            self.attach_btn,
            self.bulk_btn,
            self.rules_btn,
            self.duplicates_btn,
            self.reimbursements_btn,
            self.export_btn,
            self.export_pdf_btn,
        ]:
            btns.addWidget(button)
        for button in [
            self.save_filter_btn,
            self.load_filter_btn,
            self.delete_filter_btn,
        ]:
            btns.addWidget(button)
        filters.v.addLayout(btns)
        self.summary = QGridLayout()
        self.summary.setSpacing(14)
        self.table = self._create_movements_table()
        self._movement_hover_card = MovementHoverCard(self.table.viewport())
        self._hovered_movement_id: Optional[int] = None
        self.table.setMouseTracking(True)
        self.table.viewport().setMouseTracking(True)
        self.table.movement_hovered.connect(self._movement_hover_index)
        self.table.movement_hover_left.connect(self._hide_movement_hover)
        self.table.pressed.connect(lambda *_: self._hide_movement_hover())
        self.table.horizontalScrollBar().valueChanged.connect(
            lambda *_: self._hide_movement_hover()
        )
        self.table.verticalScrollBar().valueChanged.connect(
            lambda *_: self._hide_movement_hover()
        )
        self.table.verticalScrollBar().valueChanged.connect(self._maybe_load_more)
        table_card = GlassCard("Lista de movimentos", strong=True)
        self.table_card = table_card
        # The list is a primary working area.  The 20% increase keeps more rows
        # visible on 1080p screens without preventing vertical page scrolling.
        # BUG-UI-06: hard 720px minimum forces double scrolling on 1366x768.
        # Relax to a smaller floor that grows with available space instead.
        table_card.setMinimumHeight(480)
        table_card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        table_area = QHBoxLayout()
        table_area.setSpacing(14)
        table_area.addWidget(self.table, 3)
        self.detail_panel = QWidget()
        self.detail_panel.setMinimumWidth(280)
        self.detail_panel.setMaximumWidth(390)
        detail_layout = QVBoxLayout(self.detail_panel)
        detail_layout.setContentsMargins(8, 4, 4, 4)
        detail_layout.setSpacing(8)
        title = QLabel("Detalhe")
        title.setObjectName("SectionTitle")
        self.detail_text = QLabel(
            "Seleciona um movimento para ver detalhes, anexos, notas e ações rápidas."
        )
        self.detail_text.setWordWrap(True)
        self.detail_text.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.detail_actions = FlowLayout(hspacing=8, vspacing=8)
        self.detail_edit_btn = QPushButton("Editar")
        self.detail_attach_btn = QPushButton("Anexar")
        self.detail_split_btn = QPushButton("Dividir")
        self.detail_duplicate_btn = QPushButton("Duplicar")
        for button in [
            self.detail_edit_btn,
            self.detail_attach_btn,
            self.detail_split_btn,
            self.detail_duplicate_btn,
        ]:
            self.detail_actions.addWidget(button)
        detail_layout.addWidget(title)
        detail_layout.addWidget(self.detail_text, 1)
        detail_layout.addLayout(self.detail_actions)
        table_area.addWidget(self.detail_panel, 1)
        table_card.v.addLayout(table_area, 1)
        self.table_status = QLabel("Sem dados carregados.")
        self.table_status.setObjectName("Subtle")
        self.table_status.setWordWrap(True)
        self.load_more_btn = QPushButton("Carregar mais")
        self.load_more_btn.setObjectName("Secondary")
        self.load_more_btn.setVisible(False)
        table_status_row = QHBoxLayout()
        table_status_row.setContentsMargins(0, 0, 0, 0)
        table_status_row.setSpacing(12)
        table_status_row.addWidget(self.table_status, 0, Qt.AlignVCenter)
        table_status_row.addWidget(self.load_more_btn, 0, Qt.AlignVCenter)
        table_status_row.addStretch(1)
        table_card.v.addLayout(table_status_row)

        self.register_filter_panel(filters)
        self.body.addWidget(filters)
        self.body.addLayout(self.summary)
        self.body.addWidget(table_card, 1)
        self.apply_btn.clicked.connect(self.refresh)
        self.period.currentTextChanged.connect(self.on_period_changed)
        self.search.returnPressed.connect(self.refresh)
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(180)
        self._search_timer.timeout.connect(self.refresh)
        self.search.textChanged.connect(lambda *_: self._search_timer.start())
        self.view_mode.currentTextChanged.connect(self.change_view_mode)
        self.auto_refresh.toggled.connect(self.change_auto_refresh)
        self.export_btn.clicked.connect(self.export_csv)
        self.export_pdf_btn.clicked.connect(self.export_pdf)
        self.new_btn.clicked.connect(self.add_transaction)
        self.new_transfer_btn.clicked.connect(self.add_transfer)
        self.edit_btn.clicked.connect(self.edit_selected)
        self.delete_btn.clicked.connect(self.delete_selected)
        self.attach_btn.clicked.connect(self.attach_to_selected)
        self.duplicate_btn.clicked.connect(self.duplicate_selected)
        self.bulk_btn.clicked.connect(self.open_bulk_menu)
        self.rules_btn.clicked.connect(self.apply_rules_to_current_scope)
        self.duplicates_btn.clicked.connect(self.review_duplicates)
        self.reimbursements_btn.clicked.connect(self.open_reimbursements)
        self.save_filter_btn.clicked.connect(self.save_current_filter)
        self.load_filter_btn.clicked.connect(self.load_selected_filter)
        self.delete_filter_btn.clicked.connect(self.delete_selected_filter)
        self.detail_edit_btn.clicked.connect(self.edit_selected)
        self.detail_attach_btn.clicked.connect(self.attach_to_selected)
        self.detail_split_btn.clicked.connect(self.open_selected_in_splitter)
        self.detail_duplicate_btn.clicked.connect(self.duplicate_selected)
        self.load_more_btn.clicked.connect(self.load_more_rows)
        self._page_size = 250
        self._loaded_limit = self._page_size
        self._total_filtered = 0
        self._current_summary: dict[str, Any] = {}
        self._load_generation = 0
        self._pending_load = False
        self._pending_reset_limit = False
        self._pending_selected_tx_id: Optional[int] = None
        self._pending_force_replace = False
        self._auto_load_scheduled = False
        self.apply_view_mode(self.view_mode.currentText())
        self.refresh()
        self._live_refresh_timer = QTimer(self)
        self._live_refresh_timer.setInterval(5000)
        self._live_refresh_timer.timeout.connect(self._live_refresh_if_visible)
        self._live_refresh_timer.start()

    def showEvent(self, event):  # type: ignore[override]
        super().showEvent(event)

    def _movement_hover_index(self, index: QModelIndex, position: QPoint) -> None:
        row = self.movements_model.row_object(index.row()) if index.isValid() else None
        if row is None:
            self._hide_movement_hover()
        else:
            self._show_movement_hover(row, position)

    def _show_movement_hover(self, row: Any, position) -> None:
        card = self._movement_hover_card
        movement_id = int(row_get(row, "id", 0) or 0)
        if movement_id != self._hovered_movement_id:
            card.set_movement(row, self.app.theme.privacy)
            self._hovered_movement_id = movement_id
        viewport = self.table.viewport()
        target_width = min(500, max(300, viewport.width() - 16))
        card.setFixedWidth(target_width)
        card.adjustSize()
        card.resize(target_width, min(280, max(140, card.sizeHint().height())))
        x = min(position.x() + 18, viewport.width() - card.width() - 8)
        x = max(8, x)
        y = position.y() + 18
        if y + card.height() > viewport.height() - 8:
            y = position.y() - card.height() - 12
        y = max(8, min(y, viewport.height() - card.height() - 8))
        card.move(x, y)
        card.raise_()
        card.show()

    def _hide_movement_hover(self) -> None:
        card = getattr(self, "_movement_hover_card", None)
        if card is not None:
            card.hide()
        self._hovered_movement_id = None

    def reload_saved_filters(self) -> None:
        if not hasattr(self, "saved_filter"):
            return
        current = self.saved_filter.currentText()
        self.saved_filter.blockSignals(True)
        self.saved_filter.clear()
        self.saved_filter.addItem("—")
        for row in self.repo.saved_filters():
            self.saved_filter.addItem(str(row["name"]))
        idx = self.saved_filter.findText(current)
        if idx >= 0:
            self.saved_filter.setCurrentIndex(idx)
        self.saved_filter.blockSignals(False)

    def save_current_filter(self) -> None:
        name = self._text_dialog(
            "Guardar filtro",
            "Nome do filtro",
            (
                self.saved_filter.currentText()
                if self.saved_filter.currentText() != "—"
                else ""
            ),
        )
        if not name:
            return
        self.repo.save_filter(name, self.filter_state())
        self.reload_saved_filters()
        self.saved_filter.setCurrentText(name)
        self.table_status.setText(f"Filtro guardado: {name}")

    def load_selected_filter(self) -> None:
        name = self.saved_filter.currentText()
        if not name or name == "—":
            QMessageBox.information(
                self, APP_TITLE, "Escolhe primeiro um filtro guardado."
            )
            return
        rows = [r for r in self.repo.saved_filters() if str(r["name"]) == name]
        if not rows:
            QMessageBox.warning(self, APP_TITLE, "Não encontrei esse filtro guardado.")
            self.reload_saved_filters()
            return
        try:
            state = json.loads(str(rows[0]["filter_json"] or "{}"))
            if isinstance(state, str):
                state = json.loads(state)
        except (TypeError, ValueError, json.JSONDecodeError):
            QMessageBox.warning(self, APP_TITLE, "O filtro guardado está inválido.")
            return
        self.apply_filter_state(state if isinstance(state, dict) else {})
        self.refresh()

    def delete_selected_filter(self) -> None:
        name = self.saved_filter.currentText()
        if not name or name == "—":
            QMessageBox.information(
                self, APP_TITLE, "Escolhe primeiro um filtro guardado."
            )
            return
        if (
            QMessageBox.question(
                self, APP_TITLE, f"Eliminar o filtro guardado '{name}'?"
            )
            != QMessageBox.Yes
        ):
            return
        self.repo.delete_saved_filter(name)
        self.reload_saved_filters()
        self.table_status.setText(f"Filtro eliminado: {name}")

    def change_auto_refresh(self, checked: bool) -> None:
        self.repo.set_setting("movements_auto_refresh", "1" if checked else "0")
        if checked:
            self._live_refresh_timer.start()
            self.refresh()
        else:
            self._live_refresh_timer.stop()
            self.table_status.setText(
                "Atualização automática desligada. Usa Aplicar filtros ou F5 para atualizar."
            )

    def change_view_mode(self, mode: str) -> None:
        self.repo.set_setting("movements_view_mode", mode)
        self.apply_view_mode(mode)

    def apply_view_mode(self, mode: str) -> None:
        """Apply column density without rebuilding the table or losing filters."""
        mode = mode if mode in {"Compacta", "Normal", "Detalhada"} else "Normal"
        hidden_by_mode = {
            "Compacta": {0, 2, 5, 6, 7, 9, 10},
            "Normal": {0, 9},
            "Detalhada": {0},
        }
        hidden = hidden_by_mode[mode]
        for col in range(self.movements_model.columnCount()):
            self.table.setColumnHidden(col, col in hidden)

        row_height = {"Compacta": 30, "Normal": 36, "Detalhada": 46}[mode]
        self.table.verticalHeader().setDefaultSectionSize(row_height)
        self.table.setWordWrap(mode == "Detalhada")
        header = self.table.horizontalHeader()
        # Every column must remain draggable. ResizeToContents/Stretch takes
        # control away from the user and also overwrites persisted widths on
        # every automatic refresh of this table.
        header.setSectionResizeMode(QHeaderView.Interactive)
        header.setStretchLastSection(False)
        (
            self.table.resizeRowsToContents()
            if mode == "Detalhada"
            else self.table.verticalHeader().setDefaultSectionSize(row_height)
        )
        self.table.viewport().update()

    def _rows_signature(self, rows: list[Any]) -> tuple:
        return tuple(
            (
                int(row_get(r, "id", 0) or 0),
                str(row_get(r, "date")),
                str(row_get(r, "payment_date")),
                str(row_get(r, "purchase_date")),
                str(row_get(r, "description")),
                float(row_get(r, "amount", 0) or 0),
                str(row_get(r, "category")),
                str(row_get(r, "subcategory")),
                str(row_get(r, "payment_method")),
                str(row_get(r, "merchant")),
                str(row_get(r, "account")),
                str(row_get(r, "tags")),
                str(row_get(r, "notes")),
                int(row_get(r, "attachment_count", 0) or 0),
            )
            for r in rows
        )

    def _live_refresh_if_visible(self) -> None:
        try:
            if not self.auto_refresh.isChecked():
                return
            if (
                getattr(self.app, "stack", None) is not None
                and self.app.stack.currentWidget() is not self
            ):
                return
            if QApplication.activeModalWidget() is not None:
                return
            self.refresh(reset_limit=False, force_replace=False)
        except Exception as exc:
            log_exception("Movements live refresh failed", exc)

    @staticmethod
    def _summary_signature(summary: dict[str, Any]) -> tuple:
        return (
            int(summary.get("movement_count", 0) or 0),
            float(summary.get("income", 0) or 0),
            float(summary.get("expenses", 0) or 0),
            int(summary.get("excluded_invalid_count", 0) or 0),
            str(summary.get("first_date") or ""),
            str(summary.get("last_date") or ""),
        )

    def _apply_loaded_payload(self, payload: object) -> None:
        if not isinstance(payload, dict):
            return
        if int(payload.get("generation", -1)) != self._load_generation:
            return
        rows = list(payload.get("rows") or [])
        summary = dict(payload.get("summary") or {})
        force_replace = bool(payload.get("force_replace", True))
        if (
            not force_replace
            and getattr(self, "_last_rows_signature", None)
            == self._rows_signature(rows)
            and getattr(self, "_last_summary_signature", None)
            == self._summary_signature(summary)
        ):
            self.mark_data_updated()
            return
        self._replace_model_rows(
            rows,
            payload.get("selected_tx_id"),
            summary,
        )
        self.mark_data_updated()

    def _movement_load_error(self, message: str) -> None:
        self.table_status.setText(
            "Não foi possível atualizar a lista de movimentos. Tenta novamente."
        )
        self.mark_data_refresh_failed()
        log_exception("Movements background load failed", RuntimeError(message))

    def _movement_loader_finished(self, _cancelled: bool) -> None:
        if not self._pending_load:
            return
        reset_limit = self._pending_reset_limit
        selected_tx_id = self._pending_selected_tx_id
        force_replace = self._pending_force_replace
        self._pending_load = False
        self._pending_reset_limit = False
        self._pending_selected_tx_id = None
        self._pending_force_replace = False
        # The previous task has already released its thread when ``finished``
        # is emitted, so the coalesced request can start immediately.  Leaving
        # it queued on a zero-delay timer created a brief false-idle state: a
        # hover card could open and then be hidden by that unseen refresh.
        self.refresh(
            reset_limit=reset_limit,
            selected_tx_id=selected_tx_id,
            force_replace=force_replace,
        )

    def _update_load_more_state(self, loaded: int, total: int) -> None:
        remaining = max(0, int(total) - int(loaded))
        self.load_more_btn.setVisible(remaining > 0)
        if remaining > 0:
            next_count = min(self._page_size, remaining)
            self.load_more_btn.setText(f"Carregar mais {next_count}")
            self.load_more_btn.setToolTip(
                f"Ainda existem {remaining} movimentos para mostrar."
            )

    def _replace_model_rows(
        self,
        rows: list[Any],
        selected_tx_id: Optional[int] = None,
        summary: Optional[dict[str, Any]] = None,
    ) -> None:
        self._hide_movement_hover()
        if selected_tx_id is None:
            selected_tx_id = self.selected_id()
        summary = dict(summary or self._summary_from_rows(rows))
        self._last_rows_signature = self._rows_signature(rows)
        self._last_summary_signature = self._summary_signature(summary)
        self._current_summary = summary
        self.movements_model.set_rows(rows, self.app.theme)
        self._refresh_summary_from_rows(rows, summary)
        self.apply_view_mode(self.view_mode.currentText())
        self._select_transaction_row(selected_tx_id)
        loaded = len(rows)
        total = int(summary.get("movement_count", loaded) or 0)
        self._total_filtered = total
        self._update_load_more_state(loaded, total)
        now = datetime.now().strftime("%H:%M:%S")
        if total:
            scope = (
                f"A mostrar {loaded} de {total} movimentos"
                if loaded < total
                else f"{total} movimento(s)"
            )
            excluded_invalid = int(summary.get("excluded_invalid_count", 0) or 0)
            warning = (
                f" · ⚠️ {excluded_invalid} excluído(s) dos totais por dados inválidos"
                if excluded_invalid
                else ""
            )
            self.table_status.setText(
                f"{scope} · atualizado às {now} · "
                f"vista {self.view_mode.currentText().lower()}{warning}"
            )
        else:
            self.table_status.setText(
                "Sem movimentos para estes filtros. Ajusta período, pesquisa, categoria, tipo ou anexos."
            )
        try:
            self.table.viewport().update()
            self.table.updateGeometry()
        except Exception as _exc:
            log_exception(
                f"{type(self).__name__} failed to repaint movements table", _exc
            )

    def _create_movements_table(self) -> QTableView:
        self.movements_model = MovementsTableModel(self.app.theme, self)
        table = MovementsTableView()
        table.setProperty("columnPersistenceId", "movements.list")
        table.setModel(self.movements_model)
        table.setItemDelegateForColumn(4, MovementCategoryDelegate(table))
        table.setItemDelegateForColumn(8, PortugueseMoneyDelegate(table))
        header = table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Interactive)
        header.setStretchLastSection(False)
        header.setMinimumSectionSize(48)
        for column, width in enumerate(
            [60, 108, 108, 280, 150, 140, 125, 145, 110, 150, 82]
        ):
            header.resizeSection(column, width)
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        table.setEditTriggers(
            QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed
        )
        table.setSortingEnabled(True)
        header.sortIndicatorChanged.connect(self._on_sort_indicator_changed)
        table.setWordWrap(False)
        table.setAlternatingRowColors(True)
        table.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        table.verticalHeader().setVisible(False)
        table.verticalHeader().setDefaultSectionSize(36)
        table.setColumnHidden(0, True)
        table.setMinimumHeight(420)
        table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        table.setContextMenuPolicy(Qt.CustomContextMenu)
        table.customContextMenuRequested.connect(self.open_table_menu)
        table.selectionModel().selectionChanged.connect(
            lambda *_: self.refresh_detail_panel()
        )
        return table

    def _on_sort_indicator_changed(self, column: int, order: Qt.SortOrder) -> None:
        """BUG-MOV-05: Inform user when in-memory sorting operates on a partial page."""
        loaded = len(self.movements_model._rows)
        total = getattr(self, "_total_filtered", loaded)
        if total > loaded:
            self.table_status.setText(
                f"ℹ️ Ordenação aplicada apenas aos {loaded} movimentos visíveis (de {total} no filtro). "
                "Clica em 'Carregar mais' para ordenar um conjunto mais alargado."
            )

    def force_refresh_after_save(
        self,
        selected_tx_id: Optional[int] = None,
        *,
        notify_views: bool = True,
    ) -> bool:
        """Refresh immediately after a save without rereading the full history."""
        if not self.repo.safe_commit(
            f"{type(self).__name__} failed to commit after save"
        ):
            QMessageBox.critical(
                self,
                APP_TITLE,
                "Não foi possível confirmar a gravação. A operação foi revertida e a lista não foi atualizada.",
            )
            return False
        loader = getattr(self, "_loader_task", None)
        if loader is not None and loader.is_running():
            loader.cancel()
        payload = self._query_payload(self._loaded_limit)
        rows = list(payload.get("rows") or [])
        summary = dict(payload.get("summary") or {})
        self._replace_model_rows(rows, selected_tx_id, summary)
        QTimer.singleShot(
            180,
            lambda tx_id=selected_tx_id: self.refresh(
                reset_limit=False,
                selected_tx_id=tx_id,
                force_replace=False,
            ),
        )
        if notify_views:
            self.app.invalidate_after_movement_change(selected_tx_id=selected_tx_id)
        return True

    def _run_journaled_change(
        self,
        ids: list[int],
        mutation: Callable[[], Any],
        *,
        label: str | Callable[[Any], str],
        action_type: str = "bulk_edit",
        journal_when: Optional[Callable[[Any], bool]] = None,
    ) -> tuple[bool, Any]:
        try:
            result = self.app.change_journal_service.run_with_snapshot(
                ids,
                mutation,
                label=label,
                action_type=action_type,
                journal_when=journal_when,
            )
        except (ValueError, RuntimeError) as exc:
            log_exception("Validation error committing movement change", exc)
            QMessageBox.warning(
                self,
                APP_TITLE,
                f"Não foi possível aplicar a alteração aos movimentos:\n\n{exc}",
            )
            return False, None
        except Exception as exc:
            log_exception("Failed to commit movement change with undo history", exc)
            QMessageBox.critical(
                self,
                APP_TITLE,
                "Não foi possível aplicar a alteração. Os movimentos e o histórico de Desfazer ficaram intactos.",
            )
            return False, None
        self.app.refresh_undo_actions()
        return True, result

    def apply_inline_edit(
        self,
        tx_id: int,
        updates: dict[str, Any],
    ) -> Optional[dict[str, Any]]:
        """Validate, journal and propagate an edit made directly in the table."""

        clean = dict(updates or {})
        if "description" in clean and not str(clean["description"] or "").strip():
            QMessageBox.warning(
                self,
                "Alteração inválida",
                "A descrição do movimento não pode ficar vazia.",
            )
            return None
        try:
            self.app.change_journal_service.run_with_snapshot(
                [int(tx_id)],
                lambda: self.repo.update_transaction_fields(
                    int(tx_id),
                    clean,
                    commit=False,
                ),
                label=lambda _result: f"Edição direta · movimento {int(tx_id)}",
                action_type="inline_edit",
            )
            saved = self.repo.transaction_full_by_id(int(tx_id))
            if saved is None:
                raise RuntimeError("O movimento guardado não pôde ser relido.")
        except ValueError as exc:
            QMessageBox.warning(self, "Alteração inválida", str(exc))
            return None
        except Exception as exc:
            log_exception("Failed to journal inline movement edit", exc)
            QMessageBox.critical(
                self,
                APP_TITLE,
                "Não foi possível aplicar a alteração. O movimento e o histórico de Desfazer ficaram intactos.",
            )
            return None
        self.app.refresh_undo_actions()
        self.app.invalidate_after_movement_change(selected_tx_id=int(tx_id))
        return dict(saved)

    def filter_state(self) -> dict[str, Any]:
        return {
            "search": self.search.text(),
            "period": self.period.currentText(),
            "start": self.start.date().toPython().isoformat(),
            "end": self.end.date().toPython().isoformat(),
            "category": self.category.currentText(),
            "kind": self.kind.currentText(),
            "attachments": self.attach_filter.currentText(),
            "view_mode": self.view_mode.currentText(),
            "auto_refresh": self.auto_refresh.isChecked(),
        }

    def _capture_page_filter_state(self) -> dict[str, object]:
        return {"movements": {"type": "payload", "value": self.filter_state()}}

    def _restore_page_filter_state(self, state: dict[str, object]) -> None:
        entry = state.get("movements")
        if isinstance(entry, dict) and isinstance(entry.get("value"), dict):
            self.apply_filter_state(entry["value"])

    def apply_filter_state(self, state: Optional[dict[str, Any]]) -> None:
        if not state:
            return
        self.search.setText(str(state.get("search", "")))
        for combo, key in [
            (self.period, "period"),
            (self.category, "category"),
            (self.kind, "kind"),
            (self.attach_filter, "attachments"),
        ]:
            value = str(state.get(key, ""))
            idx = combo.findText(value)
            if idx >= 0:
                combo.setCurrentIndex(idx)
        view = str(state.get("view_mode", ""))
        idx = self.view_mode.findText(view)
        if idx >= 0:
            self.view_mode.setCurrentIndex(idx)
        if "auto_refresh" in state:
            self.auto_refresh.setChecked(bool(state.get("auto_refresh")))
        try:
            if state.get("start"):
                self.start.setDate(to_qdate(parse_date(str(state["start"]))))
            if state.get("end"):
                self.end.setDate(to_qdate(parse_date(str(state["end"]))))
        except Exception as _exc:
            log_exception(
                f"{type(self).__name__} failed to apply saved filter dates", _exc
            )

    def on_period_changed(self, text: str) -> None:
        if text != "Personalizado":
            start, end = start_end_for_period(text)
            if start:
                self.start.setDate(to_qdate(parse_date(start)))
            if end:
                self.end.setDate(to_qdate(parse_date(end)))
        self.refresh()

    def filter_dates(self) -> tuple[str, str]:
        if self.period.currentText() == "Tudo":
            return "", ""
        if self.period.currentText() == "Personalizado":
            return (
                self.start.date().toPython().isoformat(),
                self.end.date().toPython().isoformat(),
            )
        return start_end_for_period(self.period.currentText())

    def _reset_page_filters(self) -> None:
        """Clear all movement filters and restore the standard monthly view."""
        self._search_timer.stop()
        controls = [
            self.search,
            self.period,
            self.start,
            self.end,
            self.category,
            self.kind,
            self.attach_filter,
            self.view_mode,
        ]
        for control in controls:
            control.blockSignals(True)
        self.search.clear()
        self.period.setCurrentText("Mês atual")
        self.start.setDate(to_qdate(date.today().replace(day=1)))
        self.end.setDate(QDate.currentDate())
        self.category.setCurrentText("Todas")
        self.kind.setCurrentText("Todos")
        self.attach_filter.setCurrentText("Todos")
        self.view_mode.setCurrentText("Normal")
        for control in controls:
            control.blockSignals(False)
        self.apply_view_mode("Normal")
        self.refresh()

    def _filter_query_values(self) -> tuple[str, str, str, str, str, str]:
        s, e = self.filter_dates()
        return (
            s,
            e,
            self.search.text(),
            self.category.currentText(),
            self.kind.currentText(),
            self.attach_filter.currentText(),
        )

    def _query_rows(self, limit: Optional[int]) -> list[Any]:
        return list(self._query_payload(limit).get("rows") or [])

    def _query_payload(
        self,
        limit: Optional[int],
        *,
        context: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        s, e, search, category, kind, attachments = self._filter_query_values()
        operation = self.repo.transactions_filtered_payload_operation(
            s,
            e,
            search,
            category,
            kind,
            attachments=attachments,
            limit=limit,
            context=context,
        )
        return dict(operation())

    def rows(self) -> list[Any]:
        """Return the complete filtered scope for exports and explicit bulk actions."""
        return self._query_rows(None)

    def visible_rows(self) -> list[Any]:
        return self._query_rows(self._loaded_limit)

    def filtered_summary(self) -> dict[str, Any]:
        s, e, search, category, kind, attachments = self._filter_query_values()
        return self.repo.transactions_filtered_summary_fresh(
            s,
            e,
            search,
            category,
            kind,
            attachments=attachments,
        )

    def load_more_rows(self) -> None:
        if self._loaded_limit >= self._total_filtered:
            return
        self._loaded_limit = min(
            self._total_filtered, self._loaded_limit + self._page_size
        )
        self.refresh(reset_limit=False)

    def _maybe_load_more(self, value: int) -> None:
        scrollbar = self.table.verticalScrollBar()
        loader = getattr(self, "_loader_task", None)
        if (
            self._auto_load_scheduled
            or not self.load_more_btn.isVisible()
            or scrollbar.maximum() <= 0
            or int(value) < scrollbar.maximum() - 2
            or (loader is not None and loader.is_running())
        ):
            return
        self._auto_load_scheduled = True

        def load() -> None:
            self._auto_load_scheduled = False
            self.load_more_rows()

        QTimer.singleShot(80, load)

    def clear_layout(self, layout: QGridLayout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    @staticmethod
    def _filtered_day_count(rows: list[Any], start: str = "", end: str = "") -> int:
        if start and end:
            return max(1, (parse_date(end) - parse_date(start)).days + 1)
        financial_dates = [
            parse_date(str(row_get(row, "payment_date") or row_get(row, "date"))[:10])
            for row in rows
            if row_get(row, "payment_date") or row_get(row, "date")
        ]
        if not financial_dates:
            return 1
        return max(1, (max(financial_dates) - min(financial_dates)).days + 1)

    @classmethod
    def _summary_from_rows(cls, rows: list[Any]) -> dict[str, Any]:
        dates = [
            str(row_get(row, "payment_date") or row_get(row, "date") or "")[:10]
            for row in rows
            if row_get(row, "payment_date") or row_get(row, "date")
        ]
        return {
            "movement_count": len(rows),
            "income": sum(
                safe_float(row_get(row, "amount"))
                for row in rows
                if safe_float(row_get(row, "amount")) > 0
            ),
            "expenses": sum(
                abs(safe_float(row_get(row, "amount")))
                for row in rows
                if safe_float(row_get(row, "amount")) < 0
            ),
            "first_date": min(dates) if dates else "",
            "last_date": max(dates) if dates else "",
            "excluded_invalid_count": 0,
            "invalid_amounts": 0,
            "invalid_dates": 0,
        }

    def _refresh_summary_from_rows(
        self, rows: list[Any], summary: Optional[dict[str, Any]] = None
    ) -> None:
        summary = dict(summary or self._summary_from_rows(rows))
        income = safe_float(summary.get("income"))
        expenses = safe_float(summary.get("expenses"))
        total_count = int(summary.get("movement_count", len(rows)) or 0)
        excluded_invalid = int(summary.get("excluded_invalid_count", 0) or 0)
        balance = income - expenses
        s, e = self.filter_dates()
        if s and e:
            days = self._filtered_day_count([], s, e)
        else:
            try:
                first = parse_date(str(summary.get("first_date") or ""))
                last = parse_date(str(summary.get("last_date") or ""))
                days = max(1, (last - first).days + 1)
            except (AttributeError, TypeError, ValueError):
                days = self._filtered_day_count(rows)
        metrics = [
            (
                "Entradas",
                eur(income, self.app.theme.privacy),
                "período filtrado",
                GREEN,
                "↓",
            ),
            (
                "Saídas",
                eur(expenses, self.app.theme.privacy),
                "período filtrado",
                RED,
                "↑",
            ),
            (
                "Saldo do período",
                eur(balance, self.app.theme.privacy),
                (
                    f"{total_count} movimentos · {excluded_invalid} a rever"
                    if excluded_invalid
                    else f"{total_count} movimentos"
                ),
                BLUE,
                "=",
            ),
            (
                "Média diária",
                eur(expenses / days, self.app.theme.privacy),
                "gastos/dia",
                ORANGE,
                "≈",
            ),
        ]
        self.clear_layout(self.summary)
        for i, m in enumerate(metrics):
            self.summary.addWidget(MetricCard(*m), 0, i)

    def refresh(
        self,
        *_args,
        reset_limit: bool = True,
        selected_tx_id: Optional[int] = None,
        force_replace: bool = True,
    ) -> None:
        if reset_limit:
            self._loaded_limit = self._page_size
        if not hasattr(self, "_loader_task"):
            from ui.workers import AsyncTask

            self._loader_task = AsyncTask(
                self,
                registry=self.app.background_task_registry,
                label="Pesquisa de movimentos",
            )
            self._loader_task.result.connect(self._apply_loaded_payload)
            self._loader_task.error.connect(self._movement_load_error)
            self._loader_task.finished.connect(self._movement_loader_finished)

        self._load_generation += 1
        generation = self._load_generation
        if self._loader_task.is_running():
            self._pending_load = True
            self._pending_reset_limit = self._pending_reset_limit or reset_limit
            if selected_tx_id is not None:
                self._pending_selected_tx_id = selected_tx_id
            self._pending_force_replace = self._pending_force_replace or force_replace
            # AsyncTask forwards this to sqlite3_interrupt() on the connection
            # owned by the stale search instead of merely ignoring its result.
            self._loader_task.cancel()
            return

        s, e, search_txt, cat, kind, attach = self._filter_query_values()
        limit = self._loaded_limit

        background_load = self.repo.transactions_filtered_payload_operation(
            s,
            e,
            search_txt,
            cat,
            kind,
            attachments=attach,
            limit=limit,
            context={
                "generation": generation,
                "selected_tx_id": selected_tx_id,
                "force_replace": force_replace,
            },
        )

        if force_replace:
            self.table_status.setText("A carregar movimentos…")
        if not self._loader_task.start(background_load):
            self._pending_load = True

    def _select_transaction_row(self, tx_id: Optional[int]) -> None:
        if not tx_id:
            return
        for row in range(self.movements_model.rowCount()):
            if self.movements_model.transaction_id(row) == int(tx_id):
                idx = self.movements_model.index(row, 1)
                self.table.selectRow(row)
                self.table.scrollTo(idx, QAbstractItemView.PositionAtCenter)
                return

    def open_reimbursements(self) -> None:
        from services.reimbursements import ReimbursementsService
        from ui.reimbursements import ReimbursementsDialog

        dialog = ReimbursementsDialog(
            ReimbursementsService(self.repo), self, source_transaction_id=self.selected_id()
        )
        dialog.changed.connect(self.app.notify_data_changed)
        self._reimbursements_dialog = dialog
        try:
            dialog.exec()
        finally:
            self._reimbursements_dialog = None
            if is_qt_valid(dialog):
                dialog.close()
                dialog.deleteLater()

    def update_export_privacy_labels(self) -> None:
        super().update_export_privacy_labels()
        dialog = getattr(self, "_reimbursements_dialog", None)
        if is_qt_valid(dialog):
            dialog.sync_privacy()

    def selected_ids(self) -> list[int]:
        selected = (
            self.table.selectionModel().selectedRows()
            if self.table.selectionModel()
            else []
        )
        ids: list[int] = []
        if selected:
            for index in selected:
                tx_id = self.movements_model.transaction_id(index.row())
                if tx_id is not None and tx_id not in ids:
                    ids.append(tx_id)
        else:
            tx_id = self.selected_id()
            if tx_id is not None:
                ids.append(tx_id)
        return ids

    def selected_id(self) -> Optional[int]:
        rows = (
            self.table.selectionModel().selectedRows()
            if self.table.selectionModel()
            else []
        )
        if not rows:
            idx = self.table.currentIndex()
            row = idx.row() if idx.isValid() else -1
        else:
            row = rows[0].row()
        if row < 0:
            return None
        return self.movements_model.transaction_id(row)

    def refresh_detail_panel(self) -> None:
        ids = self.selected_ids()
        if not ids:
            self.detail_text.setText(
                "Seleciona um movimento para ver detalhes, anexos, notas e ações rápidas."
            )
            return
        if len(ids) > 1:
            total = 0.0
            for tx_id in ids:
                row = self.repo.transaction_by_id(tx_id)
                total += safe_float(row["amount"]) if row else 0.0
            self.detail_text.setText(
                f"{len(ids)} movimentos selecionados\n\n"
                f"Total líquido: {eur(total, self.app.theme.privacy)}\n"
                f"Usa Ações em lote para alterar categoria, conta, etiquetas, marcar como revisto ou eliminar."
            )
            return
        tx_id = ids[0]
        row = self.repo.transaction_full_by_id(tx_id) or self.repo.transaction_by_id(
            tx_id
        )
        if row is None:
            self.detail_text.setText(
                "O movimento selecionado já não existe. Atualiza a lista."
            )
            return
        meta = self.repo.transaction_meta_ext(tx_id)
        attachments = self.repo.transaction_attachment_names(tx_id)
        splits = self.repo.transaction_splits(tx_id)
        attach_text = (
            "Sem anexos"
            if not attachments
            else ", ".join(attachments[:5])
            + (f" (+{len(attachments) - 5} anexos)" if len(attachments) > 5 else "")
        )
        split_text = (
            "Sem divisão"
            if not splits
            else "; ".join(
                f"{s['category']} {percent(s['percent'])}" for s in splits[:5]
            )
            + (f" (+{len(splits) - 5} partes)" if len(splits) > 5 else "")
        )
        self.detail_text.setText(
            f"#{tx_id} · {row_get(row, 'description')}\n\n"
            f"Valor: {eur(safe_float(row_get(row, 'amount')), self.app.theme.privacy)}\n"
            f"Pagamento: {row_get(row, 'payment_date') or row_get(row, 'date')}\n"
            f"Compra: {row_get(row, 'purchase_date') or row_get(row, 'date')}\n"
            f"Categoria: {row_get(row, 'category') or 'Sem categoria'}\n"
            f"Subcategoria: {row_get(row, 'subcategory') or '—'}\n"
            f"Método: {row_get(row, 'payment_method') or '—'}\n"
            f"Conta: {row_get(row, 'account') or '—'}\n"
            f"Comerciante: {row_get(row, 'merchant') or '—'}\n"
            f"Etiquetas: {row_get(row, 'tags') or '—'}\n"
            f"Estado: {meta.get('status') or '—'}\n"
            f"Anexos: {attach_text}\n"
            f"Divisão: {split_text}\n\n"
            f"Notas:\n{row_get(row, 'notes') or '—'}"
        )

    def _select_transaction_rows(self, tx_ids: list[int]) -> None:
        from PySide6.QtCore import QItemSelectionModel

        ids = {int(x) for x in tx_ids if x}
        if not ids:
            return
        self.table.clearSelection()
        selection = self.table.selectionModel()
        first_index = None
        for row in range(self.movements_model.rowCount()):
            tx_id = self.movements_model.transaction_id(row)
            if tx_id in ids:
                idx = self.movements_model.index(row, 0)
                if selection is not None:
                    selection.select(
                        idx, QItemSelectionModel.Select | QItemSelectionModel.Rows
                    )
                if first_index is None:
                    first_index = self.movements_model.index(row, 1)
        if first_index is not None:
            self.table.scrollTo(first_index, QAbstractItemView.PositionAtCenter)
        self.refresh_detail_panel()

    def _choice_dialog(
        self, title: str, label: str, values: list[str], current: str = ""
    ) -> Optional[str]:
        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        dialog.setMinimumWidth(420)
        dialog.setStyleSheet(self.app.styleSheet())
        form = QFormLayout(dialog)
        combo = QComboBox()
        combo.setEditable(True)
        combo.addItems(values)
        if current:
            combo.setCurrentText(current)
        form.addRow(label, combo)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addWidget(buttons)
        if dialog.exec() == QDialog.Accepted:
            return combo.currentText().strip()
        return None

    def _text_dialog(self, title: str, label: str, current: str = "") -> Optional[str]:
        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        dialog.setMinimumWidth(420)
        dialog.setStyleSheet(self.app.styleSheet())
        form = QFormLayout(dialog)
        text = QLineEdit(current)
        form.addRow(label, text)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addWidget(buttons)
        if dialog.exec() == QDialog.Accepted:
            return text.text().strip()
        return None

    def _attach_dialog_files(
        self, transaction_id: int, paths: list[Path], description: str
    ) -> None:
        failed: list[str] = []
        for path in paths:
            try:
                self.app.document_service.attach_file(
                    transaction_id,
                    path,
                    description,
                )
            except Exception as exc:
                failed.append(path.name)
                log_exception(f"Failed to attach movement document: {path}", exc)
        if failed:
            names = ", ".join(failed[:3])
            extra = "" if len(failed) <= 3 else f" e mais {len(failed) - 3}"
            QMessageBox.warning(
                self,
                APP_TITLE,
                "O movimento foi guardado, mas não foi possível adicionar "
                f"{len(failed)} anexo(s): {names}{extra}.",
            )

    def add_transaction(self) -> None:
        dialog = TransactionDialog(self.repo, self.app)
        # BUG-DLG-03: a 0.00 amount reaches the repo and is rejected only
        # after the dialog closed, silently discarding notes and attachments.
        # Caller-side guard: keep the SAME dialog object alive (all form
        # values preserved) and re-exec it until the amount is valid or the
        # user cancels.
        while True:
            if dialog.exec() != QDialog.Accepted:
                return
            data = dialog.extended_data()
            if not data["description"]:
                QMessageBox.warning(self, APP_TITLE, "A descrição é obrigatória.")
                return
            if abs(float(data["amount"])) < 0.01:
                QMessageBox.warning(
                    self,
                    APP_TITLE,
                    "O valor do movimento não pode ser zero. "
                    "Corrige o valor para guardar — os dados preenchidos foram mantidos.",
                )
                continue
            break
        try:
            created_ids = (
                self.app.change_journal_service.run_with_created_transactions(
                    lambda: [
                        self.repo.add_transaction_with_meta(
                            data["date"],
                            data["description"],
                            data["amount"],
                            data["category"],
                            data["account"],
                            data["notes"],
                            tags=data["tags"],
                            merchant=data["merchant"],
                            payment_method=data["payment_method"],
                            subcategory=data["subcategory"],
                            purchase_date=data["purchase_date"],
                            payment_date=data["payment_date"],
                            commit=False,
                        )
                    ],
                    label=f"Novo movimento · {data['description']}",
                    action_type="create",
                )
            )
            tx_id = created_ids[0]
        except Exception as exc:
            log_exception("Failed to create movement and metadata", exc)
            QMessageBox.critical(
                self,
                APP_TITLE,
                "Não foi possível guardar o movimento. Nenhum dado foi aplicado.",
            )
            return
        self._attach_dialog_files(
            tx_id,
            dialog.attachment_paths(),
            "Anexo adicionado ao criar movimento",
        )
        self.app.refresh_undo_actions()
        self.force_refresh_after_save(tx_id)

    def add_transfer(self) -> None:
        from ui.dialogs import TransferDialog

        dialog = TransferDialog(self.repo, self.app)
        if dialog.exec() == QDialog.Accepted:
            data = dialog.extended_data()
            amount = data["amount"]
            date = data["date"]
            source = data["source"]
            dest = data["dest"]

            try:
                created_ids = (
                    self.app.change_journal_service.run_with_created_transactions(
                        lambda: list(
                            self.repo.add_internal_transfer(
                                date,
                                amount,
                                source,
                                dest,
                                commit=False,
                            )
                        ),
                        label=f"Transferência · {source} → {dest}",
                        action_type="transfer",
                    )
                )
            except Exception as exc:
                log_exception("Failed to create journaled internal transfer", exc)
                QMessageBox.critical(
                    self,
                    APP_TITLE,
                    "Não foi possível criar a transferência. Nenhum movimento foi guardado.",
                )
                return
            tx2 = created_ids[-1]
            self.app.refresh_undo_actions()
            self.force_refresh_after_save(tx2)

    def edit_selected(self) -> None:
        ids = self.selected_ids()
        if not ids:
            QMessageBox.information(
                self, APP_TITLE, "Seleciona primeiro um movimento para editar."
            )
            return
        if len(ids) > 1:
            box = QMessageBox(self)
            box.setWindowTitle(APP_TITLE)
            box.setIcon(QMessageBox.Question)
            box.setText(
                f"Estão selecionados {len(ids)} movimentos.\n\n"
                "Pretendes abrir o menu de ações em lote ou editar individualmente o primeiro movimento selecionado?"
            )
            btn_bulk = box.addButton("Ações em Lote", QMessageBox.ActionRole)
            box.addButton("Editar Primeiro", QMessageBox.AcceptRole)
            btn_cancel = box.addButton("Cancelar", QMessageBox.RejectRole)
            box.setDefaultButton(btn_bulk)
            box.exec()
            clicked = box.clickedButton()
            if clicked == btn_cancel:
                return
            elif clicked == btn_bulk:
                self.open_bulk_menu()
                return

        tx_id = ids[0]
        row = self.repo.transaction_by_id(tx_id)
        if row is None:
            QMessageBox.warning(
                self, APP_TITLE, "O movimento selecionado já não existe."
            )
            self.refresh()
            return
        try:
            transfer = self.repo.internal_transfer_by_id(tx_id)
        except (RuntimeError, ValueError) as exc:
            QMessageBox.warning(self, APP_TITLE, str(exc))
            return
        if transfer is not None:
            from ui.dialogs import TransferDialog

            dialog = TransferDialog(self.repo, self.app, transfer=transfer)
            if dialog.exec() != QDialog.Accepted:
                return
            data = dialog.extended_data()
            try:
                applied, _result = self._run_journaled_change(
                    list(transfer["ids"]),
                    lambda: self.repo.update_internal_transfer(
                        transfer["group_id"],
                        data["date"],
                        data["amount"],
                        data["source"],
                        data["dest"],
                        commit=False,
                    ),
                    label=(
                        f"Edição de transferência · {data['source']} → {data['dest']}"
                    ),
                    action_type="edit_transfer",
                )
            except Exception as exc:
                log_exception("Failed to edit linked transfer", exc)
                QMessageBox.critical(
                    self,
                    APP_TITLE,
                    "Não foi possível atualizar a transferência. "
                    "As duas partes foram mantidas sem alterações.",
                )
                return
            if applied:
                self.force_refresh_after_save(int(transfer["ids"][-1]))
            return
        dialog = TransactionDialog(self.repo, self.app, row=row)
        # BUG-DLG-03: same caller-side zero-amount guard as add_transaction —
        # re-exec the SAME dialog (form state preserved) instead of letting a
        # 0.00 amount reach the repo and drop the user's edits.
        while True:
            if dialog.exec() != QDialog.Accepted:
                return
            data = dialog.extended_data()
            if not data["description"]:
                QMessageBox.warning(self, APP_TITLE, "A descrição é obrigatória.")
                return
            if abs(float(data["amount"])) < 0.01:
                QMessageBox.warning(
                    self,
                    APP_TITLE,
                    "O valor do movimento não pode ser zero. "
                    "Corrige o valor para guardar — os dados preenchidos foram mantidos.",
                )
                continue
            break
        category_changed = row["category"] != data["category"]

        def apply_edit() -> None:
            self.repo.update_transaction_with_meta(
                tx_id,
                {
                    "date": data["date"],
                    "description": data["description"],
                    "amount": data["amount"],
                    "category": data["category"],
                    "account": data["account"],
                    "notes": data["notes"],
                    "subcategory": data["subcategory"],
                },
                tags=data["tags"],
                merchant=data["merchant"],
                payment_method=data["payment_method"],
                subcategory=data["subcategory"],
                purchase_date=data["purchase_date"],
                payment_date=data["payment_date"],
                commit=False,
            )
            if category_changed:
                self.repo.add_category_rule(
                    data["description"].lower().strip(),
                    data["category"],
                    [],
                    commit=False,
                )

        applied, _result = self._run_journaled_change(
            [tx_id],
            apply_edit,
            label=f"Edição · {data['description']}",
            action_type="edit",
        )
        if not applied:
            return
        self._attach_dialog_files(
            tx_id,
            dialog.attachment_paths(),
            "Anexo adicionado ao editar movimento",
        )
        # Verify with a direct read, then refresh now and again after the dialog
        # event loop fully returns. This covers both the database and Qt view layers.
        saved = self.repo.transaction_by_id(tx_id)
        if saved is None:
            QMessageBox.warning(
                self, APP_TITLE, "Não consegui reler o movimento guardado."
            )
            return
        self.force_refresh_after_save(tx_id)
        QTimer.singleShot(
            0,
            lambda tx_id=tx_id: self.force_refresh_after_save(
                tx_id, notify_views=False
            ),
        )
        QTimer.singleShot(
            120,
            lambda tx_id=tx_id: self.force_refresh_after_save(
                tx_id, notify_views=False
            ),
        )

    def delete_selected(self) -> None:
        ids = self.selected_ids()
        if len(ids) > 1:
            self.bulk_delete_selected(ids)
            return
        tx_id = ids[0] if ids else None
        if not tx_id:
            QMessageBox.information(
                self, APP_TITLE, "Seleciona primeiro um movimento para eliminar."
            )
            return
        row = self.repo.transaction_by_id(tx_id)
        desc = row["description"] if row else f"ID {tx_id}"
        linked_ids = self.repo.expand_transfer_linked_ids([tx_id])
        linked_note = (
            "\n\nEsta é uma transferência ligada: a saída e a entrada serão "
            "eliminadas em conjunto."
            if len(linked_ids) > 1
            else ""
        )
        reply = QMessageBox.question(
            self,
            APP_TITLE,
            f"Queres mesmo eliminar este movimento?\n\n{desc}{linked_note}\n\n"
            "Também serão removidas as divisões, metadados e anexos associados.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        try:
            _removed, preserved_files = (
                self.app.change_journal_service.delete_transactions(
                    [tx_id],
                    label=f"Eliminação · {desc}",
                )
            )
            self.app.document_service.invalidate_audit_cache()
        except Exception as exc:
            log_exception("Failed to delete movement", exc)
            QMessageBox.critical(
                self,
                APP_TITLE,
                "Não foi possível eliminar o movimento. Nenhum dado foi removido.",
            )
            return

        self.force_refresh_after_save(None)
        self.table_status.setText(
            f"Movimento eliminado · {preserved_files} anexo(s) protegido(s) para desfazer."
        )
        self.app.refresh_undo_actions()

    def duplicate_selected(self) -> None:
        tx_id = self.selected_id()
        if not tx_id:
            QMessageBox.information(
                self, APP_TITLE, "Seleciona primeiro um movimento para duplicar."
            )
            return
        row = self.repo.transaction_full_by_id(tx_id)
        if row is None:
            QMessageBox.warning(
                self, APP_TITLE, "Não encontrei o movimento selecionado."
            )
            self.refresh()
            return
        try:
            is_transfer = bool(row_get(row, "transfer_group_id", ""))

            def mutation() -> list[int]:
                if is_transfer:
                    return list(
                        self.repo.duplicate_internal_transfer(tx_id, commit=False)
                    )
                return [self.repo.duplicate_transaction(tx_id, commit=False)]

            created_ids = self.app.change_journal_service.run_with_created_transactions(
                mutation,
                label=(
                    "Duplicação de transferência"
                    if is_transfer
                    else f"Duplicação · {row_get(row, 'description')}"
                ),
                action_type="duplicate_transfer" if is_transfer else "duplicate",
            )
            new_id = created_ids[-1]
        except Exception as exc:
            log_exception("Failed to duplicate movement", exc)
            QMessageBox.critical(
                self,
                APP_TITLE,
                "Não foi possível duplicar o movimento. Nenhuma cópia foi criada.",
            )
            return
        self.app.refresh_undo_actions()
        self.force_refresh_after_save(new_id)

    def open_bulk_menu(self) -> None:
        ids = self.selected_ids()
        if not ids:
            QMessageBox.information(
                self,
                APP_TITLE,
                "Seleciona um ou mais movimentos para usar ações em lote.",
            )
            return
        menu = QMenu(self)
        apply_menu_theme(menu, self.app.theme)
        cat_action = menu.addAction(f"Alterar categoria ({len(ids)})")
        account_action = menu.addAction(f"Alterar conta ({len(ids)})")
        tags_action = menu.addAction(f"Adicionar etiquetas ({len(ids)})")
        reviewed_action = menu.addAction(f"Marcar como revisto ({len(ids)})")
        rules_action = menu.addAction(f"Aplicar regras aos selecionados ({len(ids)})")
        menu.addSeparator()
        delete_action = menu.addAction(f"Eliminar selecionados ({len(ids)})")
        action = menu.exec(QCursor.pos())
        if action == cat_action:
            self.bulk_update_category(ids)
        elif action == account_action:
            self.bulk_update_account(ids)
        elif action == tags_action:
            self.bulk_add_tags(ids)
        elif action == reviewed_action:
            applied, count = self._run_journaled_change(
                ids,
                lambda: self.repo.mark_transactions_reviewed(ids, commit=False),
                label=lambda value: f"Marcar como revisto · {value} movimento(s)",
            )
            if not applied:
                return
            self.force_refresh_after_save(ids[0] if ids else None)
            self.table_status.setText(f"{count} movimento(s) marcados como revistos.")
        elif action == rules_action:
            self.apply_rules_to_ids(ids)
        elif action == delete_action:
            self.bulk_delete_selected(ids)

    def bulk_update_category(self, ids: list[int]) -> None:
        category = self._choice_dialog(
            "Alterar categoria", "Nova categoria", self.repo.categories() or ["Outros"]
        )
        if not category:
            return

        splits_map = getattr(self.repo, "transaction_splits_by_ids", None)
        split_tx_ids = []
        if callable(splits_map):
            try:
                split_dict = splits_map(ids)
                split_tx_ids = [tid for tid, splits in split_dict.items() if splits]
            except Exception as exc:
                log_exception("Error checking transaction splits", exc)

        target_ids = list(ids)
        if split_tx_ids:
            box = QMessageBox(self)
            box.setWindowTitle(APP_TITLE)
            box.setIcon(QMessageBox.Warning)
            box.setText(
                f"Dos {len(ids)} movimentos selecionados, {len(split_tx_ids)} possuem "
                "divisões detalhadas de categorias (splits).\n\n"
                "Ao alterar a categoria em lote, as divisões desses movimentos serão removidas.\n\n"
                "Como pretendes proceder?"
            )
            btn_all = box.addButton("Remover divisões e alterar todos", QMessageBox.AcceptRole)
            btn_skip = box.addButton("Ignorar movimentos com divisões", QMessageBox.ActionRole)
            btn_cancel = box.addButton("Cancelar", QMessageBox.RejectRole)
            box.setDefaultButton(btn_skip)
            box.exec()

            clicked = box.clickedButton()
            if clicked == btn_cancel:
                return
            elif clicked == btn_skip:
                target_ids = [tid for tid in ids if tid not in split_tx_ids]
                if not target_ids:
                    QMessageBox.information(
                        self,
                        APP_TITLE,
                        "Todos os movimentos selecionados possuem divisões. Nenhuma alteração foi efetuada.",
                    )
                    return
            elif clicked == btn_all:
                target_ids = list(ids)
            else:
                return

        def apply_category_change() -> int:
            # Learn the same rules only if the whole bulk edit can be committed.
            for tx_id in target_ids:
                tx = self.repo.transaction_by_id(tx_id)
                if tx and tx["category"] != category:
                    self.repo.add_category_rule(
                        tx["description"].lower().strip(),
                        category,
                        [],
                        commit=False,
                    )
            return self.repo.update_transactions_category(
                target_ids,
                category,
                commit=False,
            )

        applied, count = self._run_journaled_change(
            target_ids,
            apply_category_change,
            label=lambda value: f"Categoria em lote · {value} movimento(s)",
        )
        if not applied:
            return
        self.force_refresh_after_save(target_ids[0] if target_ids else None)
        self.table_status.setText(
            f"{count} movimento(s) atualizados para categoria {category}."
        )

    def bulk_update_account(self, ids: list[int]) -> None:
        account = self._text_dialog("Alterar conta", "Nova conta", "Conta principal")
        if not account:
            return
        applied, count = self._run_journaled_change(
            ids,
            lambda: self.repo.update_transactions_account(
                ids,
                account,
                commit=False,
            ),
            label=lambda value: f"Conta em lote · {value} movimento(s)",
        )
        if not applied:
            return
        self.force_refresh_after_save(ids[0] if ids else None)
        self.table_status.setText(
            f"{count} movimento(s) atualizados para conta {account}."
        )

    def bulk_add_tags(self, ids: list[int]) -> None:
        tags = self._text_dialog(
            "Adicionar etiquetas", "Etiquetas separadas por vírgula"
        )
        if not tags:
            return
        applied, count = self._run_journaled_change(
            ids,
            lambda: self.repo.add_tags_to_transactions(ids, tags, commit=False),
            label=lambda value: f"Etiquetas em lote · {value} movimento(s)",
        )
        if not applied:
            return
        self.force_refresh_after_save(ids[0] if ids else None)
        self.table_status.setText(f"Etiquetas adicionadas a {count} movimento(s).")

    def bulk_delete_selected(self, ids: Optional[list[int]] = None) -> None:
        ids = ids or self.selected_ids()
        if not ids:
            QMessageBox.information(
                self, APP_TITLE, "Seleciona movimentos para eliminar."
            )
            return

        expand = getattr(self.repo, "expand_transfer_linked_ids", None)
        all_ids = list(ids)
        if callable(expand):
            try:
                all_ids = list(dict.fromkeys(int(x) for x in expand(ids) if int(x or 0) > 0))
            except Exception as exc:
                log_exception("Error expanding transfer linked ids", exc)
                all_ids = list(ids)

        extra_counterparts = len(all_ids) - len(ids)
        if extra_counterparts > 0:
            msg = (
                f"Queres mesmo eliminar {len(all_ids)} movimento(s)?\n\n"
                f"⚠️ Atenção: A seleção inclui transferências entre contas. "
                f"Serão eliminadas {extra_counterparts} contraparte(s) associada(s) "
                "para manter a integridade dos saldos das contas.\n\n"
                "Também serão removidas divisões, metadados e anexos associados."
            )
        else:
            msg = (
                f"Queres mesmo eliminar {len(ids)} movimento(s)?\n\n"
                "Também serão removidas divisões, metadados e anexos associados."
            )

        reply = QMessageBox.question(
            self,
            APP_TITLE,
            msg,
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        try:
            _removed, preserved_files = (
                self.app.change_journal_service.delete_transactions(
                    all_ids,
                    label=f"Eliminação em lote · {len(all_ids)} movimento(s)",
                )
            )
            self.app.document_service.invalidate_audit_cache()
        except (ValueError, RuntimeError) as exc:
            log_exception("Validation error deleting selected movements", exc)
            QMessageBox.warning(
                self,
                APP_TITLE,
                f"Não foi possível eliminar os movimentos selecionados:\n\n{exc}",
            )
            return
        except Exception as exc:
            log_exception("Failed to delete selected movements", exc)
            QMessageBox.critical(
                self,
                APP_TITLE,
                "Não foi possível eliminar a seleção. Nenhum movimento foi removido.",
            )
            return

        self.force_refresh_after_save(None)
        self.table_status.setText(
            f"{_removed} movimento(s) eliminados · "
            f"{preserved_files} anexo(s) protegido(s) para desfazer."
        )
        self.app.refresh_undo_actions()

    def apply_rules_to_ids(self, ids: list[int]) -> None:
        if not ids:
            return
        applied, count = self._run_journaled_change(
            ids,
            lambda: self.repo.apply_category_rules_to_transactions(
                ids,
                commit=False,
            ),
            label=lambda value: f"Regras de categoria · {value} movimento(s)",
            journal_when=lambda value: bool(value),
        )
        if not applied:
            return
        self.force_refresh_after_save(ids[0])
        self.table_status.setText(f"Regras aplicadas: {count} movimento(s) alterados.")

    def apply_rules_to_current_scope(self) -> None:
        ids = self.selected_ids()
        scope = "selecionados"
        if not ids:
            ids = [int(row_get(r, "id")) for r in self.rows()]
            scope = "filtrados"
        if not ids:
            QMessageBox.information(
                self, APP_TITLE, "Não há movimentos para aplicar regras."
            )
            return
        reply = QMessageBox.question(
            self,
            APP_TITLE,
            f"Aplicar regras de categorização aos {len(ids)} movimentos {scope}?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        self.apply_rules_to_ids(ids)

    def review_duplicates(self) -> None:
        rows = self.repo.possible_duplicates()
        if not rows:
            QMessageBox.information(
                self, APP_TITLE, "Não encontrei duplicados prováveis."
            )
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("Possíveis duplicados")
        dialog.setMinimumSize(760, 420)
        dialog.setStyleSheet(self.app.styleSheet())
        layout = QVBoxLayout(dialog)
        hint = QLabel(
            "Seleciona um grupo para filtrar e rever os movimentos envolvidos. A app não elimina nada automaticamente."
        )
        hint.setObjectName("Subtle")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        table = QTableWidget(0, 7)
        table.setProperty("columnPersistenceId", "movements.duplicate_review")
        table.setHorizontalHeaderLabels(
            ["Confiança", "Data", "Descrição", "Valor", "Categoria", "Qtd.", "IDs"]
        )
        table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        table.horizontalHeader().setSectionResizeMode(6, QHeaderView.Stretch)
        table.setSelectionBehavior(QTableWidget.SelectRows)
        table.setSelectionMode(QTableWidget.SingleSelection)
        table.setAlternatingRowColors(True)
        table.verticalHeader().setVisible(False)
        table.setRowCount(len(rows))
        for row, r in enumerate(rows):
            vals = [
                str(row_get(r, "confidence") or "provável").capitalize(),
                row_get(r, "date_key"),
                row_get(r, "description"),
                eur(safe_float(row_get(r, "amount_key")), self.app.theme.privacy),
                row_get(r, "category"),
                row_get(r, "count"),
                row_get(r, "ids"),
            ]
            for col, val in enumerate(vals):
                item = QTableWidgetItem(str(val))
                if col in (3, 5):
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                if col == 0:
                    confidence = str(row_get(r, "confidence") or "").casefold()
                    item.setForeground(
                        QColor(
                            RED
                            if confidence == "certa"
                            else ORANGE
                            if confidence == "provável"
                            else "#B7791F"
                        )
                    )
                table.setItem(row, col, item)
        layout.addWidget(table, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.Open | QDialogButtonBox.Close)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        table.doubleClicked.connect(lambda *_: dialog.accept())
        if dialog.exec() != QDialog.Accepted:
            return
        current = table.currentRow()
        if current < 0:
            return
        ids_text = table.item(current, 6).text() if table.item(current, 6) else ""
        ids = [int(x) for x in ids_text.split(",") if x.strip().isdigit()]
        if not ids:
            return
        self.period.setCurrentText("Tudo")
        self.refresh()
        self._select_transaction_rows(ids)

    def open_selected_in_splitter(self) -> None:
        tx_id = self.selected_id()
        if not tx_id:
            QMessageBox.information(self, APP_TITLE, "Seleciona primeiro um movimento.")
            return
        self.app.goto("divisao")
        page = self.app.pages.get("divisao")
        if page is not None and hasattr(page, "tx"):
            try:
                page.reload_transactions()
                idx = page.tx.findData(tx_id)
                if idx >= 0:
                    page.tx.setCurrentIndex(idx)
            except Exception as exc:
                log_exception("Failed to open movement in smart splitter", exc)

    def open_table_menu(self, pos: QPointF) -> None:
        if self.movements_model.rowCount() == 0:
            return
        if self.selected_id() is None:
            index = self.table.indexAt(
                pos.toPoint() if hasattr(pos, "toPoint") else pos
            )
            if index.isValid():
                self.table.selectRow(index.row())
            else:
                return
        if self.selected_id() is None:
            return
        menu = QMenu(self)
        apply_menu_theme(menu, self.app.theme)
        edit_action = menu.addAction("Editar movimento")
        duplicate_action = menu.addAction("Duplicar movimento")
        attach_action = menu.addAction("Anexar fatura/documento")
        split_action = menu.addAction("Abrir na divisão inteligente")
        bulk_action = menu.addAction("Ações em lote")
        delete_action = menu.addAction("Eliminar movimento")
        action = menu.exec(
            self.table.viewport().mapToGlobal(
                pos.toPoint() if hasattr(pos, "toPoint") else pos
            )
        )
        if action == edit_action:
            self.edit_selected()
        elif action == duplicate_action:
            self.duplicate_selected()
        elif action == attach_action:
            self.attach_to_selected()
        elif action == split_action:
            self.open_selected_in_splitter()
        elif action == bulk_action:
            self.open_bulk_menu()
        elif action == delete_action:
            self.delete_selected()

    def attach_to_selected(self) -> None:
        tx_id = self.selected_id()
        if not tx_id:
            QMessageBox.information(self, APP_TITLE, "Seleciona primeiro um movimento.")
            return
        files, _ = QFileDialog.getOpenFileNames(
            self,
            "Escolher faturas/anexos",
            str(Path.home()),
            self.app.document_service.file_dialog_filter(),
        )
        if not files:
            return
        accepted = 0
        rejected: list[str] = []
        for f in files:
            path = Path(f)
            try:
                self.app.document_service.attach_file(
                    tx_id,
                    path,
                    "Anexo adicionado na interface Liquid Glass",
                )
            except Exception as exc:
                rejected.append(f"{path.name}: {exc}")
                log_exception(f"Failed to attach document {path}", exc)
            else:
                accepted += 1
        if accepted:
            QMessageBox.information(
                self,
                APP_TITLE,
                f"{accepted} anexo(s) associado(s) ao movimento.",
            )
            self.force_refresh_after_save(tx_id)
        if rejected:
            QMessageBox.warning(
                self,
                APP_TITLE,
                "Alguns ficheiros foram recusados por segurança:\n\n"
                + "\n".join(rejected[:6]),
            )

    def export_csv(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Exportar CSV",
            str(Path.home() / "movimentos_filtrados.csv"),
            "CSV (*.csv)",
        )
        if not path:
            return
        try:
            rows = self.rows()
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f, delimiter=";")
                writer.writerow(
                    [
                        "id",
                        "data_pagamento",
                        "data_compra",
                        "descricao",
                        "categoria",
                        "subcategoria",
                        "metodo",
                        "conta",
                        "valor",
                        "etiquetas",
                        "comerciante",
                        "notas",
                        "anexos",
                    ]
                )
                for r in rows:
                    writer.writerow(
                        privacy_export_row(
                            [
                                row_get(r, "id"),
                                row_get(r, "payment_date") or row_get(r, "date"),
                                row_get(r, "purchase_date") or row_get(r, "date"),
                                row_get(r, "description"),
                                row_get(r, "category"),
                                row_get(r, "subcategory"),
                                row_get(r, "payment_method"),
                                row_get(r, "account"),
                                row_get(r, "amount"),
                                row_get(r, "tags"),
                                row_get(r, "merchant"),
                                row_get(r, "notes"),
                                row_get(r, "attachment_count", 0),
                            ],
                            [
                                "id",
                                "date",
                                "date",
                                "description",
                                "category",
                                "subcategory",
                                "method",
                                "account",
                                "amount",
                                "tags",
                                "merchant",
                                "notes",
                                "attachments",
                            ],
                            self.app.theme.privacy,
                        )
                    )
            QMessageBox.information(self, APP_TITLE, f"Exportado para:\n{path}")
        except PermissionError as exc:
            log_exception(f"Ficheiro CSV bloqueado ou sem permissão em {path}", exc)
            QMessageBox.critical(
                self,
                "Ficheiro em Uso",
                f"Erro ao exportar CSV:\nNão foi possível gravar '{Path(path).name}'. O ficheiro está aberto noutro programa (ex.: Excel). Feche o ficheiro e tente novamente.",
            )
        except Exception as exc:
            log_exception(f"Falha ao exportar CSV para {path}", exc)
            QMessageBox.critical(self, APP_TITLE, f"Erro ao exportar CSV:\n{exc}")

    def export_pdf(self, *, run_async: bool = False) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Exportar PDF",
            str(Path.home() / "movimentos_filtrados.pdf"),
            "PDF (*.pdf)",
        )
        if not path:
            return

        try:
            # SEC-EXP-03: Test file writability on Windows before QPrinter rendering
            with open(path, "ab"):
                pass

            if run_async:
                self._start_async_pdf_export(path)
                return

            export_movements_to_pdf_file(
                self.rows(),
                path,
                privacy=self.app.theme.privacy,
            )
            QMessageBox.information(self, "Sucesso", f"Relatório PDF guardado em:\n{path}")
        except PermissionError as exc:
            log_exception(f"Ficheiro PDF bloqueado ou sem permissão em {path}", exc)
            QMessageBox.critical(
                self,
                "Ficheiro em Uso",
                f"Não foi possível gravar '{Path(path).name}'. O ficheiro está aberto noutro programa (ex.: leitor de PDF). Feche o ficheiro e tente novamente.",
            )
        except Exception as exc:
            log_exception(f"Falha ao exportar PDF para {path}", exc)
            QMessageBox.critical(self, APP_TITLE, f"Erro ao exportar PDF:\n{exc}")

    def _start_async_pdf_export(self, path: str) -> None:
        """Execute PDF rendering in background via AsyncTask to prevent GUI hang (SEC-EXP-04)."""
        if not hasattr(self, "_pdf_task") or self._pdf_task is None:
            from ui.workers import AsyncTask

            self._pdf_task = AsyncTask(
                self,
                registry=self.app.background_task_registry,
                label="Exportação de PDF de movimentos",
            )
            self._pdf_task.result.connect(self._on_pdf_export_success)
            self._pdf_task.error.connect(self._on_pdf_export_error)

        if self._pdf_task.is_running():
            QMessageBox.warning(
                self,
                APP_TITLE,
                "Já existe uma exportação de PDF em curso. Aguarde a sua conclusão.",
            )
            return

        self._pending_pdf_path = path
        op = AsyncPdfExporter(
            self.rows(),
            path,
            privacy=self.app.theme.privacy,
        )
        self._pdf_task.start(op)

    def _on_pdf_export_success(self, target_path: Any) -> None:
        p = target_path or getattr(self, "_pending_pdf_path", "")
        QMessageBox.information(self, "Sucesso", f"Relatório PDF guardado em:\n{p}")

    def _on_pdf_export_error(self, err_msg: str) -> None:
        p = getattr(self, "_pending_pdf_path", "")
        if "PermissionError" in err_msg or "bloqueado" in err_msg.lower() or "permission denied" in err_msg.lower():
            QMessageBox.critical(
                self,
                "Ficheiro em Uso",
                f"Não foi possível gravar '{Path(p).name}'. O ficheiro está aberto noutro programa (ex.: leitor de PDF). Feche o ficheiro e tente novamente.",
            )
        else:
            QMessageBox.critical(self, APP_TITLE, f"Erro ao exportar PDF:\n{err_msg}")
