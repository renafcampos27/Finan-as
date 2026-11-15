"""Unit tests for P0 fixes: BUG-AUD-C67 and BUG-AUD-C88."""

import os
import time
from datetime import date
from pathlib import Path

import pytest

from database.engine import FinanceDB
from database.repo import Repo
from services.documents import DocumentService
from services.payments import PaymentsService


@pytest.fixture
def clean_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Create an isolated test repository with zero demo data."""
    monkeypatch.setenv("FINANCAS_PESSOAIS_DEMO_DATA", "0")
    db_path = tmp_path / "test_c67_c88.db"
    db = FinanceDB(str(db_path))
    repo = Repo(db)
    try:
        for table in (
            "subscription_occurrences",
            "subscriptions",
            "transaction_splits",
            "transactions",
            "transaction_attachments",
            "financial_accounts",
            "categories",
        ):
            repo.db.conn.execute(f"DELETE FROM {table}")
        repo.db.conn.commit()
        yield repo
    finally:
        db.close()


# ===========================================================================
# BUG-AUD-C67: Path Normalization in Document Storage Audit & Prune
# ===========================================================================

def test_audit_storage_windows_path_normalization_avoids_false_orphan(clean_repo, tmp_path):
    """BUG-AUD-C67: Path casing/normalization mismatches between DB and filesystem
    must not cause valid user attachments to be flagged as orphan files."""
    attachments_dir = tmp_path / "attachments"
    attachments_dir.mkdir(parents=True, exist_ok=True)
    service = DocumentService(clean_repo, attachments_directory=attachments_dir)

    # Create a transaction and add an attachment
    t_id = clean_repo.add_transaction(
        "2026-04-01", "Despesa teste", 50.0, "Alimentação", "Conta principal"
    )

    # Create target file on disk
    target_folder = attachments_dir / str(t_id)
    target_folder.mkdir(parents=True, exist_ok=True)
    doc_file = target_folder / "fatura_luz.pdf"
    doc_file.write_bytes(b"%PDF-1.4 test invoice content")

    # Insert attachment record in database with alternate casing or slashes
    stored_path_cased = str(doc_file).lower() if os.name == "nt" else str(doc_file)
    clean_repo.add_transaction_attachment(
        transaction_id=t_id,
        file_name="fatura_luz.pdf",
        stored_name="fatura_luz.pdf",
        stored_path=stored_path_cased,
        original_path=str(doc_file),
        file_type="pdf",
        description="Fatura teste",
        supplier="EDP",
        invoice_date="2026-04-01",
        total_amount=50.0,
        content_sha256="",
        file_size=len(doc_file.read_bytes()),
    )

    audit = service.audit_storage(force=True)
    assert audit["orphan_count"] == 0, f"Expected 0 orphans, got {audit['orphan_files']}"
    assert len(audit["orphan_files"]) == 0
    assert audit["available_records"] == 1
    assert audit["missing_records"] == 0
    assert audit["healthy"] is True


def test_remove_orphan_files_never_deletes_referenced_file_with_casing_difference(clean_repo, tmp_path):
    """BUG-AUD-C67: remove_orphan_files must never unlink a file that is referenced
    in the database, even if drive letters or path segments differ in casing."""
    attachments_dir = tmp_path / "attachments"
    attachments_dir.mkdir(parents=True, exist_ok=True)
    service = DocumentService(clean_repo, attachments_directory=attachments_dir)

    t_id = clean_repo.add_transaction(
        "2026-04-01", "Despesa teste", 25.0, "Outros", "Conta principal"
    )
    target_folder = attachments_dir / str(t_id)
    target_folder.mkdir(parents=True, exist_ok=True)
    doc_file = target_folder / "recibo.png"
    doc_file.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 20)

    # Record relative or mixed-case path in database
    clean_repo.add_transaction_attachment(
        transaction_id=t_id,
        file_name="recibo.png",
        stored_name="recibo.png",
        stored_path=f"{t_id}/recibo.png",
        original_path=str(doc_file),
        file_type="png",
        description="Recibo teste",
        supplier="Loja",
        invoice_date="2026-04-01",
        total_amount=25.0,
        content_sha256="",
        file_size=len(doc_file.read_bytes()),
    )

    removed = service.remove_orphan_files()
    assert removed == 0
    assert doc_file.exists(), "Referenced attachment file was incorrectly deleted by remove_orphan_files!"


def test_remove_orphan_files_deletes_true_orphan_file(clean_repo, tmp_path):
    """BUG-AUD-C67: remove_orphan_files must delete genuinely unreferenced orphan files."""
    attachments_dir = tmp_path / "attachments"
    attachments_dir.mkdir(parents=True, exist_ok=True)
    service = DocumentService(clean_repo, attachments_directory=attachments_dir)

    # Create unreferenced orphan file
    orphan_folder = attachments_dir / "999"
    orphan_folder.mkdir(parents=True, exist_ok=True)
    orphan_file = orphan_folder / "unreferenced.pdf"
    orphan_file.write_bytes(b"%PDF-1.4 orphan")
    # The writer grace period deliberately excludes files younger than 120s.
    old_timestamp = time.time() - 180
    os.utime(orphan_file, (old_timestamp, old_timestamp))

    audit = service.audit_storage(force=True)
    assert audit["orphan_count"] == 1
    assert os.path.normcase(str(orphan_file.resolve())) in [
        os.path.normcase(str(Path(p).resolve())) for p in audit["orphan_files"]
    ]

    removed = service.remove_orphan_files()
    assert removed == 1
    assert not orphan_file.exists()


# ===========================================================================
# BUG-AUD-C88: Multi-Month Overdue Subscription Occurrence Iteration
# ===========================================================================

def test_overdue_subscription_dates_multi_month_generation():
    """BUG-AUD-C88: overdue_subscription_dates must iterate through all overdue dates up to reference."""
    row = {
        "id": 1,
        "name": "Streaming",
        "amount": 14.99,
        "period": "Mensal",
        "renewal_day": 5,
        "next_renewal_date": "2026-01-05",
        "last_used_date": "",
    }
    reference = date(2026, 4, 15)
    overdue_dates, next_due = PaymentsService.overdue_subscription_dates(row, reference)

    assert overdue_dates == [
        date(2026, 1, 5),
        date(2026, 2, 5),
        date(2026, 3, 5),
        date(2026, 4, 5),
    ]
    assert next_due == date(2026, 5, 5)


def test_overdue_subscription_dates_respects_day31_anchor():
    """BUG-AUD-C88: Day-31 anchor is preserved across February and 30-day months."""
    row = {
        "id": 2,
        "name": "Renda",
        "amount": 750.0,
        "period": "Mensal",
        "renewal_day": 31,
        "next_renewal_date": "2026-01-31",
        "last_used_date": "",
    }
    reference = date(2026, 4, 15)
    overdue_dates, next_due = PaymentsService.overdue_subscription_dates(row, reference)

    assert overdue_dates == [
        date(2026, 1, 31),
        date(2026, 2, 28),
        date(2026, 3, 31),
    ]
    assert next_due == date(2026, 4, 30)


def test_process_due_subscriptions_multi_month_creates_all_ledger_occurrences(clean_repo):
    """BUG-AUD-C88: Offline for multiple months must generate all overdue ledger
    occurrences instead of dropping intermediate months."""
    sub_id = clean_repo.add_subscription(
        "Academia",
        35.0,
        "Saúde",
        10,
        "Mensal",
        next_renewal_date="2026-01-10",
    )

    count = clean_repo.process_due_subscriptions(reference_date=date(2026, 4, 15))

    assert count == 4
    report = clean_repo.last_subscription_processing_report
    assert report["created"] == 4
    assert report["historical_skipped"] == 0

    # Verify occurrences recorded in subscription_occurrences
    occurrences = clean_repo.q(
        "SELECT due_date FROM subscription_occurrences WHERE subscription_id=? ORDER BY due_date",
        (sub_id,),
    )
    due_dates = [r["due_date"] for r in occurrences]
    assert due_dates == ["2026-01-10", "2026-02-10", "2026-03-10", "2026-04-10"]

    # Verify subscription dates updated
    sub = clean_repo.q("SELECT next_renewal_date FROM subscriptions WHERE id=?", (sub_id,))[0]["next_renewal_date"]
    assert sub == "2026-05-10"
    last_used = clean_repo.q("SELECT last_used_date FROM subscriptions WHERE id=?", (sub_id,))[0]["last_used_date"]
    assert last_used == "2026-04-10"


def test_process_due_subscriptions_deferred_when_max_charges_hit(clean_repo):
    """BUG-AUD-C88: When maximum_new_charges is reached, remaining overdue charges are deferred without data loss."""
    sub_id = clean_repo.add_subscription(
        "Software SaaS",
        20.0,
        "Tecnologia",
        1,
        "Mensal",
        next_renewal_date="2026-01-01",
    )

    # Process with limit of 2
    count = clean_repo.process_due_subscriptions(maximum_new_charges=2, reference_date=date(2026, 4, 15))
    assert count == 2
    assert clean_repo.last_subscription_processing_report["deferred"] == 1

    # Check renewal date stopped at the first deferred charge (March 1)
    sub = clean_repo.q("SELECT next_renewal_date FROM subscriptions WHERE id=?", (sub_id,))[0]["next_renewal_date"]
    assert sub == "2026-03-01"
    last_used = clean_repo.q("SELECT last_used_date FROM subscriptions WHERE id=?", (sub_id,))[0]["last_used_date"]
    assert last_used == "2026-02-01"

    # Process next batch
    count2 = clean_repo.process_due_subscriptions(maximum_new_charges=2, reference_date=date(2026, 4, 15))
    assert count2 == 2

    # All 4 occurrences now exist
    sub_final = clean_repo.q("SELECT next_renewal_date FROM subscriptions WHERE id=?", (sub_id,))[0]["next_renewal_date"]
    assert sub_final == "2026-05-01"
    occurrences = clean_repo.q(
        "SELECT due_date FROM subscription_occurrences WHERE subscription_id=? ORDER BY due_date",
        (sub_id,),
    )
    assert len(occurrences) == 4


def test_process_due_subscriptions_runaway_guard_preserves_ancient_typo(clean_repo):
    """BUG-AUD-C88 / Safety: When an ancient runaway typo (>120 intervals, e.g. year 2000) is present,
    it skips the ancient backlog and charges the single latest due occurrence."""
    sub_id = clean_repo.add_subscription(
        "Subscrição do ano 2000",
        9.99,
        "Subscrições",
        5,
        "Mensal",
        next_renewal_date="2000-01-05",
    )

    count = clean_repo.process_due_subscriptions()
    assert count == 1
    report = clean_repo.last_subscription_processing_report
    assert report["historical_skipped"] > 120
    assert clean_repo.scalar("SELECT COUNT(*) FROM subscription_occurrences WHERE subscription_id=?", (sub_id,)) == 1


def test_prune_orphans_alias_callable_and_removes_orphans(clean_repo, tmp_path):
    """BUG-AUD-C67: service.prune_orphans must be available as an alias to remove_orphan_files."""
    attachments_dir = tmp_path / "attachments_prune"
    attachments_dir.mkdir(parents=True, exist_ok=True)
    service = DocumentService(clean_repo, attachments_directory=attachments_dir)
    assert hasattr(service, "prune_orphans")
    assert callable(service.prune_orphans)

    orphan_folder = attachments_dir / "888"
    orphan_folder.mkdir(parents=True, exist_ok=True)
    orphan_file = orphan_folder / "prune_test.pdf"
    orphan_file.write_bytes(b"%PDF-1.4 prune test")
    old_timestamp = time.time() - 180
    os.utime(orphan_file, (old_timestamp, old_timestamp))

    removed = service.prune_orphans()
    assert removed == 1
    assert not orphan_file.exists()


def test_process_due_subscriptions_annual_and_quarterly_multi_period(clean_repo):
    """BUG-AUD-C88: Annual and Quarterly subscriptions backfill across multiple years/quarters."""
    sub_anual = clean_repo.add_subscription(
        "Seguro Automóvel",
        300.0,
        "Transportes",
        15,
        "Anual",
        next_renewal_date="2024-01-15",
    )
    sub_trim = clean_repo.add_subscription(
        "Manutenção Trimestral",
        45.0,
        "Casa e contas",
        1,
        "Trimestral",
        next_renewal_date="2025-07-01",
    )

    count = clean_repo.process_due_subscriptions(reference_date=date(2026, 4, 15))
    assert count == 7  # 3 annual + 4 quarterly

    anual_occ = clean_repo.q(
        "SELECT due_date FROM subscription_occurrences WHERE subscription_id=? ORDER BY due_date",
        (sub_anual,),
    )
    assert [r["due_date"] for r in anual_occ] == ["2024-01-15", "2025-01-15", "2026-01-15"]
    sub_anual_row = clean_repo.subscription_by_id(sub_anual)
    assert sub_anual_row["next_renewal_date"] == "2027-01-15"
    assert sub_anual_row["last_used_date"] == "2026-01-15"

    trim_occ = clean_repo.q(
        "SELECT due_date FROM subscription_occurrences WHERE subscription_id=? ORDER BY due_date",
        (sub_trim,),
    )
    assert [r["due_date"] for r in trim_occ] == ["2025-07-01", "2025-10-01", "2026-01-01", "2026-04-01"]
    sub_trim_row = clean_repo.subscription_by_id(sub_trim)
    assert sub_trim_row["next_renewal_date"] == "2026-07-01"
    assert sub_trim_row["last_used_date"] == "2026-04-01"


def test_process_due_subscriptions_end_to_end_preserves_day31_anchor(clean_repo):
    """BUG-AUD-C88: End-to-end repository test verifying day-31 anchor across February."""
    sub_id = clean_repo.add_subscription(
        "Serviço Fim do Mês",
        50.0,
        "Serviços",
        31,
        "Mensal",
        next_renewal_date="2026-01-31",
    )

    count = clean_repo.process_due_subscriptions(reference_date=date(2026, 4, 15))
    assert count == 3  # Jan 31, Feb 28, Mar 31

    occ = clean_repo.q(
        "SELECT due_date FROM subscription_occurrences WHERE subscription_id=? ORDER BY due_date",
        (sub_id,),
    )
    assert [r["due_date"] for r in occ] == ["2026-01-31", "2026-02-28", "2026-03-31"]

    sub_row = clean_repo.subscription_by_id(sub_id)
    assert sub_row["next_renewal_date"] == "2026-04-30"  # April has 30 days
    assert sub_row["last_used_date"] == "2026-03-31"


def test_process_due_subscriptions_undo_via_change_journal(clean_repo):
    """BUG-AUD-C88: Multi-month subscription backfill interacts cleanly with ChangeJournalService undo."""
    from services.change_journal import ChangeJournalService

    journal = ChangeJournalService(clean_repo)

    sub_id = clean_repo.add_subscription(
        "Streaming 4K",
        19.99,
        "Lazer",
        10,
        "Mensal",
        last_used_date="2025-12-10",
        next_renewal_date="2026-01-10",
    )

    active_subs = clean_repo.subscriptions(active_only=True)
    active_ids = [int(s["id"]) for s in active_subs]

    def run_process():
        clean_repo.process_due_subscriptions(
            commit=False,
            subscriptions=active_subs,
            reference_date=date(2026, 4, 15),
        )
        report = dict(getattr(clean_repo, "last_subscription_processing_report", {}) or {})
        return [int(v) for v in report.get("created_ids", [])]

    created_ids = journal.run_with_created_transactions(
        run_process,
        label=lambda ids: f"Subscrições automáticas · {len(ids)}",
        action_type="automatic_subscription",
        state_targets={
            "subscriptions": {
                "ids": active_ids,
                "columns": ("last_used_date", "next_renewal_date"),
            }
        },
        delete_linked_occurrences=True,
    )

    assert len(created_ids) == 4
    assert clean_repo.scalar("SELECT COUNT(*) FROM subscription_occurrences WHERE subscription_id=?", (sub_id,)) == 4
    assert clean_repo.scalar("SELECT COUNT(*) FROM transactions WHERE description LIKE 'Streaming 4K%'") == 4
    sub_updated = clean_repo.subscription_by_id(sub_id)
    assert sub_updated["last_used_date"] == "2026-04-10"
    assert sub_updated["next_renewal_date"] == "2026-05-10"

    # Now execute undo
    entry = journal.undo_latest()
    assert entry is not None

    # Verify all created transactions deleted
    assert clean_repo.scalar("SELECT COUNT(*) FROM transactions WHERE description LIKE 'Streaming 4K%'") == 0
    # Verify all linked occurrences deleted
    assert clean_repo.scalar("SELECT COUNT(*) FROM subscription_occurrences WHERE subscription_id=?", (sub_id,)) == 0
    # Verify subscription dates restored to pre-backfill state
    sub_restored = clean_repo.subscription_by_id(sub_id)
    assert sub_restored["last_used_date"] == "2025-12-10"
    assert sub_restored["next_renewal_date"] == "2026-01-10"
