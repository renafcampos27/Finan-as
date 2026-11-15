"""Phase 12 batch 3: P1 regression tests from AUDIT_UNRESOLVED_BUGS_TEMP.

Covers:
- BUG-DLG-LIABILITY-EDIT-NO-NAME-002: edit path cannot persist empty name;
- BUG-CHART-SANKEY-NEGATIVE-GEOMETRY-001: geometry guards, no negative rects;
- BUG-CHART-STACKED-EXPENSE-NEGATIVE-002: negative segments never drawn;
- HEALTH-01: insolvent user scores 0 on the debt dimension.
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

from database.engine import FinanceDB
from database.repo.main_repo import Repo
from services.change_journal import ChangeJournalService
from services.financial_health import FinancialHealthService
from services.payments import PaymentsService
from services.financial_intelligence import FinancialIntelligenceService
from ui.components.charts.bar import StackedExpenseChart
from ui.components.charts.sankey import SankeyChart
from ui.dialogs import LiabilityDialog
from ui.pages.investments import PortfolioPage


@pytest.fixture(scope="session")
def qapp():
    return QApplication.instance() or QApplication([])


# ==============================================================================
# 1. BUG-DLG-LIABILITY-EDIT-NO-NAME-002
# ==============================================================================


class _NotifyStub:
    def notify_data_changed(self, message: str) -> None:  # noqa: ARG002
        self.last_message = message


class _AppStub(_NotifyStub):
    theme = None


class _RepoStub:
    def __init__(self) -> None:
        self.updated: list[tuple] = []
        self._row = {
            "id": 7,
            "name": "Crédito antigo",
            "liability_type": "Crédito pessoal",
            "account": "Banco",
            "balance": 1000.0,
            "interest_rate": 5.0,
            "monthly_payment": 50.0,
            "notes": "",
        }

    def liability_by_id(self, liability_id: int):
        return dict(self._row)

    def update_liability(self, *args) -> None:
        self.updated.append(args)


def test_liability_dialog_rejects_whitespace_name(
    qapp, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dialog-level: whitespace name must not accept (existing guard)."""

    warnings: list[tuple] = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: warnings.append(args))
    dialog = LiabilityDialog(None, None)
    dialog.name.setText("   ")
    dialog.accept()
    assert dialog.result() != QDialog.Accepted
    assert len(warnings) == 1


def _make_page_with_dialog(monkeypatch, name_text: str):
    """Build a page stub whose edit_liability() sees a pre-built dialog.

    LiabilityDialog is injected into the page module namespace so the real
    constructor receives ``None`` as parent (never a non-widget stub).
    """

    import ui.pages.investments as investments_module

    dialog = LiabilityDialog(None, None)
    dialog.name.setText(name_text)
    monkeypatch.setattr(
        LiabilityDialog, "exec", lambda self: QDialog.Accepted, raising=True
    )
    monkeypatch.setattr(
        investments_module,
        "LiabilityDialog",
        lambda *args, **kwargs: dialog,
        raising=True,
    )

    page = PortfolioPage.__new__(PortfolioPage)
    page.app = _AppStub()
    page.repo = _RepoStub()
    page.selected_liability_id = lambda: 7
    return page, dialog


def test_liability_edit_strips_name_before_update(
    qapp, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Page-level: padded valid name is trimmed before update_liability."""

    page, _dialog = _make_page_with_dialog(monkeypatch, "  Crédito pessoal PF  ")
    page.edit_liability()
    assert len(page.repo.updated) == 1
    assert page.repo.updated[0][1] == "Crédito pessoal PF"


def test_liability_edit_empty_name_cancels_without_write(
    qapp, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Widget-level defence: whitespace name cancels before update_liability."""

    warnings: list[tuple] = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: warnings.append(args))
    page, _dialog = _make_page_with_dialog(monkeypatch, "   ")
    page.edit_liability()

    assert len(warnings) == 1, "expected the empty-name warning"
    assert page.repo.updated == [], "no DB write may happen on empty name"


# ==============================================================================
# 2. BUG-CHART-SANKEY-NEGATIVE-GEOMETRY-001
# ==============================================================================


def test_sankey_tiny_viewport_draws_empty_without_negative_geometry(qapp) -> None:
    """A viewport too small to host the flow must draw the empty state."""

    chart = SankeyChart(theme_provider=lambda: __import__(
        "ui.core.theme", fromlist=["Theme"]
    ).Theme())
    chart.set_data([("Renda", 900.0), ("Comida", 300.0)])
    chart.resize(150, 80)  # below the w<=220 / h<=120 guards
    pixmap = QPixmap(chart.size())
    painter = QPainter(pixmap)
    try:
        chart.render_chart(painter)  # must not raise / crash
    finally:
        painter.end()
    assert chart.items == [("Renda", 900.0), ("Comida", 300.0)]


