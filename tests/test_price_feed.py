from __future__ import annotations

from price_feed.subscriber import _parse_ticker_message


def test_parses_a_real_shaped_ticker_message():
    data = {
        "type": "ticker",
        "msg": {
            "market_ticker": "KXHIGHNY-26JUL19-T80",
            "yes_bid_dollars": 0.30,
            "yes_ask_dollars": 0.34,
            "ts_ms": 1753000000000,
        },
    }
    parsed = _parse_ticker_message(data)
    assert parsed == ("KXHIGHNY-26JUL19-T80", 0.32, 1753000000000)


def test_ignores_non_ticker_message_types():
    assert _parse_ticker_message({"type": "subscribed", "msg": {"channel": "ticker", "sid": 1}}) is None


def test_ignores_ticker_message_missing_bid_or_ask():
    data = {"type": "ticker", "msg": {"market_ticker": "T1", "yes_bid_dollars": None, "ts_ms": 1}}
    assert _parse_ticker_message(data) is None


def test_ignores_message_with_no_type_key():
    assert _parse_ticker_message({}) is None
