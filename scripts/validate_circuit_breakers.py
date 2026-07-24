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

REWRITTEN 2026-07-23 for the Stage 3 circuit-breaker audit. The original
version of this script evaluated the consecutive-loss breaker only ONCE per
calendar day, at day rollover, using whatever streak state existed at that
exact moment. That silently MISSES a real activation: a losing streak that
crosses the threshold mid-day and is later broken by a win before that same
day's last trade never got evaluated at all, because the only check was at
day's end. Confirmed live against the current 121-trade paper history: the
old method found 1 trip for the 5%/3 config; the corrected per-trade
evaluation below (risk/circuit_breaker_report.py, which checks after EVERY
closed trade, not just at day boundaries) finds 4 real activations for the
same config on the same data — 3 of which the old method silently dropped.
This is very likely the actual root cause of DECISIONS.md's previously
reported non-monotonic trip counts (loose > moderate) — not a monotonicity
bug in the underlying breach predicates themselves, which
tests/test_circuit_breakers.py's TestMonotonicity class now proves directly.
"""

from __future__ import annotations

import argparse
import os
from typing import Optional

import psycopg
from dotenv import load_dotenv

from risk.circuit_breaker_report import IdentifiedTrade, build_activation_report, summarize_activations
from risk.circuit_breakers import DEFAULT_DAILY_LOSS_FRACTION, DEFAULT_MAX_CONSECUTIVE_LOSSES

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

    # $100 starting bankroll — this project's own paper_trading.STARTING_BANKROLL_USD
    # (not imported directly to avoid a circular/heavier dependency here; kept
    # as a literal with this note so the two don't silently drift unnoticed).
    starting_bankroll = 100.0

    identified_trades = [
        IdentifiedTrade(
            id=row["id"],
            market_ticker=row["market_ticker"],
            closed_at=row["closed_at"],
            realized_pnl=row["realized_pnl"],
            status=row["status"],
        )
        for row in trades_data
    ]
    config_name = f"{args.daily_fraction:.0%} daily / {args.max_consecutive} consecutive"
    activations = build_activation_report(
        identified_trades, config_name, args.daily_fraction, args.max_consecutive, starting_bankroll=starting_bankroll
    )
    counts = summarize_activations(activations)

    print("=" * 100)
    print("PER-ACTIVATION REPORT (one row per real breaker trip)")
    print("=" * 100)
    if not activations:
        print("No activations with these thresholds.")
    else:
        header = (
            f"{'Type':<17} {'Date':<12} {'Trade':<8} {'Bankroll before':>16} "
            f"{'Daily loss':>12} {'Streak':>7} {'Reason code':<24} Blocks new trades"
        )
        print(header)
        print("-" * len(header))
        for a in activations:
            trade_label = f"#{a.activating_trade_id}" if a.activating_trade_id is not None else "(day pool)"
            daily_loss_label = f"${a.daily_loss_at_activation:.2f}" if a.daily_loss_at_activation is not None else "—"
            streak_label = (
                str(a.consecutive_losses_at_activation) if a.consecutive_losses_at_activation is not None else "—"
            )
            print(
                f"{a.activation_type:<17} {a.activation_date:<12} {trade_label:<8} "
                f"${a.bankroll_before:>14.2f} {daily_loss_label:>12} {streak_label:>7} "
                f"{a.reason_code:<24} {'yes' if a.blocks_new_trades else 'no'}"
            )
    print()
    print(
        f"Unique affected days: {counts['unique_affected_days']}  |  "
        f"Daily-loss activations: {counts['daily_loss_activations']}  |  "
        f"Consecutive-loss activations: {counts['consecutive_loss_activations']}  |  "
        f"Unique trading halts: {counts['unique_trading_halts']}"
    )
    print()

    halts = counts["unique_trading_halts"]
    print("Threshold recommendations:")
    if halts == 0:
        print(f"  • These thresholds are very permissive (no halts on {len(identified_trades)} trades)")
        print("  • Consider tightening them (lower daily%, fewer consecutive losses)")
    elif halts < 3:
        print("  • Very few halts; thresholds may be too loose")
        print("  • Consider a 1-2% tighter daily limit or lower max_consecutive")
    elif halts > 10:
        print("  • Many halts; thresholds may be too tight")
        print("  • Consider a 1-2% looser daily limit or higher max_consecutive")
    else:
        print("  • Thresholds appear reasonable; monitor results")
    print()


if __name__ == "__main__":
    main()
