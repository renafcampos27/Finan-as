"""Council plan A03/A04: compatible recovery and retained rollback evidence."""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from database.engine import FinanceDB
from database.protection import DataIntegrityError, DataProtectionGuard
from database.repo import Repo
from services.backup import BackupService
from services.backup_crypto import BackupCryptoCodec, read_envelope
from services.startup_security import StartupSecurityService, clear_active_session


@pytest.fixture(autouse=True)
def no_active_security_session():
    clear_active_session()
    yield
    clear_active_session()


def database(path: Path, marker: str) -> Path:
    db = FinanceDB(path)
    try:
        db.conn.execute(
            "INSERT OR REPLACE INTO settings(key,value) VALUES('phase2_marker',?)",
            (marker,),
        )
        db.conn.commit()
    finally:
        db.conn.close()
    return path


def marker(path: Path) -> str:
    conn = sqlite3.connect(path)
    try:
        return conn.execute(
            "SELECT value FROM settings WHERE key='phase2_marker'"
        ).fetchone()[0]
    finally:
        conn.close()


@pytest.fixture
def service(tmp_path, empty_profile):
    path = database(tmp_path / "live" / "profile.sqlite3", "previous")
    db = FinanceDB(path)
    instance = BackupService(Repo(db), database_file=path)
    yield instance
    try:
        db.conn.close()
    except sqlite3.Error:
        pass


def test_future_schema_rejected_before_replacing_profile(tmp_path, service):
    source = database(tmp_path / "future.sqlite3", "future")
    with sqlite3.connect(source) as conn:
        conn.execute("INSERT INTO schema_migrations(version,name) VALUES(999,'future')")
    previous = service.database_file.read_bytes()
    with pytest.raises(ValueError, match="(?:esquema|estrutura|versão)"):
        service.restore_backup(source)
    assert service.database_file.read_bytes() == previous


def test_newer_app_version_with_supported_schema_is_compatible(tmp_path, empty_profile):
    source = database(tmp_path / "compatible.sqlite3", "compatible")
    with sqlite3.connect(source) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO settings(key,value) VALUES('data_guard_app_version','999.0.0')"
        )
    BackupService.validate_restore_database(source)


@pytest.mark.parametrize(
    "operation", ["startup", "interactive_raw", "interactive_complete"]
)
def test_compatible_future_app_restore_can_reopen_with_current_app(
    tmp_path, service, operation
):
    source = database(tmp_path / "future-app" / "source.sqlite3", "future-compatible")
    future_db = FinanceDB(source, app_version="999.0.0")
    future_db.conn.close()
    if operation == "startup":
        target = tmp_path / "fresh-target" / "profile.sqlite3"
        BackupService.restore_missing_profile(
            source, database_file=target, supported_schema_version=FinanceDB.CURRENT_SCHEMA_VERSION
        )
    else:
        target = service.database_file
        if operation == "interactive_complete":
            future_db = FinanceDB(source, app_version="999.0.0")
            source_repo = Repo(future_db)
            attachment = future_db.attachments_directory / "receipt.txt"
            attachment.parent.mkdir(parents=True, exist_ok=True)
            attachment.write_text("synthetic future receipt", encoding="utf-8")
            future_db.add_transaction(
                "2026-10-01",
                "synthetic",
                -1.0,
                "Diversos",
                "Conta principal",
                "expense",
            )
            transaction_id = int(future_db.scalar("SELECT MAX(id) FROM transactions"))
            portable_target = attachment.parent / str(transaction_id) / attachment.name
            portable_target.parent.mkdir()
            attachment.replace(portable_target)
            attachment = portable_target
            source_repo.add_transaction_attachment(
                transaction_id=transaction_id,
                file_name=attachment.name,
                stored_name=attachment.name,
                stored_path=str(attachment),
                original_path=str(attachment),
                file_type="txt",
                description="",
                supplier="",
                invoice_date="",
                total_amount=None,
            )
            source_service = BackupService(source_repo, database_file=source)
            try:
                source = source_service._create_complete_zip(
                    tmp_path / "future-complete.zip", purpose="test"
                )
            finally:
                future_db.conn.close()
        service.restore_backup(source)
    reopened = FinanceDB(target)
    try:
        assert marker(target) == "future-compatible"
    finally:
        reopened.conn.close()


