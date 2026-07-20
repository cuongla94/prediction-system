from __future__ import annotations

import os
import time
from typing import Any

import httpx

from .auth import KalshiCredentials, sign_request
from .exceptions import KalshiAPIError, KalshiAuthError
from .models import Event, Market, Position, Series

DEFAULT_BASE_URL = "https://external-api.kalshi.com/trade-api/v2"

# Confirmed live 2026-07-19: scaling from 6 tracked cities to 20 (40 series,
# ~3 calls each in a tight loop) reliably exhausted Kalshi's rate limit
# partway through a single generate_alerts.py run — every request past
# roughly the 10th station came back 429, for the rest of that run, not just
# a brief blip. Exponential backoff, retried in-place rather than surfaced
# as a per-city failure, since the request itself is fine — it just needs
# to wait for the token bucket to refill.
_MAX_RATE_LIMIT_RETRIES = 5
_INITIAL_BACKOFF_SECONDS = 1.0


class KalshiClient:
    """Thin REST client for Kalshi's market-data and portfolio-read endpoints.

    Series/event/market discovery endpoints are public and work with no credentials.
    Portfolio endpoints (e.g. get_positions) require `credentials` and are read-only —
    this project never places, cancels, or modifies real orders, and has no method
    that would.
    """

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        credentials: KalshiCredentials | None = None,
        timeout: float = 10.0,
    ):
        self._credentials = credentials
        self._http = httpx.Client(base_url=base_url, timeout=timeout)
        # The string Kalshi signs is the full request path, independent of host —
        # derive it from base_url instead of hardcoding "/trade-api/v2" a second time.
        self._sign_path_prefix = httpx.URL(base_url).path.rstrip("/")

    @classmethod
    def from_env(cls) -> "KalshiClient":
        """Build a client from KALSHI_* environment variables (loads .env if present).

        Falls back to an unauthenticated (public-data-only) client if
        KALSHI_API_KEY_ID / KALSHI_PRIVATE_KEY_PATH aren't set.
        """
        from dotenv import load_dotenv

        load_dotenv()
        base_url = os.environ.get("KALSHI_BASE_URL", DEFAULT_BASE_URL)
        key_id = os.environ.get("KALSHI_API_KEY_ID")
        key_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH")
        credentials = None
        if key_id and key_path:
            credentials = KalshiCredentials.from_pem_file(key_id, key_path)
        return cls(base_url=base_url, credentials=credentials)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "KalshiClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _request(
        self,
        method: str,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
        authed: bool = False,
    ) -> Any:
        headers: dict[str, str] = {}
        if authed:
            if self._credentials is None:
                raise KalshiAuthError(
                    f"{method} {endpoint} requires credentials; none were configured "
                    "(set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH)."
                )
            headers = sign_request(self._credentials, method, self._sign_path_prefix + endpoint)

        backoff = _INITIAL_BACKOFF_SECONDS
        for attempt in range(_MAX_RATE_LIMIT_RETRIES + 1):
            response = self._http.request(method, endpoint, params=params, headers=headers)
            if response.status_code == 429 and attempt < _MAX_RATE_LIMIT_RETRIES:
                retry_after = response.headers.get("Retry-After")
                wait_seconds = float(retry_after) if retry_after else backoff
                time.sleep(wait_seconds)
                backoff *= 2
                continue
            if response.is_error:
                raise KalshiAPIError.from_response(response)
            return response.json()

    # ---- Series -----------------------------------------------------------

    def get_series(self, series_ticker: str) -> Series:
        data = self._request("GET", f"/series/{series_ticker}")
        return Series.from_dict(data["series"])

    def get_series_list(
        self, category: str | None = None, tags: str | None = None
    ) -> list[Series]:
        params = {k: v for k, v in {"category": category, "tags": tags}.items() if v is not None}
        data = self._request("GET", "/series", params=params)
        return [Series.from_dict(s) for s in data["series"]]

    # ---- Events -------------------------------------------------------------

    def get_events(
        self,
        series_ticker: str | None = None,
        status: str | None = None,
        with_nested_markets: bool = False,
        limit: int = 200,
        cursor: str | None = None,
    ) -> tuple[list[Event], str | None]:
        params = {
            "series_ticker": series_ticker,
            "status": status,
            "with_nested_markets": with_nested_markets or None,
            "limit": limit,
            "cursor": cursor,
        }
        params = {k: v for k, v in params.items() if v is not None}
        data = self._request("GET", "/events", params=params)
        events = [Event.from_dict(e) for e in data["events"]]
        return events, data.get("cursor") or None

    def get_event(self, event_ticker: str, with_nested_markets: bool = False) -> Event:
        params = {"with_nested_markets": with_nested_markets or None}
        params = {k: v for k, v in params.items() if v is not None}
        data = self._request("GET", f"/events/{event_ticker}", params=params)
        return Event.from_dict(data["event"])

    # ---- Markets ------------------------------------------------------------

    def get_market(self, market_ticker: str) -> Market:
        data = self._request("GET", f"/markets/{market_ticker}")
        return Market.from_dict(data["market"])

    def get_markets(
        self,
        series_ticker: str | None = None,
        event_ticker: str | None = None,
        status: str | None = None,
        limit: int = 200,
        cursor: str | None = None,
    ) -> tuple[list[Market], str | None]:
        params = {
            "series_ticker": series_ticker,
            "event_ticker": event_ticker,
            "status": status,
            "limit": limit,
            "cursor": cursor,
        }
        params = {k: v for k, v in params.items() if v is not None}
        data = self._request("GET", "/markets", params=params)
        markets = [Market.from_dict(m) for m in data["markets"]]
        return markets, data.get("cursor") or None

    def get_historical_markets(
        self,
        series_ticker: str | None = None,
        event_ticker: str | None = None,
        limit: int = 1000,
        cursor: str | None = None,
    ) -> tuple[list[Market], str | None]:
        """Markets old enough to have moved past the live/historical cutoff
        (see GET /historical/cutoff) — /markets stops returning them, this is
        where they live instead. No `status` filter; everything here is settled.
        """
        params = {
            "series_ticker": series_ticker,
            "event_ticker": event_ticker,
            "limit": limit,
            "cursor": cursor,
        }
        params = {k: v for k, v in params.items() if v is not None}
        data = self._request("GET", "/historical/markets", params=params)
        markets = [Market.from_dict(m) for m in data["markets"]]
        return markets, data.get("cursor") or None

    # ---- Portfolio (read-only) ------------------------------------------

    def get_positions(self) -> list[Position]:
        """Every market with a currently non-zero position on the real
        Kalshi account these credentials belong to — actual holdings placed
        directly on kalshi.com, unrelated to this project's own paper_trades
        simulation. Requires credentials (authed=True); paginates internally
        via cursor so callers always get the full list in one call, not a
        page needing further handling. `count_filter="position"` scopes to
        markets with a nonzero position, not every market ever touched.
        """
        positions: list[Position] = []
        cursor: str | None = None
        while True:
            params = {"count_filter": "position", "limit": 1000, "cursor": cursor}
            params = {k: v for k, v in params.items() if v is not None}
            data = self._request("GET", "/portfolio/positions", params=params, authed=True)
            positions.extend(Position.from_dict(p) for p in data["market_positions"])
            cursor = data.get("cursor") or None
            if not cursor:
                return positions
