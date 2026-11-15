"""Corrective-phase regression tests (SEC-05..SEC-08).

Covers: LGBK2 credential ownership split, unlimited-length passphrases,
transactional rotation with fault injection, fail-closed LGBK2 encryption,
envelope/marker consistency binding and exact-once session ownership.
"""

from __future__ import annotations

import os

import json
from pathlib import Path
from unittest.mock import patch
from typing import Any

import pytest

from services.backup_crypto import (
    BackupCryptoCodec,
    BackupCryptoError,
    LGBK1_MAGIC,
    LGBK2_MAGIC,
    marker_matches_envelope,
    read_envelope,
    read_security_marker,
)
from database.engine import FinanceDB
from services.backup import BackupService
from services.startup_security import (
    CredentialPolicyError,
    CredentialRotationError,
    InvalidCredentialError,
    ProfileSecurityState,
    SecurityEnvelopeError,
    StartupSecurityService,
    clear_active_session,
    get_active_session,
    set_active_session,
)


@pytest.fixture(autouse=True)
def _no_leaked_session():
    clear_active_session()
    yield
    clear_active_session()


@pytest.fixture()
def service(tmp_path: Path) -> StartupSecurityService:
    return StartupSecurityService(database_file=tmp_path / "db.sqlite3")


class DatabaseStub:
    def __init__(self, path: Path):
        import sqlite3

        self.conn = sqlite3.connect(path)
        self.path = path

    def backup_to(self, target: Path) -> None:
        import sqlite3

        destination = sqlite3.connect(target)
        try:
            self.conn.backup(destination)
            destination.commit()
        finally:
            destination.close()


class RepoStub:
    def __init__(self, path: Path):
        self.db = DatabaseStub(path)

    def setting(self, _key: str, default: str = "") -> str:
        return default

    def set_setting(self, _key: str, _value: str) -> None:
        return None


@pytest.fixture()
def backup_env(tmp_path: Path):
    active = tmp_path / "active.sqlite3"
    with patch.dict(os.environ, {"FINANCAS_PESSOAIS_DEMO_DATA": "0"}):
        database = FinanceDB(active)
    database.conn.close()
    backup = BackupService(
        RepoStub(active),
        backup_directory=tmp_path / "backups",
        database_file=active,
        preferences_file=tmp_path / "preferences.json",
        attachments_directory=tmp_path / "anexos",
    )
    security = StartupSecurityService(database_file=active)
    return backup, security, tmp_path


# ---------------------------------------------------------------------------
# SEC-05: LGBK2 credential ownership (rotation via change_credential)
# ---------------------------------------------------------------------------


def test_rotation_keeps_master_key_and_lgbk2_backups_readable(
    backup_env,
) -> None:
    backup, security, tmp_path = backup_env
    context = security.configure_first_run("password", "senha original")
    set_active_session(context)
    try:
        preferences = backup.preferences()
        preferences["backup_encryption_enabled"] = True
        backup._write_preferences_file(preferences)
        old_backup = backup.create_manual_backup()
        assert BackupCryptoCodec.detect_magic(old_backup) == LGBK2_MAGIC
        master_before = bytes(context.backup_master_key)
        # Rotate the password through the canonical service API.
        security.change_credential("senha original", "password", "senha nova forte")
        # Same master key: both the live session and backups stay readable.
        session = security.unlock("password", "senha nova forte")
        try:
            assert bytes(session.backup_master_key) == master_before
        finally:
            session.close()
        report = backup.validate_backup_file(old_backup)
        assert isinstance(report, dict)
    finally:
        set_active_session(None)
        context.close()


def test_new_lgbk2_backup_readable_after_rewrap(backup_env) -> None:
    backup, security, tmp_path = backup_env
    context = security.configure_first_run("password", "primeira senha")
    set_active_session(context)
    try:
        preferences = backup.preferences()
        preferences["backup_encryption_enabled"] = True
        backup._write_preferences_file(preferences)
        security.change_credential("primeira senha", "password", "segunda senha")
        # A backup created AFTER the rotation uses the same master key.
        new_backup = backup.create_manual_backup()
        assert BackupCryptoCodec.detect_magic(new_backup) == LGBK2_MAGIC
        assert backup.validate_backup_file(new_backup) is not None
    finally:
        set_active_session(None)
        context.close()


