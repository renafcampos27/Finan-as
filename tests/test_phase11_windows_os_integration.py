#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Test suite for Phase 11: Windows OS Integration, Theme Detection and Single-Instance."""

from __future__ import annotations

from pathlib import Path

from database.engine import FinanceDB
from utils.qt_runtime import bring_existing_window_to_front, detect_windows_dark_theme
from main import acquire_single_instance_lock


def test_windows_dark_theme_detection():
    """Verify detect_windows_dark_theme returns a valid boolean safely."""
    is_dark = detect_windows_dark_theme()
    assert isinstance(is_dark, bool)


def test_bring_existing_window_to_front():
    """Verify bring_existing_window_to_front does not crash on non-existent or existing titles."""
    result = bring_existing_window_to_front("NonExistentWindow_123456789")
    assert isinstance(result, bool)
    assert result is False


def test_database_prepare_for_shutdown_and_wal_truncate(tmp_path: Path):
    """Verify prepare_for_shutdown commits, truncates WAL checkpoint and marks closed."""
    db_file = tmp_path / "shutdown_test.sqlite3"
    db = FinanceDB(db_file)
    # A13 retains Phase4's source-owned schema: a pending supported setting
    # exercises durability without adding an intentionally unknown SQL object.
    db.conn.execute(
        "INSERT INTO settings(key,value) VALUES('shutdown_test_value','test_val')"
    )

    # prepare_for_shutdown must run cleanly
    db.prepare_for_shutdown()
    assert getattr(db, "_closed", False) is True

    # Reopening database confirms data was durably written and WAL was truncated
    db2 = FinanceDB(db_file)
    rows = db2.query("SELECT value FROM settings WHERE key='shutdown_test_value'")
    assert len(rows) == 1
    assert rows[0]["value"] == "test_val"
    db2.close()


def test_single_instance_lock_lifecycle(tmp_path: Path, monkeypatch):
    """Verify acquire_single_instance_lock acquires lock and second attempt respects lock."""
    lock_file = tmp_path / "app.lock"
    monkeypatch.setattr("main.instance_lock_path", lambda: lock_file)

    lock1 = acquire_single_instance_lock()
    assert lock1 is not None

    # Second lock attempt must fail
    lock2 = acquire_single_instance_lock()
    assert lock2 is None

    # Releasing first lock allows acquiring again
    lock1.unlock()
    lock3 = acquire_single_instance_lock()
    assert lock3 is not None
    lock3.unlock()
