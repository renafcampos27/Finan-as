"""Automated verification suite for Council Audit Round 7 - Batch 40 defects.

Defects covered:
1. BUG-R7-TAX-01 [P0]: Global CIRS loss netting across Anexo G and Anexo J under autonomous regime.
2. BUG-R7-TAX-02 [P1]: PortugueseTaxDialog offshore crypto evaluation applying 35% aggravated rate without exemption.
3. BUG-R7-BNK-01 [P1]: QIFParser skips !Account header attributes and delimiters until !Type: transaction block.
4. BUG-R7-TXN-01 [P1], superseded by A11: caller batches require an explicit commit; nested helpers preserve ownership.
5. BUG-R7-PLN-01 [P2]: _planning_write captures owns_tx before savepoint and preserves ambient transaction boundaries.
6. BUG-R7-JRN-01 [P1]: ChangeJournalService prune only deletes attachment folders on owned successful commit; compact_snapshots guards commit with owns_tx.
7. BUG-R7-QUANT-01 [P2]: expected_shortfall_cvar epsilon guard prevents float overshoot on cutoff_count ceiling.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from database.engine import FinanceDB
from database.repo import Repo
from services.bank_parsers import QIFParser
from services.change_journal import ChangeJournalService
from services.finance_math import expected_shortfall_cvar
from services.tax import PortugueseTaxEngine
from ui.dialogs import PortugueseTaxDialog


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.delenv("FINANCAS_PESSOAIS_DEMO_DATA", raising=False)
    db = FinanceDB(tmp_path / "batch40_test.sqlite3")
    r = Repo(db)
    yield r
    try:
        db.conn.close()
    except Exception:
        pass


@pytest.fixture
def journal(repo, tmp_path):
    return ChangeJournalService(
        repo,
        attachments_directory=tmp_path / "attachments",
        journal_storage=tmp_path / "history",
    )


# ==============================================================================
# 1. BUG-R7-TAX-01 [P0]: Global CIRS Loss Netting Across Anexo G and Anexo J
# ==============================================================================

def test_bug_r7_tax_01_global_cirs_loss_netting():
    """BUG-R7-TAX-01: CIRS Art. 43.º n.º 1 requires netting capital gains against losses

    globally across Anexo G and Anexo J under the autonomous regime.
    """
    engine = PortugueseTaxEngine()

    # Case A: Gain 1,000 in Anexo G (PT), Loss 1,000 in Anexo J (US).
    # Net raw is 0.0 -> total estimated tax must be 0.0 EUR (not 280.0 EUR).
    engine.add_acquisition(
        lot_id="g_lot",
        asset_symbol="EDP.LS",
        acquisition_date=date(2025, 1, 10),
        quantity=100.0,
        unit_price=10.0,
        is_foreign=False,
    )
    matches_g = engine.process_disposal(
        disposal_id="dg1",
        asset_symbol="EDP.LS",
        disposal_date=date(2025, 6, 10),
        quantity=100.0,
        unit_price=20.0,  # +1,000 EUR gain
        is_foreign=False,
    )

    engine.add_acquisition(
        lot_id="j_lot",
        asset_symbol="AAPL",
        acquisition_date=date(2025, 2, 10),
        quantity=10.0,
        unit_price=200.0,
        is_foreign=True,
        country_code="US",
    )
    matches_j = engine.process_disposal(
        disposal_id="dj1",
        asset_symbol="AAPL",
        disposal_date=date(2025, 7, 10),
        quantity=10.0,
        unit_price=100.0,  # -1,000 EUR loss
        is_foreign=True,
        country_code="US",
    )

    report_zero = engine.generate_annual_tax_report(
        matches_g + matches_j,
        2025,
        opt_for_englobamento=False,
    )
    assert report_zero["global_net_capital_gain_loss_raw"] == pytest.approx(0.0)
    assert report_zero["global_estimated_tax_28pct"] == pytest.approx(0.0)
    assert report_zero["total_tax_autonomous"] == pytest.approx(0.0)

    # Case B: Overall net is negative (-500 EUR). Total tax must be 0.0.
    engine.add_acquisition(
        lot_id="j_lot2",
        asset_symbol="MSFT",
        acquisition_date=date(2025, 1, 1),
        quantity=10.0,
        unit_price=200.0,
        is_foreign=True,
        country_code="US",
    )
    matches_j2 = engine.process_disposal(
        disposal_id="dj2",
        asset_symbol="MSFT",
        disposal_date=date(2025, 9, 1),
        quantity=10.0,
        unit_price=50.0,  # -1,500 EUR loss
        is_foreign=True,
        country_code="US",
    )
    report_neg = engine.generate_annual_tax_report(
        matches_g + matches_j2,
        2025,
        opt_for_englobamento=False,
    )
    assert report_neg["global_net_capital_gain_loss_raw"] == pytest.approx(-500.0)
    assert report_neg["global_estimated_tax_28pct"] == pytest.approx(0.0)
    assert report_neg["total_tax_autonomous"] == pytest.approx(0.0)

    # Case C: Positive net with mixed rates (Anexo G 28% and Offshore Anexo J 35%):
    # Gain 1,000 at 28% (EDP) + Gain 1,000 at 35% (Cayman) + Loss 500 at 28% (Galp)
    # Total gains = 2,000. Weighted rate = (1000*0.28 + 1000*0.35) / 2000 = 0.315 (31.5%).
    # Global net = 1,500. Tax = 1,500 * 0.315 = 472.50 EUR.
    engine_mix = PortugueseTaxEngine()
    engine_mix.add_acquisition(
        lot_id="edp", asset_symbol="EDP", acquisition_date=date(2025, 1, 1),
        quantity=100.0, unit_price=10.0, is_foreign=False,
    )
    m_edp = engine_mix.process_disposal(
        disposal_id="dm1",
        asset_symbol="EDP", disposal_date=date(2025, 5, 1),
        quantity=100.0, unit_price=20.0, is_foreign=False,  # +1000 at 28%
    )
    engine_mix.add_acquisition(
        lot_id="ky_fund", asset_symbol="KYF", acquisition_date=date(2025, 1, 1),
        quantity=10.0, unit_price=100.0, is_foreign=True, country_code="KY",
    )
    m_ky = engine_mix.process_disposal(
        disposal_id="dm2",
        asset_symbol="KYF", disposal_date=date(2025, 5, 2),
        quantity=10.0, unit_price=200.0, is_foreign=True, country_code="KY",  # +1000 at 35%
    )
    engine_mix.add_acquisition(
        lot_id="galp", asset_symbol="GALP", acquisition_date=date(2025, 1, 1),
        quantity=50.0, unit_price=20.0, is_foreign=False,
    )
    m_galp = engine_mix.process_disposal(
        disposal_id="dm3",
        asset_symbol="GALP", disposal_date=date(2025, 5, 3),
        quantity=50.0, unit_price=10.0, is_foreign=False,  # -500 at 28%
    )
    report_mix = engine_mix.generate_annual_tax_report(
        m_edp + m_ky + m_galp,
        2025,
        opt_for_englobamento=False,
    )
    assert report_mix["global_net_capital_gain_loss_raw"] == pytest.approx(1500.0)
    assert report_mix["global_estimated_tax_28pct"] == pytest.approx(472.50)
    assert report_mix["total_tax_autonomous"] == pytest.approx(472.50)

    # Case D: Domestic loss (-1,000 EUR) offsets Offshore gain (+2,000 EUR @ 35%).
    # All taxable gains are offshore (35%), so global weighted rate is exactly 35%.
    # Net = 1,000 EUR -> total tax = 350.00 EUR.
    engine_off = PortugueseTaxEngine()
    engine_off.add_acquisition(
        lot_id="galp_loss", asset_symbol="GALP", acquisition_date=date(2025, 1, 1),
        quantity=100.0, unit_price=20.0, is_foreign=False,
    )
    m_galp_loss = engine_off.process_disposal(
        disposal_id="d_gl", asset_symbol="GALP", disposal_date=date(2025, 4, 1),
        quantity=100.0, unit_price=10.0, is_foreign=False,  # -1,000 EUR loss
    )
    engine_off.add_acquisition(
        lot_id="ky_gain", asset_symbol="KY_STOCK", acquisition_date=date(2025, 1, 1),
        quantity=10.0, unit_price=100.0, is_foreign=True, country_code="KY",
    )
    m_ky_gain = engine_off.process_disposal(
        disposal_id="d_kg", asset_symbol="KY_STOCK", disposal_date=date(2025, 4, 2),
        quantity=10.0, unit_price=300.0, is_foreign=True, country_code="KY",  # +2,000 EUR gain @ 35%
    )
    report_off = engine_off.generate_annual_tax_report(
        m_galp_loss + m_ky_gain, 2025, opt_for_englobamento=False
    )
    assert report_off["global_net_capital_gain_loss_raw"] == pytest.approx(1000.0)
    assert report_off["global_estimated_tax_28pct"] == pytest.approx(350.00)
    assert report_off["total_tax_autonomous"] == pytest.approx(350.00)

    # Case E: Englobamento interaction with prior year loss carryforward (CIRS Art. 55.º).
    # Gain 1,000 EUR in 2025, with 400 EUR eligible loss carryforward from 2023.
    # Autonomous regime ignores prior losses: tax = 280.00 EUR.
    # Englobamento uses 400 EUR loss: base = 600.00 EUR, total_tax_autonomous = 0.0.
    engine_eng = PortugueseTaxEngine()
    engine_eng.add_acquisition(
        lot_id="edp_e", asset_symbol="EDP", acquisition_date=date(2025, 1, 1),
        quantity=100.0, unit_price=10.0, is_foreign=False,
    )
    m_edp_e = engine_eng.process_disposal(
        disposal_id="d_ee", asset_symbol="EDP", disposal_date=date(2025, 6, 1),
        quantity=100.0, unit_price=20.0, is_foreign=False,  # +1000 EUR gain
    )
    rep_eng_off = engine_eng.generate_annual_tax_report(
        m_edp_e, 2025, opt_for_englobamento=False,
        prior_year_losses=400.0, prior_year_losses_by_year={2023: 400.0},
    )
    assert rep_eng_off["global_net_capital_gain_loss_raw"] == pytest.approx(1000.0)
    assert rep_eng_off["prior_year_losses_deducted"] == pytest.approx(0.0)
    assert rep_eng_off["final_taxable_base"] == pytest.approx(1000.0)
    assert rep_eng_off["total_tax_autonomous"] == pytest.approx(280.00)

    rep_eng_on = engine_eng.generate_annual_tax_report(
        m_edp_e, 2025, opt_for_englobamento=True,
        prior_year_losses=400.0, prior_year_losses_by_year={2023: 400.0},
    )
    assert rep_eng_on["global_net_capital_gain_loss_raw"] == pytest.approx(1000.0)
    assert rep_eng_on["prior_year_losses_deducted"] == pytest.approx(400.0)
    assert rep_eng_on["final_taxable_base"] == pytest.approx(600.0)
    assert rep_eng_on["total_tax_autonomous"] == 0.0
    assert rep_eng_on["global_estimated_tax_28pct"] == pytest.approx(168.00)


# ==============================================================================
# 2. BUG-R7-TAX-02 [P1]: Offshore Crypto Aggravated Rate (35%) Without Exemption
# ==============================================================================

def test_bug_r7_tax_02_portuguese_tax_dialog_offshore_crypto(qapp, repo):
    """BUG-R7-TAX-02: PortugueseTaxDialog._populate_tax_data() evaluates is_offshore

    before crypto branching; offshore crypto assets are taxed at 35% without >365d exemption.
    """
    from utils.helpers import eur

    # 500 days ago (>365d), held in Cayman Islands (KY - offshore)
    old_date = (date.today() - timedelta(days=500)).isoformat()
    recent_date = (date.today() - timedelta(days=30)).isoformat()
    repo.db.conn.execute(
        """
        INSERT INTO holdings (id, name, asset_class, value, cost_basis, cost_basis_known, acquisition_date, jurisdiction)
        VALUES
            (101, 'Bitcoin Offshore', 'Cripto', 5000.0, 2000.0, 1, ?, 'KY'),
            (102, 'Ethereum Domestic', 'Cripto', 3000.0, 1000.0, 1, ?, 'PT'),
            (103, 'Cardano Recent', 'Cripto', 1500.0, 1000.0, 1, ?, 'PT')
        """,
        (old_date, old_date, recent_date),
    )
    repo.db.conn.commit()

    dialog = PortugueseTaxDialog(repo)
    assert dialog.table.rowCount() == 3

    rows_by_name = {}
    for r in range(dialog.table.rowCount()):
        name = dialog.table.item(r, 0).text()
        rows_by_name[name] = {
            "gain": dialog.table.item(r, 4).text(),
            "regime": dialog.table.item(r, 5).text(),
            "tax": dialog.table.item(r, 6).text(),
        }

    # Bitcoin Offshore -> Gain +3,000.00 EUR, 35% offshore, Tax 1,050.00 EUR
    btc = rows_by_name["Bitcoin Offshore"]
    assert eur(3000.0) in btc["gain"]
    assert "Offshore" in btc["regime"]
    assert "35%" in btc["regime"]
    assert "Isento" not in btc["regime"]
    assert eur(1050.0) in btc["tax"]

    # Ethereum Domestic (>365d) -> Gain +2,000.00 EUR, Exempt, Tax 0.00 EUR
    eth = rows_by_name["Ethereum Domestic"]
    assert eur(2000.0) in eth["gain"]
    assert "Isento" in eth["regime"]
    assert "Art. 10.º CIRS" in eth["regime"]
    assert "Isento (€0,00)" in eth["tax"]

    # Cardano Recent (<365d) -> Gain +500.00 EUR, 28% tax, Tax 140.00 EUR
    ada = rows_by_name["Cardano Recent"]
    assert eur(500.0) in ada["gain"]
    assert "<365d" in ada["regime"]
    assert "28%" in ada["regime"]
    assert eur(140.0) in ada["tax"]

    dialog.close()


# ==============================================================================
# 3. BUG-R7-BNK-01 [P1]: QIFParser Skips !Account Header Block
# ==============================================================================

def test_bug_r7_bnk_01_qif_parser_skips_account_header():
    """BUG-R7-BNK-01: QIFParser skips !Account header attributes and ^ delimiters

    until the !Type: transaction block begins.
    """
    qif_content = """!Account
