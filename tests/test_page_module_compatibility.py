from __future__ import annotations

from ui.pages.history import HistoryPage as ExtractedHistoryPage
from ui.pages.investments import PortfolioPage, SubscriptionsPage
from ui.pages.planned_purchases import PlanningPage as ExtractedPlanningPage
from ui.pages.planning import BudgetsPage, PlanningPage
from ui.pages.reports import HistoryPage, ReportsPage
from ui.pages.subscriptions import SubscriptionsPage as ExtractedSubscriptionsPage


def test_extracted_pages_keep_legacy_import_paths() -> None:
    assert SubscriptionsPage is ExtractedSubscriptionsPage
    assert PlanningPage is ExtractedPlanningPage
    assert HistoryPage is ExtractedHistoryPage


def test_primary_page_classes_remain_in_original_modules() -> None:
    assert PortfolioPage.__module__ == "ui.pages.investments"
    assert BudgetsPage.__module__ == "ui.pages.planning"
    assert ReportsPage.__module__ == "ui.pages.reports"
