"""Follow-up to scripts/run_sameday_proof.py (2026-07-20): that proof found
observation_conditioned_bracket_probability still losing to the market at
all three decision times, and diagnosed why — `remaining_scale_fraction`
sits at its unshrunk default of 1.0 everywhere it's called, so the model's
"rest of day" distribution never narrows through the day the way the
market's own pricing visibly does. This fits that fraction empirically
instead of leaving it a guess, and re-scores the same three Brier numbers to
see how much of the gap actually closes.

Reuses scripts.run_sameday_proof.collect_sameday_dataset() rather than
re-pulling the same 18 NYC days, IEM observations, and 108 markets'
candlesticks a second time — same day-ahead fit, same proof window, same
intraday snapshots the first proof scored, so the two reports are directly
comparable rather than each resting on a different pull of live data.

See backtest.harness.fit_remaining_scale_fraction for the derivation and its
explicit small-sample caveat (n=18 days here) — read the fitted fraction as
"does shrinkage move the needle," not a validated production parameter.

Usage: uv run scripts/fit_remaining_scale.py
"""

from __future__ import annotations

from datetime import date
from datetime import time as dtime

from dotenv import load_dotenv

from backtest.harness import fit_remaining_scale_fraction
from scripts.run_sameday_proof import DECISION_TIMES, SameDayDataset, print_result, collect_sameday_dataset, score_decision_time
from weather.historical_observations import extreme_as_of


def _fit_pairs_for_decision_time(dataset: SameDayDataset, decision_time: dtime) -> list[tuple[float, float, float]]:
    """(actual_final_temp, loc, observed_so_far) — one tuple per day, not per
    bracket-row (a day's 6 brackets would otherwise count its residual 6x
    and inflate the apparent sample size). Days without a usable
    approx_actual_temp (a tail-bracket win only gives an inequality, not a
    point value — see BacktestRow's own docstring) or without an IEM
    observation are excluded from the *fit*, but the fraction, once fitted,
    still gets applied when *scoring* every row, whether or not that day had
    a usable point value here."""
    seen_dates: set[str] = set()
    pairs: list[tuple[float, float, float]] = []
    for row in dataset.proof_rows:
        if row.target_date in seen_dates or row.approx_actual_temp is None:
            continue
        target = date.fromisoformat(row.target_date)
        observed = extreme_as_of(dataset.readings, target, decision_time, dataset.station.metric)
        if observed is None:
            continue
        seen_dates.add(row.target_date)
        loc = row.forecast_mean + dataset.normal_bias
        pairs.append((row.approx_actual_temp, loc, observed))
    return pairs


def main() -> None:
    load_dotenv()
    dataset = collect_sameday_dataset()

    print(f"\n=== FITTING remaining_scale_fraction: {dataset.station.city}, {len(dataset.proof_dates)} days ===\n")
    print(f"day-ahead baseline scale (normal_std): {dataset.normal_std:.2f}F\n")

    fractions: dict[dtime, float] = {}
    for decision_time in DECISION_TIMES:
        pairs = _fit_pairs_for_decision_time(dataset, decision_time)
        if len(pairs) < 2:
            print(f"{decision_time.strftime('%H:%M')}: only {len(pairs)} usable day(s) — can't fit, leaving at 1.0 (no shrinkage).")
            fractions[decision_time] = 1.0
            continue
        fraction = fit_remaining_scale_fraction(pairs, baseline_scale=dataset.normal_std)
        fractions[decision_time] = fraction
        remaining_residuals = [actual - max(loc, observed) for actual, loc, observed in pairs]
        print(
            f"{decision_time.strftime('%H:%M')}: fitted on {len(pairs)}/{len(dataset.proof_dates)} days "
            f"-> remaining_scale_fraction={fraction:.3f} "
            f"(remaining residuals range {min(remaining_residuals):+.1f}F to {max(remaining_residuals):+.1f}F)"
        )

    print("\n=== RE-SCORING with fitted remaining_scale_fraction (vs. the same three Brier numbers as before) ===\n")
    for decision_time in DECISION_TIMES:
        fraction = fractions[decision_time]
        result_unshrunk = score_decision_time(dataset, decision_time, remaining_scale_fraction=1.0)
        result_fitted = score_decision_time(dataset, decision_time, remaining_scale_fraction=fraction)
        print(f"--- {decision_time.strftime('%H:%M')} (fitted fraction={fraction:.3f}) ---")
        print_result("conditioned, fraction=1.0", result_unshrunk["conditioned"])  # type: ignore[arg-type]
        print_result("conditioned, fitted", result_fitted["conditioned"])  # type: ignore[arg-type]
        print()


if __name__ == "__main__":
    main()
