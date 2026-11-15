"""I5/I6/I8: explain existing numbers and discover work without doing it."""

from __future__ import annotations

from types import SimpleNamespace
from datetime import date
import sqlite3
import json

import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication
from PySide6.QtTest import QTest
from shiboken6 import isValid

from database.engine import FinanceDB
from database.repo import Repo
from services.analytics import AnalyticsService
from services.payments import PaymentsService
from services.product_review import ProductReviewService
from services.reimbursements import ReimbursementsService
from ui.core.theme import Theme
from ui.pages.core_pages import FinanceCenterPage
from ui.pages.reports import PdfReportsPage
from ui.product_review import QualityChecklistDialog


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def dispose(widget):
    if isValid(widget):
        widget.close()
        widget.deleteLater()
        QCoreApplication.sendPostedEvents(widget, QEvent.Type.DeferredDelete)
    QApplication.processEvents()


@pytest.fixture
def app(tmp_path, empty_profile, qapp):
    repo = Repo(FinanceDB(tmp_path / "product.sqlite3"))
    payments = PaymentsService(repo)
    app = SimpleNamespace(
        repo=repo,
        theme=Theme(),
        analytics_service=AnalyticsService(repo, payments),
        payments_service=payments,
        import_service=SimpleNamespace(),
        pages={},
        goto=lambda page: None,
    )
    yield app
    repo.db.conn.close()


def test_existing_report_has_reproducible_period_scope(app):
    page = PdfReportsPage(app)
    try:
        assert "Âmbito e origem" in page.html()
    finally:
        dispose(page)


def test_finance_center_has_explicit_snooze_control(app):
    page = FinanceCenterPage(app)
    try:
        assert hasattr(page, "alert_snooze_btn")
    finally:
        dispose(page)


def test_data_quality_checklist_is_optional_and_accessible(app):
    page = FinanceCenterPage(app)
    try:
        assert hasattr(page, "quality_check_btn")
        assert page.quality_check_btn.accessibleName()
    finally:
        dispose(page)


def test_period_context_uses_payment_date_and_does_not_extrapolate(app):
    repo = app.repo
    category = repo.category_rows("expense")[0]["name"]
    first = repo.add_transaction("2025-12-20", "Paid in January", -20, category, "Bank")
    repo.set_transaction_meta_ext(first, payment_date="2026-01-10")
    second = repo.add_transaction("2026-01-25", "Paid in February", 5, category, "Bank")
    repo.set_transaction_meta_ext(second, payment_date="2026-02-02")
    service = ProductReviewService(repo)
    context = service.period_context("2026-01-01", "2026-02-28", as_of="2026-01-31")
    assert context["calendar_state"] == "in_progress"
    assert (context["recorded"], context["future_records"]) == (2, 1)
    assert (
        service.period_context("2025-12-01", "2025-12-31", as_of="2026-01-31")[
            "recorded"
        ]
        == 0
    )
    assert (
        service.period_context("2027-01-01", "2027-12-31", as_of="2026-01-31")[
            "calendar_state"
        ]
        == "future"
    )
    assert repo.totals_for_payment_period("2026-01-01", "2026-02-28")["savings"] == -15
    text = service.period_summary_text(
        "2026-01-01", "2026-02-28", as_of="2026-01-31", privacy=True
    )
    assert "em curso" in text and "•••" in text and "sem projeção" in text


def test_discovery_and_quality_do_not_write_or_generate_payments(app):
    repo = app.repo
    category = repo.category_rows("expense")[0]["name"]
    source = repo.add_transaction(
        "2026-10-01", "Synthetic invoice", -150, category, "Bank"
    )
    ReimbursementsService(repo).create(
        source, 100, label="Synthetic reimbursement", due_date="2026-10-10"
    )
    repo.add_scheduled_payment(
        "Unpaid commitment", category, "", 20, "Bank", "", "2026-10-01", 1, 1
    )
    repo.commit()
    repo.db.conn.execute(
        "INSERT INTO settings(key,value) VALUES('pending-owner','preserved')"
    )
    before = repo.db.conn.total_changes
    denied = []
    write_actions = {
        sqlite3.SQLITE_INSERT,
        sqlite3.SQLITE_UPDATE,
        sqlite3.SQLITE_DELETE,
        sqlite3.SQLITE_CREATE_TABLE,
        sqlite3.SQLITE_DROP_TABLE,
        sqlite3.SQLITE_ALTER_TABLE,
        sqlite3.SQLITE_TRANSACTION,
        sqlite3.SQLITE_ATTACH,
        sqlite3.SQLITE_DETACH,
    }

    def readonly(action, *args):
        if action in write_actions:
            denied.append((action, args))
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    repo.db.conn.set_authorizer(readonly)
    try:
        service = ProductReviewService(repo)
        items = service.administrative_items(as_of=date(2026, 10, 31))
        quality = service.quality_checklist(as_of="2026-10-31")
        service.period_context("2026-01-01", "2026-12-31", as_of="2026-10-31")
        visible = service.visible_alerts(items, as_of="2026-10-31")
        assert {row["kind"] for row in visible} >= {
            "reimbursement",
            "missing_documents",
        }
        assert all(row["origin"] and "deadline" in row for row in visible)
        assert len(quality) == 5
        assert not denied
        assert repo.db.conn.total_changes == before and repo.db.conn.in_transaction
        assert repo.movement_count() == 1
        assert repo.scalar("SELECT COUNT(*) FROM scheduled_payment_occurrences") == 0
    finally:
        repo.db.conn.set_authorizer(None)
        repo.db.conn.rollback()


