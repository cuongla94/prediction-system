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

As of 2026-07-19, generates one alert per bracket per currently-open event —
today's (lead_days=0) and tomorrow's (lead_days=1), not just whichever's
soonest. Only lead_days=1 matches the forecast lead time the calibration
above was actually fit against; lead_days=0 uses the same calibration as a
best-available approximation, not a separately validated one, and should be
treated as lower-confidence (the dashboard labels it as such).

Usage: uv run scripts/generate_alerts.py
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from edge.calculator import bracket_sum_deviation, compute_edge
from kalshi_client import Event, KalshiClient, Market, market_url, parse_event_date
from monitoring import track_run
from weather.calibration_params import get_calibration
from weather.nws_observations import fetch_today_extreme
from weather.open_meteo import fetch_daily_ensemble
from weather.probability import (
    calibrated_observation_conditioned_probability,
    check_boundary_language,
    fit_normal,
)
from weather.stations import STATIONS


def price_bracket(
    *,
    series_ticker: str,
    city: str,
    event: Event,
    market: Market,
    ensemble_mean: float,
    ensemble_std: float,
    event_date: date,
    lead_days: int,
    metric: str,
    observed_so_far: float | None,
    kalshi_url: str,
    forecast_run_time: datetime | None = None,
    forecast_availability_time: datetime | None = None,
    observation_event_time: datetime | None = None,
    observation_publication_time: datetime | None = None,
    observation_collector_received_time: datetime | None = None,
) -> dict | None:
    """Prices one bracket into a full `alerts` row dict, or None if it should
    be skipped (no bid/ask yet, or the rules text doesn't match the strikes).

    Split out of build_alert_rows() 2026-07-20 so
    scripts/refresh_same_day_observations.py can reuse the EXACT same pricing
    logic on a much tighter cadence without duplicating it — see that
    script's own docstring for why a separate, tighter-cadence refresh exists
    at all (measured, not assumed: scripts/measure_pipeline_latency.py found
    a real, measurable Brier cost from this project's 6-hourly cadence,
    concentrated at the decision times furthest past the last cron tick).
    Keeping one function here is what stops the two callers from silently
    drifting apart on how a bracket gets priced.

    `observed_so_far` is the day's raw observed extreme (or None) — the
    lead_days==0-only gating that decides whether it's actually used stays
    here, unchanged from before the extraction, so a lead_days=1 (or later)
    call is guaranteed to price identically to before regardless of what its
    caller passes for `observed_so_far`.
    """
    if market.yes_bid_dollars is None or market.yes_ask_dollars is None:
        return None
    try:
        check_boundary_language(market.rules_primary, market.floor_strike, market.cap_strike)
    except ValueError as exc:
        print(f"  WARNING: {market.ticker}: {exc} — skipping this bracket.")
        return None

    # Only today's event can have an observation to condition on; anything
    # further out prices exactly as before regardless of what was passed in.
    bracket_observation = observed_so_far if lead_days == 0 else None
    model_probability = calibrated_observation_conditioned_probability(
        series_ticker,
        ensemble_mean,
        market.floor_strike,
        market.cap_strike,
        event_date.month,
        metric,
        bracket_observation,
    )
    market_price = round((market.yes_bid_dollars + market.yes_ask_dollars) / 2, 4)
    edge_result = compute_edge(model_probability, market_price)

    return dict(
        series_ticker=series_ticker,
        event_ticker=event.event_ticker,
        market_ticker=market.ticker,
        city=city,
        bracket_label=market.bracket_label,
        floor_strike=market.floor_strike,
        cap_strike=market.cap_strike,
        model_probability=model_probability,
        ensemble_mean=ensemble_mean,
        ensemble_std=ensemble_std,
        observed_so_far=bracket_observation,
        forecast_run_time=forecast_run_time,
        forecast_availability_time=forecast_availability_time,
        observation_event_time=(
            observation_event_time if bracket_observation is not None else None
        ),
        observation_publication_time=(
            observation_publication_time if bracket_observation is not None else None
        ),
        observation_collector_received_time=(
            observation_collector_received_time
            if bracket_observation is not None
            else None
        ),
        model_version="normal-v4-observation-conditioned",
        calibration_validated=False,
        market_yes_price=market_price,
        edge=edge_result.edge,
        fee_adjusted_threshold=edge_result.threshold,
        rules_primary=market.rules_primary,
        rules_secondary=market.rules_secondary or None,
        kalshi_url=kalshi_url,
        is_actionable=edge_result.is_actionable,
        status="open",
        close_time=market.close_time,
        metric=metric,
        lead_days=lead_days,
    )


