"""Backtest the probability engine against ~1-2 years of settled Kalshi markets.

Compares three ways of turning a day-ahead ensemble forecast into a bracket
probability, all fit on the same chronological 70% of days and evaluated on the
same held-out 30% (apples to apples):

- Normal:      fixed empirical bias + fixed std — what the live pipeline
               actually uses (calibrated_bracket_probability), so its reliability
               here is the honest answer to "is the live model over/under-confident?"
- Student's t: fatter tails, fit directly on the residuals. Tried and rejected
               earlier (no Brier improvement) — kept as a standing check that it
               still doesn't win as more data accrues.
- Blended std: a heteroscedastic scale that widens on days the ensemble's own
               models disagree (backtest.harness.fit_spread_scale +
               weather.probability.heteroscedastic_bracket_probability). Not
               live; this is the comparison that decides whether it should be.

As of 2026-07-20 this also persists its per-city Brier scores and a pooled
reliability diagram (predicted vs. realized per probability bucket) to
pipeline_runs.detail via track_run, so the dashboard's /backtest page can show
whether a "98% win chance" bucket actually wins ~98% of the time — the
calibration criterion the paper-trading validation bar refers to — without a
human re-running this and reading stdout.

Usage: uv run scripts/run_backtest.py
"""

from __future__ import annotations

import json
from datetime import date, timedelta

from dotenv import load_dotenv

from backtest.calibration import MarketBenchmark, brier_score, bucket_calibration, market_benchmark
from backtest.cache import cached_collect_rows
from backtest.harness import BacktestRow, fit_empirical_normal, fit_spread_scale, fit_student_t, split_by_date
from kalshi_client import KalshiClient
from monitoring import track_run
from weather.probability import bracket_probability, heteroscedastic_bracket_probability
from weather.stations import STATIONS

START_DATE = "2024-10-01"
END_DATE = (date.today() - timedelta(days=1)).isoformat()

# Display label -> key stored in the reliability detail. "normal" is the
# live-pipeline model, listed first deliberately since it's the one whose
# calibration actually matters for live use.
VARIANTS: tuple[tuple[str, str], ...] = (
    ("Normal   ", "normal"),
    ("Student t", "student_t"),
    ("Blended  ", "blended_std"),
)


def _print_calibration(label: str, predictions: list[float], outcomes: list[bool]) -> None:
    print(f"  {label}: Brier={brier_score(predictions, outcomes):.4f}")
    for bucket in bucket_calibration(predictions, outcomes):
        if bucket.n == 0:
            continue
        print(
            f"      {bucket.label:>8}  n={bucket.n:<4} "
            f"predicted={bucket.mean_predicted:5.0%}  realized={bucket.realized_frequency:5.0%}"
        )


def evaluate_city(series_ticker: str, rows: list[BacktestRow]) -> dict:
    """Fit all three variants on the chronological-70% split and score them on
    the held-out 30%. Pure given `rows` (no I/O), so it's testable with
    synthetic BacktestRows. Returns per-variant eval predictions plus the
    shared outcomes and the fitted parameters — the caller pools predictions
    across cities (build_backtest_detail) rather than this storing them."""
    station = STATIONS[series_ticker]
    fit_rows, eval_rows = split_by_date(rows, fit_fraction=0.7)

    normal_bias, normal_std = fit_empirical_normal(fit_rows)
    t_df, t_loc, t_scale = fit_student_t(fit_rows)
    baseline_var, spread_coef = fit_spread_scale(fit_rows)

    predictions: dict[str, list[float]] = {"normal": [], "student_t": [], "blended_std": []}
    outcomes: list[bool] = []
    for row in eval_rows:
        predictions["normal"].append(
            bracket_probability(row.forecast_mean + normal_bias, normal_std, row.floor_strike, row.cap_strike)
        )
        predictions["student_t"].append(
            bracket_probability(row.forecast_mean + t_loc, t_scale, row.floor_strike, row.cap_strike, df=t_df)
        )
        predictions["blended_std"].append(
            heteroscedastic_bracket_probability(
                row.forecast_mean + normal_bias,
                baseline_var,
                spread_coef,
                row.forecast_spread,
                row.floor_strike,
                row.cap_strike,
            )
        )
        outcomes.append(row.actual_outcome)

    return {
        "city": station.city,
        "series_ticker": series_ticker,
        "eval_days": len({r.target_date for r in eval_rows}),
        "eval_rows": len(eval_rows),
        # Kalshi's own price on each eval row, for the market benchmark in
        # build_backtest_detail. Kept aligned index-for-index with
        # predictions/outcomes above; None where no price was recorded.
        "market_prices": [row.last_price for row in eval_rows],
        "fit": {
            # t_df/t_scale come from scipy as numpy scalars — cast to plain
            # float so the persisted JSON holds native numbers, not np.float64
            # (which only serializes today because it subclasses float).
            "normal_bias": round(normal_bias, 4),
            "normal_std": round(normal_std, 4),
            "t_df": round(float(t_df), 2),
            "t_scale": round(float(t_scale), 4),
            "baseline_std": round(baseline_var**0.5, 4),
            "spread_coef": round(spread_coef, 4),
        },
        "predictions": predictions,
        "outcomes": outcomes,
    }


