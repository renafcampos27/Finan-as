"""Automated verification suite for Council Audit Round 6 - Batch 38 defects.

Defects covered:
1. BUG-R6-TXN-01 [P0]: update_transaction_with_meta savepoint atomicity and error rollback when commit=False.
2. BUG-R6-IMP-01 [P1]: detect_date_format_globally ignores ISO dates; _parse_row_date supports hyphen and dot delimiters.
3. BUG-R6-IMP-02 [P1]: parse_amount with allow_zero=True accepts 0,00 and 0.00 without error.
4. BUG-R6-TXN-02 [P1]: add_transaction / add_transaction_with_meta with commit=False leaves ambient transaction uncommitted.
5. BUG-R6-TXN-03 [P1]: external_reference in FTS5 triggers/index and datetime substring filtering in _transaction_filter_parts.
6. BUG-R6-JRN-01 [P1]: ChangeJournalService._restore_existing_snapshot synchronizes goals.saved_amount on undo.
7. BUG-R6-JRN-02 [P1]: ChangeJournalService._restore_deleted_snapshot re-inserts deleted subscription/scheduled occurrences.
8. BUG-R6-IMP-03 [P2]: parse_amount decimal handling for ambiguous dot/comma formats in PT and International locales.
9. BUG-R6-IMP-04 [P2]: ImportService duplicate detection tracks claimed IDs to avoid false positives on legitimate same-day identicals.
10. BUG-R6-TXN-04 [P2]: update_internal_transfer synchronizes goals.saved_amount when legs are linked to goals.
11. BUG-R6-JRN-03 [P2]: ChangeJournalService._restore_deleted_snapshot detects ID collisions on splits and attachments.
12. BUG-R6-JRN-04 [P2]: ChangeJournalService.prune only prunes non-available entries beyond keep limit and avoids commit during active transaction.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from database.engine import FinanceDB
from database.repo import Repo
from services.change_journal import ChangeJournalService
from services.imports import (
    AMOUNT_LOCALE_INTERNATIONAL,
    AMOUNT_LOCALE_PT,
    DATE_FORMAT_DD_MM,
    DATE_FORMAT_MM_DD,
    ImportService,
    NormalizedImportRow,
)
from utils.helpers import require_eur_cent_precision


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.delenv("FINANCAS_PESSOAIS_DEMO_DATA", raising=False)
    db = FinanceDB(tmp_path / "batch38_test.sqlite3")
    r = Repo(db)
    yield r
    try:
        db.conn.close()
    except Exception:
        pass


@pytest.fixture
def journal(repo, tmp_path):
    return ChangeJournalService(
        repo,
        attachments_directory=tmp_path / "attachments",
        journal_storage=tmp_path / "history",
    )


# 1. BUG-R6-TXN-01 [P0]
def test_bug_r6_txn_01_update_with_meta_atomicity_and_rollback(repo):
    """BUG-R6-TXN-01: In update_transaction_with_meta, when commit=False, failure must roll back

    to the savepoint without persisting partial changes.
    """
    tx_id = repo.add_transaction(
        amount=-50.0,
        description="Original Desc",
        category="Alimentação",
        account="Conta Ordem",
        date_str="2026-03-01",
    )

    # Start an outer transaction
    repo.db.conn.execute("BEGIN IMMEDIATE")
    try:
        # Cause a failure in _set_transaction_meta_ext by passing invalid payment_date
        with pytest.raises(Exception):
            repo.update_transaction_with_meta(
                tx_id,
                transaction_updates={"description": "Changed Desc", "amount": -99.0},
                payment_date="INVALID-DATE-STRING-9999",
                commit=False,
            )
        # Verify that within the open transaction, the transaction row was NOT partially modified
        row = repo.transaction_by_id(tx_id)
        assert row["description"] == "Original Desc"
        assert row["amount"] == -50.0
    finally:
        repo.db.conn.rollback()


# 2. BUG-R6-IMP-01 [P1]
def test_bug_r6_imp_01_date_format_iso_skip_and_delimiters():
    """BUG-R6-IMP-01: detect_date_format_globally ignores ISO dates (YYYY-MM-DD), and

    _parse_row_date supports hyphen and dot delimiters.
    """
    service = ImportService()

    # ISO dates should not force DD/MM or MM/DD
    rows_iso = [
        {"data": "2026-01-15"},
        {"data": "2026-02-20"},
        {"data": "2026-03-25"},
    ]
    detected = service.detect_date_format_globally(rows_iso, "data")
    assert detected == ""

    # European format with hyphen and dot delimiters
    parsed_dash = service._parse_row_date("25-12-2025", DATE_FORMAT_DD_MM)
    assert parsed_dash == date(2025, 12, 25)

    parsed_dot = service._parse_row_date("05.11.2025", DATE_FORMAT_DD_MM)
    assert parsed_dot == date(2025, 11, 5)

    # US format with dot delimiter
    parsed_us_dot = service._parse_row_date("11.05.2025", DATE_FORMAT_MM_DD)
    assert parsed_us_dot == date(2025, 11, 5)


# 3. BUG-R6-IMP-02 [P1]
def test_bug_r6_imp_02_parse_amount_allow_zero():
    """BUG-R6-IMP-02: parse_amount with allow_zero=True accepts zero amounts without raising ValueError."""
    service = ImportService()

    # Default allow_zero=False raises ValueError for 0
    with pytest.raises(ValueError, match="diferente de zero"):
        service.parse_amount("0,00", AMOUNT_LOCALE_PT, allow_zero=False)

    # With allow_zero=True, 0 is parsed cleanly
    assert service.parse_amount("0,00", AMOUNT_LOCALE_PT, allow_zero=True) == 0.0
    assert service.parse_amount("0.00", AMOUNT_LOCALE_INTERNATIONAL, allow_zero=True) == 0.0
    assert service.parse_amount("0", AMOUNT_LOCALE_PT, allow_zero=True) == 0.0

    # Test helper directly
    assert require_eur_cent_precision("0.00", allow_zero=True) == 0.0


# 4. BUG-R6-TXN-02 [P1]
def test_bug_r6_txn_02_add_transaction_commit_false_keeps_ambient_tx(repo):
    """BUG-R6-TXN-02: add_transaction with commit=False when an ambient transaction is active

    leaves the caller's transaction open without premature auto-commit.
    """
    repo.db.conn.execute("BEGIN IMMEDIATE")
    assert repo.db.conn.in_transaction

    tx_id = repo.add_transaction(
        amount=-25.0,
        description="Uncommitted TX",
        category="Geral",
        account="Conta Ordem",
        commit=False,
    )

    # Still in transaction!
    assert repo.db.conn.in_transaction

    # Roll back should discard the transaction
    repo.db.conn.rollback()
    assert repo.transaction_by_id(tx_id) is None


# 5. BUG-R6-TXN-03 [P1]
def test_bug_r6_txn_03_external_reference_fts5_and_filter_parts(repo):
    """BUG-R6-TXN-03: external_reference is indexed in FTS5 and searchable via transaction_search_fts

    and _transaction_filter_parts correctly applies SUBSTR(..., 1, 10).
    """
    tx_id = repo.add_transaction(
        d="2026-04-10",
        description="Compra Online",
        amount=-100.0,
        category="Compras",
        account="Cartão",
        external_reference="REF_SUPER_UNIQUE_12345",
    )
    # Simulate legacy row with datetime in payment_date (pre-trigger validation)
    repo.db.conn.execute("DROP TRIGGER IF EXISTS validate_transaction_meta_update")
    repo.db.conn.execute(
        "UPDATE transaction_meta SET payment_date='2026-04-10 14:30:00' WHERE transaction_id=?",
        (tx_id,),
    )
    repo.db.conn.commit()

    # FTS search matches external_reference
    fts_rows = repo.db.conn.execute(
        "SELECT rowid FROM transaction_search_fts WHERE transaction_search_fts MATCH ?",
        ("REF_SUPER_UNIQUE_12345",),
    ).fetchall()
    assert len(fts_rows) == 1
    assert fts_rows[0]["rowid"] == tx_id

    # Filter parts handles datetime without excluding it
    where, params, _ = repo._transaction_filter_parts(start="2026-04-01", end="2026-04-30")
    placeholders_sql = " AND ".join(where)
    query = f"SELECT id FROM transactions t LEFT JOIN transaction_meta m ON m.transaction_id=t.id WHERE {placeholders_sql}"
    res = repo.db.conn.execute(query, tuple(params)).fetchall()
    assert any(r["id"] == tx_id for r in res)

    # Summary statement also recognizes datetime as valid_date (not invalid_dates)
    summary_sql, summary_params = repo._filtered_transactions_summary_statement(start="2026-04-01", end="2026-04-30")
    summary = repo.db.conn.execute(summary_sql, summary_params).fetchone()
    assert summary["expenses"] == 100.0
    assert summary["invalid_dates"] == 0


# 6. BUG-R6-JRN-01 [P1]
def test_bug_r6_jrn_01_undo_existing_compensates_goals(repo, journal):
    """BUG-R6-JRN-01: ChangeJournalService._restore_existing_snapshot synchronizes goals.saved_amount

    when reverting transaction edits.
    """
    repo.db.conn.execute(
        "INSERT INTO goals (id, name, target_amount, saved_amount) VALUES (1, 'Férias', 1000.0, 300.0)"
    )
    repo.db.conn.commit()

    # Create transaction with goal_id=1, amount=-100 (which added 100 to goal, now 400.0)
    tx_id = repo.add_transaction(
        amount=-100.0,
        description="Poupança Férias",
        category="Poupança",
        account="Conta Poupança",
        goal_id=1,
    )
    repo.db.conn.execute("UPDATE goals SET saved_amount = 400.0 WHERE id=1")
    repo.db.conn.commit()

    # Snapshot state before edit
    snapshot = journal.capture_transactions([tx_id])

    # User edits transaction: amount changed to -200 (delta_saved = -100 - (-200) = +100, goal becomes 500.0)
    repo.update_transaction(tx_id, amount=-200.0)
    goal_row = repo.db.conn.execute("SELECT saved_amount FROM goals WHERE id=1").fetchone()
    assert goal_row["saved_amount"] == 500.0

    # Undo the edit using change journal
    journal._restore_existing_snapshot(snapshot)

    # Goal saved_amount should be compensated back to 400.0
    goal_row_after = repo.db.conn.execute("SELECT saved_amount FROM goals WHERE id=1").fetchone()
    assert goal_row_after["saved_amount"] == 400.0

    # Transaction amount should be back to -100.0
    tx_row = repo.transaction_by_id(tx_id)
    assert tx_row["amount"] == -100.0


# 7. BUG-R6-JRN-02 [P1]
def test_bug_r6_jrn_02_undo_deleted_reinserts_deleted_occurrences(repo, journal):
    """BUG-R6-JRN-02: ChangeJournalService._restore_deleted_snapshot re-inserts deleted

    subscription and scheduled payment occurrences when cursor.rowcount == 0.
    """
    tx_id = repo.add_transaction(
        amount=-15.0,
        description="Netflix Sub",
        category="Subscrições",
        account="Conta Ordem",
    )
    repo.db.conn.execute(
        "INSERT INTO subscriptions (id, name, amount, category) VALUES (1, 'Netflix', 15.0, 'Subscrições')"
    )
    # Create subscription occurrence pointing to tx_id
    repo.db.conn.execute(
        "INSERT INTO subscription_occurrences (id, subscription_id, transaction_id, due_date) "
        "VALUES (10, 1, ?, '2026-03-01')",
        (tx_id,),
    )
    repo.db.conn.commit()

    # Capture snapshot including occurrence
    snapshot = journal.capture_transactions([tx_id])
    assert len(snapshot["subscription_occurrences"]) == 1

    # Delete occurrence and transaction completely
    repo.db.conn.execute("DELETE FROM subscription_occurrences WHERE id=10")
    repo.db.conn.execute("DELETE FROM transactions WHERE id=?", (tx_id,))
    repo.db.conn.commit()

    # Restore snapshot: should re-insert the occurrence because UPDATE had rowcount == 0
    journal._restore_deleted_snapshot(snapshot)

    occ = repo.db.conn.execute(
        "SELECT * FROM subscription_occurrences WHERE id=10"
    ).fetchone()
    assert occ is not None
    assert occ["transaction_id"] == tx_id
    assert occ["due_date"] == "2026-03-01"


# 8. BUG-R6-IMP-03 [P2]
def test_bug_r6_imp_03_amount_parsing_locale_edge_cases():
    """BUG-R6-IMP-03: parse_amount handles ambiguous dot/comma cases correctly in both locales."""
    service = ImportService()

    # In PT locale: "12.50" has a dot with 2 trailing digits -> treat as decimal separator, not thousand!
    val_pt = service.parse_amount("12.50", AMOUNT_LOCALE_PT)
    assert val_pt == 12.50

    # In International locale: "12,50" has a comma with 2 trailing digits -> treat as decimal separator!
    val_intl = service.parse_amount("12,50", AMOUNT_LOCALE_INTERNATIONAL)
    assert val_intl == 12.50

    # Standard thousand separator cases still work
    assert service.parse_amount("1.250,50", AMOUNT_LOCALE_PT) == 1250.50
    assert service.parse_amount("1,250.50", AMOUNT_LOCALE_INTERNATIONAL) == 1250.50


# 9. BUG-R6-IMP-04 [P2]
def test_bug_r6_imp_04_duplicate_detection_claimed_ids(repo):
    """BUG-R6-IMP-04: Multiple same-day transactions with same description and amount

    do not all claim the exact same stored duplicate ID.
    """
    service = ImportService(repo)
    # Insert ONE existing transaction
    tx_id = repo.add_transaction(
        amount=-5.0,
        description="Café Expresso",
        category="Restauração",
        account="Conta Ordem",
        date_str="2026-05-01",
    )

    # Import two identical coffee purchases
    row1 = NormalizedImportRow(
        source_row=1,
        date=date(2026, 5, 1),
        description="Café Expresso",
        amount=-5.0,
        account="Conta Ordem",
    )
    row2 = NormalizedImportRow(
        source_row=2,
        date=date(2026, 5, 1),
        description="Café Expresso",
        amount=-5.0,
        account="Conta Ordem",
    )

    duplicate_index = service._build_duplicate_index([row1, row2])
    claimed_stored_ids: set[int] = set()
    ev1 = service._stored_duplicate_evidence_indexed(
        row1, duplicate_index, claimed_stored_ids=claimed_stored_ids
    )
    ev2 = service._stored_duplicate_evidence_indexed(
        row2, duplicate_index, claimed_stored_ids=claimed_stored_ids
    )

    # First one matches the existing stored transaction
    assert ev1["level"] in ("probable", "possible")
    assert tx_id in ev1["ids"]
    assert tx_id in claimed_stored_ids

    # Second one should NOT match the same stored transaction (claimed_stored_ids prevents reuse)
    assert ev2["level"] == "none"


# 10. BUG-R6-TXN-04 [P2]
def test_bug_r6_txn_04_update_internal_transfer_goals_sync(repo):
    """BUG-R6-TXN-04: update_internal_transfer synchronizes delta amount to goals.saved_amount

    when one of the transfer legs is linked to a goal.
    """
    repo.db.conn.execute(
        "INSERT INTO goals (id, name, target_amount, saved_amount) VALUES (2, 'Carro', 5000.0, 200.0)"
    )
    repo.db.conn.commit()

    # Create internal transfer of 100€
    res = repo.add_internal_transfer(
        source_account="Conta Ordem",
        destination_account="Conta Poupança",
        amount=100.0,
        date_str="2026-06-01",
        description="Poupança Carro",
    )
    tx_id = res["debit_id"]
    _credit_id = res["credit_id"]

    # Link the deposit leg (amount=100.0) or debit leg (amount=-100.0) to goal 2
    # In Repo, credit_leg (positive amount in goal) reduces or debit_leg (negative amount in goal) increases
    repo.db.conn.execute("UPDATE transactions SET goal_id=2 WHERE id=?", (tx_id,))
    # Debit leg has amount = -100.0, representing deposit into goal -> saved_amount increased by 100 -> 300.0
    repo.db.conn.execute("UPDATE goals SET saved_amount=300.0 WHERE id=2")
    repo.db.conn.commit()

    # Update the transfer amount to 150.0€
    repo.update_internal_transfer(
        tx_id=tx_id,
        amount=150.0,
        source_account="Conta Ordem",
        destination_account="Conta Poupança",
        date_str="2026-06-01",
    )

    # Goal saved_amount should be updated by delta_saved = old (-100) - new (-150) = +50 -> 350.0
    goal = repo.db.conn.execute("SELECT saved_amount FROM goals WHERE id=2").fetchone()
    assert goal["saved_amount"] == 350.0


# 11. BUG-R6-JRN-03 [P2]
def test_bug_r6_jrn_03_restore_deleted_collision_splits_attachments(repo, journal):
    """BUG-R6-JRN-03: ChangeJournalService._restore_deleted_snapshot raises RuntimeError

    on ID collisions for transaction_splits and transaction_attachments.
    """
    # Ensure transaction with ID 1 exists for foreign keys
    repo.db.conn.execute(
        "INSERT INTO transactions (id, date, description, amount, category, account) "
        "VALUES (1, '2026-05-01', 'Tx Existente', -10.0, 'Geral', 'Conta')"
    )
    # Insert existing split with ID 999
    repo.db.conn.execute(
        "INSERT INTO transaction_splits (id, transaction_id, category, percent, amount) "
        "VALUES (999, 1, 'Outros', 100.0, 10.0)"
    )
    repo.db.conn.commit()

    snapshot_with_split_collision = {
        "transaction_ids": [50],
        "transactions": [{"id": 50, "description": "Tx 50", "amount": -10.0, "category": "Geral", "account": "Conta"}],
        "transaction_splits": [{"id": 999, "transaction_id": 50, "category": "Lazer", "percent": 100.0, "amount": 10.0}],
    }

    with pytest.raises(RuntimeError, match="partilha com o mesmo identificador já está em uso"):
        journal._restore_deleted_snapshot(snapshot_with_split_collision)

    # Insert existing attachment with ID 888
    repo.db.conn.execute(
        "INSERT INTO transaction_attachments (id, transaction_id, file_name, stored_name, stored_path) "
        "VALUES (888, 1, 'doc.pdf', '888_doc.pdf', '1/888_doc.pdf')"
    )
    repo.db.conn.commit()

    snapshot_with_att_collision = {
        "transaction_ids": [50],
        "transactions": [{"id": 50, "description": "Tx 50", "amount": -10.0, "category": "Geral", "account": "Conta"}],
        "transaction_splits": [],
        "transaction_attachments": [{"id": 888, "transaction_id": 50, "file_name": "x.pdf", "stored_name": "x.pdf", "stored_path": "50/x.pdf"}],
    }

    with pytest.raises(RuntimeError, match="anexo com o mesmo identificador já está em uso"):
        journal._restore_deleted_snapshot(snapshot_with_att_collision)


# 12. BUG-R6-JRN-04 [P2]
def test_bug_r6_jrn_04_prune_protects_available_and_respects_in_transaction(repo, journal):
    """BUG-R6-JRN-04: ChangeJournalService.prune preserves status='available' entries

    even beyond keep limit, and avoids committing if connection.in_transaction is True.
    """
    # Insert 15 journal entries: the oldest 5 are beyond keep=10
    # Make entry 1 available, entry 2 undone, entry 3 available, entry 4 undone
    for i in range(1, 16):
        status = "available" if i % 2 != 0 else "undone"
        repo.db.conn.execute(
            "INSERT INTO change_journal (id, action_type, label, status) VALUES (?, 'edit', ?, ?)",
            (i, f"Action {i}", status),
        )
    repo.db.conn.commit()

    # Prune with keep=10: entries 1 to 5 are candidates.
    # Entry 1, 3, 5 are 'available' -> MUST BE PROTECTED!
    # Entry 2, 4 are 'undone' -> MUST BE PRUNED!
    journal.prune(keep=10)

    remaining_ids = [
        r["id"]
        for r in repo.db.conn.execute("SELECT id FROM change_journal ORDER BY id ASC").fetchall()
    ]
    assert 1 in remaining_ids
    assert 3 in remaining_ids
    assert 5 in remaining_ids
    assert 2 not in remaining_ids
    assert 4 not in remaining_ids

    # Verify prune does not break or auto-commit ambient transaction
    repo.db.conn.execute("BEGIN IMMEDIATE")
    assert repo.db.conn.in_transaction
    journal.prune(keep=10)
    assert repo.db.conn.in_transaction
    repo.db.conn.rollback()
