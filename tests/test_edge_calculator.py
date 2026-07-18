from __future__ import annotations

import pytest

from edge.calculator import bracket_sum_deviation, compute_edge
from kalshi_client.fees import taker_fee


def test_positive_edge_favors_yes():
    result = compute_edge(model_probability=0.40, market_yes_price=0.30)
    assert result.side == "YES"
    assert result.edge == pytest.approx(0.10)


def test_negative_edge_favors_no():
    result = compute_edge(model_probability=0.10, market_yes_price=0.25)
    assert result.side == "NO"
    assert result.edge == pytest.approx(-0.15)


def test_zero_edge_is_flat():
    result = compute_edge(model_probability=0.30, market_yes_price=0.30)
    assert result.side == "FLAT"
    assert not result.is_actionable


def test_threshold_is_fee_plus_safety_margin():
    result = compute_edge(model_probability=0.40, market_yes_price=0.30, safety_margin=0.02)
    assert result.threshold == pytest.approx(taker_fee(0.30) + 0.02)


def test_edge_below_threshold_is_not_actionable():
    # fee+margin at price=0.50 (fee peaks here) is taker_fee(0.5)=0.0175, +0.02 = 0.0375
    result = compute_edge(model_probability=0.52, market_yes_price=0.50, safety_margin=0.02)
    assert result.edge == pytest.approx(0.02)
    assert not result.is_actionable


def test_edge_above_threshold_is_actionable():
    result = compute_edge(model_probability=0.60, market_yes_price=0.50, safety_margin=0.02)
    assert result.is_actionable


def test_bracket_sum_deviation_positive_when_overpriced():
    assert bracket_sum_deviation([0.4, 0.4, 0.3]) == pytest.approx(0.1)


def test_bracket_sum_deviation_zero_when_exact():
    assert bracket_sum_deviation([0.5, 0.3, 0.2]) == pytest.approx(0.0)


def test_bracket_sum_deviation_negative_when_underpriced():
    assert bracket_sum_deviation([0.2, 0.2, 0.2]) == pytest.approx(-0.4)
