"""The weather-vs-other realized-P&L breakdown of real settled trades
(dashboard.app._settlement_summary) — the "where did the real money go" split.
"""

from __future__ import annotations

from kalshi_client.models import Settlement
from dashboard.app import _is_weather_ticker, _settlement_summary


def _settlement(ticker: str, *, result: str, held: str, cost: float, revenue: float, fee: float = 0.0) -> Settlement:
    """A Settlement with a chosen held side and outcome. `held` sets which leg
    is nonzero so Settlement.held_side/won resolve as intended."""
    yes_ct, no_ct = (10.0, 0.0) if held == "yes" else (0.0, 10.0)
    return Settlement.from_dict({
        "ticker": ticker,
        "market_result": result,
        "yes_count_fp": str(yes_ct),
        "no_count_fp": str(no_ct),
        "yes_total_cost_dollars": str(cost if held == "yes" else 0.0),
        "no_total_cost_dollars": str(cost if held == "no" else 0.0),
        "revenue": int(round(revenue * 100)),  # API stores cents
        "fee_cost": str(fee),
    })


def test_is_weather_ticker_splits_project_markets_from_the_rest():
    assert _is_weather_ticker("KXHIGHNY-26JUL16-B91.5") is True
    assert _is_weather_ticker("KXLOWTCHI-26JUL19-B68.5") is True
    assert _is_weather_ticker("KXWC2HBTTS-26JUL18FRAEN") is False  # World Cup
    assert _is_weather_ticker("KXNBAFINALS-26-LAL") is False


def test_settlement_summary_separates_weather_from_other_and_totals():
    settlements = [
        # weather: both lost (held yes, settled no)
        _settlement("KXHIGHNY-26JUL16-B91.5", result="no", held="yes", cost=14.58, revenue=0.0, fee=0.71),
        _settlement("KXLOWTCHI-26JUL19-B68.5", result="no", held="yes", cost=9.41, revenue=0.0, fee=0.24),
        # other: one win (held yes, settled yes, paid out), one loss
        _settlement("KXWC2HBTTS-26JUL18FRAEN", result="yes", held="yes", cost=9.64, revenue=20.08, fee=0.17),
        _settlement("KXNBA-LEBRON", result="no", held="yes", cost=9.99, revenue=0.0, fee=0.0),
    ]
    s = _settlement_summary(settlements)

    assert s["weather"]["n"] == 2
    assert s["weather"]["wins"] == 0
    assert s["weather"]["net"] == round(-(14.58 + 0.71) - (9.41 + 0.24), 2)

    assert s["other"]["n"] == 2
    assert s["other"]["wins"] == 1
    assert s["other"]["net"] == round((20.08 - 9.64 - 0.17) + (0.0 - 9.99), 2)

    assert s["total"]["n"] == 4
    assert s["total"]["wins"] == 1
    # Total net is the sum of the two buckets.
    assert s["total"]["net"] == round(s["weather"]["net"] + s["other"]["net"], 2)


def test_settlement_summary_handles_empty():
    s = _settlement_summary([])
    assert s["total"] == {"n": 0, "wins": 0, "cost": 0.0, "revenue": 0.0, "fees": 0.0, "net": 0.0}
