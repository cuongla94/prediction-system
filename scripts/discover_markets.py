"""Manual smoke test against live Kalshi public endpoints.

Resolves the NYC daily-high-temperature series, finds the next open event, and
prints each bracket's rules text and current price. Uses no credentials — series/
event/market discovery is public.

Usage: uv run scripts/discover_markets.py [SERIES_TICKER]
"""

from __future__ import annotations

import sys

from kalshi_client import KalshiClient


def main() -> None:
    series_ticker = sys.argv[1] if len(sys.argv) > 1 else "KXHIGHNY"

    with KalshiClient() as client:
        series = client.get_series(series_ticker)
        print(f"Series: {series.ticker} — {series.title!r}")
        print(f"  category={series.category!r} frequency={series.frequency!r}")
        print(f"  settlement_sources={series.settlement_sources}")

        events, _ = client.get_events(series_ticker=series_ticker, status="open", limit=5)
        if not events:
            print(f"\nNo open events for {series_ticker} right now.")
            return

        event = events[0]
        print(f"\nEvent: {event.event_ticker} — {event.title!r} (strike_date={event.strike_date})")

        markets, _ = client.get_markets(event_ticker=event.event_ticker, limit=50)
        print(f"\n{len(markets)} bracket(s):")
        for market in sorted(markets, key=lambda m: m.floor_strike or float("-inf")):
            print(f"\n  {market.ticker}  [{market.status}]")
            print(
                f"    yes_bid={market.yes_bid_dollars}  yes_ask={market.yes_ask_dollars}  "
                f"last={market.last_price_dollars}"
            )
            print(f"    floor_strike={market.floor_strike}  cap_strike={market.cap_strike}")
            print(f"    rules_primary: {market.rules_primary}")
            if market.rules_secondary:
                print(f"    rules_secondary: {market.rules_secondary}")


if __name__ == "__main__":
    main()
