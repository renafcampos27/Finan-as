#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Regression tests for audit batch 12:

- BUG-SEC-002 (P1): ``QPrinter`` must never be constructed or painted from a
  secondary QThread (Windows GUI-thread affinity). ``render_html_to_pdf`` must
  marshal the render onto the GUI thread when invoked from a worker thread,
  and ``AsyncPdfExporter`` must not hold the shared SQLite lock while the
  render is marshalled (lock-inversion deadlock guard).
- BUG-SEC-003 (P1): ``BackupService._mirror_to_secondary`` must use a unique
  temporary file per operation (stamp+PID) so concurrent mirrors cannot race
  on a shared ``.<name>.copying`` name, and cleanup must only remove the
  operation's own temporary.
- BUG-PAY-01 (P1): scheduled-payment occurrence creation must be atomic with
  respect to UNIQUE(scheduled_id, due_date): a losing concurrent writer must
  create no movement and leave no orphan placeholder occurrence row behind.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from PySide6.QtWidgets import QApplication

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from database.engine import FinanceDB  # noqa: E402
from database.repo import Repo  # noqa: E402
from services.backup import BackupService  # noqa: E402
from services.change_journal import ChangeJournalService  # noqa: E402
from services.export_documents import (  # noqa: E402
    AsyncPdfExporter,
    export_movements_to_pdf_file,
    render_html_to_pdf,
)
from services.payments import PaymentsService  # noqa: E402


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


# ---------------------------------------------------------------------------
# BUG-SEC-002: QPrinter thread affinity
# ---------------------------------------------------------------------------


def test_sec002_render_marshals_qprinter_to_gui_thread(qapp) -> None:
    """render_html_to_pdf called from a worker thread must construct QPrinter on the GUI thread."""

    from PySide6.QtCore import QThread

    target = "PySide6.QtPrintSupport.QPrinter"
    constructing_threads: list[object] = []

    def _spy_printer_factory(*args: object, **kwargs: object) -> MagicMock:
        # Record the thread on which QPrinter would be CONSTRUCTED without
        # instantiating the real Qt object (a real QPrinter in offscreen
        # mode crashes with COM error 0x80040155 on Windows CI - the very
        # spooler fragility this bug is about).
        constructing_threads.append(QThread.currentThread())
        return MagicMock()

    html_sample = "<html><body><h1>SEC-002</h1></body></html>"
    out_pdf = Path(tempfile.gettempdir()) / "sec002_marshal.pdf"

    with patch(target, side_effect=_spy_printer_factory) as mock_printer:
        with patch("PySide6.QtGui.QTextDocument.print_") as mock_print:
            errors: list[BaseException] = []
            result: list[str] = []
            done = threading.Event()

            def worker() -> None:
                try:
                    result.append(render_html_to_pdf(html_sample, str(out_pdf)))
                except BaseException as exc:  # propagated to main thread
                    errors.append(exc)
                finally:
                    done.set()

            thread = threading.Thread(target=worker, name="sec002-worker")
            thread.start()
            deadline = time.monotonic() + 30
            while not done.is_set() and time.monotonic() < deadline:
                QApplication.processEvents()
                time.sleep(0.01)
            thread.join(timeout=10)

            assert done.is_set(), "worker did not finish"
            assert not errors, f"worker raised: {errors!r}"
            assert len(result) == 1
            assert Path(result[0]) == out_pdf.resolve()
            mock_print.assert_called_once()
            mock_printer.assert_called_once()

    # Exactly one QPrinter was constructed, and it was constructed on the
    # GUI thread (QApplication.thread()), never on the worker thread.
    assert len(constructing_threads) == 1
    assert constructing_threads[0] is QApplication.instance().thread()

    out_pdf.unlink(missing_ok=True)


def test_sec002_render_inline_when_no_gui_thread_involved(qapp) -> None:
    """On the GUI thread the render stays inline (no marshalling overhead)."""

    html_sample = "<html><body><h1>inline</h1></body></html>"
    out_pdf = Path(tempfile.gettempdir()) / "sec002_inline.pdf"
    with patch("PySide6.QtPrintSupport.QPrinter"):
        with patch("PySide6.QtGui.QTextDocument.print_") as mock_print:
            rendered = render_html_to_pdf(html_sample, str(out_pdf))
            assert Path(rendered) == out_pdf.resolve()
            mock_print.assert_called_once()
    out_pdf.unlink(missing_ok=True)


