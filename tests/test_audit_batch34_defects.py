#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Audit Batch 34 Tests: CIRS Tax Math, Loss Netting & Crisis Simulation Engine
=============================================================================
Guards against 8 defects identified in Council Audit Round 5:
- BUG-SIM-01: Asset prices never fall below 0.01 even under >100% drops.
- BUG-SIM-02: Recovery phase generation when recovery_months <= trough_month.
- BUG-SIM-03: O(N) running peak max drawdown calculation without slicing.
- BUG-SIM-04: Zero-capital / zero-savings edge case handling in backtest.
- BUG-MACRO-01: Historical CAPE percentile clamped to [0, 100].
- BUG-MACRO-02: Yield curve inversion triggers recession watch & higher liquidity.
- BUG-TAX-SIM-01: Exact cent arithmetic matching AT Modelo 3 Anexo G/J equation.
- BUG-TAX-SIM-02: Intra-sale loss netting pursuant to Art. 43. CIRS.
- BUG-TAX-SIM-03: Multi-asset symbol filtering and floating-point dust elimination.
"""

from __future__ import annotations

import math
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from motores_matematicos_simulacao.crisis_backtester_and_architecture_engine import (
    CrisisScenario,
    generate_market_path,
    run_timing_backtest,
    _safe_mdd,
)
from motores_matematicos_simulacao.portfolio_stress_and_tax_optimizer import (
    MacroCycleEngine,
    TaxLot,
    process_fifo_sale,
    summarize_tax_realization,
)


# ============================================================================
# 1. BUG-SIM-01: Prevention of Negative Asset Prices on Drops >= 100%
# ============================================================================

def test_c_sim_01_extreme_crash_drop_greater_than_100_pct_never_generates_negative_prices():
    """Drops with peak_to_trough_drop <= -1.0 clamp to -0.9999 and prices remain >= 0.01."""
    extreme_scenario = CrisisScenario(
        name="Catastrophic Total Wipeout",
        duration_months=12,
        trough_month=3,
        peak_to_trough_drop=-1.50,  # Extreme drop exceeding 100%
        recovery_months=8,
    )
    prices = generate_market_path(extreme_scenario)
    assert len(prices) > 0
    for p in prices:
        assert p >= 0.01, f"Price fell below 0.01: {p}"
        assert math.isfinite(p)


def test_c_sim_01_custom_prices_are_clamped_to_minimum():
    """Custom prices in CrisisScenario are sanitized to >= 0.01."""
    scenario = CrisisScenario(
        name="Custom With Zero",
        custom_prices=[-10.0, 0.0, 50.0, 100.0],
    )
    prices = generate_market_path(scenario)
    assert prices[0] == 0.01
    assert prices[1] == 0.01
    assert prices[2] == 50.0
    assert prices[3] == 100.0


# ============================================================================
# 2. BUG-SIM-02: Recovery Generation when recovery_months <= trough_month
# ============================================================================

def test_c_sim_02_recovery_months_less_than_trough_generates_full_path():
    """When recovery_months <= trough_month, rec_steps is >= 1 and duration is respected."""
    scenario = CrisisScenario(
        name="Flash Inversion",
        duration_months=12,
        trough_month=6,
        peak_to_trough_drop=-0.35,
        recovery_months=3,  # Less than trough_month
    )
    prices = generate_market_path(scenario)
    # Total points = duration_months + 1 (month 0 to 12)
    assert len(prices) == 13
    assert prices[0] == 100.0
    # Lowest point reached
    assert min(prices) < 100.0
    # Final price after recovery & post-recovery growth
    assert prices[-1] >= 100.0


def test_c_sim_02_recovery_longer_than_duration_extends_path():
    """When trough + recovery exceeds duration_months, total_months accommodates it."""
    scenario = CrisisScenario(
        name="Protracted L-Shape Recovery",
        duration_months=5,
        trough_month=3,
        peak_to_trough_drop=-0.40,
        recovery_months=10,
    )
    prices = generate_market_path(scenario)
    assert len(prices) >= 11  # At least 0..10


# ============================================================================
# 3. BUG-SIM-03: O(N) Max Drawdown Calculation without Slicing
# ============================================================================

def test_c_sim_03_safe_mdd_linear_complexity_and_correctness():
    """_safe_mdd correctly finds maximum peak-to-trough drop in O(N)."""
    assert _safe_mdd([]) == 0.0
    assert _safe_mdd([100.0]) == 0.0
    assert _safe_mdd([0.0, 0.0, 0.0]) == 0.0

    # 100 -> 70 (-30%) -> 100
    series = [100.0, 80.0, 70.0, 90.0, 100.0]
    assert _safe_mdd(series) == pytest.approx(-0.30, abs=1e-4)

    # 100 -> 50 (-50%) -> 150 -> 60 (-60% from 150)
    series_multi = [100.0, 50.0, 150.0, 60.0]
    assert _safe_mdd(series_multi) == pytest.approx(-0.60, abs=1e-4)


def test_c_sim_03_safe_mdd_large_series_performance():
    """_safe_mdd handles 10,000 points in milliseconds without O(N^2) memory or time lag."""
    large_series = [100.0 + (i % 50) for i in range(10000)]
    mdd = _safe_mdd(large_series)
    assert math.isfinite(mdd)
    assert mdd <= 0.0


# ============================================================================
# 4. BUG-SIM-04: Zero Savings and Zero Capital Backtest Handling
# ============================================================================

def test_c_sim_04_zero_capital_and_savings_produces_finite_metrics():
    """run_timing_backtest handles initial_capital=0 and monthly_savings=0 safely."""
    scenario = CrisisScenario(
        name="Covid-19 Mini Crash",
        duration_months=12,
        trough_month=2,
        peak_to_trough_drop=-0.30,
        recovery_months=6,
    )
    results = run_timing_backtest(scenario, initial_capital=0.0, monthly_savings=0.0)
    assert len(results) == 4
    for r in results:
        assert r.final_portfolio_value == 0.0
        assert r.total_invested == 0.0
        assert r.net_profit_pct == 0.0
        assert r.max_drawdown_pct == 0.0
        assert r.shares_accumulated == 0.0


# ============================================================================
# 5. BUG-MACRO-01: CAPE Historical Percentile Clamped to [0, 100]
# ============================================================================

def test_c_macro_01_negative_cape_percentile_clamped_to_zero():
    """Negative Shiller CAPE during severe corporate recessions clamps percentile to 0.0."""
    snap = MacroCycleEngine.evaluate_macro_regime(
        cape=-10.0,
        buffett_pct=70.0,
        spread_10y_2y=0.49,
        fear_greed=15.0,
    )
    assert snap.cape_historical_percentile == 0.0
    assert snap.shiller_cape == -10.0


def test_c_macro_01_extreme_high_cape_clamped_to_100():
    """Very high Shiller CAPE clamps percentile to 100.0."""
    snap = MacroCycleEngine.evaluate_macro_regime(
        cape=60.0,
        buffett_pct=250.0,
    )
    assert snap.cape_historical_percentile == 100.0


# ============================================================================
# 6. BUG-MACRO-02: Yield Curve Inversion (spread_10y_2y < 0) Triggers Warning
# ============================================================================

def test_c_macro_02_yield_curve_inversion_triggers_recession_watch():
    """Negative 10Y-2Y spread triggers recession watch and increases dry powder target."""
    snap_inverted = MacroCycleEngine.evaluate_macro_regime(
        cape=25.0,
        buffett_pct=130.0,
        spread_10y_2y=-0.45,  # Inverted yield curve
        fear_greed=50.0,
    )
    assert "Curva Invertida" in snap_inverted.market_phase or "Recession" in snap_inverted.market_phase
    assert snap_inverted.recommended_dry_powder_pct >= 0.20

    # Normal curve
    snap_normal = MacroCycleEngine.evaluate_macro_regime(
        cape=25.0,
        buffett_pct=130.0,
        spread_10y_2y=0.50,
        fear_greed=50.0,
    )
    assert "Fair Value" in snap_normal.market_phase
    assert snap_normal.recommended_dry_powder_pct == 0.10


# ============================================================================
# 7. BUG-TAX-SIM-01: Exact AT Invariant Arithmetic on Cent Rounding
# ============================================================================

def test_c_tax_sim_01_exact_cent_arithmetic_matches_at_validation():
    """taxable_profit strictly equals realized_value - acquisition_cost - expenses without 1-cent drift."""
    lots = [
        TaxLot(
            id=1,
            asset_symbol="VOO",
            buy_date=date(2025, 1, 15),
            quantity=33.333,
            buy_price=300.003,
            buy_fees=7.777,
        )
    ]
    _, realized = process_fifo_sale(
        lots,
        sell_date=date(2026, 2, 10),
        sell_quantity=15.123,
        sell_price=350.555,
        sell_fees=4.321,
    )
    assert len(realized) == 1
    ev = realized[0]
    expected_profit = round(ev.realized_value - ev.acquisition_cost - ev.expenses, 2)
    assert ev.taxable_profit == expected_profit, (
        f"AT invariant failed: {ev.realized_value} - {ev.acquisition_cost} - {ev.expenses} "
        f"= {expected_profit} != {ev.taxable_profit}"
    )


# ============================================================================
# 8. BUG-TAX-SIM-02: Intra-Sale Loss Netting (Art. 43. CIRS)
# ============================================================================

def test_c_tax_sim_02_loss_netting_across_lots_eliminates_tax_on_net_loss():
    """A sale with a winning lot and a larger losing lot produces zero estimated tax (Art. 43 CIRS)."""
    lot_gain = TaxLot(
        id=1,
        asset_symbol="VOO",
        buy_date=date(2025, 1, 10),
        quantity=10.0,
        buy_price=400.0,
        buy_fees=0.0,
        is_foreign=True,
    )
    lot_loss = TaxLot(
        id=2,
        asset_symbol="VOO",
        buy_date=date(2025, 3, 10),
        quantity=10.0,
        buy_price=600.0,
        buy_fees=0.0,
        is_foreign=True,
    )
    # Sell 20 units at 500:
    # Lot 1: 10 * 500 - 4000 = +1000 profit
    # Lot 2: 10 * 500 - 6000 = -1000 loss
    # Net profit: 0.0 EUR -> tax MUST be 0.0 EUR
    _, realized = process_fifo_sale(
        open_lots=[lot_gain, lot_loss],
        sell_date=date(2026, 1, 15),
        sell_quantity=20.0,
        sell_price=500.0,
        sell_fees=0.0,
    )
    assert len(realized) == 2
    assert realized[0].taxable_profit == 1000.0
    assert realized[1].taxable_profit == -1000.0
    total_tax = sum(r.estimated_tax for r in realized)
    assert total_tax == 0.0, f"Tax was charged on net zero gain: {total_tax}"


def test_c_tax_sim_02_partial_loss_netting_taxes_only_net_profit():
    """Net profit is taxed at 28% rather than the uncompensated gross gains."""
    lot_gain = TaxLot(
        id=1,
        asset_symbol="VOO",
        buy_date=date(2025, 1, 10),
        quantity=10.0,
        buy_price=400.0,
        buy_fees=0.0,
        is_foreign=True,
    )
    lot_loss = TaxLot(
        id=2,
        asset_symbol="VOO",
        buy_date=date(2025, 3, 10),
        quantity=10.0,
        buy_price=550.0,
        buy_fees=0.0,
        is_foreign=True,
    )
    # Sell 20 units at 500:
    # Lot 1: +1000 EUR
    # Lot 2: -500 EUR
    # Net profit: +500 EUR -> 28% of 500 = 140.00 EUR
    _, realized = process_fifo_sale(
        open_lots=[lot_gain, lot_loss],
        sell_date=date(2026, 1, 15),
        sell_quantity=20.0,
        sell_price=500.0,
        sell_fees=0.0,
    )
    total_tax = sum(r.estimated_tax for r in realized)
    assert total_tax == 140.00
    # Losing lot pays 0 tax
    assert realized[1].estimated_tax == 0.0
    # Winning lot pays the netted tax
    assert realized[0].estimated_tax == 140.00

    summary = summarize_tax_realization(realized)
    assert summary["net_taxable_profit"] == 500.0
    assert summary["estimated_tax"] == 140.0


# ============================================================================
# 9. BUG-TAX-SIM-03: Multi-Asset Filtering and Dust Lot Elimination
# ============================================================================

def test_c_tax_sim_03_multi_asset_symbol_filtering_preserves_other_lots():
    """Selling VOO does not consume AAPL lots even if AAPL was acquired earlier."""
    lot_aapl = TaxLot(
        id=1,
        asset_symbol="AAPL",
        buy_date=date(2024, 1, 1),
        quantity=50.0,
        buy_price=150.0,
        buy_fees=5.0,
    )
    lot_voo = TaxLot(
        id=2,
        asset_symbol="VOO",
        buy_date=date(2024, 6, 1),
        quantity=10.0,
        buy_price=450.0,
        buy_fees=5.0,
    )
    updated_lots, realized = process_fifo_sale(
        open_lots=[lot_aapl, lot_voo],
        sell_date=date(2025, 1, 1),
        sell_quantity=5.0,
        sell_price=500.0,
        sell_fees=5.0,
        asset_symbol="VOO",
    )
    assert len(realized) == 1
    assert realized[0].asset_symbol == "VOO"
    assert realized[0].quantity == 5.0

    # AAPL lot remains completely untouched
    aapl_lots = [lot for lot in updated_lots if lot.asset_symbol == "AAPL"]
    assert len(aapl_lots) == 1
    assert aapl_lots[0].quantity == 50.0

    # VOO has 5 units remaining
    voo_lots = [lot for lot in updated_lots if lot.asset_symbol == "VOO"]
    assert len(voo_lots) == 1
    assert voo_lots[0].quantity == 5.0


def test_c_tax_sim_03_floating_point_dust_lot_elimination():
    """Residual lots with negligible quantity (<= 1e-7) are pruned as floating-point dust."""
    lot = TaxLot(
        id=1,
        asset_symbol="BTC",
        buy_date=date(2024, 1, 1),
        quantity=1.0,
        buy_price=40000.0,
        buy_fees=5.0,
    )
    # Sell 0.9999999999 (difference < 1e-7)
    updated_lots, realized = process_fifo_sale(
        open_lots=[lot],
        sell_date=date(2025, 1, 1),
        sell_quantity=0.99999999,
        sell_price=60000.0,
        sell_fees=5.0,
    )
    assert len(realized) == 1
    # No lingering micro-dust lot with 1e-8 quantity created
    assert len(updated_lots) == 0
