"""A12 contracts: equivalent financial results with bounded database work."""

import json
import sqlite3

import pytest

from database.engine import FinanceDB
from database.repo import Repo


@pytest.fixture
def repo(tmp_path, empty_profile):
    db = FinanceDB(tmp_path / "performance.sqlite3")
    yield Repo(db)
    db.conn.close()


def trace_selects(connection, operation):
    queries = []
    connection.set_trace_callback(
        lambda sql: (
            queries.append(sql)
            if sql.lstrip().upper().startswith(("SELECT", "WITH"))
            else None
        )
    )
    try:
        result = operation()
    finally:
        connection.set_trace_callback(None)
    return result, queries


def test_balances_queries_do_not_grow_with_accounts(repo):
    for i in range(8):
        repo.ensure_financial_account(f"Account {i}")
    _, queries = trace_selects(
        repo.db.conn, lambda: repo.account_balances("2026-01-31")
    )
    assert len(queries) == 2


@pytest.mark.parametrize(
    "as_of, expected", [("2026-01-10", 1025), ("2026-01-20", 1000), ("2026-01-31", 980)]
)
def test_balance_reference_payment_date_and_refund_equivalence(repo, as_of, expected):
    aid, name = repo.account_identity("Bank")
    repo.db.conn.execute(
        "UPDATE financial_accounts SET reference_balance=1000,reference_date='2026-01-20',balance_known=1 WHERE stable_id=?",
        (aid,),
    )
    for day, amount in [("2026-01-15", -30), ("2026-01-18", 5), ("2026-01-25", -20)]:
        repo.add_transaction(day, "movement", amount, "Diversos", name, commit=False)
    repo.db.conn.commit()
    result = next(
        row for row in repo.account_balances(as_of) if row["account_id"] == aid
    )
    assert result["balance"] == expected
    assert result["balance_known"] is True
    # A purchase date before the cutoff must not include a future payment.
    tx = repo.add_transaction("2026-01-01", "future payment", -100, "Diversos", name)
    repo.db.conn.execute(
        "UPDATE transaction_meta SET payment_date='2026-02-01' WHERE transaction_id=?",
        (tx,),
    )
    # Plain movement creation does not necessarily create metadata.
    repo.db.conn.execute(
        "INSERT OR IGNORE INTO transaction_meta(transaction_id,payment_date) VALUES(?,'2026-02-01')",
        (tx,),
    )
    repo.db.conn.commit()
    assert (
        next(row for row in repo.account_balances(as_of) if row["account_id"] == aid)[
            "balance"
        ]
        == expected
    )


def test_history_unknown_accounts_and_orphans_are_preserved(repo):
    aid, name = repo.account_identity("Unknown reference")
    repo.add_transaction("2026-01-01", "refund", 2.25, "Diversos", name)
    repo.add_transaction("2026-01-02", "expense", -10, "Diversos", name)
    # Deactivation preserves the original account identity as orphan evidence.
    repo.db.conn.execute(
        "UPDATE financial_accounts SET is_active=0 WHERE stable_id=?", (aid,)
    )
    repo.db.conn.commit()
    result = next(
        row for row in repo.account_balances("2026-01-31") if row["account_id"] == aid
    )
    assert result["balance"] == -7.75
    assert result["source"] == "orphan_history"


def test_grouped_balance_legacy_identity_and_reference_boundaries():
    # A minimal synthetic ledger represents legacy evidence which current
    # identity triggers would normalize on new writes. No guard is weakened.
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript("""
        CREATE TABLE financial_accounts(id INTEGER PRIMARY KEY,stable_id TEXT,name TEXT,
            reference_balance REAL,reference_date TEXT,balance_known INTEGER,is_active INTEGER);
        CREATE TABLE transactions(id INTEGER PRIMARY KEY,date TEXT,amount REAL,account TEXT,account_id TEXT);
        CREATE TABLE transaction_meta(transaction_id INTEGER PRIMARY KEY,payment_date TEXT);
        INSERT INTO financial_accounts VALUES
            (1,'known','Known',100,'2025-01-02',1,1),
            (2,'future','Future',200,'2025-02-01',1,1),
            (3,'unknown','Unknown',999,'',0,1),
            (4,'inactive','Inactive',0,'',0,0);
        INSERT INTO transactions VALUES
            (1,'2025-01-01',-10,'old display','known'),
            (2,'2025-01-03',2,'Known',''),
            (3,'2025-01-04',-3,'Known','missing'),
            (4,'2025-01-04',4,'',''),
            (5,'2026-01-01',-500,'Known','known'),
            (6,'2025-01-15',-5,'Future','future'),
            (7,'2025-01-04',7,'Unknown','unknown'),
            (8,'2025-01-04',8,'Unknown','inactive'),
            (9,'2025-03-01',-90,'Known','missing'),
            (10,'bad-date',-1,'Known','known');
    """)
    db = object.__new__(FinanceDB)
    db.conn = connection
    try:
        result = db.account_balances("2025-01-10")
        assert {
            (row["account_id"], row["account"]): row["balance"] for row in result
        } == {
            ("known", "Known"): 102,
            ("future", "Future"): 205,
            ("unknown", "Unknown"): 7,
            ("missing", "Known"): -3,
            ("", "Conta principal"): 4,
            ("inactive", "Unknown"): 8,
        }
    finally:
        connection.close()


