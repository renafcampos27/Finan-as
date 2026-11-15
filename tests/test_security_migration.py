"""Migration tests: LGBK1 continuity, LGBK2 adoption, legacy store strip fix."""

from __future__ import annotations

import os

import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from database.engine import FinanceDB
from services.backup import BackupService
from services.backup_crypto import (
    BackupCryptoCodec,
    LGBK1_MAGIC,
    LGBK2_MAGIC,
    read_envelope,
)
from services.startup_security import (
    ProfileSecurityState,
    SessionSecurityContext,
    StartupSecurityService,
    clear_active_session,
    set_active_session,
)


@pytest.fixture(autouse=True)
def _no_leaked_session():
    """Never leak the module-level active session into other tests."""

    clear_active_session()
    yield
    clear_active_session()


class DatabaseStub:
    def __init__(self, path: Path):
        self.conn = sqlite3.connect(path)
        self.path = path

    def backup_to(self, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
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


def create_database(path: Path, marker: str) -> None:
    # A legacy envelope still needs an application database that can reopen.
    with patch.dict(os.environ, {"FINANCAS_PESSOAIS_DEMO_DATA": "0"}):
        database = FinanceDB(path)
    try:
        database.conn.execute(
            "INSERT OR REPLACE INTO settings(key, value) VALUES ('marker', ?)",
            (marker,),
        )
        database.conn.commit()
    finally:
        database.conn.close()


@pytest.fixture()
def backup_env(tmp_path: Path):
    active = tmp_path / "active.sqlite3"
    create_database(active, "original")
    service = BackupService(
        RepoStub(active),
        backup_directory=tmp_path / "backups",
        database_file=active,
        preferences_file=tmp_path / "preferences.json",
        attachments_directory=tmp_path / "anexos",
    )
    security = StartupSecurityService(database_file=active)
    return service, security, tmp_path


def _enable_encryption(backup: BackupService) -> None:
    preferences = backup.preferences()
    preferences["backup_encryption_enabled"] = True
    backup._write_preferences_file(preferences)


# ---------------------------------------------------------------------------
# Legacy LGBK1 continuity
# ---------------------------------------------------------------------------


def test_legacy_lgbk1_backup_still_opens_after_upgrade(
    backup_env,
) -> None:
    backup, _security, tmp_path = backup_env
    legacy_password = "palavra antiga 123"
    # Produce an LGBK1 file through the frozen legacy path.
    source_zip = tmp_path / "payload.zip"
    import zipfile

    with zipfile.ZipFile(source_zip, "w") as archive:
        archive.writestr("settings.txt", "legacy content")
    legacy_file = backup.backup_directory / "legacy.lgbak"
    backup.encrypt_file(source_zip, legacy_file, legacy_password, payload_kind="Z")
    assert legacy_file.read_bytes()[:5] == LGBK1_MAGIC
    # The codec opens it with the original password.
    out = tmp_path / "legacy_out.zip"
    kind = BackupCryptoCodec.decrypt(
        legacy_file, out, credential=legacy_password.encode()
    )
    assert kind == "Z"
    # And the service-level fallback still resolves it.
    out2 = tmp_path / "legacy_out2.zip"
    kind2 = backup._decrypt_with_available_passwords(
        legacy_file,
        out2,
        legacy_password,
    )
    assert kind2 == "Z"


def test_legacy_stored_passwords_are_never_stripped() -> None:
    from services.secret_store import MemorySecretStore

    store = MemorySecretStore()
    secret_with_spaces = "  senha com espaços  "
    store.write(secret_with_spaces)
    try:
        assert store.read() == secret_with_spaces
    finally:
        store.delete()


# ---------------------------------------------------------------------------
# LGBK2 adoption through an active security session
# ---------------------------------------------------------------------------


def test_new_backups_use_lgbk2_when_session_is_unlocked(
    backup_env,
) -> None:
    backup, security, tmp_path = backup_env
    context: SessionSecurityContext = security.configure_first_run(
        "password", "senha nova forte"
    )
    try:
        set_active_session(context)
        _enable_encryption(backup)
        target = backup.create_manual_backup()
        assert target.is_file()
        assert BackupCryptoCodec.detect_magic(target) == LGBK2_MAGIC
        header = BackupCryptoCodec.inspect_header(target)
        assert header.profile_id == context.profile_id
        # Validation via the session key succeeds (returns the inner
        # complete-archive report after decrypting the LGBK2 payload).
        report = backup.validate_backup_file(target)
        assert isinstance(report, dict)
        assert report.get("kind") != "S"
    finally:
        set_active_session(None)
        context.close()


def test_backup_without_session_falls_back_to_lgbk1(
    backup_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backup, security, tmp_path = backup_env
    security.configure_first_run("password", "senha nova forte").close()
    _enable_encryption(backup)
    # No active session: the legacy LGBK1 path requires a stored password.
    monkeypatch.setattr(backup, "stored_backup_password", lambda: "senha legado 123")
    target = backup.create_manual_backup()
    assert target.is_file()
    assert BackupCryptoCodec.detect_magic(target) == LGBK1_MAGIC


def test_lgbk2_backup_restores_with_session(backup_env) -> None:
    backup, security, tmp_path = backup_env
    context = security.configure_first_run("password", "senha nova forte")
    try:
        set_active_session(context)
        _enable_encryption(backup)
        target = backup.create_manual_backup()
        assert BackupCryptoCodec.detect_magic(target) == LGBK2_MAGIC
        # Restore it back over the active database.
        safety = backup.restore_backup(target)
        assert safety.is_file()
        connection = sqlite3.connect(backup.database_file)
        try:
            row = connection.execute(
                "SELECT value FROM settings WHERE key='marker'"
            ).fetchone()
        finally:
            connection.close()
        assert row is not None
    finally:
        set_active_session(None)
        context.close()


def test_lgbk2_backup_restores_with_credential_without_session(
    backup_env,
) -> None:
    backup, security, tmp_path = backup_env
    context = security.configure_first_run("password", "senha nova forte")
    try:
        set_active_session(context)
        _enable_encryption(backup)
        target = backup.create_manual_backup()
    finally:
        set_active_session(None)
        context.close()
    # Session is gone; the embedded header wraps make the file portable.
    restored = backup.restore_backup(target, password="senha nova forte")
    assert restored.is_file()


def test_profile_state_progression(tmp_path: Path) -> None:
    security = StartupSecurityService(database_file=tmp_path / "db.sqlite3")
    assert security.inspect_profile() == ProfileSecurityState.NEW
    context = security.configure_first_run("password", "senha de teste")
    try:
        assert security.inspect_profile() == ProfileSecurityState.CONFIGURED
        envelope = read_envelope(security.envelope_path)
        assert envelope.default_unlock_method == "password"
        assert set(envelope.unlocks) == {"password"}
    finally:
        context.close()
    # Rotation to PIN keeps the same profile identity and adds the slot.
    security.change_credential("senha de teste", "pin", "1234567890")
    envelope = read_envelope(security.envelope_path)
    assert envelope.default_unlock_method == "password"
    assert set(envelope.unlocks) == {"password", "pin"}


def test_shutdown_zeroizes_active_session(backup_env) -> None:
    from services.startup_security import clear_active_session, get_active_session

    backup, security, tmp_path = backup_env
    context = security.configure_first_run("password", "senha nova forte")
    set_active_session(context)
    assert get_active_session() is context
    assert any(context.backup_master_key)
    clear_active_session()
    assert get_active_session() is None
    assert not any(context.backup_master_key)
