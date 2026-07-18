from __future__ import annotations

import pytest

from weather.calibration_params import get_calibration
from weather.probability import (
    bracket_probability,
    calibrated_bracket_probability,
    calibrated_probability_for_market,
    check_boundary_language,
    fit_normal,
    probability_for_market,
    temperature_in_bracket,
)


def test_fit_normal_returns_sample_mean_and_std():
    mean, std = fit_normal([78.0, 80.0, 82.0])
    assert mean == pytest.approx(80.0)
    assert std == pytest.approx(2.0)


def test_fit_normal_requires_at_least_two_members():
    with pytest.raises(ValueError):
        fit_normal([80.0])


def test_fit_normal_floors_degenerate_std():
    _, std = fit_normal([80.0, 80.0, 80.0])
    assert std >= 0.5


def test_bracket_covering_the_mean_is_most_likely():
    mean, std = 82.0, 3.0
    p_center = bracket_probability(mean, std, floor_strike=81.0, cap_strike=82.0)
    p_far = bracket_probability(mean, std, floor_strike=95.0, cap_strike=96.0)
    assert p_center > p_far


def test_less_than_and_greater_than_are_complementary_around_full_range():
    # A "less than 80" and "greater than 79" bracket together with "between 79-80"
    # should fully partition the real line and sum to (almost) exactly 1.0.
    mean, std = 80.0, 4.0
    p_below = bracket_probability(mean, std, floor_strike=None, cap_strike=79.0)
    p_between = bracket_probability(mean, std, floor_strike=79.0, cap_strike=80.0)
    p_above = bracket_probability(mean, std, floor_strike=80.0, cap_strike=None)
    assert p_below + p_between + p_above == pytest.approx(1.0, abs=1e-9)


def test_full_ladder_partition_sums_to_one():
    # The real KXHIGHNY-26JUL18 ladder: T79, B79.5, B81.5, B83.5, B85.5, T86. Each
    # "between" bracket is 2 degrees wide (floor steps by 2, not 1) — e.g. B79.5
    # covers {79,80} and B81.5 covers {81,82}, with no market for {80,81}. That's
    # not a gap: 80 belongs to B79.5 and 81 belongs to B81.5, so every integer is
    # still covered by exactly one bracket. A naive 1-degree-step ladder would
    # double-count under the continuity correction, which is what this test guards.
    mean, std = 82.0, 3.5
    brackets = [
        (None, 79.0),
        (79.0, 80.0),
        (81.0, 82.0),
        (83.0, 84.0),
        (85.0, 86.0),
        (86.0, None),
    ]
    total = sum(bracket_probability(mean, std, floor, cap) for floor, cap in brackets)
    assert total == pytest.approx(1.0, abs=1e-9)


def test_bracket_probability_requires_a_bound():
    with pytest.raises(ValueError):
        bracket_probability(80.0, 3.0, floor_strike=None, cap_strike=None)


def test_student_t_ladder_partition_still_sums_to_one():
    # Same invariant as the normal case must hold for any distribution family.
    loc, scale, df = 82.0, 3.5, 5.0
    brackets = [(None, 79.0), (79.0, 80.0), (81.0, 82.0), (83.0, 84.0), (85.0, 86.0), (86.0, None)]
    total = sum(bracket_probability(loc, scale, floor, cap, df=df) for floor, cap in brackets)
    assert total == pytest.approx(1.0, abs=1e-9)


def test_student_t_has_fatter_tails_than_normal_at_same_loc_scale():
    # A far-out-of-range bracket should look more plausible under a low-df
    # Student's t (fat tails) than under Normal with the same loc/scale — this
    # is the exact property the backtest found real forecast error needs.
    loc, scale = 82.0, 3.0
    tail_bracket = dict(floor_strike=95.0, cap_strike=96.0)
    p_normal = bracket_probability(loc, scale, **tail_bracket)
    p_t = bracket_probability(loc, scale, df=3.0, **tail_bracket)
    assert p_t > p_normal


def test_student_t_with_high_df_approaches_normal():
    loc, scale = 82.0, 3.0
    bracket = dict(floor_strike=81.0, cap_strike=82.0)
    p_normal = bracket_probability(loc, scale, **bracket)
    p_t_high_df = bracket_probability(loc, scale, df=500.0, **bracket)
    assert p_t_high_df == pytest.approx(p_normal, abs=1e-3)


def test_check_boundary_language_accepts_matching_text():
    check_boundary_language("is less than 79°, then the market resolves to Yes.", None, 79.0)
    check_boundary_language("is between 79-80°, then the market resolves to Yes.", 79.0, 80.0)
    check_boundary_language("is greater than 86°, then the market resolves to Yes.", 86.0, None)


