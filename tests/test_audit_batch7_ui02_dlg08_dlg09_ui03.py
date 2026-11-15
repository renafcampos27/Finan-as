#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Regression tests for audit batch 7 (BUG-UI-02, BUG-DLG-08, BUG-DLG-09,
BUG-UI-03):

- BUG-UI-02 (P1): BudgetsPage.edit_selected guards a None table item instead
  of crashing with AttributeError when the current row has no item.
- BUG-DLG-08 (P1): BudgetEditDialog.accept() accepts 0.00 (budget removal)
  and only rejects negative amounts.
- BUG-DLG-09 (P1): BudgetAssistantDialog.get_plan() keeps categories whose
  value uses a dot decimal ("250.50") as well as comma decimals ("250,50").
- BUG-UI-03 (P1): selected_id() on PortfolioPage and ScheduledPaymentsPage
  returns None for empty selection / non-numeric item text instead of
  raising ValueError.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from unittest.mock import MagicMock, patch

import pytest
from PySide6.QtWidgets import QApplication, QDialog, QTableWidget, QWidget

from ui.dialogs import BudgetAssistantDialog, BudgetEditDialog
from ui.pages.investments import PortfolioPage
from ui.pages.planning import BudgetsPage, ScheduledPaymentsPage
from utils.helpers import parse_dot_decimal


@pytest.fixture(scope="session")
def qapp():
    return QApplication.instance() or QApplication([])


class _MockRepo:
    """Repo stub exposing exactly the surface used by the pages/dialogs."""

    def __init__(self) -> None:
        self._categories = [
            "Alimentação",
            "Habitação",
            "Transportes",
            "Lazer",
            "Restaurantes",
            "Saúde",
        ]
        self.budgets: dict[tuple[str, str], float] = {}

    # --- surface used by BudgetAssistantDialog ---
    def categories(self, kind: str = "all") -> list[str]:
        return list(self._categories)

    def category_rows(self, kind: str = "expense") -> list[dict]:
        buckets = {
            "Alimentação": "Necessidades",
            "Habitação": "Necessidades",
            "Transportes": "Necessidades",
            "Saúde": "Necessidades",
            "Lazer": "Desejos",
            "Restaurantes": "Desejos",
        }
        return [
            {"name": name, "kind": "expense", "budget_bucket": bucket}
            for name, bucket in buckets.items()
        ]

    def month_period(self, year: int, month: int):
        return (
            f"{year:04d}-{month:02d}-01",
            f"{year:04d}-{month:02d}-28",
            f"{year:04d}-{month:02d}",
        )

    def totals_for_payment_period(self, start: str, end: str) -> dict:
        return {"income": 2000.0, "expenses": 1200.0}

    # --- settings surface used by page construction ---
    def setting(self, key: str, default: str = "") -> str:
        return str(default)

    def set_setting(self, key: str, value: str) -> None:
        pass

    # --- surface used by BudgetsPage ---
    def category_budget(self, category: str, month: str | None = None) -> float:
        return self.budgets.get((category, str(month or "")), 0.0)

    def set_category_budget(
        self, category: str, budget: float, month: str | None = None
    ) -> None:
        self.budgets[(category, str(month or ""))] = float(budget)


def _make_budgets_page(qapp) -> BudgetsPage:
    """BudgetsPage with mocked app/repo and an empty (itemless) table.

    Mirrors make_mock_app() from test_phase6_v349_aud31_32.py, which already
    constructs BudgetsPage successfully under offscreen Qt.
    """
    app = MagicMock()
    app.theme.privacy = False
    app.theme.dark = False
    app.content_scroll.viewport().width.return_value = 1280
    app.content_scroll.verticalScrollBar().value.return_value = 0
    app.page_density = "normal"
    app.repo.setting.side_effect = lambda k, default="": str(default)
    app.repo.saved_filters.return_value = []
    app.repo.monthly_snapshot_notes.return_value = ""
    app.repo.month_period.return_value = ("2026-08-01", "2026-08-31", "2026-08")
    app.repo.month_target.return_value = {"expense_target": 0.0}
    app.repo.totals_for_payment_period.return_value = {
        "income": 2000.0,
        "expenses": 500.0,
        "savings": 500.0,
    }
    app.repo.top_categories_payment_period.return_value = []
    app.repo.monthly_evolution_payment_period.return_value = []
    app.repo.savings_rate_milestones.return_value = []
    app.repo.list_transactions.return_value = []
    app.repo.filter_tags.return_value = []
    app.repo.account_names.return_value = []
    app.repo.category_names.return_value = []
    app.repo.active_holding_classes.return_value = []
    app.repo.holdings_summary.return_value = {"total_value": 0.0, "holdings": []}
    app.repo.categories.return_value = []
    app.repo.category_budgets.return_value = {}
    app.repo.category_budget.return_value = 0.0
    app.analytics_service.payment_method_totals.return_value = []
    app.analytics_service.totals_for_period.return_value = {
        "income": 2000.0,
        "expenses": 500.0,
        "savings": 500.0,
        "investments": 0.0,
    }
    app.analytics_service.category_totals.return_value = []
    app.analytics_service.monthly_summary_for_period.return_value = []
    app.analytics_service.monthly_budget_forecast.return_value = {}
    page = BudgetsPage(app)
    page.refresh = MagicMock()  # isolate from repo data population
    return page


