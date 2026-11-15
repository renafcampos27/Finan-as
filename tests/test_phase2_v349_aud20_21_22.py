"""Unit and integration tests for AUD-20, AUD-21, AUD-22, and AUD-23 fixes in v3.49.0."""

import math
from datetime import date
import pytest

from services.fire import FireService
from motores_matematicos_simulacao.portfolio_stress_and_tax_optimizer import (
    TaxLot,
    process_fifo_sale,
)
from motores_matematicos_simulacao.crisis_backtester_and_architecture_engine import (
    HISTORICAL_CRISES,
    Action,
    AppState,
    app_reducer,
    generate_market_path,
    run_timing_backtest,
)


def test_aud20_coast_fire_target_with_compound_discount():
    """Verify Coast FIRE calculates discounted target capital using compound growth."""
    res = FireService.calculate_fire_milestones(
        current_invested=100000.0,
        annual_expenses=12000.0,
        annual_savings=6000.0,
        real_growth_rate_pct=4.0,
        swr_pct=4.0,
        years_to_retirement=25.0,
    )
    std = res["standard"]
    coast = res["coast"]

    assert std.target_capital == 300000.0
    # Expected discount = (1.04)^25 = 2.66583633...
    expected_discount = math.pow(1.04, 25.0)
    expected_coast_target = round(300000.0 / expected_discount, 2)
    assert coast.target_capital == expected_coast_target
    assert coast.target_capital < std.target_capital
    assert "25" in coast.description


def test_aud20_coast_fire_zero_horizon_equals_standard_target():
    """Verify Coast FIRE with 0 years to retirement equals Standard FIRE target."""
    res = FireService.calculate_fire_milestones(
        current_invested=50000.0,
        annual_expenses=10000.0,
        annual_savings=5000.0,
        real_growth_rate_pct=5.0,
        swr_pct=4.0,
        years_to_retirement=0.0,
    )
    assert res["coast"].target_capital == res["standard"].target_capital


def test_aud21_tax_lot_unit_cost_zero_quantity_no_zero_division():
    """Verify TaxLot.unit_cost handles quantity <= 0 gracefully without ZeroDivisionError."""
    lot = TaxLot(
        id=1,
        asset_symbol="VWCE",
        buy_date=date(2025, 1, 15),
        quantity=0.0,
        buy_price=120.0,
        buy_fees=0.0,
    )
    assert lot.unit_cost == 120.0


def test_aud21_process_fifo_sale_rejects_non_positive_quantity():
    """Verify process_fifo_sale rejects sell_quantity <= 0 with ValueError."""
    lots = [
        TaxLot(
            id=1,
            asset_symbol="VOO",
            buy_date=date(2025, 1, 1),
            quantity=10.0,
            buy_price=500.0,
            buy_fees=5.0,
        )
    ]
    with pytest.raises(ValueError, match="estritamente superior a zero"):
        process_fifo_sale(lots, date(2026, 1, 1), sell_quantity=0.0, sell_price=550.0, sell_fees=5.0)

    with pytest.raises(ValueError, match="estritamente superior a zero"):
        process_fifo_sale(lots, date(2026, 1, 1), sell_quantity=-2.0, sell_price=550.0, sell_fees=5.0)


def test_aud21_process_fifo_sale_partial_and_leftover():
    """Verify process_fifo_sale consumes lots accurately and calculates leftover buy_fees safely."""
    lots = [
        TaxLot(
            id=1,
            asset_symbol="VOO",
            buy_date=date(2025, 1, 1),
            quantity=10.0,
            buy_price=500.0,
            buy_fees=10.0,
        )
    ]
    remaining_lots, realized = process_fifo_sale(
        lots, date(2026, 1, 1), sell_quantity=4.0, sell_price=600.0, sell_fees=4.0
    )
    assert len(remaining_lots) == 1
    assert remaining_lots[0].quantity == 6.0
    assert round(remaining_lots[0].buy_fees, 2) == 6.0
    assert len(realized) == 1
    assert realized[0].quantity == 4.0
    assert realized[0].realized_value == 2400.0


def test_aud22_crisis_backtester_normalized_strategy_contributions():
    """Verify all strategies receive consistent contribution counts over the scenario horizon."""
    scenario = HISTORICAL_CRISES[0]
    prices = generate_market_path(scenario)
    expected_contributions = len(prices) - 1

    results = run_timing_backtest(scenario, initial_capital=100000.0, monthly_savings=1000.0)
    assert len(results) == 4

    lump_sum = results[0]
    static_dca = results[1]

    expected_total_invested = 100000.0 + expected_contributions * 1000.0
    assert lump_sum.total_invested == expected_total_invested
    assert static_dca.total_invested == expected_total_invested
    assert static_dca.final_portfolio_value > 0.0
    assert static_dca.max_drawdown_pct < 0.0


def test_aud23_reducer_udf_clamps_dry_powder_to_available_cash():
    """Verify app_reducer prevents phantom equity creation when requested amount exceeds cash."""
    initial_state = AppState(
        portfolio_cash=1500.0,
        portfolio_equity=10000.0,
        market_drawdown=-0.25,
        active_scenario="Crash Test",
        tax_lots_count=5,
    )
    action = Action("DEPLOY_DRY_POWDER_TRANCHE", {"amount": 5000.0})
    new_state = app_reducer(initial_state, action)

    # Cash must decrement by 1500 (down to 0.0)
    assert new_state.portfolio_cash == 0.0
    # Equity must increment ONLY by 1500 (up to 11500.0), NOT by requested 5000
    assert new_state.portfolio_equity == 11500.0
    assert new_state.tax_lots_count == 6
