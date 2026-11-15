"""Tests for v3.49 Phase 3: Quant Finance, CIRS Tax & Banking Parsers."""

from datetime import date

import pytest

from services.tax import PortugueseTaxEngine
from services.fire import FireService
from services.bank_parsers import OFXParser, QIFParser
from services.imports import ImportService


def test_tax_crypto_365d_exemption_and_short_term_tax():
    engine = PortugueseTaxEngine()

    # Lot 1: Long-term Bitcoin (>365 days held) -> ISENTO Art. 10 CIRS
    engine.add_acquisition(
        lot_id="btc-1",
        asset_symbol="BTC",
        acquisition_date=date(2023, 1, 1),
        quantity=1.0,
        unit_price=20000.0,
        is_crypto=True,
    )
    matches_long = engine.process_disposal(
        disposal_id="disp-1",
        asset_symbol="BTC",
        disposal_date=date(2024, 6, 1),
        quantity=0.5,
        unit_price=60000.0,
        is_crypto=True,
    )
    assert len(matches_long) == 1
    assert matches_long[0].is_exempt_crypto is True
    assert matches_long[0].estimated_tax == 0.0

    # Lot 2: Short-term Ethereum (<365 days held) -> TRIBUTÁVEL 28%
    engine.add_acquisition(
        lot_id="eth-1",
        asset_symbol="ETH",
        acquisition_date=date(2024, 1, 1),
        quantity=2.0,
        unit_price=2000.0,
        is_crypto=True,
    )
    matches_short = engine.process_disposal(
        disposal_id="disp-2",
        asset_symbol="ETH",
        disposal_date=date(2024, 4, 1),
        quantity=1.0,
        unit_price=3000.0,
        is_crypto=True,
    )
    assert len(matches_short) == 1
    assert matches_short[0].is_exempt_crypto is False
    assert matches_short[0].net_gain_loss == 1000.0
    assert round(matches_short[0].estimated_tax, 2) == 280.0


def test_tax_loss_carryforward_and_inclusion():
    engine = PortugueseTaxEngine()
    engine.add_acquisition(
        lot_id="stock-1",
        asset_symbol="AAPL",
        acquisition_date=date(2023, 1, 1),
        quantity=10.0,
        unit_price=150.0,
    )
    matches = engine.process_disposal(
        disposal_id="disp-3",
        asset_symbol="AAPL",
        disposal_date=date(2024, 5, 1),
        quantity=10.0,
        unit_price=250.0,  # +1000 gain
    )
    # Deduct 400€ prior year losses under Art. 55.º n.º 1 al. d) CIRS:
    # the carryforward is only deductible when the taxpayer opts for
    # englobamento (progressive rates). Under the 28% autonomous rate the
    # engine must NOT deduct (TAX-04 behaviour).
    report = engine.generate_annual_tax_report(
        matches,
        fiscal_year=2024,
        prior_year_losses=400.0,
        apply_50pct_inclusion=False,
        opt_for_englobamento=True,
    )
    assert report["prior_year_losses_deducted"] == 400.0
    assert report["global_net_capital_gain_loss"] == 600.0
    assert round(report["global_estimated_tax_28pct"], 2) == 168.0  # 600 * 28%

    # Same disposal without the englobamento option: no deduction allowed.
    report_autonomous = engine.generate_annual_tax_report(
        matches,
        fiscal_year=2024,
        prior_year_losses=400.0,
        apply_50pct_inclusion=False,
    )
    assert report_autonomous["prior_year_losses_deducted"] == 0.0
    assert report_autonomous["remaining_prior_year_losses"] == 400.0
    assert report_autonomous["global_net_capital_gain_loss"] == 1000.0


def _tax_engine_with_dated_lots() -> PortugueseTaxEngine:
    engine = PortugueseTaxEngine()
    engine.add_acquisition(
        lot_id="old-lot",
        asset_symbol="VWCE",
        acquisition_date=date(2024, 1, 10),
        quantity=1.0,
        unit_price=100.0,
    )
    engine.add_acquisition(
        lot_id="future-lot",
        asset_symbol="VWCE",
        acquisition_date=date(2024, 6, 10),
        quantity=1.0,
        unit_price=120.0,
    )
    return engine


@pytest.mark.parametrize("quantity", [0, -1, "abc"])
def test_tax_disposal_rejects_invalid_quantity(quantity):
    engine = _tax_engine_with_dated_lots()

    with pytest.raises(ValueError):
        engine.process_disposal(
            disposal_id="invalid-quantity",
            asset_symbol="VWCE",
            disposal_date=date(2024, 3, 10),
            quantity=quantity,
            unit_price=150.0,
        )


@pytest.mark.parametrize(
    "unit_price",
    [0, -1, "abc", float("nan"), float("inf"), float("-inf")],
)
def test_tax_disposal_rejects_invalid_unit_price(unit_price):
    engine = _tax_engine_with_dated_lots()

    with pytest.raises(ValueError):
        engine.process_disposal(
            disposal_id="invalid-price",
            asset_symbol="VWCE",
            disposal_date=date(2024, 3, 10),
            quantity=1.0,
            unit_price=unit_price,
        )


