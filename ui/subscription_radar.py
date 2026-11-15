"""Explicit, private-aware review of possible subscription charge changes."""

from __future__ import annotations

import json
from typing import Any

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGridLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from services.subscription_radar import (
    PERIODS,
    RadarCandidate,
    SubscriptionRadarService,
)
from ui.core.form_accessibility import prepare_form_fields
from ui.core.theme import theme_for_widget
from utils.helpers import eur
from utils.app_logging import log_exception


class SubscriptionRadarDialog(QDialog):
    def __init__(self, repo: Any, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.repo = repo
        self.theme = theme_for_widget(parent)
        self.service = SubscriptionRadarService(repo)
        self.changed = False
        self._candidates: list[RadarCandidate] = []
        self._history: list[dict[str, Any]] = []
        self._private = bool(self.theme.privacy)
        self.setWindowTitle("Radar de aumentos — Subscrições")
        self.setModal(True)
        self.setMinimumSize(320, 280)
        self.setStyleSheet(self.theme.stylesheet())
        root = QVBoxLayout(self)
        introduction = QLabel(
            "Uma cobrança maior pode ser preço, plano, câmbio ou uma ligação incorreta. "
            "Confirma a base antes de decidir. Guardar a revisão não altera o preço "
            "da subscrição nem cria movimentos. Para alterar o valor configurado, usa Editar subscrição."
        )
        introduction.setTextFormat(Qt.PlainText)
        introduction.setWordWrap(True)
        root.addWidget(introduction)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        body = QVBoxLayout(content)
        self.tabs = QTabWidget()
        review_tab = QWidget()
        review = QVBoxLayout(review_tab)
        self.table = QTableWidget(0, 6)
        self.table.setAccessibleName("Propostas de revisão de subscrições")
        self.table.setHorizontalHeaderLabels(
            [
                "Subscrição",
                "Data financeira",
                "Configurado EUR",
                "Cobrança EUR",
                "Período configurado",
                "Estado",
            ]
        )
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setTabKeyNavigation(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.setMinimumHeight(170)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        review.addWidget(self.table)
        self.detail = QLabel()
        self.detail.setTextFormat(Qt.PlainText)
        self.detail.setWordWrap(True)
        self.detail.setAccessibleName("Motivo e origem da proposta")
        review.addWidget(self.detail)
        form = QFormLayout()
        self.classification = QComboBox()
        for title, value in [
            ("Por classificar", "unclassified"),
            ("Preço", "price"),
            ("Plano", "plan"),
            ("Ligação ou base por rever", "matching"),
        ]:
            self.classification.addItem(title, value)
        self.period = QComboBox()
        self.period.addItem("Confirmar período da cobrança…", "")
        for period in PERIODS:
            self.period.addItem(period, period)
        self.currency = QLineEdit()
        self.currency.setMaxLength(3)
        self.currency.setPlaceholderText("Moeda original, por exemplo EUR")
        self.currency.setAccessibleName("Moeda original confirmada da cobrança")
        self.match = QCheckBox("Confirmo que a cobrança pertence a esta subscrição")
        self.note = QPlainTextEdit()
        self.note.setPlaceholderText(
            "Explica o plano ou a ligação; não são inferidos a partir do texto do extrato."
        )
        self.note.setMaximumHeight(100)
        self.note.setTabChangesFocus(True)
        form.addRow("Classificação", self.classification)
        form.addRow("Período observado", self.period)
        form.addRow("Moeda original", self.currency)
        form.addRow("Ligação", self.match)
        form.addRow("Explicação", self.note)
        review.addLayout(form)
        actions = QGridLayout()
        self.pending_button = QPushButton("Guardar pendente")
        self.accept_button = QPushButton("Aceitar revisão")
        self.reject_button = QPushButton("Recusar proposta")
        self.refresh_button = QPushButton("Atualizar")
        for index, button in enumerate(
            [
                self.pending_button,
                self.accept_button,
                self.reject_button,
                self.refresh_button,
            ]
        ):
            button.setAutoDefault(False)
            button.setObjectName("Secondary")
            actions.addWidget(button, index // 2, index % 2)
        self.pending_button.clicked.connect(lambda: self._decide("pending"))
        self.accept_button.clicked.connect(lambda: self._decide("accepted"))
        self.reject_button.clicked.connect(lambda: self._decide("rejected"))
        self.refresh_button.clicked.connect(self.refresh)
        review.addLayout(actions)
        self.tabs.addTab(review_tab, "Rever cobranças")
        self.history_table = QTableWidget(0, 5)
        self.history_table.setAccessibleName("Histórico das decisões do radar")
        self.history_table.setHorizontalHeaderLabels(
            ["Subscrição", "Estado", "Classificação", "Origem", "Explicação"]
        )
        self.history_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.history_table.setTabKeyNavigation(False)
        self.history_table.horizontalHeader().setSectionResizeMode(
            4, QHeaderView.Stretch
        )
        self.tabs.addTab(self.history_table, "Histórico")
        body.addWidget(self.tabs)
        scroll.setWidget(content)
        root.addWidget(scroll, 1)
        self.status = QLabel()
        self.status.setTextFormat(Qt.PlainText)
        self.status.setWordWrap(True)
        self.status.setAccessibleName("Estado do radar de aumentos")
        root.addWidget(self.status)
        close = QDialogButtonBox(QDialogButtonBox.Close)
        close.button(QDialogButtonBox.Close).setText("Fechar")
        close.button(QDialogButtonBox.Close).setObjectName("Secondary")
        close.rejected.connect(self.reject)
        root.addWidget(close)
        prepare_form_fields(content)
        tab_order = [
            self.table,
            self.classification,
            self.period,
            self.currency,
            self.match,
            self.note,
            self.pending_button,
            self.accept_button,
            self.reject_button,
            self.refresh_button,
            close.button(QDialogButtonBox.Close),
        ]
        for previous, following in zip(tab_order, tab_order[1:]):
            QWidget.setTabOrder(previous, following)
        geometry = self.screen().availableGeometry()
        self.resize(
            min(1000, max(320, geometry.width() - 32)),
            min(760, max(280, geometry.height() - 32)),
        )
        self._privacy_timer = QTimer(self)
        self._privacy_timer.timeout.connect(self._check_privacy)
        self._privacy_timer.start(250)
        self.refresh()

    def _selected(self) -> RadarCandidate | None:
        row = self.table.currentRow()
        return self._candidates[row] if 0 <= row < len(self._candidates) else None

    def refresh(self) -> None:
        self._check_privacy()
        try:
            # History also verifies old evidence, while discover arms only the
            # currently visible proposals for an explicit decision.
            self._history = self.service.history()
            self._candidates = self.service.discover()
            self._render()
        except Exception as exc:
            self._candidates = []
            self._render()
            self.status.setText(
                "Não foi possível consultar o radar."
                if self.theme.privacy
                else f"Não foi possível consultar o radar: {exc}"
            )

    def _render(self) -> None:
        self._private = bool(self.theme.privacy)
        self.table.setRowCount(len(self._candidates))
        for row, candidate in enumerate(self._candidates):
            values = [
                "Dados ocultos" if self._private else candidate.subscription_name,
                "—" if self._private else candidate.financial_date,
                eur(candidate.expected_amount, self._private),
                eur(candidate.observed_amount, self._private),
                "—" if self._private else candidate.expected_period,
                {"pending": "Pendente", "accepted": "Aceite", "rejected": "Recusada"}[
                    candidate.status
                ],
            ]
            for column, value in enumerate(values):
                self.table.setItem(row, column, QTableWidgetItem(str(value)))
        self.history_table.setRowCount(len(self._history))
        names = {
            "unclassified": "Por classificar",
            "price": "Preço",
            "plan": "Plano",
            "matching": "Ligação/base",
        }
        for row, stored in enumerate(self._history):
            snapshot = json.loads(stored["evidence_json"])
            values = [
                "Dados ocultos"
                if self._private
                else str(snapshot.get("subscription", {}).get("name", "")),
                {"pending": "Pendente", "accepted": "Aceite", "rejected": "Recusada"}[
                    stored["status"]
                ],
                names[stored["classification"]],
                "Origem alterada/eliminada" if stored["stale"] else "Evidência retida",
                "Dados ocultos" if self._private else stored["note"],
            ]
            for column, value in enumerate(values):
                self.history_table.setItem(row, column, QTableWidgetItem(str(value)))
        self.status.setText(
            "Modo privado: detalhes ocultos. Fecha o radar e desativa o modo privado para tomar uma decisão."
            if self._private
            else (
                "Sem cobranças maiores para rever. Os movimentos e preços não foram alterados."
                if not self._candidates
                else "Confirma período, moeda original e ligação; um valor maior não prova um aumento de preço."
            )
        )
        if self._candidates:
            self.table.selectRow(0)
        self._selection_changed()

    def _selection_changed(self) -> None:
        candidate = self._selected()
        enabled = candidate is not None and not bool(self.theme.privacy)
        for widget in [
            self.classification,
            self.period,
            self.currency,
            self.match,
            self.note,
            self.pending_button,
            self.accept_button,
            self.reject_button,
        ]:
            widget.setEnabled(enabled)
        self.period.setCurrentIndex(0)
        self.currency.clear()
        self.match.setChecked(False)
        self.note.clear()
        self.classification.setCurrentIndex(0)
        if candidate is None:
            self.detail.setText("Seleciona uma proposta para consultar a origem.")
        elif self.theme.privacy:
            self.detail.setText("Detalhes ocultos no modo privado.")
        else:
            self.detail.setText(f"{candidate.description}\n{candidate.reason}")

    def _decide(self, state: str) -> bool:
        self._check_privacy()
        candidate = self._selected()
        if candidate is None or self.theme.privacy:
            return False
        try:
            self.service.decide(
                candidate.evidence_key,
                status=state,
                classification=str(self.classification.currentData()),
                confirmed_period=str(self.period.currentData()),
                confirmed_currency=self.currency.text(),
                match_confirmed=self.match.isChecked(),
                note=self.note.toPlainText(),
            )
            self.changed = True
            self.refresh()
            return True
        except ValueError as exc:
            self.status.setText(str(exc))
            return False
        except Exception as exc:
            log_exception("Failed to save a subscription charge review", exc)
            self.status.setText(
                "Não foi possível guardar a revisão; os dados anteriores foram preservados."
                if self.theme.privacy
                else f"Não foi possível guardar a revisão: {exc}"
            )
            return False

    def _check_privacy(self) -> None:
        current = (
            theme_for_widget(self.parentWidget())
            if self.parentWidget() is not None
            else self.theme
        )
        style_changed = (current.dark, current.primary_color) != (
            self.theme.dark,
            self.theme.primary_color,
        )
        self.theme = current
        if style_changed:
            self.setStyleSheet(self.theme.stylesheet())
        if bool(current.privacy) != self._private:
            self._render()

    def closeEvent(self, event) -> None:
        self._privacy_timer.stop()
        super().closeEvent(event)

    def done(self, result: int) -> None:
        timer = getattr(self, "_privacy_timer", None)
        if timer is not None:
            timer.stop()
        super().done(result)
