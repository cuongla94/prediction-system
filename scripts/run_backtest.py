"""Backtest the probability engine against ~1-2 years of settled Kalshi markets,
comparing the original Normal(empirical mean+std) approach against a Student's t
fit — targeted at the 2026-07-17 finding that the model was under-confident near
0% and over-confident above ~40%, the classic signature of tails that are too
thin. Both are fit on the same chronological 70% of days and evaluated on the
same held-out 30%, so the comparison is apples to apples.

Usage: uv run scripts/run_backtest.py
"""

from __future__ import annotations

from datetime import date, timedelta

from backtest.calibration import brier_score, bucket_calibration
from backtest.harness import collect_rows, fit_empirical_normal, fit_student_t, split_by_date
from kalshi_client import KalshiClient
from weather.probability import bracket_probability
from weather.stations import STATIONS

START_DATE = "2024-10-01"
END_DATE = (date.today() - timedelta(days=1)).isoformat()


def _print_calibration(label: str, predictions: list[float], outcomes: list[bool]) -> None:
    print(f"  {label}: Brier={brier_score(predictions, outcomes):.4f}")
    for bucket in bucket_calibration(predictions, outcomes):
        if bucket.n == 0:
            continue
        print(
            f"      {bucket.label:>8}  n={bucket.n:<4} "
            f"predicted={bucket.mean_predicted:5.0%}  realized={bucket.realized_frequency:5.0%}"
        )


def run_for_city(client: KalshiClient, series_ticker: str) -> None:
    station = STATIONS[series_ticker]
    print(f"\n=== {station.city} ({series_ticker}) ===")

    rows = collect_rows(client, series_ticker, START_DATE, END_DATE, lead_days=1)
    if not rows:
        print("  no usable rows, skipping.")
        return

    unique_dates = {row.target_date for row in rows}
    print(f"  {len(rows)} bracket-rows across {len(unique_dates)} settled days")

    fit_rows, eval_rows = split_by_date(rows, fit_fraction=0.7)
    print(
        f"  fit: {len({r.target_date for r in fit_rows})} days, "
        f"eval: {len({r.target_date for r in eval_rows})} days (chronological split, no overlap)"
    )

    try:
        normal_bias, normal_std = fit_empirical_normal(fit_rows)
        t_df, t_loc, t_scale = fit_student_t(fit_rows)
    except ValueError as exc:
        print(f"  can't fit distributions: {exc}")
        return

    print(f"  Normal fit:      bias={normal_bias:+.2f}°F  std={normal_std:.2f}°F")
    print(f"  Student's t fit: bias={t_loc:+.2f}°F  scale={t_scale:.2f}°F  df={t_df:.1f}")

    normal_predictions: list[float] = []
    t_predictions: list[float] = []
    outcomes: list[bool] = []
    for row in eval_rows:
        normal_predictions.append(
            bracket_probability(row.forecast_mean + normal_bias, normal_std, row.floor_strike, row.cap_strike)
        )
        t_predictions.append(
            bracket_probability(
                row.forecast_mean + t_loc, t_scale, row.floor_strike, row.cap_strike, df=t_df
            )
        )
        outcomes.append(row.actual_outcome)

    print(f"  eval set: {len(outcomes)} bracket-rows")
    _print_calibration("Normal   ", normal_predictions, outcomes)
    _print_calibration("Student t", t_predictions, outcomes)


def main() -> None:
    with KalshiClient() as client:
        for series_ticker in STATIONS:
            run_for_city(client, series_ticker)


if __name__ == "__main__":
    main()
