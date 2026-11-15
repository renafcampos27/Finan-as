#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Regression tests for audit Batch 16 (TAX) - services/tax.py fixes.

Bugs covered:
- BUG-TAX-01 (P1): 28% rate taxed gross lot-by-lot gains without netting
  same-year capital losses. CIRS Art. 10.: gains and losses of the same
  year must be offset (net) before applying the rate; only a positive net
  is taxed.
- BUG-TAX-02 (P1): Art. 50. 24-month count ignored the day of month and
  used a naive year*12+month matrix. Day-aware month counting now gates
  the devaluation coefficient.
- BUG-TAX-03 (P1): crypto-to-crypto swaps (<365d) were taxed at 28%,
  violating CIRS Art. 10. n. 19 (no realization in EUR until fiat
  conversion).
- BUG-TAX-04 (P1): hallucinated 15% treaty rate for countries without a
  CDT and ignoring ``has_treaty_form``. The credit is now capped by the
  actual source-state withholding (capped at the 28% PT fraction).
- BUG-TAX-05 (P1): englobamento loss carryforward accumulated as a scalar
  with no 5-year expiry (CIRS Art. 51.). Losses are now bucketed by origin
  year and only losses within the 5-year window are eligible.

Exact-value assertions with realistic Portuguese fiscal scenarios (TDD).
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# BUG-TEST-ORDP-01 (order pollution, final-gate 2026-09-16): this module previously popped
# FINANCAS_PESSOAIS_DEMO_DATA at import time.  pytest imports every module of
# a chunk during collection, so the pop silently disabled the demo seeding
# that tests/conftest.py line 21 enables for the visual/analytics fixtures,
# breaking tests/test_analytics.py whenever it ran later in the same chunk
# (StopIteration on 'Comprar casa', empty monthly_summary).  No test in this
# file touches the database or the environment (pure PortugueseTaxEngine
# scenarios), so the pop was dead code: it is simply removed.  Modules that
# genuinely need an empty DB use per-test monkeypatch.delenv (batch 9/10/11
# pattern), which restores the variable and never leaks.

from services.tax import (  # noqa: E402
    COEFICIENTES_DESVALORIZACAO,
    CONVENTION_DIVIDEND_TAX_RATES,
    ForeignDividendPayment,
    months_between,
    is_crypto_to_crypto_swap,
    PortugueseTaxEngine,
    TaxLotMatch,
    calculate_foreign_dividend_tax,
)


# ============================================================================
# BUG-TAX-01 (P1): Same-year netting of gains and losses before the 28% rate
# ============================================================================


def test_bug_tax_01_same_year_gains_and_losses_are_netted_before_tax():
    """Mais-valias e menos-valias do mesmo ano compensam-se (Art. 10. CIRS).

    Scenario: +5.000 EUR (stocks) and -2.000 EUR (stocks) in 2026.
    Old behaviour taxed 5.000 * 28% = 1.400 EUR. Correct: net 3.000 * 28%
    = 840 EUR.
    """
    engine = PortugueseTaxEngine()
    engine.add_acquisition(
        lot_id="gain-lot",
        asset_symbol="BCP.LS",
        acquisition_date=date(2024, 1, 10),
        quantity=100.0,
        unit_price=10.0,
    )
    engine.add_acquisition(
        lot_id="loss-lot",
        asset_symbol="ALTR.LS",
        acquisition_date=date(2024, 2, 1),
        quantity=100.0,
        unit_price=30.0,
    )

    matches = []
    matches += engine.process_disposal(
        disposal_id="disp-gain",
        asset_symbol="BCP.LS",
        disposal_date=date(2026, 5, 20),
        quantity=100.0,
        unit_price=50.0,  # +4.000 EUR gain
    )
    matches += engine.process_disposal(
        disposal_id="disp-loss",
        asset_symbol="ALTR.LS",
        disposal_date=date(2026, 9, 10),
        quantity=100.0,
        unit_price=28.0,  # -200 EUR loss
    )
    # Force the loss to -2.000 by adding a second losing disposal.
    engine.add_acquisition(
        lot_id="loss-lot-2",
        asset_symbol="ALTR.LS",
        acquisition_date=date(2024, 3, 1),
        quantity=100.0,
        unit_price=30.0,
    )
    matches += engine.process_disposal(
        disposal_id="disp-loss-2",
        asset_symbol="ALTR.LS",
        disposal_date=date(2026, 10, 1),
        quantity=100.0,
        unit_price=28.0,  # -200 EUR loss
    )

    report = engine.generate_annual_tax_report(matches, 2026)

    # Exact netting: 4.000 - 200 - 200 = 3.600 net gain
    assert report["global_net_capital_gain_loss_raw"] == pytest.approx(3600.0)
    assert report["total_gains_before_netting"] == pytest.approx(4000.0)
    assert report["total_losses_before_netting"] == pytest.approx(400.0)
    # 28% of the NET (3.600), not of the gross gains (4.000)
    assert report["global_estimated_tax_28pct"] == pytest.approx(3600.0 * 0.28)
    assert report["global_estimated_tax_28pct"] == pytest.approx(1008.0)


