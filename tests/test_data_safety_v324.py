from __future__ import annotations

import json
import sqlite3
import zipfile
from datetime import date
from pathlib import Path

import pytest

from database.protection import DataProtectionGuard
from database.repo import Repo
from services.backup import BackupService
from services.payments import PaymentsService
from utils.private_temp import system_private_parent


class _SecretStore:
    def __init__(self, password: str = "palavra-passe-segura") -> None:
        self.password = password

    def read(self) -> str:
        return self.password

    def write(self, secret: str) -> None:
        self.password = str(secret)

    def delete(self) -> None:
        self.password = ""


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import utils.config as config

    monkeypatch.setattr(config, "DB_NAME", str(tmp_path / "v324.sqlite3"))
    instance = Repo()
    yield instance
    try:
        instance.db.conn.close()
    except sqlite3.ProgrammingError:
        pass


def _clear_recurring_data(repo: Repo) -> None:
    repo.db.conn.execute("DELETE FROM subscription_occurrences")
    repo.db.conn.execute("DELETE FROM scheduled_payment_occurrences")
    repo.db.conn.execute("DELETE FROM subscriptions")
    repo.db.conn.execute("DELETE FROM scheduled_payments")
    repo.db.conn.execute("DELETE FROM transactions")
    repo.db.conn.commit()


def test_stale_subscription_creates_only_current_charge_and_is_idempotent(
    repo: Repo,
) -> None:
    _clear_recurring_data(repo)
    repo.add_subscription(
        "Subscrição antiga",
        9.99,
        "Subscrições",
        5,
        "Mensal",
        next_renewal_date="2000-01-05",
    )

    assert repo.process_due_subscriptions() == 1
    report = repo.last_subscription_processing_report
    assert report["historical_skipped"] > 120
    assert repo.scalar("SELECT COUNT(*) FROM subscription_occurrences") == 1
    assert (
        repo.scalar(
            "SELECT COUNT(*) FROM transactions "
            "WHERE description='Subscrição antiga (Subscrição)'"
        )
        == 1
    )

    assert repo.process_due_subscriptions() == 0
    assert repo.scalar("SELECT COUNT(*) FROM subscription_occurrences") == 1


