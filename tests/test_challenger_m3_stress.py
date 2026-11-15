"""Adversarial stress testing and edge-case verification for Milestone 3.

Covers:
1. Corrupt or truncated compressed payloads (ZSTD/ZLIB/RAW graceful handling).
2. Very large JSON payloads (multi-megabyte transactions/splits).
3. Empty, whitespace-only, and non-ASCII/Unicode payloads.
4. Rapid multi-transaction undo/redo cycles with compressed journal records.
5. Incremental vacuuming on empty vs heavily fragmented databases.
6. Backup restoration and checksum verification under concurrent read access.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import threading
import time
from pathlib import Path

import pytest

from database.engine import FinanceDB
from database.read_only import open_read_only_connection
from services.backup import BackupService
from services.change_journal import (
    ChangeJournalService,
    compress_payload,
    decompress_payload,
)


class SimpleRepo:
    """Minimal Repo adapter for testing ChangeJournalService and BackupService."""

    def __init__(self, database: FinanceDB) -> None:
        self.db = database

    def setting(self, key: str, default: str = "") -> str:
        row = self.db.conn.execute(
            "SELECT value FROM settings WHERE key=?",
            (str(key),),
        ).fetchone()
        return str(row["value"]) if row is not None else str(default)

    def set_setting(self, key: str, value: str) -> None:
        self.db.conn.execute(
            "INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",
            (str(key), str(value)),
        )
        self.db.conn.commit()


# =====================================================================
# 1. CORRUPT OR TRUNCATED COMPRESSED PAYLOADS
# =====================================================================


class TestCorruptAndTruncatedPayloads:
    """Stress-test decompression and journal recovery with hostile/damaged byte sequences."""

    def test_decompress_truncated_zstd_prefix(self) -> None:
        # Header present but truncated payload
        corrupted = b"ZSTD:\x28\xb5\x2f\xfd\x04\x00"
        with pytest.raises(Exception):  # Should raise cleanly, never segfault
            decompress_payload(corrupted)

    def test_decompress_random_garbage_zstd(self) -> None:
        garbage = b"ZSTD:" + os.urandom(128)
        with pytest.raises(Exception):
            decompress_payload(garbage)

    def test_decompress_truncated_zlib_stream(self) -> None:
        valid_zlib = compress_payload("test payload " * 100)
        assert valid_zlib.startswith(b"ZSTD:") or valid_zlib.startswith(b"ZLIB:")
        # Force zlib compression
        import zlib
        raw = ("A" * 2048).encode("utf-8")
        zlib_blob = b"ZLIB:" + zlib.compress(raw)[:15]  # truncated
        with pytest.raises(Exception):
            decompress_payload(zlib_blob)

    def test_decompress_garbage_zlib_stream(self) -> None:
        garbage = b"ZLIB:" + os.urandom(64)
        with pytest.raises(Exception):
            decompress_payload(garbage)

    def test_decompress_raw_prefix_with_surrogates_or_invalid_utf8(self) -> None:
        raw_invalid = b"RAW:\xff\xfe\xfd\x80\x81"
        with pytest.raises(UnicodeDecodeError):
            decompress_payload(raw_invalid)

    def test_decompress_bit_flipped_payload(self) -> None:
        original = json.dumps({"key": "value" * 500})
        compressed = bytearray(compress_payload(original, threshold=100))
        if len(compressed) > 10:
            # Flip bits in the middle of payload
            compressed[len(compressed) // 2] ^= 0xFF
            compressed[len(compressed) // 2 + 1] ^= 0xAA
            with pytest.raises(Exception):
                decompress_payload(bytes(compressed))

    def test_journal_undo_handles_corrupt_payload_gracefully(self, tmp_path: Path) -> None:
        db_path = tmp_path / "corrupt_journal.sqlite3"
        db = FinanceDB(db_path)
        repo = SimpleRepo(db)
        journal = ChangeJournalService(
            repo,
            attachments_directory=tmp_path / "anexos",
            journal_storage=tmp_path / "historico",
        )

        # Insert a corrupt journal entry manually into SQLite
        cursor = db.conn.execute(
            """
            INSERT INTO change_journal(action_type, label, payload_json, status)
            VALUES('delete', 'Corrupt Operation', ?, 'available')
            """,
            (b"ZSTD:THIS_IS_TOTALLY_CORRUPTED_GARBAGE_PAYLOAD",),
        )
        corrupt_id = int(cursor.lastrowid)
        db.conn.commit()

        # Attempt undo_latest()
        with pytest.raises(Exception) as exc_info:
            journal.undo_latest()

        assert exc_info.value is not None

        # BUG-AUD-C118 contract: the exception propagates but the entry is
        # NEVER marked 'failed' -- it must remain 'available' at the top of
        # the LIFO so the failure stays visible and blocks further undos.
        row = db.conn.execute(
            "SELECT status FROM change_journal WHERE id=?",
            (corrupt_id,),
        ).fetchone()
        assert row is not None
        assert row["status"] == "available"
        latest = journal.latest_available()
        assert latest is not None
        assert latest["id"] == corrupt_id

        db.close()

    def test_journal_undo_handles_invalid_json_in_valid_compression(self, tmp_path: Path) -> None:
        db_path = tmp_path / "invalid_json_journal.sqlite3"
        db = FinanceDB(db_path)
        repo = SimpleRepo(db)
        journal = ChangeJournalService(
            repo,
            attachments_directory=tmp_path / "anexos",
            journal_storage=tmp_path / "historico",
        )

        # Valid compression, but not valid JSON
        bad_json_compressed = compress_payload("{'invalid_json_no_quotes': True, truncated...", threshold=10)
        cursor = db.conn.execute(
            """
            INSERT INTO change_journal(action_type, label, payload_json, status)
            VALUES('edit', 'Invalid JSON Operation', ?, 'available')
            """,
            (bad_json_compressed,),
        )
        bad_id = int(cursor.lastrowid)
        db.conn.commit()

        with pytest.raises(RuntimeError, match="danificado"):
            journal.undo_latest()

        # BUG-AUD-C118 contract: entry stays 'available' (LIFO top intact).
        row = db.conn.execute(
            "SELECT status FROM change_journal WHERE id=?",
            (bad_id,),
        ).fetchone()
        assert row["status"] == "available"
        latest = journal.latest_available()
        assert latest is not None
        assert latest["id"] == bad_id
        db.close()


# =====================================================================
# 2. VERY LARGE JSON PAYLOADS (MULTI-MEGABYTE)
# =====================================================================


class TestLargeJsonPayloads:
    """Stress-test compression engine with multi-megabyte payloads and deep structures."""

    def test_large_json_roundtrip_fidelity(self) -> None:
        # Generate 5MB+ structured JSON payload
        splits = []
        for i in range(25_000):
            splits.append(
                {
                    "split_id": i + 1,
                    "category": f"Categoria Detalhada #{i % 50} com acentuação €",
                    "amount": round(random.uniform(1.0, 9999.99), 2),
                    "percent": round(random.uniform(0.01, 100.0), 4),
                    "notes": f"Descrição muito longa de transação repetida número {i} para aumentar a redundância do payload " * 3,
                }
            )
        large_dict = {
            "version": "3.3.0",
            "batch_id": "batch-stress-99999",
            "total_splits": len(splits),
            "splits": splits,
        }
        raw_json = json.dumps(large_dict, ensure_ascii=False, separators=(",", ":"))
        raw_size = len(raw_json.encode("utf-8"))
        assert raw_size > 5 * 1024 * 1024  # > 5 MB

        compressed = compress_payload(raw_json, threshold=1024)
        assert compressed.startswith((b"ZSTD:", b"ZLIB:"))
        assert len(compressed) < raw_size / 3  # High compression ratio

        decompressed = decompress_payload(compressed)
        assert decompressed == raw_json

        restored_dict = json.loads(decompressed)
        assert restored_dict["total_splits"] == len(splits)
        assert restored_dict["splits"][12345]["amount"] == splits[12345]["amount"]
        assert restored_dict["splits"][12345]["category"] == splits[12345]["category"]

    def test_journal_handles_large_transaction_splits_undo(self, tmp_path: Path) -> None:
        db_path = tmp_path / "large_journal.sqlite3"
        db = FinanceDB(db_path)
        repo = SimpleRepo(db)
        journal = ChangeJournalService(
            repo,
            attachments_directory=tmp_path / "anexos",
            journal_storage=tmp_path / "historico",
        )

        # Create a parent transaction
        cursor = db.conn.execute(
            """
            INSERT INTO transactions(date, description, amount, category, account, kind, notes)
            VALUES('2026-08-01', 'Supermercado Mensal com 500 splits', -5000.00, 'Supermercado', 'Conta principal', 'expense', 'Grande compra')
            """
        )
        tx_id = int(cursor.lastrowid)

        # Insert 500 splits with valid non-negative amounts
        for i in range(500):
            db.conn.execute(
                """
                INSERT INTO transaction_splits(transaction_id, category, percent, amount)
                VALUES(?, ?, ?, ?)
                """,
                (tx_id, f"Split #{i}", 0.2, 10.00),
            )
        db.conn.commit()

        # Capture snapshot
        snapshot = journal.capture_transactions([tx_id])
        assert len(snapshot["transaction_splits"]) == 500

        # Update category
        db.conn.execute("UPDATE transactions SET category='Outros' WHERE id=?", (tx_id,))
        db.conn.commit()

        # Record snapshot (large payload > 1KB will be compressed with ZSTD/ZLIB)
        journal_id = journal.record_transaction_snapshot(
            snapshot,
            label="Editar compra grande",
            action_type="edit",
        )
        assert journal_id is not None

        # Check that stored payload in SQLite is compressed bytes
        row = db.conn.execute(
            "SELECT payload_json FROM change_journal WHERE id=?",
            (journal_id,),
        ).fetchone()
        payload_data = row["payload_json"]
        assert isinstance(payload_data, (bytes, bytearray, memoryview)) or (
            isinstance(payload_data, str) and payload_data.startswith(("ZSTD:", "ZLIB:"))
        )

        # Undo the edit
        undone = journal.undo_latest()
        assert undone is not None
        assert undone["id"] == journal_id

        # Verify restoration of all 500 splits and original category
        tx_row = db.conn.execute("SELECT category FROM transactions WHERE id=?", (tx_id,)).fetchone()
        assert tx_row["category"] == "Supermercado"
        splits_count = db.conn.execute("SELECT COUNT(*) FROM transaction_splits WHERE transaction_id=?", (tx_id,)).fetchone()[0]
        assert splits_count == 500

        db.close()


# =====================================================================
# 3. EMPTY, WHITESPACE-ONLY, AND NON-ASCII/UNICODE PAYLOADS
# =====================================================================


class TestUnicodeAndBoundaryPayloads:
    """Stress-test edge cases in string encodings, empty buffers, and Unicode sets."""

    @pytest.mark.parametrize(
        "payload",
        [
            "",
            " ",
            "   \t\r\n   ",
            "\n" * 1000,
            "0",
            "None",
            "null",
            "{}",
            "[]",
        ],
    )
    def test_empty_and_whitespace_roundtrip(self, payload: str) -> None:
        compressed = compress_payload(payload, threshold=100)
        decompressed = decompress_payload(compressed)
        assert decompressed == payload

    @pytest.mark.parametrize(
        "unicode_text",
        [
            "Café com Pão & Açúcar à saída do metro — €12,50",
            "Ações & Negócios: EDP Renováveis, Galp Energia, Millennium BCP 📈",
            "Multi-language: 简体中文, 繁體中文, 日本語 (にほんご), 한국어 (조선말), Русский язык, العربية, עברית",
            "Math symbols: ∀x ∈ ℝ, ∃y : x² + y² = 1 ∧ ∑_{i=1}^n i = n(n+1)/2",
            "Emojis: 🚀💰💳🏦🪙📊📈📉🧾🎯🔒🔑🛡️✨",
            "Control chars & tabs: Line1\tCol1\r\nLine2\tCol2\x0b\x0c",
        ],
    )
    def test_unicode_and_special_character_fidelity(self, unicode_text: str) -> None:
        data = {
            "title": unicode_text,
            "repeated": [unicode_text] * 200,
            "nested": {"deep": {"string": unicode_text * 10}},
        }
        raw_json = json.dumps(data, ensure_ascii=False)
        compressed = compress_payload(raw_json, threshold=50)
        decompressed = decompress_payload(compressed)
        assert decompressed == raw_json
        restored = json.loads(decompressed)
        assert restored["title"] == unicode_text
        assert restored["nested"]["deep"]["string"] == unicode_text * 10

    def test_journal_with_unicode_movements(self, tmp_path: Path) -> None:
        db_path = tmp_path / "unicode_journal.sqlite3"
        db = FinanceDB(db_path)
        repo = SimpleRepo(db)
        journal = ChangeJournalService(
            repo,
            attachments_directory=tmp_path / "anexos",
            journal_storage=tmp_path / "historico",
        )

        unicode_desc = "Jantar de Aniversário 🎂 na Ribeira do Porto — €45,80"
        unicode_cat = "Restaurantes & Cafés 🍷"
        db.conn.execute(
            "INSERT INTO categories(name, kind, colour) VALUES(?, 'expense', '#FF5733')",
            (unicode_cat,),
        )

        # Use run_with_created_transactions
        def create_tx() -> list[int]:
            cursor = db.conn.execute(
                """
                INSERT INTO transactions(date, description, amount, category, account, kind, notes)
                VALUES('2026-08-15', ?, -45.80, ?, 'Conta à Ordem 🏦', 'expense', 'Pagamento com Multibanco 💳')
                """,
                (unicode_desc, unicode_cat),
            )
            return [int(cursor.lastrowid)]

        created_ids = journal.run_with_created_transactions(
            create_tx,
            label=f"Criação: {unicode_desc}",
            action_type="manual_entry",
        )
        assert len(created_ids) == 1
        tx_id = created_ids[0]

        # Verify transaction created
        row = db.conn.execute("SELECT description, category, notes FROM transactions WHERE id=?", (tx_id,)).fetchone()
        assert row["description"] == unicode_desc
        assert row["category"] == unicode_cat

        # Undo the creation
        undone = journal.undo_latest()
        assert undone is not None
        assert db.conn.execute("SELECT COUNT(*) FROM transactions WHERE id=?", (tx_id,)).fetchone()[0] == 0

        db.close()


# =====================================================================
# 4. RAPID MULTI-TRANSACTION UNDO/REDO CYCLES
# =====================================================================


class TestRapidUndoRedoCycles:
    """Stress-test sequential and interleaved undo/redo cycles with mixed compressed records."""

    def test_rapid_sequential_undo_cycles(self, tmp_path: Path) -> None:
        db_path = tmp_path / "rapid_undo.sqlite3"
        db = FinanceDB(db_path)
        repo = SimpleRepo(db)
        journal = ChangeJournalService(
            repo,
            attachments_directory=tmp_path / "anexos",
            journal_storage=tmp_path / "historico",
        )

        base_count = db.conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]

        # Create 40 distinct transactions with varying payload sizes
        created_tx_ids = []
        for i in range(40):
            notes = ("Detalhes adicionais de teste " * (i * 10)) if i % 3 == 0 else ""
            cursor = db.conn.execute(
                """
                INSERT INTO transactions(date, description, amount, category, account, kind, notes)
                VALUES('2026-08-01', ?, ?, 'Outros', 'Conta principal', 'expense', ?)
                """,
                (f"Transação #{i}", -float(i + 1), notes),
            )
            tx_id = int(cursor.lastrowid)
            created_tx_ids.append(tx_id)

            # Also add meta and splits for even items
            if i % 2 == 0:
                db.conn.execute(
                    "INSERT INTO transaction_meta(transaction_id, merchant) VALUES(?, ?)",
                    (tx_id, f"Comerciante #{i}"),
                )
                db.conn.execute(
                    "INSERT INTO transaction_splits(transaction_id, category, percent, amount) VALUES(?, 'Outros', 100, ?)",
                    (tx_id, float(i + 1)),
                )

            # Record in journal
            journal.record_created_transactions(
                [tx_id],
                label=f"Criação #{i}",
                action_type="batch_test",
                commit=True,
            )

        assert db.conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == base_count + 40
        assert len(journal.history(limit=50)) >= 40

        # Rapidly undo all 40 operations
        for i in reversed(range(40)):
            undone = journal.undo_latest()
            assert undone is not None
            assert undone["status"] == "undone"
            expected_remaining = base_count + i
            assert db.conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == expected_remaining

        assert db.conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == base_count

        # Compact snapshots
        compact_res = journal.compact_snapshots()
        assert compact_res["active_journal_entries"] <= 50

        db.close()

    def test_interleaved_insert_delete_undo_workflow(self, tmp_path: Path) -> None:
        db_path = tmp_path / "interleaved_undo.sqlite3"
        db = FinanceDB(db_path)
        repo = SimpleRepo(db)
        attachments = tmp_path / "anexos"
        journal = ChangeJournalService(
            repo,
            attachments_directory=attachments,
            journal_storage=tmp_path / "historico",
        )

        base_count = db.conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]

        # 1. Insert 5 transactions
        tx_ids = []
        for i in range(5):
            cursor = db.conn.execute(
                """
                INSERT INTO transactions(date, description, amount, category, account, kind, notes)
                VALUES('2026-08-01', ?, -100.0, 'Outros', 'Conta principal', 'expense', '')
                """,
                (f"Movimento Base #{i}",),
            )
            tx_ids.append(int(cursor.lastrowid))
        db.conn.commit()

        # Add attachment to tx_ids[0]
        att_file = attachments / str(tx_ids[0]) / "recibo.pdf"
        att_file.parent.mkdir(parents=True, exist_ok=True)
        att_file.write_bytes(b"%PDF-1.4 test receipt bytes" * 50)
        db.conn.execute(
            """
            INSERT INTO transaction_attachments(transaction_id, file_name, stored_name, stored_path)
            VALUES(?, 'recibo.pdf', 'recibo.pdf', ?)
            """,
            (tx_ids[0], str(att_file)),
        )
        db.conn.commit()

        # 2. Delete tx_ids[0] and tx_ids[1] via journal.delete_transactions
        deleted_count, preserved_files = journal.delete_transactions(
            [tx_ids[0], tx_ids[1]],
            label="Eliminar 2 movimentos com anexo",
        )
        assert deleted_count == 2
        assert preserved_files == 1
        assert not att_file.exists()  # Stashed into journal storage
        assert db.conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == base_count + 3

        # 3. Undo the deletion
        undone = journal.undo_latest()
        assert undone is not None
        assert db.conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == base_count + 5
        assert att_file.exists()  # Restored from stash
        assert att_file.read_bytes().startswith(b"%PDF-1.4")

        # 4. Modify tx_ids[2] and undo edit
        before_snap = journal.capture_transactions([tx_ids[2]])
        db.conn.execute("UPDATE transactions SET amount=-999.99 WHERE id=?", (tx_ids[2],))
        db.conn.commit()
        journal.record_transaction_snapshot(before_snap, label="Editar valor", action_type="edit")

        assert db.conn.execute("SELECT amount FROM transactions WHERE id=?", (tx_ids[2],)).fetchone()[0] == -999.99
        journal.undo_latest()
        assert db.conn.execute("SELECT amount FROM transactions WHERE id=?", (tx_ids[2],)).fetchone()[0] == -100.0

        db.close()


# =====================================================================
# 5. INCREMENTAL VACUUMING ON EMPTY VS HEAVILY FRAGMENTED DATABASES
# =====================================================================


class TestIncrementalVacuumAndStorageOptimization:
    """Stress-test incremental vacuuming, WAL checkpoints, and storage defragmentation."""

    def test_vacuum_on_empty_database(self, tmp_path: Path) -> None:
        db_path = tmp_path / "empty.sqlite3"
        db = FinanceDB(db_path)

        # Test incremental vacuum on brand-new DB
        free_before = int(db.scalar("PRAGMA freelist_count"))
        reclaimed = db.incremental_vacuum(pages=0)
        free_after = int(db.scalar("PRAGMA freelist_count"))
        assert reclaimed == free_before - free_after
        assert 0 <= reclaimed <= free_before

        reclaimed_10 = db.incremental_vacuum(pages=10)
        assert reclaimed_10 == free_after - int(db.scalar("PRAGMA freelist_count"))
        assert 0 <= reclaimed_10 <= min(10, free_after)

        # Test optimize_storage
        opt_res = db.optimize_storage()
        assert opt_res["status"] == "ok"
        assert opt_res["checkpoint_status"] == "ok"
        assert opt_res["freed_bytes"] >= 0

        # Test defragment_storage
        defrag_res = db.defragment_storage()
        assert defrag_res["status"] == "ok"

        # Verify integrity
        integrity = db.conn.execute("PRAGMA quick_check(1)").fetchone()[0]
        assert str(integrity).lower() == "ok"
        db.close()

    def test_vacuum_on_heavily_fragmented_database(self, tmp_path: Path) -> None:
        db_path = tmp_path / "fragmented.sqlite3"
        db = FinanceDB(db_path)

        base_tx_count = db.conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]

        # Verify initial auto_vacuum mode (0=none, 1=full, 2=incremental)
        auto_vac_mode = int(db.conn.execute("PRAGMA auto_vacuum").fetchone()[0])
        
        # If auto_vacuum is not yet 2, VACUUM activates INCREMENTAL mode
        if auto_vac_mode != 2:
            db.conn.execute("PRAGMA auto_vacuum = INCREMENTAL")
            db.conn.execute("VACUUM")
            auto_vac_mode = int(db.conn.execute("PRAGMA auto_vacuum").fetchone()[0])

        # 1. Insert 2,000 transactions with metadata and splits to inflate DB pages
        db.conn.execute("BEGIN TRANSACTION")
        for i in range(2000):
            cursor = db.conn.execute(
                """
                INSERT INTO transactions(date, description, amount, category, account, kind, notes)
                VALUES('2026-08-01', ?, ?, 'Outros', 'Conta principal', 'expense', ?)
                """,
                (f"Transação Inflada #{i}", -round(i * 1.5 + 1.0, 2), "X" * 200),
            )
            tx_id = int(cursor.lastrowid)
            db.conn.execute(
                "INSERT INTO transaction_meta(transaction_id, merchant, tags) VALUES(?, ?, ?)",
                (tx_id, f"Merchant #{i}", "tag1,tag2,tag3,tag4"),
            )
            db.conn.execute(
                "INSERT INTO transaction_splits(transaction_id, category, percent, amount) VALUES(?, 'Outros', 100, ?)",
                (tx_id, round(i * 1.5 + 1.0, 2)),
            )
        db.conn.commit()

        # Checkpoint WAL
        db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        stats_populated = db.database_storage_stats()
        assert stats_populated["total_transactions"] == base_tx_count + 2000

        # 2. Delete 1,800 transactions (creating massive freelist fragmentation)
        db.conn.execute("DELETE FROM transactions WHERE description LIKE 'Transação Inflada %' AND id <= (SELECT id FROM transactions WHERE description='Transação Inflada #1799')")
        db.cleanup_orphaned_records()
        db.conn.commit()
        db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

        freelist_before = db.conn.execute("PRAGMA freelist_count").fetchone()[0]
        assert freelist_before is not None
        assert int(freelist_before) > 0  # Multiple free pages in freelist

        # 3. Partial incremental vacuum (reclaim 20 pages)
        db.incremental_vacuum(pages=20)
        freelist_mid = int(db.conn.execute("PRAGMA freelist_count").fetchone()[0])
        assert freelist_mid <= int(freelist_before)

        # 4. Full incremental vacuum (reclaim available tail pages)
        db.incremental_vacuum(pages=0)
        freelist_after = int(db.conn.execute("PRAGMA freelist_count").fetchone()[0])
        assert freelist_after <= freelist_mid

        # 5. Full storage optimization and defragmentation
        opt_res = db.optimize_storage()
        assert opt_res["status"] == "ok"
        assert opt_res["stats"]["total_transactions"] == base_tx_count + 200

        # Run full optimize_database
        full_opt = db.optimize_database()
        assert full_opt["status"] == "ok"
        assert full_opt["stats"]["total_transactions"] == base_tx_count + 200

        integrity = db.conn.execute("PRAGMA integrity_check").fetchone()[0]
        assert str(integrity).lower() == "ok"
        db.close()


# =====================================================================
# 6. CONCURRENT BACKUP, RESTORATION, AND CHECKSUM VERIFICATION
# =====================================================================


class TestConcurrentBackupAndRestore:
    """Stress-test backup generation and restoration under active concurrent read threads."""

    def test_concurrent_backup_under_active_readers(self, tmp_path: Path) -> None:
        db_path = tmp_path / "concurrent_app.sqlite3"
        db = FinanceDB(db_path)
        repo = SimpleRepo(db)

        base_tx_count = db.conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]

        # Seed realistic financial records
        db.conn.execute("BEGIN TRANSACTION")
        for i in range(500):
            db.conn.execute(
                """
                INSERT INTO transactions(date, description, amount, category, account, kind, notes)
                VALUES('2026-08-01', ?, -50.0, 'Supermercado', 'Conta principal', 'expense', 'Compra teste')
                """,
                (f"Despesa Concorrente #{i}",),
            )
        db.conn.commit()

        expected_total_tx = base_tx_count + 500

        service = BackupService(
            repo,
            backup_directory=tmp_path / "backups",
            database_file=db_path,
            preferences_file=tmp_path / "backup_preferences.json",
            attachments_directory=tmp_path / "anexos",
            history_directory=tmp_path / "historico",
        )

        stop_event = threading.Event()
        reader_errors: list[Exception] = []
        read_counts = [0, 0, 0, 0]

        def reader_worker(worker_id: int) -> None:
            while not stop_event.is_set():
                try:
                    reader = open_read_only_connection(db_path)
                    try:
                        c = reader.execute("SELECT COUNT(*), SUM(amount) FROM transactions").fetchone()
                        assert c[0] == expected_total_tx
                        read_counts[worker_id] += 1
                    finally:
                        reader.close()
                    time.sleep(0.005)
                except Exception as exc:
                    reader_errors.append(exc)

        # Start 4 concurrent reader threads
        threads = [threading.Thread(target=reader_worker, args=(i,), daemon=True) for i in range(4)]
        for t in threads:
            t.start()

        try:
            # While readers run, perform multiple backup operations
            for _ in range(3):
                backup_file = service.create_manual_backup()
                assert backup_file.exists()

                # Validate the generated backup
                manifest = service.validate_backup_file(backup_file)
                assert manifest is not None

                # Compute checksum
                sha256 = hashlib.sha256(backup_file.read_bytes()).hexdigest()
                assert len(sha256) == 64

                # Test backup efficiency calculator
                eff = BackupService.calculate_backup_efficiency(
                    source_db_size=db_path.stat().st_size,
                    backup_file_size=backup_file.stat().st_size,
                )
                assert eff["source_bytes"] > 0
                assert eff["backup_bytes"] > 0

            # Export encrypted backup under concurrency
            if BackupService.encryption_available():
                enc_target = tmp_path / "backups" / "concurrent_encrypted.lgbak"
                service.export_encrypted_backup(enc_target, password="PalavraPasseForte2026!")
                assert enc_target.exists()
                enc_manifest = service.validate_backup_file(enc_target, password="PalavraPasseForte2026!")
                assert enc_manifest is not None

        finally:
            stop_event.set()
            for t in threads:
                t.join(timeout=3.0)

        assert not reader_errors, f"Reader threads encountered errors: {reader_errors}"
        assert sum(read_counts) > 20, "Readers did not perform sufficient iterations"

        db.close()

    def test_restore_database_and_verify_relational_checksums(self, tmp_path: Path) -> None:
        active_db_path = tmp_path / "source_active.sqlite3"
        db = FinanceDB(active_db_path)
        repo = SimpleRepo(db)

        # Create structured sample with categories, transactions, splits, goals
        # Preserve the category identity already referenced by demo movements.
        db.conn.execute("INSERT INTO categories(name, kind, colour) VALUES('Saúde', 'expense', '#00FF00') ON CONFLICT(name) DO UPDATE SET kind=excluded.kind,colour=excluded.colour")
        db.conn.execute("INSERT OR REPLACE INTO goals(name, target_amount, saved_amount) VALUES('Carro Novo', 10000, 2500)")
        cursor = db.conn.execute(
            """
            INSERT INTO transactions(date, description, amount, category, account, kind, notes)
            VALUES('2026-08-10', 'Farmácia e Medicamentos', -45.50, 'Saúde', 'Conta principal', 'expense', 'Medicamentos')
            """
        )
        tx_id = int(cursor.lastrowid)
        db.conn.execute(
            "INSERT INTO transaction_splits(transaction_id, category, percent, amount) VALUES(?, 'Saúde', 100, 45.50)",
            (tx_id,),
        )
        db.conn.commit()

        service = BackupService(
            repo,
            backup_directory=tmp_path / "backups",
            database_file=active_db_path,
            preferences_file=tmp_path / "backup_preferences.json",
            attachments_directory=tmp_path / "anexos",
            history_directory=tmp_path / "historico",
        )

        backup_zip = service.create_manual_backup()
        assert backup_zip.exists()
        db.close()

        # Restore into a separate database location
        restored_db_path = tmp_path / "restored_target.sqlite3"
        BackupService.restore_missing_profile(
            backup_zip,
            database_file=restored_db_path,
            supported_schema_version=FinanceDB.CURRENT_SCHEMA_VERSION,
        )

        # Connect to restored database and verify exact content fidelity
        restored_db = FinanceDB(restored_db_path)
        try:
            assert restored_db.conn.execute("SELECT COUNT(*) FROM categories WHERE name='Saúde'").fetchone()[0] == 1
            assert restored_db.conn.execute("SELECT COUNT(*) FROM goals WHERE name='Carro Novo'").fetchone()[0] == 1
            assert restored_db.conn.execute("SELECT amount FROM transactions WHERE id=?", (tx_id,)).fetchone()[0] == -45.50
            assert restored_db.conn.execute("SELECT amount FROM transaction_splits WHERE transaction_id=?", (tx_id,)).fetchone()[0] == 45.50

            # Integrity checks
            integrity = restored_db.conn.execute("PRAGMA integrity_check").fetchone()[0]
            assert str(integrity).lower() == "ok"
            fk_check = restored_db.conn.execute("PRAGMA foreign_key_check").fetchall()
            assert len(fk_check) == 0
        finally:
            restored_db.close()
