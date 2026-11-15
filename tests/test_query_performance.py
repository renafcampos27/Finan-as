from __future__ import annotations

from pathlib import Path

import pytest

from database.engine import FinanceDB
from database.repo import Repo


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import utils.config as config

    monkeypatch.setattr(config, "DB_NAME", str(tmp_path / "query-performance.sqlite3"))
    instance = Repo()
    instance.db.conn.executescript(
        """
        DELETE FROM transaction_attachments;
        DELETE FROM transaction_meta;
        DELETE FROM transaction_splits;
        DELETE FROM transactions;
        """
    )
    instance.db.conn.commit()
    yield instance
    instance.db.conn.close()


def _plan(repo: Repo, sql: str, params: tuple = ()) -> str:
    return "\n".join(
        str(row["detail"]) for row in repo.q("EXPLAIN QUERY PLAN " + sql, params)
    )


def test_high_volume_indexes_cover_ordered_lists(repo: Repo) -> None:
    indexes = {
        str(row["name"])
        for row in repo.q(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%'"
        )
    }
    expected = {
        "idx_transaction_attachments_created_id",
        "idx_ai_chat_history_created_id",
        "idx_ai_chat_history_month_created_id",
        "idx_scheduled_payment_occurrences_due_id",
    }

    assert expected <= indexes
    assert "idx_transaction_attachments_created_id" in _plan(
        repo,
        """
        SELECT a.*, t.date AS tx_date, t.description AS tx_desc
        FROM transaction_attachments a
        LEFT JOIN transactions t ON t.id=a.transaction_id
        ORDER BY a.created_at DESC, a.id DESC
        LIMIT ?
        """,
        (200,),
    )
    assert "idx_ai_chat_history_created_id" in _plan(
        repo,
        "SELECT id FROM ai_chat_history ORDER BY created_at DESC, id DESC LIMIT ?",
        (100,),
    )
    assert "idx_ai_chat_history_month_created_id" in _plan(
        repo,
        "SELECT id FROM ai_chat_history WHERE month=? "
        "ORDER BY created_at DESC, id DESC LIMIT ?",
        ("2026-07", 100),
    )
    assert "idx_scheduled_payment_occurrences_due_id" in _plan(
        repo,
        "SELECT id FROM scheduled_payment_occurrences "
        "WHERE due_date>=? AND due_date<=? ORDER BY due_date, id",
        ("2026-01-01", "2026-12-31"),
    )


def test_v9_database_receives_query_indexes_once(repo: Repo) -> None:
    repo.db.conn.executescript(
        """
        DROP INDEX IF EXISTS idx_transaction_attachments_created_id;
        DROP INDEX IF EXISTS idx_ai_chat_history_created_id;
        DROP INDEX IF EXISTS idx_ai_chat_history_month_created_id;
        DROP INDEX IF EXISTS idx_scheduled_payment_occurrences_due_id;
        DROP TABLE IF EXISTS habit_change_plans;
        DELETE FROM schema_migrations WHERE version>=10;
        """
    )
    repo.db.conn.commit()

    assert repo.db.schema_version() == 9
    repo.db._ensure_migrations()
    repo.db._ensure_migrations()

    indexes = {
        str(row["name"])
        for row in repo.q(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%'"
        )
    }
    assert repo.db.schema_version() == FinanceDB.CURRENT_SCHEMA_VERSION
    assert repo.scalar("SELECT COUNT(*) FROM schema_migrations WHERE version=10") == 1
    assert repo.scalar("SELECT COUNT(*) FROM schema_migrations WHERE version=11") == 1
    assert repo.scalar("SELECT COUNT(*) FROM schema_migrations WHERE version=12") == 1
    assert repo.scalar("SELECT COUNT(*) FROM schema_migrations WHERE version=13") == 1
    assert repo.scalar("SELECT COUNT(*) FROM schema_migrations WHERE version=14") == 1
    assert repo.scalar("SELECT COUNT(*) FROM schema_migrations WHERE version=15") == 1
    assert repo.scalar("SELECT COUNT(*) FROM schema_migrations WHERE version=16") == 1
    assert repo.scalar("SELECT COUNT(*) FROM schema_migrations WHERE version=17") == 1
    assert (
        repo.scalar(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type='table' AND name='habit_change_plans'"
        )
        == 1
    )
    assert {
        "idx_transaction_attachments_created_id",
        "idx_ai_chat_history_created_id",
        "idx_ai_chat_history_month_created_id",
        "idx_scheduled_payment_occurrences_due_id",
    } <= indexes


