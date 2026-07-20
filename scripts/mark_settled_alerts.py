"""Marks alerts as settled once their underlying Kalshi market resolves.

Without this, dashboard/db.py's "latest alert per market, WHERE settled_at IS
NULL" filter has nothing to exclude — each day's alerts use distinct
market_tickers (KXHIGHNY-26JUL18-T79 vs ...-26JUL19-T79 are different rows,
not the same one overwritten), so without a settlement writeback, every past
day's alerts would keep showing up on the dashboard forever, not just today's.
This is also what lets paper_trading close out matured positions and free
their cash back up (scripts/run_paper_trading.py::plan_settlements) and what
lets live alert performance be tracked over time, independent of the one-off
backtest.

As of 2026-07-20, checks are batched per series rather than one get_market()
call per pending ticker: with ~250-500 pending tickers typically outstanding
across 40 series, the old one-call-per-ticker approach reliably tripped
Kalshi's rate limit well before finishing a single run (see
kalshi_client/client.py's own rate-limit comment — ~120 calls in a tight loop
was already enough to trigger it at 40 series). Batching to one paginated
get_markets(series_ticker=..., status="settled") call per series (~40
calls total, each returning every recently-finalized market for that series
in one page) is what makes running this on a tight, independent cadence (see
scheduler/run_settlement_cycle.sh) actually cheap enough to do — not just a
smaller version of the same problem.

Usage: uv run scripts/mark_settled_alerts.py
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime

import psycopg
from dotenv import load_dotenv

from kalshi_client import KalshiClient
from monitoring import track_run

# Defensive cap on how many pages of finalized markets to walk per series —
# genuinely shouldn't ever be hit (Kalshi ages old finalized markets out of
# /markets into /historical/markets past its own live/historical cutoff, so
# this stays a bounded, recent-only list), but a bug or an API change
# shouldn't be able to turn this into an unbounded loop against a single
# series while every other series waits behind it.
_MAX_PAGES_PER_SERIES = 5


def check_and_mark_series(
    client: KalshiClient, conn: psycopg.Connection, series_ticker: str, pending_tickers: list[str]
) -> int:
    """Batch-checks one series' recently-finalized markets against this
    run's pending set for that series, writing back settled_at/actual_outcome
    for any match. Returns how many were newly marked settled."""
    pending_set = set(pending_tickers)
    newly_settled = 0
    cursor: str | None = None

    for _page in range(_MAX_PAGES_PER_SERIES):
        try:
            markets, cursor = client.get_markets(series_ticker=series_ticker, status="settled", limit=200, cursor=cursor)
        except Exception as exc:
            print(f"  {series_ticker}: couldn't fetch finalized markets ({exc.__class__.__name__}: {exc}), skipping.")
            return newly_settled

        for market in markets:
            if market.ticker not in pending_set:
                continue
            result = market.raw.get("result")
            if result not in ("yes", "no"):
                continue  # "finalized" status but no result yet (shouldn't happen, but don't guess)

            actual_high_temp = None
            if result == "yes" and market.floor_strike is not None and market.cap_strike is not None:
                # A "between" bracket win pins the actual value to within half a
                # degree; a tail-bracket win (T-something) or a "no" result
                # doesn't give us a precise number from this one market alone —
                # actual_outcome (win/lose) is what matters most either way.
                actual_high_temp = (market.floor_strike + market.cap_strike) / 2

            with conn.cursor() as cur:
                cur.execute(
                    "update alerts set settled_at = %(settled_at)s, actual_outcome = %(outcome)s, "
                    "actual_high_temp = coalesce(%(temp)s, actual_high_temp) "
                    "where market_ticker = %(ticker)s and settled_at is null",
                    dict(
                        settled_at=datetime.now(UTC),
                        outcome=(result == "yes"),
                        temp=actual_high_temp,
                        ticker=market.ticker,
                    ),
                )
            conn.commit()
            print(f"  {market.ticker}: settled, result={result}")
            newly_settled += 1
            pending_set.discard(market.ticker)

        if not cursor or not pending_set:
            break

    return newly_settled


def main() -> int:
    load_dotenv()
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("DATABASE_URL not set — nothing to check.")
        return 0

    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute("select distinct market_ticker, series_ticker from alerts where settled_at is null")
        pending_by_series: dict[str, list[str]] = {}
        for market_ticker, series_ticker in cur.fetchall():
            pending_by_series.setdefault(series_ticker, []).append(market_ticker)

    total_pending = sum(len(tickers) for tickers in pending_by_series.values())
    if not pending_by_series:
        print("No pending alerts to check.")
        return 0

    print(f"Checking {total_pending} pending market(s) across {len(pending_by_series)} series...")
    settled_count = 0
    with track_run("mark_settled_alerts") as run, KalshiClient() as client, psycopg.connect(database_url) as conn:
        for series_ticker, tickers in pending_by_series.items():
            settled_count += check_and_mark_series(client, conn, series_ticker, tickers)
        run.summary = f"{settled_count} of {total_pending} pending market(s) newly settled"

    print(f"\n{settled_count} of {total_pending} pending market(s) newly settled.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
