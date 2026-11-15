"""PLANO_IMPLEMENTACAO_VALIDADO_COUNCIL_2026-10-25.md / A03–A04 contracts."""

from __future__ import annotations

import sqlite3
import stat
import json
import subprocess
import sys
from pathlib import Path

import pytest

from database.engine import FinanceDB
from database.protection import (
    DataCompatibilityError,
    DataIntegrityError,
    DataPreservationError,
    DataProtectionGuard,
    MissingDatabaseRecoveryError,
)
from services.backup_crypto import BackupCryptoCodec
from services.startup_security import (
    CredentialRotationError,
    SecurityRecoveryRequiredError,
    SecurityRecoveryStorageError,
    StartupSecurityService,
    get_active_session,
    set_active_session,
)
from utils import private_temp


@pytest.fixture(autouse=True)
def isolated_security_session():
    previous = get_active_session()
    set_active_session(None)
    yield
    set_active_session(previous)


def _sqlite(path: Path, label: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT)")
        connection.execute("INSERT INTO settings VALUES('synthetic_label',?)", (label,))
        connection.commit()
    finally:
        connection.close()


def _stage(tmp_path: Path):
    guard = DataProtectionGuard(
        tmp_path / "live.sqlite3",
        protection_directory=tmp_path / "protections",
        preferences_file=tmp_path / "preferences.json",
        attachments_directory=tmp_path / "attachments",
        history_directory=tmp_path / "history",
    )
    _sqlite(guard.database_file, "old")
    for directory in (guard.attachments_directory, guard.history_directory):
        directory.mkdir()
        (directory / "original.txt").write_text("old", encoding="utf-8")
    guard.preferences_file.write_text('{"label":"old"}', encoding="utf-8")
    staged = tmp_path / "stage"
    staged.mkdir()
    database = staged / "database.sqlite3"
    _sqlite(database, "new")
    attachments = staged / "attachments"
    history = staged / "history"
    for directory in (attachments, history):
        directory.mkdir()
        (directory / "new.txt").write_text("new", encoding="utf-8")
    preferences = staged / "preferences.json"
    preferences.write_text('{"label":"new"}', encoding="utf-8")
    return guard, database, attachments, history, preferences


def _install(parts, callback=None):
    guard, database, attachments, history, preferences = parts
    guard._install_staged_restore(
        database,
        attachments,
        history,
        preferences,
        preferences_present=True,
        finalize_callback=callback,
    )


@pytest.mark.parametrize(
    "step",
    [
        "database_move",
        "database_install",
        "attachments_move",
        "attachments_install",
        "history_move",
        "history_install",
        "preferences_move",
        "preferences_install",
        "finalize",
        "reopen",
        "authorization",
    ],
)
def test_each_installation_failure_restores_old_profile(tmp_path, monkeypatch, step):
    parts = _stage(tmp_path)
    guard = parts[0]
    original_database = guard.database_file.read_bytes()
    original_replace = Path.replace
    fired = False

    def replace(source, destination):
        nonlocal fired
        target = Path(destination)
        operation = None
        targets = {
            guard.database_file: "database",
            guard.attachments_directory: "attachments",
            guard.history_directory: "history",
            guard.preferences_file: "preferences",
        }
        if source in targets and target.name.endswith(".rollback"):
            operation = targets[source] + "_move"
        elif target in targets and source.name.endswith((".restore", ".candidate")):
            operation = targets[target] + "_install"
        if step == operation and not fired:
            fired = True
            raise OSError("injected installation failure")
        return original_replace(source, target)

    monkeypatch.setattr(Path, "replace", replace)
    if step == "reopen":
        original_validate = guard._validate_restored_database

        def validate(path):
            if path == guard.database_file:
                raise DataIntegrityError("injected reopened integrity failure")
            return original_validate(path)

        monkeypatch.setattr(guard, "_validate_restored_database", validate)
    if step == "authorization":

        def fail_authorization():
            raise OSError("injected authorization failure")

        monkeypatch.setattr(guard, "_authorize_compatible_restore", fail_authorization)

    def finalize():
        if step == "finalize":
            raise OSError("injected finalization failure")

    with pytest.raises((OSError, DataIntegrityError)):
        _install(parts, finalize)
    if step not in {"finalize", "reopen", "authorization"}:
        assert fired
    assert guard.database_file.read_bytes() == original_database
    assert guard.preferences_file.read_text(encoding="utf-8") == '{"label":"old"}'
    for directory in (guard.attachments_directory, guard.history_directory):
        assert (directory / "original.txt").read_text(encoding="utf-8") == "old"
        assert not (directory / "new.txt").exists()
    assert not guard.compatible_restore_marker.exists()


