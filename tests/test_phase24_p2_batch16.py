"""
Unit tests for Phase 24 / Batch 16 P2 Defects
=============================================
Defects tested:
1. TAX-07: Bloqueio de isenção de criptoativos (>365 dias) para security tokens e offshores (Art. 10.º n.º 1 k) CIRS)
2. MATH-02: Proteção contra OverflowError em potências longas e taxas extremas (finance_math.py)
3. MATH-03: Cornish-Fisher VaR com retornos constantes negativos retorna perda certa positiva
4. MATH-04: Modified Dietz com denominador <= 0 (levantamentos massivos) retorna 0.0 sem inversão de sinal
"""

from datetime import date
import math

from services.tax import (
    PortugueseTaxEngine,
)
from motores_matematicos_simulacao.portfolio_stress_and_tax_optimizer import (
    TaxLot,
    process_fifo_sale,
)
from services.finance_math import (
    cornish_fisher_var,
    modified_dietz,
    periodic_discount_factor,
    present_value_of_contributions,
    effective_annual_to_periodic_rate,
    cagr,
    twrr,
)


# ============================================================================
# 1. TAX-07 (Crypto exemption restrictions: security tokens & offshores)
# ============================================================================

def test_tax_07_regular_crypto_held_over_365_days_is_exempt():
    """Cripto regular em Portugal detida por mais de 365 dias é isenta de IRS."""
    engine = PortugueseTaxEngine()
    engine.add_acquisition(
        lot_id="acq-btc-1",
        asset_symbol="BTC",
        asset_name="Bitcoin",
        acquisition_date=date(2023, 1, 1),
        quantity=1.5,
        unit_price=20000.0,
        fees=10.0,
        is_crypto=True,
        country_code="PT",
    )

    matches = engine.process_disposal(
        disposal_id="disp-btc-1",
        asset_symbol="BTC",
        disposal_date=date(2025, 2, 1),
        quantity=1.0,
        unit_price=50000.0,
        fees=15.0,
        is_crypto=True,
        country_code="PT",
    )

    assert len(matches) == 1
    m = matches[0]
    assert m.is_exempt_crypto is True
    assert m.tax_rate_pct == 0.0
    assert m.estimated_tax == 0.0
    assert m.tax_schedule == "Anexo G"


def test_tax_07_security_token_held_over_365_days_is_not_exempt():
    """Security tokens (>365 dias) não beneficiam de isenção de criptoativos (Art. 10.º n.º 1 k) CIRS)."""
    engine = PortugueseTaxEngine()
    # 1. Via is_security_token=True
    engine.add_acquisition(
        lot_id="acq-sec-1",
        asset_symbol="TOKEN_X",
        asset_name="Security Token X",
        acquisition_date=date(2023, 1, 1),
        quantity=100.0,
        unit_price=10.0,
        fees=5.0,
        is_crypto=True,
        is_security_token=True,
        country_code="PT",
    )
    matches = engine.process_disposal(
        disposal_id="disp-sec-1",
        asset_symbol="TOKEN_X",
        disposal_date=date(2025, 2, 1),
        quantity=50.0,
        unit_price=20.0,
        fees=5.0,
        is_crypto=True,
        country_code="PT",
    )
    assert len(matches) == 1
    m = matches[0]
    assert m.is_exempt_crypto is False
    assert m.tax_rate_pct == 28.0
    assert m.estimated_tax > 0.0

    # 2. Via asset_category="security_token"
    engine.add_acquisition(
        lot_id="acq-sec-2",
        asset_symbol="TOKEN_Y",
        asset_name="Token Mobiliario Y",
        acquisition_date=date(2023, 1, 1),
        quantity=100.0,
        unit_price=10.0,
        fees=5.0,
        is_crypto=True,
        asset_category="token_mobiliario",
        country_code="PT",
    )
    matches_y = engine.process_disposal(
        disposal_id="disp-sec-2",
        asset_symbol="TOKEN_Y",
        disposal_date=date(2025, 2, 1),
        quantity=50.0,
        unit_price=25.0,
        fees=5.0,
        is_crypto=True,
        country_code="PT",
    )
    assert len(matches_y) == 1
    assert matches_y[0].is_exempt_crypto is False
    assert matches_y[0].tax_rate_pct == 28.0


