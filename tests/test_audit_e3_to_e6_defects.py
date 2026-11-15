"""Regression tests for audit defect batch E3 through E6.

Defects covered:
- E3: AI-secret ZIP scrubbing bypasses ZIP safety limits (services/backup.py)
- E4: Legacy backup-password history is unbounded (services/backup.py)
- E5: Editing a split transaction stores sub-cent allocation values (database/repo/transactions.py)
- E6: Sharpe and Sortino silently treat malformed returns as zero (services/finance_math.py)
"""

from __future__ import annotations

import json
import sqlite3
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from database.engine import FinanceDB
from database.repo.main_repo import Repo
from services.backup import BackupService
from services.finance_math import (
    _clean_returns_sequence,
    sharpe_ratio,
    sortino_ratio,
)


# ==============================================================================
# E3: AI-secret ZIP scrubbing safety limits
# ==============================================================================

def test_e3_scrub_zip_rejects_unrelated_zip(tmp_path: Path):
    """Archives without application database or metadata members are ignored."""
    archive_path = tmp_path / "unrelated.zip"
    with zipfile.ZipFile(archive_path, "w") as zf:
        zf.writestr("notes.txt", "just some text")

    result = BackupService.scrub_ai_secret_from_zip_file(archive_path)
    assert result is False


def test_e3_scrub_zip_rejects_zip_bomb_or_path_traversal(tmp_path: Path):
    """Archives with path traversal (e.g. ../secret.db) are caught by validate_zip_safety."""
    archive_path = tmp_path / "malicious.zip"
    with zipfile.ZipFile(archive_path, "w") as zf:
        zf.writestr("../evil.sqlite3", b"dummy")

    result = BackupService.scrub_ai_secret_from_zip_file(archive_path)
    assert result is False


def test_e3_scrub_zip_handles_bad_zip(tmp_path: Path):
    """Corrupted zip files fail boundedly without crashing."""
    corrupt_path = tmp_path / "corrupt.zip"
    corrupt_path.write_bytes(b"PKcorruptedbytesthatarenotvalidzip")

    result = BackupService.scrub_ai_secret_from_zip_file(corrupt_path)
    assert result is False


def test_e3_scrub_zip_scrubs_valid_sqlite(tmp_path: Path):
    """Valid archive containing sqlite db with ai_gemini_api_key is scrubbed."""
    db_path = tmp_path / "inner.sqlite3"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO settings VALUES ('ai_gemini_api_key', 'AIzaSy12345')")
    conn.commit()
    conn.close()

    archive_path = tmp_path / "backup.zip"
    with zipfile.ZipFile(archive_path, "w") as zf:
        zf.write(db_path, arcname="database.sqlite3")

    result = BackupService.scrub_ai_secret_from_zip_file(archive_path)
    assert result is True

    # Verify key was scrubbed from inner database
    with zipfile.ZipFile(archive_path, "r") as zf:
        extracted = tmp_path / "extracted.sqlite3"
        extracted.write_bytes(zf.read("database.sqlite3"))
        conn2 = sqlite3.connect(str(extracted))
        row = conn2.execute(
            "SELECT value FROM settings WHERE key='ai_gemini_api_key'"
        ).fetchone()
        conn2.close()
        assert row is None


# ==============================================================================
# E4: Legacy backup password history bounds
# ==============================================================================

def test_e4_password_history_bounded_to_max_entries(tmp_path: Path):
    """History is sliced to MAX_LEGACY_PASSWORD_HISTORY (10 entries)."""
    db_file = tmp_path / "test.db"
    fdb = FinanceDB(str(db_file))
    repo = Repo(fdb)

    service = BackupService(repo, database_file=db_file)
    mock_store = MagicMock()
    passwords = [f"password_{i:03d}" for i in range(25)]
    mock_store.read.return_value = json.dumps(passwords)
    service.password_history_store = mock_store

    history = service._stored_previous_backup_passwords()
    assert len(history) == BackupService.MAX_LEGACY_PASSWORD_HISTORY
    assert len(history) == 10
    assert history == passwords[:10]


def test_e4_backup_password_candidates_bounded(tmp_path: Path):
    """Candidates list contains current password plus up to 10 history items."""
    db_file = tmp_path / "test.db"
    fdb = FinanceDB(str(db_file))
    repo = Repo(fdb)

    service = BackupService(repo, database_file=db_file)
    service.stored_backup_password = MagicMock(return_value="current_pass123")
    mock_store = MagicMock()
    passwords = [f"old_password_{i:03d}" for i in range(20)]
    mock_store.read.return_value = json.dumps(passwords)
    service.password_history_store = mock_store

    candidates = service.backup_password_candidates()
    assert len(candidates) <= 1 + BackupService.MAX_LEGACY_PASSWORD_HISTORY
    assert candidates[0] == "current_pass123"


