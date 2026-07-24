"""Cross-bracket consistency: for each historical settled event with 2+
brackets, checks whether Kalshi's own event-level pricing was internally
consistent — bracket YES prices should sum to ~1.0, since exactly one
bracket resolves YES — and if not, whether the deviation would have cleared
taker fees on every leg.

edge.calculator.bracket_sum_deviation already computes this deviation and is
already live in generate_alerts.py/the dashboard as a diagnostic; this is the
first time it gets backtested as a strategy rather than just displayed.

IMPORTANT CAVEAT, same "don't fabricate fills" discipline as the same-day-proof
work earlier in this project: each bracket's `last_price` here is that
market's own FINAL traded price before settlement, not a synchronized
snapshot of every bracket at one instant. A deviation computed from these
prices does NOT prove all legs were simultaneously fillable at those exact
prices — it's the best approximation this project's historical data supports.
Read the numbers below as "how far apart were these brackets' own most
recent trades," not "here is a captured arbitrage." Confirming a real fill
would need order-book depth at one instant, which this project has never
had (and never places real orders against — see DECISIONS.md).

Usage: uv run scripts/analyze_cross_bracket_consistency.py [SERIES_TICKER ...]
"""

from __future__ import annotations

import sys
from collections import defaultdict
from datetime import date, timedelta

from dotenv import load_dotenv

from backtest.cache import cached_collect_rows
from edge.calculator import bracket_sum_deviation
from kalshi_client import KalshiClient
from kalshi_client.fees import taker_fee
from weather.stations import STATIONS

START_DATE = "2024-10-01"
END_DATE = (date.today() - timedelta(days=1)).isoformat()

_DEFAULT_CITIES = ["KXHIGHNY", "KXHIGHCHI", "KXHIGHPHIL", "KXHIGHAUS", "KXHIGHDEN", "KXHIGHMIA"]

# Kalshi's own price ticks are 1c — a deviation smaller than this is tick
# noise, not a real signal worth costing fees out on.
_MIN_DEVIATION_TO_ANALYZE = 0.01


def _event_fee_cost(prices: list[float], deviation: float) -> float:
    """Total taker fee across every leg of the arbitrage the deviation
    implies: buy NO on every bracket when the sum is over 1.0 (each leg
    priced at 1 - yes_price), buy YES on every bracket when it's under
    (each leg priced at yes_price itself) — the two possible directions
    bracket_sum_deviation can point in."""
    if deviation > 0:
        return sum(taker_fee(1 - p) for p in prices)
    return sum(taker_fee(p) for p in prices)


def main() -> None:
    load_dotenv()
    cities = sys.argv[1:] or _DEFAULT_CITIES

    total_events = 0
    events_with_deviation = 0
    events_net_profitable = 0

    with KalshiClient() as client:
        for series_ticker in cities:
            station = STATIONS[series_ticker]
            print(f"\n=== {station.city} ({series_ticker}) ===")
            rows = cached_collect_rows(client, series_ticker, START_DATE, END_DATE, lead_days=1)
            if not rows:
                print("  no usable rows, skipping.")
                continue

            by_event: dict[str, list] = defaultdict(list)
            for r in rows:
                by_event[r.event_ticker].append(r)

            city_events = 0
            city_flagged = 0
            city_net_profitable = 0
            for event_ticker, event_rows in by_event.items():
                prices = [r.last_price for r in event_rows if r.last_price is not None]
                if len(prices) < 2 or len(prices) != len(event_rows):
                    # Every bracket needs a real last_price to sum -- a
                    # missing leg makes the deviation meaningless, not just
                    # imprecise, so the whole event is skipped rather than
                    # summed over a partial bracket set.
                    continue
                city_events += 1
                deviation = bracket_sum_deviation(prices)
                if abs(deviation) < _MIN_DEVIATION_TO_ANALYZE:
                    continue
                city_flagged += 1
                gross = abs(deviation)
                fees = _event_fee_cost(prices, deviation)
                net = gross - fees
                if net > 0:
                    city_net_profitable += 1
                print(
                    f"  {event_ticker}: {len(prices)} brackets, sum deviation {deviation:+.4f}, "
                    f"gross {gross:.4f}, fees {fees:.4f}, net {net:+.4f}"
                    f"{' (net profitable IF fillable)' if net > 0 else ''}"
                )

            total_events += city_events
            events_with_deviation += city_flagged
            events_net_profitable += city_net_profitable
            print(
                f"  {city_events} events with every bracket priced; "
                f"{city_flagged} had |deviation| >= {_MIN_DEVIATION_TO_ANALYZE}"
            )

    print(f"\n=== SUMMARY across {len(cities)} cities ===")
    print(f"  {total_events} events had every bracket priced")
    print(f"  {events_with_deviation} had a deviation of at least {_MIN_DEVIATION_TO_ANALYZE}")
    print(
        f"  {events_net_profitable} of those would have been net profitable after fees on every "
        "leg IF every leg was simultaneously fillable at its recorded last_price"
    )
    print(
        "\n  CAVEAT: last_price is each bracket's own final traded price, not a synchronized "
        "snapshot across the event at one instant. This does not confirm real arbitrage — only "
        "that Kalshi's own historical closing prices, taken independently per bracket, were "
        "internally inconsistent by this much."
    )


if __name__ == "__main__":
    main()
