"""
Portfolio Stress Testing, Crisis Alpha Drawdown Laddering & Portuguese IRS Tax Engine
======================================================================================
Módulo de cálculo quantitativo para:
1. Avaliação Macroeconómica (CAPE, Buffett Indicator, Yield Curve, Fear & Greed)
2. Métricas de Risco de Cauda (VaR e CVaR com Expansão de Cornish-Fisher)
3. Escalonamento de Reserva de Liquidez (Drawdown Ladder de -10% a -50% & EDCA)
4. Motor Fiscal Português (Regras FIFO, Tax-Loss Harvesting, Anexo G e J do IRS)
"""

from dataclasses import dataclass
from datetime import date

from services.finance_math import cornish_fisher_var, expected_shortfall_cvar
from services.tax import TAXA_AGRAVADA_OFFSHORE

__all__ = [
    "MacroValuationSnapshot",
    "MacroCycleEngine",
    "DrawdownTrancheOrder",
    "CrisisAlphaDeployer",
    "CrisisDeploymentOrder",
    "CrisisDrawdownManager",
    "TaxLot",
    "RealizedTaxGain",
    "process_fifo_sale",
    "cornish_fisher_var",
    "expected_shortfall_cvar",
    "TAXA_AGRAVADA_OFFSHORE",
]



# ============================================================================
# 1. MACROECONOMIC VALUATION ENGINE
# ============================================================================

@dataclass(slots=True)
class MacroValuationSnapshot:
    shiller_cape: float
    cape_historical_percentile: float
    buffett_indicator_pct: float
    yield_curve_10y_2y_spread: float
    fear_and_greed_index: float
    market_phase: str
    recommended_dry_powder_pct: float


class MacroCycleEngine:
    @staticmethod
    def evaluate_macro_regime(
        cape: float = 41.5,
        buffett_pct: float = 240.0,
        spread_10y_2y: float = 0.49,
        fear_greed: float = 70.0,
    ) -> MacroValuationSnapshot:
        """Sintetiza métricas macroeconómicas para classificar o regime de valorização."""
        if spread_10y_2y < 0.0:
            if cape > 34.0 or buffett_pct > 180.0:
                phase = "Extreme Overvaluation & Yield Curve Inverted: Alerta Máximo de Recessão"
                cash_target = 0.30
            elif cape < 18.0 and fear_greed < 25.0:
                phase = "Deep Crisis: Implantação Agressiva de Capital"
                cash_target = 0.05
            else:
                phase = "Recession Watch (Curva Invertida): Preservação Preventiva de Liquidez"
                cash_target = 0.20
        elif cape > 34.0 or buffett_pct > 180.0:
            phase = "Extreme Overvaluation: Construção/Preservação de Dry Powder"
            cash_target = 0.25
        elif cape < 18.0 and fear_greed < 25.0:
            phase = "Deep Crisis: Implantação Agressiva de Capital"
            cash_target = 0.05
        else:
            phase = "Fair Value: Investimento Sistemático Regular"
            cash_target = 0.10

        return MacroValuationSnapshot(
            shiller_cape=cape,
            cape_historical_percentile=max(0.0, min(100.0, (cape / 42.0) * 100)),
            buffett_indicator_pct=buffett_pct,
            yield_curve_10y_2y_spread=spread_10y_2y,
            fear_and_greed_index=fear_greed,
            market_phase=phase,
            recommended_dry_powder_pct=cash_target,
        )


# ============================================================================
# 2. CRISIS ALPHA & DRAWDOWN LADDERING ENGINE
# ============================================================================

@dataclass(slots=True)
class CrisisDeploymentOrder:
    drawdown_threshold: float
    tranche_pct: float
    deployed_amount: float
    remaining_reserve: float
    enhanced_dca_multiplier: float
    action_note: str


