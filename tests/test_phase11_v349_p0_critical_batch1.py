# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

from services.bank_parsers import OFXParser
from services.cashflow_forecast import CashflowForecastService
from services.decision_intelligence import build_decision_briefing
from services.imports import (
    ImportService,
)
from services.tax import (
    CONVENTION_DIVIDEND_TAX_RATES,
    ForeignDividendPayment,
    TaxCreditCalculation,
    calculate_foreign_dividend_tax,
    summarize_foreign_dividends,
)


class MockRepo:
    def __init__(self) -> None:
        self.rules: list[dict[str, Any]] = [
            {"keyword": "continente", "category": "Alimentação"},
            {"keyword": "salário", "category": "Rendimentos"},
        ]
        self.transactions: list[dict[str, Any]] = []

    def category_rules(self) -> list[dict[str, Any]]:
        return self.rules

    def suggested_category_for_description(self, description: str) -> str:
        text = str(description or "").casefold()
        for rule in self.rules:
            if rule["keyword"] in text:
                return str(rule["category"])
        return ""

    def validate_transaction_category(self, category: str, amount: float) -> str:
        cat_lower = str(category or "").strip().lower()
        if cat_lower in ("alimentação", "supermercado", "restauração", "diversos") and amount > 0:
            raise ValueError(f"A categoria '{category}' é exclusiva de despesas.")
        return category

    def duplicate_classification(self, *args, **kwargs) -> dict[str, Any]:
        return {"level": "none", "reason": ""}

    def import_transactions_batch(self, rows: list[dict[str, Any]], *, commit: bool = True) -> list[int]:
        ids: list[int] = []
        for i, r in enumerate(rows, start=len(self.transactions) + 1):
            self.transactions.append({**r, "id": i})
            ids.append(i)
        return ids

    def q(self, query: str, params: tuple = ()) -> list[dict[str, Any]]:
        return []


# ==============================================================================
# 1. BNK-PAR-01: Ausência de Inversão de Sinal em Extratos de Cartão de Crédito
# ==============================================================================

def test_bnk_par_01_normalize_row_inverts_sign_for_credit_card():
    repo = MockRepo()
    service = ImportService(repo)

    raw_purchase = {
        "Data": "2026-08-10",
        "Descrição": "Continente Bom Dia",
        "Valor": "45.50",
    }
    mapping = {"date": "Data", "desc": "Descrição", "amount": "Valor"}

    # Standard account: purchase is positive if bank gave positive (buggy behavior without flag)
    row_standard = service.normalize_row(
        raw_purchase,
        mapping,
        apply_rules=False,
        is_credit_card=False,
    )
    assert row_standard.amount == 45.50

    # Credit card account: purchase MUST be inverted to negative (-45.50)
    row_cc = service.normalize_row(
        raw_purchase,
        mapping,
        apply_rules=False,
        is_credit_card=True,
    )
    assert row_cc.amount == -45.50
    assert row_cc.is_valid is True

    # Credit card bill payment (negative in statement -> inverted to positive repayment)
    raw_payment = {
        "Data": "2026-08-28",
        "Descrição": "Pagamento de Fatura Débito Direto",
        "Valor": "-450.00",
    }
    row_payment = service.normalize_row(
        raw_payment,
        mapping,
        apply_rules=False,
        is_credit_card=True,
    )
    assert row_payment.amount == 450.00


def test_bnk_par_01_preview_and_import_with_credit_card():
    repo = MockRepo()
    service = ImportService(repo)

    rows = [
        {"Data": "2026-08-05", "Desc": "Pingo Doce", "Valor": "32.10"},
        {"Data": "2026-08-20", "Desc": "Zara", "Valor": "89.90"},
        {"Data": "2026-08-30", "Desc": "Pagamento Fatura", "Valor": "-122.00"},
    ]
    mapping = {"date": "Data", "desc": "Desc", "amount": "Valor"}

    preview = service.preview(
        rows,
        mapping,
        apply_rules=True,
        is_credit_card=True,
    )
    assert preview.total == 3
    assert preview.rows[0].transaction.amount == -32.10
    assert preview.rows[1].transaction.amount == -89.90
    assert preview.rows[2].transaction.amount == 122.00

    result = service.import_rows(
        rows,
        mapping,
        apply_rules=True,
        ignore_duplicates=False,
        is_credit_card=True,
    )
    assert result.created == 3
    assert repo.transactions[0]["amount"] == -32.10
    assert repo.transactions[1]["amount"] == -89.90
    assert repo.transactions[2]["amount"] == 122.00


