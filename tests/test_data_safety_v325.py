from __future__ import annotations

import os
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from database.engine import FinanceDB
from database.protection import DataProtectionGuard
from database.repo import Repo
from services.change_journal import ChangeJournalService
from services.imports import ImportService
from services.payments import PaymentsService


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import utils.config as config

    monkeypatch.setattr(config, "DB_NAME", str(tmp_path / "v325.sqlite3"))
    instance = Repo()
    instance.db.conn.executescript(
        """
        DELETE FROM subscription_occurrences;
        DELETE FROM scheduled_payment_occurrences;
        DELETE FROM transaction_splits;
        DELETE FROM transaction_meta;
        DELETE FROM transactions;
        DELETE FROM subscriptions;
        DELETE FROM scheduled_payments;
        DELETE FROM goals;
        DELETE FROM change_journal;
        """
    )
    instance.db.conn.commit()
    yield instance
    try:
        instance.db.conn.close()
    except sqlite3.ProgrammingError:
        pass


def _journal(repo: Repo, tmp_path: Path) -> ChangeJournalService:
    return ChangeJournalService(
        repo,
        attachments_directory=tmp_path / "anexos",
        journal_storage=tmp_path / "historico",
    )


def test_duplicate_preserves_metadata_splits_and_can_be_undone(
    repo: Repo,
    tmp_path: Path,
) -> None:
    source_id = repo.add_transaction_with_meta(
        "2026-07-13",
        "Compra repartida",
        -100,
        "Compras",
        "Conta cartão",
        "Nota original",
        tags="casa, mensal",
        merchant="Loja",
        payment_method="Cartão de crédito",
        subcategory="Casa",
        purchase_date="2026-07-12",
        payment_date="2026-07-13",
    )
    repo.save_transaction_splits(
        source_id,
        [("Alimentação", 35), ("Casa", 65)],
    )
    journal = _journal(repo, tmp_path)

    created = journal.run_with_created_transactions(
        lambda: [repo.duplicate_transaction(source_id, commit=False)],
        label="Duplicação de teste",
        action_type="duplicate",
    )
    copy_id = created[0]

    source_splits = [dict(row) for row in repo.transaction_splits(source_id)]
    copy_splits = [dict(row) for row in repo.transaction_splits(copy_id)]
    assert copy_splits == source_splits
    assert repo.transaction_meta_ext(copy_id)["payment_method"] == "Cartão de crédito"
    assert repo.transaction_by_id(copy_id)["account"] == "Conta cartão"

    journal.undo_latest()
    assert repo.transaction_by_id(copy_id) is None
    assert repo.transaction_by_id(source_id) is not None
    assert [dict(row) for row in repo.transaction_splits(source_id)] == source_splits


def test_manual_creation_and_both_transfer_legs_are_reversible(
    repo: Repo,
    tmp_path: Path,
) -> None:
    journal = _journal(repo, tmp_path)
    created = journal.run_with_created_transactions(
        lambda: [
            repo.add_transaction_with_meta(
                "2026-07-13",
                "Movimento manual",
                -12.5,
                "Outros",
                "Conta principal",
                payment_method="Cartão",
                commit=False,
            )
        ],
        label="Novo movimento",
        action_type="create",
    )
    manual_id = created[0]
    assert repo.transaction_by_id(manual_id) is not None
    journal.undo_latest()
    assert repo.transaction_by_id(manual_id) is None

    transfer_ids = journal.run_with_created_transactions(
        lambda: list(
            repo.add_internal_transfer(
                "2026-07-13",
                50,
                "Conta A",
                "Conta B",
                commit=False,
            )
        ),
        label="Transferência interna",
        action_type="transfer",
    )
    assert len(transfer_ids) == 2
    journal.undo_latest()
    assert all(repo.transaction_by_id(tx_id) is None for tx_id in transfer_ids)


def test_category_change_removes_stale_splits_and_undo_restores_them(
    repo: Repo,
    tmp_path: Path,
) -> None:
    transaction_id = repo.add_transaction(
        "2026-07-13",
        "Compra mista",
        -100,
        "Compras",
        "Conta principal",
    )
    repo.save_transaction_splits(
        transaction_id,
        [("Alimentação", 40), ("Casa", 60)],
    )
    journal = _journal(repo, tmp_path)

    before = {
        row["category"]: float(row["total"])
        for row in repo.category_spend_with_splits(
            "2026-07-01",
            "2026-08-01",
        )
    }
    assert before == {"Casa": 60.0, "Supermercado": 40.0}

    journal.run_with_snapshot(
        [transaction_id],
        lambda: repo.update_transactions_category(
            [transaction_id],
            "Lazer",
            commit=False,
        ),
        label="Alterar categoria",
        action_type="bulk_edit",
    )
    assert repo.transaction_splits(transaction_id) == []
    after = repo.category_spend_with_splits("2026-07-01", "2026-08-01")
    assert [(row["category"], float(row["total"])) for row in after] == [
        ("Lazer", 100.0)
    ]

    journal.undo_latest()
    restored = {
        row["category"]: float(row["total"])
        for row in repo.category_spend_with_splits(
            "2026-07-01",
            "2026-08-01",
        )
    }
    assert restored == before


