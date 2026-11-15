from __future__ import annotations

import shutil
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pytest

from database.repo import Repo
from database.engine import FinanceDB
from database.protection import (
    DATA_GUARD_SCHEMA_KEY,
    DATA_GUARD_VERSION_KEY,
)
from services.backup import BackupService
from services.analytics import AnalyticsService
from services.documents import (
    AttachmentIntegrityError,
    DocumentService,
    attachment_storage_key,
)
from services.financial_intelligence import FinancialIntelligenceService
from services.payments import PaymentsService
from services.subscription_matching import subscription_history_linkage
from utils.config import parse_date


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import utils.config as config

    monkeypatch.setattr(config, "DB_NAME", str(tmp_path / "v336.sqlite3"))
    instance = Repo()
    yield instance
    instance.db.conn.close()


def _clear_financial_state(repo: Repo) -> None:
    connection = repo.db.conn
    for table in (
        "transaction_attachments",
        "transaction_splits",
        "transaction_meta",
        "subscription_occurrences",
        "scheduled_payment_occurrences",
        "transactions",
        "financial_state_snapshots",
        "investment_snapshots",
        "holdings",
        "liabilities",
        "goals",
    ):
        connection.execute(f"DELETE FROM {table}")
    connection.commit()


def test_goal_transfers_are_internal_assets_and_keep_history(repo: Repo) -> None:
    _clear_financial_state(repo)
    repo.add_transaction(
        "2026-08-01",
        "Salário",
        1_000,
        "Rendimentos",
        "Conta principal",
    )
    goal_id = repo.add_goal(
        "Viagem",
        2_000,
        100,
        50,
        "2027-08-01",
    )
    starting_net_worth = repo.net_worth()
    # PERS-02: Goals are virtual allocations within cash and not double-counted
    assert starting_net_worth == pytest.approx(1_000)

    deposit_id = repo.adjust_goal_with_transaction(
        goal_id,
        200,
        deposit=True,
        transaction_date="2026-08-10",
    )
    withdrawal_id = repo.adjust_goal_with_transaction(
        goal_id,
        50,
        deposit=False,
        transaction_date="2026-08-11",
    )

    totals = repo.totals_for_payment_period("2026-08-01", "2026-08-31")
    assert totals["income"] == pytest.approx(1_000)
    assert totals["expenses"] == pytest.approx(0)
    # Cash in checking account is now 1000 - 200 + 50 = 850
    assert repo.net_worth() == pytest.approx(850)
    snapshot = FinancialIntelligenceService(repo).net_worth_snapshot()

    assert snapshot["goal_assets"] == pytest.approx(250)
    assert snapshot["net_worth"] == pytest.approx(850)

    linked = repo.q(
        "SELECT id,goal_id,goal_name_snapshot,category FROM transactions "
        "WHERE id IN (?,?) ORDER BY id",
        (deposit_id, withdrawal_id),
    )
    assert [int(row["goal_id"]) for row in linked] == [goal_id, goal_id]
    assert [str(row["goal_name_snapshot"]) for row in linked] == [
        "Viagem",
        "Viagem",
    ]
    assert {str(row["category"]) for row in linked} == {"Objetivos e Poupança"}

    repo.delete_goal(goal_id)
    preserved = repo.q(
        "SELECT goal_id,goal_name_snapshot FROM transactions "
        "WHERE id IN (?,?) ORDER BY id",
        (deposit_id, withdrawal_id),
    )
    assert [row["goal_id"] for row in preserved] == [None, None]
    assert [str(row["goal_name_snapshot"]) for row in preserved] == [
        "Viagem",
        "Viagem",
    ]


def test_goal_state_is_recorded_in_historical_snapshots(repo: Repo) -> None:
    _clear_financial_state(repo)
    repo.add_goal("Reserva", 5_000, 750, 100, "2028-01-01")
    saved = repo.record_financial_state_snapshot("2026-07-31")
    assert saved["goals"][0]["saved_amount"] == pytest.approx(750)

    state = repo.financial_state_as_of("2026-07-31")
    assert state["goals_available"] is True
    assert state["goals"][0]["name"] == "Reserva"
    assert state["goals"][0]["saved_amount"] == pytest.approx(750)


