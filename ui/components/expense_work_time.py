"""Dashboard card that translates category spending into paid work time."""

from __future__ import annotations

from typing import Any, Callable, Optional

from PySide6.QtCore import QEvent, Signal, Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QGridLayout,
    QHeaderView,
    QLabel,
    QSizePolicy,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)

from ui.components.cards import GlassCard
from ui.core.theme import Theme
from ui.core.smart_table import SmartTableWidgetItem as QTableWidgetItem
from utils.helpers import eur, safe_float


def _decimal(value: float, digits: int = 1) -> str:
    return f"{safe_float(value):.{digits}f}".replace(".", ",")


def _time_text(hours: float, privacy: bool) -> str:
    if privacy:
        return "•••"
    value = max(0.0, safe_float(hours))
    if 0 < value < 1:
        return f"{max(1, round(value * 60))} min"
    return f"{_decimal(value)} h"


def _days_text(days: float, privacy: bool) -> str:
    if privacy:
        return "•••"
    value = max(0.0, safe_float(days))
    return f"{_decimal(value, 2 if value < 1 else 1)} dias"


def _apply_tone(widget: QWidget, tone: str) -> None:
    widget.setProperty("tone", str(tone or "neutral"))
    style = widget.style()
    if style is not None:
        style.unpolish(widget)
        style.polish(widget)
    widget.update()


class _ExpenseWorkTimeComparisonHintProxy(QLabel):
    """Hidden proxy label that forwards comparison text into the card's unified intro."""

    def __init__(self, card: "ExpenseWorkTimeCard") -> None:
        super().__init__(card)
        self._card = card
        self._is_visible = False
        self.setObjectName("Subtle")
        self.setProperty("comparisonContext", True)
        self.resize(0, 0)
        super().setVisible(False)

    def isVisible(self) -> bool:
        return self._is_visible

    def setVisible(self, visible: bool) -> None:
        self._is_visible = bool(visible)
        super().setVisible(False)
        self._card.set_comparison_hint(self.text() if self._is_visible else "")

    def setText(self, text: str) -> None:
        super().setText(text)
        if self._is_visible:
            self._card.set_comparison_hint(text)

    def paintEvent(self, event) -> None:
        pass