NConta Ordem Principal
TBank
DConta Bancaria Principal
^
!Type:Bank
D12/01/2026
T-18.90
PNetflix
MSubscrição Mensal
LSubscrições
^
D15/01/2026
T1500.00
PSalário Empresa
MOrdenado Janeiro
LSalário
^
"""
    parsed = QIFParser.parse(qif_content)
    # Must contain exactly 2 transaction rows; account header must not leak as a row
    assert len(parsed.rows) == 2

    assert parsed.rows[0]["Descrição"] == "Netflix · Subscrição Mensal"
    assert parsed.rows[0]["Valor"] == "-18.90"
    assert parsed.rows[0]["Categoria"] == "Subscrições"

    assert parsed.rows[1]["Descrição"] == "Salário Empresa · Ordenado Janeiro"
    assert parsed.rows[1]["Valor"] == "1500.00"
    assert parsed.rows[1]["Categoria"] == "Salário"

    # Case B: Multiple account sections interspersed with !Type: sections,
    # including an account header without trailing ^ before !Type:Bank
    multi_account_qif = """!Account
NChecking
TBank
^
!Type:Bank
D01/01/2026
T-50.00
PSupermarket
^
!Account
NSavings
TBank
!Type:Bank
D02/01/2026
T100.00
PBonus
^
"""
    parsed_multi = QIFParser.parse(multi_account_qif)
    assert len(parsed_multi.rows) == 2
    assert parsed_multi.rows[0]["Descrição"] == "Supermarket"
    assert parsed_multi.rows[0]["Valor"] == "-50.00"
    assert parsed_multi.rows[1]["Descrição"] == "Bonus"
    assert parsed_multi.rows[1]["Valor"] == "100.00"


# ==============================================================================
# 4. BUG-R7-TXN-01 [P1] / A11: Explicit Caller-Owned Batch Completion
# ==============================================================================

def test_bug_r7_txn_01_batches_require_explicit_commit_after_commit_false(repo):
    """A11: nested commit=True never flushes a caller's pending batch."""
    # 1. add_transaction batch
    tx1 = repo.add_transaction(amount=-10.0, description="TX1", commit=False)
    assert repo.db.conn.in_transaction
    tx2 = repo.add_transaction(amount=-20.0, description="TX2", commit=True)
    assert repo.db.conn.in_transaction
    repo.commit()
    assert not repo.db.conn.in_transaction

    assert repo.transaction_by_id(tx1) is not None
    assert repo.transaction_by_id(tx2) is not None

    # 2. add_transaction_with_meta batch
    tx3 = repo.add_transaction_with_meta(
        "2026-01-01", "Meta1", -30.0, "Geral", "Conta", commit=False
    )
    assert repo.db.conn.in_transaction
    tx4 = repo.add_transaction_with_meta(
        "2026-01-02", "Meta2", -40.0, "Geral", "Conta", commit=True
    )
    assert repo.db.conn.in_transaction
    repo.commit()
    assert not repo.db.conn.in_transaction

    assert repo.transaction_by_id(tx3) is not None
    assert repo.transaction_by_id(tx4) is not None

    # 3. add_internal_transfer batch
    legs1 = repo.add_internal_transfer(
        amount=50.0, source_account="Conta A", destination_account="Conta B", commit=False
    )
    assert repo.db.conn.in_transaction
    legs2 = repo.add_internal_transfer(
        amount=60.0, source_account="Conta A", destination_account="Conta B", commit=True
    )
    assert repo.db.conn.in_transaction
    repo.commit()
    assert not repo.db.conn.in_transaction

    assert repo.transaction_by_id(legs1[0]) is not None
    assert repo.transaction_by_id(legs2[0]) is not None

    # 4. update_transaction_with_meta batch
    tx5 = repo.add_transaction(amount=-100.0, description="TX5", commit=True)
    repo.add_transaction(amount=-5.0, description="Pending", commit=False)
    assert repo.db.conn.in_transaction
    repo.update_transaction_with_meta(tx5, {"amount": -999.0}, commit=True)
    assert repo.db.conn.in_transaction
    repo.commit()
    assert not repo.db.conn.in_transaction
    assert repo.transaction_by_id(tx5)["amount"] == -999.0

    # 5. update_internal_transfer batch
    tx_pending = repo.add_transaction(amount=-15.0, description="TX_Pending", commit=False)
    assert repo.db.conn.in_transaction
    tx_pair = repo.transaction_by_id(legs2[0])
    repo.update_internal_transfer(
        transfer_group_id=tx_pair["transfer_group_id"],
        d="2026-01-05",
        amount=75.0,
        source_account="Conta A",
        destination_account="Conta B",
        commit=True,
    )
    assert repo.db.conn.in_transaction
    repo.commit()
    assert not repo.db.conn.in_transaction
    assert repo.transaction_by_id(tx_pending) is not None


