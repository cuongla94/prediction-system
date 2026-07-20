from __future__ import annotations

from kalshi_client import KalshiClient


def test_get_candlesticks_parses_response_and_builds_correct_path(monkeypatch):
    calls = []

    def fake_request(method, endpoint, *, params=None, authed=False):
        calls.append((method, endpoint, params))
        return {
            "candlesticks": [
                {"end_period_ts": 100, "yes_bid": {"close_dollars": "0.10"}, "yes_ask": {"close_dollars": "0.15"}},
                {"end_period_ts": 160, "yes_bid": {"close_dollars": "0.12"}, "yes_ask": {"close_dollars": "0.16"}},
            ]
        }

    client = KalshiClient()
    monkeypatch.setattr(client, "_request", fake_request)

    candles = client.get_candlesticks("KXHIGHNY", "KXHIGHNY-26JUL19-T87", start_ts=0, end_ts=200, period_interval=1)

    assert len(candles) == 2
    assert candles[0].end_period_ts == 100
    assert candles[0].yes_bid_close_dollars == 0.10
    assert candles[1].yes_ask_close_dollars == 0.16
    assert calls == [
        (
            "GET",
            "/series/KXHIGHNY/markets/KXHIGHNY-26JUL19-T87/candlesticks",
            {"start_ts": 0, "end_ts": 200, "period_interval": 1},
        )
    ]