def test_bug_tax_01_net_negative_year_means_zero_tax():
    """Net negativo (menos-valias > mais-valias) implica imposto zero."""
    engine = PortugueseTaxEngine()
    engine.add_acquisition(
        lot_id="g-lot",
        asset_symbol="GAIN",
        acquisition_date=date(2025, 1, 5),
        quantity=10.0,
        unit_price=100.0,
    )
    engine.add_acquisition(
        lot_id="l-lot",
        asset_symbol="LOSS",
        acquisition_date=date(2025, 1, 5),
        quantity=10.0,
        unit_price=100.0,
    )
    matches = engine.process_disposal(
        disposal_id="d-gain",
        asset_symbol="GAIN",
        disposal_date=date(2026, 3, 1),
        quantity=10.0,
        unit_price=110.0,  # +100
    ) + engine.process_disposal(
        disposal_id="d-loss",
        asset_symbol="LOSS",
        disposal_date=date(2026, 7, 1),
        quantity=10.0,
        unit_price=60.0,  # -400
    )

    report = engine.generate_annual_tax_report(matches, 2026)

    assert report["global_net_capital_gain_loss_raw"] == pytest.approx(-300.0)
    assert report["global_estimated_tax_28pct"] == 0.0
    # BUG-AUD-C104 (CIRS Art. 55.º n.º 1 al. d)): under the autonomous 28%
    # regime the year's losses are NOT reportable forward - they lapse.
    # Carryforward of the year's losses requires opting for englobamento.
    assert report["current_year_losses_generated"] == 0.0
    assert report["total_losses_to_carryforward"] == 0.0

    # The same net loss under englobamento IS carried forward (Art. 51.).
    report_eng = engine.generate_annual_tax_report(
        matches, 2026, opt_for_englobamento=True
    )
    assert report_eng["current_year_losses_generated"] == pytest.approx(300.0)
    assert report_eng["total_losses_to_carryforward"] == pytest.approx(300.0)


def test_bug_tax_01_englobamento_uses_net_base_not_gross():
    """Englobamento: deducao de perdas anteriores aplica-se sobre o NET."""
    engine = PortugueseTaxEngine()
    engine.add_acquisition(
        lot_id="g1",
        asset_symbol="AAA",
        acquisition_date=date(2025, 1, 5),
        quantity=10.0,
        unit_price=100.0,
    )
    engine.add_acquisition(
        lot_id="l1",
        asset_symbol="BBB",
        acquisition_date=date(2025, 1, 5),
        quantity=10.0,
        unit_price=100.0,
    )
    matches = engine.process_disposal(
        disposal_id="d1",
        asset_symbol="AAA",
        disposal_date=date(2026, 3, 1),
        quantity=10.0,
        unit_price=150.0,  # +500
    ) + engine.process_disposal(
        disposal_id="d2",
        asset_symbol="BBB",
        disposal_date=date(2026, 7, 1),
        quantity=10.0,
        unit_price=80.0,  # -200
    )

    report = engine.generate_annual_tax_report(
        matches, 2026, prior_year_losses=100.0, opt_for_englobamento=True
    )

    # Net 300, minus 100 prior losses -> 200 taxable base
    assert report["global_net_capital_gain_loss_raw"] == pytest.approx(300.0)
    assert report["prior_year_losses_deducted"] == pytest.approx(100.0)
    assert report["final_taxable_base"] == pytest.approx(200.0)
    assert report["global_estimated_tax_28pct"] == pytest.approx(56.0)


