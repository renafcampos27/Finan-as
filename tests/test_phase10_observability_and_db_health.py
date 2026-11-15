#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Test suite for Phase 10: Observability, Query Profiling, Telemetry & Physical DB Health."""

from __future__ import annotations

import logging
from pathlib import Path

from database.engine import FinanceDB
from services.backup import BackupService
from utils.app_logging import RedactingFormatter


def test_database_physical_integrity_verification(tmp_path: Path):
    """Verify check_physical_integrity runs quick_check, integrity_check and foreign_key_check."""
    db_file = tmp_path / "health_db" / "test_health.sqlite3"
    db_file.parent.mkdir(parents=True, exist_ok=True)
    db = FinanceDB(db_file)
    results = db.check_physical_integrity()

    assert results["integrity_ok"] is True
    assert results["quick_check_ok"] is True
    assert results["foreign_keys_ok"] is True
    assert len(results["errors"]) == 0
    db.conn.close()


def test_redacting_formatter_scrubs_pii_and_secrets():
    """Verify log formatter redacts IBANs, NIFs, API keys and amounts."""
    formatter = RedactingFormatter("%(message)s")
    record = logging.LogRecord(
        name="test_logger",
        level=logging.INFO,
        pathname="test.py",
        lineno=10,
        msg="Operacao para NIF 123456789 com IBAN PT50000201231234567890154 no valor de 1250.50 EUR e chave AIzaSyD1234567890abcdef12345",
        args=(),
        exc_info=None,
    )
    formatted = formatter.format(record)
    assert "[NIF]" in formatted
    assert "123456789" not in formatted
    assert "[IBAN]" in formatted
    assert "PT50000201231234567890154" not in formatted
    assert "[AMOUNT]" in formatted
    assert "1250.50" not in formatted
    assert "[REDACTED_API_KEY]" in formatted
    assert "AIzaSyD" not in formatted


def test_prune_invalid_and_corrupt_backups(tmp_path: Path):
    """Verify prune_invalid_and_corrupt_backups removes 0-byte and incomplete files."""
    db_dir = tmp_path / "db_isolated"
    db_dir.mkdir(parents=True, exist_ok=True)
    db = FinanceDB(db_dir / "app.sqlite3")

    backup_dir = tmp_path / "backups_isolated"
    backup_dir.mkdir(parents=True, exist_ok=True)

    class MockRepo:
        def __init__(self, database: FinanceDB):
            self.db = database

        def setting(self, key, default=""):
            return default

        def set_setting(self, key, value):
            pass

    repo = MockRepo(db)
    service = BackupService(repo, backup_directory=backup_dir)

    # Valid backup
    valid_backup = backup_dir / "financas_backup_20260801_120000.zip"
    valid_backup.write_bytes(b"PK\x03\x04valid zip content")

    # Corrupt 0-byte backup
    corrupt_zero = backup_dir / "financas_backup_20260802_130000.zip"
    corrupt_zero.write_bytes(b"")

    # Incomplete copy temp file
    temp_copy = backup_dir / ".financas_backup_20260803_140000.zip.copying"
    temp_copy.write_bytes(b"incomplete")

    pruned = service.prune_invalid_and_corrupt_backups()

    assert corrupt_zero.name in pruned
    assert temp_copy.name in pruned
    assert not corrupt_zero.exists()
    assert not temp_copy.exists()
    assert valid_backup.exists()
    db.conn.close()