# ==============================================================================
# BUG-UI-02: BudgetsPage.edit_selected None-item guard
# ==============================================================================


def test_bugui02_edit_selected_returns_without_crash_on_itemless_row(qapp) -> None:
    """A row with no item at column 0 must not raise AttributeError."""
    page = _make_budgets_page(qapp)
    try:
        # Simulate a current row index but no item set (crash scenario).
        page.table.setRowCount(3)
        page.table.setCurrentCell(1, 0)
        assert page.table.item(1, 0) is None

        with patch("ui.pages.planning.QMessageBox.information") as mock_info:
            page.edit_selected()  # must not raise AttributeError
            mock_info.assert_called_once()
    finally:
        page.close()


def test_bugui02_edit_selected_opens_dialog_for_valid_row(qapp) -> None:
    """A populated row still opens BudgetEditDialog with the category name."""
    page = _make_budgets_page(qapp)
    try:
        from PySide6.QtWidgets import QTableWidgetItem

        page.table.setRowCount(1)
        page.table.setItem(0, 0, QTableWidgetItem("Alimentação"))
        page.table.setCurrentCell(0, 0)

        captured: dict = {}

        class _StubDialog:
            Accepted = QDialog.Accepted

            def exec(self):
                captured["opened"] = True
                return QDialog.Rejected

        with patch("ui.pages.planning.BudgetEditDialog", side_effect=lambda *a, **k: _StubDialog()):
            page.edit_selected()
        assert captured.get("opened") is True
    finally:
        page.close()


# ==============================================================================
# BUG-DLG-08: BudgetEditDialog accepts 0.00 (removal), rejects negatives
# ==============================================================================


def test_bugdlg08_budget_edit_accepts_zero_as_removal(qapp) -> None:
    dialog = BudgetEditDialog("Alimentação", 400.0, None)
    try:
        dialog.value.setValue(0.0)
        with patch("ui.dialogs.QMessageBox.warning") as mock_warn:
            dialog.accept()
            assert not mock_warn.called, "0,00 € must be accepted (budget removal)"
        assert dialog.result() == QDialog.Accepted
        assert dialog.get_data()["budget"] == 0.0
    finally:
        dialog.close()


def test_bugdlg08_budget_edit_rejects_negative(qapp) -> None:
    dialog = BudgetEditDialog("Alimentação", 400.0, None)
    try:
        # EuroSpinBox range starts at 0, so drive the raw validator text to
        # simulate a negative entry reaching accept().
        dialog.value.setMinimum(-1000.0)
        dialog.value.setValue(-10.0)
        warned = {"flag": False}

        def _fake_warning(*args, **kwargs):
            warned["flag"] = True
            return None

        with patch("ui.dialogs.QMessageBox.warning", side_effect=_fake_warning):
            dialog.accept()
        assert warned["flag"], "negative amounts must warn"
        assert dialog.result() != QDialog.Accepted
    finally:
        dialog.close()


def test_bugdlg08_budget_edit_still_accepts_positive(qapp) -> None:
    dialog = BudgetEditDialog("Alimentação", 400.0, None)
    try:
        dialog.value.setValue(350.0)
        dialog.accept()
        assert dialog.result() == QDialog.Accepted
    finally:
        dialog.close()


# ==============================================================================
# BUG-DLG-09: get_plan keeps categories with dot decimals
# ==============================================================================


def _assistant_with_row(qapp, raw_value: str):
    repo = _MockRepo()
    dialog = BudgetAssistantDialog(repo, "2026-09", None)
    dialog.table.setRowCount(1)
    dialog.table.setItem(0, 0, BudgetAssistantDialog and _qitem("Alimentação"))
    dialog.table.setItem(0, 1, _qitem(raw_value))
    return dialog


