"""A05: one privacy policy for detail presentation and exported evidence."""

import csv
import io
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication, QLabel, QVBoxLayout, QWidget
from shiboken6 import isValid

from services.export_documents import build_movements_pdf_html
from ui.components.chart_details import ChartDetailDialog
from ui.components.charts import DonutChart
from ui.core.theme import Theme
from ui.pages.reports import PdfReportsPage
from utils.helpers import format_period_variation


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_csv_roundtrip_quotes_delimiters_and_newlines(qapp):
    title = 'Compra; "especial"\nsegunda linha'
    dialog = ChartDetailDialog(
        {
            "rows": [
                {
                    "date": "2026-01-01",
                    "title": title,
                    "subtitle": "Casa;Família",
                    "value": -95.3,
                }
            ]
        },
        Theme(),
    )
    try:
        rows = list(csv.reader(io.StringIO(dialog._build_rows_csv()), delimiter=";"))
        assert rows[1] == ["2026-01-01", title, "Casa;Família", "-95.3"]
    finally:
        dialog.close()


@pytest.mark.parametrize("prefix", ["", "\t", "\r", " \x01", "\ufeff", "\u200b"])
def test_csv_neutralizes_text_formulas_but_keeps_numeric_negatives(qapp, prefix):
    formula = prefix + '=HYPERLINK("https://example.invalid";"x")'
    dialog = ChartDetailDialog(
        {"rows": [{"title": formula, "subtitle": "+SUM(1;2)", "value": -9.25}]}, Theme()
    )
    try:
        cells = list(csv.reader(io.StringIO(dialog._build_rows_csv()), delimiter=";"))[
            1
        ]
        assert cells[1].startswith("'")
        assert cells[2].startswith("'")
        assert cells[3] == "-9.25"
    finally:
        dialog.close()


def test_privacy_clipboard_and_visible_rows_hide_personal_text(qapp):
    secret = "Private merchant RENATO-UNIQUE-587"
    account = "Private account REF-123456789"
    dialog = ChartDetailDialog(
        {
            "title": secret,
            "subtitle": account,
            "rows": [{"title": secret, "subtitle": account, "value": -1234.56}],
        },
        Theme(privacy=True),
    )
    try:
        dialog._copy_rows_csv()
        surfaces = [dialog.windowTitle(), QApplication.clipboard().text()]
        surfaces.extend(label.text() for label in dialog.findChildren(QLabel))
        combined = "\n".join(surfaces)
        assert secret not in combined
        assert account not in combined
        assert "1234.56" not in combined
        assert "Dados ocultos" in combined
    finally:
        QApplication.clipboard().clear()
        dialog.close()


def test_private_calculation_metadata_masks_counts_and_filters(qapp):
    dialog = ChartDetailDialog(
        {
            "calculation": {
                "period": "2026-01",
                "filters": "Conta REFERENCIA-SECRET",
                "movements": 1987,
                "data_coverage": "47 de 58 movimentos",
            }
        },
        Theme(privacy=True),
    )
    try:
        text = " ".join(label.text() for label in dialog.calculation_labels)
        assert "2026-01" in text
        assert "REFERENCIA-SECRET" not in text
        assert "1987" not in text
        assert "47 de 58" not in text
    finally:
        dialog.close()


@pytest.mark.parametrize("current,previous", [(25, 20), (25000, 1), (1, 0), (0, 0)])
def test_private_period_variation_masks_percentage_and_multiplier(current, previous):
    result = format_period_variation(current, previous, privacy=True)
    assert result["display"] == "•••%"
    assert result["delta"] == current - previous


def test_private_comparison_masks_counts_percentage_points_and_identity(qapp):
    chart = DonutChart(lambda: Theme(privacy=True))
    try:
        chart.pin_detail_item(
            {
                "label": "PRIVATE-PINNED",
                "subscriptions": 987,
                "probability_target": 72.5,
                "event_count": 987,
            }
        )
        payload = chart._apply_pinned_comparison(
            {
                "label": "other",
                "subscriptions": 1000,
                "probability_target": 98.7,
                "event_count": 1000,
            },
            {},
        )
        values = [metric["value"] for metric in payload["metrics"]]
        assert all("13" not in value and "26,2" not in value for value in values)
        assert all("•••" in value for value in values)
        assert "PRIVATE-PINNED" not in payload["subtitle"]
        assert "PRIVATE-PINNED" not in chart._pinned_badge.text()
        assert chart._format_detail_value("movements", 1987) == "•••"
        assert chart._format_detail_value("return_pct", 36.7) == "•••%"
        assert chart._format_detail_value("probability_target", 72.5) == "72,5%"
    finally:
        chart.close()


