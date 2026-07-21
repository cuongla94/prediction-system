"""Is the same-day gap partly just OUR pipeline being slow to see fresh METAR
data, not the underlying conditioning model? (2026-07-20, follow-up to
scripts/investigate_market_edge.py's Q2, which found the market's price/volume
moves within the same minute a new METAR posts.)

Two things measured, in order, before any fix is proposed:

1. RAW LATENCY, from real production data, not assumption:
   - generate_alerts.py's actual historical run cadence (pipeline_runs table)
     versus the crontab it's scheduled on (scheduler/crontab.example: `0
     5,11,17,23 * * *`, i.e. every 6 hours).
   - How fresh the live NWS observation feed itself is right now (a live
     api.weather.gov call), to separate "the data isn't published yet" from
     "we haven't looked recently" -- these are very different problems with
     very different fixes.

2. WHETHER THIS ACTUALLY COSTS ANYTHING, simulated properly. This is the part
   that needs a genuinely new test, not literally "re-run the same-day proof":
   scripts/run_sameday_proof.py's `extreme_as_of(readings, target,
   decision_time, metric)` call already asks "what was truly known at this
   exact instant" -- it is a ZERO-LATENCY simulation by construction. Running
   it again, unchanged, cannot show a latency effect, because it never modeled
   the pipeline's cron-cadence latency in the first place. To actually test
   the hypothesis, this script instead asks the different, correct question:
   "what would generate_alerts.py's last completed cron run, as of this exact
   decision instant, actually have known?" -- i.e. it computes observed-so-far
   as of the most recent cron tick at or before the decision time, not as of
   the decision time itself, then scores THAT against the market's true price
   at the true decision instant (the market does not share our latency).
   Comparing this "production-realistic" score against the existing
   zero-latency numbers isolates exactly what closing this specific mechanical
   gap could recover, without changing anything else.

Station-local time is the fixed standard-time offset used everywhere else in
this project (weather/stations.py's Etc/GMT+N convention, not DST-aware), so a
city's cron-tick alignment in local time depends on its own UTC offset --
worked out per city below, not assumed to be the same everywhere.

Reuses scripts.run_sameday_proof.collect_sameday_dataset (rows/bias/std/IEM
readings/candlesticks) and weather.historical_observations.extreme_as_of
directly -- this only adds the cron-tick-lag simulation on top.

Usage: uv run scripts/measure_pipeline_latency.py [series_ticker]
(defaults to KXHIGHNY; matches investigate_market_edge.py's own CLI pattern.)
"""

from __future__ import annotations

import sys
from datetime import UTC, date, datetime, timedelta
from datetime import time as dtime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from backtest.calibration import market_benchmark
from scripts.run_sameday_proof import DECISION_TIMES, decision_ts, price_as_of, print_result
from weather.calibration_params import get_calibration
from weather.historical_observations import extreme_as_of
from weather.probability import bracket_probability, observation_conditioned_bracket_probability

SERIES_TICKER = "KXHIGHNY"

# Matches scheduler/crontab.example's `0 5,11,17,23 * * *` exactly -- the only
# schedule generate_alerts.py runs on. If that crontab entry ever changes,
# this needs to change with it or the whole simulation is testing a schedule
# that no longer exists.
CRON_HOURS_UTC: tuple[int, ...] = (5, 11, 17, 23)

# scheduler/run_observation_refresh.sh's cadence, added 2026-07-20 as the fix
# this measurement motivated -- a fixed-interval `*/15 * * * *`, not specific
# hours, so it needs a different tick model than the 6-hourly one above.
REFRESH_INTERVAL_MINUTES = 15


def last_cron_tick_before(instant_utc: datetime) -> datetime:
    """The most recent generate_alerts.py cron firing at or before
    `instant_utc` -- i.e. whatever pipeline run's data is actually sitting in
    the alerts table at that wall-clock moment, since the NEXT run hasn't
    happened yet. Walks back hour by hour; cheap, since cron only fires 4x/day
    so this is never more than a handful of iterations."""
    candidate = instant_utc.replace(minute=0, second=0, microsecond=0)
    while not (candidate.hour in CRON_HOURS_UTC and candidate <= instant_utc):
        candidate -= timedelta(hours=1)
    return candidate


