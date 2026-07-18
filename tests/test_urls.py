from __future__ import annotations

from kalshi_client.urls import market_url, slugify


def test_slugify_matches_kalshi_convention():
    assert slugify("Highest temperature in NYC") == "highest-temperature-in-nyc"


def test_slugify_strips_punctuation():
    assert slugify("Rain? In Seattle!!") == "rain-in-seattle"


def test_market_url_matches_brief_example():
    url = market_url("KXHIGHNY", "Highest temperature in NYC", "KXHIGHNY-26JUL17")
    assert url == "https://kalshi.com/markets/kxhighny/highest-temperature-in-nyc/kxhighny-26jul17"