class CrisisDrawdownManager:
    def __init__(self, total_reserve_cash: float, base_monthly_dca: float = 1000.0):
        self.initial_reserve = float(total_reserve_cash)
        self.current_reserve = float(total_reserve_cash)
        self.base_monthly_dca = float(base_monthly_dca)
        self.executed_thresholds: set[float] = set()

        # Tranches de Drawdown (Queda % do S&P 500 do pico)
        self.tranches = [
            (-0.10, 0.15, "Correction Entry: Iniciar compras táticas"),
            (-0.20, 0.25, "Bear Market Entry: Alocação com desconto de múltiplos"),
            (-0.30, 0.30, "Deep Bear Crash: Implantação pesada de liquidez (Meta do Plano)"),
            (-0.40, 0.20, "Generational Crisis: Alocação de convicção máxima"),
            (-0.50, 0.10, "Capitulation Bottom: Varredura final de liquidez"),
        ]

    def calculate_deployment_plan(
        self, current_drawdown: float, *, mark_executed: bool = False
    ) -> list[CrisisDeploymentOrder]:
        orders = []
        spent_in_batch = 0.0
        for threshold, pct_alloc, note in self.tranches:
            if threshold in self.executed_thresholds:
                continue
            tranche_val = min(
                max(0.0, self.current_reserve - spent_in_batch),
                self.initial_reserve * pct_alloc,
            )
            if tranche_val <= 0.0:
                continue
            # Multiplicador Enhanced DCA: M = 1 + 2 * |drawdown|
            edca_multiplier = 1.0 + 2.0 * abs(threshold)

            if current_drawdown <= threshold:
                spent_in_batch += tranche_val
                rem = max(0.0, self.current_reserve - spent_in_batch)
                order = CrisisDeploymentOrder(
                    drawdown_threshold=threshold,
                    tranche_pct=pct_alloc,
                    deployed_amount=tranche_val,
                    remaining_reserve=rem,
                    enhanced_dca_multiplier=edca_multiplier,
                    action_note=note,
                )
                orders.append(order)
                if mark_executed:
                    self.executed_thresholds.add(threshold)
        if mark_executed and orders:
            self.current_reserve = max(0.0, self.current_reserve - spent_in_batch)
        return orders

    def execute_tranche(self, threshold: float) -> CrisisDeploymentOrder | None:
        """Execute a specific tranche by its threshold if not already executed."""
        if threshold in self.executed_thresholds:
            return None
        for t_val, pct_alloc, note in self.tranches:
            if abs(t_val - threshold) < 1e-6:
                tranche_val = min(self.current_reserve, self.initial_reserve * pct_alloc)
                if tranche_val <= 0.0:
                    return None
                self.executed_thresholds.add(t_val)
                self.current_reserve = max(0.0, self.current_reserve - tranche_val)
                return CrisisDeploymentOrder(
                    drawdown_threshold=t_val,
                    tranche_pct=pct_alloc,
                    deployed_amount=tranche_val,
                    remaining_reserve=self.current_reserve,
                    enhanced_dca_multiplier=1.0 + 2.0 * abs(t_val),
                    action_note=note,
                )
        return None

    def reset(self) -> None:
        """Reset the crisis manager state to initial reserves."""
        self.current_reserve = self.initial_reserve
        self.executed_thresholds.clear()


# ============================================================================
# 3. FINANCIAL TAX ENGINE (PORTUGUESE IRS ANEXO G / ANEXO J)
# ============================================================================

# Keep the historical public names importable after the class rename.
DrawdownTrancheOrder = CrisisDeploymentOrder
CrisisAlphaDeployer = CrisisDrawdownManager


@dataclass(slots=True, frozen=True)
class TaxLot:
    id: int
    asset_symbol: str
    buy_date: date
    quantity: float
    buy_price: float
    buy_fees: float
    is_foreign: bool = True  # True: Anexo J, False: Anexo G
    country_code: str = "PT"
    is_security_token: bool = False

    @property
    def unit_cost(self) -> float:
        if self.quantity <= 1e-9:
            return self.buy_price
        return (self.quantity * self.buy_price + self.buy_fees) / self.quantity