def test_subscription_routing_and_automatic_charge_are_reversible(
    repo: Repo,
    tmp_path: Path,
) -> None:
    today = date.today().isoformat()
    subscription_id = repo.add_subscription(
        "Ginásio",
        33,
        "Subscrições",
        date.today().day,
        "Mensal",
        next_renewal_date=today,
        account="Conta conjunta",
        payment_method="Débito direto",
    )
    before = dict(repo.subscription_by_id(subscription_id))
    journal = _journal(repo, tmp_path)

    def generate() -> list[int]:
        repo.process_due_subscriptions(commit=False)
        return list(repo.last_subscription_processing_report["created_ids"])

    created = journal.run_with_created_transactions(
        generate,
        label="Subscrição automática",
        action_type="automatic_subscription",
        state_targets={
            "subscriptions": {
                "ids": [subscription_id],
                "columns": ("last_used_date", "next_renewal_date"),
            }
        },
        delete_linked_occurrences=True,
    )
    transaction_id = created[0]
    assert repo.transaction_by_id(transaction_id)["account"] == "Conta conjunta"
    assert (
        repo.transaction_meta_ext(transaction_id)["payment_method"] == "Débito direto"
    )
    assert repo.scalar("SELECT COUNT(*) FROM subscription_occurrences") == 1

    journal.undo_latest()
    assert repo.transaction_by_id(transaction_id) is None
    assert repo.scalar("SELECT COUNT(*) FROM subscription_occurrences") == 0
    restored = dict(repo.subscription_by_id(subscription_id))
    assert restored["last_used_date"] == before["last_used_date"]
    assert restored["next_renewal_date"] == before["next_renewal_date"]


def test_scheduled_payment_generation_and_progress_are_reversible(
    repo: Repo,
    tmp_path: Path,
) -> None:
    due = date(2026, 10, 31)  # Saturday: the effective debit is Monday.
    payment_id = repo.add_scheduled_payment(
        "Seguro",
        "Seguros",
        "Automóvel",
        40,
        "Conta despesas",
        "Débito direto",
        due.isoformat(),
        due.day,
        1,
    )
    journal = _journal(repo, tmp_path)
    service = PaymentsService(repo, journal)

    assert service.generate_due_scheduled_payments(due) == 0
    assert service.generate_due_scheduled_payments(date(2026, 11, 2)) == 1
    occurrence = repo.q("SELECT * FROM scheduled_payment_occurrences")[0]
    transaction_id = int(occurrence["transaction_id"])
    assert repo.scheduled_payment_by_id(payment_id)["months_paid"] == 1
    assert repo.scheduled_payment_by_id(payment_id)["active"] == 0

    journal.undo_latest()
    assert repo.transaction_by_id(transaction_id) is None
    assert repo.scalar("SELECT COUNT(*) FROM scheduled_payment_occurrences") == 0
    restored = repo.scheduled_payment_by_id(payment_id)
    assert restored["months_paid"] == 0
    assert restored["active"] == 1