def test_claimed_current_schema_with_missing_capability_is_rejected(
    tmp_path, empty_profile
):
    source = database(tmp_path / "incomplete.sqlite3", "incomplete")
    with sqlite3.connect(source) as conn:
        conn.execute(
            "ALTER TABLE transactions RENAME COLUMN description TO wrong_description"
        )
    with pytest.raises(ValueError, match="(?:coluna|capacidade|estrutura)"):
        BackupService.validate_restore_database(source)


def test_legacy_validation_failure_preserves_error_and_profile(tmp_path, service):
    source = tmp_path / "unrelated.sqlite3"
    with sqlite3.connect(source) as conn:
        conn.execute("CREATE TABLE unrelated(id INTEGER)")
    previous = service.database_file.read_bytes()
    with pytest.raises(ValueError, match="tabelas"):
        service.restore_backup(source)
    assert service.database_file.read_bytes() == previous


def test_restore_rejects_pending_transaction_without_committing_or_writing_recovery(
    tmp_path, service
):
    source = database(tmp_path / "replacement.sqlite3", "replacement")
    connection = service.repo.db.conn
    connection.execute("UPDATE settings SET value='pending' WHERE key='phase2_marker'")
    assert connection.in_transaction
    with pytest.raises(DataIntegrityError, match="alterações pendentes"):
        service.restore_backup(source)
    assert connection.in_transaction
    assert (
        connection.execute(
            "SELECT value FROM settings WHERE key='phase2_marker'"
        ).fetchone()[0]
        == "pending"
    )
    assert marker(service.database_file) == "previous"
    assert not service._guard().restore_marker.exists()
    assert not list(service.backup_directory.glob("antes_de_repor_*"))
    assert not list(service.protection_directory.glob("restauro_rollback_*"))
    connection.rollback()


@pytest.mark.parametrize("operation", ["startup", "interactive"])
def test_committed_source_wal_is_included_without_modifying_source(
    tmp_path, service, operation
):
    source = FinanceDB(tmp_path / "source" / "source.sqlite3")
    try:
        source.conn.execute(
            "INSERT OR REPLACE INTO settings(key,value) VALUES('phase2_marker','checkpointed')"
        )
        source.conn.commit()
        source.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        source.conn.execute(
            "UPDATE settings SET value='latest-committed-WAL' WHERE key='phase2_marker'"
        )
        source.conn.commit()
        wal = Path(f"{source.path}-wal")
        assert wal.stat().st_size > 0
        original_main = source.path.read_bytes()
        original_wal = wal.read_bytes()
        if operation == "startup":
            target = tmp_path / "startup" / "profile.sqlite3"
            BackupService.restore_missing_profile(
                source.path, database_file=target, supported_schema_version=FinanceDB.CURRENT_SCHEMA_VERSION
            )
        else:
            target = service.database_file
            service.restore_backup(source.path)
        assert marker(target) == "latest-committed-WAL"
        assert source.path.read_bytes() == original_main
        assert wal.read_bytes() == original_wal
        assert (
            source.conn.execute(
                "SELECT value FROM settings WHERE key='phase2_marker'"
            ).fetchone()[0]
            == "latest-committed-WAL"
        )
    finally:
        source.conn.close()


def test_unversioned_incomplete_financial_schema_is_rejected_before_replacement(
    tmp_path, service
):
    source = tmp_path / "toy.sqlite3"
    with sqlite3.connect(source) as conn:
        conn.executescript(
            "CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT);"
            "CREATE TABLE categories(name TEXT PRIMARY KEY);"
            "CREATE TABLE transactions(id INTEGER PRIMARY KEY);"
        )
    previous = service.database_file.read_bytes()
    with pytest.raises(ValueError, match="colunas"):
        service.restore_backup(source)
    assert service.database_file.read_bytes() == previous


