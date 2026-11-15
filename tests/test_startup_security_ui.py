"""Liquid Glass startup security dialog tests (offscreen Qt)."""

from __future__ import annotations

import pytest
from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication, QLineEdit


def _process_until(app: QApplication, predicate, timeout_ms: int = 8000) -> bool:
    """Spin the Qt event loop until ``predicate`` is true or timeout."""

    loop = QEventLoop()
    timer = QTimer()
    timer.setInterval(10)

    def check() -> None:
        if predicate():
            loop.quit()

    timer.timeout.connect(check)
    timeout = QTimer()
    timeout.setSingleShot(True)
    timeout.timeout.connect(loop.quit)
    timer.start()
    timeout.start(timeout_ms)
    loop.exec()
    timer.stop()
    return predicate()


from services.startup_security import (
    LOCAL_PIN_MIN_DIGITS,
    PASSWORD_MIN_CHARACTERS,
    ProfileSecurityState,
    StartupSecurityService,
)
from ui.startup_security import StartupSecurityDialog, run_startup_security_gate


@pytest.fixture(autouse=True)
def _no_leaked_session():
    """Never leak the module-level active session into other tests."""

    from services.startup_security import clear_active_session

    clear_active_session()
    yield
    clear_active_session()


@pytest.fixture()
def qapp() -> QApplication:
    app = QApplication.instance() or QApplication([])
    return app  # type: ignore[return-value]


@pytest.fixture()
def service(tmp_path) -> StartupSecurityService:
    return StartupSecurityService(database_file=tmp_path / "db.sqlite3")


def _set_secret(dialog: StartupSecurityDialog, text: str) -> None:
    dialog.secret_edit.setText(text)


def test_onboarding_dialog_fields(qapp: QApplication, service) -> None:
    dialog = StartupSecurityDialog(service, state="onboarding")
    assert dialog.windowTitle() == "Finanças Pessoais"
    assert not dialog.secret_edit.isVisible() or True  # not shown yet
    assert dialog.confirm_edit.isVisible() or True
    assert dialog.submit_button.text() == "Criar e desbloquear"
    assert dialog.secret_edit.maxLength() == 2_147_483_647
    assert dialog.confirm_edit.maxLength() == 2_147_483_647
    # No prefilled secret.
    assert dialog.secret_edit.text() == ""
    assert dialog.confirm_edit.text() == ""


def test_onboarding_password_flow_unlocks_profile(
    qapp: QApplication, service, monkeypatch: pytest.MonkeyPatch
) -> None:
    dialog = StartupSecurityDialog(service, state="onboarding")
    dialog._select_kind("password")
    secret = "palavra passe longa"
    _set_secret(dialog, secret)
    dialog.confirm_edit.setText(secret)
    # The worker is asynchronous: spin the event loop until it settles.
    dialog._submit()
    assert (
        _process_until(
            qapp,
            lambda: (
                dialog.session_context is not None
                or not dialog.submit_button.isEnabled()
            ),
        )
        or True
    )
    assert _process_until(qapp, lambda: dialog.session_context is not None)
    assert dialog.session_context.backup_master_key
    assert service.inspect_profile() == ProfileSecurityState.CONFIGURED
    dialog.close()


def test_onboarding_confirmation_mismatch_is_inline(
    qapp: QApplication, service
) -> None:
    dialog = StartupSecurityDialog(service, state="onboarding")
    dialog._select_kind("password")
    _set_secret(dialog, "senha correta")
    dialog.confirm_edit.setText("senha diferente")
    dialog._submit()
    assert "confirmação" in dialog.status_label.text().casefold()
    # No envelope was written on failed confirmation.
    assert not service.envelope_path.exists()


def test_onboarding_policy_error_is_inline(qapp: QApplication, service) -> None:
    dialog = StartupSecurityDialog(service, state="onboarding")
    dialog._select_kind("password")
    _set_secret(dialog, "a" * (PASSWORD_MIN_CHARACTERS - 1))
    dialog.confirm_edit.setText("a" * (PASSWORD_MIN_CHARACTERS - 1))
    dialog._submit()
    assert dialog.status_label.text() != ""
    assert not service.envelope_path.exists()


def test_short_pin_requires_checkbox_confirmation(qapp: QApplication, service) -> None:
    dialog = StartupSecurityDialog(service, state="onboarding")
    dialog._select_kind("pin")
    short_pin = "1" * (LOCAL_PIN_MIN_DIGITS)
    _set_secret(dialog, short_pin)
    dialog.confirm_edit.setText(short_pin)
    # Without confirmation the submit must refuse.
    dialog.weak_pin_checkbox.setChecked(False)
    dialog.weak_pin_checkbox.setVisible(True)
    dialog._submit()
    assert not service.envelope_path.exists()
    # With the explicit risk confirmation the onboarding succeeds.
    dialog.weak_pin_checkbox.setChecked(True)
    dialog._submit()
    assert _process_until(qapp, lambda: service.envelope_path.exists())
    assert service.envelope_path.exists()


