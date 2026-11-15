from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from database.engine import FinanceDB
from database.read_only import CancellableReadOperation, open_read_only_connection
from database.repo import Repo
from services.analytics import AnalyticsService
from services.payments import PaymentsService
from ui.dashboard_worker import DashboardWorker


def test_read_only_open_never_creates_a_missing_database(tmp_path: Path) -> None:
    missing = tmp_path / "nao_existe.sqlite3"

    with pytest.raises(FileNotFoundError):
        open_read_only_connection(missing)

    assert not missing.exists()


def test_read_only_finance_database_refuses_writes(tmp_path: Path) -> None:
    path = tmp_path / "somente_leitura.sqlite3"
    writable = FinanceDB(path)
    writable.conn.close()

    reader = FinanceDB.open_read_only(path)
    try:
        assert reader.scalar("SELECT COUNT(*) FROM transactions") >= 0
        with pytest.raises(sqlite3.OperationalError, match="readonly|read-only"):
            reader.conn.execute("DELETE FROM transactions")
    finally:
        reader.close()


def test_dashboard_worker_recreates_services_on_a_read_only_database(
    tmp_path: Path,
) -> None:
    repo = Repo.__new__(Repo)
    repo.db = FinanceDB(tmp_path / "dashboard.sqlite3")
    analytics = AnalyticsService(repo, PaymentsService(repo))
    worker = DashboardWorker(
        1,
        repo,
        analytics,
        "2026-08-01",
        "2026-08-10",
        10,
        "daily",
        "none",
    )
    local_repo, _local_analytics = worker._local_services()
    try:
        assert int(local_repo.db.conn.execute("PRAGMA query_only").fetchone()[0]) == 1
        with pytest.raises(sqlite3.OperationalError, match="readonly|read-only"):
            local_repo.db.conn.execute("DELETE FROM transactions")
    finally:
        local_repo.db.close()
        repo.db.conn.close()


def test_cancellable_read_operation_keeps_one_consistent_snapshot(
    tmp_path: Path,
) -> None:
    path = tmp_path / "snapshot.sqlite3"
    writer = FinanceDB(path)
    initial = int(writer.scalar("SELECT COUNT(*) FROM transactions"))

    def read_twice(connection: sqlite3.Connection) -> tuple[int, int]:
        before = int(
            connection.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        )
        writer.conn.execute(
            """
            INSERT INTO transactions(date,description,amount,category,account,kind)
            VALUES('2026-08-10','Concorrente',-1.25,'Outros','Conta','expense')
            """
        )
        writer.conn.commit()
        after = int(
            connection.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        )
        return before, after

    try:
        before, after = CancellableReadOperation(path, read_twice)()
        assert before == after == initial
        assert int(writer.scalar("SELECT COUNT(*) FROM transactions")) == initial + 1
    finally:
        writer.conn.close()


def test_sqlite_cancellation_interrupts_a_running_statement(tmp_path: Path) -> None:
    path = tmp_path / "cancelavel.sqlite3"
    database = FinanceDB(path)
    database.conn.close()
    started = threading.Event()
    outcome: list[BaseException | str] = []

    def long_query(connection: sqlite3.Connection) -> int:
        started.set()
        return int(
            connection.execute(
                """
                WITH RECURSIVE counter(value) AS (
                    VALUES(0)
                    UNION ALL
                    SELECT value + 1 FROM counter WHERE value < 100000000
                )
                SELECT SUM(value) FROM counter
                """
            ).fetchone()[0]
        )

    operation = CancellableReadOperation(path, long_query)

    def run() -> None:
        try:
            outcome.append(str(operation()))
        except BaseException as exc:  # capture the worker-thread outcome for assertion
            outcome.append(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert started.wait(1.0)
    operation.cancel()
    thread.join(2.0)

    assert not thread.is_alive()
    assert outcome and isinstance(outcome[0], sqlite3.OperationalError)
    assert "interrupt" in str(outcome[0]).casefold()


def test_filtered_movement_reader_does_not_create_a_ghost_database(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "fantasma.sqlite3"
    repo = Repo.__new__(Repo)
    repo.db = SimpleNamespace(path=missing)
    operation = repo.transactions_filtered_payload_operation(limit=10)

    with pytest.raises(FileNotFoundError):
        operation()

    assert not missing.exists()


def test_scheduled_occurrence_foreign_keys_cascade_and_null_transaction(
    tmp_path: Path,
) -> None:
    database = FinanceDB(tmp_path / "foreign_keys.sqlite3")
    connection = database.conn
    try:
        payment_id = int(
            connection.execute(
                """
                INSERT INTO scheduled_payments(
                    name,category,amount,start_date,debit_day,months_total
                ) VALUES('Prestação','Casa e contas',75.50,'2026-08-01',5,12)
                """
            ).lastrowid
        )
        transaction_id = int(
            connection.execute(
                """
                INSERT INTO transactions(date,description,amount,category,account,kind)
                VALUES('2026-08-05','Prestação',-75.50,'Casa e contas','Conta','expense')
                """
            ).lastrowid
        )
        connection.execute(
            """
            INSERT INTO scheduled_payment_occurrences(scheduled_id,due_date,transaction_id)
            VALUES(?,?,?)
            """,
            (payment_id, "2026-08-05", transaction_id),
        )
        connection.commit()

        connection.execute("DELETE FROM transactions WHERE id=?", (transaction_id,))
        assert (
            connection.execute(
                "SELECT transaction_id FROM scheduled_payment_occurrences"
            ).fetchone()[0]
            is None
        )
        connection.execute("DELETE FROM scheduled_payments WHERE id=?", (payment_id,))
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM scheduled_payment_occurrences"
            ).fetchone()[0]
            == 0
        )
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("statement", "params"),
    [
        (
            """
            INSERT INTO transactions(date,description,amount,category,account,kind)
            VALUES(?,?,?,?,?,?)
            """,
            ("2026-02-31", "Data inválida", -12.34, "Outros", "Conta", "expense"),
        ),
        (
            """
            INSERT INTO transactions(date,description,amount,category,account,kind)
            VALUES(?,?,?,?,?,?)
            """,
            ("2026-08-10", "Valor inválido", "texto", "Outros", "Conta", "expense"),
        ),
        (
            """
            INSERT INTO subscriptions(name,amount,category,renewal_day,period,active)
            VALUES(?,?,?,?,?,?)
            """,
            ("Renovação impossível", 10.0, "Subscrições", 32, "Mensal", 1),
        ),
        (
            "INSERT INTO saved_filters(name,filter_json) VALUES(?,?)",
            ("Filtro partido", "{sem json}"),
        ),
    ],
)
def test_database_constraints_reject_invalid_direct_writes(
    tmp_path: Path,
    statement: str,
    params: tuple,
) -> None:
    database = FinanceDB(tmp_path / "constraints.sqlite3")
    try:
        with pytest.raises(sqlite3.IntegrityError):
            database.conn.execute(statement, params)
        database.conn.rollback()
    finally:
        database.conn.close()