def last_fixed_interval_tick_before(instant_utc: datetime, interval_minutes: int) -> datetime:
    """The most recent tick of a fixed-interval cron (`*/N * * * *`) at or
    before `instant_utc` -- the tick model for run_observation_refresh.sh's
    cadence, distinct from last_cron_tick_before's specific-hours model above.
    Epoch-aligned bucketing matches cron's own `*/15` semantics exactly (cron
    fires at :00/:15/:30/:45 of every hour, which lines up with bucketing
    minutes-since-epoch since the Unix epoch itself falls on :00)."""
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    minutes_since_epoch = int((instant_utc - epoch).total_seconds() // 60)
    tick_minutes = (minutes_since_epoch // interval_minutes) * interval_minutes
    return epoch + timedelta(minutes=tick_minutes)


def _print_live_feed_freshness(station) -> None:
    """How stale the NWS feed ITSELF is right now -- deliberately the
    timestamp of the latest reading of any value, not `fetch_today_extreme`'s
    returned timestamp (which is the timestamp of whichever reading set the
    current extreme). Those are different questions: overnight, once the
    day's high has passed, every new hourly reading is LOWER than the
    existing max, so the extreme's own timestamp can be many hours stale even
    though the feed itself is being updated every hour. Conflating the two
    would misreport genuine feed freshness as if it were a multi-hour NWS
    publication delay, when it is neither -- it is simply "no new record",
    which is expected on the downslope of the day.
    """
    import httpx

    from weather.nws_observations import OBSERVATIONS_URL, _is_metar

    tz = ZoneInfo(station.standard_time_timezone)
    now_local = datetime.now(tz)
    midnight_local = datetime.combine(now_local.date(), dtime.min, tzinfo=tz)
    response = httpx.get(
        OBSERVATIONS_URL.format(station=f"K{station.nws_station_id}"),
        params={"start": midnight_local.isoformat()},
        headers={"User-Agent": "kalshi-weather-signals (internal research tool)"},
        timeout=15.0,
    )
    response.raise_for_status()
    timestamps = []
    for feature in response.json().get("features", []):
        props = feature["properties"]
        temp_c = (props.get("temperature") or {}).get("value")
        ts = props.get("timestamp")
        if temp_c is not None and ts is not None and _is_metar(ts, temp_c):
            timestamps.append(ts)
    if not timestamps:
        print("\nLive check right now: no METAR readings found for today yet.")
        return
    latest = max(timestamps)
    lag_min = (datetime.now(UTC) - datetime.fromisoformat(latest.replace("Z", "+00:00"))).total_seconds() / 60
    print(f"\nLive check right now: the NWS feed's latest METAR reading (any value, not just a new "
          f"record) is {lag_min:.0f} min old ({latest}) -- this is the live feed's own freshness, "
          f"the ceiling on how fresh data COULD be if we polled instantly. Separate from, and much "
          f"smaller than, our own {24 // len(CRON_HOURS_UTC)}-hourly polling cadence below.")


def main() -> None:
    load_dotenv()
    series_ticker = sys.argv[1] if len(sys.argv) > 1 else SERIES_TICKER

    from scripts.run_sameday_proof import collect_sameday_dataset

    dataset = collect_sameday_dataset(series_ticker)
    tz = dataset.tz
    calibration = get_calibration(series_ticker)
    station = dataset.station

    print(f"\n{'=' * 78}\n1. RAW LATENCY\n{'=' * 78}")
    print(f"generate_alerts.py's only schedule: 0 {','.join(map(str, CRON_HOURS_UTC))} * * * UTC "
          f"(scheduler/crontab.example) -- every {24 // len(CRON_HOURS_UTC)} hours, 4x/day.")
    print(f"{station.city} station-local standard time: {station.standard_time_timezone}. "
          f"Cron ticks land at station-local:")
    for h in CRON_HOURS_UTC:
        local = datetime(2026, 1, 1, h, tzinfo=UTC).astimezone(tz)
        print(f"  {h:02d}:00 UTC -> {local.strftime('%H:%M')} local")

    try:
        _print_live_feed_freshness(station)
    except Exception as exc:  # noqa: BLE001 - informational only; a network
        # blip here must not sink the actual measurement below.
        print(f"\nLive check right now: unavailable ({exc.__class__.__name__}: {exc}) — "
              "skipping, this is informational only and doesn't affect the comparison below.")

    print(f"\nFor each of the 3 proof decision times ({station.city} local), which cron tick's data "
          f"a live alert would actually be showing at that moment, and how stale:")
    for t in DECISION_TIMES:
        # Use a fixed reference date -- only the time-of-day/offset arithmetic
        # matters here, not any particular calendar day.
        instant_utc = datetime.combine(date(2026, 7, 15), t, tzinfo=tz).astimezone(UTC)
        tick_utc = last_cron_tick_before(instant_utc)
        tick_local = tick_utc.astimezone(tz)
        lag_hours = (instant_utc - tick_utc).total_seconds() / 3600
        same_day = tick_local.date() == date(2026, 7, 15)
        print(f"  {t.strftime('%H:%M')} local -> last cron tick was {tick_local.strftime('%Y-%m-%d %H:%M')} local "
              f"({'same day' if same_day else 'PREVIOUS day -- no observation for target date yet'}), "
              f"{lag_hours:.1f}h stale")

    # ============================================================
    # 2. Does the lag actually cost anything, and does the fix close it --
    #    three variants, same rows, same market prices, only the observed
    #    value's freshness differs.
    # ============================================================
    print(f"\n{'=' * 78}\n2. ZERO-LATENCY vs OLD 6-HOURLY CADENCE vs NEW 15-MIN CADENCE\n{'=' * 78}")
    header = f"{'time':<7} {'variant':<38} {'n':>4} {'model':>8} {'market':>8} {'skill':>9}"
    print(header)
    print("-" * len(header))

    def predict(loc: float, observed: float | None, row) -> float:
        if observed is None:
            return bracket_probability(loc, dataset.normal_std, row.floor_strike, row.cap_strike)
        return observation_conditioned_bracket_probability(
            loc, dataset.normal_std, row.floor_strike, row.cap_strike, station.metric, observed
        )

    def observed_as_of_tick(target: date, tick_utc: datetime) -> float | None:
        tick_local = tick_utc.astimezone(tz)
        if tick_local.date() != target:
            return None
        return extreme_as_of(dataset.readings, target, tick_local.time(), station.metric)

    for t in DECISION_TIMES:
        zero_preds, old_preds, new_preds, market_prices, outcomes = [], [], [], [], []
        n_stale_old = n_stale_new = 0
        last_old_tick_utc = last_new_tick_utc = None

        for row in dataset.proof_rows:
            target = date.fromisoformat(row.target_date)
            ts = decision_ts(target, t, tz)
            price = price_as_of(dataset.candles_by_market.get(row.market_ticker, []), ts)
            if price is None:
                continue

            loc = row.forecast_mean + calibration.bias_for_month(target.month)
            instant_utc = datetime.combine(target, t, tzinfo=tz).astimezone(UTC)

            true_observed = extreme_as_of(dataset.readings, target, t, station.metric)
            last_old_tick_utc = last_cron_tick_before(instant_utc)
            old_observed = observed_as_of_tick(target, last_old_tick_utc)
            last_new_tick_utc = last_fixed_interval_tick_before(instant_utc, REFRESH_INTERVAL_MINUTES)
            new_observed = observed_as_of_tick(target, last_new_tick_utc)

            n_stale_old += old_observed != true_observed
            n_stale_new += new_observed != true_observed

            zero_preds.append(predict(loc, true_observed, row))
            old_preds.append(predict(loc, old_observed, row))
            new_preds.append(predict(loc, new_observed, row))
            market_prices.append(price)
            outcomes.append(row.actual_outcome)

        old_lag_h = (instant_utc - last_old_tick_utc).total_seconds() / 3600 if last_old_tick_utc else 0.0
        new_lag_min = (instant_utc - last_new_tick_utc).total_seconds() / 60 if last_new_tick_utc else 0.0
        n = len(outcomes)
        print(f"\n--- {t.strftime('%H:%M')} "
              f"(old-cadence structural lag {old_lag_h:.1f}h, {n_stale_old}/{n} rows stale; "
              f"new-cadence structural lag {new_lag_min:.0f}min, {n_stale_new}/{n} rows stale) ---")
        print_result("zero-latency (theoretical ceiling)", market_benchmark(zero_preds, market_prices, outcomes))
        print_result("OLD: 6-hourly (0 5,11,17,23 UTC)", market_benchmark(old_preds, market_prices, outcomes))
        print_result(f"NEW: {REFRESH_INTERVAL_MINUTES}-min refresh (the fix)", market_benchmark(new_preds, market_prices, outcomes))


if __name__ == "__main__":
    main()