def test_supported_unversioned_profile_restores_and_reopens(tmp_path, empty_profile):
    source = tmp_path / "unversioned.sqlite3"
    conn = sqlite3.connect(source)
    try:
        conn.executescript(
            "CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT);"
            "CREATE TABLE categories(name TEXT PRIMARY KEY,kind TEXT NOT NULL,colour TEXT NOT NULL,budget REAL NOT NULL DEFAULT 0);"
            "CREATE TABLE transactions(id INTEGER PRIMARY KEY,date TEXT NOT NULL,description TEXT NOT NULL,amount REAL NOT NULL,category TEXT NOT NULL,account TEXT NOT NULL,kind TEXT NOT NULL,notes TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);"
            "INSERT INTO settings VALUES('phase2_marker','unversioned');"
            "INSERT INTO categories VALUES('Legacy Custom','expense','#112233',0);"
            "INSERT INTO transactions(date,description,amount,category,account,kind) VALUES('2026-10-01','historic',-7,'Legacy Custom','Legacy Account','expense');"
        )
        conn.commit()
    finally:
        conn.close()
    target = tmp_path / "restored" / "profile.sqlite3"
    BackupService.restore_missing_profile(
        source, database_file=target, supported_schema_version=FinanceDB.CURRENT_SCHEMA_VERSION
    )
    reopened = FinanceDB(target)
    try:
        assert marker(target) == "unversioned"
        assert reopened.scalar("SELECT MAX(version) FROM schema_migrations") == FinanceDB.CURRENT_SCHEMA_VERSION
    finally:
        reopened.conn.close()


@pytest.mark.parametrize("operation", ["startup", "interactive"])
def test_complete_archive_with_unrelated_database_rejected_before_mutation(
    tmp_path, service, monkeypatch, operation
):
    source = tmp_path / "generic.sqlite3"
    archive = tmp_path / "generic-complete.zip"
    with sqlite3.connect(source) as conn:
        conn.execute("CREATE TABLE unrelated(id INTEGER PRIMARY KEY)")
        conn.commit()
        guard = DataProtectionGuard(source, supported_schema_version=FinanceDB.CURRENT_SCHEMA_VERSION)
        guard.create_snapshot(
            conn,
            source_app_version="3.49.0",
            source_schema_version=0,
            tables=guard.capture_tables(conn),
            files=[],
            target_path=archive,
            purpose="synthetic_generic_archive",
        )
    previous = service.database_file.read_bytes()
    target = tmp_path / "missing-target" / "profile.sqlite3"
    if operation == "startup":
        with pytest.raises(ValueError, match="tabelas"):
            BackupService.restore_missing_profile(
                archive, database_file=target, supported_schema_version=FinanceDB.CURRENT_SCHEMA_VERSION
            )
        assert not target.exists()
    else:

        def forbidden_safety_backup(**_kwargs):
            pytest.fail("incompatible complete archive reached safety/marker creation")

        monkeypatch.setattr(
            service, "_create_restore_safety_backup", forbidden_safety_backup
        )
        with pytest.raises(ValueError, match="tabelas"):
            service.restore_backup(archive)
    assert service.database_file.read_bytes() == previous


@pytest.mark.parametrize("kind", ["LGBK1", "LGBK2"])
def test_missing_profile_recovers_both_encrypted_formats(tmp_path, empty_profile, kind):
    source = database(tmp_path / "source.sqlite3", "recovered")
    encrypted = tmp_path / f"{kind}.lgbak"
    password = "synthetic password with spaces  "
    if kind == "LGBK2":
        security = StartupSecurityService(database_file=source)
        context = security.configure_first_run("password", password)
        try:
            BackupCryptoCodec.encrypt_v2(
                source,
                encrypted,
                master_key=context.backup_master_key,
                envelope=read_envelope(context.envelope_path),
                payload_kind="S",
            )
        finally:
            context.close()
    else:
        BackupService.encrypt_file(source, encrypted, password, payload_kind="S")
    target = tmp_path / "restored" / "profile.sqlite3"
    BackupService.restore_missing_profile(
        encrypted,
        database_file=target,
        supported_schema_version=FinanceDB.CURRENT_SCHEMA_VERSION,
        password=password,
    )
    assert marker(target) == "recovered"