def test_explicit_password_is_never_silently_ignored(backup_env) -> None:
    """Encryption with an explicit password must honour that password."""

    backup, security, tmp_path = backup_env
    context = security.configure_first_run("password", "senha da sessão")
    set_active_session(context)
    try:
        source = tmp_path / "payload.zip"
        source.write_bytes(b"payload-bytes")
        target = tmp_path / "out.lgbak"
        # An explicit password with an active session must fail closed rather
        # than silently encrypt with the session key and validate wrongly.
        with pytest.raises(Exception):
            backup._create_encrypted_complete_backup(
                target,
                password="outra senha explícita",
                purpose="explicit_password_check",
            )
        assert not target.exists() or target.is_file()
        # Never a silent mismatch: after this corrective phase the explicit
        # password must either be honoured or the operation must fail loudly;
        # encrypting with the session key while claiming the explicit one is
        # forbidden. Current implementation fails closed.
        if target.is_file():
            out = tmp_path / "probe.sqlite3"
            with pytest.raises(Exception):
                BackupCryptoCodec.decrypt(
                    target,
                    out,
                    credential="senha da sessão".encode("utf-8"),
                )
    finally:
        set_active_session(None)
        context.close()


def test_lgbk2_failure_fails_closed_without_lgbk1_fallback(
    backup_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backup, security, tmp_path = backup_env
    context = security.configure_first_run("password", "senha da sessão")
    set_active_session(context)
    try:
        preferences = backup.preferences()
        preferences["backup_encryption_enabled"] = True
        backup._write_preferences_file(preferences)

        import services.backup_crypto as crypto_module

        def exploding_encrypt(*args: Any, **kwargs: Any) -> None:
            raise OSError("fault injection inside LGBK2 encryption")

        monkeypatch.setattr(
            crypto_module.BackupCryptoCodec, "encrypt_v2", exploding_encrypt
        )
        with pytest.raises(BackupCryptoError) as excinfo:
            backup.create_manual_backup()
        # Generic error (no crypto details, no silent downgrade).
        assert "LGBK1" not in str(excinfo.value)
        # No partial output left in the backup directory.
        assert not any(backup.backup_directory.glob("*.lgbak"))
    finally:
        set_active_session(None)
        context.close()


def test_rotate_backup_password_is_blocked_with_active_session(
    backup_env,
) -> None:
    backup, security, tmp_path = backup_env
    context = security.configure_first_run("password", "senha da sessão")
    set_active_session(context)
    try:
        with pytest.raises(BackupCryptoError):
            backup.rotate_backup_password("antiga", "nova senha 123")
    finally:
        set_active_session(None)
        context.close()


def test_lgbk1_backups_still_open_through_legacy_path(backup_env) -> None:
    backup, security, tmp_path = backup_env
    source = tmp_path / "payload.zip"
    source.write_bytes(b"legacy payload")
    legacy = backup.backup_directory / "legacy.lgbak"
    backup.encrypt_file(source, legacy, "palavra legacy 123", payload_kind="Z")
    assert BackupCryptoCodec.detect_magic(legacy) == LGBK1_MAGIC
    context = security.configure_first_run("password", "senha nova forte")
    set_active_session(context)
    try:
        out = tmp_path / "legacy_out.zip"
        kind = BackupCryptoCodec.decrypt(legacy, out, credential=b"palavra legacy 123")
        assert kind == "Z"
        assert out.read_bytes() == source.read_bytes()
    finally:
        set_active_session(None)
        context.close()


# ---------------------------------------------------------------------------
# SEC-06: unlimited-length passphrases (no WinCred dependency for LGBK2)
# ---------------------------------------------------------------------------


def test_passphrase_far_beyond_wincredd_limit_full_lifecycle(
    tmp_path: Path,
) -> None:
    security = StartupSecurityService(database_file=tmp_path / "db.sqlite3")
    # ~21k characters of mixed Unicode: far beyond the 2560-byte WinCred cap.
    passphrase = ("🔑" + "αβγδεζηθ ") * 1900
    assert len(passphrase.encode("utf-8")) > 20_000
    context = security.configure_first_run("password", passphrase)
    try:
        session = security.unlock("password", passphrase)
        try:
            assert session.backup_master_key == context.backup_master_key
        finally:
            session.close()
        # Wrong-by-one credential must fail without normalization.
        with pytest.raises(InvalidCredentialError):
            security.unlock("password", passphrase + " ")
    finally:
        context.close()
    # The stored envelope contains no raw secret and no WinCred dependency.
    raw = security.envelope_path.read_text(encoding="utf-8")
    assert passphrase not in raw
    assert len(raw) < 20_000


def test_long_passphrase_backup_validate_and_restore(backup_env) -> None:
    backup, security, tmp_path = backup_env
    passphrase = ("s€nh4-µnsual-" * 900) + "🏁"
    assert len(passphrase.encode("utf-8")) > 2560
    context = security.configure_first_run("password", passphrase)
    set_active_session(context)
    try:
        preferences = backup.preferences()
        preferences["backup_encryption_enabled"] = True
        backup._write_preferences_file(preferences)
        target = backup.create_manual_backup()
        assert BackupCryptoCodec.detect_magic(target) == LGBK2_MAGIC
        assert backup.validate_backup_file(target) is not None
        safety = backup.restore_backup(target)
        assert safety.is_file()
    finally:
        set_active_session(None)
        context.close()


# ---------------------------------------------------------------------------
# SEC-07: transactional rotation with fault injection
# ---------------------------------------------------------------------------


class _ExplodingFile:
    """Proxy that raises once write_text/open-write is invoked."""

    def __init__(self, real: Any, boom: dict[str, bool]) -> None:
        self._real = real
        self._boom = boom


def _fault(monkeypatch: pytest.MonkeyPatch, module: str, name: str) -> None:
    import services.backup_crypto as crypto_module

    def explode(path: Path, payload: bytes) -> None:
        raise OSError("fault injection: disk full")

    monkeypatch.setattr(crypto_module, "_atomic_write_private", explode)


def test_fault_during_candidate_write_rolls_back(
    service: StartupSecurityService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = service.configure_first_run("password", "senha original")
    context.close()  # onboarding writes done; rotation writes start at call 1
    import services.backup_crypto as crypto_module

    original = crypto_module._atomic_write_private

    def explode_first(path: Path, payload: bytes) -> None:
        raise OSError("fault injection during candidate write")

    monkeypatch.setattr(crypto_module, "_atomic_write_private", explode_first)
    with pytest.raises(CredentialRotationError):
        service.change_credential("senha original", "password", "senha nova")
    monkeypatch.setattr(crypto_module, "_atomic_write_private", original)
    session = service.unlock("password", "senha original")
    session.close()


def test_fault_during_reopen_verification_rolls_back(
    service: StartupSecurityService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service.configure_first_run("password", "senha original").close()

    def explode(path: Path) -> Any:
        raise OSError("fault injection during reopen")

    # Patch read_envelope only after the initial load happened: change_
    # credential loads the envelope at entry, so count calls.
    import services.startup_security as security_module

    real_read = security_module.read_envelope
    state = {"reads": 0}

    def counting_read(path: Path) -> Any:
        state["reads"] += 1
        if state["reads"] == 2:  # 1: entry load, 2: candidate reopen
            raise OSError("fault injection during reopen")
        return real_read(path)

    monkeypatch.setattr(security_module, "read_envelope", counting_read)
    with pytest.raises(CredentialRotationError):
        service.change_credential("senha original", "password", "senha nova")
    monkeypatch.setattr(security_module, "read_envelope", real_read)
    session = service.unlock("password", "senha original")
    session.close()


def test_master_key_mismatch_rolls_back(
    service: StartupSecurityService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service.configure_first_run("password", "senha original").close()
    import services.startup_security as security_module

    real_unlock = security_module.unlock_with_credential

    def tampered_unlock(*args: Any, **kwargs: Any) -> bytearray:
        key = real_unlock(*args, **kwargs)
        key[0] ^= 0xFF  # simulate a master-key comparison mismatch
        return key

    monkeypatch.setattr(security_module, "unlock_with_credential", tampered_unlock)
    with pytest.raises(CredentialRotationError):
        service.change_credential("senha original", "password", "senha nova")
    monkeypatch.setattr(security_module, "unlock_with_credential", real_unlock)
    session = service.unlock("password", "senha original")
    session.close()


def test_fault_during_marker_write_rolls_back(
    service: StartupSecurityService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service.configure_first_run("password", "senha original").close()
    import services.startup_security as security_module

    real_marker = security_module.write_security_marker_atomically

    def explode(path: Path, envelope: Any) -> None:
        raise OSError("fault injection during marker write")

    monkeypatch.setattr(security_module, "write_security_marker_atomically", explode)
    with pytest.raises(CredentialRotationError):
        service.change_credential("senha original", "password", "senha nova")
    monkeypatch.setattr(
        security_module, "write_security_marker_atomically", real_marker
    )
    session = service.unlock("password", "senha original")
    session.close()


def test_failed_rollback_is_logged_and_raises(
    service: StartupSecurityService,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    context = service.configure_first_run("password", "senha original")
    context.close()  # rotation writes start at call 1
    import services.backup_crypto as crypto_module
    from services.startup_security import SecurityRecoveryRequiredError

    # Corrupt the candidate envelope write so reopen/verify fails...
    original = crypto_module._atomic_write_private

    def write_corrupt_candidate(path: Path, payload: bytes) -> None:
        payload = b"{corrupt"
        original(path, payload)

    monkeypatch.setattr(crypto_module, "_atomic_write_private", write_corrupt_candidate)

    rollback_invoked = False

    # ...and break the rollback restore as well (patch the class method).
    def broken_rollback(
        self: StartupSecurityService,
        backup_path: Path,
        previous_envelope: Any,
        **kwargs: Any,
    ) -> None:
        nonlocal rollback_invoked
        rollback_invoked = True
        raise SecurityEnvelopeError("rollback fault injection")

    monkeypatch.setattr(StartupSecurityService, "_rollback_rotation", broken_rollback)
    with pytest.raises(SecurityRecoveryRequiredError):
        service.change_credential("senha original", "password", "senha nova")

    assert rollback_invoked is True, "The rollback failure boundary was not reached!"
    rotation_backups = list(service.configuration_dir.glob("*.rotation-backup"))
    assert len(rotation_backups) >= 1, (
        "Recovery backup material must be preserved on rollback failure!"
    )

    monkeypatch.setattr(crypto_module, "_atomic_write_private", original)
    monkeypatch.setattr(
        StartupSecurityService,
        "_rollback_rotation",
        StartupSecurityService._rollback_rotation,
    )


def test_wrong_current_credential_never_touches_disk(
    service: StartupSecurityService,
) -> None:
    context = service.configure_first_run("password", "senha original")
    context.close()
    before = service.envelope_path.read_bytes()
    with pytest.raises(InvalidCredentialError):
        service.change_credential("errada", "password", "senha nova")
    assert service.envelope_path.read_bytes() == before


def test_policy_violation_never_touches_disk(
    service: StartupSecurityService,
) -> None:
    service.configure_first_run("password", "senha original").close()
    before = service.envelope_path.read_bytes()
    with pytest.raises(CredentialPolicyError):
        service.change_credential("senha original", "password", "curta")
    assert service.envelope_path.read_bytes() == before


# ---------------------------------------------------------------------------
# SEC-08: envelope/marker consistency
# ---------------------------------------------------------------------------


def test_marker_binds_envelope_identity(service: StartupSecurityService) -> None:
    context = service.configure_first_run("password", "senha de teste")
    context.close()
    envelope = read_envelope(service.envelope_path)
    marker = read_security_marker(service.marker_path)
    assert marker_matches_envelope(marker, envelope)
    # Any mismatch is detected.
    tampered = dict(marker)
    tampered["profile_id"] = "00000000-0000-0000-0000-000000000000"
    assert not marker_matches_envelope(tampered, envelope)


def test_missing_marker_is_detected(service: StartupSecurityService) -> None:
    context = service.configure_first_run("password", "senha de teste")
    context.close()
    # The canonical bypass attempt: deleting the ENVELOPE while the MARKER
    # survives must always yield MISSING_ENVELOPE (never NEW/first-run).
    service.envelope_path.unlink()
    assert service.inspect_profile() == ProfileSecurityState.MISSING_ENVELOPE
    with pytest.raises(SecurityEnvelopeError):
        service.configure_first_run("password", "tentativa de bypass 123")
    # A missing/malformed/mismatched marker with a live envelope also means
    # the protection state cannot be trusted (never plain CONFIGURED).
    from services.backup_crypto import build_envelope, write_envelope_atomically

    envelope = build_envelope(
        profile_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        default_unlock_method="password",
        master_key=bytearray(range(32)),
        credentials={"password": b"direct write senha"},
    )
    write_envelope_atomically(service.envelope_path, envelope)
    service.marker_path.unlink()
    assert service.inspect_profile() == ProfileSecurityState.CORRUPT_ENVELOPE


def test_malformed_marker_is_detected(service: StartupSecurityService) -> None:
    service.configure_first_run("password", "senha de teste").close()
    service.marker_path.write_text("{broken", encoding="utf-8")
    with pytest.raises(SecurityEnvelopeError):
        service.verify_marker_consistency()


def test_mismatched_marker_is_detected(service: StartupSecurityService) -> None:
    service.configure_first_run("password", "senha de teste").close()
    marker = read_security_marker(service.marker_path)
    marker["envelope_identity"] = "0" * 64
    service.marker_path.write_text(json.dumps(marker), encoding="utf-8")
    with pytest.raises(SecurityEnvelopeError):
        service.verify_marker_consistency()


def test_marker_rewritten_after_rotation_keeps_binding(
    service: StartupSecurityService,
) -> None:
    service.configure_first_run("password", "senha original").close()
    service.change_credential("senha original", "password", "senha nova")
    envelope = read_envelope(service.envelope_path)
    marker = read_security_marker(service.marker_path)
    assert marker_matches_envelope(marker, envelope)


# ---------------------------------------------------------------------------
# SEC-09: session ownership (exact-once zeroization)
# ---------------------------------------------------------------------------


def test_set_active_session_closes_previous_distinct_session(
    service: StartupSecurityService,
) -> None:
    first = service.configure_first_run("password", "senha original")
    first_key = first.backup_master_key
    set_active_session(first)
    second = service.unlock("password", "senha original")
    set_active_session(second)
    assert not any(first_key), "previous session must be zeroized"
    assert get_active_session() is second
    second.close()


def test_set_active_session_same_object_is_idempotent(
    service: StartupSecurityService,
) -> None:
    context = service.configure_first_run("password", "senha original")
    set_active_session(context)
    set_active_session(context)  # same object: must NOT be closed
    assert any(context.backup_master_key)
    assert get_active_session() is context
    clear_active_session()
    assert not any(context.backup_master_key)


def test_clear_active_session_is_exact_once(
    service: StartupSecurityService,
) -> None:
    context = service.configure_first_run("password", "senha original")
    set_active_session(context)
    clear_active_session()
    assert not any(context.backup_master_key)
    assert get_active_session() is None
    # A second clear is a safe no-op.
    clear_active_session()
    assert get_active_session() is None


def test_session_none_replacement_clears_previous(
    service: StartupSecurityService,
) -> None:
    context = service.configure_first_run("password", "senha original")
    set_active_session(context)
    set_active_session(None)
    assert not any(context.backup_master_key)
    assert get_active_session() is None
