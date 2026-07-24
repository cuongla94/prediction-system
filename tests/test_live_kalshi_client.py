from __future__ import annotations

from decimal import Decimal

import pytest

from kalshi_client import KalshiClient, to_event_order_book
from kalshi_client.orders import format_count


def test_buy_yes_uses_yes_bid_book_and_same_price():
    result = to_event_order_book("BUY YES", Decimal("0.4200"))
    assert result.book_side == "bid"
    assert result.price_string == "0.4200"


def test_buy_no_uses_yes_ask_book_and_complementary_price():
    result = to_event_order_book("BUY NO", Decimal("0.4200"))
    assert result.book_side == "ask"
    assert result.price_string == "0.5800"


def test_count_that_rounds_to_zero_is_rejected():
    with pytest.raises(ValueError, match="rounds to zero"):
        format_count(Decimal("0.001"))


@pytest.mark.parametrize(
    "base_url",
    [
        "https://external-api.kalshi.com/trade-api/v2",
        "https://api.elections.kalshi.com/trade-api/v2",
    ],
)
def test_both_documented_production_hosts_are_recognized(base_url):
    assert KalshiClient(base_url=base_url).is_production is True


def test_create_order_uses_current_v2_event_path_and_fixed_point_strings(monkeypatch):
    calls = []

    def fake_request(method, endpoint, **kwargs):
        calls.append((method, endpoint, kwargs))
        return {
            "order_id": "order-1",
            "client_order_id": "client-1",
            "fill_count": "0.00",
            "remaining_count": "1.00",
            "ts_ms": 123,
        }

    client = KalshiClient(subaccount=2)
    monkeypatch.setattr(client, "_request", fake_request)
    ack = client.create_order(
        ticker="KXHIGHNY-26JUL23-T90",
        client_order_id="client-1",
        side="bid",
        count=1,
        price=Decimal("0.42"),
    )

    method, path, kwargs = calls[0]
    assert (method, path) == ("POST", "/portfolio/events/orders")
    assert kwargs["authed"] is True
    assert kwargs["json"] == {
        "ticker": "KXHIGHNY-26JUL23-T90",
        "client_order_id": "client-1",
        "side": "bid",
        "count": "1.00",
        "price": "0.4200",
        "time_in_force": "good_till_canceled",
        "self_trade_prevention_type": "taker_at_cross",
        "cancel_order_on_pause": True,
        "subaccount": 2,
    }
    assert ack.order_id == "order-1"


def test_cancel_order_uses_current_v2_event_path(monkeypatch):
    calls = []

    def fake_request(method, endpoint, **kwargs):
        calls.append((method, endpoint, kwargs))
        return {
            "order_id": "order-1",
            "client_order_id": "client-1",
            "reduced_by": "1.00",
            "ts_ms": 123,
        }

    client = KalshiClient()
    monkeypatch.setattr(client, "_request", fake_request)
    ack = client.cancel_order("order-1")
    assert calls[0][:2] == ("DELETE", "/portfolio/events/orders/order-1")
    assert calls[0][2]["authed"] is True
    assert ack.reduced_by == Decimal("1.00")


def test_current_order_fill_and_exchange_read_paths(monkeypatch):
    calls = []
    order = {
        "order_id": "order-1",
        "client_order_id": "client-1",
        "ticker": "TICKER",
        "outcome_side": "yes",
        "book_side": "bid",
        "status": "resting",
        "yes_price_dollars": "0.4200",
        "fill_count_fp": "0.00",
        "remaining_count_fp": "1.00",
        "initial_count_fp": "1.00",
    }

    def fake_request(method, endpoint, **kwargs):
        calls.append((method, endpoint, kwargs))
        if endpoint == "/portfolio/orders/order-1":
            return {"order": order}
        if endpoint == "/portfolio/orders":
            return {"orders": [order], "cursor": ""}
        if endpoint == "/portfolio/fills":
            return {
                "fills": [
                    {
                        "fill_id": "fill-1",
                        "order_id": "order-1",
                        "ticker": "TICKER",
                        "outcome_side": "yes",
                        "book_side": "bid",
                        "count_fp": "1.00",
                        "yes_price_dollars": "0.4200",
                        "fee_cost": "0.0100",
                    }
                ],
                "cursor": "",
            }
        return {"exchange_active": True, "trading_active": True}

    client = KalshiClient()
    monkeypatch.setattr(client, "_request", fake_request)
    assert client.get_order("order-1").status == "resting"
    assert len(client.list_orders()) == 1
    assert client.list_fills()[0].fee == Decimal("0.0100")
    assert client.get_exchange_status().trading_active is True
    assert [item[1] for item in calls] == [
        "/portfolio/orders/order-1",
        "/portfolio/orders",
        "/portfolio/fills",
        "/exchange/status",
    ]


def test_subaccount_is_scoped_on_authenticated_portfolio_reads(monkeypatch):
    calls = []

    def fake_request(method, endpoint, **kwargs):
        calls.append((method, endpoint, kwargs))
        if endpoint == "/portfolio/positions":
            return {"market_positions": [], "cursor": ""}
        if endpoint == "/portfolio/settlements":
            return {"settlements": [], "cursor": ""}
        if endpoint == "/portfolio/balance":
            return {"balance_dollars": "5.0100", "updated_ts": 1}
        return {
            "order": {
                "order_id": "order-1",
                "remaining_count_fp": "1.00",
                "initial_count_fp": "1.00",
            }
        }

    client = KalshiClient(subaccount=3)
    monkeypatch.setattr(client, "_request", fake_request)
    client.get_positions()
    client.get_settlements()
    client.get_balance()
    client.get_order("order-1")
    assert all(call[2]["params"]["subaccount"] == 3 for call in calls)


def test_current_orderbook_and_batch_orderbook_paths(monkeypatch):
    calls = []

    def fake_request(method, endpoint, **kwargs):
        calls.append((method, endpoint, kwargs))
        book = {
            "orderbook_fp": {
                "yes_dollars": [["0.4000", "2.00"]],
                "no_dollars": [["0.3000", "1.50"]],
            }
        }
        if endpoint == "/markets/orderbooks":
            return {
                "orderbooks": [
                    {"market_ticker": "T1", **book},
                    {"market_ticker": "T2", **book},
                ]
            }
        return book

    client = KalshiClient()
    monkeypatch.setattr(client, "_request", fake_request)
    single = client.get_orderbook("T1", depth=10)
    batch = client.get_orderbooks(["T1", "T2"])

    assert single.best_yes_bid == Decimal("0.4000")
    assert single.best_yes_ask == Decimal("0.7000")
    assert single.yes_spread == Decimal("0.3000")
    assert [book.ticker for book in batch] == ["T1", "T2"]
    assert calls[0] == (
        "GET",
        "/markets/T1/orderbook",
        {"params": {"depth": 10}, "authed": True},
    )
    assert calls[1] == (
        "GET",
        "/markets/orderbooks",
        {"params": {"tickers": "T1,T2"}, "authed": True},
    )
