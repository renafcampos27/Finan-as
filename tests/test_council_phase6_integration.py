"""Product metadata survives protected migrations/recovery without creating cash."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from database.engine import FinanceDB
from database.repo import Repo
from database.schema_contract import (
    financial_integrity_report,
    inspect_schema_contract,
    migration_dry_run,
    orphan_inventory,
    trusted_schema_contracts,
)
from services.backup import BackupService
from services.product_review import ProductReviewService
from services.reimbursements import ReimbursementsService
from services.startup_security import clear_active_session
from services.subscription_radar import SubscriptionRadarService


@pytest.fixture(autouse=True)
def synthetic_security(empty_profile):
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


def legacy_profile(path: Path, version: int) -> Path:
    """Replay source helpers, then stop before the product metadata migrations."""
    legacy = FinanceDB.__new__(FinanceDB)
    legacy.conn = sqlite3.connect(path)
    legacy.conn.row_factory = sqlite3.Row
    legacy.path = path
    legacy.attachments_directory = path.parent / "empty-attachments"
    legacy._closed, legacy._read_only, legacy._startup_progress = False, False, None
    legacy.init_schema()

    class ReachedVersion(Exception):
        pass

    def stop(current):
        if current >= 16:
            legacy._defer_validation_trigger_rebuild = False
            try:
                legacy._migration_value_constraints()
            finally:
                legacy._defer_validation_trigger_rebuild = True
        if current == version:
            raise ReachedVersion

    legacy._schema_contract_observer = stop
    try:
        legacy._apply_pending_migrations()
    except ReachedVersion:
        legacy.conn.commit()
    legacy.conn.execute(
        "INSERT INTO categories(name,kind,colour,budget) VALUES('Legacy category','expense','#123456',12.34)"
    )
    legacy.conn.execute(
        "INSERT INTO transactions(date,description,amount,category,account,kind) "
        "VALUES('2026-01-05','Original evidence',-12.34,'Legacy category','Legacy account','expense')"
    )
    legacy.conn.execute(
        "INSERT INTO monthly_snapshots(month,holdings_json) VALUES('2026-01',?)",
        (
            '[{"quantity":0.123456789,"unit_price":123.456789,"exchange_rate":0.923456789}]',
        ),
    )
    legacy.conn.commit()
    legacy.conn.close()
    return path


@pytest.mark.parametrize("version", [30, 31])
def test_supported_product_predecessors_upgrade_without_financial_rewrite(
    tmp_path, version
):
    path = legacy_profile(tmp_path / f"legacy-{version}.sqlite3", version)
    with sqlite3.connect(path) as before:
        ledger = before.execute(
            "SELECT date,description,amount,category,account,kind FROM transactions"
        ).fetchall()
        payload = before.execute(
            "SELECT holdings_json FROM monthly_snapshots"
        ).fetchone()[0]
        plan = migration_dry_run(before)
        assert [row["version"] for row in plan["pending_migrations"]] == list(
            range(version + 1, 33)
        )
        assert (
            before.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
            == version
        )
    upgraded = FinanceDB(path)
    try:
        assert upgraded.schema_version() == 32
        assert [
            tuple(row)
            for row in upgraded.query(
                "SELECT date,description,amount,category,account,kind FROM transactions"
            )
        ] == ledger
        assert (
            upgraded.query("SELECT holdings_json FROM monthly_snapshots")[0][0]
            == payload
        )
        assert not upgraded.query("SELECT * FROM reimbursement_claims")
        assert not upgraded.query("SELECT * FROM subscription_price_reviews")
        assert financial_integrity_report(upgraded.conn)["ok"]
    finally:
        upgraded.conn.close()


def test_product_migration_interruption_retains_source_then_reopens(
    tmp_path, monkeypatch
):
    path = legacy_profile(tmp_path / "interrupted.sqlite3", 30)
    assert trusted_schema_contracts()
    migration = FinanceDB._migration_reimbursement_evidence

    def fail_after_ddl(instance):
        migration(instance)
        raise RuntimeError("injected product migration interruption")

    with monkeypatch.context() as patch:
        patch.setattr(FinanceDB, "_migration_reimbursement_evidence", fail_after_ddl)
        with pytest.raises(RuntimeError):
            FinanceDB(path)
    with sqlite3.connect(path) as source:
        assert (
            source.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
            == 30
        )
        assert source.execute("SELECT amount FROM transactions").fetchone()[0] == -12.34
        assert not source.execute(
            "SELECT 1 FROM sqlite_master WHERE name='reimbursement_claims'"
        ).fetchone()
    reopened = FinanceDB(path)
    try:
        assert reopened.schema_version() == 32
        assert reopened.scalar("SELECT SUM(amount) FROM transactions") == -12.34
    finally:
        reopened.conn.close()


def _metadata(instance):
    return {
        table: [
            tuple(row) for row in instance.q(f'SELECT * FROM "{table}" ORDER BY id')
        ]
        for table in (
            "subscription_price_reviews",
            "reimbursement_claims",
            "reimbursement_receipts",
        )
    }


def test_review_partial_wallet_and_preferences_survive_complete_backup_restore(
    tmp_path, repo
):
    category = repo.category_rows("expense")[0]["name"]
    source = repo.add_transaction("2026-01-02", "Claim source", -200, category, "Bank")
    receipt = repo.add_transaction(
        "2026-01-03", "Actual partial receipt", 75, category, "Bank"
    )
    wallet = ReimbursementsService(repo)
    claim = wallet.create(source, 100, label="Synthetic claim", due_date="2026-02-01")
    wallet.link(claim, receipt, 75)
    sid = repo.add_subscription(
        "Annual Product", 100, "Diversos", 1, "Anual", account="Conta principal"
    )
    repo.add_transaction(
        "2026-01-04", "Annual Product", -120, "Diversos", "Conta principal"
    )
    repo.commit()
    radar = SubscriptionRadarService(repo)
    candidate = radar.discover()[0]
    radar.decide(
        candidate.evidence_key,
        status="accepted",
        classification="price",
        confirmed_period="Anual",
        confirmed_currency="EUR",
        match_confirmed=True,
    )
    ProductReviewService(repo).snooze("synthetic-key", "2027-01-01", as_of="2026-11-01")
    before = _metadata(repo)
    cash = repo.cash_balance("2026-12-31")
    assert cash == -245 and repo.movement_count() == 3
    assert financial_integrity_report(repo.db.conn)["ok"]
    archive = BackupService(repo, database_file=repo.db.path).export_backup_zip(
        tmp_path / "product.zip"
    )
    live = Repo(FinanceDB(tmp_path / "target" / "profile.sqlite3"))
    restored = None
    try:
        BackupService(live, database_file=live.db.path).restore_backup(archive)
        restored = Repo(FinanceDB(live.db.path))
        assert _metadata(restored) == before
        assert restored.cash_balance("2026-12-31") == cash
        assert restored.movement_count() == 3
        assert restored.reimbursement_claim(claim)["state"] == "partial"
        assert restored.subscription_by_id(sid)["amount"] == 100
        assert (
            json.loads(restored.setting("administrative_snoozes_v1"))["synthetic-key"]
            == "2027-01-01"
        )
        assert financial_integrity_report(restored.db.conn)["ok"]
    finally:
        if restored is not None:
            restored.db.conn.close()
        live.db.conn.close()


def test_declared_product_capability_cannot_omit_its_table(tmp_path):
    path = legacy_profile(tmp_path / "forged-31.sqlite3", 30)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO schema_migrations(version,name) VALUES(31,'forged missing capability')"
        )
        assert not inspect_schema_contract(connection)["ok"]
    with pytest.raises((ValueError, RuntimeError)):
        BackupService.validate_restore_database(path)


def test_financial_diagnostics_reject_corrupt_metadata_with_source_sql_restored(repo):
    category = repo.category_rows("expense")[0]["name"]
    source = repo.add_transaction("2026-01-02", "Source", -100, category, "Bank")
    receipt = repo.add_transaction("2026-01-03", "Receipt", 40, category, "Bank")
    wallet = ReimbursementsService(repo)
    claim = wallet.create(source, 100)
    wallet.link(claim, receipt, 40)
    guards = repo.q(
        "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND name LIKE '%reimbursement%'"
    )
    for guard in guards:
        repo.db.conn.execute(f'DROP TRIGGER "{guard["name"]}"')
    repo.db.conn.execute("UPDATE reimbursement_receipts SET amount_cents=5000")
    for guard in guards:
        repo.db.conn.execute(guard["sql"])
    repo.commit()
    assert inspect_schema_contract(repo.db.conn)["ok"]
    assert not financial_integrity_report(repo.db.conn)["ok"]
    inventory = orphan_inventory(repo.db.conn)
    assert (
        inventory["orphans"]["reimbursement_receipts"]["classification"]
        == "financial_or_document_evidence"
    )
    assert not inventory["orphans"]["reimbursement_receipts"][
        "automatic_delete_allowed"
    ]


def test_restore_rejects_wallet_evidence_changed_to_internal_transfer(repo):
    category = repo.category_rows("expense")[0]["name"]
    source = repo.add_transaction("2026-01-02", "Source", -100, category, "Bank A")
    receipt = repo.add_transaction("2026-01-03", "Receipt", 100, category, "Bank B")
    wallet = ReimbursementsService(repo)
    claim = wallet.create(source, 100)
    wallet.link(claim, receipt, 100)
    guards = repo.q(
        "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND name LIKE '%reimbursement%'"
    )
    for guard in guards:
        repo.db.conn.execute(f'DROP TRIGGER "{guard["name"]}"')
    repo.db.conn.execute(
        "UPDATE transactions SET transfer_group_id='synthetic-internal-pair' WHERE id IN (?,?)",
        (source, receipt),
    )
    for guard in guards:
        repo.db.conn.execute(guard["sql"])
    repo.commit()
    assert inspect_schema_contract(repo.db.conn)["ok"]
    report = financial_integrity_report(repo.db.conn)
    assert not report["ok"]
    assert any(
        "Reembolso" in issue or "Recebimento" in issue for issue in report["issues"]
    )
    with pytest.raises((ValueError, RuntimeError)):
        BackupService.validate_restore_database(repo.db.path)
