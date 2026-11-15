"""Automated unit and integration tests for Phase 13 Batch 5 (P1 priority defects).

Defects covered:
1. SEC-EXP-01: CSV formula injection prevention with leading control characters (\t, \r, \n).
2. SEC-EXP-03: Windows file-lock PermissionError handling on PDF/CSV exports in UI pages.
3. BNK-PAR-03: OFX parser tolerance for multiline XML tags and mixed SGML tags.
4. BNK-PAR-04: Bank date parser 2-digit year support, 6-digit compact dates, and century anchoring.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from PySide6.QtWidgets import QApplication

from services.bank_parsers import OFXParser
from ui.pages.movements import MovementsPage
from ui.pages.reports import MonthlyReviewPage, PdfReportsPage
from utils.helpers import parse_date, parse_flexible_date
from utils.output_security import spreadsheet_safe_cell, spreadsheet_safe_row


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def make_mock_app(tmp_path: Path) -> MagicMock:
    mock_app = MagicMock()
    mock_app.theme.privacy = False
    mock_app.theme.dark = False
    mock_app.content_scroll.viewport().width.return_value = 1280
    mock_app.content_scroll.verticalScrollBar().value.return_value = 0
    mock_app.page_density = "normal"
    db_file = tmp_path / "financas_batch5.db"
    db_file.touch()
    mock_app.repo.db.path = db_file
    mock_app.repo.db.conn = MagicMock()
    mock_app.repo.setting.side_effect = lambda k, default="": str(default)
    mock_app.repo.saved_filters.return_value = []
    mock_app.repo.monthly_snapshot_notes.return_value = ""
    mock_app.repo.available_years.return_value = [2026]
    mock_app.repo.month_period.return_value = ("2026-08-01", "2026-08-31", "2026-08")
    mock_app.repo.month_target.return_value = {"expense_target": 0.0}
    mock_app.repo.totals_for_payment_period.return_value = {
        "income": 1500.0,
        "expenses": 800.0,
        "savings": 700.0,
    }
    mock_app.repo.top_categories_payment_period.return_value = []
    mock_app.repo.monthly_evolution_payment_period.return_value = []
    mock_app.repo.savings_rate_milestones.return_value = []
    mock_app.repo.list_transactions.return_value = []
    mock_app.repo.filter_tags.return_value = []
    mock_app.repo.account_names.return_value = []
    mock_app.repo.category_names.return_value = []
    mock_app.repo.active_holding_classes.return_value = []
    mock_app.repo.holdings_summary.return_value = {"total_value": 0.0, "holdings": []}
    mock_app.analytics_service.payment_method_totals.return_value = []
    mock_app.analytics_service.totals_for_period.return_value = {
        "income": 1500.0,
        "expenses": 800.0,
        "savings": 700.0,
        "investments": 0.0,
    }
    mock_app.analytics_service.category_totals.return_value = []
    mock_app.analytics_service.monthly_summary_for_period.return_value = []
    mock_app.analytics_service.detailed_financial_score.return_value = {"score": 85}
    mock_app.analytics_service.monthly_review_data.return_value = {
        "totals": {"income": 1500.0, "expenses": 800.0, "savings": 700.0},
        "previous_month": {"savings": 600.0},
        "categories": [],
        "uncategorized": 0,
        "no_attachment": 0,
        "duplicates": [],
    }
    mock_app.analytics_service.annual_report_data.return_value = {}
    return mock_app


# ============================================================================
# 1. SEC-EXP-01: CSV Formula Injection Protection
# ============================================================================


def test_sec_exp_01_csv_formula_injection_control_chars():
    """Verify spreadsheet_safe_cell prefixes formulas preceded by control characters."""
    # Control characters preceding executable formulas
    assert spreadsheet_safe_cell("\t=CMD()").startswith("'")
    assert spreadsheet_safe_cell("\t+SUM(A1:A2)").startswith("'")
    assert spreadsheet_safe_cell("\t-10+20").startswith("'")
    assert spreadsheet_safe_cell("\t@IMPORTXML(A1)").startswith("'")
    assert spreadsheet_safe_cell("\r=1+1").startswith("'")
    assert spreadsheet_safe_cell("\n=1+1").startswith("'")
    assert spreadsheet_safe_cell("\r\n=1+1").startswith("'")
    assert spreadsheet_safe_cell(" \t=1+1").startswith("'")
    assert spreadsheet_safe_cell("\t|calc").startswith("'")
    assert spreadsheet_safe_cell("\tmalicious_cmd").startswith("'")

    # Standard formula prefixes
    assert spreadsheet_safe_cell("=1+1") == "'=1+1"
    assert spreadsheet_safe_cell("+1+1") == "'+1+1"
    assert spreadsheet_safe_cell("@SUM(1,2)") == "'@SUM(1,2)"
    assert spreadsheet_safe_cell("  =cmd()") == "'  =cmd()"

    # Legitimate numbers must NOT be altered
    assert spreadsheet_safe_cell(-12.50) == -12.50
    assert spreadsheet_safe_cell("-12.50") == "-12.50"
    assert spreadsheet_safe_cell("-12,50") == "-12,50"
    assert spreadsheet_safe_cell("+100") == "+100"
    assert spreadsheet_safe_cell(0) == 0

    # Normal strings
    assert spreadsheet_safe_cell("Supermercado Continente") == "Supermercado Continente"
    assert spreadsheet_safe_cell("Salário Mensal") == "Salário Mensal"


def test_sec_exp_01_spreadsheet_safe_row():
    """Verify spreadsheet_safe_row cleans an entire row."""
    row = ["\t=1+1", -25.50, "Continente", "@cmd"]
    safe = spreadsheet_safe_row(row)
    assert safe[0].startswith("'")
    assert safe[1] == -25.50
    assert safe[2] == "Continente"
    assert safe[3].startswith("'")


# ============================================================================
# 2. SEC-EXP-03: Windows File Lock PermissionError Handling
# ============================================================================


def test_sec_exp_03_movements_export_pdf_permission_error(qapp, tmp_path: Path):
    """MovementsPage.export_pdf intercepts locked file PermissionError gracefully."""
    app = make_mock_app(tmp_path)
    page = MovementsPage(app)
    page.rows = MagicMock(return_value=[{"id": 1, "description": "Teste", "amount": -10.0}])
    locked_pdf = str(tmp_path / "locked_report.pdf")

    with patch("PySide6.QtWidgets.QFileDialog.getSaveFileName", return_value=(locked_pdf, "PDF (*.pdf)")):
        with patch("builtins.open", side_effect=PermissionError("Permission denied: file locked on Windows")):
            with patch("PySide6.QtWidgets.QMessageBox.critical") as mock_critical:
                page.export_pdf()
                mock_critical.assert_called_once()
                title, msg = mock_critical.call_args[0][1], mock_critical.call_args[0][2]
                assert title == "Ficheiro em Uso"
                assert "locked_report.pdf" in msg


def test_sec_exp_03_movements_export_csv_permission_error(qapp, tmp_path: Path):
    """MovementsPage.export_csv intercepts locked file PermissionError gracefully."""
    app = make_mock_app(tmp_path)
    page = MovementsPage(app)
    page.rows = MagicMock(return_value=[{"id": 1, "description": "Teste", "amount": -10.0}])
    locked_csv = str(tmp_path / "locked_movements.csv")

    with patch("PySide6.QtWidgets.QFileDialog.getSaveFileName", return_value=(locked_csv, "CSV (*.csv)")):
        with patch("builtins.open", side_effect=PermissionError("Permission denied: file in use by Excel")):
            with patch("PySide6.QtWidgets.QMessageBox.critical") as mock_critical:
                page.export_csv()
                mock_critical.assert_called_once()
                title, msg = mock_critical.call_args[0][1], mock_critical.call_args[0][2]
                assert title == "Ficheiro em Uso"
                assert "locked_movements.csv" in msg


def test_sec_exp_03_reports_export_review_permission_error(qapp, tmp_path: Path):
    """MonthlyReviewPage.export_month_pdf catches PermissionError on locked PDF."""
    app = make_mock_app(tmp_path)
    with patch.object(MonthlyReviewPage, "refresh", lambda self: None):
        page = MonthlyReviewPage(app)
    page.text = MagicMock()
    page.text.toPlainText.return_value = "Relatório de Teste"
    locked_pdf = str(tmp_path / "locked_revisao.pdf")

    with patch("PySide6.QtWidgets.QFileDialog.getSaveFileName", return_value=(locked_pdf, "PDF (*.pdf)")):
        with patch("builtins.open", side_effect=PermissionError("Permission denied")):
            with patch("PySide6.QtWidgets.QMessageBox.critical") as mock_critical:
                page.export_month_pdf()
                mock_critical.assert_called_once()
                title, msg = mock_critical.call_args[0][1], mock_critical.call_args[0][2]
                assert title == "Ficheiro em Uso"
                assert "locked_revisao.pdf" in msg


def test_sec_exp_03_pdf_reports_page_export_pdf_permission_error(qapp, tmp_path: Path):
    """PdfReportsPage._on_export_html_ready emits toast on locked PDF file."""
    app = make_mock_app(tmp_path)
    with patch.object(PdfReportsPage, "refresh", lambda self: None):
        page = PdfReportsPage(app)
    locked_pdf = str(tmp_path / "locked_anual.pdf")

    with patch("PySide6.QtWidgets.QFileDialog.getSaveFileName", return_value=(locked_pdf, "PDF (*.pdf)")):
        with patch("builtins.open", side_effect=PermissionError("Permission denied")):
            page._on_export_html_ready("<h1>Relatorio</h1>", "2026")
            app.show_toast.assert_called_once()
            toast_msg = app.show_toast.call_args[0][0]
            assert "em uso" in toast_msg.lower()


# ============================================================================
# 3. BNK-PAR-03: OFX Multiline Tags and Mixed SGML
# ============================================================================


def test_bnk_par_03_ofx_multiline_tags_and_attributes():
    """OFXParser correctly extracts multiline tags and tags with attributes."""
    ofx_content = """OFXHEADER:100
