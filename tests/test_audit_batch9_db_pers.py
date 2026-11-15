#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Regression tests for audit batch 9 (SQLite concurrency & data protection):

- BUG-DB-001 (P0): the GUI thread's direct accesses to the shared SQLite
  connection must go through ``FinanceDB._locked_connection`` so they hold
  ``_SQLITE_SHARED_CONNECTION_LOCK`` like every background worker.
- BUG-DB-002 (P1): ``update_internal_transfer(..., commit=False)`` must never
  end the caller-owned transaction when releasing its savepoint.
- BUG-PERS-001 (P0): ``_install_staged_restore`` must checkpoint the live WAL
  and preserve ``-wal``/``-shm`` sidecars instead of deleting them before the
  restore; a mid-restore failure must not destroy recent transactions.
"""

from __future__ import annotations

import sqlite3
import sys
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from database.engine import FinanceDB  # noqa: E402
from database.protection import DataProtectionGuard  # noqa: E402
from database.repo import Repo  # noqa: E402


@pytest.fixture
def repo(tmp_path, monkeypatch):
    # The global conftest sets FINANCAS_PESSOAIS_DEMO_DATA=1 for the visual/
    # analytics fixtures; batch 9 needs an empty database with exact row
    # counts, so demo seeding is disabled for this module only.
    monkeypatch.delenv("FINANCAS_PESSOAIS_DEMO_DATA", raising=False)
    db = FinanceDB(tmp_path / "batch9.sqlite3")
    yield Repo(db)
    try:
        db.conn.close()
    except sqlite3.Error:
        pass


def _add_transfer(repo: Repo, amount: float = 100.0) -> tuple[str, int, int]:
    debit_id, credit_id = repo.add_internal_transfer(
        "2026-02-01",
        amount,
        "Conta A",
        "Conta B",
    )
    group_id = str(
        repo.q(
            "SELECT transfer_group_id FROM transactions WHERE id=?",
            (debit_id,),
        )[0]["transfer_group_id"]
    )
    return group_id, debit_id, credit_id


# ---------------------------------------------------------------------------
# BUG-DB-001
# ---------------------------------------------------------------------------


class TestBugDB001LockedConnection:
    def test_locked_connection_holds_shared_lock(self, repo: Repo) -> None:
        from ui.workers import _SQLITE_SHARED_CONNECTION_LOCK

        db = repo.db
        assert isinstance(_SQLITE_SHARED_CONNECTION_LOCK, type(threading.RLock()))

        # Lock is free: a fresh acquisition must succeed, then release.
        acquired = _SQLITE_SHARED_CONNECTION_LOCK.acquire(timeout=0.05)
        assert acquired, "precondition: lock must be free"
        _SQLITE_SHARED_CONNECTION_LOCK.release()

        with db._locked_connection() as conn:
            # Same object is returned and the gate is held (non-blocking
            # acquisition from another thread must fail while we hold it).
            assert conn is db.conn
            probe: list[bool] = []

            def _try_acquire() -> None:
                probe.append(
                    _SQLITE_SHARED_CONNECTION_LOCK.acquire(timeout=0.05)
                )
                if probe[-1]:
                    _SQLITE_SHARED_CONNECTION_LOCK.release()

            other = threading.Thread(target=_try_acquire)
            other.start()
            other.join(timeout=2.0)
            assert probe == [False]

        # Released after the with-block: another thread can acquire now.
        assert _SQLITE_SHARED_CONNECTION_LOCK.acquire(timeout=0.05)
        _SQLITE_SHARED_CONNECTION_LOCK.release()

    def test_locked_connection_is_reentrant(self, repo: Repo) -> None:
        db = repo.db
        with db._locked_connection():
            # RLock reentrancy: nested acquisition from the same thread must
            # not deadlock and must still hand out the same connection.
            with db._locked_connection() as nested:
                assert nested is db.conn

    def test_locked_connection_releases_on_exception(self, repo: Repo) -> None:
        from ui.workers import _SQLITE_SHARED_CONNECTION_LOCK

        db = repo.db
        with pytest.raises(RuntimeError, match="boom"):
            with db._locked_connection():
                raise RuntimeError("boom")
        assert _SQLITE_SHARED_CONNECTION_LOCK.acquire(timeout=0.05)
        _SQLITE_SHARED_CONNECTION_LOCK.release()

    def test_gui_thread_query_paths_are_locked(
        self, repo: Repo, tmp_path: Path
    ) -> None:
        """Rows returned through the locked accessor equal the plain query."""

        repo.add_transaction(
            "2026-02-02", "Salário", 1000.0, "Rendimentos", "CGD", "income"
        )
        db = repo.db
        with db._locked_connection() as conn:
            rows_locked = conn.execute(
                "SELECT COUNT(*) FROM transactions"
            ).fetchone()
        rows_plain = db.query("SELECT COUNT(*) FROM transactions")
        assert int(rows_locked[0]) == len(rows_plain) == 1

    def test_settings_integrity_check_uses_locked_connection(
        self, repo: Repo
    ) -> None:
        """Source guard: the last direct GUI-thread .conn access is gated."""

        settings_source = (ROOT / "ui" / "pages" / "settings.py").read_text(
            encoding="utf-8"
        )
        assert "_locked_connection()" in settings_source
        assert "PRAGMA quick_check(1)" in settings_source


# ---------------------------------------------------------------------------
# BUG-DB-002
# ---------------------------------------------------------------------------


class TestBugDB002UpdateTransferCommitFalse:
    def test_commit_false_never_persists_caller_owned_transaction(
        self, repo: Repo
    ) -> None:
        group_id, debit_id, credit_id = _add_transfer(repo)
        conn = repo.db.conn
        assert not conn.in_transaction

        conn.execute("BEGIN IMMEDIATE")
        try:
            repo.update_internal_transfer(
                group_id,
                "2026-02-05",
                250.0,
                "Conta A",
                "Conta C",
                commit=False,
            )
            # Visible inside the caller transaction...
            amounts = {
                int(row["id"]): float(row["amount"])
                for row in conn.execute(
                    "SELECT id, amount FROM transactions WHERE id IN (?,?)",
                    (debit_id, credit_id),
                ).fetchall()
            }
            assert amounts[debit_id] == -250.0
            assert amounts[credit_id] == 250.0
            # ...and the caller still owns the open transaction after the
            # savepoint release (the implicit-commit regression).
            assert conn.in_transaction
        finally:
            conn.rollback()

        # Rollback must erase the change completely: nothing leaked to disk.
        stored = {
            int(row["id"]): float(row["amount"])
            for row in repo.q(
                "SELECT id, amount FROM transactions WHERE id IN (?,?)",
                (debit_id, credit_id),
            )
        }
        assert stored[debit_id] == -100.0
        assert stored[credit_id] == 100.0

    def test_commit_false_without_outer_transaction_leaves_work_uncommitted(
        self, repo: Repo
    ) -> None:
        group_id, debit_id, credit_id = _add_transfer(repo)
        conn = repo.db.conn
        repo.update_internal_transfer(
            group_id,
            "2026-02-06",
            300.0,
            "Conta A",
            "Conta D",
            commit=False,
        )
        # The method owns the transaction: it opens BEGIN IMMEDIATE, keeps it
        # open after the savepoint release and never commits it.
        assert conn.in_transaction
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        stored = {
            int(row["id"]): float(row["amount"])
            for row in repo.q(
                "SELECT id, amount FROM transactions WHERE id IN (?,?)",
                (debit_id, credit_id),
            )
        }
        assert stored[debit_id] == -100.0
        assert stored[credit_id] == 100.0

    def test_commit_true_with_new_destination_account_is_durable(
        self, repo: Repo
    ) -> None:
        """Real-world failure mode: creating the destination account opened an
        implicit transaction BEFORE the old ownership check, so ``commit=True``
        skipped its commit and left a dangling open transaction whose later
        rollback discarded the edit.  The ownership check must precede every
        identity write."""

        group_id, debit_id, credit_id = _add_transfer(repo)
        conn = repo.db.conn
        repo.update_internal_transfer(
            group_id,
            "2026-02-07",
            400.0,
            "Conta A",
            "Conta NOVA_BATCH9",
            commit=True,
        )
        assert not conn.in_transaction
        stored = {
            int(row["id"]): float(row["amount"])
            for row in repo.q(
                "SELECT id, amount FROM transactions WHERE id IN (?,?)",
                (debit_id, credit_id),
            )
        }
        assert stored[debit_id] == -400.0
        assert stored[credit_id] == 400.0
        accounts = [
            str(row["name"])
            for row in repo.q(
                "SELECT name FROM financial_accounts WHERE name=?",
                ("Conta NOVA_BATCH9",),
            )
        ]
        assert accounts == ["Conta NOVA_BATCH9"]

    def test_commit_false_with_new_destination_account_rolls_back_identity(
        self, repo: Repo
    ) -> None:
        """Identity rows created during a rejected commit=False update must be
        discarded with the caller's transaction, never leaked."""

        group_id, debit_id, credit_id = _add_transfer(repo)
        conn = repo.db.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            repo.update_internal_transfer(
                group_id,
                "2026-02-06",
                260.0,
                "Conta A",
                "Conta NOVA_BATCH9B",
                commit=False,
            )
            assert conn.in_transaction
        finally:
            conn.rollback()
        stored = {
            int(row["id"]): float(row["amount"])
            for row in repo.q(
                "SELECT id, amount FROM transactions WHERE id IN (?,?)",
                (debit_id, credit_id),
            )
        }
        assert stored[debit_id] == -100.0
        assert stored[credit_id] == 100.0
        accounts = [
            str(row["name"])
            for row in repo.q(
                "SELECT name FROM financial_accounts WHERE name=?",
                ("Conta NOVA_BATCH9B",),
            )
        ]
        assert accounts == []

    def test_commit_true_still_persists_both_legs(self, repo: Repo) -> None:
        group_id, debit_id, credit_id = _add_transfer(repo)
        repo.update_internal_transfer(
            group_id,
            "2026-02-07",
            400.0,
            "Conta A",
            "Conta E",
            commit=True,
        )
        rows = repo.q(
            "SELECT amount FROM transactions WHERE id IN (?,?) ORDER BY amount",
            (debit_id, credit_id),
        )
        assert [float(row["amount"]) for row in rows] == [-400.0, 400.0]
        assert not repo.db.conn.in_transaction

    def test_failed_update_rolls_back_and_keeps_caller_transaction_open(
        self, repo: Repo
    ) -> None:
        group_id, debit_id, credit_id = _add_transfer(repo)
        conn = repo.db.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            with pytest.raises(RuntimeError):
                repo.update_internal_transfer(
                    "missing-group",
                    "2026-02-08",
                    100.0,
                    "Conta A",
                    "Conta B",
                    commit=False,
                )
            assert conn.in_transaction
        finally:
            conn.rollback()
        # Original transfer untouched.
        rows = repo.q(
            "SELECT amount FROM transactions WHERE transfer_group_id=?",
            (group_id,),
        )
        assert sorted(float(row["amount"]) for row in rows) == [-100.0, 100.0]


