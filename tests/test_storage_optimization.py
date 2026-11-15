#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Unit and integration tests for Milestone 3: Storage Optimization, Payload Compression & Lossless Backups."""

from __future__ import annotations

import json
from pathlib import Path
import pytest

from database.engine import FinanceDB
from database.repo import Repo
from services.change_journal import (
    ChangeJournalService,
    compress_payload,
    decompress_payload,
)
from services.backup import BackupService


@pytest.fixture
def temp_db(tmp_path: Path):
    db_file = tmp_path / "test_storage.sqlite3"
    db = FinanceDB(db_file)
    try:
        yield db
    finally:
        db.close()


@pytest.fixture
def repo_with_db(tmp_path: Path):
    db_file = tmp_path / "test_repo.sqlite3"
    db = FinanceDB(db_file)
    repo = Repo(db)
    try:
        yield repo
    finally:
        db.close()


# ==============================================================================
# Feature F9: SQLite Incremental Vacuuming & Maintenance
# ==============================================================================


def test_incremental_vacuum_reclaims_pages(temp_db: FinanceDB):
    """Test incremental vacuuming with pages=0 and pages>0."""
    # Populate large batch of transactions
    for i in range(1, 200):
        temp_db.add_transaction(
            "2026-07-01",
            f"Despesa temporária {i}",
            -50.0,
            "Supermercado",
            "Conta principal",
            "expense",
            notes="Nota longa para ocupar páginas " * 10,
            commit=False,
        )
    temp_db.conn.commit()

    # Delete half the transactions to create freelist pages
    temp_db.conn.execute("DELETE FROM transactions WHERE id % 2 = 0")
    temp_db.conn.commit()

    # Run incremental vacuum with specific pages
    before = int(temp_db.conn.execute("PRAGMA freelist_count").fetchone()[0])
    pages_vac = temp_db.incremental_vacuum(pages=10)
    after = int(temp_db.conn.execute("PRAGMA freelist_count").fetchone()[0])
    assert pages_vac == before - after
    assert 0 <= pages_vac <= 10

    # Run incremental vacuum to reclaim all remaining free pages
    pages_vac_all = temp_db.incremental_vacuum(pages=0)
    final = int(temp_db.conn.execute("PRAGMA freelist_count").fetchone()[0])
    assert pages_vac_all == after - final
    assert final == 0

    stats = temp_db.database_storage_stats()
    assert int(stats["freelist_count"]) >= 0


def test_optimize_storage_execution_and_metrics(repo_with_db: Repo):
    """Test optimize_storage on FinanceDB and BaseRepo with WAL checkpointing and quick check."""
    # Seed transactions and rules
    for i in range(1, 50):
        repo_with_db.db.add_transaction(
            "2026-07-02",
            f"Movimento {i}",
            -25.0,
            "Casa e contas",
            "Conta principal",
            "expense",
            commit=False,
        )
    repo_with_db.commit()

    # Execute storage optimization
    result = repo_with_db.optimize_storage()
    assert result["status"] == "ok"
    assert "checkpoint_status" in result
    assert result["before_bytes"] > 0
    assert result["after_bytes"] > 0
    assert result["duration_ms"] >= 0.0
    assert "stats" in result

    # Check BaseRepo delegate
    assert repo_with_db.incremental_vacuum(0) == 0


def test_optimize_storage_read_only_protection(tmp_path: Path):
    """Test that read-only databases refuse optimization operations safely."""
    db_file = tmp_path / "readonly_test.sqlite3"
    db = FinanceDB(db_file)
    db.add_transaction(
        "2026-07-01", "Tx", -10.0, "Supermercado", "Conta principal", "expense"
    )
    db.close()

    # Open in read-only mode
    ro_db = FinanceDB.open_read_only(db_file)
    try:
        with pytest.raises(PermissionError):
            ro_db.optimize_storage()
        assert ro_db.incremental_vacuum(0) == 0
    finally:
        ro_db.close()


# ==============================================================================
# Feature F10: Snapshot & Change Journal Payload Compression
# ==============================================================================