@dataclass(slots=True)
class RealizedTaxGain:
    asset_symbol: str
    buy_date: date
    sell_date: date
    quantity: float
    acquisition_cost: float
    realized_value: float
    expenses: float
    taxable_profit: float
    holding_days: int
    is_exempt: bool
    irs_annex: str  # "Anexo G" ou "Anexo J"
    tax_rate_pct: float = 28.0
    estimated_tax: float = 0.0


def process_fifo_sale(
    open_lots: list[TaxLot],
    sell_date: date,
    sell_quantity: float,
    sell_price: float,
    sell_fees: float,
    is_crypto: bool = False,
    asset_symbol: str | None = None,
) -> tuple[list[TaxLot], list[RealizedTaxGain]]:
    """Consome lotes fiscais em estrita ordem cronológica (FIFO) e calcula mais/menos-valias."""
    from services.tax import (
        OFFSHORE_JURISDICTIONS,
        TAXA_AGRAVADA_OFFSHORE,
        TAXA_LIBERATORIA_PADRAO,
    )

    if sell_quantity <= 1e-9:
        raise ValueError("A quantidade a vender deve ser estritamente superior a zero.")

    # BUG-TAX-SIM-03: Filtrar por símbolo de ativo para não misturar ativos diferentes
    if asset_symbol is not None:
        target_lots = [lot for lot in open_lots if lot.asset_symbol == asset_symbol]
        other_lots = [lot for lot in open_lots if lot.asset_symbol != asset_symbol]
    elif open_lots:
        first_symbol = open_lots[0].asset_symbol
        if all(lot.asset_symbol == first_symbol for lot in open_lots):
            target_lots = list(open_lots)
            other_lots = []
        else:
            target_lots = [lot for lot in open_lots if lot.asset_symbol == first_symbol]
            other_lots = [lot for lot in open_lots if lot.asset_symbol != first_symbol]
    else:
        target_lots = []
        other_lots = []

    # TAX-05: Lotes adquiridos após a data da venda não podem ser consumidos
    available_lots = sorted(
        [lot for lot in target_lots if lot.buy_date <= sell_date],
        key=lambda lot: lot.buy_date,
    )
    future_lots = [lot for lot in target_lots if lot.buy_date > sell_date]

    available_qty = sum(lot.quantity for lot in available_lots)
    if sell_quantity > available_qty + 1e-9:
        raise ValueError(
            f"Quantidade insuficiente adquirida até {sell_date} para vender {sell_quantity} unidades "
            f"(disponível: {available_qty})."
        )

    remaining_to_sell = sell_quantity
    realized_events: list[RealizedTaxGain] = []
    updated_lots: list[TaxLot] = []

    for lot in available_lots:
        if remaining_to_sell <= 1e-9:
            updated_lots.append(lot)
            continue

        matched_qty = min(lot.quantity, remaining_to_sell)
        # TAX-06: No Modelo 3 Anexo G/J, acquisition_cost é o valor nominal de compra (qty * buy_price)
        # e as despesas e encargos (Art. 51.º CIRS) acolhem todas as comissões incorridas (compra + venda)
        nominal_acq_cost = matched_qty * lot.buy_price
        lot_gross_revenue = matched_qty * sell_price
        lot_acq_fees = (
            (matched_qty / lot.quantity) * lot.buy_fees if lot.quantity > 1e-9 else 0.0
        )
        lot_sell_fees = (matched_qty / sell_quantity) * sell_fees
        total_expenses = lot_acq_fees + lot_sell_fees

        # BUG-TAX-SIM-01: Arredondar valores a 2 decimais antes da subtração para garantir
        # a identidade matemática exata exigida pela Autoridade Tributária (AT Anexo G/J):
        # realized_value - acquisition_cost - expenses == taxable_profit
        rounded_realized = round(lot_gross_revenue, 2)
        rounded_acq = round(nominal_acq_cost, 2)
        rounded_expenses = round(total_expenses, 2)
        profit = round(rounded_realized - rounded_acq - rounded_expenses, 2)

        holding_days = max(0, (sell_date - lot.buy_date).days)
        is_offshore = lot.country_code.upper() in OFFSHORE_JURISDICTIONS
        is_security_token = bool(getattr(lot, "is_security_token", False))
        # TAX-07: Isenção de cripto (>365 dias) não se aplica a security tokens nem a entidades offshore
        is_exempt = (
            is_crypto
            and (holding_days >= 365)
            and not is_offshore
            and not is_security_token
        )
        annex = (
            "Anexo J (Estrangeiro)"
            if (lot.is_foreign or is_offshore)
            else "Anexo G (Nacional)"
        )

        if is_exempt:
            tax_rate = 0.0
        elif is_offshore:
            tax_rate = TAXA_AGRAVADA_OFFSHORE
        else:
            tax_rate = TAXA_LIBERATORIA_PADRAO

        taxable_profit = 0.0 if is_exempt else profit

        realized_events.append(
            RealizedTaxGain(
                asset_symbol=lot.asset_symbol,
                buy_date=lot.buy_date,
                sell_date=sell_date,
                quantity=matched_qty,
                acquisition_cost=rounded_acq,
                realized_value=rounded_realized,
                expenses=rounded_expenses,
                taxable_profit=taxable_profit,
                holding_days=holding_days,
                is_exempt=is_exempt,
                irs_annex=annex,
                tax_rate_pct=tax_rate,
                estimated_tax=0.0,
            )
        )

        remaining_to_sell -= matched_qty

        # BUG-TAX-SIM-03: Eliminação de lotes de pó flutuante (leftover <= 1e-7)
        if lot.quantity - matched_qty > 1e-7:
            leftover_qty = round(lot.quantity - matched_qty, 8)
            updated_lots.append(
                TaxLot(
                    id=lot.id,
                    asset_symbol=lot.asset_symbol,
                    buy_date=lot.buy_date,
                    quantity=leftover_qty,
                    buy_price=lot.buy_price,
                    buy_fees=(
                        (leftover_qty / lot.quantity) * lot.buy_fees
                        if lot.quantity > 1e-9
                        else 0.0
                    ),
                    is_foreign=lot.is_foreign,
                    country_code=lot.country_code,
                    is_security_token=lot.is_security_token,
                )
            )

    if remaining_to_sell > 1e-6:
        raise ValueError(
            f"Quantidade insuficiente em carteira para vender {sell_quantity} unidades."
        )

    # BUG-TAX-SIM-02 & CIRS Art. 43.º n.º 3: Compensação de perdas (Loss Netting)
    # O imposto a pagar incide sobre o saldo líquido positivo das mais e menos-valias.
    # As menos-valias de entidades sedeadas em paraísos fiscais / offshores são excluídas da compensação.
    net_taxable_profit = sum(
        ev.taxable_profit
        for ev in realized_events
        if not ev.is_exempt
        and not (ev.tax_rate_pct == TAXA_AGRAVADA_OFFSHORE and ev.taxable_profit < 0)
    )
    if net_taxable_profit <= 0.0:
        for ev in realized_events:
            ev.estimated_tax = 0.0
    else:
        total_positive_gains = sum(
            ev.taxable_profit
            for ev in realized_events
            if ev.taxable_profit > 0 and not ev.is_exempt
        )
        for ev in realized_events:
            if (
                ev.taxable_profit > 0
                and not ev.is_exempt
                and total_positive_gains > 1e-9
            ):
                netted_gain_share = ev.taxable_profit * (
                    net_taxable_profit / total_positive_gains
                )
                ev.estimated_tax = round(
                    netted_gain_share * (ev.tax_rate_pct / 100.0), 2
                )
            else:
                ev.estimated_tax = 0.0

    # Preservar lotes futuros e lotes de outros ativos não alienados nesta operação
    updated_lots.extend(future_lots)
    updated_lots.extend(other_lots)

    return updated_lots, realized_events