def test_failed_database_rollback_keeps_main_and_wal_as_matching_evidence(
    tmp_path, monkeypatch
):
    parts = _stage(tmp_path)
    guard = parts[0]
    guard.database_file.write_bytes(b"")
    for suffix in ("-wal", "-shm"):
        Path(f"{guard.database_file}{suffix}").write_bytes(suffix.encode())
    original_replace = Path.replace

    def replace(source, target):
        if source.name.endswith(".rollback") and Path(target) == guard.database_file:
            raise OSError("old database cannot be reinstalled")
        return original_replace(source, target)

    monkeypatch.setattr(Path, "replace", replace)
    with pytest.raises(DataPreservationError, match="reversão incompleta"):
        _install(parts, lambda: (_ for _ in ()).throw(OSError("finalization failed")))
    main = list(tmp_path.glob(".live.sqlite3.*.rollback"))
    assert any(path.stat().st_size == 0 for path in main)
    for suffix in ("-wal", "-shm"):
        evidence = list(tmp_path.glob(f".live.sqlite3.*{suffix}.rollback"))
        assert len(evidence) == 1
        assert evidence[0].read_bytes() == suffix.encode()
        assert not Path(f"{guard.database_file}{suffix}").exists()


def test_invalid_staged_database_never_mutates_profile(tmp_path):
    parts = _stage(tmp_path)
    guard, staged, *_ = parts
    before = guard.database_file.read_bytes()
    staged.write_bytes(b"not sqlite")
    with pytest.raises(DataIntegrityError):
        _install(parts)
    assert guard.database_file.read_bytes() == before
    assert (guard.attachments_directory / "original.txt").exists()


def test_zero_byte_main_with_sidecars_requires_recovery(tmp_path):
    guard = DataProtectionGuard(
        tmp_path / "empty.sqlite3", protection_directory=tmp_path / "protection"
    )
    guard.database_file.touch()
    wal = Path(f"{guard.database_file}-wal")
    wal.write_bytes(b"recoverable WAL evidence")
    with pytest.raises(MissingDatabaseRecoveryError):
        guard.prepare()
    assert guard.database_file.read_bytes() == b""
    assert wal.read_bytes() == b"recoverable WAL evidence"


def test_new_profile_cleanup_removes_readonly_owned_files(tmp_path):
    guard = DataProtectionGuard(
        tmp_path / "new.sqlite3", protection_directory=tmp_path / "protection"
    )
    files = [
        guard.database_file,
        *(Path(f"{guard.database_file}{suffix}") for suffix in ("-wal", "-shm")),
    ]
    for path in files:
        path.write_bytes(b"owned new profile")
        path.chmod(stat.S_IREAD)
    guard.remove_new_database()
    assert all(not path.exists() for path in files)


