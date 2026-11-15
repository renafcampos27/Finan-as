"""Shared financial-rate conventions used by every projection engine."""

from __future__ import annotations

import math

from utils.helpers import safe_float


from datetime import date, datetime
from typing import Any, Optional, Sequence, Union


def _coerce_date(raw_d: Any) -> Optional[date]:
    """Safely convert date, datetime, or ISO string to a datetime.date object."""
    if isinstance(raw_d, datetime):
        return raw_d.date()
    if isinstance(raw_d, date):
        return raw_d
    if isinstance(raw_d, str):
        try:
            return date.fromisoformat(str(raw_d)[:10])
        except (ValueError, TypeError):
            return None
    return None


def effective_annual_to_periodic_rate(
    annual_rate_pct: float,
    periods_per_year: int = 12,
) -> float:
    """Convert an effective annual percentage to one compounded period.

    A 6% annual rate therefore compounds to exactly 6% after twelve monthly
    periods, instead of the 6.17% produced by the ``6 / 12`` approximation.
    """

    periods = max(1, int(periods_per_year or 1))
    annual = max(-0.99999999, safe_float(annual_rate_pct) / 100.0)
    if abs(annual) < 1e-15:
        return 0.0
    try:
        val = math.expm1(math.log1p(annual) / periods)
        if math.isinf(val) or math.isnan(val):
            return 1e12 if annual > 0 else -1.0
        return val
    except (OverflowError, ValueError):
        return 1e12 if annual > 0 else -1.0


def periodic_discount_factor(
    annual_rate_pct: float,
    period_index: int,
    *,
    periods_per_year: int = 12,
) -> float:
    """Return the effective periodic capitalization factor (1 + r)^n.

    BUG-AUD-C107 naming clarification: this function computes the forward
    compounding (accumulation) factor for ``period_index`` periods at the
    effective periodic rate, NOT the present-value discount factor
    (1 + r)^-n. All in-repo callers discount by dividing through it
    (``value / factor``), so the mathematics is correct; only the historical
    name is misleading. It is kept for API stability and pinned by
    ``test_e2e_requirements.test_periodic_discount_factor_annuity_pv``
    (df(5%, 12) == 1.05). Use ``1.0 / periodic_discount_factor(...)`` when a
    strict present-value discount factor is required.
    """

    periodic = effective_annual_to_periodic_rate(
        annual_rate_pct,
        periods_per_year,
    )
    try:
        safe_periods = max(0, min(1200, int(period_index)))
        base = max(1e-12, 1.0 + periodic)
        val = math.pow(base, safe_periods)
        if math.isinf(val) or math.isnan(val) or val > 1e12:
            return 1e12 if (1.0 + periodic) > 1.0 else 0.0
        return val
    except (OverflowError, ValueError):
        return 1e12 if (1.0 + periodic) > 1.0 else 0.0


def periodic_pv_discount_factor(
    annual_rate_pct: float,
    period_index: int,
    *,
    periods_per_year: int = 12,
) -> float:
    """Strict present-value discount factor: (1 + r)^-n (BUG-AUD-C107 alias).

    Companion to :func:`periodic_discount_factor` (which returns the
    capitalization factor (1+r)^n). Use this when a routine needs to bring a
    future flow back to today's euros without dividing manually.
    """
    factor = periodic_discount_factor(
        annual_rate_pct,
        period_index,
        periods_per_year=periods_per_year,
    )
    if factor <= 1e-12:
        return 0.0
    return 1.0 / factor


def present_value_of_contributions(
    amount: float,
    periods: int,
    annual_inflation_pct: float,
    *,
    periods_per_year: int = 12,
) -> float:
    """Discount each end-of-period contribution to today's euros using closed-form annuity math."""

    contribution = max(0.0, safe_float(amount))
    count = max(0, min(1200, int(periods or 0)))
    if contribution <= 0 or count <= 0:
        return 0.0
    r = effective_annual_to_periodic_rate(annual_inflation_pct, periods_per_year)
    if abs(r) < 1e-12:
        return contribution * count
    try:
        discount_factor = -math.expm1(-count * math.log1p(r))
        res = contribution * discount_factor / r
        if math.isinf(res) or math.isnan(res) or res < 0.0:
            return contribution * count
        return res
    except (OverflowError, ValueError, ZeroDivisionError):
        return contribution * count


