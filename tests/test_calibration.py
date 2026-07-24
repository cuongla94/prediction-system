from __future__ import annotations

import pytest

from backtest.calibration import brier_score, bucket_calibration, fit_remaining_scale_fraction_by_brier, market_benchmark, trade_stats


def test_brier_score_perfect_predictions_score_zero():
    assert brier_score([1.0, 0.0, 1.0], [True, False, True]) == pytest.approx(0.0)


def test_brier_score_worst_predictions_score_one():
    assert brier_score([0.0, 1.0], [True, False]) == pytest.approx(1.0)


def test_brier_score_constant_half_scores_quarter():
    assert brier_score([0.5, 0.5, 0.5, 0.5], [True, False, True, False]) == pytest.approx(0.25)


def test_brier_score_requires_predictions():
    with pytest.raises(ValueError):
        brier_score([], [])


def test_bucket_calibration_groups_by_predicted_probability():
    predictions = [0.15, 0.18, 0.75, 0.78, 0.72]
    outcomes = [False, True, True, True, False]

    buckets = bucket_calibration(predictions, outcomes, bucket_width=0.1)

    low_bucket = next(b for b in buckets if b.label == "10%-20%")
    assert low_bucket.n == 2
    assert low_bucket.realized_frequency == pytest.approx(0.5)

    high_bucket = next(b for b in buckets if b.label == "70%-80%")
    assert high_bucket.n == 3
    assert high_bucket.realized_frequency == pytest.approx(2 / 3)


def test_bucket_calibration_empty_bucket_has_no_realized_frequency():
    buckets = bucket_calibration([0.05], [True], bucket_width=0.1)
    empty_bucket = next(b for b in buckets if b.label == "50%-60%")
    assert empty_bucket.n == 0
    assert empty_bucket.realized_frequency is None


def test_bucket_calibration_includes_probability_one_in_top_bucket():
    buckets = bucket_calibration([1.0], [True], bucket_width=0.1)
    top_bucket = next(b for b in buckets if b.label == "90%-100%")
    assert top_bucket.n == 1


def test_bucket_calibration_covers_full_range():
    buckets = bucket_calibration([0.05, 0.95], [True, False], bucket_width=0.1)
    assert len(buckets) == 10
    assert buckets[0].low == pytest.approx(0.0)
    assert buckets[-1].high == pytest.approx(1.0)


# --- market benchmark (the tradeability gate added 2026-07-20) ---


def test_market_benchmark_detects_a_model_that_only_reproduces_the_price():
    # The real failure shape: the model is *calibrated* but carries no
    # information the price doesn't already have. Brier alone calls this fine;
    # only the comparison against the market catches it.
    predictions = [0.2, 0.8, 0.5, 0.1]
    prices = [0.2, 0.8, 0.5, 0.1]
    outcomes = [False, True, True, False]
    bench = market_benchmark(predictions, prices, outcomes)
    assert bench is not None
    assert bench.beats_market is False
    assert bench.skill_score == pytest.approx(0.0)


def test_market_benchmark_rewards_a_model_that_actually_beats_the_price():
    predictions = [0.05, 0.95, 0.05, 0.95]
    prices = [0.40, 0.60, 0.40, 0.60]
    outcomes = [False, True, False, True]
    bench = market_benchmark(predictions, prices, outcomes)
    assert bench.beats_market is True
    assert bench.skill_score > 0


def test_market_benchmark_flags_a_model_worse_than_the_price():
    # Negative skill — trading this is worse than not trading at all, which is
    # exactly what the live data showed (model 0.1224 vs market 0.0048).
    predictions = [0.90, 0.10, 0.90]
    prices = [0.10, 0.90, 0.10]
    outcomes = [False, True, False]
    bench = market_benchmark(predictions, prices, outcomes)
    assert bench.beats_market is False
    assert bench.skill_score < 0


def test_market_benchmark_ignores_rows_without_a_price():
    # Nothing to compare against on those rows; they must be dropped, not
    # scored as if the market had said zero.
    bench = market_benchmark([0.2, 0.8], [None, 0.8], [False, True])
    assert bench.n == 1


def test_market_benchmark_returns_none_when_nothing_is_comparable():
    # "Couldn't test" must be distinguishable from "passed" — the caller
    # treats None as a non-pass.
    assert market_benchmark([0.2, 0.8], [None, None], [False, True]) is None