@pytest.mark.parametrize("tamper", [False, True])
def test_verified_future_app_restore_has_hash_bound_one_time_handoff(tmp_path, tamper):
    from utils.version import APP_VERSION

    future_path = tmp_path / "future.sqlite3"
    future = FinanceDB(future_path, app_version="9.0.0")
    future.conn.close()
    target = tmp_path / "target.sqlite3"
    current = FinanceDB(target)
    current.conn.close()
    guard = DataProtectionGuard(
        target,
        supported_schema_version=FinanceDB.CURRENT_SCHEMA_VERSION,
        protection_directory=tmp_path / "handoff",
        current_app_version=APP_VERSION,
    )
    attachments, history = tmp_path / "staged-attachments", tmp_path / "staged-history"
    attachments.mkdir()
    history.mkdir()
    guard._install_staged_restore(
        future_path, attachments, history, None, preferences_present=False
    )
    assert guard.compatible_restore_marker.is_file()
    if tamper:
        connection = sqlite3.connect(target)
        connection.execute(
            "INSERT OR REPLACE INTO settings VALUES('unreviewed_change','1')"
        )
        connection.commit()
        connection.close()
        with pytest.raises(DataCompatibilityError):
            FinanceDB(target, protection_directory=guard.protection_directory)
        assert guard.compatible_restore_marker.is_file()
    else:
        reopened = FinanceDB(target, protection_directory=guard.protection_directory)
        try:
            assert reopened.scalar("PRAGMA user_version") >= 0
            assert not guard.compatible_restore_marker.exists()
            assert (
                reopened.conn.execute(
                    "SELECT value FROM settings WHERE key='data_guard_app_version'"
                ).fetchone()[0]
                == APP_VERSION
            )
        finally:
            reopened.conn.close()


def test_encryption_failure_never_replaces_existing_protection_or_emits_plaintext(
    tmp_path, monkeypatch
):
    database = FinanceDB(tmp_path / "profile.sqlite3")
    security = StartupSecurityService(database_file=database.path)
    session = security.configure_first_run("password", "synthetic password").close()
    del session
    session = security.unlock("password", "synthetic password")
    guard = DataProtectionGuard(
        database.path,
        supported_schema_version=FinanceDB.CURRENT_SCHEMA_VERSION,
        protection_directory=tmp_path / "protection",
        session_context=session,
    )
    target = guard.protection_directory / "existing.lgbak"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"previous complete backup")
    monkeypatch.setattr(guard, "_archive_name", lambda _version: target)

    def fail_encrypt(*_args, **_kwargs):
        raise OSError("injected encryption failure")

    monkeypatch.setattr(BackupCryptoCodec, "encrypt_v2", fail_encrypt)
    try:
        with pytest.raises(OSError, match="injected encryption failure"):
            guard.create_snapshot(
                database.conn,
                source_app_version="3.0.0",
                source_schema_version=FinanceDB.CURRENT_SCHEMA_VERSION,
                tables=guard.capture_tables(database.conn),
                files=guard.capture_files(),
            )
        assert target.read_bytes() == b"previous complete backup"
        assert not list(guard.protection_directory.glob("*.zip"))
        assert not (
            guard.protection_directory / private_temp.PRIVATE_WORK_DIRECTORY
        ).exists()
    finally:
        session.close()
        database.conn.close()


def test_private_cleanup_handles_readonly_and_reports_retryable_failure(
    tmp_path, monkeypatch
):
    root = tmp_path / "owned"
    root.mkdir()
    file = root / "secret.txt"
    file.write_text("synthetic secret", encoding="utf-8")
    file.chmod(stat.S_IREAD)
    assert private_temp.remove_private_path(root)
    assert not root.exists()
    root.mkdir()
    file.write_text("synthetic secret", encoding="utf-8")

    def fail_remove(*_args, **_kwargs):
        raise PermissionError("injected locked file")

    with monkeypatch.context() as patch:
        patch.setattr(private_temp.shutil, "rmtree", fail_remove)
        assert private_temp.remove_private_path(root) is False
    assert root in private_temp.pending_private_cleanup()
    assert private_temp.remove_private_path(root)
    assert root not in private_temp.pending_private_cleanup()


