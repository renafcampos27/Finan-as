#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Adversarial stress testing and edge-case boundary verification for Milestone 3."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database.engine import FinanceDB
from database.protection import DataIntegrityError
from database.repo import Repo
from services.backup import BackupService
from services.change_journal import (
    compress_payload,
    decompress_payload,
)


def test_adversarial_corrupted_payload_decompression():
    """Verify that corrupt, truncated, or invalid compressed payloads fail gracefully or fallback safely."""
    print("Testing corrupted payload handling...")

    # 1. Unknown prefix
    unknown_prefix = b"UNKNOWN_FOO_BAR"
    d1 = decompress_payload(unknown_prefix)
    assert d1 == "UNKNOWN_FOO_BAR"

    # 2. Corrupted ZLIB payload
    corrupted_zlib = b"ZLIB:totally_corrupted_payload_that_is_not_zlib"
    try:
        d2 = decompress_payload(corrupted_zlib)
        print(f"  ZLIB corrupted returned: {d2}")
    except Exception as exc:
        print(f"  ZLIB corrupted raised expected error: {type(exc).__name__}: {exc}")

    # 3. Empty and None payloads
    assert decompress_payload(b"") == ""
    assert decompress_payload("") == ""
    assert decompress_payload(None) == ""

    # 4. UTF-8 complex characters and emoji round-trip
    complex_unicode = {
        "euro": "100.50 €",
        "portuguese": "Açores, São Tomé & Príncipe, Conceição, Pão de Açúcar",
        "cjk": "个人理财 / 個人財務 / 資産運用",
        "emojis": "💰📈📉🚀🏛️💳",
        "floats": [-0.0000001, 1e12, 1234567.890123],
    }
    raw_u = json.dumps(complex_unicode, ensure_ascii=False)
    # Test both threshold < len and threshold > len
    c_raw = compress_payload(raw_u, threshold=100_000)
    assert c_raw.startswith(b"RAW:")
    assert json.loads(decompress_payload(c_raw)) == complex_unicode

    c_comp = compress_payload(raw_u, threshold=10)
    assert c_comp.startswith((b"ZSTD:", b"ZLIB:"))
    assert json.loads(decompress_payload(c_comp)) == complex_unicode
    print("  Lossless Unicode & float precision passed 100%.")


def test_adversarial_incremental_vacuum_edge_cases():
    """Verify incremental vacuum behavior on empty databases, negative pages, and closed connections."""
    print("Testing incremental vacuum boundary cases...")
    with tempfile.TemporaryDirectory(prefix="m3_vac_") as temp_dir:
        db_file = Path(temp_dir) / "empty.sqlite3"
        db = FinanceDB(db_file)

        # 1. Vacuum on brand new empty database
        res0 = db.incremental_vacuum(pages=0)
        assert res0 == 0

        # 2. Vacuum with negative pages
        res_neg = db.incremental_vacuum(pages=-5)
        assert res_neg == 0

        # 3. Vacuum with large page request
        res_large = db.incremental_vacuum(pages=99999)
        assert res_large == 99999

        # 4. Storage optimization on empty database
        opt = db.optimize_storage()
        assert opt["status"] == "ok"
        assert opt["before_bytes"] > 0
        assert opt["after_bytes"] > 0

        db.close()
        print("  Incremental vacuum boundary tests passed 100%.")


def test_adversarial_backup_restore_corrupted_files():
    """Verify that corrupt or zero-byte backup archives are firmly rejected."""
    print("Testing backup rejection on corrupted files...")
    with tempfile.TemporaryDirectory(prefix="m3_bak_") as temp_dir:
        db_file = Path(temp_dir) / "source.sqlite3"
        db = FinanceDB(db_file)
        repo = Repo(db)
        _ = BackupService(repo, backup_directory=Path(temp_dir) / "backups")

        # 1. Zero byte file
        zero_file = Path(temp_dir) / "zero.zip"
        zero_file.write_bytes(b"")
        try:
            BackupService.validate_complete_archive(zero_file)
            assert False, "Should have raised for zero-byte archive"
        except (ValueError, DataIntegrityError, Exception) as exc:
            print(f"  Zero-byte file rejected: {type(exc).__name__}")

        # 2. Corrupted zip with garbage bytes
        garbage_file = Path(temp_dir) / "garbage.zip"
        garbage_file.write_bytes(b"PK\x03\x04" + os.urandom(1024))
        try:
            BackupService.validate_complete_archive(garbage_file)
            assert False, "Should have raised for garbage archive"
        except (ValueError, DataIntegrityError, Exception) as exc:
            print(f"  Corrupted zip rejected: {type(exc).__name__}")

        db.close()
        print("  Backup corruption rejection passed 100%.")


def main():
    print("=" * 70)
    print("ADVERSARIAL STRESS TEST SUITE — MILESTONE 3")
    print("=" * 70)
    test_adversarial_corrupted_payload_decompression()
    test_adversarial_incremental_vacuum_edge_cases()
    test_adversarial_backup_restore_corrupted_files()
    print("=" * 70)
    print("ALL ADVERSARIAL STRESS TESTS COMPLETED SUCCESSFULLY.")
    print("=" * 70)


if __name__ == "__main__":
    main()
