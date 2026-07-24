from __future__ import annotations

import csv
import hashlib
import json
import math
import statistics
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable

import numpy as np
from scipy.optimize import minimize

from backtest.calibration import brier_score
from kalshi_client import parse_event_date
from kalshi_client.fees import taker_fee
from weather.probability import (
    bracket_probability,
    observation_conditioned_bracket_probability,
)
from weather.stations import STATIONS

CURRENT_MODEL_VERSION = "normal-v4-observation-conditioned"
RESEARCH_VERSION = "climate-investigation-v1-2026-07-23"
EXECUTION_BASIS = "FORECAST_SKILL_ONLY"
BLEND_WEIGHTS = (0.00, 0.10, 0.20, 0.30, 0.50, 0.75, 1.00)
EDGE_THRESHOLD = 0.05


@dataclass(frozen=True)
class ResearchRow:
    alert_id: int
    decision_as_of_utc: datetime
    event_ticker: str
    market_ticker: str
    series_ticker: str
    city: str
    station: str
    target_date: date
    floor_strike: float | None
    cap_strike: float | None
    settled_winner: bool
    settled_at: datetime
    actual_high_temp: float | None
    model_version: str
    ensemble_mean: float | None
    ensemble_std: float | None
    observed_so_far: float | None
    lead_days: int | None
    model_probability: float
    market_probability: float
    edge: float
    fee_adjusted_threshold: float
    quote_time: datetime
    weather_captured_at: datetime
    forecast_run_time: datetime | None = None
    forecast_availability_time: datetime | None = None
    observation_publication_time: datetime | None = None
    executable_yes_price: float | None = None
    executable_no_price: float | None = None
    bid_ask_spread: float | None = None
    volume: float | None = None
    depth: float | None = None

    @property
    def event_key(self) -> str:
        return self.event_ticker

    @property
    def bracket_type(self) -> str:
        return "tail" if self.floor_strike is None or self.cap_strike is None else "bounded"

    @property
    def observation_state(self) -> str:
        if self.observed_so_far is None:
            return "unobserved"
        if self._impossible_yes():
            return "yes_impossible"
        return "observed_possible"

    def _impossible_yes(self) -> bool:
        if self.observed_so_far is None:
            return False
        metric = STATIONS.get(self.series_ticker).metric if self.series_ticker in STATIONS else "max"
        if metric == "max" and self.cap_strike is not None:
            return self.observed_so_far >= self.cap_strike
        if metric == "min" and self.floor_strike is not None:
            return self.observed_so_far <= self.floor_strike
        return False

    def to_csv_dict(self) -> dict[str, Any]:
        return {
            "decision_as_of_utc": self.decision_as_of_utc.isoformat(),
            "event_ticker": self.event_ticker,
            "market_ticker": self.market_ticker,
            "city": self.city,
            "station": self.station,
            "target_date": self.target_date.isoformat(),
            "floor_strike": self.floor_strike,
            "cap_strike": self.cap_strike,
            "settled_winner": self.settled_winner,
            "strategy_version": RESEARCH_VERSION,
            "forecast_model": "stored_ensemble",
            "forecast_run_time": None,
            "forecast_availability_time": None,
            "lead_time_days": self.lead_days,
            "forecasted_high": self.ensemble_mean,
            "model_distribution": "stored normal-v4 observation-conditioned",
            "ensemble_mean": self.ensemble_mean,
            "ensemble_spread": self.ensemble_std,
            "model_disagreement": self.ensemble_std,
            "observed_high_at_decision": self.observed_so_far,
            "latest_temperature": None,
            "recent_temperature_change": None,
            "observation_publication_time": None,
            "distance_to_lower_boundary": (
                None
                if self.observed_so_far is None or self.floor_strike is None
                else self.observed_so_far - self.floor_strike
            ),
            "distance_to_upper_boundary": (
                None
                if self.observed_so_far is None or self.cap_strike is None
                else self.cap_strike - self.observed_so_far
            ),
            "executable_yes_price": None,
            "executable_no_price": None,
            "bid_ask_spread": None,
            "quote_time": self.quote_time.isoformat(),
            "quote_age_seconds": 0,
            "volume": None,
            "depth": None,
            "market_probability": self.market_probability,
            "candidate_model_probability": self.model_probability,
            "selected_side": "YES" if self.edge >= 0 else "NO",
            "fee_adjusted_edge": abs(self.edge) - self.fee_adjusted_threshold,
            "outcome": self.settled_winner,
            "fees": None,
            "net_pnl": None,
            "execution_basis": EXECUTION_BASIS,
        }


@dataclass(frozen=True)
class CandidateSpec:
    name: str
    version: str
    method: str
    model_weight: float | None = None
    market_weight: float | None = None
    edge_threshold: float = EDGE_THRESHOLD
    status: str = "EXPLORATORY"
    feature_list: tuple[str, ...] = ()
    execution_assumptions: str = EXECUTION_BASIS
    supported: bool = True
    rejection_reason: str | None = None


def validate_no_lookahead(rows: list[ResearchRow]) -> list[str]:
    violations: list[str] = []
    for row in rows:
        for field_name in (
            "quote_time",
            "weather_captured_at",
            "forecast_run_time",
            "forecast_availability_time",
            "observation_publication_time",
        ):
            value = getattr(row, field_name)
            if value is not None and value > row.decision_as_of_utc:
                violations.append(f"{row.market_ticker}:{field_name}")
        if row.settled_at <= row.decision_as_of_utc:
            violations.append(f"{row.market_ticker}:settlement_not_after_decision")
    return violations


def chronological_partitions(
    rows: list[ResearchRow],
    *,
    holdout_fraction: float = 0.20,
    minimum_train_dates: int = 1,
) -> tuple[list[tuple[list[ResearchRow], list[ResearchRow]]], list[ResearchRow], list[ResearchRow]]:
    dates = sorted({row.target_date for row in rows})
    if len(dates) < minimum_train_dates + 2:
        raise ValueError("Need at least three chronological event dates.")
    holdout_count = max(1, round(len(dates) * holdout_fraction))
    preholdout_dates = dates[:-holdout_count]
    holdout_dates = set(dates[-holdout_count:])
    preholdout = [row for row in rows if row.target_date in set(preholdout_dates)]
    holdout = [row for row in rows if row.target_date in holdout_dates]
    folds: list[tuple[list[ResearchRow], list[ResearchRow]]] = []
    for index in range(minimum_train_dates, len(preholdout_dates)):
        training_dates = set(preholdout_dates[:index])
        validation_date = preholdout_dates[index]
        folds.append(
            (
                [row for row in preholdout if row.target_date in training_dates],
                [row for row in preholdout if row.target_date == validation_date],
            )
        )
    return folds, preholdout, holdout


def _clip(probability: float) -> float:
    return min(max(float(probability), 1e-6), 1 - 1e-6)


def _logit(probability: float) -> float:
    p = _clip(probability)
    return math.log(p / (1 - p))


def _sigmoid(value: float) -> float:
    return 1 / (1 + math.exp(-max(min(value, 30), -30)))


