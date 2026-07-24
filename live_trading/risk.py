"""Immutable backend production limits.

There are intentionally no environment variables or request fields for these
values. Increasing one requires a reviewed code change.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_UP

from .domain import LiveRiskState, RiskVerdict

MAX_CONTRACTS_PER_ORDER = Decimal("1.00")
MAX_ORDER_COST = Decimal("1.00")
MAX_MARKET_EXPOSURE = Decimal("1.00")
MAX_EVENT_DATE_EXPOSURE = Decimal("2.00")
MAX_TOTAL_EXPOSURE = Decimal("5.00")
MAX_DAILY_REALIZED_LOSS = Decimal("2.00")
MAX_DAILY_MARK_TO_MARKET_LOSS = Decimal("3.00")
MAX_CONSECUTIVE_SETTLED_LOSSES = 2


def estimated_taker_fee(price: Decimal, count: Decimal) -> Decimal:
    return (
        Decimal("0.07") * price * (Decimal("1") - price) * count
    ).quantize(Decimal("0.0001"), rounding=ROUND_UP)


def fixed_limits_dict() -> dict[str, str | int]:
    return {
        "maximum_contracts_per_order": str(MAX_CONTRACTS_PER_ORDER),
        "maximum_order_cost_including_estimated_fees": str(MAX_ORDER_COST),
        "maximum_bot_exposure_per_market": str(MAX_MARKET_EXPOSURE),
        "maximum_bot_exposure_per_event_date": str(MAX_EVENT_DATE_EXPOSURE),
        "maximum_total_bot_exposure": str(MAX_TOTAL_EXPOSURE),
        "maximum_daily_realized_loss": str(MAX_DAILY_REALIZED_LOSS),
        "maximum_daily_mark_to_market_loss": str(MAX_DAILY_MARK_TO_MARKET_LOSS),
        "maximum_consecutive_settled_losses": MAX_CONSECUTIVE_SETTLED_LOSSES,
        "minimum_available_cash": "> 5.00",
        "order_type": "limit only",
    }


def validate_fixed_limits(
    *,
    count: Decimal,
    outcome_price: Decimal,
    estimated_fees: Decimal,
    state: LiveRiskState,
) -> RiskVerdict:
    order_cost = (count * outcome_price + estimated_fees).quantize(Decimal("0.0001"))
    blockers: list[str] = []
    if count > MAX_CONTRACTS_PER_ORDER:
        blockers.append("MAX_CONTRACTS_PER_ORDER")
    if order_cost > MAX_ORDER_COST:
        blockers.append("MAX_ORDER_COST")
    if state.market_exposure + order_cost > MAX_MARKET_EXPOSURE:
        blockers.append("MAX_MARKET_EXPOSURE")
    if state.event_exposure + order_cost > MAX_EVENT_DATE_EXPOSURE:
        blockers.append("MAX_EVENT_DATE_EXPOSURE")
    if state.total_exposure + order_cost > MAX_TOTAL_EXPOSURE:
        blockers.append("MAX_TOTAL_EXPOSURE")
    if state.daily_realized_pnl <= -MAX_DAILY_REALIZED_LOSS:
        blockers.append("MAX_DAILY_REALIZED_LOSS")
    if state.daily_mark_to_market_pnl <= -MAX_DAILY_MARK_TO_MARKET_LOSS:
        blockers.append("MAX_DAILY_MARK_TO_MARKET_LOSS")
    if state.consecutive_settled_losses >= MAX_CONSECUTIVE_SETTLED_LOSSES:
        blockers.append("MAX_CONSECUTIVE_SETTLED_LOSSES")
    if state.has_unknown_order:
        blockers.append("UNKNOWN_ORDER")
    if not state.reconciliation_healthy:
        blockers.append("RECONCILIATION_REQUIRED")
    return RiskVerdict(not blockers, tuple(blockers), order_cost)
