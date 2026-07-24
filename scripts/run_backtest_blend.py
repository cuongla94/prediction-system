"""Market/model blend: does p_final = weight * p_model + (1 - weight) * p_market,
for some fit weight, beat the raw market on its own — or does the market's own
price dominate any blend, same as every other candidate this project has
backtested against it?

Reuses backtest.calibration.fit_remaining_scale_fraction_by_brier as-is for
the weight grid search — it's already fully generic ({candidate: predictions}
-> best candidate by Brier against real outcomes), despite a name written for
a different, single-purpose caller. No new fitting function needed.

The weight is fit ONCE, pooled across every city's fit-window rows (a single
global "how much to trust the model vs. the market" number, not a per-city
one — the whole point is asking whether the model adds anything on top of
the market in general), then scored out-of-sample on every city's eval-window
rows, pooled the same way. Per-city bias/std (fit_empirical_normal) stays
per-city, same as every other backtest script here, since forecast bias is a
station-specific thing the blend weight isn't.

Kept separate from scripts/run_backtest.py for the same reason as
run_backtest_by_model.py and run_walk_forward.py: that script is the
production TRADEABLE/NO-EDGE gate and shouldn't grow slower or riskier to
change for exploratory analysis.

Usage: uv run scripts/run_backtest_blend.py [SERIES_TICKER ...]
"""

from __future__ import annotations

import sys
from datetime import date, timedelta

from dotenv import load_dotenv

from backtest.cache import cached_collect_rows
from backtest.calibration import fit_remaining_scale_fraction_by_brier, market_benchmark
from backtest.harness import BacktestRow, fit_empirical_normal, split_by_date
from kalshi_client import KalshiClient
from weather.probability import bracket_probability
from weather.stations import STATIONS

START_DATE = "2024-10-01"
END_DATE = (date.today() - timedelta(days=1)).isoformat()

# Same 6 original, most deeply backtested cities as the other Stage 2 scripts.
_DEFAULT_CITIES = ["KXHIGHNY", "KXHIGHCHI", "KXHIGHPHIL", "KXHIGHAUS", "KXHIGHDEN", "KXHIGHMIA"]

# 5-point-percent steps over the full [0, 1] range — fine enough to see a
# real optimum without an excessive number of Brier evaluations.
_WEIGHTS = [round(i / 20, 2) for i in range(21)]


def _model_and_market_preds(
    rows: list[BacktestRow], bias: float, std: float
) -> tuple[list[float], list[float], list[bool]]:
    """Rows without a market price can't be blended — filtered out, not
    padded with a placeholder, so the returned lists stay positionally
    aligned with each other and with their own outcomes."""
    usable = [r for r in rows if r.last_price is not None]
    model_preds = [bracket_probability(r.forecast_mean + bias, std, r.floor_strike, r.cap_strike) for r in usable]
    market_preds = [r.last_price for r in usable]
    outcomes = [r.actual_outcome for r in usable]
    return model_preds, market_preds, outcomes


def main() -> None:
    load_dotenv()
    cities = sys.argv[1:] or _DEFAULT_CITIES

    pooled_fit_model: list[float] = []
    pooled_fit_market: list[float] = []
    pooled_fit_outcomes: list[bool] = []
    pooled_eval_model: list[float] = []
    pooled_eval_market: list[float] = []
    pooled_eval_outcomes: list[bool] = []

    with KalshiClient() as client:
        for series_ticker in cities:
            station = STATIONS[series_ticker]
            print(f"\n=== {station.city} ({series_ticker}) ===")
            rows = cached_collect_rows(client, series_ticker, START_DATE, END_DATE, lead_days=1)
            if not rows:
                print("  no usable rows, skipping.")
                continue
            fit_rows, eval_rows = split_by_date(rows, fit_fraction=0.7)

            try:
                bias, std = fit_empirical_normal(fit_rows)
            except ValueError as exc:
                print(f"  can't fit ({exc}), skipping.")
                continue

            fit_model, fit_market, fit_outcomes = _model_and_market_preds(fit_rows, bias, std)
            eval_model, eval_market, eval_outcomes = _model_and_market_preds(eval_rows, bias, std)
            if not fit_model or not eval_model:
                print("  not enough priced rows to fit/score a blend, skipping.")
                continue

            pooled_fit_model.extend(fit_model)
            pooled_fit_market.extend(fit_market)
            pooled_fit_outcomes.extend(fit_outcomes)
            pooled_eval_model.extend(eval_model)
            pooled_eval_market.extend(eval_market)
            pooled_eval_outcomes.extend(eval_outcomes)
            print(
                f"  bias={bias:+.2f}F std={std:.2f}F  fit_n={len(fit_model)} priced rows  "
                f"eval_n={len(eval_model)} priced rows"
            )

    if not pooled_fit_model:
        print("\nNo priced rows across any city — nothing to fit a blend on.")
        return

    candidate_predictions = {
        w: [w * pm + (1 - w) * pk for pm, pk in zip(pooled_fit_model, pooled_fit_market, strict=True)]
        for w in _WEIGHTS
    }
    best_weight, fit_brier = fit_remaining_scale_fraction_by_brier(candidate_predictions, pooled_fit_outcomes)
    print(
        f"\n=== Weight fit on {len(pooled_fit_model)} pooled in-sample priced rows across all cities ===\n"
        f"  best weight (model share) = {best_weight:.2f}  (in-sample Brier {fit_brier:.4f})"
    )

    print(f"\n=== POOLED OUT-OF-SAMPLE, {len(pooled_eval_model)} priced eval rows: blend vs. model-only vs. market ===\n")
    blended_eval = [best_weight * pm + (1 - best_weight) * pk for pm, pk in zip(pooled_eval_model, pooled_eval_market, strict=True)]

    for label, preds in (("blend", blended_eval), ("model-only", pooled_eval_model)):
        bench = market_benchmark(preds, pooled_eval_market, pooled_eval_outcomes)
        if bench is None:
            print(f"  {label:<12}: no priced rows to compare against (untested)")
            continue
        verdict = "BEATS MARKET" if bench.beats_market else "no edge"
        print(
            f"  {label:<12}: n={bench.n:<5} Brier {bench.brier_model:.4f} vs market {bench.brier_market:.4f} "
            f"(skill {bench.skill_score:+.4f}) -> {verdict}"
        )

    print(
        f"\n  Fit weight {best_weight:.2f} means the grid search picked "
        f"{'the market alone' if best_weight == 0.0 else ('the model alone' if best_weight == 1.0 else 'a real mix of model and market')} "
        "as the in-sample-best blend."
    )


if __name__ == "__main__":
    main()
