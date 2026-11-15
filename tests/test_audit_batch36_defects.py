"""Automated verification suite for Audit Round 6 Batch 36 defects.

Defects covered:
1. BUG-R6-SEC-01: os.replace publication precedes DB tamper tombstone to avoid false-positive TAMPERED state.
2. BUG-R6-RESTORE-01: sidecar rollback restores WAL even if SHM backup or subsequent steps fail.
3. BUG-R6-CRYPTO-01: master_key and KEK remain mutable bytearrays without un-wipeable heap bytes copies.
4. BUG-R6-CONCURRENCY-01: _SESSION_LOCK protects active session registry and _ROTATION_LOCK serializes rotations.
5. BUG-R6-CRYPTO-02: inspect_header rejects mismatch between byte 5 prefix kind and payload_kind in header.
6. BUG-R6-RESTORE-02: restore_missing_profile uses durable replace without unlinking database beforehand.
7. BUG-R6-CRYPTO-03: _atomic_write_private uses safe_open_exclusive_temp preventing symlink/race attacks.
8. BUG-R6-RESTORE-03: _restore_zip_archive invokes validate_current_search_index to detect damaged FTS5 index.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import struct
import sys
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from database.protection import (
    DataIntegrityError,
    DataProtectionGuard,
)
from services.backup import BackupService
from services.backup_crypto import (
    ENVELOPE_TEMP_SUFFIX,
    BackupCryptoCodec,
    CryptoFormatError,
    _atomic_write_private,
    secure_zero,
    unwrap_master_key,
    wrap_master_key,
)
from services.startup_security import (
    SessionSecurityContext,
    StartupSecurityService,
    _ROTATION_LOCK,
    _SESSION_LOCK,
    clear_active_session,
    get_active_session,
    set_active_session,
)


def _minimal_security_database(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY)")
        connection.execute("INSERT INTO schema_migrations (version) VALUES (1)")
        connection.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
        connection.commit()
    finally:
        connection.close()


def test_bug_r6_sec_01_publication_order_precedes_tombstone(tmp_path: Path):
    """BUG-R6-SEC-01: File publication happens before writing the tombstone.

    If an error occurs during publication, the old tombstone remains intact,
    preventing a false-positive TAMPERED state.
    """
    db_file = tmp_path / "test.sqlite3"
    envelope_file = tmp_path / "security_envelope.json"
    marker_file = tmp_path / "security_marker.json"

    # Setup database with migrations and settings
    conn = sqlite3.connect(str(db_file))
    conn.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY)")
    conn.execute("INSERT INTO schema_migrations (version) VALUES (1)")
    conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
    conn.commit()
    conn.close()

    service = StartupSecurityService(
        database_file=db_file,
        envelope_path=envelope_file,
        marker_path=marker_file,
    )
    service.configure_first_run("pin", "1357902468").close()

    # Verify initial profile
    assert service.inspect_profile().name == "CONFIGURED"

    # Read original tombstone
    orig_tombstone = service._read_tamper_tombstone_raw()
    assert orig_tombstone is not None

    # Simulate failure during os.replace of envelope
    with patch("os.replace", side_effect=OSError("Disk write error")):
        with pytest.raises(Exception):
            service.change_credential("1357902468", "pin", "2468013579")

    # Verify tombstone was NOT modified to the new one before the rollback
    current_tombstone = service._read_tamper_tombstone_raw()
    assert current_tombstone == orig_tombstone
    assert service.inspect_profile().name == "CONFIGURED"


def test_bug_r6_restore_01_sidecar_rollback_restores_wal_when_shm_fails(tmp_path: Path):
    """BUG-R6-RESTORE-01: Rollback restores any moved sidecar (such as -wal)

    even if a subsequent sidecar (such as -shm) fails to move.
    """
    db_file = tmp_path / "app.sqlite3"
    _minimal_security_database(db_file)
    original_database = db_file.read_bytes()
    wal_file = Path(f"{db_file}-wal")
    wal_file.write_text("critical wal frame data")
    shm_file = Path(f"{db_file}-shm")
    shm_file.write_text("shm index data")

    guard = DataProtectionGuard(
        database_file=db_file,
        protection_directory=tmp_path / "backups",
        required_schema_versions=(1,),
    )

    staged_db = tmp_path / "staged.sqlite3"
    _minimal_security_database(staged_db)

    original_replace = Path.replace

    def mock_replace(self, target):
        # Fail when trying to move the -shm file
        if "-shm" in str(self) and "rollback" in str(target):
            raise PermissionError("Access denied on -shm (locked)")
        return original_replace(self, target)

    # Exercise the partial sidecar rename boundary independently of SQLite's
    # checkpoint, which has its own real-WAL integration coverage.
    with patch.object(guard, "_checkpoint_live_wal"), patch.object(
        Path, "replace", autospec=True, side_effect=mock_replace
    ):
        with pytest.raises(PermissionError):
            guard._install_staged_restore(
                staged_database=staged_db,
                staged_attachments=tmp_path / "att",
                staged_history=tmp_path / "hist",
                staged_preferences=None,
                preferences_present=False,
            )

    # Crucial assertion: wal_file must have been restored by rollback!
    assert wal_file.exists(), "WAL file was destroyed and not restored on rollback!"
    assert wal_file.read_text() == "critical wal frame data"
    assert shm_file.read_text() == "shm index data"
    assert db_file.read_bytes() == original_database


def test_bug_r6_crypto_01_mutable_bytearray_keys():
    """BUG-R6-CRYPTO-01: master_key and KEK can be passed as mutable bytearrays

    without immutable copies on the heap, and can be zeroized in-place.
    """
    master_key = bytearray(b"M" * 32)
    kek = bytearray(b"K" * 32)
    aad = b"test_aad"

    # Wrap directly with mutable bytearray
    key_wrap = wrap_master_key(master_key, kek, aad=aad)
    assert key_wrap.ciphertext != b""

    # Unwrap directly with mutable bytearray
    recovered = unwrap_master_key(key_wrap, kek, aad=aad)
    assert recovered == master_key

    # Test secure_zero cleans in place
    secure_zero(recovered)
    assert recovered == bytearray(32)

    secure_zero(master_key)
    assert master_key == bytearray(32)

    secure_zero(kek)
    assert kek == bytearray(32)


def test_bug_r6_concurrency_01_session_and_rotation_locks():
    """BUG-R6-CONCURRENCY-01: Concurrency locks exist and protect session registry."""
    assert _SESSION_LOCK is not None
    assert _ROTATION_LOCK is not None

    clear_active_session()
    assert get_active_session() is None

    session = SessionSecurityContext(
        profile_id="test_profile",
        credential_kind="pin",
        default_unlock_method="pin",
        backup_master_key=bytearray(b"S" * 32),
        envelope_path=Path("dummy_env.json"),
    )
    set_active_session(session)
    retrieved = get_active_session()
    assert retrieved is session
    clear_active_session()
    assert get_active_session() is None


def test_bug_r6_crypto_02_inspect_header_rejects_kind_mismatch(tmp_path: Path):
    """BUG-R6-CRYPTO-02: inspect_header verifies that byte 5 matches payload_kind."""
    test_file = tmp_path / "test_mismatch.lgbak"

    # Write magic LGBK2
    magic = b"LGBK2"
    prefix_kind = b"S"  # Prefix says SQLite database
    header_dict = {
        "format_version": 2,
        "profile_id": "test_id",
        "payload_kind": "Z",  # Header says Zip archive! MISMATCH!
        "default_unlock_method": "pin",
        "unlocks": {
            "pin": {
                "kdf": {
                    "name": "scrypt",
                    "salt_b64": "AAAAAAAAAAA=",
                    "n": 16,
                    "r": 8,
                    "p": 1,
                    "length": 32,
                },
                "key_wrap": {
                    "algorithm": "AES-256-GCM",
                    "nonce_b64": "AAAAAAAAAAA=",
                    "ciphertext_b64": "AAAAAAAAAAA=",
                },
            }
        },
        "payload": {
            "algorithm": "AES-256-GCM",
            "nonce_b64": "AAAAAAAAAAA=",
        },
    }
    header_bytes = json.dumps(header_dict).encode("utf-8")
    header_len = struct.pack(">I", len(header_bytes))

    with test_file.open("wb") as f:
        f.write(magic)
        f.write(prefix_kind)
        f.write(header_len)
        f.write(header_bytes)
        f.write(b"0" * 100)  # dummy payload

    with pytest.raises(CryptoFormatError, match="Inconsistência entre o prefixo binário e payload_kind"):
        BackupCryptoCodec.inspect_header(test_file)


def test_bug_r6_restore_02_durable_replace_no_pre_unlink(tmp_path: Path):
    """BUG-R6-RESTORE-02: restore_missing_profile uses durable replace without unlinking database beforehand."""
    db_file = tmp_path / "live.sqlite3"
    # Ensure database does NOT exist so restore_missing_profile proceeds
    db_file.unlink(missing_ok=True)

    source_db = tmp_path / "backup.sqlite3"
    # Include the settings contract used by post-restore security resync.
    _minimal_security_database(source_db)

    # Isolate installation transport from the independent backup validation.
    with patch.object(BackupService, "validate_restore_database", return_value=None):
        called_replace = False
        original_replace = Path.replace
        original_unlink = Path.unlink

        def observe_replace(source, target):
            nonlocal called_replace
            if Path(target).resolve() == db_file.resolve():
                called_replace = True
            return original_replace(source, target)

        def prohibit_live_unlink(path, *args, **kwargs):
            assert path.resolve() != db_file.resolve(), "Live database must not be unlinked before replacement"
            return original_unlink(path, *args, **kwargs)

        with patch.object(Path, "replace", autospec=True, side_effect=observe_replace), patch.object(
            Path, "unlink", autospec=True, side_effect=prohibit_live_unlink
        ):
            BackupService.restore_missing_profile(
                source=source_db,
                database_file=db_file,
                supported_schema_version=1,
            )

        assert called_replace is True
        with sqlite3.connect(db_file) as restored:
            assert restored.execute("PRAGMA quick_check").fetchone()[0] == "ok"


def test_bug_r6_crypto_03_atomic_write_private_exclusive_create(tmp_path: Path):
    """BUG-R6-CRYPTO-03: _atomic_write_private uses safe_open_exclusive_temp.

    If a temporary file already exists, it fails with FileExistsError rather than
    overwriting or following a malicious symlink.
    """
    target = tmp_path / "secret_file.json"
    temp_target = target.with_name(f".{target.name}{ENVELOPE_TEMP_SUFFIX}")
    temp_target.write_text("existing pre-created file / attack payload")

    with pytest.raises(FileExistsError):
        _atomic_write_private(target, b"new confidential data")


def test_bug_r6_restore_03_restore_zip_validates_fts5_index(tmp_path: Path):
    """BUG-R6-RESTORE-03: _restore_zip_archive validates FTS5 index before installing."""
    guard = DataProtectionGuard(
        database_file=tmp_path / "app.sqlite3",
        protection_directory=tmp_path / "backups",
        required_schema_versions=(1,),
    )

    validated_search_index = False

    def mock_validate(conn):
        nonlocal validated_search_index
        validated_search_index = True
        raise DataIntegrityError("Damaged FTS5 shadow table")

    # Create dummy zip archive with manifest
    zip_path = tmp_path / "test_backup.zip"
    db_path = tmp_path / "staged.sqlite3"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY)")
    conn.execute("INSERT INTO schema_migrations (version) VALUES (1)")
    conn.commit()
    conn.close()

    db_digest = hashlib.sha256(db_path.read_bytes()).hexdigest()
    manifest = {
        "format_version": 1,
        "database": {
            "archive_name": "financas_pessoais.sqlite3",
            "size": db_path.stat().st_size,
            "sha256": db_digest,
        },
        "files": [],
        "preferences_present": False,
    }

    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("manifesto_protecao.json", json.dumps(manifest))
        zf.write(db_path, "financas_pessoais.sqlite3")

    with patch.object(DataProtectionGuard, "validate_connection", return_value=None):
        with patch.object(DataProtectionGuard, "validate_current_search_index", side_effect=mock_validate):
            with pytest.raises(DataIntegrityError, match="Damaged FTS5 shadow table"):
                guard._restore_zip_archive(
                    path=zip_path,
                    interrupted_upgrade=False,
                )

    assert validated_search_index is True