def test_private_movement_report_masks_same_text_fields_as_clipboard():
    html = build_movements_pdf_html(
        [
            {
                "date": "2026-01-01",
                "description": "PRIVATE-DESCRIPTION",
                "category": "PRIVATE-CATEGORY",
                "account": "PRIVATE-ACCOUNT",
                "amount": -945.25,
            }
        ],
        privacy=True,
    )
    assert "PRIVATE-" not in html
    assert "Total: •••" in html
    assert "945" not in html


def test_privacy_toggle_refreshes_existing_focus_and_pinned_badge(qapp):
    selected = {"theme": Theme()}
    owner = QWidget()
    layout = QVBoxLayout(owner)
    chart = DonutChart(lambda: selected["theme"])
    chart.setAccessibleName("PRIVATE-ACCOUNT-FOCUS")
    layout.addWidget(chart)
    points = [("PRIVATE-PINNED", 20, "#123456")]
    chart.set_data(points)
    chart.pin_detail_item({"label": "PRIVATE-PINNED", "value": 20})
    focus = chart.open_focus_view()
    try:
        assert "PRIVATE-ACCOUNT-FOCUS" in focus.windowTitle()
        selected["theme"] = Theme(privacy=True)
        chart.set_data(points)
        assert "PRIVATE-" not in focus.windowTitle()
        assert "PRIVATE-" not in focus._heading_label.text()
        assert "PRIVATE-" not in chart._pinned_badge.text()
    finally:
        focus.close()
        QCoreApplication.sendPostedEvents(focus, QEvent.DeferredDelete)
        owner.close()
        owner.deleteLater()
        QCoreApplication.sendPostedEvents(owner, QEvent.DeferredDelete)
        assert not isValid(focus)
        assert not isValid(chart)
        assert not isValid(owner)


@pytest.mark.parametrize("annual", [False, True])
def test_private_report_preview_and_pdf_html_share_masked_ratios_counts_and_text(
    annual,
):
    repo = SimpleNamespace(
        # I5 adds read-only period provenance; keep the private metadata
        # deliberately identifying so masking remains exercised here.
        q=lambda *_: [{"recorded": 1987, "future": 1931,
                       "first_record": "2026-01-01", "last_record": "2026-12-31"}],
        month_period=lambda y, m: (f"{y}-01-01", f"{y}-01-31", f"{y}-01"),
        totals_for_payment_period=lambda *_: {
            "income": 10000,
            "expenses": 6789,
            "savings": 3211,
        },
        top_categories_payment_period=lambda *_: [
            ("PRIVATE-CATEGORY", 6789, "#123456")
        ],
    )
    annual_data = {
        "investment_available": True,
        "investment_net_return_pct": 45.7,
        "investment_data_coverage_pct": 64.3,
        "investment_fee_impact_pp": 12.3,
        "investment_attribution": [
            {
                "name": "PRIVATE-ASSET",
                "performance_available": True,
                "contribution_pp": 87.6,
            }
        ],
        "document_suppliers": [{"supplier": "PRIVATE-SUPPLIER", "documents": 1987}],
        "documents": {"document_count": 1987, "supplier_count": 1931},
        "explanations": [{"message": "PRIVATE-EXPLANATION", "change_pct": 56.7}],
    }
    service = SimpleNamespace(
        monthly_review_data=lambda *_: {
            "uncategorized": 1987,
            "no_attachment": 1931,
            "duplicates": [],
        },
        detailed_financial_score=lambda *_: {"score": 73},
        annual_report_data=lambda *_: annual_data,
    )
    page = SimpleNamespace(
        year=SimpleNamespace(currentText=lambda: "2026"),
        month=SimpleNamespace(currentIndex=lambda: 0 if annual else 1),
        app=SimpleNamespace(theme=Theme(privacy=True)),
        repo=repo,
        analytics_service=service,
    )
    html = PdfReportsPage.html(page)
    assert "PRIVATE-" not in html
    assert "1987" not in html and "1931" not in html
    assert "32,1%" not in html and "67,9%" not in html
    assert "•••%" in html
    if annual:
        assert "87.6 p.p." not in html and "12.3 p.p." not in html
        assert "45,7%" not in html and "64,3%" not in html
