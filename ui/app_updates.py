"""Asynchronous release checks and explicit, shutdown-gated installation."""

from __future__ import annotations

import os
import threading
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QCheckBox, QLabel, QMessageBox, QPushButton

from services import app_updates as updates
from ui.components import GlassCard
from ui.workers import AsyncTask
from utils.config import app_state_root
from utils.version import APP_VERSION

if TYPE_CHECKING:
    from main import LiquidFinanceApp


class _DownloadCall:
    requires_sqlite_lock = False

    def __init__(self, release, progress):
        self.release = release
        self.progress = progress
        self.cancelled = threading.Event()

    def __call__(self):
        return updates.download_update(
            self.release, app_state_root() / "updates",
            progress=self.progress, cancel_event=self.cancelled,
        )

    def cancel(self) -> None:
        self.cancelled.set()


class AppUpdateController(QObject):
    changed = Signal()
    download_progress = Signal(int, int)

    def __init__(self, app: LiquidFinanceApp):
        super().__init__(app)
        self.app = app
        self.release = None
        self.pending = None
        self._downloaded = None
        self._operation = ""
        self.status = f"Versão instalada: {APP_VERSION}."
        self.task = AsyncTask(
            self, registry=app.background_task_registry,
            label="Verificação e descarga de atualizações",
        )
        self.task.result.connect(self._result)
        self.task.error.connect(self._error)
        self.task.finished.connect(self._finished)
        self.download_progress.connect(self._progress)
        self.notification = QPushButton("Nova versão disponível")
        self.notification.setVisible(False)
        self.notification.clicked.connect(lambda: app.goto("definicoes"))
        app.statusBar().addPermanentWidget(self.notification)

    def schedule_startup_check(self) -> None:
        # Source runs and all synthetic profiles stay offline unless requested.
        if updates.managed_install_root() is None:
            return
        if os.environ.get("FINANCAS_PESSOAIS_TESTING") == "1":
            return
        if str(self.app.repo.setting("check_app_updates", "1")) == "1":
            QTimer.singleShot(5000, self, self.check)

    def _start(self, operation: str, callback) -> None:
        if self.task.is_running() or self.app.background_task_registry.is_shutting_down:
            return
        # Network operations do not acquire the shared finance-query lock.
        callback.requires_sqlite_lock = False
        self._operation = operation
        if self.task.start(callback):
            self.status = (
                "A procurar uma nova versão…" if operation == "check"
                else "A descarregar e verificar a atualização…"
            )
            self.changed.emit()

    def check(self) -> None:
        self._start("check", lambda: updates.check_for_update(APP_VERSION))

    def _result(self, value) -> None:
        if self._operation == "check":
            self.release = value
            self.notification.setVisible(value is not None)
            if value is None:
                self.status = "A aplicação está atualizada; não há uma versão estável mais recente."
            else:
                self.status = f"Está disponível a versão {value.version}."
                self.notification.setText(f"Atualizar para {value.version}")
                self.app.statusBar().showMessage(self.status, 15000)
        else:
            self._downloaded = value
            self.status = "Atualização verificada. A concluir o encerramento seguro…"
        self.changed.emit()

    def _error(self, _detail: str) -> None:
        self._downloaded = None
        self.status = (
            "Não foi possível concluir a atualização. Verifica a ligação e tenta novamente. "
            "A versão instalada foi preservada."
        )
        self.changed.emit()

    def _progress(self, received: int, total: int) -> None:
        if self.task.is_running() and self._operation == "download" and total > 0:
            self.status = f"A descarregar a atualização… {min(100, received * 100 // total)}%"
            self.changed.emit()

    def _finished(self, cancelled: bool) -> None:
        if cancelled:
            self._downloaded = None
            self.status = "Verificação ou descarga cancelada."
        if self._downloaded is not None:
            self.pending = self._downloaded
            self._downloaded = None
            QTimer.singleShot(0, self, self.app.close)
        self.changed.emit()

    def cancel_pending(self) -> None:
        if self.pending is not None:
            self.pending = None
            self.status = "Instalação adiada porque o encerramento foi cancelado."
            self.changed.emit()

    def install(self) -> None:
        if self.release is None or self.task.is_running():
            return
        if updates.managed_install_root() is None:
            QDesktopServices.openUrl(QUrl(self.release.html_url))
            return
        answer = QMessageBox.question(
            self.app, "Atualizar a aplicação",
            f"Descarregar e instalar a versão {self.release.version}?\n\n"
            "Depois da verificação, a aplicação guarda as alterações e fecha em segurança. "
            "O instalador mantém os dados pessoais na sua localização atual.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        release = self.release
        self._start("download", _DownloadCall(release, self.download_progress.emit))

    def launch_pending_after_exit(self) -> None:
        if self.pending is None:
            return
        if not getattr(self.app, "_database_closed", False):
            self.cancel_pending()
            return
        prepared, self.pending = self.pending, None
        updates.launch_windows_update(prepared)

    def make_card(self) -> GlassCard:
        card = GlassCard("Atualizações da aplicação", strong=True)
        status = QLabel()
        status.setWordWrap(True)
        card.v.addWidget(status)
        automatic = QCheckBox("Procurar atualizações ao iniciar a aplicação")
        automatic.setChecked(str(self.app.repo.setting("check_app_updates", "1")) == "1")
        automatic.toggled.connect(self._save_automatic_check)
        card.v.addWidget(automatic)
        note = QLabel(
            "Consulta apenas as versões publicadas no GitHub. Os teus dados financeiros "
            "não são enviados. A instalação exige a tua confirmação."
        )
        note.setWordWrap(True)
        note.setObjectName("Subtle")
        card.v.addWidget(note)
        check = QPushButton("Procurar atualizações")
        install = QPushButton("Descarregar e atualizar…")
        install.setObjectName("Primary")
        check.clicked.connect(self.check)
        install.clicked.connect(self.install)
        card.v.addWidget(check)
        card.v.addWidget(install)

        def refresh() -> None:
            busy = self.task.is_running()
            status.setText(self.status)
            check.setEnabled(not busy)
            install.setVisible(self.release is not None)
            install.setEnabled(not busy)
            if updates.managed_install_root() is None:
                install.setText("Abrir a página da nova versão")

        self.changed.connect(refresh)
        refresh()
        return card

    def _save_automatic_check(self, enabled: bool) -> None:
        self.app.repo.set_setting("check_app_updates", "1" if enabled else "0")
        self.app.repo.commit()
