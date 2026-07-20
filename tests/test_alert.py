from __future__ import annotations

import pytest

from dashboard.alert import Alert


def _alert(model_probability: float, market_yes_price: float, edge: float) -> Alert:
    return Alert(
        id=1,
        created_at="2026-07-18T00:00:00Z",
        series_ticker="KXHIGHNY",
        event_ticker="KXHIGHNY-26JUL18",
        market_ticker="KXHIGHNY-26JUL18-T79",
        city="NYC",
        bracket_label="< 79°",
        floor_strike=None,
        cap_strike=79.0,
        model_probability=model_probability,
        ensemble_mean=76.0,
        ensemble_std=2.0,
        model_version="normal-v3-seasonal-bias",
        calibration_validated=False,
        market_yes_price=market_yes_price,
        edge=edge,
        fee_adjusted_threshold=0.03,
        rules_primary="",
        rules_secondary=None,
        kalshi_url="https://kalshi.com/markets/kxhighny/x/kxhighny-26jul18",
        is_actionable=True,
        status="open",
        settled_at=None,
        actual_high_temp=None,
        actual_outcome=None,
        close_time=None,
    )


def test_win_probability_for_yes_side_is_model_probability():
    alert = _alert(model_probability=0.40, market_yes_price=0.25, edge=0.15)
    assert alert.side == "YES"
    assert alert.win_probability == pytest.approx(0.40)


def test_win_probability_for_no_side_is_complement():
    alert = _alert(model_probability=0.10, market_yes_price=0.30, edge=-0.20)
    assert alert.side == "NO"
    assert alert.win_probability == pytest.approx(0.90)


def test_win_probability_for_flat_side_is_model_probability():
    alert = _alert(model_probability=0.30, market_yes_price=0.30, edge=0.0)
    assert alert.side == "FLAT"
    assert alert.win_probability == pytest.approx(0.30)


def test_display_edge_for_yes_side_matches_raw_edge():
    # YES pick: the trade's own edge is just the raw (YES-referenced) edge.
    alert = _alert(model_probability=0.40, market_yes_price=0.25, edge=0.15)
    assert alert.side == "YES"
    assert alert.display_edge == pytest.approx(0.15)


def test_display_edge_for_no_side_flips_sign_to_the_traded_side():
    # NO pick: raw edge is -0.20 (the YES side's edge), but the NO trade's own
    # edge is +0.20 — the number that reads coherently next to a 90% win chance.
    alert = _alert(model_probability=0.10, market_yes_price=0.30, edge=-0.20)
    assert alert.side == "NO"
    assert alert.win_probability == pytest.approx(0.90)
    assert alert.display_edge == pytest.approx(0.20)


def test_display_edge_for_flat_side_is_zero():
    alert = _alert(model_probability=0.30, market_yes_price=0.30, edge=0.0)
    assert alert.side == "FLAT"
    assert alert.display_edge == pytest.approx(0.0)
