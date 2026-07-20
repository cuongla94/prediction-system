from __future__ import annotations

from dataclasses import replace

import pytest

from backtest.calibration import brier_score
from backtest.harness import BacktestRow
from scripts.run_backtest import build_backtest_detail, evaluate_city
from weather.probability import calibrated_bracket_probability


def _row(date: str, actual: float, spread: float) -> BacktestRow:
    """One between-bracket day (B79.5 covering {79,80}); actual_outcome is set
    from whether `actual` lands in the bracket, approx_actual_temp carries the
    numeric value the fitters need."""
    return BacktestRow(
        city="NYC",
        series_ticker="KXHIGHNY",
        event_ticker=f"KXHIGHNY-{date}",
        market_ticker=f"KXHIGHNY-{date}-B79.5",
        target_date=date,
        forecast_mean=80.0,
        forecast_spread=spread,
        n_models=3,
        actual_outcome=(79.0 <= actual <= 80.0),
        last_price=0.5,
        floor_strike=79.0,
        cap_strike=80.0,
        approx_actual_temp=actual,
    )


def _synthetic_rows() -> list[BacktestRow]:
    # 12 distinct days with varied actuals and spreads — enough for the 70/30
    # split to leave both a fittable fit set (student-t/spread-scale need >=3)
    # and a non-empty eval set.
    actuals = [80.0, 78.5, 81.0, 79.5, 77.0, 82.0, 80.5, 79.0, 83.0, 78.0, 81.5, 79.8]
    spreads = [1.0, 2.5, 1.5, 3.0, 2.0, 1.2, 2.8, 1.8, 3.2, 1.1, 2.2, 1.6]
    return [
        _row(f"2026-01-{i + 1:02d}", actual, spread)
        for i, (actual, spread) in enumerate(zip(actuals, spreads))
    ]


def test_evaluate_city_produces_all_four_variants_over_the_held_out_set():
    ev = evaluate_city("KXHIGHNY", _synthetic_rows())

    assert ev["city"] == "NYC"
    assert ev["series_ticker"] == "KXHIGHNY"
    assert ev["eval_rows"] > 0
    assert set(ev["predictions"]) == {"shipped", "normal", "student_t", "blended_std"}
    # Every variant predicts once per eval row, aligned with outcomes.
    for preds in ev["predictions"].values():
        assert len(preds) == ev["eval_rows"]
        assert all(0.0 <= p <= 1.0 for p in preds)
    assert len(ev["outcomes"]) == ev["eval_rows"]
    # spread_coef is clamped non-negative by fit_spread_scale.
    assert ev["fit"]["spread_coef"] >= 0.0


def test_shipped_variant_matches_calibrated_bracket_probability_not_the_refit_normal():
    # The whole point of the 2026-07-20 fix: "shipped" must be numerically
    # identical to what generate_alerts.py actually calls (calibrated_
    # bracket_probability), not a re-fit flat bias that happens to look
    # similar. Every synthetic row shares the same forecast_mean/floor/cap,
    # so "shipped"'s prediction is the same constant value on every eval row
    # -- the exact value calibrated_bracket_probability computes directly
    # for July (target_month=7), using NYC's live bias_for_month(7)
    # (-1.46F), not any bias this script fits itself.
    rows = _synthetic_rows()  # dated 2026-01-* -- retarget to July
    july_rows = [replace(row, target_date=row.target_date.replace("2026-01", "2026-07")) for row in rows]
    ev = evaluate_city("KXHIGHNY", july_rows)

    expected = calibrated_bracket_probability("KXHIGHNY", 80.0, 79.0, 80.0, 7)
    assert ev["predictions"]["shipped"] == [pytest.approx(expected)] * ev["eval_rows"]
    # And it must differ from "normal" -- proof this isn't accidentally
    # computing the same flat number twice under two names.
    assert ev["predictions"]["shipped"][0] != pytest.approx(ev["predictions"]["normal"][0])


