#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Finanças Pessoais – Liquid Glass
Nova interface PySide6 para a app local de finanças pessoais.
Mantém a base de dados e a app clássica intactas; esta é apenas uma camada visual moderna.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Optional

from utils.qt_runtime import configure_qt_runtime

configure_qt_runtime()

try:
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import (
        QCheckBox,
        QComboBox,
        QFileDialog,
        QFormLayout,
        QHBoxLayout,
        QInputDialog,
        QLabel,
        QLineEdit,
        QMessageBox,
        QPushButton,
        QSpinBox,
        QWidget,
    )
except (
    Exception
) as exc:  # pragma: no cover - only used when PySide6 is missing on the user's PC.
    print("PySide6 não está instalado. Instala com: pip install PySide6")
    print(exc)
    sys.exit(1)

from services.ai import GEMINI_RECOMMENDED_TEXT_MODELS
from services.security_settings import (
    BackupVerificationStatus,
    SecuritySettingsController,
    SecurityStatus,
)
from services.startup_security import StartupSecurityService
from ui.app_updates import AppUpdateController
from ui.components import GlassCard
from ui.core import BasePage, FlowLayout, apply_message_box_theme
from ui.workers import AsyncTask
from utils.app_logging import log_exception
from utils.config import version_protection_dir
from utils.helpers import (
    APP_TITLE,
    BACKUP_INTERVAL_OPTIONS,
    backup_interval_label,
    open_path,
)

if TYPE_CHECKING:
    from main import LiquidFinanceApp


