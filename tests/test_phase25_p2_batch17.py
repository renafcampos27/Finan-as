"""
Unit tests for Phase 25 / Batch 17 P2 Defects
=============================================
Defects tested:
1. MATH-05: Proteção contra divisões por zero em simulações sintéticas de crise (crisis_backtester_and_architecture_engine.py)
2. PERS-07: OperationalError database is locked em VACUUM concorrente com fallback para incremental_vacuum (database/engine.py)
3. AUD-MATH-03: Bloqueio irreversível de subperíodos TWRR a zero após perda de 100% com reinicialização pós-recapitalização (services/finance_math.py)
4. AUD-MATH-05: Implementação do rácio de Calmar (CAGR / |MaxDrawdown|) na biblioteca quantitativa (services/finance_math.py)
"""

import math
import sqlite3
from unittest.mock import MagicMock, patch
import pytest

from database.engine import FinanceDB
from motores_matematicos_simulacao.crisis_backtester_and_architecture_engine import (
    CrisisScenario,
    generate_market_path,
    run_timing_backtest,
    _safe_mdd,
    _safe_net_profit_pct,
)
from services.finance_math import (
    calmar_ratio,
    twrr,
)
import services.finance_math as fm


# ============================================================================
# 1. MATH-05 (Zero division protections in crisis backtester)
# ============================================================================

def test_math_05_pathological_scenario_zero_division():
    """Cenários extremos com trough=0, recovery=0 e capital zero não disparam ZeroDivisionError."""
    extreme_scenario = CrisisScenario(
        name="Pathological Zero Trough Crash",
        duration_months=12,
        trough_month=0,
        peak_to_trough_drop=-0.40,
        recovery_months=0,
    )
    prices = generate_market_path(extreme_scenario)
    assert len(prices) > 0
    assert all(p > 0 for p in prices)

    # Execução do backtest com capital e poupança zero
    results = run_timing_backtest(
        extreme_scenario,
        initial_capital=0.0,
        monthly_savings=0.0,
    )
    assert len(results) == 4
    for r in results:
        assert math.isfinite(r.final_portfolio_value)
        assert r.net_profit_pct == 0.0
        assert r.max_drawdown_pct <= 0.0


def test_math_05_safe_helpers():
    """_safe_mdd e _safe_net_profit_pct lidam com listas vazias ou valores nulos."""
    assert _safe_mdd([]) == 0.0
    assert _safe_mdd([0.0, 0.0, 0.0]) == 0.0
    assert _safe_net_profit_pct(100.0, 0.0) == 0.0
    assert _safe_net_profit_pct(0.0, 0.0) == 0.0
    assert _safe_net_profit_pct(150.0, 100.0) == 50.0


def test_math_05_normal_crisis_backtest_runs_all_strategies():
    """Cenário histórico executa as 4 estratégias calculando métricas finitas."""
    scenario = CrisisScenario(
        name="Covid-19 Mini Crash",
        duration_months=12,
        trough_month=2,
        peak_to_trough_drop=-0.30,
        recovery_months=6,
    )
    results = run_timing_backtest(scenario, initial_capital=10000.0, monthly_savings=500.0)
    assert len(results) == 4
    for r in results:
        assert r.final_portfolio_value > 0.0
        assert math.isfinite(r.net_profit_pct)
        assert math.isfinite(r.max_drawdown_pct)
        assert r.shares_accumulated > 0.0


# ============================================================================
# 2. PERS-07 (WAL VACUUM lock fallback to incremental_vacuum)
# ============================================================================

def test_pers_07_vacuum_lock_falls_back_to_incremental_vacuum(tmp_path):
    """optimize_database faz fallback para incremental_vacuum se VACUUM falhar por lock."""
    db_path = tmp_path / "test_wal_vacuum.db"
    db = FinanceDB(str(db_path))

    real_execute = db.conn.execute
    incremental_called = []

    def mock_execute(sql, *args, **kwargs):
        if sql == "VACUUM":
            raise sqlite3.OperationalError("database is locked")
        if "incremental_vacuum" in str(sql):
            incremental_called.append(True)
        return real_execute(sql, *args, **kwargs)

    mock_conn = MagicMock(wraps=db.conn)
    mock_conn.in_transaction = db.conn.in_transaction
    mock_conn.execute.side_effect = mock_execute

    with patch.object(db, "conn", mock_conn):
        res = db.optimize_database()
        assert res["status"] == "ok"
        assert len(incremental_called) == 1
        assert "freed_bytes" in res
        assert "duration_ms" in res

    db.close()


