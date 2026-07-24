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


def fit_remaining_scale_fraction_by_brier(
    candidate_predictions: dict[float, list[float]],
    outcomes: list[bool],
) -> tuple[float, float]:
    """Pick whichever candidate `remaining_scale_fraction` minimizes Brier
    score against real settled outcomes — a direct calibration-based fit for
    weather.probability.observation_conditioned_bracket_probability's
    shrinkage knob, built 2026-07-20 after backtest.harness.
    fit_remaining_scale_fraction (a point-value-residual approach) only had
    enough data to fit on 7 of an 18-day same-day proof window's days, and
    those 7 turned out to share a confound (the day-ahead forecast ran hot
    enough that the observation never caught up to it, on every one of
    them) that made it structurally blind either way. This works on every
    settled bracket-row instead, tail-bracket wins included — it only needs
    the same (predicted probability, actual outcome) pairs brier_score
    already scores, not a reconstructed point value for the day's actual
    extreme.

    `candidate_predictions` is `{fraction: predictions}` — one full list of
    per-row predicted-YES probabilities per candidate fraction, all in the
    same row order as `outcomes`. Generating those predictions is the
    caller's job (it needs weather.probability.
    observation_conditioned_bracket_probability plus each row's own
    loc/scale/floor/cap/observed-so-far, none of which this module
    otherwise depends on); this function only scores and picks among
    whatever candidates it's handed.

    Returns `(best_fraction, that fraction's Brier score)`. Fit and scored
    on the same rows by construction (there's no separate held-out split
    here) — read as "does shrinkage help on this data," not a validated
    production parameter, same caveat fit_remaining_scale_fraction's
    docstring already carries.
    """
    if not candidate_predictions:
        raise ValueError("Need at least one candidate fraction to choose from.")
    scored = {fraction: brier_score(predictions, outcomes) for fraction, predictions in candidate_predictions.items()}
    best_fraction = min(scored, key=lambda f: scored[f])
    return best_fraction, scored[best_fraction]


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


@dataclass(frozen=True)
class TradeStats:
    """Trading-performance stats over a sequence of realized closed-trade
    P&Ls — distinct from everything else in this module, which scores
    (predicted probability, outcome) pairs. This scores actual dollars: the
    standard trade-stats vocabulary (profit factor, expectancy, drawdown,
    streaks) that neither `dashboard/app.py`'s `realized_pnl_total` nor this
    module's calibration functions expose today, added 2026-07-23 after a
    request to see this system's real trading performance in those terms
    rather than only Brier score.
    """

    n: int
    wins: int
    losses: int
    win_rate: float | None  # wins / (wins + losses); None if neither happened yet
    gross_win: float  # sum of positive pnls, >= 0
    gross_loss: float  # sum of negative pnls, <= 0
    net_pnl: float
    avg_win: float | None  # None if there were no wins
    avg_loss: float | None  # None if there were no losses
    profit_factor: float | None  # gross_win / abs(gross_loss); None if no losses (undefined, not infinite)
    expectancy: float | None  # net_pnl / n; the honest "average $ per trade" number
    max_drawdown: float  # largest peak-to-trough decline in cumulative P&L, >= 0
    longest_win_streak: int
    longest_loss_streak: int
    current_streak: int  # +N = on an N-trade win streak, -N = on an N-trade loss streak, 0 = no trades yet


def trade_stats(pnls: list[float]) -> TradeStats:
    """Compute `TradeStats` from realized P&L per closed trade.

    `pnls` MUST already be in chronological order (oldest trade first) —
    drawdown and streaks are sequence-dependent, unlike every other function
    in this module. Callers reading from `paper_trades` (typically fetched
    most-recent-first for display) need to reverse or re-sort by `closed_at`
    ascending before calling this.

    A trade with `realized_pnl == 0.0` exactly (a genuine push, not expected
    for this project's binary weather brackets but not assumed impossible)
    counts toward `n` and `net_pnl` but is neither a win nor a loss, and
    breaks both a win streak and a loss streak — the standard trade-stats
    convention.
    """
    if not pnls:
        return TradeStats(
            n=0, wins=0, losses=0, win_rate=None,
            gross_win=0.0, gross_loss=0.0, net_pnl=0.0,
            avg_win=None, avg_loss=None, profit_factor=None, expectancy=None,
            max_drawdown=0.0, longest_win_streak=0, longest_loss_streak=0, current_streak=0,
        )

    wins_list = [p for p in pnls if p > 0]
    losses_list = [p for p in pnls if p < 0]
    gross_win = sum(wins_list)
    gross_loss = sum(losses_list)
    net_pnl = sum(pnls)
    win_rate = len(wins_list) / (len(wins_list) + len(losses_list)) if (wins_list or losses_list) else None

    cumulative = 0.0
    peak = 0.0
    max_dd = 0.0
    for p in pnls:
        cumulative += p
        peak = max(peak, cumulative)
        max_dd = max(max_dd, peak - cumulative)

    longest_win = longest_loss = current_win = current_loss = 0
    current_streak = 0
    for p in pnls:
        if p > 0:
            current_win += 1
            current_loss = 0
            longest_win = max(longest_win, current_win)
        elif p < 0:
            current_loss += 1
            current_win = 0
            longest_loss = max(longest_loss, current_loss)
        else:  # exact push — breaks both streaks
            current_win = 0
            current_loss = 0
    current_streak = current_win if current_win else (-current_loss if current_loss else 0)

    return TradeStats(
        n=len(pnls),
        wins=len(wins_list),
        losses=len(losses_list),
        win_rate=win_rate,
        gross_win=round(gross_win, 4),
        gross_loss=round(gross_loss, 4),
        net_pnl=round(net_pnl, 4),
        avg_win=round(gross_win / len(wins_list), 4) if wins_list else None,
        avg_loss=round(gross_loss / len(losses_list), 4) if losses_list else None,
        profit_factor=round(gross_win / abs(gross_loss), 4) if gross_loss != 0 else None,
        expectancy=round(net_pnl / len(pnls), 4),
        max_drawdown=round(max_dd, 4),
        longest_win_streak=longest_win,
        longest_loss_streak=longest_loss,
        current_streak=current_streak,
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
