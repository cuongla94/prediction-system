from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any


def _parse_iso_utc(value: str | None) -> datetime | None:
    """Kalshi timestamps are ISO8601 UTC (e.g. '2026-07-20T11:06:19.871262Z').
    Parsed to a tz-aware datetime so the dashboard's `pacific` filter (which
    calls .astimezone) can render it like every other timestamp in the app,
    rather than choking on a bare string."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


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
class Candlestick:
    """One period's yes-bid/yes-ask close from GET .../candlesticks — an
    intraday price snapshot, unlike Market's single "current" quote.
    `end_period_ts` is the bucket's closing Unix timestamp (UTC); the bucket
    covering a given instant is the one whose `end_period_ts` is the smallest
    value >= that instant, at the requested `period_interval`. Close prices
    only — no open/high/low/volume — since same-day backtesting (the only
    current caller) needs "what could you have traded at, as of this
    moment," not the full candle.
    """

    end_period_ts: int
    yes_bid_close_dollars: float | None
    yes_ask_close_dollars: float | None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Candlestick":
        return cls(
            end_period_ts=data["end_period_ts"],
            yes_bid_close_dollars=_float_or_none(data.get("yes_bid") or {}, "close_dollars"),
            yes_ask_close_dollars=_float_or_none(data.get("yes_ask") or {}, "close_dollars"),
        )


@dataclass(frozen=True)
class Position:
    """One market's real position on the user's actual Kalshi account —
    from GET /portfolio/positions (authenticated, real holdings, not a
    forecast/signal). Distinct from this project's own paper_trades: this
    can include either manual account activity or separately attributed
    bot-owned activity.

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


@dataclass(frozen=True)
class Settlement:
    """One settled market on the user's real Kalshi account — from GET
    /portfolio/settlements (authenticated, read-only). This is the "History"
    tab on kalshi.com: what a position actually cost, what it paid out, and
    whether it won. Settlement itself remains exchange-controlled.

    **Unit gotcha, confirmed live 2026-07-22 and the reason net P&L is
    computed here rather than by callers:** the `*_cost_dollars` fields are
    decimal-string DOLLARS, but `revenue` is an integer in CENTS. Summing them
    naively (as if both were dollars) overstates payout 100x — caught only
    because a first pass produced a +$1,117 all-time total against an account
    actually showing ~$0. `revenue_dollars` below already divides by 100, so
    downstream code should use these normalized dollar fields, never the raw
    `revenue`.
    """

    ticker: str
    event_ticker: str
    market_result: str  # "yes" | "no" — how the market settled
    yes_count: float
    no_count: float
    cost_dollars: float  # what was paid to enter (yes + no legs), fees excluded
    revenue_dollars: float  # settlement payout received (normalized from cents)
    fee_dollars: float
    settled_time: datetime | None
    raw: dict[str, Any] = field(repr=False)

    @property
    def held_side(self) -> str:
        """Which side this account actually held. A market normally holds only
        one side; if both legs exist, the larger count wins the label."""
        return "yes" if self.yes_count >= self.no_count else "no"

    @property
    def won(self) -> bool:
        """Whether this settled position paid out at all. Defined as "received
        a payout" (`revenue_dollars > 0`) rather than "held_side ==
        market_result": the payout is the unambiguous ground truth and stays
        consistent with net P&L, whereas inferring the held side from
        yes/no counts mislabeled some $0-payout rows as wins (a real
        discrepancy caught 2026-07-22 — it inflated the account's apparent win
        rate). Note this is win-vs-loss, not profit-vs-loss: a contract bought
        at 95c that settles in the money "won" here even though it barely
        profited."""
        return self.revenue_dollars > 0

    @property
    def net_pnl_dollars(self) -> float:
        """Realized dollars: payout minus entry cost minus fees. Negative on a
        loss (payout 0), positive on a win. All three inputs already in
        dollars — see the unit gotcha in the class docstring."""
        return round(self.revenue_dollars - self.cost_dollars - self.fee_dollars, 4)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Settlement":
        market_result = data.get("market_result")
        if market_result not in {"yes", "no"}:
            value = data.get("value")
            market_result = "yes" if value == 100 else "no" if value == 0 else ""
        return cls(
            ticker=data["ticker"],
            event_ticker=data.get("event_ticker", ""),
            market_result=market_result,
            yes_count=_fixed_point_float(data, "yes_count_fp"),
            no_count=_fixed_point_float(data, "no_count_fp"),
            cost_dollars=(
                _fixed_point_float(data, "yes_total_cost_dollars")
                + _fixed_point_float(data, "no_total_cost_dollars")
            ),
            # revenue is CENTS (integer), unlike every *_dollars field — normalize here.
            revenue_dollars=float(data.get("revenue", 0)) / 100.0,
            fee_dollars=_fixed_point_float(data, "fee_cost"),
            settled_time=_parse_iso_utc(data.get("settled_time")),
            raw=data,
        )