def test_build_backtest_detail_computes_per_city_brier_and_pooled_buckets():
    ev = {
        "city": "NYC",
        "series_ticker": "KXHIGHNY",
        "eval_days": 2,
        "eval_rows": 4,
        "fit": {"normal_bias": 0.0, "normal_std": 2.0, "t_df": 5.0, "t_scale": 2.0, "baseline_std": 2.0, "spread_coef": 0.0},
        "predictions": {
            "shipped": [0.92, 0.08, 0.82, 0.18],
            "normal": [0.9, 0.1, 0.8, 0.2],
            "student_t": [0.85, 0.15, 0.75, 0.25],
            "blended_std": [0.88, 0.12, 0.78, 0.22],
        },
        "outcomes": [True, False, True, False],
    }

    detail = build_backtest_detail([ev], start_date="2024-10-01", end_date="2026-07-18")

    assert detail["variants"] == ["shipped", "normal", "student_t", "blended_std"]
    assert detail["eval_rows_total"] == 4
    assert detail["start_date"] == "2024-10-01"

    city = detail["per_city"][0]
    assert city["brier"]["normal"] == pytest.approx(round(brier_score(ev["predictions"]["normal"], ev["outcomes"]), 4))

    # Pooled reliability diagram present for each variant.
    for key in ("shipped", "normal", "student_t", "blended_std"):
        assert "brier" in detail["pooled"][key]
        assert isinstance(detail["pooled"][key]["buckets"], list)

    # The well-calibrated pooled predictions land in the extreme buckets.
    normal_buckets = {b["label"]: b for b in detail["pooled"]["normal"]["buckets"]}
    assert normal_buckets["80%-90%"]["n"] == 1
    assert normal_buckets["10%-20%"]["n"] == 1


def test_build_backtest_detail_handles_no_evaluations():
    detail = build_backtest_detail([], start_date="2024-10-01", end_date="2026-07-18")
    assert detail["per_city"] == []
    assert detail["pooled"] == {}
    assert detail["eval_rows_total"] == 0


# --- tradeability gate (added 2026-07-20) ---


def _evaluation(predictions: list[float], market_prices, outcomes: list[bool]) -> dict:
    """A minimal evaluate_city-shaped dict, enough for build_backtest_detail."""
    return {
        "city": "NYC",
        "series_ticker": "KXHIGHNY",
        "eval_days": len(outcomes),
        "eval_rows": len(outcomes),
        "fit": {},
        "predictions": {key: list(predictions) for key in ("shipped", "normal", "student_t", "blended_std")},
        "outcomes": list(outcomes),
        "market_prices": market_prices,
    }


def test_gate_reports_no_edge_when_the_model_only_matches_the_market():
    detail = build_backtest_detail(
        [_evaluation([0.2, 0.8, 0.5], [0.2, 0.8, 0.5], [False, True, True])],
        start_date="2026-01-01",
        end_date="2026-07-01",
    )
    assert detail["tradeable"]["verdict"] == "NO EDGE"
    assert detail["tradeable"]["passes"] is False


def test_gate_passes_only_when_the_model_beats_the_market():
    detail = build_backtest_detail(
        [_evaluation([0.05, 0.95, 0.05], [0.4, 0.6, 0.4], [False, True, False])],
        start_date="2026-01-01",
        end_date="2026-07-01",
    )
    assert detail["tradeable"]["verdict"] == "TRADEABLE"
    assert detail["tradeable"]["passes"] is True
    assert detail["tradeable"]["skill_score"] > 0


def test_gate_reports_untested_rather_than_passing_without_market_prices():
    # The critical property: a backtest that *couldn't* run the comparison
    # must never read as a pass. This is the shape every pre-2026-07-20 run had.
    detail = build_backtest_detail(
        [_evaluation([0.2, 0.8], None, [False, True])],
        start_date="2026-01-01",
        end_date="2026-07-01",
    )
    assert detail["tradeable"]["verdict"] == "UNTESTED"
    assert detail["tradeable"]["passes"] is False


def test_per_city_detail_carries_the_market_comparison():
    detail = build_backtest_detail(
        [_evaluation([0.2, 0.8, 0.5], [0.2, 0.8, 0.5], [False, True, True])],
        start_date="2026-01-01",
        end_date="2026-07-01",
    )
    vs_market = detail["per_city"][0]["vs_market"]["normal"]
    assert vs_market["n"] == 3
    assert vs_market["brier_model"] == pytest.approx(vs_market["brier_market"])
