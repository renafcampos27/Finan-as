"""A06: month-owned notes and chart ownership across data/page replacement."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QRectF
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QApplication, QMessageBox, QVBoxLayout, QWidget
from shiboken6 import isValid

from ui.components.charts import BarChart, HeatmapChart
from ui.core.theme import Theme
from ui.pages.history import HistoryPage


@pytest.fixture
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def history(qapp, monkeypatch):
    repo = MagicMock()
    repo.report_months.return_value = ["2026-10", "2026-09"]
    repo.setting.side_effect = lambda key, default="": default
    notes = {"2026-10": "October saved", "2026-09": "September saved"}
    repo.monthly_snapshot_notes.side_effect = lambda month: notes[month]
    repo.category_budgets.return_value = {}
    repo.save_monthly_snapshot_notes.side_effect = (
        lambda month, start, end, text: notes.__setitem__(month, text)
    )
    analytics = MagicMock()
    analytics.totals_for_period.return_value = {
        "income": 1000, "expenses": 100, "savings": 900, "investments": 0,
    }
    analytics.category_totals.return_value = []
    app = SimpleNamespace(
        repo=repo, theme=Theme(), analytics_service=analytics,
        payments_service=MagicMock(), import_service=MagicMock(),
    )
    monkeypatch.setattr(QMessageBox, "critical", lambda *args: None)
    page = HistoryPage(app)
    yield page, repo, notes
    page.notes.document().setModified(False)
    page.notes.setPlainText(notes.get(getattr(page, "_notes_month", "2026-10"), ""))
    page.close()
    page.deleteLater()


def test_refresh_cancel_preserves_unsaved_month_notes(history, monkeypatch):
    page, repo, _ = history
    page.notes.setPlainText("October draft")
    page.notes.document().setModified(True)
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.Cancel)
    page.refresh()
    assert page.notes.toPlainText() == "October draft"
    assert page.notes.document().isModified()
    repo.save_monthly_snapshot_notes.assert_not_called()


def test_cancel_draft_reload_still_masks_read_only_panels(history, monkeypatch):
    page, _, _ = history
    page.analytics_service.category_totals.return_value = [("Category", 1234, "#123456")]
    page.refresh()
    assert "1 234" in page.table.item(0, 1).text()
    page.notes.setPlainText("Private unsaved draft")
    page.app.theme.privacy = True
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.Cancel)
    page.refresh()
    assert "1234" not in page.table.item(0, 1).text().replace(" ", "")
    assert "•••" in page.table.item(0, 1).text()
    assert page.notes.isHidden()
    assert page.notes.toPlainText() == "Private unsaved draft"
    assert page._note_drafts["2026-10"] == "Private unsaved draft"


def test_month_transition_save_targets_original_month(history, monkeypatch):
    page, repo, saved = history
    page.notes.setPlainText("October draft")
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.Save)
    page.month.setCurrentText("2026-09")
    repo.save_monthly_snapshot_notes.assert_called_once_with(
        "2026-10", "2026-10-01", "2026-10-31", "October draft"
    )
    assert saved["2026-10"] == "October draft"
    assert page.notes.toPlainText() == "September saved"
    assert not page.notes.document().isModified()


def test_month_transition_cancel_restores_selector(history, monkeypatch):
    page, repo, _ = history
    page.notes.setPlainText("October draft")
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.Cancel)
    page.month.setCurrentText("2026-09")
    assert page.month.currentText() == "2026-10"
    assert page.notes.toPlainText() == "October draft"
    repo.save_monthly_snapshot_notes.assert_not_called()


def test_month_transition_discard_does_not_write_either_month(history, monkeypatch):
    page, repo, saved = history
    page.notes.setPlainText("Discard this draft")
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.Discard)
    page.month.setCurrentText("2026-09")
    assert page.notes.toPlainText() == "September saved"
    assert saved["2026-10"] == "October saved"
    assert page._note_drafts == {}
    repo.save_monthly_snapshot_notes.assert_not_called()


def test_failed_note_save_cancels_transition_and_preserves_draft(history, monkeypatch):
    page, repo, _ = history
    page.notes.setPlainText("Keep on save failure")
    repo.save_monthly_snapshot_notes.side_effect = OSError("synthetic full disk")
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.Save)
    page.month.setCurrentText("2026-09")
    assert page.month.currentText() == "2026-10"
    assert page.notes.toPlainText() == "Keep on save failure"
    assert page._note_drafts == {"2026-10": "Keep on save failure"}


def test_explicit_note_save_keeps_whitespace_and_clears_dirty_state(history):
    page, repo, _ = history
    page.notes.setPlainText("  exact notes\n")
    page.notes.document().setModified(True)
    page.save_notes()
    repo.save_monthly_snapshot_notes.assert_called_once_with(
        "2026-10", "2026-10-01", "2026-10-31", "  exact notes\n"
    )
    assert not page.notes.document().isModified()
    assert page._note_drafts == {}


def test_month_reload_cancel_preserves_selector_and_draft(history, monkeypatch):
    page, repo, _ = history
    page.notes.setPlainText("Still editing")
    repo.report_months.return_value = ["2026-09"]
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.Cancel)
    page.reload_months()
    assert page.month.currentText() == "2026-10"
    assert page.notes.toPlainText() == "Still editing"


def test_month_drafts_never_move_between_months(history, monkeypatch):
    page, repo, saved = history
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.Save)
    page.notes.setPlainText("October's own draft")
    page.month.setCurrentText("2026-09")
    page.notes.setPlainText("September's own draft")
    page.month.setCurrentText("2026-10")
    assert saved == {"2026-10": "October's own draft", "2026-09": "September's own draft"}
    assert page.notes.toPlainText() == "October's own draft"
    assert [call.args[0] for call in repo.save_monthly_snapshot_notes.call_args_list] == [
        "2026-10", "2026-09",
    ]


@pytest.mark.parametrize("decision", [QMessageBox.Save, QMessageBox.Discard])
def test_close_save_or_discard_resolves_pending_notes(history, monkeypatch, decision):
    page, repo, saved = history
    page.notes.setPlainText("Resolve before closing")
    monkeypatch.setattr(QMessageBox, "question", lambda *args: decision)
    event = QCloseEvent()
    page.closeEvent(event)
    assert event.isAccepted()
    assert page._note_drafts == {}
    if decision == QMessageBox.Save:
        assert saved["2026-10"] == "Resolve before closing"
    else:
        repo.save_monthly_snapshot_notes.assert_not_called()


def test_refresh_save_reloads_only_after_success(history, monkeypatch):
    page, repo, saved = history
    page.notes.setPlainText("Save before refresh")
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.Save)
    page.refresh()
    assert page.notes.toPlainText() == saved["2026-10"] == "Save before refresh"
    assert not page.notes.document().isModified()
    assert repo.save_monthly_snapshot_notes.call_count == 1


def test_history_close_can_be_cancelled(history, monkeypatch):
    page, _, _ = history
    page.notes.setPlainText("Do not close")
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.Cancel)
    event = QCloseEvent()
    page.closeEvent(event)
    assert not event.isAccepted()
    assert page.notes.toPlainText() == "Do not close"


def test_actual_navigation_hook_stops_before_page_transition(history, monkeypatch):
    from main import LiquidFinanceApp

    page, _, _ = history
    page.notes.setPlainText("Navigation draft")
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.Cancel)

    class Pages(dict):
        def existing(self, key):
            return self.get(key)

    owner = SimpleNamespace(
        pages=Pages(historico=page, dashboard=object()),
        _current_page_key="historico", _page_scroll_positions={},
    )
    LiquidFinanceApp.goto(owner, "dashboard")
    assert owner._current_page_key == "historico"
    assert owner._page_scroll_positions == {}


def test_actual_shutdown_hook_stops_before_shutdown_side_effects(history, monkeypatch):
    from main import LiquidFinanceApp

    page, _, _ = history
    page.notes.setPlainText("Shutdown draft")
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.Cancel)
    pages = MagicMock()
    pages.existing.side_effect = lambda key: page if key == "historico" else None
    owner = SimpleNamespace(pages=pages, _closing=False, _shutdown_started=False)
    event = QCloseEvent()
    LiquidFinanceApp.closeEvent(owner, event)
    assert not event.isAccepted()
    assert owner._closing is False
    assert owner._shutdown_started is False


def test_heatmap_replacement_clears_hover_geometry(qapp):
    chart = HeatmapChart(lambda: Theme())
    chart.set_data([{"date": "2026-10-01", "expense": 10}])
    chart.hover_index = 0
    chart.tooltip_rects = [(0, QRectF(0, 0, 20, 20), {"date": "2026-10-01"})]
    chart.set_data([])
    assert chart.hover_index is None
    assert chart.tooltip_rects == []
    chart.close()


def test_replaced_series_removes_missing_pin(qapp):
    chart = BarChart(lambda: Theme())
    chart.set_items([("Old", 10, "#123456")])
    chart.pin_detail_item({"label": "Old", "value": 10})
    chart.set_items([("New", 20, "#123456")])
    assert chart._pinned_detail_item is None
    chart.close()


def test_replaced_series_preserves_only_still_valid_pin(qapp):
    chart = BarChart(lambda: Theme())
    chart.set_items([("Keep", 10, "#123456")])
    chart.pin_detail_item({"label": "Keep", "value": 10})
    chart.set_items([("Keep", 10, "#123456"), ("New", 5, "#123456")])
    assert chart._pinned_detail_item == {"label": "Keep", "value": 10}
    chart.close()


def test_destroyed_focus_container_closes_without_deleted_qt_access(qapp):
    host = QWidget()
    container = QWidget(host)
    layout = QVBoxLayout(container)
    chart = BarChart(lambda: Theme())
    layout.addWidget(chart)
    dialog = chart.open_focus_view()
    container.deleteLater()
    QCoreApplication.sendPostedEvents(container, QEvent.DeferredDelete)
    qapp.processEvents()
    if isValid(dialog):
        dialog.close()
        QCoreApplication.sendPostedEvents(dialog, QEvent.DeferredDelete)
    assert not isValid(chart)
    host.close()


def test_normal_focus_close_disconnects_owner_destruction(qapp):
    host = QWidget()
    layout = QVBoxLayout(host)
    chart = BarChart(lambda: Theme())
    layout.addWidget(chart)
    dialog = chart.open_focus_view()
    callbacks = []
    dialog.closeEvent = lambda event: callbacks.append("unexpected owner close callback")
    # Restore explicitly, keeping the dialog alive to inspect the connection.
    dialog._restore_chart()
    host.deleteLater()
    QCoreApplication.sendPostedEvents(host, QEvent.DeferredDelete)
    qapp.processEvents()
    assert not callbacks
    assert not isValid(host)
    assert not isValid(chart)
    assert not isValid(dialog)


def test_focus_close_then_owner_deletion_before_dialog_deferred_delete(qapp):
    host = QWidget()
    layout = QVBoxLayout(host)
    chart = BarChart(lambda: Theme())
    layout.addWidget(chart)
    dialog = chart.open_focus_view()
    dialog.close()
    host.deleteLater()
    QCoreApplication.sendPostedEvents(host, QEvent.DeferredDelete)
    qapp.processEvents()
    assert not isValid(host)
    assert not isValid(chart)
    assert not isValid(dialog)


def test_destroying_focus_window_owner_uses_native_child_ownership(qapp):
    host = QWidget()
    layout = QVBoxLayout(host)
    chart = BarChart(lambda: Theme())
    layout.addWidget(chart)
    dialog = chart.open_focus_view()
    host.deleteLater()
    QCoreApplication.sendPostedEvents(host, QEvent.DeferredDelete)
    qapp.processEvents()
    assert not isValid(host)
    assert not isValid(chart)
    assert not isValid(dialog)


def test_focus_title_updates_when_privacy_changes_during_focus(qapp):
    theme = Theme()
    host = QWidget()
    layout = QVBoxLayout(host)
    chart = BarChart(lambda: theme)
    chart.setAccessibleName("Private account PT501234")
    layout.addWidget(chart)
    dialog = chart.open_focus_view()
    assert "PT501234" in dialog.windowTitle()
    theme.privacy = True
    chart.set_items([("Example", 10, "#123456")])
    assert "PT501234" not in dialog.windowTitle()
    assert "PT501234" not in dialog._heading_label.text()
    dialog.close()
    QCoreApplication.sendPostedEvents(dialog, QEvent.DeferredDelete)
    host.close()
    host.deleteLater()
    QCoreApplication.sendPostedEvents(host, QEvent.DeferredDelete)
    assert not isValid(dialog)
    assert not isValid(chart)
    assert not isValid(host)