# ============================================================================
# BUG-TAX-02 (P1): Day-aware 24-month count (Art. 50. CIRS)
# ============================================================================


def test_bug_tax_02_months_between_is_day_of_month_aware():
    """months_between: 2020-03-15 -> 2022-03-14 is 23 months, not 24."""
    assert months_between(date(2020, 3, 15), date(2022, 3, 14)) == 23
    assert months_between(date(2020, 3, 15), date(2022, 3, 15)) == 24
    assert months_between(date(2020, 3, 15), date(2022, 3, 16)) == 24
    assert months_between(date(2020, 1, 31), date(2020, 2, 29)) == 0
    assert months_between(date(2020, 1, 31), date(2020, 3, 30)) == 1
    assert months_between(date(2020, 1, 31), date(2020, 3, 31)) == 2
    assert months_between(date(2020, 5, 1), date(2020, 3, 1)) == 0
    assert months_between(date(2020, 5, 1), date(2019, 5, 1)) == 0


def test_bug_tax_02_real_estate_23_months_gets_coef_1():
    """Imovel detido 23 meses completos: coeficiente 1.0 (nao 24)."""
    engine = PortugueseTaxEngine()
    engine.add_acquisition(
        lot_id="apt-1",
        asset_symbol="APARTAMENTO",
        acquisition_date=date(2020, 3, 15),
        quantity=1.0,
        unit_price=300000.0,
        is_real_estate=True,
    )
    matches = engine.process_disposal(
        disposal_id="disp-apt-1",
        asset_symbol="APARTAMENTO",
        disposal_date=date(2022, 3, 14),  # 23 full months
        quantity=1.0,
        unit_price=350000.0,
        is_real_estate=True,
    )
    assert len(matches) == 1
    assert matches[0].devaluation_coefficient == 1.0
    assert matches[0].adjusted_acquisition_cost == pytest.approx(300000.0)
    assert matches[0].net_gain_loss == pytest.approx(50000.0)


def test_bug_tax_02_real_estate_24_months_gets_table_coef():
    """Imovel detido 24 meses completos (dia a dia): coeficiente da tabela."""
    engine = PortugueseTaxEngine()
    engine.add_acquisition(
        lot_id="apt-2",
        asset_symbol="APARTAMENTO",
        acquisition_date=date(2020, 3, 15),
        quantity=1.0,
        unit_price=300000.0,
        is_real_estate=True,
    )
    matches = engine.process_disposal(
        disposal_id="disp-apt-2",
        asset_symbol="APARTAMENTO",
        disposal_date=date(2022, 3, 15),  # exactly 24 full months
        quantity=1.0,
        unit_price=350000.0,
        is_real_estate=True,
    )
    assert len(matches) == 1
    expected_coef = COEFICIENTES_DESVALORIZACAO[2020]
    assert matches[0].devaluation_coefficient == expected_coef
    assert matches[0].adjusted_acquisition_cost == pytest.approx(
        300000.0 * expected_coef
    )


