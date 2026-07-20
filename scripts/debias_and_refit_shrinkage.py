"""Third follow-up to scripts/run_sameday_proof.py (2026-07-20). The second
follow-up (scripts/fit_remaining_scale_by_brier.py) found remaining_scale_
fraction=1.0 winning outright — Brier got monotonically *worse* the more the
distribution shrank, at all three decision times — and flagged (logged, not
investigated) that 6 of the 7 days with a reconstructable point value showed
the day-ahead `loc` running hot by up to 6.2F. The deferred hypothesis: a
biased center makes shrinkage look actively harmful regardless of whether
narrowing the distribution is structurally useful, since narrowing just
concentrates probability mass tighter around a wrong number.

This checks that bias properly first — across all 18 days, not just the 7
with a point value, using backtest.harness.classify_forecast_vs_bracket
(whether `loc` fell inside, above ("hot"), or below ("cold") the settled
bracket's true range; works for a tail-bracket win too, unlike a
reconstructed point value). If confirmed and estimable, re-centers `loc` by
the estimated offset and reruns the exact same Brier-minimizing
remaining_scale_fraction grid search from the second follow-up against the
debiased center.

Reuses scripts.run_sameday_proof.collect_sameday_dataset() — same NYC data,
same three decision times, no new pull.

Usage: uv run scripts/debias_and_refit_shrinkage.py
"""

from __future__ import annotations

from datetime import date
from datetime import time as dtime

from dotenv import load_dotenv

from backtest.calibration import brier_score, fit_remaining_scale_fraction_by_brier
from backtest.harness import BacktestRow, classify_forecast_vs_bracket
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

_FRACTION_GRID: tuple[float, ...] = tuple(round(0.02 * i, 2) for i in range(1, 51))  # 0.02 .. 1.00


def _winning_row_per_day(dataset: SameDayDataset) -> list[BacktestRow]:
    """One row per day — the bracket that actually settled YES — deduped so
    a day's other 5 losing brackets don't get counted toward the bias
    tally."""
    seen: set[str] = set()
    winners: list[BacktestRow] = []
    for row in dataset.proof_rows:
        if row.target_date in seen or not row.actual_outcome:
            continue
        seen.add(row.target_date)
        winners.append(row)
    return winners


def _matched_rows(dataset: SameDayDataset, decision_time: dtime) -> list[BacktestRow]:
    rows = []
    for row in dataset.proof_rows:
        target = date.fromisoformat(row.target_date)
        ts = decision_ts(target, decision_time, dataset.tz)
        if price_as_of(dataset.candles_by_market.get(row.market_ticker, []), ts) is None:
            continue
        rows.append(row)
    return rows