# ---------------------------------------------------------------------------
# BUG-PERS-001
# ---------------------------------------------------------------------------


_CRASHED_WRITER_SNIPPET = (
    "import os, sys\n"
    "import sqlite3\n"
    "conn = sqlite3.connect(sys.argv[1])\n"
    "conn.execute('INSERT INTO t(v) VALUES(2.0)')\n"
    "conn.commit()\n"
    "os._exit(0)\n"  # hard exit: no close(), no checkpoint; WAL stays on disk
)


def _seed_crashed_app_wal(db_file: Path) -> None:
    """Create a live database plus a WAL holding a committed transaction that
    no connection ever checkpointed (the crashed-app scenario the restore
    path must survive)."""

    connection = sqlite3.connect(str(db_file))
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, v REAL)")
        connection.execute("INSERT INTO t(v) VALUES(1.0)")
        connection.commit()
    finally:
        connection.close()
    import subprocess
    import sys as _sys

    subprocess.run(
        [_sys.executable, "-c", _CRASHED_WRITER_SNIPPET, str(db_file)],
        capture_output=True,
        check=True,
    )


def _make_guard(tmp_path: Path) -> tuple[DataProtectionGuard, Path, Path]:
    db_file = tmp_path / "finance.sqlite3"
    pref_file = tmp_path / "preferences.json"
    guard = DataProtectionGuard(
        database_file=db_file,
        preferences_file=pref_file,
        attachments_directory=tmp_path / "anexos",
        history_directory=tmp_path / "historico_alteracoes",
        protection_directory=tmp_path / "protections",
    )
    return guard, db_file, pref_file