def test_point_query_never_builds_entire_evolution(repo, monkeypatch):
    repo.db.conn.execute(
        "INSERT INTO investment_snapshots(day,invested,total) VALUES('2025-01-01',100,125)"
    )
    repo.db.conn.commit()
    monkeypatch.setattr(
        repo,
        "investment_evolution",
        lambda: pytest.fail("full series requested for one point"),
    )
    assert repo.investment_state_as_of("2025-01-31")["total"] == 125


def test_point_query_skips_invalid_monthly_json_and_daily_wins_ties(repo):
    # Represent unreadable legacy evidence without relaxing the production guard.
    guards = repo.db.conn.execute(
        "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name='monthly_snapshots'"
    ).fetchall()
    for guard in guards:
        repo.db.conn.execute(f'DROP TRIGGER "{guard[0]}"')
    for month, payload in [
        ("2025-01", [{"cost_basis": 100, "value": 130, "exchange_rate": 0.9}]),
        ("2025-02", "invalid"),
        ("2025-03", {"wrong": "shape"}),
    ]:
        repo.db.conn.execute(
            "INSERT INTO monthly_snapshots(month,holdings_json) VALUES(?,?)",
            (month, payload if isinstance(payload, str) else json.dumps(payload)),
        )
    for guard in guards:
        repo.db.conn.execute(guard[1])
    repo.db.conn.commit()
    result = repo.investment_state_as_of("2025-03-31")
    assert (result["invested"], result["total"], result["as_of"]) == (
        90,
        117,
        "2025-01-01",
    )
    repo.db.conn.execute(
        "INSERT INTO investment_snapshots(day,invested,total) VALUES('2025-01-01',150,175)"
    )
    repo.db.conn.commit()
    assert repo.investment_state_as_of("2025-01-31")["total"] == 175


def test_import_identity_reads_are_per_distinct_operation_key(repo):
    repo.ensure_financial_account("Bank")
    rows = [
        dict(
            date="2025-01-01",
            description=f"row {i}",
            amount=-1,
            category="Diversos",
            account="Bank",
        )
        for i in range(100)
    ]
    ids, queries = trace_selects(
        repo.db.conn, lambda: repo.import_transactions_batch(rows)
    )
    identity_reads = [
        sql
        for sql in queries
        if "financial_accounts" in sql or "categories" in sql or "entity_aliases" in sql
    ]
    assert len(ids) == 100
    assert len(identity_reads) <= 10


def test_batch_cache_does_not_survive_rollback_or_new_operation(repo):
    repo.import_transactions_batch(
        [
            dict(
                date="2025-01-01",
                description="one",
                amount=-1,
                category="Diversos",
                account="Transient",
            )
        ],
        commit=False,
    )
    repo.db.conn.rollback()
    assert not repo.db.conn.execute(
        "SELECT 1 FROM financial_accounts WHERE name='Transient'"
    ).fetchone()
    ids = repo.import_transactions_batch(
        [
            dict(
                date="2025-01-01",
                description="two",
                amount=1,
                category="Diversos",
                account="Transient",
            )
        ]
    )
    row = repo.db.conn.execute(
        "SELECT t.account_id,t.kind,c.kind FROM transactions t JOIN categories c ON t.category_id=c.stable_id WHERE t.id=?",
        (ids[0],),
    ).fetchone()
    assert row[0] and row[1] == "income" and row[2] == "expense"


def test_batch_identity_cache_preserves_aliases_and_refund_polarity(repo):
    from services.account_identity import account_identity_key
    from services.category_taxonomy import category_identity_key

    aid, _ = repo.account_identity("Current Bank")
    cid, category = repo.category_identity("Diversos")
    repo.db.conn.executemany(
        "INSERT INTO entity_aliases(entity_type,entity_id,alias,identity_key) VALUES(?,?,?,?)",
        [
            ("account", aid, "Old Bank", account_identity_key("Old Bank")),
            ("category", cid, "Old Misc", category_identity_key("Old Misc")),
        ],
    )
    repo.db.conn.commit()
    ids = repo.import_transactions_batch(
        [
            dict(
                date="2025-01-01",
                description="purchase",
                amount=-10,
                account="Old Bank",
                category="Old Misc",
            ),
            dict(
                date="2025-01-02",
                description="refund",
                amount=2,
                account="Old Bank",
                category="Old Misc",
            ),
            dict(
                date="2025-01-03",
                description="current",
                amount=-1,
                account="Current Bank",
                category=category,
            ),
        ]
    )
    rows = repo.db.conn.execute(
        "SELECT account_id,category_id,account,category,kind FROM transactions ORDER BY id"
    ).fetchall()
    assert len(ids) == len(rows) == 3
    assert all(tuple(row[:4]) == (aid, cid, "Current Bank", category) for row in rows)
    assert [row[4] for row in rows] == ["expense", "income", "expense"]


