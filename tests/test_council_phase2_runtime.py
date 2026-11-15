"""Plan 2026-10-25 / A02,A04: functional package and integrity shutdown gates."""

from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path
from types import MethodType, SimpleNamespace

import pytest

from database.engine import FinanceDB
from database.protection import DataIntegrityError


def test_self_test_environment_owns_and_restores_generic_temporary_paths(tmp_path):
    from utils.package_self_test import _isolated_environment
    from utils.private_temp import system_private_parent

    previous = tempfile.gettempdir()
    root = tmp_path / "self-test"
    with _isolated_environment(root):
        assert Path(tempfile.gettempdir()).is_relative_to(root)
        assert system_private_parent().is_relative_to(root)
        assert Path.home().is_relative_to(root)
    assert tempfile.gettempdir() == previous


def _invalid_check_database(path: Path) -> FinanceDB:
    db = FinanceDB(path)
    db.conn.execute("CREATE TABLE synthetic_integrity(value INTEGER CHECK(value>0))")
    db.conn.execute("PRAGMA ignore_check_constraints=ON")
    db.conn.execute("INSERT INTO synthetic_integrity VALUES(-1)")
    db.conn.commit()
    db.conn.execute("PRAGMA ignore_check_constraints=OFF")
    assert db.conn.execute("PRAGMA quick_check(1)").fetchone()[0] != "ok"
    return db


def test_real_integrity_failure_has_a_typed_shutdown_error(tmp_path):
    db = _invalid_check_database(tmp_path / "invalid.sqlite3")
    try:
        with pytest.raises(DataIntegrityError):
            db.prepare_for_shutdown()
        assert not getattr(db, "_closed", False)
    finally:
        db.conn.close()


def test_integrity_shutdown_retains_maintenance_and_sql_write_block(tmp_path):
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    from main import LiquidFinanceApp
    from ui.workers import BackgroundTaskRegistry

    qapp = QApplication.instance() or QApplication([])
    db = _invalid_check_database(tmp_path / "maintenance.sqlite3")
    status = SimpleNamespace(showMessage=lambda *_args: None)
    resumed = []
    dashboard = SimpleNamespace(resume_data_access=lambda: resumed.append(True))
    pages = SimpleNamespace(existing=lambda _key: dashboard)
    fake = SimpleNamespace(
        repo=SimpleNamespace(close=db.prepare_for_shutdown, db=db),
        _database_closed=False,
        _shutdown_integrity_error=False,
        _database_maintenance_active=False,
        _shutdown_poll_timer=QTimer(),
        background_task_registry=BackgroundTaskRegistry(),
        pages=pages,
        setEnabled=lambda _value: None,
        statusBar=lambda: status,
        auto_backup_timer=QTimer(),
        _suppress_error_modals_in_testing=True,
    )
    fake._enter_integrity_maintenance = MethodType(
        LiquidFinanceApp._enter_integrity_maintenance, fake
    )
    try:
        ok, error = LiquidFinanceApp._finalize_database_shutdown(fake)
        assert not ok
        LiquidFinanceApp._abort_shutdown(fake, error)
        assert fake._database_maintenance_active
        assert not fake.background_task_registry.can_start()
        assert not fake.background_task_registry.can_start(allow_during_shutdown=True)
        assert not fake.auto_backup_timer.isActive()
        assert not resumed
        assert db.conn.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            db.conn.execute("INSERT INTO settings(key,value) VALUES('unsafe','1')")
        # A generic maintenance-complete callback cannot reopen writes.
        LiquidFinanceApp._finish_database_maintenance(fake)
        assert not fake.background_task_registry.can_start()
        assert db.conn.execute("PRAGMA query_only").fetchone()[0] == 1
    finally:
        db.conn.close()
        qapp.processEvents()


def test_package_self_test_requires_real_operations(tmp_path):
    from PySide6.QtWidgets import QApplication
    from utils.package_self_test import write_package_self_test_report

    qapp = QApplication.instance() or QApplication([])
    target = tmp_path / "functional.json"
    report = write_package_self_test_report(target)
    expected = {
        "numpy_simulation",
        "numpy_health",
        "lgbk1_roundtrip",
        "lgbk2_roundtrip",
        "window_open_close",
    }
    assert report["ok"] is True
    assert set(report["functional_checks"]) == expected
    assert all(report["functional_checks"][name]["passed"] is True for name in expected)
    assert report["functional_checks"]["numpy_simulation"]["median"] == pytest.approx(
        2200.0
    )
    for name in ("lgbk1_roundtrip", "lgbk2_roundtrip"):
        assert report["functional_checks"][name]["wrong_password_rejected"]
        assert report["functional_checks"][name]["truncated_rejected"]
        assert report["functional_checks"][name]["restored_attachment_sha256"]
    assert report["functional_checks"]["lgbk2_roundtrip"]["kdf_algorithm"] == "argon2id"
    assert json.loads(target.read_text(encoding="utf-8")) == report
    qapp.processEvents()


def test_failed_runtime_operation_never_replaces_an_existing_report(
    tmp_path, monkeypatch
):
    import services.financial_intelligence as intelligence
    from utils.package_self_test import write_package_self_test_report

    target = tmp_path / "previous-report.json"
    target.write_bytes(b"previous verified report")

    def fail(*_args, **_kwargs):
        raise RuntimeError("synthetic NumPy execution failure")

    monkeypatch.setattr(intelligence, "monte_carlo_projection", fail)
    with pytest.raises(RuntimeError, match="synthetic NumPy"):
        write_package_self_test_report(target)
    assert target.read_bytes() == b"previous verified report"
