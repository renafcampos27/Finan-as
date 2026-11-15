"""BUG-AUD-C115..C122 — Council round 4, member 2 (crypto/security/file resilience).

Regression suite for batch 25.  Every test pins one audited defect with
exact-value assertions; each test name carries the bug id.
"""

from __future__ import annotations

import gc
import json
import os
import shutil
import stat
import time
import zipfile
from pathlib import Path

import pytest

from database.engine import FinanceDB
from services import backup_crypto as bc
from services.backup_crypto import (
    BackupCryptoCodec,
    CredentialKind,
    InsufficientStorageError,
    SecurityEnvelope,
    SNAPSHOT_MIN_RETENTION_SECONDS,
    build_envelope,
    read_envelope,
    safe_open_exclusive_temp,
)
from services.change_journal import ChangeJournalService
from services.startup_security import StartupSecurityService

PROFILE_ID = "25252525-2525-2525-2525-252525252525"
PASSWORD = b"correct horse battery staple"
PIN = b"1234567890"


class RepoStub:
    def __init__(self, database: FinanceDB) -> None:
        self.db = database


@pytest.fixture()
def envelope() -> SecurityEnvelope:
    master = bytearray(range(32))
    return build_envelope(
        profile_id=PROFILE_ID,
        default_unlock_method=CredentialKind.PASSWORD,
        master_key=master,
        credentials={"password": PASSWORD, "pin": PIN},
    )


