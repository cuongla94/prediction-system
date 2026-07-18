from __future__ import annotations

import json
from dataclasses import asdict

import backtest.cache as cache_module
from backtest.cache import _cache_key, cached_collect_rows
from backtest.harness import BacktestRow


class _FakeRedis:
    """In-memory stand-in for the bits of the redis-py API this module uses."""

    def __init__(self):
        self.store: dict[str, str] = {}

    def get(self, key: str):
        value = self.store.get(key)
        return value.encode() if value is not None else None

    def setex(self, key: str, ttl: int, value: str) -> None:
        assert ttl > 0
        self.store[key] = value


def _row(market_ticker: str) -> BacktestRow:
    return BacktestRow(
        city="NYC",
        series_ticker="KXHIGHNY",
        event_ticker="KXHIGHNY-26JAN01",
        market_ticker=market_ticker,
        target_date="2026-01-01",
        forecast_mean=80.0,
        forecast_spread=1.0,
        n_models=3,
        actual_outcome=True,
        last_price=0.5,
        floor_strike=79.0,
        cap_strike=80.0,
        approx_actual_temp=80.0,
    )


def test_cache_key_distinguishes_different_params():
    key_a = _cache_key("KXHIGHNY", "2024-01-01", "2024-06-01", 1)
    key_b = _cache_key("KXHIGHNY", "2024-01-01", "2024-06-01", 2)
    key_c = _cache_key("KXHIGHCHI", "2024-01-01", "2024-06-01", 1)
    assert len({key_a, key_b, key_c}) == 3


def test_cached_collect_rows_falls_back_when_redis_unavailable(monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    expected = [_row("KXHIGHNY-26JAN01-T79")]
    monkeypatch.setattr(cache_module, "collect_rows", lambda *a, **kw: expected)

    result = cached_collect_rows(client=None, series_ticker="KXHIGHNY", start_date="2024-01-01", end_date="2024-06-01")

    assert result == expected


def test_cached_collect_rows_writes_and_reads_through_fake_redis(monkeypatch):
    fake_redis = _FakeRedis()
    monkeypatch.setattr(cache_module, "_redis_client", lambda: fake_redis)
    expected = [_row("KXHIGHNY-26JAN01-T79"), _row("KXHIGHNY-26JAN01-B79.5")]
    calls = {"n": 0}

    def fake_collect_rows(*args, **kwargs):
        calls["n"] += 1
        return expected

    monkeypatch.setattr(cache_module, "collect_rows", fake_collect_rows)

    first = cached_collect_rows(client=None, series_ticker="KXHIGHNY", start_date="2024-01-01", end_date="2024-06-01")
    second = cached_collect_rows(client=None, series_ticker="KXHIGHNY", start_date="2024-01-01", end_date="2024-06-01")

    assert first == expected
    assert second == expected
    assert calls["n"] == 1, "second call should have hit the cache, not called collect_rows again"


def test_backtest_row_survives_json_round_trip():
    row = _row("KXHIGHNY-26JAN01-T79")
    reconstructed = BacktestRow(**json.loads(json.dumps(asdict(row))))
    assert reconstructed == row