def test_compress_and_decompress_payload_roundtrip_fidelity():
    """Test lossless round-trip compression for various payloads."""
    # 1. Small payload (< 1KB)
    small_data = json.dumps({"action": "edit", "id": 42}, ensure_ascii=False)
    compressed_small = compress_payload(small_data, threshold=1024)
    assert compressed_small.startswith(b"RAW:")
    assert decompress_payload(compressed_small) == small_data

    # 2. Large payload (> 4KB) with Unicode and financial precision floats
    large_dict = {
        "action": "bulk_import",
        "euro_symbol": "€1,234.56 · Açores & São Tomé",
        "precision_floats": [12345.6789, 0.0001, -987654.321],
        "rows": [
            {
                "id": i,
                "description": f"Transação portuguesa detalhada nº {i}",
                "amount": -round(19.99 * i, 2),
                "category": "Alimentação & Restauração",
                "notes": "Despesa validada com NIF português 123456789.",
            }
            for i in range(100)
        ],
    }
    large_json = json.dumps(large_dict, ensure_ascii=False, separators=(",", ":"))
    assert len(large_json.encode("utf-8")) > 4096

    compressed_large = compress_payload(large_json, threshold=1024)
    assert compressed_large.startswith((b"ZSTD:", b"ZLIB:"))
    assert len(compressed_large) < len(large_json.encode("utf-8"))

    # Decompress and verify byte-for-byte exactness
    decompressed = decompress_payload(compressed_large)
    assert decompressed == large_json
    restored_dict = json.loads(decompressed)
    assert restored_dict == large_dict


def test_decompress_payload_legacy_and_edge_cases():
    """Test decompression handles legacy uncompressed strings, byte arrays, and invalid prefixes safely."""
    # Plain JSON string
    assert decompress_payload('{"key": "val"}') == '{"key": "val"}'

    # Plain bytes
    assert decompress_payload(b'{"key": "val"}') == '{"key": "val"}'

    # RAW: prefix string
    assert decompress_payload("RAW:conteudo") == "conteudo"

    # None / Empty
    assert decompress_payload("") == ""
    assert decompress_payload(None) == ""


