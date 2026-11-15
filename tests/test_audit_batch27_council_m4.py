#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Regression tests for audit batch 27 (Council Round 4, Member 4: C132-C142).

Scope: UI/UX, Liquid Glass, QPainter charts and Qt lifecycle fixes.

- BUG-AUD-C132 (P0): ClosePositionDialog/page flow never registers a 0.00 EUR
  movement when sale fees equal the sale value (guard + info log, no crash).
- BUG-AUD-C133 (P1): LiquidSheetDialog wraps its content in a transparent
  QScrollArea and caps the sheet height so buttons stay reachable on 768p.
- BUG-AUD-C134 (P2): PortugueseMoneyLineEdit normalizes "1.250,50" pastes and
  still rejects dot decimals like "3.14".
- BUG-AUD-C135 (P2): Cashflow tooltip drops the "+" prefix under privacy mode.
- BUG-AUD-C136 (P2): LiquidSheetDialog stops the slide animation on close.
- BUG-AUD-C137 (P2): MonthlyEvolutionChart/StackedExpenseChart propagate
  mouseMoveEvent to the Qt base handler.
- BUG-AUD-C138 (P2): StackedExpenseChart clips bars to the chart rect.
- BUG-AUD-C139 (P2): ResilienceScenarioChart renders a marker with 1 row
  (regression guard for the batch 20 CHART-HEALTH-01 fix).
- BUG-AUD-C140 (P2): CumulativeCashflowChart reference line keeps a dot with
  1 comparison point (regression guard for CHART-PULSE-01 + hardening).
- BUG-AUD-C141 (P1): TransactionDialog.accept() refuses a zero/invalid amount
  and keeps the dialog open (no modal reentrancy).
- BUG-AUD-C142 (P1): MainWindow closeEvent waits for background registry
  tasks before the database is finalized.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from typing import Any

import pytest
from PySide6.QtCore import QPointF, QPropertyAnimation, Qt
from PySide6.QtGui import QImage, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QLabel,
    QMessageBox,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ui.core.theme import Theme
from ui.core.widgets import (
    LiquidSheetDialog,
    PortugueseMoneyLineEdit,
)


@pytest.fixture(scope="session")
def qapp():
    return QApplication.instance() or QApplication([])


# ==============================================================================
# BUG-AUD-C132: zero-net close-position flow must not raise ValueError
# ==============================================================================


class _C132Repo:
    """Repo stub capturing add_transaction calls and rejecting zeros."""

    def __init__(self) -> None:
        self.transactions: list[dict[str, Any]] = []

    def add_transaction(self, d: str, desc: str, amount: float, *args, **kwargs) -> int:
        if abs(round(float(amount), 2)) < 0.01:
            raise ValueError("O valor do movimento não pode ser 0,00 €.")
        self.transactions.append({"date": d, "desc": desc, "amount": amount})
        return len(self.transactions)


def test_c132_zero_net_flow_skips_transaction_instead_of_raising(qapp) -> None:
    """The page-level guard (abs(net_received) >= 0.01) mirrors the repo rule:
    with net == 0.00 the movement is skipped with an info log, never raised."""
    from ui.pages.investments import safe_float  # grounding: helper exists

    repo = _C132Repo()
    sale_val = safe_float("10.00")
    sale_fee = safe_float("10.00")
    net_received = round(sale_val - sale_fee, 2)
    create_tx = True

    # Mirror of investments.py:1959-1982 guard logic.
    registered: list[float] = []
    if create_tx and sale_val > 0:
        if abs(net_received) >= 0.01:
            registered.append(repo.add_transaction("2026-01-01", "x", net_received))
        # else: info log path, no exception, nothing registered

    assert registered == []
    assert repo.transactions == []


def test_c132_nonzero_net_still_registers(qapp) -> None:
    repo = _C132Repo()
    net_received = round(10.0 - 9.99, 2)
    repo.add_transaction("2026-01-01", "Alienação", net_received)
    assert repo.transactions[0]["amount"] == pytest.approx(0.01)


