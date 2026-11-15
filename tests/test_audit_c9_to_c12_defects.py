"""Comprehensive regression and boundary test suite for defects C9 through C12.

Audit items:
- C9 [P1 release]: Make release validation enforce its claims (strict candidate gate,
  cross-root version isolation, unreleased heading handling, per-package provenance).
- C10 / R1 [P1]: Restore failure does not delete untouched preferences.
- C11 / R2 [P1]: Partial directory copy does not prevent restore rollback.
- C12 / R3 [P2]: Partial attachment undo compensation and retryability in ChangeJournal.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

from database.engine import FinanceDB
from database.protection import (
    DataPreservationError,
    FinanceDatabaseProtection,
)
from database.repo.main_repo import Repo
from scripts.release_gate import (
    _extract_headings,
    verify_release_gate,
)
from services.change_journal import (
    AttachmentCompensationError,
    AttachmentRestoreTransientError,
    ChangeJournalService,
)


def _write_file_hash(path: Path) -> str:
    hasher = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            hasher.update(chunk)
    return hasher.hexdigest().upper()


# ============================================================================
# C9: Release validation gate enforcement & claims
# ============================================================================


def _create_synthetic_project(
    root: Path,
    version: str = "3.50.0",
    release_version: str = "V3.50",
    headings: list[tuple[str, str, str]] | None = None,
) -> Path:
    utils_dir = root / "utils"
    utils_dir.mkdir(parents=True, exist_ok=True)
    (utils_dir / "__init__.py").write_text("", encoding="utf-8")
    (utils_dir / "version.py").write_text(
        f'APP_VERSION = "{version}"\n'
        f'APP_VERSION_LABEL = "v{version}"\n'
        f'RELEASE_VERSION = "{release_version}"\n'
        f'RELEASE_DATE = "2026-09-05"\n',
        encoding="utf-8",
    )

    if headings is None:
        headings = [(release_version, version, "release notes heading")]

    notes_lines = ["# Release Notes\n\n"]
    for rel_v, v, desc in headings:
        notes_lines.append(f"## {rel_v} — v{v} — {desc}\n\nContent here.\n\n")

    (root / "RELEASE_NOTES.md").write_text("".join(notes_lines), encoding="utf-8")
    (root / "RELEASE_NOTES_RESUME.md").write_text("".join(notes_lines), encoding="utf-8")
    (root / "release").mkdir(parents=True, exist_ok=True)
    for name in ("main.py", "requirements.txt", "LiquidGlass.spec"):
        (root / name).write_text("synthetic build input", encoding="utf-8")
    (root / ".gitignore").write_text("release/\n", encoding="utf-8")
    for arguments in (
        ("init", "--quiet"),
        ("config", "user.name", "Synthetic release test"),
        ("config", "user.email", "release-test@example.invalid"),
        ("add", "."),
        ("-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "synthetic source"),
        ("-c", "tag.gpgsign=false", "tag", f"v{version}"),
    ):
        subprocess.run(["git", *arguments], cwd=root, check=True, capture_output=True, timeout=15)
    return root


def _create_synthetic_package(
    release_dir: Path,
    file_name: str,
    manifest_version: str = "3.50.0",
    source_commit: str | None = None,
    install_test_passed: bool = True,
    create_sidecar: bool = True,
    corrupt_sidecar: bool = False,
    omit_manifest: bool = False,
) -> Path:
    from scripts.package_integrity import inventory_digest, sha256_file

    root = release_dir.parent
    if source_commit is None:
        source_commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True, timeout=15
        ).strip()
    for name in ("main.py", "requirements.txt", "LiquidGlass.spec"):
        (root / name).write_text("synthetic build input", encoding="utf-8")
    source_entries = [
        {"path": name, "size": (root / name).stat().st_size, "sha256": sha256_file(root / name)}
        for name in ("main.py", "requirements.txt", "LiquidGlass.spec", "utils/version.py")
    ]
    bundle_root = root / "synthetic_bundle" / "_internal"
    evidence = {
        "ok": True, "frozen": True, "application_version": manifest_version,
        "database_quick_check": "ok", "page_exports_loaded": 30, "numpy": "2.5.2",
        "functional_checks": {
            "numpy_simulation": {"passed": True, "median": 1000.0, "rows": 4, "simulations": 200},
            "numpy_health": {"passed": True, "score": 50, "rows": 4, "simulations": 200},
            **{name: {"passed": True, "format": format_name, "restored_marker": "synthetic-acceptance", "restored_transactions": 12, "wrong_password_rejected": True, "truncated_rejected": True, "restored_attachment_sha256": "a" * 64, **({"kdf_algorithm": "argon2id"} if format_name == "LGBK2" else {})} for name, format_name in (("lgbk1_roundtrip", "LGBK1"), ("lgbk2_roundtrip", "LGBK2"))},
            "window_open_close": {"passed": True, "shown": True, "database_closed": True},
        },
        "bundle_root": str(bundle_root),
        "bundle_module_paths": {name: str(bundle_root / name / "__init__.pyc") for name in ("numpy", "argon2")},
    }
    contents = {"app/dummy.exe": b"dummy binary content", "evidence/self_test.json": json.dumps(evidence).encode()}
    entries = [{"path": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()} for name, data in contents.items()]
    pkg_path = release_dir / file_name
    with zipfile.ZipFile(pkg_path, "w") as zf:
        for name, data in contents.items():
            zf.writestr(name, data)
        if not omit_manifest:
            manifest_data = {
                "version": manifest_version,
                "source": {
                    "kind": "git_release_source", "dirty": False,
                    "commit": source_commit,
                    "tag": f"v{manifest_version}",
                    "snapshot_directory": str(root), "entries": source_entries,
                    "inventory_sha256": inventory_digest(source_entries),
                },
                "executable": "app/dummy.exe", "executable_sha256": entries[0]["sha256"],
                "self_test": {"passed": install_test_passed, "report": "evidence/self_test.json"},
                "package_integrity": {"algorithm": "SHA-256", "file_count": len(entries), "entries": entries},
                "installation": {
                    "install_test": {
                        "passed": install_test_passed,
                    }
                },
            }
            zf.writestr("RELEASE_MANIFEST.json", json.dumps(manifest_data))

    if create_sidecar:
        sidecar_path = pkg_path.with_suffix(".zip.sha256.txt")
        sha = _write_file_hash(pkg_path)
        if corrupt_sidecar:
            sha = "0000000000000000000000000000000000000000000000000000000000000000"
        sidecar_path.write_text(f"{sha}  {file_name}\n", encoding="ascii")

    return pkg_path


def test_c9_module_cache_isolation_across_roots(tmp_path: Path):
    """Loading version metadata from explicit roots must not cross-contaminate or read sys.modules."""
    root_a = _create_synthetic_project(tmp_path / "project_a", version="1.0.0", release_version="V1.0")
    root_b = _create_synthetic_project(tmp_path / "project_b", version="2.0.0", release_version="V2.0")

    rep_a = verify_release_gate(project_root=root_a, strict=False)
    rep_b = verify_release_gate(project_root=root_b, strict=False)

    assert rep_a["version"] == "1.0.0"
    assert rep_b["version"] == "2.0.0"
    assert rep_a["version"] != rep_b["version"]


def test_c9_unreleased_headings_parser_and_handling(tmp_path: Path):
    """Heading parser intentionally detects unreleased markers and handles them in strict/diagnostic mode."""
    root = _create_synthetic_project(
        tmp_path / "proj",
        version="3.50.0",
        release_version="V3.50",
        headings=[
            ("V3.50", "3.50.0-unreleased", "trabalho em curso"),
            ("V3.49", "3.49.0", "lançamento anterior"),
        ],
    )
    headings = _extract_headings(root / "RELEASE_NOTES.md")
    assert len(headings) == 2
    assert headings[0]["is_unreleased"] is True
    assert headings[0]["version"] == "3.50.0-unreleased"
    assert headings[1]["is_unreleased"] is False
    assert headings[1]["version"] == "3.49.0"

    # Diagnostic mode: reports unreleased check without hard error
    rep_diag = verify_release_gate(project_root=root, strict=False)
    assert rep_diag["ok"] is True
    assert any("is unreleased" in c for c in rep_diag["checks_passed"])

    # Strict mode: rejects unreleased heading unless allow_unreleased=True
    _create_synthetic_package(root / "release", "LiquidGlass_App_v3.50.0.zip", manifest_version="3.50.0")
    rep_strict = verify_release_gate(project_root=root, strict=True, allow_unreleased=False)
    assert rep_strict["ok"] is False
    assert any("marked unreleased" in e for e in rep_strict["errors"])

    rep_allowed = verify_release_gate(project_root=root, strict=True, allow_unreleased=True)
    # The fixture supplies real clean/tagged provenance; only the heading policy
    # is being bypassed by this explicit development-only override.
    assert not any("marked unreleased" in e for e in rep_allowed["errors"])


def test_c9_strict_mode_rejects_missing_candidate(tmp_path: Path):
    """Strict mode must fail if no candidate package matching expected version exists."""
    root = _create_synthetic_project(tmp_path / "proj", version="3.50.0")
    # Release folder empty
    report = verify_release_gate(project_root=root, strict=True)
    assert report["ok"] is False
    assert any("No valid release candidate package found" in e for e in report["errors"])


def test_c9_strict_mode_rejects_missing_or_corrupt_sidecar(tmp_path: Path):
    """Strict mode must fail if candidate sidecar is missing or corrupt."""
    root = _create_synthetic_project(tmp_path / "proj", version="3.50.0")
    release_dir = root / "release"

    # 1. Missing sidecar
    _create_synthetic_package(release_dir, "LiquidGlass_App_v3.50.0.zip", create_sidecar=False)
    rep_missing = verify_release_gate(project_root=root, strict=True)
    assert rep_missing["ok"] is False
    assert any("missing or invalid SHA-256 sidecar" in e for e in rep_missing["errors"])

    # 2. Corrupt sidecar
    _create_synthetic_package(release_dir, "LiquidGlass_App_v3.50.0.zip", create_sidecar=True, corrupt_sidecar=True)
    rep_corrupt = verify_release_gate(project_root=root, strict=True)
    assert rep_corrupt["ok"] is False
    assert any("Checksum mismatch" in e for e in rep_corrupt["errors"])


def test_c9_strict_mode_rejects_missing_manifest_or_failed_selftest(tmp_path: Path):
    """Strict mode must fail if candidate lacks manifest or self-test passed flag."""
    root = _create_synthetic_project(tmp_path / "proj", version="3.50.0")
    release_dir = root / "release"

    # Missing manifest
    _create_synthetic_package(release_dir, "LiquidGlass_App_v3.50.0.zip", omit_manifest=True)
    rep_no_manifest = verify_release_gate(project_root=root, strict=True)
    assert rep_no_manifest["ok"] is False
    assert any("missing RELEASE_MANIFEST.json" in e for e in rep_no_manifest["errors"])

    # Manifest self-test not passed
    _create_synthetic_package(release_dir, "LiquidGlass_App_v3.50.0.zip", install_test_passed=False)
    rep_selftest_fail = verify_release_gate(project_root=root, strict=True)
    assert rep_selftest_fail["ok"] is False
    assert any("no verified install self-test evidence" in e for e in rep_selftest_fail["errors"])


def test_c9_preserves_historical_packages(tmp_path: Path):
    """Historical packages in release/ do not cause candidate gate failure when candidate is valid."""
    root = _create_synthetic_project(tmp_path / "proj", version="3.50.0")
    release_dir = root / "release"

    # Historical package v3.48.0
    _create_synthetic_package(release_dir, "LiquidGlass_App_v3.48.0.zip", manifest_version="3.48.0")
    # Current candidate package v3.50.0
    _create_synthetic_package(release_dir, "LiquidGlass_App_v3.50.0.zip", manifest_version="3.50.0")

    rep = verify_release_gate(project_root=root, strict=True)
    assert rep["candidate_package"] == "LiquidGlass_App_v3.50.0.zip"
    assert rep["ok"] is True
    assert len(rep["packages"]) == 2


# ============================================================================
# C10 / R1: Restore failure preserves untouched live preferences
# ============================================================================


def _create_valid_restore_stage(path: Path, label: bytes) -> None:
    import sqlite3

    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE restored_payload(value BLOB)")
        connection.execute("INSERT INTO restored_payload VALUES(?)", (label,))
        connection.commit()
    finally:
        connection.close()


def test_r1_restore_failure_before_preferences_preserves_live_preferences(tmp_path: Path):
    """If restore fails during DB install or directory copy, untouched live preferences must NOT be deleted."""
    db_file = tmp_path / "finance.db"
    pref_file = tmp_path / "preferences.json"
    att_dir = tmp_path / "attachments"
    hist_dir = tmp_path / "history"

    db_file.write_bytes(b"ORIGINAL_DB_V1")
    pref_bytes = b'{"theme": "dark", "locale": "pt_PT", "preserve": true}'
    pref_file.write_bytes(pref_bytes)
    att_dir.mkdir()
    (att_dir / "file1.txt").write_text("orig att", encoding="utf-8")
    hist_dir.mkdir()

    guard = FinanceDatabaseProtection(
        database_file=db_file,
        preferences_file=pref_file,
        attachments_directory=att_dir,
        history_directory=hist_dir,
        protection_directory=tmp_path / "backups",
    )

    # Prepare staged content
    staged_db = tmp_path / "staged_db.sqlite"
    _create_valid_restore_stage(staged_db, b"STAGED_NEW_DB")
    staged_att = tmp_path / "staged_att"
    staged_att.mkdir()
    (staged_att / "new.txt").write_text("new att", encoding="utf-8")
    staged_hist = tmp_path / "staged_hist"
    staged_hist.mkdir()
    staged_pref = tmp_path / "staged_pref.json"
    staged_pref.write_bytes(b'{"theme": "light"}')

    # Inject failure during directory staging/copy
    with patch("shutil.copytree", side_effect=OSError("Simulated disk error on attachment copy")):
        with pytest.raises(OSError, match="Simulated disk error"):
            guard._install_staged_restore(
                staged_database=staged_db,
                staged_attachments=staged_att,
                staged_history=staged_hist,
                staged_preferences=staged_pref,
                preferences_present=True,
            )

    # Live preferences file MUST exist and retain exact original bytes!
    assert pref_file.exists(), "Untouched preferences file was erroneously unlinked!"
    assert pref_file.read_bytes() == pref_bytes, "Preferences content was corrupted or modified!"
    # Original database rolled back
    assert db_file.read_bytes() == b"ORIGINAL_DB_V1"


def test_r1_restore_rollback_restores_moved_preferences_if_late_failure(tmp_path: Path):
    """If preferences were moved and subsequent steps fail, rollback must restore original preferences."""
    db_file = tmp_path / "finance.db"
    pref_file = tmp_path / "preferences.json"
    att_dir = tmp_path / "attachments"
    hist_dir = tmp_path / "history"

    db_file.write_bytes(b"ORIGINAL_DB")
    orig_pref = b'{"key": "original_preferences"}'
    pref_file.write_bytes(orig_pref)
    att_dir.mkdir()
    hist_dir.mkdir()

    guard = FinanceDatabaseProtection(
        database_file=db_file,
        preferences_file=pref_file,
        attachments_directory=att_dir,
        history_directory=hist_dir,
        protection_directory=tmp_path / "backups",
    )

    staged_db = tmp_path / "staged_db.sqlite"
    _create_valid_restore_stage(staged_db, b"STAGED_DB")
    staged_att = tmp_path / "staged_att"
    staged_att.mkdir()
    staged_hist = tmp_path / "staged_hist"
    staged_hist.mkdir()
    staged_pref = tmp_path / "staged_pref.json"
    staged_pref.write_bytes(b'{"key": "new_preferences"}')

    # Inject failure specifically when replacing live preferences with candidate
    real_os_replace = os.replace

    def selective_preference_replace(src, dst, *args, **kwargs):
        if ".restore" in str(src) and "preferences" in str(dst):
            raise OSError("Simulated failure replacing preferences with candidate")
        return real_os_replace(src, dst, *args, **kwargs)

    with patch("os.replace", side_effect=selective_preference_replace):
        with pytest.raises(OSError, match="Simulated failure replacing preferences"):
            guard._install_staged_restore(
                staged_database=staged_db,
                staged_attachments=staged_att,
                staged_history=staged_hist,
                staged_preferences=staged_pref,
                preferences_present=True,
            )

    # Rollback must restore original preferences and database
    assert pref_file.exists()
    assert pref_file.read_bytes() == orig_pref
    assert db_file.exists()
    assert db_file.read_bytes() == b"ORIGINAL_DB"


# ============================================================================
# C11 / R2: Partial directory copy does not prevent restore rollback
# ============================================================================


def test_r2_partial_directory_copy_permits_clean_rollback(tmp_path: Path):
    """Candidate staging ensures directory copy failures leave live directory untouched or cleanly restorable."""
    db_file = tmp_path / "finance.db"
    pref_file = tmp_path / "preferences.json"
    att_dir = tmp_path / "attachments"
    hist_dir = tmp_path / "history"

    db_file.write_bytes(b"ORIGINAL_DB")
    att_dir.mkdir()
    (att_dir / "invoice1.pdf").write_bytes(b"%PDF-1.4 invoice 1")
    (att_dir / "invoice2.pdf").write_bytes(b"%PDF-1.4 invoice 2")
    orig_hashes = {
        "invoice1.pdf": _write_file_hash(att_dir / "invoice1.pdf"),
        "invoice2.pdf": _write_file_hash(att_dir / "invoice2.pdf"),
    }
    hist_dir.mkdir()

    guard = FinanceDatabaseProtection(
        database_file=db_file,
        preferences_file=pref_file,
        attachments_directory=att_dir,
        history_directory=hist_dir,
        protection_directory=tmp_path / "backups",
    )

    staged_db = tmp_path / "staged_db.sqlite"
    _create_valid_restore_stage(staged_db, b"STAGED_DB")
    staged_att = tmp_path / "staged_att"
    staged_att.mkdir()
    (staged_att / "new_inv.pdf").write_bytes(b"%PDF-1.4 new invoice")
    staged_hist = tmp_path / "staged_hist"
    staged_hist.mkdir()

    # Fail during directory copy
    real_copytree = shutil.copytree

    def failing_copytree(src, dst, *args, **kwargs):
        if "attachments" in str(dst):
            raise OSError("Injected write failure halfway through copying attachments directory")
        return real_copytree(src, dst, *args, **kwargs)

    with patch("shutil.copytree", side_effect=failing_copytree):
        with pytest.raises(OSError, match="Injected write failure"):
            guard._install_staged_restore(
                staged_database=staged_db,
                staged_attachments=staged_att,
                staged_history=staged_hist,
                staged_preferences=None,
                preferences_present=False,
            )

    # Verify original directory was never broken and all file hashes match
    assert att_dir.is_dir()
    for fname, expected_hash in orig_hashes.items():
        file_path = att_dir / fname
        assert file_path.is_file()
        assert _write_file_hash(file_path) == expected_hash


def test_r2_rollback_failure_preserves_originals_and_raises_datapreservation(tmp_path: Path):
    """If rollback rename encounters an unrecoverable failure, originals are retained and DataPreservationError is raised."""
    db_file = tmp_path / "finance.db"
    pref_file = tmp_path / "preferences.json"
    att_dir = tmp_path / "attachments"
    hist_dir = tmp_path / "history"

    db_file.write_bytes(b"ORIGINAL_DB")
    att_dir.mkdir()
    (att_dir / "orig.txt").write_text("orig att", encoding="utf-8")
    hist_dir.mkdir()

    guard = FinanceDatabaseProtection(
        database_file=db_file,
        preferences_file=pref_file,
        attachments_directory=att_dir,
        history_directory=hist_dir,
        protection_directory=tmp_path / "backups",
    )

    staged_db = tmp_path / "staged_db.sqlite"
    _create_valid_restore_stage(staged_db, b"STAGED_DB")
    staged_att = tmp_path / "staged_att"
    staged_att.mkdir()
    (staged_att / "replacement.txt").write_text("replacement att", encoding="utf-8")
    staged_hist = tmp_path / "staged_hist"
    staged_hist.mkdir()

    # Trigger failure after target was moved, and also make rollback restore fail
    real_copy2 = shutil.copy2
    copy2_count = 0

    def failing_copy2(src, dst, *args, **kwargs):
        nonlocal copy2_count
        copy2_count += 1
        if copy2_count > 1:
            raise OSError("Fail during preferences copy")
        return real_copy2(src, dst, *args, **kwargs)

    real_os_replace = os.replace

    def selective_replace(src, dst, *args, **kwargs):
        if ".rollback" in str(src) and "attachments" in str(dst):
            raise OSError("Simulated locked destination during rollback")
        return real_os_replace(src, dst, *args, **kwargs)

    # Fail during preferences step to trigger rollback
    with patch("shutil.copy2", side_effect=failing_copy2):
        with patch("os.replace", side_effect=selective_replace):
            with pytest.raises(DataPreservationError, match="Falha no restauro e reversão incompleta"):
                guard._install_staged_restore(
                    staged_database=staged_db,
                    staged_attachments=staged_att,
                    staged_history=staged_hist,
                    staged_preferences=tmp_path / "pref.json",
                    preferences_present=True,
                )

    # Rollback files must be preserved for recovery
    rollback_dirs = list(tmp_path.glob("*.rollback"))
    assert len(rollback_dirs) >= 1, "Rollback files were wiped despite rollback failure!"


# ============================================================================
# C12 / R3: ChangeJournal partial attachment undo retryability
# ============================================================================


def test_r3_transient_attachment_restore_failure_is_retryable(tmp_path: Path):
    """Transient attachment failure during undo is compensated back to stash; journal entry remains retryable."""
    db_path = tmp_path / "finance.sqlite3"
    fdb = FinanceDB(db_path)
    repo = Repo(fdb)

    tx_id = repo.add_transaction(
        "2026-09-01",
        "Compra de teste",
        -80.0,
        "Geral",
        "Conta Principal",
    )

    att1 = tmp_path / "anexos" / str(tx_id) / "fatura1.pdf"
    att2 = tmp_path / "anexos" / str(tx_id) / "fatura2.pdf"
    att1.parent.mkdir(parents=True, exist_ok=True)
    att1.write_bytes(b"PDF 1 content")
    att2.write_bytes(b"PDF 2 content")

    repo.add_transaction_attachment(
        transaction_id=tx_id,
        file_name="fatura1.pdf",
        stored_name="fatura1.pdf",
        stored_path=str(att1),
        original_path=str(att1),
        file_type="application/pdf",
        description="Fatura 1",
        supplier="Fornecedor 1",
        invoice_date="2026-09-01",
        total_amount=40.0,
    )
    repo.add_transaction_attachment(
        transaction_id=tx_id,
        file_name="fatura2.pdf",
        stored_name="fatura2.pdf",
        stored_path=str(att2),
        original_path=str(att2),
        file_type="application/pdf",
        description="Fatura 2",
        supplier="Fornecedor 2",
        invoice_date="2026-09-01",
        total_amount=40.0,
    )

    journal = ChangeJournalService(
        repo=repo,
        attachments_directory=tmp_path / "anexos",
        journal_storage=tmp_path / "journal_storage",
    )

    # Delete transaction with 2 attachments
    journal.delete_transactions([tx_id], label="Apagar com 2 faturas")
    assert repo.transaction_by_id(tx_id) is None
    assert not att1.exists()
    assert not att2.exists()

    latest = journal.latest_available()
    assert latest is not None
    entry_id = latest["id"]

    # Inject failure on the second file move during _restore_files
    real_move = shutil.move
    call_count = 0

    def failing_move(src, dst):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise OSError("Simulated locked destination for fatura2.pdf")
        return real_move(src, dst)

    with patch("shutil.move", side_effect=failing_move):
        with pytest.raises(AttachmentRestoreTransientError, match="reposição parcial foi totalmente revertida"):
            journal.undo(entry_id)

    # 1. First attachment must be compensated back to stash
    assert not att1.exists(), "Attachment 1 was left in source directory despite failure on attachment 2!"
    assert not att2.exists()

    # 2. Database rows remain deleted
    assert repo.transaction_by_id(tx_id) is None

    # 3. Journal entry status remains 'available' (NOT 'failed'!) and is still offered for undo
    row = repo.db.conn.execute("SELECT status FROM change_journal WHERE id=?", (entry_id,)).fetchone()
    assert row[0] == "available", f"Status became {row[0]}, should remain 'available'!"

    available = journal.latest_available()
    assert available is not None
    assert available["id"] == entry_id

    # 4. Retry without the injected failure -> undo must succeed cleanly!
    undo_result = journal.undo(entry_id)
    assert undo_result["status"] == "undone"

    # Transaction and both files are fully restored!
    assert repo.transaction_by_id(tx_id) is not None
    assert att1.is_file() and att1.read_bytes() == b"PDF 1 content"
    assert att2.is_file() and att2.read_bytes() == b"PDF 2 content"

    fdb.close()


def test_r3_compensation_failure_marks_journal_failed(tmp_path: Path):
    """If compensation itself fails during rollback, AttachmentCompensationError is raised and status is 'failed'."""
    db_path = tmp_path / "finance2.sqlite3"
    fdb = FinanceDB(db_path)
    repo = Repo(fdb)

    tx_id = repo.add_transaction("2026-09-01", "Outra compra", -50.0, "Geral", "Conta Principal")
    att1 = tmp_path / "anexos" / str(tx_id) / "doc1.pdf"
    att2 = tmp_path / "anexos" / str(tx_id) / "doc2.pdf"
    att1.parent.mkdir(parents=True, exist_ok=True)
    att1.write_bytes(b"doc1")
    att2.write_bytes(b"doc2")

    repo.add_transaction_attachment(
        transaction_id=tx_id, file_name="doc1.pdf", stored_name="doc1.pdf",
        stored_path=str(att1), original_path=str(att1), file_type="application/pdf",
        description="Doc 1", supplier="Fornecedor 1", invoice_date="2026-09-01", total_amount=25.0,
    )
    repo.add_transaction_attachment(
        transaction_id=tx_id, file_name="doc2.pdf", stored_name="doc2.pdf",
        stored_path=str(att2), original_path=str(att2), file_type="application/pdf",
        description="Doc 2", supplier="Fornecedor 2", invoice_date="2026-09-01", total_amount=25.0,
    )

    journal = ChangeJournalService(
        repo=repo, attachments_directory=tmp_path / "anexos",
        journal_storage=tmp_path / "journal_storage",
    )
    journal.delete_transactions([tx_id], label="Apagar com 2 docs")
    entry_id = journal.latest_available()["id"]

    real_move = shutil.move
    call_count = 0

    def double_failing_move(src, dst):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            # Failure restoring file 2
            raise OSError("Error restoring file 2")
        if call_count == 3:
            # Failure compensating file 1 back to stash
            raise OSError("Error rolling back file 1")
        return real_move(src, dst)

    with patch("shutil.move", side_effect=double_failing_move):
        with pytest.raises(AttachmentCompensationError, match="reversão parcial dos ficheiros falhou"):
            journal.undo(entry_id)

    # BUG-AUD-C118 contract: even when compensation itself fails, the
    # exception propagates but the entry is NEVER marked 'failed'.  It must
    # remain 'available' at the top of the LIFO so the failure stays visible
    # and blocks subsequent undos instead of silently advancing the pointer.
    row = repo.db.conn.execute("SELECT status FROM change_journal WHERE id=?", (entry_id,)).fetchone()
    assert row[0] == "available", f"Status became {row[0]}, C118 requires 'available'!"
    assert journal.latest_available() is not None
    assert journal.latest_available()["id"] == entry_id

    fdb.close()


def test_c9_candidate_selection_prefers_versioned_package_over_generic(tmp_path: Path):
    """When both LiquidGlass_App.zip and LiquidGlass_App_v3.50.0.zip exist, versioned candidate takes precedence."""
    root = _create_synthetic_project(tmp_path / "proj", version="3.50.0")
    release_dir = root / "release"

    # Generic unversioned package
    _create_synthetic_package(release_dir, "LiquidGlass_App.zip", manifest_version="3.50.0")
    # Canonical versioned package
    _create_synthetic_package(release_dir, "LiquidGlass_App_v3.50.0.zip", manifest_version="3.50.0")

    rep = verify_release_gate(project_root=root, strict=True)
    assert rep["candidate_package"] == "LiquidGlass_App_v3.50.0.zip"
    assert rep["ok"] is True


def test_c9_unreleased_top_heading_verifies_target_version_heading(tmp_path: Path):
    """When top heading is unreleased for next version, verify_release_gate verifies the target version's heading."""
    # 1. Target version exists below unreleased heading
    root1 = _create_synthetic_project(
        tmp_path / "proj1",
        version="3.49.0",
        release_version="V3.49",
        headings=[
            ("V3.50", "3.50.0-unreleased", "trabalho em curso"),
            ("V3.49", "3.49.0", "versão anterior válida"),
        ],
    )
    _create_synthetic_package(root1 / "release", "LiquidGlass_App_v3.49.0.zip", manifest_version="3.49.0")
    rep1 = verify_release_gate(project_root=root1, strict=True)
    assert rep1["ok"] is True
    assert any("matches expected version 3.49.0" in c for c in rep1["checks_passed"])

    # 2. Target version does NOT exist below unreleased heading
    root2 = _create_synthetic_project(
        tmp_path / "proj2",
        version="3.49.0",
        release_version="V3.49",
        headings=[
            ("V3.50", "3.50.0-unreleased", "trabalho em curso"),
            ("V3.48", "3.48.0", "versão antiga"),
        ],
    )
    _create_synthetic_package(root2 / "release", "LiquidGlass_App_v3.49.0.zip", manifest_version="3.49.0")
    rep2 = verify_release_gate(project_root=root2, strict=True)
    assert rep2["ok"] is False
    assert any("does not contain any heading for expected version 3.49.0" in e for e in rep2["errors"])