@dataclass(frozen=True)
class Balance:
    """From GET /portfolio/balance (authenticated, read-only) — the account's
    current available cash. Added 2026-07-23 for Stage 3's capital-eligibility
    check, read-only like Position/Settlement above; nothing in this project
    writes to this endpoint or any order-placing one.

    **Field choice, confirmed live 2026-07-23**: the response carries both a
    legacy integer `balance` (whole-cent rounded — a live account showing
    "$0.0160" available came back as `balance: 1`, i.e. rounded to 1 cent,
    losing the fractional cent) and a `balance_dollars` decimal STRING
    ("0.0160") that matches `balance_breakdown[0].balance` exactly. This uses
    `balance_dollars` — same "FixedPointDollars decimal string, not cents"
    convention as Position's fields, and strictly more precise than the
    legacy integer. The legacy `balance` field is intentionally unused.
    """

    available_dollars: Decimal
    as_of: datetime | None
    raw: dict[str, Any] = field(repr=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Balance":
        updated_ts = data.get("updated_ts")
        return cls(
            available_dollars=Decimal(str(data.get("balance_dollars", "0"))),
            as_of=datetime.fromtimestamp(updated_ts, tz=UTC) if updated_ts else None,
            raw=data,
        )


@dataclass(frozen=True)
class OrderAcknowledgement:
    order_id: str
    client_order_id: str | None
    fill_count: Decimal
    remaining_count: Decimal
    average_fill_price: Decimal | None
    average_fee_paid: Decimal | None
    ts_ms: int | None
    raw: dict[str, Any] = field(repr=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "OrderAcknowledgement":
        return cls(
            order_id=data["order_id"],
            client_order_id=data.get("client_order_id"),
            fill_count=Decimal(str(data.get("fill_count", "0"))),
            remaining_count=Decimal(str(data.get("remaining_count", "0"))),
            average_fill_price=(
                Decimal(str(data["average_fill_price"]))
                if data.get("average_fill_price") is not None
                else None
            ),
            average_fee_paid=(
                Decimal(str(data["average_fee_paid"]))
                if data.get("average_fee_paid") is not None
                else None
            ),
            ts_ms=data.get("ts_ms"),
            raw=data,
        )


@dataclass(frozen=True)
class CancelOrderAcknowledgement:
    order_id: str
    client_order_id: str | None
    reduced_by: Decimal
    ts_ms: int | None
    raw: dict[str, Any] = field(repr=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CancelOrderAcknowledgement":
        return cls(
            order_id=data["order_id"],
            client_order_id=data.get("client_order_id"),
            reduced_by=Decimal(str(data.get("reduced_by", "0"))),
            ts_ms=data.get("ts_ms"),
            raw=data,
        )


@dataclass(frozen=True)
class Order:
    order_id: str
    client_order_id: str | None
    ticker: str
    outcome_side: str
    book_side: str
    status: str
    yes_price: Decimal
    fill_count: Decimal
    remaining_count: Decimal
    initial_count: Decimal
    fees_paid: Decimal
    created_time: datetime | None
    last_update_time: datetime | None
    raw: dict[str, Any] = field(repr=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Order":
        fill_count = Decimal(str(data.get("fill_count_fp", "0")))
        remaining_count = Decimal(str(data.get("remaining_count_fp", "0")))
        initial_count = Decimal(str(data.get("initial_count_fp", "0")))
        status = data.get("status")
        if not status:
            if remaining_count > 0:
                status = "resting"
            elif initial_count > 0 and fill_count >= initial_count:
                status = "executed"
            else:
                status = "canceled"
        return cls(
            order_id=data["order_id"],
            client_order_id=data.get("client_order_id"),
            ticker=data.get("ticker", ""),
            outcome_side=data.get("outcome_side") or data.get("side", ""),
            book_side=data.get("book_side", ""),
            status=status,
            yes_price=Decimal(str(data.get("yes_price_dollars", "0"))),
            fill_count=fill_count,
            remaining_count=remaining_count,
            initial_count=initial_count,
            fees_paid=(
                Decimal(str(data.get("taker_fees_dollars", "0")))
                + Decimal(str(data.get("maker_fees_dollars", "0")))
            ),
            created_time=_parse_iso_utc(data.get("created_time")),
            last_update_time=_parse_iso_utc(data.get("last_update_time")),
            raw=data,
        )


@dataclass(frozen=True)
class Fill:
    fill_id: str
    order_id: str
    ticker: str
    outcome_side: str
    book_side: str
    count: Decimal
    yes_price: Decimal
    fee: Decimal
    created_time: datetime | None
    raw: dict[str, Any] = field(repr=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Fill":
        return cls(
            fill_id=data["fill_id"],
            order_id=data.get("order_id", ""),
            ticker=data.get("ticker") or data.get("market_ticker", ""),
            outcome_side=data.get("outcome_side") or data.get("side", ""),
            book_side=data.get("book_side", ""),
            count=Decimal(str(data.get("count_fp", "0"))),
            yes_price=Decimal(str(data.get("yes_price_dollars", "0"))),
            fee=Decimal(str(data.get("fee_cost", "0"))),
            created_time=_parse_iso_utc(data.get("created_time")),
            raw=data,
        )


@dataclass(frozen=True)
class ExchangeStatus:
    exchange_active: bool
    trading_active: bool
    estimated_resume_time: datetime | None
    raw: dict[str, Any] = field(repr=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ExchangeStatus":
        return cls(
            exchange_active=bool(data.get("exchange_active")),
            trading_active=bool(data.get("trading_active")),
            estimated_resume_time=_parse_iso_utc(data.get("exchange_estimated_resume_time")),
            raw=data,
        )