def test_goal_transfers_stay_out_of_every_expense_projection(repo: Repo) -> None:
    _clear_financial_state(repo)
    reference = date.today()
    first_day = reference - timedelta(days=30)
    second_day = reference - timedelta(days=1)
    goal_id = repo.add_goal(
        "Poupança interna",
        1_000,
        0,
        50,
        (reference + timedelta(days=365)).isoformat(),
    )
    for movement_day in (first_day, second_day):
        repo.adjust_goal_with_transaction(
            goal_id,
            50,
            deposit=True,
            transaction_date=movement_day.isoformat(),
        )

    payments = PaymentsService(repo)
    analytics = AnalyticsService(repo, payments_service=payments)
    summary = repo.transactions_filtered_summary_fresh(
        first_day.isoformat(), reference.isoformat()
    )
    assert summary["movement_count"] == 2
    assert summary["income"] == pytest.approx(0)
    assert summary["expenses"] == pytest.approx(0)
    assert payments.likely_recurring_expenses(20) == []
    assert analytics.detect_recurring_subscriptions() == []
    assert (
        analytics.payment_method_totals(first_day.isoformat(), reference.isoformat())
        == []
    )
    assert (
        subscription_history_linkage(
            repo,
            [{"id": 1, "name": "Poupança interna", "aliases": ""}],
            reference_date=reference,
        ).probable
        == 0
    )
    quality = repo.transaction_data_quality_counts(
        first_day.isoformat(), reference.isoformat(), attachment_threshold=1
    )
    assert quality["expenses_without_attachment"] == 0
    assert (
        analytics.monthly_review_data(reference.strftime("%Y-%m"))["no_attachment"] == 0
    )

    goal_days = {
        row["date"]: row
        for row in analytics.balance_series(days=60)
        if row["date"] in {first_day.isoformat(), second_day.isoformat()}
    }
    assert set(goal_days) == {first_day.isoformat(), second_day.isoformat()}
    assert all(row["expense"] == pytest.approx(0) for row in goal_days.values())
    assert all(row["delta"] == pytest.approx(-50) for row in goal_days.values())

    progress, _current, _status = analytics.monthly_goal_progress(
        {
            "month": reference.strftime("%Y-%m"),
            "goal_type": "Gastar no máximo",
            "category": "",
            "target_value": 100,
        }
    )
    assert progress == pytest.approx(0)


def test_attachment_paths_survive_a_new_managed_root(
    repo: Repo, tmp_path: Path
) -> None:
    _clear_financial_state(repo)
    transaction_id = repo.add_transaction(
        "2026-08-20",
        "Compra documentada",
        -25,
        "Diversos",
        "Conta principal",
    )
    root_a = tmp_path / "perfil-a" / "anexos"
    service_a = DocumentService(repo, attachments_directory=root_a)
    source = tmp_path / "fatura.pdf"
    source.write_bytes(b"%PDF-1.4\nportable attachment\n%%EOF\n")
    stored_a = service_a.attach_file(transaction_id, source)
    record = repo.transaction_attachments(transaction_id)[0]
    expected_key = attachment_storage_key(transaction_id, stored_a.name)
    assert record["stored_path"] == expected_key
    assert not Path(str(record["stored_path"])).is_absolute()

    # Recreate a legacy absolute record, then move the managed tree and prove
    # that restore finalisation rewrites it only after validating the new file.
    repo.db.conn.execute(
        "UPDATE transaction_attachments SET stored_path=? WHERE id=?",
        (str(stored_a), int(record["id"])),
    )
    repo.db.conn.commit()
    root_b = tmp_path / "perfil-b" / "anexos"
    shutil.copytree(root_a, root_b)
    shutil.rmtree(root_a)

    report = BackupService._normalise_installed_attachment_paths(
        repo.db.path,
        root_b,
    )
    assert report["validated"] == 1
    migrated = repo.transaction_attachment(int(record["id"]))
    assert migrated["stored_path"] == expected_key
    verified = DocumentService(
        repo, attachments_directory=root_b
    ).verify_attachment_record(migrated, update_verification=False)
    assert Path(verified["path"]).resolve() == (root_b / expected_key).resolve()

    database_copy = tmp_path / "restore-without-document.sqlite3"
    repo.db.backup_to(database_copy)
    with pytest.raises(AttachmentIntegrityError):
        BackupService._normalise_installed_attachment_paths(
            database_copy,
            tmp_path / "empty-attachments",
        )