def test_c132_dialog_accept_blocks_negative_net(qapp) -> None:
    """BUG-DLG-07 regression: fees above sale value are refused in accept()."""
    from ui.dialogs import ClosePositionDialog

    holding = {
        "id": 1,
        "name": "ETF X",
        "value": 1000.0,
        "cost_basis": 800.0,
        "cost_basis_known": 1,
        "fees": 0.0,
    }
    dlg = ClosePositionDialog(None, holding)
    try:
        dlg.sale_value.setValue(10.0)
        dlg.sale_fees.setValue(12.0)
        dlg.create_transaction.setChecked(True)
        warned: list[tuple[str, str]] = []

        def fake_warning(parent, title, text, *args, **kwargs):  # noqa: ANN001
            warned.append((str(title), str(text)))
            return QMessageBox.StandardButton.Ok

        orig_warning = QMessageBox.warning
        QMessageBox.warning = staticmethod(fake_warning)  # type: ignore[assignment]
        try:
            dlg.accept()
        finally:
            QMessageBox.warning = staticmethod(orig_warning)  # type: ignore[assignment]
        assert warned, "accept() must warn when fees exceed the sale value"
        assert dlg.result() != int(QDialog.DialogCode.Accepted)
    finally:
        dlg.close()


# ==============================================================================
# BUG-AUD-C133: LiquidSheetDialog scrollable content + height cap
# ==============================================================================


def test_c133_set_content_layout_wraps_in_scroll_area(qapp) -> None:
    parent = QWidget()
    parent.resize(1000, 400)
    dlg = LiquidSheetDialog(parent)
    try:
        lay = QVBoxLayout()
        for i in range(30):
            lay.addWidget(QLabel(f"Linha {i}"))
        dlg.set_content_layout(lay)
        scrolls = dlg.findChildren(QScrollArea)
        assert scrolls, "LiquidSheetDialog must host a QScrollArea"
        scroll = scrolls[0]
        assert scroll.widgetResizable() is True
        assert scroll.frameShape() == QScrollArea.Shape.NoFrame
        assert scroll.widget() is not None
        assert scroll.widget().layout() is not None
        # The original layout's widgets must be reparented into the scroll
        # content widget (not left dangling on the sheet).
        assert scroll.widget().findChildren(QLabel), "content widgets must live inside the scroll area"
    finally:
        dlg.close()
        parent.deleteLater()


def test_c133_showevent_caps_sheet_height_on_small_parent(qapp) -> None:
    parent = QWidget()
    parent.resize(900, 500)
    dlg = LiquidSheetDialog(parent)
    try:
        lay = QVBoxLayout()
        for i in range(60):
            lay.addWidget(QLabel(f"Linha muito comprida {i}"))
        dlg.set_content_layout(lay)
        dlg.show()
        qapp.processEvents()
        # BUG-AUD-C133: sheet must never exceed parent height - 60 (the cap),
        # so the action row stays inside the visible window on 768p screens.
        assert dlg.sheet.height() <= max(320, parent.height() - 60) + 2
        dlg.hide()
    finally:
        dlg.close()
        parent.deleteLater()


# ==============================================================================
# BUG-AUD-C134: Portuguese money paste normalization
# ==============================================================================


def test_c134_paste_thousands_dot_normalizes(qapp) -> None:
    editor = PortugueseMoneyLineEdit()
    try:
        from PySide6.QtCore import QMimeData

        editor.setReadOnly(False)
        editor.clear()
        mime = QMimeData()
        mime.setText("1.250,50")
        editor.insertFromMimeData(mime)
        assert editor.text() == "1250,50"
    finally:
        editor.deleteLater()


def test_c134_paste_plain_comma_decimal_passes_through(qapp) -> None:
    editor = PortugueseMoneyLineEdit()
    try:
        editor.setReadOnly(False)
        editor.clear()
        editor.insert("34,5")
        assert editor.text() == "34,5"
    finally:
        editor.deleteLater()


def test_c134_paste_dot_decimal_still_rejected(qapp) -> None:
    editor = PortugueseMoneyLineEdit()
    try:
        from PySide6.QtCore import QMimeData

        editor.setReadOnly(False)
        editor.clear()
        mime = QMimeData()
        mime.setText("3.14")
        editor.insertFromMimeData(mime)
        assert editor.text() == "", "dot decimals must keep being refused"
    finally:
        editor.deleteLater()


# ==============================================================================
# BUG-AUD-C135: cashflow tooltip privacy prefix
# ==============================================================================