def test_financial_selectors_do_not_duplicate_original_and_payment_period(repo):
    tx = repo.add_transaction("2024-12-31", "paid later", -10, "Diversos", "Bank")
    repo.db.conn.execute(
        "INSERT INTO transaction_meta(transaction_id,payment_date) VALUES(?,'2025-01-02')",
        (tx,),
    )
    repo.db.conn.commit()
    assert 2025 in repo.available_years() and 2024 not in repo.available_years()
    assert "2025-01" in repo.report_months() and "2024-12" not in repo.report_months()


def test_new_database_enables_incremental_mode_before_wal(repo):
    assert repo.db.conn.execute("PRAGMA auto_vacuum").fetchone()[0] == 2
    assert repo.db.conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_incremental_vacuum_reports_actual_empty_freelist_delta(repo):
    assert repo.incremental_vacuum(1000) == 0


@pytest.mark.parametrize(
    "operation", ["incremental_vacuum", "optimize_storage", "optimize_database"]
)
def test_maintenance_never_commits_caller_transaction(repo, operation):
    repo.db.conn.execute("INSERT INTO settings(key,value) VALUES('pending-caller','1')")
    with pytest.raises(RuntimeError, match="(?i)(transa|pending)"):
        getattr(repo, operation)()
    assert repo.db.conn.in_transaction
    repo.db.conn.rollback()
    assert not repo.db.conn.execute(
        "SELECT 1 FROM settings WHERE key='pending-caller'"
    ).fetchone()


def test_incremental_vacuum_consumes_cursor_and_measures_real_delta(tmp_path):
    connection = sqlite3.connect(tmp_path / "pages.sqlite3")
    connection.execute("PRAGMA auto_vacuum=2")
    connection.execute("CREATE TABLE payload(id INTEGER PRIMARY KEY, data BLOB)")
    connection.executemany("INSERT INTO payload(data) VALUES(?)", [(b"x" * 8192,)] * 40)
    connection.commit()
    connection.execute("DELETE FROM payload")
    connection.commit()
    db = object.__new__(FinanceDB)
    db.conn = connection
    db._read_only = False
    before = connection.execute("PRAGMA freelist_count").fetchone()[0]
    actual = db.incremental_vacuum(10)
    after = connection.execute("PRAGMA freelist_count").fetchone()[0]
    assert before > 10 and actual == before - after and 0 < actual <= 10
    connection.close()


def test_legacy_conversion_is_explicit_backed_up_and_preserves_data(repo, tmp_path):
    repo.db.conn.execute("PRAGMA auto_vacuum=NONE")
    repo.db.conn.execute("VACUUM")
    repo.db.conn.execute(
        "INSERT INTO settings(key,value) VALUES('preserved','fractional evidence 0.123456789')"
    )
    repo.db.conn.commit()
    repo.db.init_schema()
    assert repo.db.conn.execute("PRAGMA auto_vacuum").fetchone()[0] == 0
    backup_path = tmp_path / "before-maintenance.sqlite3"
    result = repo.enable_incremental_vacuum(backup_path)
    assert result["changed"] and result["auto_vacuum_mode"] == 2
    with sqlite3.connect(backup_path) as saved:
        assert saved.execute("PRAGMA auto_vacuum").fetchone()[0] == 0
        assert (
            saved.execute(
                "SELECT value FROM settings WHERE key='preserved'"
            ).fetchone()[0]
            == "fractional evidence 0.123456789"
        )
    assert (
        repo.db.conn.execute(
            "SELECT value FROM settings WHERE key='preserved'"
        ).fetchone()[0]
        == "fractional evidence 0.123456789"
    )


def test_legacy_conversion_refuses_insufficient_space_and_existing_backup(
    repo, tmp_path, monkeypatch
):
    from collections import namedtuple
    import database.engine as engine

    repo.db.conn.execute("PRAGMA auto_vacuum=NONE")
    repo.db.conn.execute("VACUUM")
    backup_path = tmp_path / "recovery.sqlite3"
    backup_path.write_bytes(b"keep this recovery evidence")
    with pytest.raises(FileExistsError):
        repo.enable_incremental_vacuum(backup_path)
    assert backup_path.read_bytes() == b"keep this recovery evidence"
    usage = namedtuple("Usage", "total used free")
    monkeypatch.setattr(engine.shutil, "disk_usage", lambda _: usage(100, 99, 1))
    absent = tmp_path / "not-created.sqlite3"
    with pytest.raises(OSError, match="Espaço"):
        repo.enable_incremental_vacuum(absent)
    assert not absent.exists()
    assert repo.db.conn.execute("PRAGMA auto_vacuum").fetchone()[0] == 0


def test_explicit_legacy_conversion_does_not_commit_caller(repo, tmp_path):
    repo.db.conn.execute(
        "INSERT INTO settings(key,value) VALUES('pending-explicit','evidence')"
    )
    destination = tmp_path / "not-created.sqlite3"
    with pytest.raises(RuntimeError, match="transação"):
        repo.enable_incremental_vacuum(destination)
    assert repo.db.conn.in_transaction and not destination.exists()
    repo.db.conn.rollback()