def test_creation_and_companion_state_roll_back_if_journal_write_fails(
    repo: Repo,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    goal_id = repo.add_goal("Reserva", 1_000, 100, 50, "2027-01-01")
    journal = _journal(repo, tmp_path)

    def fail_journal(*_args, **_kwargs):
        raise RuntimeError("simulated journal failure")

    monkeypatch.setattr(journal, "_insert_journal", fail_journal)
    with pytest.raises(RuntimeError, match="journal failure"):
        journal.run_with_created_transactions(
            lambda: [
                repo.adjust_goal_with_transaction(
                    goal_id,
                    25,
                    deposit=True,
                    transaction_date=date.today().isoformat(),
                    commit=False,
                )
            ],
            label="Depósito",
            action_type="goal_deposit",
            state_targets={"goals": {"ids": [goal_id], "columns": ("saved_amount",)}},
        )

    assert repo.goal_by_id(goal_id)["saved_amount"] == 100
    assert repo.scalar("SELECT COUNT(*) FROM transactions") == 0
    assert repo.scalar("SELECT COUNT(*) FROM change_journal") == 0


def test_csv_structure_and_all_rows_are_strictly_validated(
    repo: Repo,
    tmp_path: Path,
) -> None:
    service = ImportService(repo)
    duplicate_headers = tmp_path / "duplicate_headers.csv"
    duplicate_headers.write_text(
        "Data; data ;Valor\n2026-07-13;Compra;-10,00\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="cabeçalhos CSV repetidos"):
        service.read_csv(duplicate_headers)

    extra_column = tmp_path / "extra_column.csv"
    extra_column.write_text(
        "Data;Descrição;Valor\n2026-07-13;Compra;-10,00;extra\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="colunas a mais"):
        service.read_csv(extra_column)

    mapping = {
        "date": "Data",
        "desc": "Descrição",
        "amount": "Valor",
        "category": "",
        "account": "",
        "method": "",
        "merchant": "",
        "reference": "",
    }
    rows = [
        {
            "Data": "2026-07-13",
            "Descrição": f"Compra {index}",
            "Valor": "-10,00",
        }
        for index in range(100)
    ]
    rows[90]["Data"] = "2026-07-13XYZ"
    rows[95]["Valor"] = "-10,009"
    preview = service.preview(rows, mapping, apply_rules=False, limit=80)

    assert preview.total == 100
    assert len(preview.rows) == 80
    assert preview.invalid == 2
    assert (
        service.normalize_row(
            {"Data": "2026-07-13XYZ", "Descrição": "X", "Valor": "-1,00"},
            mapping,
            apply_rules=False,
        ).is_valid
        is False
    )
    assert (
        service.normalize_row(
            {"Data": "2026-07-13", "Descrição": "X", "Valor": "-1,001"},
            mapping,
            apply_rules=False,
            amount_locale="pt",
        ).is_valid
        is False
    )


def test_version_protection_retention_is_bounded_and_preserves_recovery_files(
    tmp_path: Path,
) -> None:
    protection_directory = tmp_path / "proteções"
    protection_directory.mkdir()
    guard = DataProtectionGuard(
        tmp_path / "financas.sqlite3",
        supported_schema_version=19,
        current_app_version="3.25.0",
        protection_directory=protection_directory,
    )
    snapshots: list[Path] = []
    for index in range(5):
        path = protection_directory / (
            f"antes_atualizacao_v3.{index}_para_v3.25.0_{index}.zip"
        )
        path.write_bytes(bytes([index]) * 20)
        os.utime(path, (1_700_000_000 + index, 1_700_000_000 + index))
        snapshots.append(path)
    manual = protection_directory / "backup_manual.zip"
    recovery = protection_directory / "restauro_rollback_123.zip"
    manual.write_bytes(b"manual")
    recovery.write_bytes(b"recovery")

    removed = guard.prune_version_snapshots(
        keep=2,
        max_total_bytes=1_000,
        protected_paths=[snapshots[0]],
    )

    remaining = sorted(protection_directory.glob("antes_atualizacao_v*.zip"))
    assert len(remaining) == 2
    assert snapshots[0] in remaining
    assert snapshots[-1] in remaining
    assert len(removed) == 3
    assert manual.is_file()
    assert recovery.is_file()


def test_real_legacy_schema_adds_subscription_routing_non_destructively(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "legacy.sqlite3"
    kwargs = {
        "attachments_directory": tmp_path / "anexos",
        "history_directory": tmp_path / "historico",
        "preferences_file": tmp_path / "preferencias.json",
        "protection_directory": tmp_path / "proteções",
    }
    database = FinanceDB(database_path, **kwargs)
    database.conn.execute(
        "INSERT INTO subscriptions("
        "name,amount,category,renewal_day,period,account,payment_method"
        ") VALUES(?,?,?,?,?,?,?)",
        ("Legada", 8.5, "Subscrições", 12, "Mensal", "Conta antiga", "Cartão"),
    )
    database.conn.executescript(
        """
        DROP TRIGGER IF EXISTS validate_subscriptions_insert;
        DROP TRIGGER IF EXISTS validate_subscriptions_update;
        DROP TRIGGER IF EXISTS trg_subscriptions_identity_insert;
        DROP TRIGGER IF EXISTS trg_subscriptions_identity_update;
        DROP TRIGGER IF EXISTS trg_categories_display_name;
        DROP TRIGGER IF EXISTS trg_accounts_display_name;
        DROP INDEX IF EXISTS idx_subscriptions_category_account_ids;
        ALTER TABLE subscriptions DROP COLUMN payment_method;
        ALTER TABLE subscriptions DROP COLUMN account;
        ALTER TABLE subscriptions DROP COLUMN category_id;
        ALTER TABLE subscriptions DROP COLUMN account_id;
        DELETE FROM schema_migrations WHERE version>=16;
        """
    )
    database.conn.commit()
    database.close()

    upgraded = FinanceDB(database_path, **kwargs)
    try:
        columns = {
            str(row["name"])
            for row in upgraded.query("PRAGMA table_info(subscriptions)")
        }
        row = upgraded.query("SELECT * FROM subscriptions WHERE name='Legada'")[0]
        assert upgraded.schema_version() == FinanceDB.CURRENT_SCHEMA_VERSION
        assert {"account", "payment_method"}.issubset(columns)
        assert row["amount"] == 8.5
        assert row["account"] == "Conta principal"
        assert row["payment_method"] == ""
        with pytest.raises(sqlite3.IntegrityError, match="account is required"):
            upgraded.conn.execute(
                "UPDATE subscriptions SET account='' WHERE id=?",
                (int(row["id"]),),
            )
    finally:
        upgraded.conn.rollback()
        upgraded.close()
