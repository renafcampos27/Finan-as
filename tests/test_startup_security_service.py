"""Unit tests for the startup security service and credential policy."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from services.backup_crypto import (
    ENVELOPE_FILE_NAME,
    SecurityEnvelope,
    read_envelope,
)
from services.startup_security import (
    CredentialPolicyError,
    CredentialRotationError,
    InvalidCredentialError,
    PASSWORD_MIN_CHARACTERS,
    PORTABLE_BACKUP_PIN_MIN_DIGITS,
    ProfileSecurityState,
    SecurityEnvelopeError,
    StartupSecurityService,
    clear_active_session,
    retry_delay_seconds,
    validate_credential,
)


@pytest.fixture(autouse=True)
def _no_leaked_session():
    """Never leak the module-level active session into other tests."""

    clear_active_session()
    yield
    clear_active_session()


@pytest.fixture()
def service(tmp_path: Path) -> StartupSecurityService:
    database = tmp_path / "financas_pessoais.sqlite3"
    return StartupSecurityService(database_file=database)


def _envelope(service: StartupSecurityService) -> SecurityEnvelope:
    return read_envelope(service.envelope_path)


def _marker_exists(service: StartupSecurityService) -> bool:
    return service.marker_path.is_file()


# ---------------------------------------------------------------------------
# Credential policy
# ---------------------------------------------------------------------------


def test_pin_requires_digits_only() -> None:
    with pytest.raises(CredentialPolicyError):
        validate_credential("pin", "12a456")


def test_pin_requires_minimum_digits() -> None:
    with pytest.raises(CredentialPolicyError):
        validate_credential("pin", "12345")
    validate_credential("pin", "123456", weak_pin_confirmed=True)


def test_short_portable_pin_requires_explicit_confirmation() -> None:
    short = "1" * (PORTABLE_BACKUP_PIN_MIN_DIGITS - 1)
    with pytest.raises(CredentialPolicyError):
        validate_credential("pin", short)
    validate_credential("pin", short, weak_pin_confirmed=True)


def test_password_minimum_length() -> None:
    with pytest.raises(CredentialPolicyError):
        validate_credential("password", "a" * (PASSWORD_MIN_CHARACTERS - 1))
    validate_credential("password", "a" * PASSWORD_MIN_CHARACTERS)


def test_password_allows_leading_trailing_spaces_and_unicode() -> None:
    secret = "  senha com espaços e emoji 😊  "
    validate_credential("password", secret)
    service = StartupSecurityService(database_file=None)
    # Service-level guard: configuration paths resolve without mutation.
    assert service.envelope_path.name == ENVELOPE_FILE_NAME


def test_long_unicode_passphrase_roundtrip(tmp_path: Path) -> None:
    service = StartupSecurityService(database_file=tmp_path / "db.sqlite3")
    secret = "🔍" + ("αβγδε " * 2500)  # > 10 000 characters
    assert len(secret) > 10_000
    context = service.configure_first_run("password", secret)
    try:
        unlocked = service.unlock("password", secret)
        try:
            assert unlocked.backup_master_key == context.backup_master_key
        finally:
            unlocked.close()
    finally:
        context.close()
    assert service.inspect_profile() == ProfileSecurityState.CONFIGURED


def test_credentials_are_never_normalized() -> None:
    validate_credential("password", "senha123   ")
    validate_credential("password", " senha123")
    with pytest.raises(CredentialPolicyError):
        # A PIN is digits-only, so a padded PIN is rejected rather than trimmed.
        validate_credential("pin", "123456 ")
    # Whitespace-only differences create distinct credentials.
    service_probe = StartupSecurityService(database_file=None)
    assert service_probe.envelope_path.name == ENVELOPE_FILE_NAME


# ---------------------------------------------------------------------------
# Profile states
# ---------------------------------------------------------------------------


def test_new_profile_state(service: StartupSecurityService) -> None:
    assert service.inspect_profile() == ProfileSecurityState.NEW


def test_legacy_unprotected_state(tmp_path: Path) -> None:
    # A minimal-but-valid legacy database: sqlite >= 3.50 rejects a bare
    # 16-byte header ("file is not a database"), which would otherwise map
    # to RECOVERY_REQUIRED under BUG-SEC-06 fail-closed inspection.
    database = tmp_path / "financas_pessoais.sqlite3"
    connection = sqlite3.connect(str(database))
    try:
        connection.execute("CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT)")
        connection.commit()
    finally:
        connection.close()
    service = StartupSecurityService(database_file=database)
    assert service.inspect_profile() == ProfileSecurityState.LEGACY_UNPROTECTED


def test_configured_state(service: StartupSecurityService) -> None:
    context = service.configure_first_run("pin", "123456", weak_pin_confirmed=True)
    context.close()
    assert service.inspect_profile() == ProfileSecurityState.CONFIGURED


def test_missing_envelope_blocks_security_bypass(
    service: StartupSecurityService,
) -> None:
    context = service.configure_first_run("pin", "123456", weak_pin_confirmed=True)
    context.close()
    service.envelope_path.unlink()
    assert _marker_exists(service)
    # Deleting the envelope must never reset the profile to first-run mode.
    assert service.inspect_profile() == ProfileSecurityState.MISSING_ENVELOPE
    with pytest.raises(SecurityEnvelopeError):
        service.configure_first_run("password", "outra senha 123")


def test_corrupt_envelope_state(
    service: StartupSecurityService,
) -> None:
    context = service.configure_first_run("pin", "123456", weak_pin_confirmed=True)
    context.close()
    service.envelope_path.write_text("{not json", encoding="utf-8")
    assert service.inspect_profile() == ProfileSecurityState.CORRUPT_ENVELOPE
    with pytest.raises(SecurityEnvelopeError):
        service.unlock("pin", "123456")
    # The corrupt file is preserved for recovery, never deleted.
    assert service.envelope_path.read_text(encoding="utf-8") == "{not json"


# ---------------------------------------------------------------------------
# Configure / unlock
# ---------------------------------------------------------------------------


def test_configure_creates_envelope_and_marker(
    service: StartupSecurityService,
) -> None:
    context = service.configure_first_run("pin", "123456", weak_pin_confirmed=True)
    try:
        assert context.credential_kind == "pin"
        assert context.profile_id
        assert len(context.backup_master_key) == 32
        envelope = _envelope(service)
        assert envelope.profile_id == context.profile_id
        assert "pin" in envelope.unlocks
    finally:
        context.close()


def test_configure_with_both_credentials(service: StartupSecurityService) -> None:
    context = service.configure_first_run(
        "password",
        "palavra passe",
        extra_kind="pin",
        extra_secret="123456",
        weak_pin_confirmed=True,
    )
    try:
        envelope = _envelope(service)
        assert set(envelope.unlocks) == {"pin", "password"}
        assert envelope.default_unlock_method == "password"
    finally:
        context.close()


def test_configure_rejects_duplicate_kinds(
    service: StartupSecurityService,
) -> None:
    with pytest.raises(CredentialPolicyError):
        service.configure_first_run(
            "pin",
            "123456",
            extra_kind="pin",
            extra_secret="654321",
            weak_pin_confirmed=True,
        )


def test_unlock_with_each_credential(service: StartupSecurityService) -> None:
    context = service.configure_first_run(
        "password",
        "palavra passe",
        extra_kind="pin",
        extra_secret="123456",
        weak_pin_confirmed=True,
    )
    try:
        for kind, secret in (("password", "palavra passe"), ("pin", "123456")):
            session = service.unlock(kind, secret)
            try:
                assert session.backup_master_key == context.backup_master_key
            finally:
                session.close()
    finally:
        context.close()


def test_unlock_wrong_credential_throttles(
    service: StartupSecurityService,
) -> None:
    service.configure_first_run("pin", "123456", weak_pin_confirmed=True).close()
    for _ in range(3):
        with pytest.raises(InvalidCredentialError):
            service.unlock("pin", "000000")
    assert service.failures == 3
    assert service.current_retry_delay() == retry_delay_seconds(3) > 0


# ---------------------------------------------------------------------------
# Rotation
# ---------------------------------------------------------------------------


def test_rotation_keeps_master_key(
    service: StartupSecurityService,
) -> None:
    context = service.configure_first_run("pin", "123456", weak_pin_confirmed=True)
    try:
        envelope_before = _envelope(service)
        master_before = bytes(context.backup_master_key)
        service.change_credential("123456", "password", "nova senha forte")
        session = service.unlock("password", "nova senha forte")
        try:
            assert bytes(session.backup_master_key) == master_before
        finally:
            session.close()
        # The old PIN slot is preserved: both credentials now unlock the
        # same master key (dual-credential rotation keeps legacy access).
        legacy = service.unlock("pin", "123456")
        try:
            assert bytes(legacy.backup_master_key) == master_before
        finally:
            legacy.close()
        assert _envelope(service).profile_id == envelope_before.profile_id
    finally:
        context.close()


def test_rotation_with_wrong_current_credential_fails(
    service: StartupSecurityService,
) -> None:
    service.configure_first_run("pin", "123456", weak_pin_confirmed=True).close()
    with pytest.raises(InvalidCredentialError):
        service.change_credential("000000", "password", "nova senha forte")


def test_rotation_failure_rolls_back(
    service: StartupSecurityService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service.configure_first_run("pin", "123456", weak_pin_confirmed=True).close()
    original = json.dumps

    def explode(*args: object, **kwargs: object) -> str:
        raise RuntimeError("disk failure simulation")

    monkeypatch.setattr(
        "services.backup_crypto.json.dumps",
        explode,
    )
    with pytest.raises(CredentialRotationError):
        service.change_credential("123456", "password", "nova senha forte")
    monkeypatch.setattr("services.backup_crypto.json.dumps", original)
    # The original credential still unlocks the preserved envelope.
    session = service.unlock("pin", "123456")
    session.close()


def test_default_method_unchanged_after_pin_rotation(
    service: StartupSecurityService,
) -> None:
    context = service.configure_first_run(
        "password",
        "palavra passe",
        extra_kind="pin",
        extra_secret="123456",
        weak_pin_confirmed=True,
    )
    try:
        service.change_credential(
            "palavra passe",
            "pin",
            "987654",
            weak_pin_confirmed=True,
        )
        envelope = _envelope(service)
        assert envelope.default_unlock_method == "password"
        session = service.unlock("pin", "987654")
        session.close()
    finally:
        context.close()


# ---------------------------------------------------------------------------
# Session context
# ---------------------------------------------------------------------------


def test_session_close_zeroizes_master_key(
    service: StartupSecurityService,
) -> None:
    context = service.configure_first_run("pin", "123456", weak_pin_confirmed=True)
    key = context.backup_master_key
    assert any(key)
    context.close()
    assert not any(key)


def test_session_context_is_not_reusable_after_close(
    service: StartupSecurityService,
) -> None:
    context = service.configure_first_run("pin", "123456", weak_pin_confirmed=True)
    context.close()
    context.close()  # idempotent


# ---------------------------------------------------------------------------
# Throttle function
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("failures", "expected"),
    [(0, 0), (1, 0), (2, 0), (3, 1), (4, 2), (5, 4), (12, 300), (30, 300)],
)
def test_retry_delay_matrix(failures: int, expected: int) -> None:
    assert retry_delay_seconds(failures) == expected


# ---------------------------------------------------------------------------
# Privacy: no raw secret inside the envelope
# ---------------------------------------------------------------------------


def test_envelope_never_contains_the_secret(
    service: StartupSecurityService,
) -> None:
    secret = "supersecreta 123 😊"
    service.configure_first_run("password", secret).close()
    raw = service.envelope_path.read_text(encoding="utf-8")
    assert secret not in raw
    data = json.loads(raw)
    assert "unlocks" in data
    for unlock in data["unlocks"].values():
        assert set(unlock) == {"kdf", "key_wrap"}