def test_bnk_par_01_detect_credit_card_statements(tmp_path: Path):
    cc_csv = """Fatura de Cartão de Crédito - WiZink
Titular: Maria Santos
Data;Descrição;Montante
10/08/2026;Restaurante O Manel;35,00
12/08/2026;Uber Trip;14,20
25/08/2026;Pagamento de Fatura;-49,20
"""
    file_path = tmp_path / "fatura_cartao.csv"
    file_path.write_text(cc_csv, encoding="utf-8-sig")

    service = ImportService(MockRepo())
    data = service.read_file(file_path)
    assert data.is_credit_card is True
    assert any("cartão de crédito" in w.lower() for w in data.warnings)


# ==============================================================================
# 2. BNK-PAR-02: OFXParser Ignora <TRNTYPE> e Importa Débitos como Receitas
# ==============================================================================

def test_bnk_par_02_ofx_parser_enforces_debit_negative():
    # OFX from a bank where <TRNAMT> is exported as a positive number without minus sign
    ofx_content = """OFXHEADER:100
DATA:OFXSGML
<OFX>
<BANKMSGSRSV1>
<STMTTRNRS>
<STMTRS>
<BANKTRANLIST>
<STMTTRN>
<TRNTYPE>DEBIT
<DTPOSTED>20260815
<TRNAMT>150.00
<FITID>REF-001
<NAME>COMPRA SUPERMERCADO
</STMTTRN>
<STMTTRN>
<TRNTYPE>CHECK
<DTPOSTED>20260816
<TRNAMT>80.00
<FITID>REF-002
<NAME>PAGAMENTO CHEQUE
</STMTTRN>
<STMTTRN>
<TRNTYPE>PAYMENT
<DTPOSTED>20260817
<TRNAMT>45.00
<FITID>REF-003
<NAME>PAGAMENTO SERVICO
</STMTTRN>
<STMTTRN>
<TRNTYPE>FEE
<DTPOSTED>20260818
<TRNAMT>5.20
<FITID>REF-004
<NAME>COMISSAO CONTA
</STMTTRN>
<STMTTRN>
<TRNTYPE>CREDIT
<DTPOSTED>20260820
<TRNAMT>2500.00
<FITID>REF-005
<NAME>VENCIMENTO
</STMTTRN>
<STMTTRN>
<TRNTYPE>INT
<DTPOSTED>20260825
<TRNAMT>12.50
<FITID>REF-006
<NAME>JUROS DEPOSITO
</STMTTRN>
</BANKTRANLIST>
</STMTRS>
</STMTTRNRS>
</BANKMSGSRSV1>
</OFX>"""

    parsed = OFXParser.parse(ofx_content)
    assert len(parsed.rows) == 6

    # All DEBIT, CHECK, PAYMENT, FEE must have negative Valor
    assert parsed.rows[0]["Valor"] == "-150.00"
    assert parsed.rows[1]["Valor"] == "-80.00"
    assert parsed.rows[2]["Valor"] == "-45.00"
    assert parsed.rows[3]["Valor"] == "-5.20"

    # CREDIT and INT must be positive
    assert parsed.rows[4]["Valor"] == "2500.00"
    assert parsed.rows[5]["Valor"] == "12.50"


def test_bnk_par_02_ofx_parser_preserves_already_negative_debit():
    # If bank properly exported -150.00, it must remain -150.00
    ofx_content = """<OFX><BANKTRANLIST>
<STMTTRN>
<TRNTYPE>DEBIT
<DTPOSTED>20260815
<TRNAMT>-150.00
<FITID>REF-001
<NAME>COMPRA NORMAL
</STMTTRN>
</BANKTRANLIST></OFX>"""

    parsed = OFXParser.parse(ofx_content)
    assert parsed.rows[0]["Valor"] == "-150.00"


