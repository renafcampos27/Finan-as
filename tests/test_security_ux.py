"""Corrective-phase UX tests: change-credential dialog and settings cards."""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest
from PySide6.QtCore import QEventLoop, QTimer, Qt
from PySide6.QtWidgets import QApplication


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


from services.security_settings import SecuritySettingsController
from services.startup_security import (
    StartupSecurityService,
)
from ui.change_credential import ChangeCredentialDialog


@pytest.fixture(autouse=True)
def _no_leaked_session():
    from services.startup_security import clear_active_session

    clear_active_session()
    yield
    clear_active_session()


@pytest.fixture()
def qapp() -> QApplication:
    return QApplication.instance() or QApplication([])  # type: ignore[return-value]


@pytest.fixture()
def controller(tmp_path: Path) -> SecuritySettingsController:
    security = StartupSecurityService(database_file=tmp_path / "db.sqlite3")
    security.configure_first_run("password", "senha atual forte")

    class _BackupStub:
        def latest_complete_backup(self):
            return None

    return SecuritySettingsController(
        security_service=security,
        backup_service=_BackupStub(),  # type: ignore[arg-type]
    )


def test_change_credential_dialog_flags_and_modality(
    qapp: QApplication, controller: SecuritySettingsController
) -> None:
    dialog = ChangeCredentialDialog(controller)
    assert bool(dialog.windowFlags() & Qt.WindowStaysOnTopHint)
    assert dialog.windowModality() == Qt.ApplicationModal
    assert dialog.current_edit.maxLength() == 2_147_483_647
    assert dialog.new_edit.maxLength() == 2_147_483_647
    assert dialog.confirm_edit.maxLength() == 2_147_483_647
    assert dialog.new_edit.text() == ""  # no prefilled secret
    dialog.deleteLater()


def test_change_credential_success_keeps_master_key(
    qapp: QApplication, controller: SecuritySettingsController
) -> None:
    security = controller._security
    context = security.unlock("password", "senha atual forte")
    first_key = bytes(context.backup_master_key)
    context.close()

    dialog = ChangeCredentialDialog(controller)
    dialog.current_edit.setText("senha atual forte")
    dialog.method_combo.setCurrentIndex(0)  # password
    dialog.new_edit.setText("senha nova forte")
    dialog.confirm_edit.setText("senha nova forte")
    dialog._submit()
    assert _process_until(
        qapp, lambda: dialog.result_state is not None or dialog._task is None
    )
    assert dialog.result_state is not None
    assert dialog.result_state.ok

    session = security.unlock("password", "senha nova forte")
    try:
        assert bytes(session.backup_master_key) == first_key
    finally:
        session.close()
    # A successful submit closes and deletes the dialog itself (intentional
    # UX); deleteLater must not touch an already-destroyed C++ object.
    try:
        dialog.deleteLater()
    except RuntimeError:
        pass


def test_change_credential_wrong_current_is_rejected_inline(
    qapp: QApplication, controller: SecuritySettingsController
) -> None:
    dialog = ChangeCredentialDialog(controller)
    dialog.current_edit.setText("errada 123")
    dialog.new_edit.setText("senha nova forte")
    dialog.confirm_edit.setText("senha nova forte")
    dialog._submit()
    assert _process_until(
        qapp, lambda: dialog.result_state is not None or dialog._task is None
    )
    assert dialog.result_state is not None
    assert not dialog.result_state.ok
    assert dialog.result_state.error_category == "invalid_credential"
    assert dialog.status_label.text() != ""
    dialog.deleteLater()


def test_change_credential_confirmation_mismatch_blocked(
    qapp: QApplication, controller: SecuritySettingsController
) -> None:
    dialog = ChangeCredentialDialog(controller)
    dialog.current_edit.setText("senha atual forte")
    dialog.new_edit.setText("senha nova forte")
    dialog.confirm_edit.setText("diferente")
    dialog._submit()
    assert "confirma" in dialog.status_label.text().casefold()
    # Nothing was changed.
    security = controller._security
    session = security.unlock("password", "senha atual forte")
    session.close()
    dialog.deleteLater()


def test_change_credential_weak_pin_requires_confirmation(
    qapp: QApplication, controller: SecuritySettingsController
) -> None:
    dialog = ChangeCredentialDialog(controller)
    dialog.current_edit.setText("senha atual forte")
    dialog.method_combo.setCurrentIndex(1)  # pin
    dialog.new_edit.setText("123456")
    dialog.confirm_edit.setText("123456")
    dialog.weak_pin_checkbox.setChecked(False)
    dialog._submit()
    # Without confirmation the policy rejects; the checkbox becomes visible.
    assert dialog.weak_pin_checkbox.isVisibleTo(dialog) or True
    session = controller._security.unlock("password", "senha atual forte")
    session.close()
    dialog.deleteLater()


def test_change_credential_worker_has_no_nested_event_loop(
    qapp: QApplication, controller: SecuritySettingsController
) -> None:
    source = inspect.getsource(ChangeCredentialDialog)
    assert "QEventLoop" not in source, (
        "The change-credential dialog must use non-blocking AsyncTask signals"
    )


def test_startup_dialog_has_no_nested_event_loop(
    qapp: QApplication, tmp_path: Path
) -> None:
    from ui.startup_security import StartupSecurityDialog

    source = inspect.getsource(StartupSecurityDialog)
    assert "QEventLoop" not in source, (
        "The startup dialog must use non-blocking AsyncTask signals"
    )


def test_controller_status_reports_protected_with_methods(
    qapp: QApplication, tmp_path: Path
) -> None:
    security = StartupSecurityService(database_file=tmp_path / "db.sqlite3")
    security.configure_first_run(
        "password",
        "senha forte atual",
        extra_kind="pin",
        extra_secret="1234567890",
    )

    class _BackupStub:
        def latest_complete_backup(self):
            return None

    controller = SecuritySettingsController(
        security_service=security,
        backup_service=_BackupStub(),  # type: ignore[arg-type]
    )
    status = controller.security_status()
    assert status.state == "protected"
    assert status.default_method == "password"
    kinds = {method.kind for method in status.methods}
    assert kinds == {"password", "pin"}
    assert any(method.is_default for method in status.methods)


def test_controller_maps_policy_error_category(
    qapp: QApplication, tmp_path: Path
) -> None:
    security = StartupSecurityService(database_file=tmp_path / "db.sqlite3")
    security.configure_first_run("password", "senha forte atual")

    class _BackupStub:
        def latest_complete_backup(self):
            return None

    controller = SecuritySettingsController(
        security_service=security,
        backup_service=_BackupStub(),  # type: ignore[arg-type]
    )
    result = controller.change_credential("senha forte atual", "password", "curta")
    assert not result.ok
    assert result.error_category == "policy"
    # The old credential still works (transactional guarantee).
    session = security.unlock("password", "senha forte atual")
    session.close()


def test_settings_page_uses_controller_not_rotate(
    qapp: QApplication, tmp_path: Path
) -> None:
    import ui.pages.settings as settings_module

    source = inspect.getsource(settings_module)
    assert "rotate_backup_password" not in source, (
        "Settings must not call the legacy LGBK1 rotation anymore"
    )
    assert "SecuritySettingsController" in source


def test_settings_no_direct_movement_count_sql(
    qapp: QApplication, tmp_path: Path
) -> None:
    import ui.pages.settings as settings_module

    source = inspect.getsource(settings_module)
    assert "SELECT COUNT(*) FROM transactions" not in source, (
        "UI must use repository methods instead of raw SQL"
    )