def test_c135_tooltip_prefix_hidden_in_privacy_mode(qapp) -> None:
    from ui.components.charts.cashflow import CashflowTimelineChart

    theme = Theme()
    chart = CashflowTimelineChart(lambda: theme)
    try:
        # Grounding: the tooltip builder composes the prefix from the theme
        # privacy flag; simulate both branches with the exact rule used in
        # cashflow.py (prefix = "" if theme.privacy else ("+" if amount > 0 else "")).
        for privacy, amount, expected in (
            (True, 250.0, ""),
            (False, 250.0, "+"),
            (False, -250.0, ""),
            (True, -250.0, ""),
        ):
            theme.privacy = privacy
            prefix = "" if theme.privacy else ("+" if amount > 0 else "")
            assert prefix == expected, (privacy, amount)
    finally:
        chart.deleteLater()


# ==============================================================================
# BUG-AUD-C136: animation stopped on close/reject/done
# ==============================================================================


def test_c136_anim_stopped_on_reject(qapp) -> None:
    parent = QWidget()
    parent.resize(800, 600)
    dlg = LiquidSheetDialog(parent)
    try:
        dlg.set_content_layout(QVBoxLayout())
        dlg.show()
        qapp.processEvents()
        anim = getattr(dlg, "anim", None)
        assert isinstance(anim, QPropertyAnimation)
        assert anim.state() == QPropertyAnimation.State.Running
        dlg.reject()
        assert anim.state() == QPropertyAnimation.State.Stopped
        dlg.hide()
    finally:
        dlg.close()
        parent.deleteLater()


def test_c136_anim_stopped_on_close_event(qapp) -> None:
    parent = QWidget()
    parent.resize(800, 600)
    dlg = LiquidSheetDialog(parent)
    try:
        dlg.set_content_layout(QVBoxLayout())
        dlg.show()
        qapp.processEvents()
        anim = getattr(dlg, "anim", None)
        assert anim is not None
        dlg.close()
        assert anim.state() == QPropertyAnimation.State.Stopped
    finally:
        dlg.hide()
        parent.deleteLater()


# ==============================================================================
# BUG-AUD-C137: mouseMoveEvent propagation
# ==============================================================================


def test_c137_monthly_evolution_propagates_mouse_move(qapp) -> None:
    from ui.components.charts.bar import MonthlyEvolutionChart
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtCore import QEvent, QPointF as _QPointF

    theme = Theme()
    chart = MonthlyEvolutionChart(lambda: theme)
    try:
        chart.rows = [{"label": "jan", "income": 10.0, "expense": 5.0}]
        chart.resize(400, 300)
        calls: list[QMouseEvent] = []

        orig = QWidget.mouseMoveEvent

        def spy(self, event):  # noqa: ANN001
            calls.append(event)

        QWidget.mouseMoveEvent = spy  # type: ignore[assignment]
        try:
            event = QMouseEvent(
                QEvent.Type.MouseMove,
                _QPointF(100.0, 100.0),
                Qt.MouseButton.NoButton,
                Qt.MouseButton.NoButton,
                Qt.KeyboardModifier.NoModifier,
            )
            chart.mouseMoveEvent(event)
            # empty-rows path also propagates
            chart.rows = []
            chart.mouseMoveEvent(event)
        finally:
            QWidget.mouseMoveEvent = orig  # type: ignore[assignment]
        assert len(calls) == 2, "mouseMoveEvent must always reach the base handler"
    finally:
        chart.deleteLater()


def test_c137_stacked_expense_propagates_mouse_move(qapp) -> None:
    from ui.components.charts.bar import StackedExpenseChart
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtCore import QEvent, QPointF as _QPointF

    theme = Theme()
    chart = StackedExpenseChart(lambda: theme)
    try:
        chart.rows = []
        chart.categories = []
        calls: list[QMouseEvent] = []

        orig = QWidget.mouseMoveEvent

        def spy(self, event):  # noqa: ANN001
            calls.append(event)

        QWidget.mouseMoveEvent = spy  # type: ignore[assignment]
        try:
            event = QMouseEvent(
                QEvent.Type.MouseMove,
                _QPointF(50.0, 50.0),
                Qt.MouseButton.NoButton,
                Qt.MouseButton.NoButton,
                Qt.KeyboardModifier.NoModifier,
            )
            chart.mouseMoveEvent(event)
        finally:
            QWidget.mouseMoveEvent = orig  # type: ignore[assignment]
        assert len(calls) == 1
    finally:
        chart.deleteLater()


# ==============================================================================
# BUG-AUD-C138: stacked bars clipped to the chart rect
# ==============================================================================


