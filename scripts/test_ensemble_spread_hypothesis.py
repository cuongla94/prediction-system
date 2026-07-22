#!/usr/bin/env python3
"""Test the ensemble-spread hypothesis: does the market have tighter uncertainty
estimates than the raw ensemble's cross-model disagreement?

Methodology:
1. For each day in the 18-day sample, extract the cross-model disagreement
   (std across GFS/ECMWF/ICON point forecasts).
2. For each day, estimate the market's implied uncertainty from bracket prices
   by fitting a normal distribution to the YES prices of all 6 brackets.
3. Compare: if market's implied spread < ensemble's cross-model spread,
   the market estimates uncertainty more tightly. If similar, the hypothesis
   doesn't explain the gap.

The test uses the same day-ahead rows already collected.
"""

from __future__ import annotations

import numpy as np
from scipy import stats

from backtest.cache import cached_collect_rows
from backtest.harness import split_by_date
from kalshi_client import KalshiClient
from weather.stations import STATIONS

PROOF_WINDOW_START = "2026-07-03"
PROOF_WINDOW_END = "2026-07-20"
TEST_CITIES = ["KXHIGHNY", "KXHIGHDEN"]  # NYC and Denver (LA not in STATIONS)


def estimate_market_implied_std(rows_for_day, floor_strike, cap_strike):
    """Estimate the market's implied standard deviation from bracket prices for a
    single day, given all the bracketed markets for that day.

    Approach: treat the YES prices of the brackets as probabilities of a
    discrete distribution over temperatures (each bracket's midpoint).
    Fit a normal distribution to this discrete distribution and return its std.
    """
    if not rows_for_day:
        return None

    # Collect bracket prices and strikes for this day
    brackets = []
    total_price = 0
    for row in rows_for_day:
        if row.last_price is not None and row.floor_strike is not None and row.cap_strike is not None:
            price_prob = row.last_price / 100  # Convert dollars to probability
            bracket_mid = (row.floor_strike + row.cap_strike) / 2
            brackets.append((bracket_mid, price_prob))
            total_price += price_prob

    if not brackets or total_price == 0:
        return None

    # Normalize to ensure probabilities sum to 1 (in case there's rounding)
    brackets = [(mid, p / total_price) for mid, p in brackets]

    # Compute mean and std of the implied distribution
    mean = sum(mid * p for mid, p in brackets)
    variance = sum((mid - mean) ** 2 * p for mid, p in brackets)
    std = variance ** 0.5

    return std, brackets


