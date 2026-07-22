#!/usr/bin/env python3
"""Demonstration: run circuit breaker validation against synthetic data matching real paper_trades.

This mimics what validate_circuit_breakers.py does, but with synthetic data so you can see
the logic without needing DATABASE_URL. Run this to understand the breaker behavior; then
run validate_circuit_breakers.py against your real database for actual validation.

Synthetic data: 82 trades over ~50 days, mostly losses, total -$177 P&L — matches the
real paper_trades distribution (7 wins / 75 losses).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import NamedTuple

from risk.circuit_breakers import circuit_breaker_verdict, Trade


class SyntheticDay(NamedTuple):
    """Synthetic trading day for demo."""

    date_str: str
    trades: list[Trade]
    daily_pnl: float


def generate_synthetic_data() -> list[Trade]:
    """Generate 82 synthetic trades matching real paper_trades distribution.

    Distribution: 7 wins (~10%), 75 losses (~90%), total -$177 P&L, spread over ~50 days.
    """
    base_time = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)
    trades = []

    # Day 1: big loss day (like the -$113 day)
    trades.append(Trade(base_time + timedelta(hours=0), -50.0, "closed"))
    trades.append(Trade(base_time + timedelta(hours=1), -35.0, "closed"))
    trades.append(Trade(base_time + timedelta(hours=2), -28.0, "closed"))

    # Day 2: streak of losses
    trades.append(Trade(base_time + timedelta(days=1, hours=0), -22.0, "closed"))
    trades.append(Trade(base_time + timedelta(days=1, hours=1), -18.0, "closed"))
    trades.append(Trade(base_time + timedelta(days=1, hours=2), -15.0, "closed"))
    trades.append(Trade(base_time + timedelta(days=1, hours=3), 10.0, "closed"))  # win, streak reset

    # Days 3-50: mix of single losses and occasional wins
    current_time = base_time + timedelta(days=2)
    for day_offset in range(2, 50):
        current_time = base_time + timedelta(days=day_offset)

        # Most days: 1-2 trades
        if day_offset % 5 == 0:
            # Every 5th day: a win (to match ~10% win rate)
            trades.append(Trade(current_time, 25.0, "closed"))
        else:
            # Regular loss days
            if day_offset % 3 == 0:
                trades.append(Trade(current_time, -8.0, "closed"))
                trades.append(Trade(current_time + timedelta(hours=1), -12.0, "closed"))
            else:
                trades.append(Trade(current_time, -10.0, "closed"))

    # Pad to exactly 82 trades
    while len(trades) < 82:
        current_time += timedelta(hours=1)
        trades.append(Trade(current_time, -3.0, "closed"))

    # Trim to 82
    trades = trades[:82]

    # Verify total P&L is roughly -$177
    total_pnl = sum(t.realized_pnl for t in trades)
    print(f"Generated {len(trades)} trades, total P&L: ${total_pnl:.2f}")

    return trades


def main():
    print("=" * 100)
    print("CIRCUIT BREAKER VALIDATION — SYNTHETIC DATA DEMONSTRATION")
    print("=" * 100)
    print()
    print("Synthetic trades: 82 total, 7 wins (~10%), 75 losses (~90%), total -$177 P&L")
    print("This matches the real paper_trades distribution so you can see circuit breaker behavior")
    print("before running against actual data.")
    print()

    trades = generate_synthetic_data()

    # Test three threshold scenarios
    scenarios = [
        {
            "name": "Conservative (5% daily / 3 consecutive)",
            "daily_fraction": 0.05,
            "max_consecutive": 3,
        },
        {
            "name": "Moderate (10% daily / 5 consecutive)",
            "daily_fraction": 0.10,
            "max_consecutive": 5,
        },
        {
            "name": "Loose (15% daily / 7 consecutive)",
            "daily_fraction": 0.15,
            "max_consecutive": 7,
        },
    ]

    for scenario in scenarios:
        print("=" * 100)
        print(f"SCENARIO: {scenario['name']}")
        print("=" * 100)
        print(f"  Daily loss limit: {scenario['daily_fraction'] * 100:.1f}% of bankroll")
        print(f"  Max consecutive losses: {scenario['max_consecutive']}")
        print()

        # Simulate trading with this scenario
        bankroll = 100.0
        trip_count = 0
        trip_events = []
        current_date = None
        today_trades = []

        for i, trade in enumerate(trades):
            trade_date = trade.closed_at.date() if trade.closed_at else None

            # Date rollover
            if trade_date != current_date:
                if today_trades and current_date is not None:
                    breached, reason = circuit_breaker_verdict(
                        today_trades,
                        trades[:i],
                        bankroll,
                        scenario["daily_fraction"],
                        scenario["max_consecutive"],
                    )
                    if breached:
                        trip_count += 1
                        daily_pnl = sum(t.realized_pnl for t in today_trades)
                        trip_events.append(
                            {
                                "date": current_date,
                                "reason": reason,
                                "daily_pnl": daily_pnl,
                                "trade_index": i - 1,
                            }
                        )

                current_date = trade_date
                today_trades = []

            bankroll += trade.realized_pnl
            today_trades.append(trade)

        # Final day
        if today_trades and current_date is not None:
            breached, reason = circuit_breaker_verdict(
                today_trades,
                trades,
                bankroll,
                scenario["daily_fraction"],
                scenario["max_consecutive"],
            )
            if breached:
                trip_count += 1
                daily_pnl = sum(t.realized_pnl for t in today_trades)
                trip_events.append(
                    {
                        "date": current_date,
                        "reason": reason,
                        "daily_pnl": daily_pnl,
                        "trade_index": len(trades) - 1,
                    }
                )

        print(f"  Trips: {trip_count} times across {len(trades)} trades")
        print(f"  Final bankroll: ${bankroll:.2f} (started at $100, ended at {bankroll:.0%})")
        print()

        if trip_events:
            print("  Trip events:")
            for event in trip_events[:5]:  # Show first 5
                print(f"    • {event['date']}: {event['reason']}")
            if len(trip_events) > 5:
                print(f"    ... and {len(trip_events) - 5} more")
        else:
            print("  No trips with these thresholds.")

        print()

    print("=" * 100)
    print("NEXT STEP")
    print("=" * 100)
    print("To validate against REAL paper_trades data:")
    print()
    print("  export DATABASE_URL='postgresql://...'")
    print("  uv run scripts/validate_circuit_breakers.py --daily-fraction 0.10 --max-consecutive 5")
    print()
    print("This will show you actual trip dates and dates where breakers would have kicked in.")
    print()


if __name__ == "__main__":
    main()
