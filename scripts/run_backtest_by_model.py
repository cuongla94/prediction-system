"""Per-forecast-model ablation: does any single ensemble member (GFS, ECMWF,
ICON), scored on its own, beat the pooled blend the live pipeline actually
uses — or beat the market?

Kept separate from scripts/run_backtest.py deliberately: that script is the
production TRADEABLE/NO-EDGE gate (dashboard, pipeline_runs, the paper-trading
validation bar all read its output) and shouldn't grow slower or riskier to
change for an exploratory comparison. This is read-only analysis, no
persistence beyond stdout.

Uses backtest.cache.cached_collect_rows — found live 2026-07-23 while
building this that a cache entry written before per_model_forecast existed
on BacktestRow deserialized with that field silently defaulted to None
instead of erroring, so every city read from a warm cache came back with 0
usable per-model days. Fixed at the source (backtest/cache.py's
_CACHE_SCHEMA_VERSION, bumped whenever BacktestRow's shape changes — see
that module), not worked around here, so this script gets caching's real
benefit on repeat runs like the rest of backtest/.

Each candidate model may cover a different subset of eval rows than another
(weather/historical_forecast.py's own docstring: "older dates may be GFS-only,
since ECMWF/ICON archives start later") — scored on whatever rows that model
actually has data for, not silently padded or dropped to match another
model's coverage, and the row count is reported per candidate so a thin
model comparison isn't mistaken for a fair one.

Usage: uv run scripts/run_backtest_by_model.py [SERIES_TICKER ...]
"""

from __future__ import annotations

import sys
from datetime import date, timedelta

from dotenv import load_dotenv

from backtest.calibration import brier_score, market_benchmark
from backtest.harness import BacktestRow, collect_rows, fit_empirical_normal, fit_empirical_normal_for_model, split_by_date
from kalshi_client import KalshiClient
from weather.historical_forecast import DEFAULT_MODELS
from weather.probability import bracket_probability
from weather.stations import STATIONS

START_DATE = "2024-10-01"
END_DATE = (date.today() - timedelta(days=1)).isoformat()

# Default to the 6 original, most deeply backtested cities if none given on
# the command line — the newer 34 have far less history per city and would
# make a first ablation run noisy and slow for no real benefit.
_DEFAULT_CITIES = ["KXHIGHNY", "KXHIGHCHI", "KXHIGHPHIL", "KXHIGHAUS", "KXHIGHDEN", "KXHIGHMIA"]


def _score_blend(eval_rows: list[BacktestRow], bias: float, std: float) -> tuple[list[float], list[float | None], list[bool]]:
    preds = [bracket_probability(r.forecast_mean + bias, std, r.floor_strike, r.cap_strike) for r in eval_rows]
    return preds, [r.last_price for r in eval_rows], [r.actual_outcome for r in eval_rows]


def _score_model(
    eval_rows: list[BacktestRow], model: str, bias: float, std: float
) -> tuple[list[float], list[float | None], list[bool]]:
    preds, prices, outcomes = [], [], []
    for r in eval_rows:
        if r.per_model_forecast is None or model not in r.per_model_forecast:
            continue
        preds.append(bracket_probability(r.per_model_forecast[model] + bias, std, r.floor_strike, r.cap_strike))
        prices.append(r.last_price)
        outcomes.append(r.actual_outcome)
    return preds, prices, outcomes


def main() -> None:
    load_dotenv()
    cities = sys.argv[1:] or _DEFAULT_CITIES

    # Pooled across cities per candidate, matching run_backtest.py's own
    # "a single thin city has too few days to judge a candidate on its own" reasoning.
    pooled_preds: dict[str, list[float]] = {"blend": [], **{m: [] for m in DEFAULT_MODELS}}
    pooled_prices: dict[str, list[float | None]] = {"blend": [], **{m: [] for m in DEFAULT_MODELS}}
    pooled_outcomes: dict[str, list[bool]] = {"blend": [], **{m: [] for m in DEFAULT_MODELS}}

    with KalshiClient() as client:
        for series_ticker in cities:
            station = STATIONS[series_ticker]
            print(f"\n=== {station.city} ({series_ticker}) ===")
            rows = collect_rows(client, series_ticker, START_DATE, END_DATE, lead_days=1)
            if not rows:
                print("  no usable rows, skipping.")
                continue
            fit_rows, eval_rows = split_by_date(rows, fit_fraction=0.7)

            try:
                blend_bias, blend_std = fit_empirical_normal(fit_rows)
            except ValueError as exc:
                print(f"  blend: can't fit ({exc}), skipping.")
            else:
                preds, prices, outcomes = _score_blend(eval_rows, blend_bias, blend_std)
                pooled_preds["blend"].extend(preds)
                pooled_prices["blend"].extend(prices)
                pooled_outcomes["blend"].extend(outcomes)
                print(f"  blend      : bias={blend_bias:+.2f}F std={blend_std:.2f}F  n={len(preds)}  Brier={brier_score(preds, outcomes):.4f}")

            for model in DEFAULT_MODELS:
                try:
                    bias, std = fit_empirical_normal_for_model(fit_rows, model)
                except ValueError as exc:
                    print(f"  {model:<11}: can't fit ({exc}), skipping this city for this model.")
                    continue
                preds, prices, outcomes = _score_model(eval_rows, model, bias, std)
                if not preds:
                    print(f"  {model:<11}: no eval rows have this model's data, skipping.")
                    continue
                pooled_preds[model].extend(preds)
                pooled_prices[model].extend(prices)
                pooled_outcomes[model].extend(outcomes)
                print(f"  {model:<11}: bias={bias:+.2f}F std={std:.2f}F  n={len(preds)}  Brier={brier_score(preds, outcomes):.4f}")

    print("\n=== POOLED ACROSS CITIES: model Brier vs. market, out of sample ===\n")
    results = []
    for candidate in ("blend", *DEFAULT_MODELS):
        outcomes = pooled_outcomes[candidate]
        if not outcomes:
            print(f"  {candidate:<15}: no data")
            continue
        bench = market_benchmark(pooled_preds[candidate], pooled_prices[candidate], outcomes)
        if bench is None:
            print(f"  {candidate:<15}: n={len(outcomes)}, no market prices to compare against (untested)")
            continue
        verdict = "BEATS MARKET" if bench.beats_market else "no edge"
        print(
            f"  {candidate:<15}: n={bench.n:<5} Brier {bench.brier_model:.4f} vs market {bench.brier_market:.4f} "
            f"(skill {bench.skill_score:+.4f}) -> {verdict}"
        )
        results.append((candidate, bench))

    if results:
        best_candidate, best_bench = max(results, key=lambda cb: cb[1].skill_score)
        print(f"\n  Best candidate: {best_candidate} (skill {best_bench.skill_score:+.4f})")
        if best_bench.beats_market:
            print("  -> a single model or the blend beats the market out of sample. Re-run scripts/run_backtest.py to confirm via the real gate.")
        else:
            print("  -> no candidate — blended or any single model — beats the market. Splitting the ensemble apart does not surface a hidden edge.")


if __name__ == "__main__":
    main()
