from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from monitoring.trend import (
    MIN_SERIES_FOR_TREND,
    build_trend,
    comparable_trend,
    pooled_market_benchmark,
    summarize,
)

BASE = datetime(2026, 7, 20, 9, 0, tzinfo=UTC)


def _series(ticker="KXHIGHNY", adopted=0.12, market=0.01, n=400, using="flat"):
    skill = 0.0 if market == 0 else 1 - adopted / market
    return {
        "city": "NYC",
        "series_ticker": ticker,
        "flat_brier": adopted,
        "monthly_brier": adopted + 0.01,
        "using": using,
        "fit_days": 400,
        "adopted_brier": adopted,
        "market_brier": market,
        "skill_vs_market": round(skill, 4),
        "n_market_rows": n,
    }


def _run(run_id, week_offset, rows):
    return (run_id, BASE + timedelta(weeks=week_offset), json.dumps(rows))


def _full(adopted, market, count=MIN_SERIES_FOR_TREND, n=400):
    return [_series(f"T{i}", adopted=adopted, market=market, n=n) for i in range(count)]


# --- pooling ---------------------------------------------------------------


def test_pooling_is_row_weighted_not_a_plain_average():
    # A 900-row series and a 100-row series must not count equally. Brier is a
    # mean, so weighting by row count reconstructs the true pooled figure.
    rows = [
        _series("A", adopted=0.10, market=0.02, n=900),
        _series("B", adopted=0.30, market=0.02, n=100),
    ]
    pooled = pooled_market_benchmark(rows)
    assert pooled["brier_model"] == pytest.approx((0.10 * 900 + 0.30 * 100) / 1000)
    assert pooled["n"] == 1000


def test_pooled_skill_is_recomputed_not_averaged():
    # Skill is a ratio; a mean of ratios is not the ratio of means. Averaging
    # would let one tiny-n series with an extreme ratio dominate.
    rows = [
        _series("A", adopted=0.10, market=0.02, n=990),
        _series("B", adopted=0.90, market=0.001, n=10),
    ]
    pooled = pooled_market_benchmark(rows)
    # Expected is derived from UNROUNDED pooled means, which is what the
    # implementation uses. Recomputing from the rounded brier_model/brier_market
    # it reports would disagree in the 3rd decimal here: skill is a ratio and
    # the market denominator is ~0.02, so 4dp rounding is amplified ~50x. The
    # implementation rounding last is the correct order.
    total = 990 + 10
    model = (0.10 * 990 + 0.90 * 10) / total
    market = (0.02 * 990 + 0.001 * 10) / total
    assert pooled["skill_vs_market"] == pytest.approx(round(1 - model / market, 4))
    naive = sum(r["skill_vs_market"] for r in rows) / len(rows)
    assert pooled["skill_vs_market"] != pytest.approx(naive)


def test_pooling_returns_none_without_market_rows():
    assert pooled_market_benchmark([_series(n=0)]) is None
    assert pooled_market_benchmark([]) is None


def test_pooling_handles_a_perfect_market_without_dividing_by_zero():
    assert pooled_market_benchmark([_series(adopted=0.1, market=0.0)])["skill_vs_market"] == 0.0


# --- backward compatibility ------------------------------------------------


def test_reads_runs_written_before_the_market_benchmark_existed():
    # The four real runs stored on 2026-07-19 have only flat/monthly/using.
    # They must still contribute an outcome-Brier point rather than being
    # dropped, or the trend would appear to start from scratch.
    legacy = [
        {"city": "NYC", "series_ticker": "T1", "flat_brier": 0.13, "monthly_brier": 0.12,
         "using": "monthly", "fit_days": 400}
    ]
    (point,) = build_trend([_run(1, 0, legacy)])
    assert point.has_market is False
    assert point.skill_vs_market is None
    # `using` says monthly, so the monthly figure is what shipped.
    assert point.adopted_brier_unweighted == pytest.approx(0.12)


def test_unparseable_or_empty_detail_is_dropped_not_raised():
    runs = [
        (1, BASE, "not json"),
        (2, BASE, None),
        (3, BASE, json.dumps([])),
        (4, BASE, json.dumps({"unexpected": "shape"})),
        (5, BASE, json.dumps(_full(0.12, 0.01))),
    ]
    trend = build_trend(runs)
    assert [p.run_id for p in trend] == [5]


def test_points_come_back_in_chronological_order():
    runs = [_run(3, 2, _full(0.12, 0.01)), _run(1, 0, _full(0.12, 0.01)), _run(2, 1, _full(0.12, 0.01))]
    assert [p.run_id for p in build_trend(runs)] == [1, 2, 3]


# --- comparability ---------------------------------------------------------


def test_partial_runs_are_excluded_from_the_comparable_trend():
    # The real history contains 6-city and 12-city smoke runs; comparing those
    # against a 40-series run would read as a movement that never happened.
    runs = [
        _run(1, 0, _full(0.12, 0.01, count=6)),
        _run(2, 1, _full(0.12, 0.01, count=MIN_SERIES_FOR_TREND)),
    ]
    assert [p.run_id for p in build_trend(runs)] == [1, 2]
    assert [p.run_id for p in comparable_trend(runs)] == [2]


# --- summarize -------------------------------------------------------------


def test_improving_skill_is_reported_as_a_positive_delta():
    # skill rises (less negative) as the model closes on the market
    runs = [_run(1, 0, _full(0.20, 0.01)), _run(2, 1, _full(0.15, 0.01))]
    s = summarize(build_trend(runs))
    assert s.skill_delta > 0
    assert s.improving_streak == 1


def test_worsening_skill_breaks_the_streak():
    runs = [_run(1, 0, _full(0.15, 0.01)), _run(2, 1, _full(0.20, 0.01))]
    s = summarize(build_trend(runs))
    assert s.skill_delta < 0
    assert s.improving_streak == 0


def test_streak_counts_only_the_consecutive_recent_improvements():
    # improve, improve, regress, improve -> streak of 1, not 3
    runs = [
        _run(1, 0, _full(0.30, 0.01)),
        _run(2, 1, _full(0.25, 0.01)),
        _run(3, 2, _full(0.20, 0.01)),
        _run(4, 3, _full(0.28, 0.01)),
        _run(5, 4, _full(0.22, 0.01)),
    ]
    assert summarize(build_trend(runs)).improving_streak == 1


def test_a_long_improving_run_accumulates_the_streak():
    runs = [_run(i + 1, i, _full(0.30 - i * 0.02, 0.01)) for i in range(5)]
    assert summarize(build_trend(runs)).improving_streak == 4


def test_summary_is_empty_when_no_run_has_a_market_benchmark():
    legacy = [{"city": "NYC", "series_ticker": "T1", "flat_brier": 0.13,
               "monthly_brier": 0.12, "using": "flat", "fit_days": 400}]
    s = summarize(build_trend([_run(1, 0, legacy)]))
    assert s.latest is None
    assert s.skill_delta is None
    assert s.improving_streak == 0


def test_single_run_reports_no_week_over_week_movement():
    s = summarize(build_trend([_run(1, 0, _full(0.12, 0.01))]))
    assert s.latest is not None
    assert s.previous is None
    assert s.skill_delta is None