class ExpenseWorkTimeCard(GlassCard):
    """All expense categories with visible amount, hours and workdays."""

    category_clicked = Signal(str)
    category_hovered = Signal(str)

    def __init__(
        self,
        theme_provider: Callable[[], Theme],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(
            "⏳ Despesas em tempo de trabalho",
            parent,
            strong=True,
            accent="#FF9F0A",
        )
        self.theme_provider = theme_provider
        self._default_widths_applied = False
        self.setProperty("pageCardKey", "dashboard_expense_work_time")
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        self.set_compact_header(top_margin=7, bottom_margin=8, spacing=4)

        self._intro_default_text = (
            "Quanto tempo do teu trabalho líquido foi necessário para pagar cada categoria."
        )
        self._intro_base_text = self._intro_default_text
        self._comparison_text = ""

        self.intro = QLabel(self._intro_base_text)
        self.intro.setObjectName("Subtle")
        self.intro.setWordWrap(True)
        self.fit_label_height(self.intro, word_wrap=True, stable_measurement=True)
        self.v.addWidget(self.intro)

        self.comparison_hint = _ExpenseWorkTimeComparisonHintProxy(self)

        self.metrics = QGridLayout()
        self.metrics.setContentsMargins(0, 0, 0, 0)
        self.metrics.setHorizontalSpacing(8)
        self.metrics.setVerticalSpacing(4)
        self.metric_values: dict[str, tuple[QFrame, QLabel, QLabel]] = {}
        for index, (key, title, tone) in enumerate(
            (
                ("amount", "Despesas do período", "warning"),
                ("hours", "Horas de trabalho", "neutral"),
                ("days", "Dias de trabalho", "negative"),
            )
        ):
            frame, value, detail = self._metric(title, tone)
            self.metric_values[key] = (frame, value, detail)
            self.metrics.addWidget(frame, 0, index)
        self.v.addLayout(self.metrics)

        self.status = QLabel("A calcular…")
        self.status.setObjectName("MetricStatus")
        self.status.setProperty("tone", "neutral")
        self.status.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
        self.v.addWidget(self.status, 0, Qt.AlignLeft)

        self.table = QTableWidget(0, 4)
        self.table.setProperty("columnPersistenceId", "dashboard.expenses_work_time")
        self.table.setObjectName("ExpenseWorkTimeTable")
        self.table.setHorizontalHeaderLabels(
            ["Categoria", "Valor gasto", "Horas de trabalho", "Dias de trabalho"]
        )
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setMouseTracking(True)
        self.table.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(40)
        header = self.table.horizontalHeader()
        header.setStretchLastSection(False)
        for column in range(4):
            header.setSectionResizeMode(column, QHeaderView.Interactive)
        self.table.setColumnWidth(0, 300)
        self.table.setColumnWidth(1, 170)
        self.table.setColumnWidth(2, 190)
        self.table.setColumnWidth(3, 180)
        self.table.cellClicked.connect(self._emit_category)
        self.table.cellEntered.connect(self._emit_hover_category)
        self.table.viewport().installEventFilter(self)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._show_table_context_menu)
        self.v.addWidget(self.table)

        self.empty = QLabel("Ainda não existem despesas neste período.")
        self.empty.setObjectName("Subtle")
        self.empty.setAlignment(Qt.AlignCenter)
        self.empty.setMinimumHeight(70)
        self.empty.hide()
        self.v.addWidget(self.empty)

        self.method = QLabel()
        self.method.setObjectName("Subtle")
        self.method.setWordWrap(True)
        self.v.addWidget(self.method)
        self.v.addStretch(1)

    def _show_table_context_menu(self, pos) -> None:
        global_pos = self.table.viewport().mapToGlobal(pos)
        from PySide6.QtGui import QContextMenuEvent

        event = QContextMenuEvent(QContextMenuEvent.Mouse, pos, global_pos)
        self.contextMenuEvent(event)

    def set_comparison_hint(self, text: str) -> None:
        """Update comparison note, joining it seamlessly with the intro text."""
        clean = str(text or "").strip()
        if clean == self._comparison_text:
            return
        self._comparison_text = clean
        self.apply_phrase_preferences()

    def _update_intro_text(self) -> None:
        self.apply_phrase_preferences()

    def available_customization_elements(self) -> dict[str, dict[str, Any]]:
        elements = super().available_customization_elements()
        prefs = self.phrase_preferences()

        if getattr(self, "metrics", None) is not None:
            elements["work_metrics"] = {
                "key": "show_work_metrics",
                "title": "Bloco de horas e dias de trabalho",
                "preview": "Total de horas e dias necessários",
                "icon": "⏱️",
                "enabled": bool(prefs.get("show_work_metrics", True)),
                "supports_text": False,
            }

        if getattr(self, "status", None) is not None:
            elements["metric_status"] = {
                "key": "show_status",
                "title": "Mensagem de estado",
                "preview": self.status.text(),
                "icon": "💬",
                "enabled": bool(prefs.get("show_status", True)),
                "supports_text": False,
            }

        return elements

    def _apply_custom_phrase_elements(self, prefs: dict[str, Any]) -> None:
        show_desc = bool(prefs.get("show_description", True))
        custom_desc = str(prefs.get("custom_description", "") or "").strip()
        show_comp = bool(prefs.get("show_comparison", True))
        show_foot = bool(prefs.get("show_footer", True))

        base_text = custom_desc if custom_desc else self._intro_default_text
        self._intro_base_text = base_text

        has_comparison = bool(self._comparison_text) and show_comp
        if not show_desc:
            if has_comparison:
                self.intro.setText(self._comparison_text)
                self.intro.setVisible(True)
            else:
                self.intro.setVisible(False)
        else:
            if has_comparison:
                self.intro.setText(f"{base_text} · {self._comparison_text}")
            else:
                self.intro.setText(base_text)
            self.intro.setVisible(True)

        self.fit_label_height(self.intro, word_wrap=True, stable_measurement=True)

        if getattr(self, "metrics", None) is not None:
            show_metrics = bool(prefs.get("show_work_metrics", True))
            for i in range(self.metrics.count()):
                item = self.metrics.itemAt(i)
                if item is not None and item.widget() is not None:
                    item.widget().setVisible(show_metrics)

        if getattr(self, "status", None) is not None:
            show_status = bool(prefs.get("show_status", True))
            self.status.setVisible(show_status and bool(self.status.text().strip()))

        if getattr(self, "method", None) is not None:
            self.method.setVisible(show_foot and bool(self.method.text()))

    @staticmethod
    def _metric(title: str, tone: str) -> tuple[QFrame, QLabel, QLabel]:
        frame = QFrame()
        frame.setObjectName("SimulationStat")
        frame.setProperty("tone", tone)
        frame.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(2)
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

    def _emit_category(self, row: int, _column: int) -> None:
        item = self.table.item(row, 0)
        if item is not None:
            category = str(item.data(Qt.UserRole) or "")
            if category:
                self.category_clicked.emit(category)

    def _emit_hover_category(self, row: int, _column: int) -> None:
        item = self.table.item(row, 0)
        self.category_hovered.emit(
            str(item.data(Qt.UserRole) or "") if item is not None else ""
        )

    def eventFilter(self, watched, event):  # type: ignore[override]
        if watched is self.table.viewport() and event.type() == QEvent.Leave:
            self.category_hovered.emit("")
        return super().eventFilter(watched, event)

    def set_highlighted_category(self, category: str) -> None:
        selected = " ".join(str(category or "").casefold().split())
        if not selected:
            self.table.clearSelection()
            return
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            value = (
                " ".join(str(item.data(Qt.UserRole) or "").casefold().split())
                if item is not None
                else ""
            )
            if value == selected:
                self.table.selectRow(row)
                return
        self.table.clearSelection()

    def _has_saved_column_widths(self) -> bool:
        key = str(self.table.property("columnPersistenceKey") or "").strip()
        current: QWidget | None = self
        while key and current is not None:
            repo = getattr(current, "repo", None)
            if repo is not None:
                try:
                    saved = str(repo.setting(key, "") or "").strip()
                    return saved not in {"", "{}"}
                except Exception:
                    return False
            current = current.parentWidget()
        return False

    def _fit_default_columns(self) -> None:
        """Fill the card once, without overwriting widths chosen by the user."""
        if self._default_widths_applied or self._has_saved_column_widths():
            self._default_widths_applied = True
            return
        available = max(
            0,
            self.table.viewport().width(),
            self.width()
            - self.v.contentsMargins().left()
            - self.v.contentsMargins().right()
            - 4,
        )
        if available < 520:
            return
        minimums = (180, 125, 150, 135)
        ratios = (0.36, 0.20, 0.23)
        widths = [
            max(minimums[index], round(available * ratio))
            for index, ratio in enumerate(ratios)
        ]
        widths.append(max(minimums[3], available - sum(widths)))
        restoring = bool(self.table.property("columnPersistenceRestoring"))
        self.table.setProperty("columnPersistenceRestoring", True)
        try:
            for column, width in enumerate(widths):
                self.table.horizontalHeader().resizeSection(column, width)
        finally:
            self.table.setProperty("columnPersistenceRestoring", restoring)
        self._default_widths_applied = True

    def set_data(
        self,
        snapshot: dict[str, Any],
        *,
        period_label: str,
        income_reference: str,
        context_months: int,
        weekly_hours: float,
    ) -> None:
        theme = self.theme_provider()
        privacy = bool(theme.privacy)
        available = bool(snapshot.get("available"))
        rows = list(snapshot.get("rows") or [])
        total_amount = safe_float(snapshot.get("total_amount"))
        total_hours = safe_float(snapshot.get("total_hours"))
        total_days = safe_float(snapshot.get("total_days"))
        hourly_value = safe_float(snapshot.get("hourly_value"))

        values = {
            "amount": (
                eur(total_amount, privacy),
                str(period_label or "período selecionado"),
            ),
            "hours": (
                _time_text(total_hours, privacy) if available else "—",
                f"a {eur(hourly_value, privacy)}/hora"
                if available
                else "sem salário líquido",
            ),
            "days": (
                _days_text(total_days, privacy) if available else "—",
                f"{_decimal(snapshot.get('daily_hours', 8))} h por dia útil",
            ),
        }
        for key, (value_text, detail_text) in values.items():
            _frame, value, detail = self.metric_values[key]
            value.setText(value_text)
            detail.setText(detail_text)

        if available:
            self.status.setText(f"⏱️ {len(rows)} categoria(s) convertida(s) em tempo")
            _apply_tone(self.status, "neutral")
        else:
            self.status.setText("⚠️ Regista o salário líquido para calcular o tempo")
            _apply_tone(self.status, "warning")

        self.table.clearContents()
        self.table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            category = str(row.get("category") or "Sem categoria")
            values = (
                category,
                eur(row.get("amount", 0), privacy),
                _time_text(row.get("hours", 0), privacy) if available else "—",
                _days_text(row.get("days", 0), privacy) if available else "—",
            )
            for column, text in enumerate(values):
                item = QTableWidgetItem(str(text))
                item.setData(Qt.UserRole, category)
                if column > 0:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self.table.setItem(row_index, column, item)
            self.table.setRowHeight(row_index, 40)

        has_rows = bool(rows)
        self.table.setVisible(has_rows)
        self.empty.setVisible(not has_rows)
        table_height = 48 + len(rows) * 40 + 4
        self.table.setMinimumHeight(table_height if has_rows else 0)
        self.table.setMaximumHeight(table_height if has_rows else 0)
        reference = str(income_reference or "salário")
        self.method.setText(
            f"Base: {reference} líquido médio de {max(0, int(context_months))} mês(es) · "
            f"jornada de {_decimal(weekly_hours)} h/semana · valores do {period_label}. "
            "Clica numa categoria para abrir os respetivos movimentos."
        )
        prefs = self.phrase_preferences()
        self.method.setVisible(
            bool(prefs.get("show_footer", True)) and bool(self.method.text())
        )
        self._apply_custom_phrase_elements(prefs)
        self.setAccessibleDescription(
            f"{len(rows)} categorias; total {eur(total_amount, privacy)}; "
            + (
                f"equivalente a {_time_text(total_hours, privacy)}."
                if available
                else "sem referência salarial."
            )
        )
        self._fit_default_columns()
        self.setMinimumHeight(max(250, self.sizeHint().height() + 18))
        self.updateGeometry()


__all__ = ["ExpenseWorkTimeCard"]