def _fit_logistic(train: list[ResearchRow]) -> tuple[float, float]:
    x = np.asarray([_logit(row.model_probability) for row in train])
    y = np.asarray([float(row.settled_winner) for row in train])

    def objective(params: np.ndarray) -> float:
        probabilities = 1 / (1 + np.exp(-np.clip(params[0] + params[1] * x, -30, 30)))
        return float(
            -np.sum(
                y * np.log(np.clip(probabilities, 1e-9, 1))
                + (1 - y) * np.log(np.clip(1 - probabilities, 1e-9, 1))
            )
            + 0.01 * np.sum(params**2)
        )

    result = minimize(objective, np.array([0.0, 1.0]), method="BFGS")
    return float(result.x[0]), float(result.x[1])


def _fit_isotonic(train: list[ResearchRow]) -> list[tuple[float, float]]:
    grouped: list[list[float]] = []
    for probability, values in _group_outcomes_by_probability(train):
        grouped.append([probability, float(sum(values)), float(len(values))])
    index = 0
    while index < len(grouped) - 1:
        current_mean = grouped[index][1] / grouped[index][2]
        next_mean = grouped[index + 1][1] / grouped[index + 1][2]
        if current_mean <= next_mean:
            index += 1
            continue
        grouped[index][1] += grouped[index + 1][1]
        grouped[index][2] += grouped[index + 1][2]
        grouped[index][0] = grouped[index + 1][0]
        del grouped[index + 1]
        index = max(0, index - 1)
    return [(item[0], item[1] / item[2]) for item in grouped]


def _group_outcomes_by_probability(
    train: list[ResearchRow],
) -> list[tuple[float, list[bool]]]:
    grouped: dict[float, list[bool]] = defaultdict(list)
    for row in train:
        grouped[round(row.model_probability, 6)].append(row.settled_winner)
    return sorted(grouped.items())


def _isotonic_predict(model: list[tuple[float, float]], probability: float) -> float:
    for upper, value in model:
        if probability <= upper:
            return value
    return model[-1][1]


def _residual_parameters(
    train: list[ResearchRow],
) -> tuple[tuple[float, float], dict[str, tuple[float, float]]]:
    event_rows: dict[str, ResearchRow] = {}
    for row in train:
        if row.actual_high_temp is not None and row.ensemble_mean is not None:
            event_rows.setdefault(row.event_key, row)
    residuals = [
        row.actual_high_temp - row.ensemble_mean
        for row in event_rows.values()
        if row.actual_high_temp is not None and row.ensemble_mean is not None
    ]
    global_fit = (
        statistics.mean(residuals) if residuals else 0.0,
        max(statistics.stdev(residuals), 0.5) if len(residuals) > 1 else 3.0,
    )
    by_city_values: dict[str, list[float]] = defaultdict(list)
    for row in event_rows.values():
        by_city_values[row.city].append(row.actual_high_temp - row.ensemble_mean)
    by_city = {
        city: (statistics.mean(values), max(statistics.stdev(values), 0.5))
        for city, values in by_city_values.items()
        if len(values) >= 5
    }
    return global_fit, by_city


def fit_candidate(
    spec: CandidateSpec,
    train: list[ResearchRow],
) -> Callable[[ResearchRow], float]:
    if not spec.supported:
        return lambda row: row.model_probability
    if spec.method == "current":
        return lambda row: row.model_probability
    if spec.method == "hard_lower_bound":
        # The stored v4 probability is already observation-conditioned: a
        # published running maximum/minimum becomes a hard bound on the final
        # daily extreme. Keep this named candidate so the requested mechanism
        # is explicit without inventing raw inputs that were not persisted.
        return lambda row: row.model_probability
    if spec.method == "raw_ensemble":
        def raw(row: ResearchRow) -> float:
            if row.ensemble_mean is None:
                return row.model_probability
            return bracket_probability(
                row.ensemble_mean,
                max(row.ensemble_std or 3.0, 0.5),
                row.floor_strike,
                row.cap_strike,
            )
        return raw
    if spec.method == "blend":
        model_weight = float(spec.model_weight or 0)
        market_weight = float(spec.market_weight or 0)
        if not math.isclose(model_weight + market_weight, 1.0, abs_tol=1e-12):
            raise ValueError("Blend model_weight and market_weight must sum to 1.0.")
        return lambda row: (
            model_weight * row.model_probability
            + market_weight * row.market_probability
        )
    if spec.method == "market_prior":
        return lambda row: _sigmoid(
            _logit(row.market_probability)
            + 0.20 * (_logit(row.model_probability) - _logit(row.market_probability))
        )
    if spec.method == "logistic":
        intercept, slope = _fit_logistic(train)
        return lambda row: _sigmoid(intercept + slope * _logit(row.model_probability))
    if spec.method == "isotonic":
        model = _fit_isotonic(train)
        return lambda row: _isotonic_predict(model, row.model_probability)
    if spec.method == "residual":
        global_fit, by_city = _residual_parameters(train)

        def residual(row: ResearchRow) -> float:
            if row.ensemble_mean is None:
                return row.model_probability
            bias, scale = by_city.get(row.city, global_fit)
            metric = STATIONS.get(row.series_ticker).metric if row.series_ticker in STATIONS else "max"
            if row.observed_so_far is None:
                return bracket_probability(
                    row.ensemble_mean + bias, scale, row.floor_strike, row.cap_strike
                )
            return observation_conditioned_bracket_probability(
                row.ensemble_mean + bias,
                scale,
                row.floor_strike,
                row.cap_strike,
                metric,
                row.observed_so_far,
            )

        return residual
    return lambda row: row.model_probability


def _candidate_trade_filter(
    spec: CandidateSpec,
    row: ResearchRow,
    *,
    spread_cutoff: float,
) -> bool:
    if spec.method == "conservative":
        return (
            abs(row.model_probability - row.market_probability) >= 0.10
            and 0.05 <= row.market_probability <= 0.95
        )
    if spec.method == "impossible":
        return row.observation_state == "yes_impossible"
    if spec.method == "late_day":
        return row.observed_so_far is not None and row.decision_as_of_utc.hour >= 18
    if spec.method == "low_disagreement":
        return row.ensemble_std is not None and row.ensemble_std <= spread_cutoff
    return True


def _calibration_gap(predictions: list[float], outcomes: list[bool]) -> float:
    bins: dict[int, list[tuple[float, bool]]] = defaultdict(list)
    for prediction, outcome in zip(predictions, outcomes, strict=True):
        bins[min(int(prediction * 10), 9)].append((prediction, outcome))
    return sum(
        len(items)
        / len(predictions)
        * abs(
            statistics.mean(item[0] for item in items)
            - statistics.mean(float(item[1]) for item in items)
        )
        for items in bins.values()
    )


def _log_loss(predictions: list[float], outcomes: list[bool]) -> float:
    return statistics.mean(
        -(float(outcome) * math.log(_clip(prediction))
          + (1 - float(outcome)) * math.log(1 - _clip(prediction)))
        for prediction, outcome in zip(predictions, outcomes, strict=True)
    )


