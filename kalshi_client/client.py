from __future__ import annotations

import os
from typing import Any

import httpx

from .auth import KalshiCredentials, sign_request
from .exceptions import KalshiAPIError, KalshiAuthError
from .models import Event, Market, Series

DEFAULT_BASE_URL = "https://external-api.kalshi.com/trade-api/v2"


class KalshiClient:
    """Thin REST client for Kalshi's market-data and (eventually) trading endpoints.

    Series/event/market discovery endpoints are public and work with no credentials.
    Portfolio and order endpoints require `credentials` and are not implemented yet —
    this project doesn't auto-place trades.
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
        response = self._http.request(method, endpoint, params=params, headers=headers)
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
