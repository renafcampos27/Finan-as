"""Comprehensive regression and boundary test suite for defects C17 through C20.

Audit items:
- C17 / S3 [P2 crypto]: Scrypt parameter validation disagrees with the backend.
  Validate effective scrypt parameters (n must be a power of 2 > 1) before invoking
  backend KDF; convert backend resource/parameter errors into typed BackupCryptoError.
- C18 / S4 [P2 concurrency/UI]: Credential worker reads GUI fields from background thread.
  Snapshot secret inputs on the GUI thread before worker dispatch; ensure worker callable
  is pure data-only and never dereferences QWidget instances across thread boundaries.
- C19 / S5 [P2 lifecycle/security]: Completed credential dialogs retain secret text.
  Centralize terminal cleanup (_clear_secret_fields) on accept/reject/close, clear secret
  text fields, and ensure dialog disposal via deleteLater so dialogs do not accumulate.
  Preserve input on retryable errors.
- C20 / S6 [P2 chart/privacy]: Accessible chart text keeps obsolete or unmasked amounts.
  Rebuild BalanceChart accessibleDescription from scratch on every data update, empty
  transition, warning update, and privacy toggle, eliminating stale text and unmasked leaks.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest
from PySide6.QtCore import QThread
from PySide6.QtWidgets import QApplication

from services.backup_crypto import (
    ARGON2ID_ITERATIONS,
    ARGON2ID_LANES,
    ARGON2ID_MEMORY_KIB,
    KEY_LENGTH_BYTES,
    MIN_SALT_BYTES,
    BackupCryptoError,
    CryptoFormatError,
    KdfParameters,
    derive_key,
    validate_kdf_parameters,
)
from services.security_settings import CredentialChangeResult
from ui.change_credential import ChangeCredentialDialog
from ui.components.charts.balance import BalanceChart
from ui.core.theme import Theme


@pytest.fixture(scope="session")
def qapp():
    """Ensure a QApplication instance exists for GUI widget testing."""
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


# ============================================================================
# C17 / S3: Scrypt parameter validation disagrees with the backend
# ============================================================================


def test_c17_scrypt_rejects_non_power_of_two_memory():
    """Effective n = memory_kib * 2 must be a power of 2.

    Non-power-of-two memory_kib values must be rejected by validate_kdf_parameters
    with CryptoFormatError before invoking hashlib.scrypt.
    """
    # 16385 * 2 = 32770 (not a power of 2)
    params = KdfParameters(
        name="scrypt",
        salt=os.urandom(MIN_SALT_BYTES),
        memory_kib=16385,
        iterations=3,
        lanes=1,
        length=KEY_LENGTH_BYTES,
    )
    with pytest.raises(CryptoFormatError, match="potência de 2"):
        validate_kdf_parameters(params)

    # Other non-powers of 2
    for bad_mem in (20000, 30000, 50000, 65535, 100000):
        p = KdfParameters(
            name="scrypt",
            salt=os.urandom(MIN_SALT_BYTES),
            memory_kib=bad_mem,
            iterations=1,
            lanes=1,
            length=KEY_LENGTH_BYTES,
        )
        with pytest.raises(CryptoFormatError, match="potência de 2"):
            validate_kdf_parameters(p)


def test_c17_scrypt_accepts_valid_power_of_two_memory():
    """Power-of-two memory_kib values within bounds must pass validation."""
    for valid_mem in (16384, 32768, 65536, 131072, 262144):
        params = KdfParameters(
            name="scrypt",
            salt=os.urandom(MIN_SALT_BYTES),
            memory_kib=valid_mem,
            iterations=1,
            lanes=2,
            length=KEY_LENGTH_BYTES,
        )
        validate_kdf_parameters(params)


def test_c17_scrypt_derive_key_success():
    """derive_key with valid scrypt parameters succeeds and returns 32-byte key."""
    params = KdfParameters(
        name="scrypt",
        salt=b"\x01" * 16,
        memory_kib=16384,  # n = 32768
        iterations=1,
        lanes=1,
        length=32,
    )
    derived = derive_key(params, b"test_secret_key_123")
    assert isinstance(derived, bytearray)
    assert len(derived) == 32


def test_c17_scrypt_derive_key_rejects_non_power_of_two_before_backend():
    """derive_key aborts before backend call when n is not power of 2."""
    params = KdfParameters(
        name="scrypt",
        salt=b"\x01" * 16,
        memory_kib=16385,
        iterations=1,
        lanes=1,
        length=32,
    )
    with pytest.raises(CryptoFormatError, match="potência de 2"):
        derive_key(params, b"secret")


def test_c17_scrypt_backend_resource_errors_become_typed_backup_crypto_error():
    """Backend resource/value errors during scrypt derivation become BackupCryptoError."""
    params = KdfParameters(
        name="scrypt",
        salt=b"\x01" * 16,
        memory_kib=16384,
        iterations=1,
        lanes=1,
        length=32,
    )
    with patch("hashlib.scrypt", side_effect=ValueError("scrypt maxmem exceeded")):
        with pytest.raises(BackupCryptoError, match="Falha na derivação scrypt"):
            derive_key(params, b"secret")

    with patch("hashlib.scrypt", side_effect=MemoryError("out of memory")):
        with pytest.raises(BackupCryptoError, match="Falha na derivação scrypt"):
            derive_key(params, b"secret")


def test_c17_argon2id_remains_unaffected_and_valid():
    """Argon2id validation and derivation remain intact."""
    params = KdfParameters(
        name="argon2id",
        salt=os.urandom(MIN_SALT_BYTES),
        memory_kib=ARGON2ID_MEMORY_KIB,
        iterations=ARGON2ID_ITERATIONS,
        lanes=ARGON2ID_LANES,
        length=KEY_LENGTH_BYTES,
    )
    validate_kdf_parameters(params)


def test_c17_scrypt_max_kdf_memory_boundary_derives_safely():
    """derive_key with MAX_KDF_MEMORY_KIB (262144) must safely provide sufficient maxmem."""
    params = KdfParameters(
        name="scrypt",
        salt=b"\x02" * 16,
        memory_kib=262144,
        iterations=1,
        lanes=4,
        length=32,
    )
    with patch("hashlib.scrypt") as mock_scrypt:
        mock_scrypt.return_value = b"\x00" * 32
        derived = derive_key(params, b"secret")
        assert len(derived) == 32
        _, kwargs = mock_scrypt.call_args
        assert kwargs["maxmem"] >= 536875008  # Must exceed 512 MiB + thread overhead
        assert kwargs["n"] == 524288
        assert kwargs["p"] == 4


def test_c17_validate_kdf_rejects_bool_and_non_ints():
    """validate_kdf_parameters rejects boolean flags and non-integer types."""
    # Test bool passed as memory_kib, iterations, lanes, or length
    for field_kwargs in (
        {"memory_kib": True},
        {"iterations": True},
        {"lanes": True},
        {"length": True},
        {"memory_kib": 16384.5},
    ):
        base_kwargs = {
            "name": "scrypt",
            "salt": b"\x01" * 16,
            "memory_kib": 16384,
            "iterations": 1,
            "lanes": 1,
            "length": 32,
        }
        base_kwargs.update(field_kwargs)
        params = KdfParameters(**base_kwargs)
        with pytest.raises(CryptoFormatError, match="deve ser um número inteiro"):
            validate_kdf_parameters(params)


# ============================================================================
# C18 / S4: Credential worker reads GUI fields from background thread
# ============================================================================


def test_c18_worker_does_not_access_widgets_from_background_thread(qapp):
    """The background task closure must not invoke .text() or touch widgets.

    Field getters are asserted to only be called on the GUI main thread.
    """
    main_thread = QThread.currentThread()

    mock_controller = MagicMock()
    mock_controller.change_credential.return_value = CredentialChangeResult(
        ok=True, message="Sucesso", method="password"
    )

    dialog = ChangeCredentialDialog(mock_controller)
    dialog.current_edit.setText("old_pass_123")
    dialog.new_edit.setText("new_pass_456")
    dialog.confirm_edit.setText("new_pass_456")

    # Instrument text() getters to verify they are NEVER called from any worker thread
    orig_current_text = dialog.current_edit.text
    orig_new_text = dialog.new_edit.text

    def safe_current_text():
        assert QThread.currentThread() == main_thread, "current_edit accessed from worker thread!"
        return orig_current_text()

    def safe_new_text():
        assert QThread.currentThread() == main_thread, "new_edit accessed from worker thread!"
        return orig_new_text()

    dialog.current_edit.text = safe_current_text
    dialog.new_edit.text = safe_new_text

    # Submit the form
    with patch.object(dialog, "_start_worker") as mock_start:
        dialog._submit()
        assert mock_start.called
        work_fn = mock_start.call_args[0][0]

    # Execute work_fn as if on a background worker thread
    # It must NOT call text() on the widgets
    result = work_fn()
    assert isinstance(result, CredentialChangeResult)
    assert result.ok is True
    mock_controller.change_credential.assert_called_once_with(
        "old_pass_123", "password", "new_pass_456", weak_pin_confirmed=False
    )
    dialog.deleteLater()


def test_c18_worker_preserves_exact_secret_values(qapp):
    """Whitespace, special characters, and Unicode must be preserved exactly."""
    mock_controller = MagicMock()
    mock_controller.change_credential.return_value = CredentialChangeResult(
        ok=True, message="Sucesso", method="pin"
    )

    dialog = ChangeCredentialDialog(mock_controller)
    dialog.method_combo.setCurrentIndex(1)  # Select PIN method
    dialog.weak_pin_checkbox.setChecked(True)
    secret_with_spaces = "  Secret with spaces and áçé!  "
    dialog.current_edit.setText("  old  ")
    dialog.new_edit.setText(secret_with_spaces)
    dialog.confirm_edit.setText(secret_with_spaces)

    with patch.object(dialog, "_start_worker") as mock_start:
        dialog._submit()
        work_fn = mock_start.call_args[0][0]

    work_fn()
    mock_controller.change_credential.assert_called_once_with(
        "  old  ", "pin", secret_with_spaces, weak_pin_confirmed=True
    )
    dialog.deleteLater()


def test_c18_worker_mismatched_confirmation_does_not_dispatch(qapp):
    """Mismatched confirmation stops on GUI thread before worker dispatch."""
    mock_controller = MagicMock()
    dialog = ChangeCredentialDialog(mock_controller)
    dialog.current_edit.setText("old")
    dialog.new_edit.setText("new1")
    dialog.confirm_edit.setText("new2")

    with patch.object(dialog, "_start_worker") as mock_start:
        dialog._submit()
        assert not mock_start.called
        assert "não coincide" in dialog.status_label.text()
    dialog.deleteLater()


# ============================================================================
# C19 / S5: Completed credential dialogs retain secret text
# ============================================================================


def test_c19_accept_clears_secret_fields_and_disposes(qapp):
    """On accept(), secret fields must be cleared and deleteLater scheduled."""
    mock_controller = MagicMock()
    dialog = ChangeCredentialDialog(mock_controller)
    dialog.current_edit.setText("secret_old")
    dialog.new_edit.setText("secret_new")
    dialog.confirm_edit.setText("secret_new")
    dialog.weak_pin_checkbox.setChecked(True)

    with patch.object(dialog, "deleteLater") as mock_delete_later:
        dialog.accept()
        assert mock_delete_later.called

    assert dialog.current_edit.text() == ""
    assert dialog.new_edit.text() == ""
    assert dialog.confirm_edit.text() == ""
    assert not dialog.weak_pin_checkbox.isChecked()
    dialog.deleteLater()


def test_c19_reject_clears_secret_fields_and_disposes(qapp):
    """On reject() (Cancel / Esc), secret fields must be cleared and deleteLater scheduled."""
    mock_controller = MagicMock()
    dialog = ChangeCredentialDialog(mock_controller)
    dialog.current_edit.setText("secret_old")
    dialog.new_edit.setText("secret_new")
    dialog.confirm_edit.setText("secret_new")

    with patch.object(dialog, "deleteLater") as mock_delete_later:
        dialog.reject()
        assert mock_delete_later.called

    assert dialog.current_edit.text() == ""
    assert dialog.new_edit.text() == ""
    assert dialog.confirm_edit.text() == ""
    dialog.deleteLater()


def test_c19_close_event_clears_secret_fields_and_disposes(qapp):
    """On closeEvent(), secret fields must be cleared and deleteLater scheduled."""
    from PySide6.QtGui import QCloseEvent

    mock_controller = MagicMock()
    dialog = ChangeCredentialDialog(mock_controller)
    dialog.current_edit.setText("secret_old")
    dialog.new_edit.setText("secret_new")
    dialog.confirm_edit.setText("secret_new")

    event = QCloseEvent()
    with patch.object(dialog, "deleteLater") as mock_delete_later:
        dialog.closeEvent(event)
        assert event.isAccepted()
        assert mock_delete_later.called

    assert dialog.current_edit.text() == ""
    assert dialog.new_edit.text() == ""
    assert dialog.confirm_edit.text() == ""
    dialog.deleteLater()


def test_c19_busy_state_blocks_reject_and_close(qapp):
    """While busy, closeEvent and reject must be ignored."""
    from PySide6.QtGui import QCloseEvent

    mock_controller = MagicMock()
    dialog = ChangeCredentialDialog(mock_controller)
    dialog.current_edit.setText("secret")
    dialog._set_busy(True)

    event = QCloseEvent()
    dialog.closeEvent(event)
    assert not event.isAccepted()
    assert dialog.current_edit.text() == "secret"

    dialog.reject()
    assert dialog.current_edit.text() == "secret"

    dialog._set_busy(False)
    dialog.deleteLater()


def test_c19_retryable_error_preserves_input_for_correction(qapp):
    """On retryable validation error, fields are NOT cleared, allowing edit."""
    mock_controller = MagicMock()
    dialog = ChangeCredentialDialog(mock_controller)
    dialog.current_edit.setText("wrong_password")
    dialog.new_edit.setText("new_password_123")
    dialog.confirm_edit.setText("new_password_123")

    # Simulate worker error result
    error_result = CredentialChangeResult(
        ok=False, message="Credencial atual incorreta.", method="password"
    )
    dialog._on_worker_result(error_result)

    assert dialog.current_edit.text() == "wrong_password"
    assert dialog.new_edit.text() == "new_password_123"
    assert dialog.confirm_edit.text() == "new_password_123"
    assert "incorreta" in dialog.status_label.text()
    dialog.deleteLater()


def test_c19_settings_open_dialog_schedules_delete_later(qapp, tmp_path):
    """SettingsPage.open_change_credential_dialog ensures dialog.deleteLater is called."""
    from ui.pages.settings import SettingsPage

    mock_app = MagicMock()
    mock_app.theme = Theme(dark=False, privacy=False)
    mock_app.repo.setting.return_value = "0"
    mock_app.repo.db.path = tmp_path / "synthetic.sqlite3"
    mock_app.ai_service = MagicMock()
    mock_app.backup_service = MagicMock()
    mock_app.background_task_registry = MagicMock()
    mock_app.analytics_service = MagicMock()
    mock_app.payments_service = MagicMock()
    mock_app.import_service = MagicMock()

    mock_security = MagicMock()

    page = SettingsPage(mock_app, security_controller=mock_security)

    with patch("ui.change_credential.ChangeCredentialDialog.exec") as mock_exec, \
         patch("ui.change_credential.ChangeCredentialDialog.deleteLater") as mock_delete:
        mock_exec.return_value = 0
        page.open_change_credential_dialog()
        assert mock_delete.called

    page.deleteLater()


def test_c19_accept_and_reject_mark_dialog_destroyed(qapp):
    """accept() and reject() must set _destroyed to drop any pending worker callbacks."""
    mock_controller = MagicMock()
    dialog = ChangeCredentialDialog(mock_controller)
    assert not dialog._destroyed

    dialog.reject()
    assert dialog._destroyed is True

    dialog2 = ChangeCredentialDialog(mock_controller)
    dialog2.accept()
    assert dialog2._destroyed is True


# ============================================================================
# C20 / S6: Accessible chart text keeps obsolete or unmasked amounts
# ============================================================================


def _theme_provider(privacy: bool = False):
    t = Theme(dark=False, privacy=privacy)
    return lambda: t


def test_c20_balance_chart_forecast_to_historical_clears_forecast_text(qapp):
    """Transitioning from forecast data to historical-only data rebuilds description from scratch."""
    chart = BalanceChart(theme_provider=_theme_provider(privacy=False))

    # 1. Provide forecast data
    forecast_points = [
        {
            "date": "2026-08-01",
            "balance": 1000.0,
            "balance_base": 1000.0,
            "balance_pessimistic": 900.0,
            "balance_optimistic": 1100.0,
            "is_forecast": True,
        }
    ]
    chart.set_points(forecast_points)
    desc_forecast = chart.accessibleDescription()
    assert "Previsão de saldo com cenário base" in desc_forecast
    assert "1 000" in desc_forecast
    assert "900" in desc_forecast
    assert "1 100" in desc_forecast

    # 2. Transition to historical-only data
    historical_points = [
        {
            "date": "2026-08-01",
            "balance": 2500.0,
            "balance_base": 2500.0,
            "is_forecast": False,
        },
        {
            "date": "2026-08-02",
            "balance": 2600.0,
            "balance_base": 2600.0,
            "is_forecast": False,
        },
    ]
    chart.set_points(historical_points)
    desc_history = chart.accessibleDescription()

    # All forecast wording and amounts must be gone
    assert "Previsão de saldo" not in desc_history
    assert "900" not in desc_history
    assert "1 100" not in desc_history
    assert "Evolução histórica do saldo com 2 ponto(s)" in desc_history
    assert "2 600" in desc_history
    chart.deleteLater()


def test_c20_balance_chart_forecast_to_empty_clears_forecast_text(qapp):
    """Transitioning from forecast data to empty data cleans assistive description."""
    chart = BalanceChart(theme_provider=_theme_provider(privacy=False))

    chart.set_points(
        [
            {
                "date": "2026-08-01",
                "balance": 1000.0,
                "balance_base": 1000.0,
                "balance_pessimistic": 800.0,
                "balance_optimistic": 1200.0,
                "is_forecast": True,
            }
        ]
    )
    assert "Previsão de saldo" in chart.accessibleDescription()

    # Empty transition
    chart.set_points([])
    desc_empty = chart.accessibleDescription()
    assert "Previsão de saldo" not in desc_empty
    assert "800" not in desc_empty
    assert "1 200" not in desc_empty
    assert "sem dados disponíveis" in desc_empty
    chart.deleteLater()


def test_c20_balance_chart_warnings_to_clean_removes_warnings(qapp):
    """Quality warnings and invalid points must be removed when clean data is set."""
    chart = BalanceChart(theme_provider=_theme_provider(privacy=False))

    # Set data with warning and invalid point
    points_with_warnings = [
        {
            "date": "2026-08-01",
            "balance": 100.0,
            "quality_warnings": ["Dados incompletos"],
        },
        {
            "date": None,  # Invalid point
            "balance": 200.0,
        },
    ]
    chart.set_points(points_with_warnings)
    desc_warn = chart.accessibleDescription()
    assert "1 ponto(s) inválido(s) foram excluídos" in desc_warn
    assert "Aviso de qualidade: Dados incompletos" in desc_warn

    # Set clean data
    clean_points = [
        {
            "date": "2026-08-01",
            "balance": 500.0,
        }
    ]
    chart.set_points(clean_points)
    desc_clean = chart.accessibleDescription()
    assert "inválido(s)" not in desc_clean
    assert "Aviso de qualidade" not in desc_clean
    assert "Dados incompletos" not in desc_clean
    chart.deleteLater()


def test_c20_balance_chart_repeated_invalid_points_do_not_duplicate_warning(qapp):
    """Subsequent updates with invalid points rebuild description without accumulating warnings."""
    chart = BalanceChart(theme_provider=_theme_provider(privacy=False))

    bad_points = [
        {"date": "2026-08-01", "balance": 100.0},
        {"date": None, "balance": 200.0},
    ]
    chart.set_points(bad_points)
    desc1 = chart.accessibleDescription()
    assert desc1.count("1 ponto(s) inválido(s) foram excluídos.") == 1

    chart.set_points(bad_points)
    desc2 = chart.accessibleDescription()
    assert desc2.count("1 ponto(s) inválido(s) foram excluídos.") == 1
    chart.deleteLater()


def test_c20_balance_chart_privacy_toggle_immediately_masks_accessible_text(qapp):
    """When privacy mode is toggled on, invalidate_cache updates assistive description immediately."""
    current_theme = Theme(dark=False, privacy=False)
    chart = BalanceChart(theme_provider=lambda: current_theme)

    chart.set_points(
        [
            {
                "date": "2026-08-01",
                "balance": 1500.0,
                "balance_base": 1500.0,
                "balance_pessimistic": 1200.0,
                "balance_optimistic": 1800.0,
                "is_forecast": True,
            }
        ]
    )
    desc_public = chart.accessibleDescription()
    assert "1 500" in desc_public
    assert "1 200" in desc_public
    assert "1 800" in desc_public

    # Toggle privacy to True
    current_theme = Theme(dark=False, privacy=True)
    chart.invalidate_cache()

    desc_private = chart.accessibleDescription()
    assert "1 500" not in desc_private
    assert "1 200" not in desc_private
    assert "1 800" not in desc_private
    assert "••••• €" in desc_private

    # Toggle back to False
    current_theme = Theme(dark=False, privacy=False)
    chart.invalidate_cache()

    desc_restored = chart.accessibleDescription()
    assert "1 500" in desc_restored
    assert "••••• €" not in desc_restored
    chart.deleteLater()


def test_c20_balance_chart_both_historical_and_forecast_description(qapp):
    """Accessible description handles combined historical and forecast points gracefully."""
    chart = BalanceChart(theme_provider=_theme_provider(privacy=False))
    mixed_points = [
        {"date": "2026-08-01", "balance": 1000.0, "is_forecast": False},
        {
            "date": "2026-08-02",
            "balance": 1200.0,
            "balance_base": 1200.0,
            "balance_pessimistic": 1100.0,
            "balance_optimistic": 1300.0,
            "is_forecast": True,
        },
    ]
    chart.set_points(mixed_points)
    desc = chart.accessibleDescription()
    assert "Previsão de saldo com cenário base" in desc
    assert "1 200" in desc
    assert "1 100" in desc
    assert "1 300" in desc
    chart.deleteLater()


def test_c20_balance_chart_missing_dict_keys_does_not_raise_key_error(qapp):
    """Direct manipulation of points missing optional forecast keys does not crash."""
    chart = BalanceChart(theme_provider=_theme_provider(privacy=False))
    chart.points = [{"is_forecast": True}]
    # Must not raise KeyError when accessing balance_base / balance_pessimistic / etc.
    chart.update_accessible_description()
    assert "Previsão de saldo com cenário base" in chart.accessibleDescription()
    chart.deleteLater()

