from __future__ import annotations

from datetime import date, datetime, time

import httpx
import pytest

from weather.historical_observations import extreme_as_of, fetch_asos_temperatures


def _patch_response(monkeypatch, csv_body: str) -> None:
    def _get(url, *, params=None, timeout=None):
        request = httpx.Request("GET", url)
        return httpx.Response(200, text=csv_body, request=request)

    monkeypatch.setattr(httpx, "get", _get)


def test_fetch_asos_temperatures_parses_csv_rows(monkeypatch):
    _patch_response(
        monkeypatch,
        "station,valid,tmpf\n"
        "NYC,2026-07-10 09:51,82.00\n"
        "NYC,2026-07-10 12:51,84.00\n"
        "NYC,2026-07-10 15:51,86.00\n",
    )
    readings = fetch_asos_temperatures("NYC", "Etc/GMT+5", "2026-07-10", "2026-07-10")
    assert readings == [
        (datetime(2026, 7, 10, 9, 51), 82.0),
        (datetime(2026, 7, 10, 12, 51), 84.0),
        (datetime(2026, 7, 10, 15, 51), 86.0),
    ]


def test_fetch_asos_temperatures_skips_missing_values(monkeypatch):
    # IEM's missing=empty setting leaves the tmpf column blank rather than
    # omitting the row entirely.
    _patch_response(
        monkeypatch,
        "station,valid,tmpf\nNYC,2026-07-10 09:51,\nNYC,2026-07-10 10:51,80.00\n",
    )
    readings = fetch_asos_temperatures("NYC", "Etc/GMT+5", "2026-07-10", "2026-07-10")
    assert readings == [(datetime(2026, 7, 10, 10, 51), 80.0)]


def test_fetch_asos_temperatures_handles_empty_body(monkeypatch):
    _patch_response(monkeypatch, "station,valid,tmpf\n")
    assert fetch_asos_temperatures("NYC", "Etc/GMT+5", "2026-07-10", "2026-07-10") == []


# --- extreme_as_of -----------------------------------------------------


_READINGS = [
    (datetime(2026, 7, 10, 6, 51), 70.0),
    (datetime(2026, 7, 10, 12, 51), 84.0),
    (datetime(2026, 7, 10, 15, 51), 88.0),  # after the 12:00 cutoff below
    (datetime(2026, 7, 11, 9, 51), 60.0),  # a different day entirely
]


def test_extreme_as_of_respects_the_cutoff_time():
    # At a 13:00 cutoff, the 15:51 reading (the day's real max) hasn't
    # happened yet from the decision-time's point of view.
    assert extreme_as_of(_READINGS, date(2026, 7, 10), time(13, 0), "max") == 84.0


def test_extreme_as_of_includes_readings_up_through_a_later_cutoff():
    assert extreme_as_of(_READINGS, date(2026, 7, 10), time(15, 51), "max") == 88.0


def test_extreme_as_of_ignores_other_dates():
    assert extreme_as_of(_READINGS, date(2026, 7, 11), time(12, 0), "min") == 60.0


def test_extreme_as_of_returns_none_before_any_reading():
    assert extreme_as_of(_READINGS, date(2026, 7, 10), time(3, 0), "max") is None


def test_extreme_as_of_rejects_an_unknown_metric():
    with pytest.raises(ValueError):
        extreme_as_of(_READINGS, date(2026, 7, 10), time(12, 0), "mean")
