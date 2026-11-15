"""PERS-05-REG: historical financial_state_snapshots must be immutable.

record_financial_state_snapshot() used an unconditional UPSERT, so calling
it for a past day that already had a snapshot rewrote history with today's
live holdings/liabilities. Verified semantics:

- past day + existing snapshot -> returned unchanged, nothing written;
- past day + missing snapshot  -> backfilled from live state (INSERT);
- today                        -> mutable daily snapshot (UPSERT) as before.
"""

from __future__ import annotations

import json

import pytest

from database.engine import FinanceDB
from database.repo.main_repo import Repo


def _live_holding_name(repo: Repo) -> str:
    return str(repo.holdings()[0]["name"])


def test_pers_05_historical_snapshot_preserves_existing_past_day(
    tmp_path,
    monkeypatch,
) -> None:
    """Re-recording a past day must not overwrite the stored snapshot."""
    monkeypatch.delenv("FINANCAS_PESSOAIS_DEMO_DATA", raising=False)
    db = FinanceDB(tmp_path / "test_pers05_immutable.sqlite3")
    try:
        repo = Repo(db)
        past_holdings = [{"name": "Antiga Ação 2025", "value": 500.0}]
        db.conn.execute(
            "INSERT INTO financial_state_snapshots(day, holdings_json, "
            "liabilities_json, subscriptions_json, goals_json, cash_balance) "
            "VALUES('2025-12-31', ?, '[]', '[]', '[]', 1000.0)",
            (json.dumps(past_holdings),),
        )
        db.conn.execute("DELETE FROM holdings")
        db.conn.execute(
            "INSERT INTO holdings(name, asset_class, cost_basis, value) "
            "VALUES('Nova Ação 2026', 'Ações', 2000.0, 3000.0)"
        )
        db.conn.commit()

        result = repo.record_financial_state_snapshot("2025-12-31")

        assert result["holdings"] == past_holdings
        assert result["cash_balance"] == pytest.approx(1000.0)
        stored = db.conn.execute(
            "SELECT holdings_json, cash_balance FROM financial_state_snapshots "
            "WHERE day='2025-12-31'"
        ).fetchone()
        assert json.loads(str(stored["holdings_json"])) == past_holdings
        assert float(stored["cash_balance"]) == pytest.approx(1000.0)
        assert _live_holding_name(repo) == "Nova Ação 2026"
    finally:
        db.close()


def test_pers_05_backfill_missing_day(tmp_path, monkeypatch) -> None:
    """A past day without a snapshot is backfilled from live state."""
    monkeypatch.delenv("FINANCAS_PESSOAIS_DEMO_DATA", raising=False)
    db = FinanceDB(tmp_path / "test_pers05_backfill.sqlite3")
    try:
        repo = Repo(db)
        db.conn.execute("DELETE FROM holdings")
        db.conn.execute(
            "INSERT INTO holdings(name, asset_class, cost_basis, value) "
            "VALUES('Backfill ETF', 'ETF', 1500.0, 1800.0)"
        )
        db.conn.commit()

        result = repo.record_financial_state_snapshot("2025-06-30")

        assert [row["name"] for row in result["holdings"]] == ["Backfill ETF"]
        stored = db.conn.execute(
            "SELECT holdings_json FROM financial_state_snapshots "
            "WHERE day='2025-06-30'"
        ).fetchone()
        assert stored is not None
        assert json.loads(str(stored["holdings_json"])) == result["holdings"]
    finally:
        db.close()


def test_pers_05_today_still_refreshable(tmp_path, monkeypatch) -> None:
    """Today's snapshot remains mutable: re-recording overwrites."""
    monkeypatch.delenv("FINANCAS_PESSOAIS_DEMO_DATA", raising=False)
    db = FinanceDB(tmp_path / "test_pers05_today.sqlite3")
    try:
        repo = Repo(db)
        db.conn.execute("DELETE FROM holdings")
        db.conn.execute(
            "INSERT INTO holdings(name, asset_class, cost_basis, value) "
            "VALUES('ETF A', 'ETF', 100.0, 120.0)"
        )
        db.conn.commit()
        first = repo.record_financial_state_snapshot()
        assert [row["name"] for row in first["holdings"]] == ["ETF A"]

        db.conn.execute(
            "UPDATE holdings SET name='ETF B', value=250.0"
        )
        db.conn.commit()
        second = repo.record_financial_state_snapshot()

        assert [row["name"] for row in second["holdings"]] == ["ETF B"]
        today_rows = db.conn.execute(
            "SELECT COUNT(*) AS n FROM financial_state_snapshots WHERE day=?",
            (second["day"],),
        ).fetchone()
        assert int(today_rows["n"]) == 1
    finally:
        db.close()