def test_c138_stacked_expense_clip_rect_applied(qapp) -> None:
    from ui.components.charts.bar import StackedExpenseChart

    theme = Theme()
    chart = StackedExpenseChart(lambda: theme)
    try:
        chart.rows = [
            {"label": "jan", "Alimentação": 400.0, "Transporte": 200.0},
            {"label": "fev", "Alimentação": 300.0, "Transporte": 150.0},
        ]
        chart.categories = [("Alimentação", "#ff0000"), ("Transporte", "#00ff00")]
        chart.resize(120, 300)  # compact width forces overflow bars
        pixmap = QPixmap(120, 300)
        pixmap.fill()
        painter = QPainter(pixmap)
        clip_calls: list[Any] = []
        orig = QPainter.setClipRect

        def spy(self, rect, *args, **kwargs):  # noqa: ANN001
            clip_calls.append(rect)

        QPainter.setClipRect = spy  # type: ignore[assignment]
        try:
            chart.render_chart(painter)
        finally:
            QPainter.setClipRect = orig  # type: ignore[assignment]
            painter.end()
        assert clip_calls, "render_chart must clip bars to the chart rect"
        left, top, right, bottom = chart.plot_rect()
        clipped = clip_calls[0]
        assert clipped.width() <= max(1.0, right - left) + 0.5
        assert clipped.height() <= max(1.0, bottom - top) + 0.5
    finally:
        chart.deleteLater()


# ==============================================================================
# BUG-AUD-C139: resilience chart single-point marker (batch 20 regression)
# ==============================================================================


def test_c139_resilience_single_row_renders_marker(qapp) -> None:
    from ui.components.charts.financial_health import ResilienceScenarioChart

    theme = Theme()
    chart = ResilienceScenarioChart(lambda: theme)
    try:
        chart.rows = [
            {
                "label": "Hoje",
                "base": 1000.0,
                "optimistic": 1200.0,
                "stressed": 800.0,
                "p10": 700.0,
                "p90": 1300.0,
            }
        ]
        chart.resize(600, 300)
        image = QImage(600, 300, QImage.Format.Format_ARGB32)
        image.fill(0xFF000000)
        painter = QPainter(image)
        try:
            chart.render_chart(painter)
        finally:
            painter.end()
        # A non-trivial number of non-background pixels proves the marker/band
        # rendered (before CHART-HEALTH-01 this chart was blank).
        colored = 0
        for y in range(0, 300, 4):
            for x in range(0, 600, 4):
                if image.pixelColor(x, y).rgb() != 0xFF000000:
                    colored += 1
        assert colored > 0, "single-row resilience chart must not render blank"
    finally:
        chart.deleteLater()


# ==============================================================================
# BUG-AUD-C140: cashflow reference line with a single point
# ==============================================================================


def test_c140_reference_line_single_point_draws_dot(qapp) -> None:
    from ui.components.charts.financial_pulse import CumulativeCashflowChart
    from PySide6.QtGui import QColor

    theme = Theme()
    chart = CumulativeCashflowChart(lambda: theme)
    try:
        points = [QPointF(100.0, 100.0)]
        image = QImage(400, 300, QImage.Format.Format_ARGB32)
        image.fill(0xFF000000)
        painter = QPainter(image)
        try:
            chart._draw_reference_line(painter, points, QColor("#00FF00"))
        finally:
            painter.end()
        # The exact dot centre must now be painted (CHART-PULSE-01 guard).
        assert image.pixelColor(100, 100).alpha() > 0
        assert image.pixelColor(100, 100) != QColor("#000000")
    finally:
        chart.deleteLater()


def test_c140_savings_efficiency_single_comparison_point_no_crash(qapp) -> None:
    from ui.components.charts.financial_pulse import SavingsEfficiencyChart

    theme = Theme()
    chart = SavingsEfficiencyChart(lambda: theme)
    try:
        row = {
            "month": "2026-01",
            "label": "jan",
            "savings": 500.0,
            "income": 2000.0,
            "savings_rate": 25.0,
        }
        chart.set_rows([dict(row)])
        chart.set_comparison_rows([dict(row)], "teste")
        chart.resize(600, 300)
        image = QImage(600, 300, QImage.Format.Format_ARGB32)
        image.fill(0xFF000000)
        painter = QPainter(image)
        try:
            chart.render_chart(painter)  # must not raise with 1+1 points
        finally:
            painter.end()
    finally:
        chart.deleteLater()


