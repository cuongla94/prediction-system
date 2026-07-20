"""Live NWS station observations — today's already-realized temperature
extreme, straight from the same station Kalshi settles against, not a
forecast. Verified live 2026-07-19: `api.weather.gov/stations/{icao}/observations`
needs no API key or token, and every station's ICAO id this project uses is
simply "K" + `Station.nws_station_id` (spot-checked against 6 of the 20
stations' `/stations/{icao}` endpoint — all 200).
"""

from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

import httpx

OBSERVATIONS_URL = "https://api.weather.gov/stations/{station}/observations"


def fetch_today_extreme(nws_station_id: str, metric: str, timezone: str) -> tuple[float, str] | None:
    """Min or max of today's real station readings so far (°F), plus the
    observation timestamp (ISO8601 UTC) it occurred at — or None if the
    station returned nothing usable. `metric` is "min" or "max", matching
    `Station.metric`. `timezone` is the station's fixed standard-time offset
    (`Station.standard_time_timezone`) — same day-boundary convention used
    everywhere else in this project, not the DST-shifted wall clock.
    """
    if metric not in ("min", "max"):
        raise ValueError(f"metric must be 'min' or 'max', got {metric!r}")

    tz = ZoneInfo(timezone)
    now_local = datetime.now(tz)
    midnight_local = datetime.combine(now_local.date(), time.min, tzinfo=tz)

    response = httpx.get(
        OBSERVATIONS_URL.format(station=f"K{nws_station_id}"),
        params={"start": midnight_local.isoformat()},
        headers={"User-Agent": "kalshi-weather-signals (internal research tool)"},
        timeout=15.0,
    )
    response.raise_for_status()

    readings: list[tuple[float, str]] = []
    for feature in response.json().get("features", []):
        props = feature["properties"]
        temp_c = (props.get("temperature") or {}).get("value")
        timestamp = props.get("timestamp")
        if temp_c is not None and timestamp is not None:
            readings.append((temp_c * 9 / 5 + 32, timestamp))

    if not readings:
        return None
    pick = min if metric == "min" else max
    return pick(readings, key=lambda reading: reading[0])
