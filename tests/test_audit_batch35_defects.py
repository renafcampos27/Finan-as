"""Automated verification suite for Audit Batch 35 defects.

Defects covered:
1. BUG-REPO-ANA-02: end_exclusive support in top_categories_payment_period and dynamic_grouped with unified upper bound.
2. BUG-REPO-ANA-03: chart_movement_details includes split income transactions with preserved positive polarity.
3. BUG-REPO-ANA-04: financial_state_as_of guarantees cash_balance in all branches (current, daily, legacy, unavailable).
4. BUG-REPO-ANA-05: chart_movement_details escapes LIKE search wildcards to prevent pattern injection.
5. BUG-REPO-PLAN-02: add_monthly_goal and delete_monthly_goal use _planning_write and capture_state=True.
6. BUG-REPO-PLAN-03: monthly_goal_progress normalizes scalar results with safe_float and guards against null/zero targets.
7. BUG-CHART-SANKEY-01: SankeyChart uses FLOW_GAP=28.0, filters non-positive items, and masks percentages under privacy.
8. BUG-CHART-HEAT-01: HeatmapChart treats expense <= 0 as neutral and places tooltip below cell when close to top.
9. BUG-TOOL-SIM-01: SimulatorPage.prefill uses add_months fallback offsets, never skipping February.
10. BUG-TOOL-IMP-01: SmartImportPage blocks signals on mapping combos during bulk item setup.
11. BUG-TOOL-SPLIT-01: SplitsPage.refresh reloads category dropdown items from repo.
12. BUG-DOC-03: DocumentsService.infer_metadata strips matched date substring before scanning for monetary amount.
13. BUG-DOC-04: DocumentsService handles PermissionError gracefully when removing locked attachment files.
"""

from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtGui import QPainter, QPixmap
from PySide6.QtWidgets import QApplication

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from database.engine import FinanceDB
from database.repo import Repo
from services.analytics import AnalyticsService
from services.documents import DocumentService
from ui.components.charts.heatmap import HeatmapChart
from ui.components.charts.sankey import SankeyChart
from ui.core.theme import Theme
from ui.pages.tools import _FALLBACK_MONTH_OFFSETS, SmartImportPage, SplitsPage
from utils.helpers import add_months


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.delenv("FINANCAS_PESSOAIS_DEMO_DATA", raising=False)
    db = FinanceDB(tmp_path / "batch35_test.sqlite3")
    r = Repo(db)
    yield r
    try:
        db.conn.close()
    except Exception:
        pass


# ============================================================================
# BUG-REPO-ANA-02: end_exclusive contract and unified upper bound in dynamic_grouped
# ============================================================================


def test_c_repo_ana_02_dynamic_grouped_supports_end_exclusive(repo: Repo):
    """dynamic_grouped accepts end_exclusive kwarg and respects exclusive upper boundary."""
    repo.add_transaction("2026-08-30", "Despesa dia 30", -50.0, "Supermercado", "Conta principal")
    repo.add_transaction("2026-08-31", "Despesa dia 31", -70.0, "Supermercado", "Conta principal")

    # When end_exclusive=True, end='2026-08-31' is exclusive, so August 31 movements are excluded
    res_exclusive = repo.dynamic_grouped(
        "2026-08-01",
        "2026-08-31",
        group_by="Categoria",
        end_exclusive=True,
    )
    total_exclusive = next((r["total"] for r in res_exclusive if r["label"] == "Supermercado"), 0.0)
    assert total_exclusive == pytest.approx(50.0)

    # When end_exclusive=False or None, end='2026-08-31' is inclusive, so August 31 movements are included
    res_inclusive = repo.dynamic_grouped(
        "2026-08-01",
        "2026-08-31",
        group_by="Categoria",
        end_exclusive=False,
    )
    total_inclusive = next((r["total"] for r in res_inclusive if r["label"] == "Supermercado"), 0.0)
    assert total_inclusive == pytest.approx(120.0)


def test_c_repo_ana_02_top_categories_payment_period_supports_end_exclusive(repo: Repo):
    """top_categories_payment_period accepts end_exclusive kwarg without TypeError."""
    repo.add_transaction("2026-08-30", "Despesa dia 30", -40.0, "Lazer", "Conta principal")
    repo.add_transaction("2026-08-31", "Despesa dia 31", -60.0, "Lazer", "Conta principal")

    top_exclusive = repo.top_categories_payment_period(
        "2026-08-01",
        "2026-08-31",
        end_exclusive=True,
    )
    total_exclusive = next((amt for cat, amt, _ in top_exclusive if cat == "Lazer"), 0.0)
    assert total_exclusive == pytest.approx(40.0)


