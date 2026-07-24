from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal

from kalshi_client import MarketOrderbook
from kalshi_client.fees import taker_fee


@dataclass(frozen=True)
class ConservativeFill:
    status: str
    requested_quantity: Decimal
    filled_quantity: Decimal
    weighted_fill_price: Decimal | None
    estimated_fee: Decimal
    unfilled_quantity: Decimal
    reason: str

    def to_dict(self) -> dict:
        return {
            key: str(value) if isinstance(value, Decimal) else value
            for key, value in asdict(self).items()
        }


def conservative_fill(
    orderbook: MarketOrderbook,
    *,
    outcome: str,
    limit_price: Decimal,
    requested_quantity: Decimal,
    book_is_fresh: bool,
    market_is_active: bool,
) -> ConservativeFill:
    """Immediate-taker evidence only; last trades never imply a fill.

    Visible opposing depth at or better than the frozen limit is consumed
    level by level. Missing depth is a no-fill, and insufficient depth is a
    partial fill rather than an optimistic complete fill.
    """
    if requested_quantity <= 0:
        raise ValueError("requested_quantity must be positive")
    if not book_is_fresh:
        return ConservativeFill(
            "NO_FILL",
            requested_quantity,
            Decimal("0"),
            None,
            Decimal("0"),
            requested_quantity,
            "STALE_ORDERBOOK",
        )
    if not market_is_active:
        return ConservativeFill(
            "NO_FILL",
            requested_quantity,
            Decimal("0"),
            None,
            Decimal("0"),
            requested_quantity,
            "MARKET_NOT_ACTIVE",
        )
    asks = [
        level
        for level in orderbook.asks_for(outcome)
        if level.price <= limit_price and level.quantity > 0
    ]
    remaining = requested_quantity
    fills: list[tuple[Decimal, Decimal]] = []
    for level in asks:
        quantity = min(remaining, level.quantity)
        if quantity > 0:
            fills.append((level.price, quantity))
            remaining -= quantity
        if remaining == 0:
            break
    filled = requested_quantity - remaining
    if filled == 0:
        return ConservativeFill(
            "NO_FILL",
            requested_quantity,
            Decimal("0"),
            None,
            Decimal("0"),
            requested_quantity,
            "NO_EXECUTABLE_DEPTH_AT_LIMIT",
        )
    notional = sum(price * quantity for price, quantity in fills)
    weighted = (notional / filled).quantize(Decimal("0.0001"))
    fee = Decimal(str(taker_fee(float(weighted), float(filled)))).quantize(
        Decimal("0.0001")
    )
    return ConservativeFill(
        "FILLED" if remaining == 0 else "PARTIAL_FILL",
        requested_quantity,
        filled,
        weighted,
        fee,
        remaining,
        "VISIBLE_DEPTH_CONSUMED_CONSERVATIVELY",
    )
