from __future__ import annotations

import database.repo.base as repo_base
from database.repo import Repo


def test_investment_history_records_and_updates_real_daily_values(
    tmp_path, monkeypatch
):
    database_path = tmp_path / "investment-history.sqlite3"
    monkeypatch.setattr(repo_base, "db_path", lambda: database_path)
    repo = Repo()
    repo.db.conn.execute("DELETE FROM holdings")
    repo.db.conn.execute("DELETE FROM investment_snapshots")
    repo.db.conn.commit()

    repo.add_holding(
        "ETF global",
        "ETF",
        "Manual",
        1100,
        1000,
        fees=12.5,
    )
    repo.record_investment_snapshot("2026-07-09")
    holding_id = int(repo.holdings()[0]["id"])
    repo.update_holding(
        holding_id,
        "ETF global",
        "ETF",
        "Manual",
        1250,
        1000,
        fees=15,
    )
    repo.record_investment_snapshot("2026-07-10")

    points = repo.investment_evolution()
    by_day = {point["date"]: point for point in points}
    assert {"2026-07-09", "2026-07-10"}.issubset(by_day)
    assert by_day["2026-07-09"]["invested"] == 1000
    assert by_day["2026-07-09"]["total"] == 1100
    assert by_day["2026-07-10"]["gain"] == 250
    assert by_day["2026-07-10"]["return_pct"] == 25
    assert float(repo.holding_by_id(holding_id)["fees"]) == 15

    repo.update_holding(holding_id, "ETF global", "ETF", "Manual", 1300, 1000)
    repo.record_investment_snapshot("2026-07-10")
    updated = repo.investment_evolution()
    updated_by_day = {point["date"]: point for point in updated}
    assert updated_by_day["2026-07-10"]["total"] == 1300
    repo.db.conn.close()


def test_unchanged_investment_snapshot_does_not_rewrite_timestamp(
    tmp_path, monkeypatch
):
    database_path = tmp_path / "investment-snapshot-idempotent.sqlite3"
    monkeypatch.setattr(repo_base, "db_path", lambda: database_path)
    repo = Repo()
    repo.db.conn.execute("DELETE FROM holdings")
    repo.db.conn.execute("DELETE FROM investment_snapshots")
    repo.db.conn.commit()

    holding_id = repo.add_holding("ETF global", "ETF", "Manual", 1100, 1000)
    first = repo.record_investment_snapshot("2026-07-10")
    repo.db.conn.execute(
        "UPDATE investment_snapshots SET updated_at='2000-01-01 00:00:00' "
        "WHERE day='2026-07-10'"
    )
    repo.db.conn.commit()

    unchanged = repo.record_investment_snapshot("2026-07-10")
    unchanged_timestamp = repo.q(
        "SELECT updated_at FROM investment_snapshots WHERE day='2026-07-10'"
    )[0]["updated_at"]
    repo.update_holding(
        holding_id,
        "ETF global",
        "ETF",
        "Manual",
        1250,
        1000,
    )
    changed = repo.record_investment_snapshot("2026-07-10")

    assert first["changed"] is True
    assert unchanged["changed"] is False
    assert unchanged_timestamp == "2000-01-01 00:00:00"
    assert changed["changed"] is True
    repo.db.conn.close()
