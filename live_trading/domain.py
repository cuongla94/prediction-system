from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal


@dataclass(frozen=True)
class LiveSignal:
    signal_id: str
    decision_id: str
    strategy_name: str
    strategy_version: str
    market_ticker: str
    event_ticker: str
    event_date: date
    decision_at: datetime
    weather_data_at: datetime
    quote_at: datetime
    close_time: datetime
    intended_outcome: str
    model_probability: Decimal
    maximum_acceptable_price: Decimal
    probability_sum: Decimal


@dataclass(frozen=True)
class OrderIntent:
    local_order_id: str
    client_order_id: str
    signal: LiveSignal
    api_book_side: str
    submitted_yes_price: Decimal
    outcome_price: Decimal
    requested_count: Decimal
    estimated_fees: Decimal
    expires_at: datetime


@dataclass(frozen=True)
class LiveOrder:
    id: int
    local_order_id: str
    client_order_id: str
    kalshi_order_id: str | None
    market_ticker: str
    event_ticker: str
    event_date: date
    intended_outcome: str
    api_book_side: str
    submitted_yes_price: Decimal
    model_probability: Decimal
    maximum_acceptable_price: Decimal
    requested_count: Decimal
    filled_count: Decimal
    remaining_count: Decimal
    average_fill_price: Decimal | None
    estimated_fees: Decimal
    actual_fees: Decimal
    status: str
    decision_at: datetime
    quote_at: datetime
    weather_data_at: datetime
    expires_at: datetime | None
    reconciliation_status: str


@dataclass(frozen=True)
class LiveRiskState:
    market_exposure: Decimal = Decimal("0")
    event_exposure: Decimal = Decimal("0")
    total_exposure: Decimal = Decimal("0")
    daily_realized_pnl: Decimal = Decimal("0")
    daily_mark_to_market_pnl: Decimal = Decimal("0")
    consecutive_settled_losses: int = 0
    has_unknown_order: bool = False
    reconciliation_healthy: bool = False


@dataclass(frozen=True)
class RiskVerdict:
    allowed: bool
    blockers: tuple[str, ...]
    order_cost_including_fees: Decimal


@dataclass(frozen=True)
class ReconciliationResult:
    healthy: bool
    available_cash: Decimal
    balance_as_of: datetime | None
    mismatches: tuple[str, ...]
    reconciled_orders: int
    fills: int
    positions: int
    settlements: int


@dataclass(frozen=True)
class CycleResult:
    status: str
    submitted_orders: int = 0
    reconciled_orders: int = 0
    canceled_orders: int = 0
    blocker: str | None = None
    error: str | None = None
