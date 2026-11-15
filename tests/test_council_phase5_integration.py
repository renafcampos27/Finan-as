"""A12 maintenance and batching retain A03/A07/A09/A11 data contracts."""

from __future__ import annotations

import sqlite3

import pytest

from database.engine import FinanceDB
from database.repo import Repo
from services.backup import BackupService
from services.startup_security import clear_active_session


@pytest.fixture(autouse=True)
def synthetic_profile(empty_profile):
    clear_active_session()
    yield
    clear_active_session()


@pytest.fixture
def repo(tmp_path):
    instance = Repo(FinanceDB(tmp_path / "source" / "profile.sqlite3"))
    yield instance
    try:
        instance.db.conn.close()
    except sqlite3.Error:
        pass


def test_reference_balance_refund_and_financial_date_survive_complete_restore(
    tmp_path, repo
):
    category = repo.category_rows("expense")[0]["name"]
    account = "Synthetic reference account"
    repo.set_financial_account_reference(
        account, 1000, "2026-01-31", balance_known=True
    )
    entries = [
        ("2026-01-15", "Included in reference", -40),
        ("2026-01-20", "Refund included in reference", 10),
        ("2026-02-02", "After reference", -20),
        ("2026-01-25", "Paid in March", -7),
    ]
    ids = repo.import_transactions_batch(
        [
            {
                "date": day,
                "description": description,
                "amount": amount,
                "category": category,
                "account": account,
            }
            for day, description, amount in entries
        ],
        chunk_size=2,
    )
    repo.set_transaction_meta_ext(
        ids[-1], purchase_date="2026-01-25", payment_date="2026-03-04"
    )

    def expected_state(instance):
        assert instance.cash_balance("2026-02-28") == 980
        assert instance.cash_balance("2026-03-31") == 973
        refund = instance.transaction_by_id(ids[1])
        assert refund["kind"] == "income" and refund["category"] == category
        return {
            "february": instance.account_balances("2026-02-28"),
            "march": instance.account_balances("2026-03-31"),
            "ledger": [
                tuple(row)
                for row in instance.q(
                    "SELECT date,description,amount,category,kind,account_id,category_id "
                    "FROM transactions ORDER BY id"
                )
            ],
            "payment": instance.transaction_meta_ext(ids[-1])["payment_date"],
        }

    before = expected_state(repo)
    archive = BackupService(repo, database_file=repo.db.path).export_backup_zip(
        tmp_path / "financial-contracts.zip"
    )
    live = Repo(FinanceDB(tmp_path / "target" / "profile.sqlite3"))
    restored = None
    try:
        BackupService(live, database_file=live.db.path).restore_backup(archive)
        restored = Repo(FinanceDB(live.db.path))
        assert expected_state(restored) == before
        assert restored.db.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        if restored is not None:
            restored.db.conn.close()
        live.db.conn.close()


def test_maintenance_does_not_commit_batch_or_retain_rolled_back_identity(repo):
    category = repo.category_rows("expense")[0]["name"]
    entry = {
        "date": "2026-02-02",
        "description": "Caller-owned pending import",
        "amount": -11.23,
        "category": category,
        "account": "Transient batch account",
    }
    ids = repo.import_transactions_batch([entry], commit=False)
    old_identity = repo.transaction_by_id(ids[0])["account_id"]
    assert repo.db.conn.in_transaction
    with pytest.raises(RuntimeError):
        repo.incremental_vacuum(4)
    assert repo.db.conn.in_transaction, "Maintenance committed caller-owned money"
    repo.db.conn.rollback()
    assert repo.movement_count() == 0
    assert not repo.q(
        "SELECT stable_id FROM financial_accounts WHERE stable_id=?", (old_identity,)
    )

    fresh_ids = repo.import_transactions_batch([entry])
    fresh = repo.transaction_by_id(fresh_ids[0])
    assert fresh["account_id"] and fresh["account_id"] != old_identity
    assert repo.q(
        "SELECT stable_id FROM financial_accounts WHERE stable_id=?",
        (fresh["account_id"],),
    )
    assert repo.cash_balance("2026-02-03") == pytest.approx(-11.23)