def test_check_boundary_language_rejects_mismatch():
    with pytest.raises(ValueError):
        check_boundary_language("is greater than 86°, then the market resolves to Yes.", None, 79.0)


def test_probability_for_market_runs_the_cross_check():
    with pytest.raises(ValueError):
        probability_for_market(
            rules_primary="is greater than 86°, then the market resolves to Yes.",
            floor_strike=None,
            cap_strike=79.0,
            loc=80.0,
            scale=3.0,
        )


def test_probability_for_market_can_skip_the_cross_check():
    probability = probability_for_market(
        rules_primary="mismatched text on purpose",
        floor_strike=None,
        cap_strike=79.0,
        loc=80.0,
        scale=3.0,
        validate_rules_text=False,
    )
    assert 0.0 <= probability <= 1.0


def test_calibrated_bracket_probability_applies_the_fitted_bias():
    params = get_calibration("KXHIGHNY")
    raw_mean = 79.0
    month = 6  # a month outside NYC's fitted monthly dict edge cases, if any
    corrected = calibrated_bracket_probability(
        "KXHIGHNY", raw_mean, floor_strike=79.0, cap_strike=80.0, target_month=month
    )
    uncorrected = bracket_probability(raw_mean, params.std, floor_strike=79.0, cap_strike=80.0)
    biased = bracket_probability(
        raw_mean + params.bias_for_month(month), params.std, floor_strike=79.0, cap_strike=80.0
    )
    assert corrected == pytest.approx(biased)
    assert corrected != pytest.approx(uncorrected)


def test_calibrated_bracket_probability_rejects_unfitted_city():
    with pytest.raises(KeyError):
        calibrated_bracket_probability("KXHIGHNOWHERE", 80.0, floor_strike=79.0, cap_strike=80.0, target_month=1)


def test_calibrated_bracket_probability_varies_by_month_when_city_has_seasonal_data():
    # Confirmed 2026-07-18: NYC's forecast bias flips sign between winter and
    # summer, so it has a monthly correction — January and July must not
    # collapse to the same probability the way a flat-bias city would.
    params = get_calibration("KXHIGHNY")
    assert params.monthly_bias is not None, "test assumes NYC has a validated monthly correction"

    raw_mean = 79.0
    jan = calibrated_bracket_probability("KXHIGHNY", raw_mean, floor_strike=79.0, cap_strike=80.0, target_month=1)
    jul = calibrated_bracket_probability("KXHIGHNY", raw_mean, floor_strike=79.0, cap_strike=80.0, target_month=7)
    assert jan != pytest.approx(jul)


def test_calibrated_bracket_probability_flat_city_ignores_month():
    # A city where the flat bias validated better (e.g. Miami) should give the
    # same probability regardless of target_month.
    params = get_calibration("KXHIGHMIA")
    assert params.monthly_bias is None, "test assumes Miami's flat bias won validation"

    raw_mean = 88.0
    jan = calibrated_bracket_probability("KXHIGHMIA", raw_mean, floor_strike=88.0, cap_strike=89.0, target_month=1)
    jul = calibrated_bracket_probability("KXHIGHMIA", raw_mean, floor_strike=88.0, cap_strike=89.0, target_month=7)
    assert jan == pytest.approx(jul)


def test_temperature_in_bracket_cap_only_excludes_the_cap_itself():
    assert temperature_in_bracket(78.0, floor_strike=None, cap_strike=79.0) is True
    assert temperature_in_bracket(79.0, floor_strike=None, cap_strike=79.0) is False


def test_temperature_in_bracket_floor_only_excludes_the_floor_itself():
    assert temperature_in_bracket(87.0, floor_strike=86.0, cap_strike=None) is True
    assert temperature_in_bracket(86.0, floor_strike=86.0, cap_strike=None) is False


def test_temperature_in_bracket_between_includes_both_ends():
    assert temperature_in_bracket(79.0, floor_strike=79.0, cap_strike=80.0) is True
    assert temperature_in_bracket(80.0, floor_strike=79.0, cap_strike=80.0) is True
    assert temperature_in_bracket(78.0, floor_strike=79.0, cap_strike=80.0) is False
    assert temperature_in_bracket(81.0, floor_strike=79.0, cap_strike=80.0) is False


def test_temperature_in_bracket_requires_a_bound():
    with pytest.raises(ValueError):
        temperature_in_bracket(80.0, floor_strike=None, cap_strike=None)


def test_calibrated_probability_for_market_runs_the_cross_check():
    with pytest.raises(ValueError):
        calibrated_probability_for_market(
            "KXHIGHNY",
            rules_primary="is greater than 86°, then the market resolves to Yes.",
            floor_strike=None,
            cap_strike=79.0,
            ensemble_mean=80.0,
            target_month=1,
        )