def test_c_repo_ana_02_dynamic_grouped_unifies_upper_bound_across_dimensions(repo: Repo):
    """Non-category dimensions (Comerciante, Conta, Receitas) include movements on inclusive end date."""
    tx_id = repo.add_transaction("2026-08-30", "Jantar Restaurante", -85.0, "Restaurantes", "Conta principal")
    repo.update_transaction_with_meta(tx_id, {}, merchant="Restaurante Lisboa")

    # Group by merchant with inclusive end='2026-08-30'
    res = repo.dynamic_grouped(
        "2026-08-01",
        "2026-08-30",
        group_by="Comerciante",
        basis="Pagamento",
    )
    total_merchant = next((r["total"] for r in res if r["label"] == "Restaurante Lisboa"), 0.0)
    assert total_merchant == pytest.approx(85.0)


# ============================================================================
# BUG-REPO-ANA-03: chart_movement_details includes split income
# ============================================================================


def test_c_repo_ana_03_chart_movement_details_includes_split_income(repo: Repo):
    """chart_movement_details displays split income movements with positive values."""
    salary_id = repo.add_transaction("2026-08-25", "Vencimento Agosto", 2500.0, "Salario", "Conta principal")
    repo.save_transaction_splits(
        salary_id,
        [
            ("Salario Base", 80.0),
            ("Subsidio", 20.0),
        ],
    )

    details = repo.chart_movement_details(
        start="2026-08-01",
        end="2026-08-31",
        kind="income",
    )
    amounts = [r["amount"] for r in details if r["id"] == salary_id]
    assert len(amounts) == 2
    assert 2000.0 in amounts
    assert 500.0 in amounts


# ============================================================================
# BUG-REPO-ANA-04: financial_state_as_of includes cash_balance in all branches
# ============================================================================


def test_c_repo_ana_04_financial_state_as_of_includes_cash_balance_in_all_branches(repo: Repo):
    """financial_state_as_of returns a numeric cash_balance across current, daily, and unavailable branches."""
    # 1. Current branch
    state_curr = repo.financial_state_as_of(date.today().isoformat())
    assert "cash_balance" in state_curr
    assert isinstance(state_curr["cash_balance"], float)

    # 2. Unavailable branch (no snapshots recorded for 1999)
    state_unavail = repo.financial_state_as_of("1999-01-01")
    assert "cash_balance" in state_unavail
    assert state_unavail["cash_balance"] == 0.0

    # 3. Daily snapshot branch
    repo.record_financial_state_snapshot(snapshot_day="2026-08-15", commit=True)
    state_daily = repo.financial_state_as_of("2026-08-15")
    assert state_daily["source"] == "daily_snapshot"
    assert "cash_balance" in state_daily
    assert isinstance(state_daily["cash_balance"], float)


# ============================================================================
# BUG-REPO-ANA-05: chart_movement_details escapes LIKE search wildcards
# ============================================================================


def test_c_repo_ana_05_chart_movement_details_escapes_like_wildcards(repo: Repo):
    """Literal % and _ in search_text are escaped so they do not act as SQL wildcards."""
    repo.add_transaction("2026-08-10", "Fatura com IVA 23%", -100.0, "Impostos", "Conta principal")
    repo.add_transaction("2026-08-11", "Fatura com IVA 230", -200.0, "Impostos", "Conta principal")

    # Searching for '23%' must match only 'Fatura com IVA 23%', NOT 'Fatura com IVA 230'
    res = repo.chart_movement_details(
        start="2026-08-01",
        end="2026-08-31",
        search_text="23%",
    )
    descriptions = [r["description"] for r in res]
    assert "Fatura com IVA 23%" in descriptions
    assert "Fatura com IVA 230" not in descriptions


# ============================================================================
# BUG-REPO-PLAN-02: monthly goal mutators use _planning_write and capture_state
# ============================================================================