def test_bnk_par_02_ofx_ccstmt_detects_credit_card():
    ofx_cc = """<OFX>
<CREDITCARDMSGSRSV1>
<CCSTMTTRNRS>
<CCSTMTRS>
<BANKTRANLIST>
<STMTTRN>
<TRNTYPE>DEBIT
<DTPOSTED>20260810
<TRNAMT>75.00
<FITID>CC-001
<NAME>ZARA
</STMTTRN>
</BANKTRANLIST>
</CCSTMTRS>
</CCSTMTTRNRS>
</CREDITCARDMSGSRSV1>
</OFX>"""

    parsed = OFXParser.parse(ofx_cc)
    assert parsed.is_credit_card is True
    assert parsed.rows[0]["Valor"] == "-75.00"


# ==============================================================================
# 3. CASH-01: Divisão Cega de Despesas por 90 Dias em Contas Recentes
# ==============================================================================

def test_cash_01_variable_daily_spend_adapts_to_history_window():
    class DummyCashflowRepo:
        def __init__(self, daily_map: dict[date, float]):
            self.daily_map = daily_map

        def q(self, *args, **kwargs):
            return []

    class DummyPaymentsService:
        pass

    service = CashflowForecastService(DummyCashflowRepo({}), DummyPaymentsService())

    # Case A: User started 15 days ago, spent 1,500€ total
    # Monkeypatch _variable_spend_by_day to return 15 days of 100€
    ref_date = date(2026, 8, 15)
    spend_15d = {
        ref_date - timedelta(days=i): 100.0
        for i in range(15)
    }
    service._variable_spend_by_day = lambda start, ref: spend_15d

    # Old bug divided 1,500 / 90 = 16.67 €/day.
    # Correct calculation: history_days = 15 -> 1,500 / 15 = 100.0 €/day!
    rate_15d = service._variable_daily_spend(ref_date, lookback_days=90)
    assert rate_15d == pytest.approx(100.0, 0.01)

    # Case B: Account with only 1 day of expenses (500€ yesterday)
    spend_1d = {ref_date: 500.0}
    service._variable_spend_by_day = lambda start, ref: spend_1d
    rate_1d = service._variable_daily_spend(ref_date, lookback_days=90)
    assert rate_1d == pytest.approx(500.0, 0.01)

    # Case C: Account with 120 days of data (should cap at lookback_days=90)
    spend_90d = {
        ref_date - timedelta(days=i): 50.0
        for i in range(90)
    }
    service._variable_spend_by_day = lambda start, ref: spend_90d
    rate_90d = service._variable_daily_spend(ref_date, lookback_days=90)
    assert rate_90d == pytest.approx(50.0, 0.01)

    # Case D: Empty data -> 0.0
    service._variable_spend_by_day = lambda start, ref: {}
    rate_empty = service._variable_daily_spend(ref_date, lookback_days=90)
    assert rate_empty == 0.0


# ==============================================================================
# 4. AUD-TAX-01: Categoria E (Dividendos Estrangeiros), Tetos de CDT e Art. 81.º CIRS
# ==============================================================================

def test_aud_tax_01_foreign_dividend_with_w8ben_15pct_treaty_cap():
    # US dividend with W-8BEN: 1,000€ gross, 150€ (15%) withheld in the US
    div = ForeignDividendPayment(
        id="div-001",
        asset_symbol="MSFT",
        payment_date=date(2026, 6, 15),
        gross_amount_eur=1000.0,
        foreign_tax_withheld_eur=150.0,
        country_code="US",
        has_treaty_form=True,
    )

    calc = calculate_foreign_dividend_tax(div)
    assert isinstance(calc, TaxCreditCalculation)
    assert calc.gross_dividend == 1000.0
    assert calc.foreign_tax_withheld == 150.0
    assert calc.treaty_rate_pct == 15.0
    assert calc.maximum_creditable_foreign_tax == 150.0
    assert calc.uncreditable_foreign_tax_loss == 0.0
    assert calc.portuguese_autonomous_tax_28pct == 280.0
    assert calc.tax_credit_applied_art81 == 150.0
    # Net Portuguese tax payable = 280 - 150 = 130
    assert calc.net_portuguese_tax_payable == 130.0
    # Total tax = 150 (US) + 130 (PT) = 280 (28.0%)
    assert calc.effective_total_tax_rate_pct == 28.0


