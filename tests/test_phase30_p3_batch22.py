# -*- coding: utf-8 -*-
"""
Test Suite for Batch 22 (Defects: BUG-MOV-06, BUG-DASH-03, BUG-SUB-02)
Phase 30 Validation - Final Inventory Batch.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from PySide6.QtCore import QPointF
from PySide6.QtWidgets import QApplication, QMessageBox

from database.engine import FinanceDB
from database.repo import Repo
from services.analytics import AnalyticsService
from services.payments import PaymentsService
from ui.core.responsive import ResponsivePolicy
from ui.core.theme import Theme
from ui.pages.movements import MovementsPage
from ui.pages.subscriptions import SubscriptionsPage


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture
def temp_db(tmp_path: Path):
    db_path = tmp_path / "test_batch22.db"
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
    app.page_density = "comfortable"
    app.repo = repo
    app.analytics_service = MagicMock(spec=AnalyticsService)
    app.payments_service = PaymentsService(repo)
    app.import_service = MagicMock()
    app.change_journal_service = MagicMock()
    app.document_service = MagicMock()
    app.content_scroll.viewport().width.return_value = 1200
    app.notify_data_changed = MagicMock()
    return app


# ============================================================================
# BUG-MOV-06: Menu de Contexto Habilitado em Tabela de Movimentos Vazia
# ============================================================================


def test_mov_06_context_menu_suppressed_on_empty_table(qapp, mock_app):
    """Verifica que open_table_menu não abre menu quando a tabela não tem linhas."""
    page = MovementsPage(mock_app)

    # Forçar modelo vazio -> rowCount() == 0
    page._replace_model_rows([])
    assert page.movements_model.rowCount() == 0

    with patch("ui.pages.movements.QMenu") as mock_menu_cls:
        page.open_table_menu(QPointF(10, 10))
        mock_menu_cls.assert_not_called()

    # Adicionar 1 movimento na tabela
    row = {
        "id": 1,
        "date": "2026-03-01",
        "description": "Supermercado Continente",
        "amount": -50.0,
        "category": "Alimentação",
        "account": "Conta principal",
        "notes": "Compras semanais",
    }
    page._replace_model_rows([row])
    assert page.movements_model.rowCount() == 1

    # Clicar numa coordenada fora das células (ex: 9999, 9999) sem seleção prévia
    page.table.clearSelection()
    with patch.object(page, "selected_id", return_value=None):
        with patch("ui.pages.movements.QMenu") as mock_menu_cls:
            page.open_table_menu(QPointF(9999, 9999))
            mock_menu_cls.assert_not_called()

    # Clicar com linha selecionada abre o menu
    with patch.object(page, "selected_id", return_value=1):
        with patch("ui.pages.movements.QMenu") as mock_menu_cls:
            mock_menu = MagicMock()
            mock_menu_cls.return_value = mock_menu
            page.open_table_menu(QPointF(10, 10))
            mock_menu_cls.assert_called_once()
            mock_menu.exec.assert_called_once()

    page.deleteLater()


# ============================================================================
# BUG-DASH-03: Breakpoint de 3 Colunas de Métricas a 780px
# ============================================================================


def test_dash_03_metrics_columns_responsive_breakpoint():
    """Valida o novo breakpoint de 3 colunas de métricas a 780px."""
    assert ResponsivePolicy.METRICS_THREE_COLUMNS == 780
    assert ResponsivePolicy.METRICS_TWO_COLUMNS == 430

    # Testar limites exatos
    assert ResponsivePolicy.metric_columns(429) == 1
    assert ResponsivePolicy.metric_columns(430) == 2
    assert ResponsivePolicy.metric_columns(779) == 2
    assert ResponsivePolicy.metric_columns(780) == 3
    assert ResponsivePolicy.metric_columns(1200) == 3


# ============================================================================
# BUG-SUB-02: Barra de Pesquisa e Deteção de Gastos Recorrentes em Subscrições
# ============================================================================


def test_sub_02_search_filter_and_detect_recurring(qapp, mock_app):
    """Valida filtragem por texto e deteção de despesas recorrentes em SubscriptionsPage."""
    # Limpar subscrições de seed da base de dados para controlo exato
    mock_app.repo.q("DELETE FROM subscriptions")
    page = SubscriptionsPage(mock_app)

    # Verificar presença dos componentes
    assert hasattr(page, "search_input")
    assert hasattr(page, "detect_btn")
    assert page.search_input.placeholderText() == "🔍 Pesquisar subscrição..."

    # Inserir duas subscrições de teste
    mock_app.repo.add_subscription(
        "Spotify Family", 14.99, "Streaming", 5, "Mensal", "Música em família", 1
    )
    mock_app.repo.add_subscription(
        "Ginásio Fitness", 35.00, "Desporto", 1, "Mensal", "Mensalidade do ginásio", 1
    )

    page.refresh()
    assert page.table.rowCount() == 2

    # Filtrar por "spotify"
    page.search_input.setText("spotify")
    assert page.table.rowCount() == 1
    assert page.table.item(0, 1).text() == "Spotify Family"

    # Filtrar por categoria "Desporto"
    page.search_input.setText("desporto")
    assert page.table.rowCount() == 1
    assert page.table.item(0, 1).text() == "Ginásio Fitness"

    # Filtrar por termo inexistente
    page.search_input.setText("inexistente")
    assert page.table.rowCount() == 0

    # Limpar pesquisa
    page.search_input.setText("")
    assert page.table.rowCount() == 2

    # Testar detect_recurring sem suspeitas
    mock_app.analytics_service.detect_recurring_subscriptions.return_value = []
    with patch.object(QMessageBox, "information") as mock_info:
        page.detect_recurring()
        mock_info.assert_called_once()
        assert "Não foram detetadas" in mock_info.call_args[0][2]

    # Testar detect_recurring com suspeitas e utilizador a aceitar
    mock_app.analytics_service.detect_recurring_subscriptions.return_value = [
        {"name": "Netflix 4K", "amount": 17.99, "occurrences": 3}
    ]
    with patch.object(QMessageBox, "question", return_value=QMessageBox.Yes) as mock_quest:
        with patch.object(page, "add_subscription") as mock_add_sub:
            page.detect_recurring()
            mock_quest.assert_called_once()
            assert "Netflix 4K" in mock_quest.call_args[0][2]
            assert "17,99" in mock_quest.call_args[0][2]
            mock_add_sub.assert_called_once()

    page.deleteLater()
