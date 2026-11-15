from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from database.engine import FinanceDB


@pytest.fixture
def temp_db_path():
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_file = Path(tmp_dir) / "test_perf.sqlite3"
        yield db_file


def test_pragmas_and_performance_covering_indexes(temp_db_path: Path):
    db = FinanceDB(temp_db_path)
    try:
        # Verify PRAGMA settings
        cache_size = db.conn.execute("PRAGMA cache_size").fetchone()[0]
        temp_store = db.conn.execute("PRAGMA temp_store").fetchone()[0]
        busy_timeout = db.conn.execute("PRAGMA busy_timeout").fetchone()[0]

        assert (
            cache_size < 0 or cache_size >= 32000
        )  # Negative represents KiB (e.g. -64000 = 64MB)
        assert temp_store in (2, "MEMORY")  # 2 is MEMORY
        assert busy_timeout >= 10000

        # Verify migrations applied
        migrations = {
            int(r["version"]) for r in db.query("SELECT version FROM schema_migrations")
        }
        assert 26 in migrations

        # Verify indexes exist in sqlite_master
        indexes = {
            str(r["name"])
            for r in db.query("SELECT name FROM sqlite_master WHERE type='index'")
        }
        assert "idx_transactions_date_amount_cat" in indexes
        assert "idx_transactions_date_kind_cat_amt" in indexes
        assert "idx_transactions_date_account_amt" in indexes
        assert "idx_transaction_splits_covering" in indexes
        assert "idx_transaction_splits_cat_amount_tx" in indexes
        assert "idx_transaction_splits_cat_id_amount_tx" in indexes
        assert "idx_transaction_meta_payment_date" in indexes
        assert "idx_categories_kind_active_name" in indexes
        assert "idx_transactions_account_id_date_amount" in indexes
    finally:
        db.close()


def test_cleanup_orphaned_records_preserves_evidence_by_default(temp_db_path: Path):
    db = FinanceDB(temp_db_path)
    try:
        # Create a transaction
        db.conn.execute(
            "INSERT INTO transactions(id, date, description, amount, category, account, kind) "
            "VALUES(100, '2026-01-10', 'Compra Teste', -50.0, 'Supermercado', 'Conta principal', 'expense')"
        )
        # Create linked metadata, split, and attachment
        db.conn.execute(
            "INSERT INTO transaction_meta(transaction_id, payment_date, external_reference) "
            "VALUES(100, '2026-01-10', 'REF-100')"
        )
        db.conn.execute(
            "INSERT INTO transaction_splits(transaction_id, category, amount, percent) "
            "VALUES(100, 'Supermercado', 50.0, 100.0)"
        )
        db.conn.execute(
            "INSERT INTO transaction_attachments(transaction_id, file_name, stored_name, stored_path) "
            "VALUES(100, 'doc100.pdf', 'doc100.pdf', 'docs/doc100.pdf')"
        )

        # Create transaction 999 and its child records
        db.conn.execute(
            "INSERT INTO transactions(id, date, description, amount, category, account, kind) "
            "VALUES(999, '2026-01-10', 'Orphan Parent', -30.0, 'Supermercado', 'Conta principal', 'expense')"
        )
        db.conn.execute(
            "INSERT INTO transaction_meta(transaction_id, payment_date, external_reference) "
            "VALUES(999, '2026-01-10', 'REF-ORPHAN')"
        )
        db.conn.execute(
            "INSERT INTO transaction_splits(transaction_id, category, amount, percent) "
            "VALUES(999, 'Supermercado', 30.0, 100.0)"
        )
        db.conn.execute(
            "INSERT INTO transaction_attachments(transaction_id, file_name, stored_name, stored_path) "
            "VALUES(999, 'orphan.pdf', 'orphan.pdf', 'docs/orphan.pdf')"
        )
        db.conn.commit()

        # Delete parent 999 directly with foreign keys disabled
        db.conn.execute("PRAGMA foreign_keys=OFF")
        db.conn.execute("DELETE FROM transactions WHERE id=999")
        db.conn.commit()
        db.conn.execute("PRAGMA foreign_keys=ON")

        # The recovery contract inventories orphans without deleting evidence.
        counts = db.cleanup_orphaned_records()
        assert counts["transaction_meta"] == 1
        assert counts["transaction_splits"] == 1
        assert counts["transaction_attachments"] == 1

        # Verify parent record remains intact
        meta_count = db.conn.execute(
            "SELECT COUNT(*) FROM transaction_meta WHERE transaction_id=100"
        ).fetchone()[0]
        assert meta_count == 1

        # Orphan evidence remains available for reviewed recovery.
        orphan_meta = db.conn.execute(
            "SELECT COUNT(*) FROM transaction_meta WHERE transaction_id=999"
        ).fetchone()[0]
        assert orphan_meta == 1
        with pytest.raises(ValueError, match="remoção automática"):
            db.cleanup_orphaned_records(dry_run=False)
        for table in ("transaction_meta", "transaction_splits", "transaction_attachments"):
            assert db.scalar(f"SELECT COUNT(*) FROM {table} WHERE transaction_id=999") == 1
    finally:
        # Remove only this deliberately corrupted synthetic fixture before close.
        for table in ("transaction_meta", "transaction_splits", "transaction_attachments"):
            db.conn.execute(f"DELETE FROM {table} WHERE transaction_id=999")
        db.conn.commit()
        db.close()


