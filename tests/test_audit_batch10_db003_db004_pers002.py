#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Regression tests for audit batch 10 (DB/persistence):

- BUG-DB-003 (P1): the ``trg_transactions_cascade_transfer_leg`` trigger must
  compensate ``goals.saved_amount`` when a cascading delete removes a transfer
  leg linked to a goal (deposit leg -> subtract, withdrawal leg -> add back),
  following the same sign convention as ``adjust_goal_with_transaction`` and
  ``_delete_transactions`` (``saved_amount + amount`` with a MAX(0) floor).
- BUG-DB-004 (P1): ``import_transactions_batch`` must be atomic across all
  chunks: a failure in a later chunk must roll the whole batch back, leaving
  zero partial rows persisted.
- BUG-PERS-002 (P1): ``_ThreadOwnedBackupDatabase.backup_to`` must write the
  snapshot into a temporary file and only publish it with an atomic rename
  after fsync; a failure must remove the temporary and keep the previous
  backup intact.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from database.engine import FinanceDB  # noqa: E402
from database.repo import Repo  # noqa: E402
from utils.helpers import require_eur_cent_precision  # noqa: E402


@pytest.fixture
def repo(tmp_path, monkeypatch):
    # The global conftest sets FINANCAS_PESSOAIS_DEMO_DATA=1 for the visual/
    # analytics fixtures; batch 10 needs an empty database with exact row
    # counts, so demo seeding is disabled for this module only.
    monkeypatch.delenv("FINANCAS_PESSOAIS_DEMO_DATA", raising=False)
    db = FinanceDB(tmp_path / "batch10.sqlite3")
    yield Repo(db)
    try:
        db.conn.close()
    except sqlite3.Error:
        pass


def _goal_saved_amount(repo: Repo, goal_id: int) -> float:
    row = repo.q("SELECT saved_amount FROM goals WHERE id=?", (int(goal_id),))
    assert row, f"goal {goal_id} missing"
    return float(row[0]["saved_amount"])


def _link_transfer_leg_to_goal(
    repo: Repo, tx_id: int, goal_id: int, amount: float
) -> None:
    """Link a transfer leg to a goal and apply the corresponding saved_amount
    adjustment with the exact convention the codebase uses when creating a
    goal movement (adjust_goal_with_transaction: a deposit tx has a negative
    amount and RAISES saved_amount, a withdrawal tx has a positive amount and
    LOWERS saved_amount — i.e. ``saved_amount - tx_amount``).  Deleting the
    movement must invert it (``saved_amount + tx_amount``), which is what
    _delete_transactions and the fixed trigger do."""
    repo.db.conn.execute(
        "UPDATE transactions SET goal_id=? WHERE id=?",
        (int(goal_id), int(tx_id)),
    )
    repo.db.conn.execute(
        "UPDATE goals SET saved_amount = MAX(0.0, saved_amount - ?) WHERE id=?",
        (float(amount), int(goal_id)),
    )
    repo.db.conn.commit()


# ---------------------------------------------------------------------------
# BUG-DB-003
# ---------------------------------------------------------------------------