def test_market_benchmark_handles_a_perfect_market_without_dividing_by_zero():
    bench = market_benchmark([0.5, 0.5], [0.0, 1.0], [False, True])
    assert bench.brier_market == pytest.approx(0.0)
    assert bench.skill_score == 0.0
    assert bench.beats_market is False


# --- fit_remaining_scale_fraction_by_brier ------------------------------


def test_fit_remaining_scale_fraction_by_brier_picks_the_lowest_brier_candidate():
    outcomes = [True, False, True, False]
    candidates = {
        1.0: [0.9, 0.9, 0.9, 0.9],  # bad: confidently wrong on rows 2 and 4
        0.5: [0.7, 0.3, 0.7, 0.3],  # tracks every outcome's direction, lowest Brier
        0.1: [0.6, 0.6, 0.6, 0.6],  # mediocre
    }
    best_fraction, best_brier = fit_remaining_scale_fraction_by_brier(candidates, outcomes)
    assert best_fraction == 0.5
    assert best_brier == pytest.approx(brier_score(candidates[0.5], outcomes))


def test_fit_remaining_scale_fraction_by_brier_requires_at_least_one_candidate():
    with pytest.raises(ValueError):
        fit_remaining_scale_fraction_by_brier({}, [True, False])


# --- trade_stats ---------------------------------------------------------


def test_trade_stats_on_no_trades_returns_all_nones_not_a_crash():
    s = trade_stats([])
    assert s.n == 0
    assert s.win_rate is None
    assert s.profit_factor is None
    assert s.expectancy is None
    assert s.max_drawdown == 0.0
    assert s.current_streak == 0


def test_trade_stats_basic_counts_and_totals():
    # 3 wins (+10, +5, +2), 2 losses (-4, -1) -- hand-computed.
    s = trade_stats([10.0, -4.0, 5.0, -1.0, 2.0])
    assert s.n == 5
    assert s.wins == 3
    assert s.losses == 2
    assert s.win_rate == pytest.approx(3 / 5)
    assert s.gross_win == pytest.approx(17.0)
    assert s.gross_loss == pytest.approx(-5.0)
    assert s.net_pnl == pytest.approx(12.0)
    assert s.avg_win == pytest.approx(17.0 / 3, abs=1e-4)
    assert s.avg_loss == pytest.approx(-5.0 / 2)
    assert s.profit_factor == pytest.approx(17.0 / 5.0)
    assert s.expectancy == pytest.approx(12.0 / 5)


def test_trade_stats_profit_factor_is_none_when_there_are_no_losses():
    # Undefined (division by zero), not reported as infinity.
    s = trade_stats([5.0, 3.0])
    assert s.profit_factor is None
    assert s.avg_loss is None


def test_trade_stats_max_drawdown_is_the_worst_peak_to_trough_decline():
    # Cumulative path: 10, 15, 5, 8, 20 -- peak hits 15 before dropping to 5
    # (a 10 drawdown), then a new peak of 20 with nothing after it to test.
    s = trade_stats([10.0, 5.0, -10.0, 3.0, 12.0])
    assert s.max_drawdown == pytest.approx(10.0)


def test_trade_stats_max_drawdown_is_zero_when_cumulative_pnl_never_falls():
    s = trade_stats([1.0, 2.0, 3.0])
    assert s.max_drawdown == 0.0


def test_trade_stats_streaks_track_the_longest_run_of_each_sign():
    # W W L L L W -- longest win streak 2, longest loss streak 3.
    s = trade_stats([1.0, 1.0, -1.0, -1.0, -1.0, 1.0])
    assert s.longest_win_streak == 2
    assert s.longest_loss_streak == 3


def test_trade_stats_current_streak_is_signed_by_the_most_recent_run():
    # Ends on 2 straight losses.
    s = trade_stats([1.0, 1.0, -1.0, -1.0])
    assert s.current_streak == -2

    # Ends on 3 straight wins.
    s = trade_stats([-1.0, 1.0, 1.0, 1.0])
    assert s.current_streak == 3


def test_trade_stats_a_push_breaks_streaks_without_counting_as_win_or_loss():
    s = trade_stats([1.0, 1.0, 0.0, 1.0])
    assert s.wins == 3
    assert s.losses == 0
    assert s.longest_win_streak == 2  # the push at index 2 resets the streak
    assert s.current_streak == 1
    assert s.n == 4  # the push still counts toward n and net_pnl
    assert s.net_pnl == pytest.approx(3.0)