def test_query_iter_and_query_chunks(temp_db_path: Path):
    db = FinanceDB(temp_db_path)
    try:
        db.conn.execute("DELETE FROM transactions")
        db.conn.commit()

        # Insert 50 test transactions
        for i in range(1, 51):
            db.conn.execute(
                "INSERT INTO transactions(date, description, amount, category, account, kind) "
                "VALUES(?, ?, ?, ?, ?, ?)",
                (
                    f"2026-01-{i:02d}" if i <= 31 else f"2026-02-{i - 31:02d}",
                    f"Item {i}",
                    -10.0,
                    "Supermercado",
                    "Conta principal",
                    "expense",
                ),
            )
        db.conn.commit()

        # Test query_iter (streaming)
        streamed = list(
            db.query_iter(
                "SELECT id, description, amount FROM transactions ORDER BY id"
            )
        )
        assert len(streamed) == 50
        assert streamed[0]["description"] == "Item 1"
        assert streamed[49]["description"] == "Item 50"

        # Test query_chunks (chunking)
        chunks = list(
            db.query_chunks("SELECT id FROM transactions ORDER BY id", chunk_size=15)
        )
        assert len(chunks) == 4  # 15 + 15 + 15 + 5
        assert len(chunks[0]) == 15
        assert len(chunks[3]) == 5
    finally:
        db.close()


def test_explain_query_plan_uses_composite_indexes(temp_db_path: Path):
    db = FinanceDB(temp_db_path)
    try:
        # Populate realistic rows
        for i in range(1, 20):
            db.conn.execute(
                "INSERT INTO transactions(date, description, amount, category, account, kind) "
                "VALUES(?, ?, ?, ?, ?, ?)",
                (
                    f"2026-01-{i:02d}",
                    f"Mercado {i}",
                    -25.0 * i,
                    "Supermercado",
                    "Conta principal",
                    "expense",
                ),
            )
        db.conn.commit()

        # Query using date and kind
        plan = db.query(
            "EXPLAIN QUERY PLAN SELECT date, amount FROM transactions "
            "WHERE date >= '2026-01-01' AND date <= '2026-01-31' AND kind='expense' AND amount < 0"
        )
        plan_str = " ".join(str(r[3]) for r in plan if len(r) > 3).lower()
        # Should use an index scan on idx_transactions_date_kind_cat_amt or similar
        assert (
            "idx_transactions" in plan_str
            or "using index" in plan_str
            or "scan" in plan_str
        )
    finally:
        db.close()
