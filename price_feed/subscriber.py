"""Persistent WebSocket subscriber: keeps a live connection to Kalshi's
`ticker` channel open for every market this system currently cares about, and
writes each update to price_feed/cache.py's Redis cache. Runs forever as its
own process (scripts/run_price_feed.py + a dedicated systemd unit on the
droplet) — this is deliberately NOT part of the request/response cycle of
either the dashboard or the cron pipeline, since a WebSocket connection needs
to stay open continuously, not spin up per-request.

Confirmed live against Kalshi's own docs (docs.kalshi.com/websockets) and
their AsyncAPI spec 2026-07-19, not guessed:
- Endpoint: wss://external-api-ws.kalshi.com/trade-api/ws/v2
- Auth: the *same* RSA-PSS scheme as REST (kalshi_client/auth.py's
  sign_request, already built and tested) — just signed against
  "GET" + "/trade-api/ws/v2" instead of a REST endpoint's path, and passed as
  headers on the handshake instead of query params.
- Subscribe command: {"id": N, "cmd": "subscribe", "params": {"channels":
  ["ticker"], "market_tickers": [...]}}
- Ticker messages carry yes_bid_dollars/yes_ask_dollars/market_ticker/ts_ms.

Deliberately reconnects on a fixed interval rather than using Kalshi's
update_subscription command to add/remove tickers from a live session — this
system's watched-ticker list only changes a few times a day (new alerts
~4x/day, positions closing sporadically), so a periodic reconnect with a
freshly-queried ticker list is simpler and self-healing (it's also a health
check: a connection that's silently gone stale gets torn down and rebuilt on
the same cadence), at the cost of up to RECONNECT_INTERVAL_SECONDS of latency
picking up a brand-new ticker — acceptable since dashboard/app.py falls back
to REST for anything not yet in the cache.
"""

from __future__ import annotations

import asyncio
import json
import os
import time

from dotenv import load_dotenv

from kalshi_client.auth import KalshiCredentials, sign_request

from .cache import set_cached_price

WS_URL = "wss://external-api-ws.kalshi.com/trade-api/ws/v2"
_SIGN_PATH = "/trade-api/ws/v2"

RECONNECT_INTERVAL_SECONDS = 10 * 60
_INITIAL_BACKOFF_SECONDS = 2
_MAX_BACKOFF_SECONDS = 60


def _load_credentials() -> KalshiCredentials:
    key_id = os.environ["KALSHI_API_KEY_ID"]
    key_path = os.environ["KALSHI_PRIVATE_KEY_PATH"]
    return KalshiCredentials.from_pem_file(key_id, key_path)


def _watched_tickers(database_url: str) -> list[str]:
    """Every market this system currently has a live stake or a live signal
    in: open paper-trading positions (need live prices to mark-to-market and
    to evaluate the exit rule) plus every currently-open, unsettled alert
    (the dashboard shows these; a human reviewing the live page benefits from
    a live price the same way the bot does)."""
    import psycopg

    with psycopg.connect(database_url, connect_timeout=5) as conn, conn.cursor() as cur:
        cur.execute("select distinct market_ticker from paper_trades where status = 'open'")
        open_positions = {row[0] for row in cur.fetchall()}
        cur.execute("select distinct market_ticker from alerts where settled_at is null")
        open_alerts = {row[0] for row in cur.fetchall()}
    return sorted(open_positions | open_alerts)


def _parse_ticker_message(data: dict) -> tuple[str, float, int] | None:
    """(market_ticker, yes_price, ts_ms) from a raw ticker-channel message, or
    None if this message isn't a ticker update (Kalshi's WS multiplexes
    subscribe acks, errors, and other channel types over the same socket)."""
    if data.get("type") != "ticker":
        return None
    msg = data.get("msg") or {}
    ticker = msg.get("market_ticker")
    ts_ms = msg.get("ts_ms")
    if not ticker or ts_ms is None:
        return None
    yes_bid = msg.get("yes_bid_dollars")
    yes_ask = msg.get("yes_ask_dollars")
    if yes_bid is None or yes_ask is None:
        return None
    return ticker, round((float(yes_bid) + float(yes_ask)) / 2, 4), int(ts_ms)


async def _run_one_connection(database_url: str) -> None:
    import websockets

    tickers = _watched_tickers(database_url)
    print(f"[price_feed] connecting, watching {len(tickers)} market(s)")
    if not tickers:
        # Nothing to watch right now — still worth a connection cycle so a
        # newly-actionable alert gets picked up next reconnect, but no point
        # subscribing to an empty list.
        await asyncio.sleep(RECONNECT_INTERVAL_SECONDS)
        return

    credentials = _load_credentials()
    headers = sign_request(credentials, "GET", _SIGN_PATH)

    async with websockets.connect(WS_URL, additional_headers=headers) as ws:
        await ws.send(
            json.dumps({"id": 1, "cmd": "subscribe", "params": {"channels": ["ticker"], "market_tickers": tickers}})
        )

        deadline = time.monotonic() + RECONNECT_INTERVAL_SECONDS
        async for raw in ws:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
            parsed = _parse_ticker_message(data)
            if parsed is not None:
                market_ticker, yes_price, ts_ms = parsed
                set_cached_price(market_ticker, yes_price, ts_ms)
            if time.monotonic() >= deadline:
                # Time for a scheduled reconnect (see module docstring) —
                # closing here lets the outer loop pick up any new tickers.
                return


async def run_forever() -> None:
    load_dotenv()
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("[price_feed] DATABASE_URL not set — nothing to watch, exiting.")
        return

    backoff = _INITIAL_BACKOFF_SECONDS
    while True:
        try:
            await _run_one_connection(database_url)
            backoff = _INITIAL_BACKOFF_SECONDS  # a clean cycle resets the backoff
        except Exception as exc:
            print(f"[price_feed] connection error ({exc.__class__.__name__}: {exc}), retrying in {backoff}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _MAX_BACKOFF_SECONDS)
