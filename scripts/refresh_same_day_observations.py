"""Refresh same-day (lead_days=0) alerts on a tight cadence, without the
expensive Open-Meteo ensemble refetch — the mechanical fix for a real,
measured latency gap.

`scripts/measure_pipeline_latency.py` (2026-07-20) found that generate_alerts.py's
only cadence (0 5,11,17,23 UTC — every 6 hours) leaves a same-day bracket's
`observed_so_far` up to ~3-6 hours stale relative to a live decision instant,
and that this staleness has a measurable cost concentrated in the afternoon:
NYC's 15:00 model Brier went from 0.0790 (using the truly current
observation) to 0.1034 (using whatever a 6-hourly cron last saw) — a real,
recoverable degradation, not a modeling problem. The live NWS feed itself is
not the bottleneck (confirmed live: a fresh METAR is available within
minutes of being taken); the bottleneck is purely how often this project
looks at it.

This does NOT replace generate_alerts.py or duplicate its expensive work.
`fetch_daily_ensemble` (the Open-Meteo call) only needs the slow cadence —
the ensemble forecast itself doesn't change meaningfully in 15 minutes. What
DOES need refreshing every few minutes is the two cheap inputs:
`fetch_today_extreme` (one NWS call per city) and current market prices (one
Kalshi call per event). So this script reuses the LATEST known `ensemble_mean`/
`ensemble_std` already sitting in the `alerts` table from generate_alerts.py's
last real run, refetches only the cheap same-day observation and current
prices, and re-prices through `scripts.generate_alerts.price_bracket` — the
exact same pricing function generate_alerts.py itself uses, so a bracket
priced here can never silently drift from how the slow pipeline would have
priced it.

Deliberately scoped to lead_days=0 only. Day-ahead (lead_days=1) brackets have
nothing to condition on yet regardless of how often this runs — the whole
premise of the latency finding this exists to close is specific to same-day
observation conditioning.

Not part of run_pipeline.sh or run_settlement_cycle.sh — see
scheduler/run_observation_refresh.sh for why this gets its own cadence rather
than folding into either existing one.

Usage: uv run scripts/refresh_same_day_observations.py
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from kalshi_client import KalshiClient, parse_event_date
from monitoring import track_run
from scripts.generate_alerts import insert_rows, price_bracket
from weather.nws_observations import fetch_today_extreme
from weather.stations import STATIONS


def fetch_known_pricing_inputs(
    cur, series_ticker: str
) -> dict[str, tuple[float, float, str, datetime | None, datetime | None]]:
    """Latest known (ensemble_mean, ensemble_std, kalshi_url) per market_ticker,
    from the most recent unsettled lead_days=0 alert row for that market --
    this is what lets the refresh skip both the expensive Open-Meteo refetch
    AND an extra get_series() call. kalshi_url is static per event, so
    reusing it is not just cheaper than re-deriving it via market_url() but
    also safer: that function needs the series' real title to build a correct
    slug, which isn't cheaply available here without another API round trip.

    A market absent from this dict has never been priced by a real
    generate_alerts.py run yet (or has already settled), and is deliberately
    left alone: the next slow-pipeline run establishes its baseline, not this
    script.
    """
    cur.execute(
        "select distinct on (market_ticker) market_ticker, ensemble_mean, ensemble_std, "
        "kalshi_url, forecast_run_time, forecast_availability_time "
        "from alerts where series_ticker = %s and lead_days = 0 and settled_at is null "
        "order by market_ticker, created_at desc",
        (series_ticker,),
    )
    return {
        ticker: (mean, std, url, run_time, availability_time)
        for ticker, mean, std, url, run_time, availability_time in cur.fetchall()
    }


def refresh_city(client: KalshiClient, cur, series_ticker: str) -> list[dict]:
    """Re-prices every currently-open, already-known lead_days=0 bracket for
    one city with a fresh observation and fresh prices. Returns the new rows
    (empty if nothing to refresh yet, e.g. generate_alerts.py hasn't run for
    today's event at all so far)."""
    station = STATIONS[series_ticker]
    known = fetch_known_pricing_inputs(cur, series_ticker)
    if not known:
        return []

    local_today = datetime.now(UTC).astimezone(ZoneInfo(station.standard_time_timezone)).date()

    events, _ = client.get_events(series_ticker=series_ticker, status="open", limit=10)
    today_event = None
    for event in events:
        try:
            if parse_event_date(event.event_ticker) == local_today:
                today_event = event
                break
        except ValueError:
            continue
    if today_event is None:
        return []

    observation = fetch_today_extreme(station.nws_station_id, station.metric, station.standard_time_timezone)
    observed_so_far = observation[0] if observation is not None else None
    observation_event_time = None
    observation_received_time = None
    if observation is not None:
        observation_event_time = datetime.fromisoformat(
            observation[1].replace("Z", "+00:00")
        )
        observation_received_time = datetime.now(UTC)
        print(f"{station.city}: observed {station.metric} so far today {observed_so_far:.1f}F (at {observation[1]})")

    markets, _ = client.get_markets(event_ticker=today_event.event_ticker, limit=50)

    rows: list[dict] = []
    for market in markets:
        if market.ticker not in known:
            # Not priced by a real generate_alerts.py run yet -- nothing to
            # refresh from; let the slow pipeline establish this one first.
            continue
        (
            ensemble_mean,
            ensemble_std,
            kalshi_link,
            forecast_run_time,
            forecast_availability_time,
        ) = known[market.ticker]
        row = price_bracket(
            series_ticker=series_ticker,
            city=station.city,
            event=today_event,
            market=market,
            ensemble_mean=ensemble_mean,
            ensemble_std=ensemble_std,
            event_date=local_today,
            lead_days=0,
            metric=station.metric,
            observed_so_far=observed_so_far,
            kalshi_url=kalshi_link,
            forecast_run_time=forecast_run_time,
            forecast_availability_time=forecast_availability_time,
            observation_event_time=observation_event_time,
            observation_publication_time=None,
            observation_collector_received_time=observation_received_time,
        )
        if row is not None:
            rows.append(row)
    return rows


def main() -> int:
    load_dotenv()
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("DATABASE_URL not set — nothing to refresh against.")
        return 0

    import psycopg

    all_rows: list[dict] = []
    failed_cities: list[str] = []

    with track_run("refresh_same_day_observations") as run, KalshiClient() as client, psycopg.connect(database_url) as conn:
        with conn.cursor() as cur:
            for series_ticker in STATIONS:
                try:
                    all_rows.extend(refresh_city(client, cur, series_ticker))
                except Exception as exc:  # noqa: BLE001 - one city's hiccup must not sink the rest
                    failed_cities.append(series_ticker)
                    print(f"  ERROR: {series_ticker} failed: {exc.__class__.__name__}: {exc}")

        print(f"\n{len(all_rows)} bracket(s) refreshed with a fresh observation/price.")
        if failed_cities:
            print(f"Cities that errored this run: {', '.join(failed_cities)}")

        if all_rows:
            insert_rows(database_url, "alerts", all_rows)
            print(f"Inserted {len(all_rows)} rows into `alerts`.")

        run.summary = f"{len(all_rows)} brackets refreshed, {len(failed_cities)} cities errored"
        return 1 if failed_cities and not all_rows else 0


if __name__ == "__main__":
    sys.exit(main())