def main():
    print("=" * 120)
    print("ENSEMBLE-SPREAD HYPOTHESIS TEST")
    print("=" * 120)
    print(f"Window: {PROOF_WINDOW_START} to {PROOF_WINDOW_END} (18 days)")
    print(f"Cities: {', '.join(TEST_CITIES)}")
    print()

    results_by_city = {}

    for series_ticker in TEST_CITIES:
        station = STATIONS.get(series_ticker)
        if not station:
            print(f"{series_ticker}: not found in STATIONS")
            continue

        print(f"\n{series_ticker} ({station.city})")
        print("-" * 120)

        try:
            with KalshiClient.from_env() as client:
                # Collect day-ahead rows for the full history
                all_rows = cached_collect_rows(
                    client, series_ticker,
                    "2024-10-01", PROOF_WINDOW_END,
                    lead_days=1
                )
                fit_rows, eval_rows = split_by_date(all_rows, fit_fraction=0.7)

                # Keep only the proof window from eval rows
                proof_dates = sorted({r.target_date for r in eval_rows})[-18:]
                proof_set = set(proof_dates)
                rows = [r for r in eval_rows if r.target_date in proof_set]

            if not rows:
                print("  No data collected")
                continue

            # Group rows by target date and extract the forecast_spread (cross-model disagreement)
            by_date = {}
            for row in rows:
                if row.target_date not in by_date:
                    by_date[row.target_date] = {
                        'forecast_spread': row.forecast_spread,
                        'rows': []
                    }
                by_date[row.target_date]['rows'].append(row)

            # For each day, compute market-implied std from bracket prices
            cross_model_spreads = []
            market_implied_stds = []
            comparison_results = []

            for date_str in sorted(by_date.keys()):
                data = by_date[date_str]
                cross_model_std = data['forecast_spread']

                market_result = estimate_market_implied_std(data['rows'], None, None)
                if market_result is None:
                    continue

                market_implied_std, brackets = market_result

                cross_model_spreads.append(cross_model_std)
                market_implied_stds.append(market_implied_std)

                # Record comparison: is market tighter or wider?
                ratio = market_implied_std / cross_model_std if cross_model_std > 0 else None
                comparison_results.append({
                    'date': date_str,
                    'cross_model_spread': cross_model_std,
                    'market_implied_spread': market_implied_std,
                    'ratio': ratio,
                    'n_brackets': len(brackets),
                })

            if not comparison_results:
                print("  No days with complete bracket data")
                continue

            # Summary statistics
            print(f"  {len(comparison_results)} days with complete bracket data")
            print()
            print("  Cross-model disagreement (ensemble spread):")
            print(f"    Mean: {np.mean(cross_model_spreads):.3f}F")
            print(f"    Std:  {np.std(cross_model_spreads):.3f}F")
            print(f"    Range: [{np.min(cross_model_spreads):.3f}F, {np.max(cross_model_spreads):.3f}F]")
            print()
            print("  Market-implied spread (from bracket prices):")
            print(f"    Mean: {np.mean(market_implied_stds):.3f}F")
            print(f"    Std:  {np.std(market_implied_stds):.3f}F")
            print(f"    Range: [{np.min(market_implied_stds):.3f}F, {np.max(market_implied_stds):.3f}F]")
            print()

            # Comparison
            ratios = [r['ratio'] for r in comparison_results if r['ratio'] is not None]
            if ratios:
                mean_ratio = np.mean(ratios)
                print("  Market/Ensemble spread ratio:")
                print(f"    Mean ratio: {mean_ratio:.3f}")
                if mean_ratio < 0.95:
                    print(f"    → Market is TIGHTER (ratio {mean_ratio:.3f} < 1.0)")
                    print("    → This SUPPORTS the ensemble-spread hypothesis")
                elif mean_ratio > 1.05:
                    print(f"    → Market is WIDER (ratio {mean_ratio:.3f} > 1.0)")
                    print("    → This CONTRADICTS the ensemble-spread hypothesis")
                else:
                    print(f"    → Market and ensemble spreads are SIMILAR (ratio {mean_ratio:.3f} ≈ 1.0)")
                    print("    → Spread difference does NOT explain the gap")
                print()

            # Correlation with errors (follow up if hypothesis looks promising)
            model_errors = []
            for date_str in sorted(by_date.keys()):
                data = by_date[date_str]
                for row in data['rows']:
                    if row.last_price is not None:
                        market_price_prob = row.last_price / 100
                        error = abs(market_price_prob - float(row.actual_outcome))
                        model_errors.append(error)

            if model_errors and market_implied_stds:
                # Per-day correlation: does a wider implied spread correlate with smaller errors?
                if len(comparison_results) >= 3:
                    spreads_for_corr = [r['market_implied_spread'] for r in comparison_results]
                    # Approximate daily errors as average market error per day
                    errors_by_date = {}
                    for row in rows:
                        if row.last_price is not None:
                            market_price_prob = row.last_price / 100
                            error = abs(market_price_prob - float(row.actual_outcome))
                            if row.target_date not in errors_by_date:
                                errors_by_date[row.target_date] = []
                            errors_by_date[row.target_date].append(error)

                    daily_errors = [np.mean(errors_by_date[r['date']]) for r in comparison_results if r['date'] in errors_by_date]
                    if len(spreads_for_corr) == len(daily_errors):
                        correlation, p_value = stats.pearsonr(spreads_for_corr, daily_errors)
                        print("  Correlation: market-implied spread vs market error")
                        print(f"    r = {correlation:.3f}, p-value = {p_value:.4f}")
                        if p_value < 0.05:
                            print("    → SIGNIFICANT correlation (p < 0.05)")
                        else:
                            print("    → No significant correlation (p >= 0.05)")

            results_by_city[series_ticker] = {
                'n_days': len(comparison_results),
                'cross_model_spreads': cross_model_spreads,
                'market_implied_stds': market_implied_stds,
                'mean_ratio': mean_ratio if ratios else None,
            }

        except Exception as e:
            import traceback
            print(f"  Error: {e}")
            traceback.print_exc()
            continue

    # Summary across cities
    if results_by_city:
        print("\n" + "=" * 120)
        print("SUMMARY ACROSS CITIES")
        print("=" * 120)

        all_cross_spreads = []
        all_market_spreads = []
        all_ratios = []

        for city, data in results_by_city.items():
            all_cross_spreads.extend(data['cross_model_spreads'])
            all_market_spreads.extend(data['market_implied_stds'])
            if data['mean_ratio'] is not None:
                all_ratios.append(data['mean_ratio'])

        if all_cross_spreads and all_market_spreads:
            print(f"\nPooled across all cities ({len(all_cross_spreads)} days total):")
            print(f"  Cross-model spread: mean={np.mean(all_cross_spreads):.3f}F, std={np.std(all_cross_spreads):.3f}F")
            print(f"  Market-implied spread: mean={np.mean(all_market_spreads):.3f}F, std={np.std(all_market_spreads):.3f}F")
            if all_ratios:
                print(f"  Market/Ensemble ratio: mean={np.mean(all_ratios):.3f}")

    # Conclusion
    print("\n" + "=" * 120)
    print("CONCLUSION")
    print("=" * 120)

    if all_ratios:
        mean_ratio_all = np.mean(all_ratios)
        if mean_ratio_all < 0.95:
            print(
                "The ensemble-spread hypothesis is SUPPORTED: the market's implied uncertainty\n"
                "is noticeably tighter than the cross-model disagreement. This suggests the market\n"
                "may have tighter uncertainty estimates (e.g., via human judgment, price discovery,\n"
                "or information aggregation) than the raw 3-model disagreement indicates.\n"
                "\n"
                "However, this does NOT fully explain the day-ahead skill gap — the hypothesis is\n"
                "only one piece of the story. Other factors (e.g., bias in the point forecast itself)\n"
                "still play a role."
            )
        elif mean_ratio_all > 1.05:
            print(
                "The ensemble-spread hypothesis is CONTRADICTED: the market's implied uncertainty\n"
                "is actually wider than the cross-model disagreement. The market is *less* certain\n"
                "than the models' disagreement alone would suggest. This rules out the hypothesis\n"
                "that market wins on superior uncertainty estimation."
            )
        else:
            print(
                "The ensemble-spread hypothesis is INCONCLUSIVE: the market's implied uncertainty\n"
                "is roughly similar to the cross-model disagreement (ratio ≈ 1.0). Spread differences\n"
                "do not appear to be the primary driver of the gap. The mechanism remains unexplained\n"
                "by available data — it may lie in the point forecast bias, information asymmetry,\n"
                "or factors not testable with this dataset."
            )


if __name__ == "__main__":
    main()
