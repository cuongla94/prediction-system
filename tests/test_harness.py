from __future__ import annotations

import pytest

from backtest.harness import (
    BacktestRow,
    _repair_missing_strikes,
    collect_residuals,
    fit_empirical_normal,
    fit_monthly_bias,
    fit_student_t,
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
