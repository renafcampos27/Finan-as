import pytest
from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication

from ui.components.charts.financial_health import ScoreBreakdownChart, ScoreTrendChart
from ui.core.theme import Theme


@pytest.fixture
def qapp():
    return QApplication.instance() or QApplication([])


def test_score_breakdown_chart_opens_single_dialog_and_emits_selection(qapp):
    """Verify ScoreBreakdownChart opens exactly one dialog when closed/rejected and emits dimension_clicked."""
    theme = Theme(dark=False)
    chart = ScoreBreakdownChart(lambda: theme)
    chart.resize(800, 400)

    rows = [
        {"key": "savings", "name": "Poupança", "score": 80, "status": "Bom"},
        {"key": "debt", "name": "Endividamento", "score": 90, "status": "Excelente"},
    ]
    chart.set_data(rows)

    # Force geometry and layout so _row_rects are populated
    chart.show()
    QApplication.processEvents()

    # Verify rows were populated
    assert len(chart._row_rects) == 2

    open_detail_calls = []

    def mock_open_detail(item, **kwargs):
        open_detail_calls.append(item)
        # Simulate user closing/rejecting the dialog
        return False

    chart.open_detail = mock_open_detail

    emitted_keys = []
    chart.dimension_clicked.connect(emitted_keys.append)

    # Simulate hover over the first row
    chart.hover_index = 0
    row_rect = chart._row_rects[0]
    click_pos = row_rect.center()

    event = QMouseEvent(
        QEvent.Type.MouseButtonPress,
        click_pos,
        click_pos,
        Qt.LeftButton,
        Qt.LeftButton,
        Qt.NoModifier,
    )

    chart.mousePressEvent(event)

    # Must have called open_detail EXACTLY ONCE (not twice!)
    assert len(open_detail_calls) == 1
    assert open_detail_calls[0]["key"] == "savings"

    # Must have emitted dimension_clicked
    assert emitted_keys == ["savings"]
    assert chart.selected_key == "savings"
    assert event.isAccepted() is True

    chart.close()


def test_score_breakdown_chart_empty_click_does_not_open_dialog(qapp):
    """Clicking on empty space (hover_index is None) should not pop up a dialog."""
    theme = Theme(dark=False)
    chart = ScoreBreakdownChart(lambda: theme)
    chart.resize(800, 400)

    rows = [
        {"key": "savings", "name": "Poupança", "score": 80, "status": "Bom"},
    ]
    chart.set_data(rows)
    chart.show()
    QApplication.processEvents()

    open_detail_calls = []
    chart.open_detail = lambda item, **kwargs: open_detail_calls.append(item) or False

    chart.hover_index = None
    event = QMouseEvent(
        QEvent.Type.MouseButtonPress,
        QPointF(10, 10),
        QPointF(10, 10),
        Qt.LeftButton,
        Qt.LeftButton,
        Qt.NoModifier,
    )

    chart.mousePressEvent(event)

    # No dialog should open when clicking outside items
    assert len(open_detail_calls) == 0
    assert event.isAccepted() is True

    chart.close()


def test_score_trend_chart_opens_single_dialog(qapp):
    """Verify ScoreTrendChart opens exactly one dialog even when closed/rejected."""
    theme = Theme(dark=False)
    chart = ScoreTrendChart(lambda: theme)
    chart.resize(800, 300)

    trend_rows = [
        {"month": "2026-07", "score": 75, "status": "Bom"},
        {"month": "2026-08", "score": 82, "status": "Muito Bom"},
    ]
    chart.set_data(trend_rows)
    chart.show()
    QApplication.processEvents()

    open_detail_calls = []

    def mock_open_detail(item, **kwargs):
        open_detail_calls.append(item)
        # Simulate user closing/rejecting the dialog
        return False

    chart.open_detail = mock_open_detail

    emitted_months = []
    chart.month_clicked.connect(emitted_months.append)

    chart.hover_index = 0
    event = QMouseEvent(
        QEvent.Type.MouseButtonPress,
        QPointF(100, 150),
        QPointF(100, 150),
        Qt.LeftButton,
        Qt.LeftButton,
        Qt.NoModifier,
    )

    chart.mousePressEvent(event)

    # Must have called open_detail EXACTLY ONCE
    assert len(open_detail_calls) == 1
    assert open_detail_calls[0]["month"] == "2026-07"
    assert event.isAccepted() is True
    # Not accepted action, so month_clicked is not emitted
    assert emitted_months == []

    chart.close()
