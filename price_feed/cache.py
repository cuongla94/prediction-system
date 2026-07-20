"""Redis-backed latest-price cache, written by scripts/run_price_feed.py's
WebSocket subscriber and read by dashboard/app.py's mark-to-market instead of
a synchronous REST call per position per page load.

Deliberately a *short* TTL (see backtest/cache.py for the opposite case —
immutable historical data, a 30-day TTL) — a live price is only meaningful if
it's actually recent. If the subscriber process dies, entries simply expire
and callers fall back to REST rather than silently serving stale-but-present
data forever.
"""

from __future__ import annotations

import json
import os

_PRICE_TTL_SECONDS = 90


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
        print(f"  Redis unavailable ({exc.__class__.__name__}) — price feed cache disabled.")
        return None


def _cache_key(market_ticker: str) -> str:
    prefix = os.environ.get("REDIS_KEY_PREFIX", "kalshi-prediction-market")
    return f"{prefix}:price:{market_ticker}"


def set_cached_price(market_ticker: str, yes_price: float, ts_ms: int) -> None:
    """Called by the WebSocket subscriber on every ticker update it receives."""
    redis_client = _redis_client()
    if redis_client is None:
        return
    try:
        redis_client.setex(
            _cache_key(market_ticker), _PRICE_TTL_SECONDS, json.dumps({"yes_price": yes_price, "ts_ms": ts_ms})
        )
    except Exception as exc:
        print(f"  Couldn't write price cache for {market_ticker} ({exc.__class__.__name__}).")


def get_cached_price(market_ticker: str) -> float | None:
    """The live yes-price for one market, or None if there's no fresh entry
    (Redis unset/unreachable, subscriber not running, or this ticker just
    hasn't gotten an update in the last _PRICE_TTL_SECONDS) — callers should
    fall back to a direct REST fetch in that case, not treat None as $0."""
    redis_client = _redis_client()
    if redis_client is None:
        return None
    try:
        raw = redis_client.get(_cache_key(market_ticker))
    except Exception:
        return None
    if raw is None:
        return None
    return json.loads(raw)["yes_price"]


def get_cached_prices(market_tickers: list[str]) -> dict[str, float]:
    """Batch form of get_cached_price — one Redis round trip via MGET instead
    of N, for the dashboard's per-page-load mark-to-market of every open
    position at once."""
    if not market_tickers:
        return {}
    redis_client = _redis_client()
    if redis_client is None:
        return {}
    try:
        keys = [_cache_key(t) for t in market_tickers]
        raw_values = redis_client.mget(keys)
    except Exception:
        return {}
    result = {}
    for ticker, raw in zip(market_tickers, raw_values, strict=True):
        if raw is not None:
            result[ticker] = json.loads(raw)["yes_price"]
    return result
