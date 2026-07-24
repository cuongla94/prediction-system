from __future__ import annotations

import pytest

from backtest.harness import (
    BacktestRow,
    _repair_missing_strikes,
    classify_forecast_vs_bracket,
    collect_dated_residuals_with_spread,
    collect_residuals,
    fit_empirical_normal,
    fit_empirical_normal_for_model,
    fit_monthly_bias,
    fit_remaining_scale_fraction,
    fit_spread_scale,
    fit_student_t,
    rolling_splits,
    split_by_date,
)
from kalshi_client.models import Market


def _market(
    ticker: str,
    floor_strike: float | None,
    cap_strike: float | None,
    result: str = "no",
) -> Market:
    return Market(
        ticker=ticker,
        event_ticker=ticker.rsplit("-", 1)[0],
        status="settled",
        title="",
        yes_sub_title="",
        no_sub_title="",
        rules_primary="",
        rules_secondary="",
        floor_strike=floor_strike,
        cap_strike=cap_strike,
        yes_bid_dollars=None,
        yes_ask_dollars=None,
        no_bid_dollars=None,
        no_ask_dollars=None,
        last_price_dollars=None,
        close_time=None,
        raw={"result": result},
    )


def _row(
    date: str,
    forecast_mean: float = 80.0,
    approx_actual_temp: float | None = 80.0,
    forecast_spread: float = 1.0,
    per_model_forecast: dict[str, float] | None = None,
) -> BacktestRow:
    return BacktestRow(
        city="NYC",
        series_ticker="KXHIGHNY",
        event_ticker=f"KXHIGHNY-{date}",
        market_ticker=f"KXHIGHNY-{date}-B79.5",
        target_date=date,
        forecast_mean=forecast_mean,
        forecast_spread=forecast_spread,
        n_models=3,
        actual_outcome=True,
        last_price=0.5,
        floor_strike=79.0,
        cap_strike=80.0,
        approx_actual_temp=approx_actual_temp,
        per_model_forecast=per_model_forecast,
    )


def test_split_by_date_keeps_same_day_rows_together():
    rows = [_row("2026-01-01"), _row("2026-01-01"), _row("2026-01-02"), _row("2026-01-03")]

    fit_rows, eval_rows = split_by_date(rows, fit_fraction=0.5)

    fit_dates = {r.target_date for r in fit_rows}
    eval_dates = {r.target_date for r in eval_rows}
    assert fit_dates.isdisjoint(eval_dates)
    assert fit_dates | eval_dates == {"2026-01-01", "2026-01-02", "2026-01-03"}


def test_split_by_date_is_chronological():
    rows = [_row("2026-03-01"), _row("2026-01-01"), _row("2026-02-01")]

    fit_rows, _ = split_by_date(rows, fit_fraction=1 / 3)

    assert {r.target_date for r in fit_rows} == {"2026-01-01"}


def _dates(n: int) -> list[str]:
    return [f"2026-01-{day:02d}" for day in range(1, n + 1)]


def test_rolling_splits_fit_window_grows_each_fold():
    rows = [_row(d) for d in _dates(20)]

    folds = rolling_splits(rows, n_folds=4, min_fit_fraction=0.4)

    assert len(folds) == 4
    prev_fit_dates: set[str] = set()
    for fit_rows, eval_rows in folds:
        fit_dates = {r.target_date for r in fit_rows}
        eval_dates = {r.target_date for r in eval_rows}
        assert prev_fit_dates <= fit_dates, "fit window must never shrink between folds"
        assert fit_dates.isdisjoint(eval_dates)
        assert max(fit_dates) < min(eval_dates), "must never eval on a date earlier than what it fit on"
        prev_fit_dates = fit_dates | eval_dates


def test_rolling_splits_eval_slices_cover_all_remaining_dates_exactly_once():
    dates = _dates(20)
    rows = [_row(d) for d in dates]

    folds = rolling_splits(rows, n_folds=4, min_fit_fraction=0.4)

    all_eval_dates = [r.target_date for _, eval_rows in folds for r in eval_rows]
    initial_fit_count = round(len(dates) * 0.4)
    assert sorted(all_eval_dates) == dates[initial_fit_count:]


