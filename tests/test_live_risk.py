from __future__ import annotations

from decimal import Decimal

import pytest

from live_trading import LiveRiskState
from live_trading.repository import _order_exposure
from live_trading.risk import estimated_taker_fee, validate_fixed_limits


def _verdict(**state_overrides):
    state = LiveRiskState(reconciliation_healthy=True, **state_overrides)
    return validate_fixed_limits(
        count=Decimal("1"),
        outcome_price=Decimal("0.50"),
        estimated_fees=estimated_taker_fee(Decimal("0.50"), Decimal("1")),
        state=state,
    )


def test_fixed_limits_allow_one_small_reconciled_order():
    assert _verdict().allowed


@pytest.mark.parametrize(
    ("overrides", "blocker"),
    [
        ({"market_exposure": Decimal("0.60")}, "MAX_MARKET_EXPOSURE"),
        ({"event_exposure": Decimal("1.60")}, "MAX_EVENT_DATE_EXPOSURE"),
        ({"total_exposure": Decimal("4.60")}, "MAX_TOTAL_EXPOSURE"),
        ({"daily_realized_pnl": Decimal("-2.00")}, "MAX_DAILY_REALIZED_LOSS"),
        ({"daily_mark_to_market_pnl": Decimal("-3.00")}, "MAX_DAILY_MARK_TO_MARKET_LOSS"),
        ({"consecutive_settled_losses": 2}, "MAX_CONSECUTIVE_SETTLED_LOSSES"),
        ({"has_unknown_order": True}, "UNKNOWN_ORDER"),
    ],
)
def test_fixed_limits_block_each_backend_risk(overrides, blocker):
    assert blocker in _verdict(**overrides).blockers


def test_more_than_one_contract_is_blocked():
    verdict = validate_fixed_limits(
        count=Decimal("1.01"),
        outcome_price=Decimal("0.20"),
        estimated_fees=Decimal("0"),
        state=LiveRiskState(reconciliation_healthy=True),
    )
    assert "MAX_CONTRACTS_PER_ORDER" in verdict.blockers


def test_one_dollar_total_cost_including_fee_is_strictly_enforced():
    verdict = validate_fixed_limits(
        count=Decimal("1"),
        outcome_price=Decimal("0.9990"),
        estimated_fees=Decimal("0.0020"),
        state=LiveRiskState(reconciliation_healthy=True),
    )
    assert "MAX_ORDER_COST" in verdict.blockers


def test_unfilled_canceled_order_has_zero_exposure():
    assert _order_exposure(
        status="CANCELED",
        intended_outcome="YES",
        submitted_yes_price=Decimal("0.5000"),
        requested_count=Decimal("1"),
        filled_count=Decimal("0"),
        remaining_count=Decimal("0"),
        estimated_fees=Decimal("0.02"),
        actual_fees=Decimal("0"),
    ) == Decimal("0.0000")


def test_partial_canceled_order_keeps_only_filled_exposure_and_actual_fee():
    assert _order_exposure(
        status="CANCELED",
        intended_outcome="NO",
        submitted_yes_price=Decimal("0.6000"),
        requested_count=Decimal("1"),
        filled_count=Decimal("0.25"),
        remaining_count=Decimal("0"),
        estimated_fees=Decimal("0.02"),
        actual_fees=Decimal("0.0040"),
    ) == Decimal("0.1040")