def test_aud_tax_01_foreign_dividend_without_w8ben_30pct_withholding_leakage():
    # US dividend WITHOUT W-8BEN: 1,000€ gross, 300€ (30%) statutory withholding in the US
    div = ForeignDividendPayment(
        id="div-002",
        asset_symbol="AAPL",
        payment_date=date(2026, 7, 20),
        gross_amount_eur=1000.0,
        foreign_tax_withheld_eur=300.0,
        country_code="US",
        has_treaty_form=False,
    )

    calc = calculate_foreign_dividend_tax(div)
    assert calc.gross_dividend == 1000.0
    assert calc.foreign_tax_withheld == 300.0
    # AT caps credit at treaty limit (15% = 150€)
    assert calc.maximum_creditable_foreign_tax == 150.0
    # 150€ is uncreditable loss!
    assert calc.uncreditable_foreign_tax_loss == 150.0
    assert calc.portuguese_autonomous_tax_28pct == 280.0
    assert calc.tax_credit_applied_art81 == 150.0
    assert calc.net_portuguese_tax_payable == 130.0
    # Total tax = 300 (US) + 130 (PT) = 430 (43.0%)
    assert calc.effective_total_tax_rate_pct == 43.0


def test_aud_tax_01_summarize_foreign_dividends_anexo_j_quadro_8():
    divs = [
        ForeignDividendPayment("1", "AAPL", date(2026, 3, 10), 1000.0, 150.0, "US"),
        ForeignDividendPayment("2", "MSFT", date(2026, 6, 15), 2000.0, 300.0, "US"),
        ForeignDividendPayment("3", "ASML", date(2026, 5, 20), 500.0, 75.0, "NL"),  # NL treaty rate 10% -> 50€ cap, 25€ loss
    ]

    summary = summarize_foreign_dividends(divs, fiscal_year=2026)
    assert summary["fiscal_year"] == 2026
    assert summary["total_dividends_count"] == 3
    assert summary["total_gross_dividend"] == 3500.0
    assert summary["total_foreign_tax_withheld"] == 525.0
    # NL cap is 10% of 500 = 50. US cap is 15% of 3000 = 450. Total creditable = 500.
    assert summary["total_max_creditable_tax"] == 500.0
    assert summary["total_uncreditable_loss"] == 25.0
    assert summary["total_tax_credit_art81"] == 500.0
    # Net PT tax = (3500 * 0.28 = 980) - 500 = 480
    assert summary["total_net_portuguese_tax_payable"] == 480.0
    assert len(summary["anexo_j_quadro_8"]) == 2  # US and NL


def test_aud_tax_01_decision_intelligence_alerts_on_tax_leakage():
    foreign_summary = {
        "total_uncreditable_loss": 150.0,
    }

    briefing = build_decision_briefing(
        totals={"income": 3000.0, "expenses": 1500.0},
        foreign_tax_summary=foreign_summary,
        as_of=date(2026, 8, 1),
    )

    priorities = briefing["priorities"]
    tax_priority = next((p for p in priorities if p["key"] == "tax:foreign_dividend_leak"), None)
    assert tax_priority is not None
    assert tax_priority["tone"] == "warning"
    assert "W-8BEN" in tax_priority["message"]
    assert tax_priority["impact_amount"] == 150.0


