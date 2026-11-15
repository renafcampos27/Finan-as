"""Opt-in A12 serial benchmark; synthetic profiles only, no timing thresholds."""

import ast
import importlib
import json
import math
import os
import statistics
import time
import tracemalloc
from pathlib import Path

import pytest

from database.engine import FinanceDB
from database.repo import Repo


pytestmark = pytest.mark.skipif(
    os.environ.get("FINANCAS_PESSOAIS_PHASE5_BENCHMARK") != "1",
    reason="Opt-in controlled serial benchmark; not a unit-test timing gate",
)


def reference_method(relative_path, class_name, method_name):
    root = Path(__file__).resolve().parents[1]
    path = root / "artifacts/council_2026-10-31_phase5/before" / relative_path
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    cls = next(
        item
        for item in tree.body
        if isinstance(item, ast.ClassDef) and item.name == class_name
    )
    fn = next(
        item
        for item in cls.body
        if isinstance(item, ast.FunctionDef) and item.name == method_name
    )
    module = importlib.import_module(
        relative_path.removesuffix(".py").replace("/", ".")
    )
    namespace = dict(vars(module))
    source = "from __future__ import annotations\n" + ast.unparse(fn)
    exec(compile(source, str(path), "exec"), namespace)
    return namespace[method_name]


def measured(operation, samples):
    statements = []
    connection = operation.connection
    connection.set_trace_callback(statements.append)
    try:
        operation()
    finally:
        connection.set_trace_callback(None)
    tracemalloc.start()
    try:
        operation()
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return {
        "p50_ms": statistics.median(samples),
        "p95_ms": sorted(samples)[math.ceil(0.95 * len(samples)) - 1],
        "samples_ms": samples,
        "python_peak_bytes": peak,
        "select_count": sum(
            sql.lstrip().upper().startswith(("SELECT", "WITH")) for sql in statements
        ),
        "identity_select_count": sum(
            sql.lstrip().upper().startswith(("SELECT", "WITH"))
            and any(
                table in sql
                for table in ["financial_accounts", "categories", "entity_aliases"]
            )
            for sql in statements
        ),
    }


def measured_pair(connection, before, after):
    functions = {"reference": before, "optimized": after}
    for _ in range(2):
        for function in functions.values():
            function()
    samples = {key: [] for key in functions}
    for index in range(9):
        for key in list(functions) if index % 2 == 0 else list(reversed(functions)):
            start = time.perf_counter()
            functions[key]()
            samples[key].append((time.perf_counter() - start) * 1000)
    return {
        key: measured(Operation(connection, function), samples[key])
        for key, function in functions.items()
    }


class Operation:
    def __init__(self, connection, function):
        self.connection = connection
        self.function = function

    def __call__(self):
        return self.function()


def assert_balances_equal(expected, actual):
    def key(row):
        return row["account_id"], row["account"], row["orphan"]

    expected, actual = sorted(expected, key=key), sorted(actual, key=key)
    assert len(expected) == len(actual)
    for old, new in zip(expected, actual):
        assert {k: v for k, v in old.items() if k != "balance"} == {
            k: v for k, v in new.items() if k != "balance"
        }
        assert new["balance"] == pytest.approx(old["balance"], rel=0, abs=1e-8)