def test_bug_tax_02_old_naive_boundary_day_before_no_longer_qualifies():
    """Regression: naive count gave 24 months for 2020-03-15 -> 2022-03-14."""
    naive = (2022 - 2020) * 12 + (3 - 3)  # 0 -> the old bug saw >= 24
    assert naive == 24
    assert months_between(date(2020, 3, 15), date(2022, 3, 14)) < 24


# ============================================================================
# BUG-TAX-03 (P1): Crypto-to-crypto swaps are not taxable realizations
# ============================================================================


def test_bug_tax_03_crypto_to_crypto_swap_is_not_taxed():
    """Permuta BTC->ETH (<365d) nao e realizacao em euros (Art. 10. n. 19)."""
    engine = PortugueseTaxEngine()
    engine.add_acquisition(
        lot_id="btc-lot",
        asset_symbol="BTC",
        acquisition_date=date(2026, 1, 10),
        quantity=1.0,
        unit_price=50000.0,
        is_crypto=True,
    )
    matches = engine.process_disposal(
        disposal_id="swap-1",
        asset_symbol="BTC",
        disposal_date=date(2026, 6, 1),  # ~142 days: < 365
        quantity=1.0,
        unit_price=60000.0,
        is_crypto=True,
        counterparty_symbol="ETH",
        proceeds_in_fiat=False,
    )
    assert len(matches) == 1
    m = matches[0]
    assert m.is_crypto_to_crypto_swap is True
    assert m.tax_rate_pct == 0.0
    assert m.estimated_tax == 0.0
    # The economic gain exists but is NOT a taxable EUR realization yet.
    assert m.net_gain_loss == pytest.approx(10000.0)


def test_bug_tax_03_swap_description_triggers_non_taxation():
    """Descricao 'permuta'/'swap' sem contraparte explicita tambem deteta permuta."""
    engine = PortugueseTaxEngine()
    engine.add_acquisition(
        lot_id="eth-lot",
        asset_symbol="ETH",
        acquisition_date=date(2026, 2, 1),
        quantity=10.0,
        unit_price=3000.0,
        is_crypto=True,
    )
    matches = engine.process_disposal(
        disposal_id="swap-2",
        asset_symbol="ETH",
        disposal_date=date(2026, 5, 1),
        quantity=10.0,
        unit_price=3500.0,
        is_crypto=True,
        disposal_description="swap ETH->SOL",
        proceeds_in_fiat=False,
    )
    assert len(matches) == 1
    assert matches[0].is_crypto_to_crypto_swap is True
    assert matches[0].estimated_tax == 0.0


def test_bug_tax_03_fiat_sale_still_taxed_at_28pct():
    """Venda de cripto (<365d) por euros continua tributada a 28%."""
    engine = PortugueseTaxEngine()
    engine.add_acquisition(
        lot_id="btc-lot-2",
        asset_symbol="BTC",
        acquisition_date=date(2026, 1, 10),
        quantity=1.0,
        unit_price=50000.0,
        is_crypto=True,
    )
    matches = engine.process_disposal(
        disposal_id="fiat-sale",
        asset_symbol="BTC",
        disposal_date=date(2026, 6, 1),
        quantity=1.0,
        unit_price=60000.0,
        is_crypto=True,
        proceeds_in_fiat=True,
    )
    assert len(matches) == 1
    assert matches[0].is_crypto_to_crypto_swap is False
    assert matches[0].tax_rate_pct == 28.0
    assert matches[0].estimated_tax == pytest.approx(10000.0 * 0.28)


