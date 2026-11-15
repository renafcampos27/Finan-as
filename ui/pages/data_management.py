"""Visual tools for stable identities and non-destructive duplicate merging."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
)

from ui.components.cards import GlassCard, MetricCard
from ui.core.page import BasePage
from ui.core.smart_table import SmartTableWidgetItem as QTableWidgetItem
from utils.app_logging import log_exception
from utils.helpers import APP_TITLE, BLUE, GREEN, ORANGE, PURPLE


if TYPE_CHECKING:
    from main import LiquidFinanceApp


_AFFECTED_LABELS = {
    "transactions": "movimentos",
    "transaction_splits": "divisões",
    "subscriptions": "subscrições",
    "scheduled_payments": "pagamentos futuros",
    "holdings": "investimentos ligados",
    "liabilities": "dívidas ligadas",
    "category_rules": "regras",
    "monthly_category_budgets": "orçamentos mensais",
    "monthly_goals": "metas mensais",
    "subcategories": "subcategorias",
}


class DataManagementPage(BasePage):
    """Let the user merge duplicate labels while IDs preserve relationships."""

    def __init__(self, app: "LiquidFinanceApp") -> None:
        super().__init__(
            app,
            "Gestão e identidade dos dados",
            "Funde contas ou categorias duplicadas sem apagar movimentos nem documentos.",
        )
        self._rows_by_id: dict[str, dict[str, Any]] = {}
        self._last_preview: dict[str, Any] = {}

        self.summary_grid = QGridLayout()
        self.summary_grid.setContentsMargins(0, 0, 0, 0)
        self.summary_grid.setSpacing(8)
        self.summary_card = GlassCard("🧬 Identidades internas estáveis", strong=True)
        summary_hint = QLabel(
            "Os nomes continuam visíveis, mas as relações usam IDs internos. "
            "Renomear ou fundir deixa de separar o histórico financeiro."
        )
        summary_hint.setObjectName("Subtle")
        summary_hint.setWordWrap(True)
        self.summary_card.v.addWidget(summary_hint)
        self.summary_card.v.addLayout(self.summary_grid)
        self.register_responsive_grid(
            self.summary_grid,
            max_columns=5,
            min_column_width=170,
        )

        self.entity_type = QComboBox()
        self.entity_type.setAccessibleName("Tipo de registo a fundir")
        self.entity_type.addItem("🏦 Contas", "account")
        self.entity_type.addItem("🏷️ Categorias", "category")
        self.source = QComboBox()
        self.source.setAccessibleName("Registo duplicado de origem")
        self.target = QComboBox()
        self.target.setAccessibleName("Registo principal de destino")
        self.preview_btn = QPushButton("🔎 Pré-visualizar")
        self.preview_btn.setObjectName("Secondary")
        self.merge_btn = QPushButton("🧬 Fundir sem perder histórico")
        self.merge_btn.setObjectName("Primary")
        self.merge_btn.setEnabled(False)

        merge_form = QGridLayout()
        merge_form.setHorizontalSpacing(10)
        merge_form.setVerticalSpacing(6)
        merge_form.addWidget(QLabel("Tipo"), 0, 0)
        merge_form.addWidget(QLabel("Duplicado que será arquivado"), 0, 1)
        merge_form.addWidget(QLabel("Registo principal que permanece"), 0, 2)
        merge_form.addWidget(self.entity_type, 1, 0)
        merge_form.addWidget(self.source, 1, 1)
        merge_form.addWidget(self.target, 1, 2)
        actions = QHBoxLayout()
        actions.addStretch(1)
        actions.addWidget(self.preview_btn)
        actions.addWidget(self.merge_btn)
        self.preview_text = QLabel(
            "Seleciona uma origem e um destino para ver exatamente o que muda."
        )
        self.preview_text.setObjectName("CardDescription")
        self.preview_text.setWordWrap(True)
        self.preview_text.setTextFormat(Qt.RichText)
        self.preview_text.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.merge_card = GlassCard("🔗 Fundir duplicados", strong=True)
        self.merge_card.setProperty("pageCardKey", "stable_identity_merge")
        self.merge_card.v.addLayout(merge_form)
        self.merge_card.v.addWidget(self.preview_text)
        self.merge_card.v.addLayout(actions)

        self.suggestions = QTableWidget(0, 4)
        self.suggestions.setProperty(
            "columnPersistenceId",
            "data_management.merge_suggestions",
        )
        self.suggestions.setProperty(
            "columnPersistenceColumnIds",
            ["similarity", "source", "target", "impact"],
        )
        self.suggestions.setHorizontalHeaderLabels(
            ["Semelhança", "Possível duplicado", "Registo principal", "Impacto"]
        )
        self.suggestions.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.suggestions.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.suggestions.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.suggestions.setSelectionMode(QAbstractItemView.SingleSelection)
        self.suggestions.setAlternatingRowColors(True)
        self.suggestions.verticalHeader().setVisible(False)
        self.suggestions.setMinimumHeight(190)
        suggestions_hint = QLabel(
            "As sugestões usam apenas semelhança de nomes. Nada é fundido "
            "automaticamente: revê sempre a pré-visualização."
        )
        suggestions_hint.setObjectName("Subtle")
        suggestions_hint.setWordWrap(True)
        self.suggestions_card = GlassCard("✨ Sugestões de duplicados", strong=True)
        self.suggestions_card.v.addWidget(suggestions_hint)
        self.suggestions_card.v.addWidget(self.suggestions)

        self.history = QTableWidget(0, 5)
        self.history.setProperty(
            "columnPersistenceId",
            "data_management.merge_history",
        )
        self.history.setProperty(
            "columnPersistenceColumnIds",
            ["created_at", "entity_type", "source", "target", "affected"],
        )
        self.history.setHorizontalHeaderLabels(
            ["Data", "Tipo", "Origem preservada", "Destino", "Registos ligados"]
        )
        self.history.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.history.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.history.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.history.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.history.setAlternatingRowColors(True)
        self.history.verticalHeader().setVisible(False)
        self.history.setMinimumHeight(210)
        self.history_card = GlassCard("🕘 Histórico de fusões", strong=True)
        self.history_card.v.addWidget(self.history)

        self.maintenance_grid = QGridLayout()
        self.maintenance_grid.setContentsMargins(0, 0, 0, 0)
        self.maintenance_grid.setSpacing(8)
        self.maintenance_card = GlassCard(
            "⚡ Desempenho & Manutenção da Base de Dados", strong=True
        )
        maintenance_hint = QLabel(
            "Compacta o espaço em disco (VACUUM), reconstrói os índices B-Tree (REINDEX) "
            "e atualiza as estatísticas de velocidade de consultas (ANALYZE). "
            "Recomendado após grandes importações ou eliminações de movimentos."
        )
        maintenance_hint.setObjectName("Subtle")
        maintenance_hint.setWordWrap(True)
        self.optimize_btn = QPushButton("⚡ Otimizar & Compactar Base de Dados")
        self.optimize_btn.setObjectName("Primary")
        self.optimize_btn.clicked.connect(self._optimize_database)

        opt_actions = QHBoxLayout()
        opt_actions.addStretch(1)
        opt_actions.addWidget(self.optimize_btn)

        self.maintenance_card.v.addWidget(maintenance_hint)
        self.maintenance_card.v.addLayout(self.maintenance_grid)
        self.maintenance_card.v.addLayout(opt_actions)
        self.register_responsive_grid(
            self.maintenance_grid,
            max_columns=4,
            min_column_width=180,
        )

        self.body.addWidget(self.summary_card)
        self.body.addWidget(self.maintenance_card)
        self.body.addWidget(self.merge_card)
        self.body.addWidget(self.suggestions_card)
        self.body.addWidget(self.history_card)

        self.entity_type.currentIndexChanged.connect(self._reload_entities)
        self.source.currentIndexChanged.connect(self._selection_changed)
        self.target.currentIndexChanged.connect(self._selection_changed)
        self.preview_btn.clicked.connect(self.preview_merge)
        self.merge_btn.clicked.connect(self.merge_selected)
        self.suggestions.itemDoubleClicked.connect(self._use_suggestion)
        self.refresh()

    def _optimize_database(self) -> None:
        try:
            result = self.repo.optimize_database()
            freed_kb = round(result["freed_bytes"] / 1024.0, 1)
            duration_ms = result["duration_ms"]
            before_kb = round(result["before_bytes"] / 1024.0, 1)
            after_kb = round(result["after_bytes"] / 1024.0, 1)
            msg = (
                f"Base de dados otimizada com sucesso em {duration_ms} ms!\n\n"
                f"• Tamanho anterior: {before_kb} KB\n"
                f"• Tamanho atual: {after_kb} KB\n"
                f"• Espaço libertado: {freed_kb} KB\n"
                f"• Estado de integridade: {result['status']}"
            )
            QMessageBox.information(self, APP_TITLE, msg)
            self.refresh()
            self.app.notify_data_changed("Base de dados compactada e otimizada.")
        except Exception as exc:
            log_exception("Failed to optimize database", exc)
            QMessageBox.warning(
                self, APP_TITLE, f"Não foi possível otimizar a base de dados:\n{exc}"
            )

    @staticmethod
    def _clear_layout(layout: QGridLayout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _entity_key(self) -> str:
        return str(self.entity_type.currentData() or "account")

    def _entity_label(self, row: dict[str, Any]) -> str:
        name = str(row.get("name") or "Sem nome")
        usage = int(row.get("usage_count", 0) or 0)
        if self._entity_key() == "category":
            kind = "Receita" if row.get("kind") == "income" else "Despesa"
            return f"{name} · {kind} · {usage} uso(s)"
        return f"{name} · {usage} movimento(s)"

    def _reload_entities(self, _index: int = -1) -> None:
        entity_type = self._entity_key()
        rows = [dict(row) for row in self.repo.entity_identity_rows(entity_type)]
        self._rows_by_id = {str(row["stable_id"]): row for row in rows}
        previous_source = str(self.source.currentData() or "")
        previous_target = str(self.target.currentData() or "")
        for combo in (self.source, self.target):
            combo.blockSignals(True)
            combo.clear()
            combo.addItem("— Selecionar —", "")
            for row in rows:
                combo.addItem(self._entity_label(row), str(row["stable_id"]))
            combo.blockSignals(False)
        source_index = self.source.findData(previous_source)
        target_index = self.target.findData(previous_target)
        self.source.setCurrentIndex(max(0, source_index))
        self.target.setCurrentIndex(max(0, target_index))
        self._fill_suggestions()
        self._selection_changed()

    def _selection_changed(self, _index: int = -1) -> None:
        self._last_preview = {}
        source_id = str(self.source.currentData() or "")
        target_id = str(self.target.currentData() or "")
        valid = bool(source_id and target_id and source_id != target_id)
        self.preview_btn.setEnabled(valid)
        self.merge_btn.setEnabled(False)
        if not valid:
            self.preview_text.setText(
                "Seleciona uma origem e um destino diferentes. A origem será "
                "preservada como alias inativo."
            )

    def _fill_suggestions(self) -> None:
        suggestions = self.repo.identity_merge_suggestions(self._entity_key())
        self.suggestions.setSortingEnabled(False)
        self.suggestions.setRowCount(len(suggestions))
        for row_index, suggestion in enumerate(suggestions):
            source_id = str(suggestion["source_id"])
            target_id = str(suggestion["target_id"])
            source = self._rows_by_id.get(source_id, {})
            target = self._rows_by_id.get(target_id, {})
            values = (
                f"{int(suggestion['similarity_pct'])}%",
                str(suggestion["source_name"]),
                str(suggestion["target_name"]),
                f"{int(source.get('usage_count', 0) or 0)} + "
                f"{int(target.get('usage_count', 0) or 0)} ligações",
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.UserRole, (source_id, target_id))
                if column == 0:
                    item.setForeground(QColor(PURPLE))
                    item.setTextAlignment(Qt.AlignCenter)
                self.suggestions.setItem(row_index, column, item)
        self.suggestions.setSortingEnabled(True)

    def _use_suggestion(self, item: QTableWidgetItem | None) -> None:
        # BUG-UI-04: clicking empty table area emits with a None item; guard it.
        if item is None:
            return
        data = item.data(Qt.UserRole)
        if not isinstance(data, (tuple, list)) or len(data) != 2:
            return
        source_index = self.source.findData(str(data[0]))
        target_index = self.target.findData(str(data[1]))
        if source_index >= 0 and target_index >= 0:
            self.source.setCurrentIndex(source_index)
            self.target.setCurrentIndex(target_index)
            self.preview_merge()

    def preview_merge(self) -> None:
        try:
            preview = self.repo.identity_merge_preview(
                self._entity_key(),
                str(self.source.currentData() or ""),
                str(self.target.currentData() or ""),
            )
        except Exception as exc:
            self._last_preview = {}
            self.merge_btn.setEnabled(False)
            QMessageBox.warning(self, APP_TITLE, str(exc))
            return
        self._last_preview = preview
        source = preview["source"]
        target = preview["target"]
        affected = [
            f"{count} {_AFFECTED_LABELS.get(table, table)}"
            for table, count in preview["affected"].items()
            if int(count or 0) > 0
        ]
        impact = " · ".join(affected) or "nenhuma ligação financeira"
        warnings = list(preview.get("warnings") or [])
        warning_html = (
            "<br><span style='color:#C66A00;'>⚠️ "
            + " ".join(str(value) for value in warnings)
            + "</span>"
            if warnings
            else ""
        )
        self.preview_text.setText(
            f"<b>{source['name']}</b> → <b>{target['name']}</b><br>"
            f"Serão religados: {impact}. O registo de origem, o nome antigo e a "
            f"auditoria da fusão permanecem guardados.{warning_html}"
        )
        self.merge_btn.setEnabled(True)

    def merge_selected(self) -> None:
        if not self._last_preview:
            self.preview_merge()
        if not self._last_preview:
            return
        source = self._last_preview["source"]
        target = self._last_preview["target"]
        answer = QMessageBox.question(
            self,
            "Confirmar fusão segura",
            f"Fundir «{source['name']}» em «{target['name']}»?\n\n"
            "Os movimentos e documentos não serão apagados. A origem fica "
            "preservada no histórico e como alias do destino.",
            QMessageBox.Yes | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        if answer != QMessageBox.Yes:
            return
        try:
            result = self.repo.merge_financial_identities(
                self._entity_key(),
                str(source["stable_id"]),
                str(target["stable_id"]),
            )
        except Exception as exc:
            QMessageBox.critical(
                self,
                APP_TITLE,
                "A fusão foi revertida por completo. Nenhum dado ficou a meio.\n\n"
                + str(exc),
            )
            return
        self._last_preview = {}
        self.app.notify_data_changed(
            f"«{result['source']['name']}» foi fundido em "
            f"«{result['target']['name']}» sem eliminar o histórico."
        )

    def _fill_history(self) -> None:
        rows = self.repo.identity_merge_history()
        self.history.setSortingEnabled(False)
        self.history.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            try:
                details = json.loads(str(row.get("affected_json") or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                details = {}
            total = int(details.get("affected_total", 0) or 0)
            values = (
                str(row.get("created_at") or "").replace("T", " ")[:19],
                "Conta" if row.get("entity_type") == "account" else "Categoria",
                str(row.get("source_name") or ""),
                str(row.get("target_name") or ""),
                str(total),
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 4:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self.history.setItem(row_index, column, item)
        self.history.setSortingEnabled(True)

    def refresh(self) -> None:
        summary = self.repo.identity_integrity_summary()
        self._clear_layout(self.summary_grid)
        metrics = (
            (
                "Contas",
                str(summary["active_accounts"]),
                "identidades ativas",
                BLUE,
                "🏦",
            ),
            (
                "Categorias",
                str(summary["active_categories"]),
                "identidades ativas",
                PURPLE,
                "🏷️",
            ),
            ("Ativos", str(summary["assets"]), "IDs estáveis", GREEN, "📈"),
            (
                "Transferências",
                str(summary["linked_transfers"]),
                "grupos ligados",
                BLUE,
                "↔️",
            ),
            (
                "Integridade",
                "OK" if not summary["problems"] else str(summary["problems"]),
                "ligações por rever" if summary["problems"] else "sem ligações órfãs",
                GREEN if not summary["problems"] else ORANGE,
                "✅" if not summary["problems"] else "⚠️",
            ),
        )
        for index, metric in enumerate(metrics):
            self.summary_grid.addWidget(MetricCard(*metric), 0, index)

        self._clear_layout(self.maintenance_grid)
        try:
            stats = self.repo.database_storage_stats()
            total_kb = round(stats["total_size_bytes"] / 1024.0, 1)
            wal_kb = round(stats["wal_size_bytes"] / 1024.0, 1)
            storage_metrics = (
                (
                    "Tamanho BD",
                    f"{total_kb} KB"
                    if total_kb < 1024
                    else f"{round(total_kb / 1024.0, 2)} MB",
                    "em disco",
                    BLUE,
                    "💾",
                ),
                (
                    "WAL Buffer",
                    f"{wal_kb} KB",
                    "páginas em cache",
                    PURPLE,
                    "⚡",
                ),
                (
                    "Movimentos",
                    str(stats["total_transactions"]),
                    f"{stats['total_splits']} divisões",
                    GREEN,
                    "📝",
                ),
                (
                    "Snapshots & Anexos",
                    f"{stats['total_snapshots']} / {stats['total_attachments']}",
                    "registos históricos",
                    ORANGE,
                    "📦",
                ),
            )
            for index, metric in enumerate(storage_metrics):
                self.maintenance_grid.addWidget(MetricCard(*metric), 0, index)
        except Exception as exc:
            log_exception(
                "Failed to load database storage stats in DataManagementPage", exc
            )

        self._reload_entities()
        self._fill_history()


__all__ = ["DataManagementPage"]
