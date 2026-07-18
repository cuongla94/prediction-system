"""Per-city bias and spread corrections, fit from the 2026-07-17 backtest
(~600 real settled days per city, Oct 2024-Jul 2026 — see scripts/run_backtest.py
and kalshi-backtest-findings memory for the full methodology and comparison this
came from).

Two things were validated there: every city's day-1-ahead forecast runs
systematically cold by 1.4-2.1°F (mean_bias corrects this), and the live
pooled-ensemble std generally runs wider than the empirically-observed forecast
error (std here replaces it). A fatter-tailed Student's t distribution was also
tested and rejected — it didn't improve Brier score in any city and was worse in
several — so this stays Normal.

This is a fitted snapshot, not a permanent constant: re-run the backtest
periodically as more settled days accumulate, and update these. Don't hand-edit
the numbers without re-running the fit.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CalibrationParams:
    mean_bias: float  # add to the live ensemble mean; positive = forecast runs cold
    std: float  # replaces the live ensemble's own std, which runs wide (see above)
    fit_date: str
    fit_days: int


CALIBRATION: dict[str, CalibrationParams] = {
    "KXHIGHNY": CalibrationParams(mean_bias=0.92, std=2.22, fit_date="2026-07-17", fit_days=458),
    "KXHIGHCHI": CalibrationParams(mean_bias=1.77, std=1.69, fit_date="2026-07-17", fit_days=458),
    "KXHIGHPHIL": CalibrationParams(mean_bias=2.02, std=1.90, fit_date="2026-07-17", fit_days=423),
    "KXHIGHAUS": CalibrationParams(mean_bias=2.07, std=1.95, fit_date="2026-07-17", fit_days=458),
    "KXHIGHDEN": CalibrationParams(mean_bias=1.37, std=1.96, fit_date="2026-07-17", fit_days=423),
    "KXHIGHMIA": CalibrationParams(mean_bias=2.13, std=1.50, fit_date="2026-07-17", fit_days=458),
}


def get_calibration(series_ticker: str) -> CalibrationParams:
    try:
        return CALIBRATION[series_ticker]
    except KeyError:
        raise KeyError(
            f"No fitted calibration for {series_ticker!r}. Run scripts/run_backtest.py "
            "and add its output here before using this series — don't fall back to an "
            "unvalidated default for a city that hasn't actually been backtested."
        ) from None