class TestBugDB003CascadeGoalCompensation:
    def test_trigger_exists_with_goal_compensation(
        self, repo: Repo, tmp_path: Path
    ) -> None:
        """Source guard: both trigger definitions (bootstrap + migration) must
        carry the goals compensation statement."""
        engine_source = (ROOT / "database" / "engine.py").read_text(
            encoding="utf-8"
        )
        assert engine_source.count("trg_transactions_cascade_transfer_leg") >= 4
        occurrences = engine_source.count(
            "UPDATE goals\n                SET saved_amount = MAX(0.0, saved_amount + COALESCE("
        )
        assert occurrences == 2, (
            "both trigger definitions must compensate goals.saved_amount"
        )

        installed = {
            str(row["name"])
            for row in repo.q("SELECT name FROM sqlite_master WHERE type='trigger'")
        }
        assert "trg_transactions_cascade_transfer_leg" in installed

    def test_cascade_delete_compensates_goal_deposit_leg(
        self, repo: Repo
    ) -> None:
        """Deposit leg (-) of a transfer linked to a goal: the cascading
        delete must subtract the deposit back out of saved_amount."""
        goal_id = repo.add_goal("Fundo Viagem", 1000.0, 0.0, 0.0, "2026-12-31")
        debit_id, credit_id = repo.add_internal_transfer(
            "2026-02-01", 200.0, "Conta A", "Conta B"
        )
        # debit leg amount is -200.0 (deposit into the goal pot): creating the
        # linked movement raised saved_amount from 0.0 to 200.0.
        _link_transfer_leg_to_goal(repo, debit_id, goal_id, -200.0)
        assert _goal_saved_amount(repo, goal_id) == 200.0

        repo.delete_transactions([debit_id])

        assert repo.transaction_by_id(debit_id) is None
        assert _goal_saved_amount(repo, goal_id) == 0.0

    def test_cascade_delete_compensates_goal_withdrawal_leg(
        self, repo: Repo
    ) -> None:
        """Withdrawal leg (+) linked to a goal: the cascading delete must add
        the amount back into saved_amount."""
        goal_id = repo.add_goal("Fundo Viagem", 1000.0, 150.0, 0.0, "2026-12-31")
        _debit, credit_id = repo.add_internal_transfer(
            "2026-02-02", 60.0, "Conta A", "Conta B"
        )
        # credit leg amount is +60.0 (withdrawal from the goal pot): creating
        # the linked movement lowered saved_amount from 150.0 to 90.0.
        _link_transfer_leg_to_goal(repo, credit_id, goal_id, 60.0)
        assert _goal_saved_amount(repo, goal_id) == 90.0

        repo.delete_transactions([credit_id])

        assert repo.transaction_by_id(credit_id) is None
        assert _goal_saved_amount(repo, goal_id) == 150.0

    def test_cascade_delete_without_goal_link_leaves_goals_untouched(
        self, repo: Repo
    ) -> None:
        goal_id = repo.add_goal("Sem ligacao", 500.0, 320.0, 0.0, "2026-12-31")
        debit_id, _credit = repo.add_internal_transfer(
            "2026-02-03", 45.0, "Conta A", "Conta B"
        )

        repo.delete_transactions([debit_id])

        assert _goal_saved_amount(repo, goal_id) == 320.0

    def test_cascade_delete_via_raw_sql_also_compensates_goal(
        self, repo: Repo
    ) -> None:
        """The trigger is the safety net for raw-SQL deletes (the path that
        bypasses delete_transactions' own compensation)."""
        goal_id = repo.add_goal("Pote direto", 800.0, 100.0, 0.0, "2026-12-31")
        debit_id, _credit = repo.add_internal_transfer(
            "2026-02-04", 30.0, "Conta A", "Conta B"
        )
        _link_transfer_leg_to_goal(repo, debit_id, goal_id, -30.0)
        assert _goal_saved_amount(repo, goal_id) == 130.0
        # Direct SQL delete of one leg (not via delete_transactions).
        repo.db.conn.execute("DELETE FROM transactions WHERE id=?", (debit_id,))
        repo.db.conn.commit()

        rows = repo.q("SELECT id FROM transactions WHERE goal_id=?", (int(goal_id),))
        assert rows == []
        # The goal-linked deposit (30) is undone: 130.0 back to the
        # pre-movement 100.0.
        assert _goal_saved_amount(repo, goal_id) == 100.0

    def test_multi_goal_transfer_compensates_first_linked_goal_only(
        self, repo: Repo
    ) -> None:
        """The compensating UPDATE targets exactly one goal row; a transfer
        with two differently-linked legs must not touch the unlinked goal."""
        goal_a = repo.add_goal("Meta A", 500.0, 100.0, 0.0, "2026-12-31")
        goal_b = repo.add_goal("Meta B", 500.0, 70.0, 0.0, "2026-12-31")
        debit_id, credit_id = repo.add_internal_transfer(
            "2026-02-05", 25.0, "Conta A", "Conta B"
        )
        _link_transfer_leg_to_goal(repo, debit_id, goal_a, -25.0)  # 100 -> 125

        repo.delete_transactions([debit_id])

        assert _goal_saved_amount(repo, goal_a) == 100.0
        assert _goal_saved_amount(repo, goal_b) == 70.0
        assert repo.transaction_by_id(credit_id) is None

# ---------------------------------------------------------------------------
# BUG-DB-004
# ---------------------------------------------------------------------------


