"""Current portfolio return and attribution calculations.

The cost basis remains the capital invested in the asset.  Recorded fees are
kept separately so gross and net performance can both be explained without
rewriting existing holdings or historical snapshots.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from typing import Any

from utils.helpers import row_get, safe_float


def portfolio_performance_attribution(
    holdings: Iterable[Any],
) -> dict[str, Any]:
    """Return weighted net performance and each asset's contribution.

    Contribution is expressed in percentage points of the whole portfolio:
    ``asset net gain / total cash committed * 100``.  Consequently, the sum of
    all asset contributions equals the portfolio's weighted net return.
    """

    source_rows = list(holdings or [])
    for source in source_rows:
        for key in ("value", "cost_basis", "fees", "exchange_rate", "historical_exchange_rate", "acquisition_exchange_rate", "buy_exchange_rate", "historical_rate"):
            value = row_get(source, key, None)
            if value is not None:
                try:
                    finite = math.isfinite(float(value))
                except (ValueError, TypeError, OverflowError):
                    finite = False
                if not finite:
                    raise ValueError(f"O campo {key} exige um valor numérico finito.")

    def _rate(row: Any) -> float:
        r = safe_float(row_get(row, "exchange_rate", 1.0) or 1.0)
        return r if r > 0 else 1.0

    def _historical_rate(row: Any) -> float | None:
        if str(row_get(row, "currency", "EUR") or "EUR").upper() == "EUR":
            return 1.0
        # BUG-AUD-C98: Preservar mais/menos-valias cambiais sob CIRS Art. 43.º
        # convertendo custos e encargos históricos ao câmbio da aquisição.
        for key in (
            "historical_exchange_rate",
            "acquisition_exchange_rate",
            "buy_exchange_rate",
            "historical_rate",
        ):
            hr = row_get(row, key, None)
            if hr is not None and safe_float(hr) > 0:
                return safe_float(hr)
        meta = row_get(row, "metadata", None)
        if isinstance(meta, str):
            try:
                import json

                meta = json.loads(meta)
            except Exception:
                meta = None
        if isinstance(meta, dict):
            for key in (
                "historical_exchange_rate",
                "acquisition_exchange_rate",
                "buy_exchange_rate",
                "historical_rate",
            ):
                hr = meta.get(key)
                if hr is not None:
                    try:
                        historical = float(hr)
                    except (TypeError, ValueError, OverflowError) as exc:
                        raise ValueError(f"O campo {key} exige um valor numérico finito.") from exc
                    if not math.isfinite(historical):
                        raise ValueError(f"O campo {key} exige um valor numérico finito.")
                    if historical > 0:
                        return historical
        return None

    total_value = sum(
        max(0.0, safe_float(row_get(row, "value"))) * _rate(row) for row in source_rows
    )
    known_rows = [
        row
        for row in source_rows
        if bool(
            int(
                row_get(
                    row,
                    "cost_basis_known",
                    1 if safe_float(row_get(row, "cost_basis")) > 0 else 0,
                )
                or 0
            )
        )
        and safe_float(row_get(row, "cost_basis")) > 0
        and _historical_rate(row) is not None
    ]
    known_value = sum(
        max(0.0, safe_float(row_get(row, "value"))) * _rate(row) for row in known_rows
    )
    total_cost_basis = sum(
        max(0.0, safe_float(row_get(row, "cost_basis"))) * (_historical_rate(row) or 0)
        for row in known_rows
    )
    total_fees = sum(
        max(0.0, safe_float(row_get(row, "fees"))) * (_historical_rate(row) or 0) for row in source_rows
    )
    known_fees = sum(
        max(0.0, safe_float(row_get(row, "fees"))) * (_historical_rate(row) or 0) for row in known_rows
    )
    total_cash_invested = total_cost_basis + known_fees
    gross_gain = (known_value - total_cost_basis) if known_rows else 0.0
    net_gain = (known_value - total_cash_invested) if known_rows else 0.0
    gross_return_pct = (
        gross_gain / total_cost_basis * 100.0 if total_cost_basis > 0 else None
    )
    net_return_pct = (
        net_gain / total_cash_invested * 100.0 if total_cash_invested > 0 else None
    )
    fee_impact_pp = (
        gross_return_pct - net_return_pct
        if gross_return_pct is not None and net_return_pct is not None
        else 0.0
    )
    fees_pct_of_cash = (
        known_fees / total_cash_invested * 100.0 if total_cash_invested else None
    )
    fees_pct_of_cost = (
        known_fees / total_cost_basis * 100.0 if total_cost_basis else None
    )

    rows: list[dict[str, Any]] = []
    for source in source_rows:
        rate = _rate(source)
        hist_rate = _historical_rate(source)
        native_val = max(0.0, safe_float(row_get(source, "value")))
        native_cost = max(0.0, safe_float(row_get(source, "cost_basis")))
        native_fees = max(0.0, safe_float(row_get(source, "fees")))
        value = native_val * rate
        cost_basis = native_cost * hist_rate if hist_rate is not None else None
        fees = native_fees * hist_rate if hist_rate is not None else None
        cost_basis_known = bool(
            int(
                row_get(
                    source,
                    "cost_basis_known",
                    1 if native_cost > 0 else 0,
                )
                or 0
            )
        )
        performance_available = cost_basis_known and cost_basis is not None and cost_basis > 0
        cash_invested = cost_basis + fees if cost_basis is not None else None
        asset_gross_gain = value - cost_basis if performance_available else None
        asset_net_gain = value - cash_invested if performance_available else None
        cost_weight_pct = (
            cash_invested / total_cash_invested * 100.0
            if performance_available and total_cash_invested > 0
            else None
        )
        market_weight_pct = (value / total_value * 100.0) if total_value > 0 else 0.0
        portfolio_weight_pct = market_weight_pct

        gross_asset_return = (
            safe_float(asset_gross_gain) / cost_basis * 100.0
            if performance_available and cost_basis > 0
            else None
        )
        net_asset_return = (
            safe_float(asset_net_gain) / cash_invested * 100.0
            if performance_available and cash_invested > 0
            else None
        )
        contribution_pp = (
            safe_float(asset_net_gain) / total_cash_invested * 100.0
            if performance_available and total_cash_invested > 0
            else None
        )

        rows.append(
            {
                "id": int(row_get(source, "id") or 0),
                "name": str(
                    row_get(source, "name")
                    or row_get(source, "asset_class")
                    or "Investimento"
                ),
                "asset_class": str(row_get(source, "asset_class") or "Outro"),
                "account": str(row_get(source, "account") or "Manual"),
                "current_value": value,
                "cost_basis": cost_basis,
                "cost_basis_known": cost_basis_known,
                "performance_available": performance_available,
                "fees": fees,
                "cash_invested": cash_invested,
                "gross_gain": asset_gross_gain,
                "net_gain": asset_net_gain,
                "gross_return_pct": gross_asset_return,
                "net_return_pct": net_asset_return,
                "contribution_pp": contribution_pp,
                "portfolio_weight_pct": portfolio_weight_pct,
                "market_weight_pct": market_weight_pct,
                "cost_weight_pct": cost_weight_pct,
                "native_value": native_val,
                "native_cost_basis": native_cost,
                "native_fees": native_fees,
                "currency": str(row_get(source, "currency") or "EUR").strip().upper() or "EUR",
                "exchange_rate": rate,
                "historical_exchange_rate": hist_rate,
                "historical_exchange_rate_known": hist_rate is not None,
                "weight_pct": portfolio_weight_pct,
            }
        )

    rows.sort(
        key=lambda row: (
            0 if row.get("performance_available") else 1,
            -abs(safe_float(row.get("contribution_pp"))),
            str(row.get("name") or "").casefold(),
        )
    )
    return {
        "rows": rows,
        "holding_count": len(rows),
        "holding_count_known": len(known_rows),
        "total_value": total_value,
        "known_value": known_value,
        "data_coverage_pct": (
            known_value / total_value * 100.0 if total_value else 100.0
        ),
        "total_cost_basis": total_cost_basis,
        "total_fees": total_fees,
        "fees_conversion_complete": all(_historical_rate(row) is not None for row in source_rows),
        "known_total_fees": known_fees,
        "total_cash_invested": total_cash_invested,
        "gross_gain": gross_gain,
        "net_gain": net_gain,
        "gross_return_pct": gross_return_pct,
        "net_return_pct": net_return_pct,
        "weighted_return_pct": net_return_pct,
        "fee_impact_pp": fee_impact_pp,
        "return_gap_pp": fee_impact_pp if net_return_pct is not None else None,
        "return_gap_definition": "gross_return_on_cost_minus_net_return_on_cash_invested",
        "fees_pct_of_cash": fees_pct_of_cash,
        "fees_pct_of_cost": fees_pct_of_cost,
        "fee_metrics_available": total_cost_basis > 0,
        "fee_metrics_basis": {
            "fees_pct_of_cost": "known_acquisition_cost",
            "fees_pct_of_cash": "known_acquisition_cost_plus_fees",
            "fee_impact_pp": "compatibility_alias_for_return_gap_pp",
        },
        "contribution_total_pp": math.fsum(
            safe_float(row.get("contribution_pp")) for row in rows
        ),
        "method_label": (
            "Rentabilidade líquida ponderada pelo capital efetivamente "
            "comprometido (capital investido + comissões). Ativos sem custo de "
            "aquisição ou câmbio histórico conhecido ficam fora da percentagem."
        ),
    }


def calculate_holding_xirr(
    acquisition_date: Any,
    cost_basis: float,
    current_date: Any,
    current_value: float,
    *,
    dividends: list[tuple[Any, float]] | None = None,
    fees: float = 0.0,
) -> float | None:
    """Calculate the Money-Weighted Return (XIRR) in percentage points for an individual asset."""
    from services.finance_math import _coerce_date, xirr_pct

    cost = safe_float(cost_basis) + safe_float(fees)
    val = safe_float(current_value)
    if cost <= 0 or val < 0:
        return None

    # Total loss / liquidated holding handling (BUG-AUD-C86 & AUD-MATH-02)
    total_divs = sum(max(0.0, safe_float(div_amt)) for _, div_amt in (dividends or []))
    if val == 0.0:
        if total_divs <= 0:
            return -100.0
        # BUG-AUD-C86: If dividends or positive cash flows were received, calculate net return
        # rather than falsely reporting a total loss (-100%).
        if cost > 0:
            return ((total_divs - cost) / cost) * 100.0

    # BUG-AUD-C85: GIPS standard - gate explosive annualization for short holding periods (< 30 days)
    # by reporting nominal cumulative return instead of compounding over 365/days.
    d_start = _coerce_date(acquisition_date)
    d_end = _coerce_date(current_date)
    if d_start is not None and d_end is not None:
        holding_days = (d_end - d_start).days
        if holding_days < 0:
            return None
        if holding_days < 30:
            return ((val + total_divs - cost) / cost) * 100.0

    flows: list[tuple[Any, float]] = [(acquisition_date, -cost)]
    if dividends:
        for div_d, div_amt in dividends:
            if safe_float(div_amt) > 0:
                flows.append((div_d, safe_float(div_amt)))
    flows.append((current_date, val))

    return xirr_pct(flows)


def calculate_portfolio_xirr(
    cash_flows: Iterable[tuple[Any, float]],
    current_date: Any,
    current_portfolio_value: float,
) -> float | None:
    """Calculate the Money-Weighted Return (XIRR) in percentage points for the entire portfolio."""
    from services.finance_math import _coerce_date, xirr_pct

    flows: list[tuple[Any, float]] = list(cash_flows)
    val = max(0.0, safe_float(current_portfolio_value))
    # MATH-02: a fully liquidated portfolio (value 0) must still register its
    # terminal date as an explicit (zero) cash flow so the XIRR horizon ends at
    # the liquidation date instead of the last dividend.
    flows.append((current_date, val))

    has_outflows = any(safe_float(amt) < 0 for _, amt in flows)
    has_inflows = any(safe_float(amt) > 0 for _, amt in flows)
    if val == 0.0 and has_outflows:
        if not has_inflows:
            return -100.0
        # BUG-AUD-C86: Liquidated portfolio with past inflows calculates net return instead of -100%
        total_outflows = sum(-safe_float(amt) for _, amt in flows if safe_float(amt) < 0)
        total_inflows = sum(safe_float(amt) for _, amt in flows if safe_float(amt) > 0)
        if total_outflows > 0:
            return ((total_inflows - total_outflows) / total_outflows) * 100.0

    # BUG-AUD-C85: GIPS standard - gate explosive annualization for short portfolio horizons (< 30 days)
    # by reporting nominal cumulative return.
    parsed_dates = [_coerce_date(d) for d, _ in flows]
    valid_dates = [d for d in parsed_dates if d is not None]
    if valid_dates:
        p_days = (max(valid_dates) - min(valid_dates)).days
        if 0 <= p_days < 30:
            total_outflows = sum(-safe_float(amt) for _, amt in flows if safe_float(amt) < 0)
            total_inflows = sum(safe_float(amt) for _, amt in flows if safe_float(amt) > 0)
            if total_outflows > 0:
                return ((total_inflows - total_outflows) / total_outflows) * 100.0

    return xirr_pct(flows)


__all__ = [
    "calculate_holding_xirr",
    "calculate_portfolio_xirr",
    "portfolio_performance_attribution",
]
