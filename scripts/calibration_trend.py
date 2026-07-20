"""Is the weekly recalibration actually closing the gap to the market?

The one command to run when asking "has anything changed since we paused
trading-mechanics work?" — see the kalshi-no-edge-root-cause memory for that
decision and the numbers behind it.

Reads every `fit_calibration_params` run recorded in `pipeline_runs` and shows
the pooled model-vs-market skill over time, so the question is answered from
accumulated evidence rather than a fresh manual audit each time.

Skill is the headline, not Brier. A model can improve against outcomes for
months while never gaining on the market's own prices, and only the latter
decides whether anything is tradeable. Positive skill means the model beat the
market on held-out rows; 0 means it merely reproduced the price; negative means
trading it would be worse than not.

Usage: uv run scripts/calibration_trend.py
"""

from __future__ import annotations

import os
import sys

from dotenv import load_dotenv

from monitoring import build_trend, summarize
from monitoring.trend import MIN_SERIES_FOR_TREND, REVISIT_STREAK


def _fetch_runs(database_url: str) -> list[tuple]:
    import psycopg

    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            "select id, started_at, detail from pipeline_runs "
            "where script = 'fit_calibration_params' and status in ('success', 'partial') "
            "order by started_at"
        )
        return cur.fetchall()


def main() -> int:
    load_dotenv()
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("DATABASE_URL not set — nothing to read.")
        return 1

    points = build_trend(_fetch_runs(database_url))
    if not points:
        print("No fit_calibration_params runs recorded yet.")
        return 0

    print(f"{'run':>5}  {'when':<16} {'series':>6} {'brier':>8} {'market':>8} {'skill':>9}  note")
    prev = None
    for p in points:
        skill = f"{p.skill_vs_market:+.4f}" if p.has_market else "     n/a"
        model = f"{p.brier_model:.4f}" if p.brier_model is not None else f"{p.adopted_brier_unweighted or 0:.4f}*"
        market = f"{p.brier_market:.4f}" if p.brier_market is not None else "     n/a"

        notes = []
        if not p.comparable:
            notes.append(f"partial run (<{MIN_SERIES_FOR_TREND} series), excluded from trend")
        if not p.has_market:
            notes.append("predates market benchmark")
        if prev and p.has_market and prev.has_market:
            d = p.skill_vs_market - prev.skill_vs_market
            notes.append(f"{'better' if d > 0 else 'worse' if d < 0 else 'flat'} by {abs(d):.4f}")
        print(f"{p.run_id:>5}  {p.started_at:%Y-%m-%d %H:%M}  {p.series_count:>6} {model:>8} {market:>8} {skill:>9}  {'; '.join(notes)}")
        if p.has_market:
            prev = p

    print("\n* = Brier against outcomes only; that run predates the market benchmark.")

    summary = summarize([p for p in points if p.comparable])
    print("\n=== Where this stands ===")
    if summary.latest is None:
        print("  No run yet carries a market benchmark — re-run the weekly fit to record one.")
        return 0

    print(f"  Latest skill vs market: {summary.latest.skill_vs_market:+.4f} "
          f"(model {summary.latest.brier_model} vs market {summary.latest.brier_market}, "
          f"n={summary.latest.n_market_rows})")
    if summary.skill_delta is None:
        print("  Only one comparable run so far — no week-over-week movement to report yet.")
    else:
        direction = "toward" if summary.skill_delta > 0 else "away from"
        print(f"  Week-over-week: {summary.skill_delta:+.4f} ({direction} tradeable)")
    print(f"  Consecutive improving runs: {summary.improving_streak}")

    if summary.latest.skill_vs_market > 0:
        print("\n  *** SKILL IS POSITIVE — the model beat the market on held-out rows. ***")
        print("  This is the strong revisit condition. Re-run scripts/run_backtest.py to")
        print("  confirm the gate reports TRADEABLE before acting on it.")
    elif summary.improving_streak >= REVISIT_STREAK:
        print(f"\n  *** {summary.improving_streak} consecutive improving runs (bar: {REVISIT_STREAK}). ***")
        print("  Worth a look at whether the trend is real or drift — still negative skill,")
        print("  so this is a 'go investigate the forecasting core', not 'resume trading work'.")
    else:
        print(f"\n  No revisit trigger met (needs positive skill, or {REVISIT_STREAK} consecutive")
        print("  improving runs). Trading-mechanics work stays paused.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
