"""R1-19: bound journal materialization while preserving undo evidence."""

from __future__ import annotations

import gc
import json
import math
import shutil
import statistics
import time
import tracemalloc

import pytest

from database.engine import FinanceDB
from database.repo import Repo
from services.change_journal import ChangeJournalService
from utils.app_logging import log_warning
from utils.helpers import chunked_iterable


def _legacy_materializing_prune(self: ChangeJournalService, keep: int = 100) -> None:
    """Frozen pre-Phase-5 reference, reviewed independently of local artifacts.

    Materializes every journal row before selecting obsolete IDs. This is the
    former retention implementation, not an estimate of historical timings.
    Keep its body unchanged so comparisons retain the original algorithm.
    """
    rows = self.repo.db.conn.execute(
        "SELECT id, status FROM change_journal ORDER BY id DESC"
    ).fetchall()
    keep_limit = max(10, int(keep))
    candidates = rows[keep_limit:]
    obsolete = [
        int(row["id"])
        for row in candidates
        if str(row["status"] or "").lower() != "available"
    ]
    if not obsolete:
        return
    owns_tx = not self.repo.db.conn.in_transaction
    for chunk in chunked_iterable(obsolete, chunk_size=400):
        placeholders = ",".join("?" for _ in chunk)
        self.repo.db.conn.execute(
            f"DELETE FROM change_journal WHERE id IN ({placeholders})",
            tuple(chunk),
        )
    if owns_tx:
        self.repo.db.conn.commit()
        for journal_id in obsolete:
            folder = self.journal_storage / str(journal_id)
            if folder.exists():
                try:
                    shutil.rmtree(folder)
                except OSError as exc:
                    log_warning(
                        "Failed to prune undo attachment folder %s: %s",
                        folder,
                        exc,
                    )


@pytest.mark.parametrize("implementation", ["legacy_reference", "current"])
@pytest.mark.parametrize("keep", [0, 17, 100])
def test_retention_preserves_exact_recent_and_available_set(
    tmp_path, empty_profile, implementation, keep
):
    repo = Repo(FinanceDB(tmp_path / "retention.sqlite3"))
    journal = ChangeJournalService(repo)
    records = [(n * 7, ["undone", "available", "failed"][n % 3]) for n in range(1, 73)]
    try:
        repo.db.conn.executemany(
            "INSERT INTO change_journal(id,action_type,label,status) VALUES(?,'edit','Synthetic',?)",
            records,
        )
        repo.db.conn.commit()
        # Independent set oracle: newest min-ten entries plus all live undos.
        expected = {identity for identity, _ in records[-max(10, keep):]}
        expected.update(
            identity for identity, status in records if status == "available"
        )
        folders = {}
        for identity, _ in records:
            folder = journal.journal_storage / str(identity)
            folder.mkdir(parents=True)
            (folder / "synthetic.txt").write_text("retained undo evidence", encoding="utf-8")
            folders[identity] = folder
        prune = journal.prune if implementation == "current" else lambda count: _legacy_materializing_prune(journal, count)
        repo.db.conn.execute("BEGIN IMMEDIATE")
        prune(keep)
        assert repo.db.conn.in_transaction
        assert {row[0] for row in repo.q("SELECT id FROM change_journal")} == expected
        assert all(folder.exists() for folder in folders.values())
        repo.db.conn.rollback()
        assert len(repo.q("SELECT id FROM change_journal")) == len(records)
        prune(keep)
        assert not repo.db.conn.in_transaction
        assert {row[0] for row in repo.q("SELECT id FROM change_journal")} == expected
        for identity, folder in folders.items():
            assert folder.exists() == (identity in expected)
        prune(keep)
        assert {row[0] for row in repo.q("SELECT id FROM change_journal")} == expected
    finally:
        repo.db.conn.close()


@pytest.mark.parametrize("size", [1000, 10000, 100000])
def test_retention_reference_measurement(tmp_path, empty_profile, size):
    """No latency threshold: run serially and retain stdout for measured claims."""
    repo = Repo(FinanceDB(tmp_path / "measurement.sqlite3"))
    journal = ChangeJournalService(repo)
    try:
        repo.db.conn.executemany(
            "INSERT INTO change_journal(action_type,label,status) VALUES('edit',?,'available')",
            ((f"Synthetic {n}",) for n in range(size)),
        )
        repo.db.conn.commit()
        methods = {
            "before": lambda: _legacy_materializing_prune(journal),
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
        result = {
            "journal_rows": size,
            "retained_rows": size,
            "reference_kind": "frozen_pre_phase5_materializing_prune",
            "methods": {},
        }
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
