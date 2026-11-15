"""Responsive card for explainable financial priorities."""

from __future__ import annotations

import sys
from datetime import date
from typing import Any

from utils.qt_runtime import configure_qt_runtime

configure_qt_runtime()

try:
    from PySide6.QtCore import Signal, Qt
    from PySide6.QtWidgets import (
        QFrame,
        QHBoxLayout,
        QLabel,
        QPushButton,
        QSizePolicy,
        QVBoxLayout,
        QWidget,
    )
except Exception as exc:  # pragma: no cover - only used when PySide6 is missing.
    print("PySide6 não está instalado. Instala com: pip install PySide6")
    print(exc)
    sys.exit(1)

from ui.components.cards import GlassCard
from ui.core.layouts import FlowLayout
from utils.helpers import eur, percent, safe_float


def _repolish(widget: QWidget) -> None:
    style = widget.style()
    if style is not None:
        style.unpolish(widget)
        style.polish(widget)
    widget.update()


def _format_date(value: Any) -> str:
    try:
        parsed = date.fromisoformat(str(value or "")[:10])
    except (TypeError, ValueError):
        return "—"
    return parsed.strftime("%d/%m/%Y")


def _format_metric(metric: dict[str, Any], privacy: bool) -> str:
    kind = str(metric.get("kind") or "money")
    value = metric.get("value")
    if kind == "money":
        return eur(safe_float(value), privacy)
    if kind == "percent":
        return percent(safe_float(value))
    if kind == "months":
        return f"{safe_float(value):.1f} meses".replace(".", ",")
    if kind == "days":
        return f"{max(0, int(safe_float(value)))} dias"
    if kind == "count":
        return str(max(0, int(safe_float(value))))
    if kind == "date":
        return _format_date(value)
    return str(value or "—")


