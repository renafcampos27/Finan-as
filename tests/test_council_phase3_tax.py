"""A08: realized records must retain evidence instead of inferring sales."""

import csv
import io
import json

import pytest
from PySide6.QtWidgets import QApplication

from database.engine import FinanceDB
from database.repo import Repo
from ui.dialogs import ClosePositionDialog, PortugueseTaxDialog
from services.tax_reporting import (
    DISPOSAL_PREFIX,
    build_realized_report,
    create_disposal_record,
    load_disposal_records,
    realized_report_csv,
    realized_report_html,
    realized_report_rows,
)


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def repo(tmp_path, empty_profile):
    instance = Repo(FinanceDB(tmp_path / "tax.sqlite3"))
    yield instance
    instance.db.conn.close()


def test_open_positions_are_explicitly_hypothetical(qapp, repo):
    repo.add_holding("Open ETF", "ETF", "Manual", 1200, 1000)
    dialog = PortugueseTaxDialog(repo)
    assert "hipot" in dialog._summary_text.casefold()
    assert "realizad" in dialog._summary_text.casefold()
    assert dialog._realized_report["rows"] == []
    dialog.close()


def test_unknown_cost_is_not_a_zero_cost_sale_preview(qapp):
    holding = {"name": "Unknown", "value": 500, "cost_basis": 0, "cost_basis_known": 0}
    dialog = ClosePositionDialog(None, holding)
    assert dialog.get_data()["net_gain"] is None
    assert "desconhecido" in dialog.gain_summary.text().casefold()
    dialog.close()


def _holding(**changes):
    return {
        "id": 1,
        "stable_id": "ast_1",
        "name": "ETF",
        "account": "Broker",
        "currency": "EUR",
        "cost_basis": 1000,
        "cost_basis_known": 1,
        "fees": 10,
        "value": 1500,
        "acquisition_date": "2024-01-05",
        **changes,
    }


def _record(holding=None, **changes):
    return create_disposal_record(
        holding or _holding(),
        sale_date="2026-05-02",
        sale_value_eur=1500,
        sale_fees_eur=5,
        **changes,
    )


def test_unknown_and_known_zero_acquisition_cost_are_distinct():
    unknown = build_realized_report([_record(_holding(cost_basis_known=0))], 2026)
    assert unknown["rows"][0]["net_gain_eur"] is None
    assert not unknown["export_allowed"]
    known_zero = build_realized_report([_record(_holding(cost_basis=0, fees=0))], 2026)
    assert known_zero["rows"][0]["net_gain_eur"] == 1495
    assert known_zero["export_allowed"]


def test_current_fx_cannot_replace_unknown_historical_fx():
    holding = _holding(currency="USD", exchange_rate=1.2)
    missing = build_realized_report([_record(holding)], 2026)
    assert missing["rows"][0]["net_gain_eur"] is None
    assert "histórico" in " ".join(missing["rows"][0]["issues"])
    complete = build_realized_report(
        [_record(holding, acquisition_exchange_rate=0.8)], 2026
    )
    assert complete["rows"][0]["net_gain_eur"] == 687
    assert complete["rows"][0]["acquisition_exchange_rate"] == 0.8


def test_equal_names_retain_individual_acquisition_dates_and_fees():
    first = _record(_holding(id=1, acquisition_date="2023-01-01", fees=3))
    second = _record(_holding(id=2, acquisition_date="2025-02-02", fees=7))
    report = build_realized_report([second, first], 2026)
    assert [r["acquisition_date"] for r in report["rows"]] == [
        "2023-01-01",
        "2025-02-02",
    ]
    assert [r["acquisition_fees_eur"] for r in report["rows"]] == [3, 7]
    assert len(report["rows"]) == 2
    assert build_realized_report([first], 2025)["rows"] == []


def test_csv_and_pdf_share_canonical_rows_and_provenance():
    name = '=HYPERLINK("evil");\n"ETF"'
    report = build_realized_report([_record(_holding(name=name))], 2026)
    rows = realized_report_rows(report)
    csv_rows = list(csv.reader(io.StringIO(realized_report_csv(report)), delimiter=";"))
    assert csv_rows[2][1] == "'" + name
    assert float(csv_rows[2][11]) == rows[0][11] == 485
    html = realized_report_html(report)
    assert "&quot;evil&quot;" in html and "485.0" in html
    assert report["rows"][0]["provenance"] in html
    assert csv_rows[0] == [report["limitation"]]


@pytest.mark.parametrize("builder", [realized_report_csv, realized_report_html])
def test_incomplete_and_private_exports_are_blocked(builder):
    with pytest.raises(ValueError):
        builder(build_realized_report([_record(_holding(cost_basis_known=0))], 2026))
    with pytest.raises(ValueError):
        builder(build_realized_report([_record()], 2026), privacy=True)


