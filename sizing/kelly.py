"""How much to stake, given a bracket's edge — and how much total exposure is
reasonable across a whole event's worth of correlated brackets.

Single-bracket sizing uses the standard closed-form Kelly fraction for a binary
contract (see `single_bracket_kelly`) — this part is textbook, not a guess.
Multi-bracket sizing is deliberately simpler than it could be: the fully rigorous
version (jointly optimizing stakes across a whole partition of mutually exclusive
outcomes against a market with an overround) has no closed form in the general
case and needs numerical optimization. Same-event brackets are mutually
exclusive (at most one settles Yes), so summing each bracket's Kelly fraction as
if it were an isolated bet overstates the case for spreading across several of
them. `size_event` handles this with a hard cap on total event exposure rather
than a joint optimizer — simpler, auditable by a human reviewing the dashboard,
and conservative in the direction that matters (caps risk down, never up).

Both DEFAULT_KELLY_FRACTION and DEFAULT_MAX_EVENT_EXPOSURE are risk-tolerance
choices, not derived constants — see their own comments for the reasoning, and
treat them as starting points to tune via KELLY_FRACTION / MAX_EVENT_EXPOSURE
env vars, not settled numbers.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import NamedTuple

# Quarter-Kelly, not full Kelly. Full Kelly is growth-optimal only if the
# probability estimate is exactly right; this system's own dashboard banner
# says calibration isn't fully validated yet (thin samples above ~40%, no
# independent ground-truth check as of 2026-07-18 — see kalshi-backtest-findings
# memory). Betting full Kelly against an estimate you already know is uncertain
# is a well-documented way to overshoot and give back gains in the downside
# scenarios; a fractional Kelly is the standard mitigation (Thorp and others).
DEFAULT_KELLY_FRACTION = 0.25

# Cap on total recommended stake across one event's brackets, as a fraction of
# bankroll — a risk-tolerance judgment call, not a derived number. Exists
# because same-event brackets are correlated (mutually exclusive outcomes of
# one temperature draw), so "3 brackets each look good in isolation" doesn't
# mean 3x the exposure is reasonable.
DEFAULT_MAX_EVENT_EXPOSURE = 0.15

# Kalshi caps position size per market around $25k (see kalshi-api-gotchas
# memory) — independent of bankroll, so this binds separately from the
# fraction-based caps above whenever bankroll * fraction would exceed it.
KALSHI_POSITION_LIMIT_USD = 25_000


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def kelly_fraction_setting() -> float:
    return _env_float("KELLY_FRACTION", DEFAULT_KELLY_FRACTION)


def max_event_exposure_setting() -> float:
    return _env_float("MAX_EVENT_EXPOSURE", DEFAULT_MAX_EVENT_EXPOSURE)


def single_bracket_kelly(model_probability: float, market_yes_price: float, side: str) -> float:
    """Full-Kelly fraction of bankroll for one bracket, bet in isolation.

    Standard binary-contract Kelly: staking fraction f of bankroll on a
    contract priced p that pays $1 on Yes multiplies bankroll by (1 + f*(1-p)/p)
    on a win and (1-f) on a loss. Solving for the growth-maximizing f given true
    win probability q collapses to f* = (q - p) / (1 - p) for a Yes bet — i.e.
    edge over (1 - price). The Yes/No symmetry gives f* = (p - q) / p for a No
    bet. Returns 0.0 for FLAT or a degenerate price (nothing to stake either way).
    """
    if side == "YES":
        denom = 1 - market_yes_price
        if denom <= 0:
            return 0.0
        f = (model_probability - market_yes_price) / denom
    elif side == "NO":
        if market_yes_price <= 0:
            return 0.0
        f = (market_yes_price - model_probability) / market_yes_price
    else:
        return 0.0
    return max(0.0, f)


class BracketInput(NamedTuple):
    market_ticker: str
    model_probability: float
    market_yes_price: float
    side: str
    is_actionable: bool


@dataclass(frozen=True)
class SizeRecommendation:
    market_ticker: str
    full_kelly_fraction: float  # theoretical, single-bet-in-isolation
    recommended_fraction: float  # after the fractional multiplier + event-cap scaling


def size_event(
    brackets: list[BracketInput],
    *,
    kelly_fraction: float | None = None,
    max_event_exposure: float | None = None,
) -> dict[str, SizeRecommendation]:
    """Position size for every bracket in one event, capped as a group.

    Takes plain `BracketInput` tuples rather than dashboard `Alert` objects, so
    this stays testable and has no dependency on the dashboard package.
    Non-actionable brackets get a recommendation of 0 — sizing shouldn't
    recommend a stake on an edge too small to clear its own fee threshold.
    """
    kelly_fraction = DEFAULT_KELLY_FRACTION if kelly_fraction is None else kelly_fraction
    max_event_exposure = (
        DEFAULT_MAX_EVENT_EXPOSURE if max_event_exposure is None else max_event_exposure
    )

    full_kelly: dict[str, float] = {}
    scaled: dict[str, float] = {}
    for b in brackets:
        full = (
            single_bracket_kelly(b.model_probability, b.market_yes_price, b.side)
            if b.is_actionable
            else 0.0
        )
        full_kelly[b.market_ticker] = full
        scaled[b.market_ticker] = full * kelly_fraction

    total = sum(scaled.values())
    event_scale = min(1.0, max_event_exposure / total) if total > 0 else 1.0

    return {
        market_ticker: SizeRecommendation(
            market_ticker=market_ticker,
            full_kelly_fraction=full_kelly[market_ticker],
            recommended_fraction=frac * event_scale,
        )
        for market_ticker, frac in scaled.items()
    }


def recommended_dollars(recommended_fraction: float, bankroll_usd: float) -> float:
    """Dollar stake for a recommended fraction, clipped to Kalshi's per-market cap."""
    return min(recommended_fraction * bankroll_usd, KALSHI_POSITION_LIMIT_USD)