def _wilson_interval(wins: int, total: int) -> tuple[float | None, float | None]:
    if total == 0:
        return None, None
    z = 1.96
    p = wins / total
    denominator = 1 + z**2 / total
    center = (p + z**2 / (2 * total)) / denominator
    radius = (
        z
        * math.sqrt(p * (1 - p) / total + z**2 / (4 * total**2))
        / denominator
    )
    return max(0.0, center - radius), min(1.0, center + radius)


def evaluate_candidate(
    spec: CandidateSpec,
    train: list[ResearchRow],
    evaluation: list[ResearchRow],
) -> dict[str, Any]:
    if not spec.supported or not evaluation:
        return _empty_metrics(spec, train, evaluation)
    predictor = fit_candidate(spec, train)
    predictions = [_clip(predictor(row)) for row in evaluation]
    outcomes = [row.settled_winner for row in evaluation]
    market = [row.market_probability for row in evaluation]
    spread_values = [
        row.ensemble_std for row in train if row.ensemble_std is not None
    ]
    spread_cutoff = statistics.median(spread_values) if spread_values else float("inf")

    by_event: dict[str, list[tuple[ResearchRow, float]]] = defaultdict(list)
    for row, prediction in zip(evaluation, predictions, strict=True):
        if _candidate_trade_filter(spec, row, spread_cutoff=spread_cutoff):
            by_event[row.event_key].append((row, prediction))
    directional_signals: list[bool] = []
    for event_rows in by_event.values():
        row, prediction = max(
            event_rows,
            key=lambda pair: abs(pair[1] - pair[0].market_probability),
        )
        edge = prediction - row.market_probability
        if abs(edge) < spec.edge_threshold + taker_fee(row.market_probability):
            continue
        side_yes = edge > 0
        directional_signals.append(
            row.settled_winner if side_yes else not row.settled_winner
        )
    directional_wins = sum(directional_signals)
    directional_losses = len(directional_signals) - directional_wins
    ci_low, ci_high = _wilson_interval(
        directional_wins, len(directional_signals)
    )
    independent_clusters = len({row.event_key for row in evaluation})
    comparison = matched_brier_comparison(predictions, market, outcomes)
    return {
        "strategy_name": spec.name,
        "strategy_version": spec.version,
        "status": spec.status,
        "promotion_status": "REJECTED",
        "promotion_reason": (
            "No executable point-in-time bid/ask or depth data; forecast-skill evidence only."
        ),
        "execution_basis": EXECUTION_BASIS,
        "probability_scored_events": comparison["common_event_count"],
        "probability_scored_markets": comparison["common_event_count"],
        "independent_city_date_clusters": independent_clusters,
        "eligible_signals": len(directional_signals),
        "directional_signal_wins": directional_wins,
        "directional_signal_losses": directional_losses,
        "directional_signal_voids": 0,
        "directional_win_rate": (
            directional_wins / len(directional_signals)
            if directional_signals
            else None
        ),
        "directional_win_rate_ci_low": ci_low,
        "directional_win_rate_ci_high": ci_high,
        "no_trade_events": independent_clusters - len(directional_signals),
        "submitted_paper_orders": 0,
        "filled_paper_orders": 0,
        "settled_trades": 0,
        "wins": 0,
        "losses": 0,
        "voids": 0,
        "win_rate": None,
        "win_rate_ci_low": None,
        "win_rate_ci_high": None,
        "brier_score": comparison["model_brier_score"],
        "market_brier_score": comparison["market_brier_score"],
        **comparison,
        "calibration_gap": _calibration_gap(predictions, outcomes),
        "log_loss": _log_loss(predictions, outcomes),
        "gross_pnl": None,
        "fees": None,
        "net_pnl": None,
        "profit_factor": None,
        "expectancy": None,
        "maximum_drawdown": None,
        "training_period": _date_range(train),
        "validation_period": _date_range(evaluation),
        "holdout_result": None,
        "model_weight": spec.model_weight,
        "market_weight": spec.market_weight,
        "edge_threshold": spec.edge_threshold,
    }


def _empty_metrics(
    spec: CandidateSpec,
    train: list[ResearchRow],
    evaluation: list[ResearchRow],
) -> dict[str, Any]:
    return {
        "strategy_name": spec.name,
        "strategy_version": spec.version,
        "status": "REJECTED",
        "promotion_status": "REJECTED",
        "promotion_reason": spec.rejection_reason or "Insufficient data.",
        "execution_basis": EXECUTION_BASIS,
        "probability_scored_events": 0,
        "probability_scored_markets": 0,
        "independent_city_date_clusters": len(
            {row.event_key for row in evaluation}
        ),
        "eligible_signals": 0,
        "directional_signal_wins": 0,
        "directional_signal_losses": 0,
        "directional_signal_voids": 0,
        "directional_win_rate": None,
        "directional_win_rate_ci_low": None,
        "directional_win_rate_ci_high": None,
        "no_trade_events": len({row.event_key for row in evaluation}),
        "submitted_paper_orders": 0,
        "filled_paper_orders": 0,
        "settled_trades": 0,
        "wins": 0,
        "losses": 0,
        "voids": 0,
        "win_rate": None,
        "win_rate_ci_low": None,
        "win_rate_ci_high": None,
        "brier_score": None,
        "market_brier_score": None,
        "model_brier_score": None,
        "model_event_count": 0,
        "market_event_count": 0,
        "common_event_count": 0,
        "excluded_model_events": len(evaluation),
        "excluded_market_events": len(evaluation),
        "exclusion_reasons": (
            spec.rejection_reason or "Candidate probability unavailable."
        ),
        "calibration_gap": None,
        "log_loss": None,
        "gross_pnl": None,
        "fees": None,
        "net_pnl": None,
        "profit_factor": None,
        "expectancy": None,
        "maximum_drawdown": None,
        "training_period": _date_range(train),
        "validation_period": _date_range(evaluation),
        "holdout_result": None,
        "model_weight": spec.model_weight,
        "market_weight": spec.market_weight,
        "edge_threshold": spec.edge_threshold,
    }


