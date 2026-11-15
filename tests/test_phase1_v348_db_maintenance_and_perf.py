from __future__ import annotations

from pathlib import Path
import pytest

from database.engine import FinanceDB
from database.repo.main_repo import Repo


def test_database_storage_stats_reporting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.delenv("FINANCAS_PESSOAIS_DEMO_DATA", raising=False)
    db_file = tmp_path / "stats_test.sqlite3"
    db = FinanceDB(db_file)
    try:
        db.add_transaction(
            "2026-08-01", "Salário", 2500.0, "Salário", "Conta principal", "income"
        )
        db.add_transaction(
            "2026-08-05",
            "Supermercado",
            -85.50,
            "Alimentação",
            "Conta principal",
            "expense",
        )

        stats = db.database_storage_stats()
        assert stats["db_size_bytes"] > 0
        assert stats["total_transactions"] == 2
        assert stats["total_categories"] >= 2
        assert stats["total_accounts"] >= 1
        assert stats["schema_version"] == FinanceDB.CURRENT_SCHEMA_VERSION
        assert stats["total_size_bytes"] >= stats["db_size_bytes"]
    finally:
        db.close()


def test_optimize_database_compacts_and_reindexes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.delenv("FINANCAS_PESSOAIS_DEMO_DATA", raising=False)
    db_file = tmp_path / "optimize_test.sqlite3"
    db = FinanceDB(db_file)
    try:
        for i in range(100):
            db.add_transaction(
                "2026-08-01",
                f"Despesa {i}",
                -10.0,
                "Diversos",
                "Conta principal",
                "expense",
            )

        db.conn.execute("DELETE FROM transactions WHERE id <= 50")
        db.conn.commit()

        result = db.optimize_database()
        assert result["status"] == "ok"
        assert result["duration_ms"] >= 0
        assert result["stats"]["total_transactions"] == 50
        assert result["before_bytes"] > 0
        assert result["after_bytes"] > 0
    finally:
        db.close()


def test_repo_forwards_maintenance_operations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.delenv("FINANCAS_PESSOAIS_DEMO_DATA", raising=False)
    monkeypatch.setenv("FINANCAS_PESSOAIS_STATE_DIR", str(tmp_path / "profile"))
    repo = Repo()
    try:
        stats = repo.database_storage_stats()
        assert isinstance(stats, dict)
        assert "total_transactions" in stats

        result = repo.optimize_database()
        assert result["status"] == "ok"
    finally:
        repo.close()