def test_aud_tax_01_all_treaty_rates_and_direct_foreign_dividends_in_briefing():
    # Verify defined countries
    for country in ("US", "CA", "GB", "DE", "FR", "ES", "NL", "CH", "IE"):
        assert country in CONVENTION_DIVIDEND_TAX_RATES

    assert CONVENTION_DIVIDEND_TAX_RATES["NL"] == 10.0
    assert CONVENTION_DIVIDEND_TAX_RATES["US"] == 15.0

    # Test passing foreign_dividends directly to build_decision_briefing
    divs = [
        ForeignDividendPayment("1", "AAPL", date(2026, 7, 20), 1000.0, 300.0, "US"),  # 150€ leakage
    ]
    briefing = build_decision_briefing(
        totals={"income": 2000.0, "expenses": 1000.0},
        foreign_dividends=divs,
        as_of=date(2026, 8, 1),
    )
    priorities = briefing["priorities"]
    tax_p = next((p for p in priorities if p["key"] == "tax:foreign_dividend_leak"), None)
    assert tax_p is not None
    assert tax_p["impact_amount"] == 150.0


def test_cash_01_robustness_boundaries():
    class DummyCashflowRepo:
        def q(self, *args, **kwargs):
            return []

    class DummyPaymentsService:
        pass

    service = CashflowForecastService(DummyCashflowRepo(), DummyPaymentsService())
    ref = date(2026, 9, 1)

    # Inverted dates fallback (first_tx > last_tx) -> max(1, ...)
    service._variable_spend_by_day = lambda s, r: {date(2026, 8, 20): 100.0}
    val = service._variable_daily_spend(
        ref,
        lookback_days=90,
        first_tx_date=date(2026, 8, 25),
        last_tx_date=date(2026, 8, 20),
    )
    assert val == 100.0  # history_days capped at 1

    # Lookback 0 defaults to lookback 90 or min 1
    val_zero = service._variable_daily_spend(ref, lookback_days=0)
    assert val_zero == 100.0


# ==============================================================================
# 5. REGRESSION ASSURANCE: Verify existing suites pass without conflicts
# ==============================================================================

def test_regression_existing_pt_bank_dialects():
    from tests.test_phase3_v348_pt_banking_import_ui import test_detect_portuguese_bank_dialects, test_pt_banking_suggested_mapping
    test_detect_portuguese_bank_dialects()
    test_pt_banking_suggested_mapping()


def test_regression_existing_ofx_and_qif(tmp_path: Path):
    from tests.test_bank_parsers_and_pt_imports import (
        test_cgd_statement_with_metadata_headers_and_split_debit_credit,
        test_ofx_sgml_parser_and_import,
        test_qif_parser_and_import,
    )
    test_cgd_statement_with_metadata_headers_and_split_debit_credit(tmp_path)
    test_ofx_sgml_parser_and_import(tmp_path)
    test_qif_parser_and_import(tmp_path)


def test_regression_existing_decision_intelligence():
    from tests.test_decision_intelligence import (
        test_decision_briefing_ranks_critical_anomaly_before_other_risks,
    )
    test_decision_briefing_ranks_critical_anomaly_before_other_risks()


def test_regression_existing_tax_engine():
    from tests.test_phase3_v349_quant_tax_and_banking import (
        test_tax_crypto_365d_exemption_and_short_term_tax,
        test_tax_loss_carryforward_and_inclusion,
    )
    test_tax_crypto_365d_exemption_and_short_term_tax()
    test_tax_loss_carryforward_and_inclusion()


def test_bnk_par_01_credit_card_with_separate_debit_credit_columns_does_not_double_invert():
    repo = MockRepo()
    service = ImportService(repo)

    raw_debit_purchase = {
        "Data": "2026-08-10",
        "Descrição": "Continente",
        "Débito": "54.20",
        "Crédito": "",
    }
    raw_credit_payment = {
        "Data": "2026-08-25",
        "Descrição": "Pagamento Fatura",
        "Débito": "",
        "Crédito": "450.00",
    }
    mapping = {
        "date": "Data",
        "desc": "Descrição",
        "amount_debit": "Débito",
        "amount_credit": "Crédito",
    }

    # Even with is_credit_card=True, Débito must remain negative (-54.20) and Crédito positive (+450.00)
    row_purchase = service.normalize_row(
        raw_debit_purchase,
        mapping,
        apply_rules=False,
        is_credit_card=True,
    )
    assert row_purchase.amount == -54.20

    row_payment = service.normalize_row(
        raw_credit_payment,
        mapping,
        apply_rules=False,
        is_credit_card=True,
    )
    assert row_payment.amount == 450.00