# ==============================================================================
# 5. BUG-R7-PLN-01 [P2]: _planning_write Preserves Ambient Transaction Boundaries
# ==============================================================================

def test_bug_r7_pln_01_planning_write_preserves_ambient_transaction(repo):
    """BUG-R7-PLN-01: _planning_write() captures owns_tx before savepoint;

    does not prematurely commit or roll back caller's ambient transaction.
    """
    # Caller opens transaction
    repo.db.conn.execute("BEGIN IMMEDIATE")
    assert repo.db.conn.in_transaction

    # Write inside planning
    with repo._planning_write("test_planning_sp") as conn:
        conn.execute(
            "INSERT INTO liabilities (name, balance, monthly_payment) VALUES ('Empréstimo', 5000.0, 150.0)"
        )

    # Ambient transaction MUST remain open!
    assert repo.db.conn.in_transaction

    # Caller rolls back
    repo.db.conn.rollback()
    assert not repo.db.conn.in_transaction
    assert len(repo.q("SELECT * FROM liabilities WHERE name='Empréstimo'")) == 0

    # Without ambient transaction, _planning_write owns the transaction and commits
    with repo._planning_write("test_planning_standalone") as conn:
        conn.execute(
            "INSERT INTO liabilities (name, balance, monthly_payment) VALUES ('Auto', 8000.0, 200.0)"
        )
    assert not repo.db.conn.in_transaction
    assert len(repo.q("SELECT * FROM liabilities WHERE name='Auto'")) == 1


