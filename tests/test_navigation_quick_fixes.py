"""Regressões da Fase 0: pesquisa, rótulos compactos, grupos e separador FIRE."""

from __future__ import annotations

import os
import re

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from ui.navigation import (
    SIMULATIONS_WORKSPACE,
    all_navigation_items,
    contextual_navigation_label,
)
from ui.search_routing import ROUTE_TERMS, SYNONYMS, resolve_route

SEARCH_CASES = [
    ("dashboard", "dashboard"),
    ("início", "dashboard"),
    ("centro", "centro"),
    ("movimentos", "movimentos"),
    ("despesas", "movimentos"),
    ("orçamento", "orcamentos"),
    ("subscrições", "subscricoes"),
    ("pagamentos", "pagamentos"),
    ("importação", "importacao"),
    ("divisão", "divisao"),
    ("documentos", "documentos"),
    ("investimentos", "carteira"),
    ("fire", "carteira"),
    ("independência financeira", "carteira"),
    ("objetivos", "objetivos"),
    ("planeador", "planeador"),
    ("metas", "metas_mensais"),
    ("análises", "analises"),
    ("relatórios", "analises"),
    ("análise dinâmica", "dinamica"),
    ("revisão mensal", "revisao"),
    ("histórico", "historico"),
    ("pdf", "relatorios_pdf"),
    ("saúde", "saude"),
    ("score", "score"),
    ("risco", "risco"),
    ("simulações", "simulacoes"),
    ("hábitos", "simulacoes"),
    ("simulador de decisões", "simulador"),
    ("juros compostos", "juros"),
    ("assistente", "assistente"),
    ("ia", "ia"),
    ("gemini", "ia"),
    ("definições", "definicoes"),
    ("gestão dos dados", "gestao_dados"),
    ("backup", "gestao_dados"),
]


@pytest.mark.parametrize(("query", "expected"), SEARCH_CASES)
def test_global_search_reaches_the_intended_route(query: str, expected: str) -> None:
    assert resolve_route(query) == expected


@pytest.mark.parametrize("query", ["a", "e", "p", "or", "ge", "x", "?", "", "  "])
def test_short_or_empty_queries_do_not_navigate(query: str) -> None:
    assert resolve_route(query) is None


def test_two_letter_exact_match_still_opens_the_ai_page() -> None:
    assert resolve_route("ia") == "ia"


def test_synonyms_only_reference_existing_routes() -> None:
    known = {item.key for item in all_navigation_items()}
    assert set(SYNONYMS) <= known


def test_every_route_has_at_least_one_search_term() -> None:
    routes = {item.key for item in all_navigation_items()}
    assert set(ROUTE_TERMS) == routes


def test_investments_compact_label_fits_the_142_px_sidebar() -> None:
    assert contextual_navigation_label("carteira") == "💼 Carteira"


def test_simulations_tab_tells_the_truth() -> None:
    item = next(
        item for item in SIMULATIONS_WORKSPACE.items if item.key == "simulador"
    )
    plain = re.sub(r"[^0-9A-Za-zÀ-ÿ ]", "", item.label).strip()
    assert plain == "Simulador de decisões"


def test_compact_sidebar_labels_fit_and_groups_start_open(tmp_path, monkeypatch) -> None:
    import database.repo.base as repo_base
    import services.documents as document_service_module
    import utils.app_logging as app_logging

    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    monkeypatch.setattr(
        repo_base, "db_path", lambda: tmp_path / "financas_pessoais.sqlite3"
    )
    monkeypatch.setattr(
        document_service_module, "attachments_dir", lambda: tmp_path / "anexos"
    )
    monkeypatch.setattr(app_logging, "configure_logging", lambda *_a, **_k: None)

    from main import LiquidFinanceApp

    app = QApplication.instance() or QApplication([])
    window = LiquidFinanceApp()
    window.repo.set_setting("backup_on_close", "0")
    window.backup_service.run_backup_on_close = lambda silent=True: None
    window.check_subscription_reminders = lambda: None
    window.show_dashboard_notice = lambda *_args, **_kwargs: None
    window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    window.repo.set_setting("window_maximized", "0")
    window.setWindowState(Qt.WindowNoState)
    window.resize(960, 640)
    window.show()
    for _ in range(12):
        app.processEvents()
        QTest.qWait(10)
    window._apply_responsive_chrome()
    app.processEvents()

    try:
        # 5: primeiro arranque com todos os grupos abertos
        assert window._collapsed_nav_groups() == set()

        # 3: sidebar compacta continua nos 142 px e nenhum rótulo é cortado
        assert window._sidebar_contextual is True
        assert window.sidebar.width() == 142
        assert window.nav_buttons["carteira"].text() == "💼 Carteira"
        if os.environ.get("QT_QPA_PLATFORM") != "offscreen":
            overflows = []
            for key, button in window.nav_buttons.items():
                if not button.isVisibleTo(window) or not button.text().strip():
                    continue
                advance = button.fontMetrics().horizontalAdvance(button.text())
                if advance > max(1, button.width() - 18):
                    overflows.append((key, button.text(), advance, button.width()))
            assert overflows == []
        else:
            assert max(len(b.text()) for b in window.nav_buttons.values() if b.isVisibleTo(window)) <= 18

        # 4: o separador e a página dizem a mesma coisa
        assert window.pages["simulador"].page_title == "Simulador de decisões"
    finally:
        window.close()