def cagr(start_value: float, end_value: float, years: float) -> float:
    """Calculate Compound Annual Growth Rate (CAGR) safely in percentage points."""
    start = safe_float(start_value)
    end = safe_float(end_value)
    duration = safe_float(years)
    if start <= 0.0 or duration <= 0.0:
        return 0.0
    if end <= 0.0:
        return -100.0
    try:
        return (math.pow(end / start, 1.0 / duration) - 1.0) * 100.0
    except (ValueError, OverflowError):
        return 0.0


def twrr(
    period_returns_pct: Sequence[float],
    *,
    allow_recapitalization: bool = False,
) -> float:
    """Calculate Time-Weighted Rate of Return (TWRR) over consecutive sub-periods.

    Returns the geometric linking of sub-period returns in percentage points.
    By default (allow_recapitalization=False), uninterrupted geometric linking
    is enforced: a total portfolio loss (-100%) zeroes capital and retains a -100%
    cumulative return across subsequent sub-periods. If allow_recapitalization=True,
    a post-liquidation cash injection/recapitalization baseline restart is permitted,
    but an account that suffered a total (100%) loss cannot report a positive cumulative
    rate after subsequent fresh deposits (BUG-AUD-C97).
    """
    compound = 1.0
    had_total_loss = False
    for rate_pct in period_returns_pct:
        r = safe_float(rate_pct) / 100.0
        factor = max(0.0, 1.0 + r)
        if compound <= 1e-12:
            had_total_loss = True
            if allow_recapitalization:
                # AUD-MATH-03 / BUG-AUD-C97: Explicit post-liquidation recapitalization restarts compounding baseline
                compound = factor
            else:
                compound = 0.0
        else:
            compound *= factor
            if compound <= 1e-12:
                had_total_loss = True
        if math.isinf(compound) or compound > 1e12:
            compound = 1e12
            break

    result = (compound - 1.0) * 100.0
    # BUG-AUD-C97: An account that suffered a total (-100%) loss cannot report a positive cumulative rate
    if had_total_loss and allow_recapitalization and result > 0.0:
        return 0.0
    return result


def modified_dietz(
    start_value: float,
    end_value: float,
    cash_flows: Sequence[tuple[Union[date, str], float]],
    start_date: Union[date, str],
    end_date: Union[date, str],
) -> float:
    """Calculate Modified Dietz return rate weighted by the timing of cash flows.

    Returns the percentage return: (V_end - V_start - NetFlows) / (V_start + WeightedFlows) * 100.
    """
    d_start = _coerce_date(start_date) or date.today()
    d_end = _coerce_date(end_date) or date.today()
    if d_end < d_start:
        d_start, d_end = d_end, d_start
    total_days = max(1, (d_end - d_start).days)

    v_start = max(0.0, safe_float(start_value))
    v_end = max(0.0, safe_float(end_value))

    net_flows = 0.0
    weighted_flows = 0.0

    for flow_date, flow_amount in cash_flows:
        d_flow = _coerce_date(flow_date)
        if d_flow is None:
            continue
        amount = safe_float(flow_amount, default=float("nan"))
        if not math.isfinite(amount) or abs(amount) < 1e-12:
            continue
        if d_flow < d_start or d_flow > d_end:
            continue
        weight = (d_end - d_flow).days / total_days
        net_flows += amount
        weighted_flows += amount * weight

    denominator = v_start + weighted_flows
    # MATH-04: If denominator <= 1e-6 (e.g. massive withdrawals during the period),
    # Modified Dietz is mathematically undefined and inverts the sign of returns.
    if denominator <= 1e-6:
        return 0.0
    res = ((v_end - v_start - net_flows) / denominator) * 100.0
    if not math.isfinite(res):
        return 0.0
    return res


