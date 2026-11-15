"""Automated verification suite for Council Audit Round 6 - Batch 39 defects.

Defects covered:
1. BUG-R6-DLG-01 [P0]: PortugueseTaxDialog._populate_tax_data initializes rate for crypto, eliminating UnboundLocalError.
2. BUG-R6-CHART-01 [P1]: StackedExpenseChart.render_chart balances p.save() and p.restore() when hover_index is None.
3. BUG-R6-PRIV-01 [P1]: StackedExpenseChart.render_chart masks tooltip percentages under privacy mode.
4. BUG-R6-PRIV-02 [P1]: InvestmentsPage masks return rates, gross returns, fee impact, and table performance columns under privacy.
5. BUG-R6-PRIV-03 [P2]: DashboardPage._on_data_loaded masks savings rate under privacy mode.
6. BUG-R6-A11Y-01 [P2]: CashflowTimelineChart and DonutChart update accessibleDescription dynamically on cache invalidation and privacy toggles.
7. BUG-R6-UI-02 [P2]: HeatmapChart calls super().mouseMoveEvent and super().leaveEvent.
8. BUG-R6-UI-03 [P2]: ClosePositionDialog and TransferDialog configure calendar popups via prepare_date_edit.
9. BUG-R6-UI-04 [P3]: PlanningPage deposit and withdrawal dialogs pass initial value 0.01 (satisfying initial >= min).
"""

from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QMouseEvent, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QCalendarWidget

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from database.engine import FinanceDB
from database.repo import Repo
from ui.components.charts.bar import StackedExpenseChart
from ui.components.charts.cashflow import CashflowTimelineChart
from ui.components.charts.donut import DonutChart
from ui.components.charts.heatmap import HeatmapChart
from ui.core.theme import Theme
from ui.dialogs import ClosePositionDialog, PortugueseTaxDialog, TransferDialog


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.delenv("FINANCAS_PESSOAIS_DEMO_DATA", raising=False)
    db = FinanceDB(tmp_path / "batch39_test.sqlite3")
    r = Repo(db)
    yield r
    try:
        db.conn.close()
    except Exception:
        pass


# 1. BUG-R6-DLG-01 [P0]
def test_bug_r6_dlg_01_portuguese_tax_dialog_crypto_unbound_local(qapp, repo):
    """BUG-R6-DLG-01: PortugueseTaxDialog._populate_tax_data() must not raise UnboundLocalError

    when processing taxable crypto assets (<365 days or without proven date).
    """
    repo.db.conn.execute(
        """
        INSERT INTO holdings (id, name, asset_class, value, cost_basis, cost_basis_known, acquisition_date, jurisdiction)
        VALUES (1, 'Bitcoin', 'Cripto', 5000.0, 2000.0, 1, ?, 'PT')
        """,
        (date.today().isoformat(),),
    )
    repo.db.conn.commit()

    dialog = PortugueseTaxDialog(repo)
    # Should populate without crashing with UnboundLocalError
    assert dialog.table.rowCount() == 1
    # Check that rate was applied (28% on 3000 gain = 840)
    item_tax = dialog.table.item(0, 6)
    assert item_tax is not None
    assert "840" in item_tax.text()
    dialog.close()


# 2. BUG-R6-CHART-01 [P1] & 3. BUG-R6-PRIV-01 [P1]
def test_bug_r6_chart_01_and_priv_01_stacked_bar_painter_balance_and_privacy(qapp):
    """BUG-R6-CHART-01: StackedExpenseChart balances p.save()/p.restore() when hover_index is None.

    BUG-R6-PRIV-01: Tooltip passes theme.privacy to percent() so percentages are masked.
    """
    theme = Theme(dark=False, privacy=True)
    chart = StackedExpenseChart(lambda: theme)
    chart.resize(400, 300)
    chart.set_data(
        rows=[
            {"label": "Jan", "Alimentação": 200.0, "Transporte": 100.0},
            {"label": "Fev", "Alimentação": 250.0, "Transporte": 150.0},
        ],
        categories=[("Alimentação", "#FF5733"), ("Transporte", "#33FF57")],
    )

    # Render without hover: p.save() and p.restore() must be balanced
    pixmap = QPixmap(400, 300)
    p = QPainter(pixmap)
    chart.hover_index = None
    chart.render_chart(p)
    # If p.save() was unbalanced, p.end() would log warning or fail
    p.end()

    # Render with hover and privacy=True: tooltip should mask percentages
    p2 = QPainter(pixmap)
    chart.hover_index = 0
    with patch.object(chart, "_paint_tooltip") as mock_tooltip:
        chart.render_chart(p2)
        assert mock_tooltip.called
        lines = mock_tooltip.call_args[0][3]
        # Check that percent is masked
        assert any("•••%" in line for line in lines)
        # Check that amounts are masked
        assert any("••••• €" in line for line in lines)
    p2.end()