def test_subscription_startup_batch_rolls_back_when_history_fails(
    repo: Repo,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_recurring_data(repo)
    subscription_id = repo.add_subscription(
        "Cobrança atómica",
        15,
        "Subscrições",
        date.today().day,
        "Mensal",
        next_renewal_date=date.today().isoformat(),
    )
    before = dict(repo.subscription_by_id(subscription_id))

    def fail_snapshot(*_args, **_kwargs):
        raise RuntimeError("simulated history failure")

    monkeypatch.setattr(repo, "record_financial_state_snapshot", fail_snapshot)
    with pytest.raises(RuntimeError, match="history failure"):
        repo.process_due_subscriptions()

    assert repo.scalar("SELECT COUNT(*) FROM subscription_occurrences") == 0
    assert repo.scalar("SELECT COUNT(*) FROM transactions") == 0
    after = dict(repo.subscription_by_id(subscription_id))
    assert after["last_used_date"] == before["last_used_date"]
    assert after["next_renewal_date"] == before["next_renewal_date"]


def test_planning_edits_roll_back_if_their_snapshot_fails(
    repo: Repo,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    counts = {
        table: int(repo.scalar(f"SELECT COUNT(*) FROM {table}"))
        for table in ("holdings", "liabilities", "subscriptions")
    }

    def fail_snapshot(*_args, **_kwargs):
        raise RuntimeError("simulated snapshot failure")

    with monkeypatch.context() as scoped:
        scoped.setattr(repo, "record_investment_snapshot", fail_snapshot)
        with pytest.raises(RuntimeError, match="snapshot failure"):
            repo.add_holding("ETF atómico", "ETF", "Conta", 100, 90)
    assert repo.scalar("SELECT COUNT(*) FROM holdings") == counts["holdings"]

    with monkeypatch.context() as scoped:
        scoped.setattr(repo, "record_financial_state_snapshot", fail_snapshot)
        with pytest.raises(RuntimeError, match="snapshot failure"):
            repo.add_liability("Dívida atómica", "Crédito", "Conta", 100, 2, 10)
        with pytest.raises(RuntimeError, match="snapshot failure"):
            repo.add_subscription(
                "Subscrição atómica",
                10,
                "Subscrições",
                1,
                "Mensal",
            )
    assert repo.scalar("SELECT COUNT(*) FROM liabilities") == counts["liabilities"]
    assert repo.scalar("SELECT COUNT(*) FROM subscriptions") == counts["subscriptions"]


def test_emergency_goal_switch_is_atomic(repo: Repo) -> None:
    old_goal = repo.add_goal(
        "Reserva atual",
        5_000,
        1_000,
        100,
        "2027-01-01",
        goal_type="emergency_fund",
    )
    repo.db.conn.execute(
        f"""
        CREATE TRIGGER reject_old_emergency_demotion
        BEFORE UPDATE ON goals
        WHEN OLD.id={old_goal} AND NEW.goal_type='standard'
        BEGIN
            SELECT RAISE(ABORT, 'simulated goal history failure');
        END
        """
    )
    repo.db.conn.commit()
    before = int(repo.scalar("SELECT COUNT(*) FROM goals"))

    with pytest.raises(sqlite3.IntegrityError, match="goal history failure"):
        repo.add_goal(
            "Nova reserva",
            8_000,
            500,
            150,
            "2028-01-01",
            goal_type="emergency_fund",
        )

    assert repo.scalar("SELECT COUNT(*) FROM goals") == before
    assert repo.goal_by_id(old_goal)["goal_type"] == "emergency_fund"


def test_unlimited_old_payment_is_created_in_reviewable_batches(repo: Repo) -> None:
    _clear_recurring_data(repo)
    repo.add_scheduled_payment(
        "Pagamento sem fim",
        "Casa e contas",
        "",
        25,
        "Conta principal",
        "Débito direto",
        "2000-01-01",
        1,
        0,
    )
    service = PaymentsService(repo)

    assert service.generate_due_scheduled_payments(date.today()) == 12
    assert service.last_generation_report["remaining_due"] > 0
    assert service.last_generation_report["limited_payments"] == 1
    assert repo.scalar("SELECT COUNT(*) FROM transactions") == 12

    assert service.generate_due_scheduled_payments(date.today()) == 12
    assert repo.scalar("SELECT COUNT(*) FROM transactions") == 24


def test_zero_value_movements_and_recurring_items_are_rejected(repo: Repo) -> None:
    _clear_recurring_data(repo)
    with pytest.raises(ValueError, match="diferente de zero"):
        repo.add_transaction(
            date.today().isoformat(),
            "Movimento zero",
            0,
            "Outros",
            "Conta principal",
        )
    with pytest.raises(ValueError, match="diferente de zero"):
        repo.add_subscription("Subscrição zero", 0, "Subscrições", 1, "Mensal")
    with pytest.raises(ValueError, match="diferente de zero"):
        repo.add_scheduled_payment(
            "Pagamento zero",
            "Outros",
            "",
            0,
            "Conta principal",
            "",
            date.today().isoformat(),
            1,
            0,
        )
    with pytest.raises(sqlite3.IntegrityError, match="cannot be zero"):
        repo.db.conn.execute(
            "INSERT INTO transactions"
            "(date,description,amount,category,account,kind,notes) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                date.today().isoformat(),
                "Zero direto",
                0,
                "Outros",
                "Conta principal",
                "income",
                "",
            ),
        )
    repo.db.conn.rollback()

    # Legacy rows are not rewritten destructively, but they are visible in the
    # quality centre and excluded from financial totals.
    repo.db.conn.execute("DROP TRIGGER validate_transactions_insert")
    repo.db.conn.execute(
        "INSERT INTO transactions"
        "(date,description,amount,category,account,kind,notes) "
        "VALUES(?,?,?,?,?,?,?)",
        (
            date.today().isoformat(),
            "Zero legado",
            0,
            "Outros",
            "Conta principal",
            "income",
            "",
        ),
    )
    repo.db.conn.commit()
    quality = repo.transaction_data_quality_counts(
        date.today().isoformat(),
        date.today().isoformat(),
    )
    summary = repo.transactions_filtered_summary_fresh(
        date.today().isoformat(),
        date.today().isoformat(),
    )
    assert quality["invalid_amounts"] == 1
    assert summary["excluded_invalid_count"] == 1


def test_version_snapshot_is_encrypted_and_scrubs_legacy_ai_key(
    repo: Repo,
) -> None:
    password = "palavra-passe-segura"
    repo.set_setting("backup_encryption_enabled", "1")
    repo.set_setting("ai_gemini_api_key", "segredo-antigo")
    repo.paths.backup_preferences.parent.mkdir(parents=True, exist_ok=True)
    repo.paths.backup_preferences.write_text(
        json.dumps({"backup_encryption_enabled": "1"}),
        encoding="utf-8",
    )
    guard = DataProtectionGuard(
        repo.db.path,
        supported_schema_version=repo.db.CURRENT_SCHEMA_VERSION,
        current_app_version="3.24.0",
        attachments_directory=repo.paths.attachments,
        history_directory=repo.paths.history,
        preferences_file=repo.paths.backup_preferences,
        protection_directory=repo.paths.protections,
        protection_password=password,
    )
    snapshot = guard.create_snapshot(
        repo.db.conn,
        source_app_version="3.23.0",
        source_schema_version=repo.db.CURRENT_SCHEMA_VERSION,
        tables=guard.capture_tables(repo.db.conn),
        files=guard.capture_files(),
    )

    assert snapshot.suffix == ".lgbak"
    assert BackupService.is_encrypted_backup(snapshot)
    with guard._readable_archive(snapshot) as readable:
        manifest = guard.validate_archive(readable)
        with zipfile.ZipFile(readable, "r") as archive:
            extracted = snapshot.parent / "inspect.sqlite3"
            extracted.write_bytes(archive.read(manifest["database"]["archive_name"]))
        connection = sqlite3.connect(extracted)
        try:
            assert (
                connection.execute(
                    "SELECT value FROM settings WHERE key='ai_gemini_api_key'"
                ).fetchone()
                is None
            )
        finally:
            connection.close()
            extracted.unlink(missing_ok=True)
    assert not (snapshot.parent / ".private_work").exists()


def test_backup_status_uses_the_actual_latest_file(repo: Repo) -> None:
    secret_store = _SecretStore()
    service = BackupService(
        repo,
        database_file=repo.db.path,
        backup_directory=repo.paths.backups,
        attachments_directory=repo.paths.attachments,
        history_directory=repo.paths.history,
        preferences_file=repo.paths.backup_preferences,
        secret_store=secret_store,
    )
    service.save_preferences(1440, 10, False, encryption_enabled=False)
    plain = service.create_manual_backup()
    assert plain.suffix == ".zip"

    service.save_preferences(1440, 10, False, encryption_enabled=True)
    status = service.protection_health_snapshot()
    assert status["encryption_requested"] is True
    assert status["encrypted"] is False
    assert "ainda não está cifrado" in status["message"]

    encrypted = service.create_manual_backup()
    assert encrypted.suffix == ".lgbak"
    assert service.protection_health_snapshot()["encrypted"] is True


def test_interrupted_plaintext_workspace_is_removed_on_next_service_start(
    repo: Repo,
) -> None:
    leaked = repo.paths.backups / ".private_work" / "old-process" / "payload.zip"
    leaked.parent.mkdir(parents=True, exist_ok=True)
    leaked.write_bytes(b"plaintext financial data")
    system_leaked = (
        system_private_parent()
        / ".private_work"
        / "old-validation"
        / "database.sqlite3"
    )
    system_leaked.parent.mkdir(parents=True, exist_ok=True)
    system_leaked.write_bytes(b"plaintext validation database")
    restore_leaked = (
        repo.db.path.parent
        / ".private_work"
        / "old-version-restore"
        / "restored.sqlite3"
    )
    restore_leaked.parent.mkdir(parents=True, exist_ok=True)
    restore_leaked.write_bytes(b"plaintext version restore")

    BackupService(
        repo,
        database_file=repo.db.path,
        backup_directory=repo.paths.backups,
        attachments_directory=repo.paths.attachments,
        history_directory=repo.paths.history,
        preferences_file=repo.paths.backup_preferences,
        secret_store=_SecretStore(),
    )
    DataProtectionGuard(
        repo.db.path,
        supported_schema_version=repo.db.CURRENT_SCHEMA_VERSION,
        current_app_version="3.24.0",
        attachments_directory=repo.paths.attachments,
        history_directory=repo.paths.history,
        preferences_file=repo.paths.backup_preferences,
        protection_directory=repo.paths.protections,
    )

    assert not leaked.exists()
    assert not (repo.paths.backups / ".private_work").exists()
    assert not system_leaked.exists()
    assert not (system_private_parent() / ".private_work").exists()
    assert not restore_leaked.exists()
    assert not (repo.db.path.parent / ".private_work").exists()