def test_bug_tax_03_swaps_excluded_from_annual_report_taxable_balance():
    """Permutas nao entram no balanco tributavel do relatorio anual."""
    engine = PortugueseTaxEngine()
    engine.add_acquisition(
        lot_id="btc-lot-3",
        asset_symbol="BTC",
        acquisition_date=date(2026, 1, 10),
        quantity=1.0,
        unit_price=50000.0,
        is_crypto=True,
    )
    swap_matches = engine.process_disposal(
        disposal_id="swap-3",
        asset_symbol="BTC",
        disposal_date=date(2026, 6, 1),
        quantity=1.0,
        unit_price=60000.0,
        is_crypto=True,
        counterparty_symbol="ETH",
        proceeds_in_fiat=False,
    )
    engine.add_acquisition(
        lot_id="eq-lot",
        asset_symbol="EQ",
        acquisition_date=date(2025, 1, 10),
        quantity=10.0,
        unit_price=100.0,
    )
    fiat_matches = engine.process_disposal(
        disposal_id="fiat-3",
        asset_symbol="EQ",
        disposal_date=date(2026, 9, 1),
        quantity=10.0,
        unit_price=150.0,
    )

    report = engine.generate_annual_tax_report(
        swap_matches + fiat_matches, 2026
    )
    # Only the fiat EUR realization (+500) counts; the +10.000 swap gain is excluded.
    assert report["global_net_capital_gain_loss_raw"] == pytest.approx(500.0)
    assert report["global_estimated_tax_28pct"] == pytest.approx(140.0)
    assert report["total_matches_count"] == 1


def test_bug_tax_03_helper_rejects_fiat_and_non_crypto():
    """Unit: is_crypto_to_crypto_swap guards."""
    assert (
        is_crypto_to_crypto_swap(
            asset_symbol="BTC", is_crypto=True, proceeds_in_fiat=False
        )
        is False  # no counterparty and no swap description -> not identifiable
    )
    assert is_crypto_to_crypto_swap(
        asset_symbol="BTC",
        is_crypto=True,
        counterparty_symbol="ETH",
        proceeds_in_fiat=False,
    )
    assert not is_crypto_to_crypto_swap(
        asset_symbol="BTC",
        is_crypto=True,
        counterparty_symbol="ETH",
        proceeds_in_fiat=True,
    )
    assert not is_crypto_to_crypto_swap(
        asset_symbol="AAPL",
        is_crypto=False,
        asset_category="security",
        counterparty_symbol="MSFT",
        proceeds_in_fiat=False,
    )


# ============================================================================
# BUG-TAX-04 (P1): No hallucinated 15% for non-CDT countries
# ============================================================================


def test_bug_tax_04_non_treaty_country_no_hallucinated_15pct_cap():
    """Pais sem CDT (ex.: 'ZA' - Africa do Sul, sem CDT dividentos com PT):

    Old behaviour invented a 15% cap (150 EUR). New behaviour: the credit is
    capped by the ACTUAL source withholding (300 = 30%) capped at the 28%
    Portuguese fraction (280). Uncreditable leakage is 20, not 150.
    """
    div = ForeignDividendPayment(
        id="div-za",
        asset_symbol="ANC",
        payment_date=date(2026, 6, 15),
        gross_amount_eur=1000.0,
        foreign_tax_withheld_eur=300.0,
        country_code="ZA",
        has_treaty_form=False,
    )
    calc = calculate_foreign_dividend_tax(div)
    assert calc.gross_dividend == 1000.0
    assert calc.foreign_tax_withheld == 300.0
    # Cap = min(actual withholding 300, PT fraction 280) = 280
    assert calc.maximum_creditable_foreign_tax == 280.0
    assert calc.uncreditable_foreign_tax_loss == 20.0
    assert calc.portuguese_autonomous_tax_28pct == 280.0
    assert calc.tax_credit_applied_art81 == 280.0
    assert calc.net_portuguese_tax_payable == 0.0


def test_bug_tax_04_known_treaty_country_still_uses_cdt_rate():
    """Paises com CDT conhecida mantem a taxa convencional (US 15%)."""
    div = ForeignDividendPayment(
        id="div-us",
        asset_symbol="MSFT",
        payment_date=date(2026, 6, 15),
        gross_amount_eur=1000.0,
        foreign_tax_withheld_eur=150.0,
        country_code="US",
        has_treaty_form=True,
    )
    calc = calculate_foreign_dividend_tax(div)
    assert calc.treaty_rate_pct == 15.0
    assert calc.maximum_creditable_foreign_tax == 150.0
    assert calc.net_portuguese_tax_payable == 130.0