def xirr(
    cash_flows: Sequence[tuple[Union[date, str], float]],
    *,
    guess: float = 0.10,
    max_iterations: int = 100,
    tolerance: float = 1e-7,
) -> Optional[float]:
    """Calculate annualized Internal Rate of Return (XIRR) using hybrid Newton-Raphson + Bisection.

    Parameters:
        cash_flows: sequence of (date, amount) tuples. Inflows (investments) are negative,
                    outflows/liquidations (current value/returns) are positive, or vice versa.
        guess: initial rate guess (default 10% = 0.10).
        max_iterations: maximum solver attempts before bisection fallback.
        tolerance: absolute difference tolerance for NPV convergence to 0.

    Returns:
        Annualized rate as a float (e.g. 0.125 for 12.5%), or None if non-convergent or invalid.
    """
    if not cash_flows:
        return None

    parsed: dict[date, float] = {}
    for raw_d, raw_amt in cash_flows:
        d = _coerce_date(raw_d)
        if d is None:
            continue
        amt = safe_float(raw_amt)
        if abs(amt) > 1e-12:
            parsed[d] = parsed.get(d, 0.0) + amt

    if len(parsed) < 2:
        return None

    sorted_flows = sorted(parsed.items(), key=lambda item: item[0])
    first_date = sorted_flows[0][0]

    normalized: list[tuple[float, float]] = [
        ((d - first_date).days / 365.0, amt) for d, amt in sorted_flows
    ]

    # BUG-AUD-C82: Resilient runtime fallback. When NumPy is installed, use vectorized
    # acceleration for high performance on large series; if NumPy is absent, fall back
    # seamlessly to pure-Python math so clean installs never raise ModuleNotFoundError.
    try:
        import numpy as np

        t_arr = np.array([t for t, _ in normalized], dtype=np.float64)
        amt_arr = np.array([amt for _, amt in normalized], dtype=np.float64)

        has_pos = bool(np.any(amt_arr > 0))
        has_neg = bool(np.any(amt_arr < 0))
        if not (has_pos and has_neg):
            return None

        t_amt_arr = t_arr * amt_arr

        def npv_and_deriv(rate: float) -> tuple[float, float]:
            if rate <= -0.99999999:
                return float("inf"), float("inf")
            ln_r1 = math.log1p(rate)
            r_plus_1 = 1.0 + rate
            # BUG-AUD-C106: near r = -1 the exponent overflows float64 for long
            # horizons; numpy emits a RuntimeWarning before returning inf, which
            # pytest (-W error configs) would turn into a test failure. Suppress
            # locally: the inf residual is handled by the Brent fallback below.
            with np.errstate(over="ignore"):
                inv_disc = np.exp(-t_arr * ln_r1)
            val = float(np.dot(amt_arr, inv_disc))
            deriv = float(-np.dot(t_amt_arr, inv_disc) / r_plus_1)
            return val, deriv
    except ImportError:
        has_pos = any(amt > 0 for _, amt in normalized)
        has_neg = any(amt < 0 for _, amt in normalized)
        if not (has_pos and has_neg):
            return None

        def npv_and_deriv(rate: float) -> tuple[float, float]:
            if rate <= -0.99999999:
                return float("inf"), float("inf")
            ln_r1 = math.log1p(rate)
            r_plus_1 = 1.0 + rate
            val = 0.0
            deriv_num = 0.0
            for t, amt in normalized:
                try:
                    inv_disc = math.exp(-t * ln_r1)
                except OverflowError:
                    inv_disc = float("inf")
                val += amt * inv_disc
                deriv_num += (t * amt) * inv_disc
            deriv = -deriv_num / r_plus_1 if math.isfinite(deriv_num) else float("inf")
            return val, deriv

    # Phase 1: Newton-Raphson with domain guardrails
    rate = max(-0.99, min(10.0, guess))
    for _ in range(max_iterations):
        val, deriv = npv_and_deriv(rate)
        if abs(val) < tolerance:
            return rate
        if abs(deriv) < 1e-12:
            break
        step = val / deriv
        new_rate = rate - step
        if new_rate <= -0.999999 or new_rate > 100.0 or math.isnan(new_rate):
            break
        if abs(new_rate - rate) < tolerance:
            return new_rate
        rate = new_rate

    # Phase 2: Brent's root-finding method (combines inverse quadratic interpolation, secant, and bisection)
    a = -0.9999
    b = 10.0
    fa, _ = npv_and_deriv(a)
    fb, _ = npv_and_deriv(b)

    for expansion in (50.0, 100.0, 500.0, 1000.0):
        if fa * fb <= 0:
            break
        b = expansion
        fb, _ = npv_and_deriv(b)

    if fa * fb > 0:
        return None

    if abs(fa) < abs(fb):
        a, b = b, a
        fa, fb = fb, fa

    c = a
    fc = fa
    mflag = True
    d = 0.0

    def _safe_npv(rate: float) -> float:
        """BUG-AUD-C106: NPV with clamped rate and overflow guards.

        Near r = -1 the discount factor (1+r)^-t diverges; Brent evaluates
        trial points in that region, so NaN/OverflowError must degrade to a
        large signed residual instead of killing the solver.

        Sign semantics: NPV(r) = sum(amt_i * (1+r)^-t_i) and as r -> -1+ the
        term with the LARGEST t dominates the divergence, so the true limit
        is sign(last flow amount) * inf. The fallback must reproduce that
        limit, otherwise Brent sees an inverted bracket (e.g. a portfolio
        whose final flow is positive reports -1e12 near the singularity,
        fa*fb > 0, and the solver wrongly returns None).
        """
        clamped_rate = max(-0.999999, float(rate))
        if math.isnan(clamped_rate) or math.isinf(clamped_rate):
            return float("inf")
        # normalized is sorted by t ascending; the last entry dominates.
        dominant_sign = 1.0 if normalized[-1][1] >= 0.0 else -1.0
        total = 0.0
        for t, amt in normalized:
            try:
                factor = math.pow(1.0 + clamped_rate, -t)
                total += amt * factor
            except (OverflowError, ValueError, ZeroDivisionError):
                # Sign of the residual steers Brent away from the singularity.
                return dominant_sign * 1e12
        if math.isnan(total) or math.isinf(total):
            return dominant_sign * 1e12
        return total

    for _ in range(60):
        if abs(fb) < tolerance or abs(b - a) < tolerance:
            return b

        if abs(fa - fc) > 1e-12 and abs(fb - fc) > 1e-12 and abs(fa - fb) > 1e-12:
            # Inverse quadratic interpolation
            s = (
                (a * fb * fc) / ((fa - fb) * (fa - fc))
                + (b * fa * fc) / ((fb - fa) * (fb - fc))
                + (c * fa * fb) / ((fc - fa) * (fc - fb))
            )
        elif abs(fb - fa) > 1e-12:
            # Secant method
            s = b - fb * (b - a) / (fb - fa)
        else:
            s = (a + b) / 2.0
            mflag = True

        # Conditions to force bisection step
        cond1 = not ((3.0 * a + b) / 4.0 <= s <= b or b <= s <= (3.0 * a + b) / 4.0)
        cond2 = mflag and abs(s - b) >= abs(b - c) / 2.0
        cond3 = (not mflag) and abs(s - b) >= abs(c - d) / 2.0
        cond4 = mflag and abs(b - c) < tolerance
        cond5 = (not mflag) and abs(c - d) < tolerance

        if cond1 or cond2 or cond3 or cond4 or cond5:
            s = (a + b) / 2.0
            mflag = True
        else:
            mflag = False

        # BUG-AUD-C106: near r = -1 the vectorized derivative path can
        # produce NaN/OverflowError; fall back to the scalar clamped NPV so
        # Brent keeps a usable signed residual instead of crashing.
        fs, _ = npv_and_deriv(s)
        if math.isnan(fs) or math.isinf(fs):
            fs = _safe_npv(s)
        d = c
        c = b
        fc = fb

        if fa * fs < 0:
            b = s
            fb = fs
        else:
            a = s
            fa = fs

        if abs(fa) < abs(fb):
            a, b = b, a
            fa, fb = fb, fa

    return b


