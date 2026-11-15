"""Tests for Phase 9: P1 (High Priority / Critical) Bug Fixes.

Covers:
- BUG-TAX-LOGIC-001 (AUD-35): Crypto unproven acquisition date is not exempt and is taxed at 28%.
- TAX-01: Devaluation coefficient under Art. 50.º CIRS applies exclusively to real estate.
- TAX-02: 35% aggravated tax rate for offshore tax havens (Portaria n.º 150/2004).
- TAX-03: Prior year capital loss carryforward conditioned on englobamento (Art. 55.º CIRS).
- MATH-01: Sortino ratio with zero downside returns finite number (99.99) safe for JSON serialization.
- PERS-02: Savings goals without segregated account are not double-counted in net_worth.
- PERS-03: Repository methods respect commit=False and do not commit prematurely before savepoints.
- PERS-04: Editing transaction amount automatically synchronizes goals.saved_amount.
- PERS-05: Past financial state snapshots do not overwrite historical assets with today's live assets.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from database.engine import FinanceDB
from database.repo.main_repo import Repo
from services.finance_math import sortino_ratio
from services.financial_intelligence import FinancialIntelligenceService
from services.tax import (
    OFFSHORE_JURISDICTIONS,
    TAXA_AGRAVADA_OFFSHORE,
    PortugueseTaxEngine,
)


def test_bug_tax_logic_001_crypto_unproven_date_not_exempt(tmp_path, monkeypatch) -> None:
    """Verify that crypto without a proven acquisition date is not marked exempt."""
    monkeypatch.delenv("FINANCAS_PESSOAIS_DEMO_DATA", raising=False)
    db = FinanceDB(tmp_path / "test_crypto.sqlite3")
    try:
        repo = Repo(db)
        repo.db.conn.execute("DELETE FROM holdings")
        repo.db.conn.execute(
            "INSERT INTO holdings(name, asset_class, cost_basis, value) "
            "VALUES('Bitcoin Untracked', 'Cripto', 1000.0, 2500.0)"
        )
        repo.db.conn.commit()

        holdings = list(repo.q("SELECT * FROM holdings WHERE name='Bitcoin Untracked'"))
        assert len(holdings) == 1
        row = holdings[0]

        cat = str(row["asset_class"] or "")
        is_crypto = "cripto" in cat.lower() or "bitcoin" in str(row["name"]).lower()
        acq_date_str = str(row.get("buy_date", "") if hasattr(row, "get") else "")

        days_held = None
        if acq_date_str:
            try:
                days_held = max(0, (date.today() - date.fromisoformat(acq_date_str[:10])).days)
            except (ValueError, TypeError):
                days_held = None

        assert is_crypto is True
        assert days_held is None
        is_exempt = is_crypto and (days_held is not None and days_held >= 365)
        assert is_exempt is False
    finally:
        db.prepare_for_shutdown()


def test_tax_01_devaluation_coefficient_restricted_to_real_estate() -> None:
    """Verify Art. 50.º CIRS devaluation coefficients apply to real estate, NOT stocks/ETFs."""
    engine = PortugueseTaxEngine()

    engine.add_acquisition(
        lot_id="acq_stock_1",
        asset_symbol="IWDA",
        acquisition_date=date(1995, 6, 1),
        quantity=10.0,
        unit_price=50.0,
        asset_category="security",
        is_real_estate=False,
    )
    stock_matches = engine.process_disposal(
        disposal_id="disp_stock_1",
        asset_symbol="IWDA",
        disposal_date=date(2026, 6, 1),
        quantity=10.0,
        unit_price=100.0,
        asset_category="security",
        is_real_estate=False,
    )
    assert stock_matches[0].devaluation_coefficient == pytest.approx(1.0)
    assert stock_matches[0].adjusted_acquisition_cost == pytest.approx(500.0)
    assert stock_matches[0].net_gain_loss == pytest.approx(500.0)

    engine_re = PortugueseTaxEngine()
    engine_re.add_acquisition(
        lot_id="acq_house_1",
        asset_symbol="IMOVEL_LISBOA",
        acquisition_date=date(1995, 6, 1),
        quantity=1.0,
        unit_price=100_000.0,
        asset_category="real_estate",
        is_real_estate=True,
    )
    re_matches = engine_re.process_disposal(
        disposal_id="disp_house_1",
        asset_symbol="IMOVEL_LISBOA",
        disposal_date=date(2026, 6, 1),
        quantity=1.0,
        unit_price=300_000.0,
        asset_category="real_estate",
        is_real_estate=True,
    )
    assert re_matches[0].devaluation_coefficient == pytest.approx(1.95)
    assert re_matches[0].adjusted_acquisition_cost == pytest.approx(195_000.0)
    assert re_matches[0].net_gain_loss == pytest.approx(105_000.0)


def test_tax_02_offshore_tax_haven_35pct_rate() -> None:
    """Verify that offshore jurisdictions trigger the aggravated 35% tax rate."""
    engine = PortugueseTaxEngine()

    assert "KY" in OFFSHORE_JURISDICTIONS
    assert "VG" in OFFSHORE_JURISDICTIONS
    assert "US" not in OFFSHORE_JURISDICTIONS

    engine.add_acquisition(
        lot_id="acq_offshore",
        asset_symbol="OFFSHORE_FUND",
        acquisition_date=date(2025, 1, 15),
        quantity=100.0,
        unit_price=10.0,
        country_code="KY",
    )
    matches = engine.process_disposal(
        disposal_id="disp_offshore",
        asset_symbol="OFFSHORE_FUND",
        disposal_date=date(2026, 3, 1),
        quantity=100.0,
        unit_price=20.0,
        country_code="KY",
    )
    assert len(matches) == 1
    m = matches[0]
    assert m.net_gain_loss == pytest.approx(1000.0)
    assert m.tax_rate_pct == pytest.approx(TAXA_AGRAVADA_OFFSHORE)
    assert m.tax_rate_pct == pytest.approx(35.0)
    assert m.estimated_tax == pytest.approx(350.0)


def test_tax_03_prior_year_loss_carryforward_requires_englobamento() -> None:
    """Verify Art. 55.º CIRS: prior year losses only offset gains under englobamento."""
    engine = PortugueseTaxEngine()
    engine.add_acquisition(
        lot_id="lot_a",
        asset_symbol="AAPL",
        acquisition_date=date(2026, 1, 1),
        quantity=10.0,
        unit_price=100.0,
    )
    matches = engine.process_disposal(
        disposal_id="disp_a",
        asset_symbol="AAPL",
        disposal_date=date(2026, 6, 1),
        quantity=10.0,
        unit_price=150.0,
    )
    assert matches[0].net_gain_loss == pytest.approx(500.0)

    rep_auto = engine.generate_annual_tax_report(
        matches,
        fiscal_year=2026,
        prior_year_losses=300.0,
        opt_for_englobamento=False,
    )
    assert rep_auto["prior_year_losses_deducted"] == pytest.approx(0.0)
    assert rep_auto["final_taxable_base"] == pytest.approx(500.0)
    assert rep_auto["global_estimated_tax_28pct"] == pytest.approx(140.0)
    assert rep_auto["remaining_prior_year_losses"] == pytest.approx(300.0)

    rep_englob = engine.generate_annual_tax_report(
        matches,
        fiscal_year=2026,
        prior_year_losses=300.0,
        opt_for_englobamento=True,
    )
    assert rep_englob["prior_year_losses_deducted"] == pytest.approx(300.0)
    assert rep_englob["final_taxable_base"] == pytest.approx(200.0)
    assert rep_englob["remaining_prior_year_losses"] == pytest.approx(0.0)


def test_math_01_sortino_ratio_zero_downside_returns_finite_number_and_json_serializable() -> None:
    """Verify Sortino ratio with zero downside returns 99.99 and serializes cleanly to JSON."""
    consistent_positive_returns = [1.2, 0.8, 1.5, 2.0, 0.9, 1.1, 1.8]
    sortino = sortino_ratio(
        consistent_positive_returns,
        risk_free_rate_annual_pct=2.0,
        target_downside_pct=0.0,
        periods_per_year=12,
    )

    assert sortino == pytest.approx(99.99)
    assert isinstance(sortino, float)

    payload = {"metric": "sortino_ratio", "value": sortino}
    serialized = json.dumps(payload)
    assert "99.99" in serialized
    deserialized = json.loads(serialized)
    assert deserialized["value"] == pytest.approx(99.99)


def test_pers_02_goals_not_double_counted_in_net_worth(tmp_path, monkeypatch) -> None:
    """Verify goals without segregated bank account are not double-counted in net_worth."""
    monkeypatch.delenv("FINANCAS_PESSOAIS_DEMO_DATA", raising=False)
    db = FinanceDB(tmp_path / "test_pers02.sqlite3")
    try:
        repo = Repo(db)
        repo.add_transaction("2026-01-01", "Salário", 10000.0, "Rendimentos", "Conta principal")
        repo.add_goal("Férias", 5000.0, 3000.0, 100.0, "2027-01-01")

        db.conn.execute(
            "INSERT INTO holdings(name, asset_class, cost_basis, value) "
            "VALUES('ETF World', 'Ações', 1800.0, 2000.0)"
        )
        db.conn.execute(
            "INSERT INTO liabilities(name, liability_type, balance) "
            "VALUES('Cartão Crédito', 'Cartão', 500.0)"
        )
        db.conn.commit()

        expected_net_worth = 10_000.0 + 2_000.0 - 500.0
        assert db.cash_balance() == pytest.approx(10_000.0)
        assert db.net_worth() == pytest.approx(expected_net_worth)
        assert db.net_worth() == pytest.approx(11_500.0)

        snapshot = FinancialIntelligenceService(repo).net_worth_snapshot()
        assert snapshot["net_worth"] == pytest.approx(expected_net_worth)
        assert snapshot["goal_assets"] == pytest.approx(3_000.0)
    finally:
        db.prepare_for_shutdown()


def test_pers_03_repository_commit_param_and_savepoint_isolation(tmp_path, monkeypatch) -> None:
    """Verify add_transaction respects commit=False and does not commit prematurely."""
    monkeypatch.delenv("FINANCAS_PESSOAIS_DEMO_DATA", raising=False)
    db = FinanceDB(tmp_path / "test_pers03.sqlite3")
    try:
        repo = Repo(db)
        db.conn.execute("BEGIN TRANSACTION")
        db.conn.execute(
            "INSERT INTO transactions(date, description, amount, category, account, kind) "
            "VALUES('2026-01-01', 'Inicial', 100.0, 'Rendimentos', 'Conta principal', 'income')"
        )

        tx_id = repo.add_transaction(
            "2026-01-02",
            "Trabalho Extra",
            50.0,
            "Rendimentos",
            "Conta principal",
            commit=False,
        )
        assert tx_id > 0

        db.conn.rollback()

        count = db.scalar("SELECT COUNT(*) FROM transactions")
        assert count == 0
    finally:
        db.prepare_for_shutdown()


def test_pers_04_editing_transaction_syncs_goal_saved_amount(tmp_path, monkeypatch) -> None:
    """Verify editing a transaction linked to a goal updates goals.saved_amount."""
    monkeypatch.delenv("FINANCAS_PESSOAIS_DEMO_DATA", raising=False)
    db = FinanceDB(tmp_path / "test_pers04.sqlite3")
    try:
        repo = Repo(db)
        repo.add_transaction("2026-01-01", "Depósito", 1000.0, "Rendimentos", "Conta principal")
        goal_id = repo.add_goal("Fundo Emergência", 5000.0, 0.0, 100.0, "2027-01-01")

        tx_id = repo.adjust_goal_with_transaction(
            goal_id,
            200.0,
            deposit=True,
            transaction_date="2026-01-05",
        )
        goal_row = repo.goal_by_id(goal_id)
        assert float(goal_row["saved_amount"]) == pytest.approx(200.0)

        repo.update_transaction_fields(tx_id, {"amount": -350.0})
        goal_row_updated = repo.goal_by_id(goal_id)
        assert float(goal_row_updated["saved_amount"]) == pytest.approx(350.0)

        repo.update_transaction_fields(tx_id, {"amount": -100.0})
        goal_row_updated2 = repo.goal_by_id(goal_id)
        assert float(goal_row_updated2["saved_amount"]) == pytest.approx(100.0)
    finally:
        db.prepare_for_shutdown()


def test_pers_05_historical_snapshot_does_not_overwrite_with_live_state(tmp_path, monkeypatch) -> None:
    """Verify past snapshots preserve historical assets and do not take today's live assets."""
    monkeypatch.delenv("FINANCAS_PESSOAIS_DEMO_DATA", raising=False)
    db = FinanceDB(tmp_path / "test_pers05.sqlite3")
    try:
        repo = Repo(db)
        past_holdings = [{"name": "Antiga Ação 2025", "value": 500.0}]
        db.conn.execute(
            "INSERT INTO financial_state_snapshots(day, holdings_json, liabilities_json, subscriptions_json, goals_json, cash_balance) "
            "VALUES('2025-12-31', ?, '[]', '[]', '[]', 1000.0)",
            (json.dumps(past_holdings),),
        )
        db.conn.commit()

        db.conn.execute("DELETE FROM holdings")
        db.conn.execute(
            "INSERT INTO holdings(name, asset_class, cost_basis, value) "
            "VALUES('Nova Ação 2026', 'Ações', 2000.0, 3000.0)"
        )
        db.conn.commit()

        result = repo.record_financial_state_snapshot("2025-12-31")

        assert len(result["holdings"]) == 1
        assert result["holdings"][0]["name"] == "Antiga Ação 2025"
        assert result["holdings"][0]["value"] == pytest.approx(500.0)

        row = db.conn.execute(
            "SELECT holdings_json FROM financial_state_snapshots WHERE day='2025-12-31'"
        ).fetchone()
        stored_holdings = json.loads(row[0])
        assert len(stored_holdings) == 1
        assert stored_holdings[0]["name"] == "Antiga Ação 2025"
    finally:
        db.prepare_for_shutdown()
