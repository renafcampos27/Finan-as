from __future__ import annotations

import pytest

from services.bank_parsers import OFXParser
from services.fire import FirePlanningEngine
from services.imports import ImportService
from services.investment_performance import portfolio_performance_attribution


def test_investment_return_without_cost_basis_does_not_return_fictional_100_pct() -> (
    None
):
    """Ensure holdings with missing or 0 cost basis return None rather than 100% of fictional return."""
    result_unknown = portfolio_performance_attribution(
        [
            {
                "id": 1,
                "name": "Imóvel Herdado / Ativo sem Custo",
                "asset_class": "Imobiliário",
                "value": 150_000,
                "cost_basis": 0,
                "cost_basis_known": 0,
                "fees": 0,
            }
        ]
    )
    assert result_unknown["total_value"] == 150_000
    assert result_unknown["total_cost_basis"] == 0
    assert result_unknown["gross_return_pct"] is None
    assert result_unknown["net_return_pct"] is None
    assert result_unknown["rows"][0]["performance_available"] is False
    assert result_unknown["rows"][0]["gross_return_pct"] is None
    assert result_unknown["rows"][0]["net_return_pct"] is None

    result_mixed = portfolio_performance_attribution(
        [
            {
                "id": 1,
                "name": "Ativo sem Custo",
                "asset_class": "Outro",
                "value": 5_000,
                "cost_basis": 0,
                "cost_basis_known": 0,
                "fees": 0,
            },
            {
                "id": 2,
                "name": "VWCE ETF",
                "asset_class": "ETF",
                "value": 12_000,
                "cost_basis": 10_000,
                "cost_basis_known": 1,
                "fees": 50,
            },
        ]
    )
    assert result_mixed["total_value"] == 17_000
    assert result_mixed["total_cost_basis"] == 10_000
    assert result_mixed["total_cash_invested"] == 10_050
    assert (
        result_mixed["kunown_value"]
        if "kunown_value" in result_mixed
        else result_mixed["known_value"] == 12_000
    )
    assert (
        result_mixed["kunown_value"]
        if "kunown_value" in result_mixed
        else result_mixed["gross_gain"] == 2_000
    )
    assert result_mixed["net_gain"] == 1_950
    assert result_mixed["gross_return_pct"] == pytest.approx(20.0)
    assert result_mixed["net_return_pct"] == pytest.approx(1_950 / 10_050 * 100)


def test_fire_zero_initial_capital_and_unfunded_goals() -> None:
    """Verify FIRE engine behavior for 0 capital, survival calculation and impossible goals."""
    # 1. Zero initial capital with ongoing expenses
    srr_zero = FirePlanningEngine.simulate_sequence_of_returns_risk(
        initial_capital=0.0,
        initial_annual_withdrawal=12_000.0,
        duration_years=30,
        average_real_return_pct=5.0,
        early_crash_scenario="baseline_steady",
    )
    assert srr_zero.starting_capital == 0.0
    assert srr_zero.portfolio_survived is False
    assert srr_zero.depletion_year == 1
    assert srr_zero.final_capital_real == 0.0

    # 2. Impossible goals (0 savings, capital < target)
    milestones = FirePlanningEngine.calculate_fire_milestones(
        current_invested=0.0,
        annual_expenses=20_000.0,
        annual_savings=0.0,
        real_growth_rate_pct=4.0,
        swr_pct=4.0,
    )
    std = milestones["standard"]
    assert std.target_capital == 500_000.0
    assert std.progress_pct == 0.0
    assert std.years_to_goal is None

    # 3. Achieved goal (capital >= target)
    milestones_achieved = FirePlanningEngine.calculate_fire_milestones(
        current_invested=600_000.0,
        annual_expenses=20_000.0,
        annual_savings=0.0,
        real_growth_rate_pct=4.0,
        swr_pct=4.0,
    )
    std_achieved = milestones_achieved["standard"]
    assert std_achieved.progress_pct >= 100.0
    assert std_achieved.years_to_goal == 0.0


