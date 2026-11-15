"""Unit and integration tests for Phase 6 fixes (AUD-31 and AUD-32).

AUD-31: Robust I/O and permission exception handling during file exports in UI pages.
AUD-32: Elimination of function/constructor calls in lambda default arguments (B008).
"""

import ast
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication

from ui.core import BasePage
from ui.pages.movements import MovementsPage
from ui.pages.planning import BudgetsPage
from ui.pages.dynamic_analysis import DynamicAnalysisPage
from ui.pages.history import HistoryPage
from ui.pages.panorama import PanoramaPage
from ui.pages.reports import ReportsPage, PdfReportsPage


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _dispose_owned_pages(qapp):
    existing = set(qapp.topLevelWidgets())
    yield
    for widget in qapp.topLevelWidgets():
        if widget not in existing and isinstance(widget, BasePage):
            widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)


def make_mock_app(tmp_path):
    mock_app = MagicMock()
    # These export-error tests have no background-query ownership contract.
    mock_app.background_task_registry.can_start.return_value = False
    mock_app.theme.privacy = False
    mock_app.theme.dark = False
    mock_app.content_scroll.viewport().width.return_value = 1280
    mock_app.content_scroll.verticalScrollBar().value.return_value = 0
    mock_app.page_density = "normal"
    db_file = tmp_path / "financas.db"
    db_file.touch()
    mock_app.repo.db.path = db_file
    mock_app.repo.db.conn = MagicMock()
    mock_app.repo.setting.side_effect = lambda k, default="": str(default)
    mock_app.repo.saved_filters.return_value = []
    mock_app.repo.monthly_snapshot_notes.return_value = ""
    mock_app.repo.month_period.return_value = ("2026-08-01", "2026-08-31", "2026-08")
    mock_app.repo.month_target.return_value = {"expense_target": 0.0}
    mock_app.repo.totals_for_payment_period.return_value = {
        "income": 1000.0,
        "expenses": 500.0,
        "savings": 500.0,
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
        "income": 1000.0,
        "expenses": 500.0,
        "savings": 500.0,
        "investments": 0.0,
    }
    mock_app.analytics_service.category_totals.return_value = []
    mock_app.analytics_service.monthly_summary_for_period.return_value = []
    return mock_app


# ============================================================================
# AUD-32 Tests: AST Verification of Lambda Defaults
# ============================================================================


def test_aud32_no_calls_in_lambda_defaults_core_pages():
    """Verify ui/pages/core_pages.py has no function/method calls in lambda defaults."""
    path = Path("ui/pages/core_pages.py")
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Lambda):
            for default in node.args.defaults:
                assert not isinstance(
                    default, ast.Call
                ), f"Found ast.Call in lambda default at {path}:{node.lineno}: {ast.unparse(default)}"


def test_aud32_no_calls_in_lambda_defaults_table_state():
    """Verify ui/core/table_state.py has no function/method calls in lambda defaults."""
    path = Path("ui/core/table_state.py")
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Lambda):
            for default in node.args.defaults:
                assert not isinstance(
                    default, ast.Call
                ), f"Found ast.Call in lambda default at {path}:{node.lineno}: {ast.unparse(default)}"


# ============================================================================
# AUD-31 Tests: I/O & Permission Error Resilience in Export Routines
# ============================================================================


def test_aud31_movements_export_csv_handles_permission_error(qapp, tmp_path):
    """MovementsPage.export_csv catches PermissionError without crashing."""
    locked_file = tmp_path / "locked_movements.csv"
    app = make_mock_app(tmp_path)
    page = MovementsPage(app)
    page.rows = MagicMock(return_value=[{"id": 1, "description": "Test", "amount": -10.0}])

    with patch("PySide6.QtWidgets.QFileDialog.getSaveFileName", return_value=(str(locked_file), "CSV (*.csv)")):
        with patch("builtins.open", side_effect=PermissionError("Permission denied: file locked by Excel")):
            with patch("PySide6.QtWidgets.QMessageBox.critical") as mock_critical:
                page.export_csv()
                mock_critical.assert_called_once()
                assert "Erro ao exportar CSV" in mock_critical.call_args[0][2]


def test_aud31_movements_export_pdf_handles_print_error(qapp, tmp_path):
    """MovementsPage.export_pdf catches print/I/O error without crashing."""
    pdf_file = tmp_path / "report.pdf"
    app = make_mock_app(tmp_path)
    page = MovementsPage(app)
    page.rows = MagicMock(return_value=[])

    with patch("PySide6.QtWidgets.QFileDialog.getSaveFileName", return_value=(str(pdf_file), "PDF (*.pdf)")):
        with patch("PySide6.QtPrintSupport.QPrinter"):
            with patch("PySide6.QtGui.QTextDocument.print_", side_effect=OSError("Disk write failed")):
                with patch("PySide6.QtWidgets.QMessageBox.critical") as mock_critical:
                    page.export_pdf()
                    mock_critical.assert_called_once()
                    assert "Erro ao exportar PDF" in mock_critical.call_args[0][2]


def test_aud31_pdf_reports_export_pdf_ready_handles_error(qapp, tmp_path):
    """PdfReportsPage._on_export_html_ready catches errors and emits a danger toast."""
    pdf_file = tmp_path / "report.pdf"
    app = make_mock_app(tmp_path)
    with patch.object(PdfReportsPage, "refresh", lambda self: None):
        page = PdfReportsPage(app)

    with patch("PySide6.QtWidgets.QFileDialog.getSaveFileName", return_value=(str(pdf_file), "PDF (*.pdf)")):
        with patch("PySide6.QtPrintSupport.QPrinter"):
            with patch("PySide6.QtGui.QTextDocument.print_", side_effect=PermissionError("File in use by PDF reader")):
                page._on_export_html_ready("<html>Test</html>", "2026-09")
                app.show_toast.assert_called_once()
                assert app.show_toast.call_args[1].get("tone") == "danger"