@pytest.mark.parametrize(
    "fees",
    [-1, "abc", float("nan"), float("inf"), float("-inf")],
)
def test_tax_disposal_rejects_invalid_fees(fees):
    engine = _tax_engine_with_dated_lots()

    with pytest.raises(ValueError):
        engine.process_disposal(
            disposal_id="invalid-fees",
            asset_symbol="VWCE",
            disposal_date=date(2024, 3, 10),
            quantity=1.0,
            unit_price=150.0,
            fees=fees,
        )


def test_tax_disposal_preserves_localized_numeric_inputs():
    engine = _tax_engine_with_dated_lots()

    matches = engine.process_disposal(
        disposal_id="localized-values",
        asset_symbol="VWCE",
        disposal_date=date(2024, 3, 10),
        quantity="1,0",
        unit_price="150,0",
        fees="2,5",
    )

    assert len(matches) == 1
    assert matches[0].matched_quantity == 1.0
    assert matches[0].disposal_price == 150.0
    assert matches[0].disposal_fees_allocated == 2.5


def test_tax_disposal_between_lot_dates_uses_only_eligible_fifo_lots():
    engine = _tax_engine_with_dated_lots()

    matches = engine.process_disposal(
        disposal_id="between-lot-dates",
        asset_symbol="VWCE",
        disposal_date=date(2024, 3, 10),
        quantity=1.0,
        unit_price=150.0,
    )

    assert [match.acquisition_lot_id for match in matches] == ["old-lot"]
    assert all(match.holding_days >= 0 for match in matches)


def test_tax_disposal_rejects_oversell_using_only_lots_acquired_by_sale_date():
    engine = _tax_engine_with_dated_lots()

    with pytest.raises(ValueError):
        engine.process_disposal(
            disposal_id="future-lot-oversell",
            asset_symbol="VWCE",
            disposal_date=date(2024, 3, 10),
            quantity=1.5,
            unit_price=150.0,
        )

    # A failed sale must not consume the one lot that was already eligible.
    matches = engine.process_disposal(
        disposal_id="valid-after-rejected-oversell",
        asset_symbol="VWCE",
        disposal_date=date(2024, 3, 10),
        quantity=1.0,
        unit_price=150.0,
    )
    assert [match.acquisition_lot_id for match in matches] == ["old-lot"]


def test_guyton_klinger_modified_inflation_freeze():
    # Sequence with negative return in year 1 (-15%), then positive in year 2 (+10%)
    returns = [-15.0, 10.0]
    inflation = [5.0, 4.0]
    history = FireService.simulate_guyton_klinger_drawdown(
        initial_capital=500000.0,
        initial_annual_withdrawal=20000.0,
        annual_returns=returns,
        annual_inflation_rates=inflation,
        initial_swr_pct=4.0,
    )
    assert len(history) == 2
    # In year 2, inflation is frozen because year 1 had a negative return!
    assert history[1]["applied_inflation_pct"] == 0.0
    assert "Congelada" in history[1]["inflation_status"]


def test_bank_dialect_detection():
    # Crédito Agrícola
    assert (
        ImportService.detect_bank_dialect(["Data Valor", "CA Online", "Descritivo"])
        == "Crédito Agrícola"
    )
    # WiZink
    assert (
        ImportService.detect_bank_dialect(
            ["Cartão WiZink", "Data Transacção", "Montante"]
        )
        == "WiZink"
    )
    # Trade Republic
    assert (
        ImportService.detect_bank_dialect(["ISIN", "Shares", "Transação de Ações"])
        == "Trade Republic"
    )


def test_ofx_and_qif_parsing_entities():
    ofx_content = """
    <OFX>
      <BANKTRANLIST>
        <STMTTRN>
          <DTPOSTED>20240510
          <TRNAMT>-45.50
          <FITID>TX-98765
          <NAME>P&atilde;o de A&ccedil;&uacute;car &amp; Co.
          <MEMO>Compra &lt;Supermercado&gt;
        </STMTTRN>
      </BANKTRANLIST>
    </OFX>
    """
    stmt = OFXParser.parse(ofx_content)
    assert len(stmt.rows) == 1
    assert "Pão de Açúcar & Co." in stmt.rows[0]["Descrição"]
    assert "<Supermercado>" in stmt.rows[0]["Descrição"]

    qif_content = """
    !Type:Bank
    D10/05/2024
    T-15.00
    PFarm&aacute;cia Central
    LCuidados de Sa&uacute;de
    ^
    """
    qif_stmt = QIFParser.parse(qif_content)
    assert len(qif_stmt.rows) == 1
    assert qif_stmt.rows[0]["Comerciante"] == "Farmácia Central"
    assert qif_stmt.rows[0]["Categoria"] == "Cuidados de Saúde"