def _bucket_dicts(predictions: list[float], outcomes: list[bool]) -> list[dict]:
    return [
        {
            "label": b.label,
            "low": b.low,
            "high": b.high,
            "n": b.n,
            "predicted": round(b.mean_predicted, 4),
            "realized": (round(b.realized_frequency, 4) if b.realized_frequency is not None else None),
        }
        for b in bucket_calibration(predictions, outcomes)
    ]


def build_backtest_detail(evaluations: list[dict], *, start_date: str, end_date: str) -> dict:
    """Turn per-city evaluate_city results into the JSON blob stored in
    pipeline_runs.detail: per-city Brier for each variant, plus a pooled
    reliability diagram (predicted vs. realized per bucket) per variant. Pure —
    the raw per-row predictions are aggregated away here, not persisted, so the
    stored detail stays small. Pooling across cities is a deliberate choice:
    the reliability of a single confidence bucket needs more samples than any
    one thin city has, and the page labels it as pooled."""
    variant_keys = [key for _, key in VARIANTS]
    per_city = []
    pooled_predictions: dict[str, list[float]] = {key: [] for key in variant_keys}
    pooled_outcomes: list[bool] = []
    pooled_market_prices: list[float | None] = []

    for ev in evaluations:
        # An evaluation without market prices scores as untested rather than
        # erroring — market_benchmark returns None for it, and _tradeable_
        # verdict treats "couldn't test" as a non-pass, which is the whole
        # point of the gate.
        market_prices = ev.get("market_prices") or [None] * len(ev["outcomes"])
        city_benchmarks = {
            key: market_benchmark(ev["predictions"][key], market_prices, ev["outcomes"])
            for key in variant_keys
        }
        per_city.append(
            {
                "city": ev["city"],
                "series_ticker": ev["series_ticker"],
                "eval_days": ev["eval_days"],
                "eval_rows": ev["eval_rows"],
                "fit": ev["fit"],
                "brier": {key: round(brier_score(ev["predictions"][key], ev["outcomes"]), 4) for key in variant_keys},
                "vs_market": {key: _benchmark_dict(bench) for key, bench in city_benchmarks.items()},
            }
        )
        for key in variant_keys:
            pooled_predictions[key].extend(ev["predictions"][key])
        pooled_outcomes.extend(ev["outcomes"])
        pooled_market_prices.extend(market_prices)

    pooled = {}
    if pooled_outcomes:
        for key in variant_keys:
            pooled[key] = {
                "brier": round(brier_score(pooled_predictions[key], pooled_outcomes), 4),
                "buckets": _bucket_dicts(pooled_predictions[key], pooled_outcomes),
                "vs_market": _benchmark_dict(
                    market_benchmark(pooled_predictions[key], pooled_market_prices, pooled_outcomes)
                ),
            }

    return {
        "start_date": start_date,
        "end_date": end_date,
        "variants": variant_keys,
        "eval_rows_total": len(pooled_outcomes),
        "per_city": per_city,
        "pooled": pooled,
        "tradeable": _tradeable_verdict(pooled, variant_keys),
    }


def _benchmark_dict(bench: MarketBenchmark | None) -> dict | None:
    if bench is None:
        return None
    return {
        "n": bench.n,
        "brier_model": round(bench.brier_model, 4),
        "brier_market": round(bench.brier_market, 4),
        "skill_score": round(bench.skill_score, 4),
        "beats_market": bench.beats_market,
    }


