#!/usr/bin/env python3
"""Validate circuit-breaker thresholds against historical paper-trading data.

Runs the circuit-breaker logic against real paper_trades to show what would have
tripped in the past, helping inform threshold choices.

Usage:
    uv run scripts/validate_circuit_breakers.py [--daily-fraction FRAC] [--max-consecutive N]

Examples:
    # Test with 5% daily loss limit and 3 consecutive losses
    uv run scripts/validate_circuit_breakers.py --daily-fraction 0.05 --max-consecutive 3

    # Test a tighter 2% daily limit
    uv run scripts/validate_circuit_breakers.py --daily-fraction 0.02 --max-consecutive 5
"""

from __future__ import annotations

import argparse
import os
from typing import Optional

import psycopg
from dotenv import load_dotenv

from risk.circuit_breakers import (
    DEFAULT_DAILY_LOSS_FRACTION,
    DEFAULT_MAX_CONSECUTIVE_LOSSES,
    Trade,
    circuit_breaker_verdict,
)

load_dotenv()


def fetch_paper_trades() -> Optional[list[dict]]:
    """Fetch all closed paper trades from the database."""
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("ERROR: DATABASE_URL not set")
        return None

    try:
        with psycopg.connect(database_url, connect_timeout=5) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        id, closed_at, realized_pnl, status, close_reason,
                        opened_at, market_ticker
                    FROM paper_trades
                    WHERE status = 'closed'
                    ORDER BY closed_at ASC
                    """
                )
                rows = cur.fetchall()
                columns = [desc.name for desc in cur.description]
                return [dict(zip(columns, row)) for row in rows]
    except Exception as e:
        print(f"ERROR fetching trades: {e}")
        return None


def main():
    parser = argparse.ArgumentParser(
        description="Validate circuit-breaker thresholds against historical data"
    )
    parser.add_argument(
        "--daily-fraction",
        type=float,
        default=DEFAULT_DAILY_LOSS_FRACTION,
        help=(
            f"Daily loss limit as a fraction of bankroll "
            f"(default: {DEFAULT_DAILY_LOSS_FRACTION:.0%})"
        ),
    )
    parser.add_argument(
        "--max-consecutive",
        type=int,
        default=DEFAULT_MAX_CONSECUTIVE_LOSSES,
        help=(
            f"Max consecutive losses before breach "
            f"(default: {DEFAULT_MAX_CONSECUTIVE_LOSSES})"
        ),
    )
    args = parser.parse_args()

    print("=" * 100)
    print("CIRCUIT BREAKER VALIDATION AGAINST HISTORICAL DATA")
    print("=" * 100)
    print(f"Daily loss limit: {args.daily_fraction * 100:.1f}% of bankroll")
    print(f"Max consecutive losses: {args.max_consecutive}")
    print()

    trades_data = fetch_paper_trades()
    if not trades_data:
        return

    print(f"Loaded {len(trades_data)} closed trades")
    print()

    # Convert to Trade objects, sorted by date
    trades = [
        Trade(
            closed_at=row["closed_at"],
            realized_pnl=row["realized_pnl"],
            status=row["status"],
        )
        for row in trades_data
    ]

    # Compute starting bankroll from first trade's cost basis
    # (rough estimate: assume starting with $100)
    bankroll = 100.0
    trip_events = []

    print("Simulating trading day-by-day:")
    print("-" * 100)

    current_date = None
    today_trades = []

    for i, trade in enumerate(trades):
        trade_date = trade.closed_at.date() if trade.closed_at else None

        # Date rollover: check breakers and reset daily trades
        if trade_date != current_date:
            if today_trades and current_date is not None:
                # Check breakers at end of day
                breached, reason = circuit_breaker_verdict(
                    today_trades,
                    trades[:i],
                    bankroll,
                    args.daily_fraction,
                    args.max_consecutive,
                )
                if breached:
                    daily_pnl = sum(t.realized_pnl for t in today_trades)
                    trip_events.append(
                        {
                            "date": current_date,
                            "trade_count": len(today_trades),
                            "daily_pnl": daily_pnl,
                            "bankroll": bankroll,
                            "reason": reason,
                            "trade_index": i - 1,
                        }
                    )
                    print(
                        f"  {current_date}: BREACHED after {len(today_trades)} trade(s), "
                        f"P&L=${daily_pnl:.2f}, bankroll=${bankroll:.2f}"
                    )
                    print(f"    Reason: {reason}")

            current_date = trade_date
            today_trades = []

        # Update bankroll cumulatively
        bankroll += trade.realized_pnl
        today_trades.append(trade)

    # Final day
    if today_trades and current_date is not None:
        breached, reason = circuit_breaker_verdict(
            today_trades,
            trades,
            bankroll,
            args.daily_fraction,
            args.max_consecutive,
        )
        if breached:
            daily_pnl = sum(t.realized_pnl for t in today_trades)
            trip_events.append(
                {
                    "date": current_date,
                    "trade_count": len(today_trades),
                    "daily_pnl": daily_pnl,
                    "bankroll": bankroll,
                    "reason": reason,
                    "trade_index": len(trades) - 1,
                }
            )
            print(
                f"  {current_date}: BREACHED after {len(today_trades)} trade(s), "
                f"P&L=${daily_pnl:.2f}, bankroll=${bankroll:.2f}"
            )
            print(f"    Reason: {reason}")

    print()
    print("=" * 100)
    print("SUMMARY")
    print("=" * 100)
    print(f"Total trips: {len(trip_events)}")
    print(f"Final bankroll: ${bankroll:.2f}")
    print()

    if trip_events:
        print("Trip events (in order):")
        for i, event in enumerate(trip_events, 1):
            print(
                f"  {i}. {event['date']}: {event['reason']} "
                f"(${event['daily_pnl']:.2f} / {event['trade_count']} trades)"
            )
    else:
        print("No circuit-breaker trips with these thresholds.")

    print()
    print("Threshold recommendations:")
    if len(trip_events) == 0:
        print("  • These thresholds are very permissive (no trips on 82 trades)")
        print("  • Consider tightening them (lower daily%, fewer consecutive losses)")
    elif len(trip_events) < 3:
        print("  • Very few trips; thresholds may be too loose")
        print("  • Consider a 1-2% tighter daily limit or lower max_consecutive")
    elif len(trip_events) > 10:
        print("  • Many trips; thresholds may be too tight")
        print("  • Consider a 1-2% looser daily limit or higher max_consecutive")
    else:
        print("  • Thresholds appear reasonable; monitor results")

    print()


if __name__ == "__main__":
    main()
