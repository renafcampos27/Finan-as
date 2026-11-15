#!/usr/bin/env python3
"""Regression guard for the single canonical net worth definition.

Guards the v3.49 audit fixes:
  * ``FinanceDB.net_worth`` is the one canonical patrimony calculation:
    confirmed reference balances + movements, manual holdings, goals and
    liabilities -- never the double-counted legacy total.
  * Holdings bound to a bank account (``linked_account``) are excluded
    because the linked account ledger already represents that money.
  * Movements on orphan accounts (no active identity row) still count.
  * ``AnalyticsService.invalidate_caches`` really clears metric caches.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from database.engine import FinanceDB  # noqa: E402
from database.repo import Repo  # noqa: E402


@pytest.fixture
def repo(tmp_path, monkeypatch):
    import utils.config as config

    monkeypatch.setattr(config, "DB_NAME", str(tmp_path / "networth.sqlite3"))
    r = Repo()
    yield r
    r.db.conn.close()


def _seed_canonical_scenario(db: FinanceDB) -> None:
    db.conn.execute(
        "INSERT INTO financial_accounts(name,reference_balance,reference_date,"
        "balance_known) VALUES('CGD',1000,'2026-08-01',1)"
    )
    db.add_transaction("2026-08-05", "Salario", 500.0, "Rendimentos", "CGD", "income")
    db.add_transaction(
        "2026-08-06", "Supermercado", -300.0, "Supermercado", "CGD", "expense"
    )
    db.conn.execute(
        "INSERT INTO holdings(name,asset_class,value,linked_account) "
        "VALUES('ETF','ETF',650,'CGD')"
    )
    db.conn.execute(
        "INSERT INTO holdings(name,asset_class,value,linked_account) "
        "VALUES('Cripto','Cripto manual',500,'')"
    )
    db.conn.execute(
        "INSERT INTO goals(name,target_amount,saved_amount) VALUES('Fundo',1000,200)"
    )
    db.conn.execute("INSERT INTO liabilities(name,balance) VALUES('Credito',150)")
    db.conn.commit()


def test_net_worth_uses_reference_balances_and_excludes_linked_holdings(
    tmp_path, monkeypatch
):
    # Direct FinanceDB construction would otherwise seed the demo profile
    # (tests/conftest.py exports FINANCAS_PESSOAIS_DEMO_DATA for fixtures
    # that want a populated app).
    monkeypatch.delenv("FINANCAS_PESSOAIS_DEMO_DATA", raising=False)
    db = FinanceDB(tmp_path / "canonical.sqlite3")
    try:
        _seed_canonical_scenario(db)
        # Cash 1000 + 500 - 300 = 1200; manual holding 500; liability 150.
        # Goals (200) are virtual envelopes within cash and not double-counted (PERS-02).
        # The linked ETF (650) must not be counted twice.
        expected = 1200.0 + 500.0 - 150.0
        assert db.cash_balance() == pytest.approx(1200.0)
        assert db.net_worth() == pytest.approx(expected)
        # The raw-history legacy definition returned 1400 and understated cash.
        raw_cash = db.scalar("SELECT COALESCE(SUM(amount),0) FROM transactions")
        legacy = raw_cash + 650.0 + 500.0 - 150.0
        assert legacy == pytest.approx(1200.0)
        assert db.net_worth() != pytest.approx(legacy)
    finally:
        db.prepare_for_shutdown()


def test_net_worth_counts_orphan_account_movements(tmp_path, monkeypatch):
    monkeypatch.delenv("FINANCAS_PESSOAIS_DEMO_DATA", raising=False)
    db = FinanceDB(tmp_path / "orphans.sqlite3")
    try:
        db.conn.execute(
            "INSERT INTO transactions(date,description,amount,category,account,kind) "
            "VALUES('2026-08-05','Legado',250.0,'Diversos','Conta Fantasma','income')"
        )
        db.conn.commit()
        # No active identity row for 'Conta Fantasma': the money must still
        # appear through the orphan safety net.
        assert db.net_worth() == pytest.approx(250.0)
    finally:
        db.prepare_for_shutdown()


def test_cash_balance_reconciles_reference_at_any_date(tmp_path, monkeypatch):
    monkeypatch.delenv("FINANCAS_PESSOAIS_DEMO_DATA", raising=False)
    db = FinanceDB(tmp_path / "cash-reference.sqlite3")
    try:
        db.conn.execute(
            "INSERT INTO financial_accounts(name,reference_balance,reference_date,"
            "balance_known) VALUES('CGD',1000,'2026-08-10',1)"
        )
        db.add_transaction(
            "2026-08-01", "Histórico", 500.0, "Rendimentos", "CGD", "income"
        )
        db.add_transaction(
            "2026-08-11", "Entrada", 200.0, "Rendimentos", "CGD", "income"
        )
        db.add_transaction(
            "2026-08-12", "Saída", -50.0, "Supermercado", "CGD", "expense"
        )
        assert db.cash_balance("2026-07-31") == pytest.approx(500.0)
        assert db.cash_balance("2026-08-09") == pytest.approx(1000.0)
        assert db.cash_balance("2026-08-10") == pytest.approx(1000.0)
        assert db.cash_balance("2026-08-12") == pytest.approx(1150.0)
    finally:
        db.prepare_for_shutdown()


def test_account_balances_combine_multiple_references_and_orphans(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("FINANCAS_PESSOAIS_DEMO_DATA", raising=False)
    db = FinanceDB(tmp_path / "multi-account.sqlite3")
    try:
        repo = Repo(db)
        repo.set_financial_account_reference(
            "Conta A", 1_000.0, "2026-08-10", balance_known=True
        )
        repo.set_financial_account_reference(
            "Conta B", 500.0, "2026-08-12", balance_known=True
        )
        db.add_transaction(
            "2026-08-11", "Entrada A", 100.0, "Rendimentos", "Conta A", "income"
        )
        db.add_transaction(
            "2026-08-13", "Saída B", -50.0, "Diversos", "Conta B", "expense"
        )
        db.conn.execute(
            "INSERT INTO transactions(date,description,amount,category,account,kind) "
            "VALUES('2026-08-13','Legado',25,'Diversos','Conta órfã','income')"
        )
        db.conn.commit()

        rows = db.account_balances("2026-08-13")
        by_name = {str(row["account"]): row for row in rows}
        assert float(by_name["Conta A"]["balance"]) == pytest.approx(1_100.0)
        assert float(by_name["Conta B"]["balance"]) == pytest.approx(450.0)
        assert float(by_name["Conta órfã"]["balance"]) == pytest.approx(25.0)
        assert by_name["Conta órfã"]["source"] == "orphan_history"
        assert db.cash_balance("2026-08-13") == pytest.approx(1_575.0)
    finally:
        db.prepare_for_shutdown()


def test_broken_holding_link_remains_in_net_worth(tmp_path, monkeypatch):
    from services.financial_intelligence import FinancialIntelligenceService

    monkeypatch.delenv("FINANCAS_PESSOAIS_DEMO_DATA", raising=False)
    db = FinanceDB(tmp_path / "broken-link.sqlite3")
    try:
        db.conn.execute(
            "INSERT INTO holdings(name,asset_class,value,linked_account) "
            "VALUES('ETF órfão','ETF',700,'Conta inexistente')"
        )
        db.conn.commit()
        repo = Repo(db)
        snapshot = FinancialIntelligenceService(repo).net_worth_snapshot()
        assert db.net_worth() == pytest.approx(700.0)
        assert snapshot["investment_assets"] == pytest.approx(700.0)
        assert snapshot["data_quality"]["broken_holding_links"] == ["ETF órfão"]
    finally:
        db.prepare_for_shutdown()


def test_dashboard_and_forecasts_share_the_canonical_cash_balance(
    tmp_path, monkeypatch
):
    from datetime import date, timedelta

    from services.analytics import AnalyticsService

    monkeypatch.delenv("FINANCAS_PESSOAIS_DEMO_DATA", raising=False)
    db = FinanceDB(tmp_path / "consumer-balance.sqlite3")
    try:
        repo = Repo(db)
        today = date.today()
        reference_day = today - timedelta(days=2)
        repo.set_financial_account_reference(
            "CGD",
            1_000.0,
            reference_day.isoformat(),
            balance_known=True,
        )
        repo.add_transaction(
            (reference_day - timedelta(days=2)).isoformat(),
            "Histórico anterior",
            500.0,
            "Rendimentos",
            "CGD",
        )
        repo.add_transaction(
            (today - timedelta(days=1)).isoformat(),
            "Entrada posterior",
            100.0,
            "Rendimentos",
            "CGD",
        )

        analytics = AnalyticsService(repo)
        canonical = repo.cash_balance(today.isoformat())
        assert canonical == pytest.approx(1_100.0)
        assert analytics.balance_series(7)[-1]["balance"] == pytest.approx(canonical)
        assert analytics.cashflow_calendar_forecast(3, today)[
            "starting_balance"
        ] == pytest.approx(canonical)
        assert analytics.cash_runway_scenarios(3, today)[
            "starting_balance"
        ] == pytest.approx(canonical)
    finally:
        db.prepare_for_shutdown()


def test_monthly_and_daily_snapshots_store_canonical_values(tmp_path, monkeypatch):
    monkeypatch.delenv("FINANCAS_PESSOAIS_DEMO_DATA", raising=False)
    db = FinanceDB(tmp_path / "snapshot-balance.sqlite3")
    try:
        repo = Repo(db)
        repo.set_financial_account_reference(
            "CGD", 1_000.0, "2026-08-10", balance_known=True
        )
        repo.add_transaction("2026-08-01", "Histórico", 500.0, "Rendimentos", "CGD")
        repo.add_transaction("2026-08-11", "Entrada", 100.0, "Rendimentos", "CGD")
        repo.add_holding(
            "Saldo bancário",
            "Depósito",
            "Banco",
            1_100.0,
            1_100.0,
            linked_account="CGD",
        )
        repo.add_holding("ETF", "ETF", "Corretora", 700.0, 600.0)
        repo.db.conn.execute(
            "INSERT INTO goals(name,target_amount,saved_amount) VALUES('Fundo',500,200)"
        )
        repo.db.conn.execute(
            "INSERT INTO liabilities(name,balance) VALUES('Crédito',100)"
        )
        repo.db.conn.commit()

        repo.record_financial_state_snapshot("2026-08-20")
        repo.save_monthly_snapshot(
            "2026-08", "2026-08-01", "2026-08-20", "Fecho canónico"
        )

        daily = repo.q(
            "SELECT cash_balance FROM financial_state_snapshots WHERE day=?",
            ("2026-08-20",),
        )[0]
        monthly = repo.q(
            "SELECT cash_balance,net_worth FROM monthly_snapshots WHERE month=?",
            ("2026-08",),
        )[0]
        assert float(daily["cash_balance"]) == pytest.approx(1_100.0)
        assert float(monthly["cash_balance"]) == pytest.approx(1_100.0)
        assert float(monthly["net_worth"]) == pytest.approx(1_900.0)
    finally:
        db.prepare_for_shutdown()


def test_analytics_invalidate_caches_clears_metric_caches(repo):
    from services.analytics import AnalyticsService

    service = AnalyticsService(repo)
    service._monthly_summary_cache[("k",)] = []
    service._coverage_cache[("a", "b")] = {}
    service.invalidate_caches()
    assert not service._monthly_summary_cache
    assert not service._coverage_cache


def test_new_movement_reaches_monthly_summary_after_invalidation(repo):
    from datetime import date

    from services.analytics import AnalyticsService

    service = AnalyticsService(repo)
    before = service.monthly_summary(3)
    assert before == service.monthly_summary(3)
    month_key = date.today().strftime("%Y-%m")
    amount = 123.45
    repo.add_transaction(
        date.today().replace(day=10).isoformat(),
        "Unico",
        amount,
        "Rendimentos",
        "Conta unica",
        "manual",
    )
    service.invalidate_caches()
    after = service.monthly_summary(3)
    assert any(row.get("month") == month_key for row in after)
    row = next(row for row in after if row.get("month") == month_key)
    assert float(row.get("income") or 0) >= amount
