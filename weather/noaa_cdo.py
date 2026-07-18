"""Independent ground truth for settled markets, via NOAA's Climate Data Online
(CDO) API v2 — GHCND (Global Historical Climatology Network Daily) dataset.

This exists to answer one question the backtest harness couldn't answer on its
own: is Kalshi's own settlement `result` field actually correct? Everything in
backtest/harness.py trusts Kalshi's word for which bracket won. That's a
reasonable default (Kalshi's result is itself sourced from the NWS
Climatological Report per each market's rules_primary — see kalshi-api-gotchas
memory) but it's never been cross-checked against a second, independent source
of the same station's daily max temperature until now. GHCND station IDs for
all 6 cities were already resolved via NOAA's public station list — see
kalshi-backtest-findings memory — and are re-exported from weather.stations.

**Request contract verified live 2026-07-18, still no real token to test a
successful fetch with.** No NCDC token existed yet, so real data still can't
be pulled — but the endpoint, param names, and (importantly) the token
delivery mechanism were confirmed for real without needing one, by reading
the *shape* of 400 responses: an unauthenticated request to
https://www.ncdc.noaa.gov/cdo-web/api/v2/data with datasetid/stationid/
datatypeid/startdate/enddate/units all present returned exactly
`{"status": "400", "message": "Token parameter is required."}` — confirming
the base URL and every param name are right (a wrong param name would more
plausibly 404 or error differently, not specifically complain about the
token). Sending a fake value via `token=...` as a *query* param left that
message unchanged, byte for byte; sending the same fake value via a `token:`
*header* changed it to `"The token parameter provided is not valid."` — a
clean, unambiguous confirmation that the header-based auth this client
already used was correct, despite NOAA's own error text calling it a
"parameter." `fetch_daily_tmax` now surfaces that `message` field in
`NoaaCdoError` instead of a generic httpx status error, since it's
substantially more actionable ("token invalid" vs "400 Bad Request").

What's still genuinely unverified: the *successful* response shape
(results/metadata structure), and real pagination behavior — both need an
actual valid token to observe, which is why `units=standard` (server-side
Fahrenheit conversion, sidesteps guessing GHCND's tenths-of-Celsius native
storage) and `_sanity_check_fahrenheit` (rejects implausible values outright)
both stay in place as defensive measures. Run this for real against a known
day (e.g. yesterday's NYC high) the moment a token exists, and update this
docstring once that's happened.
"""

from __future__ import annotations

import time
from datetime import date, timedelta

import httpx

CDO_BASE_URL = "https://www.ncdc.noaa.gov/cdo-web/api/v2/data"
GHCND_DATASET = "GHCND"
TMAX_DATATYPE = "TMAX"

# NOAA CDO v2 is documented at 5 requests/second and 10,000/day per token.
# This project's call volume is tiny in comparison (a handful of requests per
# backtest cross-check run), so a fixed small delay is a simple, sufficient
# guard rather than needing real token-bucket accounting.
_REQUEST_DELAY_SECONDS = 0.25

# Chunk size for date-range requests. Not confirmed against current API docs
# (see module docstring) — 365 days is the commonly documented per-request
# span limit for CDO v2's /data endpoint, so this chunks defensively at that
# boundary rather than assuming a single request can cover a multi-year range.
_MAX_CHUNK_DAYS = 365

_PLAUSIBLE_FAHRENHEIT_RANGE = (-40.0, 130.0)


class NoaaCdoError(Exception):
    pass


def _raise_for_noaa_error(response: httpx.Response) -> None:
    """Like response.raise_for_status(), but surfaces NOAA's own `message`
    field when present — confirmed live 2026-07-18 that CDO v2 error bodies
    look like {"status": "400", "message": "..."} (see module docstring),
    which is far more actionable than a generic "400 Bad Request" (e.g. it's
    the difference between "token invalid" and "token missing" — both 400s).
    Falls back to the plain httpx error for non-JSON or differently-shaped
    error bodies (a 5xx from an upstream proxy, network-level failures, etc.)
    rather than assuming every failure matches NOAA's own error contract.
    """
    if response.is_success:
        return
    message = None
    try:
        message = response.json().get("message")
    except Exception:
        pass
    if message:
        raise NoaaCdoError(f"NOAA CDO request failed ({response.status_code}): {message}")
    response.raise_for_status()


def _sanity_check_fahrenheit(value: float, station_id: str, date_str: str) -> float:
    low, high = _PLAUSIBLE_FAHRENHEIT_RANGE
    if not (low <= value <= high):
        raise NoaaCdoError(
            f"NOAA CDO returned TMAX={value} for {station_id} on {date_str}, outside the "
            f"plausible range [{low}, {high}]°F — likely a units mismatch (native GHCND "
            "storage is tenths of Celsius; this client requests units=standard specifically "
            "to avoid that) rather than an actual reading. Not treating this as ground truth."
        )
    return value


def _date_chunks(start: date, end: date, max_days: int) -> list[tuple[date, date]]:
    chunks = []
    chunk_start = start
    while chunk_start <= end:
        chunk_end = min(chunk_start + timedelta(days=max_days - 1), end)
        chunks.append((chunk_start, chunk_end))
        chunk_start = chunk_end + timedelta(days=1)
    return chunks


def fetch_daily_tmax(
    station_id: str,
    start_date: str,
    end_date: str,
    token: str,
    *,
    client: httpx.Client | None = None,
) -> dict[str, float]:
    """Daily max temperature (°F) for one GHCND station over a date range.

    `station_id` is the bare GHCND id (e.g. "USW00094728", as stored in
    weather.stations.STATIONS) — the "GHCND:" dataset prefix CDO's API expects
    on stationid is added here, not by the caller. Returns {date_iso: tmax_f},
    with any date the station has no reading for simply absent from the dict
    (missing station-days happen; caller decides how to treat gaps).
    """
    start = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date)
    if start > end:
        raise ValueError(f"start_date {start_date} is after end_date {end_date}")

    owns_client = client is None
    http_client = client or httpx.Client(timeout=30.0)
    results: dict[str, float] = {}
    try:
        for chunk_start, chunk_end in _date_chunks(start, end, _MAX_CHUNK_DAYS):
            offset = 1  # CDO v2 pagination is 1-indexed, not 0-indexed.
            while True:
                response = http_client.get(
                    CDO_BASE_URL,
                    headers={"token": token},
                    params={
                        "datasetid": GHCND_DATASET,
                        "stationid": f"{GHCND_DATASET}:{station_id}",
                        "datatypeid": TMAX_DATATYPE,
                        "startdate": chunk_start.isoformat(),
                        "enddate": chunk_end.isoformat(),
                        "units": "standard",  # Fahrenheit, converted server-side.
                        "limit": 1000,
                        "offset": offset,
                    },
                )
                _raise_for_noaa_error(response)
                payload = response.json()
                for row in payload.get("results", []):
                    row_date = row["date"].split("T")[0]
                    results[row_date] = _sanity_check_fahrenheit(
                        float(row["value"]), station_id, row_date
                    )

                result_count = payload.get("metadata", {}).get("resultset", {}).get("count", 0)
                time.sleep(_REQUEST_DELAY_SECONDS)
                if offset + 1000 > result_count:
                    break
                offset += 1000
    finally:
        if owns_client:
            http_client.close()

    return results


def fetch_single_day_tmax(station_id: str, day: str, token: str) -> float | None:
    """Convenience wrapper for a single date. Returns None if the station has
    no reading for that day rather than raising — a missing station-day isn't
    an error, just something the caller can't cross-check."""
    return fetch_daily_tmax(station_id, day, day, token).get(day)
