"""Marks alerts as settled once their underlying Kalshi market resolves.

Without this, dashboard/db.py's "latest alert per market, WHERE settled_at IS
NULL" filter has nothing to exclude — each day's alerts use distinct
market_tickers (KXHIGHNY-26JUL18-T79 vs ...-26JUL19-T79 are different rows,
not the same one overwritten), so without a settlement writeback, every past
day's alerts would keep showing up on the dashboard forever, not just today's.

Checks every still-open alert's market status via Kalshi and writes back
settled_at/actual_outcome/actual_high_temp once it's finalized. This is also
what will eventually let live alert performance be tracked over time,
independent of the one-off backtest.

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


def check_and_mark(client: KalshiClient, conn: psycopg.Connection, market_ticker: str) -> bool:
    """Returns True if this market was newly marked settled this call."""
    try:
        market = client.get_market(market_ticker)
    except Exception as exc:
        print(f"  {market_ticker}: couldn't fetch ({exc.__class__.__name__}: {exc}), skipping.")
        return False

    result = market.raw.get("result")
    if result not in ("yes", "no"):
        return False  # not settled yet

    actual_high_temp = None
    if result == "yes" and market.floor_strike is not None and market.cap_strike is not None:
        # A "between" bracket win pins the actual value to within half a degree;
        # a tail-bracket win (T-something) or a "no" result doesn't give us a
        # precise number from this one market alone — actual_outcome (win/lose)
        # is what matters most for tracking either way.
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
                ticker=market_ticker,
            ),
        )
    conn.commit()
    print(f"  {market_ticker}: settled, result={result}")
    return True


def main() -> int:
    load_dotenv()
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("DATABASE_URL not set — nothing to check.")
        return 0

    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute("select distinct market_ticker from alerts where settled_at is null")
        pending_tickers = [row[0] for row in cur.fetchall()]

    if not pending_tickers:
        print("No pending alerts to check.")
        return 0

    print(f"Checking {len(pending_tickers)} pending market(s)...")
    settled_count = 0
    with track_run("mark_settled_alerts") as run, KalshiClient() as client, psycopg.connect(database_url) as conn:
        for ticker in pending_tickers:
            if check_and_mark(client, conn, ticker):
                settled_count += 1
        run.summary = f"{settled_count} of {len(pending_tickers)} pending market(s) newly settled"

    print(f"\n{settled_count} of {len(pending_tickers)} pending market(s) newly settled.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