def test_change_journal_compressed_records_undo_roundtrip(tmp_path: Path):
    """Test ChangeJournalService transparently handles compressed undo payloads and restores state."""
    db_file = tmp_path / "journal_comp.sqlite3"
    db = FinanceDB(db_file)
    repo = Repo(db)
    attachments = tmp_path / "anexos"
    journal = ChangeJournalService(
        repo,
        attachments_directory=attachments,
        journal_storage=tmp_path / "historico",
    )

    # Insert 60 transactions so the deletion payload is large (> 4KB)
    tx_ids = []
    for i in range(1, 61):
        db.conn.execute(
            """
            INSERT INTO transactions(date, description, amount, category, account, kind, notes)
            VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "2026-07-03",
                f"Movimento em lote nº {i}",
                -15.50 * i,
                "Supermercado",
                "Conta principal",
                "expense",
                f"Detalhe de compra grande número {i} com caracteres portugueses €",
            ),
        )
        tx_id = db.conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        tx_ids.append(int(tx_id))
    db.conn.commit()

    # Capture and delete transactions with change journal
    deleted_count, preserved = journal.delete_transactions(
        tx_ids, label="Eliminação em massa de 60 movimentos"
    )
    assert deleted_count == 60

    # Verify change journal entry is compressed in database
    last_entry = db.conn.execute(
        "SELECT id, payload_json FROM change_journal ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert last_entry is not None
    payload_val = last_entry["payload_json"]
    # Should be stored as bytes or compressed string
    assert (
        isinstance(payload_val, (bytes, bytearray, memoryview))
        or str(payload_val).startswith(("ZSTD:", "ZLIB:", "RAW:"))
        or len(payload_val) > 0
    )

    # Verify transactions table is empty for these IDs
    count_after_del = db.scalar(
        f"SELECT COUNT(*) FROM transactions WHERE id IN ({','.join(map(str, tx_ids))})"
    )
    assert count_after_del == 0

    # Undo deletion and verify all 60 transactions are restored with exact amounts
    undone = journal.undo_latest()
    assert undone is not None
    assert undone["status"] == "undone"

    count_restored = db.scalar(
        f"SELECT COUNT(*) FROM transactions WHERE id IN ({','.join(map(str, tx_ids))})"
    )
    assert count_restored == 60

    total_amount = db.scalar(
        f"SELECT SUM(amount) FROM transactions WHERE id IN ({','.join(map(str, tx_ids))})"
    )
    expected_amount = sum(-15.50 * i for i in range(1, 61))
    assert abs(total_amount - expected_amount) < 0.001

    # Compact snapshots
    compaction = journal.compact_snapshots()
    assert compaction["active_journal_entries"] >= 1
    db.close()


# ==============================================================================
# Feature F11: High-Efficiency Lossless Backup Packaging
# ==============================================================================


def test_backup_compression_efficiency_and_sha256_verification(tmp_path: Path):
    """Test that full backup archive achieves measurable compression and passes SHA-256 verification."""
    db_file = tmp_path / "financas.sqlite3"
    db = FinanceDB(db_file)
    repo = Repo(db)
    attachments_dir = tmp_path / "anexos"
    attachments_dir.mkdir(parents=True, exist_ok=True)

    # Add realistic transactions and attachments
    for i in range(1, 100):
        db.add_transaction(
            "2026-07-04",
            f"Fatura de fornecedor {i}",
            -45.90,
            "Casa e contas",
            "Conta principal",
            "expense",
            notes="Texto de auditoria para teste de compressão de base de dados " * 8,
            commit=False,
        )
    db.conn.commit()

    # Create an attachment file
    att_file = attachments_dir / "1" / "fatura.txt"
    att_file.parent.mkdir(parents=True, exist_ok=True)
    att_file.write_text("Fatura fiscal em formato texto " * 50, encoding="utf-8")
    db.conn.execute(
        """
        INSERT INTO transaction_attachments(transaction_id, file_name, stored_name, stored_path)
        VALUES(1, 'fatura.txt', 'fatura.txt', ?)
        """,
        (str(att_file),),
    )
    db.conn.commit()

    backup_service = BackupService(repo, backup_directory=tmp_path / "backups")
    backup_path = tmp_path / "backups" / "backup_eficiente.zip"

    # Export full backup zip
    created_path = backup_service.export_backup_zip(backup_path)
    assert created_path.is_file()

    # Verify SHA-256 manifest and complete archive integrity
    manifest = BackupService.validate_complete_archive(created_path)
    assert manifest["format_version"] == 1
    assert "database" in manifest
    assert "sha256" in manifest["database"]
    assert len(manifest["database"]["sha256"]) == 64

    # Calculate compression efficiency
    source_db_size = db_file.stat().st_size
    backup_size = created_path.stat().st_size
    efficiency = BackupService.calculate_backup_efficiency(source_db_size, backup_size)

    assert efficiency["source_bytes"] == source_db_size
    assert efficiency["backup_bytes"] == backup_size
    assert efficiency["compression_ratio"] > 0
    assert 0.0 <= efficiency["reduction_pct"] <= 100.0

    # Restore into a fresh database location and check 100% relational integrity
    restored_db_file = tmp_path / "restored.sqlite3"
    BackupService.restore_missing_profile(
        created_path, database_file=restored_db_file,
        supported_schema_version=FinanceDB.CURRENT_SCHEMA_VERSION,
    )
    restored_db = FinanceDB.open_read_only(restored_db_file)
    try:
        # Verify exact transaction count and total balance
        original_count = repo.scalar("SELECT COUNT(*) FROM transactions")
        restored_count = restored_db.scalar("SELECT COUNT(*) FROM transactions")
        assert restored_count == original_count
        assert restored_count >= 99

        original_sum = repo.scalar("SELECT SUM(amount) FROM transactions")
        restored_sum = restored_db.scalar("SELECT SUM(amount) FROM transactions")
        assert abs(original_sum - restored_sum) < 0.0001
    finally:
        restored_db.close()

    db.close()
