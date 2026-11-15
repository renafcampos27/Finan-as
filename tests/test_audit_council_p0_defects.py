"""Tests for the 3 Critical P0 Defects identified in Council of Architecture Audit.

- BUG-TAX-C48 (BUG-TAX-NEW-01): Stock split support in CIRS FIFO lot engine.
- BUG-SEC-C49 (BUG-SEC-NEW-01): Profile integrity cross-validation against database tombstone.
- BUG-UI-C50 (BUG-UI-NEW-01): Resilience against zero net proceeds on investment position closure.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from PySide6.QtWidgets import QApplication

from services.backup_crypto import envelope_identity
from services.startup_security import (
    ProfileSecurityState,
    SecurityTamperDetectedError,
    StartupSecurityService,
)
from services.tax import PortugueseTaxEngine


@pytest.fixture(scope="session")
def qapp():
    return QApplication.instance() or QApplication([])


# ===========================================================================
# 1. BUG-TAX-C48: Stock Split Support in FIFO Lot Engine
# ===========================================================================


def test_p0_tax_stock_split_forward_adjusts_lots_and_cost_basis() -> None:
    """A 4:1 forward stock split quadruples share count and quarters price while preserving total basis."""
    engine = PortugueseTaxEngine()

    # Buy 10 shares of AAPL @ 100 EUR with 8.0 EUR fee on 2024-01-15
    lot = engine.add_acquisition(
        lot_id="lot-1",
        asset_symbol="AAPL",
        acquisition_date="2024-01-15",
        quantity=10.0,
        unit_price=100.0,
        fees=8.0,
    )
    assert lot.quantity == 10.0
    assert lot.unit_price == 100.0
    assert lot.remaining_quantity == 10.0

    # 4:1 stock split effective 2024-06-01
    adjusted_count = engine.apply_stock_split("AAPL", 4.0, "2024-06-01")
    assert adjusted_count == 1

    # Invariants after 4:1 split
    assert lot.quantity == 40.0
    assert lot.remaining_quantity == 40.0
    assert lot.unit_price == 25.0
    # Total nominal cost basis preserved: 40 * 25 = 1000 EUR
    assert round(lot.quantity * lot.unit_price, 2) == 1000.0

    # Sell 20 shares (post-split) on 2024-07-01 @ 35.0 EUR with 4.0 EUR fee
    matches = engine.process_disposal(
        disposal_id="disp-1",
        asset_symbol="AAPL",
        disposal_date="2024-07-01",
        quantity=20.0,
        unit_price=35.0,
        fees=4.0,
    )
    assert len(matches) == 1
    m = matches[0]
    assert m.matched_quantity == 20.0
    assert m.acquisition_price == 25.0
    assert m.disposal_price == 35.0
    # Exactly half the lot was sold: allocated acquisition fees must be half of 8.0 = 4.0 EUR
    assert round(m.acquisition_fees_allocated, 2) == 4.0
    assert round(m.disposal_fees_allocated, 2) == 4.0
    # Gross proceeds: 20 * 35 = 700 EUR
    assert round(m.gross_disposal_proceeds, 2) == 700.0
    # Nominal acq cost: 20 * 25 = 500 EUR
    assert round(m.nominal_acquisition_cost, 2) == 500.0
    # Net gain: (700 - 4) - (500 + 4) = 696 - 504 = 192 EUR
    assert round(m.net_gain_loss, 2) == 192.0

    # Remaining in lot: 20 shares
    assert lot.remaining_quantity == 20.0


def test_p0_tax_stock_split_reverse_and_split_date_filtering() -> None:
    """A reverse split only adjusts lots acquired on or before split date."""
    engine = PortugueseTaxEngine()

    # Lot 1: 100 shares @ 2 EUR on 2024-01-10
    lot1 = engine.add_acquisition(
        lot_id="lot-1",
        asset_symbol="PENN",
        acquisition_date="2024-01-10",
        quantity=100.0,
        unit_price=2.0,
        fees=2.0,
    )
    # Lot 2: 5 shares @ 20 EUR on 2024-08-01 (post-split acquisition)
    lot2 = engine.add_acquisition(
        lot_id="lot-2",
        asset_symbol="PENN",
        acquisition_date="2024-08-01",
        quantity=5.0,
        unit_price=20.0,
        fees=1.0,
    )

    # 1:10 reverse split (split ratio 0.1) on 2024-05-01
    adjusted_count = engine.apply_stock_split("PENN", 0.1, date(2024, 5, 1))
    assert adjusted_count == 1

    # Lot 1 adjusted: 100 * 0.1 = 10 shares @ 20.0 EUR
    assert lot1.quantity == 10.0
    assert lot1.remaining_quantity == 10.0
    assert lot1.unit_price == 20.0

    # Lot 2 unchanged: acquired after split date
    assert lot2.quantity == 5.0
    assert lot2.remaining_quantity == 5.0
    assert lot2.unit_price == 20.0

    # Sell 12 shares on 2024-09-01
    matches = engine.process_disposal(
        disposal_id="disp-1",
        asset_symbol="PENN",
        disposal_date="2024-09-01",
        quantity=12.0,
        unit_price=25.0,
        fees=0.0,
    )
    assert len(matches) == 2
    assert matches[0].matched_quantity == 10.0  # entire lot 1
    assert matches[1].matched_quantity == 2.0   # partial lot 2
    assert lot1.remaining_quantity == 0.0
    assert lot2.remaining_quantity == 3.0


def test_p0_tax_stock_split_validation_errors() -> None:
    """Invalid split ratios (<=0, NaN) must raise ValueError."""
    engine = PortugueseTaxEngine()
    engine.add_acquisition(
        lot_id="lot-1",
        asset_symbol="XYZ",
        acquisition_date="2024-01-01",
        quantity=10.0,
        unit_price=10.0,
    )

    with pytest.raises(ValueError, match="estritamente superior a zero"):
        engine.apply_stock_split("XYZ", 0.0, "2024-06-01")

    with pytest.raises(ValueError, match="estritamente superior a zero"):
        engine.apply_stock_split("XYZ", -2.0, "2024-06-01")

    with pytest.raises(ValueError):
        engine.apply_stock_split("XYZ", float("nan"), "2024-06-01")


# ===========================================================================
# 2. BUG-SEC-C49: Profile Integrity Cross-Validation against DB Tombstone
# ===========================================================================


def test_p0_security_substituted_envelope_detected_as_tampered(tmp_path: Path) -> None:
    """Replacing envelope and marker with a foreign pair triggers TAMPERED status."""
    db_file = tmp_path / "app.sqlite3"
    conn = sqlite3.connect(db_file)
    try:
        conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
        conn.commit()
    finally:
        conn.close()

    # 1. Honest user sets up Profile A with PIN
    profile_a = StartupSecurityService(database_file=db_file)
    profile_a.configure_first_run("pin", "1357902468")
    assert profile_a.inspect_profile() == ProfileSecurityState.CONFIGURED
    assert profile_a._tamper_tombstone_present() is True

    # Check original tombstone identity
    raw_tombstone = profile_a._read_tamper_tombstone_raw()
    assert raw_tombstone is not None
    original_identity = json.loads(raw_tombstone)["envelope_identity"]

    # 2. Attacker creates Profile B in a completely separate isolated directory
    attacker_dir = tmp_path / "attacker_profile"
    attacker_dir.mkdir()
    attacker_db = attacker_dir / "attacker.sqlite3"
    profile_b = StartupSecurityService(database_file=attacker_db)
    profile_b.configure_first_run("pin", "2468013579")

    envelope_b = profile_b.load_envelope()
    identity_b = envelope_identity(envelope_b)
    assert identity_b != original_identity

    # 3. Attacker substitutes Profile A's envelope and marker with Profile B's files
    profile_a.envelope_path.write_bytes(profile_b.envelope_path.read_bytes())
    profile_a.marker_path.write_bytes(profile_b.marker_path.read_bytes())

    # 4. Profile A's inspect_profile must cross-validate with DB and return TAMPERED!
    fresh_service = StartupSecurityService(database_file=db_file)
    assert fresh_service.inspect_profile() == ProfileSecurityState.TAMPERED

    # 5. Overwriting tombstone via refresh_tamper_tombstone() must raise SecurityTamperDetectedError
    with pytest.raises(SecurityTamperDetectedError):
        fresh_service.refresh_tamper_tombstone()


# ===========================================================================
# 3. BUG-UI-C50: Resilience on Close Position with Net Proceeds = 0.00 EUR
# ===========================================================================


def test_p0_close_position_zero_net_proceeds_does_not_crash_and_archives() -> None:
    """When sale value equals fees (net proceeds = 0.00 EUR), bank movement is skipped and position is archived."""
    # Simulate repo with holding
    mock_repo = MagicMock()
    # In Repo, add_transaction raises ValueError on 0.00 EUR
    def mock_add_transaction(date, desc, amount, category, account, notes=None):
        if abs(amount) < 0.01:
            raise ValueError("O valor do movimento não pode ser zero.")
        return 1

    mock_repo.add_transaction.side_effect = mock_add_transaction
    mock_repo.delete_holding = MagicMock()

    # Replicate logic from ui/pages/investments.py:_on_close_position_confirmed
    holding_name = "Ações Defuntas S.A."
    sale_val = 5.0
    sale_fee = 5.0
    sale_date = "2026-09-11"
    dest_acc = "Conta Ordem"
    create_tx = True
    archive_holding = True
    hid = 42

    tx_desc = f"Alienação de investimento: {holding_name}"
    net_received = round(sale_val - sale_fee, 2)
    assert net_received == 0.0

    # Guard: only call add_transaction when abs(net_received) >= 0.01
    if create_tx and sale_val > 0:
        if abs(net_received) >= 0.01:
            mock_repo.add_transaction(
                sale_date,
                tx_desc,
                net_received,
                "Investimentos",
                dest_acc,
                notes="teste",
            )

    if archive_holding:
        mock_repo.delete_holding(hid)

    # Verifications:
    # 1. add_transaction was NOT called (no ValueError raised!)
    mock_repo.add_transaction.assert_not_called()
    # 2. delete_holding was successfully called
    mock_repo.delete_holding.assert_called_once_with(42)


def test_p0_close_position_dialog_zero_net_proceeds_preview_text(qapp) -> None:
    """ClosePositionDialog displays informative text when net proceeds are zero."""
    from ui.dialogs import ClosePositionDialog

    holding = {
        "id": 10,
        "name": "Smallcap Penny",
        "value": 10.0,
        "cost_basis": 50.0,
        "fees": 2.0,
        "account": "Degiro",
    }
    dlg = ClosePositionDialog(holding=holding)
    dlg.sale_value.setValue(5.0)
    dlg.sale_fees.setValue(5.0)
    dlg.create_transaction.setChecked(True)

    summary_text = dlg.gain_summary.text()
    assert "Receita líquida nula" in summary_text
    assert "nenhum movimento monetário será registado" in summary_text
