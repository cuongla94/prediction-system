"""Calibration diagnostics for (predicted_probability, actual_outcome) pairs:
bucketed reliability (a reliability diagram in table form) and Brier score,
plus the market benchmark that says whether any of it is worth trading on.
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


@dataclass(frozen=True)
class MarketBenchmark:
    n: int
    brier_model: float
    brier_market: float
    skill_score: float  # 1 - brier_model/brier_market; > 0 means the model beat the market
    beats_market: bool


def market_benchmark(
    predictions: list[float],
    market_prices: list[float | None],
    outcomes: list[bool],
) -> MarketBenchmark | None:
    """Score the model against the market's own price on the same rows.

    This is the check whose absence let the 2026-07-20 no-edge failure ship
    (see kalshi-no-edge-root-cause memory). Everything else in this module
    answers "is the model calibrated?" — and the model *was* roughly
    calibrated in aggregate, which is exactly why it passed. But calibration
    is not edge: the market was calibrated too, and far better (Brier 0.0048
    vs the model's 0.1224 over 462 settled markets). A model that is
    well-calibrated but no better than the price it trades against loses money
    at exactly the rate of the fees, every time.

    So the only question that decides whether a signal is tradeable is whether
    it beats the market *on the same rows*, out of sample. `skill_score` is the
    standard Brier skill score against the market as reference: positive means
    real edge, zero means the model is merely reproducing the price, negative
    means trading it is actively worse than not.

    Rows without a market price are dropped (nothing to compare against);
    returns None if that leaves nothing, which is a "couldn't test" — not a
    pass.
    """
    paired = [
        (p, m, o)
        for p, m, o in zip(predictions, market_prices, outcomes, strict=True)
        if m is not None
    ]
    if not paired:
        return None

    model_preds = [p for p, _, _ in paired]
    market_preds = [m for _, m, _ in paired]
    paired_outcomes = [o for _, _, o in paired]

    brier_model = brier_score(model_preds, paired_outcomes)
    brier_market = brier_score(market_preds, paired_outcomes)
    # A market with a perfect Brier of 0 can't be improved on; treat that as
    # zero skill rather than dividing by zero.
    skill = 0.0 if brier_market == 0 else 1 - brier_model / brier_market

    return MarketBenchmark(
        n=len(paired),
        brier_model=brier_model,
        brier_market=brier_market,
        skill_score=skill,
        beats_market=brier_model < brier_market,
    )


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
