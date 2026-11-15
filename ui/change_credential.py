"""Liquid Glass change-credential dialog backed by StartupSecurityService.

Requires the current credential, the new credential and its confirmation.
Supports PIN and passphrase, reveal controls, Caps Lock warning and
exact-value preservation.  No credential comparison or key derivation here.
"""

from __future__ import annotations

import ctypes
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
)

from services.security_settings import CredentialChangeResult
from ui.workers import AsyncTask

DIALOG_PREFERRED_WIDTH = 560


def _caps_lock_on() -> bool:
    try:
        return bool(ctypes.windll.user32.GetKeyState(0x14) & 0x0001)
    except Exception:  # noqa: BLE001 - cosmetic warning only
        return False


class ChangeCredentialDialog(QDialog):
    """Modal Liquid Glass dialog for PIN/passphrase changes."""

    change_started = Signal()
    change_finished = Signal(bool)

    def __init__(
        self,
        controller: Any,
        *,
        parent: QDialog | None = None,
    ) -> None:
        super().__init__(parent)
        self.controller = controller
        self.result_state: CredentialChangeResult | None = None
        self._busy = False
        self._destroyed = False

        self.setObjectName("ChangeCredentialDialog")
        self.setWindowTitle("Alterar credencial de segurança")
        # Mandatory: overlays both the loading window and the main window.
        self.setWindowFlags(
            Qt.Dialog | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setModal(True)
        self.setAccessibleName("Alterar credencial de segurança")
        self._build_ui()
        self._apply_style()

    # ------------------------------------------------------------------ UI

    def _build_ui(self) -> None:
        from PySide6.QtWidgets import QComboBox

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
        layout.setSpacing(10)

        title = QLabel("Alterar credencial de segurança")
        title.setObjectName("SecurityTitle")
        subtitle = QLabel(
            "A mesma chave de proteção continua a abrir os backups atuais."
        )
        subtitle.setObjectName("SecuritySubtitle")
        subtitle.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(subtitle)

        self.current_label = QLabel("Credencial atual")
        self.current_label.setObjectName("SecurityFieldLabel")
        self.current_edit = QLineEdit()
        self.current_edit.setEchoMode(QLineEdit.Password)
        self.current_edit.setMaxLength(2_147_483_647)
        self.current_edit.setAccessibleName("Credencial atual")
        layout.addWidget(self.current_label)
        layout.addWidget(self.current_edit)

        method_row = QHBoxLayout()
        method_label = QLabel("Novo método")
        method_label.setObjectName("SecurityFieldLabel")
        self.method_combo = QComboBox()
        self.method_combo.addItem("Palavra-passe", "password")
        self.method_combo.addItem("PIN", "pin")
        self.method_combo.setAccessibleName("Novo método de desbloqueio")
        method_row.addWidget(method_label)
        method_row.addWidget(self.method_combo, 1)
        layout.addLayout(method_row)

        self.new_label = QLabel("Nova credencial")
        self.new_label.setObjectName("SecurityFieldLabel")
        self.new_edit = QLineEdit()
        self.new_edit.setEchoMode(QLineEdit.Password)
        self.new_edit.setMaxLength(2_147_483_647)
        self.new_edit.setAccessibleName("Nova credencial")
        self.new_edit.textChanged.connect(self._live_validate)
        layout.addWidget(self.new_label)
        layout.addWidget(self.new_edit)

        self.confirm_label = QLabel("Confirmar nova credencial")
        self.confirm_label.setObjectName("SecurityFieldLabel")
        self.confirm_edit = QLineEdit()
        self.confirm_edit.setEchoMode(QLineEdit.Password)
        self.confirm_edit.setMaxLength(2_147_483_647)
        self.confirm_edit.setAccessibleName("Confirmar nova credencial")
        self.confirm_edit.textChanged.connect(self._live_validate)
        layout.addWidget(self.confirm_label)
        layout.addWidget(self.confirm_edit)

        self.caps_lock_label = QLabel("⚠ Caps Lock ativo")
        self.caps_lock_label.setObjectName("SecurityWarning")
        self.caps_lock_label.hide()
        layout.addWidget(self.caps_lock_label)

        self.weak_pin_checkbox = QCheckBox(
            "Aceito o risco: usar um PIN curto também nos backups portáteis"
        )
        self.weak_pin_checkbox.setObjectName("SecurityWeakPin")
        self.weak_pin_checkbox.hide()
        layout.addWidget(self.weak_pin_checkbox)

        self.status_label = QLabel()
        self.status_label.setObjectName("SecurityStatus")
        self.status_label.setWordWrap(True)
        self.status_label.setMinimumHeight(30)
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
        self.cancel_button = QPushButton("Cancelar")
        self.cancel_button.setObjectName("SecurityExit")
        self.cancel_button.clicked.connect(self.reject)
        buttons.addWidget(self.cancel_button)
        buttons.addStretch(1)
        self.submit_button = QPushButton("Alterar credencial")
        self.submit_button.setObjectName("SecuritySubmit")
        self.submit_button.setDefault(True)
        self.submit_button.clicked.connect(self._submit)
        buttons.addWidget(self.submit_button)
        layout.addLayout(buttons)

        self.setTabOrder(self.current_edit, self.method_combo)
        self.setTabOrder(self.method_combo, self.new_edit)
        self.setTabOrder(self.new_edit, self.confirm_edit)
        self.setTabOrder(self.confirm_edit, self.submit_button)
        self.setTabOrder(self.submit_button, self.cancel_button)

    def _apply_style(self) -> None:
        dark = self._is_dark()
        if dark:
            surface, border = "#111A2D", "#3C4C70"
            title, body, subtle = "#F5F8FF", "#DCE6FA", "#9EB0D1"
            _chip, track, accent = "#203B67", "#263552", "#66B3FF"
            field_bg, field_border, field_text = "#0D1526", "#3C4C70", "#F5F8FF"
            danger_text = "#FFC7C2"
        else:
            surface, border = "#F8FAFF", "#C9D8F2"
            title, body, subtle = "#071A3D", "#19345F", "#60779D"
            _chip, track, accent = "#E0EEFF", "#DFE9F8", "#2788F5"
            field_bg, field_border, field_text = "#FFFFFF", "#B7C9E8", "#0F1F3D"
            danger_text = "#8A1C13"
        self.setStyleSheet(
            f"""
            QFrame#SecurityCard {{
                background-color: {surface};
                border: 1px solid {border};
                border-radius: 24px;
            }}
            QLabel {{ color: {body}; background: transparent; border: none; }}
            QLabel#SecurityTitle {{ color: {title}; font-size: 20px; font-weight: 800; }}
            QLabel#SecuritySubtitle {{ color: {subtle}; font-size: 12px; }}
            QLabel#SecurityFieldLabel {{ color: {subtle}; font-size: 11px; font-weight: 600; }}
            QLabel#SecurityStatus {{ color: {subtle}; font-size: 12px; }}
            QLabel#SecurityWarning {{ color: {danger_text}; font-size: 11px; font-weight: 700; }}
            QLineEdit {{
                background-color: {field_bg}; color: {field_text};
                border: 1px solid {field_border}; border-radius: 10px;
                padding: 9px 12px; font-size: 14px;
                selection-background-color: {accent};
            }}
            QLineEdit:focus {{ border: 1px solid {accent}; }}
            QComboBox {{
                background-color: {field_bg}; color: {field_text};
                border: 1px solid {field_border}; border-radius: 10px;
                padding: 8px 12px; font-size: 13px;
            }}
            QPushButton {{ border-radius: 10px; padding: 9px 16px; font-weight: 700; font-size: 13px; }}
            QPushButton#SecuritySubmit {{ background-color: {accent}; color: #FFFFFF; border: none; min-width: 130px; }}
            QPushButton#SecuritySubmit:disabled {{ background-color: {track}; color: {subtle}; }}
            QPushButton#SecurityExit {{ background-color: transparent; color: {subtle}; border: 1px solid {border}; }}
            QProgressBar#SecurityBusy {{ background-color: {track}; border: none; border-radius: 4px; }}
            QProgressBar#SecurityBusy::chunk {{ background-color: {accent}; border-radius: 4px; }}
            """
        )

    def _is_dark(self) -> bool:
        app = QApplication.instance()
        return bool(
            app is not None and app.palette().window().color().lightness() < 128
        )

    def _shadow_color(self):
        from PySide6.QtGui import QColor

        return QColor(5, 18, 55, 145 if self._is_dark() else 72)

    # ---------------------------------------------------------- validation

    def _live_validate(self) -> None:
        self.caps_lock_label.setVisible(_caps_lock_on())
        kind = str(self.method_combo.currentData())
        text = self.new_edit.text()
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
            validate_credential(kind, text, weak_pin_confirmed=weak_confirmed)
        except CredentialPolicyError as exc:
            self._set_status(str(exc), error=True)
            self.weak_pin_checkbox.setVisible(is_weak_portable_pin(kind, text))
            return
        self.weak_pin_checkbox.setVisible(is_weak_portable_pin(kind, text))
        if self.confirm_edit.text() and self.confirm_edit.text() != text:
            self._set_status("A confirmação não coincide.", error=True)
            return
        self._set_status("")

    def _set_status(self, message: str, *, error: bool = False) -> None:
        self.status_label.setText(message)
        self._status_is_error = error

    def _clear_secret_fields(self) -> None:
        """Clear secret widgets and release sensitive references on terminal completion."""
        try:
            self.current_edit.clear()
            self.new_edit.clear()
            self.confirm_edit.clear()
            self.weak_pin_checkbox.setChecked(False)
            self.status_label.setText("")
        except (RuntimeError, AttributeError):
            pass
        self._task = None

    # ------------------------------------------------------------- submit

    def _submit(self) -> None:
        if self._busy:
            return
        if self.controller is None:
            self._set_status("Controlador de segurança não disponível.", error=True)
            return
        kind = str(self.method_combo.currentData())
        weak_confirmed = self.weak_pin_checkbox.isChecked()
        if self.new_edit.text() != self.confirm_edit.text():
            self._set_status(
                "A confirmação não coincide com a nova credencial.", error=True
            )
            return

        # Snapshot data on the GUI thread before background execution
        current_secret = self.current_edit.text()
        new_secret = self.new_edit.text()
        controller = self.controller

        self._set_busy(True)
        self.change_started.emit()

        def work() -> Any:
            try:
                return controller.change_credential(
                    current_secret,
                    kind,
                    new_secret,
                    weak_pin_confirmed=weak_confirmed,
                )
            except Exception as exc:  # noqa: BLE001 - mapped by the UI thread
                return exc

        self._start_worker(work)

    def _start_worker(self, work: Any) -> None:
        """Non-blocking AsyncTask; results arrive via typed slots."""

        # BUG-AUD-C142: register against the window registry so closeEvent
        # cancels and waits for this thread before the database is closed.
        task = AsyncTask(
            self,
            registry=getattr(self.parent(), "background_task_registry", None),
            label="Alteração de credencial",
        )
        self._task = task
        task.result.connect(self._on_worker_result)
        task.error.connect(self._on_worker_error)
        if not task.start(work):
            self._set_busy(False)
            self._task = None
            self._set_status("Não foi possível iniciar a alteração.", error=True)

    def _on_worker_result(self, outcome: object) -> None:
        if self._destroyed:
            return
        self._task = None
        self._set_busy(False)
        if isinstance(outcome, BaseException):
            self._on_worker_error(str(outcome))
            return
        if not isinstance(outcome, CredentialChangeResult):
            self._on_worker_error("Resultado inesperado ao alterar credencial.")
            return
        self.result_state = outcome
        if outcome.ok:
            self.change_finished.emit(True)
            self.accept()
            return
        self._set_status(outcome.message, error=True)
        self.current_edit.selectAll()
        self.current_edit.setFocus()

    def _on_worker_error(self, message: str) -> None:
        if self._destroyed:
            return
        self._task = None
        self._set_busy(False)
        self._set_status(message, error=True)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.busy_bar.setVisible(busy)
        self.submit_button.setEnabled(not busy)
        self.cancel_button.setEnabled(not busy)
        self.current_edit.setEnabled(not busy)
        self.new_edit.setEnabled(not busy)
        self.confirm_edit.setEnabled(not busy)
        self.method_combo.setEnabled(not busy)

    # --------------------------------------------------------------- misc

    def accept(self) -> None:
        self._destroyed = True
        self._clear_secret_fields()
        super().accept()
        self.deleteLater()

    def reject(self) -> None:
        if self._busy:
            return
        self._destroyed = True
        self._clear_secret_fields()
        super().reject()
        self.deleteLater()

    def showEvent(self, event: Any) -> None:  # type: ignore[override]
        super().showEvent(event)
        screen = self.screen() or QApplication.primaryScreen()
        if screen is None:
            return
        area = screen.availableGeometry()
        self.adjustSize()
        width = min(DIALOG_PREFERRED_WIDTH, area.width() - 24)
        height = min(self.sizeHint().height(), area.height() - 24)
        self.resize(width, height)
        self.move(
            area.x() + max(0, (area.width() - self.width()) // 2),
            area.y() + max(0, (area.height() - self.height()) // 2),
        )
        self.raise_()
        self.activateWindow()
        self.current_edit.setFocus()

    def keyPressEvent(self, event: Any) -> None:  # type: ignore[override]
        if event.key() == Qt.Key_Escape and not self._busy:
            self.reject()
            return
        if event.key() == Qt.Key_Escape:
            return  # ignore escape while busy
        super().keyPressEvent(event)

    def closeEvent(self, event: Any) -> None:  # type: ignore[override]
        if self._busy:
            event.ignore()
            return
        self._destroyed = True
        self._clear_secret_fields()
        super().closeEvent(event)
        self.deleteLater()


__all__ = ["ChangeCredentialDialog"]