class SettingsPage(BasePage):
    def __init__(
        self,
        app: "LiquidFinanceApp",
        *,
        security_controller: SecuritySettingsController | None = None,
    ):
        super().__init__(
            app,
            "Definições",
            "Tema, privacidade, backups e IA Gemini",
        )
        self.ai_service = app.ai_service
        self.backup_service = app.backup_service

        self.security_controller = security_controller or SecuritySettingsController(
            security_service=StartupSecurityService(
                database_file=Path(self.repo.db.path)
            ),
            backup_service=self.backup_service,
        )
        self.ai_task = AsyncTask(
            self,
            registry=app.background_task_registry,
            label="Configuração do assistente de IA",
        )
        self.ai_task.result.connect(self._on_settings_ai_result)
        self.ai_task.error.connect(self._on_settings_ai_error)
        self.ai_task.finished.connect(self._on_settings_ai_finished)
        self.backup_task = AsyncTask(
            self,
            registry=app.background_task_registry,
            label="Backup e teste de recuperação",
        )
        self.backup_task.result.connect(self._on_backup_task_result)
        self.backup_task.error.connect(self._on_backup_task_error)
        self.backup_task.finished.connect(self._on_backup_task_finished)
        self._pending_clear_movements_count = 0
        self.backup_health_task = AsyncTask(
            self,
            registry=app.background_task_registry,
            label="Validação real do backup mais recente",
        )
        self.backup_health_task.result.connect(self._on_backup_health_result)
        self.backup_health_task.error.connect(self._on_backup_health_error)
        self._backup_health_fingerprint = None
        self._backup_health_result: dict[str, Any] = {}
        self._backup_operation = ""
        self._settings_ai_operation = ""
        self._settings_ai_current_model = ""
        self._settings_ai_busy_message = ""
        self._settings_ai_elapsed_seconds = 0
        self.settings_ai_elapsed_timer = QTimer(self)
        self.settings_ai_elapsed_timer.setInterval(1000)
        self.settings_ai_elapsed_timer.timeout.connect(self._update_settings_ai_elapsed)
        self.layout().setSpacing(3)
        self.body.setSpacing(6)

        prefs = GlassCard("Preferências", strong=True)
        prefs.v.setSpacing(6)
        self.dark = QCheckBox("Tema escuro Liquid Glass")
        self.dark.setChecked(self.app.theme.dark)
        self.privacy = QCheckBox("Modo privacidade: esconder valores")
        self.privacy.setChecked(self.app.theme.privacy)
        prefs.v.addWidget(self.dark)
        prefs.v.addWidget(self.privacy)

        color_row = QHBoxLayout()
        color_row.setContentsMargins(0, 0, 0, 0)
        color_row.setSpacing(8)
        color_row.addWidget(QLabel("Cor de Destaque:"))
        self.primary_color_cb = QComboBox()
        self.primary_color_cb.addItems(["Azul", "Verde", "Roxo", "Rosa", "Laranja"])
        current_color = self.repo.setting("liquid_primary", "Azul")
        self.primary_color_cb.setCurrentText(str(current_color))
        self.primary_color_cb.currentTextChanged.connect(self.update_theme)
        color_row.addWidget(self.primary_color_cb)
        color_row.addStretch(1)
        prefs.v.addLayout(color_row)
        font_row = QHBoxLayout()
        font_row.setContentsMargins(0, 0, 0, 0)
        font_row.setSpacing(8)
        font_row.addWidget(QLabel("Tamanho da letra:"))
        self.font_size = QComboBox()
        self.font_size.addItems(
            ["Pequeno (90%)", "Normal (100%)", "Grande (110%)", "Muito Grande (125%)"]
        )
        current_font = self.repo.setting("global_font_scale", "Normal (100%)")
        self.font_size.setCurrentText(str(current_font))
        self.font_size.currentTextChanged.connect(self.update_font)
        font_row.addWidget(self.font_size)
        font_row.addStretch(1)
        prefs.v.addLayout(font_row)

        backup_card = GlassCard("Backups automáticos", strong=True)
        backup_card.v.setSpacing(8)
        auto_row = QHBoxLayout()
        auto_row.setContentsMargins(0, 0, 0, 0)
        auto_row.setSpacing(8)
        auto_row.addWidget(QLabel("Criar backup a cada"))
        self.auto_interval = QComboBox()
        for label, minutes in BACKUP_INTERVAL_OPTIONS:
            self.auto_interval.addItem(label, minutes)
        backup_prefs = self.backup_service.preferences()
        current_interval = int(backup_prefs.get("auto_backup_interval_minutes", 1440))
        idx = self.auto_interval.findData(current_interval)
        if idx < 0:
            idx = self.auto_interval.findData(1440)
        self.auto_interval.setCurrentIndex(max(0, idx))
        self.auto_interval.setMinimumWidth(170)
        self.auto_interval.setToolTip(
            "Define o intervalo do backup automático. Usa 'Desativado' para desligar."
        )
        auto_row.addWidget(self.auto_interval)
        auto_row.addWidget(QLabel("manter"))
        self.keep_backups = QSpinBox()
        self.keep_backups.setRange(1, 250)
        self.keep_backups.setValue(int(backup_prefs.get("backup_keep", 15)))
        self.keep_backups.setSuffix(" backups")
        auto_row.addWidget(self.keep_backups)
        self.backup_on_close = QCheckBox("Criar também um backup quando fecho a app")
        self.backup_on_close.setChecked(
            bool(backup_prefs.get("backup_on_close", False))
        )
        auto_row.addWidget(self.backup_on_close)
        save_backup = QPushButton("Guardar")
        save_backup.setObjectName("Primary")
        auto_row.addWidget(save_backup)
        auto_row.addStretch(1)
        backup_card.v.addLayout(auto_row)

        # Password management moved to the dedicated change-credential dialog:
        # the security envelope owns the credentials (LGBK2), so inline
        # password rows in Settings could silently ignore explicit values.
        self.encrypt_backups = QCheckBox(
            "🔐 Cifrar backups com a segurança de arranque"
        )
        self.encrypt_backups.setChecked(
            bool(backup_prefs.get("backup_encryption_enabled", False))
        )
        self.encrypt_backups.setEnabled(self.backup_service.encryption_available())
        encryption_row = FlowLayout(hspacing=8, vspacing=8)
        encryption_row.addWidget(self.encrypt_backups)
        self.change_credential_button = QPushButton("Alterar credencial…")
        self.change_credential_button.setObjectName("Primary")
        self.change_credential_button.setAccessibleName(
            "Alterar credencial de segurança"
        )
        self.change_credential_button.setToolTip(
            "Abre o diálogo para mudar o PIN ou a palavra-passe que protege a "
            "aplicação e os backups portáteis."
        )
        encryption_row.addWidget(self.change_credential_button)
        backup_card.v.addLayout(encryption_row)
        self.encryption_status = QLabel()
        self.encryption_status.setObjectName("Subtle")
        self.encryption_status.setWordWrap(True)
        backup_card.v.addWidget(self.encryption_status)

        configured_primary = str(
            backup_prefs.get("primary_backup_directory") or ""
        ).strip()
        effective_primary = str(
            backup_prefs.get("effective_backup_directory")
            or self.backup_service.backup_directory
        )
        self.primary_backup_uses_default = not bool(configured_primary)
        self.primary_backup_path = QLineEdit(configured_primary or effective_primary)
        self.primary_backup_path.setReadOnly(True)
        self.primary_backup_path.setMinimumWidth(320)
        self.primary_backup_path.setToolTip(
            "Esta é a pasta usada pelos backups automáticos, manuais e ao fechar."
        )
        self.choose_primary_backup = QPushButton("Escolher pasta principal")
        self.reset_primary_backup = QPushButton("Usar localização predefinida")
        primary_row = FlowLayout(hspacing=8, vspacing=8)
        primary_label = QLabel("📁 Pasta principal de backups")
        primary_label.setObjectName("Subtle")
        primary_row.addWidget(primary_label)
        primary_row.addWidget(self.primary_backup_path)
        primary_row.addWidget(self.choose_primary_backup)
        primary_row.addWidget(self.reset_primary_backup)
        backup_card.v.addLayout(primary_row)

        self.secondary_backups = QCheckBox(
            "🗂️ Guardar uma segunda cópia noutra localização"
        )
        self.secondary_backups.setChecked(
            bool(backup_prefs.get("secondary_backup_enabled", False))
        )
        self.secondary_backup_path = QLineEdit(
            str(backup_prefs.get("secondary_backup_directory") or "")
        )
        self.secondary_backup_path.setPlaceholderText(
            "Ex.: disco externo, NAS ou pasta sincronizada"
        )
        self.secondary_backup_path.setMinimumWidth(280)
        self.choose_secondary_backup = QPushButton("Escolher pasta")
        secondary_row = FlowLayout(hspacing=8, vspacing=8)
        secondary_row.addWidget(self.secondary_backups)
        secondary_row.addWidget(self.secondary_backup_path)
        secondary_row.addWidget(self.choose_secondary_backup)
        backup_card.v.addLayout(secondary_row)

        self.last_backup = QLabel()
        self.last_backup.setObjectName("Subtle")
        self.last_backup.setWordWrap(True)
        backup_card.v.addWidget(self.last_backup)
        self.backup_health = QLabel()
        self.backup_health.setObjectName("MetricStatus")
        self.backup_health.setWordWrap(True)
        backup_card.v.addWidget(self.backup_health)
        self.backup_recommendations = QLabel()
        self.backup_recommendations.setObjectName("Subtle")
        self.backup_recommendations.setWordWrap(True)
        backup_card.v.addWidget(self.backup_recommendations)
        self.backup_save_status = QLabel(
            "As alterações de backup ficam guardadas automaticamente."
        )
        self.backup_save_status.setObjectName("Subtle")
        self.backup_save_status.setWordWrap(True)
        backup_card.v.addWidget(self.backup_save_status)
        self.backup_save_timer = QTimer(self)
        self.backup_save_timer.setSingleShot(True)
        self.backup_save_timer.timeout.connect(
            lambda: self.save_backup_settings(silent=True)
        )

        actions = FlowLayout(hspacing=8, vspacing=8)
        self.manual_backup = QPushButton("Backup completo agora")
        self.export_zip = QPushButton("Exportar ZIP completo")
        self.export_zip.setProperty("privacyExportExempt", True)
        self.export_zip.setToolTip(
            "Cópia integral de segurança: preserva todos os dados mesmo com "
            "o modo privacidade ativo."
        )
        self.restore_btn = QPushButton("Recuperar backup")
        self.recovery_drill_btn = QPushButton("Testar recuperação")
        self.open_backup = QPushButton("Abrir pasta backups")
        for button in [
            self.manual_backup,
            self.export_zip,
            self.restore_btn,
            self.recovery_drill_btn,
            self.open_backup,
        ]:
            actions.addWidget(button)
        backup_card.v.addLayout(actions)

        ai_card = GlassCard("IA Gemini", strong=True)
        ai_card.v.setSpacing(8)
        ai_prefs = self.ai_service.preferences()
        self.ai_enabled = QCheckBox("Usar Google Gemini nesta app")
        self.ai_enabled.setChecked(bool(ai_prefs.get("enabled")))
        self.ai_use_env = QCheckBox("Usar variável de ambiente GEMINI_API_KEY")
        self.ai_use_env.setChecked(bool(ai_prefs.get("use_env")))
        ai_card.v.addWidget(self.ai_enabled)
        ai_card.v.addWidget(self.ai_use_env)
        ai_form = QFormLayout()
        ai_form.setContentsMargins(0, 0, 0, 0)
        ai_form.setSpacing(8)
        self.ai_key = QLineEdit()
        self.ai_key.setEchoMode(QLineEdit.Password)
        self.ai_key.setPlaceholderText(
            (
                "Deixa vazio para manter a chave atual"
                if ai_prefs.get("has_api_key")
                else "Cola aqui uma nova chave Gemini"
            )
        )
        self.ai_model = QComboBox()
        self.ai_model.setEditable(True)
        self.ai_model.setMinimumWidth(260)
        self.ai_model.setToolTip(
            "Escolhe um modelo Gemini. A lista pode ser atualizada pela API ou podes escrever o ID manualmente."
        )
        current_model = str(ai_prefs.get("model", "gemini-2.5-flash"))
        for model in GEMINI_RECOMMENDED_TEXT_MODELS:
            self.ai_model.addItem(model)
        idx_model = self.ai_model.findText(current_model)
        if idx_model >= 0:
            self.ai_model.setCurrentIndex(idx_model)
        else:
            self.ai_model.setEditText(current_model)
        self.ai_refresh_models = QPushButton("Atualizar modelos")
        self.ai_refresh_models.setToolTip(
            "Vai buscar à Gemini API os modelos disponíveis para a tua chave."
        )
        model_container = QWidget()
        model_row = QHBoxLayout(model_container)
        model_row.setContentsMargins(0, 0, 0, 0)
        model_row.setSpacing(8)
        model_row.addWidget(self.ai_model, 1)
        model_row.addWidget(self.ai_refresh_models)
        ai_form.addRow("Chave API", self.ai_key)
        ai_form.addRow("Modelo", model_container)
        ai_card.v.addLayout(ai_form)
        self.ai_aggregated = QCheckBox("Enviar por defeito apenas dados agregados")
        self.ai_aggregated.setChecked(bool(ai_prefs.get("privacy_aggregated", True)))
        self.ai_anonymize = QCheckBox("Anonimizar sempre que possível")
        self.ai_anonymize.setChecked(bool(ai_prefs.get("anonymize", True)))
        self.ai_ask_details = QCheckBox(
            "Pedir confirmação antes de enviar dados detalhados"
        )
        self.ai_ask_details.setChecked(bool(ai_prefs.get("ask_before_details", True)))
        ai_card.v.addWidget(self.ai_aggregated)
        ai_card.v.addWidget(self.ai_anonymize)
        ai_card.v.addWidget(self.ai_ask_details)
        ai_actions = QHBoxLayout()
        ai_actions.setContentsMargins(0, 0, 0, 0)
        ai_actions.setSpacing(8)
        self.ai_save = QPushButton("Guardar IA")
        self.ai_save.setObjectName("Primary")
        self.ai_test = QPushButton("Testar ligação Gemini")
        self.ai_cancel = QPushButton("Cancelar")
        self.ai_cancel.setEnabled(False)
        self.ai_status = QLabel()
        self.ai_status.setObjectName("Subtle")
        self.ai_status.setWordWrap(True)
        ai_actions.addWidget(self.ai_save)
        ai_actions.addWidget(self.ai_test)
        ai_actions.addWidget(self.ai_cancel)
        ai_actions.addStretch(1)
        ai_card.v.addLayout(ai_actions)
        ai_card.v.addWidget(self.ai_status)

        maintenance_card = GlassCard("🧹 Gestão dos movimentos", strong=True)
        maintenance_card.v.setSpacing(7)
        maintenance_note = QLabel(
            "Remove todos os movimentos e respetivos metadados/anexos ligados, "
            "sem apagar categorias, contas, orçamentos, subscrições, objetivos "
            "ou investimentos. Antes da limpeza é criado e validado um backup completo."
        )
        maintenance_note.setObjectName("Subtle")
        maintenance_note.setWordWrap(True)
        maintenance_card.fit_label_height(
            maintenance_note,
            word_wrap=True,
            stable_measurement=True,
        )
        self.clear_movements_status = QLabel()
        self.clear_movements_status.setObjectName("MetricStatus")
        self.clear_movements_status.setProperty("tone", "warning")
        self.clear_movements_status.setWordWrap(True)
        self.clear_all_movements_btn = QPushButton("Apagar todos os movimentos…")
        self.clear_all_movements_btn.setObjectName("Danger")
        self.clear_all_movements_btn.setToolTip(
            "Exige duas confirmações e um backup completo validado antes de apagar."
        )
        maintenance_card.v.addWidget(maintenance_note)
        maintenance_card.v.addWidget(self.clear_movements_status)
        maintenance_card.v.addWidget(
            self.clear_all_movements_btn,
            0,
        )
        self._update_ai_status()

        paths = GlassCard("Localização dos dados")
        paths.v.setSpacing(5)
        database_location = Path(self.repo.db.path)
        attachments_location = Path(self.app.document_service.attachments_directory)
        db_label = QLabel(f"Base de dados: {database_location}")
        db_label.setObjectName("Subtle")
        db_label.setWordWrap(True)
        self.backup_location_label = QLabel(
            f"Pasta de backups: {self.backup_service.backup_directory}"
        )
        self.backup_location_label.setObjectName("Subtle")
        self.backup_location_label.setWordWrap(True)
        attachments_label = QLabel(f"Documentos anexados: {attachments_location}")
        attachments_label.setObjectName("Subtle")
        attachments_label.setWordWrap(True)
        history_label = QLabel(
            f"Histórico de alterações: {self.backup_service.history_directory}"
        )
        history_label.setObjectName("Subtle")
        history_label.setWordWrap(True)
        preferences_label = QLabel(
            f"Preferências locais: {self.backup_service.preferences_file}"
        )
        preferences_label.setObjectName("Subtle")
        preferences_label.setWordWrap(True)
        protection_directory = version_protection_dir(database_location)
        protection_label = QLabel(
            f"Proteções antes de atualizar: {protection_directory}"
        )
        protection_label.setObjectName("Subtle")
        protection_label.setWordWrap(True)
        protection_status = QLabel(
            "🛡️ Proteção de versões ativa: antes da primeira escrita de cada "
            "nova versão é criada e verificada uma cópia dos movimentos, "
            "documentos, histórico e preferências."
        )
        protection_status.setObjectName("MetricStatus")
        protection_status.setProperty("tone", "positive")
        protection_status.setWordWrap(True)
        shutdown_status = QLabel(
            "✅ Encerramento protegido: a app aguarda tarefas, guarda alterações "
            "pendentes, valida e sincroniza a base de dados e fecha a ligação "
            "explicitamente. Se alguma etapa falhar, a janela permanece aberta."
        )
        shutdown_status.setObjectName("MetricStatus")
        shutdown_status.setProperty("tone", "positive")
        shutdown_status.setWordWrap(True)
        note = QLabel(
            "Os dados ficam separados dos ficheiros do programa e sobrevivem à "
            "substituição ou extração de uma nova versão. A app continua local/offline."
        )
        note.setObjectName("Subtle")
        note.setWordWrap(True)
        paths.v.addWidget(db_label)
        paths.v.addWidget(self.backup_location_label)
        paths.v.addWidget(attachments_label)
        paths.v.addWidget(history_label)
        paths.v.addWidget(preferences_label)
        paths.v.addWidget(protection_label)
        paths.v.addWidget(protection_status)
        paths.v.addWidget(shutdown_status)
        paths.v.addWidget(note)

        bottom = QHBoxLayout()
        bottom.setContentsMargins(0, 0, 0, 0)
        bottom.setSpacing(8)
        open_db = QPushButton("Abrir pasta da base de dados")
        open_protections = QPushButton("Abrir proteções de versão")
        bottom.addWidget(open_db)
        bottom.addWidget(open_protections)
        bottom.addStretch(1)
        paths.v.addLayout(bottom)

        self.body.addWidget(prefs)
        updater = getattr(self.app, "app_updates", None)
        if isinstance(updater, AppUpdateController):
            self.body.addWidget(updater.make_card())
        self.body.addWidget(backup_card)
        self.body.addWidget(ai_card)
        self.body.addWidget(maintenance_card)
        self.body.addWidget(paths)
        self.body.addStretch(1)

        self.dark.stateChanged.connect(self.update_theme)
        self.privacy.stateChanged.connect(self.update_theme)
        save_backup.clicked.connect(self.save_backup_settings)
        self.change_credential_button.clicked.connect(
            self.open_change_credential_dialog
        )
        self.ai_save.clicked.connect(self.save_ai_settings)
        self.ai_test.clicked.connect(self.test_ai_connection)
        self.ai_refresh_models.clicked.connect(self.refresh_gemini_models)
        self.ai_cancel.clicked.connect(self.cancel_settings_ai_operation)
        self.ai_enabled.stateChanged.connect(self.mark_ai_settings_dirty)
        self.ai_use_env.stateChanged.connect(self.mark_ai_settings_dirty)
        self.ai_model.currentTextChanged.connect(self.mark_ai_settings_dirty)
        self.ai_aggregated.stateChanged.connect(self.mark_ai_settings_dirty)
        self.ai_anonymize.stateChanged.connect(self.mark_ai_settings_dirty)
        self.ai_ask_details.stateChanged.connect(self.mark_ai_settings_dirty)
        self.ai_save_timer = QTimer(self)
        self.ai_save_timer.setSingleShot(True)
        self.ai_save_timer.timeout.connect(lambda: self.save_ai_settings(silent=True))
        self.auto_interval.currentIndexChanged.connect(self.mark_backup_settings_dirty)
        self.keep_backups.valueChanged.connect(self.mark_backup_settings_dirty)
        self.backup_on_close.stateChanged.connect(self.mark_backup_settings_dirty)
        self.encrypt_backups.stateChanged.connect(self.mark_backup_settings_dirty)
        self.secondary_backups.stateChanged.connect(self.mark_backup_settings_dirty)
        self.secondary_backup_path.textChanged.connect(self.mark_backup_settings_dirty)
        self.choose_primary_backup.clicked.connect(self.choose_primary_backup_directory)
        self.reset_primary_backup.clicked.connect(
            self.use_default_primary_backup_directory
        )
        self.choose_secondary_backup.clicked.connect(
            self.choose_secondary_backup_directory
        )
        self.manual_backup.clicked.connect(self.backup_database)
        self.export_zip.clicked.connect(self.export_backup)
        self.restore_btn.clicked.connect(self.restore_backup)
        self.clear_all_movements_btn.clicked.connect(self.clear_all_movements)
        self.recovery_drill_btn.clicked.connect(self.test_backup_recovery)
        self.open_backup.clicked.connect(
            lambda: open_path(self.backup_service.backup_directory)
        )
        open_db.clicked.connect(lambda: open_path(database_location.parent))
        open_protections.clicked.connect(lambda: open_path(protection_directory))
        self.auto_interval.installEventFilter(self)
        self.keep_backups.installEventFilter(self)
        self.ai_model.installEventFilter(self)
        self.refresh()

    def eventFilter(self, obj, event):
        from PySide6.QtCore import QEvent
        from PySide6.QtWidgets import QComboBox, QDoubleSpinBox, QSpinBox

        if event.type() == QEvent.Wheel and isinstance(
            obj, (QComboBox, QSpinBox, QDoubleSpinBox)
        ):
            if not obj.hasFocus():
                event.ignore()
                return True
        return super().eventFilter(obj, event)

    def _update_ai_status(self) -> None:
        try:
            prefs = self.ai_service.preferences()
            if prefs.get("legacy_key_pending"):
                self.ai_status.setText(
                    "Aviso: a chave antiga ainda não foi migrada para o Gestor de Credenciais do Windows."
                )
                return
            if prefs.get("enabled") and prefs.get("has_api_key"):
                if prefs.get("use_env") and prefs.get("env_key_present"):
                    source = "GEMINI_API_KEY"
                    masked = "definida"
                else:
                    source = "Credencial Windows"
                    masked = str(prefs.get("key_hint") or "configurada")
                self.ai_status.setText(
                    f"Estado: Gemini ativo · modelo {prefs.get('model')} · {source}: {masked}"
                )
            elif prefs.get("enabled"):
                self.ai_status.setText("Estado: Gemini ativo, mas falta a chave API.")
            else:
                self.ai_status.setText("Estado: Gemini desligado.")
        except Exception as exc:
            self.ai_status.setText(f"Estado IA indisponível: {exc}")

    def mark_ai_settings_dirty(self, *args: Any) -> None:
        try:
            self.ai_status.setText("Definições de IA alteradas…")
            if hasattr(self, "ai_save_timer"):
                self.ai_save_timer.start(800)
        except Exception as _exc:
            log_exception(
                f"{type(self).__name__} failed to mark AI settings dirty", _exc
            )

    def save_ai_settings(self, silent: bool = False) -> bool:
        try:
            self.ai_service.save_preferences(
                enabled=bool(self.ai_enabled.isChecked()),
                use_env=bool(self.ai_use_env.isChecked()),
                api_key="" if silent else self.ai_key.text().strip(),
                model=self.ai_model.currentText().strip(),
                privacy_aggregated=bool(self.ai_aggregated.isChecked()),
                anonymize=bool(self.ai_anonymize.isChecked()),
                ask_before_details=bool(self.ai_ask_details.isChecked()),
            )
        except Exception as exc:
            self.ai_status.setText(
                f"Não foi possível guardar a chave com segurança: {exc}"
            )
            if not silent:
                QMessageBox.warning(
                    self,
                    APP_TITLE,
                    f"Não foi possível guardar as definições de IA:\n{exc}",
                )
            return False
        if not silent:
            self.ai_key.clear()
            self.ai_key.setPlaceholderText("Deixa vazio para manter a chave atual")
        self._update_ai_status()
        if not silent:
            QMessageBox.information(self, APP_TITLE, "Definições de IA guardadas.")
        return True

    def _set_settings_ai_busy(self, busy: bool, message: str = "") -> None:
        controls = [
            self.ai_enabled,
            self.ai_use_env,
            self.ai_key,
            self.ai_model,
            self.ai_refresh_models,
            self.ai_test,
            self.ai_save,
            self.ai_aggregated,
            self.ai_anonymize,
            self.ai_ask_details,
        ]
        for control in controls:
            control.setEnabled(not busy)
        self.ai_cancel.setEnabled(busy)
        if busy:
            self._settings_ai_busy_message = message
            self._settings_ai_elapsed_seconds = 0
            self.ai_status.setText(f"{message} · 0 s")
            self.settings_ai_elapsed_timer.start()
        else:
            self.settings_ai_elapsed_timer.stop()

    def _update_settings_ai_elapsed(self) -> None:
        if not self.ai_task.is_running():
            return
        self._settings_ai_elapsed_seconds += 1
        self.ai_status.setText(
            f"{self._settings_ai_busy_message} · {self._settings_ai_elapsed_seconds} s"
        )

    def _start_settings_ai_task(
        self,
        callback: Callable[[], Any],
        *,
        operation: str,
        message: str,
        current_model: str = "",
    ) -> bool:
        if self.ai_task.is_running():
            return False
        if self.ai_save_timer.isActive():
            self.ai_save_timer.stop()
        self._settings_ai_operation = operation
        self._settings_ai_current_model = current_model
        if not self.ai_task.start(callback):
            self._settings_ai_operation = ""
            return False
        self._set_settings_ai_busy(True, message)
        return True

    def cancel_settings_ai_operation(self) -> None:
        if self.ai_task.cancel():
            self.ai_cancel.setEnabled(False)
            self.ai_status.setText("A cancelar a operação Gemini…")

    def _on_settings_ai_result(self, value: Any) -> None:
        self.settings_ai_elapsed_timer.stop()
        if self._settings_ai_operation == "models":
            models = list(value) if isinstance(value, (list, tuple)) else []
            current = self._settings_ai_current_model
            self.ai_model.blockSignals(True)
            try:
                self.ai_model.clear()
                for model in models:
                    self.ai_model.addItem(str(model))
                index = self.ai_model.findText(current)
                if index >= 0:
                    self.ai_model.setCurrentIndex(index)
                else:
                    self.ai_model.setEditText(current)
            finally:
                self.ai_model.blockSignals(False)
            if not self.save_ai_settings(silent=True):
                return
            self.ai_status.setText(
                f"Lista atualizada: {len(models)} modelos disponíveis. "
                f"Modelo actual: {self.ai_model.currentText()}."
            )
        else:
            self.ai_status.setText(f"Ligação Gemini OK: {str(value)[:180]}")

    def _on_settings_ai_error(self, message: str) -> None:
        self.settings_ai_elapsed_timer.stop()
        if self._settings_ai_operation == "models":
            self.ai_status.setText(
                f"Não foi possível atualizar modelos Gemini: {message}"
            )
        else:
            self.ai_status.setText(f"Erro Gemini: {message}")

    def _on_settings_ai_finished(self, cancelled: bool) -> None:
        self._set_settings_ai_busy(False)
        if cancelled:
            self.ai_status.setText("Operação Gemini cancelada.")
        self._settings_ai_operation = ""
        self._settings_ai_current_model = ""

    def refresh_gemini_models(self) -> None:
        if self.ai_task.is_running():
            return
        if not self.save_ai_settings(silent=True):
            return
        preferences = self.ai_service.preferences()
        current = self.ai_model.currentText().strip() or str(
            preferences.get("model", "gemini-2.5-flash")
        )
        try:
            callback = self.ai_service.prepare_list_models(model=current)
        except Exception as exc:
            self.ai_status.setText(f"Não foi possível atualizar modelos Gemini: {exc}")
            return
        self._start_settings_ai_task(
            callback,
            operation="models",
            message="A atualizar a lista de modelos Gemini",
            current_model=current,
        )

    def test_ai_connection(self) -> None:
        if self.ai_task.is_running():
            return
        if not self.save_ai_settings(silent=True):
            return
        try:
            callback = self.ai_service.prepare_generate(
                "Responde em português de Portugal apenas com: ligação OK."
            )
        except Exception as exc:
            self.ai_status.setText(f"Erro Gemini: {exc}")
            return
        self._start_settings_ai_task(
            callback,
            operation="test",
            message="A testar ligação ao Gemini",
        )

    def refresh(self) -> None:
        last = self.repo.setting(
            "last_auto_backup_file", "Ainda sem backup automático."
        )
        prefs = self.backup_service.preferences()
        interval = int(prefs.get("auto_backup_interval_minutes", 1440))
        keep = int(prefs.get("backup_keep", 15))
        close_status = "sim" if bool(prefs.get("backup_on_close", False)) else "não"
        encryption_enabled = bool(prefs.get("backup_encryption_enabled", False))
        status = (
            "desativado"
            if interval <= 0
            else f"a cada {backup_interval_label(interval)}"
        )
        secondary_status = (
            "ativa"
            if bool(prefs.get("secondary_backup_enabled", False))
            else "desativada"
        )
        self.last_backup.setText(
            f"Estado: backup automático {status}; backup ao fechar: {close_status}; "
            f"cifragem: {'ativa' if encryption_enabled else 'desativada'}; "
            f"segunda localização: {secondary_status}; manter {keep} cópias. "
            f"Último backup: {last}"
        )
        effective_primary = str(
            prefs.get("effective_backup_directory")
            or self.backup_service.backup_directory
        )
        configured_primary = str(prefs.get("primary_backup_directory") or "").strip()
        self.primary_backup_uses_default = not bool(configured_primary)
        self.primary_backup_path.setText(configured_primary or effective_primary)
        self.backup_location_label.setText(f"Pasta de backups: {effective_primary}")
        primary_error = str(prefs.get("primary_backup_directory_error") or "").strip()
        if primary_error:
            self.backup_save_status.setText("⚠️ " + primary_error)
        movement_count = int(self.repo.movement_count() or 0)
        if self._backup_operation != "clear_movements":
            self.clear_movements_status.setText(
                f"Movimentos atualmente guardados: {movement_count}. "
                "A limpeza não é automática e nunca é executada sem confirmação."
            )
        self.clear_all_movements_btn.setEnabled(
            movement_count > 0 and not self.backup_task.is_running()
        )
        fingerprint = self.backup_service.latest_backup_fingerprint()
        if (
            self._backup_health_result
            and fingerprint == self._backup_health_fingerprint
        ):
            health = dict(self._backup_health_result)
            current_verification = True
        else:
            health = self.backup_service.protection_health_snapshot()
            current_verification = False
        status = self.security_controller.security_status(
            cached_verification=self._backup_health_result if current_verification else None,
            cached_fingerprint=self._backup_health_fingerprint,
        )
        self._render_security_status(status)
        self._render_backup_health(health, current=current_verification)
        if (
            fingerprint is not None
            and fingerprint != self._backup_health_fingerprint
            and not self.backup_health_task.is_running()
        ):
            self.backup_health.setText(
                self.backup_health.text() + " · 🔄 a validar agora em segundo plano"
            )
            database_file = Path(self.repo.db.path)
            service_type = type(self.backup_service)
            self.backup_health_task.start(
                lambda target=database_file: (
                    service_type.run_verify_latest_backup_for_database(target)
                )
            )

    @staticmethod
    def _verification_time(value: object) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        try:
            return datetime.fromisoformat(text).strftime("%d/%m/%Y às %H:%M:%S")
        except ValueError:
            return text

    def _render_backup_health(
        self,
        health: dict[str, Any],
        *,
        current: bool,
    ) -> None:
        score = int(health.get("score", 0))
        self.backup_health.setProperty(
            "tone",
            "positive" if score >= 80 else "warning" if score >= 40 else "danger",
        )
        verified_at = self._verification_time(
            health.get("verified_at") if current else health.get("last_verified_at")
        )
        verification_label = (
            f"Verificado realmente em {verified_at}."
            if current and health.get("verified") and verified_at
            else f"Verificação registada em {verified_at}; falta confirmar o ficheiro atual."
            if verified_at
            else "Ainda sem validação integral registada."
        )
        self.backup_health.setText(
            f"🛡️ Proteção dos dados: {score}/100 · {health.get('message', '')} "
            f"{verification_label}"
        )
        recommendations = [
            str(value).strip()
            for value in health.get("recommendations", [])
            if str(value).strip()
        ]
        self.backup_recommendations.setText(
            "Próximos passos recomendados: " + " ".join(recommendations)
            if recommendations
            else (
                "Proteção completa: backup validado, cifrado e duplicado."
                if current
                else "A confirmação real do ficheiro será feita em segundo plano."
            )
        )
        self.backup_health.style().unpolish(self.backup_health)
        self.backup_health.style().polish(self.backup_health)

    def _render_security_status(self, status: SecurityStatus) -> None:
        if status.state == "protected":
            badge = "🛡️ Protegido"
        elif status.state == "recovery_required":
            badge = "⚠️ Recuperação necessária"
        else:
            badge = "⚠️ Atenção"
        methods_text = (
            ", ".join(
                method.kind + (" (predefinido)" if method.is_default else "")
                for method in status.methods
            )
            or "nenhum método configurado"
        )
        verif_labels = {
            BackupVerificationStatus.CURRENT_VERIFIED: "sim",
            BackupVerificationStatus.RECORDED_PREVIOUSLY: "registo anterior",
            BackupVerificationStatus.VERIFICATION_FAILED: "falhou",
            BackupVerificationStatus.NO_BACKUP: "não",
            BackupVerificationStatus.UNAVAILABLE: "indisponível",
        }
        verif_display = verif_labels.get(status.verification_status, "não")
        format_display = status.configured_format
        if status.latest_backup and status.latest_format not in {"nenhum", ""}:
            archive_info = f"{status.latest_backup} ({status.latest_format})"
        else:
            archive_info = status.latest_backup or "nenhum"

        self.encryption_status.setText(
            f"{badge} · Métodos: {methods_text} · Formato: "
            f"{format_display} · Último backup: "
            f"{archive_info} · "
            f"verificado: {verif_display} · "
            "A credencial nunca é escrita na base de dados nem no backup."
        )

    def _on_backup_health_result(self, result: Any) -> None:
        self._backup_health_fingerprint = (
            self.backup_service.latest_backup_fingerprint()
        )
        self._backup_health_result = dict(result or {})
        self._render_backup_health(self._backup_health_result, current=True)
        status = self.security_controller.security_status(
            cached_verification=self._backup_health_result,
            cached_fingerprint=self._backup_health_fingerprint,
        )
        self._render_security_status(status)

    def _on_backup_health_error(self, message: str) -> None:
        self.backup_health.setProperty("tone", "danger")
        self.backup_health.setText(
            "🛡️ Não foi possível confirmar agora o backup mais recente: " + str(message)
        )
        status = self.security_controller.security_status()
        self._render_security_status(status)

    def update_theme(self) -> None:
        self.app.repo.set_setting("liquid_dark", "1" if self.dark.isChecked() else "0")
        self.app.repo.set_setting(
            "liquid_privacy", "1" if self.privacy.isChecked() else "0"
        )
        self.app.repo.set_setting("liquid_primary", self.primary_color_cb.currentText())
        self.app.apply_theme()

    def update_font(self) -> None:
        val = self.font_size.currentText()
        self.repo.set_setting("global_font_scale", val)
        self.app.apply_font_scale()

    def mark_backup_settings_dirty(self, *args: Any) -> None:
        try:
            self.backup_save_status.setText("Alterações por guardar…")
            self.backup_save_timer.start(700)
        except Exception as _exc:
            log_exception(
                f"{type(self).__name__} failed to mark backup settings dirty", _exc
            )

    def flush_pending_state(self) -> bool:
        """Save every debounced preference before the database is finalized."""

        saved = True
        if self.backup_save_timer.isActive():
            self.backup_save_timer.stop()
            saved = self.save_backup_settings(silent=True) and saved
        if self.ai_save_timer.isActive():
            self.ai_save_timer.stop()
            saved = self.save_ai_settings(silent=True) and saved
        return saved

    def open_change_credential_dialog(self) -> None:
        """Open the dedicated Liquid Glass change-credential dialog."""

        from ui.change_credential import ChangeCredentialDialog

        dialog = ChangeCredentialDialog(self.security_controller, parent=self)
        dialog.change_finished.connect(lambda ok: self.refresh())
        try:
            dialog.exec()
        finally:
            dialog.deleteLater()

    def save_backup_settings(self, silent: bool = False) -> bool:
        interval = int(self.auto_interval.currentData() or 0)
        keep = int(self.keep_backups.value())
        close_enabled = bool(self.backup_on_close.isChecked())
        encryption_enabled = bool(self.encrypt_backups.isChecked())
        primary_directory = self._primary_backup_preference_value()
        try:
            self.backup_service.save_preferences(
                interval,
                keep,
                close_enabled,
                encryption_enabled,
                bool(self.secondary_backups.isChecked()),
                self.secondary_backup_path.text().strip(),
                primary_directory,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            self.backup_save_status.setText(str(exc))
            if not silent:
                QMessageBox.warning(self, APP_TITLE, str(exc))
            return False
        self.backup_service.prune_backups()
        self.refresh()
        try:
            saved_at = datetime.now().strftime("%H:%M:%S")
            self.backup_save_status.setText(
                f"Definições guardadas às {saved_at}. "
                f"Ficheiro: {self.backup_service.preferences_file.name}"
            )
        except Exception as _exc:
            log_exception(
                f"{type(self).__name__} failed to update backup save status", _exc
            )
        if not silent:
            QMessageBox.information(self, APP_TITLE, "Definições de backup guardadas.")
        return True

    def _primary_backup_preference_value(self) -> str:
        return (
            ""
            if self.primary_backup_uses_default
            else self.primary_backup_path.text().strip()
        )

    def choose_primary_backup_directory(self) -> None:
        current = self.primary_backup_path.text().strip()
        selected = QFileDialog.getExistingDirectory(
            self,
            "Escolher pasta principal de backups",
            current or str(Path.home()),
        )
        if not selected:
            return
        self.primary_backup_uses_default = False
        self.primary_backup_path.setText(selected)
        self.mark_backup_settings_dirty()

    def use_default_primary_backup_directory(self) -> None:
        self.primary_backup_uses_default = True
        self.primary_backup_path.setText(
            str(self.backup_service.default_backup_directory)
        )
        self.mark_backup_settings_dirty()

    def choose_secondary_backup_directory(self) -> None:
        current = self.secondary_backup_path.text().strip()
        selected = QFileDialog.getExistingDirectory(
            self,
            "Escolher segunda localização de backup",
            current or str(Path.home()),
        )
        if selected:
            self.secondary_backup_path.setText(selected)
            self.secondary_backups.setChecked(True)

    def clear_all_movements(self) -> None:
        count = int(self.repo.movement_count() or 0)
        if count <= 0:
            QMessageBox.information(
                self,
                APP_TITLE,
                "Não existem movimentos para apagar.",
            )
            return
        warning = QMessageBox(self)
        warning.setWindowTitle("Apagar todos os movimentos")
        warning.setIcon(QMessageBox.Icon.Warning)
        warning.setText(
            f"Serão apagados {count} movimentos e todos os dados diretamente "
            "associados."
        )
        warning.setInformativeText(
            "As categorias, contas, orçamentos, subscrições, objetivos e "
            "investimentos serão mantidos. Primeiro será criado e validado "
            "um backup completo. Queres continuar?"
        )
        warning.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        warning.setDefaultButton(QMessageBox.No)
        yes_button = warning.button(QMessageBox.Yes)
        no_button = warning.button(QMessageBox.No)
        if yes_button is not None:
            yes_button.setText("Continuar")
        if no_button is not None:
            no_button.setText("Cancelar")
        apply_message_box_theme(warning, self.app.theme)
        answer = QMessageBox.StandardButton(warning.exec())
        if answer != QMessageBox.Yes:
            return
        confirmation, accepted = QInputDialog.getText(
            self,
            "Confirmação final",
            "Escreve APAGAR TODOS para confirmar a limpeza integral:",
        )
        if not accepted:
            return
        if confirmation.strip() != "APAGAR TODOS":
            QMessageBox.warning(
                self,
                APP_TITLE,
                "A frase de confirmação não corresponde. Nenhum dado foi alterado.",
            )
            return
        if not self.save_backup_settings(silent=True):
            return
        database_file = Path(self.repo.db.path)
        service_type = type(self.backup_service)
        self._pending_clear_movements_count = count
        self.clear_movements_status.setText(
            f"🛡️ A criar e validar o backup de proteção antes de apagar {count} movimentos…"
        )
        self._start_backup_task(
            "clear_movements",
            lambda: service_type.run_manual_backup_for_database(database_file),
        )

    def test_backup_recovery(self) -> None:
        if not self.save_backup_settings(silent=True):
            return
        database_file = Path(self.repo.db.path)
        service_type = type(self.backup_service)
        self._start_backup_task(
            "drill",
            lambda: service_type.run_recovery_drill_for_database(database_file),
        )

    def backup_database(self) -> None:
        if not self.save_backup_settings(silent=True):
            return
        database_file = Path(self.repo.db.path)
        service_type = type(self.backup_service)
        self._start_backup_task(
            "manual",
            lambda: service_type.run_manual_backup_for_database(database_file),
        )

    def _start_backup_task(self, operation: str, callback: Callable[[], Any]) -> None:
        if self.backup_task.is_running():
            return
        self._backup_operation = str(operation)
        self.manual_backup.setEnabled(False)
        self.recovery_drill_btn.setEnabled(False)
        self.restore_btn.setEnabled(False)
        self.export_zip.setEnabled(False)
        if operation == "export":
            self.export_zip.setText("A exportar…")
        self.manual_backup.setText(
            "A criar…" if operation == "manual" else "Backup completo agora"
        )
        self.recovery_drill_btn.setText(
            "A testar…" if operation == "drill" else "Testar recuperação"
        )
        self.clear_all_movements_btn.setEnabled(False)
        if operation == "clear_movements":
            self.clear_all_movements_btn.setText("A proteger os dados…")
        if not self.backup_task.start(callback):
            self._on_backup_task_error("Não foi possível iniciar a tarefa de backup.")
            self._on_backup_task_finished(False)

    def _on_backup_task_result(self, result: Any) -> None:
        if self._backup_operation == "export":
            path_str, label = (
                result if isinstance(result, tuple) else (str(result), "Backup")
            )
            QMessageBox.information(
                self,
                APP_TITLE,
                f"{label} criado em:\n{path_str}",
            )
            return
        if self._backup_operation == "manual":
            QMessageBox.information(self, APP_TITLE, f"Backup criado em:\n{result}")
            return
        if self._backup_operation == "clear_movements":
            backup_path = Path(str(result))
            try:
                report = self.app.document_service.clear_all_transactions()
                # BUG-DB-001: the only remaining direct GUI-thread access to
                # the shared SQLite connection; route it through the locked
                # accessor so workers cannot interleave on the connection.
                with self.repo.db._locked_connection() as conn:
                    integrity_row = conn.execute(
                        "PRAGMA quick_check(1)"
                    ).fetchone()
                integrity = str(integrity_row[0] if integrity_row else "")
                if integrity.casefold() != "ok":
                    raise RuntimeError(
                        "A verificação SQLite após a limpeza não devolveu 'ok'."
                    )
                self.app.notify_data_changed("Todos os movimentos foram apagados")
                self.app.refresh_undo_actions()
                self.clear_movements_status.setText(
                    f"✅ {int(report['movements'])} movimentos removidos; "
                    f"{int(report['attachment_files'])} ficheiros de anexos limpos."
                )
                QMessageBox.information(
                    self,
                    "Movimentos apagados",
                    f"Foram apagados {int(report['movements'])} movimentos.\n\n"
                    f"O backup completo e validado ficou guardado em:\n{backup_path}",
                )
            except Exception as exc:
                log_exception("Failed to clear all movements after safety backup", exc)
                QMessageBox.critical(
                    self,
                    APP_TITLE,
                    "O backup foi criado, mas a limpeza não foi concluída com "
                    f"segurança.\n\nBackup: {backup_path}\n\nDetalhe: {exc}",
                )
            finally:
                self._pending_clear_movements_count = 0
            return
        data = dict(result or {})
        QMessageBox.information(
            self,
            APP_TITLE,
            "Teste de recuperação concluído sem alterar os dados atuais.\n\n"
            f"Backup: {Path(data['file']).name}\n"
            f"Tabelas verificadas: {len(data.get('tables', {}))}\n"
            f"Ficheiros verificados: {int(data.get('files', 0))}",
        )

    def _on_backup_task_error(self, message: str) -> None:
        label = (
            "exportar o backup"
            if self._backup_operation == "export"
            else "criar o backup"
            if self._backup_operation in {"manual", "clear_movements"}
            else "concluir o teste de recuperação"
        )
        QMessageBox.critical(self, APP_TITLE, f"Não foi possível {label}:\n{message}")

    def _on_backup_task_finished(self, _cancelled: bool) -> None:
        self.manual_backup.setEnabled(True)
        self.recovery_drill_btn.setEnabled(True)
        self.restore_btn.setEnabled(True)
        self.export_zip.setEnabled(True)
        self.export_zip.setText("Exportar ZIP completo")
        self.clear_all_movements_btn.setText("Apagar todos os movimentos…")
        self.manual_backup.setText("Backup completo agora")
        self.recovery_drill_btn.setText("Testar recuperação")
        self._backup_operation = ""
        self.refresh()

    def export_backup(self) -> None:
        if not self.save_backup_settings(silent=True):
            return
        default = self.backup_service.default_export_path()
        encrypted = bool(
            self.backup_service.preferences().get(
                "backup_encryption_enabled",
                False,
            )
        )
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Guardar backup cifrado" if encrypted else "Guardar backup ZIP",
            str(default),
            ("Backup cifrado (*.lgbak)" if encrypted else "ZIP (*.zip)"),
        )
        if not path:
            return
        target_path = Path(path)
        if encrypted:
            if not target_path.name.lower().endswith(".lgbak"):
                target_path = target_path.with_suffix(".lgbak")
            service = self.backup_service
            self._start_backup_task(
                "export",
                lambda: (
                    service.export_encrypted_backup(target_path),
                    str(target_path),
                    "Backup cifrado",
                )[1:],
            )
        else:
            if not target_path.name.lower().endswith(".zip"):
                target_path = target_path.with_suffix(".zip")
            service = self.backup_service
            self._start_backup_task(
                "export",
                lambda: (
                    service.export_backup_zip(target_path),
                    str(target_path),
                    "Backup ZIP",
                )[1:],
            )

    def restore_backup(self) -> None:
        source, _ = QFileDialog.getOpenFileName(
            self,
            "Recuperar backup",
            str(self.backup_service.backup_directory),
            "Backups (*.lgbak *.sqlite3 *.db *.zip);;"
            "Cifrados (*.lgbak);;SQLite (*.sqlite3 *.db);;"
            "ZIP (*.zip);;Todos (*.*)",
        )
        if not source:
            return
        answer = QMessageBox.question(
            self,
            APP_TITLE,
            "Isto vai substituir a base de dados atual. Será criada uma cópia de segurança antes de repor. Continuar?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        src = Path(source)
        password: Optional[str] = None
        if self.backup_service.is_encrypted_backup(src):
            password, accepted = QInputDialog.getText(
                self,
                "Palavra-passe do backup",
                "Introduz a palavra-passe deste backup. Deixa em branco para "
                "tentar as credenciais guardadas em segurança:",
                QLineEdit.Password,
            )
            if not accepted:
                return
        try:
            safety = self.backup_service.restore_backup(
                src,
                before_replace=self.app.prepare_database_restore,
                password=password,
            )
            self.app.reload_data_services()
            self.repo = self.app.repo
            self.refresh()
            self.app.notify_data_changed()
            QMessageBox.information(
                self,
                APP_TITLE,
                f"Backup reposto. Cópia anterior guardada em:\n{safety}",
            )
        except Exception as exc:
            self.app.reload_data_services()
            self.repo = self.app.repo
            QMessageBox.critical(
                self, APP_TITLE, f"Não foi possível recuperar o backup:\n{exc}"
            )