def test_private_workspace_failure_is_visible_and_cleanup_can_be_retried(
    tmp_path, monkeypatch
):
    def fail_remove(*_args, **_kwargs):
        raise PermissionError("injected locked private workspace")

    with monkeypatch.context() as patch:
        patch.setattr(private_temp.shutil, "rmtree", fail_remove)
        with pytest.raises(private_temp.PrivateCleanupError):
            with private_temp.private_workspace(tmp_path, prefix="test") as workspace:
                (workspace / "payload").write_bytes(b"synthetic")
    assert workspace in private_temp.pending_private_cleanup()
    assert private_temp.cleanup_private_work(tmp_path) == ()
    assert not workspace.exists()


def test_cleanup_never_deletes_active_private_workspace(tmp_path):
    with private_temp.private_workspace(tmp_path, prefix="active") as workspace:
        payload = workspace / "payload"
        payload.write_bytes(b"active operation")
        assert private_temp.cleanup_private_work(tmp_path) == ()
        assert payload.read_bytes() == b"active operation"
    assert not workspace.exists()


def test_pending_root_cleanup_does_not_bypass_active_workspace_lease(tmp_path):
    with private_temp.private_workspace(tmp_path, prefix="active-at-exit") as workspace:
        payload = workspace / "payload"
        payload.write_bytes(b"active operation")
        private_temp._FAILED_CLEANUPS.add(workspace.parent)
        private_temp._cleanup_all_active_workspaces()
        assert payload.read_bytes() == b"active operation"
    assert not workspace.exists()


@pytest.mark.parametrize("ending", ["normal", "crash"])
def test_cleanup_respects_foreign_process_owner_and_recovers_crash(tmp_path, ending):
    code = """
import os, sys
from pathlib import Path
from utils.private_temp import private_workspace
with private_workspace(Path(sys.argv[1]), prefix='foreign') as workspace:
    (workspace / 'payload').write_bytes(b'live synthetic operation')
    print(workspace, flush=True)
    if sys.stdin.readline().strip() == 'crash':
        os._exit(17)
"""
    child = subprocess.Popen(
        [sys.executable, "-c", code, str(tmp_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        workspace = Path(child.stdout.readline().strip())
        assert workspace.is_relative_to(tmp_path)
        assert workspace.is_dir()
        payload = workspace / "payload"
        assert private_temp.cleanup_private_work(tmp_path) == ()
        assert payload.read_bytes() == b"live synthetic operation"
        _stdout, stderr = child.communicate(ending + "\n", timeout=20)
        assert child.returncode == (17 if ending == "crash" else 0), stderr
        if ending == "crash":
            assert payload.is_file()
            assert private_temp.cleanup_private_work(tmp_path) == ()
        assert not workspace.exists()
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=10)


def test_readonly_policy_never_follows_mocked_root_links(tmp_path, monkeypatch):
    link = tmp_path / "synthetic-link"
    link.write_bytes(b"link node")
    outside = tmp_path / "outside"
    outside.write_bytes(b"outside content")
    original_is_symlink = Path.is_symlink

    def is_symlink(path):
        return path == link or original_is_symlink(path)

    def forbid_chmod(*_args, **_kwargs):
        pytest.fail("A link target must never be chmod'ed")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "is_symlink", is_symlink)
        patch.setattr("database.protection.os.chmod", forbid_chmod)
        DataProtectionGuard._ensure_writable(link)
        DataProtectionGuard._remove_path(link)
    assert not link.exists()
    assert outside.read_bytes() == b"outside content"


def test_junction_root_is_removed_as_link_without_tree_or_permission_recursion(
    tmp_path, monkeypatch
):
    junction = tmp_path / "synthetic-junction"
    junction.mkdir()
    child = junction / "must-survive"
    child.write_bytes(b"external target")
    original_is_junction = Path.is_junction

    def is_junction(path):
        return path == junction or original_is_junction(path)

    def forbid_chmod(*_args, **_kwargs):
        pytest.fail("A junction target must never be chmod'ed")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "is_junction", is_junction)
        patch.setattr("database.protection.os.chmod", forbid_chmod)
        DataProtectionGuard._ensure_writable(junction)
        with pytest.raises(OSError):
            DataProtectionGuard._remove_path(junction)
    assert child.read_bytes() == b"external target"


