"""R1-19: bound journal materialization while preserving undo evidence."""

from __future__ import annotations

import ast
import gc
import json
import math
import statistics
import time
import tracemalloc
from pathlib import Path

import pytest

from database.engine import FinanceDB
from database.repo import Repo
from services.change_journal import ChangeJournalService


def _before_prune():
    source = (
        Path(__file__).resolve().parents[1]
        / "artifacts/council_2026-10-31_phase5/before/services/change_journal.py"
    ).read_text(encoding="utf-8-sig")
    tree = ast.parse(source)
    cls = next(
        item
        for item in tree.body
        if isinstance(item, ast.ClassDef) and item.name == "ChangeJournalService"
    )
    method = next(
        item
        for item in cls.body
        if isinstance(item, ast.FunctionDef) and item.name == "prune"
    )
    isolated = ast.Module(body=[method], type_ignores=[])
    from services import change_journal

    namespace = dict(vars(change_journal))
    exec(
        compile(ast.fix_missing_locations(isolated), "<frozen-before-prune>", "exec"),
        namespace,
    )
    return namespace["prune"]


def test_retention_preserves_exact_recent_and_available_set(tmp_path, empty_profile):
    repo = Repo(FinanceDB(tmp_path / "retention.sqlite3"))
    journal = ChangeJournalService(repo)
    records = [(n * 7, ["undone", "available", "failed"][n % 3]) for n in range(1, 73)]
    try:
        repo.db.conn.executemany(
            "INSERT INTO change_journal(id,action_type,label,status) VALUES(?,'edit','Synthetic',?)",
            records,
        )
        repo.db.conn.commit()
        keep = 17
        expected = {identity for identity, _ in records[-keep:]}
        expected.update(
            identity for identity, status in records if status == "available"
        )
        repo.db.conn.execute("BEGIN IMMEDIATE")
        journal.prune(keep)
        assert repo.db.conn.in_transaction
        assert {row[0] for row in repo.q("SELECT id FROM change_journal")} == expected
        repo.db.conn.rollback()
        assert len(repo.q("SELECT id FROM change_journal")) == len(records)
        journal.prune(keep)
        assert not repo.db.conn.in_transaction
        assert {row[0] for row in repo.q("SELECT id FROM change_journal")} == expected
    finally:
        repo.db.conn.close()


@pytest.mark.parametrize("size", [1000, 10000, 100000])
def test_retention_reference_measurement(tmp_path, empty_profile, size):
    """No latency threshold: run serially and retain stdout for measured claims."""
    repo = Repo(FinanceDB(tmp_path / "measurement.sqlite3"))
    journal = ChangeJournalService(repo)
    reference = _before_prune()
    try:
        repo.db.conn.executemany(
            "INSERT INTO change_journal(action_type,label,status) VALUES('edit',?,'available')",
            ((f"Synthetic {n}",) for n in range(size)),
        )
        repo.db.conn.commit()
        methods = {
            "before": lambda: reference(journal),
            "after": journal.prune,
        }
        samples = {name: [] for name in methods}
        for method in methods.values():
            method()
        for index in range(9):
            for name in list(methods) if index % 2 else list(reversed(methods)):
                started = time.perf_counter_ns()
                methods[name]()
                samples[name].append((time.perf_counter_ns() - started) / 1_000_000)
        result = {"journal_rows": size, "retained_rows": size, "methods": {}}
        for name, method in methods.items():
            gc.collect()
            tracemalloc.start()
            method()
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            ordered = sorted(samples[name])
            result["methods"][name] = {
                "p50_ms": statistics.median(ordered),
                "p95_ms": ordered[math.ceil(len(ordered) * 0.95) - 1],
                "peak_python_bytes": peak,
                "samples": len(ordered),
            }
        assert repo.scalar("SELECT COUNT(*) FROM change_journal") == size
        assert not repo.db.conn.in_transaction
        print("RETENTION_MEASUREMENT=" + json.dumps(result, sort_keys=True))
    finally:
        repo.db.conn.close()
