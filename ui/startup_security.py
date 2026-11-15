"""Liquid Glass startup security dialog (onboarding + unlock).

The dialog never compares secrets, derives keys, opens SQLite or writes
security files — all of that belongs to ``StartupSecurityService``.  Crypto
runs in a background worker so the UI thread stays responsive.
"""

from __future__ import annotations

import ctypes
from typing import Any

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ui.workers import AsyncTask
from utils.version import APP_VERSION_LABEL

DIALOG_PREFERRED_WIDTH = 560
PIN_BUTTON = "pin"
PASSWORD_BUTTON = "password"


def _caps_lock_on() -> bool:
    """Best-effort Caps Lock detection (Windows only; other OS: False)."""

    if ctypes.sizeof(ctypes.c_void_p) == 0:  # pragma: no cover - safety
        return False
    try:
        return bool(ctypes.windll.user32.GetKeyState(0x14) & 0x0001)
    except Exception:  # noqa: BLE001 - cosmetic warning only
        return False


class StartupSecurityDialog(QDialog):
    """Frameless Liquid Glass dialog used both for onboarding and unlock."""

    operation_started = Signal()
    operation_finished = Signal(bool)

    def __init__(
        self,
        service: Any,
        *,
        state: str,
        parent: QDialog | None = None,
    ) -> None:
        super().__init__(parent)
        self.service = service
        self.state = str(state)
        self.session_context = None
        self._busy = False
        self._worker_finalized = False
        self._worker_task = None
        self._throttle_until_ms = 0
        self._countdown_timer = QTimer(self)
        self._countdown_timer.setInterval(250)
        self._countdown_timer.timeout.connect(self._poll_throttle)

        self.setObjectName("StartupSecurityDialog")
        self.setWindowTitle("Finanças Pessoais")
        # StaysOnTop is mandatory: this dialog must always overlay the
        # StartupProgressWindow (which itself carries WindowStaysOnTopHint).
        self.setWindowFlags(
            Qt.Dialog | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setModal(True)
        self.setAccessibleName("Segurança de arranque")
        self._selected_kind = PIN_BUTTON
        self._build_ui()
        self._apply_style()
        self._configure_for_state()

    # ------------------------------------------------------------------ UI

    def _build_ui(self) -> None:
        from PySide6.QtWidgets import QCheckBox

        self._checkbox_class = QCheckBox
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 18, 18, 18)
        card = QFrame()
        card.setObjectName("SecurityCard")
        card.setAttribute(Qt.WA_StyledBackground, True)
        root.addWidget(card)

        shadow = QGraphicsDropShadowEffect(card)
        shadow.setBlurRadius(38)
        shadow.setOffset(0, 10)
        shadow.setColor(self._shadow_color())
        card.setGraphicsEffect(shadow)

        layout = QVBoxLayout(card)
        layout.setContentsMargins(28, 24, 28, 22)
        layout.setSpacing(12)

        header = QHBoxLayout()
        header.setSpacing(14)
        self.icon_label = QLabel("🛡️")
        self.icon_label.setObjectName("SecurityIcon")
        self.icon_label.setAlignment(Qt.AlignCenter)
        self.icon_label.setFixedSize(54, 54)
        header.addWidget(self.icon_label)

        names = QVBoxLayout()
        names.setSpacing(2)
        title = QLabel("Finanças Pessoais")
        title.setObjectName("SecurityTitle")
        subtitle = QLabel(f"Liquid Glass  ·  {APP_VERSION_LABEL}")
        subtitle.setObjectName("SecuritySubtitle")
        names.addWidget(title)
        names.addWidget(subtitle)
        header.addLayout(names, 1)
        layout.addLayout(header)

        self.stage_label = QLabel()
        self.stage_label.setObjectName("SecurityStage")
        self.stage_label.setWordWrap(True)
        self.stage_label.setAccessibleName("Estado da proteção")
        layout.addWidget(self.stage_label)

        # Segmented control: PIN / Palavra-passe.
        segmented = QHBoxLayout()
        segmented.setSpacing(8)
        self.pin_button = QPushButton("PIN")
        self.password_button = QPushButton("Palavra-passe")
        for button, kind in (
            (self.pin_button, PIN_BUTTON),
            (self.password_button, PASSWORD_BUTTON),
        ):
            button.setObjectName("SecuritySegment")
            button.setCheckable(True)
            button.setCursor(Qt.PointingHandCursor)
            button.clicked.connect(lambda checked, k=kind: self._select_kind(k))
            segmented.addWidget(button, 1)
        self.pin_button.setObjectName("SecuritySegment")
        self._segment_buttons = {
            PIN_BUTTON: self.pin_button,
            PASSWORD_BUTTON: self.password_button,
        }
        layout.addLayout(segmented)

        secret_row = QHBoxLayout()
        secret_row.setSpacing(8)
        self.secret_edit = QLineEdit()
        self.secret_edit.setEchoMode(QLineEdit.Password)
        # No artificial product limit: memory is the only real bound.
        self.secret_edit.setMaxLength(2_147_483_647)
        self.secret_edit.setAccessibleName("Credencial de desbloqueio")
        self.secret_edit.textChanged.connect(self._live_validate)
        self.secret_edit.returnPressed.connect(self._submit)
        secret_row.addWidget(self.secret_edit, 1)

        self.reveal_button = QToolButton()
        self.reveal_button.setObjectName("SecurityReveal")
        self.reveal_button.setText("👁")
        self.reveal_button.setCheckable(True)
        self.reveal_button.setToolTip("Mostrar ou ocultar a credencial")
        self.reveal_button.setAccessibleName("Mostrar credencial")
        self.reveal_button.toggled.connect(self._toggle_reveal)
        secret_row.addWidget(self.reveal_button)
        layout.addLayout(secret_row)

        self.caps_lock_label = QLabel("⚠ Caps Lock ativo")
        self.caps_lock_label.setObjectName("SecurityWarning")
        self.caps_lock_label.hide()
        layout.addWidget(self.caps_lock_label)

        self.confirm_edit = QLineEdit()
        self.confirm_edit.setEchoMode(QLineEdit.Password)
        self.confirm_edit.setMaxLength(2_147_483_647)
        self.confirm_edit.setAccessibleName("Confirmar credencial")
        self.confirm_edit.textChanged.connect(self._live_validate)
        self.confirm_edit.returnPressed.connect(self._submit)
        self.confirm_label = QLabel("Confirmar credencial")
        self.confirm_label.setObjectName("SecurityFieldLabel")
        layout.addWidget(self.confirm_label)
        layout.addWidget(self.confirm_edit)

        self.default_method_checkbox = self._checkbox_class(
            "Tornar este o método predefinido"
        )
        self.default_method_checkbox.setObjectName("SecurityDefaultMethod")
        self.default_method_checkbox.setChecked(True)
        self.default_method_checkbox.setAccessibleName(
            "Método predefinido de desbloqueio"
        )
        layout.addWidget(self.default_method_checkbox)

        self.weak_pin_checkbox = self._checkbox_class(
            "Aceito o risco: usar um PIN curto também nos backups portáteis"
        )
        self.weak_pin_checkbox.setObjectName("SecurityWeakPin")
        self.weak_pin_checkbox.setAccessibleName(
            "Confirmação de PIN curto para backups"
        )
        self.weak_pin_checkbox.hide()
        layout.addWidget(self.weak_pin_checkbox)

        self.status_label = QLabel()
        self.status_label.setObjectName("SecurityStatus")
        self.status_label.setWordWrap(True)
        self.status_label.setMinimumHeight(34)
        self.status_label.setAccessibleName("Mensagens de validação")
        layout.addWidget(self.status_label)

        self.busy_bar = QProgressBar()
        self.busy_bar.setObjectName("SecurityBusy")
        self.busy_bar.setRange(0, 0)
        self.busy_bar.setTextVisible(False)
        self.busy_bar.setFixedHeight(8)
        self.busy_bar.hide()
        layout.addWidget(self.busy_bar)

        buttons = QHBoxLayout()
        buttons.setSpacing(10)
        self.exit_button = QPushButton("Sair")
        self.exit_button.setObjectName("SecurityExit")
        self.exit_button.clicked.connect(self.reject)
        buttons.addWidget(self.exit_button)
        buttons.addStretch(1)
        self.submit_button = QPushButton("Continuar")
        self.submit_button.setObjectName("SecuritySubmit")
        self.submit_button.setDefault(True)
        self.submit_button.clicked.connect(self._submit)
        buttons.addWidget(self.submit_button)
        layout.addLayout(buttons)

        self.setTabOrder(self.pin_button, self.password_button)
        self.setTabOrder(self.password_button, self.secret_edit)
        self.setTabOrder(self.secret_edit, self.reveal_button)
        self.setTabOrder(self.reveal_button, self.confirm_edit)
        self.setTabOrder(self.confirm_edit, self.default_method_checkbox)
        self.setTabOrder(self.default_method_checkbox, self.submit_button)
        self.setTabOrder(self.submit_button, self.exit_button)

        self._worker_task: AsyncTask | None = None

    def _apply_style(self) -> None:
        if self._is_dark():
            surface = "#111A2D"
            border = "#3C4C70"
            title = "#F5F8FF"
            body = "#DCE6FA"
            subtle = "#9EB0D1"
            chip = "#203B67"
            track = "#263552"
            accent = "#66B3FF"
            field_bg = "#0D1526"
            field_border = "#3C4C70"
            field_text = "#F5F8FF"
            danger_text = "#FFC7C2"
        else:
            surface = "#F8FAFF"
            border = "#C9D8F2"
            title = "#071A3D"
            body = "#19345F"
            subtle = "#60779D"
            chip = "#E0EEFF"
            track = "#DFE9F8"
            accent = "#2788F5"
            field_bg = "#FFFFFF"
            field_border = "#B7C9E8"
            field_text = "#0F1F3D"
            danger_text = "#8A1C13"
        self.setStyleSheet(
            f"""
            QFrame#SecurityCard {{
                background-color: {surface};
                border: 1px solid {border};
                border-radius: 24px;
            }}
            QLabel {{
                color: {body};
                background: transparent;
                border: none;
            }}
            QLabel#SecurityIcon {{
                background-color: {chip};
                border: 1px solid {border};
                border-radius: 18px;
                font-size: 25px;
            }}
            QLabel#SecurityTitle {{
                color: {title};
                font-size: 22px;
                font-weight: 800;
            }}
            QLabel#SecuritySubtitle {{
                color: {subtle};
                font-size: 12px;
                font-weight: 600;
            }}
            QLabel#SecurityStage {{
                color: {title};
                font-size: 14px;
                font-weight: 700;
            }}
            QLabel#SecurityFieldLabel {{
                color: {subtle};
                font-size: 11px;
                font-weight: 600;
            }}
            QLabel#SecurityStatus {{
                color: {subtle};
                font-size: 12px;
            }}
            QLabel#SecurityWarning {{
                color: {danger_text};
                font-size: 11px;
                font-weight: 700;
            }}
            QLineEdit {{
                background-color: {field_bg};
                color: {field_text};
                border: 1px solid {field_border};
                border-radius: 10px;
                padding: 9px 12px;
                font-size: 14px;
                selection-background-color: {accent};
            }}
            QLineEdit:focus {{
                border: 1px solid {accent};
            }}
            QToolButton#SecurityReveal {{
                background-color: {chip};
                color: {body};
                border: 1px solid {border};
                border-radius: 10px;
                padding: 7px 10px;
                font-size: 13px;
            }}
            QPushButton {{
                border-radius: 10px;
                padding: 9px 16px;
                font-weight: 700;
                font-size: 13px;
            }}
            QPushButton#SecuritySegment {{
                background-color: {track};
                color: {body};
                border: 1px solid {border};
            }}
            QPushButton#SecuritySegment:checked {{
                background-color: {chip};
                color: {accent};
                border: 1px solid {accent};
            }}
            QPushButton#SecuritySubmit {{
                background-color: {accent};
                color: #FFFFFF;
                border: none;
                min-width: 120px;
            }}
            QPushButton#SecuritySubmit:disabled {{
                background-color: {track};
                color: {subtle};
            }}
            QPushButton#SecurityExit {{
                background-color: transparent;
                color: {subtle};
                border: 1px solid {border};
            }}
            QProgressBar#SecurityBusy {{
                background-color: {track};
                border: none;
                border-radius: 4px;
            }}
            QProgressBar#SecurityBusy::chunk {{
                background-color: {accent};
                border-radius: 4px;
            }}
            """
        )

    def _is_dark(self) -> bool:
        app = QApplication.instance()
        return bool(
            app is not None and app.palette().window().color().lightness() < 128
        )

    def _shadow_color(self) -> QColor:
        return QColor(5, 18, 55, 145 if self._is_dark() else 72)

    # ------------------------------------------------------------- states

    def _configure_for_state(self) -> None:
        self.confirm_label.hide()
        self.confirm_edit.hide()
        self.default_method_checkbox.hide()
        self.default_method_checkbox.setChecked(True)
        if self.state == "onboarding":
            self.stage_label.setText(
                "Primeira utilização: define como queres desbloquear a aplicação."
            )
            self.submit_button.setText("Criar e desbloquear")
            self._select_kind(PASSWORD_BUTTON)
            self.confirm_label.show()
            self.confirm_edit.show()
        elif self.state == "unlock":
            envelope = self.service.load_envelope()
            self.stage_label.setText(
                "Introduz a tua credencial para desbloquear os dados."
            )
            self.submit_button.setText("Desbloquear")
            # Only configured methods may be selected; the default method is
            # indicated and preselected (never an unlimited-attempt mix).
            for kind, button in self._segment_buttons.items():
                configured = kind in envelope.unlocks
                button.setEnabled(configured)
                button.setToolTip(
                    "" if configured else "Este método não está configurado."
                )
            default_kind = str(envelope.default_unlock_method)
            if default_kind in envelope.unlocks:
                self._select_kind(default_kind)
            elif str(envelope.unlocks) and envelope.unlocks:
                first_configured = sorted(envelope.unlocks)[0]
                self._select_kind(first_configured)
        else:  # blocked / recovery states
            self.stage_label.setText(
                "A proteção deste perfil está comprometida e o arranque foi bloqueado."
            )
            self.secret_edit.setEnabled(False)
            self.submit_button.hide()
            self.pin_button.setEnabled(False)
            self.password_button.setEnabled(False)
            self._select_kind(PIN_BUTTON)

    def _select_kind(self, kind: str) -> None:
        self._selected_kind = kind
        for button_kind, button in self._segment_buttons.items():
            button.setChecked(button_kind == kind)
        self.secret_edit.setFocus(Qt.OtherFocusReason)

    def _toggle_reveal(self, visible: bool) -> None:
        self.secret_edit.setEchoMode(
            QLineEdit.Normal if visible else QLineEdit.Password
        )
        self.confirm_edit.setEchoMode(
            QLineEdit.Normal if visible else QLineEdit.Password
        )

    # ---------------------------------------------------------- validation

    def _live_validate(self) -> None:
        if self.state != "onboarding":
            return
        text = self.secret_edit.text()
        if self._caps_lock_active():
            self.caps_lock_label.show()
        else:
            self.caps_lock_label.hide()
        if not text:
            self._set_status("")
            return
        from services.startup_security import (
            CredentialPolicyError,
            is_weak_portable_pin,
            validate_credential,
        )

        weak_confirmed = self.weak_pin_checkbox.isChecked()
        try:
            validate_credential(
                self._selected_kind,
                text,
                weak_pin_confirmed=weak_confirmed,
            )
        except CredentialPolicyError as exc:
            self._set_status(str(exc), error=True)
            self.weak_pin_checkbox.setVisible(
                is_weak_portable_pin(self._selected_kind, text)
            )
            return
        self.weak_pin_checkbox.setVisible(
            is_weak_portable_pin(self._selected_kind, text)
        )
        if is_weak_portable_pin(self._selected_kind, text):
            self._set_status(
                "PIN curto: também protege backups portáteis. Recomenda-se "
                "uma palavra-passe.",
                error=False,
            )
            return
        if self.confirm_edit.isVisible() and self.confirm_edit.text():
            if self.confirm_edit.text() != text:
                self._set_status("A confirmação não coincide.", error=True)
                return
        self._set_status("", error=False)

    def _caps_lock_active(self) -> bool:
        return _caps_lock_on()

    def _set_status(self, message: str, *, error: bool = False) -> None:
        self.status_label.setText(message)
        self._status_is_error = error

    # ------------------------------------------------------------- submit

    def _submit(self) -> None:
        if self._busy:
            return
        import time

        now_ms = int(time.monotonic() * 1000)
        if now_ms < self._throttle_until_ms:
            return
        if self.state == "onboarding":
            self._submit_onboarding()
        elif self.state == "unlock":
            self._submit_unlock()

    def _submit_onboarding(self) -> None:
        from services.startup_security import (
            CredentialPolicyError,
            is_weak_portable_pin,
        )

        secret = self.secret_edit.text()
        weak_confirmed = self.weak_pin_checkbox.isChecked()
        try:
            self.service.validate_credential_for_ui(
                self._selected_kind,
                secret,
                weak_pin_confirmed=weak_confirmed,
            )
        except CredentialPolicyError as exc:
            self._set_status(str(exc), error=True)
            self.weak_pin_checkbox.setVisible(
                is_weak_portable_pin(self._selected_kind, secret)
            )
            return
        if self.confirm_edit.text() != secret:
            self._set_status("A confirmação não coincide com a credencial.", error=True)
            return
        self._set_busy(True)
        self.operation_started.emit()

        def work() -> Any:
            # Relay the typed exception through the result channel: the
            # AsyncTask error signal only carries a str message.
            try:
                return self.service.configure_first_run_ui(
                    self._selected_kind,
                    secret,
                    weak_pin_confirmed=weak_confirmed,
                )
            except Exception as exc:  # noqa: BLE001 - mapped by the UI thread
                return exc

        self._run_worker(work)

    def _submit_unlock(self) -> None:
        secret = self.secret_edit.text()
        self._set_busy(True)
        self.operation_started.emit()

        def work() -> Any:
            try:
                return self.service.unlock_ui(self._selected_kind, secret)
            except Exception as exc:  # noqa: BLE001 - mapped by the UI thread
                return exc

        self._run_worker(work)

    def _run_worker(self, work: Any) -> None:
        """Non-blocking worker: typed slots without nested event loops."""

        self._worker_finalized = False
        # BUG-AUD-C142: register against the window registry when the parent
        # is the main window so closeEvent cancels and waits for this thread.
        task = AsyncTask(
            self,
            registry=getattr(self.parent(), "background_task_registry", None),
            label="Verificação de segurança de arranque",
        )
        self._worker_task = task
        task.result.connect(self._on_worker_result)
        task.error.connect(self._on_worker_error_str)
        if not task.start(work):
            self._worker_task = None
            self._set_busy(False)
            self._set_status("Não foi possível iniciar a verificação.", error=True)

    def _on_worker_result(self, outcome: object) -> None:
        if self._worker_finalized:
            return  # late result after the dialog was closed: ignore
        self._worker_task = None
        if isinstance(outcome, BaseException):
            self._set_busy(False)
            self._handle_worker_error(outcome)
            return
        self._worker_finalized = True
        self.session_context = outcome
        self.operation_finished.emit(True)
        self.accept()

    def _on_worker_error_str(self, message: str) -> None:
        if self._worker_finalized:
            return
        self._worker_task = None
        self._set_busy(False)
        self._handle_worker_error(RuntimeError(message))

    def _handle_worker_error(self, exc: BaseException) -> None:
        from services.startup_security import (
            CredentialPolicyError,
            InvalidCredentialError,
            SecurityEnvelopeError,
            UnsupportedSecurityFormatError,
        )

        message = str(exc)
        # The service already counted this failure inside unlock(); the
        # dialog only reads the counter to schedule the countdown.
        self._arm_throttle_from_failures()
        if isinstance(exc, InvalidCredentialError):
            self._set_status(message, error=True)
            self.secret_edit.selectAll()
            self.secret_edit.setFocus()
            return
        if isinstance(exc, CredentialPolicyError):
            self._set_status(message, error=True)
            return
        if isinstance(exc, UnsupportedSecurityFormatError):
            self._set_status(message, error=True)
            return
        if isinstance(exc, SecurityEnvelopeError):
            self._set_status(message, error=True)
            return
        self._set_status(
            "Não foi possível desbloquear. Verifica a credencial e tenta novamente.",
            error=True,
        )

    def register_failure(self) -> None:
        failures = self.service.register_failure()
        self._arm_throttle(failures)

    def _arm_throttle_from_failures(self) -> None:
        """Arm the countdown from the service's failure counter (no double
        counting: ``unlock`` already registered the failure)."""

        self._arm_throttle(getattr(self.service, "failures", 0))

    def _arm_throttle(self, failures: int) -> None:
        from services.startup_security import retry_delay_seconds

        delay = retry_delay_seconds(failures)
        if delay > 0:
            import time

            self._throttle_until_ms = int(time.monotonic() * 1000) + delay * 1000
            self._countdown_timer.start()
            self._poll_throttle()

    def _poll_throttle(self) -> None:
        import time

        now_ms = int(time.monotonic() * 1000)
        remaining_ms = self._throttle_until_ms - now_ms
        if remaining_ms <= 0:
            self._countdown_timer.stop()
            self.submit_button.setEnabled(not self._busy)
            self.secret_edit.setEnabled(not self._busy)
            return
        self.submit_button.setEnabled(False)
        self.secret_edit.setEnabled(False)
        self._set_status(
            f"Aguarda {max(1, (remaining_ms + 999) // 1000)} s antes de "
            "tentar novamente.",
            error=True,
        )

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.busy_bar.setVisible(busy)
        self.submit_button.setEnabled(not busy)
        self.secret_edit.setEnabled(not busy)
        self.confirm_edit.setEnabled(not busy)
        self.pin_button.setEnabled(not busy and self.state != "blocked")
        self.password_button.setEnabled(not busy and self.state != "blocked")

    # --------------------------------------------------------------- misc

    def showEvent(self, event: Any) -> None:  # type: ignore[override]
        super().showEvent(event)
        screen = self.screen() or QApplication.primaryScreen()
        if screen is None:
            return
        area = screen.availableGeometry()
        self.adjustSize()
        width = min(DIALOG_PREFERRED_WIDTH, area.width() - 24)
        self.resize(width, self.sizeHint().height())
        self.move(
            area.x() + max(0, (area.width() - self.width()) // 2),
            area.y() + max(0, (area.height() - self.height()) // 2),
        )
        # Force the dialog above the startup progress window even when that
        # window refreshes its topmost flag during progress updates.
        self.raise_()
        self.activateWindow()
        self.secret_edit.setFocus()

    def keyPressEvent(self, event: Any) -> None:  # type: ignore[override]
        if event.key() == Qt.Key_Escape:
            self.reject()
            return
        super().keyPressEvent(event)

    def reject(self) -> None:
        if self._busy:
            return
        super().reject()


# ---------------------------------------------------------------------------
# Module-level gate used by main.py
# ---------------------------------------------------------------------------


def _run_security_dialog(
    service: Any,
    mode: str,
    startup: Any = None,
) -> Any:
    """Show one modal dialog above the loading window and return the context."""

    dialog = StartupSecurityDialog(service, state=mode, parent=startup)
    if startup is not None:
        # Reuse the canonical stacking helper: application-modal, topmost,
        # raised and activated above the StartupProgressWindow.
        from ui.startup import prepare_foreground_dialog

        prepare_foreground_dialog(dialog, startup)
    dialog.exec()
    context = dialog.session_context
    try:
        dialog.deleteLater()
    except RuntimeError:
        pass
    return context


def run_startup_security_gate(
    service: Any = None,
    *,
    startup: Any = None,
) -> Any:
    """Inspect, onboard or unlock; return a session context or None.

    ``startup`` (the StartupProgressWindow) is optional: when provided, the
    dialog is stacked above it.  Returning ``None`` means startup must not
    continue (cancel).  No database object is created here.
    """

    from services.startup_security import (
        ProfileSecurityState,
        SecurityRecoveryRequiredError,
        StartupSecurityService,
    )

    service = service or StartupSecurityService()
    state = service.inspect_profile()
    if state in {
        ProfileSecurityState.MISSING_ENVELOPE,
        ProfileSecurityState.CORRUPT_ENVELOPE,
        ProfileSecurityState.TAMPERED,
        ProfileSecurityState.RECOVERY_REQUIRED,
    }:
        raise SecurityRecoveryRequiredError(
            "A proteção deste perfil está danificada ou foi removida."
        )
    if state == ProfileSecurityState.CONFIGURED:
        return _run_security_dialog(service, "unlock", startup)
    if state in {
        ProfileSecurityState.NEW,
        ProfileSecurityState.LEGACY_UNPROTECTED,
    }:
        return _run_security_dialog(service, "onboarding", startup)
    dialog = StartupSecurityDialog(service, state="blocked")
    dialog.show()
    QMessageBox.warning(
        dialog,
        "Finanças Pessoais",
        (
            "A proteção deste perfil está danificada ou foi removida. "
            "Para proteger os teus dados, o arranque foi bloqueado.\n\n"
            "Restaura o envelope de segurança a partir de um backup ou "
            "contacta o suporte."
        ),
    )
    dialog.close()
    return None


def run_onboarding_gate(service: Any = None, *, startup: Any = None) -> Any:
    """Run the first-run onboarding dialog after the data location is ready.

    ``startup`` must be the live StartupProgressWindow so the dialog is
    stacked strictly above the loading screen (never hidden by it).
    """

    from services.startup_security import StartupSecurityService

    service = service or StartupSecurityService()
    return _run_security_dialog(service, "onboarding", startup)


class TamperRecoveryDialog(QDialog):
    """Dedicated modal dialog for tamper and protection recovery acknowledgment.

    Provides a clean, explicit layout (QVBoxLayout + QLineEdit + QPushButton)
    without depending on internal QMessageBox structure (which uses QGridLayout
    and lacks insertRow). Explains missing, corrupt, indeterminate and removed
    protection as distinct causes.
    """

    MIN_WIDTH = 520
    MIN_HEIGHT = 340

    def __init__(
        self,
        service: Any,
        *,
        state: Any = None,
        parent: QDialog | None = None,
    ) -> None:
        super().__init__(parent)
        self.service = service
        self.state = state
        self._confirmed = False

        self.setObjectName("TamperRecoveryDialog")
        self.setWindowTitle("Finanças Pessoais — Recuperação de Segurança")
        self.setModal(True)
        self.setMinimumWidth(self.MIN_WIDTH)
        self.setMinimumHeight(self.MIN_HEIGHT)

        self._setup_ui()

    def _explanation_for_state(self) -> tuple[str, str]:
        state_val = str(getattr(self.state, "value", self.state) or "").lower()
        if not state_val and hasattr(self.service, "inspect_profile"):
            try:
                inspected = self.service.inspect_profile()
                state_val = str(getattr(inspected, "value", inspected) or "").lower()
            except Exception:
                state_val = "recovery_required"

        if "missing" in state_val:
            title = "Envelope de segurança ausente"
            desc = (
                "O envelope de segurança deste perfil está ausente, mas os registos "
                "provam que o perfil já estava protegido anteriormente."
            )
        elif "corrupt" in state_val:
            title = "Envelope de segurança corrompido"
            desc = (
                "Os metadados de segurança deste perfil estão ilegíveis ou corrompidos. "
                "Não é possível validar as credenciais existentes com segurança."
            )
        elif "indeterminate" in state_val or "recovery_required" in state_val:
            title = "Recuperação de segurança necessária"
            desc = (
                "A proteção da base de dados encontra-se num estado indeterminado ou "
                "requer recuperação assistida antes de o arranque poder prosseguir."
            )
        else:
            title = "Proteção removida ou adulterada"
            desc = (
                "A proteção deste perfil foi removida (envelope e marcador apagados), "
                "mas os dados mostram que este perfil já estava protegido."
            )
        return title, desc

    def _setup_ui(self) -> None:
        from services.startup_security import TAMPER_RECOVERY_CONFIRMATION

        phrase = getattr(
            self.service,
            "tamper_recovery_confirmation_phrase",
            TAMPER_RECOVERY_CONFIRMATION,
        )
        if not isinstance(phrase, str):
            phrase = TAMPER_RECOVERY_CONFIRMATION
        self._phrase = str(phrase)

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(20, 20, 20, 20)
        main_layout.setSpacing(14)

        scroll = QScrollArea(self)
        scroll.setObjectName("RecoveryScrollArea")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        content_widget = QWidget()
        layout = QVBoxLayout(content_widget)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(14)

        title, desc = self._explanation_for_state()

        self.title_label = QLabel(f"⚠️  {title}", content_widget)
        self.title_label.setObjectName("RecoveryTitleLabel")
        self.title_label.setStyleSheet("font-size: 15px; font-weight: bold; color: #DC2626;")
        layout.addWidget(self.title_label)

        self.desc_label = QLabel(desc, content_widget)
        self.desc_label.setObjectName("RecoveryDescriptionLabel")
        self.desc_label.setWordWrap(True)
        self.desc_label.setStyleSheet("font-size: 12px; color: #64748B; line-height: 1.4;")
        layout.addWidget(self.desc_label)

        instruction_text = (
            "Se apagaste ou reconfiguraste estes ficheiros intencionalmente, escreve a frase "
            "abaixo para confirmar. Nota: os backups encriptados existentes permanecem recuperáveis "
            "apenas com a credencial original com que foram criados (a nova credencial a configurar não os abrirá):\n\n"
            f"<b>{self._phrase}</b>"
        )
        self.instruction_label = QLabel(instruction_text, content_widget)
        self.instruction_label.setObjectName("RecoveryInstructionLabel")
        self.instruction_label.setWordWrap(True)
        self.instruction_label.setStyleSheet("font-size: 12px; padding: 8px; background: rgba(0,0,0,0.04); border-radius: 6px;")
        layout.addWidget(self.instruction_label)

        input_prompt = QLabel("Confirmação:", content_widget)
        input_prompt.setStyleSheet("font-weight: bold; font-size: 12px;")
        layout.addWidget(input_prompt)

        self.input_box = QLineEdit(content_widget)
        self.input_box.setObjectName("ConfirmationInput")
        self.input_box.setPlaceholderText(str(self._phrase))
        self.input_box.setClearButtonEnabled(True)
        self.input_box.setStyleSheet("padding: 8px; font-size: 12px;")
        self.input_box.returnPressed.connect(self._on_confirm)
        layout.addWidget(self.input_box)

        self.error_label = QLabel(content_widget)
        self.error_label.setObjectName("ErrorLabel")
        self.error_label.setStyleSheet("color: #DC2626; font-size: 11px; font-weight: bold;")
        self.error_label.setWordWrap(True)
        self.error_label.hide()
        layout.addWidget(self.error_label)

        layout.addStretch()
        scroll.setWidget(content_widget)
        main_layout.addWidget(scroll, 1)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)
        btn_row.addStretch()

        self.cancel_btn = QPushButton("Cancelar", self)
        self.cancel_btn.setObjectName("CancelButton")
        self.cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(self.cancel_btn)

        self.confirm_btn = QPushButton("Confirmar Recuperação", self)
        self.confirm_btn.setObjectName("ConfirmButton")
        self.confirm_btn.setStyleSheet(
            "background-color: #DC2626; color: white; font-weight: bold; padding: 6px 14px;"
        )
        self.confirm_btn.clicked.connect(self._on_confirm)
        btn_row.addWidget(self.confirm_btn)

        main_layout.addLayout(btn_row)

    def _on_confirm(self) -> None:
        import hmac

        typed = self.input_box.text().strip()
        phrase_bytes = str(self._phrase).encode("utf-8")
        typed_bytes = typed.encode("utf-8")
        if not hmac.compare_digest(typed_bytes, phrase_bytes):
            self.error_label.setText(
                "Frase de confirmação incorreta. Escreve a frase exatamente como apresentada."
            )
            self.error_label.show()
            self.input_box.setFocus()
            self.input_box.selectAll()
            return

        try:
            self.service.acknowledge_tamper_recovery(typed)
            self._confirmed = True
            self.accept()
        except Exception as exc:
            from services.startup_security import (
                CredentialPolicyError,
                SecurityRecoveryIncompleteError,
                SecurityRecoveryStorageError,
            )

            if isinstance(exc, CredentialPolicyError):
                self.error_label.setText(
                    f"Frase de confirmação incorreta: {exc}"
                )
                self.input_box.setFocus()
                self.input_box.selectAll()
            elif isinstance(exc, SecurityRecoveryStorageError):
                self.error_label.setText(
                    f"Falha de armazenamento ao arquivar resíduos: {exc}"
                )
            elif isinstance(exc, SecurityRecoveryIncompleteError):
                self.error_label.setText(
                    f"Recuperação incompleta: {exc}"
                )
            else:
                self.error_label.setText(f"Falha ao executar recuperação: {exc}")
            self.error_label.show()


def run_tamper_recovery_dialog(service: Any, state: Any = None) -> bool:
    """Explicit, typed acknowledgment that protection was removed.

    Shown when the anti-sabotage tombstone proves this profile was
    configured before (SEC-TOMBSTONE).  Returns True only after the user
    types the exact recovery phrase; the service then archives leftover
    security residue and the normal onboarding may run.
    """

    dialog = TamperRecoveryDialog(service, state=state)
    theme = _active_theme_or_default()
    if theme is not None:
        try:
            from ui.core.theme import apply_message_box_theme

            apply_message_box_theme(dialog, theme)
        except Exception as exc:
            # BUG-SEC-02: styling is cosmetic; a failure must never prevent
            # the tamper recovery dialog from running, but it must be logged
            # (zero except:pass rule) instead of being silently swallowed.
            from utils.app_logging import log_exception

            log_exception(
                "Failed to apply theme to TamperRecoveryDialog", exc
            )
    dialog.exec()
    return bool(dialog._confirmed)


def _active_theme_or_default() -> Any:
    """Best-effort theme lookup for dialog styling during recovery.

    BUG-SEC-02: the previous implementation referenced a nonexistent
    ``SecuritySettingsService`` class and its nonexistent ``current_theme``
    attribute, so this always returned ``None`` and the caller then crashed
    inside ``apply_message_box_theme(dialog, None)`` with
    ``AttributeError: 'NoneType' object has no attribute 'message_surface'``
    (masked by a bare ``except: pass``).  The real controller is
    ``SecuritySettingsController``; the theme is read from the live
    application window via ``theme_for_widget`` and falls back to a plain
    ``Theme`` instance so styling can never resolve to ``None``.
    """

    try:
        from ui.core.theme import Theme, theme_for_widget

        app = QApplication.instance()
        if app is not None:
            for widget in app.topLevelWidgets():
                if widget.isVisible() and hasattr(widget, "theme"):
                    theme = theme_for_widget(widget)
                    if isinstance(theme, Theme):
                        return theme
    except Exception as exc:
        from utils.app_logging import log_exception

        log_exception("Failed to resolve the active theme for recovery", exc)
    try:
        from ui.core.theme import Theme

        return Theme(dark=False)
    except Exception as exc:
        from utils.app_logging import log_exception

        log_exception("Failed to build the fallback theme for recovery", exc)
        return None


__all__ = [
    "StartupSecurityDialog",
    "TamperRecoveryDialog",
    "run_onboarding_gate",
    "run_startup_security_gate",
    "run_tamper_recovery_dialog",
]