def test_empty_document_search_skips_wildcards_and_joined_count(repo: Repo) -> None:
    transaction_id = repo.add_transaction(
        "2026-07-17",
        "Documento indexado",
        -10.0,
        "Outros",
        "Conta principal",
    )
    repo.add_transaction_attachment(
        transaction_id=transaction_id,
        file_name="fatura.pdf",
        stored_name="fatura.pdf",
        stored_path="fatura.pdf",
        original_path="fatura.pdf",
        file_type="PDF",
        description="",
        supplier="",
        invoice_date="2026-07-17",
        total_amount=10.0,
    )
    statements: list[str] = []
    repo.db.conn.set_trace_callback(statements.append)
    try:
        rows = repo.document_attachments(limit=20)
        assert repo.document_attachment_count() == 1
    finally:
        repo.db.conn.set_trace_callback(None)

    selects = "\n".join(
        statement
        for statement in statements
        if statement.lstrip().upper().startswith("SELECT")
    )
    assert len(rows) == 1
    assert "LIKE '%%'" not in selects
    assert any(
        statement.strip() == "SELECT COUNT(*) FROM transaction_attachments"
        for statement in statements
    )
    assert len(repo.document_attachments("fatura")) == 1
    assert repo.document_attachment_count("fatura") == 1
    assert repo.document_attachments("inexistente") == []


def test_composite_indexes_accelerate_account_and_category_date_queries(
    repo: Repo,
) -> None:
    indexes = {
        str(row["name"])
        for row in repo.q(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%'"
        )
    }
    composite_expected = {
        "idx_transactions_account_date",
        "idx_transactions_category_date",
        "idx_transactions_account_id_date",
        "idx_transactions_category_id_date",
        "idx_transaction_splits_category_tx",
    }
    assert composite_expected <= indexes

    plan_account_date = _plan(
        repo,
        "SELECT id FROM transactions WHERE account=? AND date BETWEEN ? AND ?",
        ("Conta principal", "2026-01-01", "2026-12-31"),
    )
    assert "idx_transactions_account_date" in plan_account_date

    plan_category_date = _plan(
        repo,
        "SELECT id FROM transactions WHERE category=? AND date BETWEEN ? AND ?",
        ("Supermercado", "2026-01-01", "2026-12-31"),
    )
    assert "idx_transactions_category_date" in plan_category_date


def test_transaction_splits_by_ids_loads_batches_in_single_query(repo: Repo) -> None:
    tx1 = repo.add_transaction(
        "2026-08-01", "Compra Mista 1", -100.0, "Supermercado", "Conta principal"
    )
    tx2 = repo.add_transaction(
        "2026-08-02", "Compra Mista 2", -50.0, "Supermercado", "Conta principal"
    )

    repo.save_transaction_splits(tx1, [("Supermercado", 60.0), ("Casa e contas", 40.0)])
    repo.save_transaction_splits(tx2, [("Supermercado", 50.0), ("Transporte", 50.0)])

    statements: list[str] = []
    repo.db.conn.set_trace_callback(statements.append)
    try:
        splits_map = repo.transaction_splits_by_ids([tx1, tx2])
    finally:
        repo.db.conn.set_trace_callback(None)

    # Must execute exactly 1 SELECT query for the batch
    select_queries = [s for s in statements if s.strip().upper().startswith("SELECT")]
    assert len(select_queries) == 1
    assert len(splits_map[tx1]) == 2
    assert len(splits_map[tx2]) == 2
    assert splits_map[tx1][0]["category"] == "Supermercado"
    assert splits_map[tx1][1]["category"] == "Casa e contas"
