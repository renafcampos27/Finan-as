"""
Crisis Simulation, Investment Timing Backtester & Architecture Blueprint
========================================================================
Módulo executável para simulação quantitativa de estratégias de timing em crises
e implementação dos padrões arquiteturais (UDF Store + CQRS Dual Engine).
"""

import logging
import math
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger(__name__)



# ============================================================================
# PARTE I: MOTOR DE SIMULAÇÃO DE CRISES & TIMING DE INVESTIMENTO
# ============================================================================

@dataclass(slots=True)
class CrisisScenario:
    name: str
    duration_months: int = 12
    trough_month: int = 1
    peak_to_trough_drop: float = -0.30  # Ex: -0.491 para -49.1%
    recovery_months: int = 6
    custom_prices: list[float] = field(default_factory=list)


HISTORICAL_CRISES = [
    CrisisScenario("Dot-com Crash (2000-2002)", duration_months=60, trough_month=30, peak_to_trough_drop=-0.491, recovery_months=55),
    CrisisScenario("Great Financial Crisis (2007-2009)", duration_months=48, trough_month=17, peak_to_trough_drop=-0.568, recovery_months=42),
    CrisisScenario("Covid-19 Flash Crash (2020)", duration_months=12, trough_month=1, peak_to_trough_drop=-0.339, recovery_months=5),
    CrisisScenario("Inflation Shock (2022)", duration_months=24, trough_month=10, peak_to_trough_drop=-0.254, recovery_months=18),
]


def generate_market_path(scenario: CrisisScenario) -> list[float]:
    """Gera uma trajetória sintética de preços indexados (Base 100.0) representativa do crash."""
    if scenario.custom_prices:
        return [max(0.01, round(float(p), 2)) for p in scenario.custom_prices]
    prices = [100.0]
    trough = max(1, scenario.trough_month)
    clamped_drop = min(0.0, max(-0.9999, scenario.peak_to_trough_drop))

    # Fase 1: Queda do topo ao fundo
    for m in range(1, trough + 1):
        progress = m / trough
        price = max(
            0.01,
            100.0 * (1.0 + clamped_drop * (1.0 - math.cos(progress * math.pi / 2))),
        )
        prices.append(round(price, 2))

    # Fase 2: Recuperação do fundo ao topo
    trough_price = max(0.01, 100.0 * (1.0 + clamped_drop))
    if scenario.recovery_months > scenario.trough_month:
        rec_steps = scenario.recovery_months - scenario.trough_month
    else:
        rec_steps = max(1, scenario.recovery_months)

    for m in range(1, rec_steps + 1):
        progress = m / rec_steps
        price = max(0.01, trough_price + (100.0 - trough_price) * (progress**1.2))
        prices.append(round(price, 2))

    # Fase 3: Pós-recuperação até à duração total do cenário (crescimento anualizado padrão de 7%)
    total_months = max(scenario.duration_months, trough + rec_steps)
    current_months = trough + rec_steps
    for m in range(current_months + 1, total_months + 1):
        extra_steps = m - current_months
        price = max(0.01, 100.0 * ((1.0 + 0.07 / 12.0) ** extra_steps))
        prices.append(round(price, 2))

    return prices


@dataclass(slots=True)
class BacktestResult:
    strategy_name: str
    scenario_name: str
    final_portfolio_value: float
    total_invested: float
    net_profit_pct: float
    max_drawdown_pct: float
    shares_accumulated: float


def _safe_mdd(values: list[float]) -> float:
    """Calcula o drawdown máximo com complexidade O(N) e proteção contra divisão por zero."""
    if not values or len(values) < 2:
        return 0.0
    peak = 0.0
    min_dd = 0.0
    for v in values:
        if v > peak:
            peak = v
        elif peak > 1e-12:
            safe_v = max(0.0, v)
            dd = (safe_v - peak) / peak
            if dd < min_dd:
                min_dd = dd
    return min_dd


def _safe_net_profit_pct(final_val: float, total_invested: float) -> float:
    if total_invested <= 1e-12:
        return 0.0
    return ((final_val - total_invested) / total_invested) * 100.0