def _tradeable_verdict(pooled: dict, variant_keys: list[str]) -> dict:
    """The gate: is ANY variant good enough to justify risking capital?

    Deliberately separate from "which variant has the best Brier" — that
    question is only about which model is least wrong, and it has an answer
    even when every option is worthless to trade. This one asks whether the
    best variant beats the price it would be trading against, which is the
    thing that actually decides profitability, and which nothing in this
    script asked before 2026-07-20.

    A missing benchmark (no market prices on the eval rows) is reported as
    untested and does NOT pass — "couldn't check" must never read as "fine."
    """
    tested = {
        key: pooled[key]["vs_market"]
        for key in variant_keys
        if key in pooled and pooled[key].get("vs_market") is not None
    }
    if not tested:
        return {
            "verdict": "UNTESTED",
            "passes": False,
            "reason": "No market prices on the evaluation rows — the model was never compared against the price it would trade against.",
        }

    best_key = max(tested, key=lambda key: tested[key]["skill_score"])
    best = tested[best_key]
    passes = bool(best["beats_market"])
    return {
        "verdict": "TRADEABLE" if passes else "NO EDGE",
        "passes": passes,
        "best_variant": best_key,
        "skill_score": best["skill_score"],
        "brier_model": best["brier_model"],
        "brier_market": best["brier_market"],
        "n": best["n"],
        "reason": (
            f"{best_key} beats the market on {best['n']} held-out rows "
            f"(Brier {best['brier_model']} vs {best['brier_market']})."
            if passes
            else f"No variant beats the market. Best was {best_key}: Brier {best['brier_model']} "
            f"vs market's {best['brier_market']} on {best['n']} held-out rows "
            f"(skill {best['skill_score']:+.4f}). A calibrated model that only reproduces the "
            f"price is not tradeable — it loses at the rate of the fees."
        ),
    }


def main() -> None:
    load_dotenv()
    with track_run("run_backtest") as run, KalshiClient() as client:
        evaluations: list[dict] = []
        for series_ticker in STATIONS:
            station = STATIONS[series_ticker]
            print(f"\n=== {station.city} ({series_ticker}) ===")
            rows = cached_collect_rows(client, series_ticker, START_DATE, END_DATE, lead_days=1)
            if not rows:
                print("  no usable rows, skipping.")
                continue
            try:
                ev = evaluate_city(series_ticker, rows)
            except ValueError as exc:
                print(f"  can't fit distributions: {exc}")
                continue
            evaluations.append(ev)
            print(f"  {ev['eval_rows']} eval bracket-rows across {ev['eval_days']} days")
            for label, key in VARIANTS:
                _print_calibration(label, ev["predictions"][key], ev["outcomes"])

        if not evaluations:
            run.status = "failed"
            run.detail = "No series produced usable rows to backtest."
            print("\nNo series produced usable rows — nothing to persist.")
            return

        detail = build_backtest_detail(evaluations, start_date=START_DATE, end_date=END_DATE)
        pooled = detail["pooled"]
        tradeable = detail["tradeable"]
        run.summary = (
            f"Backtested {len(evaluations)} series ({detail['eval_rows_total']} held-out rows). "
            f"Pooled Brier: normal={pooled['normal']['brier']}, "
            f"student_t={pooled['student_t']['brier']}, blended_std={pooled['blended_std']['brier']}. "
            f"Vs market: {tradeable['verdict']}"
        )
        run.detail = json.dumps(detail)
        print(f"\n{run.summary}")

        print("\n=== MARKET BENCHMARK (the gate that decides tradeability) ===")
        for key in detail["variants"]:
            bench = pooled.get(key, {}).get("vs_market")
            if bench is None:
                print(f"  {key:>12}: no market prices on eval rows — untested")
                continue
            print(
                f"  {key:>12}: model Brier {bench['brier_model']:.4f} vs market {bench['brier_market']:.4f} "
                f"(skill {bench['skill_score']:+.4f}, n={bench['n']})"
            )
        print(f"\n  VERDICT: {tradeable['verdict']} — {tradeable['reason']}")

        # A backtest that runs clean but produces an untradeable model is not a
        # success: reporting it as one is what let this go unnoticed for 76
        # losing trades. The run itself didn't error, so this isn't "failed"
        # either — 'partial' is the honest status, and it's what /status shows.
        if not tradeable["passes"]:
            run.status = "partial"


if __name__ == "__main__":
    main()