class TestBugDB004ImportAtomicity:
    def _rows(self, count: int) -> list[dict[str, object]]:
        return [
            {
                "date": "2026-09-01",
                "description": f"Importado #{i:03d}",
                "amount": -10.0,
                "category": "Alimentacao",
                "account": "Conta Principal",
            }
            for i in range(count)
        ]

    def test_all_chunks_commit_together(
        self, repo: Repo, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The WAL checkpoint between chunks must run only after the whole
        batch is committed (single transaction across chunks)."""
        calls: list[str] = []
        monkeypatch.setattr(
            repo.db,
            "wal_checkpoint",
            lambda mode="PASSIVE": calls.append(str(mode)) or (0, 0, 0),
        )
        rows = self._rows(6)

        created = repo.import_transactions_batch(rows, chunk_size=2)

        assert len(created) == 6
        total = int(
            repo.scalar(
                "SELECT COUNT(*) FROM transactions WHERE description LIKE 'Importado #%'"
            )
        )
        assert total == 6
        assert calls == ["PASSIVE"]

    def test_failure_in_later_chunk_rolls_back_entire_batch(
        self, repo: Repo, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failure while persisting chunk 3 of 3 must leave zero partial
        rows: the whole file-level batch is rolled back."""
        real_require = require_eur_cent_precision
        calls = {"n": 0}

        def _fail_on_third_chunk(value: object, *, field_name: str = "valor") -> float:
            calls["n"] += 1
            # 2 chunks x 3 rows each: the 7th row belongs to chunk 3.
            if calls["n"] > 6:
                raise ValueError("valor do movimento importado invalido (simulado)")
            return real_require(value, field_name=field_name)

        monkeypatch.setattr(
            "database.repo.transactions.require_eur_cent_precision",
            _fail_on_third_chunk,
        )
        rows = self._rows(9)

        with pytest.raises(ValueError, match="simulado"):
            repo.import_transactions_batch(rows, chunk_size=3)

        total = int(
            repo.scalar(
                "SELECT COUNT(*) FROM transactions WHERE description LIKE 'Importado #%'"
            )
        )
        assert total == 0
        meta = int(
            repo.scalar(
                "SELECT COUNT(*) FROM transaction_meta tm "
                "JOIN transactions t ON t.id = tm.transaction_id "
                "WHERE t.description LIKE 'Importado #%'")
        )
        # No orphaned metadata rows either (they were written per row).
        assert meta == 0
        assert not repo.db.conn.in_transaction

    def test_failure_in_later_chunk_rolls_back_multi_chunk_import_with_prior_data(
        self, repo: Repo, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Pre-existing rows must survive untouched when a later chunk fails;
        the rollback must not nuke the whole table."""
        repo.add_transaction(
            "2026-08-01", "Pre-existente", 50.0, "Rendimentos", "CGD"
        )
        real_require = require_eur_cent_precision
        calls = {"n": 0}

        def _fail_on_second_chunk(value: object, *, field_name: str = "valor") -> float:
            calls["n"] += 1
            if calls["n"] > 3:  # chunk_size=3 -> first row of chunk 2
                raise ValueError("falha no chunk 2 (simulada)")
            return real_require(value, field_name=field_name)

        monkeypatch.setattr(
            "database.repo.transactions.require_eur_cent_precision",
            _fail_on_second_chunk,
        )
        rows = self._rows(6)

        with pytest.raises(ValueError, match="simulada"):
            repo.import_transactions_batch(rows, chunk_size=3)

        assert int(repo.scalar("SELECT COUNT(*) FROM transactions")) == 1
        remaining = repo.q(
            "SELECT description FROM transactions"
        )
        assert [str(row["description"]) for row in remaining] == ["Pre-existente"]

    def test_success_path_persists_everything_exactly_once(
        self, repo: Repo
    ) -> None:
        rows = self._rows(5)
        created = repo.import_transactions_batch(rows, chunk_size=2)
        assert len(created) == 5
        assert len(set(created)) == 5
        total = int(
            repo.scalar(
                "SELECT COUNT(*) FROM transactions WHERE description LIKE 'Importado #%'"
            )
        )
        assert total == 5
        assert not repo.db.conn.in_transaction


# ---------------------------------------------------------------------------
# BUG-PERS-002
# ---------------------------------------------------------------------------


class _BackupProxy:
    """Minimal proxy around ``sqlite3.Connection`` so tests can stub the
    C-level ``backup`` method (which does not allow attribute assignment)."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        object.__setattr__(self, "_connection", connection)

    @property
    def backup(self):
        override = self.__dict__.get("_backup_override")
        if override is not None:
            return override
        return self._connection.backup

    @backup.setter
    def backup(self, value) -> None:
        object.__setattr__(self, "_backup_override", value)

    def __getattr__(self, item):
        return getattr(self._connection, item)


def _thread_owned_database(db_file: Path):
    from services.backup import _ThreadOwnedBackupDatabase

    return _ThreadOwnedBackupDatabase(db_file)


class TestBugPERS002BackupToTempPublish:
    def _seed(self, db_file: Path) -> None:
        conn = sqlite3.connect(str(db_file))
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, v REAL)")
            conn.execute("INSERT INTO t(v) VALUES(1.0)")
            conn.commit()
        finally:
            conn.close()

    def test_backup_to_publishes_via_temporary_and_cleans_up(
        self, tmp_path: Path
    ) -> None:
        db_file = tmp_path / "live.sqlite3"
        self._seed(db_file)
        target = tmp_path / "backups" / "backup.sqlite3"
        wrapper = _thread_owned_database(db_file)
        try:
            wrapper.backup_to(target)
        finally:
            wrapper.close()

        assert target.is_file() and target.stat().st_size > 0
        # The published file is a valid SQLite database with the row.
        check = sqlite3.connect(str(target))
        try:
            values = [float(r[0]) for r in check.execute("SELECT v FROM t")]
        finally:
            check.close()
        assert values == [1.0]
        # No temporary leftovers in the target directory.
        leftovers = [p for p in target.parent.iterdir() if p.name.endswith(".tmp")]
        assert leftovers == []

    def test_failure_mid_copy_removes_temp_and_keeps_previous_backup(
        self, tmp_path: Path
    ) -> None:
        db_file = tmp_path / "live.sqlite3"
        self._seed(db_file)
        target = tmp_path / "backups" / "backup.sqlite3"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"PREVIOUS_GOOD_BACKUP")
        previous_size = target.stat().st_size

        wrapper = _thread_owned_database(db_file)
        try:
            real_replace = Path.replace

            def _failing_replace(self: Path, target_path: Path) -> Path:
                if str(self).endswith(".tmp") and str(target_path) == str(target):
                    raise OSError("simulated publish failure")
                return real_replace(self, target_path)

            with patch.object(Path, "replace", _failing_replace):
                with pytest.raises(OSError, match="simulated publish failure"):
                    wrapper.backup_to(target)
        finally:
            wrapper.close()

        # The previous backup is byte-identical and untouched.
        assert target.read_bytes() == b"PREVIOUS_GOOD_BACKUP"
        assert target.stat().st_size == previous_size
        # The temporary snapshot was cleaned up.
        leftovers = [p for p in target.parent.iterdir() if p.name.endswith(".tmp")]
        assert leftovers == []

    def test_failure_during_copy_keeps_target_intact(
        self, tmp_path: Path
    ) -> None:
        db_file = tmp_path / "live.sqlite3"
        self._seed(db_file)
        target = tmp_path / "backups" / "backup.sqlite3"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"PREVIOUS_GOOD_BACKUP")

        wrapper = _thread_owned_database(db_file)
        wrapper.conn = _BackupProxy(wrapper.conn)  # type: ignore[assignment]

        def _failing_backup(destination, **_kwargs):
            raise sqlite3.OperationalError("disk I/O error (simulado)")

        wrapper.conn.backup = _failing_backup  # type: ignore[method-assign]
        try:
            with pytest.raises(sqlite3.OperationalError, match="simulado"):
                wrapper.backup_to(target)
        finally:
            wrapper.close()
        assert target.read_bytes() == b"PREVIOUS_GOOD_BACKUP"
        leftovers = [p for p in target.parent.iterdir() if p.name.endswith(".tmp")]
        assert leftovers == []

    def test_busy_source_retries_then_succeeds(
        self, tmp_path: Path
    ) -> None:
        db_file = tmp_path / "live.sqlite3"
        self._seed(db_file)
        target = tmp_path / "backups" / "backup.sqlite3"
        wrapper = _thread_owned_database(db_file)
        wrapper.conn = _BackupProxy(wrapper.conn)  # type: ignore[assignment]
        attempts = {"n": 0}
        real_backup = wrapper.conn.backup

        def _busy_then_ok(destination, **_kwargs):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise sqlite3.OperationalError("database is locked")
            return real_backup(destination, pages=250, sleep=0.01)

        wrapper.conn.backup = _busy_then_ok  # type: ignore[method-assign]
        try:
            wrapper.backup_to(target)
        finally:
            wrapper.close()

        assert attempts["n"] == 2
        assert target.is_file() and target.stat().st_size > 0
        leftovers = [p for p in target.parent.iterdir() if p.name.endswith(".tmp")]
        assert leftovers == []
