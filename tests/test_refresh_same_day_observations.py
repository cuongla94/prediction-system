from __future__ import annotations

from unittest.mock import patch

from kalshi_client import Event, Market
from scripts.refresh_same_day_observations import fetch_known_pricing_inputs, refresh_city


class FakeCursor:
    """Returns one canned result set for fetch_known_pricing_inputs; records
    the query so tests can assert it filtered on the right series."""

    def __init__(self, rows: list[tuple]):
        self._rows = rows
        self.last_params: tuple | None = None

    def execute(self, _sql, params=None):
        self.last_params = params

    def fetchall(self):
        return self._rows


class FakeClient:
    """Stands in for KalshiClient's two read calls refresh_city makes."""

    def __init__(self, events: list[Event], markets: list[Market]):
        self._events = events
        self._markets = markets
        self.get_markets_calls: list[str] = []

    def get_events(self, *, series_ticker, status, limit):  # noqa: ARG002
        return self._events, None

    def get_markets(self, *, event_ticker, limit):  # noqa: ARG002
        self.get_markets_calls.append(event_ticker)
        return self._markets, None


def _event(event_ticker: str) -> Event:
    return Event(
        event_ticker=event_ticker, series_ticker="KXHIGHNY", title="", sub_title="",
        strike_date=None, strike_period=None, mutually_exclusive=True, raw={},
    )


def _market(ticker: str, *, yes_bid=0.30, yes_ask=0.34) -> Market:
    return Market(
        ticker=ticker, event_ticker="KXHIGHNY-26JUL20", status="open", title="",
        yes_sub_title="", no_sub_title="",
        rules_primary="is between 79-80°, then the market resolves to Yes.",
        rules_secondary="", floor_strike=79.0, cap_strike=80.0,
        yes_bid_dollars=yes_bid, yes_ask_dollars=yes_ask,
        no_bid_dollars=None, no_ask_dollars=None, last_price_dollars=None,
        close_time="2026-07-21T04:00:00Z", raw={},
    )


def test_fetch_known_pricing_inputs_filters_by_series_lead_days_and_unsettled():
    cur = FakeCursor([("KXHIGHNY-26JUL20-B79.5", 82.0, 2.1, "https://kalshi.com/x")])
    result = fetch_known_pricing_inputs(cur, "KXHIGHNY")
    assert result == {"KXHIGHNY-26JUL20-B79.5": (82.0, 2.1, "https://kalshi.com/x")}
    assert cur.last_params == ("KXHIGHNY",)


def test_refresh_city_returns_nothing_when_nothing_known_yet():
    # generate_alerts.py hasn't priced anything for today yet -- there is no
    # baseline to refresh from, so this must not try to invent one.
    cur = FakeCursor([])
    client = FakeClient(events=[_event("KXHIGHNY-26JUL20")], markets=[_market("KXHIGHNY-26JUL20-B79.5")])
    with patch("scripts.refresh_same_day_observations.fetch_today_extreme", return_value=(82.0, "2026-07-20T18:00:00Z")):
        rows = refresh_city(client, cur, "KXHIGHNY")
    assert rows == []
    assert client.get_markets_calls == []  # short-circuited before any Kalshi calls


def test_refresh_city_returns_nothing_when_todays_event_is_not_open():
    # Known pricing inputs exist (generate_alerts.py has run before), but no
    # currently-open event matches today's date -- nothing to refresh.
    cur = FakeCursor([("KXHIGHNY-26JUL19-B79.5", 82.0, 2.1, "https://kalshi.com/x")])
    client = FakeClient(events=[_event("KXHIGHNY-26JUL25")], markets=[])
    with patch("scripts.refresh_same_day_observations.datetime") as mock_dt:
        import datetime as real_datetime

        mock_dt.now.return_value = real_datetime.datetime(2026, 7, 20, 12, 0, tzinfo=real_datetime.UTC)
        rows = refresh_city(client, cur, "KXHIGHNY")
    assert rows == []


def test_refresh_city_prices_only_markets_with_a_known_baseline():
    known_ticker = "KXHIGHNY-26JUL20-B79.5"
    unknown_ticker = "KXHIGHNY-26JUL20-T77"
    cur = FakeCursor([(known_ticker, 82.0, 2.2, "https://kalshi.com/x")])
    client = FakeClient(
        events=[_event("KXHIGHNY-26JUL20")],
        markets=[_market(known_ticker), _market(unknown_ticker, yes_bid=0.90, yes_ask=0.95)],
    )
    with (
        patch("scripts.refresh_same_day_observations.fetch_today_extreme", return_value=(81.0, "2026-07-20T18:00:00Z")),
        patch("scripts.refresh_same_day_observations.datetime") as mock_dt,
    ):
        import datetime as real_datetime

        mock_dt.now.return_value = real_datetime.datetime(2026, 7, 20, 12, 0, tzinfo=real_datetime.UTC)
        rows = refresh_city(client, cur, "KXHIGHNY")

    assert len(rows) == 1
    assert rows[0]["market_ticker"] == known_ticker
    assert rows[0]["lead_days"] == 0
    assert rows[0]["observed_so_far"] == 81.0
    assert rows[0]["kalshi_url"] == "https://kalshi.com/x"
    assert client.get_markets_calls == ["KXHIGHNY-26JUL20"]


def test_refresh_city_uses_the_stored_kalshi_url_not_a_reconstructed_one():
    # market_url() needs a real series title to build a correct slug; that is
    # not cheaply available here without another API call, so this must reuse
    # whatever kalshi_url was already stored rather than rebuilding it (which
    # would produce a broken URL with an empty title/slug).
    cur = FakeCursor([("KXHIGHNY-26JUL20-B79.5", 82.0, 2.2, "https://kalshi.com/markets/kxhighny/real-slug/kxhighny-26jul20")])
    client = FakeClient(events=[_event("KXHIGHNY-26JUL20")], markets=[_market("KXHIGHNY-26JUL20-B79.5")])
    with (
        patch("scripts.refresh_same_day_observations.fetch_today_extreme", return_value=None),
        patch("scripts.refresh_same_day_observations.datetime") as mock_dt,
    ):
        import datetime as real_datetime

        mock_dt.now.return_value = real_datetime.datetime(2026, 7, 20, 12, 0, tzinfo=real_datetime.UTC)
        rows = refresh_city(client, cur, "KXHIGHNY")
    assert rows[0]["kalshi_url"] == "https://kalshi.com/markets/kxhighny/real-slug/kxhighny-26jul20"


def test_refresh_city_handles_no_observation_yet_gracefully():
    # fetch_today_extreme returning None (e.g. right after local midnight)
    # must degrade to unconditional pricing, not crash.
    cur = FakeCursor([("KXHIGHNY-26JUL20-B79.5", 82.0, 2.2, "https://kalshi.com/x")])
    client = FakeClient(events=[_event("KXHIGHNY-26JUL20")], markets=[_market("KXHIGHNY-26JUL20-B79.5")])
    with (
        patch("scripts.refresh_same_day_observations.fetch_today_extreme", return_value=None),
        patch("scripts.refresh_same_day_observations.datetime") as mock_dt,
    ):
        import datetime as real_datetime

        mock_dt.now.return_value = real_datetime.datetime(2026, 7, 20, 12, 0, tzinfo=real_datetime.UTC)
        rows = refresh_city(client, cur, "KXHIGHNY")
    assert len(rows) == 1
    assert rows[0]["observed_so_far"] is None