def test_sankey_many_categories_stay_inside_canvas(qapp) -> None:
    """Twelve categories must not push the flow column below the canvas."""

    chart = SankeyChart(theme_provider=lambda: __import__(
        "ui.core.theme", fromlist=["Theme"]
    ).Theme())
    data = [(f"Cat {i}", float(100 - i)) for i in range(12)]
    chart.set_data(data)
    chart.resize(900, 520)
    pixmap = QPixmap(chart.size())
    painter = QPainter(pixmap)
    try:
        chart.render_chart(painter)  # must not raise / crash
    finally:
        painter.end()
    heights = SankeyChart.flow_heights_for_items(data, 520.0)
    total_flow = sum(heights)
    gaps = SankeyChart.FLOW_GAP * (len(heights) - 1)
    assert total_flow + gaps <= 520.0 - SankeyChart.TOP_MARGIN + 0.5


# ==============================================================================
# 3. BUG-CHART-STACKED-EXPENSE-NEGATIVE-002
# ==============================================================================


def test_stacked_expense_skips_negative_segments_without_raising(qapp) -> None:
    """Refund month (negative category) renders without negative rects."""

    from ui.core.theme import Theme

    chart = StackedExpenseChart(theme_provider=lambda: Theme())
    chart.set_data(
        [
            {"Supermercado": 200.0, "Casa": 300.0},
            {"Supermercado": -450.0, "Casa": 250.0},
        ],
        [("Supermercado", "#ff0000"), ("Casa", "#00ff00")],
    )
    chart.resize(640, 360)
    pixmap = QPixmap(chart.size())
    painter = QPainter(pixmap)
    try:
        chart.render_chart(painter)  # must not raise / paint negative rects
    finally:
        painter.end()
    assert chart.rows[1]["Supermercado"] == -450.0  # data preserved for tooltip


# ==============================================================================
# 4. HEALTH-01
# ==============================================================================


def _build_service(tmp_path: Path):
    database = FinanceDB(tmp_path / "health01.sqlite3")
    repo = Repo(database)
    repo.db.seed_if_empty()
    journal = ChangeJournalService(repo)
    payments = PaymentsService(repo, journal)
    service = FinancialHealthService(repo, payments)
    intelligence = FinancialIntelligenceService(repo)
    return service, intelligence, database


def _strip_seed_data(repo: Repo) -> None:
    """Remove demo/seed rows so the test owns the whole scenario."""

    for table in (
        "transaction_splits",
        "transactions",
        "holdings",
        "liabilities",
        "goals",
        "subscriptions",
        "scheduled_payments",
        "financial_accounts",
    ):
        repo.db.conn.execute(f"DELETE FROM {table}")
    repo.db.conn.commit()


def _debt_dimension(result: dict) -> dict:
    """dimensions is a LIST of dicts keyed by 'key'; find the debt one."""
    for row in result["dimensions"]:
        if row["key"] == "debt":
            return row
    raise AssertionError("debt dimension missing from financial score")


def test_health01_insolvent_user_scores_zero_on_debt(tmp_path: Path) -> None:
    """Liabilities >= assets with net worth <= 0 must score 0, not 60+."""

    service, intelligence, database = _build_service(tmp_path)
    try:
        repo = service.repo
        _strip_seed_data(repo)
        repo.add_holding("Poupança", "Monetário", "Banco", 500.0, 500.0)
        repo.add_liability(
            "Crédito pessoal", "Crédito pessoal", "Banco", 20_000.0, 8.0, 250.0
        )
        # A completed observed month is required for a non-neutral debt score.
        repo.add_transaction("2026-08-01", "Despesa observada", -10.0, "Diversos", "Banco")

        # net_worth_snapshot() for a past month-end reads the stored
        # financial_state_snapshots row: backfill it from live state first.
        repo.record_financial_state_snapshot("2026-08-31")
        net_worth = intelligence.net_worth_snapshot(date(2026, 8, 31))
        assert net_worth["total_liabilities"] > 0
        assert net_worth["net_worth"] < 0, (
            f"scenario must be insolvent, got {net_worth['net_worth']}"
        )

        result = service.financial_score("2026-08")
        assert _debt_dimension(result)["score"] == pytest.approx(0.0), (
            "insolvent user must score 0 on debt"
        )
    finally:
        database.close()


def test_health01_no_debt_keeps_full_score(tmp_path: Path) -> None:
    """No liabilities at all keeps the positive 100 reading."""

    service, _intelligence, database = _build_service(tmp_path)
    try:
        repo = service.repo
        _strip_seed_data(repo)
        repo.add_holding("Poupança", "Monetário", "Banco", 300.0, 300.0)
        # Record the month-end state so liabilities_available is True with
        # zero liabilities (the full-score branch this test guards).
        repo.record_financial_state_snapshot("2026-08-31")
        result = service.financial_score("2026-08")
        assert _debt_dimension(result)["score"] == pytest.approx(100.0)
    finally:
        database.close()
