"""Fourth follow-up to scripts/run_sameday_proof.py (2026-07-20) — the first
real test of whether the live system has edge. The prior three rounds tested
a center that isn't what's actually running: round 1-2 used the flat
fit_empirical_normal bias (+0.97F for NYC), round 3 used an ad-hoc offset
fit from the proof's own 7 point-value days (-2.62F). Neither is
weather/calibration_params.py's real shipped July bias for NYC
(bias_for_month(7) = -1.4589F, confirmed live 2026-07-20 to be within
~0.5F of the ideal for this window — see kalshi-backtest-findings memory).

This re-centers `loc` on the exact shipped bias (get_calibration(series_
ticker).bias_for_month(month) — not hardcoded, read live from weather/
calibration_params.py) and reruns the same two things every prior round
reported: Brier/skill at fraction=1.0, and the Brier-minimizing
remaining_scale_fraction grid search, at all three decision times.

Reuses scripts.run_sameday_proof.collect_sameday_dataset() — same NYC data,
same three decision times, no new pull.

Usage: uv run scripts/sameday_proof_shipped_bias.py
"""

from __future__ import annotations

from datetime import date
from datetime import time as dtime

from dotenv import load_dotenv

from backtest.calibration import brier_score, fit_remaining_scale_fraction_by_brier
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

    # Every proof-window day is July 2026 (single month), so one offset
    # covers every row -- no per-row month lookup needed. Read live from
    # calibration_params.py rather than hardcoding the number quoted in
    # conversation, so this always tests whatever is actually shipped.
    calibration = get_calibration("KXHIGHNY")
    shipped_bias = calibration.bias_for_month(7)
    offset = shipped_bias - dataset.normal_bias
    print(
        f"\nShipped July bias (live, weather/calibration_params.py): {shipped_bias:+.4f}F. "
        f"Flat bias this proof otherwise uses: {dataset.normal_bias:+.4f}F. "
        f"Offset applied to every row's loc: {offset:+.4f}F."
    )

    print("\n=== fraction=1.0, centered on the REAL shipped bias, vs. every prior round's baseline ===\n")
    for decision_time in DECISION_TIMES:
        flat = score_decision_time(dataset, decision_time, remaining_scale_fraction=1.0, loc_offset=0.0)
        shipped = score_decision_time(dataset, decision_time, remaining_scale_fraction=1.0, loc_offset=offset)
        print(f"--- {decision_time.strftime('%H:%M')} ---")
        print_result("market benchmark", flat["normal"])  # type: ignore[arg-type]
        print_result("conditioned, flat bias (rounds 1-2)", flat["conditioned"])  # type: ignore[arg-type]
        print_result("conditioned, shipped bias (this round)", shipped["conditioned"])  # type: ignore[arg-type]
        print()

    print("=== RE-FITTING remaining_scale_fraction BY BRIER, centered on the shipped bias ===\n")
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

    print("\n=== FINAL: shipped bias + fitted shrinkage, vs. market ===\n")
    for decision_time in DECISION_TIMES:
        fraction = fractions[decision_time]
        market_only = score_decision_time(dataset, decision_time, remaining_scale_fraction=1.0, loc_offset=0.0)
        final = score_decision_time(dataset, decision_time, remaining_scale_fraction=fraction, loc_offset=offset)
        print(f"--- {decision_time.strftime('%H:%M')} (shipped bias {shipped_bias:+.2f}F, fitted fraction={fraction:.2f}) ---")
        print_result("market benchmark", market_only["normal"])  # type: ignore[arg-type]
        print_result("shipped bias, fitted shrinkage", final["conditioned"])  # type: ignore[arg-type]
        print()


if __name__ == "__main__":
    main()
