from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from database.repo import Repo
from services.change_journal import ChangeJournalService
from services.documents import DocumentService


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import utils.config as config

    monkeypatch.setattr(config, "DB_NAME", str(tmp_path / "integrity.sqlite3"))
    instance = Repo()
    yield instance
    instance.db.conn.close()


def test_create_movement_rolls_back_when_metadata_fails(repo: Repo) -> None:
    before = int(repo.scalar("SELECT COUNT(*) FROM transactions"))
    repo.db.conn.execute(
        """
        CREATE TRIGGER reject_new_movement_meta
        BEFORE INSERT ON transaction_meta
        BEGIN
            SELECT RAISE(ABORT, 'simulated metadata insert failure');
        END
        """
    )
    repo.db.conn.commit()

    with pytest.raises(sqlite3.IntegrityError, match="metadata insert failure"):
        repo.add_transaction_with_meta(
            "2026-07-17",
            "Compra que deve recuar",
            -45.0,
            "Outros",
            "Conta principal",
            tags="teste",
            merchant="Loja",
            payment_date="2026-07-17",
        )

    assert int(repo.scalar("SELECT COUNT(*) FROM transactions")) == before


def test_full_movement_read_keeps_workflow_metadata(repo: Repo) -> None:
    transaction_id = repo.add_transaction_with_meta(
        "2026-07-17",
        "Despesa reembolsável",
        -75.0,
        "Outros",
        "Conta principal",
        status="Revisto",
        is_reimbursable=1,
        payment_date="2026-07-17",
    )

    row = repo.transaction_full_by_id(transaction_id)

    assert row is not None
    assert row["status"] == "Revisto"
    assert int(row["is_reimbursable"]) == 1


def test_edit_movement_and_splits_roll_back_when_metadata_fails(repo: Repo) -> None:
    transaction_id = repo.add_transaction_with_meta(
        "2026-07-10",
        "Compra original",
        -100.0,
        "Outros",
        "Conta principal",
        tags="original",
        merchant="Loja original",
        payment_date="2026-07-10",
    )
    repo.save_transaction_splits(transaction_id, [("Outros", 100.0)])
    before_transaction = dict(repo.transaction_by_id(transaction_id))
    before_meta = repo.transaction_meta_ext(transaction_id)
    before_splits = [dict(row) for row in repo.transaction_splits(transaction_id)]
    repo.db.conn.execute(
        """
        CREATE TRIGGER reject_updated_movement_meta
        BEFORE UPDATE ON transaction_meta
        BEGIN
            SELECT RAISE(ABORT, 'simulated metadata update failure');
        END
        """
    )
    repo.db.conn.commit()

    with pytest.raises(sqlite3.IntegrityError, match="metadata update failure"):
        repo.update_transaction_with_meta(
            transaction_id,
            {
                "date": "2026-07-11",
                "description": "Compra alterada",
                "amount": -250.0,
                "category": "Outros",
                "account": "Outra conta",
            },
            tags="alterado",
            merchant="Outra loja",
            payment_date="2026-07-11",
        )

    assert dict(repo.transaction_by_id(transaction_id)) == before_transaction
    assert repo.transaction_meta_ext(transaction_id) == before_meta
    assert [
        dict(row) for row in repo.transaction_splits(transaction_id)
    ] == before_splits


def test_delete_keeps_database_and_file_when_database_step_fails(
    repo: Repo, tmp_path: Path
) -> None:
    transaction_id = repo.add_transaction_with_meta(
        "2026-07-12",
        "Movimento documentado",
        -60.0,
        "Outros",
        "Conta principal",
        tags="documentado",
        payment_date="2026-07-12",
    )
    repo.save_transaction_splits(transaction_id, [("Outros", 100.0)])
    source = tmp_path / "fatura_60,00.pdf"
    source.write_bytes(b"%PDF-1.4\ninvoice\n%%EOF\n")
    service = DocumentService(repo, attachments_directory=tmp_path / "anexos")
    stored = service.attach_file(transaction_id, source, "Fatura")
    repo.db.conn.execute(
        """
        CREATE TRIGGER reject_movement_meta_delete
        BEFORE DELETE ON transaction_meta
        BEGIN
            SELECT RAISE(ABORT, 'simulated delete failure');
        END
        """
    )
    repo.db.conn.commit()

    with pytest.raises(sqlite3.IntegrityError, match="simulated delete failure"):
        service.delete_transaction(transaction_id)

    assert stored.exists()
    assert repo.transaction_by_id(transaction_id) is not None
    assert len(repo.transaction_attachments(transaction_id)) == 1
    assert len(repo.transaction_splits(transaction_id)) == 1
    assert repo.transaction_meta_ext(transaction_id)

    repo.db.conn.execute("DROP TRIGGER reject_movement_meta_delete")
    repo.db.conn.commit()
    removed = service.delete_transaction(transaction_id)

    assert removed == 1
    assert not stored.exists()
    assert repo.transaction_by_id(transaction_id) is None
    assert repo.transaction_attachments(transaction_id) == []


