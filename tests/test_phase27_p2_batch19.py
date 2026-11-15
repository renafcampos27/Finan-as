# -*- coding: utf-8 -*-
"""
Test Suite for Batch 19 (P2 Defects: BUG-MOV-01, BUG-MOV-03, BUG-MOV-05, BUG-INV-01)
Phase 27 Validation.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QMessageBox, QWidget

from database.engine import FinanceDB
from database.repo import Repo
from ui.core.theme import Theme
from ui.dialogs import ClosePositionDialog, HoldingDialog
from ui.models.movements import MovementsTableModel
from ui.pages.investments import PortfolioPage
from ui.pages.movements import MovementsPage


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture
def temp_db(tmp_path: Path):
    db_path = tmp_path / "test_batch19.db"
    db = FinanceDB(db_path)
    db.init_schema()
    return db


@pytest.fixture
def repo(temp_db):
    return Repo(temp_db)


@pytest.fixture
def mock_app(repo):
    app = MagicMock()
    app.theme = Theme()
    app.theme.privacy = False
    app.theme.dark = False
    app.page_density = "normal"
    app.repo = repo
    app.background_task_registry = MagicMock()
    app.change_journal_service = MagicMock()
    app.document_service = MagicMock()
    return app


# ============================================================================
# BUG-MOV-01: Edição Individual Silenciosa ao Selecionar Múltiplas Linhas
# ============================================================================


def test_mov_01_edit_selected_no_selection_shows_info(qapp, mock_app):
    page = MovementsPage(mock_app)
    with patch.object(page, "selected_ids", return_value=[]):
        with patch.object(QMessageBox, "information") as mock_info:
            page.edit_selected()
            mock_info.assert_called_once()
            assert "Seleciona primeiro um movimento" in mock_info.call_args[0][2]
    page.deleteLater()


def test_mov_01_edit_selected_multiple_rows_cancel_aborts(qapp, mock_app):
    page = MovementsPage(mock_app)
    with patch.object(page, "selected_ids", return_value=[1, 2, 3]):
        box_mock = MagicMock()
        btn_cancel_mock = MagicMock()
        box_mock.addButton.side_effect = lambda text, role: btn_cancel_mock if "Cancelar" in text else MagicMock()
        box_mock.clickedButton.return_value = btn_cancel_mock
        with patch("ui.pages.movements.QMessageBox", return_value=box_mock):
            with patch.object(page.repo, "transaction_by_id") as mock_tx:
                page.edit_selected()
                mock_tx.assert_not_called()
    page.deleteLater()


def test_mov_01_edit_selected_multiple_rows_bulk_menu_opened(qapp, mock_app):
    page = MovementsPage(mock_app)
    with patch.object(page, "selected_ids", return_value=[1, 2, 3]):
        with patch.object(page, "open_bulk_menu") as mock_bulk:
            box_mock = MagicMock()
            btn_bulk_mock = MagicMock()
            box_mock.addButton.side_effect = lambda text, role: btn_bulk_mock if "Lote" in text else MagicMock()
            box_mock.clickedButton.return_value = btn_bulk_mock
            with patch("ui.pages.movements.QMessageBox", return_value=box_mock):
                page.edit_selected()
                mock_bulk.assert_called_once()
    page.deleteLater()


# ============================================================================
# BUG-MOV-03: Mensagem de Validação em Lote Exibe Exceção Específica
# ============================================================================


def test_mov_03_run_journaled_change_validation_error_displays_specific_message(qapp, mock_app):
    page = MovementsPage(mock_app)
    page.app.change_journal_service.run_with_snapshot.side_effect = ValueError(
        "Não é permitido alterar transferências em lote."
    )

    with patch.object(QMessageBox, "warning") as mock_warning:
        with patch.object(QMessageBox, "critical") as mock_critical:
            applied, res = page._run_journaled_change(
                [10, 11],
                lambda: None,
                label="Teste",
            )
            assert applied is False
            assert res is None
            mock_warning.assert_called_once()
            mock_critical.assert_not_called()
            warning_msg = mock_warning.call_args[0][2]
            assert "Não é permitido alterar transferências em lote." in warning_msg
    page.deleteLater()


def test_mov_03_bulk_delete_validation_error_displays_specific_message(qapp, mock_app):
    page = MovementsPage(mock_app)
    page.app.change_journal_service.delete_transactions.side_effect = RuntimeError(
        "Transação vinculada a reconciliação bloqueada."
    )

    with patch.object(QMessageBox, "question", return_value=QMessageBox.Yes):
        with patch.object(QMessageBox, "warning") as mock_warning:
            with patch.object(QMessageBox, "critical") as mock_critical:
                page.bulk_delete_selected([100])
                mock_warning.assert_called_once()
                mock_critical.assert_not_called()
                warning_msg = mock_warning.call_args[0][2]
                assert "Transação vinculada a reconciliação bloqueada." in warning_msg
    page.deleteLater()


# ============================================================================
# BUG-MOV-05: Ordenação de Categorias sem Agrupamento por Ícone ⚠️
# ============================================================================


def test_mov_05_category_sort_key_strips_warning_emoji(qapp):
    theme = Theme()
    model = MovementsTableModel(theme)

    row_clean = {"id": 1, "category": "Alimentação"}
    row_warn = {"id": 2, "category": "⚠️ Alimentação"}
    row_zoo = {"id": 3, "category": "⚠️ Zootecnia"}
    row_bar = {"id": 4, "category": "Bar e Lazer"}

    # Sort key for col 4 (Category)
    key_clean = model._sort_key(row_clean, 4)
    key_warn = model._sort_key(row_warn, 4)
    key_zoo = model._sort_key(row_zoo, 4)
    key_bar = model._sort_key(row_bar, 4)

    assert key_clean == "alimentação"
    assert key_warn == "alimentação"
    assert key_zoo == "zootecnia"
    assert key_bar == "bar e lazer"

    # Both Alimentação entries sort together and before Bar e Lazer
    sorted_rows = model._sorted_rows([row_zoo, row_clean, row_bar, row_warn], 4, Qt.AscendingOrder)
    sorted_cats = [r["category"].lstrip("⚠️").strip() for r in sorted_rows]
    assert sorted_cats == ["Alimentação", "Alimentação", "Bar e Lazer", "Zootecnia"]


def test_mov_05_sort_indicator_partial_table_status_warning(qapp, mock_app):
    page = MovementsPage(mock_app)
    page._total_filtered = 200
    page.movements_model._rows = [{"id": i} for i in range(50)]

    page._on_sort_indicator_changed(4, Qt.AscendingOrder)
    assert "ℹ️ Ordenação aplicada apenas aos 50 movimentos visíveis" in page.table_status.text()
    assert "200 no filtro" in page.table_status.text()

    # When all rows are loaded, status is not overridden with partial warning
    page._total_filtered = 50
    page.table_status.setText("Status Original")
    page._on_sort_indicator_changed(4, Qt.AscendingOrder)
    assert page.table_status.text() == "Status Original"
    page.deleteLater()


# ============================================================================
# BUG-INV-01: Alerta de Valor 0,00 € e Fluxo Dedicado de Fecho de Posição
# ============================================================================


def test_inv_01_holding_dialog_zero_value_warning(qapp):
    dialog = HoldingDialog()
    dialog.name.setText("ETF Global")
    dialog.cost_known.setChecked(False)
    dialog.value.setValue(0.0)

    # If user rejects the zero value confirmation, accept() returns and dialog stays open
    with patch.object(QMessageBox, "question", return_value=QMessageBox.No) as mock_q:
        dialog.accept()
        mock_q.assert_called_once()
        assert "Valor de Mercado Nulo" in mock_q.call_args[0][1]

    # If user confirms, dialog accepts
    with patch.object(QMessageBox, "question", return_value=QMessageBox.Yes):
        with patch.object(QWidget, "close"):
            dialog.accept()
    dialog.deleteLater()


def test_inv_01_close_position_dialog_gain_calculation(qapp):
    holding_data = {
        "id": 5,
        "name": "iShares Core S&P 500",
        "value": 1500.0,
        "cost_basis": 1000.0,
        "fees": 15.0,
        "account": "Degiro",
        "linked_account": "Conta Principal",
    }

    dlg = ClosePositionDialog(holding=holding_data)
    # Default values initialized
    assert dlg.sale_value.value() == 1500.0
    assert dlg.sale_fees.value() == 0.0

    # Net gain = 1500 - 1000 - (15 + 0) = +485.0
    data = dlg.get_data()
    assert data["sale_value"] == 1500.0
    assert data["total_fees"] == 15.0
    assert data["net_gain"] == 485.0

    # Increase sale fees
    dlg.sale_fees.setValue(25.0)
    data2 = dlg.get_data()
    # Net gain = 1500 - 1000 - (15 + 25) = +460.0
    assert data2["total_fees"] == 40.0
    assert data2["net_gain"] == 460.0
    assert "460,00 €" in dlg.gain_summary.text()
    assert "Mais-valia" in dlg.gain_summary.text()

    # Test loss scenario
    dlg.sale_value.setValue(800.0)
    data3 = dlg.get_data()
    # Net gain = 800 - 1000 - 40 = -240.0
    assert data3["net_gain"] == -240.0
    assert "Menos-valia" in dlg.gain_summary.text()
    dlg.deleteLater()


def test_inv_01_close_selected_position_creates_tx_and_archives(qapp, temp_db, mock_app):
    page = PortfolioPage(mock_app)
    page.repo = Repo(temp_db)
    page.refresh = MagicMock()
    page.app.change_journal_service.delete_holding.side_effect = lambda hid, **kw: page.repo.delete_holding(hid)

    # Add a holding to DB
    hid = page.repo.add_holding(
        "Vanguard FTSE All-World",
        "ETF",
        "Interactive Brokers",
        2000.0,
        1600.0,
        "Aporte mensal",
        fees=20.0,
    )

    with patch.object(page, "selected_id", return_value=hid):
        mock_dialog_instance = MagicMock()
        mock_dialog_instance.exec.return_value = 1  # Accepted
        mock_dialog_instance.get_data.return_value = {
            "sale_value": 2200.0,
            "sale_date": "2026-09-04",
            "sale_fees": 10.0,
            "total_fees": 30.0,
            "net_gain": 570.0,
            "account": "Conta principal",
            "create_transaction": True,
            "archive_holding": True,
        }

        with patch("ui.dialogs.ClosePositionDialog", return_value=mock_dialog_instance):
            page.close_selected_position()

    # Check holding was deleted/archived
    assert page.repo.holding_by_id(hid) is None

    # Check sale transaction was recorded in destination account
    cur = temp_db.conn.cursor()
    cur.execute("SELECT * FROM transactions WHERE description LIKE '%Alienação%'")
    rows = cur.fetchall()
    assert len(rows) == 1
    sale_tx = rows[0]
    assert sale_tx["amount"] == 2190.0  # 2200 - 10 fees
    assert sale_tx["account"] == "Conta principal"
    assert "+570.00 €" in str(sale_tx["notes"] or "")

    page.deleteLater()
