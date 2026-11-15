"""Unit tests for Phase 23 (Batch 15 P2 defects).

Covers:
- BUG-PLANNING-DELETE-001: Confirmation dialogue and safe integer validation in MonthlyGoalsPage.delete_goal.
- TAX-04: 50% inclusion rule under CIRS Art. 43.º n.º 2 forces progressive rates and sets autonomous 28% tax to 0.0.
- TAX-05: Strict FIFO chronological ordering excludes future lots with buy_date > sell_date.
- TAX-06: Nominal acquisition cost separated from acquisition/disposal expenses and fees under CIRS Art. 51.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from unittest.mock import MagicMock, patch

import pytest
from PySide6.QtWidgets import QMessageBox

from motores_matematicos_simulacao.portfolio_stress_and_tax_optimizer import (
    TaxLot,
    process_fifo_sale,
)
from services.tax import PortugueseTaxEngine, TaxLotMatch


# ==============================================================================
# BUG-PLANNING-DELETE-001: MonthlyGoalsPage.delete_goal protections
# ==============================================================================


def test_bug_planning_delete_001_no_selection_or_none_item():
    """delete_goal does nothing when no row is selected or item is None."""
    from ui.pages.planning import MonthlyGoalsPage

    page = MagicMock(spec=MonthlyGoalsPage)
    page.table = MagicMock()
    page.repo = MagicMock()

    # Case 1: currentRow is -1
    page.table.currentRow.return_value = -1
    MonthlyGoalsPage.delete_goal(page)
    page.repo.delete_monthly_goal.assert_not_called()

    # Case 2: row is 0 but item(row, 0) is None
    page.table.currentRow.return_value = 0
    page.table.item.return_value = None
    MonthlyGoalsPage.delete_goal(page)
    page.repo.delete_monthly_goal.assert_not_called()


def test_bug_planning_delete_001_non_numeric_id_safeguard():
    """delete_goal safely aborts if the ID cell is non-numeric or blank."""
    from ui.pages.planning import MonthlyGoalsPage

    page = MagicMock(spec=MonthlyGoalsPage)
    page.table = MagicMock()
    page.repo = MagicMock()

    page.table.currentRow.return_value = 0
    id_item = MagicMock()
    id_item.text.return_value = "ABC_NOT_NUMERIC"
    page.table.item.return_value = id_item

    MonthlyGoalsPage.delete_goal(page)
    page.repo.delete_monthly_goal.assert_not_called()


def test_bug_planning_delete_001_user_cancels_confirmation():
    """delete_goal displays confirmation dialogue; clicking No aborts deletion."""
    from ui.pages.planning import MonthlyGoalsPage

    page = MagicMock(spec=MonthlyGoalsPage)
    page.table = MagicMock()
    page.repo = MagicMock()
    page.table.currentRow.return_value = 0

    id_item = MagicMock()
    id_item.text.return_value = "101"
    name_item = MagicMock()
    name_item.text.return_value = "Férias no Verão"
    page.table.item.side_effect = lambda row, col: id_item if col == 0 else name_item

    with patch("ui.pages.planning.QMessageBox.question", return_value=QMessageBox.No) as mock_q:
        MonthlyGoalsPage.delete_goal(page)
        mock_q.assert_called_once()
        assert "Férias no Verão" in mock_q.call_args[0][2]
        page.repo.delete_monthly_goal.assert_not_called()


def test_bug_planning_delete_001_user_confirms_success():
    """delete_goal deletes goal on confirmation and notifies data changed."""
    from ui.pages.planning import MonthlyGoalsPage

    page = MagicMock(spec=MonthlyGoalsPage)
    page.table = MagicMock()
    page.repo = MagicMock()
    page.app = MagicMock()
    page.table.currentRow.return_value = 0

    id_item = MagicMock()
    id_item.text.return_value = "101"
    name_item = MagicMock()
    name_item.text.return_value = "Férias no Verão"
    page.table.item.side_effect = lambda row, col: id_item if col == 0 else name_item

    with patch("ui.pages.planning.QMessageBox.question", return_value=QMessageBox.Yes):
        MonthlyGoalsPage.delete_goal(page)
        page.repo.delete_monthly_goal.assert_called_once_with(101)
        page.app.notify_data_changed.assert_called_once_with("Meta mensal eliminada.")


def test_bug_planning_delete_001_exception_handling():
    """delete_goal catches database exceptions and presents critical alert."""
    from ui.pages.planning import MonthlyGoalsPage

    page = MagicMock(spec=MonthlyGoalsPage)
    page.table = MagicMock()
    page.repo = MagicMock()
    page.app = MagicMock()
    page.table.currentRow.return_value = 0

    id_item = MagicMock()
    id_item.text.return_value = "101"
    page.table.item.side_effect = lambda row, col: id_item if col == 0 else None
    page.repo.delete_monthly_goal.side_effect = sqlite3.OperationalError("database is locked")

    with patch("ui.pages.planning.QMessageBox.question", return_value=QMessageBox.Yes) as mock_q, \
         patch("ui.pages.planning.QMessageBox.critical") as mock_crit:
        MonthlyGoalsPage.delete_goal(page)
        assert "esta meta" in mock_q.call_args[0][2]
        mock_crit.assert_called_once()
        assert "Erro ao eliminar a meta mensal" in mock_crit.call_args[0][2]


# ==============================================================================
# TAX-04: 50% Inclusion Rule vs 28% Flat Tax in CIRS
# ==============================================================================


def test_tax_04_50pct_inclusion_nullifies_flat_28pct_tax():
    """Under CIRS Art. 43.º n.º 2, 50% inclusion requires englobamento and zeros autonomous 28% tax."""
    engine = PortugueseTaxEngine()
    match = TaxLotMatch(
        disposal_id="disp-001",
        acquisition_lot_id="acq-001",
        asset_symbol="PME_UNLISTED",
        acquisition_date=date(2023, 5, 10),
        disposal_date=date(2025, 6, 15),
        matched_quantity=50.0,
        acquisition_price=100.0,
        disposal_price=200.0,
        acquisition_fees_allocated=0.0,
        disposal_fees_allocated=0.0,
        devaluation_coefficient=1.0,
        adjusted_acquisition_cost=5000.0,
        gross_disposal_proceeds=10000.0,
        net_gain_loss=5000.0,
        holding_days=767,
        is_crypto=False,
        is_exempt_crypto=False,
        tax_rate_pct=28.0,
        estimated_tax=1400.0,
        tax_schedule="Anexo G",
    )

    report = engine.generate_annual_tax_report(
        [match],
        fiscal_year=2025,
        apply_50pct_inclusion=True,
    )

    # 50% of 5000 is 2500
    assert report["final_taxable_base"] == 2500.0
    assert report["global_estimated_tax_28pct"] == 0.0
    assert report["anexo_g"]["estimated_tax_autonomous_28pct"] == 0.0
    assert any("Art. 43.º n.º 2" in note for note in report["legal_notes"])


def test_tax_04_without_50pct_inclusion_calculates_autonomous_tax():
    """Without 50% inclusion, standard 28% autonomous tax is correctly applied."""
    engine = PortugueseTaxEngine()
    match = TaxLotMatch(
        disposal_id="disp-002",
        acquisition_lot_id="acq-002",
        asset_symbol="LISTED_STOCK",
        acquisition_date=date(2024, 1, 15),
        disposal_date=date(2025, 4, 10),
        matched_quantity=10.0,
        acquisition_price=100.0,
        disposal_price=150.0,
        acquisition_fees_allocated=0.0,
        disposal_fees_allocated=0.0,
        devaluation_coefficient=1.0,
        adjusted_acquisition_cost=1000.0,
        gross_disposal_proceeds=1500.0,
        net_gain_loss=500.0,
        holding_days=451,
        is_crypto=False,
        is_exempt_crypto=False,
        tax_rate_pct=28.0,
        estimated_tax=140.0,
        tax_schedule="Anexo G",
    )

    report = engine.generate_annual_tax_report(
        [match],
        fiscal_year=2025,
        apply_50pct_inclusion=False,
    )

    assert report["final_taxable_base"] == 500.0
    assert report["global_estimated_tax_28pct"] == 140.0
    assert report["anexo_g"]["estimated_tax_autonomous_28pct"] == 140.0


# ==============================================================================
# TAX-05: Strict Chronological FIFO Excludes Future Lots
# ==============================================================================


def test_tax_05_process_fifo_sale_filters_future_lots():
    """process_fifo_sale must not consume lots acquired after the sale date."""
    lot_valid = TaxLot(
        id=1,
        asset_symbol="VWCE",
        buy_date=date(2024, 6, 1),
        quantity=10.0,
        buy_price=100.0,
        buy_fees=5.0,
        country_code="IE",
    )
    lot_future = TaxLot(
        id=2,
        asset_symbol="VWCE",
        buy_date=date(2025, 8, 1),
        quantity=20.0,
        buy_price=110.0,
        buy_fees=5.0,
        country_code="IE",
    )

    # Sale on 2025-01-15 (between lot_valid and lot_future)
    updated_lots, realized = process_fifo_sale(
        open_lots=[lot_future, lot_valid],
        sell_date=date(2025, 1, 15),
        sell_quantity=6.0,
        sell_price=120.0,
        sell_fees=4.0,
        is_crypto=False,
    )

    assert len(realized) == 1
    assert realized[0].buy_date == date(2024, 6, 1)
    assert realized[0].quantity == 6.0

    # Lot 1 should have 4 remaining; Lot 2 (future) should remain completely intact (20 units)
    lot1_retained = [lot for lot in updated_lots if lot.id == 1][0]
    assert lot1_retained.quantity == 4.0
    lot2_retained = [lot for lot in updated_lots if lot.id == 2][0]
    assert lot2_retained.quantity == 20.0


def test_tax_05_process_fifo_sale_insufficient_lots_before_sell_date_raises():
    """Selling more than available up to sell_date raises ValueError even if future lots exist."""
    lot_past = TaxLot(
        id=1,
        asset_symbol="VWCE",
        buy_date=date(2024, 6, 1),
        quantity=5.0,
        buy_price=100.0,
        buy_fees=2.0,
    )
    lot_future = TaxLot(
        id=2,
        asset_symbol="VWCE",
        buy_date=date(2025, 8, 1),
        quantity=50.0,
        buy_price=110.0,
        buy_fees=5.0,
    )

    with pytest.raises(ValueError, match="Quantidade insuficiente adquirida até"):
        process_fifo_sale(
            open_lots=[lot_past, lot_future],
            sell_date=date(2025, 1, 15),
            sell_quantity=10.0,  # Only 5 available on 2025-01-15
            sell_price=120.0,
            sell_fees=5.0,
        )


def test_tax_05_portuguese_tax_engine_process_disposal_filters_future_acquisitions():
    """PortugueseTaxEngine.process_disposal only satisfies sales with lots acquired on or before disposal date."""
    engine = PortugueseTaxEngine()
    engine.add_acquisition(
        lot_id="lot-past",
        asset_symbol="AAPL",
        acquisition_date=date(2024, 2, 1),
        quantity=10.0,
        unit_price=150.0,
        fees=5.0,
    )
    engine.add_acquisition(
        lot_id="lot-future",
        asset_symbol="AAPL",
        acquisition_date=date(2025, 9, 1),
        quantity=20.0,
        unit_price=180.0,
        fees=5.0,
    )

    matches = engine.process_disposal(
        disposal_id="disp-1",
        asset_symbol="AAPL",
        disposal_date=date(2025, 5, 1),
        quantity=8.0,
        unit_price=190.0,
        fees=5.0,
    )
    assert len(matches) == 1
    assert matches[0].acquisition_lot_id == "lot-past"
    assert matches[0].matched_quantity == 8.0

    # Remaining in lot-past is 2.0. Trying to sell 5.0 on 2025-05-01 must raise ValueError.
    with pytest.raises(ValueError, match="excede a quantidade adquirida"):
        engine.process_disposal(
            disposal_id="disp-2",
            asset_symbol="AAPL",
            disposal_date=date(2025, 5, 1),
            quantity=5.0,
            unit_price=190.0,
            fees=5.0,
        )


# ==============================================================================
# TAX-06: CIRS Art. 51 Expenses Segregation from Nominal Acquisition Cost
# ==============================================================================


def test_tax_06_model_3_expenses_segregation_in_process_fifo_sale():
    """process_fifo_sale isolates nominal acquisition cost and aggregates fees in expenses (Art. 51.º CIRS)."""
    lot = TaxLot(
        id=1,
        asset_symbol="EQQQ",
        buy_date=date(2024, 3, 1),
        quantity=20.0,
        buy_price=350.0,
        buy_fees=20.0,  # 1€ fee per unit
        country_code="IE",
    )

    _, realized = process_fifo_sale(
        open_lots=[lot],
        sell_date=date(2025, 3, 1),
        sell_quantity=10.0,
        sell_price=400.0,
        sell_fees=15.0,
        is_crypto=False,
    )

    event = realized[0]
    # Nominal acquisition cost: 10 units * 350.0 = 3500.0
    assert event.acquisition_cost == 3500.0
    # Realized gross value: 10 units * 400.0 = 4000.0
    assert event.realized_value == 4000.0
    # Expenses: 10€ allocated buy fees + 15€ sell fees = 25.0€
    assert event.expenses == 25.0
    # Taxable profit: 4000 - 3500 - 25 = 475.0€
    assert event.taxable_profit == 475.0
    # Estimated tax: 475 * 0.28 = 133.0€
    assert event.estimated_tax == 133.0