def test_sec002_async_exporter_does_not_hold_sqlite_lock() -> None:
    """AsyncPdfExporter must opt out of the shared SQLite lock (rows already materialised)."""

    exporter = AsyncPdfExporter([], "unused.pdf")
    assert getattr(exporter, "requires_sqlite_lock", True) is False


def test_sec002_movements_export_off_thread_uses_gui_thread(qapp) -> None:
    """export_movements_to_pdf_file from a worker thread must marshal to the GUI thread."""

    from PySide6.QtCore import QThread

    constructing_threads: list[object] = []

    def _spy_printer_factory(*args: object, **kwargs: object) -> MagicMock:
        constructing_threads.append(QThread.currentThread())
        return MagicMock()

    rows = [
        {
            "date": "2026-03-01",
            "description": "Supermercado",
            "category": "Alimentacao",
            "account": "Ordem",
            "amount": -45.50,
        },
    ]
    out_mov = Path(tempfile.gettempdir()) / "sec002_mov.pdf"

    with patch(
        "PySide6.QtPrintSupport.QPrinter", side_effect=_spy_printer_factory
    ):
        with patch("PySide6.QtGui.QTextDocument.print_"):
            errors: list[BaseException] = []
            done = threading.Event()

            def worker() -> None:
                try:
                    export_movements_to_pdf_file(rows, str(out_mov))
                except BaseException as exc:
                    errors.append(exc)
                finally:
                    done.set()

            thread = threading.Thread(target=worker, name="sec002-worker-mov")
            thread.start()
            deadline = time.monotonic() + 30
            while not done.is_set() and time.monotonic() < deadline:
                QApplication.processEvents()
                time.sleep(0.01)
            thread.join(timeout=10)

            assert done.is_set(), "worker did not finish"
            assert not errors, f"worker raised: {errors!r}"

    assert len(constructing_threads) == 1
    assert constructing_threads[0] is QApplication.instance().thread()
    out_mov.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# BUG-SEC-003: unique temporary file per mirror operation
# ---------------------------------------------------------------------------


class _Database:
    def __init__(self, path: Path):
        self.path = path
        self.conn = sqlite3.connect(path)

    def backup_to(self, target: Path) -> None:
        self.conn.commit()
        destination = sqlite3.connect(target)
        try:
            self.conn.backup(destination)
            destination.commit()
        finally:
            destination.close()


class _Repo:
    def __init__(self, path: Path):
        self.db = _Database(path)

    def setting(self, key: str, default: str = "") -> str:
        row = self.db.conn.execute(
            "SELECT value FROM settings WHERE key=?", (key,)
        ).fetchone()
        return str(row[0]) if row else default

    def set_setting(self, key: str, value: str) -> None:
        self.db.conn.execute(
            "INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (key, str(value))
        )
        self.db.conn.commit()


def _backup_service(tmp_path: Path) -> BackupService:
    database = tmp_path / "dados" / "financas.sqlite3"
    database.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
    connection.execute("INSERT INTO settings(key,value) VALUES('marker','sec003')")
    connection.commit()
    connection.close()
    repo = _Repo(database)
    attachments = tmp_path / "dados" / "anexos"
    history = tmp_path / "dados" / "backups" / "historico_alteracoes"
    attachments.mkdir(parents=True, exist_ok=True)
    history.mkdir(parents=True, exist_ok=True)
    preferences = tmp_path / "dados" / "configuracao" / "backup_preferences.json"
    preferences.parent.mkdir(parents=True, exist_ok=True)
    preferences.write_text(
        json.dumps({"backup_keep": 10, "backup_encryption_enabled": "0"}),
        encoding="utf-8",
    )
    return BackupService(
        repo,
        database_file=database,
        backup_directory=tmp_path / "dados" / "backups",
        attachments_directory=attachments,
        history_directory=history,
        preferences_file=preferences,
    )