class DecisionPriorityRow(QFrame):
    action_requested = Signal(str, str, str)

    def __init__(
        self,
        priority: dict[str, Any],
        *,
        privacy: bool,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.priority = dict(priority)
        tone = str(priority.get("tone") or "neutral")
        self.setObjectName("DecisionPriorityRow")
        self.setProperty("tone", tone)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(5)

        badges = QHBoxLayout()
        badges.setSpacing(7)
        self.rank_badge = QLabel(
            f"{int(safe_float(priority.get('rank'), 1))} · "
            f"{str(priority.get('urgency') or 'Prioridade').upper()}"
        )
        self.rank_badge.setObjectName("DecisionRank")
        self.rank_badge.setProperty("tone", tone)
        self.rank_badge.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
        badges.addWidget(self.rank_badge)
        badges.addStretch()

        impact_amount = max(0.0, safe_float(priority.get("impact_amount")))
        self.impact_badge = QLabel()
        self.impact_badge.setObjectName("DecisionImpact")
        self.impact_badge.setProperty("tone", tone)
        self.impact_badge.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
        if impact_amount > 0:
            impact_label = str(priority.get("impact_label") or "valor em jogo")
            self.impact_badge.setText(f"{eur(impact_amount, privacy)} · {impact_label}")
            badges.addWidget(self.impact_badge)
        else:
            self.impact_badge.hide()
        layout.addLayout(badges)

        self.scope_label = QLabel(
            f"🗓️  {str(priority.get('scope_label') or 'Período não identificado')}"
        )
        self.scope_label.setObjectName("DecisionScope")
        self.scope_label.setWordWrap(True)
        self.scope_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        layout.addWidget(self.scope_label)

        self.title_label = QLabel(
            f"{priority.get('icon', '')}  {priority.get('title', '')}".strip()
        )
        self.title_label.setObjectName("DecisionPriorityTitle")
        self.title_label.setWordWrap(True)
        self.title_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        layout.addWidget(self.title_label)

        self.message_label = QLabel(str(priority.get("message") or ""))
        self.message_label.setObjectName("DecisionPriorityMessage")
        self.message_label.setWordWrap(True)
        self.message_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        layout.addWidget(self.message_label)

        metrics = [
            f"{metric.get('label', '')}: {_format_metric(metric, privacy)}"
            for metric in priority.get("metrics", [])
            if isinstance(metric, dict)
        ]
        self.metrics_label = QLabel("   ·   ".join(metrics))
        self.metrics_label.setObjectName("DecisionMetrics")
        self.metrics_label.setWordWrap(True)
        self.metrics_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.metrics_label.setVisible(bool(metrics))
        layout.addWidget(self.metrics_label)

        footer = QHBoxLayout()
        footer.setSpacing(9)
        confidence = str(priority.get("confidence") or "initial")
        confidence_tone = (
            "positive"
            if confidence == "high"
            else "warning"
            if confidence == "medium"
            else "neutral"
        )
        self.evidence_label = QLabel(
            f"{priority.get('confidence_label', 'Confiança inicial')} · "
            f"{priority.get('evidence', '')}".rstrip(" ·")
        )
        self.evidence_label.setObjectName("DecisionEvidence")
        self.evidence_label.setProperty("tone", confidence_tone)
        self.evidence_label.setWordWrap(True)
        self.evidence_label.setSizePolicy(
            QSizePolicy.Expanding,
            QSizePolicy.Preferred,
        )
        footer.addWidget(self.evidence_label, 1)

        self.action_button = QPushButton(f"{str(priority.get('action') or 'Abrir')}  →")
        self.action_button.setObjectName("DecisionAction")
        self.action_button.setCursor(Qt.PointingHandCursor)
        self.action_button.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
        self.action_button.clicked.connect(self._request_action)
        footer.addWidget(self.action_button, 0, Qt.AlignBottom)
        layout.addLayout(footer)

        self.setAccessibleName(
            f"Prioridade {priority.get('rank', 1)}: {priority.get('title', '')}"
        )
        self.setAccessibleDescription(
            f"Período: {priority.get('scope_label', 'não identificado')}. "
            f"{priority.get('message', '')}. {'; '.join(metrics)}. "
            f"{priority.get('confidence_label', '')}."
        )

    def _request_action(self) -> None:
        self.action_requested.emit(
            str(self.priority.get("page") or "dashboard"),
            str(self.priority.get("category") or ""),
            str(self.priority.get("search") or ""),
        )


class DecisionBriefingCard(GlassCard):
    action_requested = Signal(str, str, str)

    def __init__(
        self,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__("🧠 Prioridades financeiras", parent, strong=True)
        self.set_compact_header(top_margin=10, bottom_margin=12, spacing=7)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        self._data: dict[str, Any] = {}
        self.priority_rows: list[DecisionPriorityRow] = []

        self.summary_panel = QFrame()
        self.summary_panel.setObjectName("DecisionSummary")
        self.summary_panel.setProperty("tone", "neutral")
        summary_layout = QVBoxLayout(self.summary_panel)
        summary_layout.setContentsMargins(12, 10, 12, 10)
        summary_layout.setSpacing(4)

        self.summary_top_wrap = QWidget()
        self.summary_top_wrap.setSizePolicy(
            QSizePolicy.Expanding,
            QSizePolicy.Fixed,
        )
        summary_top = FlowLayout(
            self.summary_top_wrap,
            margin=0,
            hspacing=7,
            vspacing=4,
        )
        self.summary_eyebrow = QLabel("PRÓXIMA MELHOR DECISÃO")
        self.summary_eyebrow.setObjectName("DecisionEyebrow")
        summary_top.addWidget(self.summary_eyebrow)
        self.coverage_badge = QLabel()
        self.coverage_badge.setObjectName("DecisionCoverage")
        self.coverage_badge.setProperty("tone", "neutral")
        self.coverage_badge.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
        summary_top.addWidget(self.coverage_badge)
        summary_layout.addWidget(self.summary_top_wrap)
        self.fit_widget_height(self.summary_top_wrap)

        self.summary_title = QLabel()
        self.summary_title.setObjectName("DecisionSummaryTitle")
        self.summary_title.setWordWrap(True)
        self.summary_title.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        summary_layout.addWidget(self.summary_title)
        self.fit_label_height(
            self.summary_title,
            word_wrap=True,
            stable_measurement=True,
        )

        self.summary_meta = QLabel()
        self.summary_meta.setObjectName("DecisionSummaryMeta")
        self.summary_meta.setWordWrap(True)
        summary_layout.addWidget(self.summary_meta)
        self.fit_label_height(
            self.summary_meta,
            word_wrap=True,
            stable_measurement=True,
        )

        self.scope_summary = QLabel()
        self.scope_summary.setObjectName("DecisionScopeSummary")
        self.scope_summary.setWordWrap(True)
        self.scope_summary.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        summary_layout.addWidget(self.scope_summary)
        self.fit_label_height(
            self.scope_summary,
            word_wrap=True,
            stable_measurement=True,
        )
        self.v.addWidget(self.summary_panel)

        self.rows_host = QWidget()
        self.rows_host.setObjectName("DecisionRows")
        self.rows_layout = QVBoxLayout(self.rows_host)
        self.rows_layout.setContentsMargins(0, 0, 0, 0)
        self.rows_layout.setSpacing(7)
        self.v.addWidget(self.rows_host)

        self.method_label = QLabel()
        self.method_label.setObjectName("DecisionMethod")
        self.method_label.setWordWrap(True)
        self.method_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.v.addWidget(self.method_label)
        self.fit_label_height(
            self.method_label,
            word_wrap=True,
            stable_measurement=True,
        )

    def set_data(
        self,
        data: dict[str, Any] | None,
        *,
        privacy: bool,
        max_items: int = 3,
    ) -> None:
        self._data = dict(data or {})
        urgent = int(safe_float(self._data.get("urgent_count")))
        warnings = int(safe_float(self._data.get("warning_count")))
        tone = "negative" if urgent else "warning" if warnings else "positive"
        self.summary_panel.setProperty("tone", tone)
        _repolish(self.summary_panel)

        headline = str(self._data.get("headline") or "Sem leitura disponível")
        self.summary_title.setText(headline)
        coverage_level = str(self._data.get("coverage_level") or "initial")
        coverage_tone = (
            "positive"
            if coverage_level == "high"
            else "warning"
            if coverage_level == "medium"
            else "neutral"
        )
        self.coverage_badge.setText(
            f"Dados {int(safe_float(self._data.get('coverage_score')))}% · "
            f"{str(self._data.get('coverage_label') or 'Confiança inicial').replace('Confiança ', '')}"
        )
        self.coverage_badge.setProperty("tone", coverage_tone)
        _repolish(self.coverage_badge)

        as_of = _format_date(self._data.get("as_of"))
        states = []
        if urgent:
            states.append(f"{urgent} urgente{'s' if urgent != 1 else ''}")
        if warnings:
            states.append(f"{warnings} em atenção")
        state_text = " · ".join(states) if states else "sem risco imediato"
        self.scope_summary.setText(
            "🗓️ O filtro aplica-se aos totais. Cada prioridade indica abaixo o "
            "período específico dos dados que utiliza."
        )

        while self.rows_layout.count():
            item = self.rows_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.priority_rows.clear()

        priorities = [
            dict(row)
            for row in self._data.get("priorities", [])
            if isinstance(row, dict)
        ][: max(1, min(6, int(max_items or 3)))]
        # BUG-DEC-04: the summary counter must match the rows actually
        # rendered, not the engine-side priority_count.
        count = len(priorities)
        self.summary_meta.setText(
            f"{count} prioridade{'s' if count != 1 else ''} · {state_text} · "
            f"🔒 cálculo local · dados até {as_of}"
        )
        for priority in priorities:
            row = DecisionPriorityRow(
                priority,
                privacy=privacy,
                parent=self.rows_host,
            )
            row.action_requested.connect(self.action_requested)
            self.priority_rows.append(row)
            self.rows_layout.addWidget(row)
            for label in (
                row.scope_label,
                row.title_label,
                row.message_label,
                row.metrics_label,
                row.evidence_label,
            ):
                self.fit_label_height(
                    label,
                    word_wrap=True,
                    stable_measurement=True,
                )

        self.method_label.setText(str(self._data.get("method_label") or ""))
        scope_details = str(self._data.get("scope_summary") or "").strip()
        if scope_details:
            self.method_label.setText(
                f"{scope_details}\n{self.method_label.text()}".strip()
            )
        self.setAccessibleDescription(
            f"{headline}. {count} prioridades. "
            f"{self._data.get('coverage_label', 'Confiança inicial')}."
        )
        self.refresh_compact_heights()
        self.updateGeometry()


__all__ = ["DecisionBriefingCard", "DecisionPriorityRow"]