def build_alert_rows(client: KalshiClient, series_ticker: str) -> tuple[list[dict], list[dict]]:
    """Returns (alert_rows, preview_rows). alert_rows are real, tradeable
    Kalshi brackets with a computed edge, one per currently-open event.
    preview_rows are informational-only forecasts for any date within the
    same ensemble fetch (see fetch_daily_ensemble's forecast_days=3 below)
    that Kalshi hasn't opened a market for yet — typically 2 days out, once
    today's and tomorrow's events are both accounted for. There's no bracket
    structure or market price to compute a real edge against that far out,
    so these are a calibrated expected range only, not an edge/probability —
    see forecast_previews in db/schema.sql.
    """
    station = STATIONS[series_ticker]
    series = client.get_series(series_ticker)
    # More than one event is open at once — today's still-trading market plus
    # tomorrow's, which opens before today's closes (confirmed live
    # 2026-07-19 across several series: always exactly these two, never a
    # third). Generate a full row for EACH one, not just whichever's soonest —
    # the pipeline used to only ever look at "the earliest," so its effective
    # forecast lead time silently swung between same-day (while today's
    # market was still open) and day-ahead (once it rolled over), even though
    # this project's calibration was fit and validated specifically for a
    # ~24h-ahead lead time (see kalshi-implementation-progress memory,
    # 2026-07-19). Tagging every row with lead_days lets the dashboard and the
    # paper-trading bot's existing same-day exclusion both tell the two apart
    # instead of silently mixing them.
    events, _ = client.get_events(series_ticker=series_ticker, status="open", limit=10)
    dated_events = []
    for e in events:
        try:
            dated_events.append((parse_event_date(e.event_ticker), e))
        except ValueError:
            continue
    if not dated_events:
        print(f"{station.city}: no open event with a parseable date, skipping.")
        return [], []
    dated_events.sort(key=lambda pair: pair[0])

    # Same station-local-date convention as paper_trading/engine.py's
    # _is_same_day — a fixed standard-time offset, not a DST-shifting wall
    # clock, so "today" here always means the calendar day the weather
    # station itself is on.
    local_today = datetime.now(UTC).astimezone(ZoneInfo(station.standard_time_timezone)).date()

    # Today's already-recorded extreme at the very station Kalshi settles
    # against. This is the single input whose absence caused the 2026-07-20
    # no-edge finding (kalshi-no-edge-root-cause memory): a same-day market is
    # priced by traders who can see the thermometer, while this pipeline was
    # pricing it off a morning forecast as though the day hadn't happened,
    # which is where the phantom "edge" came from. Only meaningful for a
    # lead_days=0 event — a market settling tomorrow has nothing observed yet.
    #
    # A failure here must not take the city's whole run down: falling back to
    # None just restores the previous (unconditional) behavior for this cycle,
    # which is worse but not broken.
    observed_so_far: float | None = None
    observation_event_time: datetime | None = None
    observation_publication_time: datetime | None = None
    observation_collector_received_time: datetime | None = None
    if any(event_date == local_today for event_date, _ in dated_events):
        try:
            observation = fetch_today_extreme(
                station.nws_station_id, station.metric, station.standard_time_timezone
            )
        except Exception as exc:  # noqa: BLE001 - network/shape errors are all non-fatal here
            print(f"  WARNING: {station.city}: today's observations unavailable ({exc}) — pricing unconditionally.")
        else:
            if observation is not None:
                observed_so_far, observed_at = observation
                observation_event_time = datetime.fromisoformat(
                    observed_at.replace("Z", "+00:00")
                )
                # NWS exposes the observation event time but not a separate
                # source publication/revision timestamp on this endpoint.
                observation_collector_received_time = datetime.now(UTC)
                print(f"{station.city}: observed {station.metric} so far today {observed_so_far:.1f}F (at {observed_at})")

    ensemble = fetch_daily_ensemble(
        station.latitude,
        station.longitude,
        station.standard_time_timezone,
        metric=station.metric,
        forecast_days=3,
    )
    # Open-Meteo's response does not expose an authoritative underlying model
    # run/publication timestamp. Receipt time is the future-safe availability
    # boundary; forecast_run_time stays null rather than being inferred.
    forecast_availability_time = datetime.now(UTC)

    all_rows: list[dict] = []
    for event_date, event in dated_events:
        lead_days = (event_date - local_today).days
        members = ensemble.get(event_date.isoformat())
        if not members:
            print(f"{station.city}: no ensemble forecast for {event_date} (lead_days={lead_days}) yet, skipping.")
            continue
        mean, std = fit_normal(members)

        markets, _ = client.get_markets(event_ticker=event.event_ticker, limit=50)
        kalshi_link = market_url(series_ticker, series.title, event.event_ticker)

        rows: list[dict] = []
        market_prices: list[float] = []
        for market in markets:
            row = price_bracket(
                series_ticker=series_ticker,
                city=station.city,
                event=event,
                market=market,
                ensemble_mean=mean,
                ensemble_std=std,
                event_date=event_date,
                lead_days=lead_days,
                metric=station.metric,
                observed_so_far=observed_so_far,
                kalshi_url=kalshi_link,
                forecast_run_time=None,
                forecast_availability_time=forecast_availability_time,
                observation_event_time=observation_event_time,
                observation_publication_time=observation_publication_time,
                observation_collector_received_time=observation_collector_received_time,
            )
            if row is None:
                continue
            rows.append(row)
            market_prices.append(row["market_yes_price"])

        deviation = bracket_sum_deviation(market_prices)
        print(
            f"{station.city} lead_days={lead_days}: {len(rows)} brackets, "
            f"bracket-sum deviation from 1.0: {deviation:+.3f}"
        )
        all_rows.extend(rows)

    # Any date the ensemble covers but Kalshi hasn't opened a market for
    # yet — informational preview only, reusing the same lead=1 calibration
    # as a rough (not separately validated) approximation.
    open_dates = {event_date for event_date, _ in dated_events}
    calibration = get_calibration(series_ticker)
    preview_rows: list[dict] = []
    for date_str, members in ensemble.items():
        preview_date = datetime.fromisoformat(date_str).date()
        if preview_date in open_dates or not members:
            continue
        preview_lead_days = (preview_date - local_today).days
        if preview_lead_days <= 0:
            continue
        mean, _std = fit_normal(members)
        preview_rows.append(
            dict(
                series_ticker=series_ticker,
                city=station.city,
                metric=station.metric,
                target_date=preview_date.isoformat(),
                lead_days=preview_lead_days,
                ensemble_mean=mean,
                ensemble_std=_std,
                calibrated_mean=mean + calibration.bias_for_month(preview_date.month),
                calibrated_std=calibration.std,
            )
        )
        print(f"{station.city} lead_days={preview_lead_days}: preview only, no market open yet")

    return all_rows, preview_rows