def test_rolling_splits_raises_when_too_few_remaining_dates_for_fold_count():
    rows = [_row(d) for d in _dates(5)]

    with pytest.raises(ValueError, match="Need at least"):
        rolling_splits(rows, n_folds=4, min_fit_fraction=0.8)


def test_collect_residuals_excludes_tail_bracket_days():
    rows = [
        _row("2026-01-01", forecast_mean=80.0, approx_actual_temp=82.0),
        _row("2026-01-02", forecast_mean=80.0, approx_actual_temp=None),  # tail-bracket win
    ]
    assert collect_residuals(rows) == [2.0]


def test_collect_residuals_dedupes_multiple_brackets_per_day():
    rows = [
        _row("2026-01-01", forecast_mean=80.0, approx_actual_temp=82.0),
        _row("2026-01-01", forecast_mean=80.0, approx_actual_temp=82.0),
        _row("2026-01-02", forecast_mean=80.0, approx_actual_temp=78.0),
    ]
    assert collect_residuals(rows) == [2.0, -2.0]


def test_fit_empirical_normal_computes_bias_and_sample_std():
    # residuals: actual - forecast = 82-80=2, 76-80=-4, 80-80=0
    rows = [
        _row("2026-01-01", forecast_mean=80.0, approx_actual_temp=82.0),
        _row("2026-01-02", forecast_mean=80.0, approx_actual_temp=76.0),
        _row("2026-01-03", forecast_mean=80.0, approx_actual_temp=80.0),
    ]

    mean_bias, std = fit_empirical_normal(rows)

    residuals = [2.0, -4.0, 0.0]
    expected_bias = sum(residuals) / 3
    expected_std = (sum((r - expected_bias) ** 2 for r in residuals) / 2) ** 0.5
    assert mean_bias == pytest.approx(expected_bias)
    assert std == pytest.approx(expected_std)


def test_fit_empirical_normal_detects_systematic_bias():
    # forecast consistently runs 3 degrees cold
    rows = [
        _row("2026-01-01", forecast_mean=80.0, approx_actual_temp=83.0),
        _row("2026-01-02", forecast_mean=80.0, approx_actual_temp=83.0),
        _row("2026-01-03", forecast_mean=80.0, approx_actual_temp=83.0),
    ]

    mean_bias, std = fit_empirical_normal(rows)

    assert mean_bias == pytest.approx(3.0)
    assert std == pytest.approx(0.0)


def test_fit_empirical_normal_requires_at_least_two_days():
    with pytest.raises(ValueError):
        fit_empirical_normal([_row("2026-01-01")])


# --- fit_empirical_normal_for_model --------------------------------------


def test_fit_empirical_normal_for_model_uses_that_models_own_forecast():
    # GFS runs 2 cold, ECMWF runs 5 cold on the same days -- fitting against
    # each model separately should recover each model's own bias, not a
    # blended one.
    rows = [
        _row("2026-01-01", approx_actual_temp=82.0, per_model_forecast={"gfs": 80.0, "ecmwf": 77.0}),
        _row("2026-01-02", approx_actual_temp=82.0, per_model_forecast={"gfs": 80.0, "ecmwf": 77.0}),
    ]
    gfs_bias, gfs_std = fit_empirical_normal_for_model(rows, "gfs")
    ecmwf_bias, ecmwf_std = fit_empirical_normal_for_model(rows, "ecmwf")
    assert gfs_bias == pytest.approx(2.0)
    assert ecmwf_bias == pytest.approx(5.0)
    assert gfs_std == pytest.approx(0.0)
    assert ecmwf_std == pytest.approx(0.0)


def test_fit_empirical_normal_for_model_skips_rows_missing_that_model():
    rows = [
        _row("2026-01-01", approx_actual_temp=82.0, per_model_forecast={"gfs": 80.0}),
        _row("2026-01-02", approx_actual_temp=82.0, per_model_forecast={"gfs": 80.0, "ecmwf": 77.0}),
        _row("2026-01-03", approx_actual_temp=82.0, per_model_forecast={"gfs": 80.0, "ecmwf": 77.0}),
    ]
    # "ecmwf" only has 2 usable days (row 1 lacks it) -- still enough to fit.
    bias, _ = fit_empirical_normal_for_model(rows, "ecmwf")
    assert bias == pytest.approx(5.0)