def test_sec003_temporary_name_is_unique_per_operation(tmp_path: Path) -> None:
    """_mirror_to_secondary must use stamp+PID-unique temporary names."""

    service = _backup_service(tmp_path)
    source = tmp_path / "fonte.zip"
    source.write_bytes(b"conteudo-do-backup")

    seen_temporaries: list[str] = []
    real_copy2 = shutil.copy2

    def _spy_copy2(src: object, dst: object, **kwargs: object) -> None:
        seen_temporaries.append(Path(str(dst)).name)
        real_copy2(src, dst, **kwargs)

    secondary = tmp_path / "secundaria"
    with patch.object(
        service, "preferences", return_value={"secondary_backup_enabled": True,
                                              "secondary_backup_directory": str(secondary)}
    ):
        with patch("shutil.copy2", _spy_copy2):
            result = service._mirror_to_secondary(source)

    assert result is not None
    assert len(seen_temporaries) == 1
    name = seen_temporaries[0]
    # Format: ".<name>.copying.<monotonic_ns>_<pid>" (BUG-SEC-003 fix).
    assert name.startswith(".fonte.zip.copying.")
    stamp, pid = name.rsplit("_", 1)
    assert int(pid) == os.getpid()
    # The stamp must be a monotonic timestamp (digits only).
    stamp_value = stamp.rsplit(".", 1)[-1]
    assert stamp_value.isdigit()
    assert stamp.endswith("." + stamp_value)
    # The temporary must be gone after the operation (own-cleanup).
    assert not (secondary / name).exists()
    assert (secondary / "fonte.zip").read_bytes() == b"conteudo-do-backup"


def test_sec003_concurrent_mirrors_do_not_collide(tmp_path: Path) -> None:
    """Two concurrent mirror operations must both succeed (no shared temp name)."""

    service = _backup_service(tmp_path)
    source = tmp_path / "fonte.zip"
    source.write_bytes(b"conteudo-do-backup")

    used_names: set[str] = set()
    names_lock = threading.Lock()
    real_copy2 = shutil.copy2

    def _spy_copy2(src: object, dst: object, **kwargs: object) -> None:
        with names_lock:
            assert Path(str(dst)).name not in used_names, (
                f"temporary name collision: {dst}"
            )
            used_names.add(Path(str(dst)).name)
        real_copy2(src, dst, **kwargs)

    secondary = tmp_path / "secundaria"
    with patch.object(
        service, "preferences", return_value={"secondary_backup_enabled": True,
                                              "secondary_backup_directory": str(secondary)}
    ):
        with patch("shutil.copy2", _spy_copy2):
            errors: list[BaseException] = []
            barrier = threading.Barrier(2)

            def mirror_worker() -> None:
                try:
                    barrier.wait(timeout=10)
                    service._mirror_to_secondary(source)
                except BaseException as exc:
                    errors.append(exc)

            threads = [
                threading.Thread(target=mirror_worker, name=f"mirror-{i}")
                for i in range(2)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)

            assert not errors, f"mirror raised: {errors!r}"
    assert len(used_names) == 2
    # Both operations used distinct unique names; both temporaries are gone.
    assert not list(secondary.glob(".*copying*"))


def test_sec003_cleanup_only_removes_own_temporary(tmp_path: Path) -> None:
    """A failure inside _mirror_to_secondary must not delete another operation's temp."""

    service = _backup_service(tmp_path)
    source = tmp_path / "fonte.zip"
    source.write_bytes(b"conteudo-do-backup")

    secondary = tmp_path / "secundaria"
    foreign_temp = secondary / ".fonte.zip.copying.999999999999999999_424242"
    secondary.mkdir(parents=True, exist_ok=True)
    foreign_temp.write_bytes(b"foreign")

    with patch.object(
        service, "preferences", return_value={"secondary_backup_enabled": True,
                                              "secondary_backup_directory": str(secondary)}
    ):
        with patch(
            "shutil.copy2",
            side_effect=OSError("disco cheio simulado"),
        ):
            result = service._mirror_to_secondary(source)

    assert result is None  # failure is swallowed and logged by design
    # Own temporary cleaned, foreign temporary untouched.
    assert not list(p for p in secondary.glob(".*copying*") if p != foreign_temp)
    assert foreign_temp.exists()


# ---------------------------------------------------------------------------
# BUG-PAY-01: atomic scheduled-payment occurrence creation
# ---------------------------------------------------------------------------


@pytest.fixture
def payments_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("FINANCAS_PESSOAIS_DEMO_DATA", raising=False)
    db = FinanceDB(tmp_path / "batch12_pay01.sqlite3")
    repo = Repo(db)
    repo.db.seed_if_empty()
    for table in ("transaction_splits", "transactions", "scheduled_payments"):
        repo.db.conn.execute(f"DELETE FROM {table}")
    repo.db.conn.commit()
    journal = ChangeJournalService(repo)
    payments = PaymentsService(repo, journal)
    try:
        yield repo, payments, db
    finally:
        try:
            db.conn.close()
        except Exception:
            pass