def _close(repo, holding_id, **overrides):
    return repo.record_investment_disposal(
        holding_id,
        sale_date="2026-05-02",
        sale_value_eur=1500,
        sale_fees_eur=5,
        account="Conta principal",
        **overrides,
    )


def test_record_survives_holding_removal_and_database_reopen(repo):
    hid = repo.add_holding(
        "ETF", "ETF", "Broker", 1400, 1000, fees=10, acquisition_date="2024-01-05"
    )
    original = dict(repo.holding_by_id(hid))
    result = _close(repo, hid)
    assert repo.holding_by_id(hid) is None
    assert repo.movement_count() == 1
    assert result["holding"] == original
    rows = load_disposal_records(repo)
    assert rows[0]["holding"] == original
    report = build_realized_report(rows, 2026)
    assert report["net_gain_eur"] == 485
    connection = FinanceDB(repo.db.path)
    try:
        assert (
            build_realized_report(load_disposal_records(Repo(connection)), 2026)[
                "net_gain_eur"
            ]
            == 485
        )
    finally:
        connection.conn.close()


def test_sale_undo_restores_all_evidence_and_cash_atomically(repo):
    from services.change_journal import ChangeJournalService

    hid = repo.add_holding(
        "ETF", "ETF", "Broker", 1400, 1000, fees=10, acquisition_date="2024-01-05"
    )
    original = dict(repo.holding_by_id(hid))
    journal = ChangeJournalService(repo)
    _close(repo, hid, journal=journal)
    assert repo.movement_count() == 1
    assert journal.undo_latest()["status"] == "undone"
    assert dict(repo.holding_by_id(hid)) == original
    assert repo.movement_count() == 0
    assert load_disposal_records(repo) == []


def test_sale_failure_rolls_back_receipt_cash_and_holding(repo):
    class FailingJournal:
        def record_investment_disposal_undo(self, *args):
            raise RuntimeError("injected journal failure")

    hid = repo.add_holding("ETF", "ETF", "Broker", 1400, 1000)
    original = dict(repo.holding_by_id(hid))
    with pytest.raises(RuntimeError, match="injected"):
        _close(repo, hid, journal=FailingJournal())
    assert repo.movement_count() == 0
    assert load_disposal_records(repo) == []
    assert dict(repo.holding_by_id(hid)) == original


def test_sale_undo_refuses_subsequently_edited_cash(repo):
    from services.change_journal import ChangeJournalService

    hid = repo.add_holding(
        "ETF", "ETF", "Broker", 1400, 1000, acquisition_date="2024-01-05"
    )
    journal = ChangeJournalService(repo)
    _close(repo, hid, journal=journal)
    repo.db.conn.execute("UPDATE transactions SET description='Later user edit'")
    repo.db.conn.commit()
    with pytest.raises(RuntimeError):
        journal.undo_latest()
    assert repo.movement_count() == 1 and len(load_disposal_records(repo)) == 1
    assert repo.holding_by_id(hid) is None


def test_repeated_close_and_caller_owned_transaction_are_rejected(repo):
    hid = repo.add_holding("ETF", "ETF", "Broker", 1400, 1000)
    _close(repo, hid, create_transaction=False)
    with pytest.raises(ValueError, match="já não existe"):
        _close(repo, hid, create_transaction=False)
    repo.db.conn.execute("BEGIN")
    with pytest.raises(RuntimeError, match="pendente"):
        _close(repo, hid)
    assert repo.db.conn.in_transaction
    repo.db.conn.rollback()


def test_full_sale_cannot_leave_an_active_position_with_fictitious_loss(repo):
    hid = repo.add_holding("ETF", "ETF", "Broker", 1400, 1000)
    with pytest.raises(ValueError, match="arquivar"):
        _close(repo, hid, archive_holding=False)
    assert repo.holding_by_id(hid)["value"] == 1400
    assert load_disposal_records(repo) == [] and repo.movement_count() == 0


def test_free_text_sales_are_not_inferred_and_corrupt_receipts_block_export(repo):
    repo.add_holding("ETF", "ETF", "Broker", 0, 1000, notes="Sold 2026 for 2000 EUR")
    assert load_disposal_records(repo) == []
    record = _record()
    repo.set_setting(DISPOSAL_PREFIX + record["id"], json.dumps(record))
    repo.set_setting(DISPOSAL_PREFIX + "damaged", "not-json")
    report = build_realized_report(load_disposal_records(repo), 2026)
    assert len(report["rows"]) == 1 and report["issues"]
    assert not report["export_allowed"]


def test_performance_does_not_infer_historical_conversion_from_spot():
    from services.investment_performance import portfolio_performance_attribution

    result = portfolio_performance_attribution(
        [_holding(currency="USD", exchange_rate=1.2)]
    )
    assert result["total_value"] == 1800
    assert result["rows"][0]["net_gain"] is None
    assert result["rows"][0]["historical_exchange_rate_known"] is False
    assert result["data_coverage_pct"] == 0


