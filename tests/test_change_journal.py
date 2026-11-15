from __future__ import annotations

from pathlib import Path

from database.engine import FinanceDB
from services.change_journal import ChangeJournalService


class RepoStub:
    def __init__(self, database: FinanceDB) -> None:
        self.db = database


def _add_transaction(database: FinanceDB, description: str) -> int:
    cursor = database.conn.execute(
        """
        INSERT INTO transactions(
            date,description,amount,category,account,kind,notes
        ) VALUES(?,?,?,?,?,?,?)
        """,
        (
            "2026-07-01",
            description,
            -12.5,
            "Outros",
            "Conta principal",
            "expense",
            "",
        ),
    )
    transaction_id = int(cursor.lastrowid)
    database.conn.execute(
        """
        INSERT INTO transaction_meta(
            transaction_id,merchant,payment_date,payment_month
        ) VALUES(?,?,?,?)
        """,
        (transaction_id, "Loja", "2026-07-01", "2026-07"),
    )
    database.conn.commit()
    return transaction_id


def test_delete_edit_and_import_are_reversible(tmp_path: Path) -> None:
    database = FinanceDB(tmp_path / "journal.sqlite3")
    repo = RepoStub(database)
    attachments = tmp_path / "anexos"
    journal = ChangeJournalService(
        repo,
        attachments_directory=attachments,
        journal_storage=tmp_path / "historico",
    )
    transaction_id = _add_transaction(database, "Movimento reversível")
    attachment = attachments / str(transaction_id) / "fatura.txt"
    attachment.parent.mkdir(parents=True)
    attachment.write_text("documento", encoding="utf-8")
    database.conn.execute(
        """
        INSERT INTO transaction_attachments(
            transaction_id,file_name,stored_name,stored_path
        ) VALUES(?,?,?,?)
        """,
        (transaction_id, "fatura.txt", "fatura.txt", str(attachment)),
    )
    database.conn.commit()

    deleted, preserved = journal.delete_transactions(
        [transaction_id],
        label="Eliminar movimento",
    )
    assert (deleted, preserved) == (1, 1)
    assert not attachment.exists()
    assert (
        database.scalar(
            "SELECT COUNT(*) FROM transactions WHERE id=?",
            (transaction_id,),
        )
        == 0
    )

    undone = journal.undo_latest()
    assert undone is not None
    assert attachment.read_text(encoding="utf-8") == "documento"
    assert (
        database.scalar(
            "SELECT COUNT(*) FROM transaction_attachments WHERE transaction_id=?",
            (transaction_id,),
        )
        == 1
    )

    before = journal.capture_transactions([transaction_id])
    database.conn.execute(
        "UPDATE transactions SET category='Lazer' WHERE id=?",
        (transaction_id,),
    )
    database.conn.commit()
    journal.record_transaction_snapshot(
        before,
        label="Editar categoria",
        action_type="edit",
    )
    journal.undo_latest()
    assert (
        database.scalar(
            "SELECT COUNT(*) FROM transactions WHERE id=? AND category='Outros'",
            (transaction_id,),
        )
        == 1
    )

    imported_id = _add_transaction(database, "Importado")
    journal.record_created_transactions(
        [imported_id],
        label="Importação CSV · 1 movimento",
    )
    journal.undo_latest()
    assert (
        database.scalar(
            "SELECT COUNT(*) FROM transactions WHERE id=?",
            (imported_id,),
        )
        == 0
    )
    assert len(journal.history()) == 3
    database.conn.close()