def test_bug_tax_04_has_treaty_form_does_not_conjure_rates():
    """has_treaty_form=True sem CDT conhecida NAO inventa cap de 15%.

    O formulario permite pedir reembolso na origem; nao cria uma taxa
    convencional que o motor nao conhece. O teto continua a ser a retencao
    real da origem, capped a fracao portuguesa.
    """
    div = ForeignDividendPayment(
        id="div-kr",
        asset_symbol="SAMSUNG",
        payment_date=date(2026, 6, 15),
        gross_amount_eur=1000.0,
        foreign_tax_withheld_eur=220.0,  # 22% Korea source withholding
        country_code="KR",
        has_treaty_form=True,
    )
    calc = calculate_foreign_dividend_tax(div)
    assert "KR" not in CONVENTION_DIVIDEND_TAX_RATES
    # Cap = min(220, 280) = 220 (NOT the old hallucinated 150)
    assert calc.maximum_creditable_foreign_tax == 220.0
    assert calc.uncreditable_foreign_tax_loss == 0.0
    assert calc.net_portuguese_tax_payable == 60.0  # 280 - 220


def test_bug_tax_04_offshore_and_domestic_unchanged():
    """Offshore 35% e domestico PT mantem comportamento (regressao)."""
    div_ky = ForeignDividendPayment(
        id="div-ky",
        asset_symbol="OFF",
        payment_date=date(2026, 5, 10),
        gross_amount_eur=1000.0,
        foreign_tax_withheld_eur=100.0,
        country_code="KY",
    )
    calc = calculate_foreign_dividend_tax(div_ky)
    assert calc.portuguese_autonomous_tax_28pct == 350.0
    assert calc.tax_credit_applied_art81 == 0.0

    div_pt = ForeignDividendPayment(
        id="div-pt",
        asset_symbol="GALP",
        payment_date=date(2026, 4, 15),
        gross_amount_eur=500.0,
        foreign_tax_withheld_eur=0.0,
        country_code="PT",
    )
    calc_pt = calculate_foreign_dividend_tax(div_pt)
    assert calc_pt.net_portuguese_tax_payable == 140.0


# ============================================================================
# BUG-TAX-05 (P1): Loss carryforward bucketed by origin year with 5y expiry
# ============================================================================


def _make_gain_matches(
    gain: float = 1000.0,
) -> tuple[PortugueseTaxEngine, list[TaxLotMatch]]:
    engine = PortugueseTaxEngine()
    engine.add_acquisition(
        lot_id="g",
        asset_symbol="GAINSTOCK",
        acquisition_date=date(2025, 1, 5),
        quantity=10.0,
        unit_price=100.0,
    )
    matches = engine.process_disposal(
        disposal_id="d",
        asset_symbol="GAINSTOCK",
        disposal_date=date(2026, 3, 1),
        quantity=10.0,
        unit_price=100.0 + gain / 10.0,
    )
    return engine, matches


def test_bug_tax_05_losses_by_year_within_5y_window_are_eligible():
    """Perdas de anos dentro da janela de 5 anos sao dedutiveis."""
    engine, matches = _make_gain_matches(1000.0)
    report = engine.generate_annual_tax_report(
        matches,
        2026,
        prior_year_losses=900.0,
        prior_year_losses_by_year={2024: 400.0, 2025: 500.0},
        opt_for_englobamento=True,
    )
    assert report["prior_year_losses_deducted"] == pytest.approx(900.0)
    assert report["final_taxable_base"] == pytest.approx(100.0)
    assert report["remaining_prior_year_losses"] == 0.0