def test_explicit_fiat_proceeds_override_counterparty_inference():
    from services.tax import is_crypto_to_crypto_swap

    assert not is_crypto_to_crypto_swap(
        asset_symbol="BTC",
        is_crypto=True,
        counterparty_symbol="ETH",
        proceeds_in_fiat=True,
    )


def test_disposal_before_acquisition_is_rejected_without_mutation(repo):
    hid = repo.add_holding(
        "ETF", "ETF", "Broker", 1400, 1000, acquisition_date="2027-01-01"
    )
    with pytest.raises(ValueError, match="anteceder"):
        _close(repo, hid)
    assert repo.holding_by_id(hid) is not None
    assert repo.movement_count() == 0 and load_disposal_records(repo) == []


@pytest.mark.parametrize("currency,rate", [("USD", 1), ("EUR", 0.8), (None, None)])
def test_receipt_cannot_reinterpret_foreign_proceeds_as_euros(currency, rate):
    record = _record()
    record["sale_currency"] = currency
    record["sale_exchange_rate"] = rate
    report = build_realized_report([record], 2026)
    assert report["issues"] and not report["export_allowed"]


def test_sale_undo_refuses_conflicting_stable_identity_without_deletion(repo):
    from services.change_journal import ChangeJournalService

    hid = repo.add_holding(
        "Original", "ETF", "Broker", 1400, 1000, acquisition_date="2024-01-05"
    )
    identity = repo.holding_by_id(hid)["stable_id"]
    journal = ChangeJournalService(repo)
    _close(repo, hid, journal=journal)
    other = repo.add_holding("Current unrelated position", "ETF", "Broker", 200, 100)
    repo.db.conn.execute(
        "UPDATE holdings SET stable_id=? WHERE id=?", (identity, other)
    )
    repo.db.conn.commit()
    current = dict(repo.holding_by_id(other))
    with pytest.raises(RuntimeError, match="identidade"):
        journal.undo_latest()
    assert dict(repo.holding_by_id(other)) == current
    assert repo.holding_by_id(hid) is None
    assert repo.movement_count() == 1 and len(load_disposal_records(repo)) == 1


def test_private_tax_scenario_hides_acquisition_duration(qapp, repo, monkeypatch):
    repo.add_holding(
        "Secret BTC", "Cripto", "Broker", 1400, 1000, acquisition_date="2024-01-05"
    )
    dialog = PortugueseTaxDialog(repo)
    monkeypatch.setattr(dialog.theme, "privacy", True)
    dialog._populate_tax_data()
    assert "Secret BTC" not in dialog._summary_text
    assert ">102" not in dialog._summary_text
    assert dialog.table.item(0, 5).text() == "Cenário oculto (modo de privacidade)"
    dialog.close()


def test_realized_dialog_uses_recorded_sales_and_selected_year(qapp, repo):
    hid = repo.add_holding(
        "Recorded ETF", "ETF", "Broker", 1400, 1000, acquisition_date="2024-01-05"
    )
    _close(repo, hid)
    dialog = PortugueseTaxDialog(repo)
    dialog.fiscal_year.setValue(2026)
    assert dialog.table.rowCount() == 0
    assert dialog.realized_table.rowCount() == 1
    assert dialog.csv_btn.isEnabled() and dialog.pdf_btn.isEnabled()
    assert "Recorded ETF" in dialog._summary_text
    dialog.fiscal_year.setValue(2025)
    assert dialog.realized_table.rowCount() == 0
    assert not dialog.csv_btn.isEnabled() and not dialog.pdf_btn.isEnabled()
    dialog.close()


def test_foreign_close_preview_requires_explicit_historical_rate(qapp):
    dialog = ClosePositionDialog(None, _holding(currency="USD", exchange_rate=1.2))
    dialog.sale_value.setValue(1500)
    dialog.sale_fees.setValue(5)
    assert dialog.get_data()["net_gain"] is None
    dialog.acquisition_fx.setValue(0.8)
    dialog.acquisition_fx_known.setChecked(True)
    assert dialog.get_data()["net_gain"] == 687
    assert dialog.get_data()["acquisition_exchange_rate"] == 0.8
    dialog.close()


def test_domain_holding_does_not_turn_unknown_cost_into_profit():
    from domain.models import Holding

    holding = Holding(id=1, name="Unknown", asset_class="ETF", value=500,
                      cost_basis=0, cost_basis_known=False)
    assert holding.gross_gain is None and holding.net_gain is None
    assert holding.return_pct is None
    holding.cost_basis_known = True
    assert holding.gross_gain == 500 and holding.net_gain == 500
    assert holding.return_pct is None  # A known zero cost has no percentage denominator.
