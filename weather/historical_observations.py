"""Historical intraday station observations — the IEM ASOS archive, not
api.weather.gov (which only retains ~7 days; see weather/nws_observations.py).
Built for the 2026-07-20 same-day backtest proof: reconstructing "what had
this station recorded by this time of day" on a past date, the same question
nws_observations.fetch_today_extreme answers for today.

The Iowa Environmental Mesonet's request API
(mesonet.agron.iastate.edu/cgi-bin/request/asos.py) needs no API key/token
and archives decades of METAR-derived readings per station. `report_type=3,4`
restricts to hourly + special METAR reports only, server-side — the same
"only hourly/special METAR" restriction nws_observations.py applies
client-side to the live NWS feed, and for the same reason (see that module's
docstring on the 5-minute rounded feed corrupting an extreme): this keeps the
backtest's observation resolution on the same footing as the live pricing
path, rather than testing against a different-resolution proxy. Verified live
2026-07-20 against NYC (station "NYC", no "K" prefix — IEM's ASOS network id,
unlike api.weather.gov's ICAO id): returns hourly-cadence rows with no
5-minute-feed entries mixed in.
"""

from __future__ import annotations

from datetime import date, datetime, time

import httpx

ASOS_URL = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"


def fetch_asos_temperatures(
    nws_station_id: str, timezone: str, start_date: str, end_date: str
) -> list[tuple[datetime, float]]:
    """(station-local naive datetime, temperature degF) pairs for every
    hourly/special METAR in [start_date, end_date] (inclusive, ISO dates) —
    one request for the whole window, not one per day, since IEM supports a
    date-range query directly.

    `nws_station_id` is the bare id (e.g. "NYC"), matching Station.
    nws_station_id directly — IEM's ASOS network uses the same identifier
    api.weather.gov does, just without the "K" prefix that endpoint needs.
    `timezone` is the station's fixed standard-time offset (Station.
    standard_time_timezone), passed straight through as IEM's `tz` param so
    returned timestamps are already in the same day-boundary convention used
    everywhere else in this project, not UTC or DST wall-clock.
    """
    start = datetime.strptime(start_date, "%Y-%m-%d")
    end = datetime.strptime(end_date, "%Y-%m-%d")
    response = httpx.get(
        ASOS_URL,
        params={
            "station": nws_station_id,
            "data": "tmpf",
            "year1": start.year,
            "month1": start.month,
            "day1": start.day,
            "year2": end.year,
            "month2": end.month,
            "day2": end.day,
            "tz": timezone,
            "format": "onlycomma",
            "latlon": "no",
            "elev": "no",
            "missing": "empty",
            "trace": "empty",
            "direct": "no",
            "report_type": [3, 4],
        },
        timeout=30.0,
    )
    response.raise_for_status()

    readings: list[tuple[datetime, float]] = []
    lines = response.text.strip().splitlines()
    for line in lines[1:]:  # header: station,valid,tmpf
        parts = line.split(",")
        if len(parts) != 3 or not parts[2]:
            continue
        _station, valid, tmpf = parts
        try:
            when = datetime.strptime(valid, "%Y-%m-%d %H:%M")
            temp_f = float(tmpf)
        except ValueError:
            continue
        readings.append((when, temp_f))
    return readings


def extreme_as_of(
    readings: list[tuple[datetime, float]],
    target_date: date,
    cutoff_time: time,
    metric: str,
) -> float | None:
    """Min or max reading on `target_date`, restricted to station-local
    readings at or before `cutoff_time` — "what had the station recorded by
    this moment," the same quantity fetch_today_extreme gives for today, here
    reconstructed for a fixed past instant instead of "right now". Returns
    None if nothing qualifies (no readings that day yet, or an IEM gap).
    """
    if metric not in ("min", "max"):
        raise ValueError(f"metric must be 'min' or 'max', got {metric!r}")
    values = [temp for when, temp in readings if when.date() == target_date and when.time() <= cutoff_time]
    if not values:
        return None
    return max(values) if metric == "max" else min(values)
