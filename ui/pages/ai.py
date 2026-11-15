#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Finanças Pessoais – Liquid Glass
Nova interface PySide6 para a app local de finanças pessoais.
Mantém a base de dados e a app clássica intactas; esta é apenas uma camada visual moderna.
"""

from __future__ import annotations

import sys
from datetime import date
from typing import TYPE_CHECKING, Any, Callable, Optional

from utils.qt_runtime import configure_qt_runtime

configure_qt_runtime()

try:
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtWidgets import (
        QComboBox,
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
        QWidget,
    )
except (
    Exception
) as exc:  # pragma: no cover - only used when PySide6 is missing on the user's PC.
    print("PySide6 não está instalado. Instala com: pip install PySide6")
    print(exc)
    sys.exit(1)

from ui.components import GlassCard
from ui.core import BasePage, FlowLayout
from ui.core.smart_table import SmartTableWidgetItem as QTableWidgetItem
from ui.workers import AsyncTask
from utils.app_logging import log_exception
from utils.helpers import (
    APP_TITLE,
    PT_MONTHS,
    eur,
    percent,
    row_get,
    safe_json_dumps,
)

if TYPE_CHECKING:
    from main import LiquidFinanceApp


class AIAssistantPage(BasePage):
    def __init__(self, app: "LiquidFinanceApp"):
        super().__init__(
            app,
            "Assistente IA Gemini",
            "Análise financeira com Google Gemini, com controlo de privacidade",
        )
        self.ai_service = app.ai_service
        self.ai_task = AsyncTask(
            self,
            registry=app.background_task_registry,
            label="Pedido ao assistente de IA",
        )
        self.ai_task.result.connect(self._on_ai_result)
        self.ai_task.error.connect(self._on_ai_error)
        self.ai_task.finished.connect(self._on_ai_finished)
        self._ai_operation: dict[str, Any] = {}
        self._ai_busy_message = ""
        self._ai_elapsed_seconds = 0
        self._ai_had_error = False
        self.ai_elapsed_timer = QTimer(self)
        self.ai_elapsed_timer.setInterval(1000)
        self.ai_elapsed_timer.timeout.connect(self._update_ai_elapsed)
        # This page contains long AI answers. Let the body use the available height
        # instead of staying at a fixed compact height, while keeping content anchored.
        try:
            self.layout().setStretch(1, 1)
            self.layout().setStretch(2, 0)
        except Exception as _exc:
            log_exception(
                f"{type(self).__name__} failed to adjust AI page stretch", _exc
            )
        top = GlassCard("Estado da ligação", strong=True)
        top.v.setSpacing(8)
        self.status = QLabel()
        self.status.setObjectName("Subtle")
        self.status.setWordWrap(True)
        self.month = QComboBox()
        self.month.setMinimumWidth(135)
        self.month.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self.year = QComboBox()
        self.year.setMinimumWidth(96)
        self.year.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        for y in self.repo.available_years():
            self.year.addItem(str(y), y)
        current_y = str(date.today().year)
        idx_y = self.year.findText(current_y)
        if idx_y >= 0:
            self.year.setCurrentIndex(idx_y)
        for i, name in enumerate(PT_MONTHS, 1):
            self.month.addItem(name.capitalize(), i)
        self.month.setCurrentIndex(date.today().month - 1)
        self.open_settings = QPushButton("Definições de IA")
        self.test = QPushButton("Testar Gemini")
        for b in [self.open_settings, self.test]:
            b.setMinimumWidth(145)
            b.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        row_widget = QWidget()
        row = FlowLayout(row_widget, margin=0, hspacing=8, vspacing=8)
        lbl_year = QLabel("Ano")
        lbl_month = QLabel("Mês")
        for w in [
            lbl_year,
            self.year,
            lbl_month,
            self.month,
            self.open_settings,
            self.test,
        ]:
            row.addWidget(w)
        top.v.addWidget(self.status)
        top.v.addWidget(row_widget)

        actions = GlassCard("Ações da IA", strong=True)
        action_wrap = QWidget()
        action_flow = FlowLayout(action_wrap, margin=0, hspacing=8, vspacing=8)
        self.btn_month = QPushButton("Analisar mês")
        self.btn_budget = QPushButton("Sugerir orçamento")
        self.btn_audit = QPushButton("Auditoria financeira")
        self.btn_categories = QPushButton("Sugerir categorias")
        self.btn_questions = QPushButton("Responder pergunta")
        self.btn_sentiment = QPushButton("Análise Sentimental")
        self.btn_simulator = QPushButton("Simulador E Se?")
        self.btn_vacation = QPushButton("Planeador de Férias")
        for i, b in enumerate(
            [
                self.btn_month,
                self.btn_budget,
                self.btn_audit,
                self.btn_categories,
                self.btn_sentiment,
                self.btn_simulator,
                self.btn_vacation,
                self.btn_questions,
            ]
        ):
            if i == 0:
                b.setObjectName("Primary")
            b.setMinimumWidth(170)
            b.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
            action_flow.addWidget(b)
        self.cancel_ai = QPushButton("Cancelar")
        self.cancel_ai.setMinimumWidth(120)
        self.cancel_ai.setEnabled(False)
        action_flow.addWidget(self.cancel_ai)
        actions.v.addWidget(action_wrap)

        question_card = GlassCard("Pergunta livre")
        self.question = QTextEdit()
        self.question.setPlaceholderText(
            "Ex.: Porque poupei menos? (Pergunta) / Quero comprar uma PS5 por 500€ (Simulador) / Férias em Paris com 1500€ (Planeador)"
        )
        self.question.setMinimumHeight(78)
        self.question.setMaximumHeight(150)
        self.question.setLineWrapMode(QTextEdit.WidgetWidth)
        self.question.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        question_card.v.addWidget(self.question)

        output = GlassCard("Resposta", strong=True)
        self.output = QTextEdit()
        self.output.setReadOnly(True)
        self.output.setLineWrapMode(QTextEdit.WidgetWidth)
        self.output.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.output.setMinimumHeight(360)
        self.output.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.output.setPlaceholderText(
            "As respostas da IA aparecem aqui. As sugestões não são aplicadas automaticamente."
        )
        output.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        output.v.addWidget(self.output, 1)

        history = GlassCard("Histórico de conversas IA")
        history.v.setSpacing(8)
        hist_controls = QWidget()
        hist_flow = FlowLayout(hist_controls, margin=0, hspacing=8, vspacing=8)
        hist_flow.addWidget(QLabel("Filtro"))
        self.history_filter = QComboBox()
        self.history_filter.setMinimumWidth(170)
        hist_flow.addWidget(self.history_filter)
        self.history_reload = QPushButton("Atualizar")
        self.history_load = QPushButton("Carregar resposta")
        self.history_delete = QPushButton("Eliminar entrada")
        self.history_clear_month = QPushButton("Limpar mês")
        self.history_load_more = QPushButton("Carregar mais")
        self.history_load_more.setObjectName("Secondary")
        self.history_load_more.setVisible(False)
        for b in [
            self.history_reload,
            self.history_load,
            self.history_delete,
            self.history_clear_month,
            self.history_load_more,
        ]:
            b.setMinimumWidth(120)
            b.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
            hist_flow.addWidget(b)
        history.v.addWidget(hist_controls)
        self.history_status = QLabel()
        self.history_status.setObjectName("Subtle")
        self.history_status.setWordWrap(True)
        history.v.addWidget(self.history_status)
        self.history_table = QTableWidget(0, 5)
        self.history_table.setProperty("columnPersistenceId", "ai.conversation_history")
        self.history_table.setHorizontalHeaderLabels(
            ["Data", "Mês", "Tipo", "Modelo", "Resumo"]
        )
        self.history_table.verticalHeader().setVisible(False)
        self.history_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.history_table.setSelectionMode(QTableWidget.SingleSelection)
        self.history_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.history_table.setMinimumHeight(220)
        self.history_table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        hh = self.history_table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(4, QHeaderView.Stretch)
        history.v.addWidget(self.history_table, 1)

        privacy = GlassCard("Privacidade")
        txt = QLabel(
            "A app aplica as escolhas de privacidade antes de preparar cada pedido. "
            "O modo agregado exclui descrições e contas; operações que precisam de "
            "movimentos detalhados mostram os campos e a quantidade antes do envio, "
            "quando essa confirmação está ativa. Identificadores pessoais comuns são "
            "ocultados e a IA nunca altera dados sem confirmação."
        )
        txt.setObjectName("Subtle")
        txt.setWordWrap(True)
        privacy.v.addWidget(txt)

        self.body.addWidget(top)
        self.body.addWidget(actions)
        self.body.addWidget(question_card)
        self.body.addWidget(output, 8)
        self.body.addWidget(history, 4)
        self.body.addWidget(privacy)

        self.open_settings.clicked.connect(lambda: self.app.goto("definicoes"))
        self.test.clicked.connect(self.test_gemini)
        self.btn_month.clicked.connect(lambda: self.run_ai("month"))
        self.btn_budget.clicked.connect(lambda: self.run_ai("budget"))
        self.btn_audit.clicked.connect(lambda: self.run_ai("audit"))
        self.btn_categories.clicked.connect(lambda: self.run_ai("categories"))
        self.btn_sentiment.clicked.connect(lambda: self.run_ai("sentiment"))
        self.btn_simulator.clicked.connect(lambda: self.run_ai("simulator"))
        self.btn_vacation.clicked.connect(lambda: self.run_ai("vacation"))
        self.btn_questions.clicked.connect(lambda: self.run_ai("question"))
        self.cancel_ai.clicked.connect(self.cancel_ai_request)
        self.history_filter.currentIndexChanged.connect(self.refresh_history)
        self.history_reload.clicked.connect(self.refresh_history_months)
        self.history_load_more.clicked.connect(self.load_more_history)
        self.history_load.clicked.connect(self.load_selected_history)
        self.history_delete.clicked.connect(self.delete_selected_history)
        self.history_clear_month.clicked.connect(self.clear_history_month)
        self.history_table.doubleClicked.connect(
            lambda *_: self.load_selected_history()
        )
        self._history_page_size = 100
        self._history_loaded_limit = self._history_page_size
        self._history_total = 0
        self.refresh()
        self.refresh_history_months()

    def ym(self) -> str:
        return f"{int(self.year.currentData())}-{int(self.month.currentData()):02d}"

    def _month_label(self, ym: str) -> str:
        try:
            y = int(str(ym)[:4])
            m = int(str(ym)[5:7])
            return f"{PT_MONTHS[m - 1].capitalize()} {y}"
        except Exception:
            return str(ym or "")

    def _action_label(self, action: str) -> str:
        return {
            "month": "Análise mensal",
            "budget": "Orçamento",
            "audit": "Auditoria",
            "categories": "Categorias",
            "question": "Pergunta livre",
        }.get(str(action or ""), str(action or "IA"))

    def refresh_history_months(self) -> None:
        current = (
            self.history_filter.currentData()
            if hasattr(self, "history_filter")
            else "Todos"
        )
        self.history_filter.blockSignals(True)
        self.history_filter.clear()
        self.history_filter.addItem("Todos os meses", "Todos")
        for ym in self.ai_service.history_months():
            self.history_filter.addItem(self._month_label(ym), ym)
        idx = self.history_filter.findData(current)
        if idx >= 0:
            self.history_filter.setCurrentIndex(idx)
        self.history_filter.blockSignals(False)
        self.refresh_history()

    def refresh_history(self, *_args, reset_limit: bool = True) -> None:
        if not hasattr(self, "history_table"):
            return
        if reset_limit:
            self._history_loaded_limit = self._history_page_size
        month = self.history_filter.currentData() or "Todos"
        total = self.ai_service.history_count(str(month))
        rows = self.ai_service.history(str(month), self._history_loaded_limit)
        self._history_total = total
        self.history_table.setRowCount(0)
        for r in rows:
            row = self.history_table.rowCount()
            self.history_table.insertRow(row)
            created = str(row_get(r, "created_at", ""))[:19].replace("T", " ")
            ym = str(row_get(r, "month", ""))
            action = self._action_label(str(row_get(r, "action", "")))
            model = str(row_get(r, "model", ""))
            question = str(row_get(r, "question", "") or "")
            response = str(row_get(r, "response", "") or "")
            preview = question.strip() or response.strip().replace("\n", " ")[:180]
            values = [created, self._month_label(ym), action, model, preview]
            for col, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setData(Qt.UserRole, int(row_get(r, "id", 0)))
                self.history_table.setItem(row, col, item)
        self.history_table.resizeRowsToContents()
        loaded = len(rows)
        remaining = max(0, total - loaded)
        self.history_load_more.setVisible(remaining > 0)
        if remaining > 0:
            next_count = min(self._history_page_size, remaining)
            self.history_load_more.setText(f"Carregar mais {next_count}")
            self.history_status.setText(
                f"A mostrar {loaded} de {total} conversas guardadas."
            )
        else:
            self.history_status.setText(
                f"{total} conversa(s) guardada(s)."
                if total
                else "Ainda não existem conversas guardadas neste período."
            )

    def load_more_history(self) -> None:
        if self._history_loaded_limit >= self._history_total:
            return
        self._history_loaded_limit = min(
            self._history_total,
            self._history_loaded_limit + self._history_page_size,
        )
        self.refresh_history(reset_limit=False)

    def selected_history_id(self) -> Optional[int]:
        if not hasattr(self, "history_table"):
            return None
        row = self.history_table.currentRow()
        if row < 0:
            return None
        item = self.history_table.item(row, 0)
        if not item:
            return None
        try:
            return int(item.data(Qt.UserRole))
        except Exception:
            return None

    def load_selected_history(self) -> None:
        entry_id = self.selected_history_id()
        if not entry_id:
            self.output.setPlainText("Escolhe primeiro uma conversa no histórico.")
            return
        entry = self.ai_service.history_entry(entry_id)
        if not entry:
            self.output.setPlainText(
                "Não consegui encontrar essa entrada no histórico."
            )
            return
        response = str(row_get(entry, "response", ""))
        question = str(row_get(entry, "question", ""))
        action = self._action_label(str(row_get(entry, "action", "")))
        created = str(row_get(entry, "created_at", ""))[:19].replace("T", " ")
        model = str(row_get(entry, "model", ""))
        month = self._month_label(str(row_get(entry, "month", "")))
        header = (
            f"Histórico IA · {action} · {month} · {created} · {model}\n"
            + "─" * 72
            + "\n\n"
        )
        if question:
            self.question.setPlainText(question)
            header += f"Pergunta:\n{question}\n\n" + "─" * 72 + "\n\n"
        self.output.setPlainText(header + response)

    def delete_selected_history(self) -> None:
        entry_id = self.selected_history_id()
        if not entry_id:
            self.output.setPlainText(
                "Escolhe primeiro uma conversa no histórico para eliminar."
            )
            return
        if (
            QMessageBox.question(
                self, APP_TITLE, "Eliminar esta entrada do histórico da IA?"
            )
            != QMessageBox.Yes
        ):
            return
        self.ai_service.delete_history_entry(entry_id)
        self.refresh_history_months()

    def clear_history_month(self) -> None:
        month = str(self.history_filter.currentData() or "Todos")
        if month == "Todos":
            self.output.setPlainText(
                "Escolhe um mês específico para limpar o histórico desse mês."
            )
            return
        if (
            QMessageBox.question(
                self,
                APP_TITLE,
                f"Eliminar todo o histórico de IA de {self._month_label(month)}?",
            )
            != QMessageBox.Yes
        ):
            return
        self.ai_service.clear_history_month(month)
        self.refresh_history_months()

    def refresh(self) -> None:
        prefs = self.ai_service.preferences()
        if prefs.get("enabled") and prefs.get("has_api_key"):
            source = (
                "variável GEMINI_API_KEY"
                if prefs.get("use_env") and prefs.get("env_key_present")
                else "chave guardada localmente"
            )
            self.status.setText(
                f"Gemini ativo · modelo {prefs.get('model')} · chave: {source}"
            )
        elif prefs.get("enabled"):
            self.status.setText(
                "Gemini está ativo, mas falta configurar a chave API nas Definições."
            )
        else:
            self.status.setText(
                "Gemini está desligado. Ativa em Definições → IA Gemini."
            )

    def _base_instruction(self) -> str:
        return (
            "Responde em português de Portugal, com novo acordo ortográfico. "
            "Sê prático, direto e cauteloso. Não inventes dados. "
            "Não recomendes produtos financeiros específicos. "
            "Quando sugerires alterações, apresenta-as como sugestões e nunca como ações já aplicadas."
        )

    def _confirm_detailed_payload(
        self,
        *,
        mode: str,
        row_count: int,
        fields: list[str],
        anonymized: bool,
    ) -> bool:
        action = self._action_label(mode)
        field_text = ", ".join(fields) if fields else "dados do movimento"
        privacy_text = (
            "Os identificadores pessoais reconhecidos serão ocultados."
            if anonymized
            else "As descrições serão enviadas sem anonimização adicional."
        )
        box = QMessageBox(self)
        box.setWindowTitle("Confirmar dados enviados à IA")
        box.setIcon(QMessageBox.Information)
        box.setText(f"{action} precisa de dados detalhados.")
        box.setInformativeText(
            f"Serão enviados {row_count} movimento(s), com: {field_text}.\n\n"
            f"{privacy_text}\n\nQueres continuar?"
        )
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.Cancel)
        box.setDefaultButton(QMessageBox.Cancel)
        return box.exec() == QMessageBox.Yes

    def _set_ai_busy(self, busy: bool, message: str = "") -> None:
        controls = [
            self.test,
            self.btn_month,
            self.btn_budget,
            self.btn_audit,
            self.btn_categories,
            self.btn_questions,
            self.open_settings,
            self.year,
            self.month,
            self.question,
        ]
        for control in controls:
            control.setEnabled(not busy)
        self.cancel_ai.setEnabled(busy)
        if busy:
            self._ai_busy_message = message
            self._ai_elapsed_seconds = 0
            self.status.setText(f"{message} · 0 s")
            self.ai_elapsed_timer.start()
        else:
            self.ai_elapsed_timer.stop()

    def _update_ai_elapsed(self) -> None:
        if not self.ai_task.is_running():
            return
        self._ai_elapsed_seconds += 1
        self.status.setText(f"{self._ai_busy_message} · {self._ai_elapsed_seconds} s")

    def _start_ai_task(
        self,
        callback: Callable[[], Any],
        *,
        message: str,
        operation: dict[str, Any],
    ) -> bool:
        if self.ai_task.is_running():
            return False
        self._ai_operation = operation
        self._ai_had_error = False
        if not self.ai_task.start(callback):
            self._ai_operation = {}
            return False
        self._set_ai_busy(True, message)
        return True

    def cancel_ai_request(self) -> None:
        if self.ai_task.cancel():
            self.cancel_ai.setEnabled(False)
            self.status.setText("A cancelar o pedido Gemini…")

    def _on_ai_result(self, value: Any) -> None:
        self.ai_elapsed_timer.stop()
        answer = str(value)
        operation = self._ai_operation
        self.output.setPlainText(answer)
        if operation.get("kind") == "generation":
            try:
                self.ai_service.save_history(
                    month=str(operation.get("month", "")),
                    action=str(operation.get("mode", "")),
                    question=str(operation.get("question", "")),
                    prompt=str(operation.get("prompt", "")),
                    response=answer,
                )
                self.refresh_history_months()
            except Exception as exc:
                log_exception("Failed to save AI chat history", exc)
        self.refresh()

    def _on_ai_error(self, message: str) -> None:
        self.ai_elapsed_timer.stop()
        self._ai_had_error = True
        if self._ai_operation.get("kind") == "test":
            detail = (
                f"Erro ao testar Gemini:\n{message}\n\n"
                "Confirma a chave API, a ligação à Internet e as permissões da firewall."
            )
        else:
            detail = (
                f"Erro ao gerar resposta com Gemini:\n{message}\n\n"
                "Verifica a chave, o modelo e a ligação à Internet."
            )
        self.output.setPlainText(detail)
        self.status.setText(f"Erro Gemini: {message}")

    def _on_ai_finished(self, cancelled: bool) -> None:
        self._set_ai_busy(False)

    def test_gemini(self) -> None:

        if self.ai_task.is_running():
            return
        self.output.setPlainText("A preparar teste ao Gemini…")
        try:
            callback = self.ai_service.prepare_generate(
                "Responde apenas: ligação OK.",
                system_instruction=self._base_instruction(),
            )
        except Exception as exc:
            self._ai_operation = {"kind": "test"}
            self._on_ai_error(str(exc))
            self._ai_operation = {}
            return
        self.output.setPlainText("A testar ligação ao Gemini…")
        self._start_ai_task(
            callback,
            message="A testar ligação ao Gemini",
            operation={"kind": "test"},
        )

    def run_ai(self, mode: str) -> None:
        if self.ai_task.is_running():
            return
        prefs = self.ai_service.preferences()
        effective_anonymize = bool(
            prefs.get("anonymize") or getattr(self.app.theme, "privacy", False)
        )
        context = self.ai_service.month_context(
            self.ym(), include_descriptions=False, anonymize=True
        )
        prompt = ""
        question = ""
        detailed_rows: list[dict[str, Any]] = []
        history_summary = "Pedido preparado apenas com dados agregados."
        if mode == "month":
            prompt += "Analisa este mês e cria um resumo financeiro com pontos positivos, riscos, gastos fora do normal e 5 ações recomendadas.\n"
            prompt += (
                "<financial_ledger>\n"
                + safe_json_dumps(context)
                + "\n</financial_ledger>"
            )
        elif mode == "budget":
            prompt += "Cria uma sugestão de orçamento para o próximo mês. Usa percentagens e valores por categoria, adapta ao histórico e compara com 50/30/20 e 70/20/10 quando fizer sentido.\n"
            prompt += (
                "<financial_ledger>\n"
                + safe_json_dumps(context)
                + "\n</financial_ledger>"
            )
        elif mode == "audit":
            prompt += "Faz uma auditoria financeira. Procura subscrições prováveis, gastos anormais, categorias genéricas, movimentos sem anexo relevante, riscos de orçamento e melhorias de organização.\n"
            prompt += (
                "<financial_ledger>\n"
                + safe_json_dumps(context)
                + "\n</financial_ledger>"
            )
        elif mode == "categories":
            source_items = self.ai_service.uncategorized_context(50)
            items = self.ai_service.sanitize_transaction_rows(source_items)
            detailed_rows = items
            prompt += (
                "Sugere categoria, subcategoria, comerciante provável e forma de pagamento provável para estes movimentos. "
                "Devolve em tabela Markdown com colunas: ID, categoria, subcategoria, comerciante, confiança, motivo. "
                "Não assumes que a sugestão foi aplicada.\n"
            )

            # 🤖 AI Classificator 2.0: Feed user's learned rules!
            try:
                user_rules = self.ai_service.repo.query(
                    "SELECT keyword, category FROM category_rules LIMIT 50"
                )
                if user_rules:
                    prompt += "As tuas regras de classificação manuais aprendidas (usa-as como prioridade):\n"
                    for r in user_rules:
                        keyword = self.ai_service.sanitize_text(r["keyword"])
                        prompt += f"- Se a descrição contiver '{keyword}', a categoria é '{r['category']}'\n"
                    prompt += "\n"
            except Exception as exc:
                log_exception("Failed to load learned AI category rules", exc)

            prompt += (
                "Movimentos a classificar:\n<financial_ledger>\n"
                + safe_json_dumps(items)
                + "\n</financial_ledger>"
            )
        elif mode == "sentiment":
            include_descriptions = not bool(prefs.get("privacy_aggregated", True))
            detailed_context = self.ai_service.month_context(
                self.ym(),
                include_descriptions=include_descriptions,
                anonymize=effective_anonymize,
            )
            if include_descriptions:
                detailed_rows = list(detailed_context.get("movimentos_amostra") or [])
            prompt += (
                "Analisa padrões de decisão de consumo deste mês. Apresenta apenas "
                "hipóteses prudentes sobre compras por impulso ou lazer; não infiras "
                "emoções, saúde mental ou intenções como factos. Explica sempre a "
                "evidência financeira e a incerteza.\n"
            )
            prompt += (
                (
                    "Dados com amostra de descrições:\n"
                    if include_descriptions
                    else "Dados agregados:\n"
                )
                + "<financial_ledger>\n"
                + safe_json_dumps(detailed_context)
                + "\n</financial_ledger>"
            )
        elif mode == "simulator":
            question = self.question.toPlainText().strip()
            if not question:
                self.output.setPlainText(
                    "Para o Simulador, escreve na caixa 'Pergunta livre' o que queres simular (ex: 'Comprar um carro de 5000€', 'Aumentar a prestação em 200€')."
                )
                return
            question_for_prompt = (
                self.ai_service.sanitize_text(question)
                if effective_anonymize
                else question
            )
            prompt += "Atua como um simulador financeiro 'E se?'. O utilizador vai sugerir uma despesa grande ou alteração de hábito. Calcula o impacto dessa decisão nos próximos meses, na taxa de poupança e nos objetivos, usando o estado atual do orçamento.\n"
            prompt += (
                f"Simulação desejada: {question_for_prompt}\n\n<financial_ledger>\n"
                + safe_json_dumps(context)
                + "\n</financial_ledger>"
            )
        elif mode == "vacation":
            question = self.question.toPlainText().strip()
            if not question:
                self.output.setPlainText(
                    "Para o Planeador de Férias, escreve o destino e orçamento na caixa (ex: 'Férias no Japão, orçamento de 2000€')."
                )
                return
            question_for_prompt = (
                self.ai_service.sanitize_text(question)
                if effective_anonymize
                else question
            )
            prompt += "Atua como um Planeador de Férias Financeiro. Pega no destino e orçamento sugerido pelo utilizador e cria um plano de poupança mensal (assumindo a viagem a 6 meses de distância), sugerindo cortes em categorias atuais para financiar a viagem sem afetar a poupança estrutural.\n"
            prompt += (
                f"Plano desejado: {question_for_prompt}\n\n<financial_ledger>\n"
                + safe_json_dumps(context)
                + "\n</financial_ledger>"
            )
        elif mode == "question":
            question = self.question.toPlainText().strip()
            if not question:
                self.output.setPlainText(
                    "Escreve primeiro uma pergunta na caixa 'Pergunta livre'."
                )
                return
            question_for_prompt = (
                self.ai_service.sanitize_text(question)
                if effective_anonymize
                else question
            )
            prompt += (
                "Responde à pergunta do utilizador com base nestes dados agregados.\n"
            )
            prompt += (
                f"Pergunta: {question_for_prompt}\n\n<financial_ledger>\n"
                + safe_json_dumps(context)
                + "\n</financial_ledger>"
            )
        else:
            return

        if detailed_rows:
            fields = self.ai_service.detailed_payload_fields(detailed_rows)
            must_confirm = bool(prefs.get("ask_before_details")) or bool(
                getattr(self.app.theme, "privacy", False)
            )
            if must_confirm and not self._confirm_detailed_payload(
                mode=mode,
                row_count=len(detailed_rows),
                fields=fields,
                anonymized=effective_anonymize,
            ):
                self.output.setPlainText(
                    "Envio cancelado. Nenhum dado detalhado foi enviado à IA."
                )
                return
            history_summary = (
                f"Pedido com {len(detailed_rows)} movimento(s) e os campos "
                f"{', '.join(fields)}. "
                + (
                    "Identificadores pessoais reconhecidos foram ocultados."
                    if effective_anonymize
                    else "Anonimização adicional desativada."
                )
            )

        try:
            callback = self.ai_service.prepare_generate(
                prompt,
                system_instruction=self._base_instruction(),
            )
        except Exception as exc:
            self._ai_operation = {"kind": "generation"}
            self._on_ai_error(str(exc))
            self._ai_operation = {}
            return

        self.output.setPlainText("A gerar resposta com Gemini…")
        self._start_ai_task(
            callback,
            message="A gerar resposta com Gemini",
            operation={
                "kind": "generation",
                "mode": mode,
                "month": self.ym(),
                "question": self.ai_service.sanitize_text(question),
                "prompt": history_summary,
            },
        )


class ExpandableAnalysisSection(QWidget):
    """Compact local-analysis section that reveals detail only on request."""

    def __init__(self, title: str, icon: str, *, expanded: bool = False):
        super().__init__()
        self._title = str(title)
        self._icon = str(icon)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)
        self.toggle = QPushButton()
        self.toggle.setObjectName("Secondary")
        self.toggle.setCheckable(True)
        self.toggle.setChecked(bool(expanded))
        self.toggle.setAccessibleName(f"Expandir ou recolher {self._title}")
        self.content = QLabel()
        self.content.setObjectName("CardDescription")
        self.content.setWordWrap(True)
        self.content.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.content.setMinimumWidth(0)
        self.content.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        layout.addWidget(self.toggle)
        layout.addWidget(self.content)
        self.toggle.toggled.connect(self.set_expanded)
        self.set_expanded(bool(expanded))

    def set_content(self, lines: list[str]) -> None:
        values = [str(line).strip() for line in lines if str(line).strip()]
        self.content.setText("\n".join(f"• {line}" for line in values) or "Sem dados.")
        self.content.setAccessibleDescription(self.content.text())

    def set_expanded(self, expanded: bool) -> None:
        selected = bool(expanded)
        self.toggle.blockSignals(True)
        self.toggle.setChecked(selected)
        self.toggle.blockSignals(False)
        self.toggle.setText(f"{'▾' if selected else '▸'}  {self._icon} {self._title}")
        self.toggle.setToolTip(
            f"{'Recolher' if selected else 'Ver'} {self._title.casefold()}"
        )
        self.content.setVisible(selected)
        self.updateGeometry()


class AssistantPage(BasePage):
    def __init__(self, app: "LiquidFinanceApp"):
        super().__init__(
            app,
            "Assistente",
            "Análise local dos teus dados, sem enviar informação para fora",
        )
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(8)
        self.generate_btn = QPushButton("Gerar análise")
        self.generate_btn.setObjectName("CardAction")
        self.explanation_btn = QPushButton("Ver explicação completa")
        self.explanation_btn.setObjectName("Secondary")
        self.explanation_btn.setCheckable(True)
        top.addStretch()
        top.addWidget(self.explanation_btn)
        top.addWidget(self.generate_btn)

        summary_card = GlassCard("Resumo do mês", strong=True)
        self.summary_card = summary_card
        summary_card.set_description(
            "Leitura local e compacta. Abre apenas as secções que queres consultar."
        )
        self.summary_status = QLabel()
        self.summary_status.setObjectName("MetricStatus")
        self.summary_status.setWordWrap(True)
        summary_card.v.addWidget(self.summary_status)

        metrics = QGridLayout()
        metrics.setContentsMargins(0, 2, 0, 2)
        metrics.setSpacing(8)
        self.income_value = self._add_summary_metric(metrics, 0, "💶", "Receitas")
        self.expenses_value = self._add_summary_metric(metrics, 1, "🧾", "Despesas")
        self.savings_value = self._add_summary_metric(metrics, 2, "🐷", "Poupança")
        self.register_responsive_grid(metrics, max_columns=3, min_column_width=180)
        summary_card.v.addLayout(metrics)

        sections = QGridLayout()
        sections.setContentsMargins(0, 0, 0, 0)
        sections.setSpacing(8)
        self.categories_section = ExpandableAnalysisSection(
            "Categorias com maior peso", "📊"
        )
        self.alerts_section = ExpandableAnalysisSection("Alertas", "🚦", expanded=True)
        self.suggestions_section = ExpandableAnalysisSection("Próximos passos", "✨")
        for column, section in enumerate(
            (
                self.categories_section,
                self.alerts_section,
                self.suggestions_section,
            )
        ):
            sections.addWidget(section, 0, column)
        self.register_responsive_grid(sections, max_columns=3, min_column_width=250)

        # This is a temporary disclosure panel, not a separately customisable
        # dashboard card.  Keep the GlassCard title empty so BasePage does not
        # override its visibility when applying card preferences.
        details_card = GlassCard()
        self.details_card = details_card
        details_title = QLabel("Explicação completa")
        details_title.setObjectName("SectionTitle")
        details_card.v.addWidget(details_title)
        self.text = QTextEdit()
        self.text.setReadOnly(True)
        self.text.setMinimumHeight(180)
        self.text.setMaximumHeight(340)
        self.text.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        details_card.v.addWidget(self.text)
        details_card.setVisible(False)

        self.body.addLayout(top)
        self.body.addWidget(summary_card)
        self.body.addLayout(sections)
        self.body.addWidget(details_card)
        self.generate_btn.clicked.connect(self.refresh)
        self.explanation_btn.toggled.connect(self._show_full_explanation)
        self.refresh()

    @staticmethod
    def _add_summary_metric(
        layout: QGridLayout, column: int, icon: str, title: str
    ) -> QLabel:
        host = QWidget()
        host.setProperty("assistantSummaryMetric", True)
        metric_layout = QVBoxLayout(host)
        metric_layout.setContentsMargins(10, 7, 10, 7)
        metric_layout.setSpacing(2)
        title_label = QLabel(f"{icon}  {title}")
        title_label.setObjectName("MetricTitle")
        value_label = QLabel("—")
        value_label.setObjectName("MetricValue")
        value_label.setMinimumWidth(0)
        value_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        metric_layout.addWidget(title_label)
        metric_layout.addWidget(value_label)
        layout.addWidget(host, 0, column)
        return value_label

    def _show_full_explanation(self, visible: bool) -> None:
        selected = bool(visible)
        self.details_card.setVisible(selected)
        self.explanation_btn.setText(
            "Ocultar explicação completa" if selected else "Ver explicação completa"
        )
        self.explanation_btn.setToolTip(
            "Recolher o texto detalhado"
            if selected
            else "Abrir a explicação detalhada sem aumentar permanentemente a página"
        )
        self.details_card.updateGeometry()

    def refresh(self):
        today = date.today()
        start = today.replace(day=1).isoformat()
        end = today.isoformat()
        totals = self.analytics_service.totals_for_period(start, end)
        income = totals["income"]
        expenses = totals["expenses"]
        savings = totals["savings"]
        rate = (savings / income * 100) if income else 0
        cats = self.analytics_service.category_totals(start, end, 5)
        subs = self.payments_service.active_subscription_monthly_total()
        privacy = self.app.theme.privacy
        self.income_value.setText(eur(income, privacy))
        self.expenses_value.setText(eur(expenses, privacy))
        self.savings_value.setText(f"{eur(savings, privacy)} · {percent(rate)}")
        if rate >= 20:
            self.summary_status.setText(
                f"🟢 Ritmo saudável: estás a guardar {percent(rate)} das receitas deste mês."
            )
        elif rate >= 10:
            self.summary_status.setText(
                f"🟡 Poupança positiva: {percent(rate)} das receitas ficou disponível."
            )
        else:
            self.summary_status.setText(
                f"🔴 Margem curta: a taxa de poupança está em {percent(rate)}."
            )

        category_lines: list[str] = []
        if cats:
            for name, total, _colour in cats:
                category_lines.append(f"{name}: {eur(total, privacy)}")
        self.categories_section.set_content(category_lines)

        alert_lines: list[str] = []
        if rate < 10:
            alert_lines.append(
                "A taxa de poupança está baixa; revê gastos variáveis e subscrições."
            )
        else:
            alert_lines.append(
                "A taxa de poupança está positiva; mantém o controlo semanal."
            )
        if subs:
            alert_lines.append(
                f"As subscrições ativas representam cerca de {eur(subs, privacy)} por mês."
            )
        duplicates = self.repo.simple_duplicate_groups(limit=5)
        if duplicates:
            alert_lines.append(
                f"Existem {len(duplicates)} grupos de possíveis duplicados a rever."
            )
        unc = self.repo.generic_category_count()
        if unc:
            alert_lines.append(f"Tens {int(unc)} movimentos em categorias genéricas.")
        self.alerts_section.set_content(alert_lines)

        suggestion_lines = [
            "Fecha o mês no Histórico mensal para guardar um retrato financeiro.",
            "Usa a Divisão inteligente nas compras com várias categorias.",
            "Anexa faturas aos movimentos importantes para manter o arquivo organizado.",
        ]
        self.suggestions_section.set_content(suggestion_lines)

        txt = [
            "ANÁLISE DO MÊS",
            "",
            f"Receitas: {eur(income, privacy)}",
            f"Despesas: {eur(expenses, privacy)}",
            f"Poupança: {eur(savings, privacy)} ({percent(rate)})",
            "",
            "CATEGORIAS COM MAIOR PESO",
            *[f"• {line}" for line in category_lines],
            "",
            "ALERTAS",
            *[f"• {line}" for line in alert_lines],
            "",
            "PRÓXIMOS PASSOS",
            *[f"• {line}" for line in suggestion_lines],
            "",
            "Esta análise é calculada localmente com os movimentos do mês atual.",
        ]
        self.text.setPlainText("\n".join(txt))
        self.summary_card.set_footer(
            "Dados locais · mês atual · nenhum dado enviado para serviços externos"
        )