def xirr_pct(
    cash_flows: Sequence[tuple[Union[date, str], float]],
    *,
    guess: float = 0.10,
    max_iterations: int = 100,
    tolerance: float = 1e-7,
) -> Optional[float]:
    """Calculate XIRR returning percentage points (e.g. 12.5 for 12.5%)."""
    rate = xirr(
        cash_flows,
        guess=guess,
        max_iterations=max_iterations,
        tolerance=tolerance,
    )
    return rate * 100.0 if rate is not None else None


def _clean_returns_sequence(returns_pct: Sequence[Any]) -> list[float]:
    """Extract valid finite float returns, discarding None, booleans, NaN, inf and malformed strings."""
    if not returns_pct:
        return []
    clean: list[float] = []
    for r in returns_pct:
        if r is None or isinstance(r, bool):
            continue
        try:
            val = float(r)
            if math.isfinite(val):
                clean.append(val)
        except (ValueError, TypeError):
            continue
    return clean


def sharpe_ratio(
    returns_pct: Sequence[float],
    *,
    risk_free_rate_annual_pct: float = 2.5,
    periods_per_year: int = 12,
) -> float:
    """Calculate annualized Sharpe ratio from a sequence of periodic returns (in %)."""
    clean_returns = _clean_returns_sequence(returns_pct)
    n = len(clean_returns)
    if n < 2:
        return 0.0

    periods = max(1, int(periods_per_year or 12))
    rf_periodic = safe_float(risk_free_rate_annual_pct) / float(periods)
    excess = [r - rf_periodic for r in clean_returns]

    mean_excess = sum(excess) / n
    var = sum((x - mean_excess) ** 2 for x in excess) / (n - 1)
    std_dev = math.sqrt(var)

    if std_dev < 1e-12:
        return 99.99 if mean_excess > 0 else 0.0
    raw_sharpe = (mean_excess / std_dev) * math.sqrt(periods)
    return max(-99.99, min(99.99, raw_sharpe))


