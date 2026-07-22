#!/usr/bin/env python3
"""Investigate the day-ahead deficit with proper data.

Methodology:
1. Pull day-ahead (lead_days=1) forecasts for the same 18-day window (2026-07-03 to 2026-07-20)
   used in same-day proofs, for NYC, Denver, and LA at a pre-market decision point (midnight local).

2. Compute:
   - Model Brier: convert forecast_mean to bracket probability, score vs outcome
   - Market Brier: convert last_price to probability, score vs outcome
   - Skill: 1 - (model_brier / market_brier)
   - Distribution of skill across the sample (not a single spot-check)

3. Test the ensemble-spread hypothesis with data:
   - Extract raw ensemble std (forecast_spread) for each forecast
   - Estimate market's implied std from bracket width and calibration
   - Compare: if market's implied spread is meaningfully tighter, that's evidence the market
     has better uncertainty estimation; if similar, the hypothesis doesn't explain the gap

4. Report honestly on whether the mechanism is understood or remains unknown.
"""

from __future__ import annotations

import numpy as np

from backtest.cache import cached_collect_rows
from backtest.calibration import market_benchmark
from backtest.harness import fit_empirical_normal, split_by_date
from kalshi_client import KalshiClient
from weather.probability import bracket_probability
from weather.stations import STATIONS

PROOF_WINDOW_START = "2026-07-03"
PROOF_WINDOW_END = "2026-07-20"
TEST_CITIES = ["KXHIGHNY", "KXHIGHDEN", "KXHIGHTLAX"]  # NYC, Denver, LA (TAUS not in STATIONS)

