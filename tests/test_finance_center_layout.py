from __future__ import annotations

import os
from datetime import date, timedelta

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableView,
)

from ui.core import Theme
from ui.pages.core_pages import FinanceCenterPage


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


class _Repo:
    def __init__(self) -> None:
        self.settings: dict[str, str] = {}

    def setting(self, key: str, default: str = "") -> str:
        return self.settings.get(key, default)

    def set_setting(self, key: str, value: str) -> None:
        self.settings[key] = value


class _Analytics:
    @staticmethod
    def cashflow_calendar_forecast(_days: int, today: date) -> dict:
        return {
            "starting_balance": 1_000.0,
            "method_label": "Previsão de teste.",
            "points": [],
            "events": [
                {
                    "date": today.isoformat(),
                    "name": "Ginásio",
                    "source": "subscription",
                    "amount": -33.0,
                    "overdue": False,
                }
            ],
            "summaries": {
                "60": {
                    "ending_balance": 900.0,
                    "minimum_balance": 850.0,
                    "minimum_date": today.isoformat(),
                    "tone": "positive",
                    "status": "Estável",
                    "expected_income": 500.0,
                    "total_outflow": 600.0,
                    "overdue_count": 0,
                }
            },
        }

    @staticmethod
    def financial_alert_center(_today: date) -> list[dict]:
        return []


class _DenseAnalytics(_Analytics):
    @staticmethod
    def cashflow_calendar_forecast(_days: int, today: date) -> dict:
        payload = _Analytics.cashflow_calendar_forecast(_days, today)
        payload["events"] = [
            {
                "date": (today + timedelta(days=index)).isoformat(),
                "name": f"Compromisso {index + 1}",
                "source": "subscription" if index % 2 else "scheduled",
                "amount": -(20.0 + index),
                "overdue": False,
            }
            for index in range(24)
        ]
        return payload


class _App:
    def __init__(self, analytics=None) -> None:
        self.repo = _Repo()
        self.analytics_service = analytics or _Analytics()
        # This layout fixture supplies analytics and administrative discovery;
        # real read-only database behavior is tested in Phase 6 contracts.
        self.product_review_service = _AdministrativeReview()
        self.payments_service = object()
        self.import_service = object()
        self.theme = Theme(dark=False)
        self.pages: dict[str, object] = {}

    def goto(self, _page: str) -> None:
        pass


class _AdministrativeReview:
    @staticmethod
    def administrative_items(**_kwargs):
        return []

    @staticmethod
    def visible_alerts(alerts, **_kwargs):
        return list(alerts)


def _process_layout(qapp: QApplication, page: FinanceCenterPage) -> None:
    for _ in range(4):
        page.reflow_responsive_grids()
        qapp.processEvents()


def test_finance_center_calendar_and_treasury_stay_compact_at_narrow_width(qapp):
    page = FinanceCenterPage(_App())
    page.show()
    qapp.processEvents()
    page.resize(560, 1_500)
    _process_layout(qapp, page)

    calendar_view = page.calendar.findChild(QTableView, "qt_calendar_calendarview")
    assert calendar_view is not None
    widths = [
        calendar_view.columnWidth(column)
        for column in range(calendar_view.model().columnCount())
    ]

    assert page.minimumSizeHint().width() <= 560
    assert page.calendar.height() == 320
    assert page.calendar_card.height() <= 378
    assert page.forecast.height() <= 260
    assert page.forecast.v.spacing() == 5
    assert page.forecast.title_label is not None
    assert page.forecast.title_label.geometry().top() <= 12
    assert page.forecast.geometry().top() > page.calendar_card.geometry().bottom()
    assert len(widths) == 7
    assert min(widths) >= 60
    assert sum(widths) >= calendar_view.viewport().width() - 7
    assert all(
        calendar_view.horizontalHeader().sectionResizeMode(column)
        == QHeaderView.ResizeMode.Stretch
        for column in range(7)
    )
    assert calendar_view.textElideMode() == Qt.TextElideMode.ElideNone

    event_row = page.recurring.findChild(QFrame, "CashflowEventRow")
    assert event_row is not None
    event_text = " ".join(label.text() for label in event_row.findChildren(QLabel))
    event_actions = event_row.findChildren(QPushButton)
    assert "Ginásio" in event_text
    assert "Subscrição" in event_text
    assert "33 €" in event_text
    assert [button.text() for button in event_actions] == ["Abrir"]
    assert "→" not in event_text

    page.resize(1_280, 1_500)
    _process_layout(qapp, page)
    calendar_position = page.calendar_card.mapTo(page, QPoint(0, 0))
    recurring_position = page.recurring.mapTo(page, QPoint(0, 0))
    forecast_position = page.forecast.mapTo(page, QPoint(0, 0))

    assert recurring_position.x() > calendar_position.x()
    assert abs(recurring_position.y() - calendar_position.y()) <= 2
    assert forecast_position.y() > (calendar_position.y() + page.calendar_card.height())
    assert page.recurring.width() <= 520

    page.close()


def test_finance_center_dense_events_scroll_without_growing_the_page(qapp):
    page = FinanceCenterPage(_App(_DenseAnalytics()))
    page.resize(1_280, 1_100)
    page.show()
    _process_layout(qapp, page)

    event_rows = page.recurring_events_host.findChildren(QFrame, "CashflowEventRow")
    assert len(event_rows) == 24
    assert "24 evento(s)" in page.recurring_summary.text()
    assert page.recurring.height() <= 620
    assert page.recurring_scroll.height() <= 540
    assert page.recurring_scroll.verticalScrollBar().maximum() > 0

    page.close()