def run_timing_backtest(
    scenario: CrisisScenario,
    initial_capital: float = 100000.0,
    monthly_savings: float = 1000.0,
) -> list[BacktestResult]:
    """Executa a simulação comparativa das 4 estratégias de timing."""
    prices = generate_market_path(scenario)
    results = []

    # 1. PURE LUMP SUM (100% Investido no Mês 0 + Aportes Mensais Fixos)
    shares = initial_capital / prices[0] if (prices and prices[0] > 1e-12) else 0.0
    total_invested = initial_capital
    portfolio_values = [shares * prices[0]] if prices else [0.0]

    for p in prices[1:]:
        shares += monthly_savings / p if p > 1e-12 else 0.0
        total_invested += monthly_savings
        portfolio_values.append(shares * p)

    final_val = portfolio_values[-1]
    mdd = _safe_mdd(portfolio_values)
    results.append(
        BacktestResult(
            "1. Pure Lump Sum",
            scenario.name,
            final_val,
            total_invested,
            _safe_net_profit_pct(final_val, total_invested),
            mdd * 100,
            shares,
        )
    )

    # 2. STATIC DCA (Capital inicial dividido por 24 meses + Aporte mensal fixo)
    dca_tranche = initial_capital / 24.0
    cash = initial_capital
    shares = 0.0
    total_invested = initial_capital

    # Mês 0: primeira tranche do capital inicial
    m0_alloc = min(cash, dca_tranche)
    shares += m0_alloc / prices[0] if (prices and prices[0] > 1e-12) else 0.0
    cash -= m0_alloc
    dca_portfolio_values = [shares * prices[0] + cash] if prices else [0.0]

    for p in prices[1:]:
        # Aporte mensal regular
        shares += monthly_savings / p if p > 1e-12 else 0.0
        total_invested += monthly_savings
        # Alocação de capital inicial em 24 meses
        if cash > 0:
            invest_amount = min(cash, dca_tranche)
            shares += invest_amount / p if p > 1e-12 else 0.0
            cash -= invest_amount
        dca_portfolio_values.append(shares * p + cash)

    final_val = dca_portfolio_values[-1]
    mdd = _safe_mdd(dca_portfolio_values)
    results.append(
        BacktestResult(
            "2. Static DCA (24m)",
            scenario.name,
            final_val,
            total_invested,
            _safe_net_profit_pct(final_val, total_invested),
            mdd * 100,
            shares,
        )
    )

    # 3. ENHANCED DCA (Aporte mensal multiplicado por M = 1 + 2*|DD|)
    shares = initial_capital / prices[0] if (prices and prices[0] > 1e-12) else 0.0
    total_invested = initial_capital
    edca_values = [shares * prices[0]] if prices else [0.0]
    peak_price = prices[0] if prices else 100.0

    for p in prices[1:]:
        peak_price = max(peak_price, p)
        current_dd = (p - peak_price) / peak_price if peak_price > 1e-12 else 0.0
        m = 1.0 + 2.0 * abs(min(0.0, current_dd))
        monthly_amount = monthly_savings * m
        shares += monthly_amount / p if p > 1e-12 else 0.0
        total_invested += monthly_amount
        edca_values.append(shares * p)

    final_val = edca_values[-1]
    mdd = _safe_mdd(edca_values)
    results.append(
        BacktestResult(
            "3. Enhanced DCA",
            scenario.name,
            final_val,
            total_invested,
            _safe_net_profit_pct(final_val, total_invested),
            mdd * 100,
            shares,
        )
    )

    # 4. CRISIS ALPHA LADDER (75% Inicial + 25% Dry Powder escalonado a -10%, -20%, -30%, -40%, -50%)
    dry_powder = initial_capital * 0.25
    shares = (
        (initial_capital * 0.75) / prices[0]
        if (prices and prices[0] > 1e-12)
        else 0.0
    )
    total_invested = initial_capital
    ladder_values = [shares * prices[0] + dry_powder] if prices else [0.0]

    tranches = [
        (-0.10, 0.15),
        (-0.20, 0.25),
        (-0.30, 0.30),
        (-0.40, 0.20),
        (-0.50, 0.10),
    ]
    triggered_tranches = set()
    peak_price = prices[0] if prices else 100.0

    for p in prices[1:]:
        peak_price = max(peak_price, p)
        current_dd = (p - peak_price) / peak_price if peak_price > 1e-12 else 0.0
        # Verificar gatilhos de tranches de crise
        for threshold, pct_alloc in tranches:
            if current_dd <= threshold and threshold not in triggered_tranches:
                alloc_val = (initial_capital * 0.25) * pct_alloc
                if dry_powder >= alloc_val:
                    shares += alloc_val / p if p > 1e-12 else 0.0
                    dry_powder -= alloc_val
                triggered_tranches.add(threshold)

        # Aporte mensal com multiplicador
        m = 1.0 + 2.0 * abs(min(0.0, current_dd))
        shares += (monthly_savings * m) / p if p > 1e-12 else 0.0
        total_invested += monthly_savings * m
        ladder_values.append(shares * p + dry_powder)

    final_val = ladder_values[-1]
    mdd = _safe_mdd(ladder_values)
    results.append(
        BacktestResult(
            "4. Crisis Alpha Ladder",
            scenario.name,
            final_val,
            total_invested,
            _safe_net_profit_pct(final_val, total_invested),
            mdd * 100,
            shares,
        )
    )

    return results


run_crisis_backtest = run_timing_backtest


# ============================================================================
# PARTE II: BLUEPRINT DE ARQUITETURA — UNIDIRECTIONAL DATA FLOW (UDF STORE)
# ============================================================================

