from __future__ import annotations

import pytest

from backtest.harness import (
    BacktestRow,
    collect_residuals,
    fit_empirical_normal,
    fit_student_t,
    split_by_date,
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