def test_csv_import_normalizes_debit_columns_with_correct_negative_sign() -> None:
    """Ensure CSV import mapping of amount_debit produces negative amounts for expense rows."""

    class DummyRepo:
        def suggested_category_for_description(self, desc: str) -> str:
            return ""

    service = ImportService(repo=DummyRepo())  # type: ignore

    mapping = {
        "date": "Data",
        "desc": "Descritivo",
        "amount": "",
        "amount_debit": "Débito",
        "amount_credit": "Crédito",
        "category": "Categoria",
        "account": "Conta",
        "method": "",
        "merchant": "",
        "reference": "",
    }

    # Expense row: debit has positive 45.80 in CSV
    row_expense = {
        "Data": "2026-02-15",
        "Descritivo": "Supermercado Continente",
        "Débito": "45,80",
        "Crédito": "",
        "Categoria": "Alimentação",
        "Conta": "Conta Principal",
    }
    normalized_expense = service.normalize_row(
        row_expense,
        mapping,
        apply_rules=False,
        amount_locale="pt",
    )
    assert normalized_expense.is_valid
    assert normalized_expense.amount == -45.80

    # Income row: credit has 1250.00 in CSV
    row_income = {
        "Data": "2026-02-28",
        "Descritivo": "Vencimento Mensal",
        "D,9bito": "",
        "Crédito": "1250,00",
        "Categoria": "Rendimentos",
        "Conta": "Conta Principal",
    }
    normalized_income = service.normalize_row(
        row_income,
        mapping,
        apply_rules=False,
        amount_locale="pt",
    )
    assert normalized_income.is_valid
    assert normalized_income.amount == 1250.00


def test_ofx_multi_account_statement_assigns_distinct_accounts() -> None:
    """Ensure OFX statements containing multiple account blocks do not mix transactions into one account."""
    multi_account_ofx = """OFXHEADER:100
DATA:OFXSNML
VERSION:102
SECURITY:NONE
ENCODING:USASCII
CHARSET:1252
COMPRESSION:NONEOLDFILEENGODING:USASCII
NEWFILEENGODING:USASCII

<OFX>
  <SIGNONMSGSRSV1>
    <SONRS>
      <STATUS>
        <CODE>0
        <SEVERITY>INFO
      </STATUS>
      <DTSERVER>20260228120000
      <LANGUAGE>POR
    </SONRS>
  </SIGNONMSGRSV1>
  <BANKMSGSRSV1>
    <STMTTRNRS>
      <TRNUID>1001
      <STATUS><CODE>0<SEVERITY>INFO</STATUS>
      <STMTRS>
        <CURDEF>EUR
        <BANKACCTFROM>
          <BANKID>0033
          <ACCTID>PT500033000012345678901
          <ACCTTYPE>CHECKING
        </BANKACCTFROM>
        <BANKTRANLIST>
          <DTSTART>20260201
          <DTEND>20260228
          <STMTTRN>
            <TRNTYPE>DEBIT
            <DTPOSTED>20260205
            <TRNAMT>-32.50
            <FITID>TX-CHK-01
            <NAME>Mercadona
          </STMTTRN>
        </BANKTRANLIST>
      </STMTRS>
    </STMTTRNRS>
  </BANKMSGSRSV1>
  <CREDITCARDMSGSRSV1>
    <CCSTMTTRNRS>
      <TRNUID>1002
      <STATUS><CODE>0<SEVERITY>INFO</STATUS>
      <CCSTMTRS>
        <CURDEF>EUR
        <CCACCTFROM>
          <ACCTID>CARD-9988-7766
        </CCACCTFROM>
        <BANKTRANLIST>
          <DTSTART>20260201
          <DTEND>20260228
          <STMTTRN>
            <TRNTYPE>DEBIT
            <DTPOSTED>20260210
            <TRNAMT>-15.99
            <FITID>TX-CC-01
            <NAME>Netflix
          </STMTTRN>
        </BANKTRANLIST>
      </CCSTMTRS>
    </CCSTMTTRNRS>
  </CREDITCARDMSGSRSV1>
</OFX>
"""
    parsed = OFXParser.parse(multi_account_ofx)
    assert len(parsed.rows) == 2

    checking_row = next(
        r for r in parsed.rows if r["Referência bancária"] == "TX-CHK-01"
    )
    assert checking_row["Conta"] == "PT500033000012345678901"
    assert checking_row["Descrição"] == "Mercadona"
    assert checking_row["Valor"] == "-32.50"

    card_row = next(r for r in parsed.rows if r["Referência bancária"] == "TX-CC-01")
    assert card_row["Conta"] == "CARD-9988-7766"
    assert card_row["Descrição"] == "Netflix"
    assert card_row["Valor"] == "-15.99"