def test_startup_validation_failure_retains_zero_byte_profile_and_sidecars(
    tmp_path, empty_profile
):
    source = tmp_path / "bad.sqlite3"
    source.write_bytes(b"bad")
    target = tmp_path / "live" / "profile.sqlite3"
    target.parent.mkdir()
    target.touch()
    wal = Path(f"{target}-wal")
    shm = Path(f"{target}-shm")
    wal.write_bytes(b"recoverable wal evidence")
    shm.write_bytes(b"recoverable shm evidence")
    with pytest.raises(ValueError):
        BackupService.restore_missing_profile(
            source, database_file=target, supported_schema_version=FinanceDB.CURRENT_SCHEMA_VERSION
        )
    assert target.read_bytes() == b""
    assert wal.read_bytes() == b"recoverable wal evidence"
    assert shm.read_bytes() == b"recoverable shm evidence"


def test_successful_startup_recovery_preserves_zero_byte_sidecar_evidence(
    tmp_path, empty_profile
):
    source = database(tmp_path / "source.sqlite3", "recovered")
    target = tmp_path / "live" / "profile.sqlite3"
    target.parent.mkdir()
    target.touch()
    Path(f"{target}-wal").write_bytes(b"original recovery WAL")
    Path(f"{target}-shm").write_bytes(b"original recovery SHM")
    preserved = BackupService.restore_missing_profile(
        source, database_file=target, supported_schema_version=FinanceDB.CURRENT_SCHEMA_VERSION
    )
    assert marker(target) == "recovered"
    assert preserved is not None
    assert (preserved / "database" / target.name).read_bytes() == b""
    assert (
        preserved / "database" / f"{target.name}-wal"
    ).read_bytes() == b"original recovery WAL"
    assert (
        preserved / "database" / f"{target.name}-shm"
    ).read_bytes() == b"original recovery SHM"


def test_failed_finalization_and_rollback_retains_durable_recovery_then_startup_recovers(
    tmp_path, service, monkeypatch
):
    source = database(tmp_path / "replacement.sqlite3", "replacement")

    def reject_tombstone(*_args, **_kwargs):
        raise OSError("synthetic tombstone failure")

    with monkeypatch.context() as patch:
        patch.setattr(service, "_resync_security_tombstone", reject_tombstone)
        with pytest.raises(DataIntegrityError, match="recuperação automática"):
            service.restore_backup(source)
    guard = service._guard()
    assert guard.restore_marker.is_file()
    assert list(service.protection_directory.glob("restauro_rollback_*.zip"))
    assert marker(service.database_file) == "previous"
    marker_before_retry = guard.restore_marker.read_bytes()
    recovery_before_retry = list(
        service.protection_directory.glob("restauro_rollback_*.zip")
    )
    with pytest.raises(DataIntegrityError, match="pendente"):
        service.restore_backup(source)
    assert guard.restore_marker.read_bytes() == marker_before_retry
    assert (
        list(service.protection_directory.glob("restauro_rollback_*.zip"))
        == recovery_before_retry
    )
    reopened = FinanceDB(service.database_file)
    try:
        assert marker(service.database_file) == "previous"
        assert not guard.restore_marker.exists()
    finally:
        reopened.conn.close()


def test_rapid_restore_safety_backups_keep_distinct_recovery_points(
    service, monkeypatch
):
    monkeypatch.setattr("services.backup.backup_stamp", lambda: "20260101_010101")
    first = service._create_restore_safety_backup()
    second = service._create_restore_safety_backup()
    assert first != second
    assert first.is_file()
    assert second.is_file()
    BackupService.validate_complete_archive(first)
    BackupService.validate_complete_archive(second)


def test_attachment_double_failure_retains_previous_directory(
    tmp_path, service, monkeypatch
):
    target = service.attachments_directory
    target.mkdir(parents=True, exist_ok=True)
    (target / "previous.txt").write_text("previous", encoding="utf-8")
    staged = tmp_path / "staged"
    staged.mkdir()
    (staged / "new.txt").write_text("new", encoding="utf-8")
    original = Path.replace

    def refuse_install_and_rollback(path, destination):
        if Path(destination) == target and path != target:
            raise OSError("synthetic install and rollback failure")
        return original(path, destination)

    monkeypatch.setattr(Path, "replace", refuse_install_and_rollback)
    with pytest.raises((OSError, DataIntegrityError)):
        service._restore_attachments(staged)
    recovery = list(target.parent.glob(f".{target.name}.restore.previous*"))
    assert recovery
    assert (recovery[0] / "previous.txt").read_text(encoding="utf-8") == "previous"