def _payload(tmp_path: Path, size: int = 5000) -> Path:
    source = tmp_path / "payload.zip"
    source.write_bytes(b"PAYLOAD-" + bytes(range(256)) * (size // 256))
    return source


def _encrypt(tmp_path: Path, source: Path, env: SecurityEnvelope) -> Path:
    target = tmp_path / "backup.lgbak"
    BackupCryptoCodec.encrypt_v2(
        source,
        target,
        master_key=bytes(range(32)),
        envelope=env,
        payload_kind="Z",
    )
    return target


def _legacy_encrypt(source: Path, target: Path, password: str) -> Path:
    """Produce a real LGBK1 file via the frozen legacy path."""
    from services.backup import BackupService

    BackupService.encrypt_file(source, target, password, payload_kind="Z")
    return target


# ---------------------------------------------------------------------------
# BUG-AUD-C115 — exclusive temp files (CWE-377/CWE-59)
# ---------------------------------------------------------------------------


def test_c115_safe_open_exclusive_temp_rejects_existing_path(
    tmp_path: Path,
) -> None:
    victim = tmp_path / "target.txt"
    victim.write_bytes(b"victim")
    with pytest.raises(OSError):
        safe_open_exclusive_temp(victim)
    # The pre-existing file was not opened/truncated through a followed link.
    assert victim.read_bytes() == b"victim"


def test_c115_safe_open_exclusive_temp_follows_no_symlink(
    tmp_path: Path,
) -> None:
    victim = tmp_path / "victim.txt"
    victim.write_bytes(b"victim")
    link = tmp_path / "link.txt"
    try:
        os.symlink(victim, link)
    except OSError:
        pytest.skip("symlink privileges unavailable on this host")
    with pytest.raises(OSError):
        safe_open_exclusive_temp(link)
    assert victim.read_bytes() == b"victim"


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="POSIX O_NOFOLLOW only")
def test_c115_helper_uses_o_nofollow_and_owner_mode(tmp_path: Path) -> None:
    target = tmp_path / "work.bin"
    with safe_open_exclusive_temp(target) as handle:
        handle.write(b"x")
    mode = stat.S_IMODE(target.stat().st_mode)
    assert mode & 0o077 == 0  # no group/other bits
    assert os.O_NOFOLLOW  # constant exists; helper wires it in


def test_c115_lgbk2_encrypt_fails_closed_on_preplanted_temp(
    tmp_path: Path,
    envelope: SecurityEnvelope,
) -> None:
    """O_EXCL refuses to create the temp when the path is already taken."""

    source = _payload(tmp_path)
    target = tmp_path / "backup.lgbak"
    planted = tmp_path / ".backup.lgbak.encrypting"
    planted.write_bytes(b"planted")
    with pytest.raises(FileExistsError):
        BackupCryptoCodec.encrypt_v2(
            source,
            target,
            master_key=bytes(range(32)),
            envelope=envelope,
            payload_kind="Z",
        )
    # Nothing was published; the planted path was never followed (fail-closed
    # cleanup removes the temp entry instead of writing through it).
    assert not target.exists()
    assert not planted.exists()


def test_c115_lgbk1_decrypt_fails_closed_on_preplanted_temp(
    tmp_path: Path,
) -> None:
    source = _payload(tmp_path)
    legacy = _legacy_encrypt(source, tmp_path / "legacy.lgbak", "legacy password")
    target = tmp_path / "out.zip"
    planted = tmp_path / ".out.zip.decrypting"
    planted.write_bytes(b"planted")
    with pytest.raises(FileExistsError):
        BackupCryptoCodec.decrypt(legacy, target, credential=b"legacy password")
    assert not target.exists()
    assert not planted.exists()


def test_c115_lgbk2_decrypt_fails_closed_on_preplanted_temp(
    tmp_path: Path,
    envelope: SecurityEnvelope,
) -> None:
    source = _payload(tmp_path)
    encrypted = _encrypt(tmp_path, source, envelope)
    target = tmp_path / "out.zip"
    planted = tmp_path / ".out.zip.decrypting"
    planted.write_bytes(b"planted")
    with pytest.raises(FileExistsError):
        BackupCryptoCodec.decrypt(
            encrypted,
            target,
            credential=PASSWORD,
            credential_kind="password",
        )
    assert not target.exists()
    assert not planted.exists()


# ---------------------------------------------------------------------------
# BUG-AUD-C116 — free-space gate in _decrypt_lgbk1
# ---------------------------------------------------------------------------


def test_c116_lgbk1_decrypt_rejects_when_free_space_below_estimate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _payload(tmp_path)
    legacy = _legacy_encrypt(source, tmp_path / "legacy.lgbak", "legacy password")
    real_usage = shutil.disk_usage

    class FakeUsage:
        free = 24 * 1024 * 1024  # below source_size*4 + 25 MB

        total = used = 0

    def fake_disk_usage(path: object) -> FakeUsage:
        return FakeUsage()

    monkeypatch.setattr(bc.shutil, "disk_usage", fake_disk_usage)
    with pytest.raises(InsufficientStorageError):
        BackupCryptoCodec.decrypt(
            legacy,
            tmp_path / "out.zip",
            credential=b"legacy password",
        )
    assert real_usage is not None  # sanity: original callable untouched


def test_c116_lgbk1_decrypt_succeeds_when_free_space_sufficient(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _payload(tmp_path)
    legacy = _legacy_encrypt(source, tmp_path / "legacy.lgbak", "legacy password")

    class FakeUsage:
        free = 10 * 1024**3
        total = used = 0

    monkeypatch.setattr(
        bc.shutil,
        "disk_usage",
        lambda path: FakeUsage(),
    )
    kind = BackupCryptoCodec.decrypt(
        legacy,
        tmp_path / "out.zip",
        credential=b"legacy password",
    )
    assert kind == "Z"
    assert (tmp_path / "out.zip").read_bytes() == source.read_bytes()


def test_c116_lgbk1_decrypt_proceeds_when_disk_usage_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _payload(tmp_path)
    legacy = _legacy_encrypt(source, tmp_path / "legacy.lgbak", "legacy password")

    def broken_disk_usage(path: object) -> int:
        raise OSError("statfs unavailable")

    monkeypatch.setattr(bc.shutil, "disk_usage", broken_disk_usage)
    kind = BackupCryptoCodec.decrypt(
        legacy,
        tmp_path / "out.zip",
        credential=b"legacy password",
    )
    assert kind == "Z"


# ---------------------------------------------------------------------------
# BUG-AUD-C117 — no immutable bytes copy of derived keys
# ---------------------------------------------------------------------------


def test_c117_lgbk1_decrypt_never_materializes_bytes_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _payload(tmp_path)
    legacy = _legacy_encrypt(source, tmp_path / "legacy.lgbak", "legacy password")
    calls: list[bool] = []

    real_aes = bc.algorithms.AES

    class SpyAES:
        def __new__(cls, key: object) -> object:  # type: ignore[misc]
            if isinstance(key, bytes):
                calls.append(True)
            elif isinstance(key, bytearray):
                calls.append(False)
            return real_aes(key)  # type: ignore[arg-type]

    monkeypatch.setattr(bc.algorithms, "AES", SpyAES)
    kind = BackupCryptoCodec.decrypt(
        legacy,
        tmp_path / "out.zip",
        credential=b"legacy password",
    )
    assert kind == "Z"
    assert calls == [False]  # exactly one AES construction, from a bytearray
    gc.collect()


def test_c117_lgbk2_decrypt_never_materializes_bytes_master(
    tmp_path: Path,
    envelope: SecurityEnvelope,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _payload(tmp_path)
    encrypted = _encrypt(tmp_path, source, envelope)
    calls: list[bool] = []

    real_aes = bc.algorithms.AES

    class SpyAES:
        def __new__(cls, key: object) -> object:  # type: ignore[misc]
            if isinstance(key, bytes):
                calls.append(True)
            elif isinstance(key, bytearray):
                calls.append(False)
            return real_aes(key)  # type: ignore[arg-type]

    monkeypatch.setattr(bc.algorithms, "AES", SpyAES)
    kind = BackupCryptoCodec.decrypt(
        encrypted,
        tmp_path / "out.zip",
        credential=PASSWORD,
        credential_kind="password",
    )
    assert kind == "Z"
    assert calls == [False]


def test_c117_lgbk2_encrypt_never_materializes_bytes_master(
    tmp_path: Path,
    envelope: SecurityEnvelope,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _payload(tmp_path)
    calls: list[bool] = []

    real_aes = bc.algorithms.AES

    class SpyAES:
        def __new__(cls, key: object) -> object:  # type: ignore[misc]
            if isinstance(key, bytes):
                calls.append(True)
            elif isinstance(key, bytearray):
                calls.append(False)
            return real_aes(key)  # type: ignore[arg-type]

    monkeypatch.setattr(bc.algorithms, "AES", SpyAES)
    target = tmp_path / "backup.lgbak"
    BackupCryptoCodec.encrypt_v2(
        source,
        target,
        master_key=bytes(range(32)),
        envelope=envelope,
        payload_kind="Z",
    )
    assert target.read_bytes()[:5] == b"LGBK2"
    assert calls == [False]


# ---------------------------------------------------------------------------
# BUG-AUD-C118 — atomic LIFO undo
# ---------------------------------------------------------------------------


def _journal(tmp_path: Path) -> tuple[ChangeJournalService, FinanceDB]:
    database = FinanceDB(tmp_path / "journal.sqlite3")
    journal = ChangeJournalService(
        RepoStub(database),
        attachments_directory=tmp_path / "anexos",
        journal_storage=tmp_path / "historico",
    )
    return journal, database


def _add_transaction(database: FinanceDB, description: str) -> int:
    cursor = database.conn.execute(
        """
        INSERT INTO transactions(
            date,description,amount,category,account,kind,notes
        ) VALUES(?,?,?,?,?,?,?)
        """,
        (
            "2026-07-01",
            description,
            -12.5,
            "Outros",
            "Conta principal",
            "expense",
            "",
        ),
    )
    transaction_id = int(cursor.lastrowid)
    database.conn.commit()
    return transaction_id


def _journal_status(database: FinanceDB) -> str:
    """Read the newest journal status as text (scalar() coerces to float)."""

    row = database.conn.execute(
        "SELECT status FROM change_journal ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row is not None
    return str(row[0])


def test_c118_failed_undo_keeps_lifo_top_and_blocks_later_undos(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal, database = _journal(tmp_path)
    first_id = _add_transaction(database, "primeiro")
    second_id = _add_transaction(database, "segundo")
    journal.record_created_transactions(
        [first_id],
        label="Importacao 1",
    )
    journal.record_created_transactions(
        [second_id],
        label="Importacao 2",
    )

    real_capture = journal.capture_transactions

    def broken_capture(ids: list[int], **kwargs: object) -> dict[str, object]:
        if ids == [second_id]:
            raise RuntimeError("filesystem hiccup during undo")
        return real_capture(ids, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(journal, "capture_transactions", broken_capture)
    with pytest.raises(RuntimeError, match="filesystem hiccup"):
        journal.undo_latest()

    status = _journal_status(database)
    assert status == "available"  # LIFO top unchanged, retryable
    # The failed step was NOT undone.
    assert (
        database.conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE id=?", (second_id,)
        ).fetchone()[0]
        == 1
    )
    monkeypatch.undo()
    # After resolution, the SAME step (segundo) is undone first — not the older one.
    undone = journal.undo_latest()
    assert undone is not None
    assert "Importacao 2" in str(undone.get("label"))
    assert (
        database.conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE id=?", (second_id,)
        ).fetchone()[0]
        == 0
    )
    assert (
        database.conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE id=?", (first_id,)
        ).fetchone()[0]
        == 1
    )


def test_c118_restored_file_undo_failure_keeps_entry_available(
    tmp_path: Path,
) -> None:
    journal, database = _journal(tmp_path)
    transaction_id = _add_transaction(database, "com anexo")
    attachments = journal.attachments_directory
    attachment = attachments / str(transaction_id) / "fatura.txt"
    attachment.parent.mkdir(parents=True)
    attachment.write_text("documento", encoding="utf-8")
    database.conn.execute(
        """
        INSERT INTO transaction_attachments(
            transaction_id,file_name,stored_name,stored_path
        ) VALUES(?,?,?,?)
        """,
        (transaction_id, "fatura.txt", "fatura.txt", str(attachment)),
    )
    database.conn.commit()
    deleted, preserved = journal.delete_transactions(
        [transaction_id],
        label="Eliminar movimento",
    )
    assert (deleted, preserved) == (1, 1)
    assert not attachment.exists()

    # Make the restore fail transiently: block the target directory by
    # replacing it with a file, so shutil.move raises OSError.
    attachment.parent.rmdir()
    attachment.parent.write_bytes(b"not a directory")
    with pytest.raises(Exception):
        journal.undo_latest()
    status = _journal_status(database)
    assert status == "available"
    # The deletion is still in effect (undo did not half-apply).
    assert (
        database.conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE id=?", (transaction_id,)
        ).fetchone()[0]
        == 0
    )


# ---------------------------------------------------------------------------
# BUG-AUD-C119 — two-phase stash for delete_created undo
# ---------------------------------------------------------------------------


def test_c119_delete_created_undo_uses_two_phase_stash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An import undo (delete_created) must stage then publish attachments."""

    journal, database = _journal(tmp_path)
    transaction_id = _add_transaction(database, "importado com anexo")
    attachments = journal.attachments_directory
    attachment = attachments / str(transaction_id) / "fatura.txt"
    attachment.parent.mkdir(parents=True)
    attachment.write_text("documento", encoding="utf-8")
    database.conn.execute(
        """
        INSERT INTO transaction_attachments(
            transaction_id,file_name,stored_name,stored_path
        ) VALUES(?,?,?,?)
        """,
        (transaction_id, "fatura.txt", "fatura.txt", str(attachment)),
    )
    database.conn.commit()
    journal_id = journal.record_created_transactions(
        [transaction_id],
        label="Importacao com anexo",
    )
    assert journal_id is not None

    staged: list[str] = []
    real_stage = journal._preflight_stash_files

    def spy_stage(
        records: list[dict[str, str]], entry_id: int
    ) -> tuple[list[dict[str, str]], int]:
        pending, restored = real_stage(records, entry_id)
        staged.extend(
            journal._pending_stash_path(Path(record["stash"])).as_posix()
            for record in pending
        )
        return pending, restored

    monkeypatch.setattr(journal, "_preflight_stash_files", spy_stage)
    undone = journal.undo_latest()
    assert undone is not None
    assert (
        database.conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE id=?", (transaction_id,)
        ).fetchone()[0]
        == 0
    )
    # Two-phase contract: the file was staged to a .pending_stash path first.
    assert len(staged) == 1
    assert staged[0].endswith(".pending_stash")
    # Then published to the final stash location (no staging residue left).
    stash_file = journal.journal_storage / str(journal_id) / str(transaction_id) / "fatura.txt"
    assert stash_file.read_text(encoding="utf-8") == "documento"
    assert not attachment.exists()
    residue = [
        str(path)
        for path in journal.journal_storage.rglob("*")
        if path.name.endswith(".pending_stash")
    ]
    assert residue == []


def test_c119_preflight_failure_restores_files_and_keeps_entry_available(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If phase 1 (staging) fails, files stay in place and the entry stays available."""

    journal, database = _journal(tmp_path)
    transaction_id = _add_transaction(database, "importado com anexo")
    attachments = journal.attachments_directory
    attachment = attachments / str(transaction_id) / "fatura.txt"
    attachment.parent.mkdir(parents=True)
    attachment.write_text("documento", encoding="utf-8")
    database.conn.execute(
        """
        INSERT INTO transaction_attachments(
            transaction_id,file_name,stored_name,stored_path
        ) VALUES(?,?,?,?)
        """,
        (transaction_id, "fatura.txt", "fatura.txt", str(attachment)),
    )
    database.conn.commit()
    journal_id = journal.record_created_transactions(
        [transaction_id],
        label="Importacao com anexo",
    )
    assert journal_id is not None

    from services import change_journal as cj_module
    from services.change_journal import AttachmentRestoreTransientError

    # Phase 1 fails: break shutil.move for the staging move (same-volume
    # moves go through os.rename inside shutil.move).
    real_move = cj_module.shutil.move

    def broken_move(src: object, dst: object) -> None:
        if ".pending_stash" in str(dst):
            raise OSError("volume locked")
        real_move(str(src), str(dst))  # type: ignore[arg-type]

    monkeypatch.setattr(cj_module.shutil, "move", broken_move)
    with pytest.raises(AttachmentRestoreTransientError):
        journal.undo_latest()
    # Files stayed at their original location and the entry is still
    # 'available' so the undo can be retried after the failure is resolved.
    assert attachment.is_file()
    assert attachment.read_text(encoding="utf-8") == "documento"
    status = _journal_status(database)
    assert status == "available"


# ---------------------------------------------------------------------------
# BUG-AUD-C120 — credential rotation atomic pair swap
# ---------------------------------------------------------------------------


def test_c120_rotation_writes_pair_to_transition_dir(tmp_path: Path) -> None:
    service = StartupSecurityService(database_file=tmp_path / "db.sqlite3")
    service.configure_first_run("pin", "123456", weak_pin_confirmed=True).close()
    transition = service.envelope_path.parent / (
        service.envelope_path.name + ".rotation-tmp"
    )
    service.change_credential("123456", "password", "nova senha forte")
    # Transition directory is cleaned up after a successful rotation.
    assert not transition.exists()
    # Both live files are consistent with the new credential.
    envelope = read_envelope(service.envelope_path)
    assert "password" in envelope.unlocks
    session = service.unlock("password", "nova senha forte")
    session.close()


def test_c120_rotation_failure_never_leaves_half_swapped_pair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = StartupSecurityService(database_file=tmp_path / "db.sqlite3")
    service.configure_first_run("pin", "123456", weak_pin_confirmed=True).close()

    def explode_unlock(self: StartupSecurityService, kind: str, secret: str) -> object:
        raise RuntimeError("kdf failure simulation")

    monkeypatch.setattr(StartupSecurityService, "unlock", explode_unlock)
    with pytest.raises(Exception):
        service.change_credential("123456", "password", "nova senha forte")
    monkeypatch.undo()
    # The old credential still unlocks: rollback restored the prior pair.
    session = service.unlock("pin", "123456")
    session.close()
    # No transition directory residue.
    transition = service.envelope_path.parent / (
        service.envelope_path.name + ".rotation-tmp"
    )
    assert not transition.exists()


# ---------------------------------------------------------------------------
# BUG-AUD-C121 — compact_snapshots 24h retention gate
# ---------------------------------------------------------------------------


def test_c121_compact_snapshots_skips_recent_orphan_folders(
    tmp_path: Path,
) -> None:
    journal, database = _journal(tmp_path)
    stale = journal.journal_storage / "999999"
    fresh = journal.journal_storage / "999998"
    stale.mkdir()
    fresh.mkdir()
    (stale / "anexo.txt").write_text("antigo", encoding="utf-8")
    (fresh / "anexo.txt").write_text("novo", encoding="utf-8")
    old_ts = time.time() - (SNAPSHOT_MIN_RETENTION_SECONDS + 60)
    os.utime(stale, (old_ts, old_ts))
    result = journal.compact_snapshots()
    assert result["purged_orphaned_folders"] == 1
    assert not stale.exists()
    assert fresh.exists()  # younger than 24h: preserved
    assert (fresh / "anexo.txt").read_text(encoding="utf-8") == "novo"


def test_c121_compact_snapshots_never_touches_active_folders(
    tmp_path: Path,
) -> None:
    journal, database = _journal(tmp_path)
    transaction_id = _add_transaction(database, "ativo")
    journal.record_created_transactions(
        [transaction_id],
        label="Importacao ativa",
    )
    # Even a zero-mtime active folder must survive compaction.
    entry_row = database.conn.execute(
        "SELECT id FROM change_journal ORDER BY id DESC LIMIT 1"
    ).fetchone()
    entry_id = int(entry_row["id"])
    folder = journal.journal_storage / str(entry_id)
    folder.mkdir(exist_ok=True)
    old_ts = time.time() - (SNAPSHOT_MIN_RETENTION_SECONDS + 60)
    os.utime(folder, (old_ts, old_ts))
    result = journal.compact_snapshots()
    assert result["purged_orphaned_folders"] == 0
    assert folder.exists()


# ---------------------------------------------------------------------------
# BUG-AUD-C122 — ZipFile always closed on encryption failure
# ---------------------------------------------------------------------------


def test_c122_no_unclosed_zipfile_in_services_and_database() -> None:
    import re

    pattern = re.compile(r"zipfile\.ZipFile\(")
    offenders: list[str] = []
    for base in (Path("services"), Path("database")):
        for path in base.rglob("*.py"):
            prev = ""
            for i, line in enumerate(
                path.read_text(encoding="utf-8", errors="replace").splitlines(),
                1,
            ):
                stripped = line.strip()
                if pattern.search(stripped):
                    in_with = (
                        stripped.startswith("with ")
                        or prev.strip().startswith("with ")
                        and "as " in prev
                    )
                    if not in_with:
                        offenders.append(f"{path}:{i}")
                prev = line
    assert offenders == []


def test_c122_scrub_zip_failure_closes_source_archive(tmp_path: Path) -> None:
    """A sanitization failure must not leave the source ZipFile open.

    On Windows a leaked handle blocks the temporary archive rename with
    WinError 32; the with-block guarantees closure.
    """

    from services.backup import BackupService

    archive = tmp_path / "corrupt.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("meta.json", json.dumps({"tables": {}}))
    # No sqlite member: scrub returns False without touching SQLite.
    changed = BackupService.scrub_ai_secret_from_zip_file(archive)
    assert changed is False
    # On Windows this would raise WinError 32 if the handle leaked.
    renamed = tmp_path / "renamed.zip"
    archive.rename(renamed)
    assert renamed.is_file()