@pytest.mark.parametrize("error,expected_message", [
    (PermissionError("Permission denied"), "O ficheiro está aberto noutro programa"),
    (OSError("Drive disconnected"), "Erro ao exportar análise"),
])
def test_aud31_reports_export_txt_handles_io_error(qapp, tmp_path, error, expected_message):
    """ReportsPage.export_txt catches I/O errors and displays a critical message box."""
    txt_file = tmp_path / "analise.txt"
    app = make_mock_app(tmp_path)
    with patch.object(ReportsPage, "refresh", lambda self: None):
        page = ReportsPage(app)
    page.analytics_service = MagicMock()
    page.analytics_service.monthly_summary_for_period = MagicMock(return_value=[])

    with patch("PySide6.QtWidgets.QFileDialog.getSaveFileName", return_value=(str(txt_file), "Texto (*.txt)")):
        with patch("builtins.open", side_effect=error):
            with patch("PySide6.QtWidgets.QMessageBox.critical") as mock_critical:
                page.export_txt()
                mock_critical.assert_called_once()
                message = mock_critical.call_args[0][2]
                assert expected_message in message
                if isinstance(error, PermissionError):
                    assert txt_file.name in message
                    assert "Feche o ficheiro e tente novamente" in message


def test_aud31_planning_export_csv_handles_io_error(qapp, tmp_path):
    """BudgetsPage.export_csv catches I/O errors cleanly."""
    csv_file = tmp_path / "orcamentos.csv"
    app = make_mock_app(tmp_path)
    page = BudgetsPage(app)
    page._rows = MagicMock(return_value=[])

    with patch("PySide6.QtWidgets.QFileDialog.getSaveFileName", return_value=(str(csv_file), "CSV (*.csv)")):
        with patch("builtins.open", side_effect=OSError("Drive disconnected")):
            with patch("PySide6.QtWidgets.QMessageBox.critical") as mock_critical:
                page.export_csv()
                mock_critical.assert_called_once()
                assert "Erro ao exportar CSV" in mock_critical.call_args[0][2]


def test_aud31_dynamic_analysis_export_csv_handles_io_error(qapp, tmp_path):
    """DynamicAnalysisPage.export_csv catches I/O errors cleanly."""
    csv_file = tmp_path / "analise_dinamica.csv"
    app = make_mock_app(tmp_path)
    page = DynamicAnalysisPage(app)
    page.rows = MagicMock(return_value=[])

    with patch("PySide6.QtWidgets.QFileDialog.getSaveFileName", return_value=(str(csv_file), "CSV (*.csv)")):
        with patch("builtins.open", side_effect=PermissionError("File locked")):
            with patch("PySide6.QtWidgets.QMessageBox.critical") as mock_critical:
                page.export_csv()
                mock_critical.assert_called_once()
                assert "Erro ao exportar CSV" in mock_critical.call_args[0][2]


def test_aud31_history_export_html_handles_io_error(qapp, tmp_path):
    """HistoryPage.export_html catches write_text failure cleanly."""
    html_file = tmp_path / "history.html"
    app = make_mock_app(tmp_path)
    page = HistoryPage(app)
    page.period = MagicMock(return_value=("2026-08-01", "2026-08-31"))
    page.analytics_service = MagicMock()
    page.analytics_service.totals_for_period = MagicMock(
        return_value={"income": 1000, "expenses": 500, "savings": 500, "investments": 0}
    )
    page.analytics_service.category_totals = MagicMock(return_value=[])
    page._comparison_html = MagicMock(return_value="")

    with patch("PySide6.QtWidgets.QFileDialog.getSaveFileName", return_value=(str(html_file), "HTML (*.html)")):
        with patch.object(Path, "write_text", side_effect=PermissionError("File locked")):
            with patch("PySide6.QtWidgets.QMessageBox.critical") as mock_critical:
                page.export_html()
                mock_critical.assert_called_once()
                assert "Erro ao exportar relatório" in mock_critical.call_args[0][2]


def test_aud31_panorama_export_html_handles_io_error(qapp, tmp_path):
    """PanoramaPage.export_html catches write_text failure cleanly."""
    html_file = tmp_path / "panorama.html"
    app = make_mock_app(tmp_path)
    page = PanoramaPage(app)
    page.selected_period = MagicMock(return_value=("2026-08-01", "2026-08-31", "2026-08", 2026, 8))
    page.repo = MagicMock()
    page.repo.month_target.return_value = {"expense_target": 0.0}
    page.repo.totals_for_payment_period.return_value = {"income": 1000, "expenses": 500, "savings": 500}
    page.repo.top_categories_payment_period.return_value = []

    with patch("PySide6.QtWidgets.QFileDialog.getSaveFileName", return_value=(str(html_file), "HTML (*.html)")):
        with patch.object(Path, "write_text", side_effect=PermissionError("Access is denied")):
            with patch("PySide6.QtWidgets.QMessageBox.critical") as mock_critical:
                page.export_html()
                mock_critical.assert_called_once()
                assert "Erro ao exportar panorama" in mock_critical.call_args[0][2]
