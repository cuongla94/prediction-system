from __future__ import annotations

from datetime import date

import pytest

from kalshi_client.tickers import parse_event_date


def test_parse_event_date_matches_real_ticker():
    assert parse_event_date("KXHIGHNY-26JUL18") == date(2026, 7, 18)


def test_parse_event_date_handles_different_month_and_padded_day():
    assert parse_event_date("KXHIGHNY-26JAN05") == date(2026, 1, 5)


def test_parse_event_date_rejects_unparseable_ticker():
    with pytest.raises(ValueError):
        parse_event_date("KXHIGHNY-NOTADATE")


def test_parse_event_date_rejects_market_ticker_with_bracket_suffix():
    # The date must anchor to the end of the string — a market ticker (event
    # ticker + bracket suffix) shouldn't silently match against the wrong tail.
    with pytest.raises(ValueError):
        parse_event_date("KXHIGHNY-26JUL18-B79.5")
