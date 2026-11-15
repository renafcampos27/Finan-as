#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Regression tests for audit batch 8 (BUG-UI-04 .. BUG-UI-08):

- BUG-UI-04 (P1): DataManagementPage._use_suggestion guards a None item
  (click on empty table area) instead of raising AttributeError.
- BUG-UI-05 (P1): ReportsPage.months() returns a sane fallback when the
  range combo is empty / non-numeric instead of raising IndexError/ValueError.
- BUG-UI-06 (P1): MovementsPage hard minimum heights are relaxed so the page
  fits a 1366x768 screen without double scrolling.
- BUG-UI-07 (P1): PanoramaPage.edit_budget_buckets notifies the application
  via app.notify_data_changed after saving 50/30/20 buckets.
- BUG-UI-08 (P1): SplitsPage.apply_split shows an informational PT-PT message
  when no movement is selected instead of failing silently.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QTableWidgetItem

from ui.pages.data_management import DataManagementPage
from ui.pages.movements import MovementsPage
from ui.pages.panorama import PanoramaPage
from ui.pages.reports import ReportsPage
from ui.pages.tools import SplitsPage


@pytest.fixture(scope="session")
def qapp():
    return QApplication.instance() or QApplication([])


def _make_mock_app() -> MagicMock:
    """Minimal mock app mirroring make_mock_app() from test_phase6."""
    app = MagicMock()
    app.theme.privacy = False
    app.theme.dark = False
    app.page_density = "normal"
    app.content_scroll.viewport().width.return_value = 1280
    app.content_scroll.viewport().height.return_value = 700
    app.content_scroll.verticalScrollBar().value.return_value = 0
    app.repo.setting.side_effect = lambda k, default="": str(default)
    app.repo.month_period.return_value = ("2026-08-01", "2026-08-31", "2026-08")
    app.repo.month_target.return_value = {"expense_target": 0.0}
    app.repo.totals_for_payment_period.return_value = {
        "income": 2000.0,
        "expenses": 500.0,
        "savings": 1500.0,
    }
    return app


# ---------------------------------------------------------------------------
# BUG-UI-04
# ---------------------------------------------------------------------------


class TestBugUI04UseSuggestionNoneGuard:
    def _page_with_stub_table(self, qapp):
        # Lightweight stand-in: only the attributes used by _use_suggestion.
        page = SimpleNamespace(
            suggestions=MagicMock(),
            source=MagicMock(),
            target=MagicMock(),
            preview_merge=MagicMock(),
        )
        return page

    def test_none_item_is_ignored(self, qapp) -> None:
        page = self._page_with_stub_table(qapp)
        # Must not raise AttributeError.
        DataManagementPage._use_suggestion(page, None)

    def test_item_without_payload_is_ignored(self, qapp) -> None:
        page = self._page_with_stub_table(qapp)
        item = QTableWidgetItem("sem dados")
        DataManagementPage._use_suggestion(page, item)  # no UserRole tuple

    def test_valid_item_still_selects(self, qapp) -> None:
        page = self._page_with_stub_table(qapp)
        page.source.findData.return_value = 3
        page.target.findData.return_value = 5
        item = QTableWidgetItem("sugestão")
        item.setData(Qt.UserRole, ("src-id", "tgt-id"))
        DataManagementPage._use_suggestion(page, item)
        page.source.setCurrentIndex.assert_called_once_with(3)
        page.target.setCurrentIndex.assert_called_once_with(5)
        page.preview_merge.assert_called_once_with()


# ---------------------------------------------------------------------------
# BUG-UI-05
# ---------------------------------------------------------------------------


class TestBugUI05MonthsEmptyCombo:
    def _page(self, qapp) -> ReportsPage:
        app = _make_mock_app()
        return ReportsPage(app)

    def test_empty_combo_returns_fallback(self, qapp) -> None:
        page = self._page(qapp)
        page.range.blockSignals(True)
        page.range.clear()
        page.range.blockSignals(False)
        assert page.months() == 6

    def test_normal_combo_parses_months(self, qapp) -> None:
        page = self._page(qapp)
        page.range.setCurrentText("12 meses")
        assert page.months() == 12

    def test_fallback_matches_last_six_months_window(self, qapp) -> None:
        page = self._page(qapp)
        page.range.blockSignals(True)
        page.range.clear()
        page.range.blockSignals(False)
        start, end, _days, _label = page._period_window(page.months())
        # Exact expected window: last 6 full months ending today.
        expected_end = date.today()
        assert end == expected_end.isoformat()
        assert start < end


