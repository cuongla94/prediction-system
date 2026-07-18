from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


def _float_or_none(data: dict[str, Any], key: str) -> float | None:
    value = data.get(key)
    return float(value) if value is not None else None


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