DATA:OFXSGML
VERSION:102
SECURITY:NONE
ENCODING:USASCII
CHARSET:1252
COMPRESSION:NONE
OLDFILEUID:NONE
NEWFILEUID:NONE

<OFX>
  <BANKMSGSRSV1>
    <STMTTRNRS>
      <STMTRS>
        <BANKACCTFROM>
          <BANKID>
            0035
          </BANKID>
          <ACCTID>
            123456789
          </ACCTID>
        </BANKACCTFROM>
        <BANKTRANLIST>
          <STMTTRN>
            <TRNTYPE>DEBIT</TRNTYPE>
            <DTPOSTED>
              20260824120000
            </DTPOSTED>
            <TRNAMT>
              -35.40
            </TRNAMT>
            <FITID>TX_MULTILINE_001</FITID>
            <NAME>
              Continente Matosinhos
            </NAME>
            <MEMO id="m1">
              Compras Semanais
              Mercearia e Fruta
            </MEMO>
          </STMTTRN>
          <STMTTRN>
            <TRNTYPE>CREDIT</TRNTYPE>
            <DTPOSTED>20260825</DTPOSTED>
            <TRNAMT>
              1250.00
            </TRNAMT>
            <FITID>TX_MULTILINE_002</FITID>
            <NAME>Empresa XPTO</NAME>
            <MEMO>Vencimento Agosto</MEMO>
          </STMTTRN>
        </BANKTRANLIST>
      </STMTRS>
    </STMTTRNRS>
  </BANKMSGSRSV1>
