"""Redis-backed cache for backtest data collection.

Settled-market and historical-forecast data for a past date range never
changes — once a day has settled, the record is permanent. Without caching,
every backtest run (e.g. testing a model change, as happened repeatedly while
comparing Normal vs. Student's t — see kalshi-backtest-findings memory)
re-pages through months of Kalshi history and re-fetches Open-Meteo's
Previous-Runs archive from scratch. This is what REDIS_URL is for in this
project — nothing else here currently benefits from caching the way this
does: the live pipeline (scripts/generate_alerts.py) needs fresh data every
run by design, and its own API calls are already cheap and infrequent
(4x/day), so it deliberately doesn't use this.

Optional by design: if REDIS_URL isn't set, or Redis is unreachable, this
falls back to an uncached fetch rather than failing — a cache is a
performance layer here, not a dependency this system should break without.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict

from kalshi_client import KalshiClient

from .harness import BacktestRow, collect_rows

# Backtest data is immutable once past — a long TTL, deliberately not the
# general-purpose REDIS_DEFAULT_TTL_SECONDS (meant for shorter-lived,
# time-sensitive caching elsewhere).
_BACKTEST_CACHE_TTL_SECONDS = 30 * 24 * 60 * 60  # 30 days

# Bump this whenever BacktestRow's fields change. Found live 2026-07-23 while
# adding per_model_forecast: a cache entry written under the OLD shape
# deserializes via BacktestRow(**row) with the new field silently defaulted
# (None) rather than erroring — every city read from a warm cache came back
# with 0 usable per-model days, not a refetch, until this was traced back to
# stale cache entries. The version is embedded in the key itself so an old
# entry is simply never looked up again (a clean miss that refetches with
# the current shape), rather than needing every existing cache entry
# manually flushed.
_CACHE_SCHEMA_VERSION = 2


def _redis_client():
    redis_url = os.environ.get("REDIS_URL")
    if not redis_url:
        return None
    try:
        import redis

        client = redis.Redis.from_url(redis_url, socket_connect_timeout=5)
        client.ping()
        return client
    except Exception as exc:
        print(f"  Redis unavailable ({exc.__class__.__name__}) — fetching uncached.")
        return None


def _cache_key(series_ticker: str, start_date: str, end_date: str, lead_days: int) -> str:
    prefix = os.environ.get("REDIS_KEY_PREFIX", "kalshi-prediction-market")
    return f"{prefix}:backtest:v{_CACHE_SCHEMA_VERSION}:{series_ticker}:{start_date}:{end_date}:{lead_days}"


def cached_collect_rows(
    client: KalshiClient,
    series_ticker: str,
    start_date: str,
    end_date: str,
    lead_days: int = 1,
) -> list[BacktestRow]:
    """collect_rows(), cached in Redis when available — same signature, drop-in
    replacement. Falls back to collect_rows() directly if Redis isn't
    configured, unreachable, or the cache entry is missing/expired.
    """
    redis_client = _redis_client()
    key = _cache_key(series_ticker, start_date, end_date, lead_days)

    if redis_client is not None:
        cached = redis_client.get(key)
        if cached is not None:
            print(f"  Cache hit: {series_ticker} {start_date}..{end_date} (lead_days={lead_days}).")
            return [BacktestRow(**row) for row in json.loads(cached)]

    rows = collect_rows(client, series_ticker, start_date, end_date, lead_days)

    if redis_client is not None:
        try:
            redis_client.setex(key, _BACKTEST_CACHE_TTL_SECONDS, json.dumps([asdict(row) for row in rows]))
        except Exception as exc:
            print(f"  Couldn't write to Redis cache ({exc.__class__.__name__}) — continuing without it.")

    return rows
