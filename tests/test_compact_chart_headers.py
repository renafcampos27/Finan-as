from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication


def _expected_label_height(label) -> int:
    metrics = label.fontMetrics()
    margins = label.contentsMargins()
    horizontal_padding = margins.left() + margins.right() + label.margin() * 2
    vertical_padding = margins.top() + margins.bottom() + label.margin() * 2
    if bool(label.property("stableCompactHeight")):
        if label.wordWrap():
            fitted = metrics.boundingRect(
                QRect(
                    0,
                    0,
                    max(1, label.width() - horizontal_padding),
                    1_000_000,
                ),
                Qt.TextWordWrap | Qt.AlignLeft | Qt.AlignTop,
                label.text(),
            ).height()
            if fitted > 0:
                return max(
                    metrics.height() + vertical_padding,
                    fitted + vertical_padding,
                )
        return metrics.height() + vertical_padding
    if label.wordWrap():
        fitted = label.heightForWidth(max(1, label.width()))
        if fitted > 0:
            return max(metrics.height(), fitted)
    return metrics.height()


def _expected_widget_height(widget) -> int:
    if widget.hasHeightForWidth():
        fitted = widget.heightForWidth(max(1, widget.width()))
        if fitted > 0:
            return fitted
    return max(1, widget.sizeHint().height())


def _first_content_gap(card) -> int:
    title = card.title_label
    assert title is not None
    anchor = title
    anchor_index = card.v.indexOf(anchor)
    if anchor_index < 0:
        anchor = title.parentWidget()
        anchor_index = card.v.indexOf(anchor)
    assert anchor_index >= 0
    for index in range(anchor_index + 1, card.v.count()):
        item = card.v.itemAt(index)
        widget = item.widget()
        if widget is not None and widget.isHidden():
            continue
        return item.geometry().top() - anchor.geometry().bottom() - 1
    return 0


def _settled_first_content_gap(card, maximum_gap: int) -> int:
    """Wait for queued responsive-height updates before measuring geometry."""

    gap = _first_content_gap(card)
    for _attempt in range(20):
        if 0 <= gap <= maximum_gap:
            return gap
        refresh = getattr(card, "refresh_compact_heights", None)
        if callable(refresh):
            refresh()
        QApplication.processEvents()
        card.v.activate()
        QApplication.processEvents()
        QTest.qWait(10)
        gap = _first_content_gap(card)
    return gap