def test_fit_empirical_normal_for_model_requires_at_least_two_usable_days():
    rows = [_row("2026-01-01", per_model_forecast={"gfs": 80.0})]
    with pytest.raises(ValueError):
        fit_empirical_normal_for_model(rows, "gfs")

    # Also raises when the model simply isn't present in enough rows, even
    # if other models are.
    rows2 = [
        _row("2026-01-01", per_model_forecast={"gfs": 80.0}),
        _row("2026-01-02", per_model_forecast={"gfs": 80.0}),
    ]
    with pytest.raises(ValueError):
        fit_empirical_normal_for_model(rows2, "ecmwf")


def test_fit_empirical_normal_for_model_ignores_rows_with_no_per_model_data_at_all():
    # A row collected before per_model_forecast existed (None) must not crash.
    rows = [
        _row("2026-01-01", per_model_forecast=None),
        _row("2026-01-02", approx_actual_temp=82.0, per_model_forecast={"gfs": 80.0}),
        _row("2026-01-03", approx_actual_temp=82.0, per_model_forecast={"gfs": 80.0}),
    ]
    bias, _ = fit_empirical_normal_for_model(rows, "gfs")
    assert bias == pytest.approx(2.0)


def test_fit_student_t_returns_df_loc_scale():
    rows = [
        _row("2026-01-01", forecast_mean=80.0, approx_actual_temp=82.0),
        _row("2026-01-02", forecast_mean=80.0, approx_actual_temp=76.0),
        _row("2026-01-03", forecast_mean=80.0, approx_actual_temp=80.0),
        _row("2026-01-04", forecast_mean=80.0, approx_actual_temp=81.0),
    ]
    df, loc, scale = fit_student_t(rows)
    assert df > 0
    assert scale > 0
    # loc should land near the residuals' central tendency (2, -4, 0, 1)
    assert -2.0 < loc < 2.0


def test_fit_student_t_requires_at_least_three_days():
    rows = [
        _row("2026-01-01", forecast_mean=80.0, approx_actual_temp=82.0),
        _row("2026-01-02", forecast_mean=80.0, approx_actual_temp=76.0),
    ]
    with pytest.raises(ValueError):
        fit_student_t(rows)


def test_fit_monthly_bias_separates_months():
    rows = [
        _row("2026-01-01", forecast_mean=80.0, approx_actual_temp=83.0),  # +3
        _row("2026-01-02", forecast_mean=80.0, approx_actual_temp=83.0),  # +3
        _row("2026-07-01", forecast_mean=80.0, approx_actual_temp=78.0),  # -2
        _row("2026-07-02", forecast_mean=80.0, approx_actual_temp=78.0),  # -2
    ]
    monthly = fit_monthly_bias(rows, min_samples=2)
    assert monthly[1] == pytest.approx(3.0)
    assert monthly[7] == pytest.approx(-2.0)
    assert set(monthly) == {1, 7}


def test_fit_monthly_bias_omits_thin_months():
    rows = [
        _row("2026-01-01", forecast_mean=80.0, approx_actual_temp=83.0),
        _row("2026-01-02", forecast_mean=80.0, approx_actual_temp=83.0),
        _row("2026-01-03", forecast_mean=80.0, approx_actual_temp=83.0),
        _row("2026-02-01", forecast_mean=80.0, approx_actual_temp=78.0),  # only 1 sample
    ]
    monthly = fit_monthly_bias(rows, min_samples=2)
    assert set(monthly) == {1}


def test_fit_monthly_bias_dedupes_multiple_brackets_per_day():
    rows = [
        _row("2026-03-01", forecast_mean=80.0, approx_actual_temp=82.0),
        _row("2026-03-01", forecast_mean=80.0, approx_actual_temp=82.0),  # same day, 2nd bracket
        _row("2026-03-02", forecast_mean=80.0, approx_actual_temp=82.0),
    ]
    monthly = fit_monthly_bias(rows, min_samples=2)
    assert monthly[3] == pytest.approx(2.0)