def test_tax_07_offshore_crypto_held_over_365_days_taxed_at_35pct_anexo_j():
    """Criptoativos detidos em offshores/paraísos fiscais são tributados à taxa agravada de 35% e vão para o Anexo J."""
    engine = PortugueseTaxEngine()
    engine.add_acquisition(
        lot_id="acq-btc-ky",
        asset_symbol="BTC_KY",
        asset_name="Bitcoin Cayman",
        acquisition_date=date(2023, 1, 1),
        quantity=1.0,
        unit_price=20000.0,
        fees=10.0,
        is_crypto=True,
        country_code="KY",  # Cayman Islands (offshore)
    )

    matches = engine.process_disposal(
        disposal_id="disp-btc-ky",
        asset_symbol="BTC_KY",
        disposal_date=date(2025, 2, 1),
        quantity=1.0,
        unit_price=60000.0,
        fees=20.0,
        is_crypto=True,
        country_code="KY",
    )

    assert len(matches) == 1
    m = matches[0]
    assert m.is_exempt_crypto is False
    assert m.tax_rate_pct == 35.0
    assert m.tax_schedule == "Anexo J"
    assert m.estimated_tax > 0.0


def test_tax_07_process_fifo_sale_optimizer_blocks_exemption_for_security_and_offshore():
    """No motor de simulação fiscal, security tokens e offshores são tributados e devidamente enquadrados."""
    lot_sec = TaxLot(
        id=1,
        asset_symbol="SEC1",
        buy_date=date(2023, 1, 1),
        quantity=10.0,
        buy_price=50.0,
        buy_fees=2.0,
        is_security_token=True,
        country_code="PT",
    )
    lot_off = TaxLot(
        id=2,
        asset_symbol="BTC_OFF",
        buy_date=date(2023, 1, 1),
        quantity=1.0,
        buy_price=20000.0,
        buy_fees=10.0,
        is_security_token=False,
        country_code="VG",  # British Virgin Islands (offshore)
    )

    # 1. Venda de security token
    _, rel_sec = process_fifo_sale(
        open_lots=[lot_sec],
        sell_date=date(2025, 1, 1),
        sell_quantity=10.0,
        sell_price=100.0,
        sell_fees=5.0,
        is_crypto=True,
    )
    assert len(rel_sec) == 1
    assert rel_sec[0].is_exempt is False
    assert rel_sec[0].tax_rate_pct == 28.0

    # 2. Venda de ativo em jurisdição offshore
    _, rel_off = process_fifo_sale(
        open_lots=[lot_off],
        sell_date=date(2025, 1, 1),
        sell_quantity=1.0,
        sell_price=50000.0,
        sell_fees=15.0,
        is_crypto=True,
    )
    assert len(rel_off) == 1
    assert rel_off[0].is_exempt is False
    assert rel_off[0].tax_rate_pct == 35.0
    assert rel_off[0].irs_annex == "Anexo J (Estrangeiro)"


# ============================================================================
# 2. MATH-02 (Overflow and math stability in powers and compounding)
# ============================================================================

def test_math_02_periodic_discount_factor_extreme_rates_and_periods():
    """periodic_discount_factor nunca lança OverflowError mesmo com taxas e períodos exorbitantes."""
    # Períodos extremos e taxas elevadas
    val1 = periodic_discount_factor(annual_rate_pct=1e9, period_index=50000)
    assert math.isfinite(val1)
    assert val1 <= 1e12

    # Taxa negativa extrema (-100%)
    val2 = periodic_discount_factor(annual_rate_pct=-100.0, period_index=12)
    assert math.isfinite(val2)
    assert val2 >= 0.0

    # Período negativo ou zero
    val3 = periodic_discount_factor(annual_rate_pct=5.0, period_index=-5)
    assert math.isfinite(val3)
    assert val3 == 1.0


