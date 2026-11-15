"""Portuguese IRS Tax Engine (CIRS - Código do Imposto sobre o Rendimento das Pessoas Singulares).

Provides FIFO tax-lot matching, capital gains/losses calculations, currency
devaluation coefficients, crypto holding-period exemption (>365 days), and
tax reporting breakdown for Modelo 3 (Anexo G and Anexo J).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Sequence, Union

from utils.helpers import safe_float, strict_float


# Official Portuguese Currency Devaluation Coefficients (Artigo 50.º do CIRS)
# Published annually by the Ministry of Finance (Portaria n.º 340/2023 and updates)
COEFICIENTES_DESVALORIZACAO: dict[int, float] = {
    1990: 2.76,
    1991: 2.48,
    1992: 2.27,
    1993: 2.13,
    1994: 2.03,
    1995: 1.95,
    1996: 1.89,
    1997: 1.85,
    1998: 1.80,
    1999: 1.76,
    2000: 1.71,
    2001: 1.64,
    2002: 1.58,
    2003: 1.53,
    2004: 1.49,
    2005: 1.46,
    2006: 1.42,
    2007: 1.38,
    2008: 1.35,
    2009: 1.36,
    2010: 1.34,
    2011: 1.29,
    2012: 1.25,
    2013: 1.25,
    2014: 1.25,
    2015: 1.24,
    2016: 1.23,
    2017: 1.21,
    2018: 1.20,
    2019: 1.19,
    2020: 1.19,
    2021: 1.17,
    2022: 1.09,
    2023: 1.04,
    2024: 1.00,
    2025: 1.00,
    2026: 1.00,
}

TAXA_LIBERATORIA_PADRAO = 28.0  # Artigo 72.º n.º 1 do CIRS
TAXA_AGRAVADA_OFFSHORE = 35.0  # Artigo 72.º n.º 14 do CIRS (Portaria n.º 150/2004)

# Jurisdições com regime de tributação claramente mais favorável (Portaria n.º 150/2004)
OFFSHORE_JURISDICTIONS: frozenset[str] = frozenset(
    {
        "AD",
        "AI",
        "AG",
        "AN",
        "AW",
        "BS",
        "BH",
        "BB",
        "BZ",
        "BM",
        "BO",
        "VG",
        "BN",
        "KY",
        "CK",
        "CR",
        "CW",
        "DM",
        "FJ",
        "GI",
        "GD",
        "GU",
        "GG",
        "GY",
        "HK",
        "IM",
        "JE",
        "JO",
        "LB",
        "LR",
        "LI",
        # LU (Luxemburgo) é Estado-Membro da UE, não consta da lista de paraísos fiscais (Portaria 150/2004) - BUG-AUD-C51
        "MO",
        "MV",
        "MH",
        "MU",
        "MC",
        "MS",
        "NR",
        "NC",
        "OM",
        "PA",
        # QA (Qatar) removido por Portaria n.º 345-B/2016 (BUG-AUD-C62)
        "WS",
        "SM",
        "ST",
        "SC",
        "SG",
        "SX",
        "KN",
        "LC",
        "VC",
        "SZ",
        "TT",
        "TC",
        # AE (Emirados Árabes Unidos) removido por Portaria n.º 309-A/2020 (BUG-AUD-C62)
        "VI",
        "VU",
        "YE",
    }
)


def months_between(start: date, end: date) -> int:
    """Full months elapsed between two dates, day-of-month aware (BUG-TAX-02).

    The old naive ``year*12 + month`` count ignored the day of month and
    inflated holding periods (e.g. 2020-03-15 -> 2022-03-14 was wrongly
    counted as 24 months). A month only counts once the end date reaches the
    start's day of month; negative spans return 0.
    """
    if end <= start:
        return 0
    months = (end.year - start.year) * 12 + (end.month - start.month)
    if end.day < start.day:
        months -= 1
    return max(0, months)


# BUG-TAX-03 (CIRS Art. 10.? n.? 19): crypto-to-crypto swaps are not a
# realization event in EUR. Only conversion into fiat (or payment for
# goods/services) triggers taxable capital gains.
_CRYPTO_CATEGORIES: frozenset[str] = frozenset(
    {
        "crypto",
        "cripto",
        "criptoativo",
        "criptoativos",
        "criptomoeda",
        "criptomoedas",
        "crypto_manual",
        "cripto manual",
        "stablecoin",
        "token",
    }
)

# Well-known cryptoasset symbols (exact match only, uppercase).
_CRYPTO_SYMBOLS: frozenset[str] = frozenset(
    {
        "BTC", "ETH", "XRP", "LTC", "BCH", "ADA", "SOL", "DOT", "DOGE",
        "AVAX", "MATIC", "LINK", "UNI", "AAVE", "ATOM", "XLM", "ETC",
        "TRX", "FIL", "XMR", "ALGO", "VET", "ICP", "HBAR", "SAND",
        "MANA", "AXS", "SHIB", "PEPE", "BNB", "USDT", "USDC", "DAI",
        "WBTC", "ETH2", "STETH", "NEAR", "ARB", "OP", "SUI",
    }
)

# Exact descriptions/categories that unambiguously identify a swap/exchange
# between cryptoassets (never a fiat realization).
CRYPTO_SWAP_EXCHANGE_EXACT: frozenset[str] = frozenset(
    {"swap", "permuta", "exchange", "trade", "troca", "transferencia", "transfer"}
)

# Substring patterns (encoding-agnostic: "permut" matches permuta/permutou,
# "convers" matches conversao/conversao with any accent variant).
CRYPTO_SWAP_EXCHANGE_PATTERNS: tuple[str, ...] = (
    "swap",
    "permut",
    "exchange",
    "troca",
    "convers",
    "trade",
    "transfer",
)


def _looks_like_crypto_asset(asset_symbol: str, asset_category: str = "") -> bool:
    """Heuristic crypto-asset detection by category tag or well-known symbol."""
    category = str(asset_category or "").strip().lower()
    if category in _CRYPTO_CATEGORIES:
        return True
    symbol = str(asset_symbol or "").strip().upper()
    return symbol in _CRYPTO_SYMBOLS


def is_crypto_to_crypto_swap(
    *,
    asset_symbol: str,
    is_crypto: bool = False,
    asset_category: str = "",
    counterparty_symbol: str = "",
    counterparty_is_crypto: bool = False,
    disposal_description: str = "",
    disposal_category: str = "",
    proceeds_in_fiat: bool = True,
) -> bool:
    """Return True when a disposal is a crypto-to-crypto swap (BUG-TAX-03).

    Under CIRS Art. 10.? n.? 19 a permuta of one cryptoasset for another is
    not a realization in euros: no IRS is due until the received asset is
    converted into fiat (or used to pay for goods/services).
    """
    disposed_is_crypto = bool(is_crypto) or _looks_like_crypto_asset(
        asset_symbol, asset_category
    )
    # Explicit fiat proceeds take precedence over inferred counterparty labels.
    if not disposed_is_crypto or proceeds_in_fiat:
        return False
    received_is_crypto = bool(counterparty_is_crypto) or _looks_like_crypto_asset(
        counterparty_symbol, ""
    )
    if received_is_crypto:
        return True
    description = str(disposal_description or "").strip().lower()
    category = str(disposal_category or "").strip().lower()
    if description in CRYPTO_SWAP_EXCHANGE_EXACT:
        return True
    if category in CRYPTO_SWAP_EXCHANGE_EXACT:
        return True
    return any(pattern in description for pattern in CRYPTO_SWAP_EXCHANGE_PATTERNS)


@dataclass(slots=True)
class AcquisitionLot:
    """Individual tax lot representing an asset purchase."""

    id: str
    asset_symbol: str
    asset_name: str
    acquisition_date: date
    quantity: float
    unit_price: float
    fees: float = 0.0
    is_crypto: bool = False
    is_foreign: bool = False
    country_code: str = "PT"
    asset_category: str = "security"
    is_real_estate: bool = False
    is_security_token: bool = False
    remaining_quantity: float = field(init=False)

    def __post_init__(self) -> None:
        self.remaining_quantity = max(0.0, float(self.quantity))


@dataclass(slots=True)
class DisposalTransaction:
    """Disposal/sale of an asset requiring capital gains determination."""

    id: str
    asset_symbol: str
    disposal_date: date
    quantity: float
    unit_price: float
    fees: float = 0.0
    is_crypto: bool = False
    is_foreign: bool = False
    country_code: str = "PT"
    asset_category: str = "security"
    is_real_estate: bool = False
    is_security_token: bool = False


@dataclass(slots=True)
class TaxLotMatch:
    """Detailed matching between a disposal and an acquisition lot under FIFO."""

    disposal_id: str
    acquisition_lot_id: str
    asset_symbol: str
    acquisition_date: date
    disposal_date: date
    matched_quantity: float
    acquisition_price: float
    disposal_price: float
    acquisition_fees_allocated: float
    disposal_fees_allocated: float
    devaluation_coefficient: float
    adjusted_acquisition_cost: float
    gross_disposal_proceeds: float
    net_gain_loss: float
    holding_days: int
    is_crypto: bool
    is_exempt_crypto: bool
    tax_rate_pct: float
    estimated_tax: float
    tax_schedule: str  # "Anexo G" or "Anexo J"
    is_crypto_to_crypto_swap: bool = False
    nominal_acquisition_cost: float = 0.0
    deductible_expenses: float = 0.0

    def __post_init__(self) -> None:
        if self.nominal_acquisition_cost == 0.0 and self.adjusted_acquisition_cost > 0.0:
            self.nominal_acquisition_cost = self.adjusted_acquisition_cost
        if self.deductible_expenses == 0.0 and (
            self.acquisition_fees_allocated > 0.0 or self.disposal_fees_allocated > 0.0
        ):
            self.deductible_expenses = (
                self.acquisition_fees_allocated + self.disposal_fees_allocated
            )


class PortugueseTaxEngine:
    """Engine for Portuguese IRS capital gains, tax lots, and exemptions."""

    def __init__(self) -> None:
        self._lots: dict[str, list[AcquisitionLot]] = {}

    def add_acquisition(
        self,
        *,
        lot_id: str,
        asset_symbol: str,
        asset_name: str = "",
        acquisition_date: Union[date, str],
        quantity: float,
        unit_price: float,
        fees: float = 0.0,
        is_crypto: bool = False,
        is_foreign: bool = False,
        country_code: str = "PT",
        asset_category: str = "security",
        is_real_estate: bool = False,
        is_security_token: bool = False,
    ) -> AcquisitionLot:
        d_acq = (
            date.fromisoformat(str(acquisition_date)[:10])
            if isinstance(acquisition_date, str)
            else acquisition_date
        )
        lot = AcquisitionLot(
            id=str(lot_id),
            asset_symbol=str(asset_symbol).upper().strip(),
            asset_name=str(asset_name or asset_symbol).strip(),
            acquisition_date=d_acq,
            quantity=max(0.0, safe_float(quantity)),
            unit_price=max(0.0, safe_float(unit_price)),
            fees=max(0.0, safe_float(fees)),
            is_crypto=bool(is_crypto),
            is_foreign=bool(is_foreign),
            country_code=str(country_code or "PT").upper()[:2],
            asset_category=str(asset_category or "security").strip().lower(),
            is_real_estate=bool(is_real_estate),
            is_security_token=bool(is_security_token),
        )
        sym = lot.asset_symbol
        if sym not in self._lots:
            self._lots[sym] = []
        self._lots[sym].append(lot)
        # Keep FIFO order by acquisition date
        self._lots[sym].sort(key=lambda item: (item.acquisition_date, item.id))
        return lot

    def add_lot(self, lot: AcquisitionLot) -> AcquisitionLot:
        """Register an existing AcquisitionLot directly (e.g. rolled over from crypto swap)."""
        sym = lot.asset_symbol
        if sym not in self._lots:
            self._lots[sym] = []
        self._lots[sym].append(lot)
        self._lots[sym].sort(key=lambda item: (item.acquisition_date, item.id))
        return lot

    def apply_stock_split(
        self,
        asset_symbol: str,
        split_ratio: float,
        split_date: Union[date, str],
    ) -> int:
        """Apply a forward or reverse stock split to existing acquisition lots.

        Adjusts lot quantity, remaining quantity and unit price while strictly
        preserving the total acquisition cost basis and historical fee allocations.

        Args:
            asset_symbol: Ticker or symbol of the security (case-insensitive).
            split_ratio: Split factor (> 0.0, e.g. 2.0 for 2:1 forward split, 0.1 for 1:10 reverse split).
            split_date: Effective execution date of the corporate action. Only lots acquired on
                        or before this date are adjusted.

        Returns:
            Number of adjusted lots.
        """
        ratio = strict_float(
            safe_float(split_ratio, default=float("nan")),
            field_name="rácio de split",
        )
        if ratio <= 0.0:
            raise ValueError("O rácio de split deve ser estritamente superior a zero.")

        d_split = (
            date.fromisoformat(str(split_date)[:10])
            if isinstance(split_date, str)
            else split_date
        )
        sym = str(asset_symbol).upper().strip()
        lots = self._lots.get(sym, [])
        adjusted_count = 0
        for lot in lots:
            if lot.acquisition_date <= d_split and lot.remaining_quantity > 1e-9:
                lot.quantity = round(lot.quantity * ratio, 8)
                lot.remaining_quantity = round(lot.remaining_quantity * ratio, 8)
                lot.unit_price = round(lot.unit_price / ratio, 8)
                adjusted_count += 1
        return adjusted_count

    def process_disposal(
        self,
        *,
        disposal_id: str,
        asset_symbol: str,
        disposal_date: Union[date, str],
        quantity: float,
        unit_price: float,
        fees: float = 0.0,
        is_crypto: bool = False,
        is_foreign: bool = False,
        country_code: str = "PT",
        asset_category: str = "security",
        is_real_estate: bool = False,
        is_security_token: bool = False,
        counterparty_symbol: str = "",
        counterparty_is_crypto: bool = False,
        disposal_description: str = "",
        disposal_category: str = "",
        proceeds_in_fiat: bool = True,
    ) -> list[TaxLotMatch]:
        """Match disposal against existing acquisition lots using FIFO.

        BUG-TAX-03 (CIRS Art. 10.? n.? 19): crypto-to-crypto swaps are not a
        realization event in euros. Pass ``proceeds_in_fiat=False`` plus the
        counterparty asset (or an explicit swap description/category) to mark
        the disposal as a permuta cripto-cripto; the engine then returns
        zero-tax matches that must not enter the Anexo G/J taxable balance.
        """
        d_disp = (
            date.fromisoformat(str(disposal_date)[:10])
            if isinstance(disposal_date, str)
            else disposal_date
        )
        sym = str(asset_symbol).upper().strip()
        # A disposal must preserve the user's declared values. Coercing invalid
        # inputs to zero can turn a malformed transaction into an apparently
        # valid tax-free sale.
        disp_qty = strict_float(
            safe_float(quantity, default=float("nan")),
            field_name="quantidade vendida",
        )
        if disp_qty <= 0.0:
            raise ValueError("Quantidade vendida deve ser superior a zero.")

        disp_price = strict_float(
            safe_float(unit_price, default=float("nan")),
            field_name="preço unitário de venda",
        )
        if disp_price <= 0.0:
            raise ValueError("Preço unitário de venda deve ser superior a zero.")

        total_disp_fees = strict_float(
            safe_float(fees, default=float("nan")),
            field_name="taxas da venda",
        )
        if total_disp_fees < 0.0:
            raise ValueError("Taxas da venda não podem ser negativas.")

        available_lots = [
            lot
            for lot in self._lots.get(sym, [])
            if lot.remaining_quantity > 1e-9 and lot.acquisition_date <= d_disp
        ]
        matches: list[TaxLotMatch] = []
        remaining_to_match = disp_qty

        # Only lots owned on the disposal date can satisfy the sale. The list
        # is already maintained in acquisition-date/id order by add_acquisition,
        # preserving deterministic FIFO matching among eligible lots.
        acquired_total = sum(lot.remaining_quantity for lot in available_lots)
        if disp_qty > acquired_total:
            raise ValueError(
                "Venda de "
                f"{disp_qty:g} {sym} excede a quantidade adquirida "
                f"({acquired_total:g}). Regista as aquisições em falta antes "
                "de processar a venda."
            )

        for lot in available_lots:
            if remaining_to_match <= 1e-9:
                break
            matched_qty = min(remaining_to_match, lot.remaining_quantity)
            if matched_qty <= 1e-9:
                continue

            lot.remaining_quantity -= matched_qty
            remaining_to_match -= matched_qty

            # Allocate fees proportionally
            disp_fee_alloc = (
                (matched_qty / disp_qty) * total_disp_fees if disp_qty > 0 else 0.0
            )
            acq_fee_alloc = (
                (matched_qty / lot.quantity) * lot.fees if lot.quantity > 0 else 0.0
            )

            holding_days = max(0, (d_disp - lot.acquisition_date).days)
            # BUG-TAX-02: day-of-month aware month count (Art. 50.? CIRS).
            # 2020-03-15 -> 2022-03-14 is 23 full months, not 24.
            holding_months = months_between(lot.acquisition_date, d_disp)
            # Artigo 50.º CIRS: Coeficientes de desvalorização monetária
            # Desde a Reforma do IRS de 2014, aplicam-se exclusivamente a bens imóveis.
            # Valores mobiliários cotados (ações, fundos, ETFs) e criptoativos têm coef = 1.0.
            is_re = bool(
                lot.is_real_estate
                or is_real_estate
                or str(lot.asset_category or asset_category or "").lower()
                in ("real_estate", "imobiliario", "imobiliário", "imovel", "imóvel")
            )
            coef = 1.0
            if is_re and holding_months >= 24.0:
                coef = COEFICIENTES_DESVALORIZACAO.get(lot.acquisition_date.year, 1.0)

            # AUD-TAX-02 & CIRS Art. 51.º:
            # Nominal acquisition cost (adjusted by currency devaluation coefficient if applicable)
            # is separated from deductible expenses and charges (acquisition and disposal fees).
            nominal_acq_cost = (matched_qty * lot.unit_price) * coef
            deductible_expenses = acq_fee_alloc + disp_fee_alloc
            gross_proceeds = matched_qty * disp_price
            net_gain_loss = (gross_proceeds - disp_fee_alloc) - (nominal_acq_cost + acq_fee_alloc)

            # CIRS Crypto Exemption (Art. 10.º n.º 1 k) e n.º 19 CIRS)
            # Não se aplica se for security token ou se o intermediário/entidade for offshore (regime privilegiado)
            disp_country = str(country_code or "").upper().strip()[:2]
            acq_country = str(getattr(lot, "country_code", "") or "").upper().strip()[:2]
            is_offshore = (
                acq_country in OFFSHORE_JURISDICTIONS
                or (bool(disp_country) and disp_country in OFFSHORE_JURISDICTIONS)
            )
            security_token_flag = bool(
                getattr(lot, "is_security_token", False)
                or is_security_token
                or str(getattr(lot, "asset_category", "") or asset_category or "").lower()
                in ("security_token", "security_tokens", "token_mobiliario", "token_mobiliário")
            )
            crypto_flag = bool(lot.is_crypto or is_crypto)
            is_exempt_crypto = (
                crypto_flag
                and (holding_days >= 365)
                and not is_offshore
                and not security_token_flag
            )

            # BUG-TAX-03 (CIRS Art. 10.? n.? 19): a permuta cripto-cripto nao e
            # realizacao em euros. Nao ha IRS ate a conversao para fiat.
            is_swap = is_crypto_to_crypto_swap(
                asset_symbol=sym,
                is_crypto=crypto_flag,
                asset_category=str(
                    getattr(lot, "asset_category", "") or asset_category or ""
                ),
                counterparty_symbol=counterparty_symbol,
                counterparty_is_crypto=counterparty_is_crypto,
                disposal_description=disposal_description,
                disposal_category=disposal_category,
                proceeds_in_fiat=proceeds_in_fiat,
            )
            if is_swap:
                tax_rate = 0.0
                est_tax = 0.0

            # Tax rate & estimated tax (Artigo 72.º n.º 1 e n.º 14 CIRS)
            elif is_exempt_crypto:
                tax_rate = 0.0
                est_tax = 0.0
            elif is_offshore:
                tax_rate = TAXA_AGRAVADA_OFFSHORE
                est_tax = (
                    net_gain_loss * (tax_rate / 100.0) if net_gain_loss > 0 else 0.0
                )
            elif net_gain_loss > 0:
                tax_rate = TAXA_LIBERATORIA_PADRAO
                est_tax = net_gain_loss * (tax_rate / 100.0)
            else:
                tax_rate = TAXA_LIBERATORIA_PADRAO
                est_tax = 0.0

            schedule = "Anexo J" if (lot.is_foreign or is_foreign or is_offshore) else "Anexo G"

            match = TaxLotMatch(
                disposal_id=str(disposal_id),
                acquisition_lot_id=lot.id,
                asset_symbol=sym,
                acquisition_date=lot.acquisition_date,
                disposal_date=d_disp,
                matched_quantity=matched_qty,
                acquisition_price=lot.unit_price,
                disposal_price=disp_price,
                acquisition_fees_allocated=acq_fee_alloc,
                disposal_fees_allocated=disp_fee_alloc,
                devaluation_coefficient=coef,
                adjusted_acquisition_cost=nominal_acq_cost,
                gross_disposal_proceeds=gross_proceeds,
                net_gain_loss=net_gain_loss,
                holding_days=holding_days,
                is_crypto=crypto_flag,
                is_exempt_crypto=is_exempt_crypto,
                is_crypto_to_crypto_swap=is_swap,
                tax_rate_pct=tax_rate,
                estimated_tax=est_tax,
                tax_schedule=schedule,
                nominal_acquisition_cost=nominal_acq_cost,
                deductible_expenses=deductible_expenses,
            )
            matches.append(match)

        return matches

    def process_crypto_swap(
        self,
        *,
        swap_id: str,
        from_asset_symbol: str,
        to_asset_symbol: str,
        swap_date: Union[date, str],
        quantity_disposed: float,
        quantity_acquired: float,
        fees: float = 0.0,
        country_code: str = "PT",
    ) -> tuple[list[TaxLotMatch], list[AcquisitionLot]]:
        """Process a crypto-to-crypto swap pursuant to CIRS Art. 10.º n.º 19.

        Under Portuguese CIRS Art. 10.º n.º 19, crypto-to-crypto swaps do not constitute
        a taxable capital realization event. The newly acquired cryptoassets inherit
        the original acquisition date and acquisition cost of the delivered lots proportionally,
        preserving continuous holding periods for the >365-day exemption.
        """
        qty_disp = strict_float(
            safe_float(quantity_disposed, default=float("nan")),
            field_name="quantidade permutada entregue",
        )
        if qty_disp <= 0.0:
            raise ValueError("Quantidade permutada deve ser superior a zero.")

        qty_acq = strict_float(
            safe_float(quantity_acquired, default=float("nan")),
            field_name="quantidade permutada recebida",
        )
        if qty_acq <= 0.0:
            raise ValueError("Quantidade recebida deve ser superior a zero.")

        matches = self.process_disposal(
            disposal_id=str(swap_id),
            asset_symbol=from_asset_symbol,
            disposal_date=swap_date,
            quantity=qty_disp,
            unit_price=1.0,
            fees=fees,
            is_crypto=True,
            country_code=country_code,
            counterparty_symbol=to_asset_symbol,
            counterparty_is_crypto=True,
            proceeds_in_fiat=False,
            disposal_description=f"Permuta {from_asset_symbol}->{to_asset_symbol}",
        )

        new_lots: list[AcquisitionLot] = []
        for idx, match in enumerate(matches):
            ratio = (match.matched_quantity / qty_disp) if qty_disp > 0 else 0.0
            new_qty = qty_acq * ratio
            inherited_cost = (
                match.nominal_acquisition_cost
                + match.acquisition_fees_allocated
                + match.disposal_fees_allocated
            )
            unit_price = (inherited_cost / new_qty) if new_qty > 0 else 0.0
            lot = self.add_acquisition(
                lot_id=f"{swap_id}_swap_{idx}",
                asset_symbol=to_asset_symbol,
                asset_name=to_asset_symbol,
                acquisition_date=match.acquisition_date,
                quantity=new_qty,
                unit_price=unit_price,
                fees=0.0,
                is_crypto=True,
                country_code=country_code,
            )
            new_lots.append(lot)

        return matches, new_lots

    def generate_annual_tax_report(
        self,
        matches: Sequence[TaxLotMatch],
        fiscal_year: int,
        *,
        prior_year_losses: float = 0.0,
        apply_50pct_inclusion: bool = False,
        opt_for_englobamento: bool = False,
        prior_year_losses_by_year: dict[int, float] | None = None,
    ) -> dict[str, Any]:
        """Aggregate tax lot matches into a structured Portuguese IRS declaration summary.

        Args:
            matches: Sequence of matched tax lot transactions.
            fiscal_year: Tax year to report.
            prior_year_losses: Prior 5-year eligible capital loss carryforward under Art. 55.º CIRS.
            apply_50pct_inclusion: If True, applies 50% inclusion coefficient for eligible assets (Art. 43.º CIRS).
            opt_for_englobamento: If True, allows deducting prior year losses against net capital gains.
                                  Under Art. 55.º n.º 1 al. d) CIRS, loss carryforward requires opting
                                  for englobamento (general progressive tax rates); under autonomous
                                  taxation (28%), prior losses cannot be offset.
            prior_year_losses_by_year: BUG-TAX-05 - loss carryforward bucketed by origin year
                                  (Art. 51.? CIRS). When provided, only losses from years within
                                  the 5-year carryforward window are eligible for offset; the
                                  scalar ``prior_year_losses`` is then treated as the total of
                                  the (already expiry-filtered) buckets.
        """
        # BUG-TAX-03: crypto-to-crypto swaps never enter the taxable balance.
        year_matches = [
            m
            for m in matches
            if m.disposal_date.year == fiscal_year and not m.is_crypto_to_crypto_swap
        ]

        anexo_g_matches = [m for m in year_matches if m.tax_schedule == "Anexo G"]
        anexo_j_matches = [m for m in year_matches if m.tax_schedule == "Anexo J"]

        def summarize_group(group: list[TaxLotMatch]) -> dict[str, Any]:
            taxable = [m for m in group if not m.is_exempt_crypto]
            exempt = [m for m in group if m.is_exempt_crypto]

            gains = sum(m.net_gain_loss for m in taxable if m.net_gain_loss > 0)
            losses = sum(abs(m.net_gain_loss) for m in taxable if m.net_gain_loss < 0)
            net_balance = sum(m.net_gain_loss for m in taxable)

            exempt_proceeds = sum(m.gross_disposal_proceeds for m in exempt)
            exempt_gains = sum(m.net_gain_loss for m in exempt if m.net_gain_loss > 0)

            taxable_base = net_balance
            if apply_50pct_inclusion and taxable_base > 0:
                taxable_base = taxable_base * 0.50

            # TAX-04: CIRS Art. 43.º n.º 2: A inclusão a 50% é estritamente aplicável
            # a rendimentos sujeitos a englobamento obrigatório (taxas progressivas do Art. 68.º).
            # Não existe no ordenamento português a combinação de 50% com a taxa autónoma de 28%.
            if apply_50pct_inclusion or taxable_base <= 0:
                est_tax = 0.0
            else:
                # BUG-AUD-C61: CIRS Art. 43.º - sob taxa autónoma, as menos-valias compensam
                # as mais-valias do mesmo ano fiscal no apuramento do imposto estimado,
                # aplicando a taxa média ponderada dos ganhos ao saldo líquido positivo.
                if gains > 0:
                    group_weighted_rate = (
                        sum(
                            m.net_gain_loss * (m.tax_rate_pct / 100.0)
                            for m in taxable
                            if m.net_gain_loss > 0
                        )
                        / gains
                    )
                else:
                    group_weighted_rate = TAXA_LIBERATORIA_PADRAO / 100.0
                est_tax = taxable_base * group_weighted_rate

            return {
                "transaction_count": len(group),
                "taxable_transactions": len(taxable),
                "exempt_crypto_transactions": len(exempt),
                "total_gross_proceeds": sum(m.gross_disposal_proceeds for m in group),
                "total_adjusted_cost": sum(
                    m.adjusted_acquisition_cost for m in taxable
                ),
                "total_nominal_cost": sum(
                    getattr(m, "nominal_acquisition_cost", m.adjusted_acquisition_cost)
                    for m in taxable
                ),
                "total_deductible_expenses": sum(
                    getattr(
                        m,
                        "deductible_expenses",
                        m.acquisition_fees_allocated + m.disposal_fees_allocated,
                    )
                    for m in taxable
                ),
                "capital_gains": gains,
                "capital_losses": losses,
                "net_taxable_balance": net_balance,
                "taxable_base_after_inclusion": taxable_base,
                "estimated_tax_autonomous_28pct": est_tax,
                "exempt_crypto_proceeds": exempt_proceeds,
                "exempt_crypto_gains": exempt_gains,
                "matches": group,
            }

        total_net_raw = sum(
            m.net_gain_loss for m in year_matches if not m.is_exempt_crypto
        )
        # BUG-TAX-01 (CIRS Art. 10.?): mais-valias e menos-valias do mesmo ano
        # compensam-se (net) antes de qualquer tributacao; apenas o net
        # positivo e tributavel.
        total_gains_raw = sum(
            m.net_gain_loss
            for m in year_matches
            if not m.is_exempt_crypto and m.net_gain_loss > 0
        )
        total_losses_raw = sum(
            -m.net_gain_loss
            for m in year_matches
            if not m.is_exempt_crypto and m.net_gain_loss < 0
        )
        prior_losses = max(0.0, safe_float(prior_year_losses))
        # BUG-TAX-05 (CIRS Art. 51.?): perdas reportadas caducam 5 anos apos o
        # ano da menos-valia. Com buckets por ano de origem, apenas as perdas
        # de anos dentro da janela sao elegiveis para compensacao.
        if prior_year_losses_by_year is not None:
            eligible_buckets = {
                year: max(0.0, safe_float(value))
                for year, value in prior_year_losses_by_year.items()
                if fiscal_year - int(year) <= 5 and safe_float(value) > 0.0
            }
            eligible_prior_losses = sum(eligible_buckets.values())
            expired_prior_losses = max(
                0.0, prior_losses - eligible_prior_losses
            )
        else:
            eligible_prior_losses = prior_losses
            expired_prior_losses = 0.0
        if opt_for_englobamento:
            eligible_prior_loss_used = min(
                eligible_prior_losses, max(0.0, total_net_raw)
            )
            net_after_loss_carryforward = (
                max(0.0, total_net_raw - eligible_prior_loss_used)
                if total_net_raw > 0
                else total_net_raw
            )
        else:
            eligible_prior_loss_used = 0.0
            net_after_loss_carryforward = total_net_raw

        final_taxable_base = net_after_loss_carryforward
        if apply_50pct_inclusion and final_taxable_base > 0:
            final_taxable_base = final_taxable_base * 0.50

        anexo_g_summary = summarize_group(anexo_g_matches)
        anexo_j_summary = summarize_group(anexo_j_matches)

        if apply_50pct_inclusion:
            total_tax = 0.0
            total_tax_autonomous = 0.0
        elif opt_for_englobamento:
            # Reference baseline 28% estimate uses post-deduction taxable base (Art. 55.º CIRS)
            total_tax = max(0.0, round(final_taxable_base * (TAXA_LIBERATORIA_PADRAO / 100.0), 2))
            total_tax_autonomous = 0.0
        else:
            # BUG-R7-TAX-01: CIRS Art. 43.º n.º 1 - net capital gains against capital losses
            # globally across Anexo G and Anexo J under the autonomous regime.
            if total_net_raw <= 0:
                total_tax = 0.0
            else:
                if total_gains_raw > 0:
                    global_weighted_rate = (
                        sum(
                            m.net_gain_loss * (m.tax_rate_pct / 100.0)
                            for m in year_matches
                            if not m.is_exempt_crypto and m.net_gain_loss > 0
                        )
                        / total_gains_raw
                    )
                else:
                    global_weighted_rate = TAXA_LIBERATORIA_PADRAO / 100.0
                total_tax = round(total_net_raw * global_weighted_rate, 2)
            total_tax_autonomous = total_tax

        # BUG-AUD-C104 (CIRS Art. 55.º n.º 1 al. d)): loss carryforward to
        # subsequent years is only deductible when the taxpayer opts for
        # englobamento. Under the autonomous 28% rate the year's losses are
        # NOT reportable forward (they lapse) and prior carryforward buckets
        # remain frozen (never usable, never expanded).
        if opt_for_englobamento:
            new_losses_generated = abs(total_net_raw) if total_net_raw < 0 else 0.0
            # BUG-TAX-05: remaining carryforward keeps only non-expired prior
            # losses; expired buckets are dropped (Art. 51.? CIRS, 5-year window).
            remaining_prior = max(
                0.0, eligible_prior_losses - eligible_prior_loss_used
            )
            total_losses_to_carry = remaining_prior + new_losses_generated
        else:
            new_losses_generated = 0.0
            remaining_prior = max(
                0.0, eligible_prior_losses - eligible_prior_loss_used
            )
            total_losses_to_carry = remaining_prior

        return {
            "fiscal_year": fiscal_year,
            "total_matches_count": len(year_matches),
            "global_net_capital_gain_loss_raw": total_net_raw,
            "prior_year_losses_deducted": eligible_prior_loss_used,
            "remaining_prior_year_losses": remaining_prior,
            "current_year_losses_generated": new_losses_generated,
            "total_losses_to_carryforward": total_losses_to_carry,
            "global_net_capital_gain_loss": net_after_loss_carryforward,
            "opt_for_englobamento": opt_for_englobamento,
            "final_taxable_base": final_taxable_base,
            "global_estimated_tax_28pct": total_tax,
            "total_tax_autonomous": total_tax_autonomous,
            # BUG-TAX-01: gross gains/losses of the year before netting.
            "total_gains_before_netting": total_gains_raw,
            "total_losses_before_netting": total_losses_raw,
            # BUG-TAX-05: expired prior losses (older than 5 years, Art. 51.?).
            "expired_prior_year_losses": expired_prior_losses,
            "anexo_g": anexo_g_summary,
            "anexo_j": anexo_j_summary,
            "legal_notes": [
                "CIRS Art. 10.º n.º 1 al. k) e n.º 19: Criptoativos detidos há 365 dias ou mais com aquisição comprovada estão isentos de tributação.",
                "CIRS Art. 50.º: Coeficientes de desvalorização da moeda aplicados exclusivamente a bens imóveis detidos há mais de 24 meses.",
                "CIRS Art. 72.º n.º 1: Taxa liberatória autónoma de 28% com faculdade de opção pelo englobamento.",
                "CIRS Art. 72.º n.º 14: Taxa agravada de 35% aplicada a rendimentos auferidos em entidades com domicílio em paraíso fiscal (Portaria n.º 150/2004).",
                "CIRS Art. 55.º n.º 1 al. d): O reporte de perdas de anos anteriores só é dedutível se o sujeito passivo optar pelo englobamento. Na taxa autónoma de 28%, perdas passadas não são abatidas.",
                "CIRS Art. 43.º n.º 2: A regra de inclusão a 50% obriga a englobamento nas taxas gerais progressivas do IRS (Art. 68.º), não sendo legalmente admissível a liquidação à taxa autónoma de 28%.",
            ],
        }


# ==============================================================================
# CATEGORIA E — RENDIMENTOS DE CAPITAIS & CRÉDITO DE IMPOSTO (ART. 81.º CIRS)
# ==============================================================================

# Official Double Taxation Conventions (CDT) maximum dividend withholding rates in source state
CONVENTION_DIVIDEND_TAX_RATES: dict[str, float] = {
    "US": 15.0,  # CDT Portugal - EUA (Artigo 10.º n.º 2 b) mediante W-8BEN)
    "CA": 15.0,  # CDT Portugal - Canadá
    "GB": 15.0,  # CDT Portugal - Reino Unido
    "DE": 15.0,  # CDT Portugal - Alemanha
    "FR": 15.0,  # CDT Portugal - França
    "ES": 15.0,  # CDT Portugal - Espanha
    "NL": 10.0,  # CDT Portugal - Países Baixos
    "LU": 15.0,  # CDT Portugal - Luxemburgo (Artigo 10.º n.º 2) - BUG-AUD-C51
    "CH": 15.0,  # CDT Portugal - Suíça
    "IE": 15.0,  # CDT Portugal - Irlanda
    "BE": 15.0,  # CDT Portugal - Bélgica
    "IT": 15.0,  # CDT Portugal - Itália
    "AT": 15.0,  # CDT Portugal - Áustria
    "DK": 10.0,  # CDT Portugal - Dinamarca
    "SE": 10.0,  # CDT Portugal - Suécia
    "NO": 15.0,  # CDT Portugal - Noruega
    "JP": 10.0,  # CDT Portugal - Japão
    "BR": 15.0,  # CDT Portugal - Brasil
}


@dataclass(slots=True, frozen=True)
class ForeignDividendPayment:
    """Foreign capital income / dividend payment (Categoria E)."""

    id: str
    asset_symbol: str
    payment_date: date
    gross_amount_eur: float
    foreign_tax_withheld_eur: float
    country_code: str
    has_treaty_form: bool = True
    domestic_tax_withheld_eur: float = 0.0


@dataclass(slots=True)
class TaxCreditCalculation:
    """International double taxation credit calculation under Artigo 81.º do CIRS."""

    gross_dividend: float
    foreign_tax_withheld: float
    treaty_rate_pct: float
    maximum_creditable_foreign_tax: float
    uncreditable_foreign_tax_loss: float
    portuguese_autonomous_tax_28pct: float
    tax_credit_applied_art81: float
    net_portuguese_tax_payable: float
    effective_total_tax_rate_pct: float
    irs_annex: str = "Anexo J (Quadro 8)"
    domestic_tax_withheld: float = 0.0
    tax_refund_due: float = 0.0


def calculate_foreign_dividend_tax(
    dividend: ForeignDividendPayment,
    *,
    domestic_tax_withheld: float = 0.0,
) -> TaxCreditCalculation:
    """Calculate Portuguese tax and foreign tax credit (Art. 81.º CIRS) for foreign dividends.

    Under CIRS Art. 81.º, the foreign tax credit is capped at the lower of:
      1. Foreign tax actually withheld;
      2. Portuguese autonomous tax fraction (28%);
      3. Double Taxation Convention (CDT) maximum source rate (e.g. 15% under US W-8BEN).
    Any tax withheld in excess of the treaty cap (e.g. 30% US statutory rate without W-8BEN)
    cannot be credited against Portuguese tax and represents irreversible fiscal leakage.

    ``domestic_tax_withheld`` (BUG-AUD-C105): withholding already retained in
    Portugal by a national intermediary (e.g. CGD/ActivoBank/Millennium
    retaining the 28% liberatória at source on foreign dividends). It is
    deducted from the Portuguese tax due on top of the Art. 81.º foreign
    credit, so the taxpayer is never charged twice for the same income.

    Jurisdictions on the Portuguese tax haven blacklist (OFFSHORE_JURISDICTIONS / Portaria 150/2004)
    are taxed at 35% (Art. 72.º n.º 14) and are ineligible for any foreign tax credit (Art. 81.º n.º 6).
    """
    gross = max(0.0, safe_float(dividend.gross_amount_eur))
    foreign_withheld = max(0.0, safe_float(dividend.foreign_tax_withheld_eur))
    domestic_withheld = max(0.0, safe_float(domestic_tax_withheld))
    country = str(dividend.country_code or "PT").strip().upper()[:2]
    is_offshore = country in OFFSHORE_JURISDICTIONS
    is_domestic = country == "PT"

    if is_offshore:
        # CIRS Art. 72.º n.º 14 (35%) & Art. 81.º n.º 6 (no credit for blacklisted havens)
        treaty_rate = 0.0
        pt_rate = TAXA_AGRAVADA_OFFSHORE
        creditable_foreign_tax = 0.0
        uncreditable_loss = foreign_withheld
    elif is_domestic:
        treaty_rate = 0.0
        pt_rate = TAXA_LIBERATORIA_PADRAO
        creditable_foreign_tax = 0.0
        uncreditable_loss = 0.0
    else:
        # BUG-TAX-04: no hallucinated 15% default for countries without a
        # published CDT rate. The effective cap becomes the actual
        # source-state withholding rate (best available evidence), capped at
        # the Portuguese fraction (Art. 81.? CIRS). ``has_treaty_form``
        # documents that a relief form exists, but it cannot conjure a
        # convention rate the engine does not know; known CDT rates in
        # CONVENTION_DIVIDEND_TAX_RATES always take precedence.
        pt_rate = TAXA_LIBERATORIA_PADRAO
        treaty_rate = CONVENTION_DIVIDEND_TAX_RATES.get(country)
        if treaty_rate is None:
            statutory_rate = (
                (foreign_withheld / gross * 100.0) if gross > 0 else 0.0
            )
            treaty_rate = min(statutory_rate, TAXA_LIBERATORIA_PADRAO)
        max_treaty_tax = gross * (treaty_rate / 100.0)
        creditable_foreign_tax = min(foreign_withheld, max_treaty_tax)
        uncreditable_loss = max(0.0, foreign_withheld - creditable_foreign_tax)

    pt_gross_tax = gross * (pt_rate / 100.0)
    tax_credit = min(creditable_foreign_tax, pt_gross_tax)
    # BUG-AUD-C105 & BUG-R6-TAX-04: national withholding at source (retained by a Portuguese
    # intermediary) also settles the Portuguese liability, after the foreign
    # Art. 81.º credit. If domestic withholding exceeds remaining liability, a refund is due.
    liability_after_credit = pt_gross_tax - tax_credit
    raw_balance = round(liability_after_credit - domestic_withheld, 2)
    net_pt_payable = max(0.0, raw_balance)
    tax_refund_due = max(0.0, -raw_balance)
    total_tax_paid = round(foreign_withheld + domestic_withheld + net_pt_payable - tax_refund_due, 2)
    eff_rate = (total_tax_paid / gross * 100.0) if gross > 0 else 0.0

    return TaxCreditCalculation(
        gross_dividend=round(gross, 2),
        foreign_tax_withheld=round(foreign_withheld, 2),
        treaty_rate_pct=treaty_rate,
        maximum_creditable_foreign_tax=round(creditable_foreign_tax, 2),
        uncreditable_foreign_tax_loss=round(uncreditable_loss, 2),
        portuguese_autonomous_tax_28pct=round(pt_gross_tax, 2),
        tax_credit_applied_art81=round(tax_credit, 2),
        net_portuguese_tax_payable=round(net_pt_payable, 2),
        effective_total_tax_rate_pct=round(eff_rate, 2),
        irs_annex="Anexo J (Quadro 8)" if not is_domestic else "Anexo E / Retenção na Fonte",
        domestic_tax_withheld=round(domestic_withheld, 2),
        tax_refund_due=round(tax_refund_due, 2),
    )


def summarize_foreign_dividends(
    dividends: Sequence[ForeignDividendPayment],
    *,
    fiscal_year: int | None = None,
) -> dict[str, Any]:
    """Aggregate foreign dividend taxation and tax credits for Modelo 3 Anexo J Quadro 8."""
    filtered = [
        d
        for d in dividends
        if fiscal_year is None or d.payment_date.year == fiscal_year
    ]
    calculations = [
        calculate_foreign_dividend_tax(
            d, domestic_tax_withheld=getattr(d, "domestic_tax_withheld_eur", 0.0)
        )
        for d in filtered
    ]
    total_gross = sum(c.gross_dividend for c in calculations)
    total_withheld = sum(c.foreign_tax_withheld for c in calculations)
    total_domestic_withheld = sum(c.domestic_tax_withheld for c in calculations)
    total_max_creditable = sum(c.maximum_creditable_foreign_tax for c in calculations)
    total_uncreditable_loss = sum(c.uncreditable_foreign_tax_loss for c in calculations)
    total_pt_tax = sum(c.portuguese_autonomous_tax_28pct for c in calculations)
    total_tax_credit = sum(c.tax_credit_applied_art81 for c in calculations)
    total_net_pt_payable = sum(c.net_portuguese_tax_payable for c in calculations)
    total_tax_refund_due = sum(c.tax_refund_due for c in calculations)
    total_tax_overall = total_withheld + total_domestic_withheld + total_net_pt_payable - total_tax_refund_due
    eff_rate = (total_tax_overall / total_gross * 100.0) if total_gross > 0 else 0.0

    by_country: dict[str, dict[str, float]] = {}
    for d, c in zip(filtered, calculations, strict=True):
        country = str(d.country_code or "PT").strip().upper()[:2]
        entry = by_country.setdefault(
            country,
            {
                "gross": 0.0,
                "foreign_tax": 0.0,
                "domestic_tax": 0.0,
                "tax_credit": 0.0,
                "net_pt_tax": 0.0,
                "tax_refund_due": 0.0,
                "uncreditable_loss": 0.0,
            },
        )
        entry["gross"] += c.gross_dividend
        entry["foreign_tax"] += c.foreign_tax_withheld
        entry["domestic_tax"] += c.domestic_tax_withheld
        entry["tax_credit"] += c.tax_credit_applied_art81
        entry["net_pt_tax"] += c.net_portuguese_tax_payable
        entry["tax_refund_due"] += c.tax_refund_due
        entry["uncreditable_loss"] += c.uncreditable_foreign_tax_loss

    return {
        "fiscal_year": fiscal_year,
        "total_dividends_count": len(filtered),
        "total_gross_dividend": round(total_gross, 2),
        "total_foreign_tax_withheld": round(total_withheld, 2),
        "total_domestic_tax_withheld": round(total_domestic_withheld, 2),
        "total_max_creditable_tax": round(total_max_creditable, 2),
        "total_uncreditable_loss": round(total_uncreditable_loss, 2),
        "total_portuguese_tax_28pct": round(total_pt_tax, 2),
        "total_tax_credit_art81": round(total_tax_credit, 2),
        "total_net_portuguese_tax_payable": round(total_net_pt_payable, 2),
        "total_tax_refund_due": round(total_tax_refund_due, 2),
        "effective_total_tax_rate_pct": round(eff_rate, 2),
        "by_country": {
            k: {sk: round(sv, 2) for sk, sv in v.items()}
            for k, v in by_country.items()
        },
        "anexo_j_quadro_8": [
            {
                "country_code": k,
                "income_type": "E20 (Dividendos)",
                "gross_amount": round(v["gross"], 2),
                "foreign_tax_paid": round(v["foreign_tax"], 2),
                "domestic_tax_paid": round(v.get("domestic_tax", 0.0), 2),
                "tax_credit_art81": round(v["tax_credit"], 2),
                "net_portuguese_tax": round(v["net_pt_tax"], 2),
                "tax_refund_due": round(v.get("tax_refund_due", 0.0), 2),
            }
            for k, v in by_country.items()
        ],
        "legal_notes": [
            "CIRS Art. 81.º n.º 1: Crédito de imposto por dupla tributação jurídica internacional dedutível até à concorrência da fração do IRS (28%) ou da taxa fixada na Convenção de Dupla Tributação (CDT).",
            "CIRS Art. 72.º n.º 1: Taxa especial/liberatória de 28% para rendimentos de capitais (Categoria E).",
            "Convenção Portugal - EUA (Art. 10.º): Taxa máxima na fonte de 15% mediante W-8BEN. Retenção estatutária de 30% gera 15% de perda fiscal não recuperável via crédito de imposto.",
        ],
    }


__all__ = [
    "COEFICIENTES_DESVALORIZACAO",
    "TAXA_LIBERATORIA_PADRAO",
    "TAXA_AGRAVADA_OFFSHORE",
    "OFFSHORE_JURISDICTIONS",
    "CONVENTION_DIVIDEND_TAX_RATES",
    "months_between",
    "is_crypto_to_crypto_swap",
    "AcquisitionLot",
    "DisposalTransaction",
    "ForeignDividendPayment",
    "PortugueseTaxEngine",
    "TaxCreditCalculation",
    "TaxLotMatch",
    "calculate_foreign_dividend_tax",
    "summarize_foreign_dividends",
]