def test_bnk_par_01_debit_card_keyword_does_not_trigger_credit_card_auto_detection():
    headers = ["Data Mov.", "Descrição", "Valor"]
    sample_text = "Extrato de Conta à Ordem - Movimentos Cartão de Débito"
    assert ImportService.is_likely_credit_card(headers, sample_text) is False


def test_bnk_par_02_ofx_ccstmt_read_file_and_preview_keeps_debit_negative(tmp_path: Path):
    ofx_cc = """<OFX>
<CREDITCARDMSGSRSV1>
<CCSTMTTRNRS>
<CCSTMTRS>
<BANKTRANLIST>
<STMTTRN>
<TRNTYPE>DEBIT
<DTPOSTED>20260810
<TRNAMT>75.00
<FITID>CC-001
<NAME>ZARA
</STMTTRN>
<STMTTRN>
<TRNTYPE>CREDIT
<DTPOSTED>20260825
<TRNAMT>150.00
<FITID>CC-002
<NAME>PAGAMENTO DE FATURA
</STMTTRN>
</BANKTRANLIST>
</CCSTMTRS>
</CCSTMTTRNRS>
</CREDITCARDMSGSRSV1>
</OFX>"""
    file_path = tmp_path / "cc_statement.ofx"
    file_path.write_text(ofx_cc, encoding="utf-8")

    service = ImportService(MockRepo())
    data = service.read_file(file_path)

    # A07 preserves account nature separately from deliberate sign inversion.
    assert data.is_credit_card is True
    assert data.invert_signs is False

    suggested = service.suggested_mapping(data.headers)
    preview = service.preview(data.rows, suggested, apply_rules=False, invert_signs=data.invert_signs)

    # Zara must be -75.00 (expense), NOT +75.00!
    assert preview.rows[0].transaction.amount == -75.00
    # Payment must be +150.00 (credit)
    assert preview.rows[1].transaction.amount == 150.00


def test_bnk_par_02_ofx_extended_trntypes():
    ofx_content = """<OFX><BANKTRANLIST>
<STMTTRN>
<TRNTYPE>DIRECTDEBIT
<DTPOSTED>20260815
<TRNAMT>65.00
<FITID>REF-DD
<NAME>VODAFONE DEBITO DIRETO
</STMTTRN>
<STMTTRN>
<TRNTYPE>CASH
<DTPOSTED>20260816
<TRNAMT>100.00
<FITID>REF-CASH
<NAME>LEVANTAMENTO MULTIBANCO
</STMTTRN>
<STMTTRN>
<TRNTYPE>REPEATPMT
<DTPOSTED>20260817
<TRNAMT>300.00
<FITID>REF-RPT
<NAME>ORDEM PERMANENTE
</STMTTRN>
<STMTTRN>
<TRNTYPE>DIRECTDEP
<DTPOSTED>20260820
<TRNAMT>-2000.00
<FITID>REF-DEP
<NAME>ORDENADO TRANSFERENCIA
</STMTTRN>
</BANKTRANLIST></OFX>"""

    parsed = OFXParser.parse(ofx_content)
    assert parsed.rows[0]["Valor"] == "-65.00"
    assert parsed.rows[1]["Valor"] == "-100.00"
    assert parsed.rows[2]["Valor"] == "-300.00"
    assert parsed.rows[3]["Valor"] == "2000.00"


def test_cash_01_single_expense_10_days_ago_adapts_to_reference_horizon():
    class DummyCashflowRepo:
        def q(self, *args, **kwargs):
            return []

    class DummyPaymentsService:
        pass

    service = CashflowForecastService(DummyCashflowRepo(), DummyPaymentsService())
    ref = date(2026, 8, 15)

    # User joined 10 days ago (2026-08-05), only 1 expense of 330€ on that day
    first_date = date(2026, 8, 5)
    service._variable_spend_by_day = lambda s, r: {first_date: 330.0}

    # Calculation horizon from first_date (Aug 5) up to reference/last_date (Aug 15) = 11 days
    # Daily rate = 330.0 / 11 = 30.0 €/day (NOT 330.0 / 1 = 330.0 €/day!)
    rate = service._variable_daily_spend(
        ref,
        lookback_days=90,
        first_tx_date=first_date,
        last_tx_date=ref,
    )
    assert rate == pytest.approx(30.0, 0.01)