@pytest.mark.parametrize("choice", ["snooze", "dismiss", "reset"])
def test_administrative_choice_preserves_caller_transaction_and_rollback(app, choice):
    repo = app.repo
    service = ProductReviewService(repo)
    repo.db.conn.execute(
        "INSERT INTO settings(key,value) VALUES('pending-choice','preserved')"
    )
    if choice == "snooze":
        service.snooze("synthetic:1", "2026-11-08", as_of="2026-11-01")
    elif choice == "dismiss":
        service.dismiss("synthetic:1")
    else:
        service.reset_choices()
    assert repo.db.conn.in_transaction
    repo.db.conn.rollback()
    assert repo.setting("pending-choice", "") == ""
    assert repo.setting("administrative_snoozes_v1", "") == ""
    assert repo.setting("dismissed_financial_alerts", "") == ""


def test_snooze_expires_read_only_on_the_selected_day(app):
    service = ProductReviewService(app.repo)
    alert = {"key": "synthetic:1", "kind": "subscription", "date": "2026-11-08"}
    service.snooze(alert["key"], "2026-11-08", as_of="2026-11-01")
    before = app.repo.db.conn.total_changes
    assert service.visible_alerts([alert], as_of="2026-11-07") == []
    assert len(service.visible_alerts([alert], as_of="2026-11-08")) == 1
    assert app.repo.db.conn.total_changes == before
    assert not app.repo.db.conn.in_transaction
    with pytest.raises(ValueError):
        service.snooze(alert["key"], "20261108", as_of="2026-11-01")


def test_private_administrative_rows_hide_names_amounts_and_tooltips(app):
    page = FinanceCenterPage(app)
    try:
        app.theme.privacy = True
        page._alerts_data = [
            {
                "key": "private",
                "title": "SECRET-EMPLOYER",
                "message": "SECRET-RECEIPT",
                "origin": "Reembolsos",
                "amount": 9123.45,
                "category": "SECRET-CATEGORY",
            }
        ]
        page._fill_alert_table()
        visible = " ".join(
            page.alert_table.item(0, column).text()
            + page.alert_table.item(0, column).toolTip()
            for column in range(5)
        )
        assert "SECRET-" not in visible and "9123" not in visible
        assert "Dados ocultos" in visible
    finally:
        dispose(page)


def test_quality_dialog_observes_replaced_theme_and_disposes_owned_timer(app):
    dialog = QualityChecklistDialog(app)
    timer = dialog._privacy_timer
    counts = [str(row["count"]) for row in dialog._rows]
    try:
        assert [dialog.table.item(i, 2).text() for i in range(len(counts))] == counts
        app.theme = Theme()
        app.theme.privacy = True
        app.theme.dark = not dialog._presentation_state[1]
        QTest.qWait(180)
        assert all(dialog.table.item(i, 2).text() == "•••" for i in range(len(counts)))
        assert dialog.styleSheet() == app.theme.stylesheet()
        app.theme = Theme()
        dialog.update_privacy()
        assert [dialog.table.item(i, 2).text() for i in range(len(counts))] == counts
        app.theme = Theme(primary_color_name="Verde")
        dialog.update_privacy()
        assert dialog.styleSheet() == app.theme.stylesheet()
    finally:
        dispose(dialog)
    assert not isValid(timer)


def test_quality_origin_navigation_is_explicit_and_does_not_write(app):
    destinations = []
    app.goto = destinations.append
    dialog = QualityChecklistDialog(app)
    before = app.repo.db.conn.total_changes
    try:
        assert not destinations
        dialog.table.cellWidget(0, 4).click()
        assert destinations == [dialog._rows[0]["action_page"]]
        assert app.repo.db.conn.total_changes == before
    finally:
        dispose(dialog)


def test_document_dismissal_does_not_hide_replacement_evidence_of_same_count(app):
    repo = app.repo
    category = repo.category_rows("expense")[0]["name"]
    source = repo.add_transaction(
        "2026-10-01", "First evidence", -150, category, "Bank"
    )
    service = ProductReviewService(repo)
    original = service.administrative_items(as_of="2026-11-01")
    first = next(row for row in original if row["kind"] == "missing_documents")
    service.dismiss(first["key"])
    service.snooze(first["key"], "2026-11-08", as_of="2026-11-01")
    repo.db.conn.execute("DELETE FROM transactions WHERE id=?", (source,))
    repo.commit()
    repo.add_transaction("2026-10-02", "Different evidence", -150, category, "Bank")
    replacement = service.administrative_items(as_of="2026-11-01")
    current = next(row for row in replacement if row["kind"] == "missing_documents")
    assert current["key"] != first["key"]
    assert current in service.visible_alerts(replacement, as_of="2026-11-01")
    assert first["key"] in json.loads(repo.setting("dismissed_financial_alerts"))