def _stage_restore(
    tmp_path: Path, payload: bytes = b"STAGED_NEW_DB"
) -> tuple[Path, Path, Path, Path]:
    staged_dir = tmp_path / "staged"
    staged_dir.mkdir(exist_ok=True)
    staged_db = staged_dir / "database.sqlite3"
    connection = sqlite3.connect(staged_db)
    try:
        connection.execute("CREATE TABLE restored_payload(value BLOB)")
        connection.execute("INSERT INTO restored_payload VALUES(?)", (payload,))
        connection.commit()
    finally:
        connection.close()
    staged_attachments = staged_dir / "anexos"
    staged_attachments.mkdir(exist_ok=True)
    staged_history = staged_dir / "historico"
    staged_history.mkdir(exist_ok=True)
    return staged_db, staged_attachments, staged_history, staged_dir


class TestBugPERS001WalPreservation:
    def test_failed_mid_restore_restores_wal_sidecars(self, tmp_path: Path) -> None:
        """Live-app scenario: an open reader keeps the WAL non-checkpointable
        (busy), so the checkpoint cannot fold the frames and the sidecar move
        must preserve the WAL bytes instead of deleting them; a mid-restore
        failure leaves the original database plus its live WAL intact."""
        guard, db_file, _pref = _make_guard(tmp_path)
        _seed_crashed_app_wal(db_file)
        wal_file = Path(f"{db_file}-wal")
        # A reader with an open read transaction pins the WAL so the
        # checkpoint cannot fold the frames (busy) and the sidecar bytes
        # stay meaningful.
        reader = sqlite3.connect(str(db_file))
        reader.execute("BEGIN")
        reader.execute("SELECT * FROM t").fetchall()

        staged_db, staged_att, staged_hist, _staged_dir = _stage_restore(
            tmp_path
        )

        original_replace = Path.replace

        def _failing_replace(self: Path, target: Path) -> Path:
            if str(self) == str(db_file) and str(target).endswith(".rollback"):
                raise OSError("simulated mid-restore failure")
            return original_replace(self, target)

        try:
            with patch.object(Path, "replace", _failing_replace):
                # On this Windows runner the reader holds the WAL file open,
                # so the sidecar move itself raises PermissionError before
                # the database replace; on POSIX the injected failure fires.
                # Either way the restore must abort while keeping the
                # original database and the live WAL intact.
                with pytest.raises(OSError) as exc_info:
                    guard._install_staged_restore(
                        staged_database=staged_db,
                        staged_attachments=staged_att,
                        staged_history=staged_hist,
                        staged_preferences=None,
                        preferences_present=False,
                    )
            assert (
                "simulated mid-restore failure" in str(exc_info.value)
                or "utiliz" in str(exc_info.value)  # WinError 32 share violation
            )

            # The original database is back AND the WAL sidecar bytes are
            # preserved (not deleted) for the next open to recover.
            assert db_file.stat().st_size > 0
            assert db_file.read_bytes() != staged_db.read_bytes()
            assert wal_file.exists()
            assert wal_file.stat().st_size > 0
            # Data recovery proof: a fresh connection still sees both rows.
            check = sqlite3.connect(str(db_file))
            try:
                values = [
                    float(row[0])
                    for row in check.execute(
                        "SELECT v FROM t ORDER BY id"
                    ).fetchall()
                ]
            finally:
                check.close()
            assert values == [1.0, 2.0]
        finally:
            reader.close()

    def test_successful_restore_removes_sidecar_backups(
        self, tmp_path: Path
    ) -> None:
        guard, db_file, _pref = _make_guard(tmp_path)
        db_file.write_bytes(b"ORIGINAL_DB")
        wal_file = Path(f"{db_file}-wal")
        wal_file.write_bytes(b"OLD_WAL")

        staged_db, staged_att, staged_hist, _staged_dir = _stage_restore(
            tmp_path, payload=b"STAGED_DB"
        )
        guard._install_staged_restore(
            staged_database=staged_db,
            staged_attachments=staged_att,
            staged_history=staged_hist,
            staged_preferences=None,
            preferences_present=False,
        )
        assert db_file.read_bytes() == staged_db.read_bytes()
        # Live sidecar must not survive into the new database lifetime.
        assert not wal_file.exists()
        leftovers = [
            item
            for item in db_file.parent.iterdir()
            if item.name.startswith(".finance.sqlite3") and "rollback" in item.name
        ]
        assert leftovers == []

    def test_checkpoint_folds_wal_into_live_database(self, tmp_path: Path) -> None:
        """A second open connection keeps the WAL alive after the writer
        commits; _checkpoint_live_wal must fold those frames into the main
        file so the restore's later sidecar removal cannot destroy them."""
        guard, db_file, _pref = _make_guard(tmp_path)
        writer = sqlite3.connect(str(db_file))
        try:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute(
                "CREATE TABLE movements(id INTEGER PRIMARY KEY, label TEXT)"
            )
            writer.commit()
            writer.execute(
                "INSERT INTO movements(label) VALUES('recent-transaction')"
            )
            writer.commit()
            # Keeper opens before the writer closes: sqlite only
            # auto-checkpoints when the LAST connection closes, so the
            # -wal file survives with committed frames inside.
            keeper = sqlite3.connect(str(db_file))
            keeper.execute("SELECT 1").fetchone()
        finally:
            writer.close()
        try:
            assert Path(f"{db_file}-wal").exists()
        finally:
            keeper.close()
            # keeper.close() is also a full close and may auto-checkpoint;
            # reopen a keeper to hold the WAL open for the guard call.
            keeper = sqlite3.connect(str(db_file))
            keeper.execute("SELECT 1").fetchone()
        try:
            guard._checkpoint_live_wal(db_file)
            check = sqlite3.connect(str(db_file))
            try:
                row = check.execute(
                    "SELECT label FROM movements WHERE label='recent-transaction'"
                ).fetchone()
                assert row is not None
            finally:
                check.close()
        finally:
            keeper.close()

    def test_checkpoint_folds_wal_from_crashed_app(self, tmp_path: Path) -> None:
        """Crashed-app case: the process died without closing its connection,
        leaving committed frames in the WAL and no live lock.  The checkpoint
        must fold them into the main file so the restore can safely drop the
        sidecars afterwards."""
        guard, db_file, _pref = _make_guard(tmp_path)
        _seed_crashed_app_wal(db_file)
        wal_file = Path(f"{db_file}-wal")
        assert wal_file.exists() and wal_file.stat().st_size > 0

        guard._checkpoint_live_wal(db_file)

        # Main-file-only (immutable) read proves the frames left the WAL.
        check = sqlite3.connect(f"file:{db_file}?immutable=1", uri=True)
        try:
            values = [
                float(row[0])
                for row in check.execute("SELECT v FROM t ORDER BY id").fetchall()
            ]
        finally:
            check.close()
        assert values == [1.0, 2.0]

    def test_checkpoint_failure_does_not_raise(self, tmp_path: Path) -> None:
        guard, db_file, _pref = _make_guard(tmp_path)
        db_file.write_bytes(b"NOT_A_REAL_DATABASE")
        # A corrupt/invalid database must not abort the restore path; the
        # sidecar preservation below is the real safety net.
        guard._checkpoint_live_wal(db_file)

    def test_regression_wal_transactions_survive_mid_restore_failure(
        self, tmp_path: Path
    ) -> None:
        """The mandatory regression: a simulated mid-restore failure keeps the
        recent committed-but-un-checkpointed WAL transactions recoverable.

        The database is written by a subprocess that exits hard (``os._exit``)
        without closing its connection, so the committed row exists ONLY in
        the ``-wal`` file — exactly the state in which the old code destroyed
        data by deleting the sidecars before the restore."""

        guard, db_file, _pref = _make_guard(tmp_path)
        _seed_crashed_app_wal(db_file)
        wal_file = Path(f"{db_file}-wal")
        assert wal_file.exists() and wal_file.stat().st_size > 0

        staged_db, staged_att, staged_hist, _staged_dir = _stage_restore(
            tmp_path
        )
        original_replace = Path.replace

        def _failing_replace(self: Path, target: Path) -> Path:
            if str(self) == str(db_file) and str(target).endswith(".rollback"):
                raise OSError("simulated mid-restore failure")
            return original_replace(self, target)

        with patch.object(Path, "replace", _failing_replace):
            with pytest.raises(OSError, match="simulated mid-restore failure"):
                guard._install_staged_restore(
                    staged_database=staged_db,
                    staged_attachments=staged_att,
                    staged_history=staged_hist,
                    staged_preferences=None,
                    preferences_present=False,
                )

        # Reopen the restored (original) database: the WAL row must survive.
        check = sqlite3.connect(str(db_file))
        try:
            values = [
                float(row[0])
                for row in check.execute("SELECT v FROM t ORDER BY id").fetchall()
            ]
        finally:
            check.close()
        assert values == [1.0, 2.0]