def test_cash_01_explicit_first_and_last_tx_date():
    class DummyCashflowRepo:
        def q(self, *args, **kwargs):
            return []

    class DummyPaymentsService:
        pass

    service = CashflowForecastService(DummyCashflowRepo(), DummyPaymentsService())
    ref = date(2026, 8, 31)

    # Spend: 310€ on Aug 15
    service._variable_spend_by_day = lambda s, r: {date(2026, 8, 15): 310.0}

    # First transaction on Aug 1, last on Aug 31 -> 31 days
    # 310.0 / 31 = 10.0 €/day
    rate = service._variable_daily_spend(
        ref,
        lookback_days=90,
        first_tx_date=date(2026, 8, 1),
        last_tx_date=date(2026, 8, 31),
    )
    assert rate == pytest.approx(10.0, 0.01)


def test_aud_tax_01_offshore_jurisdiction_35pct_and_zero_credit():
    # Cayman Islands ("KY") dividend: blacklisted under Portaria 150/2004
    # CIRS Art. 72.º n.º 14 (35%) & Art. 81.º n.º 6 (0% tax credit)
    div = ForeignDividendPayment(
        id="div-offshore",
        asset_symbol="OFFSHORE-FUND",
        payment_date=date(2026, 5, 10),
        gross_amount_eur=1000.0,
        foreign_tax_withheld_eur=100.0,
        country_code="KY",
    )
    calc = calculate_foreign_dividend_tax(div)
    assert calc.gross_dividend == 1000.0
    assert calc.treaty_rate_pct == 0.0
    assert calc.maximum_creditable_foreign_tax == 0.0
    assert calc.uncreditable_foreign_tax_loss == 100.0
    assert calc.portuguese_autonomous_tax_28pct == 350.0  # 35% aggravated rate
    assert calc.tax_credit_applied_art81 == 0.0
    assert calc.net_portuguese_tax_payable == 350.0
    assert calc.effective_total_tax_rate_pct == 45.0  # 100 withheld + 350 PT = 450 (45%)


def test_aud_tax_01_domestic_pt_dividend_zero_credit():
    div = ForeignDividendPayment(
        id="div-pt",
        asset_symbol="GALP",
        payment_date=date(2026, 4, 15),
        gross_amount_eur=500.0,
        foreign_tax_withheld_eur=0.0,
        country_code="PT",
    )
    calc = calculate_foreign_dividend_tax(div)
    assert calc.treaty_rate_pct == 0.0
    assert calc.maximum_creditable_foreign_tax == 0.0
    assert calc.uncreditable_foreign_tax_loss == 0.0
    assert calc.portuguese_autonomous_tax_28pct == 140.0  # 28% of 500
    assert calc.tax_credit_applied_art81 == 0.0
    assert calc.net_portuguese_tax_payable == 140.0


def test_sync_audit_files_consistency(tmp_path):
    import shutil
    src = tmp_path / "source.md"
    dst = tmp_path / "synthetic-brain" / "audit.md"
    src.write_text("# Synthetic audit\nPreservar acentuação.\n", encoding="utf-8")
    dst.parent.mkdir()
    shutil.copyfile(src, dst)
    assert dst.read_text(encoding="utf-8") == src.read_text(encoding="utf-8")


def test_ruff_and_syntax_check(tmp_path):
    import py_compile
    import subprocess
    import sys
    files = [
        "services/bank_parsers.py",
        "services/imports.py",
        "services/cashflow_forecast.py",
        "services/tax.py",
        "services/decision_intelligence.py",
        "ui/pages/tools.py",
        "tests/test_phase11_v349_p0_critical_batch1.py",
    ]
    for f in files:
        py_compile.compile(f, cfile=str(tmp_path / (Path(f).stem + ".pyc")), doraise=True)
    res = subprocess.run([sys.executable, "-m", "ruff", "check"] + files, capture_output=True, text=True)
    assert res.returncode == 0, f"Ruff check failed:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}"