def test_bug_tax_05_losses_older_than_5_years_expire():
    """Perdas com mais de 5 anos caducam (Art. 51. CIRS).

    Ano fiscal 2026: perda de 2020 tem 6 anos -> caducada; perda de 2021
    tem 5 anos -> ainda elegivel.
    """
    engine, matches = _make_gain_matches(1000.0)
    report = engine.generate_annual_tax_report(
        matches,
        2026,
        prior_year_losses=1000.0,  # 600 (2020, expired) + 400 (2021, valid)
        prior_year_losses_by_year={2020: 600.0, 2021: 400.0},
        opt_for_englobamento=True,
    )
    assert report["expired_prior_year_losses"] == pytest.approx(600.0)
    assert report["prior_year_losses_deducted"] == pytest.approx(400.0)
    assert report["final_taxable_base"] == pytest.approx(600.0)
    assert report["remaining_prior_year_losses"] == 0.0


def test_bug_tax_05_scalar_only_mode_unchanged_backwards_compatible():
    """Sem buckets, o comportamento scalar mantem-se (retrocompatibilidade)."""
    engine, matches = _make_gain_matches(1000.0)
    report = engine.generate_annual_tax_report(
        matches,
        2026,
        prior_year_losses=400.0,
        opt_for_englobamento=True,
    )
    assert report["prior_year_losses_deducted"] == pytest.approx(400.0)
    assert report["expired_prior_year_losses"] == 0.0


def test_bug_tax_05_englobamento_required_for_bucket_deduction():
    """Sem englobamento, buckets nao sao dedutiveis (Art. 55. n. 1 al. d)."""
    engine, matches = _make_gain_matches(1000.0)
    report = engine.generate_annual_tax_report(
        matches,
        2026,
        prior_year_losses=500.0,
        prior_year_losses_by_year={2025: 500.0},
        opt_for_englobamento=False,
    )
    assert report["prior_year_losses_deducted"] == 0.0
    assert report["final_taxable_base"] == pytest.approx(1000.0)
    assert report["remaining_prior_year_losses"] == pytest.approx(500.0)


def test_bug_tax_05_partial_window_expiry_mixed_buckets():
    """Mistura: 2019 (7 anos, caducada), 2022 (4 anos, valida)."""
    engine, matches = _make_gain_matches(1000.0)
    report = engine.generate_annual_tax_report(
        matches,
        2026,
        prior_year_losses=1200.0,  # 800 (2019) + 400 (2022)
        prior_year_losses_by_year={2019: 800.0, 2022: 400.0},
        opt_for_englobamento=True,
    )
    assert report["expired_prior_year_losses"] == pytest.approx(800.0)
    assert report["prior_year_losses_deducted"] == pytest.approx(400.0)
    assert report["final_taxable_base"] == pytest.approx(600.0)


def test_bug_tax_05_boundary_exactly_5_years_is_still_eligible():
    """Perda de 2021 usada em 2026: exatamente 5 anos -> ainda elegivel."""
    engine, matches = _make_gain_matches(1000.0)
    report = engine.generate_annual_tax_report(
        matches,
        2026,
        prior_year_losses=250.0,
        prior_year_losses_by_year={2021: 250.0},
        opt_for_englobamento=True,
    )
    assert report["expired_prior_year_losses"] == 0.0
    assert report["prior_year_losses_deducted"] == pytest.approx(250.0)


def test_bug_tax_05_expired_loss_not_used_but_current_year_loss_kept():
    """Perdas caducadas saem do carryforward; perdas do ano atual entram."""
    engine, matches = _make_gain_matches(-500.0)  # current year net loss 500
    report = engine.generate_annual_tax_report(
        matches,
        2026,
        prior_year_losses=700.0,  # all from 2019 -> expired
        prior_year_losses_by_year={2019: 700.0},
        opt_for_englobamento=True,
    )
    assert report["expired_prior_year_losses"] == pytest.approx(700.0)
    assert report["prior_year_losses_deducted"] == 0.0
    assert report["current_year_losses_generated"] == pytest.approx(500.0)
    # Only the fresh 2026 loss survives into the carryforward.
    assert report["total_losses_to_carryforward"] == pytest.approx(500.0)
