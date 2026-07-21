"""Does the same-day observation-conditioning wall generalize beyond NYC, or
is NYC's specific result (shipped-bias-conditioned skill -0.21/-1.19/-2.73 at
09:00/12:00/15:00, market ~3.7x better even at 15:00 -- reproduced by this
script's own run against scripts/sameday_proof_shipped_bias.py before writing
this generalization) a quirk of that one city/dataset? Built 2026-07-20.

Runs the EXACT same thin-proof methodology already built and verified for NYC
(scripts/run_sameday_proof.py + scripts/sameday_proof_shipped_bias.py) against
additional cities: same three decision times, same ~18-day out-of-sample
window, same shipped seasonal bias via get_calibration(...).bias_for_month()
rather than a flat fit (round 4 already established that distinction matters
for NYC), same day-ahead loc/scale otherwise held fixed, same
remaining_scale_fraction grid search. Reuses collect_sameday_dataset,
score_decision_time, price_as_of, decision_ts, print_result and
fit_remaining_scale_fraction_by_brier directly -- no reimplementation.

Cities chosen (see kalshi-backtest-findings memory for the full reasoning):
  - KXHIGHDEN (Denver): the OTHER monthly-bias city already known reliable
    (unlike Philadelphia's noisy case -- see kalshi-philadelphia-monthly-bias
    -recheck memory), and one of only two stations (with KNYC) documented in
    weather/nws_observations.py as single-feed, so it carries the same
    METAR/5-minute-feed-confound-free guarantee as NYC's own proof.
  - KXHIGHAUS (Austin): a flat-bias city, tests whether the wall depends on
    seasonal correction quality at all. Not documented single-feed, but
    verified live (2026-07-20) that weather.historical_observations.
    fetch_asos_temperatures' server-side report_type=3,4 filter yields the
    same clean ~hourly cadence for AUS as for the documented single-feed
    stations (~24 readings/day, no 5-minute-feed contamination) -- that
    filter, not the station's live-feed topology, is what protects this
    proof's IEM data, so the single-feed distinction from nws_observations.py
    (which guards the LIVE api.weather.gov path) does not actually transfer
    to this backtest's data source. Reported as a checked assumption, not an
    unstated one.

One methodological generalization beyond the NYC-specific script: the shipped
bias is looked up PER ROW via get_calibration(ticker).bias_for_month(target.
month), not a single flat offset assumed to cover the whole window.
sameday_proof_shipped_bias.py could get away with one offset because NYC's
18-day window happened to sit entirely in July; this does not assume that
holds for every city and is exactly as correct if it does.

Not part of the live pipeline; writes nothing to the database. Same role as
scripts/forecast_vs_market.py and its own predecessor here.

Usage: uv run scripts/sameday_proof_multi_city.py
"""

from __future__ import annotations

from datetime import date
from datetime import time as dtime

from dotenv import load_dotenv

from backtest.calibration import MarketBenchmark, brier_score, fit_remaining_scale_fraction_by_brier, market_benchmark
from backtest.harness import BacktestRow
from scripts.run_sameday_proof import (
    DECISION_TIMES,
    SameDayDataset,
    collect_sameday_dataset,
    decision_ts,
    price_as_of,
    print_result,
    score_decision_time,
)
from weather.calibration_params import get_calibration
from weather.historical_observations import extreme_as_of
from weather.probability import bracket_probability, observation_conditioned_bracket_probability

CITIES: tuple[str, ...] = ("KXHIGHDEN", "KXHIGHAUS")
_FRACTION_GRID: tuple[float, ...] = tuple(round(0.02 * i, 2) for i in range(1, 51))  # 0.02 .. 1.00


def _matched_rows(dataset: SameDayDataset, decision_time: dtime) -> list[BacktestRow]:
    rows = []
    for row in dataset.proof_rows:
        target = date.fromisoformat(row.target_date)
        ts = decision_ts(target, decision_time, dataset.tz)
        if price_as_of(dataset.candles_by_market.get(row.market_ticker, []), ts) is None:
            continue
        rows.append(row)
    return rows


def _shipped_loc(series_ticker: str, row: BacktestRow) -> float:
    """Per-row shipped bias -- see module docstring on why this is looked up
    per row rather than as one flat offset like the NYC-only script uses."""
    target = date.fromisoformat(row.target_date)
    return row.forecast_mean + get_calibration(series_ticker).bias_for_month(target.month)


def _predict_shipped(
    dataset: SameDayDataset, series_ticker: str, row: BacktestRow, decision_time: dtime, fraction: float
) -> float:
    target = date.fromisoformat(row.target_date)
    loc = _shipped_loc(series_ticker, row)
    observed = extreme_as_of(dataset.readings, target, decision_time, dataset.station.metric)
    if observed is None:
        return bracket_probability(loc, dataset.normal_std, row.floor_strike, row.cap_strike)
    return observation_conditioned_bracket_probability(
        loc, dataset.normal_std, row.floor_strike, row.cap_strike,
        dataset.station.metric, observed, remaining_scale_fraction=fraction,
    )


def score_shipped_decision_time(
    dataset: SameDayDataset, series_ticker: str, decision_time: dtime, fraction: float = 1.0
) -> MarketBenchmark | None:
    preds: list[float] = []
    market_prices: list[float] = []
    outcomes: list[bool] = []
    for row in dataset.proof_rows:
        target = date.fromisoformat(row.target_date)
        ts = decision_ts(target, decision_time, dataset.tz)
        price = price_as_of(dataset.candles_by_market.get(row.market_ticker, []), ts)
        if price is None:
            continue
        preds.append(_predict_shipped(dataset, series_ticker, row, decision_time, fraction))
        market_prices.append(price)
        outcomes.append(row.actual_outcome)
    return market_benchmark(preds, market_prices, outcomes)