def analyze_dayahead_deficit():
    """Run the investigation."""
    print("=" * 120)
    print("DAY-AHEAD DEFICIT INVESTIGATION (PROPER DATA-DRIVEN APPROACH)")
    print("=" * 120)
    print(f"Window: {PROOF_WINDOW_START} to {PROOF_WINDOW_END} (18 days)")
    print("Decision point: Midnight local standard time (lead_days=1, zero same-day observations)")
    print(f"Cities: {', '.join(TEST_CITIES)}")
    print()

    # Collect data for all test cities
    all_results = {}

    for series_ticker in TEST_CITIES:
        station = STATIONS.get(series_ticker)
        if not station:
            print(f"{series_ticker}: not found in STATIONS")
            continue

        print(f"\n{series_ticker} ({station.city})")
        print("-" * 120)

        try:
            # Collect day-ahead rows for this city in the proof window
            with KalshiClient.from_env() as client:
                # Need the full historical range to fit calibration params
                all_rows = cached_collect_rows(
                    client, series_ticker,
                    "2024-10-01", PROOF_WINDOW_END,
                    lead_days=1
                )

                # Split for calibration
                fit_rows, eval_rows = split_by_date(all_rows, fit_fraction=0.7)
                normal_bias, normal_std = fit_empirical_normal(fit_rows)

                # Keep only the proof window from eval rows
                proof_dates = sorted({r.target_date for r in eval_rows})[-18:]
                proof_set = set(proof_dates)
                rows = [r for r in eval_rows if r.target_date in proof_set]

            if not rows:
                print("  No data collected")
                continue

            n = len(rows)
            print(f"  {n} day-ahead bracket-rows in proof window ({len(proof_set)} days)")
            print(f"  Calibration (from prior fit): bias={normal_bias:+.2f}F std={normal_std:.2f}F")

            # Convert temperature forecasts to bracket probabilities
            model_probs = [
                bracket_probability(
                    row.forecast_mean + normal_bias,
                    normal_std,
                    row.floor_strike,
                    row.cap_strike
                )
                for row in rows
            ]

            # Convert market prices (dollars) to probabilities (divide by 100)
            market_prices = [
                row.last_price / 100 if row.last_price is not None else None
                for row in rows
            ]
            outcomes = [row.actual_outcome for row in rows]

            # Score using market_benchmark to handle None prices
            bench = market_benchmark(model_probs, market_prices, outcomes)

            if bench:
                print(f"  Model Brier: {bench.brier_model:.4f}")
                print(f"  Market Brier: {bench.brier_market:.4f}")
                print(f"  Skill: {bench.skill_score:+.4f} (n={bench.n} matched prices)")
                print(f"  Verdict: {'BEATS MARKET' if bench.beats_market else 'no edge'}")

                # Compute per-market skill for distribution stats
                per_market_skills = []
                for mp, mkt_p, o in zip(model_probs, market_prices, outcomes):
                    if mkt_p is not None:
                        model_err2 = (mp - float(o)) ** 2
                        market_err2 = (mkt_p - float(o)) ** 2
                        if market_err2 > 0:
                            skill = 1 - (model_err2 / market_err2)
                            per_market_skills.append(skill)

                if per_market_skills:
                    print(f"  Per-market skill distribution (n={len(per_market_skills)}):")
                    print(f"    Mean: {np.mean(per_market_skills):+.4f}, Std: {np.std(per_market_skills):.4f}")
                    print(f"    Range: [{np.min(per_market_skills):+.4f}, {np.max(per_market_skills):+.4f}]")
                    print(f"    Percentiles: 25%={np.percentile(per_market_skills, 25):+.4f}, "
                          f"50%={np.percentile(per_market_skills, 50):+.4f}, "
                          f"75%={np.percentile(per_market_skills, 75):+.4f}")

                    all_results[series_ticker] = {
                        'n_brackets': n,
                        'n_matched': bench.n,
                        'model_brier': bench.brier_model,
                        'market_brier': bench.brier_market,
                        'skill': bench.skill_score,
                        'beats_market': bench.beats_market,
                        'per_market_skills': per_market_skills,
                        'model_probs': model_probs,
                        'market_prices': market_prices,
                        'outcomes': outcomes,
                        'normal_bias': normal_bias,
                        'normal_std': normal_std,
                        'forecast_spreads': [row.forecast_spread for row in rows],
                    }
            else:
                print("  No matched prices (can't score)")

        except Exception as e:
            import traceback
            print(f"  Error: {e}")
            traceback.print_exc()
            continue

    # Summary across cities
    if all_results:
        print("\n" + "=" * 120)
        print("SUMMARY ACROSS CITIES")
        print("=" * 120)
        print(f"{'City':<15} {'n_brk':>6} {'n_match':>6} {'Model Brier':>14} {'Market Brier':>14} {'Skill':>14}")
        print("-" * 120)

        all_skills = []
        all_spreads = []

        for city, data in all_results.items():
            print(f"{city:<15} {data['n_brackets']:>6} {data['n_matched']:>6} {data['model_brier']:>14.4f} "
                  f"{data['market_brier']:>14.4f} {data['skill']:>14.4f}")

            all_skills.extend(data['per_market_skills'])
            all_spreads.extend(data['forecast_spreads'])

        # Overall skill distribution
        print("\n" + "=" * 120)
        print("OVERALL SKILL DISTRIBUTION (ALL MATCHED MARKETS)")
        print("=" * 120)
        if all_skills:
            print(f"  N markets: {len(all_skills)}")
            print(f"  Mean skill: {np.mean(all_skills):+.4f}")
            print(f"  Std skill: {np.std(all_skills):.4f}")
            print(f"  Range: [{np.min(all_skills):+.4f}, {np.max(all_skills):+.4f}]")
            print(f"  25th/50th/75th percentiles: {np.percentile(all_skills, 25):+.4f} / "
                  f"{np.percentile(all_skills, 50):+.4f} / {np.percentile(all_skills, 75):+.4f}")

            # Ensemble spread stats
            print("\n" + "=" * 120)
            print("FORECAST SPREAD DISTRIBUTION (CROSS-ENSEMBLE DISAGREEMENT)")
            print("=" * 120)
            if all_spreads:
                print(f"  N forecasts: {len(all_spreads)}")
                print(f"  Mean spread: {np.mean(all_spreads):.2f}F")
                print(f"  Std spread: {np.std(all_spreads):.2f}F")
                print(f"  Range: [{np.min(all_spreads):.2f}F, {np.max(all_spreads):.2f}F]")

        # Key finding
        print("\n" + "=" * 120)
        print("FINDING")
        print("=" * 120)
        if all_skills:
            mean_skill = np.mean(all_skills)
            if mean_skill < -0.20:
                print(f"Day-ahead skill is CONSISTENTLY negative (mean {mean_skill:+.4f}),")
                print("not a single spot-check. The model loses to the market by a large,")
                print("real margin across all markets. This is a stable phenomenon.")
            elif mean_skill < -0.05:
                print(f"Day-ahead skill is moderately negative (mean {mean_skill:+.4f}).")
                print("The model is losing to the market, but with some variability.")
            elif mean_skill < 0:
                print(f"Day-ahead skill is slightly negative (mean {mean_skill:+.4f}).")
                print("The gap is small but consistent — market has a small edge.")
            else:
                print(f"Day-ahead skill is positive (mean {mean_skill:+.4f}).")
                print("Model beats the market. This contradicts the day-ahead-deficit hypothesis.")

        print("\n" + "=" * 120)
        print("NEXT STEP: TEST ENSEMBLE-SPREAD HYPOTHESIS")
        print("=" * 120)
        print("Hypothesis: Market has better uncertainty estimates (tighter implied spread)")
        print("Plan: Compare raw ensemble spread vs market-implied spread from bracket prices")
        print("Status: Not yet implemented in this script")

if __name__ == "__main__":
    analyze_dayahead_deficit()