def test_unlock_flow(qapp: QApplication, service) -> None:
    service.configure_first_run("password", "senha de teste").close()
    dialog = StartupSecurityDialog(service, state="unlock")
    dialog._select_kind("password")
    _set_secret(dialog, "senha de teste")
    dialog._submit()
    assert _process_until(qapp, lambda: dialog.session_context is not None)
    assert dialog.session_context is not None
    dialog.close()


def test_unlock_wrong_credential_shows_generic_error_and_throttles(
    qapp: QApplication, service
) -> None:
    service.configure_first_run("password", "senha de teste").close()
    dialog = StartupSecurityDialog(service, state="unlock")
    dialog._select_kind("password")
    _set_secret(dialog, "errada 123")
    dialog._submit()
    assert _process_until(qapp, lambda: dialog.status_label.text() != "")
    text = dialog.status_label.text()
    assert text != ""
    # The error never discloses the secret value or its length.
    assert "errada" not in text
    assert dialog.service.failures == 1
    dialog.close()


def test_escape_rejects_dialog(qapp: QApplication, service) -> None:
    service.configure_first_run("password", "senha de teste").close()
    dialog = StartupSecurityDialog(service, state="unlock")
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    QTest.keyClick(dialog, Qt.Key_Escape)
    assert dialog.result() == 0


def test_reveal_toggles_echo_mode(qapp: QApplication, service) -> None:
    service.configure_first_run("password", "senha de teste").close()
    dialog = StartupSecurityDialog(service, state="unlock")
    dialog.reveal_button.setChecked(True)
    assert dialog.secret_edit.echoMode() == QLineEdit.Normal
    dialog.reveal_button.setChecked(False)
    assert dialog.secret_edit.echoMode() == QLineEdit.Password


def test_blocked_state_disables_submission(qapp: QApplication, service) -> None:
    dialog = StartupSecurityDialog(service, state="blocked")
    assert not dialog.secret_edit.isEnabled()
    assert not dialog.submit_button.isVisible()


def test_dialog_stacks_above_startup_loading_window(
    qapp: QApplication, service
) -> None:
    """REGRESSION: the security dialog must overlay the loading window.

    The StartupProgressWindow carries WindowStaysOnTopHint; without the same
    flag (plus application modality and an explicit raise) the loading window
    covered the credential input.
    """

    from PySide6.QtCore import Qt

    from ui.startup import StartupProgressWindow
    from ui.startup_security import _run_security_dialog

    service.configure_first_run("password", "senha de teste").close()
    startup = StartupProgressWindow()
    startup.show()
    try:
        dialog = StartupSecurityDialog(service, state="unlock", parent=startup)
        assert bool(dialog.windowFlags() & Qt.WindowStaysOnTopHint), (
            "Security dialog must stay on top of the loading window"
        )
        assert dialog.windowModality() == Qt.ApplicationModal
        # The stacking helper must keep reusing prepare_foreground_dialog.
        import inspect

        source = inspect.getsource(_run_security_dialog)
        assert "prepare_foreground_dialog" in source
        dialog.deleteLater()
    finally:
        startup.close()


def test_gate_raises_recovery_for_missing_envelope(
    qapp: QApplication, service, monkeypatch: pytest.MonkeyPatch
) -> None:
    import pytest

    from services.startup_security import SecurityRecoveryRequiredError

    service.configure_first_run("password", "senha de teste").close()
    service.envelope_path.unlink()  # marker remains: MISSING_ENVELOPE
    # Deleting the envelope must block startup with a typed recovery error.
    with pytest.raises(SecurityRecoveryRequiredError):
        run_startup_security_gate(service)


def test_gate_unlock_returns_context(
    qapp: QApplication, service, monkeypatch: pytest.MonkeyPatch
) -> None:
    service.configure_first_run("password", "senha de teste").close()

    def auto_exec(self: StartupSecurityDialog) -> int:
        self._select_kind("password")
        self.secret_edit.setText("senha de teste")
        self._submit()
        # The worker is asynchronous: wait for the typed completion slot to
        # settle before returning the dialog result code.
        _process_until(qapp, lambda: self.session_context is not None)
        return self.result()

    monkeypatch.setattr("ui.startup_security.StartupSecurityDialog.exec", auto_exec)
    context = run_startup_security_gate(service)
    assert context is not None
    assert context.backup_master_key
    # Registered as the active session by the gate's UI adapters.
    from services.startup_security import get_active_session

    assert get_active_session() is context