def run_city(series_ticker: str) -> dict:
    calibration = get_calibration(series_ticker)
    kind = "monthly" if calibration.monthly_bias is not None else "flat"
    print(f"\n{'=' * 70}\n{series_ticker} ({kind}-bias city)\n{'=' * 70}")
    dataset = collect_sameday_dataset(series_ticker)

    months = sorted({date.fromisoformat(r.target_date).month for r in dataset.proof_rows})
    print(f"  proof window spans month(s): {months} (per-row bias lookup, not a single offset)")

    results: dict[dtime, dict] = {}
    print(f"\n--- fraction=1.0: market / flat-conditioned / shipped-conditioned, {series_ticker} ---")
    for decision_time in DECISION_TIMES:
        flat = score_decision_time(dataset, decision_time, remaining_scale_fraction=1.0, loc_offset=0.0)
        shipped_bench = score_shipped_decision_time(dataset, series_ticker, decision_time, fraction=1.0)
        print(f"  {decision_time.strftime('%H:%M')}:")
        print_result("market benchmark", flat["normal"])  # type: ignore[arg-type]
        print_result("flat-bias conditioned", flat["conditioned"])  # type: ignore[arg-type]
        print_result("shipped-bias conditioned", shipped_bench)
        results[decision_time] = {"market_only": flat["normal"], "shipped_1_0": shipped_bench}

    print(f"\n--- fitted remaining_scale_fraction by Brier, shipped bias, {series_ticker} ---")
    fractions: dict[dtime, float] = {}
    for decision_time in DECISION_TIMES:
        rows = _matched_rows(dataset, decision_time)
        outcomes = [row.actual_outcome for row in rows]
        candidate_predictions = {
            fraction: [_predict_shipped(dataset, series_ticker, row, decision_time, fraction) for row in rows]
            for fraction in _FRACTION_GRID
        }
        brier_by_fraction = {f: brier_score(preds, outcomes) for f, preds in candidate_predictions.items()}
        best_fraction, best_brier = fit_remaining_scale_fraction_by_brier(candidate_predictions, outcomes)
        fractions[decision_time] = best_fraction
        print(
            f"  {decision_time.strftime('%H:%M')}: n={len(rows)} -> best fraction={best_fraction:.2f} "
            f"(Brier {best_brier:.4f}); fraction=1.0 Brier={brier_by_fraction[1.0]:.4f}; "
            f"curve range [{min(brier_by_fraction.values()):.4f}, {max(brier_by_fraction.values()):.4f}]"
        )

    print(f"\n--- FINAL: shipped bias + fitted shrinkage vs market, {series_ticker} ---")
    for decision_time in DECISION_TIMES:
        fraction = fractions[decision_time]
        final = score_shipped_decision_time(dataset, series_ticker, decision_time, fraction=fraction)
        print(f"  {decision_time.strftime('%H:%M')} (fitted fraction={fraction:.2f}):")
        print_result("market benchmark", results[decision_time]["market_only"])
        print_result("shipped bias, fitted shrinkage", final)
        results[decision_time]["fitted_fraction"] = fraction
        results[decision_time]["shipped_fitted"] = final

    return {
        "series_ticker": series_ticker,
        "city": dataset.station.city,
        "kind": kind,
        "n_days": len(dataset.proof_dates),
        "n_readings": len(dataset.readings),
        "results": results,
    }


def main() -> None:
    load_dotenv()
    all_results = [run_city(ticker) for ticker in CITIES]

    print(f"\n\n{'#' * 78}\n# SUMMARY ACROSS CITIES (fraction=1.0, apples-to-apples with NYC's own numbers)\n{'#' * 78}")
    header = f"{'city':<14} {'bias':<8} {'time':<6} {'n':>4} {'model':>8} {'market':>8} {'skill':>9}  verdict"
    print(header)
    print("-" * len(header))
    for entry in all_results:
        for decision_time in DECISION_TIMES:
            bench = entry["results"][decision_time]["shipped_1_0"]
            if bench is None:
                print(f"{entry['city']:<14} {entry['kind']:<8} {decision_time.strftime('%H:%M'):<6}   -        -        -        -  no matched rows")
                continue
            verdict = "BEATS MARKET" if bench.beats_market else "no edge"
            print(
                f"{entry['city']:<14} {entry['kind']:<8} {decision_time.strftime('%H:%M'):<6} "
                f"{bench.n:>4} {bench.brier_model:>8.4f} {bench.brier_market:>8.4f} "
                f"{bench.skill_score:>+9.4f}  {verdict}"
            )

    print(f"\n{'#' * 78}\n# SUMMARY (shipped bias + fitted shrinkage -- the final, best-case number)\n{'#' * 78}")
    print(header)
    print("-" * len(header))
    for entry in all_results:
        for decision_time in DECISION_TIMES:
            bench = entry["results"][decision_time].get("shipped_fitted")
            if bench is None:
                continue
            verdict = "BEATS MARKET" if bench.beats_market else "no edge"
            frac = entry["results"][decision_time]["fitted_fraction"]
            print(
                f"{entry['city']:<14} {entry['kind']:<8} {decision_time.strftime('%H:%M'):<6} "
                f"{bench.n:>4} {bench.brier_model:>8.4f} {bench.brier_market:>8.4f} "
                f"{bench.skill_score:>+9.4f}  {verdict} (fraction={frac:.2f})"
            )


if __name__ == "__main__":
    main()
