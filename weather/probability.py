"""Ensemble members -> per-bracket probability, matched to Kalshi's settlement math.

v1 (Normal, raw live-pooled-ensemble mean/std) shipped 2026-07-17 and was
backtested the same day against ~600 real settled days per city: it had a
specific, systematic bias — under-confident near 0% predicted probability,
over-confident above ~40%. The natural first guess was that the distribution
needed fatter tails; that was tested (optional `df` param below, Student's t)
and rejected — it didn't improve Brier score in any city and was worse in
several. What actually explained the pattern: every city's day-1-ahead forecast
runs systematically cold by 1.4-2.1°F on average, which alone produces exactly
this miscalibration shape.

v3 (2026-07-18): that average bias isn't stable across the year — NYC's flips
sign entirely between winter (+2.7°F cold) and summer (-1.6°F warm), a pattern
a single flat correction can't represent. Re-validated per city rather than
assumed: NYC/Chicago/Denver's Brier score improves with a per-month bias,
Philadelphia/Austin/Miami's seasonal swing is modest enough that a flat bias
still wins (less estimation noise). `weather/calibration_params.py` encodes
whichever won per city; `calibrated_bracket_probability` /
`calibrated_probability_for_market` need the target month to look it up — use
these for anything live-facing instead of raw `fit_normal` output. Whichever
path is used, "runs without error" is still not the same as "calibrated" —
re-run scripts/fit_calibration_params.py periodically as more data accumulates.
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


def heteroscedastic_scale(baseline_var: float, spread_coef: float, forecast_spread: float) -> float:
    """Per-day forecast std as sqrt(baseline_var + spread_coef * spread^2),
    floored the same way fit_normal floors its std.

    `baseline_var` is the day-independent error variance; `spread_coef` scales
    the live ensemble's own cross-model disagreement (`forecast_spread`) into
    extra variance on days the models diverge. The whole point is that a day
    the models agree on and a day they don't shouldn't get the same fixed
    confidence — which the current per-city fixed std (calibrated_bracket_
    probability) structurally can't express. Both params come from
    backtest.harness.fit_spread_scale, fit on data the eval set is held out
    from, not guessed; `spread_coef == 0` collapses this back to a constant
    sqrt(baseline_var), i.e. exactly the fixed-std behavior.
    """
    return max((baseline_var + spread_coef * forecast_spread**2) ** 0.5, _MIN_STD)


def heteroscedastic_bracket_probability(
    loc: float,
    baseline_var: float,
    spread_coef: float,
    forecast_spread: float,
    floor_strike: float | None,
    cap_strike: float | None,
) -> float:
    """bracket_probability with a per-day scale from heteroscedastic_scale
    instead of a fixed std.

    Deliberately NOT wired into the live pipeline yet: generate_alerts.py still
    uses the fixed-std calibrated_* path. This exists for scripts/run_backtest.py
    to evaluate against the fixed-std Normal baseline on real settled data
    first. Promote it to live use only if that comparison shows a real Brier
    improvement — the same bar the Student's t alternative had to clear (and
    failed). "Runs without error" is not "calibrated," exactly as this module's
    own docstring warns.
    """
    scale = heteroscedastic_scale(baseline_var, spread_coef, forecast_spread)
    return bracket_probability(loc, scale, floor_strike, cap_strike)


def observation_conditioned_bracket_probability(
    loc: float,
    scale: float,
    floor_strike: float | None,
    cap_strike: float | None,
    metric: str,
    observed_so_far: float,
    *,
    remaining_scale_fraction: float = 1.0,
) -> float:
    """P(bracket resolves YES) given `observed_so_far` — the extreme this
    station has *already recorded* today (weather/nws_observations.py).

    This is the fix for the failure diagnosed 2026-07-20 (see the
    kalshi-no-edge-root-cause memory): every other function in this module
    prices a bracket off an unconditional full-day-ahead forecast, which is
    simply the wrong distribution once part of the day has happened. A daily
    *high* cannot come in below a temperature the station has already hit —
    that is arithmetic, not a modelling opinion — yet the unconditional model
    was assigning an average 34.5% to brackets already ruled out this way, and
    went 0-for-171 on them live while the market priced them at ~1c.

    The model: the final extreme is `max(observed_so_far, M)` for a "max"
    metric (`min(...)` for "min"), where `M ~ Normal(loc, scale)` is the
    extreme over the rest of the day. That yields a point mass at
    `observed_so_far` — the genuinely large probability that today's extreme
    has already happened — rather than smearing that mass across brackets the
    day has already excluded. Boundary/rounding convention is bracket_
    probability's, unchanged.

    `remaining_scale_fraction` (0-1] multiplies `scale` to reflect that less
    of the day left means less room left to move. Left at 1.0 it applies no
    shrinkage at all, which is deliberately the conservative default: the
    truncation above is correct by construction and needs no validation, but
    *how fast* forecast uncertainty should decay through the day is a real
    modelling choice, and this project's standard (see module docstring, and
    the Student's-t alternative that was tested and rejected) is that such a
    choice earns its way in through backtest.harness, not by assertion. Fit it
    before passing anything else.
    """
    if metric not in ("min", "max"):
        raise ValueError(f"metric must be 'min' or 'max', got {metric!r}")
    if not 0 < remaining_scale_fraction <= 1:
        raise ValueError(f"remaining_scale_fraction must be in (0, 1], got {remaining_scale_fraction}")

    dist = stats.norm(loc=loc, scale=max(scale * remaining_scale_fraction, _MIN_STD))

    # The bracket's continuous bounds under the same half-degree correction
    # bracket_probability applies, with an unbounded side left as +/-inf.
    if floor_strike is None and cap_strike is not None:
        low, high = float("-inf"), cap_strike - 0.5
    elif cap_strike is None and floor_strike is not None:
        low, high = floor_strike + 0.5, float("inf")
    elif floor_strike is not None and cap_strike is not None:
        low, high = floor_strike - 0.5, cap_strike + 0.5
    else:
        raise ValueError("Market has neither floor_strike nor cap_strike set — can't bound a bracket.")

    in_bracket = low <= observed_so_far <= high
    if metric == "max":
        # Final = max(observed, M): mass below `observed` collapses onto it.
        continuous = dist.cdf(high) - dist.cdf(max(low, observed_so_far))
        atom = dist.cdf(observed_so_far) if in_bracket else 0.0
    else:
        # Final = min(observed, M): mass above `observed` collapses onto it.
        continuous = dist.cdf(min(high, observed_so_far)) - dist.cdf(low)
        atom = dist.sf(observed_so_far) if in_bracket else 0.0

    return float(min(max(max(continuous, 0.0) + atom, 0.0), 1.0))


def calibrated_observation_conditioned_probability(
    series_ticker: str,
    ensemble_mean: float,
    floor_strike: float | None,
    cap_strike: float | None,
    target_month: int,
    metric: str,
    observed_so_far: float | None,
    *,
    remaining_scale_fraction: float = 1.0,
) -> float:
    """calibrated_bracket_probability, conditioned on today's already-recorded
    extreme when there is one.

    `observed_so_far=None` (no observation available, or the target date isn't
    today) falls back to the unconditional calibrated path, so this is safe to
    call for every market regardless of lead time — a market settling tomorrow
    genuinely has nothing observed yet, and gets exactly the old behavior.
    """
    params = get_calibration(series_ticker)
    loc = ensemble_mean + params.bias_for_month(target_month)
    if observed_so_far is None:
        return bracket_probability(loc, params.std, floor_strike, cap_strike)
    return observation_conditioned_bracket_probability(
        loc,
        params.std,
        floor_strike,
        cap_strike,
        metric,
        observed_so_far,
        remaining_scale_fraction=remaining_scale_fraction,
    )


def temperature_in_bracket(actual_temp: float, floor_strike: float | None, cap_strike: float | None) -> bool:
    """Whether an already-whole-degree actual reading falls inside this
    bracket — the boolean counterpart to bracket_probability's boundary
    convention (see that docstring): "less than 79" excludes 79 itself,
    "between 79 and 80" includes both ends. Callers feeding in a value from a
    source that isn't natively whole-degree Fahrenheit (e.g. NOAA CDO's
    GHCND TMAX, converted server-side from tenths of Celsius) should round it
    first — this function doesn't apply its own rounding, since whether that's
    appropriate depends on the data source, not on bracket semantics.
    """
    if floor_strike is None and cap_strike is not None:
        return actual_temp < cap_strike
    if cap_strike is None and floor_strike is not None:
        return actual_temp > floor_strike
    if floor_strike is not None and cap_strike is not None:
        return floor_strike <= actual_temp <= cap_strike
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
    target_month: int,
) -> float:
    """bracket_probability(), using the per-city bias/std correction fit from
    the 2026-07-18 backtest instead of the raw live ensemble mean/std. Use this
    (or calibrated_probability_for_market) for anything live-facing — the raw
    ensemble mean runs cold and its std runs wide relative to what actually
    happened historically, both confirmed in the backtest.

    `target_month` (1-12) is the settlement date's month, not today's — for a
    market settling tomorrow that's usually the same, but always pass the
    actual target date's month rather than assuming. Bias varies by season for
    some cities (see module docstring); get_calibration's bias_for_month()
    falls back to that city's flat bias for a month with insufficient fit data
    or a city where the flat bias validated better overall.
    """
    params = get_calibration(series_ticker)
    bias = params.bias_for_month(target_month)
    return bracket_probability(ensemble_mean + bias, params.std, floor_strike, cap_strike)


def calibrated_probability_for_market(
    series_ticker: str,
    rules_primary: str,
    floor_strike: float | None,
    cap_strike: float | None,
    ensemble_mean: float,
    target_month: int,
    *,
    validate_rules_text: bool = True,
) -> float:
    if validate_rules_text:
        check_boundary_language(rules_primary, floor_strike, cap_strike)
    return calibrated_bracket_probability(series_ticker, ensemble_mean, floor_strike, cap_strike, target_month)