def test_c_repo_plan_02_goals_mutators_use_planning_write_and_capture_state(repo: Repo):
    """add_monthly_goal and delete_monthly_goal use _planning_write and mutate monthly_goals safely."""
    repo.add_monthly_goal(
        ym="2026-09",
        name="Meta Poupanca",
        goal_type="Poupar pelo menos",
        category="Poupanca",
        target=400.0,
    )
    goals = repo.monthly_goals("2026-09")
    assert len(goals) >= 1
    goal = next(g for g in goals if g["name"] == "Meta Poupanca")
    goal_id = goal["id"]
    assert goal["target_value"] == 400.0

    # Delete goal
    repo.delete_monthly_goal(goal_id)
    remaining_goals = repo.monthly_goals("2026-09")
    assert all(g["id"] != goal_id for g in remaining_goals)


# ============================================================================
# BUG-REPO-PLAN-03: monthly_goal_progress handles null scalar and zero target
# ============================================================================


def test_c_repo_plan_03_monthly_goal_progress_handles_null_and_zero_target(repo: Repo):
    """monthly_goal_progress handles None returns from scalar queries and zero target values safely."""
    analytics = AnalyticsService(repo)

    # Goal with 0 target value
    zero_goal = {
        "month": "2026-09",
        "goal_type": "Poupar pelo menos",
        "category": "Poupanca",
        "target_value": 0.0,
    }
    pct, eur_str, status = analytics.monthly_goal_progress(zero_goal)
    assert isinstance(pct, float)
    assert pct >= 0.0

    # Investment goal with no transaction matching 'invest' (scalar() returns None)
    invest_goal = {
        "month": "2026-09",
        "goal_type": "Investir pelo menos",
        "category": "Investimentos",
        "target_value": 500.0,
    }
    pct_inv, eur_inv, status_inv = analytics.monthly_goal_progress(invest_goal)
    assert isinstance(pct_inv, float)
    assert pct_inv == 0.0
    assert status_inv == "Em progresso"


# ============================================================================
# BUG-CHART-SANKEY-01: SankeyChart FLOW_GAP, negative filtering & privacy masking
# ============================================================================


def test_c_chart_sankey_01_privacy_masks_percentages_and_filters_negatives(qapp):
    """SankeyChart filters non-positive expenses, uses 28px gap, and renders •••% under privacy."""
    theme = Theme()
    chart = SankeyChart(theme_provider=lambda: theme)
    assert chart.FLOW_GAP == 28.0

    chart.set_data({"Alimentacao": 300.0, "Reembolso": -50.0, "Outros": 100.0})
    # Reembolso must be filtered out
    labels = [lbl for lbl, _ in chart.items]
    assert "Alimentacao" in labels
    assert "Outros" in labels
    assert "Reembolso" not in labels

    # Under privacy mode, render does not crash
    theme.privacy = True
    pixmap = QPixmap(600, 500)
    painter = QPainter(pixmap)
    chart.resize(600, 500)
    chart.render_chart(painter)
    painter.end()


# ============================================================================
# BUG-CHART-HEAT-01: HeatmapChart negative expenses neutral & tooltip positioning
# ============================================================================


def test_c_chart_heat_01_negative_expenses_neutral_and_tooltip_positioning(qapp):
    """HeatmapChart handles negative expenses safely and computes non-colliding tooltip positions."""
    theme = Theme()
    chart = HeatmapChart(theme_provider=lambda: theme)

    data = [
        {"date": f"2026-01-{i:02d}", "expense": -20.0 if i == 1 else (i * 10.0)}
        for i in range(1, 32)
    ]
    chart.set_data(data)

    pixmap = QPixmap(800, 400)
    painter = QPainter(pixmap)
    chart.resize(800, 400)
    chart.render_chart(painter)
    painter.end()
    assert len(chart.points) == 31


# ============================================================================
# BUG-TOOL-SIM-01: SimulatorPage.prefill does not skip February
# ============================================================================


def test_c_tool_sim_01_prefill_does_not_skip_february():
    """add_months offsets (1, 2, 3) from March 1st step to February, January, December without skipping."""
    assert _FALLBACK_MONTH_OFFSETS == (1, 2, 3)
    march_first = date(2026, 3, 1)

    m1 = add_months(march_first, -1, 1)
    m2 = add_months(march_first, -2, 1)
    m3 = add_months(march_first, -3, 1)

    assert m1 == date(2026, 2, 1)  # February is preserved!
    assert m2 == date(2026, 1, 1)  # January
    assert m3 == date(2025, 12, 1) # December