def test_private_readonly_retry_unlinks_mocked_nested_link_without_chmod(
    tmp_path, monkeypatch
):
    root = tmp_path / "private"
    root.mkdir()
    link = root / "synthetic-link"
    link.write_bytes(b"link node")
    original_is_symlink = Path.is_symlink

    def is_symlink(path):
        return path == link or original_is_symlink(path)

    def forbid_chmod(*_args, **_kwargs):
        pytest.fail("A private link target must never be chmod'ed")

    def synthetic_rmtree(path, *, onexc):
        onexc(
            lambda *_args: pytest.fail("Do not retry a traversal on a link"),
            link,
            PermissionError(),
        )
        Path(path).rmdir()

    with monkeypatch.context() as patch:
        patch.setattr(Path, "is_symlink", is_symlink)
        patch.setattr(private_temp.os, "chmod", forbid_chmod)
        patch.setattr(private_temp.shutil, "rmtree", synthetic_rmtree)
        assert private_temp.remove_private_path(root)
    assert not root.exists()


def test_private_cleanup_failure_preserves_original_operation_error(
    tmp_path, monkeypatch
):
    def fail_remove(*_args, **_kwargs):
        raise PermissionError("injected locked private workspace")

    with monkeypatch.context() as patch:
        patch.setattr(private_temp.shutil, "rmtree", fail_remove)
        with pytest.raises(ValueError, match="original operation failure") as captured:
            with private_temp.private_workspace(tmp_path, prefix="error") as workspace:
                (workspace / "payload").write_bytes(b"synthetic")
                raise ValueError("original operation failure")
    assert any("temporários privados" in note for note in captured.value.__notes__)
    assert private_temp.cleanup_private_work(tmp_path) == ()


def test_completed_recovery_cleanup_retries_without_reverting_verified_profile(
    tmp_path, monkeypatch
):
    parts = _stage(tmp_path)
    guard = parts[0]
    guard.protection_directory.mkdir()
    recovery = guard.protection_directory / "recovery.zip"
    recovery.write_bytes(b"synthetic completed recovery residue")
    guard.restore_marker.write_text(
        json.dumps(
            {
                "database": str(guard.database_file.resolve()),
                "recovery_archive": str(recovery.resolve()),
            }
        ),
        encoding="utf-8",
    )
    before = guard.database_file.read_bytes()
    original_remove = guard._remove_path

    def fail_archive_remove(path):
        if path == recovery:
            raise PermissionError("injected recovery cleanup failure")
        return original_remove(path)

    with monkeypatch.context() as patch:
        patch.setattr(
            DataProtectionGuard, "_remove_path", staticmethod(fail_archive_remove)
        )
        with pytest.raises(PermissionError):
            guard.clear_restore_marker(remove_archive=True)
    assert json.loads(guard.restore_marker.read_text(encoding="utf-8"))["completed"]
    assert recovery.is_file()

    def forbid_restore(*_args, **_kwargs):
        pytest.fail("A completed restore must only retry cleanup")

    monkeypatch.setattr(guard, "restore_archive", forbid_restore)
    guard._recover_incomplete_restore()
    assert not guard.restore_marker.exists()
    assert not recovery.exists()
    assert guard.database_file.read_bytes() == before


def test_unrecognized_recovery_marker_is_retained_for_manual_recovery(tmp_path):
    guard = _stage(tmp_path)[0]
    guard.protection_directory.mkdir()
    guard.restore_marker.write_text('{"unknown":"metadata"}', encoding="utf-8")
    before = guard.restore_marker.read_bytes()
    with pytest.raises(DataIntegrityError):
        guard.clear_restore_marker(remove_archive=True)
    assert guard.restore_marker.read_bytes() == before