def test_audited_card_headers_and_descriptions_stay_compact(
    tmp_path,
    monkeypatch,
) -> None:
    import database.repo.base as repo_base
    import services.documents as document_service_module
    import utils.app_logging as app_logging

    database_path = tmp_path / "compact-chart-headers.sqlite3"
    monkeypatch.setattr(repo_base, "db_path", lambda: database_path)
    monkeypatch.setattr(
        document_service_module,
        "attachments_dir",
        lambda: tmp_path / "anexos",
    )
    monkeypatch.setattr(app_logging, "configure_logging", lambda *_a, **_k: None)

    from main import LiquidFinanceApp

    qapp = QApplication.instance() or QApplication([])
    window = LiquidFinanceApp()
    window.repo.set_setting("backup_on_close", "0")
    window.backup_service.run_backup_on_close = lambda silent=True: None
    window.resize(1600, 900)
    window.show()
    QTest.qWait(250)

    try:
        dynamic_page = window.pages["dinamica"]
        history_page = window.pages["historico"]
        panorama_page = window.pages["panorama"]
        risk_page = window.pages["risco"]
        score_page = window.pages["score"]
        simulations_page = window.pages["simulacoes"]
        budgets_page = window.pages["orcamentos"]
        dashboard_page = window.pages["dashboard"]
        checks = (
            ("dinamica", dynamic_page.results_card),
            ("historico", history_page.monthly_distribution_card),
            ("historico", history_page.comparison_card),
            ("panorama", panorama_page.payment),
            ("panorama", panorama_page.diagnostic),
            ("risco", risk_page.actions_card),
            ("score", score_page.trend_card),
            ("simulacoes", simulations_page.main_card),
            ("orcamentos", budgets_page.smart_budget_card),
            ("orcamentos", budgets_page.forecast_card),
            ("dashboard", dashboard_page.expense_work_time_card),
        )
        for width in (960, 1280, 1600, 1920):
            window.resize(width, 1080 if width == 1920 else 900)
            for page_key, card in checks:
                window.goto(page_key)
                QTest.qWait(45)
                title = card.title_label
                assert title is not None
                margins = card.v.contentsMargins()
                assert margins.top() == 7
                assert card.v.spacing() == 4
                assert abs(title.mapTo(card, QPoint(0, 0)).y() - margins.top()) <= 2
                assert abs(title.height() - _expected_label_height(title)) <= 2, (
                    f"{page_key}: {title.text()!r} at {width}px "
                    f"({title.width()}x{title.height()})"
                )
                assert 0 <= _settled_first_content_gap(card, 4) <= 4

            window.goto("dashboard")
            QTest.qWait(80)
            QApplication.processEvents()

            for card, hint in (
                (
                    dashboard_page.cashflow_pulse_card,
                    dashboard_page.cashflow_pulse_hint,
                ),
                (
                    dashboard_page.savings_efficiency_card,
                    dashboard_page.savings_efficiency_hint,
                ),
            ):
                title = card.title_label
                assert title is not None
                margins = card.v.contentsMargins()
                assert margins.top() == 5
                assert card.v.spacing() == 2
                assert 0 <= _settled_first_content_gap(card, 2) <= 2
                assert abs(hint.height() - _expected_label_height(hint)) <= 2
                assert hint.alignment() & Qt.AlignTop
                title_top = title.mapTo(card, QPoint()).y()
                title_bottom = title_top + title.height()
                hint_top = hint.mapTo(card, QPoint()).y()
                assert title_top <= margins.top() + 2
                assert 0 <= hint_top - title_bottom <= 10
                context_header = title.parentWidget()
                assert context_header.height() <= (
                    max(
                        title.height(),
                        card.context_badge.sizeHint().height(),
                    )
                    + 2
                )

            responsive_dashboard_labels = [
                dashboard_page.page_title_label,
                dashboard_page.page_subtitle_label,
                dashboard_page.data_updated_label,
                dashboard_page.chart_data_badge,
                dashboard_page.insights.summary_title,
                dashboard_page.insights.summary_meta,
                dashboard_page.insights.scope_summary,
                dashboard_page.insights.method_label,
            ]
            for row in dashboard_page.insights.priority_rows:
                responsive_dashboard_labels.extend(
                    (
                        row.scope_label,
                        row.title_label,
                        row.message_label,
                        row.metrics_label,
                        row.evidence_label,
                    )
                )
            for label in responsive_dashboard_labels:
                if not label.isVisibleTo(dashboard_page) or not label.text().strip():
                    continue
                assert label.width() > 0
                assert label.height() + 2 >= _expected_label_height(label), (
                    f"dashboard: {label.objectName()} {label.text()!r} at "
                    f"{width}px ({label.width()}x{label.height()})"
                )
            assert dashboard_page.budget_status_label.width() > 0
            assert dashboard_page.budget_status_label.height() + 2 >= (
                _expected_label_height(dashboard_page.budget_status_label)
            )

            fitted_labels = (
                simulations_page.choose_title,
                simulations_page.controls_title,
                simulations_page.result_title,
                simulations_page.preset_description,
                simulations_page.income_effort_label,
                simulations_page.chart_detail,
                budgets_page.forecast_hint,
                budgets_page.smart_budget_hint,
                history_page.comparison_label,
            )
            for label in fitted_labels:
                assert abs(label.height() - _expected_label_height(label)) <= 2

            fitted_widgets = (
                simulations_page.intro_wrap,
                simulations_page.quick_actions_wrap,
            )
            for widget in fitted_widgets:
                assert abs(widget.height() - _expected_widget_height(widget)) <= 2

        assert dynamic_page.chart.height() >= dynamic_page.chart.minimumHeight()
        assert history_page.bar.height() >= history_page.bar.minimumHeight()
    finally:
        window.auto_backup_timer.stop()
        window.pages["dashboard"].shutdown_workers()
        window.close()
        qapp.processEvents()
        window.repo.db.conn.close()