def sortino_ratio(
    returns_pct: Sequence[float],
    *,
    risk_free_rate_annual_pct: float = 2.5,
    target_downside_pct: float = 0.0,
    periods_per_year: int = 12,
) -> float:
    """Calculate annualized Sortino ratio considering only downside volatility (in %)."""
    clean_returns = _clean_returns_sequence(returns_pct)
    n = len(clean_returns)
    if n < 2:
        return 0.0

    periods = max(1, int(periods_per_year or 12))
    rf_periodic = safe_float(risk_free_rate_annual_pct) / float(periods)
    target_periodic = safe_float(target_downside_pct) / float(periods)

    mean_excess = (sum(clean_returns) / n) - rf_periodic
    downside_sq_sum = sum(min(0.0, r - target_periodic) ** 2 for r in clean_returns)
    downside_dev = math.sqrt(downside_sq_sum / n)

    if downside_dev < 1e-12:
        return 99.99 if mean_excess > 0 else 0.0
    raw_sortino = (mean_excess / downside_dev) * math.sqrt(periods)
    return max(-99.99, min(99.99, raw_sortino))



def max_drawdown(series: Sequence[float]) -> tuple[float, int, int]:
    """Calculate maximum peak-to-trough drawdown in a cumulative asset/portfolio series.

    Returns:
        tuple of (max_drawdown_pct, peak_index, trough_index)
        where max_drawdown_pct is a non-negative float (e.g. 24.5 for a 24.5% drop).
    """
    clean_series = [max(0.0, safe_float(v)) for v in series]
    if len(clean_series) < 2:
        return 0.0, 0, 0

    max_dd = 0.0
    peak_val = clean_series[0]
    peak_idx = 0
    best_peak_idx = 0
    best_trough_idx = 0

    for idx, val in enumerate(clean_series):
        if val > peak_val:
            peak_val = val
            peak_idx = idx
        elif peak_val > 0.0:
            dd = (peak_val - val) / peak_val * 100.0
            if dd > max_dd:
                max_dd = dd
                best_peak_idx = peak_idx
                best_trough_idx = idx

    return round(max_dd, 2), best_peak_idx, best_trough_idx