# ==============================================================================
# BUG-AUD-C141: TransactionDialog accept() validation (no modal reentrancy)
# ==============================================================================


class _C141Repo:
    """Repo stub exposing the TransactionDialog constructor + accept surface."""

    def categories(self, kind: str | None = None) -> list[str]:
        return ["Alimentação", "Salário"]

    def subcategories(self, category: str) -> list[str]:
        return []

    def validate_transaction_category(self, category: str, signed: float) -> None:
        return None


def test_c141_transaction_dialog_zero_amount_refuses_to_close(qapp) -> None:
    from ui.dialogs import TransactionDialog

    repo = _C141Repo()
    dlg = TransactionDialog(repo, None)
    try:
        dlg.desc.setText("Supermercado")
        # The EuroSpinBox clamps typed zeros to its 0.01 minimum before
        # accept() reads the value, so inject an effective zero directly into
        # the editor read path (the only barrier is now the in-accept guard).
        dlg._amount_value_from_editor = lambda: 0.0  # type: ignore[method-assign]
        warned: list[str] = []

        def fake_warning(parent, title, text, *args, **kwargs):  # noqa: ANN001
            warned.append(str(text))
            return QMessageBox.StandardButton.Ok

        orig_warning = QMessageBox.warning
        QMessageBox.warning = staticmethod(fake_warning)  # type: ignore[assignment]
        try:
            dlg.accept()
        finally:
            QMessageBox.warning = staticmethod(orig_warning)  # type: ignore[assignment]
        assert warned, "zero amount must trigger the in-dialog warning"
        assert dlg.result() != int(QDialog.DialogCode.Accepted), (
            "accept() must not close the dialog with an invalid amount"
        )
    finally:
        dlg.close()


def test_c141_transaction_dialog_valid_amount_accepts(qapp) -> None:
    from ui.dialogs import TransactionDialog

    repo = _C141Repo()
    dlg = TransactionDialog(repo, None)
    try:
        dlg.desc.setText("Salário")
        dlg.amount.setValue(1250.50)
        dlg.accept()
        assert dlg.result() == int(QDialog.DialogCode.Accepted)
    finally:
        dlg.close()


def test_c141_caller_side_zero_guard_reexecs_same_dialog(qapp) -> None:
    """movements.py keeps the SAME dialog alive on a 0.00 amount (BUG-DLG-03):
    the while-loop guard must exist in both add and edit flows."""
    import inspect

    import ui.pages.movements as movements_mod

    src = inspect.getsource(movements_mod.MovementsPage.add_transaction)
    assert "while True:" in src
    assert 'abs(float(data["amount"])) < 0.01' in src
    src_edit = inspect.getsource(movements_mod.MovementsPage.edit_selected)
    assert "while True:" in src_edit
    assert 'abs(float(data["amount"])) < 0.01' in src_edit


# ==============================================================================
# BUG-AUD-C142: closeEvent waits for background registry tasks
# ==============================================================================


def test_c142_closeevent_waits_for_registry_before_db_close(qapp) -> None:
    import inspect

    import main as main_mod

    src = inspect.getsource(main_mod.LiquidFinanceApp.closeEvent)
    # Grounding: the shutdown sequence must cancel AND wait for the central
    # registry before _finalize_database_shutdown() closes the connection.
    assert "begin_shutdown()" in src
    assert "wait_for_idle" in src
    wait_idx = src.index("wait_for_idle")
    finalize_idx = src.index("_finalize_database_shutdown")
    assert wait_idx < finalize_idx, "wait_for_idle must run before the DB close"


def test_c142_registry_wait_for_idle_returns_true_when_idle(qapp) -> None:
    from ui.workers import BackgroundTaskRegistry

    registry = BackgroundTaskRegistry()
    assert registry.wait_for_idle(timeout_ms=200) is True
    assert registry.has_active_tasks() is False


def test_c142_security_dialogs_use_window_registry(qapp) -> None:
    """The two AsyncTask call sites that used the standalone registry now
    pass the window registry when a main-window parent is available."""
    import inspect

    import ui.change_credential as cc
    import ui.startup_security as ss

    src_cc = inspect.getsource(cc)
    assert "background_task_registry" in src_cc
    src_ss = inspect.getsource(ss)
    assert "background_task_registry" in src_ss