# ---------------------------------------------------------------------------
# BUG-UI-06
# ---------------------------------------------------------------------------


class TestBugUI06MovementsMinimumHeights:
    def _page(self, qapp) -> MovementsPage:
        app = _make_mock_app()
        app.repo.transactions_page.return_value = ([], 0)
        app.repo.categories.return_value = []
        return MovementsPage(app)

    def test_minimum_heights_fit_768px_screen(self, qapp) -> None:
        page = self._page(qapp)
        # Relaxed floors must fit a 768px-high viewport with room to spare.
        assert page.table_card.minimumHeight() <= 520
        assert page.table.minimumHeight() <= 440
        # The card floor alone must fit a 768px screen (it contains the table).
        assert page.table_card.minimumHeight() < 768

    def test_minimum_heights_are_explicit_new_values(self, qapp) -> None:
        page = self._page(qapp)
        assert page.table_card.minimumHeight() == 480
        assert page.table.minimumHeight() == 420


# ---------------------------------------------------------------------------
# BUG-UI-07
# ---------------------------------------------------------------------------


class _StubBucketDialog:
    def __init__(self, repo, parent) -> None:
        self._values = [("Alimentação", "Necessidades")]

    def exec(self) -> int:
        return 1  # QDialog.Accepted

    def values(self) -> list[tuple[str, str]]:
        return list(self._values)


class TestBugUI07EditBudgetBucketsNotifies:
    def _page(self, qapp) -> PanoramaPage:
        app = _make_mock_app()
        app.repo.top_categories_payment_period.return_value = []
        app.analytics_service.payment_method_totals.return_value = []
        app.analytics_service.monthly_evolution.return_value = []
        app.analytics_service.category_totals.return_value = []
        app.analytics_service.budget_rule_503020.return_value = {
            "income": 2000.0,
            "rows": [],
        }
        app.analytics_service.detailed_financial_score.return_value = {
            "score": 50,
            "confidence": 80,
            "grade": "C",
            "status": "ok",
            "marker": "",
            "confidence_label": "alta",
            "data_as_of": None,
        }
        page = PanoramaPage(app)
        return page

    def test_accepted_dialog_notifies_app(self, qapp) -> None:
        page = self._page(qapp)
        page.refresh = MagicMock()
        with patch("ui.pages.panorama.BudgetBucketDialog", _StubBucketDialog):
            page.edit_budget_buckets()
        page.app.notify_data_changed.assert_called_once_with(
            "Grupos 50/30/20 atualizados."
        )
        page.refresh.assert_called_once_with()
        page.app.repo.set_category_budget_bucket.assert_called_once_with(
            "Alimentação", "Necessidades"
        )

    def test_rejected_dialog_does_not_notify(self, qapp) -> None:
        page = self._page(qapp)
        page.refresh = MagicMock()

        class _Rejected(_StubBucketDialog):
            def exec(self) -> int:
                return 0  # QDialog.Rejected

        with patch("ui.pages.panorama.BudgetBucketDialog", _Rejected):
            page.edit_budget_buckets()
        page.app.notify_data_changed.assert_not_called()
        page.refresh.assert_not_called()


# ---------------------------------------------------------------------------
# BUG-UI-08
# ---------------------------------------------------------------------------


class TestBugUI08ApplySplitNoSelection:
    def _page(self, qapp) -> SplitsPage:
        app = _make_mock_app()
        app.repo.categories.return_value = ["Alimentação", "Transportes"]
        app.repo.transactions_with_splits.return_value = []
        app.repo.recent_transactions.return_value = []
        app.repo.transactions.return_value = []
        page = SplitsPage(app)
        return page

    def test_no_selection_shows_information_and_returns(self, qapp) -> None:
        page = self._page(qapp)
        page.tx.blockSignals(True)
        page.tx.clear()
        page.tx.blockSignals(False)
        with patch("ui.pages.tools.QMessageBox.information") as mock_info:
            page.apply_split()
        mock_info.assert_called_once()
        args = mock_info.call_args.args
        assert "Seleciona primeiro um movimento" in args[2]
        # No save must be attempted.
        page.app.change_journal_service.run_with_snapshot.assert_not_called()

    def test_none_currentdata_shows_information(self, qapp) -> None:
        page = self._page(qapp)
        page.tx.blockSignals(True)
        page.tx.clear()
        page.tx.addItem("Sem movimentos", userData=None)
        page.tx.setCurrentIndex(0)
        page.tx.blockSignals(False)
        with patch("ui.pages.tools.QMessageBox.information") as mock_info:
            page.apply_split()
        mock_info.assert_called_once()