def test_file_cleanup_never_recurses_into_unexpected_marker_or_sidecar_directories(
    tmp_path,
):
    guard = _stage(tmp_path)[0]
    guard.restore_marker.mkdir(parents=True)
    protected = guard.restore_marker / "unrecognized-content"
    protected.write_bytes(b"preserve evidence")
    with pytest.raises(IsADirectoryError):
        guard.clear_restore_marker()
    assert protected.read_bytes() == b"preserve evidence"
    wal = Path(f"{guard.database_file}-wal")
    wal.mkdir()
    content = wal / "unrecognized-content"
    content.write_bytes(b"preserve sidecar evidence")
    before = guard.database_file.read_bytes()
    with pytest.raises(IsADirectoryError):
        guard.remove_new_database()
    assert content.read_bytes() == b"preserve sidecar evidence"
    assert guard.database_file.read_bytes() == before


def test_failed_rotation_retains_transition_when_rollback_fails(tmp_path, monkeypatch):
    security = StartupSecurityService(database_file=tmp_path / "profile.sqlite3")
    security.configure_first_run("password", "synthetic old password").close()

    def fail_write(*_args, **_kwargs):
        raise OSError("injected transition failure")

    def fail_rollback(*_args, **_kwargs):
        raise OSError("injected rollback failure")

    monkeypatch.setattr(
        "services.startup_security.write_security_marker_atomically", fail_write
    )
    monkeypatch.setattr(security, "_rollback_rotation", fail_rollback)
    with pytest.raises(SecurityRecoveryRequiredError):
        security.change_credential(
            "synthetic old password", "password", "synthetic new password"
        )
    transition = security.envelope_path.with_name(
        security.envelope_path.name + ".rotation-tmp"
    )
    assert transition.exists()
    assert list(security.configuration_dir.glob("*.rotation-backup"))
    with pytest.raises(SecurityRecoveryRequiredError):
        security.change_credential(
            "synthetic old password", "password", "another synthetic password"
        )


def test_failed_candidate_rotation_restores_old_credential_and_removes_transition(
    tmp_path, monkeypatch
):
    security = StartupSecurityService(database_file=tmp_path / "profile.sqlite3")
    security.configure_first_run("password", "synthetic old password").close()

    def fail_write(*_args, **_kwargs):
        raise OSError("injected marker failure")

    monkeypatch.setattr(
        "services.startup_security.write_security_marker_atomically", fail_write
    )
    with pytest.raises(CredentialRotationError):
        security.change_credential(
            "synthetic old password", "password", "synthetic new password"
        )
    session = security.unlock("password", "synthetic old password")
    session.close()
    assert not security.envelope_path.with_name(
        security.envelope_path.name + ".rotation-tmp"
    ).exists()
    assert not list(security.configuration_dir.glob("*.rotation-backup"))


def test_committed_rotation_cleanup_failure_keeps_new_credential_valid(
    tmp_path, monkeypatch
):
    security = StartupSecurityService(database_file=tmp_path / "profile.sqlite3")
    security.configure_first_run("password", "synthetic old password").close()
    original_unlink = Path.unlink

    def locked_backup(path, *args, **kwargs):
        if path.name.endswith(".rotation-backup"):
            raise PermissionError("injected locked rotation backup")
        return original_unlink(path, *args, **kwargs)

    def forbid_rollback(*_args, **_kwargs):
        pytest.fail(
            "A verified committed rotation must not roll back on cleanup failure"
        )

    monkeypatch.setattr(Path, "unlink", locked_backup)
    monkeypatch.setattr(security, "_rollback_rotation", forbid_rollback)
    with pytest.raises(SecurityRecoveryStorageError, match="nova credencial"):
        security.change_credential(
            "synthetic old password", "password", "synthetic new password"
        )
    session = security.unlock("password", "synthetic new password")
    session.close()
    assert list(security.configuration_dir.glob("*.rotation-backup"))