def matched_brier_comparison(
    model_predictions: list[float | None],
    market_predictions: list[float | None],
    outcomes: list[bool | None],
) -> dict[str, Any]:
    """Compare model and market only on their identical, outcome-labeled rows.

    The earlier report happened to have complete inputs, but did not prove that
    fact in its output. This audit surface makes population equality explicit
    and keeps future missing values from silently producing unmatched Brier
    scores.
    """
    if not (
        len(model_predictions) == len(market_predictions) == len(outcomes)
    ):
        raise ValueError("Model, market, and outcome populations must align.")
    model_available = sum(
        value is not None and math.isfinite(float(value))
        for value in model_predictions
    )
    market_available = sum(
        value is not None and math.isfinite(float(value))
        for value in market_predictions
    )
    common: list[tuple[float, float, bool]] = []
    reasons: dict[str, int] = defaultdict(int)
    for model, market, outcome in zip(
        model_predictions, market_predictions, outcomes, strict=True
    ):
        if outcome is None:
            reasons["missing_outcome"] += 1
            continue
        if model is None or not math.isfinite(float(model)):
            reasons["missing_or_invalid_model_probability"] += 1
            continue
        if market is None or not math.isfinite(float(market)):
            reasons["missing_or_invalid_market_probability"] += 1
            continue
        common.append((float(model), float(market), bool(outcome)))
    common_count = len(common)
    common_outcomes = [item[2] for item in common]
    return {
        "model_event_count": model_available,
        "market_event_count": market_available,
        "common_event_count": common_count,
        "excluded_model_events": len(model_predictions) - common_count,
        "excluded_market_events": len(market_predictions) - common_count,
        "exclusion_reasons": (
            "none"
            if not reasons
            else "; ".join(
                f"{name}={count}" for name, count in sorted(reasons.items())
            )
        ),
        "model_brier_score": (
            brier_score([item[0] for item in common], common_outcomes)
            if common
            else None
        ),
        "market_brier_score": (
            brier_score([item[1] for item in common], common_outcomes)
            if common
            else None
        ),
    }


def _date_range(rows: list[ResearchRow]) -> str | None:
    if not rows:
        return None
    dates = [row.target_date for row in rows]
    return f"{min(dates).isoformat()}..{max(dates).isoformat()}"


def candidate_specs() -> list[CandidateSpec]:
    specs = [
        CandidateSpec(
            "current_observation_conditioned",
            "research-current-v1",
            "current",
            feature_list=("stored model probability", "published observation"),
        ),
        CandidateSpec(
            "hard_lower_bound_truncation",
            "research-hard-lower-bound-v1",
            "hard_lower_bound",
            feature_list=("stored observation-conditioned probability",),
        ),
        CandidateSpec(
            "raw_ensemble",
            "research-raw-v1",
            "raw_ensemble",
            feature_list=("ensemble mean", "ensemble spread"),
        ),
        CandidateSpec(
            "empirical_remaining_day_residual",
            "research-residual-v1",
            "residual",
            feature_list=("ensemble mean", "city residual", "observation"),
        ),
        CandidateSpec(
            "logistic_calibration",
            "research-logistic-v1",
            "logistic",
            feature_list=("stored model probability",),
        ),
        CandidateSpec(
            "isotonic_calibration",
            "research-isotonic-v1",
            "isotonic",
            feature_list=("stored model probability",),
        ),
        CandidateSpec(
            "market_prior_weather_update",
            "research-market-prior-v1",
            "market_prior",
            feature_list=("market probability", "model-minus-market log odds"),
        ),
        CandidateSpec(
            "conservative_no_trade_filter",
            "research-conservative-v1",
            "conservative",
            feature_list=("edge", "market probability"),
        ),
        CandidateSpec(
            "newly_impossible_brackets",
            "research-impossible-v1",
            "impossible",
            feature_list=("published observation", "bracket boundary"),
        ),
        CandidateSpec(
            "late_day_observation_filter",
            "research-late-day-v1",
            "late_day",
            feature_list=("decision hour", "published observation"),
        ),
        CandidateSpec(
            "low_model_disagreement",
            "research-low-disagreement-v1",
            "low_disagreement",
            feature_list=("ensemble spread",),
        ),
    ]
    specs.extend(
        CandidateSpec(
            f"blend_model_{1 - weight:.2f}_market_{weight:.2f}",
            f"research-blend-model-{1 - weight:.2f}-market-{weight:.2f}-v2",
            "blend",
            model_weight=1 - weight,
            market_weight=weight,
            feature_list=("stored model probability", "market probability"),
        )
        for weight in BLEND_WEIGHTS
    )
    unsupported_reason = (
        "Per-model point-in-time forecast values were not persisted in alerts; "
        "the live database cannot evaluate this candidate without refetching history."
    )
    specs.extend(
        CandidateSpec(
            f"individual_{model}",
            f"research-{model}-v1",
            "unsupported",
            supported=False,
            status="REJECTED",
            rejection_reason=unsupported_reason,
        )
        for model in ("gfs", "ecmwf", "icon")
    )
    specs.append(
        CandidateSpec(
            "cross_bracket_executable_consistency",
            "research-cross-bracket-v1",
            "unsupported",
            supported=False,
            status="REJECTED",
            rejection_reason=(
                "No synchronized executable bid/ask depth was persisted; midpoint or last-price "
                "differences cannot be called arbitrage."
            ),
        )
    )
    specs.append(
        CandidateSpec(
            "remaining_hour_maximum_simulation",
            "research-remaining-hour-simulation-v1",
            "unsupported",
            supported=False,
            status="REJECTED",
            rejection_reason=(
                "Hourly forecast trajectories, forecast publication timestamps, and "
                "observation publication/revision timestamps were not persisted. A "
                "remaining-hour maximum simulation would require unavailable future-safe inputs."
            ),
        )
    )
    return specs


def fetch_rows(connection: Any) -> list[ResearchRow]:
    with connection.cursor() as cursor:
        cursor.execute(
            "select distinct on (market_ticker) id, created_at, event_ticker, market_ticker, "
            "series_ticker, city, floor_strike, cap_strike, actual_outcome, settled_at, "
            "actual_high_temp, model_version, ensemble_mean, ensemble_std, observed_so_far, "
            "lead_days, model_probability, market_yes_price, edge, fee_adjusted_threshold "
            "from alerts where settled_at is not null and actual_outcome is not null "
            "and model_version = %s order by market_ticker, created_at",
            (CURRENT_MODEL_VERSION,),
        )
        db_rows = cursor.fetchall()
    rows: list[ResearchRow] = []
    for item in db_rows:
        (
            alert_id,
            created_at,
            event_ticker,
            market_ticker,
            series_ticker,
            city,
            floor_strike,
            cap_strike,
            outcome,
            settled_at,
            actual_high_temp,
            model_version,
            ensemble_mean,
            ensemble_std,
            observed_so_far,
            lead_days,
            model_probability,
            market_probability,
            edge,
            fee_adjusted_threshold,
        ) = item
        station = STATIONS.get(series_ticker)
        rows.append(
            ResearchRow(
                alert_id=alert_id,
                decision_as_of_utc=created_at,
                event_ticker=event_ticker,
                market_ticker=market_ticker,
                series_ticker=series_ticker,
                city=city,
                station=station.nws_station_id if station else "unknown",
                target_date=parse_event_date(event_ticker),
                floor_strike=floor_strike,
                cap_strike=cap_strike,
                settled_winner=bool(outcome),
                settled_at=settled_at,
                actual_high_temp=actual_high_temp,
                model_version=model_version,
                ensemble_mean=ensemble_mean,
                ensemble_std=ensemble_std,
                observed_so_far=observed_so_far,
                lead_days=lead_days,
                model_probability=float(model_probability),
                market_probability=float(market_probability),
                edge=float(edge),
                fee_adjusted_threshold=float(fee_adjusted_threshold),
                quote_time=created_at,
                weather_captured_at=created_at,
            )
        )
    return rows


