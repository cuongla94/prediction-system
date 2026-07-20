from __future__ import annotations

from kalshi_client.models import Candlestick, Market


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