@pytest.mark.parametrize("size", [1000, 10000, 100000])
def test_serial_financial_benchmark(tmp_path, empty_profile, size):
    db = FinanceDB(tmp_path / "synthetic.sqlite3")
    repo = Repo(db)
    try:
        accounts = [repo.account_identity(f"Account {i}") for i in range(8)]
        for i, (aid, _) in enumerate(accounts):
            db.conn.execute(
                "UPDATE financial_accounts SET reference_balance=?,reference_date=?,balance_known=? WHERE stable_id=?",
                (
                    1000 + i * 100,
                    "" if i == 7 else ("2025-06-15" if i % 2 else "2026-06-15"),
                    0 if i == 7 else 1,
                    aid,
                ),
            )
        db.conn.commit()
        category_id, category = repo.category_identity("Diversos")
        values = []
        for i in range(size):
            aid, account = accounts[i % 8]
            day = f"2025-{i % 12 + 1:02d}-{i % 28 + 1:02d}"
            amount = ((i % 100) + 1) / 100 * (1 if i % 9 == 0 else -1)
            values.append(
                (
                    day,
                    f"synthetic {i}",
                    amount,
                    category,
                    account,
                    "income" if amount > 0 else "expense",
                    category_id,
                    aid,
                )
            )
        db.conn.executemany(
            "INSERT INTO transactions(date,description,amount,category,account,kind,category_id,account_id) VALUES(?,?,?,?,?,?,?,?)",
            values,
        )
        db.conn.executemany(
            "INSERT INTO transaction_meta(transaction_id,payment_date) VALUES(?,?)",
            [(i + 1, f"2026-{i % 12 + 1:02d}-01") for i in range(size) if i % 7 == 0],
        )
        db.conn.commit()
        # Completed monthly history with nontrivial JSON. Corrupt newest legacy
        # evidence exercises the required fallback; source validators are restored.
        payload = json.dumps(
            [
                {
                    "cost_basis": 123.456789,
                    "value": 150.987654,
                    "exchange_rate": 0.923456789,
                }
            ]
            * 20
        )
        db.conn.executemany(
            "INSERT INTO monthly_snapshots(month,holdings_json) VALUES(?,?)",
            [
                (f"{year}-{month:02d}", payload)
                for year in range(1990, 2020)
                for month in range(1, 13)
            ],
        )
        guards = db.conn.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name='monthly_snapshots'"
        ).fetchall()
        for guard in guards:
            db.conn.execute(f'DROP TRIGGER "{guard[0]}"')
        db.conn.execute(
            "INSERT INTO monthly_snapshots(month,holdings_json) VALUES('2020-01','invalid legacy JSON')"
        )
        for guard in guards:
            db.conn.execute(guard[1])
        db.conn.execute(
            "INSERT INTO investment_snapshots(day,invested,total) VALUES('2019-11-01',300,400)"
        )
        db.conn.commit()

        old_balance = reference_method(
            "database/engine.py", "FinanceDB", "account_balances"
        )
        old_point = reference_method(
            "database/repo/analytics.py", "AnalyticsRepoMixin", "investment_state_as_of"
        )
        old_import = reference_method(
            "database/repo/transactions.py",
            "TransactionsRepoMixin",
            "import_transactions_batch",
        )
        assert_balances_equal(
            old_balance(db, "2025-12-31"), db.account_balances("2025-12-31")
        )
        assert old_point(repo, "2020-12-31") == repo.investment_state_as_of(
            "2020-12-31"
        )

        imports = [
            dict(
                date="2025-12-01",
                description=f"batch {i}",
                amount=-1.25 if i % 3 else 0.75,
                category="Diversos",
                account="Account 0",
            )
            for i in range(400)
        ]

        def importing(function):
            db.conn.execute("SAVEPOINT benchmark_import")
            try:
                ids = function(imports, commit=False)
                result = [
                    tuple(row)
                    for row in db.conn.execute(
                        "SELECT date,description,amount,category,account,kind,category_id,account_id FROM transactions WHERE id>=? ORDER BY id",
                        (ids[0],),
                    )
                ]
            finally:
                db.conn.execute("ROLLBACK TO benchmark_import")
                db.conn.execute("RELEASE benchmark_import")
            return result

        assert importing(
            lambda rows, **kwargs: old_import(repo, rows, **kwargs)
        ) == importing(repo.import_transactions_batch)

        groups = {
            "account_balances": (
                lambda: old_balance(db, "2025-12-31"),
                lambda: db.account_balances("2025-12-31"),
            ),
            "investment_point": (
                lambda: old_point(repo, "2020-12-31"),
                lambda: repo.investment_state_as_of("2020-12-31"),
            ),
            "import_400": (
                lambda: importing(
                    lambda rows, **kwargs: old_import(repo, rows, **kwargs)
                ),
                lambda: importing(repo.import_transactions_batch),
            ),
        }
        measurements = {}
        for name, (before, after) in groups.items():
            measurements[name] = measured_pair(db.conn, before, after)
        result = {
            "ledger_rows": size,
            "synthetic_accounts": 8,
            "active_accounts_including_default": len(repo.financial_accounts()),
            "monthly_snapshots": 361,
            "import_batch_rows": 400,
            "import_timing_includes": "metadata/ledger writes, result read and savepoint rollback",
            "python_memory_scope": "tracemalloc peak during operation, excludes SQLite native allocations and prebuilt inputs",
            "equivalent": True,
            "measurements": measurements,
        }
        print("PHASE5_BENCHMARK_JSON=" + json.dumps(result))
    finally:
        db.conn.close()