def test_emergency_goal_switch_rolls_back_when_demotion_fails(repo: Repo) -> None:
    repo.db.conn.execute("DELETE FROM goals")
    repo.db.conn.commit()
    original_id = repo.add_goal(
        "Reserva atual",
        5000,
        1000,
        100,
        "2027-01-01",
        goal_type="emergency_fund",
    )
    repo.db.conn.execute(
        """
        CREATE TRIGGER reject_emergency_goal_demotion
        BEFORE UPDATE OF goal_type ON goals
        WHEN OLD.goal_type='emergency_fund' AND NEW.goal_type='standard'
        BEGIN
            SELECT RAISE(ABORT, 'simulated goal demotion failure');
        END
        """
    )
    repo.db.conn.commit()

    with pytest.raises(sqlite3.IntegrityError, match="goal demotion failure"):
        repo.add_goal(
            "Nova reserva",
            9000,
            0,
            200,
            "2028-01-01",
            goal_type="emergency_fund",
        )

    rows = repo.q("SELECT id,goal_type FROM goals ORDER BY id")
    assert [(int(row["id"]), row["goal_type"]) for row in rows] == [
        (original_id, "emergency_fund")
    ]


def test_goal_adjustment_and_matching_movement_roll_back_together(repo: Repo) -> None:
    goal_id = repo.add_goal("Viagem", 2000, 250, 50, "2027-08-01")
    before_movements = int(repo.scalar("SELECT COUNT(*) FROM transactions"))
    repo.db.conn.execute(
        """
        CREATE TRIGGER reject_goal_movement
        BEFORE INSERT ON transactions
        WHEN NEW.description LIKE 'Reforço do pote:%'
        BEGIN
            SELECT RAISE(ABORT, 'simulated goal movement failure');
        END
        """
    )
    repo.db.conn.commit()

    with pytest.raises(sqlite3.IntegrityError, match="goal movement failure"):
        repo.adjust_goal_with_transaction(
            goal_id,
            25,
            deposit=True,
            transaction_date="2026-08-10",
        )

    assert float(repo.goal_by_id(goal_id)["saved_amount"]) == pytest.approx(250)
    assert int(repo.scalar("SELECT COUNT(*) FROM transactions")) == before_movements


def test_split_replacement_restores_previous_rows_after_insert_failure(
    repo: Repo,
) -> None:
    transaction_id = repo.add_transaction(
        "2026-08-01",
        "Compra dividida",
        -100,
        "Outros",
        "Conta principal",
    )
    repo.save_transaction_splits(transaction_id, [("Original", 100)])
    repo.db.conn.execute(
        """
        CREATE TRIGGER reject_one_split
        BEFORE INSERT ON transaction_splits
        WHEN NEW.category='Falha'
        BEGIN
            SELECT RAISE(ABORT, 'simulated split failure');
        END
        """
    )
    repo.db.conn.commit()

    with pytest.raises(sqlite3.IntegrityError, match="split failure"):
        repo.save_transaction_splits(
            transaction_id,
            [("Casa", 50), ("Falha", 50)],
        )

    rows = [dict(row) for row in repo.transaction_splits(transaction_id)]
    assert [(row["category"], float(row["percent"])) for row in rows] == [
        ("Original", 100.0)
    ]


def test_import_and_undo_record_are_committed_as_one_unit(
    repo: Repo,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal = ChangeJournalService(
        repo,
        attachments_directory=tmp_path / "anexos",
        journal_storage=tmp_path / "historico",
    )
    before = int(repo.scalar("SELECT COUNT(*) FROM transactions"))

    def fail_journal(*_args, **_kwargs):
        raise sqlite3.OperationalError("simulated journal failure")

    monkeypatch.setattr(journal, "_insert_journal", fail_journal)
    with pytest.raises(sqlite3.OperationalError, match="journal failure"):
        journal.run_with_created_transactions(
            lambda: repo.import_transactions_batch(
                [
                    {
                        "date": "2026-08-10",
                        "description": "Importação atómica",
                        "amount": -18.5,
                        "category": "Outros",
                        "account": "Conta principal",
                    }
                ],
                commit=False,
            ),
            label="Importação de teste",
        )

    assert int(repo.scalar("SELECT COUNT(*) FROM transactions")) == before
    assert int(repo.scalar("SELECT COUNT(*) FROM change_journal")) == 0


def test_safe_commit_rolls_back_and_reports_failure(
    repo: Repo,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo.db.conn.execute(
        "INSERT INTO settings(key,value) VALUES('temporary_safe_commit','1')"
    )

    def fail_commit() -> None:
        raise sqlite3.OperationalError("simulated commit failure")

    monkeypatch.setattr(repo, "commit", fail_commit)
    assert repo.safe_commit("simulated safe commit") is False
    assert not repo.q("SELECT 1 FROM settings WHERE key='temporary_safe_commit'")