def test_e4_rotate_password_rejects_oversized_history(tmp_path: Path):
    """Rotation raises ValueError if prospective history bytes exceed MAX_LEGACY_PASSWORD_HISTORY_BYTES."""
    db_file = tmp_path / "test.db"
    fdb = FinanceDB(str(db_file))
    repo = Repo(fdb)

    service = BackupService(repo, database_file=db_file)
    service.stored_backup_password = MagicMock(return_value="curr_pass123")
    huge_pw = "a" * 300
    mock_store = MagicMock()
    mock_store.read.return_value = json.dumps([f"{huge_pw}_{i}" for i in range(10)])
    service.password_history_store = mock_store

    with patch("services.startup_security.get_active_session", return_value=None):
        with pytest.raises(ValueError, match="excedeu o limite máximo seguro"):
            service.rotate_backup_password("curr_pass123", "new_secure_pass123")


# ==============================================================================
# E5: Split transaction amount updates with exact cents
# ==============================================================================

def test_e5_split_transaction_amount_update_exact_cents(tmp_path: Path):
    """Updating transaction amount with 3 equal splits keeps exact integer cents."""
    db_file = tmp_path / "financas.db"
    fdb = FinanceDB(str(db_file))
    repo = Repo(fdb)

    tx_id = repo.add_transaction(
        d="2026-09-01",
        desc="Compra dividida",
        amount=-10.00,
        category="Despesas Gerais",
        account="Conta Ordem",
    )

    splits = [
        ("Alimentação", 33.33),
        ("Transportes", 33.33),
        ("Lazer", 33.34),
    ]
    repo.save_transaction_splits(tx_id, splits)

    # Now update the transaction amount to -0.05
    repo._update_transaction_fields(tx_id, amount=-0.05)

    rows = fdb.conn.execute(
        "SELECT category, percent, amount FROM transaction_splits WHERE transaction_id=? ORDER BY id ASC",
        (tx_id,),
    ).fetchall()

    assert len(rows) == 3
    amounts = [float(r["amount"]) for r in rows]
    assert sum(amounts) == pytest.approx(0.05, abs=1e-6)
    for a in amounts:
        assert round(a, 2) == a
        assert round(a * 100) == a * 100


def test_e5_update_transaction_fields_supports_goal_id(tmp_path: Path):
    """_update_transaction_fields accepts goal_id in allowed_fields."""
    db_file = tmp_path / "financas.db"
    fdb = FinanceDB(str(db_file))
    repo = Repo(fdb)

    goal_id = repo.add_goal("Poupança Férias", 1000.0, 0.0, 100.0, "2026-12-31")

    tx_id = repo.add_transaction(
        d="2026-09-01",
        desc="Poupança",
        amount=-50.00,
        category="Poupança",
        account="Conta Ordem",
    )

    repo._update_transaction_fields(tx_id, goal_id=goal_id)

    row = fdb.conn.execute(
        "SELECT goal_id FROM transactions WHERE id=?", (tx_id,)
    ).fetchone()
    assert row["goal_id"] == goal_id

    # Test clearing goal_id with None
    repo._update_transaction_fields(tx_id, goal_id=None)
    row_cleared = fdb.conn.execute(
        "SELECT goal_id FROM transactions WHERE id=?", (tx_id,)
    ).fetchone()
    assert row_cleared["goal_id"] is None


# ==============================================================================
# E6: Sharpe and Sortino ratio return cleaning
# ==============================================================================

def test_e6_clean_returns_sequence():
    """_clean_returns_sequence filters out None, booleans, NaN, inf, and strings."""
    raw = [10.0, "bad", None, float("nan"), float("inf"), True, False, -2.5, "3.5"]
    clean = _clean_returns_sequence(raw)
    assert clean == [10.0, -2.5, 3.5]


def test_e6_sharpe_ratio_ignores_malformed_entries():
    """sharpe([10, "bad", 10]) equals sharpe([10, 10])."""
    res_clean = sharpe_ratio([10.0, 10.0])
    res_dirty = sharpe_ratio([10.0, "bad", 10.0])
    assert res_clean == 99.99
    assert res_dirty == 99.99


def test_e6_sharpe_ratio_clamped_bounds():
    """Sharpe ratio is clamped within [-99.99, 99.99]."""
    res_high = sharpe_ratio([1000.0, 1000.01])
    assert res_high <= 99.99
    assert res_high >= -99.99


def test_e6_sortino_ratio_ignores_malformed_entries():
    """sortino_ratio handles None, strings, NaN without treating them as 0."""
    res_clean = sortino_ratio([5.0, 8.0, 12.0])
    res_dirty = sortino_ratio([5.0, "invalid", None, float("nan"), 8.0, 12.0])
    assert res_clean == pytest.approx(res_dirty, rel=1e-6)


def test_e6_sharpe_and_sortino_insufficient_data():
    """Fewer than 2 valid returns results in 0.0."""
    assert sharpe_ratio([]) == 0.0
    assert sharpe_ratio([10.0]) == 0.0
    assert sharpe_ratio(["bad", None, float("nan")]) == 0.0

    assert sortino_ratio([]) == 0.0
    assert sortino_ratio([10.0]) == 0.0
    assert sortino_ratio(["bad", None, float("nan")]) == 0.0
