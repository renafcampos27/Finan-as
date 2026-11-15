#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Suite de Testes Automatizados – Fase 10 (Prioridade P2: Defeitos Médios/Impacto Funcional)
Valida a resolução dos 12 defeitos:
1. BUG-COMBOBOX-CRASH-001 (reports.py e planning.py)
2. BUG-INVESTMENTS-CRUD-001 (investments.py)
3. BUG-PLANNING-DELETE-001 (planning.py)
4. TAX-04 (services/tax.py)
5. TAX-05 (portfolio_stress_and_tax_optimizer.py)
6. TAX-06 (portfolio_stress_and_tax_optimizer.py)
7. TAX-07 (services/tax.py e portfolio_stress_and_tax_optimizer.py)
8. MATH-02 (services/finance_math.py)
9. MATH-03 (services/finance_math.py)
10. MATH-04 (services/finance_math.py)
11. MATH-05 (crisis_backtester_and_architecture_engine.py)
12. PERS-07 (database/engine.py)
"""

from __future__ import annotations

import sqlite3
from datetime import date
from unittest.mock import MagicMock, patch

import pytest

from database.engine import FinanceDB
from motores_matematicos_simulacao.crisis_backtester_and_architecture_engine import (
    CrisisScenario,
    generate_market_path,
    run_timing_backtest,
)
from motores_matematicos_simulacao.portfolio_stress_and_tax_optimizer import (
    TaxLot,
    process_fifo_sale,
)
from services.finance_math import (
    cornish_fisher_var,
    modified_dietz,
    periodic_discount_factor,
    present_value_of_contributions,
)
from services.tax import (
    PortugueseTaxEngine,
    TaxLotMatch,
)


# ============================================================================
# 1. BUG-COMBOBOX-CRASH-001 & BUG-PLANNING-DELETE-001 (UI Pages)
# ============================================================================

def test_bug_combobox_crash_001_score_details_ym_fallback():
    """ScoreDetailsPage.ym() não deve falhar com ValueError quando o ano estiver vazio."""
    from ui.pages.reports import ScoreDetailsPage

    page = MagicMock(spec=ScoreDetailsPage)
    page.year = MagicMock()
    page.month = MagicMock()

    page.year.currentText.return_value = ""
    page.month.currentIndex.return_value = 2  # Março (índice 2 -> 03)

    ym_val = ScoreDetailsPage.ym(page)
    expected_year = date.today().year
    assert ym_val == f"{expected_year}-03"

    page.year.currentText.return_value = "Todos"
    ym_val = ScoreDetailsPage.ym(page)
    assert ym_val == f"{expected_year}-03"

    page.year.currentText.return_value = "2025"
    ym_val = ScoreDetailsPage.ym(page)
    assert ym_val == "2025-03"


def test_bug_combobox_crash_001_planning_monthly_goals_ym_fallback():
    """MonthlyGoalsPage.ym() não deve falhar com ValueError quando o ano estiver vazio."""
    from ui.pages.planning import MonthlyGoalsPage

    page = MagicMock(spec=MonthlyGoalsPage)
    page.year = MagicMock()
    page.month = MagicMock()

    page.year.currentText.return_value = "   "
    page.month.currentIndex.return_value = 11  # Dezembro (índice 11 -> 12)

    ym_val = MonthlyGoalsPage.ym(page)
    expected_year = date.today().year
    assert ym_val == f"{expected_year}-12"


def test_bug_planning_delete_001_delete_goal_confirmation_and_safety():
    """MonthlyGoalsPage.delete_goal() deve pedir confirmação e tratar exceções com segurança."""
    from ui.pages.planning import MonthlyGoalsPage
    from PySide6.QtWidgets import QMessageBox

    page = MagicMock(spec=MonthlyGoalsPage)
    page.table = MagicMock()
    page.repo = MagicMock()
    page.app = MagicMock()

    # Cenário 1: Sem linha selecionada
    page.table.currentRow.return_value = -1
    MonthlyGoalsPage.delete_goal(page)
    page.repo.delete_monthly_goal.assert_not_called()

    # Cenário 2: Utilizador cancela no diálogo de confirmação
    page.table.currentRow.return_value = 0
    id_item = MagicMock()
    id_item.text.return_value = "42"
    name_item = MagicMock()
    name_item.text.return_value = "Fundo de Emergência"
    page.table.item.side_effect = lambda row, col: id_item if col == 0 else name_item

    with patch("ui.pages.planning.QMessageBox.question", return_value=QMessageBox.No) as mock_q:
        MonthlyGoalsPage.delete_goal(page)
        mock_q.assert_called_once()
        page.repo.delete_monthly_goal.assert_not_called()

    # Cenário 3: Utilizador confirma e eliminação ocorre com sucesso
    with patch("ui.pages.planning.QMessageBox.question", return_value=QMessageBox.Yes):
        MonthlyGoalsPage.delete_goal(page)
        page.repo.delete_monthly_goal.assert_called_once_with(42)
        page.app.notify_data_changed.assert_called_with("Meta mensal eliminada.")

    # Cenário 4: Falha na base de dados é capturada sem crash
    page.repo.delete_monthly_goal.side_effect = RuntimeError("DB Lock")
    with patch("ui.pages.planning.QMessageBox.question", return_value=QMessageBox.Yes), \
         patch("ui.pages.planning.QMessageBox.critical") as mock_crit:
        MonthlyGoalsPage.delete_goal(page)
        mock_crit.assert_called_once()


# ============================================================================
# 2. BUG-INVESTMENTS-CRUD-001 (PortfolioPage)
# ============================================================================

def test_bug_investments_crud_001_delete_liability_none_item_and_exception():
    """delete_liability() não deve lançar AttributeError quando item for None e deve capturar falhas."""
    from ui.pages.investments import PortfolioPage
    from PySide6.QtWidgets import QMessageBox

    page = MagicMock(spec=PortfolioPage)
    page.selected_liability_id.return_value = 10
    page.liability_table = MagicMock()
    page.liability_table.currentRow.return_value = 0
    page.liability_table.item.return_value = None
    page.repo = MagicMock()
    page.app = MagicMock()

    with patch("ui.pages.investments.QMessageBox.question", return_value=QMessageBox.Yes) as mock_q:
        PortfolioPage.delete_liability(page)
        assert "esta dívida" in mock_q.call_args[0][2]
        page.repo.delete_liability.assert_called_once_with(10)
        page.app.notify_data_changed.assert_called_with("Dívida eliminada.")

    page.repo.delete_liability.side_effect = sqlite3.OperationalError("database is locked")
    with patch("ui.pages.investments.QMessageBox.question", return_value=QMessageBox.Yes), \
         patch("ui.pages.investments.QMessageBox.critical") as mock_crit:
        PortfolioPage.delete_liability(page)
        mock_crit.assert_called_once()


# ============================================================================
# 3. TAX-04 (Englobamento Obrigatório vs Taxa de 28% na Inclusão a 50%)
# ============================================================================

def test_tax_04_50pct_inclusion_blocks_flat_28pct():
    """CIRS Art. 43.º n.º 2: A inclusão a 50% obriga a englobamento e anula a taxa autónoma de 28%."""
    engine = PortugueseTaxEngine()
    match = TaxLotMatch(
        disposal_id="disp-1",
        acquisition_lot_id="acq-1",
        asset_symbol="PME_UNLISTED",
        acquisition_date=date(2024, 1, 15),
        disposal_date=date(2025, 6, 20),
        matched_quantity=100.0,
        acquisition_price=10.0,
        disposal_price=20.0,
        acquisition_fees_allocated=0.0,
        disposal_fees_allocated=0.0,
        devaluation_coefficient=1.0,
        adjusted_acquisition_cost=1000.0,
        gross_disposal_proceeds=2000.0,
        net_gain_loss=1000.0,
        holding_days=522,
        is_crypto=False,
        is_exempt_crypto=False,
        tax_rate_pct=28.0,
        estimated_tax=280.0,
        tax_schedule="Anexo G",
    )

    report = engine.generate_annual_tax_report(
        [match],
        fiscal_year=2025,
        apply_50pct_inclusion=True,
    )

    assert report["final_taxable_base"] == 500.0
    assert report["global_estimated_tax_28pct"] == 0.0
    assert report["anexo_g"]["estimated_tax_autonomous_28pct"] == 0.0
    assert any("Art. 43.º n.º 2" in note for note in report["legal_notes"])


# ============================================================================
# 4. TAX-05 & TAX-06 & TAX-07 (FIFO Lotes Futuros, Comissões e Isenções)
# ============================================================================

def test_tax_05_fifo_sale_filters_future_lots():
    """Venda no passado não pode consumir lotes adquiridos no futuro."""
    lot_past = TaxLot(
        id=1,
        asset_symbol="IWDA",
        buy_date=date(2025, 1, 10),
        quantity=10.0,
        buy_price=80.0,
        buy_fees=2.0,
        country_code="IE",
    )
    lot_future = TaxLot(
        id=2,
        asset_symbol="IWDA",
        buy_date=date(2025, 6, 15),
        quantity=20.0,
        buy_price=85.0,
        buy_fees=2.0,
        country_code="IE",
    )

    updated_lots, realized = process_fifo_sale(
        open_lots=[lot_future, lot_past],
        sell_date=date(2025, 3, 1),
        sell_quantity=5.0,
        sell_price=90.0,
        sell_fees=3.0,
        is_crypto=False,
    )

    assert len(realized) == 1
    assert realized[0].buy_date == date(2025, 1, 10)
    assert realized[0].holding_days >= 0
    future_retained = [lot for lot in updated_lots if lot.id == 2]
    assert len(future_retained) == 1
    assert future_retained[0].quantity == 20.0

    with pytest.raises(ValueError, match="Quantidade insuficiente adquirida"):
        process_fifo_sale(
            open_lots=[lot_future, lot_past],
            sell_date=date(2025, 3, 1),
            sell_quantity=15.0,
            sell_price=90.0,
            sell_fees=3.0,
            is_crypto=False,
        )


def test_tax_06_model_3_expenses_aggregation():
    """Anexo G/J: acquisition_cost é o custo nominal puro e expenses agrega comissões de compra e venda."""
    lot = TaxLot(
        id=1,
        asset_symbol="VWCE",
        buy_date=date(2024, 1, 1),
        quantity=10.0,
        buy_price=100.0,
        buy_fees=10.0,
        country_code="IE",
    )

    updated_lots, realized = process_fifo_sale(
        open_lots=[lot],
        sell_date=date(2025, 1, 1),
        sell_quantity=10.0,
        sell_price=120.0,
        sell_fees=5.0,
        is_crypto=False,
    )

    event = realized[0]
    assert event.acquisition_cost == 1000.0
    assert event.expenses == 15.0
    assert event.taxable_profit == 185.0


def test_tax_07_crypto_exemption_blocked_for_security_tokens_and_offshore():
    """Isenção cripto (>365 dias) não se aplica a security tokens nem a jurisdições offshore."""
    # 1. Crypto regular em Portugal -> Isento
    lot_normal = TaxLot(
        id=1, asset_symbol="BTC", buy_date=date(2024, 1, 1),
        quantity=1.0, buy_price=30000.0, buy_fees=10.0, country_code="PT"
    )
    _, rel_normal = process_fifo_sale([lot_normal], date(2025, 3, 1), 1.0, 60000.0, 10.0, is_crypto=True)
    assert rel_normal[0].is_exempt is True
    assert rel_normal[0].estimated_tax == 0.0

    # 2. Security Token -> Não Isento (28%)
    lot_sec = TaxLot(
        id=2, asset_symbol="SEC_TOKEN", buy_date=date(2024, 1, 1),
        quantity=1.0, buy_price=1000.0, buy_fees=10.0, country_code="PT",
        is_security_token=True,
    )
    _, rel_sec = process_fifo_sale([lot_sec], date(2025, 3, 1), 1.0, 2000.0, 10.0, is_crypto=True)
    assert rel_sec[0].is_exempt is False
    assert rel_sec[0].tax_rate_pct == 28.0

    # 3. Offshore (KY) -> Não Isento (35%)
    lot_offshore = TaxLot(
        id=3, asset_symbol="BTC", buy_date=date(2024, 1, 1),
        quantity=1.0, buy_price=30000.0, buy_fees=10.0, country_code="KY"
    )
    _, rel_offshore = process_fifo_sale([lot_offshore], date(2025, 3, 1), 1.0, 60000.0, 10.0, is_crypto=True)
    assert rel_offshore[0].is_exempt is False
    assert rel_offshore[0].tax_rate_pct == 35.0

    # 4. PortugueseTaxEngine
    tax_engine = PortugueseTaxEngine()
    tax_engine.add_acquisition(
        lot_id="4",
        asset_symbol="SEC_TOKEN",
        asset_name="Security Token",
        acquisition_date=date(2024, 1, 1),
        quantity=1.0,
        unit_price=1000.0,
        fees=0.0,
        country_code="PT",
        is_crypto=True,
        is_security_token=True,
    )
    matches = tax_engine.process_disposal(
        disposal_id="disp-4",
        asset_symbol="SEC_TOKEN",
        disposal_date=date(2025, 3, 1),
        quantity=1.0,
        unit_price=2000.0,
        fees=0.0,
        is_crypto=True,
    )
    assert matches[0].is_exempt_crypto is False
    assert matches[0].tax_rate_pct == 28.0


# ============================================================================
# 5. MATH-02, MATH-03, MATH-04, MATH-05 (Matemática e Backtesting)
# ============================================================================

def test_math_02_overflow_protection_in_discount_factor_and_contributions():
    """periodic_discount_factor e present_value_of_contributions não lançam OverflowError."""
    factor = periodic_discount_factor(annual_rate_pct=10000.0, period_index=50000)
    assert factor >= 1.0
    assert not factor == float("inf") or factor <= 1e13

    pv = present_value_of_contributions(100.0, periods=50000, annual_inflation_pct=500.0)
    assert pv >= 0.0


def test_math_03_cornish_fisher_var_with_constant_negative_returns():
    """Retornos constantes negativos têm variância 0 mas geram VaR positivo correspondente à perda certa."""
    returns = [-0.015, -0.015, -0.015, -0.015, -0.015]
    var_val = cornish_fisher_var(returns, confidence=0.95)
    assert var_val == 0.015


def test_math_04_modified_dietz_negative_denominator():
    """Quando os levantamentos superam o capital inicial (denominador <= 0), Dietz retorna 0.0."""
    flows = [("2025-01-02", -1500.0)]
    ret = modified_dietz(
        start_value=1000.0,
        end_value=200.0,
        cash_flows=flows,
        start_date="2025-01-01",
        end_date="2025-01-31",
    )
    assert ret == 0.0


def test_math_05_crisis_backtester_zero_division_guard():
    """Cenários de crise com trough_month=0, recovery_months=0 ou capital 0 não causam ZeroDivisionError."""
    extreme_scenario = CrisisScenario(
        name="Zero Trough Crash",
        duration_months=12,
        trough_month=0,
        peak_to_trough_drop=-0.30,
        recovery_months=0,
    )
    prices = generate_market_path(extreme_scenario)
    assert len(prices) > 0

    results = run_timing_backtest(
        extreme_scenario,
        initial_capital=0.0,
        monthly_savings=0.0,
    )
    assert len(results) == 4
    for r in results:
        assert r.max_drawdown_pct <= 0.0
        assert r.net_profit_pct == 0.0


# ============================================================================
# 6. PERS-07 (optimize_database WAL lock fallback)
# ============================================================================

def test_pers_07_optimize_database_wal_vacuum_lock_fallback(tmp_path):
    """optimize_database() faz fallback para incremental_vacuum se VACUUM falhar por lock em WAL."""
    db_path = tmp_path / "test_vacuum_lock.db"
    db = FinanceDB(str(db_path))

    real_execute = db.conn.execute
    incremental_called = []

    def mock_execute(sql, *args, **kwargs):
        if sql == "VACUUM":
            raise sqlite3.OperationalError("database is locked")
        if "incremental_vacuum" in sql:
            incremental_called.append(True)
        return real_execute(sql, *args, **kwargs)

    mock_conn = MagicMock(wraps=db.conn)
    mock_conn.in_transaction = db.conn.in_transaction
    mock_conn.execute.side_effect = mock_execute

    with patch.object(db, "conn", mock_conn):
        res = db.optimize_database()
        assert res["status"] == "ok"
        assert len(incremental_called) == 1

    db.close()