def _predict(dataset: SameDayDataset, row: BacktestRow, decision_time: dtime, fraction: float, loc_offset: float) -> float:
    target = date.fromisoformat(row.target_date)
    observed = extreme_as_of(dataset.readings, target, decision_time, dataset.station.metric)
    loc = row.forecast_mean + dataset.normal_bias + loc_offset
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

    print(f"\n=== BIAS CHECK: loc vs actual, {dataset.station.city}, {len(dataset.proof_dates)} days ===\n")

    winners = _winning_row_per_day(dataset)
    tally = {"hot": 0, "cold": 0, "inside": 0}
    exact_misses: list[float] = []
    for row in sorted(winners, key=lambda r: r.target_date):
        loc = row.forecast_mean + dataset.normal_bias
        category = classify_forecast_vs_bracket(loc, row.floor_strike, row.cap_strike)
        tally[category] += 1
        exact_str = ""
        if row.approx_actual_temp is not None:
            miss = loc - row.approx_actual_temp
            exact_misses.append(miss)
            exact_str = f"  exact miss={miss:+.1f}F"
        print(f"  {row.target_date}: loc={loc:.1f}  bracket=[{row.floor_strike}, {row.cap_strike}]  {category:>7}{exact_str}")

    print(f"\n  Tally across all {len(winners)} days (bracket-resolution only): hot={tally['hot']}  cold={tally['cold']}  inside={tally['inside']}")
    mean_miss: float | None = None
    if exact_misses:
        mean_miss = sum(exact_misses) / len(exact_misses)
        print(
            f"  Exact miss (loc - actual) on the {len(exact_misses)} point-value days: mean={mean_miss:+.2f}F, "
            f"range [{min(exact_misses):+.1f}F, {max(exact_misses):+.1f}F]"
        )

    # Confirmation gate: require agreement between the two independent
    # signals -- a lopsided categorical tally across all 18 days (not
    # swamped by "inside"), AND a positive point-value mean on the smaller
    # exact-value subsample. Either alone could be noise on this sample
    # size; both agreeing is the honest bar for "confirmed" here, not a
    # rigorous significance test.
    confirmed = tally["hot"] > tally["cold"] and mean_miss is not None and mean_miss > 0
    if not confirmed:
        print("\n  Hot bias NOT confirmed by this check (tally and/or point-value mean don't agree) -- stopping, not re-centering.")
        return

    offset = -mean_miss
    print(f"\n  Hot bias CONFIRMED: re-centering loc by {offset:+.2f}F for everything below.")
    print(
        f"  Caveat: this offset is estimated in-sample, from the same {len(exact_misses)} point-value days "
        f"being re-scored below, and n is small — read what follows as 'does de-biasing move the needle,' "
        f"not a validated correction."
    )

    print("\n=== DEBIASING ALONE (fraction=1.0, no shrinkage) vs. the original un-debiased baseline ===\n")
    for decision_time in DECISION_TIMES:
        original = score_decision_time(dataset, decision_time, remaining_scale_fraction=1.0, loc_offset=0.0)
        debiased = score_decision_time(dataset, decision_time, remaining_scale_fraction=1.0, loc_offset=offset)
        print(f"--- {decision_time.strftime('%H:%M')} ---")
        print_result("conditioned, original center", original["conditioned"])  # type: ignore[arg-type]
        print_result("conditioned, debiased center", debiased["conditioned"])  # type: ignore[arg-type]
        print()

    print("=== RE-FITTING remaining_scale_fraction BY BRIER against the debiased center ===\n")
    fractions: dict[dtime, float] = {}
    for decision_time in DECISION_TIMES:
        rows = _matched_rows(dataset, decision_time)
        outcomes = [row.actual_outcome for row in rows]
        candidate_predictions = {
            fraction: [_predict(dataset, row, decision_time, fraction, offset) for row in rows]
            for fraction in _FRACTION_GRID
        }
        brier_by_fraction = {fraction: brier_score(preds, outcomes) for fraction, preds in candidate_predictions.items()}
        best_fraction, best_brier = fit_remaining_scale_fraction_by_brier(candidate_predictions, outcomes)
        fractions[decision_time] = best_fraction
        print(
            f"{decision_time.strftime('%H:%M')}: n={len(rows)} -> best fraction={best_fraction:.2f} (Brier {best_brier:.4f}); "
            f"fraction=1.0 Brier={brier_by_fraction[1.0]:.4f}; "
            f"curve range [{min(brier_by_fraction.values()):.4f}, {max(brier_by_fraction.values()):.4f}]"
        )

    print("\n=== FINAL RE-SCORE: debiased center + fitted shrinkage, vs. the same three baselines from every prior round ===\n")
    for decision_time in DECISION_TIMES:
        fraction = fractions[decision_time]
        original_unconditional = score_decision_time(dataset, decision_time, remaining_scale_fraction=1.0, loc_offset=0.0)
        final = score_decision_time(dataset, decision_time, remaining_scale_fraction=fraction, loc_offset=offset)
        print(f"--- {decision_time.strftime('%H:%M')} (offset={offset:+.2f}F, fitted fraction={fraction:.2f}) ---")
        print_result("market benchmark (unchanged)", original_unconditional["normal"])  # type: ignore[arg-type]
        print_result("original: conditioned, fraction=1.0, no debias", original_unconditional["conditioned"])  # type: ignore[arg-type]
        print_result("final: conditioned, debiased + fitted fraction", final["conditioned"])  # type: ignore[arg-type]
        print()


if __name__ == "__main__":
    main()
