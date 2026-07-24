"""Rolling walk-forward validation: does the live calibration (bias + std,
fit via fit_empirical_normal) actually hold up as more history accumulates,
or does it improve / stay flat / degrade fold over fold?

Answers retrospectively, in one run over existing history, the same question
DECISIONS.md's WEAK revisit trigger watches for via weekly cron (4 consecutive
improving weekly recalibrations) — see kalshi-no-edge-root-cause memory for
the trigger's exact wording and why trading-mechanics work is paused pending it.

Kept separate from scripts/run_backtest.py for the same reason as
run_backtest_by_model.py: that script is the production TRADEABLE/NO-EDGE
gate and shouldn't grow slower or riskier to change for exploratory analysis.

Usage: uv run scripts/run_walk_forward.py [SERIES_TICKER ...]
"""

from __future__ import annotations

import sys
from datetime import date, timedelta

from dotenv import load_dotenv

from backtest.cache import cached_collect_rows
from backtest.calibration import brier_score, market_benchmark
from backtest.harness import fit_empirical_normal, rolling_splits
from kalshi_client import KalshiClient
from weather.probability import bracket_probability
from weather.stations import STATIONS

START_DATE = "2024-10-01"
END_DATE = (date.today() - timedelta(days=1)).isoformat()

# Same 6 original, most deeply backtested cities as run_backtest_by_model.py's
# default — the newer 34 have far less history per city and would leave folds
# too thin to be meaningful.
_DEFAULT_CITIES = ["KXHIGHNY", "KXHIGHCHI", "KXHIGHPHIL", "KXHIGHAUS", "KXHIGHDEN", "KXHIGHMIA"]

N_FOLDS = 4
MIN_FIT_FRACTION = 0.4


def main() -> None:
    load_dotenv()
    cities = sys.argv[1:] or _DEFAULT_CITIES

    # Pooled across cities per fold index — fold 0 across every city, fold 1
    # across every city, etc. — matching run_backtest_by_model.py's own
    # "a single thin city has too few days to judge a fold on its own" reasoning.
    pooled_preds: list[list[float]] = [[] for _ in range(N_FOLDS)]
    pooled_prices: list[list[float | None]] = [[] for _ in range(N_FOLDS)]
    pooled_outcomes: list[list[bool]] = [[] for _ in range(N_FOLDS)]

    with KalshiClient() as client:
        for series_ticker in cities:
            station = STATIONS[series_ticker]
            print(f"\n=== {station.city} ({series_ticker}) ===")
            rows = cached_collect_rows(client, series_ticker, START_DATE, END_DATE, lead_days=1)
            if not rows:
                print("  no usable rows, skipping.")
                continue

            try:
                folds = rolling_splits(rows, n_folds=N_FOLDS, min_fit_fraction=MIN_FIT_FRACTION)
            except ValueError as exc:
                print(f"  can't build folds ({exc}), skipping city.")
                continue

            for fold_index, (fit_rows, eval_rows) in enumerate(folds):
                try:
                    bias, std = fit_empirical_normal(fit_rows)
                except ValueError as exc:
                    print(f"  fold {fold_index}: can't fit ({exc}), skipping.")
                    continue
                preds = [bracket_probability(r.forecast_mean + bias, std, r.floor_strike, r.cap_strike) for r in eval_rows]
                prices = [r.last_price for r in eval_rows]
                outcomes = [r.actual_outcome for r in eval_rows]
                pooled_preds[fold_index].extend(preds)
                pooled_prices[fold_index].extend(prices)
                pooled_outcomes[fold_index].extend(outcomes)
                print(
                    f"  fold {fold_index}: fit_n={len({r.target_date for r in fit_rows})} days  "
                    f"bias={bias:+.2f}F std={std:.2f}F  eval_n={len(preds)}  Brier={brier_score(preds, outcomes):.4f}"
                )

    print(f"\n=== POOLED ACROSS CITIES, {N_FOLDS} FOLDS: trend in model Brier + market skill over time ===\n")
    skill_scores: list[float] = []
    for fold_index in range(N_FOLDS):
        outcomes = pooled_outcomes[fold_index]
        if not outcomes:
            print(f"  fold {fold_index}: no data")
            continue
        bench = market_benchmark(pooled_preds[fold_index], pooled_prices[fold_index], outcomes)
        if bench is None:
            print(f"  fold {fold_index}: n={len(outcomes)}, no market prices to compare against (untested)")
            continue
        skill_scores.append(bench.skill_score)
        verdict = "BEATS MARKET" if bench.beats_market else "no edge"
        print(
            f"  fold {fold_index}: n={bench.n:<5} Brier {bench.brier_model:.4f} vs market {bench.brier_market:.4f} "
            f"(skill {bench.skill_score:+.4f}) -> {verdict}"
        )

    if len(skill_scores) >= 2:
        diffs = [b - a for a, b in zip(skill_scores, skill_scores[1:])]
        improving = sum(1 for d in diffs if d > 0)
        degrading = sum(1 for d in diffs if d < 0)
        print(f"\n  Skill-score trend across {len(skill_scores)} scored folds: {[f'{s:+.4f}' for s in skill_scores]}")
        if improving == len(diffs):
            print("  -> consistently IMPROVING fold over fold.")
        elif degrading == len(diffs):
            print("  -> consistently DEGRADING fold over fold.")
        else:
            print(f"  -> MIXED trend ({improving} improving step(s), {degrading} degrading step(s)) — not a clean signal either way.")
    else:
        print("\n  Fewer than 2 scored folds — not enough to characterize a trend.")


if __name__ == "__main__":
    main()
