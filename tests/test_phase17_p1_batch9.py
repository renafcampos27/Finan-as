#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tests for Phase 17 - Batch 9 P1 Defect Resolutions:
- BUG-MOV-02: Destruição Silenciosa de Divisões (transaction_splits) na Alteração de Categorias em Lote
- BUG-MOV-04: Exclusão em Lote Apaga Pernas de Transferência Não Selecionadas sem Aviso Prévio
- BUG-INV-02: Distorção Patrimonial por Inexistência de Multi-Moeda (USD/GBP) e Paridade Cambial Forçada 1:1
- BUG-INV-04: Exclusão Destrutiva de Ativos sem Integração com Undo/Journal e Desvinculação Órfã de Contas
"""

from __future__ import annotations

import os
import tempfile
from datetime import date

import pytest

from database.engine import FinanceDB
from database.repo import Repo
from services.change_journal import ChangeJournalService
from services.investment_performance import portfolio_performance_attribution


@pytest.fixture
def temp_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db = FinanceDB(path)
    db.init_schema()
    yield db
    try:
        os.remove(path)
    except OSError:
        pass


@pytest.fixture
def repo(temp_db):
    return Repo(temp_db)


@pytest.fixture
def journal(repo, tmp_path):
    attachments = tmp_path / "attachments"
    history = tmp_path / "history"
    attachments.mkdir(parents=True, exist_ok=True)
    history.mkdir(parents=True, exist_ok=True)
    return ChangeJournalService(
        repo,
        attachments_directory=attachments,
        journal_storage=history,
    )


# ==============================================================================
# BUG-INV-02: Multi-Currency Support & Exchange Rate Normalization
# ==============================================================================

def test_holding_multi_currency_schema_and_defaults(temp_db):
    """Verify holding schema has currency and exchange_rate with standard defaults."""
    columns = {
        row["name"]: row
        for row in temp_db.query("PRAGMA table_info(holdings)")
    }
    assert "currency" in columns
    assert "exchange_rate" in columns
    assert "EUR" in str(columns["currency"]["dflt_value"])
    assert "1.0" in str(columns["exchange_rate"]["dflt_value"])


def test_holding_add_and_update_with_currency(repo):
    """Test adding and updating holdings with foreign currencies and exchange rates."""
    # Add USD holding
    hid = repo.add_holding(
        name="Vanguard S&P 500 (VOO)",
        asset_class="ETF",
        account="Interactive Brokers",
        value=1000.0,
        cost_basis=800.0,
        notes="Held in USD",
        currency="USD",
        exchange_rate=0.92,
    )
    holding = repo.holding_by_id(hid)
    assert holding is not None
    assert holding["currency"] == "USD"
    assert abs(holding["exchange_rate"] - 0.92) < 1e-5
    assert holding["value"] == 1000.0
    assert holding["cost_basis"] == 800.0

    # Update holding with GBP
    repo.update_holding(
        hid,
        name="Vanguard FTSE (VUKE)",
        asset_class="ETF",
        account="Interactive Brokers",
        value=2000.0,
        cost_basis=1500.0,
        notes="Swapped to GBP",
        currency="GBP",
        exchange_rate=1.17,
    )
    updated = repo.holding_by_id(hid)
    assert updated["currency"] == "GBP"
    assert abs(updated["exchange_rate"] - 1.17) < 1e-5
    assert updated["value"] == 2000.0


def test_net_worth_multi_currency_consolidation(temp_db, repo):
    """Verify net worth consolidates investment assets into EUR via exchange_rate."""
    temp_db.conn.execute("DELETE FROM holdings")
    temp_db.conn.commit()
    initial_nw = temp_db.net_worth()

    # Asset 1: 1000 EUR @ rate 1.0 = 1000 EUR
    repo.add_holding(
        name="PPR Alves Ribeiro",
        asset_class="PPR",
        account="Banco Invest",
        value=1000.0,
        cost_basis=1000.0,
        currency="EUR",
        exchange_rate=1.0,
    )
    # Asset 2: 1000 USD @ rate 0.92 = 920 EUR
    repo.add_holding(
        name="Apple Inc (AAPL)",
        asset_class="Ações",
        account="Revolut",
        value=1000.0,
        cost_basis=800.0,
        currency="USD",
        exchange_rate=0.92,
    )
    # Asset 3: 500 GBP @ rate 1.20 = 600 EUR
    repo.add_holding(
        name="British American Tobacco",
        asset_class="Ações",
        account="Trading212",
        value=500.0,
        cost_basis=400.0,
        currency="GBP",
        exchange_rate=1.20,
    )

    nw = temp_db.net_worth()
    # Expected incremental investments in EUR = 1000 + 920 + 600 = 2520 EUR
    assert abs((nw - initial_nw) - 2520.0) < 1e-2


def test_portfolio_performance_attribution_multi_currency():
    """Verify portfolio attribution calculates gain/return weighted in EUR while preserving native values."""
    holdings = [
        {
            "id": 1,
            "name": "EUR Asset",
            "value": 100.0,
            "cost_basis": 80.0,
            "fees": 5.0,
            "currency": "EUR",
            "exchange_rate": 1.0,
        },
        {
            "id": 2,
            "name": "USD Asset",
            "value": 200.0,      # In EUR: 200 * 0.50 = 100.0
            "cost_basis": 160.0, # In EUR: 160 * 0.50 = 80.0
            "fees": 10.0,        # In EUR: 10 * 0.50 = 5.0
            "currency": "USD",
            "exchange_rate": 0.50,
            "historical_exchange_rate": 0.50,
        },
    ]
    result = portfolio_performance_attribution(holdings)
    assert result["holding_count"] == 2
    # Consolidated in EUR: Total value = 100 + 100 = 200; Cost = 80 + 80 = 160; Fees = 5 + 5 = 10.
    assert abs(result["total_value"] - 200.0) < 1e-4
    assert abs(result["total_cost_basis"] - 160.0) < 1e-4
    assert abs(result["total_fees"] - 10.0) < 1e-4

    # Check holding items preserve native values and rate
    h2 = [h for h in result["rows"] if h["id"] == 2][0]
    assert h2["currency"] == "USD"
    assert h2["exchange_rate"] == 0.50
    assert h2["native_value"] == 200.0
    assert h2["native_cost_basis"] == 160.0
    assert abs(h2["current_value"] - 100.0) < 1e-4
    assert abs(h2["cost_basis"] - 80.0) < 1e-4


def test_analytics_investment_snapshots_and_state_multi_currency(temp_db, repo):
    """Verify analytics repo normalizes holdings by exchange_rate in investment_state_as_of."""
    temp_db.conn.execute("DELETE FROM holdings")
    temp_db.conn.commit()

    repo.add_holding(
        name="Global ETF",
        asset_class="ETF",
        account="Degiro",
        value=500.0,
        cost_basis=400.0,
        currency="USD",
        exchange_rate=0.80, # 500 * 0.80 = 400 EUR; 400 * 0.80 = 320 EUR
    )
    state = repo.investment_state_as_of(date.today().isoformat())
    assert state["available"] is True
    assert abs(state["total"] - 400.0) < 1e-2
    assert abs(state["invested"] - 320.0) < 1e-2
    assert abs(state["gain"] - 80.0) < 1e-2


# ==============================================================================
# BUG-INV-04: Investment Holding Deletion with ChangeJournal Undo Integration
# ==============================================================================

def test_delete_holding_with_change_journal_and_undo(repo, journal):
    """Verify holding deletion is captured in ChangeJournal and can be restored via undo."""
    hid = repo.add_holding(
        name="Tesla Inc (TSLA)",
        asset_class="Ações",
        account="Revolut",
        value=1500.0,
        cost_basis=1200.0,
        notes="EV growth",
        fees=15.0,
        currency="USD",
        exchange_rate=0.91,
    )
    original_holding = repo.holding_by_id(hid)
    assert original_holding is not None

    # Delete via ChangeJournalService
    journal.delete_holding(hid, label=f"Eliminar investimento · {original_holding['name']}")

    # Holding must no longer exist in database
    assert repo.holding_by_id(hid) is None

    # Verify latest journal entry
    latest = journal.latest_available()
    assert latest is not None
    assert latest["action_type"] == "delete_holding"
    assert "Tesla" in latest["label"]

    # Undo deletion
    undone_entry = journal.undo_latest()
    assert undone_entry is not None
    assert undone_entry["status"] == "undone"

    # Holding must be restored with exact same ID and attributes
    restored = repo.holding_by_id(hid)
    assert restored is not None
    assert restored["id"] == hid
    assert restored["name"] == "Tesla Inc (TSLA)"
    assert restored["currency"] == "USD"
    assert abs(restored["exchange_rate"] - 0.91) < 1e-5
    assert restored["value"] == 1500.0
    assert restored["cost_basis"] == 1200.0
    assert restored["fees"] == 15.0


def test_delete_holding_non_existent_fails(journal):
    """Verify deleting a non-existent holding raises RuntimeError safely."""
    with pytest.raises(RuntimeError, match="não existe"):
        journal.delete_holding(999999, label="Eliminar fantasma")


def test_restore_deleted_holding_id_collision_guard(repo, journal):
    """Verify undo fails safely if another holding has since claimed the same ID."""
    hid = repo.add_holding(
        name="Holding A",
        asset_class="Outro",
        account="Manual",
        value=100.0,
        cost_basis=100.0,
    )
    journal.delete_holding(hid, label="Eliminar Holding A")

    # Manually insert a new holding reusing the exact ID
    repo.db.conn.execute(
        "INSERT INTO holdings (id, name, asset_class, account, value, cost_basis) "
        "VALUES (?, 'Holding Impostor', 'Outro', 'Manual', 50.0, 50.0)",
        (hid,),
    )
    repo.db.conn.commit()

    # Undo must detect ID collision and abort safely
    with pytest.raises(RuntimeError, match="já está em uso"):
        journal.undo_latest()


# ==============================================================================
# BUG-MOV-02: Bulk Category Update & Split Preservation
# ==============================================================================

def test_bulk_category_update_safeguards_splits(repo):
    """Verify bulk category update behavior when splits exist vs when skipped."""
    for cat in ("Supermercado", "Lazer", "Casa", "Alimentação", "Vestuário"):
        repo.category_identity(cat, create=True, kind="expense")
    repo.db.conn.commit()

    # Transaction 1: Simple movement
    t1 = repo.add_transaction(
        "2026-09-01",
        "Compra Simples",
        -20.0,
        "Supermercado",
        "Conta Ordem",
    )
    # Transaction 2: Movement with splits
    t2 = repo.add_transaction(
        "2026-09-02",
        "Hipermercado Misto",
        -50.0,
        "Supermercado",
        "Conta Ordem",
    )
    repo.save_transaction_splits(
        t2,
        [
            ("Alimentação", 60.0),
            ("Vestuário", 40.0),
        ],
    )

    # Inspect splits map
    splits_map = repo.transaction_splits_by_ids([t1, t2])
    assert t1 not in splits_map or len(splits_map[t1]) == 0
    assert len(splits_map[t2]) == 2

    # Scenario A: User chooses to skip transactions with splits
    target_ids = [tid for tid in [t1, t2] if not splits_map.get(tid)]
    assert target_ids == [t1]

    repo.update_transactions_category(target_ids, "Lazer")

    # t1 category changed to Lazer
    tx1 = repo.transaction_by_id(t1)
    assert tx1["category"] == "Lazer"

    # t2 splits and category remain intact
    tx2 = repo.transaction_by_id(t2)
    assert tx2["category"] == "Supermercado"
    splits_t2 = repo.transaction_splits(t2)
    assert len(splits_t2) == 2

    # Scenario B: User chooses to remove splits and update all
    repo.update_transactions_category([t2], "Casa")
    tx2_updated = repo.transaction_by_id(t2)
    assert tx2_updated["category"] == "Casa"
    assert len(repo.transaction_splits(t2)) == 0


# ==============================================================================
# BUG-MOV-04: Bulk Deletion & Transfer Counterparts
# ==============================================================================

def test_bulk_delete_transfer_counterparts_expansion(repo, journal):
    """Verify bulk deletion expands linked transfer counterparts and reports total accurately."""
    # Create transfer between Conta A and Conta B
    t_out, t_in = repo.add_internal_transfer(
        "2026-09-03",
        100.0,
        "Conta A",
        "Conta B",
    )

    # Verify link
    tx_out = repo.transaction_by_id(t_out)
    tx_in = repo.transaction_by_id(t_in)
    assert tx_out["transfer_group_id"] is not None
    assert tx_out["transfer_group_id"] == tx_in["transfer_group_id"]

    # User selected only t_out
    selected_ids = [t_out]
    expanded_ids = repo.expand_transfer_linked_ids(selected_ids)
    assert set(expanded_ids) == {t_out, t_in}
    extra = len(expanded_ids) - len(selected_ids)
    assert extra == 1  # 1 counterpart detected

    # Delete transactions via ChangeJournalService
    removed_count, _ = journal.delete_transactions(
        expanded_ids,
        label=f"Eliminação em lote · {len(expanded_ids)} movimento(s)",
    )
    assert removed_count == 2

    # Both legs must be gone
    assert repo.transaction_by_id(t_out) is None
    assert repo.transaction_by_id(t_in) is None

    # Undo must restore both transfer legs cleanly
    undone = journal.undo_latest()
    assert undone is not None
    assert undone["status"] == "undone"

    restored_out = repo.transaction_by_id(t_out)
    restored_in = repo.transaction_by_id(t_in)
    assert restored_out is not None
    assert restored_in is not None
    assert restored_out["transfer_group_id"] == restored_in["transfer_group_id"]