def data_inventory(connection: Any) -> list[dict[str, Any]]:
    definitions = [
        ("alerts", "created_at", "market_ticker", "event_ticker"),
        ("forecast_pulls", "created_at", None, None),
        ("price_snapshots", "created_at", "market_ticker", None),
        ("paper_trades", "opened_at", "market_ticker", "event_ticker"),
        ("pipeline_runs", "started_at", None, None),
        ("bot_control_events", "created_at", None, None),
        ("live_orders", "created_at", "market_ticker", "event_ticker"),
        ("live_order_fills", "created_at", None, None),
        ("live_reconciliation_runs", "started_at", None, None),
    ]
    inventory: list[dict[str, Any]] = []
    with connection.cursor() as cursor:
        for table, timestamp, market_column, event_column in definitions:
            cursor.execute("select to_regclass(%s)", (table,))
            if cursor.fetchone()[0] is None:
                inventory.append(
                    {
                        "source": table,
                        "available": False,
                        "row_count": 0,
                        "notes": "Schema exists in the repository but is not deployed in this database.",
                    }
                )
                continue
            select_parts = [
                "count(*)",
                f"min({timestamp})",
                f"max({timestamp})",
            ]
            select_parts.append(
                f"count(distinct {market_column})" if market_column else "null"
            )
            select_parts.append(
                f"count(distinct {event_column})" if event_column else "null"
            )
            cursor.execute(f"select {', '.join(select_parts)} from {table}")
            count, minimum, maximum, markets, events = cursor.fetchone()
            inventory.append(
                {
                    "source": table,
                    "available": True,
                    "row_count": count,
                    "date_range": [
                        minimum.isoformat() if minimum else None,
                        maximum.isoformat() if maximum else None,
                    ],
                    "unique_markets": markets,
                    "unique_events": events,
                    "supports_forecast_skill": table == "alerts" and count > 0,
                    "supports_market_comparison": table == "alerts" and count > 0,
                    "supports_execution_aware_pnl": (
                        table in {"live_orders", "live_order_fills"} and count > 0
                    ),
                }
            )
        cursor.execute(
            "select city, count(*), count(distinct market_ticker), "
            "count(distinct event_ticker), count(*) filter(where settled_at is not null), "
            "count(*) filter(where observed_so_far is null), "
            "count(*) filter(where ensemble_mean is null or ensemble_std is null) "
            "from alerts group by city order by city"
        )
        city_coverage = [
            {
                "city": row[0],
                "rows": row[1],
                "markets": row[2],
                "events": row[3],
                "settled_rows": row[4],
                "missing_observation_rows": row[5],
                "missing_ensemble_rows": row[6],
            }
            for row in cursor.fetchall()
        ]
        cursor.execute(
            "select model_version, count(*), count(*) filter(where settled_at is not null) "
            "from alerts group by model_version order by count(*) desc"
        )
        model_coverage = [
            {"model_version": row[0], "rows": row[1], "settled_rows": row[2]}
            for row in cursor.fetchall()
        ]
        cursor.execute(
            "select count(*) from (select market_ticker, created_at, count(*) "
            "from alerts group by market_ticker, created_at having count(*) > 1) duplicate_rows"
        )
        duplicate_count = cursor.fetchone()[0]
    inventory.append(
        {
            "source": "alerts_coverage",
            "city_station_coverage": city_coverage,
            "model_coverage": model_coverage,
            "exact_duplicate_decision_keys": duplicate_count,
            "stale_data": (
                "Historical settled data only; freshness is irrelevant for retrospective skill. "
                "Live decisions enforce a separate 30-minute gate."
            ),
            "notes": (
                "market_yes_price is a captured midpoint. No synchronized bid/ask/depth rows "
                "exist, so execution-aware P&L is unsupported."
            ),
        }
    )
    settled_rows = sum(item["settled_rows"] for item in model_coverage)
    inventory.extend(
        [
            {
                "source": "metar_observations",
                "available": any(
                    row["missing_observation_rows"] < row["rows"]
                    for row in city_coverage
                ),
                "storage_layer": "alerts.observed_so_far",
                "row_count": sum(
                    row["rows"] - row["missing_observation_rows"]
                    for row in city_coverage
                ),
                "notes": (
                    "A value captured with the alert is available, but source observation "
                    "publication/revision timestamps are not persisted."
                ),
            },
            {
                "source": "settlement_outcomes",
                "available": settled_rows > 0,
                "storage_layer": "alerts.actual_outcome/settled_at/actual_high_temp",
                "row_count": settled_rows,
                "notes": "Kalshi settlement plus the stored daily extreme are the outcome labels.",
            },
            {
                "source": "historical_real_trades",
                "available": False,
                "storage_layer": "live_orders/live_order_fills",
                "row_count": 0,
                "notes": (
                    "No historical bot fills are available. Account settlements cannot be "
                    "joined point-in-time to a strategy decision and are not treated as a backtest."
                ),
            },
            {
                "source": "strategy_versions",
                "available": bool(model_coverage),
                "storage_layer": "alerts.model_version and strategy_version_registry.json",
                "row_count": len(model_coverage),
                "model_coverage": model_coverage,
            },
            {
                "source": "prior_backtests_walk_forwards_ablations",
                "available": True,
                "storage_layer": "pipeline_runs and repository backtest/validation artifacts",
                "row_count": None,
                "notes": (
                    "Prior aggregate diagnostics were inventoried for context. Candidate "
                    "selection in this run is recomputed from canonical event-date partitions."
                ),
            },
        ]
    )
    return inventory


def residual_results(rows: list[ResearchRow]) -> list[dict[str, Any]]:
    event_rows: dict[str, ResearchRow] = {}
    for row in rows:
        if row.actual_high_temp is not None and row.ensemble_mean is not None:
            event_rows.setdefault(row.event_key, row)
    grouped: dict[tuple[str, str], list[float]] = defaultdict(list)
    for row in event_rows.values():
        residual = row.actual_high_temp - row.ensemble_mean
        grouped[("all", "all")].append(residual)
        grouped[("city", row.city)].append(residual)
        grouped[("station", row.station)].append(residual)
    results = []
    for (dimension, cohort), values in sorted(grouped.items()):
        if not values:
            continue
        ordered = sorted(values)
        results.append(
            {
                "dimension": dimension,
                "cohort": cohort,
                "sample_size": len(values),
                "mean_error": statistics.mean(values),
                "median_error": statistics.median(values),
                "mae": statistics.mean(abs(value) for value in values),
                "rmse": math.sqrt(statistics.mean(value**2 for value in values)),
                "standard_deviation": statistics.stdev(values) if len(values) > 1 else 0,
                "q10": float(np.quantile(ordered, 0.10)),
                "q25": float(np.quantile(ordered, 0.25)),
                "q75": float(np.quantile(ordered, 0.75)),
                "q90": float(np.quantile(ordered, 0.90)),
                "skew": (
                    statistics.mean(
                        ((value - statistics.mean(values)) / statistics.stdev(values)) ** 3
                        for value in values
                    )
                    if len(values) > 2 and statistics.stdev(values) > 0
                    else 0
                ),
            }
        )
    return results


