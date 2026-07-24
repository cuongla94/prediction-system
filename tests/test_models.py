from __future__ import annotations

from decimal import Decimal

import pytest

from kalshi_client.models import Balance, Candlestick, Market, Settlement


def _market(floor_strike: float | None, cap_strike: float | None) -> Market:
    return Market(
        ticker="TEST",
        event_ticker="TEST-EVENT",
        status="active",
        title="",
        yes_sub_title="",
        no_sub_title="",
        rules_primary="",
        rules_secondary="",
        floor_strike=floor_strike,
        cap_strike=cap_strike,
        yes_bid_dollars=None,
        yes_ask_dollars=None,
        no_bid_dollars=None,
        no_ask_dollars=None,
        last_price_dollars=None,
        close_time=None,
        raw={},
    )


def test_bracket_label_cap_only():
    assert _market(None, 79.0).bracket_label == "< 79°"


def test_bracket_label_floor_only():
    assert _market(86.0, None).bracket_label == "> 86°"


def test_bracket_label_between():
    assert _market(79.0, 80.0).bracket_label == "79–80°"


def test_bracket_label_neither_set():
    assert _market(None, None).bracket_label == "?"


def test_candlestick_from_dict_parses_close_prices():
    data = {
        "end_period_ts": 1784437740,
        "yes_bid": {"close_dollars": "0.4300", "open_dollars": "0.4100"},
        "yes_ask": {"close_dollars": "0.4700", "open_dollars": "0.4500"},
    }
    candle = Candlestick.from_dict(data)
    assert candle.end_period_ts == 1784437740
    assert candle.yes_bid_close_dollars == 0.43
    assert candle.yes_ask_close_dollars == 0.47


def test_candlestick_from_dict_handles_missing_quote_side():
    # A period with no resting bid (or ask) at all — a real, if uncommon,
    # state for these thin weather markets, not an API error.
    data = {"end_period_ts": 1784437740, "yes_bid": {}, "yes_ask": {"close_dollars": "0.9900"}}
    candle = Candlestick.from_dict(data)
    assert candle.yes_bid_close_dollars is None
    assert candle.yes_ask_close_dollars == 0.99


# --- Settlement: the cents-vs-dollars gotcha is the whole reason net P&L lives
#     in the model, so it's the thing worth pinning hardest.


def test_settlement_normalizes_revenue_from_cents_to_dollars():
    # A real winning row (World Cup BTTS): held 20.1 YES contracts that settled
    # YES, so payout ~= $20.10. The API returns revenue=2008 (CENTS) while the
    # cost fields are dollar strings — the model must divide revenue by 100 or
    # net P&L is overstated 100x (this exact bug shipped a +$1,117 phantom
    # total against a ~$0 account before being caught).
    s = Settlement.from_dict({
        "ticker": "KXWC2HBTTS-26JUL18FRAEN",
        "market_result": "yes",
        "yes_count_fp": "20.10",
        "no_count_fp": "0.00",
        "yes_total_cost_dollars": "9.640000",
        "no_total_cost_dollars": "0.000000",
        "revenue": 2008,
        "fee_cost": "0.170000",
    })
    assert s.revenue_dollars == pytest.approx(20.08)
    assert s.cost_dollars == pytest.approx(9.64)
    assert s.won is True
    assert s.held_side == "yes"
    assert s.net_pnl_dollars == pytest.approx(20.08 - 9.64 - 0.17)


def test_settlement_loss_is_negative_with_zero_revenue():
    # The Chicago low-temp bet: held YES, settled NO, $0 payout — a total loss
    # of cost plus fee.
    s = Settlement.from_dict({
        "ticker": "KXLOWTCHI-26JUL19-B68.5",
        "market_result": "no",
        "yes_count_fp": "14.77",
        "no_count_fp": "0.00",
        "yes_total_cost_dollars": "9.412800",
        "no_total_cost_dollars": "0.000000",
        "revenue": 0,
        "fee_cost": "0.239000",
    })
    assert s.won is False
    assert s.revenue_dollars == 0.0
    assert s.net_pnl_dollars == pytest.approx(-(9.4128 + 0.239), abs=1e-3)


@pytest.mark.parametrize(("value", "expected"), [(100, "yes"), (0, "no")])
def test_settlement_derives_result_from_current_value_field(value, expected):
    settlement = Settlement.from_dict(
        {
            "ticker": "X",
            "value": value,
            "yes_count_fp": "1.00",
            "no_count_fp": "0.00",
            "yes_total_cost_dollars": "0.50",
            "no_total_cost_dollars": "0.00",
            "revenue": value,
            "fee_cost": "0.00",
        }
    )
    assert settlement.market_result == expected


def test_settlement_parses_settled_time_to_aware_datetime():
    s = Settlement.from_dict({
        "ticker": "X", "market_result": "yes",
        "yes_count_fp": "1.0", "no_count_fp": "0.0",
        "yes_total_cost_dollars": "0.5", "no_total_cost_dollars": "0.0",
        "revenue": 100, "fee_cost": "0.0",
        "settled_time": "2026-07-20T11:06:19.871262Z",
    })
    assert s.settled_time is not None
    assert s.settled_time.year == 2026 and s.settled_time.month == 7 and s.settled_time.day == 20
    assert s.settled_time.utcoffset().total_seconds() == 0  # tz-aware UTC, renderable by the pacific filter


def test_settlement_held_side_prefers_the_larger_leg():
    s = Settlement.from_dict({
        "ticker": "X", "market_result": "no",
        "yes_count_fp": "1.00", "no_count_fp": "10.00",
        "yes_total_cost_dollars": "0.50", "no_total_cost_dollars": "3.00",
        "revenue": 1000, "fee_cost": "0.05",
    })
    assert s.held_side == "no"
    assert s.won is True


def test_balance_uses_balance_dollars_not_the_legacy_cent_rounded_integer():
    # Confirmed live 2026-07-23: a real account showing $0.0160 available
    # returns balance_dollars="0.0160" but the legacy integer balance=1
    # (rounded to the nearest whole cent, losing the fractional cent).
    b = Balance.from_dict({
        "balance": 1,
        "balance_breakdown": [{"balance": "0.0160", "exchange_index": 0}],
        "balance_dollars": "0.0160",
        "portfolio_value": 0,
        "updated_ts": 1784845281,
    })
    assert b.available_dollars == Decimal("0.0160")


def test_balance_as_of_parses_unix_timestamp():
    b = Balance.from_dict({"balance_dollars": "5.00", "updated_ts": 1784845281})
    assert b.as_of is not None
    assert b.as_of.tzinfo is not None


def test_balance_missing_fields_default_safely():
    b = Balance.from_dict({})
    assert b.available_dollars == Decimal("0")
    assert b.as_of is None
