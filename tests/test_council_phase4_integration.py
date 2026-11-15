"""A09-A11 boundaries must preserve accepted recovery and sale provenance."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from database.engine import FinanceDB
from database.protection import DataProtectionGuard
from database.repo import Repo
from services.backup import BackupService
from services.change_journal import ChangeJournalService
from services.startup_security import clear_active_session
from services.tax_reporting import build_realized_report, load_disposal_records


@pytest.fixture(autouse=True)
def synthetic_security_session(empty_profile):
    clear_active_session()
    yield
    clear_active_session()


@pytest.fixture
def live(tmp_path):
    repo = Repo(FinanceDB(tmp_path / "live" / "profile.sqlite3"))
    yield repo
    try:
        repo.db.conn.close()
    except sqlite3.Error:
        pass


def _database(path: Path) -> Path:
    instance = FinanceDB(path)
    instance.conn.close()
    return path


@pytest.mark.parametrize("name", ["trg_phase4_unknown", "validate_phase4_unknown"])
def test_unknown_sql_restore_is_rejected_before_replacement(tmp_path, live, name):
    source = _database(tmp_path / "untrusted.sqlite3")
    with sqlite3.connect(source) as conn:
        conn.execute(
            f"CREATE TRIGGER {name} AFTER UPDATE ON settings BEGIN "
            "INSERT OR REPLACE INTO settings(key,value) "
            "VALUES('phase4_unknown_ran','yes'); END"
        )
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    before = live.db.path.read_bytes()
    connection = live.db.conn
    callbacks = []

    def must_not_replace():
        callbacks.append(True)
        raise AssertionError("untrusted SQL reached the replacement boundary")

    service = BackupService(live, database_file=live.db.path)
    with pytest.raises((ValueError, RuntimeError)):
        service.restore_backup(source, before_replace=must_not_replace)
    assert callbacks == []
    assert live.db.conn is connection
    assert connection.execute("SELECT 1").fetchone()[0] == 1
    assert live.db.path.read_bytes() == before
    assert hashlib.sha256(source.read_bytes()).hexdigest() == source_hash
    assert live.setting("phase4_unknown_ran", "") == ""


def test_sale_receipt_cash_and_undo_survive_complete_backup_restore(tmp_path, live):
    source = Repo(FinanceDB(tmp_path / "source" / "profile.sqlite3"))
    try:
        expense_category = source.category_rows("expense")[0]["name"]
        source.import_transactions_batch(
            [
                {
                    "date": "2026-05-01",
                    "description": "Synthetic refund",
                    "amount": 25.0,
                    "category": expense_category,
                    "account": "Conta principal",
                },
            ]
        )
        holding_id = source.add_holding(
            "Synthetic ETF",
            "ETF",
            "Broker",
            1400,
            1000,
            fees=10,
            acquisition_date="2024-01-05",
        )
        original = dict(source.holding_by_id(holding_id))
        source.record_investment_disposal(
            holding_id,
            sale_date="2026-05-02",
            sale_value_eur=1500,
            sale_fees_eur=5,
            account="Conta principal",
            journal=ChangeJournalService(source),
        )
        receipts = load_disposal_records(source)
        assert build_realized_report(receipts, 2026)["net_gain_eur"] == 485
        totals = [
            tuple(row)
            for row in source.q(
                "SELECT account,category,kind,SUM(amount) FROM transactions "
                "GROUP BY account,category,kind ORDER BY account,category,kind"
            )
        ]
        archive = BackupService(source, database_file=source.db.path).export_backup_zip(
            tmp_path / "sale-evidence.zip"
        )
    finally:
        source.db.conn.close()

    service = BackupService(live, database_file=live.db.path)
    service.restore_backup(archive)
    restored = Repo(FinanceDB(live.db.path))
    try:
        assert load_disposal_records(restored) == receipts
        assert restored.movement_count() == 2
        assert [
            tuple(row)
            for row in restored.q(
                "SELECT account,category,kind,SUM(amount) FROM transactions "
                "GROUP BY account,category,kind ORDER BY account,category,kind"
            )
        ] == totals
        assert restored.holding_by_id(holding_id) is None
        report = build_realized_report(load_disposal_records(restored), 2026)
        assert report["export_allowed"] and report["net_gain_eur"] == 485
        journal = ChangeJournalService(restored)
        assert journal.undo_latest()["status"] == "undone"
        assert dict(restored.holding_by_id(holding_id)) == original
        assert restored.movement_count() == 1 and load_disposal_records(restored) == []
        refund = restored.q("SELECT amount,kind,category FROM transactions")[0]
        assert tuple(refund) == (25.0, "income", expense_category)
    finally:
        restored.db.conn.close()


def test_reopen_preserves_historical_snapshot_payload_and_fx_precision(tmp_path, live):
    rate = 0.923456789
    holding_id = live.add_holding(
        "Precision fixture",
        "ETF",
        "Broker",
        1234.56,
        1000,
        currency="USD",
        exchange_rate=rate,
        acquisition_date="2024-01-05",
    )
    payload = json.dumps(
        [
            {
                "id": holding_id,
                "quantity": 0.123456789,
                "unit_price": 123.456789,
                "exchange_rate": rate,
                "source": "historical reconstruction",
            },
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    live.db.conn.execute(
        "INSERT INTO monthly_snapshots(month,holdings_json,notes) VALUES(?,?,?)",
        ("2024-01", payload, "Retain original reconstruction evidence"),
    )
    live.db.conn.commit()
    path = live.db.path
    live.db.conn.close()
    reopened = Repo(FinanceDB(path))
    try:
        assert reopened.holding_by_id(holding_id)["exchange_rate"] == rate
        row = reopened.q(
            "SELECT holdings_json,notes FROM monthly_snapshots WHERE month='2024-01'"
        )[0]
        assert row["holdings_json"] == payload
        assert row["notes"] == "Retain original reconstruction evidence"
    finally:
        reopened.db.conn.close()


def test_generic_protection_does_not_classify_evidence_by_fts_substring():
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("CREATE TABLE receipts_fts_evidence(payload BLOB)")
        connection.execute(
            "INSERT INTO receipts_fts_evidence VALUES(?)", (b"original evidence",)
        )
        assert "receipts_fts_evidence" in DataProtectionGuard.protected_tables(
            connection
        )
    finally:
        connection.close()


def test_failed_transfer_preserves_caller_transaction_and_discards_partial_legs(
    live, monkeypatch
):
    connection = live.db.conn
    connection.execute(
        "INSERT INTO settings(key,value) VALUES('caller_pending','keep')"
    )
    accounts = [
        tuple(row)
        for row in connection.execute("SELECT * FROM financial_accounts ORDER BY id")
    ]
    original_insert = live._insert_transaction
    calls = []

    def fail_credit(*args, **kwargs):
        calls.append(True)
        if len(calls) == 2:
            raise RuntimeError("injected credit failure")
        return original_insert(*args, **kwargs)

    monkeypatch.setattr(live, "_insert_transaction", fail_credit)
    with pytest.raises(RuntimeError, match="credit failure"):
        live.add_internal_transfer(
            "2026-01-05", 30, "Synthetic Source", "Synthetic Target"
        )
    assert connection.in_transaction
    assert live.setting("caller_pending", "") == "keep"
    assert live.movement_count() == 0
    assert [
        tuple(row)
        for row in connection.execute("SELECT * FROM financial_accounts ORDER BY id")
    ] == accounts
    connection.rollback()
    assert live.setting("caller_pending", "") == ""
