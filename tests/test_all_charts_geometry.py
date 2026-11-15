from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
from ui.core.theme import Theme


from ui.components.charts.balance import BalanceChart
from ui.components.charts.bar import MonthlyEvolutionChart, StackedExpenseChart
from ui.components.charts.budget import BudgetForecastChart
from ui.components.charts.cashflow import CashflowTimelineChart
from ui.components.charts.commitments import FixedCommitmentsChart
from ui.components.charts.diagnostics import (
    ForecastAccuracyChart,
    SpendingSeasonalityChart,
)
from ui.components.charts.financial_health import (
    ResilienceScenarioChart,
    ScoreTrendChart,
)
from ui.components.charts.financial_pulse import (
    CumulativeCashflowChart,
    SavingsEfficiencyChart,
)
from ui.components.charts.habit_plan import HabitPlanTrackingChart
from ui.components.charts.habit_simulation import HabitImpactChart
from ui.components.charts.heatmap import HeatmapChart
from ui.components.charts.investment import (
    InvestmentEvolutionChart,
    InvestmentProjectionChart,
)
from ui.components.charts.operational import (
    CashRunwayProbabilityChart,
    DataQualityTrendChart,
    RecurringCommitmentsTimelineChart,
)
from ui.components.charts.waterfall import CashFlowWaterfallChart
from ui.components.charts.wealth_bridge import NetWorthBridgeChart


def test_all_standard_charts_have_safe_bottom_margins():
    _qapp = QApplication.instance() or QApplication([])
    assert _qapp is not None
    theme = Theme()

    def tp():
        return theme

    # Check charts with plot_rect or _plot_rect
    test_specs = [
        (BalanceChart(tp), 48),
        (MonthlyEvolutionChart(tp), 48),
        (StackedExpenseChart(tp), 48),
        (CumulativeCashflowChart(tp), 48),
        (SavingsEfficiencyChart(tp), 48),
        (InvestmentEvolutionChart(tp), 48),
        (BudgetForecastChart(tp), 48),
        (CashflowTimelineChart(tp), 48),
        (FixedCommitmentsChart(tp), 48),
        (ForecastAccuracyChart(tp), 48),
        (SpendingSeasonalityChart(tp), 48),
        (ScoreTrendChart(tp), 48),
        (ResilienceScenarioChart(tp), 48),
        (CashRunwayProbabilityChart(tp), 48),
        (HabitPlanTrackingChart(tp), 48),
        (HabitImpactChart(tp), 48),
        (InvestmentProjectionChart(tp), 48),
        (DataQualityTrendChart(tp), 48),
        (RecurringCommitmentsTimelineChart(tp), 48),
        (CashFlowWaterfallChart(tp), 48),
        (NetWorthBridgeChart(tp), 48),
        (HeatmapChart(tp), 20),
    ]

    for chart, min_margin in test_specs:
        chart.resize(800, 400)
        margin = 0
        if hasattr(chart, "plot_rect"):
            _l, _t, _r, bottom = chart.plot_rect()
            margin = chart.height() - bottom
        elif hasattr(chart, "_plot_rect"):
            rect = chart._plot_rect()
            if isinstance(rect, tuple):
                _l, _t, _r, bottom = rect
                margin = chart.height() - bottom
            else:
                margin = chart.height() - rect.bottom()
        elif hasattr(chart, "_plot"):
            rect = chart._plot()
            margin = chart.height() - rect.bottom()

        if margin > 0:
            assert margin >= min_margin, (
                f"{chart.__class__.__name__} has bottom margin {margin} < {min_margin}"
            )
