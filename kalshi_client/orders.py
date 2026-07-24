"""Kalshi V2 event-order helpers.

The V2 event endpoint exposes a single YES-side book. Strategy code speaks in
the economically clearer "BUY YES" / "BUY NO" vocabulary; this module is the
only place that translates those intentions into Kalshi's `bid` / `ask`
request fields.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

_PRICE_QUANTUM = Decimal("0.0001")
_COUNT_QUANTUM = Decimal("0.01")
_ONE = Decimal("1.0000")


def _decimal(value: Decimal | str | int) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"Invalid fixed-point value: {value!r}") from exc
    if not result.is_finite():
        raise ValueError(f"Fixed-point value must be finite: {value!r}")
    return result


def format_price(value: Decimal | str) -> str:
    price = _decimal(value)
    if price <= 0 or price >= 1:
        raise ValueError("Order price must be strictly between 0 and 1 dollar.")
    rounded = price.quantize(_PRICE_QUANTUM, rounding=ROUND_HALF_UP)
    if rounded <= 0 or rounded >= 1:
        raise ValueError("Order price rounds outside Kalshi's open interval.")
    return format(rounded, ".4f")


def format_count(value: Decimal | str | int) -> str:
    count = _decimal(value)
    if count <= 0:
        raise ValueError("Order count must be positive.")
    rounded = count.quantize(_COUNT_QUANTUM, rounding=ROUND_HALF_UP)
    if rounded <= 0:
        raise ValueError("Order count rounds to zero.")
    return format(rounded, ".2f")


@dataclass(frozen=True)
class EventOrderBookIntent:
    intended_outcome: str
    book_side: str
    yes_price: Decimal

    @property
    def price_string(self) -> str:
        return format_price(self.yes_price)


def to_event_order_book(
    intended_outcome: str,
    intended_price: Decimal | str,
) -> EventOrderBookIntent:
    """Translate a strategy BUY into Kalshi V2's YES-side order book.

    `intended_price` is the maximum dollar price for the outcome being bought.
    Buying NO at N is represented as selling YES (`ask`) at 1-N.
    """
    outcome = intended_outcome.strip().upper().replace("BUY ", "")
    price = _decimal(intended_price)
    if price <= 0 or price >= 1:
        raise ValueError("Intended outcome price must be strictly between 0 and 1 dollar.")
    if outcome == "YES":
        return EventOrderBookIntent(
            "YES", "bid", price.quantize(_PRICE_QUANTUM, rounding=ROUND_HALF_UP)
        )
    if outcome == "NO":
        return EventOrderBookIntent(
            "NO",
            "ask",
            (_ONE - price).quantize(_PRICE_QUANTUM, rounding=ROUND_HALF_UP),
        )
    raise ValueError("intended_outcome must be YES, NO, BUY YES, or BUY NO.")
