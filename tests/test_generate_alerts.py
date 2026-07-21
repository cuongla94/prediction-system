from __future__ import annotations

from datetime import date

import pytest

from kalshi_client import Event, Market
from scripts.generate_alerts import price_bracket


def _event(event_ticker: str = "KXHIGHNY-26JUL20") -> Event:
    return Event(
        event_ticker=event_ticker,
        series_ticker="KXHIGHNY",
        title="",
        sub_title="",
        strike_date=None,
        strike_period=None,
        mutually_exclusive=True,
        raw={},
    )


def _market(
    ticker: str = "KXHIGHNY-26JUL20-B79.5",
    floor_strike: float | None = 79.0,
    cap_strike: float | None = 80.0,
    yes_bid: float | None = 0.30,
    yes_ask: float | None = 0.34,
    rules_primary: str = "is between 79-80°, then the market resolves to Yes.",
) -> Market:
    return Market(
        ticker=ticker,
        event_ticker="KXHIGHNY-26JUL20",
        status="open",
        title="",
        yes_sub_title="",
        no_sub_title="",
        rules_primary=rules_primary,
        rules_secondary="",
        floor_strike=floor_strike,
        cap_strike=cap_strike,
        yes_bid_dollars=yes_bid,
        yes_ask_dollars=yes_ask,
        no_bid_dollars=None,
        no_ask_dollars=None,
        last_price_dollars=None,
        close_time="2026-07-21T04:00:00Z",
        raw={},
    )


def _price(**overrides):
    kwargs = dict(
        series_ticker="KXHIGHNY",
        city="NYC",
        event=_event(),
        market=_market(),
        ensemble_mean=80.0,
        ensemble_std=2.0,
        event_date=date(2026, 7, 20),
        lead_days=0,
        metric="max",
        observed_so_far=None,
        kalshi_url="https://kalshi.com/markets/kxhighny",
    )
    kwargs.update(overrides)
    return price_bracket(**kwargs)


def test_returns_none_when_bid_or_ask_missing():
    assert _price(market=_market(yes_bid=None)) is None
    assert _price(market=_market(yes_ask=None)) is None


def test_returns_none_on_rules_text_mismatch():
    assert _price(market=_market(rules_primary="is greater than 86°, then the market resolves to Yes.")) is None


def test_lead_days_zero_uses_the_observation():
    with_obs = _price(lead_days=0, observed_so_far=82.0)
    without_obs = _price(lead_days=0, observed_so_far=None)
    assert with_obs["observed_so_far"] == 82.0
    assert with_obs["model_probability"] != without_obs["model_probability"]


def test_lead_days_nonzero_ignores_the_observation():
    # A market settling tomorrow (or later) has nothing observed for its own
    # day yet -- passing a same-day observation through would be a real bug,
    # not a feature, so it must be dropped regardless of what the caller sent.
    row = _price(lead_days=1, observed_so_far=82.0)
    assert row["observed_so_far"] is None
    unconditioned = _price(lead_days=1, observed_so_far=None)
    assert row["model_probability"] == pytest.approx(unconditioned["model_probability"])


def test_row_carries_the_inputs_through_unchanged():
    row = _price(ensemble_mean=85.0, ensemble_std=3.0, lead_days=0)
    assert row["series_ticker"] == "KXHIGHNY"
    assert row["event_ticker"] == "KXHIGHNY-26JUL20"
    assert row["market_ticker"] == "KXHIGHNY-26JUL20-B79.5"
    assert row["ensemble_mean"] == 85.0
    assert row["ensemble_std"] == 3.0
    assert row["floor_strike"] == 79.0
    assert row["cap_strike"] == 80.0
    assert row["status"] == "open"
    assert row["metric"] == "max"
    assert row["model_version"] == "normal-v4-observation-conditioned"
    assert row["calibration_validated"] is False


def test_market_price_is_the_bid_ask_midpoint():
    row = _price(market=_market(yes_bid=0.20, yes_ask=0.30))
    assert row["market_yes_price"] == pytest.approx(0.25)


def _cap_only_market(**overrides) -> Market:
    kwargs = dict(
        floor_strike=None,
        cap_strike=79.0,
        rules_primary="is less than 79°, then the market resolves to Yes.",
    )
    kwargs.update(overrides)
    return _market(**kwargs)


def test_edge_and_actionability_are_computed_not_hardcoded():
    # calibrated_observation_conditioned_probability uses the SHIPPED std from
    # weather/calibration_params.py, not the ensemble_std passed through here
    # (that value is stored as metadata only) -- so push the mean far below a
    # cap-only bracket rather than relying on a tight std to get certainty.
    disagreeing = _price(ensemble_mean=50.0, market=_cap_only_market(yes_bid=0.01, yes_ask=0.02))
    assert disagreeing["model_probability"] > 0.99
    assert disagreeing["is_actionable"] is True
    assert disagreeing["edge"] != 0

    # Same model probability, but the market already agrees with it -> the
    # edge collapses toward zero even though nothing else about the row changed.
    agreeing = _price(ensemble_mean=50.0, market=_cap_only_market(
        yes_bid=round(disagreeing["model_probability"] - 0.001, 4),
        yes_ask=round(disagreeing["model_probability"] + 0.001, 4),
    ))
    assert abs(agreeing["edge"]) < abs(disagreeing["edge"])