</OFX>
"""
    statement = OFXParser.parse(ofx_content)
    assert statement.account_id == "123456789"
    assert statement.bank_id == "0035"
    assert len(statement.rows) == 2

    r1 = statement.rows[0]
    assert r1["Data"] == "2026-08-24"
    assert r1["Valor"] == "-35.40"
    assert r1["Comerciante"] == "Continente Matosinhos"
    assert "Compras Semanais" in r1["Descrição"]
    assert "Mercearia e Fruta" in r1["Descrição"]

    r2 = statement.rows[1]
    assert r2["Data"] == "2026-08-25"
    assert r2["Valor"] == "1250.00"
    assert r2["Comerciante"] == "Empresa XPTO"


def test_bnk_par_03_ofx_sgml_open_tags_without_closing():
    """OFXParser correctly handles legacy SGML open leaf tags without closing tags."""
    sgml_content = """<OFX>
<BANKMSGSRSV1>
<STMTTRNRS>
<STMTRS>
<BANKTRANLIST>
<STMTTRN>
<TRNTYPE>DEBIT
<DTPOSTED>20260820
<TRNAMT>-15.00
<FITID>SGML001
<NAME>Farmacia Central
<MEMO>Medicamentos
</STMTTRN>
</BANKTRANLIST>
</STMTRS>
</STMTTRNRS>
</BANKMSGSRSV1>
</OFX>
"""
    statement = OFXParser.parse(sgml_content)
    assert len(statement.rows) == 1
    row = statement.rows[0]
    assert row["Data"] == "2026-08-20"
    assert row["Valor"] == "-15.00"
    assert row["Comerciante"] == "Farmacia Central"


# ============================================================================
# 4. BNK-PAR-04: Bank Date Parser 2-Digit Year and Century Anchoring
# ============================================================================


def test_bnk_par_04_parse_flexible_date_two_digit_years():
    """Verify parse_flexible_date and parse_date handle 2-digit years with century anchoring."""
    # Standard Portuguese and European bank statements
    assert parse_flexible_date("24/08/26") == date(2026, 8, 24)
    assert parse_flexible_date("24-08-26") == date(2026, 8, 24)
    assert parse_flexible_date("24.08.26") == date(2026, 8, 24)
    assert parse_flexible_date("05/03/25") == date(2025, 3, 5)

    # Single-digit day and month with 2-digit year
    assert parse_flexible_date("5/8/26") == date(2026, 8, 5)
    assert parse_flexible_date("1/2/26") == date(2026, 2, 1)
    assert parse_flexible_date("5-8-26") == date(2026, 8, 5)

    # Short ISO format: YY-MM-DD
    assert parse_flexible_date("26-08-24") == date(2024, 8, 26)

    # Century anchoring: 2-digit years >= 69 are anchored to 2000-2099
    assert parse_flexible_date("25/08/85") == date(2085, 8, 25)
    assert parse_flexible_date("25/08/99") == date(2099, 8, 25)
    assert parse_flexible_date("25/08/70") == date(2070, 8, 25)

    # Compact 6-digit dates: YYMMDD or DDMMYY
    assert parse_flexible_date("260824") == date(2026, 8, 24)
    assert parse_flexible_date("240826") == date(2024, 8, 26)

    # parse_date alias in utils.helpers
    assert parse_date("24/08/26") == date(2026, 8, 24)
    assert parse_date("2026-08-24") == date(2026, 8, 24)
    assert parse_date("") is None
    assert parse_date(None) is None