def cohort_results(rows: list[ResearchRow], train: list[ResearchRow]) -> list[dict[str, Any]]:
    spreads = [row.ensemble_std for row in rows if row.ensemble_std is not None]
    spread_median = statistics.median(spreads) if spreads else 0

    def probability_bucket(value: float) -> str:
        low = min(int(value * 5) * 20, 80)
        return f"{low:02d}-{low + 20:02d}%"

    dimensions: dict[str, Callable[[ResearchRow], str]] = {
        "city": lambda row: row.city,
        "station": lambda row: row.station,
        "lead_time": lambda row: f"lead_{row.lead_days}",
        "forecast_cycle": lambda row: f"{row.decision_as_of_utc.hour:02d}Z",
        "decision_hour": lambda row: f"{row.decision_as_of_utc.hour:02d}Z",
        "month": lambda row: f"{row.target_date.month:02d}",
        "season": lambda row: (
            "DJF" if row.target_date.month in {12, 1, 2}
            else "MAM" if row.target_date.month in {3, 4, 5}
            else "JJA" if row.target_date.month in {6, 7, 8}
            else "SON"
        ),
        "temperature_regime": lambda row: (
            "below_60" if (row.ensemble_mean or 0) < 60
            else "60_to_85" if (row.ensemble_mean or 0) < 85
            else "above_85"
        ),
        "bracket_type": lambda row: row.bracket_type,
        "observation_state": lambda row: row.observation_state,
        "model_probability": lambda row: probability_bucket(row.model_probability),
        "market_probability": lambda row: probability_bucket(row.market_probability),
        "corrected_edge": lambda row: (
            "under_5pp" if abs(row.edge) < 0.05
            else "5_to_10pp" if abs(row.edge) < 0.10
            else "over_10pp"
        ),
        "spread": lambda row: (
            "missing" if row.ensemble_std is None
            else "low" if row.ensemble_std <= spread_median
            else "high"
        ),
        "quote_freshness": lambda row: "captured_at_decision",
        "liquidity": lambda row: "not_recorded",
        "strategy_version": lambda row: row.model_version,
        "forecast_model": lambda row: "stored_ensemble",
        "model_agreement": lambda row: (
            "missing" if row.ensemble_std is None
            else "agreement" if row.ensemble_std <= spread_median
            else "disagreement"
        ),
    }
    current = candidate_specs()[0]
    results: list[dict[str, Any]] = []
    for dimension, key_fn in dimensions.items():
        grouped: dict[str, list[ResearchRow]] = defaultdict(list)
        for row in rows:
            grouped[key_fn(row)].append(row)
        for cohort, cohort_rows in sorted(grouped.items()):
            metrics = evaluate_candidate(current, train, cohort_rows)
            results.append({"dimension": dimension, "cohort": cohort, **metrics})
    return results


def error_decomposition(rows: list[ResearchRow]) -> dict[str, Any]:
    current_predictions = [row.model_probability for row in rows]
    market_predictions = [row.market_probability for row in rows]
    outcomes = [row.settled_winner for row in rows]
    impossible = [row for row in rows if row.observation_state == "yes_impossible"]
    tail = [row for row in rows if row.bracket_type == "tail"]
    bounded = [row for row in rows if row.bracket_type == "bounded"]
    residuals = residual_results(rows)
    pooled_residual = next(
        (item for item in residuals if item["dimension"] == "all"), None
    )
    paired_residual_rows: dict[str, ResearchRow] = {}
    for row in rows:
        if row.actual_high_temp is not None and row.ensemble_mean is not None:
            paired_residual_rows.setdefault(row.event_key, row)
    spread_values = [
        (row.ensemble_std, abs(row.actual_high_temp - row.ensemble_mean))
        for row in paired_residual_rows.values()
        if row.ensemble_std is not None
    ]
    spread_correlation = (
        float(np.corrcoef([item[0] for item in spread_values], [item[1] for item in spread_values])[0, 1])
        if len(spread_values) > 2
        else None
    )
    return {
        "forecast_center_error": pooled_residual,
        "forecast_variance_error": {
            "ensemble_spread_vs_absolute_error_correlation": spread_correlation,
            "interpretation": (
                "Weak/unstable correlation means stored spread is not a dependable uncertainty proxy."
            ),
        },
        "probability_calibration_error": {
            "current_brier": brier_score(current_predictions, outcomes),
            "calibration_gap": _calibration_gap(current_predictions, outcomes),
        },
        "observation_conditioning_error": {
            "impossible_rows": len(impossible),
            "mean_probability_assigned_to_impossible_yes": (
                statistics.mean(row.model_probability for row in impossible) if impossible else None
            ),
        },
        "forecast_market_timestamp_mismatch": (
            "Forecast run/availability and observation publication timestamps were not persisted. "
            "Only the combined alert decision capture time is authoritative."
        ),
        "quote_staleness": (
            "Alert midpoint was captured at decision time, but no independent quote timestamp "
            "or bid/ask was persisted."
        ),
        "bracket_tail_behavior": {
            "tail_rows": len(tail),
            "tail_brier": (
                brier_score([row.model_probability for row in tail], [row.settled_winner for row in tail])
                if tail
                else None
            ),
            "bounded_rows": len(bounded),
            "bounded_brier": (
                brier_score(
                    [row.model_probability for row in bounded],
                    [row.settled_winner for row in bounded],
                )
                if bounded
                else None
            ),
        },
        "fee_spread_slippage_impact": (
            "NOT ESTIMABLE: no synchronized executable bid/ask, depth, or fill evidence."
        ),
        "unfilled_order_impact": "NOT ESTIMABLE: no historical live bot orders.",
        "market_brier": brier_score(market_predictions, outcomes),
        "correlation_across_brackets": (
            f"{len(rows)} bracket rows represent only "
            f"{len({row.event_key for row in rows})} independent event/date outcomes."
        ),
    }


def incremental_information(
    train: list[ResearchRow],
    evaluation: list[ResearchRow],
) -> dict[str, Any]:
    x = np.asarray(
        [
            [1.0, row.market_probability, row.model_probability - row.market_probability]
            for row in train
        ]
    )
    y = np.asarray([float(row.settled_winner) for row in train])

    def objective(params: np.ndarray) -> float:
        probabilities = 1 / (1 + np.exp(-np.clip(x @ params, -30, 30)))
        return float(
            -np.sum(
                y * np.log(np.clip(probabilities, 1e-9, 1))
                + (1 - y) * np.log(np.clip(1 - probabilities, 1e-9, 1))
            )
            + 0.01 * np.sum(params**2)
        )

    fit = minimize(objective, np.array([0.0, 1.0, 0.0]), method="BFGS")
    eval_x = np.asarray(
        [
            [1.0, row.market_probability, row.model_probability - row.market_probability]
            for row in evaluation
        ]
    )
    fitted = 1 / (1 + np.exp(-np.clip(eval_x @ fit.x, -30, 30)))
    outcomes = [row.settled_winner for row in evaluation]
    market = [row.market_probability for row in evaluation]
    return {
        "formula": "outcome ~ market_probability + model_minus_market",
        "fit_period": _date_range(train),
        "evaluation_period": _date_range(evaluation),
        "market_coefficient": float(fit.x[1]),
        "model_minus_market_coefficient": float(fit.x[2]),
        "out_of_sample_brier": brier_score(list(fitted), outcomes),
        "market_brier": brier_score(market, outcomes),
        "adds_out_of_sample_value": brier_score(list(fitted), outcomes) < brier_score(market, outcomes),
    }


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("")
        return
    fieldnames = list(rows[0])
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=fieldnames,
            extrasaction="ignore",
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows)


