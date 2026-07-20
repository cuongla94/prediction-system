"""Second follow-up to scripts/run_sameday_proof.py (2026-07-20). The first
follow-up (scripts/fit_remaining_scale.py) tried fitting remaining_scale_
fraction from point-value residuals and found it could only fit on 7 of the
18-day proof window's days (a "between"-bracket win is required for a point
value; tail-bracket wins are excluded) — and all 7 of those days shared a
confound (the day-ahead forecast ran hot enough that the observation never
caught up to it, at any of the three decision times), which left that
method structurally unable to detect shrinkage either way, regardless of
whether shrinkage is real.

This fits remaining_scale_fraction differently: minimize Brier score
directly against real settled outcomes (backtest.calibration.
fit_remaining_scale_fraction_by_brier), using every one of the 108
bracket-rows in the same 18-day window, tail-bracket wins included — no
point-value reconstruction needed, since Brier only needs (predicted
probability, actual win/loss).

Reuses scripts.run_sameday_proof.collect_sameday_dataset() — same NYC data,
same three decision times, no new pull.

Usage: uv run scripts/fit_remaining_scale_by_brier.py
"""

from __future__ import annotations

from datetime import date
from datetime import time as dtime

from dotenv import load_dotenv

from backtest.calibration import brier_score, fit_remaining_scale_fraction_by_brier
from scripts.run_sameday_proof import (
    DECISION_TIMES,
    SameDayDataset,
    collect_sameday_dataset,
    decision_ts,
    price_as_of,
    print_result,
    score_decision_time,
)
from weather.historical_observations import extreme_as_of
from weather.probability import bracket_probability, observation_conditioned_bracket_probability

# Fine enough to find a meaningfully different optimum than 1.0 if one
# exists, cheap enough that a grid search (not an iterative optimizer) is
# plenty — ~50 candidates x ~108 rows is instant, and grid search doesn't
# assume the Brier-vs-fraction relationship is unimodal, which an optimizer
# like golden-section search would.
_FRACTION_GRID: tuple[float, ...] = tuple(round(0.02 * i, 2) for i in range(1, 51))  # 0.02 .. 1.00


def _matched_rows(dataset: SameDayDataset, decision_time: dtime) -> list:
    """proof_rows restricted to those with a matched market price at this
    decision time — the same n=108 filter score_decision_time already
    applies, so the fit and the final comparison run on identical data."""
    rows = []
    for row in dataset.proof_rows:
        target = date.fromisoformat(row.target_date)
        ts = decision_ts(target, decision_time, dataset.tz)
        if price_as_of(dataset.candles_by_market.get(row.market_ticker, []), ts) is None:
            continue
        rows.append(row)
    return rows


def _predict(dataset: SameDayDataset, row, decision_time: dtime, fraction: float) -> float:
    target = date.fromisoformat(row.target_date)
    observed = extreme_as_of(dataset.readings, target, decision_time, dataset.station.metric)
    loc = row.forecast_mean + dataset.normal_bias
    if observed is None:
        return bracket_probability(loc, dataset.normal_std, row.floor_strike, row.cap_strike)
    return observation_conditioned_bracket_probability(
        loc,
        dataset.normal_std,
        row.floor_strike,
        row.cap_strike,
        dataset.station.metric,
        observed,
        remaining_scale_fraction=fraction,
    )


def main() -> None:
    load_dotenv()
    dataset = collect_sameday_dataset()

    print(f"\n=== FITTING remaining_scale_fraction BY BRIER: {dataset.station.city}, {len(dataset.proof_dates)} days ===\n")

    fractions: dict[dtime, float] = {}
    for decision_time in DECISION_TIMES:
        rows = _matched_rows(dataset, decision_time)
        outcomes = [row.actual_outcome for row in rows]

        candidate_predictions = {
            fraction: [_predict(dataset, row, decision_time, fraction) for row in rows] for fraction in _FRACTION_GRID
        }
        brier_by_fraction = {fraction: brier_score(preds, outcomes) for fraction, preds in candidate_predictions.items()}
        best_fraction, best_brier = fit_remaining_scale_fraction_by_brier(candidate_predictions, outcomes)
        fractions[decision_time] = best_fraction

        # Show the shrinkage curve, not just the winner -- a flat curve
        # (every fraction scoring about the same) is a genuinely different
        # finding from "1.0 won by a clear margin over a real curve."
        print(
            f"{decision_time.strftime('%H:%M')}: n={len(rows)} rows -> best fraction={best_fraction:.2f} "
            f"(Brier {best_brier:.4f}); fraction=1.0 Brier={brier_by_fraction[1.0]:.4f}; "
            f"curve range [{min(brier_by_fraction.values()):.4f}, {max(brier_by_fraction.values()):.4f}] "
            f"across the {len(_FRACTION_GRID)}-point grid"
        )

    print("\n=== RE-SCORING with fitted remaining_scale_fraction (vs. the same three Brier numbers as before) ===\n")
    for decision_time in DECISION_TIMES:
        fraction = fractions[decision_time]
        result_unshrunk = score_decision_time(dataset, decision_time, remaining_scale_fraction=1.0)
        result_fitted = score_decision_time(dataset, decision_time, remaining_scale_fraction=fraction)
        print(f"--- {decision_time.strftime('%H:%M')} (fitted fraction={fraction:.2f}) ---")
        print_result("market benchmark (unchanged)", result_unshrunk["normal"])  # type: ignore[arg-type]
        print_result("conditioned, fraction=1.0", result_unshrunk["conditioned"])  # type: ignore[arg-type]
        print_result("conditioned, fitted", result_fitted["conditioned"])  # type: ignore[arg-type]
        print()


if __name__ == "__main__":
    main()
