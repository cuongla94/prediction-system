"""Live NWS station observations — today's already-realized temperature
extreme, straight from the same station Kalshi settles against, not a
forecast. Verified live 2026-07-19: `api.weather.gov/stations/{icao}/observations`
needs no API key or token, and every station's ICAO id this project uses is
simply "K" + `Station.nws_station_id` (spot-checked against 6 of the 20
stations' `/stations/{icao}` endpoint — all 200).

**This endpoint returns two interleaved feeds, and mixing them corrupts the
extreme (found 2026-07-20).** Most stations return ~100 observations per day:
the hourly METAR (at :51-:54, temperature to 0.1C) plus a 5-minute feed whose
temperature is rounded to whole degrees Celsius. Rounding is harmless per
reading, but a min/max *over* the rounded feed is biased outward — the extreme
lands on whichever sample rounded furthest in that direction, and whole-degree
C converts to a 1.8F-quantized ladder (18C -> 64.4F, 19C -> 66.2F). Live check
at KPHL: the 5-minute feed gave a daily low of 64.4F while the hourly METARs
gave 66.0F, and Kalshi's own market priced the 65-66 bracket at 99.5c — i.e.
the rounded feed was a full degree too cold and would have zeroed out the
bracket that was virtually certain to settle YES.

Only hourly/special METARs are used, therefore (`_is_metar`). Some stations
(KNYC, KDEN) only ever return those anyway and are unaffected. Sampling hourly
can miss an extreme that occurs between observations, which makes the result
slightly *less* extreme than truth — deliberately the safe direction here,
since this value is used to rule brackets out (weather/probability.py's
observation_conditioned_bracket_probability): an under-stated extreme rules out
less than it could, while an over-stated one rules out brackets that can still
happen.
"""

from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

import httpx

OBSERVATIONS_URL = "https://api.weather.gov/stations/{station}/observations"

# The 5-minute feed lands on exact multiples of 5 (:00, :05, ... :50, :55);
# the hourly METAR lands just before the hour, at :51-:54 across every station
# checked live (KNYC :51, KMDW/KAUS/KMIA/KHOU :53, KPHL :54). So "minute is
# not a multiple of 5" separates them — note this is why a `minute >= 50` rule
# is NOT enough: :50 and :55 are 5-minute-feed slots.
_FIVE_MINUTE_FEED_INTERVAL = 5


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
        if temp_c is None or timestamp is None or not _is_metar(timestamp, temp_c):
            continue
        readings.append((temp_c * 9 / 5 + 32, timestamp))

    if not readings:
        return None
    pick = min if metric == "min" else max
    return pick(readings, key=lambda reading: reading[0])


def _is_metar(timestamp: str, temp_c: float) -> bool:
    """Whether this reading is an hourly/special METAR rather than the
    rounded 5-minute feed — see the module docstring for why mixing them
    corrupts a min/max.

    Two independent signals, either of which is sufficient: a minute that
    isn't a 5-minute-feed slot, or sub-degree Celsius precision, which only
    the METAR temperature group reports. The second catches a SPECI issued
    off-hour (seen live at KAUS :13) that would otherwise be discarded
    despite being a real observation.
    """
    try:
        minute = int(timestamp[14:16])
    except (ValueError, IndexError):
        return False
    return minute % _FIVE_MINUTE_FEED_INTERVAL != 0 or abs(temp_c - round(temp_c)) > 1e-9