# ==============================================================================
# 6. BUG-R7-JRN-01 [P1]: ChangeJournal Prune and Compact Ambient Transaction Safety
# ==============================================================================

def test_bug_r7_jrn_01_prune_and_compact_ambient_transaction_safety(journal, repo):
    """BUG-R7-JRN-01: ChangeJournalService.prune() only removes attachment folders

    when owns_tx is True and commit succeeded. compact_snapshots() respects ambient transaction.
    """
    # Populate 15 change_journal records with status='undone'
    for i in range(1, 16):
        repo.db.conn.execute(
            """
            INSERT INTO change_journal (id, action_type, label, status, payload_json)
            VALUES (?, 'delete', 'Test', 'undone', ?)
            """,
            (i, json.dumps({"test": "data" * 200})),
        )
        folder = journal.journal_storage / str(i)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "file.txt").write_text("sample attachment", encoding="utf-8")
    repo.db.conn.commit()

    # Case A: prune during active ambient transaction must NOT delete disk folders
    repo.db.conn.execute("BEGIN IMMEDIATE")
    assert repo.db.conn.in_transaction
    journal.prune(keep=10)
    # Folder 1 (an obsolete entry candidate) must NOT be deleted while tx is uncommitted
    folder1 = journal.journal_storage / "1"
    assert folder1.exists()

    repo.db.conn.rollback()
    assert folder1.exists()

    # Case B: prune without ambient transaction deletes obsolete folders upon commit
    journal.prune(keep=10)
    assert not folder1.exists()

    # Case C: compact_snapshots during active ambient transaction does not commit ambient tx
    repo.db.conn.execute("BEGIN IMMEDIATE")
    assert repo.db.conn.in_transaction
    journal.compact_snapshots()
    assert repo.db.conn.in_transaction
    repo.db.conn.rollback()


