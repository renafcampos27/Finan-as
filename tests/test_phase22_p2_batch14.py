"""Unit tests for Phase 22 (Batch 14 P2 defects).

Covers:
- DOC-SEC-01: Filename truncation does not reintroduce trailing spaces or dots on Windows.
- DOC-SEC-02: Bypass of reserved DOS/Win32 device names with spaces (e.g., 'CON .pdf', 'aux .txt').
- BUG-COMBOBOX-CRASH-001: Resilient year extraction from comboboxes and PDF export period.
- BUG-INVESTMENTS-CRUD-001: Protection against None items and unhandled exceptions in liability CRUD.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from unittest.mock import MagicMock, patch

from PySide6.QtWidgets import QMessageBox

from utils.helpers import safe_year_from_combo, sanitize_filename


# ==============================================================================
# DOC-SEC-01: sanitize_filename truncation rstrip
# ==============================================================================


def test_doc_sec_01_filename_truncation_rstrip_with_extension():
    """Truncating a filename that cuts at spaces must not leave trailing spaces before extension."""
    stem = "relatorio_financeiro_mensal_detalhado_de_contas_e_movimentos     "
    long_name = f"{stem}.pdf"
    # max_length = 40; ext is '.pdf' (4 chars); stem truncated to 36 chars will cut into spaces
    sanitized = sanitize_filename(long_name, max_length=40)
    assert len(sanitized) <= 40
    assert sanitized.endswith(".pdf")
    base_part = sanitized.rsplit(".", 1)[0]
    assert not base_part.endswith(" ")
    assert not base_part.endswith(".")


def test_doc_sec_01_filename_truncation_rstrip_without_extension():
    """Truncating a filename without extension must not leave trailing spaces or dots."""
    long_name = "ficheiro de arquivo de seguranca com espacos no fim     " * 3
    sanitized = sanitize_filename(long_name, max_length=50)
    assert len(sanitized) <= 50
    assert not sanitized.endswith(" ")
    assert not sanitized.endswith(".")


def test_doc_sec_01_filename_truncation_all_spaces_stem_fallback():
    """If truncated stem becomes empty after rstrip, fallback to 'ficheiro'."""
    long_name = "a" * 150 + ".pdf"
    sanitized = sanitize_filename(long_name, max_length=4)
    assert sanitized == "ficheiro.pdf"

    long_no_ext = "abc" + (" " * 50) + "xyz"
    sanitized_no_ext = sanitize_filename(long_no_ext, max_length=5)
    assert sanitized_no_ext == "abc"


# ==============================================================================
# DOC-SEC-02: sanitize_filename reserved DOS device names with spaces
# ==============================================================================


def test_doc_sec_02_reserved_win32_names_with_spaces_bypasses():
    """Filenames matching DOS device names with trailing/internal spaces must be prefixed with safe_."""
    assert sanitize_filename("CON .pdf") == "safe_CON .pdf"
    assert sanitize_filename("aux .txt") == "safe_aux .txt"
    assert sanitize_filename("NUL   .zip") == "safe_NUL   .zip"
    assert sanitize_filename("com1 .dat") == "safe_com1 .dat"
    assert sanitize_filename("lpt3 .log") == "safe_lpt3 .log"
    assert sanitize_filename("prn  .csv") == "safe_prn  .csv"


def test_doc_sec_02_legitimate_words_not_prefixed():
    """Legitimate filenames starting with substrings of reserved names must NOT be prefixed."""
    assert sanitize_filename("contact.txt") == "contact.txt"
    assert sanitize_filename("constant.pdf") == "constant.pdf"
    assert sanitize_filename("auxiliary.csv") == "auxiliary.csv"
    assert sanitize_filename("null_pointer.log") == "null_pointer.log"
    assert sanitize_filename("communication.docx") == "communication.docx"


# ==============================================================================
# BUG-COMBOBOX-CRASH-001: safe_year_from_combo and UI pages
# ==============================================================================


def test_bug_combobox_crash_001_safe_year_from_combo_unit():
    """safe_year_from_combo safely extracts 4-digit year from diverse combobox/text states."""
    mock_combo = MagicMock()

    # Valid 4-digit string
    mock_combo.currentText.return_value = "2026"
    assert safe_year_from_combo(mock_combo) == 2026

    # Embedded in text
    mock_combo.currentText.return_value = "Ano 2024"
    assert safe_year_from_combo(mock_combo) == 2024

    mock_combo.currentText.return_value = "2025 (anterior)"
    assert safe_year_from_combo(mock_combo) == 2025

    # Empty string
    mock_combo.currentText.return_value = ""
    assert safe_year_from_combo(mock_combo) == date.today().year

    # Whitespace only
    mock_combo.currentText.return_value = "   "
    assert safe_year_from_combo(mock_combo) == date.today().year

    # Non-numeric text
    mock_combo.currentText.return_value = "Sem seleção"
    assert safe_year_from_combo(mock_combo) == date.today().year
    assert safe_year_from_combo(mock_combo, default=2030) == 2030

    # None combo
    assert safe_year_from_combo(None) == date.today().year
    assert safe_year_from_combo(None, default=1999) == 1999

    # Direct integer or string passed
    assert safe_year_from_combo("2028") == 2028


def test_bug_combobox_crash_001_reports_page_ym_resilience():
    """MonthlyReviewPage and ScoreDetailsPage .ym() must never raise ValueError on empty year."""
    from ui.pages.reports import MonthlyReviewPage, ScoreDetailsPage

    # MonthlyReviewPage
    m_page = MagicMock(spec=MonthlyReviewPage)
    m_page.year = MagicMock()
    m_page.year.currentText.return_value = ""
    m_page.month = MagicMock()
    m_page.month.currentIndex.return_value = 0  # Janeiro
    ym = MonthlyReviewPage.ym(m_page)
    assert ym == f"{date.today().year:04d}-01"

    # ScoreDetailsPage
    s_page = MagicMock(spec=ScoreDetailsPage)
    s_page.year = MagicMock()
    s_page.year.currentText.return_value = "   "
    s_page.month = MagicMock()
    s_page.month.currentIndex.return_value = 11  # Dezembro
    ym_score = ScoreDetailsPage.ym(s_page)
    assert ym_score == f"{date.today().year:04d}-12"


def test_bug_combobox_crash_001_pdf_reports_export_period_resilience():
    """PdfReportsPage.export_pdf must use formatted safe year even when combobox text is empty."""
    from ui.pages.reports import PdfReportsPage

    page = MagicMock(spec=PdfReportsPage)
    page.year = MagicMock()
    page.year.currentText.return_value = ""
    page.month = MagicMock()
    page.month.currentIndex.return_value = 0  # Annual mode
    page.html.return_value = "<html>test</html>"
    page._on_export_html_ready = MagicMock()

    PdfReportsPage.export_pdf(page)
    expected_year = f"{date.today().year:04d}"
    page._on_export_html_ready.assert_called_once_with("<html>test</html>", expected_year)

    # Monthly mode
    page.month.currentIndex.return_value = 5  # May
    PdfReportsPage.export_pdf(page)
    expected_period = f"{date.today().year:04d}-05"
    page._on_export_html_ready.assert_called_with("<html>test</html>", expected_period)


def test_bug_combobox_crash_001_planning_monthly_goals_ym_resilience():
    """MonthlyGoalsPage.ym must never crash on empty combobox and return valid YYYY-MM."""
    from ui.pages.planning import MonthlyGoalsPage

    page = MagicMock(spec=MonthlyGoalsPage)
    page.year = MagicMock()
    page.year.currentText.return_value = ""
    page.month = MagicMock()
    page.month.currentIndex.return_value = 2  # Março -> month 3
    ym = MonthlyGoalsPage.ym(page)
    assert ym == f"{date.today().year:04d}-03"


# ==============================================================================
# BUG-INVESTMENTS-CRUD-001: Liability CRUD protections
# ==============================================================================


def test_bug_investments_crud_001_selected_liability_id_none_and_invalid():
    """selected_liability_id must return None safely on empty selection or non-integer cells."""
    from ui.pages.investments import PortfolioPage

    page = MagicMock(spec=PortfolioPage)
    page.liability_table = MagicMock()

    # No row selected
    page.liability_table.currentRow.return_value = -1
    assert PortfolioPage.selected_liability_id(page) is None

    # Row 0, but cell is None
    page.liability_table.currentRow.return_value = 0
    page.liability_table.item.return_value = None
    assert PortfolioPage.selected_liability_id(page) is None

    # Row 0, cell exists but empty text
    item_mock = MagicMock()
    item_mock.text.return_value = ""
    page.liability_table.item.return_value = item_mock
    assert PortfolioPage.selected_liability_id(page) is None

    # Row 0, cell text is not digits
    item_mock.text.return_value = "ABC"
    assert PortfolioPage.selected_liability_id(page) is None

    # Row 0, cell text is valid integer
    item_mock.text.return_value = " 42 "
    assert PortfolioPage.selected_liability_id(page) == 42


def test_bug_investments_crud_001_delete_liability_none_cell_fallback_and_exception():
    """delete_liability must handle None cell without AttributeError and catch repo exceptions."""
    from ui.pages.investments import PortfolioPage

    page = MagicMock(spec=PortfolioPage)
    page.selected_liability_id.return_value = 15
    page.liability_table = MagicMock()
    page.liability_table.currentRow.return_value = 0
    page.liability_table.item.return_value = None  # None cell
    page.repo = MagicMock()
    page.repo.liability_by_id.return_value = None
    page.app = MagicMock()

    with patch("ui.pages.investments.QMessageBox.question", return_value=QMessageBox.Yes) as mock_q:
        PortfolioPage.delete_liability(page)
        assert "esta dívida" in mock_q.call_args[0][2]
        page.repo.delete_liability.assert_called_once_with(15)
        page.app.notify_data_changed.assert_called_with("Dívida eliminada.")

    # SQLite OperationalError handling
    page.repo.delete_liability.side_effect = sqlite3.OperationalError("database is locked")
    with patch("ui.pages.investments.QMessageBox.question", return_value=QMessageBox.Yes),          patch("ui.pages.investments.QMessageBox.critical") as mock_crit:
        PortfolioPage.delete_liability(page)
        mock_crit.assert_called_once()


def test_bug_investments_crud_001_edit_liability_exception_handling():
    """edit_liability must catch exceptions during lookup or update and display critical message."""
    from ui.pages.investments import PortfolioPage

    page = MagicMock(spec=PortfolioPage)
    page.selected_liability_id.return_value = 20
    page.repo = MagicMock()
    page.app = MagicMock()
    page.repo.liability_by_id.side_effect = sqlite3.OperationalError("disk I/O error")

    with patch("ui.pages.investments.QMessageBox.critical") as mock_crit:
        PortfolioPage.edit_liability(page)
        mock_crit.assert_called_once()
        assert "Erro ao atualizar dívida" in mock_crit.call_args[0][2]


def test_bug_investments_crud_001_add_liability_exception_handling():
    """add_liability must catch exceptions during insertion and display critical message."""
    from ui.pages.investments import PortfolioPage

    page = MagicMock(spec=PortfolioPage)
    page.repo = MagicMock()
    page.app = MagicMock()

    mock_dialog = MagicMock()
    mock_dialog.exec.return_value = 1  # QDialog.Accepted
    mock_dialog.get_data.return_value = {
        "name": "Crédito Automóvel",
        "liability_type": "Empréstimo",
        "account": "Principal",
        "balance": 15000.0,
        "interest_rate": 6.5,
        "monthly_payment": 250.0,
        "notes": "Compra viatura",
    }
    page.repo.add_liability.side_effect = sqlite3.IntegrityError("FOREIGN KEY constraint failed")

    with patch("ui.pages.investments.LiabilityDialog", return_value=mock_dialog),          patch("ui.pages.investments.QMessageBox.critical") as mock_crit:
        PortfolioPage.add_liability(page)
        mock_crit.assert_called_once()
        assert "Erro ao adicionar dívida" in mock_crit.call_args[0][2]