def test_collect_dated_residuals_with_spread_carries_spread_and_dedupes():
    rows = [
        _row("2026-01-01", forecast_mean=80.0, approx_actual_temp=82.0, forecast_spread=1.5),
        _row("2026-01-01", forecast_mean=80.0, approx_actual_temp=82.0, forecast_spread=1.5),  # same day
        _row("2026-01-02", forecast_mean=80.0, approx_actual_temp=78.0, forecast_spread=4.0),
    ]
    result = collect_dated_residuals_with_spread(rows)
    assert result == [("2026-01-01", 2.0, 1.5), ("2026-01-02", -2.0, 4.0)]


def test_fit_spread_scale_finds_a_positive_coef_when_disagreement_predicts_error():
    # High cross-model spread days have large errors, low-spread days small
    # ones — so the fitted spread_coef should come out positive (disagreement
    # is a real predictor of that day's error). Residuals average to 0 so the
    # bias-centering is a no-op and the relationship is clean.
    rows = [
        _row("2026-01-01", approx_actual_temp=80.5, forecast_spread=1.0),  # +0.5, low spread
        _row("2026-01-02", approx_actual_temp=79.5, forecast_spread=1.0),  # -0.5, low spread
        _row("2026-01-03", approx_actual_temp=83.0, forecast_spread=3.0),  # +3.0, high spread
        _row("2026-01-04", approx_actual_temp=77.0, forecast_spread=3.0),  # -3.0, high spread
    ]
    baseline_var, spread_coef = fit_spread_scale(rows)
    assert spread_coef > 0
    assert baseline_var >= 0.25  # floored, never degenerate


def test_fit_spread_scale_clamps_coef_to_zero_when_disagreement_does_not_help():
    # The reverse relationship (high spread, small error): the honest answer is
    # "spread doesn't predict error," so spread_coef must clamp to 0 rather than
    # perversely *narrowing* the distribution on high-disagreement days.
    rows = [
        _row("2026-01-01", approx_actual_temp=83.0, forecast_spread=1.0),  # +3.0, low spread
        _row("2026-01-02", approx_actual_temp=77.0, forecast_spread=1.0),  # -3.0, low spread
        _row("2026-01-03", approx_actual_temp=80.5, forecast_spread=3.0),  # +0.5, high spread
        _row("2026-01-04", approx_actual_temp=79.5, forecast_spread=3.0),  # -0.5, high spread
    ]
    baseline_var, spread_coef = fit_spread_scale(rows)
    assert spread_coef == pytest.approx(0.0)
    # Falls back to the pooled variance of the (zero-mean) residuals.
    assert baseline_var == pytest.approx((9.0 + 9.0 + 0.25 + 0.25) / 4)


def test_fit_spread_scale_requires_at_least_three_days():
    with pytest.raises(ValueError):
        fit_spread_scale([_row("2026-01-01"), _row("2026-01-02")])


# --- fit_remaining_scale_fraction ---------------------------------------


def test_fit_remaining_scale_fraction_is_one_when_nothing_is_known_yet():
    # observed_so_far == loc on every day (no information beyond the
    # day-ahead forecast) means the remaining residual IS the day-ahead
    # residual, unchanged — the fraction should come back at (approximately)
    # the full day-ahead baseline, i.e. no shrinkage earned.
    pairs = [
        (82.0, 80.0, 80.0),  # actual=82, max(loc, observed)=80 -> residual +2
        (78.0, 80.0, 80.0),  # residual -2
        (81.0, 80.0, 80.0),  # residual +1
        (79.0, 80.0, 80.0),  # residual -1
    ]
    # sample sd of [2, -2, 1, -1] (mean 0) = sqrt((4+4+1+1)/3) = sqrt(10/3)
    expected = (10 / 3) ** 0.5 / 2.0
    assert fit_remaining_scale_fraction(pairs, baseline_scale=2.0) == pytest.approx(expected)


