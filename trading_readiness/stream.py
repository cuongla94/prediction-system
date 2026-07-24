from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from kalshi_client import MarketOrderbook, OrderbookLevel


def _levels(
    values: list[list[Any]] | None, *, no_side_uses_yes_scale: bool
) -> dict[Decimal, Decimal]:
    result: dict[Decimal, Decimal] = {}
    for price_value, quantity_value, *_ in values or []:
        price = Decimal(str(price_value))
        if no_side_uses_yes_scale:
            price = Decimal("1") - price
        quantity = Decimal(str(quantity_value))
        if quantity > 0:
            result[price] = quantity
    return result


@dataclass
class OrderbookState:
    ticker: str
    yes_bids: dict[Decimal, Decimal] = field(default_factory=dict)
    no_bids: dict[Decimal, Decimal] = field(default_factory=dict)
    last_sequence: int | None = None
    source_publish_time: datetime | None = None
    collector_received_time: datetime | None = None
    recovery_reason: str | None = None

    @classmethod
    def from_rest(
        cls,
        book: MarketOrderbook,
        *,
        received_at: datetime,
        recovery_reason: str | None = None,
    ) -> "OrderbookState":
        return cls(
            ticker=book.ticker,
            yes_bids={level.price: level.quantity for level in book.yes_bids},
            no_bids={level.price: level.quantity for level in book.no_bids},
            collector_received_time=received_at,
            recovery_reason=recovery_reason,
        )

    @classmethod
    def from_websocket_snapshot(
        cls,
        message: dict[str, Any],
        *,
        received_at: datetime,
        use_yes_price: bool,
    ) -> "OrderbookState":
        payload = message.get("msg") or {}
        source_time = _message_time(payload)
        return cls(
            ticker=payload["market_ticker"],
            yes_bids=_levels(
                payload.get("yes_dollars_fp")
                or payload.get("yes_dollars"),
                no_side_uses_yes_scale=False,
            ),
            no_bids=_levels(
                payload.get("no_dollars_fp")
                or payload.get("no_dollars"),
                no_side_uses_yes_scale=use_yes_price,
            ),
            last_sequence=_optional_int(message.get("seq")),
            source_publish_time=source_time,
            collector_received_time=received_at,
        )

    def apply_delta(
        self,
        message: dict[str, Any],
        *,
        received_at: datetime,
        use_yes_price: bool,
        enforce_sequence: bool = True,
    ) -> None:
        payload = message.get("msg") or {}
        if payload.get("market_ticker") != self.ticker:
            raise ValueError("Orderbook delta ticker does not match state.")
        sequence = _optional_int(message.get("seq"))
        if (
            enforce_sequence
            and
            sequence is not None
            and self.last_sequence is not None
            and sequence != self.last_sequence + 1
        ):
            raise SequenceGap(self.last_sequence, sequence)
        side = payload.get("side")
        if side not in {"yes", "no"}:
            raise ValueError("Orderbook delta side must be yes or no.")
        price = Decimal(str(payload["price_dollars"]))
        if side == "no" and use_yes_price:
            price = Decimal("1") - price
        delta = Decimal(str(payload.get("delta_fp") or payload.get("delta") or "0"))
        levels = self.yes_bids if side == "yes" else self.no_bids
        quantity = levels.get(price, Decimal("0")) + delta
        if quantity < 0:
            raise ValueError("Orderbook delta produced negative depth.")
        if quantity == 0:
            levels.pop(price, None)
        else:
            levels[price] = quantity
        self.last_sequence = sequence
        self.source_publish_time = _message_time(payload)
        self.collector_received_time = received_at
        self.recovery_reason = None

    def as_orderbook(self) -> MarketOrderbook:
        return MarketOrderbook(
            ticker=self.ticker,
            yes_bids=tuple(
                OrderbookLevel(price, quantity)
                for price, quantity in sorted(
                    self.yes_bids.items(), reverse=True
                )
            ),
            no_bids=tuple(
                OrderbookLevel(price, quantity)
                for price, quantity in sorted(
                    self.no_bids.items(), reverse=True
                )
            ),
            raw={},
        )

    def diagnostics(
        self,
        *,
        now: datetime,
        stale_after: timedelta,
        delayed_after: timedelta,
    ) -> dict[str, bool]:
        book = self.as_orderbook()
        impossible = any(
            price < 0 or price > 1 or quantity <= 0
            for levels in (self.yes_bids, self.no_bids)
            for price, quantity in levels.items()
        )
        crossed = bool(
            book.best_yes_bid is not None
            and book.best_no_bid is not None
            and book.best_yes_bid + book.best_no_bid > Decimal("1")
        )
        stale = bool(
            self.collector_received_time is None
            or now - self.collector_received_time > stale_after
        )
        delayed = bool(
            self.source_publish_time is not None
            and self.collector_received_time is not None
            and self.collector_received_time - self.source_publish_time
            > delayed_after
        )
        return {
            "stale": stale,
            "crossed_or_impossible": crossed or impossible,
            "missing_opposing_levels": not (
                self.yes_bids and self.no_bids
            ),
            "delayed_local_receipt": delayed,
        }


class SequenceGap(Exception):
    def __init__(self, previous: int, received: int):
        self.previous = previous
        self.received = received
        super().__init__(f"Expected sequence {previous + 1}, received {received}.")


def _optional_int(value: Any) -> int | None:
    return int(value) if value is not None else None


def _message_time(payload: dict[str, Any]) -> datetime | None:
    if payload.get("ts_ms") is not None:
        return datetime.fromtimestamp(int(payload["ts_ms"]) / 1000, tz=UTC)
    value = payload.get("time") or payload.get("ts")
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=UTC)
    return None