def cornish_fisher_var(
    returns_pct: Sequence[float],
    *,
    confidence: float = 0.95,
) -> float:
    """Calculate Value-at-Risk (VaR) using Cornish-Fisher expansion for non-normal skew/kurtosis.

    Returns estimated maximum loss percentage (e.g. 8.4 for 8.4% loss at 95% confidence).
    """
    clean = [
        val
        for r in returns_pct
        if (val := safe_float(r, default=float("nan"))) is not None
        and math.isfinite(val)
    ]
    n = len(clean)
    if n < 4:
        return 0.0

    mean_r = sum(clean) / n
    variance = max(0.0, sum((x - mean_r) ** 2 for x in clean) / (n - 1))
    std_dev = math.sqrt(variance)
    if std_dev < 1e-12:
        # MATH-03: Zero volatility with constant negative returns is a guaranteed loss, not zero risk
        return max(0.0, round(-mean_r, 4))

    # Moments: Skewness and Excess Kurtosis
    m3 = sum((x - mean_r) ** 3 for x in clean) / n
    m4 = sum((x - mean_r) ** 4 for x in clean) / n
    skew = m3 / (std_dev**3)
    kurt_excess = (m4 / (std_dev**4)) - 3.0

    # Standard normal critical value (approximation for common confidence intervals)
    conf = max(0.50, min(0.999, safe_float(confidence, 0.95)))
    # Rational approximation of inverse error function for normal quantile
    # For 95%: z ≈ 1.64485, 99%: z ≈ 2.32635
    if abs(conf - 0.95) < 0.01:
        z = 1.6448536
    elif abs(conf - 0.99) < 0.005:
        z = 2.3263479
    elif abs(conf - 0.90) < 0.01:
        z = 1.2815516
    else:
        # Standard normal inverse CDF approximation
        p = 1.0 - conf
        t = math.sqrt(-2.0 * math.log(p))
        c0, c1, c2 = 2.515517, 0.802853, 0.010328
        d1, d2, d3 = 1.432788, 0.189269, 0.001308
        z = t - (
            (c0 + c1 * t + c2 * t * t) / (1.0 + d1 * t + d2 * t * t + d3 * t * t * t)
        )

    # Cornish-Fisher expansion adjustment for alpha quantile with positive z
    # Negative skewness (heavy left crash tail) strictly increases z_cf and VaR loss
    z_cf = (
        z
        - (skew / 6.0) * (z**2 - 1.0)
        + (kurt_excess / 24.0) * (z**3 - 3.0 * z)
        - ((skew**2) / 36.0) * (2.0 * (z**3) - 5.0 * z)
    )

    # MATH-03: the closed-form CF polynomial loses monotonicity under extreme
    # skewness (e.g. skew ~ -20 collapses z_cf below zero, reporting VaR=0.0%)
    # and silently understates the left tail outside its moderate-moments
    # validity domain. Gate: (a) reject non-positive or non-finite z_cf via a
    # bracketed numerical inversion of the CF polynomial; (b) treat the closed
    # form as untrusted when |skew| > 1 or excess kurtosis > 6; (c) in every
    # untrusted case, validate the resulting VaR against the empirical
    # (1 - confidence) tail quantile (historical floor).
    collapsed = (not math.isfinite(z_cf)) or z_cf <= 0.0
    beyond_regime = abs(skew) > 1.0 or abs(kurt_excess) > 6.0
    if collapsed:
        z_cf = _cornish_fisher_quantile_numeric(z, skew, kurt_excess)

    # VaR as positive percentage loss
    var_loss = z_cf * std_dev - mean_r
    if collapsed or beyond_regime:
        # Epsilon guard: (1 - 0.99) * 100 == 1.0000000000000009 in IEEE-754,
        # which would ceil to 2 and skip the single worst observation.
        tail_n = max(1, int(math.ceil((1.0 - conf) * n - 1e-9)))
        empirical_loss = max(0.0, -sorted(clean)[tail_n - 1])
        if not math.isfinite(var_loss) or var_loss < empirical_loss:
            var_loss = empirical_loss
    if not math.isfinite(var_loss):
        return max(0.0, round(-mean_r, 4))
    return max(0.0, round(var_loss, 2))


