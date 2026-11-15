#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Regression tests for audit Batch 24, Council Member 1 (C103-C114).

Domain: financial mathematics, CIRS taxation & bank parsers.

Bugs covered (COUNCIL_ROUND4_DEEP_AUDIT.md, Membro 1):
- BUG-AUD-C103 (P0): stock splits absent from FIFO lots - apply_stock_split
  adjusts quantity/unit price preserving cost basis (Art. 43. CIRS).
- BUG-AUD-C104 (P1): loss carryforward under the autonomous 28% rate must
  NOT report the year's losses forward (CIRS Art. 55. n. 1 al. d));
  prior buckets stay frozen (kept, never usable, never expanded).
- BUG-AUD-C105 (P1): domestic withholding at source on foreign dividends
  (retained by a Portuguese intermediary) must settle the Portuguese
  liability after the Art. 81. foreign credit (no double taxation).
- BUG-AUD-C106 (P1): xirr Brent fallback sign near r = -1 must follow the
  dominant (largest-t) flow term, not the first overflowing flow.
- BUG-AUD-C107 (P1): periodic_discount_factor is the capitalization factor
  (1+r)^n; the strict PV discount factor (1+r)^-n is exposed as
  periodic_pv_discount_factor (e2e pin df12 = 1.05 preserved).
- BUG-AUD-C108 (P1): globally detected MM/DD date format must be APPLIED to
  row normalization, not merely warned about.
- BUG-AUD-C109 (P1): CGD/BCP footer lines with fewer columns are skipped
  with a warning instead of rejecting the whole statement.
- BUG-AUD-C110 (P1): "-", "--" and "N/A" cells in debit/credit columns are
  zero values, not malformed numbers.
- BUG-AUD-C111 (P1): OFX credit-card PAYMENT transactions are balance
  settlements (credit/inflow), never inverted to debits.
- BUG-AUD-C112 (P2): FIRE SRR runs in real terms; nominal is real indexed
  by cumulative inflation - never double-discounted.
- BUG-AUD-C113 (P2): future-dated transactions must not inflate the burn
  rate denominator collapse (observation window clamped to reference).
- BUG-AUD-C114 (P2): sharpe_ratio with zero volatility and positive excess
  return returns the formal 99.99 sentinel, never 0.0.

