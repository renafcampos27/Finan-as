"""Tests for Phase 21 Batch 13 P2 defects:
- TIME-04: Purchase regime temporal grouping chronological ordering.
- SEC-EXP-02: CSV sanitization without false positives on legitimate hyphens.
- SEC-EXP-04: Non-blocking PDF generation and AsyncPdfExporter.
- SEC-EXP-05: Category, entity, and percentage masking in privacy mode CSV exports.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from PySide6.QtWidgets import QApplication

from database.engine import FinanceDB
from database.repo import Repo
from services.export_documents import (
    AsyncPdfExporter,
    export_movements_to_pdf_file,
    render_html_to_pdf,
)
from ui.pages.movements import MovementsPage
from utils.output_security import (
    privacy_export_cell,
    privacy_export_row,
    spreadsheet_safe_cell,
)


@pytest.fixture(scope="session")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    db_file = tmp_path / "test_batch13.db"
    engine = FinanceDB(str(db_file))
    return Repo(engine)


def test_time04_dynamic_grouped_chronological_ordering_purchase_basis(repo: Repo) -> None:
    """TIME-04: dynamic_grouped by month/day must be sorted chronologically and respect purchase_date."""
    # Transaction 1: payment in March, but purchased in January (amount: -50.0)
    repo.add_transaction_with_meta(
        "2026-03-15",
        "Bilhete de Comboio",
        -50.0,
        "Viagens",
        "Banco Teste",
        payment_date="2026-03-15",
        purchase_date="2026-01-10",
    )

    # Transaction 2: paid and purchased in February (amount: -200.0)
    repo.add_transaction_with_meta(
        "2026-02-14",
        "Jantar",
        -200.0,
        "Alimentação",
        "Banco Teste",
        payment_date="2026-02-14",
        purchase_date="2026-02-14",
    )

    # Transaction 3: payment in January, but purchased in March (amount: -500.0)
    repo.add_transaction_with_meta(
        "2026-01-05",
        "Hotel Reserva",
        -500.0,
        "Viagens",
        "Banco Teste",
        payment_date="2026-01-05",
        purchase_date="2026-03-20",
    )

    # 1. Test Purchase Regime (basis="Compra") grouping by "Mês"
    purchase_monthly = repo.dynamic_grouped(
        "2026-01-01",
        "2026-03-31",
        group_by="Mês",
        basis="Compra",
        kind="Despesas",
    )
    # Expected chronological order: 2026-01, 2026-02, 2026-03
    labels = [r["label"] for r in purchase_monthly]
    assert labels == ["2026-01", "2026-02", "2026-03"]
    # Total for 2026-01 is 50.0, 2026-02 is 200.0, 2026-03 is 500.0
    totals = {r["label"]: r["total"] for r in purchase_monthly}
    assert totals["2026-01"] == 50.0
    assert totals["2026-02"] == 200.0
    assert totals["2026-03"] == 500.0

    # 2. Test Payment Regime (basis="Pagamento") grouping by "Mês"
    payment_monthly = repo.dynamic_grouped(
        "2026-01-01",
        "2026-03-31",
        group_by="Mês",
        basis="Pagamento",
        kind="Despesas",
    )
    labels_pay = [r["label"] for r in payment_monthly]
    assert labels_pay == ["2026-01", "2026-02", "2026-03"]
    totals_pay = {r["label"]: r["total"] for r in payment_monthly}
    assert totals_pay["2026-01"] == 500.0
    assert totals_pay["2026-02"] == 200.0
    assert totals_pay["2026-03"] == 50.0

    # 3. Test grouping by "Dia"
    daily = repo.dynamic_grouped(
        "2026-01-01",
        "2026-03-31",
        group_by="Dia",
        basis="Compra",
        kind="Despesas",
    )
    daily_labels = [r["label"] for r in daily]
    assert daily_labels == ["2026-01-10", "2026-02-14", "2026-03-20"]

    # 4. Nominal groupings (Categoria) still sort by total DESC
    by_cat = repo.dynamic_grouped(
        "2026-01-01",
        "2026-03-31",
        group_by="Categoria",
        basis="Compra",
        kind="Despesas",
    )
    assert by_cat[0]["label"] == "Viagens"
    assert by_cat[0]["total"] == 550.0
    assert by_cat[1]["total"] == 200.0


def test_sec_exp02_safe_bullet_and_text_hyphens() -> None:
    """SEC-EXP-02: Legitimate hyphens and list markers are not prefixed with apostrophe."""
    # Text notes starting with hyphens or pluses followed by whitespace
    assert spreadsheet_safe_cell("- Continente") == "- Continente"
    assert spreadsheet_safe_cell("- Farmácia Central") == "- Farmácia Central"
    assert spreadsheet_safe_cell("- Pão, leite e fruta") == "- Pão, leite e fruta"
    assert spreadsheet_safe_cell("+ Desconto de fidelização") == "+ Desconto de fidelização"
    assert spreadsheet_safe_cell("- Compra em loja - Continente") == "- Compra em loja - Continente"
    assert spreadsheet_safe_cell("-") == "-"
    assert spreadsheet_safe_cell("--") == "--"
    assert spreadsheet_safe_cell("---") == "---"

    # Numbers remain unescaped
    assert spreadsheet_safe_cell("-12.50") == "-12.50"
    assert spreadsheet_safe_cell("+100") == "+100"

    # DDE and formula injection attacks MUST still be escaped
    assert spreadsheet_safe_cell("-10+20").startswith("'")
    assert spreadsheet_safe_cell("+SUM(A1:A2)").startswith("'")
    assert spreadsheet_safe_cell("-@SUM(1,2)").startswith("'")
    assert spreadsheet_safe_cell("-=1+1").startswith("'")
    assert spreadsheet_safe_cell("- 10+20").startswith("'")
    assert spreadsheet_safe_cell("- @SUM(1,2)").startswith("'")
    assert spreadsheet_safe_cell("-cmd|' /C calc'!A0").startswith("'")
    assert spreadsheet_safe_cell("+cmd|' /C calc'!A0").startswith("'")
    assert spreadsheet_safe_cell("	- Continente").startswith("'")


def test_sec_exp04_render_html_to_pdf_and_async_exporter(qapp: QApplication, tmp_path: Path) -> None:
    """SEC-EXP-04: render_html_to_pdf and AsyncPdfExporter generate PDF off main thread."""
    html_sample = "<html><body><h1>Relatório de Teste</h1></body></html>"
    out_pdf = tmp_path / "test_report.pdf"

    # Test pure renderer with isolated QPrinter mock (prevents Windows spooler COM hanging in headless test)
    with patch("PySide6.QtPrintSupport.QPrinter"):
        with patch("PySide6.QtGui.QTextDocument.print_") as mock_print:
            rendered = render_html_to_pdf(html_sample, str(out_pdf))
            assert Path(rendered) == out_pdf.resolve()
            mock_print.assert_called_once()

    # Test movements export
    rows = [
        {"date": "2026-03-01", "description": "Supermercado", "category": "Alimentação", "account": "Ordem", "amount": -45.50},
        {"date": "2026-03-02", "description": "Salário", "category": "Rendimento", "account": "Ordem", "amount": 2000.0},
    ]
    out_mov = tmp_path / "movements_out.pdf"
    with patch("PySide6.QtPrintSupport.QPrinter"):
        with patch("PySide6.QtGui.QTextDocument.print_") as mock_print:
            res_mov = export_movements_to_pdf_file(rows, str(out_mov), privacy=False)
            assert Path(res_mov) == out_mov.resolve()
            mock_print.assert_called_once()

    # Test AsyncPdfExporter callable
    async_pdf = tmp_path / "async_out.pdf"
    op = AsyncPdfExporter(rows, str(async_pdf), privacy=True)
    with patch("PySide6.QtPrintSupport.QPrinter"):
        with patch("PySide6.QtGui.QTextDocument.print_") as mock_print:
            async_res = op()
            assert Path(async_res) == async_pdf.resolve()
            mock_print.assert_called_once()


def test_sec_exp04_movements_page_async_export(qapp: QApplication, tmp_path: Path) -> None:
    """SEC-EXP-04: MovementsPage.export_pdf(run_async=True) starts async worker task."""
    mock_app = MagicMock()
    mock_app.theme.privacy = False
    mock_app.background_task_registry = None
    mock_app.repo.setting.side_effect = lambda k, default="": str(default)
    mock_app.repo.categories.return_value = ["Alimentação", "Transporte"]
    mock_app.repo.saved_filters.return_value = []
    mock_app.content_scroll.viewport().width.return_value = 1280
    mock_app.content_scroll.verticalScrollBar().value.return_value = 0

    page = MovementsPage(mock_app)
    page.rows = MagicMock(return_value=[{"date": "2026-01-01", "description": "Item", "amount": -10.0}])

    target_pdf = str(tmp_path / "async_mov.pdf")
    with patch("PySide6.QtWidgets.QFileDialog.getSaveFileName", return_value=(target_pdf, "PDF (*.pdf)")):
        with patch.object(page, "_start_async_pdf_export") as mock_start_async:
            page.export_pdf(run_async=True)
            mock_start_async.assert_called_once_with(target_pdf)


def test_sec_exp05_privacy_export_categories_and_percentages() -> None:
    """SEC-EXP-05: Masking percentages, banking entities, and optional categories under privacy."""
    # 1. Sensitive banking and entity fields masked as "Dados ocultos"
    assert privacy_export_cell("PT50000000000000000000000", "iban", True) == "Dados ocultos"
    assert privacy_export_cell("123456789", "nif", True) == "Dados ocultos"
    assert privacy_export_cell("João Silva", "beneficiary", True) == "Dados ocultos"
    assert privacy_export_cell("EDP Comercial", "entity", True) == "Dados ocultos"
    assert privacy_export_cell("Cartão Visa", "card", True) == "Dados ocultos"

    # 2. Percentage fields masked as "••• %" under privacy
    assert privacy_export_cell(85.5, "percentage", True) == "••• %"
    assert privacy_export_cell(12.0, "percentagem", True) == "••• %"
    assert privacy_export_cell(95.0, "utilisation", True) == "••• %"
    assert privacy_export_cell("10%", "taxa", True) == "••• %"

    # When privacy=False, percentages and entities remain visible
    assert privacy_export_cell(85.5, "percentage", False) == 85.5
    assert privacy_export_cell("PT50000", "iban", False) == "PT50000"

    # 3. Categories under privacy mode:
    # Default: mask_categories=False (preserves backward compatibility)
    assert privacy_export_cell("Restaurantes", "category", True) == "Restaurantes"
    assert privacy_export_cell("Refeições fora", "subcategory", True) == "Refeições fora"

    # Opt-in mask_categories=True: masks as "Dados ocultos"
    assert privacy_export_cell("Restaurantes", "category", True, mask_categories=True) == "Dados ocultos"
    assert privacy_export_cell("Refeições fora", "subcategory", True, mask_categories=True) == "Dados ocultos"
    assert privacy_export_cell("Alimentação", "group", True, mask_categories=True) == "Dados ocultos"

    # 4. privacy_export_row with full combinations
    values = ["Almoço", "PT50000", -25.0, 75.0, "Restaurantes"]
    fields = ["description", "iban", "amount", "percentage", "category"]

    masked_row = privacy_export_row(values, fields, privacy=True, mask_categories=True)
    assert masked_row == ["Dados ocultos", "Dados ocultos", "••••• €", "••• %", "Dados ocultos"]

    unmasked_cat_row = privacy_export_row(values, fields, privacy=True, mask_categories=False)
    assert unmasked_cat_row == ["Dados ocultos", "Dados ocultos", "••••• €", "••• %", "Restaurantes"]
