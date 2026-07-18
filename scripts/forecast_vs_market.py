"""Live check: real Open-Meteo ensemble vs. real Kalshi prices, one city/event,
using the bias/std-corrected probability (see kalshi-backtest-findings memory).

This is a manual verification script, not the pipeline — it doesn't write to the
database or generate dashboard alerts (scripts/generate_alerts.py does that,
across all 6 cities). Useful for a quick single-city sanity check of what the
current calibrated model says without touching the database.

Usage: uv run scripts/forecast_vs_market.py [SERIES_TICKER]
"""

from __future__ import annotations

import sys

from dotenv import load_dotenv

from kalshi_client import KalshiClient, parse_event_date, taker_fee
from weather.open_meteo import fetch_daily_max_ensemble
from weather.probability import calibrated_probability_for_market, check_boundary_language, fit_normal
from weather.stations import get_station


def main() -> None:
    load_dotenv()
    series_ticker = sys.argv[1] if len(sys.argv) > 1 else "KXHIGHNY"
    station = get_station(series_ticker)

    with KalshiClient() as client:
        events, _ = client.get_events(series_ticker=series_ticker, status="open", limit=1)
        if not events:
            print(f"No open events for {series_ticker} right now.")
            return
        event = events[0]
        markets, _ = client.get_markets(event_ticker=event.event_ticker, limit=50)

    event_date = parse_event_date(event.event_ticker)
    print(f"{station.city}: {event.title!r} ({event_date})")

    ensemble = fetch_daily_max_ensemble(
        station.latitude, station.longitude, station.standard_time_timezone, forecast_days=3
    )
    members = ensemble.get(event_date.isoformat())
    if not members:
        print(f"No ensemble forecast for {event_date} yet (too far out) — try again closer to the date.")
        return

    mean, std = fit_normal(members)
    print(f"Ensemble: n={len(members)} mean={mean:.2f}°F std={std:.2f}°F\n")

    print(f"{'bracket':<10} {'model':>8} {'market':>8} {'edge':>8} {'fee':>7}  rules")
    for market in sorted(markets, key=lambda m: m.floor_strike or float("-inf")):
        try:
            check_boundary_language(market.rules_primary, market.floor_strike, market.cap_strike)
        except ValueError as exc:
            print(f"  WARNING: {market.ticker}: {exc}")

        model_probability = calibrated_probability_for_market(
            series_ticker, market.rules_primary, market.floor_strike, market.cap_strike, mean, event_date.month
        )
        if market.yes_bid_dollars is None or market.yes_ask_dollars is None:
            continue
        market_price = (market.yes_bid_dollars + market.yes_ask_dollars) / 2
        edge = model_probability - market_price
        fee = taker_fee(market_price)

        label = market.ticker.rsplit("-", 1)[-1]
        print(
            f"{label:<10} {model_probability * 100:7.1f}% {market_price * 100:7.1f}% "
            f"{edge * 100:+7.1f}% {fee * 100:6.1f}%  {market.rules_primary[:60]}..."
        )


if __name__ == "__main__":
    main()