def _cf_quantile_z(z0: float, skew: float, kurt: float, y: float) -> float:
    """Solve w - z0 = 0 where w is the CF-expanded quantile of standard normal y.

    Rearranged CF expansion: w = y - (S/6)(y^2-1) + (K/24)(y^3-3y)
                              - (S^2/36)(2y^3-5y); residual = w - z0.
    """

    return (
        y
        - (skew / 6.0) * (y**2 - 1.0)
        + (kurt / 24.0) * (y**3 - 3.0 * y)
        - ((skew**2) / 36.0) * (2.0 * (y**3) - 5.0 * y)
        - z0
    )


def _cornish_fisher_quantile_numeric(z0: float, skew: float, kurt: float) -> float:
    """Numerically invert the Cornish-Fisher polynomial via bracketed bisection.

    MATH-03 fallback: scans a bracket over standard-normal ordinates y in
    [z0/8, z0*4], expands on sign change of the residual, then bisects 200
    iterations (tolerance ~1e-12). For negative skewness the returned quantile
    is validated to be at least as deep as the Gaussian z0 (a heavy left tail
    must never shrink risk); the empirical tail floor is applied by the caller.
    """
    try:
        lo, hi = z0 / 8.0, z0 * 4.0
        f_lo = _cf_quantile_z(z0, skew, kurt, lo)
        f_hi = _cf_quantile_z(z0, skew, kurt, hi)
        # Expand bracket until the residual changes sign (residual is
        # monotonically increasing in y for the CF polynomial).
        for _ in range(60):
            if f_lo * f_hi <= 0.0 or abs(f_hi) < 1e-12:
                break
            lo = lo / 2.0
            f_lo = _cf_quantile_z(z0, skew, kurt, lo)
            if f_lo * f_hi <= 0.0:
                break
            hi = hi * 2.0
            f_hi = _cf_quantile_z(z0, skew, kurt, hi)
        else:
            return z0  # No sign change found: conservative Gaussian fallback

        if abs(f_hi) < 1e-12:
            y_root = hi
        elif abs(f_lo) < 1e-12:
            y_root = lo
        else:
            a, b = lo, hi
            fa = f_lo
            for _ in range(200):
                m = (a + b) / 2.0
                fm = _cf_quantile_z(z0, skew, kurt, m)
                if abs(fm) < 1e-12 or (b - a) / 2.0 < 1e-12:
                    break
                if fa * fm < 0.0:
                    b = m
                else:
                    a = m
                    fa = fm
            y_root = (a + b) / 2.0

        if not math.isfinite(y_root) or y_root <= 0.0:
            return z0

        # Validation: for negative skewness (heavy left tail) the CF quantile
        # must be at least as deep as the Gaussian quantile z0; never shallower.
        # The empirical (1 - confidence) tail floor is enforced by the caller
        # on the final VaR value (see cornish_fisher_var).
        if skew < 0.0 and y_root < z0:
            y_root = z0
        return y_root
    except (ValueError, OverflowError, ZeroDivisionError):
        return z0