def test_database_constraints_keep_decimal_euro_values(tmp_path: Path) -> None:
    database = FinanceDB(tmp_path / "decimais.sqlite3")
    try:
        identifier = int(
            database.conn.execute(
                """
                INSERT INTO transactions(date,description,amount,category,account,kind)
                VALUES('2026-08-10','Valor decimal',-12.34,'Outros','Conta','expense')
                """
            ).lastrowid
        )
        stored = database.conn.execute(
            "SELECT amount FROM transactions WHERE id=?", (identifier,)
        ).fetchone()[0]
        assert stored == pytest.approx(-12.34)
    finally:
        database.conn.close()


def test_scheduled_occurrence_migration_preserves_existing_rows(
    tmp_path: Path,
) -> None:
    database = FinanceDB(tmp_path / "migration.sqlite3")
    connection = database.conn
    try:
        connection.execute("DROP TABLE scheduled_payment_occurrences")
        connection.execute(
            """
            CREATE TABLE scheduled_payment_occurrences (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scheduled_id INTEGER NOT NULL,
                due_date TEXT NOT NULL,
                transaction_id INTEGER,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(scheduled_id, due_date)
            )
            """
        )
        payment_id = int(
            connection.execute(
                """
                INSERT INTO scheduled_payments(name,amount,start_date,debit_day,months_total)
                VALUES('Teste',20.25,'2026-08-01',10,2)
                """
            ).lastrowid
        )
        transaction_id = int(
            connection.execute(
                """
                INSERT INTO transactions(date,description,amount,category,account,kind)
                VALUES('2026-08-10','Teste',-20.25,'Outros','Conta','expense')
                """
            ).lastrowid
        )
        occurrence_id = int(
            connection.execute(
                """
                INSERT INTO scheduled_payment_occurrences(scheduled_id,due_date,transaction_id)
                VALUES(?,?,?)
                """,
                (payment_id, "2026-08-10", transaction_id),
            ).lastrowid
        )
        connection.commit()

        database._migration_scheduled_occurrence_foreign_keys()

        row = connection.execute(
            "SELECT * FROM scheduled_payment_occurrences WHERE id=?",
            (occurrence_id,),
        ).fetchone()
        assert row is not None
        assert int(row["scheduled_id"]) == payment_id
        assert int(row["transaction_id"]) == transaction_id
        foreign_keys = {
            (str(item["from"]), str(item["table"]), str(item["on_delete"]).upper())
            for item in connection.execute(
                "PRAGMA foreign_key_list(scheduled_payment_occurrences)"
            )
        }
        assert ("scheduled_id", "scheduled_payments", "CASCADE") in foreign_keys
        assert ("transaction_id", "transactions", "SET NULL") in foreign_keys
    finally:
        connection.close()


def test_scheduled_occurrence_migration_never_drops_orphaned_legacy_data(
    tmp_path: Path,
) -> None:
    database = FinanceDB(tmp_path / "orphan_migration.sqlite3")
    connection = database.conn
    try:
        connection.execute("DROP TABLE scheduled_payment_occurrences")
        connection.execute(
            """
            CREATE TABLE scheduled_payment_occurrences (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scheduled_id INTEGER NOT NULL,
                due_date TEXT NOT NULL,
                transaction_id INTEGER,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(scheduled_id, due_date)
            )
            """
        )
        connection.execute(
            """
            INSERT INTO scheduled_payment_occurrences(scheduled_id,due_date)
            VALUES(999999,'2026-08-10')
            """
        )
        connection.commit()

        with pytest.raises(sqlite3.IntegrityError, match="orphan"):
            database._migration_scheduled_occurrence_foreign_keys()

        assert (
            connection.execute(
                "SELECT COUNT(*) FROM scheduled_payment_occurrences"
            ).fetchone()[0]
            == 1
        )
        assert (
            connection.execute(
                "SELECT scheduled_id FROM scheduled_payment_occurrences"
            ).fetchone()[0]
            == 999999
        )
    finally:
        connection.close()
