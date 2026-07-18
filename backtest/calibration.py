"""Calibration diagnostics for (predicted_probability, actual_outcome) pairs:
bucketed reliability (a reliability diagram in table form) and Brier score.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CalibrationBucket:
    label: str
    low: float
    high: float
    n: int
    mean_predicted: float
    realized_frequency: float | None


def brier_score(predictions: list[float], outcomes: list[bool]) -> float:
    """Mean squared error between predicted probability and the binary outcome.
    Lower is better; a constant 50% prediction scores 0.25 against a 50/50 coin
    flip, a useful rough ceiling for "no better than a coin flip."
    """
    if not predictions:
        raise ValueError("Need at least one prediction to score.")
    return sum(
        (p - float(o)) ** 2 for p, o in zip(predictions, outcomes, strict=True)
    ) / len(predictions)


def bucket_calibration(
    predictions: list[float], outcomes: list[bool], bucket_width: float = 0.1
) -> list[CalibrationBucket]:
    """Groups predictions into probability buckets and compares each bucket's
    average prediction against how often the outcome was actually true — the
    core check for "do our 70% calls hit 70% of the time?"
    """
    n_buckets = round(1 / bucket_width)
    buckets = []
    for i in range(n_buckets):
        low = round(i * bucket_width, 4)
        high = round(low + bucket_width, 4)
        in_bucket = [
            (p, o)
            for p, o in zip(predictions, outcomes, strict=True)
            if (low <= p < high) or (high >= 1.0 and p == 1.0)
        ]
        n = len(in_bucket)
        mean_predicted = sum(p for p, _ in in_bucket) / n if n else (low + high) / 2
        realized = sum(1 for _, o in in_bucket if o) / n if n else None
        buckets.append(CalibrationBucket(f"{low:.0%}-{high:.0%}", low, high, n, mean_predicted, realized))
    return buckets