def _add_scheduled(repo: Repo, name: str = "Streaming", amount: float = 15.0) -> int:
    return repo.add_scheduled_payment(
        name,
        "Assinaturas",
        "",
        amount,
        "Conta principal",
        "",
        "2026-01-01",
        1,
        6,
    )


def test_pay01_charge_creation_is_atomic_idempotent(payments_env) -> None:
    """Second charge for the same (scheduled_id, due_date) must create nothing."""

    repo, _payments, _db = payments_env
    scheduled_id = _add_scheduled(repo)

    first = repo._create_scheduled_payment_charge_uncommitted(
        scheduled_id,
        "2026-02-01",
        "Streaming (1/6)",
        15.0,
        "Assinaturas",
        "Conta principal",
    )
    assert first is not None
    repo.db.conn.commit()

    second = repo._create_scheduled_payment_charge_uncommitted(
        scheduled_id,
        "2026-02-01",
        "Streaming (1/6)",
        15.0,
        "Assinaturas",
        "Conta principal",
    )
    assert second is None

    # Exactly one occurrence and one movement exist.
    occurrences = repo.db.conn.execute(
        "SELECT COUNT(*) FROM scheduled_payment_occurrences WHERE scheduled_id=?",
        (scheduled_id,),
    ).fetchone()[0]
    assert occurrences == 1
    movements = repo.db.conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE description LIKE 'Streaming%'"
    ).fetchone()[0]
    assert movements == 1
    # The occurrence row is fully materialised (no NULL tombstone left).
    tx_id = repo.db.conn.execute(
        "SELECT transaction_id FROM scheduled_payment_occurrences "
        "WHERE scheduled_id=? AND due_date='2026-02-01'",
        (scheduled_id,),
    ).fetchone()[0]
    assert tx_id == first


def test_pay01_invalid_amount_leaves_no_orphan_occurrence(payments_env) -> None:
    """A failed movement creation must not leave a NULL placeholder occurrence."""

    repo, _payments, _db = payments_env
    scheduled_id = _add_scheduled(repo)

    with pytest.raises(ValueError):
        repo._create_scheduled_payment_charge_uncommitted(
            scheduled_id,
            "2026-02-01",
            "Streaming (1/6)",
            0.0,  # invalid: require_nonzero_eur rejects effective zero
            "Assinaturas",
            "Conta principal",
        )

    # No occurrence row (placeholder or otherwise) may survive.
    occurrences = repo.db.conn.execute(
        "SELECT COUNT(*) FROM scheduled_payment_occurrences WHERE scheduled_id=?",
        (scheduled_id,),
    ).fetchone()[0]
    assert occurrences == 0


def test_pay01_generation_reports_exact_single_charge(payments_env) -> None:
    """generate_due_scheduled_payments stays exact after the atomicity change."""

    repo, payments, _db = payments_env
    scheduled_id = _add_scheduled(repo, "Assinatura Mensal", 30.0)

    created = payments.generate_due_scheduled_payments(date(2026, 3, 2))
    assert created == 3  # March 1 is Sunday; the third charge is effective March 2.

    # Running it again must be a no-op (idempotent).
    created_again = payments.generate_due_scheduled_payments(date(2026, 3, 2))
    assert created_again == 0

    occurrences = repo.db.conn.execute(
        "SELECT COUNT(*) FROM scheduled_payment_occurrences WHERE scheduled_id=?",
        (scheduled_id,),
    ).fetchone()[0]
    assert occurrences == 3


def test_pay01_no_null_transaction_ids_after_generation(payments_env) -> None:
    """No placeholder (NULL transaction_id) occurrence rows may survive generation."""

    repo, payments, _db = payments_env
    _add_scheduled(repo, "Assinatura Anual", 120.0)

    payments.generate_due_scheduled_payments(date(2026, 6, 1))

    null_rows = repo.db.conn.execute(
        "SELECT COUNT(*) FROM scheduled_payment_occurrences "
        "WHERE transaction_id IS NULL"
    ).fetchone()[0]
    assert null_rows == 0