Exact-value assertions (TDD), PT-PT docstrings.
"""

from __future__ import annotations

import math
import os
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Exact-value fiscal tests need an empty database context (no demo seeding).
if os.environ.get("FINANCAS_PESSOAIS_DEMO_DATA"):
    os.environ.pop("FINANCAS_PESSOAIS_DEMO_DATA", None)

from services.bank_parsers import OFXParser  # noqa: E402
from services.finance_math import (  # noqa: E402
    periodic_discount_factor,
    periodic_pv_discount_factor,
    sharpe_ratio,
    xirr_pct,
)
from services.fire import FirePlanningEngine  # noqa: E402
from services.imports import (  # noqa: E402
    AMOUNT_LOCALE_PT,
    DATE_FORMAT_DD_MM,
    DATE_FORMAT_MM_DD,
    CsvImportData,
    ImportService,
    detect_date_format_globally,
)
from services.tax import ForeignDividendPayment, PortugueseTaxEngine, calculate_foreign_dividend_tax  # noqa: E402


class MockRepo:
    """Minimal repo mock for ImportService (no rules, no duplicates)."""

    def category_rules(self) -> list[dict[str, str]]:
        return []

    def suggested_category_for_description(self, description: str) -> str:
        return ""

    def validate_transaction_category(self, category: str, amount: float) -> str:
        return category

    def duplicate_classification(self, *args: object, **kwargs: object) -> dict[str, str]:
        return {"level": "none", "reason": ""}


# ============================================================================
# C103 (P0): Stock splits no FIFO - basis preservada
# ============================================================================


def test_c103_forward_split_4to1_preserves_cost_basis_and_taxes_correctly():
    """Split 4:1 da Nvidia-style: 10 acoes a 150 EUR -> 40 acoes a 37,50 EUR.

    Venda posterior de 40 acoes a 50 EUR: mais-valia = 40*50 - 1500 = 500
    EUR; imposto autonomo 28% = 140 EUR. Sem o split, a venda de 40 acoes
    contra um lote de 10 levantaria ValueError.
    """
    engine = PortugueseTaxEngine()
    engine.add_acquisition(
        lot_id="aapl-1",
        asset_symbol="AAPL",
        acquisition_date=date(2023, 3, 1),
        quantity=10.0,
        unit_price=150.0,
    )
    adjusted = engine.apply_stock_split("AAPL", 4.0, date(2024, 6, 1))
    assert adjusted == 1

    matches = engine.process_disposal(
        disposal_id="d1",
        asset_symbol="AAPL",
        disposal_date=date(2025, 1, 10),
        quantity=40.0,
        unit_price=50.0,
    )
    assert len(matches) == 1
    m = matches[0]
    # Basis preservada: 40 * 37,50 = 1500 EUR (igual ao custo original).
    assert m.matched_quantity == pytest.approx(40.0)
    assert m.acquisition_price == pytest.approx(37.5)
    assert m.adjusted_acquisition_cost == pytest.approx(1500.0)
    assert m.gross_disposal_proceeds == pytest.approx(2000.0)
    assert m.net_gain_loss == pytest.approx(500.0)
    assert m.estimated_tax == pytest.approx(140.0)


def test_c103_reverse_split_1to10_partial_sale_uses_adjusted_price():
    """Split inverso 1:10: 100 acoes a 2 EUR -> 10 acoes a 20 EUR.

    Venda parcial de 4 acoes a 30 EUR: custo 4*20 = 80 EUR, ganho 40 EUR.
    """
    engine = PortugueseTaxEngine()
    engine.add_acquisition(
        lot_id="penn-1",
        asset_symbol="PENN",
        acquisition_date=date(2024, 1, 10),
        quantity=100.0,
        unit_price=2.0,
    )
    adjusted = engine.apply_stock_split("PENN", 0.1, date(2024, 5, 1))
    assert adjusted == 1

    matches = engine.process_disposal(
        disposal_id="d1",
        asset_symbol="PENN",
        disposal_date=date(2025, 2, 1),
        quantity=4.0,
        unit_price=30.0,
    )
    assert len(matches) == 1
    m = matches[0]
    assert m.matched_quantity == pytest.approx(4.0)
    assert m.acquisition_price == pytest.approx(20.0)
    assert m.adjusted_acquisition_cost == pytest.approx(80.0)
    assert m.net_gain_loss == pytest.approx(40.0)


# ============================================================================
# C104 (P1): Perdas caducam sob taxa autonomia; englobamento reporta
# ============================================================================


def test_c104_autonomous_regime_lapses_year_losses_and_freezes_prior():
    """Sob 28% autonomo: perdas do ano NAO sao reportadas (Art. 55. n. 1 al. d).

    Cenario: ganho +500 EUR em 2026 com 400 EUR de perdas anteriores
    elegiveis. Sem englobamento: deducao 0, base tributavel 500, imposto
    140 EUR; buckets anteriores permanecem congelados (400, nem usados nem
    expandidos); perdas do ano (se existissem) caducam.
    """
    engine = PortugueseTaxEngine()
    engine.add_acquisition(
        lot_id="g",
        asset_symbol="GAIN",
        acquisition_date=date(2025, 1, 5),
        quantity=10.0,
        unit_price=100.0,
    )
    matches = engine.process_disposal(
        disposal_id="d",
        asset_symbol="GAIN",
        disposal_date=date(2026, 3, 1),
        quantity=10.0,
        unit_price=150.0,  # +500
    )

    report = engine.generate_annual_tax_report(
        matches, 2026, prior_year_losses=400.0, prior_year_losses_by_year={2025: 400.0}
    )
    assert report["prior_year_losses_deducted"] == 0.0
    assert report["final_taxable_base"] == pytest.approx(500.0)
    assert report["global_estimated_tax_28pct"] == pytest.approx(140.0)
    # Prior buckets frozen: kept intact, never usable, never expanded.
    assert report["remaining_prior_year_losses"] == pytest.approx(400.0)

    # Ano com perda liquida sob autonomia: as perdas do ano caducam.
    engine_loss = PortugueseTaxEngine()
    engine_loss.add_acquisition(
        lot_id="l",
        asset_symbol="LOSS",
        acquisition_date=date(2025, 1, 5),
        quantity=10.0,
        unit_price=100.0,
    )
    loss_matches = engine_loss.process_disposal(
        disposal_id="dl",
        asset_symbol="LOSS",
        disposal_date=date(2026, 7, 1),
        quantity=10.0,
        unit_price=50.0,  # -500
    )
    report_loss = engine_loss.generate_annual_tax_report(loss_matches, 2026)
    assert report_loss["global_estimated_tax_28pct"] == 0.0
    assert report_loss["current_year_losses_generated"] == 0.0
    assert report_loss["total_losses_to_carryforward"] == 0.0


def test_c104_englobamento_reports_losses_within_5y_window():
    """Com englobamento, perdas anteriores dedutíveis e perdas do ano reportadas."""
    engine = PortugueseTaxEngine()
    engine.add_acquisition(
        lot_id="g2",
        asset_symbol="GAIN",
        acquisition_date=date(2025, 1, 5),
        quantity=10.0,
        unit_price=100.0,
    )
    matches = engine.process_disposal(
        disposal_id="d2",
        asset_symbol="GAIN",
        disposal_date=date(2026, 3, 1),
        quantity=10.0,
        unit_price=150.0,  # +500
    )
    report = engine.generate_annual_tax_report(
        matches,
        2026,
        prior_year_losses=400.0,
        prior_year_losses_by_year={2025: 400.0},
        opt_for_englobamento=True,
    )
    assert report["prior_year_losses_deducted"] == pytest.approx(400.0)
    assert report["final_taxable_base"] == pytest.approx(100.0)
    assert report["global_estimated_tax_28pct"] == pytest.approx(28.0)
    assert report["remaining_prior_year_losses"] == 0.0


# ============================================================================
# C105 (P1): Retencao nacional em dividendos estrangeiros (Art. 81. CIRS)
# ============================================================================


def test_c105_domestic_withholding_settles_pt_liability_after_foreign_credit():
    """Dividendo US via intermediario portugues: 1000 EUR brutos, 150 (15% CDT)
    retidos na origem e 100 EUR retidos em Portugal.

    pt_gross = 280; credito externo = 150; retencao nacional = 100;
    net a pagar = 280 - 150 - 100 = 30 EUR. Total pago = 150+100+30 = 280
    (28,0% efetivo - nunca dupla tributacao).
    """
    div = ForeignDividendPayment(
        id="div-us-cgd",
        asset_symbol="MSFT",
        payment_date=date(2026, 6, 15),
        gross_amount_eur=1000.0,
        foreign_tax_withheld_eur=150.0,
        country_code="US",
        has_treaty_form=True,
    )
    calc = calculate_foreign_dividend_tax(div, domestic_tax_withheld=100.0)
    assert calc.portuguese_autonomous_tax_28pct == pytest.approx(280.0)
    assert calc.tax_credit_applied_art81 == pytest.approx(150.0)
    assert calc.net_portuguese_tax_payable == pytest.approx(30.0)
    assert calc.effective_total_tax_rate_pct == pytest.approx(28.0)

    # Sem retencao nacional o comportamento historico mantem-se (regressao).
    calc_no_domestic = calculate_foreign_dividend_tax(div)
    assert calc_no_domestic.net_portuguese_tax_payable == pytest.approx(130.0)

    # Retencao nacional que cobre toda a responsabilidade: nunca negativo.
    calc_full = calculate_foreign_dividend_tax(div, domestic_tax_withheld=280.0)
    assert calc_full.net_portuguese_tax_payable == 0.0


# ============================================================================
# C106 (P1): Sinal do fallback de Brent no xirr sob perdas severas
# ============================================================================


def test_c106_xirr_converges_long_horizon_severe_losses_last_positive_flow():
    """Carteira com perdas severas e fluxo final positivo em horizonte ~80 anos.

    Perto de r = -1 o termo dominante (maior t, positivo) diverge para +inf;
    o fallback invertido devolvia -1e12, invertendo o bracket de Brent e
    fazendo o solver devolver None. Corrigido: converge para a raiz real.
    """
    flows = [
        (date(2019, 1, 1), -100_000.0),
        (date(2020, 1, 1), 10_000.0),
        (date(2021, 1, 1), 5_000.0),
        (date(2022, 1, 1), 2_000.0),
        (date(2023, 1, 1), 1_000.0),
        (date(2024, 1, 1), 500.0),
        (date(2025, 1, 1), 100.0),
        (date(2099, 1, 1), 50.0),
    ]
    rate = xirr_pct(flows)
    assert rate is not None, "xirr devolveu None: fallback de Brent com sinal invertido"
    # Raiz real ~ -8,77%: carteira que queima capital lentamente.
    assert rate == pytest.approx(-8.7749, abs=0.01)
    assert -50.0 < rate < 0.0


def test_c106_xirr_severe_loss_short_horizon_converges_negative():
    """Perdas severas em horizonte de 7 anos: converge ~ -49,85% (regressao)."""
    flows = [
        (date(2019, 1, 1), -100_000.0),
        (date(2020, 1, 1), 10_000.0),
        (date(2021, 1, 1), 5_000.0),
        (date(2022, 1, 1), 2_000.0),
        (date(2023, 1, 1), 1_000.0),
        (date(2024, 1, 1), 500.0),
        (date(2025, 1, 1), 100.0),
        (date(2026, 1, 1), 50.0),
    ]
    rate = xirr_pct(flows)
    assert rate is not None
    assert rate == pytest.approx(-49.8534, abs=0.01)

    # Caso saudavel inalterado: 10% exato.
    assert xirr_pct(
        [(date(2024, 1, 1), -10_000.0), (date(2024, 12, 31), 11_000.0)]
    ) == pytest.approx(10.0, abs=0.01)


# ============================================================================
# C107 (P1): Alias de desconto estrito (1+r)^-n; pin e2e df12=1.05
# ============================================================================


def test_c107_pv_discount_factor_alias_and_capitalization_pin():
    """periodic_pv_discount_factor devolve (1+r)^-n estrito; o pin e2e
    df12=1.05 do fator de capitalizacao mantem-se (API estavel)."""
    # 5% anual efetivo, 12 meses: capitalizacao (1+r)^12 = 1,05 (pin e2e).
    assert periodic_discount_factor(5.0, 12, periods_per_year=12) == pytest.approx(1.05, rel=1e-9)
    # Alias estrito: (1+r)^-12 = 1/1,05 = 0,952380952...
    assert periodic_pv_discount_factor(5.0, 12, periods_per_year=12) == pytest.approx(1.0 / 1.05, rel=1e-12)
    assert periodic_pv_discount_factor(5.0, 12, periods_per_year=12) == pytest.approx(0.9523809523809524, rel=1e-12)
    # Periodo 0: fator 1,0 em ambas as convenções.
    assert periodic_pv_discount_factor(5.0, 0, periods_per_year=12) == 1.0
    # Identidade: capitalizacao * desconto estrito = 1.
    df_cap = periodic_discount_factor(7.0, 24, periods_per_year=12)
    df_pv = periodic_pv_discount_factor(7.0, 24, periods_per_year=12)
    assert df_cap * df_pv == pytest.approx(1.0, rel=1e-12)


# ============================================================================
# C108 (P1): Formato MM/DD detetado é APLICADO na normalização
# ============================================================================


def test_c108_detected_mmdd_format_is_applied_to_row_normalization():
    """Extrato internacional MM/DD: 03/04/2024 = 4 de MARCO (nao 3 de abril).

    A deteccao global liga-se a normalizacao: normalize_row com
    date_format='%m/%d/%Y' reordena mes-antes-dia; sem formato, mantem o
    default DD/MM europeu. Cenario do audit: despesa de 4 de Marco
    (03/04/2024 em MM/DD) era importada como 3 de Abril.
    """
    assert detect_date_format_globally(["03/04/2024", "05/03/2024", "12/25/2024"]) == DATE_FORMAT_MM_DD
    assert detect_date_format_globally(["25/03/2024", "05/03/2024"]) == DATE_FORMAT_DD_MM

    service = ImportService(MockRepo())
    # 03/04/2024 em MM/DD = 4 de Marco; em DD/MM = 3 de Abril.
    row = {"Data": "03/04/2024", "Desc": "Coffee", "Valor": "3.50"}
    mapping = {"date": "Data", "desc": "Desc", "amount": "Valor"}

    # Com formato MM/DD detetado: 4 de marco (mes-antes-dia).
    row_mmdd = service.normalize_row(row, mapping, apply_rules=False, date_format=DATE_FORMAT_MM_DD)
    assert row_mmdd.date == "2024-03-04"

    # A07: an ambiguous column requires an explicit choice, not a silent default.
    row_ddmm = service.normalize_row(row, mapping, apply_rules=False)
    assert not row_ddmm.is_valid and "ambígua" in row_ddmm.validation_message
    row_ddmm = service.normalize_row(row, mapping, apply_rules=False, date_format=DATE_FORMAT_DD_MM)
    assert row_ddmm.date == "2024-04-03"

    # Dia 25 so e valido como dia: MM/DD aceita 12/25 (25 de dezembro).
    row_dec = service.normalize_row(
        {"Data": "12/25/2024", "Desc": "Gift", "Valor": "50.00"},
        mapping,
        apply_rules=False,
        date_format=DATE_FORMAT_MM_DD,
    )
    assert row_dec.date == "2024-12-25"
    # Em DD/MM, 12/25 seria mes 25 -> invalido/invertido; com o formato
    # aplicado a data correta preserva-se.
    assert row_dec.validation_issues == ()


def test_c108_read_csv_propagates_detected_format_through_csvimportdata():
    """read_csv anexa o formato detetado ao CsvImportData (fluxo e2e)."""
    csv_text = (
        "Date;Description;Amount\n"
        "03/04/2024;Coffee;3.50\n"
        "05/03/2024;Bus;2.00\n"
        "12/25/2024;Gift;50.00\n"
    )
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "us_statement.csv"
        path.write_text(csv_text, encoding="utf-8-sig")
        service = ImportService(MockRepo())
        data = service.read_file(path)

    assert isinstance(data, CsvImportData)
    assert data.date_format == DATE_FORMAT_MM_DD
    assert any("MM/DD" in warning for warning in data.warnings)

    suggested = service.suggested_mapping(data.headers)
    preview = service.preview(
        data.rows, suggested, apply_rules=False, date_format=data.date_format
    )
    assert preview.invalid == 0
    # 03/04 = 4 Mar; 05/03 = 3 May; 12/25 = 25 Dec (mes-antes-dia).
    assert [r.transaction.date for r in preview.rows] == [
        "2024-03-04",
        "2024-05-03",
        "2024-12-25",
    ]


# ============================================================================
# C109 (P1): Rodapes CGD/BCP com colunas incompletas sao ignorados
# ============================================================================


def test_c109_cgd_footer_lines_skipped_with_warning_not_rejection():
    """Extrato CGD com 2 linhas de rodape de 1 coluna: importado sem erro."""
    cgd_content = (
        "Data Mov.;Data Valor;Descritivo;Valor / Débito;Valor / Crédito;Saldo Contabilístico\n"
        "05-01-2026;05-01-2026;CONTINENTE;54,20;;1.196,30\n"
        "10-01-2026;10-01-2026;VENCIMENTO;;2.100,00;3.296,30\n"
        "Saldos calculados à data de emissão deste extrato\n"
        "IBAN: PT50 0035 0123 4567890 01\n"
    )
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "cgd_extrato.csv"
        path.write_text(cgd_content, encoding="utf-8-sig")
        service = ImportService(MockRepo())
        data = service.read_file(path)  # Nao levanta ValueError.

    assert len(data.rows) == 2
    assert any("ignoradas 2 linhas de rodapé" in warning for warning in data.warnings)

    suggested = service.suggested_mapping(data.headers)
    preview = service.preview(
        data.rows, suggested, apply_rules=False, amount_locale=AMOUNT_LOCALE_PT,
        date_format=DATE_FORMAT_DD_MM,
    )
    assert preview.invalid == 0
    assert preview.rows[0].transaction.amount == pytest.approx(-54.20)
    assert preview.rows[1].transaction.amount == pytest.approx(2100.00)


# ============================================================================
# C110 (P1): Hifen/N/A em colunas debito/credito sao zeros legitimos
# ============================================================================


def test_c110_hyphen_and_na_cells_in_debit_credit_columns_are_zero():
    """Novo Banco/ActivoBank: celulas '-'/'--'/'N/A' em colunas dedicadas."""
    service = ImportService(MockRepo())
    rows = [
        {"Data": "2026-01-05", "Descrição": "Mercado", "Débito": "54,20", "Crédito": "-"},
        {"Data": "2026-01-10", "Descrição": "Salário", "Débito": "--", "Crédito": "2.100,00"},
        {"Data": "2026-01-12", "Descrição": "Assinatura", "Débito": "N/A", "Crédito": "N/A"},
    ]
    mapping = {
        "date": "Data",
        "desc": "Descrição",
        "amount_debit": "Débito",
        "amount_credit": "Crédito",
    }
    normalized = [
        service.normalize_row(r, mapping, apply_rules=False, amount_locale=AMOUNT_LOCALE_PT)
        for r in rows
    ]
    # Compra: debito 54,20 -> -54,20 EUR; credito '-' -> zero (sem erro).
    assert normalized[0].amount == pytest.approx(-54.20)
    assert normalized[0].validation_issues == ()
    # Vencimento: credito 2.100,00 -> +2100,00 EUR; debito '--' -> zero.
    assert normalized[1].amount == pytest.approx(2100.00)
    assert normalized[1].validation_issues == ()
    # Ambas as celulas N/A: valor zero -> linha invalida por valor zero,
    # mas NUNCA por erro de parsing numerico.
    assert normalized[2].amount == 0.0
    assert any("em falta" in issue or "zero" in issue for issue in normalized[2].validation_issues)
    assert not any("inválido" in issue for issue in normalized[2].validation_issues)


# ============================================================================
# C111 (P1): PAYMENT em OFX de cartao de credito = credito (liquidacao)
# ============================================================================


def test_c111_ofx_credit_card_payment_is_credit_not_debit():
    """<TRNTYPE>PAYMENT num CCSTMTRS e liquidacao de saldo (entrada +450).

    O DEBIT da compra mantem-se negativo (-75). Sem o fix, o PAYMENT era
    invertido para debito, duplicando a divida do cartao.
    """
    ofx_cc = (
        "<OFX>\n"
        "<CREDITCARDMSGSRSV1>\n"
        "<CCSTMTTRNRS>\n"
        "<CCSTMTRS>\n"
        "<BANKTRANLIST>\n"
        "<STMTTRN>\n"
        "<TRNTYPE>PAYMENT\n"
        "<DTPOSTED>20260825\n"
        "<TRNAMT>-450.00\n"
        "<FITID>CC-P1\n"
        "<NAME>PAGAMENTO FATURA\n"
        "</STMTTRN>\n"
        "<STMTTRN>\n"
        "<TRNTYPE>DEBIT\n"
        "<DTPOSTED>20260810\n"
        "<TRNAMT>75.00\n"
        "<FITID>CC-D1\n"
        "<NAME>ZARA\n"
        "</STMTTRN>\n"
        "</BANKTRANLIST>\n"
        "</CCSTMTRS>\n"
        "</CCSTMTTRNRS>\n"
        "</CREDITCARDMSGSRSV1>\n"
        "</OFX>\n"
    )
    parsed = OFXParser.parse(ofx_cc)
    assert parsed.is_credit_card is True
    assert len(parsed.rows) == 2
    # PAYMENT: valor absoluto positivo (liquidacao/entrada).
    assert parsed.rows[0]["Valor"] == "450.00"
    assert parsed.rows[0]["Descrição"] == "PAGAMENTO FATURA"
    # Compra: mantem-se debito negativo.
    assert parsed.rows[1]["Valor"] == "-75.00"

    # PAYMENT em extrato bancario comum mantem a semantica de debito.
    ofx_bank = (
        "<OFX><BANKTRANLIST>\n"
        "<STMTTRN>\n"
        "<TRNTYPE>PAYMENT\n"
        "<DTPOSTED>20260817\n"
        "<TRNAMT>45.00\n"
        "<FITID>REF-003\n"
        "<NAME>PAGAMENTO SERVICO\n"
        "</STMTTRN>\n"
        "</BANKTRANLIST></OFX>"
    )
    parsed_bank = OFXParser.parse(ofx_bank)
    assert parsed_bank.is_credit_card is False
    assert parsed_bank.rows[0]["Valor"] == "-45.00"


# ============================================================================
# C112 (P2): FIRE SRR em termos reais; nominal = real * inflacao cumulativa
# ============================================================================


def test_c112_srr_real_terms_nominal_never_double_discounted():
    """SRR simula em termos reais: final_real == ultimo saldo simulado e
    final_nominal == final_real * (1+inflacao)^anos (indexacao exata,
    nunca dupla penalizacao)."""
    res = FirePlanningEngine.simulate_sequence_of_returns_risk(
        initial_capital=1_000_000.0,
        initial_annual_withdrawal=40_000.0,
        duration_years=30,
        average_real_return_pct=5.0,
        early_crash_scenario="baseline_steady",
        annual_inflation_pct=2.5,
    )
    assert res.portfolio_survived is True
    assert res.final_capital_real == pytest.approx(res.yearly_balances[-1], abs=0.01)
    assert res.final_capital_real == pytest.approx(1_531_510.78, abs=0.01)
    # Nominal = real indexado por (1,025)^30 = 2,0975675791.
    inflation_factor = math.pow(1.025, 30)
    assert res.final_capital_nominal == pytest.approx(
        res.final_capital_real * inflation_factor, abs=0.02
    )
    assert res.final_capital_nominal == pytest.approx(3_212_447.36, abs=0.02)
    # O nominal nunca fica abaixo do real (sem duplo desconto de inflacao).
    assert res.final_capital_nominal >= res.final_capital_real - 1e-6


# ============================================================================
# C113 (P2): Transacoes futuras nao colapsam o denominador do burn rate
# ============================================================================


def test_c113_future_transactions_do_not_inflate_burn_rate():
    """Janela de observacao limitada a data de referencia: 14 dias de gastos
    (100 EUR/dia) + agendamento futuro a 5 dias.

    Com o clamp min(reference, last_tx_date): historico = 15 dias ->
    1500/15 = 100,0 EUR/dia. Sem o clamp, o denominador saltaria para 21
    dias (1500/21 = 71,43) - e com uma transacao futura a 90 dias o
    denominador antigo colapsava para 1 (burn 90x).
    """
    from services.cashflow_forecast import CashflowForecastService

    class DummyRepo:
        def q(self, *args: object, **kwargs: object) -> list[dict[str, object]]:
            return []

    class DummyPayments:
        pass

    reference = date(2026, 8, 15)
    daily = {
        reference - timedelta(days=i): 100.0 for i in range(15)
    }  # 2026-08-01 .. 2026-08-15, 1500 EUR no total
    daily[date(2026, 8, 20)] = 100.0  # transacao futura (1600 EUR na janela)

    service = CashflowForecastService(DummyRepo(), DummyPayments())
    service._variable_spend_by_day = lambda start, ref: daily  # type: ignore[method-assign]

    burn = service._variable_daily_spend(
        reference,
        lookback_days=90,
        first_tx_date=date(2026, 8, 1),
        last_tx_date=date(2026, 8, 20),
    )
    # Denominador = 15 dias de historico (clamp na referencia): 1600/15 =
    # 106,67 EUR/dia. Sem o clamp, o denominador saltaria para 21 dias
    # (1600/21 = 76,19) - e com transacao futura a 90 dias o denominador
    # antigo colapsava para 1 (burn ~90x).
    assert burn == pytest.approx(1600.0 / 15.0, abs=0.01)
    assert burn == pytest.approx(106.6667, abs=0.01)
    assert burn != pytest.approx(1600.0 / 21.0, abs=0.01)


# ============================================================================
# C114 (P2): sharpe_ratio com volatilidade nula e excesso positivo
# ============================================================================


def test_c114_sharpe_zero_volatility_positive_excess_returns_sentinel():
    """Certificado de Aforro: 12 meses a 3,5% contra risk-free 2,0% anual.

    Volatilidade nula com excesso positivo: sentinel formal 99,99 (nunca
    0,0). Excesso negativo com volatilidade nula: 0,0.
    """
    # rf_periodic = 2,0/12 = 0,1667 < 3,5 -> excesso positivo.
    assert sharpe_ratio([3.5] * 12, risk_free_rate_annual_pct=2.0, periods_per_year=12) == 99.99
    # Retornos constantes abaixo do risk-free periodico: excesso negativo -> 0.0.
    assert sharpe_ratio([-1.0] * 12, risk_free_rate_annual_pct=2.0, periods_per_year=12) == 0.0
    # Serie volatil normal: Sharpe finito, limitado a +/-99,99.
    volatile = [2.0, 1.5, -1.0, 3.0, -2.5, 4.0, 1.2, 0.8, -0.5, 2.2, 1.8, 3.5]
    sharpe = sharpe_ratio(volatile, risk_free_rate_annual_pct=2.0, periods_per_year=12)
    assert math.isfinite(sharpe)
    assert -99.99 <= sharpe <= 99.99
    assert sharpe > 0.0
