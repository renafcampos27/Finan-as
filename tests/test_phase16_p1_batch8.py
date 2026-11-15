#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tests for Batch 8:
- AUD-DB-01: Largest Remainder (Hare-Niemeyer) split cent allocation and SQLite trigger amount validation.
- AUD-DB-03: Categories COLLATE NOCASE and UNIQUE index enforcement preventing case duplicate categories.
- AUD-DB-04: Prevent delete trigger on financial_accounts when transactions reference the account.
- AUD-DB-06: Chunked query handling in ChangeJournal and TransactionsRepo preventing SQL variable overflow.
"""

from __future__ import annotations

import sqlite3
import pytest
from database.engine import FinanceDB
from database.repo import Repo
from services.change_journal import ChangeJournalService
from utils.helpers import chunked_iterable


@pytest.fixture
def clean_db(tmp_path):
    db_file = tmp_path / "test_batch8.sqlite3"
    db = FinanceDB(str(db_file))
    repo = Repo(db)
    yield db, repo
    try:
        db.close()
    except Exception:
        pass


def test_aud_db_01_largest_remainder_exact_cents(clean_db):
    db, repo = clean_db

    # Test 10.00 divided in 3 equal splits
    normalized = [("Cat A", 100.0 / 3), ("Cat B", 100.0 / 3), ("Cat C", 100.0 / 3)]
    allocated = repo._allocate_split_amounts(10.00, normalized)
    amounts = [amt for _, _, amt in allocated]

    assert sum(amounts) == 10.00
    assert sorted(amounts) == [3.33, 3.33, 3.34]

    # Test 100.00 divided in 7 splits
    normalized_7 = [(f"Cat {i}", 100.0 / 7) for i in range(7)]
    allocated_7 = repo._allocate_split_amounts(100.00, normalized_7)
    amounts_7 = [amt for _, _, amt in allocated_7]
    assert round(sum(amounts_7), 2) == 100.00

    # Test tiny amount 0.05 in 3 splits
    allocated_tiny = repo._allocate_split_amounts(0.05, normalized)
    amounts_tiny = [amt for _, _, amt in allocated_tiny]
    assert round(sum(amounts_tiny), 2) == 0.05
    assert sorted(amounts_tiny) == [0.01, 0.02, 0.02]

    # Test 0.00
    allocated_zero = repo._allocate_split_amounts(0.00, normalized)
    assert all(amt == 0.0 for _, _, amt in allocated_zero)


def test_aud_db_01_save_splits_database_cents_integrity(clean_db):
    db, repo = clean_db

    # Create transaction with 10.00 €
    tx_id = repo.add_transaction(
        d="2026-09-04",
        desc="Jantar Grupo",
        amount=-10.00,
        category="Alimentação",
        account="Conta principal",
    )

    # Save 3 equal splits (33.33% each)
    splits = [("Comida", 33.3333), ("Bebida", 33.3333), ("Sobremesa", 33.3333)]
    repo.save_transaction_splits(tx_id, splits)

    rows = repo.q("SELECT category, percent, amount FROM transaction_splits WHERE transaction_id=? ORDER BY id", (tx_id,))
    assert len(rows) == 3

    amounts = [float(r["amount"]) for r in rows]
    assert sum(amounts) == 10.00
    assert 3.34 in amounts and 3.33 in amounts

    # Trigger amount validation: inserting a split exceeding transaction amount should abort
    with pytest.raises(sqlite3.IntegrityError, match="transaction_splits total exceeds"):
        db.conn.execute(
            "INSERT INTO transaction_splits(transaction_id, category, category_id, percent, amount) VALUES(?, 'Excesso', '', 10.0, 50.0)",
            (tx_id,),
        )


def test_aud_db_03_categories_nocase_and_unique(clean_db):
    db, repo = clean_db

    cat_id1, name1 = repo.category_identity("Restaurantes", create=True)
    assert name1 == "Restaurantes"

    # Querying with lowercase "restaurantes" should resolve to existing category without creating duplicate
    cat_id2, name2 = repo.category_identity("restaurantes", create=True)
    assert cat_id2 == cat_id1
    assert name2 == "Restaurantes"

    # Count of active categories named Restaurantes should be exactly 1
    count = db.conn.execute(
        "SELECT COUNT(*) FROM categories WHERE name = 'Restaurantes' COLLATE NOCASE"
    ).fetchone()[0]
    assert count == 1

    # Direct SQL INSERT with different casing should raise IntegrityError due to idx_categories_name_nocase
    with pytest.raises(sqlite3.IntegrityError):
        db.conn.execute(
            "INSERT INTO categories(name, kind, colour, stable_id, identity_key) VALUES('RESTAURANTES', 'expense', '#000', 'cat_dup', 'cat_dup_key')"
        )


def test_aud_db_04_prevent_delete_account_with_transactions(clean_db):
    db, repo = clean_db

    # Create account and add a transaction
    acc_id, acc_name = repo.account_identity("Banco Teste")
    repo.add_transaction(
        d="2026-09-04",
        desc="Depósito Teste",
        amount=50.00,
        category="Salário",
        account=acc_name,
    )

    # Attempting to delete the account should be rejected by the trigger
    with pytest.raises(sqlite3.IntegrityError, match="Não é possível eliminar uma conta com transações associadas"):
        db.conn.execute("DELETE FROM financial_accounts WHERE name = ?", (acc_name,))

    # An account without transactions should be deletable without errors
    _, empty_acc = repo.account_identity("Conta Sem Movimentos")
    db.conn.execute("DELETE FROM financial_accounts WHERE name = ?", (empty_acc,))
    assert db.conn.execute("SELECT 1 FROM financial_accounts WHERE name = ?", (empty_acc,)).fetchone() is None


def test_aud_db_06_chunked_query_large_volumes(clean_db):
    db, repo = clean_db
    journal = ChangeJournalService(repo)

    # Verify chunked_iterable utility
    items = list(range(1050))
    chunks = list(chunked_iterable(items, chunk_size=400))
    assert len(chunks) == 3
    assert len(chunks[0]) == 400
    assert len(chunks[1]) == 400
    assert len(chunks[2]) == 250
    assert [x for c in chunks for x in c] == items

    # Clear baseline transactions to test pure bulk operations
    db.conn.execute("DELETE FROM transactions")
    db.conn.commit()

    # Create 1,100 transactions in bulk (exceeding standard 999 SQLite variable limit)
    db.conn.execute("BEGIN TRANSACTION")
    tx_ids = []
    for i in range(1100):
        cur = db.conn.execute(
            "INSERT INTO transactions(date, description, amount, category, account, kind) "
            "VALUES('2026-09-04', ?, -5.0, 'Diversos', 'Conta principal', 'expense')",
            (f"Despesa #{i}",),
        )
        tx_ids.append(int(cur.lastrowid))
    db.conn.commit()

    assert len(tx_ids) == 1100

    # Capture transactions should safely chunk through rows without OperationalError
    snapshot = journal.capture_transactions(tx_ids)
    assert len(snapshot.get("transactions", [])) == 1100

    # delete_transactions should safely chunk through deletes without OperationalError
    deleted = repo.delete_transactions(tx_ids)
    assert deleted == 1100

    remaining = db.conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    assert remaining == 0