def test_fit_remaining_scale_fraction_shrinks_when_observation_already_explains_the_day():
    # observed_so_far tracks the actual final almost exactly, and (the
    # realistic afternoon case for a "max" metric) has already climbed above
    # the day-ahead loc -- so max(loc, observed) tracks the true actual
    # closely and the remaining residual collapses toward 0.
    pairs = [
        (85.0, 80.0, 84.8),
        (89.0, 80.0, 88.9),
        (90.0, 80.0, 89.7),
        (86.0, 80.0, 85.8),
    ]
    fraction = fit_remaining_scale_fraction(pairs, baseline_scale=5.0)
    assert fraction < 0.2


def test_fit_remaining_scale_fraction_is_clamped_to_one():
    # A residual spread that comes out larger than the day-ahead baseline
    # itself (small-sample noise, or a genuinely bad baseline_scale) must not
    # produce a fraction above 1.0 -- observation_conditioned_bracket_
    # probability would reject it, and ">1" isn't a meaningful "more than the
    # whole day is still uncertain" claim anyway.
    pairs = [(90.0, 80.0, 80.0), (70.0, 80.0, 80.0)]
    assert fit_remaining_scale_fraction(pairs, baseline_scale=1.0) == 1.0


def test_fit_remaining_scale_fraction_is_floored_above_zero():
    # Every day nailed exactly -- a zero residual spread must still clamp to
    # a small positive floor, not 0.0 itself, which the callee rejects.
    pairs = [(80.0, 80.0, 80.0), (80.0, 80.0, 80.0), (80.0, 80.0, 80.0)]
    assert fit_remaining_scale_fraction(pairs, baseline_scale=2.0) == pytest.approx(0.01)


def test_fit_remaining_scale_fraction_requires_at_least_two_days():
    with pytest.raises(ValueError):
        fit_remaining_scale_fraction([(80.0, 80.0, 80.0)], baseline_scale=2.0)


def test_fit_remaining_scale_fraction_requires_a_positive_baseline():
    with pytest.raises(ValueError):
        fit_remaining_scale_fraction([(80.0, 80.0, 80.0), (81.0, 80.0, 80.0)], baseline_scale=0.0)


# --- classify_forecast_vs_bracket ----------------------------------------


def test_classify_between_bracket_hot():
    # Winning bracket is 79-80 (true range 78.5-80.5); loc=83 predicted well
    # above it -- the forecast ran hot relative to what actually happened.
    assert classify_forecast_vs_bracket(83.0, 79.0, 80.0) == "hot"


def test_classify_between_bracket_cold():
    assert classify_forecast_vs_bracket(75.0, 79.0, 80.0) == "cold"


def test_classify_between_bracket_inside():
    assert classify_forecast_vs_bracket(79.7, 79.0, 80.0) == "inside"


def test_classify_lowest_tail_bracket_only_ever_hot_or_inside():
    # "< 79" (the ladder's lowest bracket) won -- there's no lower bound to
    # undershoot, so a low loc is just "inside", never "cold".
    assert classify_forecast_vs_bracket(90.0, None, 79.0) == "hot"
    assert classify_forecast_vs_bracket(60.0, None, 79.0) == "inside"


def test_classify_highest_tail_bracket_only_ever_cold_or_inside():
    # "> 86" (the ladder's highest bracket) won -- there's no upper bound to
    # overshoot, so a high loc is just "inside", never "hot".
    assert classify_forecast_vs_bracket(70.0, 86.0, None) == "cold"
    assert classify_forecast_vs_bracket(95.0, 86.0, None) == "inside"


def test_classify_uses_the_half_degree_continuity_boundary():
    # loc landing exactly on cap_strike (80.0) is still "inside" -- the true
    # boundary is cap+0.5 (80.5), matching bracket_probability's own
    # convention, not the raw strike value.
    assert classify_forecast_vs_bracket(80.0, 79.0, 80.0) == "inside"
    assert classify_forecast_vs_bracket(80.6, 79.0, 80.0) == "hot"


def test_classify_rejects_a_market_with_neither_strike():
    with pytest.raises(ValueError):
        classify_forecast_vs_bracket(80.0, None, None)


