from __future__ import annotations

from datetime import date

import httpx
import pytest

from weather.noaa_cdo import NoaaCdoError, _date_chunks, _raise_for_noaa_error, _sanity_check_fahrenheit


def _response(status_code: int, json: dict | None = None, text: str = "") -> httpx.Response:
    request = httpx.Request("GET", "https://www.ncdc.noaa.gov/cdo-web/api/v2/data")
    if json is not None:
        return httpx.Response(status_code, json=json, request=request)
    return httpx.Response(status_code, text=text, request=request)


def test_sanity_check_accepts_plausible_value():
    assert _sanity_check_fahrenheit(85.0, "USW00094728", "2026-07-18") == 85.0


def test_sanity_check_rejects_implausible_value():
    # e.g. what a silent tenths-of-Celsius unit bug would produce for an
    # actual ~85F day (850 raw, or something similarly out of range).
    with pytest.raises(NoaaCdoError):
        _sanity_check_fahrenheit(850.0, "USW00094728", "2026-07-18")


def test_sanity_check_rejects_below_range():
    with pytest.raises(NoaaCdoError):
        _sanity_check_fahrenheit(-100.0, "USW00094728", "2026-07-18")


def test_date_chunks_single_chunk_when_under_limit():
    chunks = _date_chunks(date(2026, 1, 1), date(2026, 1, 31), max_days=365)
    assert chunks == [(date(2026, 1, 1), date(2026, 1, 31))]


def test_date_chunks_splits_across_boundary():
    start = date(2024, 1, 1)
    end = date(2025, 6, 1)  # spans more than 365 days
    chunks = _date_chunks(start, end, max_days=365)
    assert len(chunks) == 2
    assert chunks[0][0] == start
    assert chunks[-1][1] == end
    # Chunks must be contiguous with no gap or overlap.
    for (_, prev_end), (next_start, _) in zip(chunks, chunks[1:]):
        assert (next_start - prev_end).days == 1


def test_date_chunks_single_day():
    chunks = _date_chunks(date(2026, 7, 18), date(2026, 7, 18), max_days=365)
    assert chunks == [(date(2026, 7, 18), date(2026, 7, 18))]


def test_raise_for_noaa_error_passes_through_success():
    _raise_for_noaa_error(_response(200, json={"results": []}))  # must not raise


def test_raise_for_noaa_error_surfaces_noaa_message():
    # Real shape confirmed live 2026-07-18 against the actual CDO v2 endpoint.
    resp = _response(400, json={"status": "400", "message": "The token parameter provided is not valid."})
    with pytest.raises(NoaaCdoError, match="token parameter provided is not valid"):
        _raise_for_noaa_error(resp)


def test_raise_for_noaa_error_includes_status_code():
    resp = _response(400, json={"status": "400", "message": "Token parameter is required."})
    with pytest.raises(NoaaCdoError, match="400"):
        _raise_for_noaa_error(resp)


def test_raise_for_noaa_error_falls_back_for_non_json_body():
    # A 5xx from an upstream proxy, or any other failure that doesn't match
    # NOAA's own {"status", "message"} error contract, shouldn't be silently
    # swallowed — falls back to the plain httpx status error instead.
    resp = _response(502, text="<html>Bad Gateway</html>")
    with pytest.raises(httpx.HTTPStatusError):
        _raise_for_noaa_error(resp)
