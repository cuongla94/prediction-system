from __future__ import annotations

import httpx
import pytest

from weather.nws_observations import _is_metar, fetch_today_extreme


# --- _is_metar: separating the hourly METAR from the rounded 5-minute feed ---


def test_hourly_metar_slots_are_kept():
    # The real slots observed live: KNYC :51, KMDW/KAUS/KMIA/KHOU :53, KPHL :54.
    for minute in ("51", "53", "54"):
        assert _is_metar(f"2026-07-20T09:{minute}:00+00:00", 20.0) is True


def test_five_minute_feed_slots_are_dropped():
    for minute in ("00", "05", "35", "45"):
        assert _is_metar(f"2026-07-20T09:{minute}:00+00:00", 20.0) is False


def test_the_50_and_55_slots_are_dropped():
    # The specific hole in a naive `minute >= 50` rule: :50 and :55 are
    # 5-minute-feed slots, and it was KHOU's :50 reading (24.0C -> 75.2F)
    # slipping through that kept Houston's low a full degree too cold.
    assert _is_metar("2026-07-20T11:50:00+00:00", 24.0) is False
    assert _is_metar("2026-07-20T11:55:00+00:00", 24.0) is False


def test_sub_degree_precision_is_kept_even_off_hour():
    # A SPECI issued off-hour (seen live at KAUS :13) is a real observation;
    # only the METAR temperature group reports tenths, so precision alone is
    # sufficient evidence.
    assert _is_metar("2026-07-20T09:13:00+00:00", 18.9) is True


def test_malformed_timestamp_is_rejected_rather_than_crashing():
    assert _is_metar("not-a-timestamp", 20.0) is False


# --- fetch_today_extreme end-to-end over a mixed feed ---


def _feature(timestamp: str, temp_c: float | None) -> dict:
    return {"properties": {"timestamp": timestamp, "temperature": {"value": temp_c}}}


def _patch_response(monkeypatch, features: list[dict]) -> None:
    def _get(*_args, **_kwargs):
        request = httpx.Request("GET", "https://api.weather.gov/stations/KPHL/observations")
        return httpx.Response(200, json={"features": features}, request=request)

    monkeypatch.setattr(httpx, "get", _get)


def test_min_ignores_the_rounded_five_minute_feed(monkeypatch):
    # The exact KPHL failure of 2026-07-20: the 5-minute feed rounds to whole
    # Celsius, so 18C reads as 64.4F while the hourly METAR's 18.9C is the real
    # 66.0F. Taking the min across both understates the low by more than a
    # degree — enough to zero out a bracket the market priced at 99.5c.
    _patch_response(
        monkeypatch,
        [
            _feature("2026-07-20T09:35:00+00:00", 18.0),  # 5-minute feed, 64.4F
            _feature("2026-07-20T09:45:00+00:00", 18.0),
            _feature("2026-07-20T09:54:00+00:00", 18.9),  # hourly METAR, 66.0F
            _feature("2026-07-20T10:54:00+00:00", 20.0),
        ],
    )
    result = fetch_today_extreme("PHL", "min", "Etc/GMT+5")
    assert result is not None
    temperature, timestamp = result
    assert temperature == pytest.approx(66.02)
    assert timestamp == "2026-07-20T09:54:00+00:00"


def test_max_ignores_the_rounded_five_minute_feed(monkeypatch):
    # Same bias in the other direction: rounding up exaggerates a daily high.
    _patch_response(
        monkeypatch,
        [
            _feature("2026-07-20T13:15:00+00:00", 24.0),  # 5-minute feed, 75.2F
            _feature("2026-07-20T12:54:00+00:00", 23.3),  # hourly METAR, 73.9F
        ],
    )
    result = fetch_today_extreme("PHL", "max", "Etc/GMT+5")
    assert result is not None
    assert result[0] == pytest.approx(73.94)


def test_returns_none_when_no_metar_readings_are_available(monkeypatch):
    # Better to price unconditionally than to condition on the rounded feed.
    _patch_response(
        monkeypatch,
        [
            _feature("2026-07-20T09:35:00+00:00", 18.0),
            _feature("2026-07-20T09:40:00+00:00", 19.0),
        ],
    )
    assert fetch_today_extreme("PHL", "min", "Etc/GMT+5") is None


def test_readings_without_a_temperature_are_skipped(monkeypatch):
    _patch_response(
        monkeypatch,
        [
            _feature("2026-07-20T09:54:00+00:00", None),
            _feature("2026-07-20T10:54:00+00:00", 21.1),
        ],
    )
    result = fetch_today_extreme("PHL", "min", "Etc/GMT+5")
    assert result is not None
    assert result[0] == pytest.approx(69.98)


def test_rejects_an_unknown_metric():
    with pytest.raises(ValueError):
        fetch_today_extreme("PHL", "mean", "Etc/GMT+5")
