"""Lazy public exports for application pages.

Importing ``ui.pages`` used to import every page, chart and dialog before the
main window could appear.  Keep the familiar public names while loading each
module only when that page is requested.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any


_EXPORTS = {
    "AIAssistantPage": ("ui.pages.ai", "AIAssistantPage"),
    "AssistantPage": ("ui.pages.ai", "AssistantPage"),
    "BudgetsPage": ("ui.pages.planning", "BudgetsPage"),
    "CompoundInterestPage": ("ui.pages.tools", "CompoundInterestPage"),
    "DataManagementPage": ("ui.pages.data_management", "DataManagementPage"),
    "DashboardPage": ("ui.pages.dashboard", "DashboardPage"),
    "DocumentsPage": ("ui.pages.core_pages", "DocumentsPage"),
    "DynamicAnalysisPage": ("ui.pages.dynamic_analysis", "DynamicAnalysisPage"),
    "FinanceCenterPage": ("ui.pages.core_pages", "FinanceCenterPage"),
    "GoalsPage": ("ui.pages.planning", "GoalsPage"),
    "HabitSimulationsPage": ("ui.pages.simulations", "HabitSimulationsPage"),
    "HealthPage": ("ui.pages.reports", "HealthPage"),
    "HistoryPage": ("ui.pages.history", "HistoryPage"),
    "MonthlyGoalsPage": ("ui.pages.planning", "MonthlyGoalsPage"),
    "MonthlyReviewPage": ("ui.pages.reports", "MonthlyReviewPage"),
    "MovementsPage": ("ui.pages.movements", "MovementsPage"),
    "MovementsTableModel": ("ui.models", "MovementsTableModel"),
    "PanoramaPage": ("ui.pages.panorama", "PanoramaPage"),
    "PdfReportsPage": ("ui.pages.reports", "PdfReportsPage"),
    "PlanningPage": ("ui.pages.planned_purchases", "PlanningPage"),
    "PortfolioPage": ("ui.pages.investments", "PortfolioPage"),
    "ReportsPage": ("ui.pages.reports", "ReportsPage"),
    "RiskResiliencePage": ("ui.pages.risk_resilience", "RiskResiliencePage"),
    "ScheduledPaymentsPage": ("ui.pages.planning", "ScheduledPaymentsPage"),
    "ScoreDetailsPage": ("ui.pages.reports", "ScoreDetailsPage"),
    "SettingsPage": ("ui.pages.settings", "SettingsPage"),
    "SimulatorPage": ("ui.pages.tools", "SimulatorPage"),
    "SmartImportPage": ("ui.pages.tools", "SmartImportPage"),
    "SplitsPage": ("ui.pages.tools", "SplitsPage"),
    "SubscriptionsPage": ("ui.pages.subscriptions", "SubscriptionsPage"),
}

__all__ = list(_EXPORTS)


def __getattr__(name: str) -> Any:
    try:
        module_name, attribute = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(name) from exc
    value = getattr(import_module(module_name), attribute)
    globals()[name] = value
    return value
