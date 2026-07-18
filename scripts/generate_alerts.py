"""Generate alerts for all in-scope cities from live data.

Fetches live ensemble forecasts and Kalshi prices, computes model probability and
edge per bracket (steps 2+3), and either inserts into Postgres (if DATABASE_URL is
set) or prints a table. This is the piece a future scheduler (build step 6) would
run on a cadence — running it manually today produces real rows without the
scheduler built yet.

Uses the bias/std-corrected probability (weather.probability.calibrated_*),
fit from the 2026-07-17 backtest against ~600 real settled days per city — see
kalshi-backtest-findings memory. Every row is still written with
calibration_validated=False: the correction measurably improved Brier score in
backtesting, but hasn't been checked against an independent ground truth (NOAA
CDO) yet, and higher-confidence predictions (40%+) remain thinly sampled.
Nothing here should be read as a trading signal without that context.

Usage: uv run scripts/generate_alerts.py
"""

from __future__ import annotations

import os

from edge.calculator import bracket_sum_deviation, compute_edge
from kalshi_client import KalshiClient, market_url, parse_event_date
from weather.open_meteo import fetch_daily_max_ensemble
from weather.probability import calibrated_probability_for_market, check_boundary_language, fit_normal
from weather.stations import STATIONS


def build_alert_rows(client: KalshiClient, series_ticker: str) -> list[dict]:
    station = STATIONS[series_ticker]
    series = client.get_series(series_ticker)
    events, _ = client.get_events(series_ticker=series_ticker, status="open", limit=1)
    if not events:
        print(f"{station.city}: no open event, skipping.")
        return []
    event = events[0]
    event_date = parse_event_date(event.event_ticker)

    ensemble = fetch_daily_max_ensemble(
        station.latitude, station.longitude, station.standard_time_timezone, forecast_days=3
    )
    members = ensemble.get(event_date.isoformat())
    if not members:
        print(f"{station.city}: no ensemble forecast for {event_date} yet, skipping.")
        return []
    mean, std = fit_normal(members)

    markets, _ = client.get_markets(event_ticker=event.event_ticker, limit=50)
    kalshi_link = market_url(series_ticker, series.title, event.event_ticker)

    rows: list[dict] = []
    market_prices: list[float] = []
    for market in markets:
        if market.yes_bid_dollars is None or market.yes_ask_dollars is None:
            continue
        try:
            check_boundary_language(market.rules_primary, market.floor_strike, market.cap_strike)
        except ValueError as exc:
            print(f"  WARNING: {market.ticker}: {exc} — skipping this bracket.")
            continue

        model_probability = calibrated_probability_for_market(
            series_ticker,
            market.rules_primary,
            market.floor_strike,
            market.cap_strike,
            mean,
            validate_rules_text=False,
        )
        market_price = round((market.yes_bid_dollars + market.yes_ask_dollars) / 2, 4)
        market_prices.append(market_price)
        edge_result = compute_edge(model_probability, market_price)

        rows.append(
            dict(
                series_ticker=series_ticker,
                event_ticker=event.event_ticker,
                market_ticker=market.ticker,
                city=station.city,
                bracket_label=market.bracket_label,
                floor_strike=market.floor_strike,
                cap_strike=market.cap_strike,
                model_probability=model_probability,
                ensemble_mean=mean,
                ensemble_std=std,
                model_version="normal-v2-bias-corrected",
                calibration_validated=False,
                market_yes_price=market_price,
                edge=edge_result.edge,
                fee_adjusted_threshold=edge_result.threshold,
                rules_primary=market.rules_primary,
                rules_secondary=market.rules_secondary or None,
                kalshi_url=kalshi_link,
                is_actionable=edge_result.is_actionable,
                status="open",
            )
        )

    deviation = bracket_sum_deviation(market_prices)
    print(f"{station.city}: {len(rows)} brackets, bracket-sum deviation from 1.0: {deviation:+.3f}")
    return rows


def insert_rows(database_url: str, rows: list[dict]) -> None:
    import psycopg

    if not rows:
        return
    columns = list(rows[0].keys())
    placeholders = ", ".join(f"%({c})s" for c in columns)
    query = f"insert into alerts ({', '.join(columns)}) values ({placeholders})"
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.executemany(query, rows)
        conn.commit()


def main() -> None:
    database_url = os.environ.get("DATABASE_URL")
    all_rows: list[dict] = []

    with KalshiClient() as client:
        for series_ticker in STATIONS:
            all_rows.extend(build_alert_rows(client, series_ticker))

    actionable = [row for row in all_rows if row["is_actionable"]]
    print(f"\n{len(all_rows)} total brackets, {len(actionable)} actionable (unvalidated model).")

    if database_url:
        insert_rows(database_url, all_rows)
        print(f"Inserted {len(all_rows)} rows into `alerts`.")
    else:
        print("DATABASE_URL not set — not writing anywhere. Top actionable brackets:")
        for row in sorted(actionable, key=lambda r: -abs(r["edge"]))[:10]:
            print(
                f"  {row['city']:<14} {row['bracket_label']:<10} "
                f"model={row['model_probability'] * 100:5.1f}% "
                f"market={row['market_yes_price'] * 100:5.1f}% "
                f"edge={row['edge'] * 100:+6.1f}%"
            )


if __name__ == "__main__":
    main()