def test_r2_candidate_directory_cleaned_up_on_copytree_failure(tmp_path: Path):
    """If shutil.copytree fails midway, the candidate directory must be cleaned up and not left orphaned on disk."""
    db_file = tmp_path / "finance.db"
    pref_file = tmp_path / "preferences.json"
    att_dir = tmp_path / "attachments"
    hist_dir = tmp_path / "history"

    db_file.write_bytes(b"ORIGINAL_DB")
    att_dir.mkdir()
    (att_dir / "file.txt").write_text("orig", encoding="utf-8")
    hist_dir.mkdir()

    guard = FinanceDatabaseProtection(
        database_file=db_file,
        preferences_file=pref_file,
        attachments_directory=att_dir,
        history_directory=hist_dir,
        protection_directory=tmp_path / "backups",
    )

    staged_db = tmp_path / "staged_db.sqlite"
    _create_valid_restore_stage(staged_db, b"STAGED_DB")
    staged_att = tmp_path / "staged_att"
    staged_att.mkdir()
    (staged_att / "new_file.txt").write_text("new", encoding="utf-8")
    staged_hist = tmp_path / "staged_hist"
    staged_hist.mkdir()

    def partial_failing_copytree(src, dst, *args, **kwargs):
        # Simulate partial directory creation before crash
        Path(dst).mkdir(parents=True, exist_ok=True)
        (Path(dst) / "partial.tmp").write_bytes(b"partial content")
        raise OSError("Injected disk failure during directory copy")

    with patch("shutil.copytree", side_effect=partial_failing_copytree):
        with pytest.raises(OSError, match="Injected disk failure"):
            guard._install_staged_restore(
                staged_database=staged_db,
                staged_attachments=staged_att,
                staged_history=staged_hist,
                staged_preferences=None,
                preferences_present=False,
            )

    # Ensure NO candidate directories were left behind on disk
    candidate_dirs = list(tmp_path.glob("*.candidate"))
    assert len(candidate_dirs) == 0, f"Candidate directory leaked: {candidate_dirs}"
    assert att_dir.is_dir()
    assert (att_dir / "file.txt").read_text(encoding="utf-8") == "orig"
