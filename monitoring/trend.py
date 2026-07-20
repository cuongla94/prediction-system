"""Week-over-week trend of the calibration fit, from stored pipeline_runs.

Built 2026-07-20, when weekly auto-applied recalibration went live and
trading-mechanics work was paused (see the kalshi-no-edge-root-cause memory).
The pause needs a *checkable* exit condition rather than "re-audit it by hand
occasionally", and that means a number that moves on its own and can be looked
at in one place.

The number that matters is **skill vs market**, not Brier. The model was
already reasonably calibrated against outcomes while losing decisively to the
market's own prices — Brier 0.1224 vs 0.0048 live, 0.1215 vs 0.0010 on the full
backtest. So a Brier-only trend could improve steadily for months while the
thing that actually decides tradeability never budged. Both are reported here,
but skill is the one the revisit trigger is written against.

Pure aggregation on purpose: callers hand in already-fetched rows, so this is
testable without a database and is shared unchanged between
scripts/calibration_trend.py and the dashboard's /backtest page.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

# A fit is only comparable to another if it covered a similar amount of data.
# Guards against reading a 6-city smoke run (there are several in the history
# from 2026-07-19) as a real week-over-week movement against a 40-series run.
MIN_SERIES_FOR_TREND = 20

# Consecutive improving weekly fits before the paused trading-mechanics work is
# worth reconsidering. Deliberately a judgment call, not a statistical
# threshold: one better week is noise, and the failure mode being guarded
# against is resuming automation on top of a signal that never really
# recovered. Lives here rather than in the CLI so the dashboard, the script and
# the tests all quote the same bar. See the kalshi-no-edge-root-cause memory.
REVISIT_STREAK = 4


@dataclass(frozen=True)
class TrendPoint:
    """One recalibration run, pooled across every series it covered."""

    run_id: int
    started_at: datetime
    series_count: int
    # Pooled across series, weighted by each series' own held-out row count.
    # Brier is a mean, so a row-weighted mean of per-series means reconstructs
    # the true pooled figure exactly — no need to have stored every prediction.
    brier_model: float | None
    brier_market: float | None
    skill_vs_market: float | None
    n_market_rows: int
    # Brier against outcomes only (no market involved), always available.
    adopted_brier_unweighted: float | None

    @property
    def comparable(self) -> bool:
        return self.series_count >= MIN_SERIES_FOR_TREND

    @property
    def has_market(self) -> bool:
        return self.skill_vs_market is not None


def pooled_market_benchmark(results: list[dict[str, Any]]) -> dict[str, float] | None:
    """Row-weighted pooling of per-series market benchmarks.

    Shared with scripts/fit_calibration_params.py so the summary line it writes
    and the trend computed from its stored detail can never disagree.
    """
    usable = [r for r in results if r.get("n_market_rows") and r.get("market_brier") is not None]
    if not usable:
        return None
    total = sum(r["n_market_rows"] for r in usable)
    if total == 0:
        return None
    model = sum(r["adopted_brier"] * r["n_market_rows"] for r in usable) / total
    market = sum(r["market_brier"] * r["n_market_rows"] for r in usable) / total
    return {
        "brier_model": round(model, 4),
        "brier_market": round(market, 4),
        # Recomputed from the pooled figures rather than averaging per-series
        # skill: skill is a ratio, and a mean of ratios is not the ratio of
        # means. Averaging them would let one tiny-n city with a wild ratio
        # dominate the headline number.
        "skill_vs_market": round(0.0 if market == 0 else 1 - model / market, 4),
        "n": total,
    }


def _to_point(run_id: int, started_at: datetime, detail: str | None) -> TrendPoint | None:
    if not detail:
        return None
    try:
        rows = json.loads(detail)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(rows, list) or not rows:
        return None

    pooled = pooled_market_benchmark(rows)
    # Pre-2026-07-20 runs predate the adopted_brier field; fall back to whichever
    # variant their own `using` says shipped so the outcome-Brier series still
    # extends back through the older history instead of starting flat.
    adopted = [
        r.get("adopted_brier")
        if r.get("adopted_brier") is not None
        else (r.get("monthly_brier") if r.get("using") == "monthly" else r.get("flat_brier"))
        for r in rows
    ]
    adopted = [a for a in adopted if a is not None]

    return TrendPoint(
        run_id=run_id,
        started_at=started_at,
        series_count=len(rows),
        brier_model=pooled["brier_model"] if pooled else None,
        brier_market=pooled["brier_market"] if pooled else None,
        skill_vs_market=pooled["skill_vs_market"] if pooled else None,
        n_market_rows=pooled["n"] if pooled else 0,
        adopted_brier_unweighted=round(sum(adopted) / len(adopted), 4) if adopted else None,
    )


def build_trend(runs: list[tuple[int, datetime, str | None]]) -> list[TrendPoint]:
    """`(run_id, started_at, detail_json)` rows -> chronological trend points.

    Unparseable or empty runs are dropped rather than raising: this feeds a
    dashboard panel and a status script, neither of which should break because
    one historical row was written by an older version of the fit.
    """
    points = [p for p in (_to_point(*r) for r in runs) if p is not None]
    return sorted(points, key=lambda p: p.started_at)


def comparable_trend(runs: list[tuple[int, datetime, str | None]]) -> list[TrendPoint]:
    """build_trend, restricted to runs broad enough to compare against each
    other (see MIN_SERIES_FOR_TREND)."""
    return [p for p in build_trend(runs) if p.comparable]


@dataclass(frozen=True)
class TrendSummary:
    points: list[TrendPoint]
    latest: TrendPoint | None
    previous: TrendPoint | None
    skill_delta: float | None  # latest - previous; POSITIVE means moving toward tradeable
    improving_streak: int  # consecutive most-recent runs where skill rose


def summarize(points: list[TrendPoint]) -> TrendSummary:
    """Latest vs previous, plus how many consecutive recent runs improved.

    `improving_streak` is what the revisit trigger is written against — one
    better week is noise, several consecutive ones is a signal worth acting on.
    Only runs that actually carry a market benchmark count toward it.
    """
    with_market = [p for p in points if p.has_market]
    latest = with_market[-1] if with_market else None
    previous = with_market[-2] if len(with_market) >= 2 else None

    delta = None
    if latest and previous:
        delta = round(latest.skill_vs_market - previous.skill_vs_market, 4)

    streak = 0
    for newer, older in zip(reversed(with_market), reversed(with_market[:-1]), strict=False):
        if newer.skill_vs_market > older.skill_vs_market:
            streak += 1
        else:
            break

    return TrendSummary(
        points=points, latest=latest, previous=previous, skill_delta=delta, improving_streak=streak
    )