def summarize_tax_realization(
    realized: list[RealizedTaxGain],
) -> dict[str, float]:
    """Sintetiza os resultados tributários de uma alienação conforme o Art. 43.º do CIRS."""
    total_realized = sum(r.realized_value for r in realized)
    total_acq = sum(r.acquisition_cost for r in realized)
    total_expenses = sum(r.expenses for r in realized)
    net_profit = sum(r.taxable_profit for r in realized)
    total_tax = sum(r.estimated_tax for r in realized)
    return {
        "total_realized_value": round(total_realized, 2),
        "total_acquisition_cost": round(total_acq, 2),
        "total_expenses": round(total_expenses, 2),
        "net_taxable_profit": round(net_profit, 2),
        "estimated_tax": round(total_tax, 2),
    }



# ============================================================================
# DEMO EXECUTION
# ============================================================================

if __name__ == "__main__":
    print("=" * 70)
    print("1. MACRO VALUATION ENGINE SNAPSHOT")
    print("=" * 70)
    macro_snap = MacroCycleEngine.evaluate_macro_regime(
        cape=41.5,
        buffett_pct=240.0,
        spread_10y_2y=0.49,
        fear_greed=70.0
    )
    print(f"Estado de Ciclo: {macro_snap.market_phase}")
    print(f"Shiller CAPE: {macro_snap.shiller_cape:.1f}x (Percentil ~{macro_snap.cape_historical_percentile:.1f}%)")
    print(f"Buffett Indicator: {macro_snap.buffett_indicator_pct:.1f}%")
    print(f"Reserva de Liquidez Tática Recomendada: {macro_snap.recommended_dry_powder_pct*100:.0f}%\n")

    print("=" * 70)
    print("2. PLANO DE TRANCHES PARA QUEDA DE 30% (CRISIS ALPHA LADDER)")
    print("=" * 70)
    total_dry_powder = 25000.0  # Exemplo: 25.000 € em reserva
    base_dca = 1000.0           # Aporte mensal regular de 1.000 €
    
    manager = CrisisDrawdownManager(total_dry_powder, base_dca)
    orders = manager.calculate_deployment_plan(-0.30)
    
    for o in orders:
        print(f"Trigger {o.drawdown_threshold*100:+.0f}%: {o.action_note}")
        print(f"  -> Alocar da Reserva: {o.deployed_amount:,.2f} € ({o.tranche_pct*100:.0f}% da reserva total)")
        print(f"  -> Reserva Restante: {o.remaining_reserve:,.2f} €")
        print(f"  -> Aporte Mensal Dinâmico (EDCA {o.enhanced_dca_multiplier:.1f}x): {base_dca * o.enhanced_dca_multiplier:,.2f} €/mês\n")

    print("=" * 70)
    print("3. SIMULAÇÃO FISCAL FIFO & ANEXO J (PORTUGAL)")
    print("=" * 70)
    sample_lots = [
        TaxLot(id=1, asset_symbol="VOO", buy_date=date(2023, 1, 15), quantity=20, buy_price=360.0, buy_fees=2.0, is_foreign=True),
        TaxLot(id=2, asset_symbol="VOO", buy_date=date(2024, 6, 10), quantity=15, buy_price=490.0, buy_fees=2.0, is_foreign=True),
        TaxLot(id=3, asset_symbol="VOO", buy_date=date(2025, 11, 20), quantity=10, buy_price=560.0, buy_fees=2.0, is_foreign=True),
    ]
    
    remaining_lots, realized = process_fifo_sale(
        open_lots=sample_lots,
        sell_date=date(2026, 8, 24),
        sell_quantity=25,
        sell_price=540.0,
        sell_fees=5.0,
        is_crypto=False
    )
    
    total_taxable_gain = sum(r.taxable_profit for r in realized)
    print(f"Venda Efetuada: 25 unidades de VOO a 540.00 € (Total Bruto: {25*540:.2f} €)")
    print(f"Lotes Restantes em Carteira: {len(remaining_lots)} lotes ({sum(lot.quantity for lot in remaining_lots)} unidades)")

    print(f"Mais-Valia Líquida Tributável Total: {total_taxable_gain:,.2f} €")
    print(f"Imposto Estimado (28% Taxa Autónoma): {total_taxable_gain * 0.28:,.2f} €")
    print(f"Classificação Fiscal: {realized[0].irs_annex} (Quadro 9.2A)")
    print("=" * 70)