# _repair_missing_strikes — modeled directly on real broken ladders found live
# 2026-07-18 via scripts/validate_against_noaa.py (KXHIGHNY-25JAN31 and
# KXHIGHNY-25FEB09), not synthetic guesses at the shape of the bug.


def test_repair_leaves_valid_markets_untouched():
    markets = [_market("KXHIGHNY-25JAN31-B43.5", 43.0, 44.0)]
    repaired = _repair_missing_strikes(markets)
    assert repaired[0].floor_strike == 43.0
    assert repaired[0].cap_strike == 44.0


def test_repair_recovers_between_bracket_from_ticker():
    markets = [
        _market("KXHIGHNY-25FEB09-T42", 42.0, None),
        _market("KXHIGHNY-25FEB09-B41.5", 41.0, 42.0),
        _market("KXHIGHNY-25FEB09-B39.5", 39.0, 40.0),
        _market("KXHIGHNY-25FEB09-B37.5", 37.0, 38.0),
        _market("KXHIGHNY-25FEB09-B35.5", None, None, result="yes"),  # the broken winner
        _market("KXHIGHNY-25FEB09-T35", None, 35.0),
    ]
    repaired = _repair_missing_strikes(markets)
    fixed = next(m for m in repaired if m.ticker == "KXHIGHNY-25FEB09-B35.5")
    assert fixed.floor_strike == pytest.approx(35.0)
    assert fixed.cap_strike == pytest.approx(36.0)


def test_repair_recovers_high_tail_bracket_using_sibling_bounds():
    # Real case: KXHIGHNY-25JAN31 — the broken winner is the HIGH tail (its
    # value, 44, sits at the top of the ladder's known range) not the low one.
    markets = [
        _market("KXHIGHNY-25JAN31-T44", None, None, result="yes"),  # the broken winner
        _market("KXHIGHNY-25JAN31-T37", None, 37.0),
        _market("KXHIGHNY-25JAN31-B43.5", 43.0, 44.0),
        _market("KXHIGHNY-25JAN31-B41.5", 41.0, 42.0),
        _market("KXHIGHNY-25JAN31-B39.5", 39.0, 40.0),
        _market("KXHIGHNY-25JAN31-B37.5", 37.0, 38.0),
    ]
    repaired = _repair_missing_strikes(markets)
    fixed = next(m for m in repaired if m.ticker == "KXHIGHNY-25JAN31-T44")
    assert fixed.floor_strike == pytest.approx(44.0)
    assert fixed.cap_strike is None


def test_repair_recovers_low_tail_bracket_using_sibling_bounds():
    markets = [
        _market("KXHIGHNY-25FEB09-T35", None, None, result="yes"),  # broken, low tail this time
        _market("KXHIGHNY-25FEB09-T42", 42.0, None),
        _market("KXHIGHNY-25FEB09-B41.5", 41.0, 42.0),
        _market("KXHIGHNY-25FEB09-B39.5", 39.0, 40.0),
        _market("KXHIGHNY-25FEB09-B37.5", 37.0, 38.0),
        _market("KXHIGHNY-25FEB09-B35.5", 35.0, 36.0),
    ]
    repaired = _repair_missing_strikes(markets)
    fixed = next(m for m in repaired if m.ticker == "KXHIGHNY-25FEB09-T35")
    assert fixed.floor_strike is None
    assert fixed.cap_strike == pytest.approx(35.0)


def test_repair_leaves_market_unfixed_when_no_siblings_have_strikes():
    markets = [_market("KXHIGHNY-25FEB09-T35", None, None, result="yes")]
    repaired = _repair_missing_strikes(markets)
    assert repaired[0].floor_strike is None
    assert repaired[0].cap_strike is None


def test_repair_leaves_market_unfixed_for_unparseable_ticker_suffix():
    markets = [
        _market("KXHIGHNY-25FEB09-WEIRD", None, None, result="yes"),
        _market("KXHIGHNY-25FEB09-B35.5", 35.0, 36.0),
    ]
    repaired = _repair_missing_strikes(markets)
    fixed = next(m for m in repaired if m.ticker == "KXHIGHNY-25FEB09-WEIRD")
    assert fixed.floor_strike is None
    assert fixed.cap_strike is None
