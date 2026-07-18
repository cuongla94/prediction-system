"""Ensemble members -> per-bracket probability, matched to Kalshi's settlement math.

v1 (Normal, raw live-pooled-ensemble mean/std) shipped 2026-07-17 and was
backtested the same day against ~600 real settled days per city: it had a
specific, systematic bias — under-confident near 0% predicted probability,
over-confident above ~40%. The natural first guess was that the distribution
needed fatter tails; that was tested (optional `df` param below, Student's t)
and rejected — it didn't improve Brier score in any city and was worse in
several. What actually explained the pattern: every city's day-1-ahead forecast
runs systematically cold by 1.4-2.1°F, which alone produces exactly this
miscalibration shape. `calibrated_bracket_probability` /
`calibrated_probability_for_market` apply that fitted correction (see
weather/calibration_params.py and kalshi-backtest-findings memory) — use those
for anything live-facing instead of raw `fit_normal` output. Whichever path is
used, "runs without error" is still not the same as "calibrated" — re-run the
backtest after any further change here.
"""

from __future__ import annotations

import numpy as np
from scipy import stats

from .calibration_params import get_calibration

_MIN_STD = 0.5  # guards against a degenerate near-zero spread, not a smoothing prior


def fit_normal(members: list[float]) -> tuple[float, float]:
    """Mean and std (ddof=1) of pooled ensemble members, treating them as a
    sample from the true forecast-error distribution rather than the full
    population."""
    if len(members) < 2:
        raise ValueError(f"Need at least 2 ensemble members to fit a distribution, got {len(members)}.")
    arr = np.array(members, dtype=float)
    return float(arr.mean()), max(float(arr.std(ddof=1)), _MIN_STD)


def bracket_probability(
    loc: float,
    scale: float,
    floor_strike: float | None,
    cap_strike: float | None,
    df: float | None = None,
) -> float:
    """P(bracket resolves YES).

    NWS reports the daily high rounded to the nearest whole degree, and Kalshi's
    floor_strike/cap_strike are defined on that rounded value: "less than 79"
    excludes a 79° reading; "between 79 and 80" means the rounded reading is 79 or
    80. A half-degree continuity correction converts the rounding boundary into a
    continuous cutoff for the fitted distribution: "less than 79" -> P(X < 78.5);
    "greater than 86" -> P(X > 86.5); "between 79 and 80" -> P(78.5 < X < 80.5).

    Distribution is Normal(loc, scale) by default. Pass `df` to use a Student's t
    instead (fatter tails) — `loc`/`scale` for a fitted t should come straight
    from `scipy.stats.t.fit()`, not a normal mean/std reused as-is: a
    t-distribution's `scale` isn't its standard deviation except as df -> infinity,
    so mixing a normal-fit std into a t-distribution call under-widens it.
    """
    dist = stats.t(df, loc=loc, scale=scale) if df is not None else stats.norm(loc=loc, scale=scale)
    if floor_strike is None and cap_strike is not None:
        return float(dist.cdf(cap_strike - 0.5))
    if cap_strike is None and floor_strike is not None:
        return float(dist.sf(floor_strike + 0.5))
    if floor_strike is not None and cap_strike is not None:
        return float(dist.cdf(cap_strike + 0.5) - dist.cdf(floor_strike - 0.5))
    raise ValueError("Market has neither floor_strike nor cap_strike set — can't bound a bracket.")


def check_boundary_language(rules_primary: str, floor_strike: float | None, cap_strike: float | None) -> None:
    """Cross-checks rules_primary prose against the structured strike fields.

    Cheap insurance against Kalshi's schema/semantics shifting silently — if
    floor_strike/cap_strike ever stopped meaning what they mean today, this flags
    a text/field mismatch instead of quietly mispricing every bracket. See
    kalshi-api-gotchas memory on why boundary precision matters here.
    """
    text = rules_primary.lower()
    if floor_strike is None and cap_strike is not None:
        if "less than" not in text and "below" not in text:
            raise ValueError(f"Expected 'less than' language for a cap-only bracket, got: {rules_primary!r}")
    elif cap_strike is None and floor_strike is not None:
        if not any(phrase in text for phrase in ("greater than", "above", "more than")):
            raise ValueError(f"Expected 'greater than' language for a floor-only bracket, got: {rules_primary!r}")
    elif floor_strike is not None and cap_strike is not None:
        if "between" not in text:
            raise ValueError(f"Expected 'between' language for a floor+cap bracket, got: {rules_primary!r}")
    else:
        raise ValueError("Market has neither floor_strike nor cap_strike set.")


def probability_for_market(
    rules_primary: str,
    floor_strike: float | None,
    cap_strike: float | None,
    loc: float,
    scale: float,
    *,
    df: float | None = None,
    validate_rules_text: bool = True,
) -> float:
    if validate_rules_text:
        check_boundary_language(rules_primary, floor_strike, cap_strike)
    return bracket_probability(loc, scale, floor_strike, cap_strike, df=df)


def calibrated_bracket_probability(
    series_ticker: str,
    ensemble_mean: float,
    floor_strike: float | None,
    cap_strike: float | None,
) -> float:
    """bracket_probability(), using the per-city bias/std correction fit from
    the 2026-07-17 backtest instead of the raw live ensemble mean/std. Use this
    (or calibrated_probability_for_market) for anything live-facing — the raw
    ensemble mean runs cold and its std runs wide relative to what actually
    happened historically, both confirmed in the backtest.
    """
    params = get_calibration(series_ticker)
    return bracket_probability(ensemble_mean + params.mean_bias, params.std, floor_strike, cap_strike)


def calibrated_probability_for_market(
    series_ticker: str,
    rules_primary: str,
    floor_strike: float | None,
    cap_strike: float | None,
    ensemble_mean: float,
    *,
    validate_rules_text: bool = True,
) -> float:
    if validate_rules_text:
        check_boundary_language(rules_primary, floor_strike, cap_strike)
    return calibrated_bracket_probability(series_ticker, ensemble_mean, floor_strike, cap_strike)
