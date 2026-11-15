from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["FINANCAS_PESSOAIS_TESTING"] = "1"

import pytest

from database.protection import DataProtectionGuard
from services.backup_crypto import (
    BackupCryptoCodec,
    BackupCryptoError,
    CredentialKind,
    CryptoFormatError,
    MAX_ENVELOPE_BYTES,
    MAX_SECURITY_MARKER_BYTES,
    build_envelope,
    read_envelope,
    read_security_marker,
)


# ============================================================================
# E19 (C45): Strict master-key length, slot kind contract & buffer zeroization
# ============================================================================

def test_e19_build_envelope_enforces_key_length_and_slot_kinds():
    # 1. Invalid key lengths must raise BackupCryptoError before any KDF work
    with pytest.raises(BackupCryptoError, match="32 bytes"):
        build_envelope(
            profile_id="test-p",
            default_unlock_method=str(CredentialKind.PASSWORD),
            master_key=bytearray(16),  # too short
            credentials={str(CredentialKind.PASSWORD): b"SecretPass123!"},
        )

    with pytest.raises(BackupCryptoError, match="32 bytes"):
        build_envelope(
            profile_id="test-p",
            default_unlock_method=str(CredentialKind.PASSWORD),
            master_key=bytearray(64),  # too long
            credentials={str(CredentialKind.PASSWORD): b"SecretPass123!"},
        )

    # 2. Unsupported slot kind rejected
    with pytest.raises(BackupCryptoError, match="Tipo de credencial inválido"):
        build_envelope(
            profile_id="test-p",
            default_unlock_method="biometric",
            master_key=bytearray(32),
            credentials={"biometric": b"SecretPass123!"},
        )

    # 3. Valid 32-byte key and allowed kinds succeed
    envelope = build_envelope(
        profile_id="test-p",
        default_unlock_method=str(CredentialKind.PASSWORD),
        master_key=bytearray(os.urandom(32)),
        credentials={
            str(CredentialKind.PASSWORD): b"SecretPass123!",
            str(CredentialKind.PIN): b"9999",
        },
    )
    assert envelope.profile_id == "test-p"
    assert set(envelope.unlocks.keys()) == {"password", "pin"}


def test_e19_encrypt_v2_and_decrypt_validate_master_key_contracts(tmp_path: Path):
    source_zip = tmp_path / "source.zip"
    source_zip.write_bytes(bytes([0x50, 0x4B, 0x05, 0x06]) + bytes(18))
    target_lgbk2 = tmp_path / "backup.lgbk2"

    master_key = bytearray(os.urandom(32))
    envelope = build_envelope(
        profile_id="test-p",
        default_unlock_method=str(CredentialKind.PASSWORD),
        master_key=master_key,
        credentials={str(CredentialKind.PASSWORD): b"SecretPass123!"},
    )

    # 1. encrypt_v2 rejects invalid master_key length
    with pytest.raises(BackupCryptoError, match="32 bytes"):
        BackupCryptoCodec.encrypt_v2(
            source_zip,
            target_lgbk2,
            master_key=bytes(16),
            envelope=envelope,
            payload_kind="Z",
        )

    # 2. encrypt_v2 succeeds with 32-byte key
    BackupCryptoCodec.encrypt_v2(
        source_zip,
        target_lgbk2,
        master_key=bytes(master_key),
        envelope=envelope,
        payload_kind="Z",
    )
    assert target_lgbk2.exists()

    # 3. decrypt rejects unsupported credential_kind
    out_zip = tmp_path / "out.zip"
    with pytest.raises(BackupCryptoError, match="Tipo de credencial inválido"):
        BackupCryptoCodec.decrypt(
            target_lgbk2,
            out_zip,
            credential=b"SecretPass123!",
            credential_kind="face_id",
        )

    # 4. decrypt rejects malformed session_master_key length with CryptoFormatError
    with pytest.raises(CryptoFormatError, match="32 bytes"):
        BackupCryptoCodec.decrypt(
            target_lgbk2,
            out_zip,
            session_master_key=b"too-short-key",
        )

    # 5. decrypt_v2 rejects malformed key with CryptoFormatError
    with pytest.raises(CryptoFormatError, match="32 bytes"):
        BackupCryptoCodec.decrypt_v2(
            target_lgbk2,
            out_zip,
            master_key=b"1234567890",
        )


# ============================================================================
# E20 (C46): Restore cleanup in finally does not mask primary failure
# ============================================================================