def test_schema_26_rewrites_absolute_paths_through_the_upgrade_guard(
    tmp_path: Path,
) -> None:
    database_file = tmp_path / "upgrade.sqlite3"
    attachments = tmp_path / "anexos"
    arguments = {
        "attachments_directory": attachments,
        "history_directory": tmp_path / "historico",
        "preferences_file": tmp_path / "preferencias.json",
        "protection_directory": tmp_path / "protecoes",
    }
    # Generate a genuine schema-25 source, rather than deleting one ledger row
    # from a current schema and falsely claiming that it is historical.
    database = FinanceDB.__new__(FinanceDB)
    database.conn = sqlite3.connect(database_file)
    database.conn.row_factory = sqlite3.Row
    database.path = database_file
    database.attachments_directory = attachments
    database._closed = False
    database._read_only = False
    database._startup_progress = None
    database.init_schema()

    class ReachedVersion(Exception):
        pass

    def stop(version):
        if version == 25:
            raise ReachedVersion

    database._schema_contract_observer = stop
    with pytest.raises(ReachedVersion):
        database._apply_pending_migrations()
    database.seed_if_empty()
    database.conn.execute(
        "INSERT INTO financial_accounts(name,account_key,stable_id) VALUES(?,?,?)",
        ("Conta principal", "conta principal", "account-legacy-test"),
    )
    transaction_id = int(
        database.conn.execute(
            """
            INSERT INTO transactions(
                date,description,amount,category,account,kind,notes,
                category_id,account_id
            ) VALUES(
                '2026-08-20','Documento legado',-10,'Diversos',
                'Conta principal','expense','',
                (SELECT stable_id FROM categories WHERE name='Diversos'),
                (SELECT stable_id FROM financial_accounts
                 WHERE name='Conta principal')
            )
            """
        ).lastrowid
    )
    managed = attachments / str(transaction_id) / "legado.pdf"
    managed.parent.mkdir(parents=True)
    managed.write_bytes(b"%PDF-1.4\nlegacy absolute path\n%%EOF\n")
    database.conn.execute(
        """
        INSERT INTO transaction_attachments(
            transaction_id,file_name,stored_name,stored_path,file_type
        ) VALUES(?,?,?,?,?)
        """,
        (transaction_id, "legado.pdf", "legado.pdf", str(managed), "PDF"),
    )
    database.conn.execute("DELETE FROM schema_migrations WHERE version=26")
    database.conn.executemany(
        "INSERT INTO settings(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (
            (DATA_GUARD_VERSION_KEY, "3.35.0"),
            (DATA_GUARD_SCHEMA_KEY, "25"),
        ),
    )
    database.conn.commit()
    database.conn.close()

    upgraded = FinanceDB(database_file, app_version="3.36.0", **arguments)
    try:
        row = upgraded.query(
            "SELECT * FROM transaction_attachments WHERE transaction_id=?",
            (transaction_id,),
        )[0]
        assert upgraded.schema_version() == FinanceDB.CURRENT_SCHEMA_VERSION
        assert row["stored_path"] == f"{transaction_id}/legado.pdf"
        assert len(str(row["content_sha256"])) == 64
        assert int(row["file_size"]) == managed.stat().st_size
        assert managed.read_bytes().startswith(b"%PDF-1.4")
    finally:
        upgraded.close()
    assert list((tmp_path / "protecoes").glob("*.zip"))


def test_invalid_legacy_date_is_excluded_from_dashboard_anomalies(repo: Repo) -> None:
    _clear_financial_state(repo)
    for day, amount in (
        ("2026-08-01", -10),
        ("2026-08-02", -11),
        ("2026-08-03", -9),
        ("2026-08-20", -100),
    ):
        repo.add_transaction(
            day,
            f"Compra {day}",
            amount,
            "Diversos",
            "Conta principal",
        )

    repo.db.conn.execute("DROP TRIGGER validate_transactions_insert")
    repo.db.conn.execute(
        """
        INSERT INTO transactions(
            date,description,amount,category,account,kind,notes,
            category_id,account_id
        ) VALUES('2026-08-99','Movimento legado inválido',-500,
                 'Diversos','Conta principal','expense','',
                 (SELECT stable_id FROM categories WHERE name='Diversos'),
                 (SELECT stable_id FROM financial_accounts
                  WHERE name='Conta principal'))
        """
    )
    repo.db.conn.commit()

    alerts = FinancialIntelligenceService(repo).detect_anomalies(date(2026, 8, 23))
    assert any(alert["kind"] == "spike" for alert in alerts)
    assert all(alert["date"] != "2026-08-99" for alert in alerts)
    with pytest.raises(ValueError, match="Data inválida"):
        parse_date("2026-08-13XYZ")