def expected_shortfall_cvar(
    returns_pct: Sequence[float],
    *,
    confidence: float = 0.95,
) -> float:
    """Calculate Conditional Value-at-Risk (CVaR / Expected Shortfall) below the VaR cutoff.

    Returns the expected average loss percentage in the worst (1 - confidence)% scenarios.
    """
    clean = [
        val
        for r in returns_pct
        if (val := safe_float(r, default=float("nan"))) is not None
        and math.isfinite(val)
    ]
    n = len(clean)
    if n < 4:
        return 0.0

    sorted_returns = sorted(clean)
    cutoff_count = max(1, int(math.ceil((1.0 - confidence) * n - 1e-9)))
    tail = sorted_returns[:cutoff_count]

    avg_tail_return = sum(tail) / len(tail)
    return max(0.0, round(-avg_tail_return, 2))


def nominal_to_real_rate(nominal_rate_pct: float, inflation_rate_pct: float) -> float:
    """Convert nominal interest/return rate to real rate using Fisher's exact equation: (1+r_real) = (1+r_nom)/(1+i)."""
    i_nom = safe_float(nominal_rate_pct) / 100.0
    infl = safe_float(inflation_rate_pct) / 100.0
    if infl <= -1.0:
        return 0.0
    i_real = ((1.0 + i_nom) / (1.0 + infl)) - 1.0
    return round(i_real * 100.0, 4)


def real_to_nominal_rate(real_rate_pct: float, inflation_rate_pct: float) -> float:
    """Convert real interest/return rate to nominal rate using Fisher's exact equation: (1+r_nom) = (1+r_real)*(1+i)."""
    i_real = safe_float(real_rate_pct) / 100.0
    infl = safe_float(inflation_rate_pct) / 100.0
    i_nom = ((1.0 + i_real) * (1.0 + infl)) - 1.0
    return round(i_nom * 100.0, 4)


def calmar_ratio(cagr_pct: float, max_drawdown_pct: float) -> float:
    """Calculate the Calmar Ratio: annualized growth rate divided by maximum drawdown.

    Formula:
        Calmar = CAGR / |MaxDrawdown|

    Parameters:
        cagr_pct: Compound Annual Growth Rate in percentage points (e.g. 15.0 for 15%).
        max_drawdown_pct: Maximum peak-to-trough drawdown in percentage points (e.g. 20.0 or -20.0 for 20%).

    Returns:
        Calmar ratio rounded and bounded to [-99.99, 99.99]. Effectively zero
        drawdown uses a signed bound; invalid/non-finite input returns 0.0.
    """
    # Keep invalid input distinct from a legitimate zero drawdown. Otherwise
    # infinity/NaN would be normalized to zero and reported as a superb ratio.
    cagr_val = safe_float(cagr_pct, default=math.nan)
    mdd_val = abs(safe_float(max_drawdown_pct, default=math.nan))
    if not math.isfinite(cagr_val) or not math.isfinite(mdd_val):
        return 0.0
    if mdd_val < 1e-6:
        return 99.99 if cagr_val > 0 else (-99.99 if cagr_val < 0 else 0.0)
    raw_calmar = cagr_val / mdd_val
    return round(max(-99.99, min(99.99, raw_calmar)), 2)


__all__ = [
    "cagr",
    "calmar_ratio",
    "cornish_fisher_var",
    "effective_annual_to_periodic_rate",
    "expected_shortfall_cvar",
    "max_drawdown",
    "modified_dietz",
    "nominal_to_real_rate",
    "periodic_discount_factor",
    "periodic_pv_discount_factor",
    "present_value_of_contributions",
    "real_to_nominal_rate",
    "sharpe_ratio",
    "sortino_ratio",
    "twrr",
    "xirr",
    "xirr_pct",
]