def test_e20_install_staged_restore_cleanup_does_not_mask_primary_failure(tmp_path: Path):
    db_file = tmp_path / "test.db"
    with closing(sqlite3.connect(db_file)) as connection:
        connection.execute("CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT)")
        connection.execute("INSERT INTO settings VALUES('marker','live')")
        connection.commit()
    pref_file = tmp_path / "pref.json"
    pref_file.write_text("{}", encoding="utf-8")

    staged_dir = tmp_path / "staged"
    staged_dir.mkdir()
    staged_db = staged_dir / "database.sqlite3"
    with closing(sqlite3.connect(staged_db)) as connection:
        connection.execute("CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT)")
        connection.execute("INSERT INTO settings VALUES('marker','staged')")
        connection.commit()

    guard = DataProtectionGuard(
        database_file=db_file,
        preferences_file=pref_file,
        protection_directory=tmp_path / "backups",
    )

    staged_attachments = staged_dir / "anexos"
    staged_attachments.mkdir()
    staged_history = staged_dir / "history"
    staged_history.mkdir()

    # Inject a primary failure during file replacement, AND make candidate unlinking raise PermissionError
    orig_replace = Path.replace
    orig_unlink = Path.unlink
    cleanup_failures = []

    def _failing_replace(self, target):
        if str(target) == str(db_file) and self.name.endswith(".restore"):
            raise OSError("Primary replace I/O failure on live database")
        return orig_replace(self, target)

    def _selective_unlink(self, *args, **kwargs):
        if self.name.startswith(f".{db_file.name}.") and self.name.endswith(".restore"):
            cleanup_failures.append(self)
            raise PermissionError("Simulated locked candidate file in cleanup")
        return orig_unlink(self, *args, **kwargs)

    with (
        patch.object(Path, "replace", _failing_replace),
        patch.object(Path, "unlink", _selective_unlink),
    ):
        # The primary exception must be preserved, NOT masked by PermissionError
        with pytest.raises(Exception) as exc_info:
            guard._install_staged_restore(
                staged_database=staged_db,
                staged_attachments=staged_attachments,
                staged_history=staged_history,
                staged_preferences=None,
                preferences_present=False,
            )

        # Confirm the raised error preserves the primary failure
        err_msg = str(exc_info.value)
        assert "Primary replace I/O failure" in err_msg or "Falha no restauro" in err_msg
        assert not isinstance(exc_info.value, PermissionError)
        assert cleanup_failures, "The secondary cleanup fault was not exercised"
    with closing(sqlite3.connect(db_file)) as connection:
        assert connection.execute("SELECT value FROM settings WHERE key='marker'").fetchone()[0] == "live"


# ============================================================================
# E21 (C47): Security envelope and marker reads bounded by max file sizes
# ============================================================================

def test_e21_security_marker_size_limits(tmp_path: Path):
    marker_file = tmp_path / "security_marker.json"

    # 1. Normal marker loads cleanly
    valid_marker_json = (
        '{"format_version": 1, "profile_id": "test-p", "default_unlock_method": "password", '
        '"envelope_identity": "abc"}'
    )
    marker_file.write_text(valid_marker_json, encoding="utf-8")
    loaded = read_security_marker(marker_file)
    assert loaded["profile_id"] == "test-p"

    # 2. Oversized marker rejected with CryptoFormatError before large allocation
    oversized_data = " " * (MAX_SECURITY_MARKER_BYTES + 100)
    marker_file.write_text(oversized_data, encoding="utf-8")
    with pytest.raises(CryptoFormatError, match="excede o limite máximo permitido"):
        read_security_marker(marker_file)


def test_e21_security_envelope_size_limits(tmp_path: Path):
    envelope_file = tmp_path / "security_envelope.json"

    # 1. Normal envelope loads cleanly
    envelope = build_envelope(
        profile_id="test-p",
        default_unlock_method=str(CredentialKind.PASSWORD),
        master_key=bytearray(os.urandom(32)),
        credentials={str(CredentialKind.PASSWORD): b"SecretPass123!"},
    )
    from services.backup_crypto import write_envelope_atomically
    write_envelope_atomically(envelope_file, envelope)

    loaded = read_envelope(envelope_file)
    assert loaded.profile_id == "test-p"

    # 2. Oversized envelope rejected with CryptoFormatError
    oversized_data = " " * (MAX_ENVELOPE_BYTES + 500)
    envelope_file.write_text(oversized_data, encoding="utf-8")
    with pytest.raises(CryptoFormatError, match="excede o limite máximo permitido"):
        read_envelope(envelope_file)
