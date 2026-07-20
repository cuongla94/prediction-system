from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


def _float_or_none(data: dict[str, Any], key: str) -> float | None:
    value = data.get(key)
    return float(value) if value is not None else None


def _fixed_point_float(data: dict[str, Any], key: str) -> float:
    """Portfolio-endpoint fields come back as "FixedPointDollars"/
    "FixedPointCount" — decimal strings (e.g. "12.340000", "10.00"), already
    in dollars/whole-contracts, not cents — `float()` handles them directly.
    Defaults to 0.0, not None: an absent field on a real position row would
    mean "no exposure," not "unknown," unlike Market's genuinely-optional
    bid/ask fields that _float_or_none serves."""
    value = data.get(key, 0)
    return float(value)


@dataclass(frozen=True)
class Series:
    ticker: str
    title: str
    category: str
    frequency: str
    settlement_sources: list[dict[str, str]]
    fee_type: str | None
    fee_multiplier: float | None
    raw: dict[str, Any] = field(repr=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Series":
        return cls(
            ticker=data["ticker"],
            title=data.get("title", ""),
            category=data.get("category", ""),
            frequency=data.get("frequency", ""),
            settlement_sources=data.get("settlement_sources", []),
            fee_type=data.get("fee_type"),
            fee_multiplier=data.get("fee_multiplier"),
            raw=data,
        )


@dataclass(frozen=True)
class Event:
    event_ticker: str
    series_ticker: str
    title: str
    sub_title: str
    strike_date: str | None
    strike_period: str | None
    mutually_exclusive: bool
    raw: dict[str, Any] = field(repr=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Event":
        return cls(
            event_ticker=data["event_ticker"],
            series_ticker=data.get("series_ticker", ""),
            title=data.get("title", ""),
            sub_title=data.get("sub_title", ""),
            strike_date=data.get("strike_date"),
            strike_period=data.get("strike_period"),
            mutually_exclusive=data.get("mutually_exclusive", False),
            raw=data,
        )


@dataclass(frozen=True)
class Market:
    ticker: str
    event_ticker: str
    status: str
    # Human-readable question/outcome text — e.g. title "What will be the
    # exact finishing order for the final four teams in the 2026 Men's FIFA
    # World Cup?", yes_sub_title "1: Argentina / 2: Spain / 3: England / 4:
    # France". Weather alerts build their own question/bracket_label instead
    # (dashboard/alert.py) since that phrasing needed to be metric-aware; these
    # exist for markets outside this project's own domain — see Position,
    # used to make a real portfolio holding on an arbitrary market readable
    # instead of showing a bare ticker.
    title: str
    yes_sub_title: str
    no_sub_title: str
    rules_primary: str
    rules_secondary: str
    floor_strike: float | None
    cap_strike: float | None
    yes_bid_dollars: float | None
    yes_ask_dollars: float | None
    no_bid_dollars: float | None
    no_ask_dollars: float | None
    last_price_dollars: float | None
    # When trading actually stops (ISO8601 UTC) — this is `close_time`, not
    # `expiration_time`/`latest_expiration_time` (a much later worst-case
    # fallback if settlement data is delayed) or `expected_expiration_time`
    # (when it's expected to settle/pay out, a distinct moment from "can I
    # still trade this"). Confirmed live 2026-07-18 against a real open
    # KXHIGHNY market: close_time landed at 11:59 PM ET the same day the
    # market's own early_close_condition text describes, i.e. the deadline a
    # trader actually cares about.
    close_time: str | None
    raw: dict[str, Any] = field(repr=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Market":
        return cls(
            ticker=data["ticker"],
            event_ticker=data.get("event_ticker", ""),
            status=data.get("status", ""),
            title=data.get("title", ""),
            yes_sub_title=data.get("yes_sub_title", ""),
            no_sub_title=data.get("no_sub_title", ""),
            rules_primary=data.get("rules_primary", ""),
            rules_secondary=data.get("rules_secondary", ""),
            floor_strike=_float_or_none(data, "floor_strike"),
            cap_strike=_float_or_none(data, "cap_strike"),
            yes_bid_dollars=_float_or_none(data, "yes_bid_dollars"),
            yes_ask_dollars=_float_or_none(data, "yes_ask_dollars"),
            no_bid_dollars=_float_or_none(data, "no_bid_dollars"),
            no_ask_dollars=_float_or_none(data, "no_ask_dollars"),
            last_price_dollars=_float_or_none(data, "last_price_dollars"),
            close_time=data.get("close_time"),
            raw=data,
        )

    @property
    def bracket_label(self) -> str:
        """Human-readable bracket, matching Kalshi's own UI phrasing style
        (e.g. "78° or below" is displayed there for what the API calls
        floor_strike=None, cap_strike=79 — see kalshi-api-gotchas memory on why
        those mean the same thing under whole-degree rounding)."""
        if self.floor_strike is None and self.cap_strike is not None:
            return f"< {self.cap_strike:g}°"
        if self.cap_strike is None and self.floor_strike is not None:
            return f"> {self.floor_strike:g}°"
        if self.floor_strike is not None and self.cap_strike is not None:
            return f"{self.floor_strike:g}–{self.cap_strike:g}°"
        return "?"


@dataclass(frozen=True)
class Position:
    """One market's real position on the user's actual Kalshi account —
    from GET /portfolio/positions (authenticated, real holdings, not a
    forecast/signal). Distinct from this project's own paper_trades: this
    reflects trades placed directly on kalshi.com, which this project has
    never placed and still doesn't — read-only visibility only.

    No `event_ticker` field: confirmed live 2026-07-19 that MarketPosition
    doesn't actually return one (only `ticker`), unlike Market's own
    from_dict. Not derived from `ticker` via string-splitting either —
    real positions returned here are for whatever the account happens to be
    trading (confirmed live: NBA/World Cup markets, nothing to do with this
    project's weather tickers), so there's no single reliable split pattern
    to assume the way weather tickers have one."""

    ticker: str
    contracts: float  # always positive; see `side` for direction
    side: str  # "yes" | "no", derived from position_fp's sign
    total_traded_dollars: float
    market_exposure_dollars: float
    realized_pnl_dollars: float
    fees_paid_dollars: float
    last_updated_ts: str | None
    raw: dict[str, Any] = field(repr=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Position":
        position_fp = _fixed_point_float(data, "position_fp")
        return cls(
            ticker=data["ticker"],
            contracts=abs(position_fp),
            side="yes" if position_fp >= 0 else "no",
            total_traded_dollars=_fixed_point_float(data, "total_traded_dollars"),
            market_exposure_dollars=_fixed_point_float(data, "market_exposure_dollars"),
            realized_pnl_dollars=_fixed_point_float(data, "realized_pnl_dollars"),
            fees_paid_dollars=_fixed_point_float(data, "fees_paid_dollars"),
            last_updated_ts=data.get("last_updated_ts"),
            raw=data,
        )
