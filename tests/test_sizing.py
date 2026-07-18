from __future__ import annotations

import pytest

from sizing.kelly import (
    BracketInput,
    recommended_dollars,
    single_bracket_kelly,
    size_event,
)


def test_single_bracket_kelly_yes():
    f = single_bracket_kelly(model_probability=0.40, market_yes_price=0.30, side="YES")
    assert f == pytest.approx(0.10 / 0.70)


def test_single_bracket_kelly_no():
    f = single_bracket_kelly(model_probability=0.10, market_yes_price=0.25, side="NO")
    assert f == pytest.approx(0.6)


def test_single_bracket_kelly_flat_is_zero():
    assert single_bracket_kelly(model_probability=0.30, market_yes_price=0.30, side="FLAT") == 0.0


def test_single_bracket_kelly_yes_at_dollar_price_is_zero():
    # Buying Yes at $1 has no upside left to size for — must not divide by zero.
    assert single_bracket_kelly(model_probability=0.99, market_yes_price=1.0, side="YES") == 0.0


def test_single_bracket_kelly_no_at_zero_price_is_zero():
    assert single_bracket_kelly(model_probability=0.01, market_yes_price=0.0, side="NO") == 0.0


def test_size_event_single_bracket_under_cap_gets_fractional_kelly():
    brackets = [BracketInput("T1", 0.40, 0.30, "YES", True)]
    recs = size_event(brackets, kelly_fraction=0.25, max_event_exposure=0.15)
    full = 0.10 / 0.70
    assert recs["T1"].full_kelly_fraction == pytest.approx(full)
    assert recs["T1"].recommended_fraction == pytest.approx(full * 0.25)


def test_size_event_non_actionable_bracket_gets_zero():
    brackets = [BracketInput("T1", 0.90, 0.10, "YES", False)]
    recs = size_event(brackets, kelly_fraction=0.25, max_event_exposure=0.15)
    assert recs["T1"].full_kelly_fraction == 0.0
    assert recs["T1"].recommended_fraction == 0.0


def test_size_event_scales_down_when_sum_exceeds_event_cap():
    # Two deliberately aggressive brackets: full Kelly 0.8 each, *0.25 = 0.2 each,
    # summing to 0.4 — well past a 0.15 cap.
    brackets = [
        BracketInput("T1", 0.90, 0.50, "YES", True),
        BracketInput("T2", 0.90, 0.50, "YES", True),
    ]
    recs = size_event(brackets, kelly_fraction=0.25, max_event_exposure=0.15)
    total = sum(r.recommended_fraction for r in recs.values())
    assert total == pytest.approx(0.15)
    # Both brackets were identical, so the cap should split evenly.
    assert recs["T1"].recommended_fraction == pytest.approx(recs["T2"].recommended_fraction)


def test_size_event_leaves_total_alone_when_under_cap():
    # Small edges on purpose: scaled Kelly stakes here sum well under the 0.15
    # cap, so this exercises the no-scaling branch distinctly from the
    # over-cap test above.
    brackets = [
        BracketInput("T1", 0.32, 0.30, "YES", True),
        BracketInput("T2", 0.24, 0.25, "NO", True),
    ]
    recs = size_event(brackets, kelly_fraction=0.25, max_event_exposure=0.15)
    expected_t1 = (0.02 / 0.70) * 0.25
    expected_t2 = (0.01 / 0.25) * 0.25
    assert sum(r.recommended_fraction for r in recs.values()) < 0.15
    assert recs["T1"].recommended_fraction == pytest.approx(expected_t1)
    assert recs["T2"].recommended_fraction == pytest.approx(expected_t2)


def test_size_event_all_flat_has_no_recommendation_and_no_division_error():
    brackets = [BracketInput("T1", 0.30, 0.30, "FLAT", False)]
    recs = size_event(brackets)
    assert recs["T1"].recommended_fraction == 0.0


def test_recommended_dollars_under_position_limit():
    assert recommended_dollars(0.05, bankroll_usd=10_000) == pytest.approx(500)


def test_recommended_dollars_clipped_to_position_limit():
    assert recommended_dollars(0.05, bankroll_usd=1_000_000) == pytest.approx(25_000)