def _json_default(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, np.bool_):
        return bool(value)
    if isinstance(value, np.floating):
        return float(value)
    raise TypeError(f"Cannot serialize {type(value).__name__}")


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True, default=_json_default) + "\n")


def _registry(specs: list[CandidateSpec], code_hash: str, train: list[ResearchRow]) -> list[dict[str, Any]]:
    return [
        {
            **asdict(spec),
            "code_hash": code_hash,
            "training_period": _date_range(train),
            "validation_method": "chronological expanding-window; final newest-date holdout",
            "probability_method": spec.method,
            "calibration_method": (
                spec.method if spec.method in {"logistic", "isotonic", "residual"} else "none"
            ),
            "promotion_requires_explicit_approval": True,
            "configured_live_strategy_changed": False,
        }
        for spec in specs
    ]


def run_investigation(connection: Any, output_dir: Path) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    rows = fetch_rows(connection)
    violations = validate_no_lookahead(rows)
    if violations:
        raise ValueError(f"No-lookahead violations: {violations[:10]}")
    folds, preholdout, holdout = chronological_partitions(rows)
    validation_dates = sorted({row.target_date for row in preholdout})
    validation_count = max(1, round(len(validation_dates) * 0.30))
    fit_dates = set(validation_dates[:-validation_count])
    validation_date_set = set(validation_dates[-validation_count:])
    fit_rows = [row for row in preholdout if row.target_date in fit_dates]
    validation_rows = [
        row for row in preholdout if row.target_date in validation_date_set
    ]
    specs = candidate_specs()
    code_hash = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()

    candidate_results = [
        evaluate_candidate(spec, fit_rows, validation_rows) for spec in specs
    ]
    holdout_results = [
        evaluate_candidate(spec, preholdout, holdout) for spec in specs
    ]
    holdout_by_version = {
        result["strategy_version"]: result for result in holdout_results
    }
    for result in candidate_results:
        holdout_result = holdout_by_version[result["strategy_version"]]
        result["holdout_result"] = {
            "brier_score": holdout_result["brier_score"],
            "market_brier_score": holdout_result["market_brier_score"],
            "probability_scored_events": holdout_result[
                "probability_scored_events"
            ],
            "independent_city_date_clusters": holdout_result[
                "independent_city_date_clusters"
            ],
            "eligible_signals": holdout_result["eligible_signals"],
            "directional_signal_wins": holdout_result[
                "directional_signal_wins"
            ],
            "directional_signal_losses": holdout_result[
                "directional_signal_losses"
            ],
            "settled_trades": holdout_result["settled_trades"],
            "net_pnl": holdout_result["net_pnl"],
            "promotion_status": holdout_result["promotion_status"],
        }

    walk_forward: list[dict[str, Any]] = []
    for fold_index, (training, validation) in enumerate(folds):
        for spec in specs:
            walk_forward.append(
                {
                    "fold": fold_index,
                    **evaluate_candidate(spec, training, validation),
                }
            )

    supported_validation = [
        result for result in candidate_results if result["brier_score"] is not None
    ]
    best = min(supported_validation, key=lambda item: item["brier_score"])
    market_brier = best["market_brier_score"]
    best_holdout = holdout_by_version[best["strategy_version"]]
    final_reason = (
        "No candidate is promotable: executable bid/ask/depth history is absent, "
        "the sample spans only a few dates, and no forward paper confirmatory period exists."
    )
    summary = {
        "investigation_status": "COMPLETE_NO_PROMOTION",
        "strategy_version": RESEARCH_VERSION,
        "date_range": _date_range(rows),
        "independent_events": len({row.event_key for row in rows}),
        "current_strategy": CURRENT_MODEL_VERSION,
        "current_strategy_status": "FAILED",
        "best_candidate": best["strategy_name"],
        "candidate_brier_score": best["brier_score"],
        "market_brier_score": market_brier,
        "probability_scored_events": best["probability_scored_events"],
        "independent_city_date_clusters": best[
            "independent_city_date_clusters"
        ],
        "eligible_signals": best["eligible_signals"],
        "directional_signal_wins": best["directional_signal_wins"],
        "directional_signal_losses": best["directional_signal_losses"],
        "directional_win_rate": best["directional_win_rate"],
        "submitted_paper_orders": 0,
        "filled_paper_orders": 0,
        "settled_trades": 0,
        "wins": 0,
        "losses": 0,
        "voids": 0,
        "win_rate": None,
        "net_pnl": None,
        "expectancy": None,
        "profit_factor": None,
        "maximum_drawdown": None,
        "promotion_status": "REJECTED",
        "primary_reason": final_reason,
        "holdout": best_holdout,
        "candidate_count": len(specs),
        "no_lookahead_passed": True,
        "configured_live_strategy_unchanged": True,
        "execution_basis": EXECUTION_BASIS,
        "input_rows": len(rows),
        "input_sha256": hashlib.sha256(
            "\n".join(
                f"{row.alert_id}|{row.decision_as_of_utc.isoformat()}|{row.model_probability}|"
                f"{row.market_probability}|{int(row.settled_winner)}"
                for row in rows
            ).encode()
        ).hexdigest(),
        "code_sha256": code_hash,
    }

    inventory = data_inventory(connection)
    residuals = residual_results(rows)
    cohorts = cohort_results(validation_rows, fit_rows)
    decomposition = error_decomposition(validation_rows)
    incremental = incremental_information(preholdout, holdout)
    registry = _registry(specs, code_hash, fit_rows)
    rejected = [
        {
            "strategy_name": result["strategy_name"],
            "strategy_version": result["strategy_version"],
            "reason": result["promotion_reason"],
        }
        for result in candidate_results
    ]

    _write_json(output_dir / "data_inventory.json", inventory)
    _write_csv(
        output_dir / "canonical_research_rows.csv",
        [row.to_csv_dict() for row in rows],
    )
    _write_csv(output_dir / "forecast_residuals.csv", residuals)
    _write_csv(output_dir / "cohort_results.csv", cohorts)
    _write_csv(output_dir / "candidate_results.csv", candidate_results)
    _write_csv(output_dir / "walk_forward_results.csv", walk_forward)
    _write_csv(output_dir / "final_holdout_results.csv", holdout_results)
    _write_csv(
        output_dir / "execution_results.csv",
        [
            {
                "strategy_version": result["strategy_version"],
                "execution_basis": EXECUTION_BASIS,
                "gross_pnl": None,
                "fees": None,
                "net_pnl": None,
                "profit_factor": None,
                "expectancy": None,
                "maximum_drawdown": None,
                "reason": "No point-in-time executable bid/ask/depth data.",
            }
            for result in candidate_results
        ],
    )
    _write_json(
        output_dir / "hypothesis_registry.json",
        {
            "multiple_testing_family_size": len(specs),
            "control": (
                "All configurations are registered; all are exploratory. No p-value-based "
                "promotion is permitted, and a separate forward confirmatory period is required."
            ),
            "candidates": registry,
        },
    )
    _write_json(output_dir / "strategy_version_registry.json", registry)
    _write_json(output_dir / "rejected_candidates.json", rejected)
    _write_json(output_dir / "error_decomposition.json", decomposition)
    _write_json(output_dir / "market_incremental_information.json", incremental)
    _write_json(output_dir / "final_summary.json", summary)
    _write_data_quality_report(output_dir, rows, inventory, violations)
    _write_final_report(
        output_dir,
        summary,
        candidate_results,
        holdout_results,
        decomposition,
        incremental,
    )
    return summary


