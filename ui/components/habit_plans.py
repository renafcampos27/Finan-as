"""Compact UI for creating and following habit-change plans."""

from __future__ import annotations

from datetime import date
from typing import Any, Mapping, Optional

from PySide6.QtCore import QDate, Signal, Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QTableWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from services.habit_plans import SCENARIO_LABELS, build_habit_plan_tracking
from ui.components.cards import GlassCard
from ui.components.charts.habit_plan import HabitPlanTrackingChart
from ui.core import FlowLayout, prepare_date_edit
from ui.core.smart_table import SmartTableWidgetItem as QTableWidgetItem
from utils.helpers import eur, safe_float


def _row_dict(row: Any) -> dict[str, Any]:
    if isinstance(row, Mapping):
        return dict(row)
    try:
        return {key: row[key] for key in row.keys()}
    except (AttributeError, KeyError, TypeError):
        return {}


def _set_tone(widget: QWidget, tone: str) -> None:
    widget.setProperty("tone", str(tone or "neutral"))
    style = widget.style()
    if style is not None:
        style.unpolish(widget)
        style.polish(widget)
    widget.update()


class HabitPlanDialog(QDialog):
    """Small, non-technical confirmation dialog for a plan snapshot."""

    def __init__(
        self,
        *,
        habit_name: str,
        scenario_key: str,
        planned_monthly_savings: float,
        goals: list[dict[str, Any]],
        selected_goal_id: int = 0,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        scenario_label = SCENARIO_LABELS.get(scenario_key, "Plano personalizado")
        self.setWindowTitle("Guardar plano de mudança")
        self.setModal(True)
        self.resize(520, 390)
        self.setMinimumSize(470, 350)
        self.setMaximumSize(620, 470)

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(10)
        title = QLabel("🎯 Transformar a simulação num plano")
        title.setObjectName("DialogTitle")
        root.addWidget(title)
        intro = QLabel(
            f"{habit_name} · {scenario_label} · previsão de libertar "
            f"{eur(planned_monthly_savings)}/mês. A app irá comparar o limite "
            "planeado com os movimentos reais que forem sendo registados."
        )
        intro.setObjectName("Subtle")
        intro.setWordWrap(True)
        root.addWidget(intro)

        form = QFormLayout()
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(9)
        self.name_edit = QLineEdit(f"Plano {habit_name} · {scenario_label}")
        self.name_edit.setMaxLength(100)
        self.name_edit.setClearButtonEnabled(True)
        form.addRow("Nome", self.name_edit)

        self.start_date = QDateEdit()
        self.start_date.setDisplayFormat("dd/MM/yyyy")
        self.start_date.setDate(QDate.currentDate())
        self.start_date.setMinimumDate(QDate.currentDate().addYears(-2))
        self.start_date.setMaximumDate(QDate.currentDate().addYears(2))
        prepare_date_edit(self.start_date)
        form.addRow("Começar em", self.start_date)

        self.goal_combo = QComboBox()
        self.goal_combo.addItem("Sem objetivo associado", 0)
        for goal in goals:
            goal_id = int(safe_float(goal.get("id")))
            if goal_id > 0:
                self.goal_combo.addItem(str(goal.get("name") or "Objetivo"), goal_id)
        selected_index = self.goal_combo.findData(int(selected_goal_id or 0))
        self.goal_combo.setCurrentIndex(max(0, selected_index))
        form.addRow("Objetivo", self.goal_combo)

        self.notes_edit = QTextEdit()
        self.notes_edit.setPlaceholderText(
            "Opcional: motivo, regra pessoal ou recompensa por cumprir…"
        )
        self.notes_edit.setMaximumHeight(82)
        form.addRow("Nota", self.notes_edit)
        root.addLayout(form)

        note = QLabel(
            "🔒 O plano guarda uma fotografia dos valores e das regras de deteção. "
            "Não altera nem elimina movimentos."
        )
        note.setObjectName("Subtle")
        note.setWordWrap(True)
        root.addWidget(note)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        save = buttons.button(QDialogButtonBox.Save)
        if save is not None:
            save.setText("Guardar e acompanhar")
            save.setObjectName("Primary")
        cancel = buttons.button(QDialogButtonBox.Cancel)
        if cancel is not None:
            cancel.setText("Cancelar")
        buttons.accepted.connect(self._accept_if_valid)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _accept_if_valid(self) -> None:
        if not self.name_edit.text().strip():
            self.name_edit.setFocus()
            return
        self.accept()

    def values(self) -> dict[str, Any]:
        return {
            "name": self.name_edit.text().strip(),
            "start_date": self.start_date.date().toString("yyyy-MM-dd"),
            "goal_id": int(safe_float(self.goal_combo.currentData())) or None,
            "notes": self.notes_edit.toPlainText().strip(),
        }


class HabitPlansCard(GlassCard):
    """Saved plan selector, live status, chart and monthly evidence."""

    plans_changed = Signal(int)

    def __init__(self, app: Any, parent: Optional[QWidget] = None) -> None:
        super().__init__(
            "📈 Acompanhamento dos planos · planeado vs. real",
            parent,
            strong=True,
            accent="#30D158",
        )
        self.app = app
        self.repo = app.repo
        self.analytics_service = app.analytics_service
        self.setProperty("pageCardKey", "habit_change_plans")
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        self._plans: dict[int, dict[str, Any]] = {}
        self._tracking: dict[str, Any] = {}
        self._updating = False

        intro = QLabel(
            "A app compara o limite acumulado do plano com o custo real dos movimentos "
            "detetados. Passa pelo gráfico para consultar cada mês."
        )
        intro.setObjectName("Subtle")
        intro.setWordWrap(True)
        self.v.addWidget(intro)

        toolbar = FlowLayout(margin=0, hspacing=8, vspacing=8)
        self.plan_combo = QComboBox()
        self.plan_combo.setMinimumWidth(290)
        self.plan_combo.setAccessibleName("Plano acompanhado")
        toolbar.addWidget(self.plan_combo)
        self.status_badge = QLabel("Sem planos")
        self.status_badge.setObjectName("MetricStatus")
        self.status_badge.setProperty("tone", "neutral")
        toolbar.addWidget(self.status_badge)
        self.toggle_btn = QPushButton("Pausar")
        self.toggle_btn.setObjectName("CardAction")
        toolbar.addWidget(self.toggle_btn)
        self.complete_btn = QPushButton("Concluir")
        self.complete_btn.setObjectName("CardAction")
        toolbar.addWidget(self.complete_btn)
        self.delete_btn = QPushButton("Eliminar")
        self.delete_btn.setObjectName("CardAction")
        toolbar.addWidget(self.delete_btn)
        self.v.addLayout(toolbar)

        self.empty_panel = QFrame()
        self.empty_panel.setObjectName("StatusBanner")
        self.empty_panel.setProperty("tone", "neutral")
        empty_layout = QHBoxLayout(self.empty_panel)
        empty_layout.setContentsMargins(14, 12, 14, 12)
        empty_icon = QLabel("🧭")
        empty_icon.setObjectName("StatusBannerIcon")
        empty_icon.setAlignment(Qt.AlignCenter)
        empty_icon.setFixedSize(42, 42)
        empty_text = QLabel(
            "Ainda não há planos guardados. Volta a “Simular”, compara −25%, metade, "
            "uma alternativa ou eliminar e escolhe “Guardar como plano”."
        )
        empty_text.setObjectName("StatusBannerDescription")
        empty_text.setWordWrap(True)
        empty_layout.addWidget(empty_icon)
        empty_layout.addWidget(empty_text, 1)
        self.v.addWidget(self.empty_panel)

        self.content = QWidget()
        content_layout = QVBoxLayout(self.content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(8)

        self.metrics_grid = QGridLayout()
        self.metrics_grid.setContentsMargins(0, 0, 0, 0)
        self.metrics_grid.setHorizontalSpacing(8)
        self.metrics_grid.setVerticalSpacing(8)
        self.metric_labels: dict[str, tuple[QFrame, QLabel, QLabel]] = {}
        for index, (key, title, tone) in enumerate(
            (
                ("planned", "Limite até hoje", "neutral"),
                ("actual", "Realizado até hoje", "warning"),
                ("saved", "Poupança real", "positive"),
            )
        ):
            frame, value, detail = self._metric(title, tone)
            self.metric_labels[key] = (frame, value, detail)
            self.metrics_grid.addWidget(frame, 0, index)
        content_layout.addLayout(self.metrics_grid)

        self.summary_banner = QFrame()
        self.summary_banner.setObjectName("StatusBanner")
        summary_layout = QHBoxLayout(self.summary_banner)
        summary_layout.setContentsMargins(13, 10, 13, 10)
        summary_layout.setSpacing(10)
        self.summary_icon = QLabel("📍")
        self.summary_icon.setObjectName("StatusBannerIcon")
        self.summary_icon.setAlignment(Qt.AlignCenter)
        self.summary_icon.setFixedSize(42, 42)
        self.summary_text = QLabel()
        self.summary_text.setObjectName("StatusBannerDescription")
        self.summary_text.setWordWrap(True)
        summary_layout.addWidget(self.summary_icon)
        summary_layout.addWidget(self.summary_text, 1)
        content_layout.addWidget(self.summary_banner)

        self.chart = HabitPlanTrackingChart(lambda: self.app.theme)
        content_layout.addWidget(self.chart)

        table_title = QLabel("Últimos meses observados")
        table_title.setObjectName("SimulationSectionTitle")
        content_layout.addWidget(table_title)
        self.table = QTableWidget(0, 6)
        self.table.setProperty("columnPersistenceId", "simulations.habit_plans")
        self.table.setObjectName("HabitPlanTrackingTable")
        self.table.setHorizontalHeaderLabels(
            [
                "Mês",
                "Planeado",
                "Realizado",
                "Diferença",
                "Poup. prevista",
                "Poup. real",
            ]
        )
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(40)
        self.table.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        header = self.table.horizontalHeader()
        header.setStretchLastSection(True)
        for column in range(6):
            header.setSectionResizeMode(column, QHeaderView.Interactive)
        self.table.setColumnWidth(0, 100)
        for column in range(1, 6):
            self.table.setColumnWidth(column, 145)
        content_layout.addWidget(self.table)

        self.notes_label = QLabel()
        self.notes_label.setObjectName("Subtle")
        self.notes_label.setWordWrap(True)
        content_layout.addWidget(self.notes_label)
        self.v.addWidget(self.content)

        self.plan_combo.currentIndexChanged.connect(self._selection_changed)
        self.toggle_btn.clicked.connect(self._toggle_status)
        self.complete_btn.clicked.connect(self._complete)
        self.delete_btn.clicked.connect(self._delete)
        self._show_empty(True)

    @staticmethod
    def _metric(title: str, tone: str) -> tuple[QFrame, QLabel, QLabel]:
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

    def _show_empty(self, empty: bool) -> None:
        self.empty_panel.setVisible(empty)
        self.content.setVisible(not empty)
        for widget in (
            self.plan_combo,
            self.toggle_btn,
            self.complete_btn,
            self.delete_btn,
        ):
            widget.setEnabled(not empty)
        if empty:
            self.status_badge.setText("Sem planos")
            _set_tone(self.status_badge, "neutral")

    def selected_plan_id(self) -> int:
        return int(safe_float(self.plan_combo.currentData()))

    def plan_count(self) -> int:
        return len(self._plans)

    def refresh(self, *, select_id: Optional[int] = None) -> None:
        preferred = int(select_id or self.selected_plan_id() or 0)
        rows = [_row_dict(row) for row in self.repo.habit_change_plans()]
        self._plans = {
            int(safe_float(row.get("id"))): row
            for row in rows
            if int(safe_float(row.get("id"))) > 0
        }
        self._updating = True
        self.plan_combo.blockSignals(True)
        self.plan_combo.clear()
        for row in rows:
            identifier = int(safe_float(row.get("id")))
            status = str(row.get("status") or "active")
            suffix = {"paused": " · pausado", "completed": " · concluído"}.get(
                status, ""
            )
            self.plan_combo.addItem(f"{row.get('name') or 'Plano'}{suffix}", identifier)
        index = self.plan_combo.findData(preferred)
        self.plan_combo.setCurrentIndex(index if index >= 0 else (0 if rows else -1))
        self.plan_combo.blockSignals(False)
        self._updating = False
        self._show_empty(not rows)
        self.plans_changed.emit(len(rows))
        if rows:
            self._refresh_selected()

    def _selection_changed(self, _index: int = -1) -> None:
        if not self._updating:
            self._refresh_selected()

    def _refresh_selected(self) -> None:
        plan = self._plans.get(self.selected_plan_id())
        if plan is None:
            self._show_empty(True)
            return
        start = str(plan.get("start_date") or date.today().isoformat())[:10]
        context = self.analytics_service.habit_transactions_for_period(
            start,
            date.today().isoformat(),
        )
        tracking = build_habit_plan_tracking(
            plan,
            list(context.get("transactions") or []),
        )
        self._tracking = tracking
        privacy = bool(self.app.theme.privacy)
        tone = str(tracking.get("tone") or "neutral")
        status = str(tracking.get("status") or "Sem estado")
        self.status_badge.setText(status)
        _set_tone(self.status_badge, tone)
        _set_tone(self.summary_banner, tone)
        _set_tone(self.summary_icon, tone)

        planned = safe_float(tracking.get("planned_total"))
        actual = safe_float(tracking.get("actual_total"))
        variance = safe_float(tracking.get("variance_total"))
        planned_savings = safe_float(tracking.get("planned_savings_total"))
        actual_savings = safe_float(tracking.get("actual_savings_total"))
        count = int(tracking.get("transaction_count", 0) or 0)
        values = {
            "planned": (
                eur(planned, privacy),
                f"poupança prevista {eur(planned_savings, privacy)}",
            ),
            "actual": (
                eur(actual, privacy),
                f"{eur(abs(variance), privacy)} {'acima' if variance > 0 else 'abaixo'} do limite",
            ),
            "saved": (
                eur(actual_savings, privacy),
                f"{count} movimento(s) contabilizado(s)",
            ),
        }
        for key, (value_text, detail_text) in values.items():
            _frame, value, detail = self.metric_labels[key]
            value.setText(value_text)
            detail.setText(detail_text)
        _set_tone(
            self.metric_labels["actual"][0],
            "positive"
            if variance <= 0
            else ("warning" if tone == "warning" else "negative"),
        )
        _set_tone(
            self.metric_labels["saved"][0],
            "positive" if actual_savings >= 0 else "negative",
        )

        goal_text = (
            f" · associado a «{tracking.get('goal_name')}»"
            if tracking.get("goal_name")
            else ""
        )
        if not tracking.get("rows"):
            message = (
                f"O plano começa em {start}; ainda não existe um período para medir."
            )
        elif variance <= 0:
            message = (
                f"Estás {eur(abs(variance), privacy)} abaixo do limite acumulado{goal_text}. "
                "Mantém o ritmo e continua a registar os movimentos normalmente."
            )
        else:
            message = (
                f"O custo real está {eur(variance, privacy)} acima do limite acumulado{goal_text}. "
                "Revê os movimentos usados ou ajusta o comportamento nas próximas ocorrências."
            )
        self.summary_text.setText(message)
        self.chart.set_data(tracking)
        self._fill_table(list(tracking.get("rows") or []))

        status_value = str(plan.get("status") or "active")
        self.toggle_btn.setText("Retomar" if status_value == "paused" else "Pausar")
        self.toggle_btn.setEnabled(status_value != "completed")
        self.complete_btn.setEnabled(status_value != "completed")
        notes = str(plan.get("notes") or "").strip()
        scenario = SCENARIO_LABELS.get(
            str(plan.get("scenario_key") or ""), "Plano personalizado"
        )
        self.notes_label.setText(
            f"📌 {scenario} · início {start}" + (f" · {notes}" if notes else "")
        )
        self.notes_label.setVisible(True)
        self._sync_height()

    def _fill_table(self, rows: list[dict[str, Any]]) -> None:
        visible_rows = rows[-12:]
        privacy = bool(self.app.theme.privacy)
        self.table.clearContents()
        self.table.setRowCount(len(visible_rows))
        for row_index, row in enumerate(visible_rows):
            variance = safe_float(row.get("variance"))
            values = (
                str(row.get("label") or "—") + (" *" if row.get("partial") else ""),
                eur(row.get("planned_cost", 0), privacy),
                eur(row.get("actual_cost", 0), privacy),
                # BUG-HABIT-02: with privacy on, the "+" prefix would leak the
                # sign of the variance outside the masked value.
                (("" if privacy else "+") if variance > 0 else "")
                + eur(variance, privacy),
                eur(row.get("planned_savings", 0), privacy),
                eur(row.get("actual_savings", 0), privacy),
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column > 0:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self.table.setItem(row_index, column, item)
            self.table.setRowHeight(row_index, 40)
        table_height = 48 + max(1, len(visible_rows)) * 40 + 4
        self.table.setMinimumHeight(table_height)
        self.table.setMaximumHeight(table_height)

    def _sync_height(self) -> None:
        self.adjustSize()
        self.setMinimumHeight(max(740, self.sizeHint().height() + 20))
        self.updateGeometry()
        sync = getattr(self.app, "sync_content_size", None)
        if callable(sync):
            sync()

    def _toggle_status(self) -> None:
        plan = self._plans.get(self.selected_plan_id())
        if plan is None:
            return
        status = "active" if str(plan.get("status")) == "paused" else "paused"
        self.repo.set_habit_change_plan_status(self.selected_plan_id(), status)
        self.refresh(select_id=self.selected_plan_id())
        self.app.show_toast(
            "Plano retomado." if status == "active" else "Plano pausado.",
            "▶️" if status == "active" else "⏸️",
            title="Acompanhamento atualizado",
            tone="success" if status == "active" else "info",
        )

    def _complete(self) -> None:
        if self.selected_plan_id() <= 0:
            return
        self.repo.set_habit_change_plan_status(self.selected_plan_id(), "completed")
        self.refresh(select_id=self.selected_plan_id())
        self.app.show_toast(
            "O plano ficou guardado no histórico.",
            "🏁",
            title="Plano concluído",
            tone="success",
        )

    def _delete(self) -> None:
        plan = self._plans.get(self.selected_plan_id())
        if plan is None:
            return
        answer = QMessageBox.question(
            self,
            "Eliminar plano",
            f"Eliminar «{plan.get('name') or 'Plano'}»? Os movimentos não serão alterados.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        self.repo.delete_habit_change_plan(self.selected_plan_id())
        self.refresh()
        self.app.show_toast(
            "O plano foi eliminado; os movimentos mantêm-se intactos.",
            "🗑️",
            title="Plano removido",
            tone="info",
        )


__all__ = ["HabitPlanDialog", "HabitPlansCard"]
