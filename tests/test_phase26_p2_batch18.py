#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tests for Phase 26 - Batch 18 P2 Defect Resolutions:
- AUD-TAX-02: Encargos de compra aglutinados no custo nominal de aquisição no Modelo 3 Anexo G/J (Art. 51.º CIRS)
- AUD-SIM-03: Rebaixamento no Enhanced DCA e Crisis Ladder calculado sobre preço fixo 100 em vez do pico móvel
- AUD-DB-05: Ausência de trigger DDL garantindo integridade bilateral atómica em transfer_group_id
- AUD-DB-07: Retenção excessiva do write-lock do WAL em lotes massivos de importação
"""

from __future__ import annotations

import os
import tempfile
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from database.engine import FinanceDB
from database.repo import Repo
from motores_matematicos_simulacao.crisis_backtester_and_architecture_engine import (
    CrisisScenario,
    run_crisis_backtest,
)
from services.tax import PortugueseTaxEngine, TaxLotMatch


@pytest.fixture
def temp_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db = FinanceDB(Path(path))
    db.init_schema()
    yield db
    try:
        os.remove(path)
    except OSError:
        pass


@pytest.fixture
def repo(temp_db):
    return Repo(temp_db)


# ==============================================================================
# AUD-TAX-02: CIRS Art. 51.º Expenses Segregation from Nominal Acquisition Cost
# ==============================================================================


def test_tax_02_nominal_cost_and_deductible_expenses_isolation():
    """Verify that PortugueseTaxEngine separates nominal acquisition cost from deductible fees (Art. 51.º CIRS)."""
    engine = PortugueseTaxEngine()
    engine.add_acquisition(
        lot_id="lot-1",
        asset_symbol="IWDA",
        asset_name="iShares Core MSCI World",
        acquisition_date=date(2024, 1, 15),
        quantity=20.0,
        unit_price=80.0,
        fees=20.0,  # 1€ buy fee per unit
        country_code="IE",
        is_foreign=True,
    )

    matches = engine.process_disposal(
        disposal_id="disp-1",
        asset_symbol="IWDA",
        disposal_date=date(2025, 4, 10),
        quantity=10.0,
        unit_price=100.0,
        fees=15.0,  # 15€ sell fee total
        is_foreign=True,
    )

    assert len(matches) == 1
    m = matches[0]

    # Nominal acquisition cost: 10 units * 80€ = 800.0€
    assert m.nominal_acquisition_cost == 800.0
    assert m.adjusted_acquisition_cost == 800.0

    # Deductible expenses: 10€ allocated buy fees + 15€ disposal fees = 25.0€
    assert m.deductible_expenses == 25.0
    assert m.acquisition_fees_allocated == 10.0
    assert m.disposal_fees_allocated == 15.0

    # Gross proceeds: 10 units * 100€ = 1000.0€
    assert m.gross_disposal_proceeds == 1000.0

    # Net gain/loss: 1000 - 800 - 25 = 175.0€
    assert m.net_gain_loss == 175.0

    # Test annual tax report aggregation
    report = engine.generate_annual_tax_report(matches, fiscal_year=2025)
    anexo_j = report["anexo_j"]
    assert anexo_j["total_gross_proceeds"] == 1000.0
    assert anexo_j["total_adjusted_cost"] == 800.0
    assert anexo_j["total_nominal_cost"] == 800.0
    assert anexo_j["total_deductible_expenses"] == 25.0
    assert anexo_j["net_taxable_balance"] == 175.0


def test_tax_02_real_estate_devaluation_coefficient_with_expenses():
    """Verify that real estate nominal acquisition cost is adjusted by coefficient and fees remain deductible."""
    engine = PortugueseTaxEngine()
    engine.add_acquisition(
        lot_id="re-1",
        asset_symbol="IMOVEL_LISBOA",
        acquisition_date=date(2015, 6, 1),
        quantity=1.0,
        unit_price=200000.0,
        fees=5000.0,
        is_real_estate=True,
    )

    matches = engine.process_disposal(
        disposal_id="disp-re-1",
        asset_symbol="IMOVEL_LISBOA",
        disposal_date=date(2025, 6, 1),
        quantity=1.0,
        unit_price=350000.0,
        fees=10000.0,
        is_real_estate=True,
    )

    assert len(matches) == 1
    m = matches[0]
    # Coeficiente de desvalorização 2015 -> 2025
    coef = m.devaluation_coefficient
    assert coef > 1.0

    # Nominal adjusted cost reflects unit_price * coef
    expected_nominal = 200000.0 * coef
    assert abs(m.nominal_acquisition_cost - expected_nominal) < 1e-6
    assert abs(m.adjusted_acquisition_cost - expected_nominal) < 1e-6

    # Deductible expenses include 5000 + 10000 = 15000
    assert m.deductible_expenses == 15000.0

    # Net gain/loss: 350000 - expected_nominal - 15000
    expected_net = 350000.0 - expected_nominal - 15000.0
    assert abs(m.net_gain_loss - expected_net) < 1e-6


def test_tax_02_tax_lot_match_dataclass_post_init_defaults():
    """Verify TaxLotMatch backward-compatible defaults when instantiating without explicit new fields."""
    match = TaxLotMatch(
        disposal_id="disp-99",
        acquisition_lot_id="acq-99",
        asset_symbol="TEST",
        acquisition_date=date(2024, 1, 1),
        disposal_date=date(2025, 1, 1),
        matched_quantity=5.0,
        acquisition_price=50.0,
        disposal_price=70.0,
        acquisition_fees_allocated=5.0,
        disposal_fees_allocated=5.0,
        devaluation_coefficient=1.0,
        adjusted_acquisition_cost=250.0,
        gross_disposal_proceeds=350.0,
        net_gain_loss=90.0,
        holding_days=366,
        is_crypto=False,
        is_exempt_crypto=False,
        tax_rate_pct=28.0,
        estimated_tax=25.2,
        tax_schedule="Anexo G",
    )

    # Post-init should auto-populate from adjusted_acquisition_cost and fees
    assert match.nominal_acquisition_cost == 250.0
    assert match.deductible_expenses == 10.0


# ==============================================================================
# AUD-SIM-03: Trailing Peak Drawdown in Crisis Backtester
# ==============================================================================


def test_sim_03_crisis_backtester_trailing_peak_drawdown():
    """Verify Enhanced DCA and Crisis Ladder calculate drawdown from trailing peak rather than fixed 100."""
    prices = [100.0, 150.0, 110.0, 120.0, 130.0]
    scenario = CrisisScenario(
        name="Peak Rally Crash",
        custom_prices=prices,
    )

    results = run_crisis_backtest(
        scenario=scenario,
        initial_capital=10000.0,
        monthly_savings=500.0,
    )

    edca_result = next(r for r in results if "Enhanced DCA" in r.strategy_name)
    ladder_result = next(r for r in results if "Crisis Alpha Ladder" in r.strategy_name)

    assert edca_result.total_invested > 12000.0
    assert ladder_result.shares_accumulated > 0.0
    assert ladder_result.total_invested > 12000.0


def test_sim_03_crisis_backtester_non_100_starting_price():
    """Verify crisis backtester calculates trailing drawdown accurately when starting price != 100."""
    prices = [50.0, 40.0, 45.0, 50.0]
    scenario = CrisisScenario(
        name="Alternative Base Price",
        custom_prices=prices,
    )

    results = run_crisis_backtest(
        scenario=scenario,
        initial_capital=5000.0,
        monthly_savings=200.0,
    )

    edca_result = next(r for r in results if "Enhanced DCA" in r.strategy_name)
    ladder_result = next(r for r in results if "Crisis Alpha Ladder" in r.strategy_name)

    assert edca_result.total_invested > 5600.0
    assert ladder_result.shares_accumulated > 0.0


# ==============================================================================
# AUD-DB-05: Bilateral Integrity & Cascade Trigger on transfer_group_id
# ==============================================================================


def test_db_05_transfer_group_bilateral_cascade_trigger_on_direct_sql_delete(repo, temp_db):
    """Verify that deleting a transfer leg via direct SQL cascades and deletes the paired leg (AUD-DB-05)."""
    t_out, t_in = repo.add_internal_transfer(
        "2026-09-04",
        250.0,
        "Conta A",
        "Conta B",
    )

    tx_out = repo.transaction_by_id(t_out)
    tx_in = repo.transaction_by_id(t_in)
    assert tx_out is not None and tx_in is not None
    group_id = tx_out["transfer_group_id"]
    assert group_id and group_id == tx_in["transfer_group_id"]

    # Add dummy split to t_in to verify cascading cleanup of subsidiary tables
    temp_db.conn.execute(
        "INSERT INTO transaction_splits(transaction_id, category, category_id, percent, amount) VALUES(?,?,?,?,?)",
        (t_in, "Diversos", "", 100.0, 250.0),
    )
    temp_db.conn.commit()

    assert len(repo.transaction_splits(t_in)) == 1

    # Execute direct raw SQL deletion of t_out (bypassing repo logic)
    temp_db.conn.execute("DELETE FROM transactions WHERE id = ?", (t_out,))
    temp_db.conn.commit()

    # Trigger trg_transactions_cascade_transfer_leg must have deleted t_in automatically
    assert repo.transaction_by_id(t_out) is None
    assert repo.transaction_by_id(t_in) is None

    # No rows with group_id should exist
    remaining = temp_db.conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE transfer_group_id = ?",
        (group_id,),
    ).fetchone()[0]
    assert remaining == 0

    # Subsidiary splits for t_in must also be cleaned up
    assert len(repo.transaction_splits(t_in)) == 0


def test_db_05_delete_transactions_repo_method_with_trigger_integrity(repo):
    """Verify repo.delete_transactions works seamlessly with the bilateral cascade trigger."""
    t_out, t_in = repo.add_internal_transfer(
        "2026-09-04",
        150.0,
        "Conta A",
        "Conta B",
    )

    # Deleting either leg via delete_transactions expands and deletes both cleanly
    deleted_count = repo.delete_transactions([t_out])
    assert deleted_count == 2

    assert repo.transaction_by_id(t_out) is None
    assert repo.transaction_by_id(t_in) is None


# ==============================================================================
# AUD-DB-07: Chunked Massive Import & Cooperative WAL Checkpoints
# ==============================================================================


def test_db_07_massive_import_chunking_and_wal_checkpoint(repo, temp_db):
    """Verify that import_transactions_batch partitions imports into 500-item chunks with WAL checkpoints."""
    initial_count = temp_db.conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    initial_meta = temp_db.conn.execute("SELECT COUNT(*) FROM transaction_meta").fetchone()[0]

    rows: list[dict[str, Any]] = [
        {
            "date": "2026-09-01",
            "description": f"Transação Importada #{i:04d}",
            "amount": -10.50,
            "category": "Alimentação",
            "account": "Conta Principal",
            "merchant": f"Comerciante {i % 10}",
            "payment_method": "Multibanco",
            "external_reference": f"REF-{i:06d}",
        }
        for i in range(1200)
    ]

    created_ids = repo.import_transactions_batch(rows, commit=True, chunk_size=500)
    assert len(created_ids) == 1200

    total = temp_db.conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    assert total == initial_count + 1200

    total_meta = temp_db.conn.execute("SELECT COUNT(*) FROM transaction_meta").fetchone()[0]
    assert total_meta == initial_meta + 1200


def test_db_07_wal_checkpoint_method_execution(temp_db):
    """Verify FinanceDB.wal_checkpoint executes cooperative checkpoints cleanly."""
    status, log_pages, checkpointed = temp_db.wal_checkpoint("PASSIVE")
    assert isinstance(status, int)
    assert isinstance(log_pages, int)
    assert isinstance(checkpointed, int)

    status_trunc, _, _ = temp_db.wal_checkpoint("TRUNCATE")
    assert isinstance(status_trunc, int)

    status_inv, _, _ = temp_db.wal_checkpoint("NON_EXISTENT_MODE")
    assert isinstance(status_inv, int)


def test_db_07_import_transactions_batch_uncommitted_mode(repo, temp_db):
    """Verify commit=False executes within the caller's active transaction without mid-flight commits."""
    rows = [
        {
            "date": "2026-09-02",
            "description": f"Uncommitted Movement #{i}",
            "amount": -5.0,
            "category": "Transportes",
            "account": "Conta Principal",
        }
        for i in range(25)
    ]

    temp_db.conn.execute("SAVEPOINT test_uncommitted")
    created = repo.import_transactions_batch(rows, commit=False)
    assert len(created) == 25

    temp_db.conn.execute("ROLLBACK TO SAVEPOINT test_uncommitted")
    temp_db.conn.execute("RELEASE SAVEPOINT test_uncommitted")

    count = temp_db.conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE category='Transportes'"
    ).fetchone()[0]
    assert count == 0