def test_pers_07_optimize_database_read_only_raises():
    """optimize_database recusa execução em modo read-only."""
    db = MagicMock(spec=FinanceDB)
    db._read_only = True
    with pytest.raises(PermissionError, match="modo de leitura"):
        FinanceDB.optimize_database(db)


# ============================================================================
# 3. AUD-MATH-03 (TWRR unblocking after 100% loss / recapitalization)
# ============================================================================

def test_aud_math_03_twrr_normal_compounding():
    """TWRR calcula retornos geométricos corretamente em séries normais."""
    # +10% seguido de +20%: 1.10 * 1.20 = 1.32 (+32%)
    assert round(twrr([10.0, 20.0]), 2) == 32.0
    # +50% seguido de -50%: 1.50 * 0.50 = 0.75 (-25%)
    assert round(twrr([50.0, -50.0]), 2) == -25.0


def test_aud_math_03_twrr_recapitalization_after_total_loss():
    """TWRR reinicializa a base pós-liquidação apenas com flag explícito; retém -100% por padrão."""
    # Vinculação geométrica ininterrupta por padrão (sem injeção de capital metadata)
    assert twrr([-100.0, 50.0, 20.0]) == -100.0

    # BUG-AUD-C97: Conta que sofreu perda total (-100%) não pode reportar taxa cumulativa
    # positiva após aportes frescos subsequentes. É limitada a 0.0%.
    ret = twrr([-100.0, 50.0, 20.0], allow_recapitalization=True)
    assert round(ret, 2) == 0.0
    # Recapitalização com perdas subsequentes reflete a perda pós-recapitalização (-20%)
    ret_neg = twrr([-100.0, -20.0], allow_recapitalization=True)
    assert round(ret_neg, 2) == -20.0

    # Perda total isolada
    assert twrr([-100.0]) == -100.0
    assert twrr([-100.0], allow_recapitalization=True) == -100.0

    # Múltiplas perdas totais
    assert twrr([-100.0, -100.0]) == -100.0
    assert twrr([-100.0, -100.0], allow_recapitalization=True) == -100.0


def test_aud_math_03_twrr_empty_and_extreme():
    """TWRR lida com lista vazia e valores extremos sem crash."""
    assert twrr([]) == 0.0
    res_extreme = twrr([1e6, 1e6])
    assert math.isfinite(res_extreme)


# ============================================================================
# 4. AUD-MATH-05 (Calmar Ratio: CAGR / |MaxDrawdown|)
# ============================================================================

def test_aud_math_05_calmar_ratio_standard():
    """Calmar ratio divide o CAGR pelo módulo do Max Drawdown."""
    # CAGR 15%, Max Drawdown 20% -> Calmar = 0.75
    assert calmar_ratio(15.0, 20.0) == 0.75

    # Max Drawdown expresso como número negativo (-20%)
    assert calmar_ratio(15.0, -20.0) == 0.75

    # CAGR 25%, Max Drawdown 10% -> Calmar = 2.50
    assert calmar_ratio(25.0, 10.0) == 2.50


def test_aud_math_05_calmar_ratio_edge_cases():
    """Calmar ratio trata drawdown nulo, retornos negativos e valores não finitos com segurança."""
    # BUG-R6-MATH-01 supersedes the original neutral-zero convention with
    # finite signed bounds when drawdown is effectively zero.
    assert calmar_ratio(10.0, 0.0) == 99.99
    assert calmar_ratio(10.0, 1e-7) == 99.99
    assert calmar_ratio(-10.0, 0.0) == -99.99

    # CAGR zero
    assert calmar_ratio(0.0, 20.0) == 0.0

    # CAGR negativo: -10% / 25% = -0.40
    assert calmar_ratio(-10.0, 25.0) == -0.40

    # Entradas não finitas (NaN / Inf)
    assert calmar_ratio(float("nan"), 20.0) == 0.0
    assert calmar_ratio(15.0, float("inf")) == 0.0
    assert calmar_ratio(15.0, float("nan")) == 0.0
    assert calmar_ratio(15.0, float("-inf")) == 0.0
    assert calmar_ratio(float("inf"), 20.0) == 0.0


def test_aud_math_05_calmar_ratio_exported_in_all():
    """calmar_ratio está devidamente listado em __all__ do módulo finance_math."""
    assert "calmar_ratio" in fm.__all__
    assert callable(getattr(fm, "calmar_ratio"))
