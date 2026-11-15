"""Regression tests for audit issues C1, C2, C3, and C4.

C1: Recoverable credential rotation with multi-record rollback and retained recovery copies on failure.
C2: Route encrypted export through LGBK2 with active session, preserve ADR-009 legacy reading, reject wrong explicit passwords.
C3: Preserve existing backup destinations byte-for-byte on failure across backup and export APIs.
C4: Protection inspection distinguishes confirmed absence, confirmed protected, and indeterminate states (RECOVERY_REQUIRED).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from database.engine import FinanceDB
from database.repo import Repo
from services.backup import BackupService
from services.backup_crypto import (
    LGBK2_MAGIC,
    BackupCryptoCodec,
    BackupCryptoError,
    read_security_marker,
)
from services.startup_security import (
    CredentialRotationError,
    ProfileSecurityState,
    SecurityRecoveryRequiredError,
    StartupSecurityService,
    clear_active_session,
    set_active_session,
)


@pytest.fixture(autouse=True)
def _cleanup_session():
    clear_active_session()
    yield
    clear_active_session()


# ===========================================================================
# C1: Recoverable credential rotation
# ===========================================================================


def test_c1_fault_after_file_replacement_rolls_back_completely(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_file = tmp_path / "app.sqlite3"
    conn = sqlite3.connect(db_file)
    conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
    conn.commit()
    conn.close()

    service = StartupSecurityService(database_file=db_file)
    ctx = service.configure_first_run("password", "old_password_1234")
    ctx.close()

    assert service.inspect_profile() == ProfileSecurityState.CONFIGURED
    prior_envelope = service.load_envelope()
    prior_marker = read_security_marker(service.marker_path)

    # Inject failure during reopen/validation after the live file has been replaced
    import services.startup_security as sec_module

    real_read = sec_module.read_envelope
    reads = 0

    def failing_read(p: Path) -> Any:
        nonlocal reads
        reads += 1
        if reads == 2:  # First read is load_envelope, second is reopen after write
            raise OSError("Injected disk failure during reopen")
        return real_read(p)

    monkeypatch.setattr(sec_module, "read_envelope", failing_read)

    with pytest.raises(CredentialRotationError):
        service.change_credential("old_password_1234", "password", "new_password_5678")

    monkeypatch.undo()

    # Verify complete rollback
    assert service.inspect_profile() == ProfileSecurityState.CONFIGURED
    restored_envelope = service.load_envelope()
    assert restored_envelope.to_json_dict() == prior_envelope.to_json_dict()
    assert read_security_marker(service.marker_path) == prior_marker

    # Old credential remains usable
    unlocked = service.unlock("password", "old_password_1234")
    assert unlocked is not None
    unlocked.close()

    # Recovery copies cleaned up after successful rollback
    assert not list(service.configuration_dir.glob("*.rotation-backup"))


def test_c1_fault_after_tombstone_update_rolls_back_tombstone_and_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_file = tmp_path / "app.sqlite3"
    conn = sqlite3.connect(db_file)
    conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
    conn.commit()
    conn.close()

    service = StartupSecurityService(database_file=db_file)
    ctx = service.configure_first_run("password", "old_password_1234")
    ctx.close()

    prior_tombstone = service._read_tamper_tombstone_raw()
    assert prior_tombstone is not None

    # Inject failure during session unlock after tombstone write.
    # BUG-AUD-C120: step 5 now reopens the candidate through a fresh
    # ``session_probe`` instance bound to the transition directory, so an
    # instance-level patch on ``service`` is dead.  Patch the class-level
    # ``unlock`` for the duration of the rotation instead; the final
    # post-rollback unlock below runs after ``monkeypatch.undo()``.
    def explode_unlock(self: StartupSecurityService, *args: Any, **kwargs: Any) -> Any:
        raise OSError("Injected failure during post-tombstone session unlock")

    monkeypatch.setattr(StartupSecurityService, "unlock", explode_unlock)

    with pytest.raises(CredentialRotationError):
        service.change_credential("old_password_1234", "password", "new_password_5678")

    monkeypatch.undo()

    # Tombstone and envelope restored
    assert service._read_tamper_tombstone_raw() == prior_tombstone
    assert service.inspect_profile() == ProfileSecurityState.CONFIGURED
    unlocked = service.unlock("password", "old_password_1234")
    unlocked.close()


def test_c1_failed_rollback_retains_recovery_material_and_raises_recovery_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_file = tmp_path / "app.sqlite3"
    conn = sqlite3.connect(db_file)
    conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
    conn.commit()
    conn.close()

    service = StartupSecurityService(database_file=db_file)
    ctx = service.configure_first_run("password", "old_password_1234")
    ctx.close()

    import services.backup_crypto as crypto_module

    original_atomic = crypto_module._atomic_write_private

    def corrupt_write(path: Path, payload: bytes) -> None:
        original_atomic(path, b"{corrupt-json")

    monkeypatch.setattr(crypto_module, "_atomic_write_private", corrupt_write)

    rollback_invoked = False

    def failing_rollback(self: StartupSecurityService, *args: Any, **kwargs: Any) -> None:
        nonlocal rollback_invoked
        rollback_invoked = True
        raise OSError("Injected disk failure during rollback restoration")

    monkeypatch.setattr(StartupSecurityService, "_rollback_rotation", failing_rollback)

    with pytest.raises(SecurityRecoveryRequiredError):
        service.change_credential("old_password_1234", "password", "new_password_5678")

    assert rollback_invoked is True
    # Recovery material MUST be retained on disk!
    backups = list(service.configuration_dir.glob("*.rotation-backup"))
    assert len(backups) >= 1, "Recovery backup must not be deleted when rollback fails!"


# ===========================================================================
# C2: Route encrypted export through LGBK2
# ===========================================================================


def test_c2_pin_only_profile_exports_valid_lgbk2(tmp_path: Path) -> None:
    db_file = tmp_path / "app.sqlite3"
    repo = Repo(FinanceDB(str(db_file)))
    backup_service = BackupService(repo, backup_directory=tmp_path / "backups")
    security_service = StartupSecurityService(database_file=db_file)

    session = security_service.configure_first_run("pin", "12345678", weak_pin_confirmed=True)
    set_active_session(session)

    # Ensure no legacy password is configured
    assert not backup_service.stored_backup_password()

    export_path = tmp_path / "exported.lgbak"
    backup_service.export_encrypted_backup(export_path)

    assert export_path.is_file()
    assert export_path.read_bytes()[:5] == LGBK2_MAGIC

    header = BackupCryptoCodec.inspect_header(export_path)
    assert header.format_version == 2
    assert header.payload_kind == "Z"

    # Validates cleanly via backup service
    meta = backup_service.validate_backup_file(export_path)
    assert "database" in meta


def test_c2_retained_legacy_secret_does_not_force_lgbk1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_file = tmp_path / "app.sqlite3"
    repo = Repo(FinanceDB(str(db_file)))
    backup_service = BackupService(repo, backup_directory=tmp_path / "backups")
    security_service = StartupSecurityService(database_file=db_file)

    session = security_service.configure_first_run("password", "strong_session_password_1")
    set_active_session(session)

    # Retain a legacy backup password in preferences / secret store
    monkeypatch.setattr(backup_service, "stored_backup_password", lambda: "legacy_retained_password_999")

    export_path = tmp_path / "exported.lgbak"
    backup_service.export_encrypted_backup(export_path)

    # Output MUST be LGBK2, not legacy LGBK1
    assert export_path.read_bytes()[:5] == LGBK2_MAGIC
    header = BackupCryptoCodec.inspect_header(export_path)
    assert header.format_version == 2
    assert header.payload_kind == "Z"
    meta = backup_service.validate_backup_file(export_path)
    assert "database" in meta


def test_c2_export_rejects_mismatched_explicit_password(tmp_path: Path) -> None:
    db_file = tmp_path / "app.sqlite3"
    repo = Repo(FinanceDB(str(db_file)))
    backup_service = BackupService(repo, backup_directory=tmp_path / "backups")
    security_service = StartupSecurityService(database_file=db_file)

    session = security_service.configure_first_run("password", "correct_password_1")
    set_active_session(session)

    export_path = tmp_path / "exported.lgbak"
    with pytest.raises(BackupCryptoError, match="não corresponde à credencial"):
        backup_service.export_encrypted_backup(export_path, password="wrong_explicit_pwd")

    assert not export_path.exists()


# ===========================================================================
# C3: Preserve existing backup destinations on failure
# ===========================================================================


def test_c3_destination_preserved_byte_identical_on_payload_generation_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_file = tmp_path / "app.sqlite3"
    repo = Repo(FinanceDB(str(db_file)))
    backup_service = BackupService(repo, backup_directory=tmp_path / "backups")
    security_service = StartupSecurityService(database_file=db_file)

    session = security_service.configure_first_run("password", "session_pass_1234")
    set_active_session(session)

    target = tmp_path / "preexisting_backup.lgbak"
    sentinel_content = b"DO_NOT_DELETE_OR_CORRUPT_EXISTING_BACKUP_BYTES_12345"
    target.write_bytes(sentinel_content)

    def explode_zip(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("Injected payload creation failure")

    monkeypatch.setattr(backup_service, "_create_complete_zip", explode_zip)

    with pytest.raises(Exception):
        backup_service.export_encrypted_backup(target)

    assert target.is_file()
    assert target.read_bytes() == sentinel_content, "Pre-existing file was modified or deleted!"


def test_c3_destination_preserved_byte_identical_on_encryption_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_file = tmp_path / "app.sqlite3"
    repo = Repo(FinanceDB(str(db_file)))
    backup_service = BackupService(repo, backup_directory=tmp_path / "backups")
    security_service = StartupSecurityService(database_file=db_file)

    session = security_service.configure_first_run("password", "session_pass_1234")
    set_active_session(session)

    target = tmp_path / "preexisting_backup.lgbak"
    sentinel_content = b"DO_NOT_DELETE_OR_CORRUPT_ENCRYPT_SENTINEL_BYTES"
    target.write_bytes(sentinel_content)

    def explode_encrypt(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("Injected encryption failure")

    monkeypatch.setattr(BackupCryptoCodec, "encrypt_v2", explode_encrypt)

    with pytest.raises(Exception):
        backup_service.export_encrypted_backup(target)

    assert target.is_file()
    assert target.read_bytes() == sentinel_content, "Target file was deleted or corrupted on encryption failure!"


def test_c3_destination_preserved_byte_identical_on_validation_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_file = tmp_path / "app.sqlite3"
    repo = Repo(FinanceDB(str(db_file)))
    backup_service = BackupService(repo, backup_directory=tmp_path / "backups")
    security_service = StartupSecurityService(database_file=db_file)

    session = security_service.configure_first_run("password", "session_pass_1234")
    set_active_session(session)

    target = tmp_path / "preexisting_backup.lgbak"
    sentinel_content = b"DO_NOT_DELETE_OR_CORRUPT_VALIDATION_SENTINEL"
    target.write_bytes(sentinel_content)

    def explode_validate(*args: Any, **kwargs: Any) -> Any:
        raise ValueError("Injected validation failure")

    monkeypatch.setattr(backup_service, "validate_backup_file", explode_validate)

    with pytest.raises(Exception):
        backup_service.export_encrypted_backup(target)

    assert target.is_file()
    assert target.read_bytes() == sentinel_content, "Target file was deleted or corrupted on validation failure!"


def test_c3_successful_backup_publishes_complete_archive(tmp_path: Path) -> None:
    db_file = tmp_path / "app.sqlite3"
    repo = Repo(FinanceDB(str(db_file)))
    backup_service = BackupService(repo, backup_directory=tmp_path / "backups")
    security_service = StartupSecurityService(database_file=db_file)

    session = security_service.configure_first_run("password", "session_pass_1234")
    set_active_session(session)

    target = tmp_path / "backup_out.lgbak"
    target.write_bytes(b"OLD_CONTENT")

    published = backup_service.create_lgbk2_complete_backup(target, purpose="test_publish")
    assert published == target
    assert target.is_file()
    assert target.read_bytes()[:5] == LGBK2_MAGIC


# ===========================================================================
# C4: Protection inspection & legacy identification
# ===========================================================================


def test_c4_corrupt_database_reports_recovery_required(tmp_path: Path) -> None:
    db_file = tmp_path / "app.sqlite3"
    db_file.write_bytes(b"NOT_A_VALID_SQLITE_DATABASE_FILE_HEADER")

    service = StartupSecurityService(database_file=db_file)
    assert not service.envelope_path.is_file()
    assert not service.marker_path.is_file()

    # Must NOT report LEGACY_UNPROTECTED
    state = service.inspect_profile()
    assert state == ProfileSecurityState.RECOVERY_REQUIRED


def test_c4_locked_database_reports_recovery_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_file = tmp_path / "app.sqlite3"
    conn = sqlite3.connect(db_file)
    conn.execute("CREATE TABLE transactions (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()

    service = StartupSecurityService(database_file=db_file)

    def failing_connect(*args: Any, **kwargs: Any) -> Any:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(sqlite3, "connect", failing_connect)

    assert service.inspect_profile() == ProfileSecurityState.RECOVERY_REQUIRED


def test_c4_unrecognized_schema_reports_recovery_required(tmp_path: Path) -> None:
    db_file = tmp_path / "app.sqlite3"
    conn = sqlite3.connect(db_file)
    conn.execute("CREATE TABLE random_unknown_table (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()

    service = StartupSecurityService(database_file=db_file)
    assert service.inspect_profile() == ProfileSecurityState.RECOVERY_REQUIRED


def test_c4_known_legacy_schema_without_tombstone_reports_legacy_unprotected(
    tmp_path: Path,
) -> None:
    db_file = tmp_path / "app.sqlite3"
    conn = sqlite3.connect(db_file)
    conn.execute("CREATE TABLE transactions (id INTEGER PRIMARY KEY, amount REAL)")
    conn.execute("CREATE TABLE categories (id INTEGER PRIMARY KEY, name TEXT)")
    conn.commit()
    conn.close()

    service = StartupSecurityService(database_file=db_file)
    assert service.inspect_profile() == ProfileSecurityState.LEGACY_UNPROTECTED


def test_c4_only_positively_identified_legacy_profile_may_onboard(tmp_path: Path) -> None:
    db_file = tmp_path / "app.sqlite3"
    db_file.write_bytes(b"CORRUPT")

    service = StartupSecurityService(database_file=db_file)
    assert service.inspect_profile() == ProfileSecurityState.RECOVERY_REQUIRED

    with pytest.raises(SecurityRecoveryRequiredError, match="requer recuperação manual"):
        service.configure_first_run("pin", "12345678", weak_pin_confirmed=True)


def test_c4_path_with_spaces_correctly_identified_as_legacy(tmp_path: Path) -> None:
    spaced_dir = tmp_path / "Minhas Financas 2026 - Pessoal"
    spaced_dir.mkdir(parents=True)
    db_file = spaced_dir / "app.sqlite3"
    conn = sqlite3.connect(db_file)
    conn.execute("CREATE TABLE transactions (id INTEGER PRIMARY KEY, amount REAL)")
    conn.execute("CREATE TABLE categories (id INTEGER PRIMARY KEY, name TEXT)")
    conn.commit()
    conn.close()

    service = StartupSecurityService(database_file=db_file)
    # With improper URI escaping, spaces raise sqlite3.OperationalError and yield INDETERMINATE.
    # With canonical as_uri(), it must be recognized cleanly as LEGACY_UNPROTECTED.
    assert service.inspect_profile() == ProfileSecurityState.LEGACY_UNPROTECTED


def test_c1_early_preparation_failure_cleans_up_rotation_backups(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_file = tmp_path / "app.sqlite3"
    conn = sqlite3.connect(db_file)
    conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
    conn.commit()
    conn.close()

    service = StartupSecurityService(database_file=db_file)
    ctx = service.configure_first_run("password", "old_password_1234")
    ctx.close()

    # Fail during marker backup creation, after envelope rotation backup was written
    def explode_marker_backup(*args: Any, **kwargs: Any) -> bool:
        raise OSError("Injected disk failure during marker backup creation")

    monkeypatch.setattr(service, "_create_marker_backup", explode_marker_backup)

    with pytest.raises(Exception):
        service.change_credential("old_password_1234", "password", "new_password_5678")

    # Verify no orphaned rotation backup files remain
    backups = list(service.configuration_dir.glob("*.rotation-backup"))
    assert len(backups) == 0, f"Orphaned rotation backups leaked on disk: {backups}"


def test_c1_rollback_handles_legacy_db_without_settings_table(tmp_path: Path) -> None:
    db_file = tmp_path / "app.sqlite3"
    conn = sqlite3.connect(db_file)
    conn.execute("CREATE TABLE transactions (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()

    service = StartupSecurityService(database_file=db_file)
    # Directly test that restoring None tombstone on a DB without settings does not crash
    service._restore_tamper_tombstone(None)


def test_c2_export_returns_target_path(tmp_path: Path) -> None:
    db_file = tmp_path / "app.sqlite3"
    repo = Repo(FinanceDB(str(db_file)))
    backup_service = BackupService(repo, backup_directory=tmp_path / "backups")
    security_service = StartupSecurityService(database_file=db_file)

    session = security_service.configure_first_run("password", "session_pass_1234")
    set_active_session(session)

    target = tmp_path / "exported_return_check.lgbak"
    result = backup_service.export_encrypted_backup(target)
    assert isinstance(result, Path)
    assert result == target
    assert target.is_file()


def test_c3_export_to_separate_directory_publishes_via_target_parent_workspace(
    tmp_path: Path,
) -> None:
    db_file = tmp_path / "app.sqlite3"
    repo = Repo(FinanceDB(str(db_file)))
    backup_service = BackupService(repo, backup_directory=tmp_path / "primary_backups")
    security_service = StartupSecurityService(database_file=db_file)

    session = security_service.configure_first_run("password", "session_pass_1234")
    set_active_session(session)

    # Export to a completely separate target directory (simulating USB / external drive)
    external_dir = tmp_path / "external_drive" / "subfolder"
    target = external_dir / "external_backup.lgbak"

    published = backup_service.export_encrypted_backup(target)
    assert published == target
    assert target.is_file()
    assert target.read_bytes()[:5] == LGBK2_MAGIC
