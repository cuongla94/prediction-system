from __future__ import annotations

from kalshi_client.models import Market


def _market(floor_strike: float | None, cap_strike: float | None) -> Market:
    return Market(
        ticker="TEST",
        event_ticker="TEST-EVENT",
        status="active",
        rules_primary="",
        rules_secondary="",
        floor_strike=floor_strike,
        cap_strike=cap_strike,
        yes_bid_dollars=None,
        yes_ask_dollars=None,
        no_bid_dollars=None,
        no_ask_dollars=None,
        last_price_dollars=None,
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
