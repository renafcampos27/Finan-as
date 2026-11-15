from __future__ import annotations

import pytest
from PySide6.QtWidgets import QApplication

from services.investment_performance import (
    portfolio_performance_attribution,
)
from ui.components import PortfolioContributionChart
from ui.core import Theme


def test_portfolio_attribution_reconciles_weighted_return_and_fees() -> None:
    result = portfolio_performance_attribution(
        [
            {
                "id": 1,
                "name": "ETF global",
                "asset_class": "ETF",
                "account": "Corretora",
                "value": 1_200,
                "cost_basis": 1_000,
                "fees": 20,
            },
            {
                "id": 2,
                "name": "Ação individual",
                "asset_class": "Ações",
                "account": "Corretora",
                "value": 900,
                "cost_basis": 1_000,
                "fees": 10,
            },
        ]
    )

    assert result["total_value"] == pytest.approx(2_100)
    assert result["total_cost_basis"] == pytest.approx(2_000)
    assert result["total_fees"] == pytest.approx(30)
    assert result["total_cash_invested"] == pytest.approx(2_030)
    assert result["gross_gain"] == pytest.approx(100)
    assert result["net_gain"] == pytest.approx(70)
    assert result["gross_return_pct"] == pytest.approx(5.0)
    assert result["net_return_pct"] == pytest.approx(70 / 2_030 * 100)
    assert result["fee_impact_pp"] == pytest.approx(
        result["gross_return_pct"] - result["net_return_pct"]
    )
    assert sum(row["contribution_pp"] for row in result["rows"]) == pytest.approx(
        result["net_return_pct"]
    )
    assert result["contribution_total_pp"] == pytest.approx(result["net_return_pct"])


def test_zero_fees_preserve_the_previous_portfolio_return() -> None:
    result = portfolio_performance_attribution(
        [
            {
                "id": 1,
                "name": "ETF",
                "value": 1_250,
                "cost_basis": 1_000,
            }
        ]
    )

    assert result["total_fees"] == 0
    assert result["gross_gain"] == result["net_gain"] == 250
    assert result["gross_return_pct"] == result["net_return_pct"] == 25
    assert result["fee_impact_pp"] == 0


def test_portfolio_contribution_chart_renders_every_holding() -> None:
    app = QApplication.instance() or QApplication([])
    data = portfolio_performance_attribution(
        [
            {
                "id": 1,
                "name": "ETF global",
                "asset_class": "ETF",
                "value": 1_250,
                "cost_basis": 1_000,
                "fees": 12,
            },
            {
                "id": 2,
                "name": "Obrigações",
                "asset_class": "Obrigações",
                "value": 950,
                "cost_basis": 1_000,
                "fees": 5,
            },
        ]
    )
    chart = PortfolioContributionChart(lambda: Theme(dark=False))
    chart.resize(1_080, 360)
    chart.set_data(data)
    chart.show()
    app.processEvents()

    assert not chart.grab().isNull()
    assert chart._paint_error == ""
    assert len(chart._row_rects) == 2
    assert "Rentabilidade líquida ponderada" in (chart.accessibleDescription())
    chart.close()


def test_zero_cost_basis_does_not_produce_fictional_return() -> None:
    result = portfolio_performance_attribution(
        [
            {
                "id": 1,
                "name": "Airdrop / Recompensa Cripto",
                "asset_class": "Cripto",
                "value": 500,
                "cost_basis": 0,
                "cost_basis_known": 1,
                "fees": 0,
            },
            {
                "id": 2,
                "name": "ETF S&P 500",
                "asset_class": "ETF",
                "value": 1_200,
                "cost_basis": 1_000,
                "cost_basis_known": 1,
                "fees": 0,
            },
        ]
    )

    assert result["total_value"] == 1_700
    assert result["total_cost_basis"] == 1_000
    assert result["total_cash_invested"] == 1_000
    assert result["gross_gain"] == 200.0
    assert result["net_gain"] == 200.0
    assert result["gross_return_pct"] == 20.0
    assert result["net_return_pct"] == 20.0
    airdrop_row = next(r for r in result["rows"] if r["id"] == 1)
    assert airdrop_row["performance_available"] is False
    assert airdrop_row["gross_return_pct"] is None
    assert airdrop_row["net_return_pct"] is None
    assert airdrop_row["contribution_pp"] is None