@pytest.mark.parametrize(
    "method", ["_begin_durable_restore", "_create_restore_safety_backup"]
)
def test_requested_encryption_never_falls_back_to_cleartext(
    service, monkeypatch, method
):
    monkeypatch.setattr(service, "_encrypted_backups_enabled", lambda: True)

    def missing_password():
        raise RuntimeError("synthetic missing credential")

    monkeypatch.setattr(service, "_required_backup_password", missing_password)
    with pytest.raises(RuntimeError):
        getattr(service, method)()
    assert not list(service.backup_directory.glob("antes_de_repor_*.zip"))
    assert not list(service.protection_directory.glob("restauro_rollback_*.zip"))


def test_complete_restore_keeps_marker_through_tombstone_update(
    tmp_path, service, monkeypatch
):
    recovery = tmp_path / "recovery.zip"
    recovery.write_bytes(b"recovery evidence")
    marker_path = tmp_path / "restore-marker.json"
    marker_path.write_text("{}", encoding="utf-8")
    guard = SimpleNamespace(
        database_file=service.database_file,
        clear_restore_marker=lambda: marker_path.unlink(),
    )
    monkeypatch.setattr(
        service, "_normalise_installed_attachment_paths", lambda *_: None
    )
    monkeypatch.setattr(service, "_validate_installed_database", lambda *_: None)

    def reject_tombstone(*_args, **_kwargs):
        assert marker_path.is_file()
        assert recovery.is_file()
        raise RuntimeError("synthetic tombstone failure")

    monkeypatch.setattr(service, "_resync_security_tombstone", reject_tombstone)
    with pytest.raises(RuntimeError, match="tombstone"):
        service._complete_durable_restore(guard, recovery)
    assert marker_path.is_file()
    assert recovery.is_file()


def test_preflight_counts_attachments_before_database_copy(
    tmp_path, service, monkeypatch
):
    service.attachments_directory.mkdir(parents=True, exist_ok=True)
    with (service.attachments_directory / "large.bin").open("wb") as handle:
        handle.truncate(90 * 1024 * 1024)
    monkeypatch.setattr(
        shutil, "disk_usage", lambda _: SimpleNamespace(free=100 * 1024 * 1024)
    )

    def unexpected_copy(*_args):
        pytest.fail("preflight ignored attachment space")

    monkeypatch.setattr(service.repo.db, "backup_to", unexpected_copy)
    with pytest.raises(DataIntegrityError, match="Espaço"):
        service._create_complete_zip(tmp_path / "out.zip", purpose="test")


def test_secondary_full_disk_leaves_primary_valid_and_does_not_copy(
    tmp_path, service, monkeypatch
):
    source = tmp_path / "primary.zip"
    source.write_bytes(b"verified primary evidence")
    secondary = tmp_path / "secondary"
    secondary.mkdir()
    monkeypatch.setattr(
        service,
        "preferences",
        lambda: {
            "secondary_backup_enabled": True,
            "secondary_backup_directory": str(secondary),
        },
    )
    monkeypatch.setattr(
        service, "validate_secondary_directory", lambda *_a, **_k: secondary
    )
    monkeypatch.setattr(shutil, "disk_usage", lambda _: SimpleNamespace(free=0))
    copied = []
    monkeypatch.setattr(shutil, "copy2", lambda *_a, **_k: copied.append(True))
    metadata = {}
    monkeypatch.setattr(
        service, "_update_backup_metadata", lambda **kwargs: metadata.update(kwargs)
    )
    assert service._mirror_to_secondary(source) is None
    assert copied == []
    assert source.read_bytes() == b"verified primary evidence"
    assert "espaço" in str(metadata.get("last_secondary_backup_error", "")).lower()