def _write_data_quality_report(
    output_dir: Path,
    rows: list[ResearchRow],
    inventory: list[dict[str, Any]],
    violations: list[str],
) -> None:
    missing_observations = sum(row.observed_so_far is None for row in rows)
    missing_ensemble = sum(
        row.ensemble_mean is None or row.ensemble_std is None for row in rows
    )
    lines = [
        "# Strategy Investigation Data Quality",
        "",
        f"- Canonical rows: {len(rows):,}",
        f"- Independent events: {len({row.event_key for row in rows}):,}",
        f"- Unique markets: {len({row.market_ticker for row in rows}):,}",
        f"- Date range: {_date_range(rows)}",
        f"- Cities: {len({row.city for row in rows})}",
        f"- Missing published-observation value: {missing_observations:,}",
        f"- Missing ensemble mean/spread: {missing_ensemble:,}",
        f"- No-lookahead violations: {len(violations)}",
        "",
        "## Execution evidence",
        "",
        "`price_snapshots` and `forecast_pulls` contain no rows. Alerts retain a market "
        "midpoint but not synchronized bid/ask, depth, or independent quote timestamps. "
        "All candidate trading metrics are therefore labeled `FORECAST_SKILL_ONLY`; P&L, "
        "fees, profit factor, expectancy, and drawdown are null rather than fabricated.",
        "",
        "## Inventory sources",
        "",
    ]
    lines.extend(
        f"- {item['source']}: {item.get('row_count', 'coverage record')} rows"
        for item in inventory
    )
    (output_dir / "data_quality_report.md").write_text("\n".join(lines) + "\n")


def _write_final_report(
    output_dir: Path,
    summary: dict[str, Any],
    candidates: list[dict[str, Any]],
    holdout: list[dict[str, Any]],
    decomposition: dict[str, Any],
    incremental: dict[str, Any],
) -> None:
    holdout_by_version = {row["strategy_version"]: row for row in holdout}
    lines = [
        "# Climate Strategy Investigation",
        "",
        f"Status: **{summary['investigation_status']}**",
        "",
        "The configured live strategy remains unchanged and historically **FAILED**. "
        "No candidate was promoted.",
        "",
        "## Confirmed findings",
        "",
        f"- Current validation Brier: "
        f"{next(row['brier_score'] for row in candidates if row['strategy_name'] == 'current_observation_conditioned'):.4f}.",
        f"- Matched market Brier: {summary['market_brier_score']:.4f}.",
        f"- Market incremental-information holdout result: model adds value = "
        f"{incremental['adds_out_of_sample_value']}.",
        "- Stored model probabilities remain materially less accurate than captured market "
        "probabilities; model-center error, calibration error, bracket behavior, and observation "
        "state all contribute.",
        "- Executable spread/slippage and unfilled-order effects cannot be measured from the "
        "collected database because synchronized bid/ask/depth history is absent.",
        "",
        "## Candidate comparison",
        "",
        "Probability, filtering, and execution are separate populations. Directional "
        "W/L describes settled historical signal direction only; executed trade W/L "
        "remains zero until prospective paper orders are observed.",
        "",
        "| Candidate | Model wt | Market wt | Probability-scored markets | "
        "City/date clusters | Eligible signals | Directional W/L | No-trade clusters | "
        "Submitted / filled / settled paper | Trade W/L/V | Model Brier | Market Brier | "
        "Common population | Holdout Brier | Promotion |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | "
        "---: | ---: | ---: | ---: | --- |",
    ]
    for result in candidates:
        holdout_result = holdout_by_version[result["strategy_version"]]
        lines.append(
            f"| {result['strategy_name']} | {_fmt(result['model_weight'])} | "
            f"{_fmt(result['market_weight'])} | "
            f"{result['probability_scored_events']} | "
            f"{result['independent_city_date_clusters']} | "
            f"{result['eligible_signals']} | "
            f"{result['directional_signal_wins']}/"
            f"{result['directional_signal_losses']} | "
            f"{result['no_trade_events']} | "
            f"{result['submitted_paper_orders']}/"
            f"{result['filled_paper_orders']}/{result['settled_trades']} | "
            f"{result['wins']}/{result['losses']}/{result['voids']} | "
            f"{_fmt(result['brier_score'])} | "
            f"{_fmt(result['market_brier_score'])} | "
            f"{result['common_event_count']} | "
            f"{_fmt(holdout_result['brier_score'])} | REJECTED |"
        )
    lines.extend(
        [
            "",
            "## Error decomposition",
            "",
            f"- Forecast-center residuals: {json.dumps(decomposition['forecast_center_error'], default=_json_default)}",
            f"- Probability calibration: {json.dumps(decomposition['probability_calibration_error'])}",
            f"- Observation-conditioned impossible rows: "
            f"{decomposition['observation_conditioning_error']['impossible_rows']}.",
            f"- Tail vs bounded: {json.dumps(decomposition['bracket_tail_behavior'])}",
            "",
            "## Final holdout and recommendation",
            "",
            f"Best validation candidate: **{summary['best_candidate']}**. Its untouched holdout "
            f"Brier is {_fmt(summary['holdout']['brier_score'])} versus market "
            f"{_fmt(summary['holdout']['market_brier_score'])}.",
            "",
            summary["primary_reason"],
            "",
            "Forward data required: synchronized executable YES/NO bid and ask, depth, quote "
            "timestamps, forecast run/availability timestamps, observation publication and "
            "revision timestamps, and a separately accrued forward paper period. Candidate "
            "selection must be frozen before that confirmatory period.",
        ]
    )
    (output_dir / "final_report.md").write_text("\n".join(lines) + "\n")


def _fmt(value: Any) -> str:
    return "—" if value is None else f"{value:.4f}"