# 4. BUG-R6-PRIV-02 [P1]
def test_bug_r6_priv_02_investments_privacy_masking(qapp, repo):
    """BUG-R6-PRIV-02: InvestmentsPage masks returns, gross returns, fee impact,

    and table performance columns with •••% and ••• p.p. when privacy=True.
    """
    from ui.pages.investments import InvestmentsPage

    app_mock = MagicMock()
    app_mock.repo = repo
    app_mock.theme = Theme(dark=False, privacy=True)
    page = InvestmentsPage(app_mock)

    # Insert holding with known gain
    repo.db.conn.execute(
        """
        INSERT INTO holdings (id, name, asset_class, account, value, cost_basis, cost_basis_known, fees, currency)
        VALUES (1, 'VWCE ETF', 'Ações', 'Degiro', 1500.0, 1000.0, 1, 10.0, 'EUR')
        """,
    )
    repo.db.conn.commit()

    page.refresh()

    # Check holdings table row: return (col 8) should be •••%, contribution (col 9) should be ••• p.p.
    assert page.table.rowCount() >= 1
    return_item = page.table.item(0, 8)
    contrib_item = page.table.item(0, 9)
    assert return_item is not None and return_item.text() == "•••%"
    assert contrib_item is not None and contrib_item.text() == "••• p.p."

    # Check summary text: fee impact and gross/net returns must be masked
    summary_text = page.holdings_summary.text()
    assert "••• p.p." in summary_text
    assert "•••%" in summary_text
    if hasattr(page, "_monte_carlo_timer"):
        page._monte_carlo_timer.stop()


# 5. BUG-R6-PRIV-03 [P2]
def test_bug_r6_priv_03_dashboard_savings_rate_privacy(qapp, repo):
    """BUG-R6-PRIV-03: DashboardPage masks savings rate in KPI card when privacy is active."""
    from ui.pages.dashboard import DashboardPage

    app_mock = MagicMock()
    app_mock.repo = repo
    app_mock.theme = Theme(dark=False, privacy=True)
    page = DashboardPage(app_mock)

    # Trigger data loaded with positive savings
    mock_data = {
        "totals": {"income": 2000.0, "expenses": 1200.0, "savings": 800.0},
        "totals_prev": {"income": 2000.0, "expenses": 1200.0, "savings": 800.0},
        "balance": 1500.0,
        "net": 5000.0,
        "comparison_mode": "month",
        "comparison_label": "mês anterior",
        "work_value": {},
        "emergency_reserve": {},
        "financial_independence": {},
        "metric_trends": {},
        "forecast": [],
        "investment_evolution": [],
        "health": {},
    }

    page._on_data_loaded(page._dashboard_request_id, mock_data)

    # Find the savings card in the metric widgets
    savings_card = next(
        (
            w
            for w in getattr(page, "metric_widgets", [])
            if hasattr(w, "title_label") and w.title_label.text() == "Poupança"
        ),
        None,
    )

    assert savings_card is not None
    # Delta label must mask the savings rate
    assert "•••%" in savings_card.delta_label.text()


# 6. BUG-R6-A11Y-01 [P2]
def test_bug_r6_a11y_01_accessible_description_dynamic_update(qapp):
    """BUG-R6-A11Y-01: CashflowTimelineChart and DonutChart update accessibleDescription

    dynamically on cache invalidation and reflect privacy toggles.
    """
    theme = Theme(dark=False, privacy=False)
    cashflow_chart = CashflowTimelineChart(lambda: theme)
    cashflow_chart.set_data({
        "points": [
            {"date": "2026-03-01", "balance": 1000.0, "balance_base": 1000.0},
            {"date": "2026-03-02", "balance": 1200.0, "balance_base": 1200.0},
        ],
        "events": [],
    })
    desc_public = cashflow_chart.accessibleDescription()
    assert "1 200" in desc_public or "1.200" in desc_public

    # Toggle privacy mode and invalidate cache
    theme.privacy = True
    cashflow_chart.invalidate_cache()
    desc_private = cashflow_chart.accessibleDescription()
    assert "••••• €" in desc_private

    # Test DonutChart
    donut_theme = Theme(dark=False, privacy=False)
    donut = DonutChart(lambda: donut_theme)
    donut.set_data([("Alimentação", 300.0, "#FF0000"), ("Lazer", 100.0, "#00FF00")])
    desc_donut_public = donut.accessibleDescription()
    assert "75,0%" in desc_donut_public

    # Toggle privacy on donut and invalidate cache
    donut_theme.privacy = True
    donut.invalidate_cache()
    desc_donut_private = donut.accessibleDescription()
    assert "•••%" in desc_donut_private
    assert "••••• €" in desc_donut_private