def insert_rows(database_url: str, table: str, rows: list[dict]) -> None:
    import psycopg

    if not rows:
        return
    columns = list(rows[0].keys())
    placeholders = ", ".join(f"%({c})s" for c in columns)
    query = f"insert into {table} ({', '.join(columns)}) values ({placeholders})"
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.executemany(query, rows)
        conn.commit()


def main() -> int:
    """Returns a process exit code: 0 if at least one city produced rows
    (partial failures are logged but not fatal — a cron run that gets 5 of 6
    cities is more useful than one that produces nothing), 1 if every city
    failed or errored, which is worth cron surfacing as a real failure.
    """
    print(f"[{datetime.now(UTC).isoformat()}] generate_alerts starting")
    load_dotenv()
    database_url = os.environ.get("DATABASE_URL")
    all_rows: list[dict] = []
    all_preview_rows: list[dict] = []
    failed_cities: list[str] = []

    with track_run("generate_alerts") as run, KalshiClient() as client:
        for series_ticker in STATIONS:
            try:
                rows, preview_rows = build_alert_rows(client, series_ticker)
                all_rows.extend(rows)
                all_preview_rows.extend(preview_rows)
            except Exception as exc:
                # One city's transient API hiccup shouldn't take down the
                # other five — log it and keep going, but track it so the
                # run can still fail loudly if literally nothing worked.
                failed_cities.append(series_ticker)
                print(f"  ERROR: {series_ticker} failed: {exc.__class__.__name__}: {exc}")

        actionable = [row for row in all_rows if row["is_actionable"]]
        print(f"\n{len(all_rows)} total brackets, {len(actionable)} actionable (unvalidated model).")
        print(f"{len(all_preview_rows)} preview-only rows (no market open yet).")
        if failed_cities:
            print(f"Cities that errored this run: {', '.join(failed_cities)}")

        if database_url:
            if all_rows:
                insert_rows(database_url, "alerts", all_rows)
                print(f"Inserted {len(all_rows)} rows into `alerts`.")
            if all_preview_rows:
                insert_rows(database_url, "forecast_previews", all_preview_rows)
                print(f"Inserted {len(all_preview_rows)} rows into `forecast_previews`.")
        else:
            print("DATABASE_URL not set — not writing anywhere. Top actionable brackets:")
            for row in sorted(actionable, key=lambda r: -abs(r["edge"]))[:10]:
                print(
                    f"  {row['city']:<14} {row['bracket_label']:<10} "
                    f"model={row['model_probability'] * 100:5.1f}% "
                    f"market={row['market_yes_price'] * 100:5.1f}% "
                    f"edge={row['edge'] * 100:+6.1f}%"
                )

        run.summary = (
            f"{len(all_rows)} alerts ({len(actionable)} actionable) across "
            f"{len(STATIONS) - len(failed_cities)}/{len(STATIONS)} cities"
        )
        if failed_cities:
            run.status = "partial" if all_rows else "failed"
            run.detail = f"Failed cities: {', '.join(failed_cities)}"

        if not all_rows:
            print("No rows produced from any city — treating this as a failed run.")
            if not failed_cities:
                # Every city "succeeded" but produced nothing (e.g. no open
                # event for any of them) — still worth flagging as failed
                # rather than a quiet, misleadingly-green success.
                run.status = "failed"
                run.detail = "No rows from any city despite no per-city errors."
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