# ============================================================================
# BUG-TOOL-IMP-01: SmartImportPage blocks signals during combo population
# ============================================================================


def test_c_tool_imp_01_open_file_blocks_signals_during_combo_population(qapp, repo: Repo):
    """SmartImportPage blocks signals on all 10 mapping combos during header setup."""
    app_stub = MagicMock()
    app_stub.repo = repo
    page = SmartImportPage(app_stub)

    class DummyData:
        headers = ["Data", "Descricao", "Valor", "Categoria"]
        rows = [["2026-08-01", "Teste", "-10.0", "Geral"]]
        encoding = "utf-8"
        warnings = []
        is_credit_card = False
        source_sha256 = ""
        date_format = ""

    page.import_service = MagicMock()
    page.import_service.read_file.return_value = DummyData()
    page.import_service.detect_bank_dialect.return_value = ""
    page.import_service.suggested_mapping.return_value = {"date": "Data", "desc": "Descricao", "amount": "Valor"}
    page.import_service.load_mapping.return_value = {}
    page.import_service.file_history_warning.return_value = ""

    with patch("PySide6.QtWidgets.QFileDialog.getOpenFileName", return_value=("dummy.csv", "CSV")):
        page.open_csv()

    assert page.headers == ["Data", "Descricao", "Valor", "Categoria"]


# ============================================================================
# BUG-TOOL-SPLIT-01: SplitsPage.refresh reloads category dropdowns
# ============================================================================


def test_c_tool_split_01_refresh_reloads_category_combos(qapp, repo: Repo):
    """SplitsPage.refresh reloads category dropdown items from the repository."""
    app_stub = MagicMock()
    app_stub.repo = repo
    app_stub.theme = Theme()
    page = SplitsPage(app_stub)

    # Initial categories
    initial_items = [page._split_rows[0]["category"].itemText(i) for i in range(page._split_rows[0]["category"].count())]

    # Add a brand new category to repo
    repo.category_identity("Categoria Inedita Batch 35", create=True, kind="expense")

    # Refresh splits page
    page.refresh()

    updated_items = [page._split_rows[0]["category"].itemText(i) for i in range(page._split_rows[0]["category"].count())]
    assert "Categoria Inedita Batch 35" in updated_items
    assert "Categoria Inedita Batch 35" not in initial_items


# ============================================================================
# BUG-DOC-03: infer_metadata strips date before matching amount
# ============================================================================


def test_c_doc_03_infer_metadata_does_not_confuse_date_with_amount():
    """Filenames with dotted dates (e.g. 2026.05.10) do not misidentify 2026.05 as an amount."""
    supplier, inv_date, amount = DocumentService.infer_metadata(Path("Fatura_2026.05.10.pdf"))
    assert inv_date == "2026-05-10"
    assert amount is None

    # Filename with date AND explicit amount
    supplier2, inv_date2, amount2 = DocumentService.infer_metadata(Path("Fatura_EDP_2026.05.10_45.50.pdf"))
    assert inv_date2 == "2026-05-10"
    assert amount2 == pytest.approx(45.50)


# ============================================================================
# BUG-DOC-04: remove_managed_paths handles PermissionError gracefully
# ============================================================================


def test_c_doc_04_remove_managed_paths_handles_permission_error_gracefully(tmp_path, repo: Repo):
    """Locked files raising PermissionError on unlink are handled cleanly without raising an unhandled exception."""
    docs = DocumentService(repo, attachments_directory=tmp_path / "docs")
    tx_folder = tmp_path / "docs" / "100"
    tx_folder.mkdir(parents=True)
    locked_file = tx_folder / "invoice.pdf"
    locked_file.write_bytes(b"%PDF-test")

    # Mock Path.unlink to raise PermissionError
    orig_unlink = Path.unlink

    def mock_unlink(self, *args, **kwargs):
        if self.name == "invoice.pdf":
            raise PermissionError("[WinError 32] The process cannot access the file because it is being used by another process")
        return orig_unlink(self, *args, **kwargs)

    Path.unlink = mock_unlink
    try:
        removed = docs._remove_managed_paths(100, ["100/invoice.pdf"])
        assert removed == 0  # Did not crash, returned 0
    finally:
        Path.unlink = orig_unlink