# 7. BUG-R6-UI-02 [P2]
def test_bug_r6_ui_02_heatmap_mouse_events_super_propagation(qapp):
    """BUG-R6-UI-02: HeatmapChart calls super().mouseMoveEvent and super().leaveEvent."""
    chart = HeatmapChart(lambda: Theme(dark=False, privacy=False))
    chart.set_data([
        {"date": "2026-01-01", "amount": 25.0},
        {"date": "2026-01-02", "amount": 50.0},
    ])

    with patch("PySide6.QtWidgets.QWidget.mouseMoveEvent") as mock_super_move, \
         patch("PySide6.QtWidgets.QWidget.leaveEvent") as mock_super_leave:
        pos = QPointF(10.0, 10.0)
        mouse_event = QMouseEvent(
            QEvent.Type.MouseMove,
            pos,
            pos,
            Qt.MouseButton.NoButton,
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
        )
        chart.mouseMoveEvent(mouse_event)
        assert mock_super_move.called

        leave_event = QEvent(QEvent.Type.Leave)
        chart.leaveEvent(leave_event)
        assert mock_super_leave.called


# 8. BUG-R6-UI-03 [P2]
def test_bug_r6_ui_03_date_edit_calendar_popup_styled(qapp, repo):
    """BUG-R6-UI-03: ClosePositionDialog and TransferDialog configure calendar popups via prepare_date_edit."""
    holding = {"id": 1, "name": "ETF", "value": 100.0, "cost_basis": 80.0}
    close_dlg = ClosePositionDialog(None, holding)
    cal_close = close_dlg.sale_date.calendarWidget()
    assert isinstance(cal_close, QCalendarWidget)
    assert cal_close.isGridVisible() is False
    close_dlg.close()

    transfer_dlg = TransferDialog(repo)
    cal_transfer = transfer_dlg.date_edit.calendarWidget()
    assert isinstance(cal_transfer, QCalendarWidget)
    assert cal_transfer.isGridVisible() is False
    transfer_dlg.close()


# 9. BUG-R6-UI-04 [P3]
def test_bug_r6_ui_04_planning_get_double_initial_value(qapp, repo):
    """BUG-R6-UI-04: PlanningPage deposit and withdrawal pass initial value 0.01 (initial >= min)."""
    from ui.pages.planning import GoalsPage

    app_mock = MagicMock()
    app_mock.repo = repo
    app_mock.theme = Theme(dark=False, privacy=False)
    page = GoalsPage(app_mock)

    # Insert a goal
    repo.db.conn.execute(
        "INSERT INTO goals (id, name, target_amount, saved_amount) VALUES (1, 'Férias', 1000.0, 50.0)"
    )
    repo.db.conn.commit()
    page.refresh()

    with patch("PySide6.QtWidgets.QInputDialog.getDouble", return_value=(10.0, False)) as mock_get_double:
        page.selected_id = MagicMock(return_value=1)
        page.deposit_selected()
        assert mock_get_double.called
        # Check initial value is 0.01 (args: parent, title, label, initial, min, max, decimals)
        args = mock_get_double.call_args[0]
        initial_val = args[3]
        min_val = args[4]
        assert initial_val == 0.01
        assert initial_val >= min_val

    with patch("PySide6.QtWidgets.QInputDialog.getDouble", return_value=(10.0, False)) as mock_get_double_w:
        page.selected_id = MagicMock(return_value=1)
        page.withdraw_selected()
        assert mock_get_double_w.called
        args_w = mock_get_double_w.call_args[0]
        initial_val_w = args_w[3]
        min_val_w = args_w[4]
        assert initial_val_w == 0.01
        assert initial_val_w >= min_val_w