def test_math_02_present_value_of_contributions_extreme_inflation():
    """present_value_of_contributions lida com hiperinflação sem estourar OverflowError."""
    pv1 = present_value_of_contributions(amount=100.0, periods=50000, annual_inflation_pct=1e6)
    assert math.isfinite(pv1)
    assert pv1 >= 0.0

    # Inflação zero
    pv2 = present_value_of_contributions(amount=50.0, periods=12, annual_inflation_pct=0.0)
    assert pv2 == 600.0

    # Contribuição ou períodos nulos
    assert present_value_of_contributions(amount=0.0, periods=12, annual_inflation_pct=2.0) == 0.0
    assert present_value_of_contributions(amount=100.0, periods=0, annual_inflation_pct=2.0) == 0.0


def test_math_02_effective_rate_and_cagr_overflow_guards():
    """effective_annual_to_periodic_rate e cagr não quebram com inputs anómalos."""
    rate = effective_annual_to_periodic_rate(annual_rate_pct=1e15, periods_per_year=12)
    assert math.isfinite(rate)

    # CAGR com duração zero ou negativa
    assert cagr(100.0, 200.0, 0.0) == 0.0
    assert cagr(100.0, 200.0, -2.0) == 0.0
    # CAGR com rácios astronómicos
    val_cagr = cagr(1.0, 1e20, 0.001)
    assert math.isfinite(val_cagr)

    # TWRR com retornos infinitos/astronómicos
    t_ret = twrr([1e6, 1e6, 1e6])
    assert math.isfinite(t_ret)


# ============================================================================
# 3. MATH-03 (Cornish-Fisher VaR with constant negative returns)
# ============================================================================

def test_math_03_constant_negative_returns_yields_positive_certain_loss():
    """Série com perdas constantes (volatilidade 0) deve reportar VaR positivo da perda certa."""
    returns = [-0.02, -0.02, -0.02, -0.02, -0.02]
    var_val = cornish_fisher_var(returns, confidence=0.95)
    assert var_val == 0.02

    # Retornos constantes em percentagem direta (-2.5%)
    returns_pct = [-2.5, -2.5, -2.5, -2.5]
    var_pct = cornish_fisher_var(returns_pct, confidence=0.99)
    assert var_pct == 2.5


def test_math_03_constant_positive_returns_yields_zero_var():
    """Série com ganhos constantes tem risco de cauda nulo (VaR = 0.0)."""
    returns = [0.03, 0.03, 0.03, 0.03]
    var_val = cornish_fisher_var(returns, confidence=0.95)
    assert var_val == 0.0


def test_math_03_robust_to_nan_inf_and_insufficient_samples():
    """cornish_fisher_var filtra nan/inf e trata amostras insuficientes (< 4)."""
    assert cornish_fisher_var([0.01, -0.02]) == 0.0
    assert cornish_fisher_var([]) == 0.0

    # Com inf/nan misturados
    returns_dirty = [-0.02, -0.02, -0.02, -0.02, float("nan"), float("inf")]
    var_val = cornish_fisher_var(returns_dirty, confidence=0.95)
    assert var_val == 0.02


# ============================================================================
# 4. MATH-04 (Modified Dietz with zero or negative denominator)
# ============================================================================

def test_math_04_modified_dietz_with_excessive_withdrawals_returns_zero():
    """Quando levantamentos maciços tornam o denominador <= 0, Modified Dietz retorna 0.0 em vez de inverter sinal."""
    flows = [("2025-01-02", -1500.0)]
    ret = modified_dietz(
        start_value=1000.0,
        end_value=200.0,
        cash_flows=flows,
        start_date="2025-01-01",
        end_date="2025-01-31",
    )
    assert ret == 0.0


def test_math_04_modified_dietz_normalizes_inverted_dates_and_handles_nan():
    """Modified Dietz normaliza datas invertidas e ignora montantes não finitos."""
    # Data de fim anterior à de início
    flows = [("2025-01-15", 500.0), ("2025-01-16", float("nan")), ("2025-01-17", float("inf"))]
    ret = modified_dietz(
        start_value=1000.0,
        end_value=1600.0,
        cash_flows=flows,
        start_date="2025-01-31",
        end_date="2025-01-01",
    )
    assert math.isfinite(ret)
    assert ret > 0.0


def test_math_04_modified_dietz_zero_portfolio_returns_zero():
    """Modified Dietz com portfólio a zero e sem fluxos retorna 0.0."""
    assert modified_dietz(0.0, 0.0, [], "2025-01-01", "2025-01-31") == 0.0
