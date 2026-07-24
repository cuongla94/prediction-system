from __future__ import annotations

import os
import time
from decimal import Decimal
from typing import Any

import httpx

from .auth import KalshiCredentials, sign_request
from .exceptions import KalshiAPIError, KalshiAuthError
from .models import (
    Balance,
    CancelOrderAcknowledgement,
    Candlestick,
    Event,
    ExchangeStatus,
    Fill,
    Market,
    Order,
    OrderAcknowledgement,
    Position,
    Series,
    Settlement,
)
from .orders import format_count, format_price

PRODUCTION_BASE_URL = "https://external-api.kalshi.com/trade-api/v2"
DEMO_BASE_URL = "https://demo-api.kalshi.co/trade-api/v2"
DEFAULT_BASE_URL = PRODUCTION_BASE_URL

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
    """Thin REST client for Kalshi's V2 market, portfolio, and order endpoints.

    Series/event/market discovery endpoints are public and work with no credentials.
    Portfolio and order endpoints require backend-only `credentials`.
    """

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        credentials: KalshiCredentials | None = None,
        timeout: float = 10.0,
        subaccount: int | None = None,
    ):
        self._credentials = credentials
        self.base_url = base_url.rstrip("/")
        self._http = httpx.Client(base_url=base_url, timeout=timeout)
        self._subaccount = subaccount
        # The string Kalshi signs is the full request path, independent of host —
        # derive it from base_url instead of hardcoding "/trade-api/v2" a second time.
        self._sign_path_prefix = httpx.URL(base_url).path.rstrip("/")

    @property
    def is_production(self) -> bool:
        return httpx.URL(self.base_url).host in {
            "external-api.kalshi.com",
            "api.elections.kalshi.com",
        }

    @classmethod
    def from_env(cls) -> "KalshiClient":
        """Build a client from KALSHI_* environment variables (loads .env if present).

        Falls back to an unauthenticated (public-data-only) client if
        KALSHI_API_KEY_ID / KALSHI_PRIVATE_KEY_PATH aren't set.
        """
        from dotenv import load_dotenv

        load_dotenv()
        environment = os.environ.get("KALSHI_ENVIRONMENT", "production").strip().lower()
        if environment in {"demo", "sandbox"}:
            default_base_url = DEMO_BASE_URL
            configured_base_url = os.environ.get("KALSHI_DEMO_BASE_URL")
        else:
            default_base_url = PRODUCTION_BASE_URL
            configured_base_url = os.environ.get("KALSHI_PRODUCTION_BASE_URL")
        # KALSHI_BASE_URL remains a backwards-compatible explicit override.
        base_url = os.environ.get("KALSHI_BASE_URL") or configured_base_url or default_base_url
        key_id = os.environ.get("KALSHI_API_KEY_ID")
        key_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH")
        subaccount_raw = os.environ.get("KALSHI_SUBACCOUNT")
        subaccount = int(subaccount_raw) if subaccount_raw else None
        credentials = None
        if key_id and key_path:
            credentials = KalshiCredentials.from_pem_file(key_id, key_path)
        return cls(base_url=base_url, credentials=credentials, subaccount=subaccount)

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
        json: dict[str, Any] | None = None,
        authed: bool = False,
    ) -> Any:
        if authed and self._credentials is None:
            raise KalshiAuthError(
                f"{method} {endpoint} requires credentials; none were configured "
                "(set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH)."
            )

        backoff = _INITIAL_BACKOFF_SECONDS
        for attempt in range(_MAX_RATE_LIMIT_RETRIES + 1):
            headers = (
                sign_request(self._credentials, method, self._sign_path_prefix + endpoint)
                if authed and self._credentials is not None
                else {}
            )
            response = self._http.request(
                method, endpoint, params=params, json=json, headers=headers
            )
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

    def get_candlesticks(
        self,
        series_ticker: str,
        market_ticker: str,
        start_ts: int,
        end_ts: int,
        period_interval: int = 1,
    ) -> list[Candlestick]:
        """Intraday yes-bid/yes-ask close history for one market, `start_ts`
        to `end_ts` (Unix seconds, UTC). `period_interval` is bucket width in
        minutes — 1 minute by default, the finest Kalshi offers, to minimize
        how stale a same-day backtest's "price as of this decision time"
        snapshot is (see backtest/harness.py's day-ahead backtest, which only
        needed one price per market; this is for the same-day proof, which
        needs price as of a specific intraday instant).
        """
        data = self._request(
            "GET",
            f"/series/{series_ticker}/markets/{market_ticker}/candlesticks",
            params={"start_ts": start_ts, "end_ts": end_ts, "period_interval": period_interval},
        )
        return [Candlestick.from_dict(c) for c in data["candlesticks"]]

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

    # ---- Portfolio ------------------------------------------------------

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
            params = {
                "count_filter": "position",
                "limit": 1000,
                "cursor": cursor,
                "subaccount": self._subaccount,
            }
            params = {k: v for k, v in params.items() if v is not None}
            data = self._request("GET", "/portfolio/positions", params=params, authed=True)
            positions.extend(Position.from_dict(p) for p in data["market_positions"])
            cursor = data.get("cursor") or None
            if not cursor:
                return positions

    def get_settlements(self, limit: int = 1000) -> list[Settlement]:
        """Every settled market on the real account — GET /portfolio/settlements
        (authenticated, read-only), the "History" tab on kalshi.com. Paginates
        internally. `limit` caps total rows fetched (not per-page) so a very
        long history can't page forever; the default is generous for this
        project's single small account.
        """
        settlements: list[Settlement] = []
        cursor: str | None = None
        while len(settlements) < limit:
            params = {
                "limit": min(200, limit - len(settlements)),
                "cursor": cursor,
                "subaccount": self._subaccount,
            }
            params = {k: v for k, v in params.items() if v is not None}
            data = self._request("GET", "/portfolio/settlements", params=params, authed=True)
            settlements.extend(Settlement.from_dict(s) for s in data.get("settlements", []))
            cursor = data.get("cursor") or None
            if not cursor:
                break
        return settlements

    def get_balance(self) -> Balance:
        """Current available cash from authenticated GET /portfolio/balance."""
        params = {"subaccount": self._subaccount} if self._subaccount is not None else None
        data = self._request("GET", "/portfolio/balance", params=params, authed=True)
        return Balance.from_dict(data)

    def get_order(self, order_id: str) -> Order:
        params = {"subaccount": self._subaccount} if self._subaccount is not None else None
        data = self._request(
            "GET", f"/portfolio/orders/{order_id}", params=params, authed=True
        )
        return Order.from_dict(data["order"])

    def list_orders(
        self,
        *,
        ticker: str | None = None,
        event_ticker: str | None = None,
        status: str | None = None,
        min_ts: int | None = None,
        max_ts: int | None = None,
        limit: int = 1000,
    ) -> list[Order]:
        orders: list[Order] = []
        cursor: str | None = None
        while len(orders) < limit:
            params = {
                "ticker": ticker,
                "event_ticker": event_ticker,
                "status": status,
                "min_ts": min_ts,
                "max_ts": max_ts,
                "limit": min(1000, limit - len(orders)),
                "cursor": cursor,
                "subaccount": self._subaccount,
            }
            data = self._request(
                "GET",
                "/portfolio/orders",
                params={k: v for k, v in params.items() if v is not None},
                authed=True,
            )
            orders.extend(Order.from_dict(row) for row in data.get("orders", []))
            cursor = data.get("cursor") or None
            if not cursor:
                break
        return orders

    def list_fills(
        self,
        *,
        ticker: str | None = None,
        order_id: str | None = None,
        min_ts: int | None = None,
        max_ts: int | None = None,
        limit: int = 1000,
    ) -> list[Fill]:
        fills: list[Fill] = []
        cursor: str | None = None
        while len(fills) < limit:
            params = {
                "ticker": ticker,
                "order_id": order_id,
                "min_ts": min_ts,
                "max_ts": max_ts,
                "limit": min(1000, limit - len(fills)),
                "cursor": cursor,
                "subaccount": self._subaccount,
            }
            data = self._request(
                "GET",
                "/portfolio/fills",
                params={k: v for k, v in params.items() if v is not None},
                authed=True,
            )
            fills.extend(Fill.from_dict(row) for row in data.get("fills", []))
            cursor = data.get("cursor") or None
            if not cursor:
                break
        return fills

    def create_order(
        self,
        *,
        ticker: str,
        client_order_id: str,
        side: str,
        count: Decimal | str | int,
        price: Decimal | str,
        time_in_force: str = "good_till_canceled",
        self_trade_prevention_type: str = "taker_at_cross",
        cancel_order_on_pause: bool = True,
        expiration_time: int | None = None,
        post_only: bool | None = None,
    ) -> OrderAcknowledgement:
        """Submit one V2 event order.

        Callers must use `orders.to_event_order_book` before this method so
        strategy YES/NO semantics never leak into the API book representation.
        """
        book_side = side.strip().lower()
        if book_side not in {"bid", "ask"}:
            raise ValueError("side must be Kalshi V2 book side 'bid' or 'ask'.")
        body: dict[str, Any] = {
            "ticker": ticker,
            "client_order_id": client_order_id,
            "side": book_side,
            "count": format_count(count),
            "price": format_price(price),
            "time_in_force": time_in_force,
            "self_trade_prevention_type": self_trade_prevention_type,
            "cancel_order_on_pause": cancel_order_on_pause,
        }
        if expiration_time is not None:
            body["expiration_time"] = expiration_time
        if post_only is not None:
            body["post_only"] = post_only
        if self._subaccount is not None:
            body["subaccount"] = self._subaccount
        data = self._request(
            "POST", "/portfolio/events/orders", json=body, authed=True
        )
        return OrderAcknowledgement.from_dict(data)

    def cancel_order(self, order_id: str) -> CancelOrderAcknowledgement:
        params = {"subaccount": self._subaccount} if self._subaccount is not None else None
        data = self._request(
            "DELETE",
            f"/portfolio/events/orders/{order_id}",
            params=params,
            authed=True,
        )
        return CancelOrderAcknowledgement.from_dict(data)

    def get_exchange_status(self) -> ExchangeStatus:
        return ExchangeStatus.from_dict(self._request("GET", "/exchange/status"))