def test_qif_parsing_various_amount_formats():
    """Verify QIF parser handles European and international decimal notations accurately."""
    qif_content = """
    !Type:Bank
    D10/05/2024
    T-1,234.56
    PCompra Grande
    ^
    D11/05/2024
    T250.75
    PRecebimento
    ^
    """
    stmt = QIFParser.parse(qif_content)
    assert len(stmt.rows) == 2
    assert stmt.rows[0]["Valor"] == "-1,234.56"
    assert stmt.rows[1]["Valor"] == "250.75"

    parsed_1 = ImportService.parse_amount(stmt.rows[0]["Valor"], "auto")
    parsed_2 = ImportService.parse_amount(stmt.rows[1]["Valor"], "auto")
    assert parsed_1 == -1234.56
    assert parsed_2 == 250.75


def test_import_deduplication_different_accounts(tmp_path):
    """Verify identical external references in different accounts are not discarded."""
    from database.engine import FinanceDB
    from database.repo import Repo

    db_file = tmp_path / "test_dedup.sqlite3"
    db = FinanceDB(db_file)
    repo = Repo(db)
    service = ImportService(repo)

    # Insert transaction in Conta A with reference REF-100
    tx_id = repo.add_transaction(
        "2026-08-01",
        "Transferência recebida",
        500.0,
        "Rendimentos",
        "Conta A",
        "income",
    )
    db.conn.execute(
        "INSERT INTO transaction_meta(transaction_id, external_reference) VALUES(?,?)",
        (tx_id, "REF-100"),
    )
    db.conn.commit()

    # Import transaction in Conta B with the same reference REF-100
    row_b = service.normalize_row(
        {
            "Data": "2026-08-01",
            "Descrição": "Transferência recebida",
            "Valor": "500.00",
            "Conta": "Conta B",
            "Referência": "REF-100",
        },
        {
            "date": "Data",
            "desc": "Descrição",
            "amount": "Valor",
            "account": "Conta",
            "reference": "Referência",
        },
        apply_rules=False,
    )
    index = service._build_duplicate_index([row_b])
    evidence = service._stored_duplicate_evidence_indexed(row_b, index)

    # Must NOT be marked as duplicate in Conta B
    assert evidence["level"] == "none"
    db.close()


def test_goal_transaction_deletion_syncs_saved_amount(tmp_path):
    """Verify deleting a goal contribution transaction adjusts goal.saved_amount."""
    from database.engine import FinanceDB
    from database.repo import Repo

    db_file = tmp_path / "test_goal.sqlite3"
    db = FinanceDB(db_file)
    repo = Repo(db)

    # Create goal with 0 initial
    goal_id = repo.add_goal(
        "Fundo Emergência",
        5000.0,
        0.0,
        0.0,
        "2026-12-31",
    )

    goal = repo.goal_by_id(goal_id)
    assert float(goal["saved_amount"]) == 0.0

    # Adjust goal with deposit of 200€
    tx_id = repo.adjust_goal_with_transaction(
        goal_id,
        200.0,
        deposit=True,
        transaction_date="2026-08-01",
        account="Conta Principal",
    )
    goal = repo.goal_by_id(goal_id)
    assert float(goal["saved_amount"]) == 200.0

    # Delete the transaction
    repo.delete_transaction(tx_id)
    goal = repo.goal_by_id(goal_id)
    # saved_amount must be automatically reversed to 0.0
    assert float(goal["saved_amount"]) == 0.0
    db.close()


def test_backup_verification_does_not_commit_in_flight_transaction(tmp_path):
    """Verify _record_verified_backup does not commit active in-flight transactions."""
    from database.engine import FinanceDB
    from database.repo import Repo
    from services.backup import BackupService

    db_file = tmp_path / "test_bkp.sqlite3"
    db = FinanceDB(db_file)
    repo = Repo(db)
    backup_service = BackupService(repo)

    # Create a dummy backup file
    bkp_file = tmp_path / "test_backup.zip"
    bkp_file.write_bytes(b"PK\x03\x04test")

    # Start an uncommitted transaction on repo.db.conn
    repo.db.conn.execute("BEGIN TRANSACTION")
    repo.db.conn.execute(
        "INSERT INTO categories(name,kind,colour,budget,stable_id,identity_key) VALUES(?,?,?,?,?,?)",
        ("TempUncommitted", "expense", "#FF0000", 100, "cat_temp", "tempuncommitted"),
    )

    # Record verified backup while transaction is active
    backup_service._record_verified_backup(bkp_file)

    # Roll back the transaction
    repo.db.conn.rollback()

    # The uncommitted category must NOT be present!
    found = repo.db.conn.execute(
        "SELECT COUNT(*) FROM categories WHERE name='TempUncommitted'"
    ).fetchone()[0]
    assert found == 0
    db.close()
