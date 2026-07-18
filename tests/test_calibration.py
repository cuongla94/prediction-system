from __future__ import annotations

import pytest

from backtest.calibration import brier_score, bucket_calibration


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