def _qitem(text: str):
    from PySide6.QtWidgets import QTableWidgetItem

    return QTableWidgetItem(text)


def test_bugdlg09_get_plan_accepts_comma_decimal(qapp) -> None:
    dialog = _assistant_with_row(qapp, "250,50 €")
    try:
        plan = dialog.get_plan()
        assert plan == [("Alimentação", 250.50)]
    finally:
        dialog.close()


def test_bugdlg09_get_plan_accepts_dot_decimal(qapp) -> None:
    dialog = _assistant_with_row(qapp, "250.50 €")
    try:
        plan = dialog.get_plan()
        assert plan == [("Alimentação", 250.50)]
    finally:
        dialog.close()


def test_bugdlg09_get_plan_still_discards_garbage(qapp) -> None:
    dialog = _assistant_with_row(qapp, "abc")
    try:
        assert dialog.get_plan() == []
    finally:
        dialog.close()


def test_bugdlg09_get_plan_discards_ambiguous_thousands(qapp) -> None:
    """1.234 stays ambiguous (PT thousands) and must not become 1234."""
    dialog = _assistant_with_row(qapp, "1.234")
    try:
        assert dialog.get_plan() == []
    finally:
        dialog.close()


def test_parse_dot_decimal_strict_form() -> None:
    assert parse_dot_decimal("250.50") == pytest.approx(250.50)
    assert parse_dot_decimal("100") == pytest.approx(100.0)
    with pytest.raises(ValueError):
        parse_dot_decimal("1.234,56")
    with pytest.raises(ValueError):
        parse_dot_decimal("")


# ==============================================================================
# BUG-UI-03: defensive selected_id on PortfolioPage / ScheduledPaymentsPage
# ==============================================================================


class _TableHost(QWidget):
    """Minimal host exposing selected_id() bound from the real page class.

    PySide6 classes cannot be instantiated via object.__new__, so we bind the
    unbound selected_id from the real page class onto a plain QWidget holding
    a bare table. This exercises the exact production method under test.
    """

    selected_id = None  # replaced per page class below

    def __init__(self, page_cls) -> None:
        super().__init__(None)
        self.table = QTableWidget(2, 3)
        self.selected_id = page_cls.selected_id.__get__(self, type(self))


def _page_with_table(page_cls, qapp):
    """Build a host widget running the real page selected_id() implementation."""
    return _TableHost(page_cls)


def test_bugui03_portfolio_selected_id_empty_selection_returns_none(qapp) -> None:
    page = _page_with_table(PortfolioPage, qapp)
    assert page.selected_id() is None  # no current row


def test_bugui03_portfolio_selected_id_non_numeric_text_returns_none(qapp) -> None:
    from PySide6.QtWidgets import QTableWidgetItem

    page = _page_with_table(PortfolioPage, qapp)
    page.table.setItem(0, 0, QTableWidgetItem("ETF World"))
    page.table.setCurrentCell(0, 0)
    assert page.selected_id() is None  # must not raise ValueError


def test_bugui03_portfolio_selected_id_numeric_text_parses(qapp) -> None:
    from PySide6.QtWidgets import QTableWidgetItem

    page = _page_with_table(PortfolioPage, qapp)
    page.table.setItem(0, 0, QTableWidgetItem("42"))
    page.table.setCurrentCell(0, 0)
    assert page.selected_id() == 42


def test_bugui03_scheduled_selected_id_empty_selection_returns_none(qapp) -> None:
    page = _page_with_table(ScheduledPaymentsPage, qapp)
    assert page.selected_id() is None


def test_bugui03_scheduled_selected_id_non_numeric_text_returns_none(qapp) -> None:
    from PySide6.QtWidgets import QTableWidgetItem

    page = _page_with_table(ScheduledPaymentsPage, qapp)
    page.table.setItem(0, 0, QTableWidgetItem("N/A"))
    page.table.setCurrentCell(0, 0)
    assert page.selected_id() is None


def test_bugui03_scheduled_selected_id_numeric_text_parses(qapp) -> None:
    from PySide6.QtWidgets import QTableWidgetItem

    page = _page_with_table(ScheduledPaymentsPage, qapp)
    page.table.setItem(0, 0, QTableWidgetItem("7"))
    page.table.setCurrentCell(0, 0)
    assert page.selected_id() == 7