# ==============================================================================
# 7. BUG-R7-QUANT-01 [P2]: CVaR Cutoff Float Precision Epsilon Guard
# ==============================================================================

def test_bug_r7_quant_01_expected_shortfall_cvar_epsilon():
    """BUG-R7-QUANT-01: expected_shortfall_cvar() uses - 1e-9 epsilon guard

    to prevent (1.0 - 0.95) * 20 = 1.0000000000000009 from ceiling to 2 instead of 1.
    """
    # Exactly 20 returns: one severe loss (-10.0%) and 19 zero returns (0.0%).
    # At 95% confidence on n=20: (1 - 0.95) * 20 = 1 item in the tail.
    # Cutoff count must be 1 -> tail is [-10.0] -> CVaR = 10.0.
    # (Without epsilon guard, float overshoot makes cutoff_count = 2 -> tail [-10.0, 0.0] -> CVaR = 5.0).
    returns = [-10.0] + [0.0] * 19
    cvar = expected_shortfall_cvar(returns, confidence=0.95)
    assert cvar == pytest.approx(10.0)

    # 100 returns at 95% confidence: exactly 5 items in the tail.
    # Worst 5 returns: -20, -18, -16, -14, -12 (average = -16.0 -> CVaR = 16.0).
    returns_100 = [-20.0, -18.0, -16.0, -14.0, -12.0] + [1.0] * 95
    cvar_100 = expected_shortfall_cvar(returns_100, confidence=0.95)
    assert cvar_100 == pytest.approx(16.0)

    # 40 returns at 95% confidence: (1 - 0.95) * 40 = 2 items in the tail.
    # Floating point overshoot: (1.0 - 0.95) * 40 = 2.0000000000000018.
    # Without epsilon, math.ceil yields 3 items; with epsilon, yields exactly 2 items.
    # Worst 2: -20, -10 (average = -15.0 -> CVaR = 15.0).
    returns_40 = [-20.0, -10.0] + [0.0] * 38
    cvar_40 = expected_shortfall_cvar(returns_40, confidence=0.95)
    assert cvar_40 == pytest.approx(15.0)

    # 100 returns at 99% confidence: (1 - 0.99) * 100 = 1 item in the tail.
    # Floating point: (1.0 - 0.99) * 100 = 1.0000000000000009.
    # Epsilon ensures cutoff_count is 1 -> tail [-30.0] -> CVaR = 30.0.
    returns_99 = [-30.0] + [0.0] * 99
    cvar_99 = expected_shortfall_cvar(returns_99, confidence=0.99)
    assert cvar_99 == pytest.approx(30.0)