@dataclass(slots=True)
class Action:
    type: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AppState:
    portfolio_cash: float = 25000.0
    portfolio_equity: float = 75000.0
    market_drawdown: float = 0.0
    active_scenario: str = "Live Market"
    tax_lots_count: int = 12



def app_reducer(state: AppState, action: Action) -> AppState:
    """Função pura de transição de estado unidirecional (Redux-style reducer)."""
    if action.type in ("UPDATE_MARKET_TICK", "MARKET_DRAWDOWN_UPDATED"):
        new_equity = action.payload.get("new_equity")
        drawdown = action.payload.get("drawdown")
        return AppState(
            portfolio_cash=state.portfolio_cash,
            portfolio_equity=(
                float(new_equity) if new_equity is not None else state.portfolio_equity
            ),
            market_drawdown=(
                float(drawdown) if drawdown is not None else state.market_drawdown
            ),
            active_scenario=state.active_scenario,
            tax_lots_count=state.tax_lots_count,
        )
    elif action.type == "DEPLOY_DRY_POWDER_TRANCHE":
        try:
            raw_amount = action.payload.get("amount", 0.0)
            requested = max(0.0, float(0.0 if raw_amount is None else raw_amount))
        except (ValueError, TypeError):
            requested = 0.0
        actual_deploy = min(max(0.0, state.portfolio_cash), requested)
        new_cash = state.portfolio_cash - actual_deploy
        new_equity = state.portfolio_equity + actual_deploy
        return AppState(
            portfolio_cash=new_cash,
            portfolio_equity=new_equity,
            market_drawdown=state.market_drawdown,
            active_scenario=state.active_scenario,
            tax_lots_count=state.tax_lots_count + (1 if actual_deploy > 0 else 0),
        )
    return state


reduce_app_state = app_reducer



class AppStateStore:
    """Store centralizado reativo com notificações de listeners."""

    def __init__(self, initial_state: AppState):
        self._lock = threading.RLock()
        self._state = initial_state
        self._subscribers: list[Callable[[AppState], None]] = []

    @property
    def state(self) -> AppState:
        with self._lock:
            return self._state

    def subscribe(self, listener: Callable[[AppState], None]) -> None:
        with self._lock:
            if listener not in self._subscribers:
                self._subscribers.append(listener)

    def unsubscribe(self, callback: Callable[[AppState], None]) -> None:
        with self._lock:
            self._subscribers = [s for s in self._subscribers if s != callback]

    def dispatch(self, action: Action) -> None:
        with self._lock:
            self._state = app_reducer(self._state, action)
            current_state = self._state
            subscribers_snapshot = list(self._subscribers)
            for sub in subscribers_snapshot:
                try:
                    sub(current_state)
                except Exception as exc:
                    logger.warning("AppStateStore subscriber failed: %s", exc)


# ============================================================================
# EXECUÇÃO & RELATÓRIO DO BACKTEST
# ============================================================================

if __name__ == "__main__":
    print("=" * 80)
    print("SIMULAÇÃO DE CRISES HISTÓRICAS & TIMING DE INVESTIMENTO (S&P 500)")
    print("Capital Inicial: 100.000 € | Aporte Mensal Base: 1.000 €")
    print("=" * 80)
    
    for crisis in HISTORICAL_CRISES:
        print(f"\n[*] CENÁRIO: {crisis.name.upper()} (Queda Pico-Fundo: {crisis.peak_to_trough_drop*100:.1f}%)")

        print("-" * 80)
        print(f"{'Estratégia':<25} | {'Valor Final':<15} | {'Lucro Líquido %':<16} | {'Max Drawdown':<12}")
        print("-" * 80)
        
        sim_results = run_timing_backtest(crisis)
        for r in sim_results:
            print(f"{r.strategy_name:<25} | {r.final_portfolio_value:>12,.2f} € | {r.net_profit_pct:>14.2f} % | {r.max_drawdown_pct:>10.2f} %")

    print("\n" + "=" * 80)
    print("TESTE DO MOTOR DE ARQUITETURA UNIDIRECIONAL (UDF STATE STORE)")
    print("=" * 80)
    
    store = AppStateStore(AppState())
    def on_state_change(st: AppState):
        print(f"[STORE NOTIFICATION] Cash: {st.portfolio_cash:,.2f} € | Equity: {st.portfolio_equity:,.2f} € | DD: {st.market_drawdown*100:.1f}% | Lotes: {st.tax_lots_count}")
        
    store.subscribe(on_state_change)
    
    print("\n1. Despachando evento: Mercado caiu -20%")
    store.dispatch(Action("MARKET_DRAWDOWN_UPDATED", {"drawdown": -0.20}))
    
    print("2. Despachando evento: Ativação de Tranche de Crise (6.250 € implantados)")
    store.dispatch(Action("DEPLOY_DRY_POWDER_TRANCHE", {"amount": 6250.0}))
    print("=" * 80)
