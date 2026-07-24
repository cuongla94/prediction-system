from __future__ import annotations

import math
import statistics
from collections import defaultdict
from typing import Any, Callable

import numpy as np

from strategy_research.investigation import ResearchRow


def blend_probability(
    model_probability: float,
    market_probability: float,
    *,
    model_weight: float,
    market_weight: float,
) -> float:
    if not math.isclose(model_weight + market_weight, 1.0, abs_tol=1e-12):
        raise ValueError("model_weight and market_weight must sum to 1.0")
    return (
        model_weight * model_probability + market_weight * market_probability
    )


def _row_difference(
    row: ResearchRow, *, model_weight: float, market_weight: float
) -> float:
    candidate = blend_probability(
        row.model_probability,
        row.market_probability,
        model_weight=model_weight,
        market_weight=market_weight,
    )
    outcome = float(row.settled_winner)
    return (candidate - outcome) ** 2 - (row.market_probability - outcome) ** 2


def _cluster_differences(
    rows: list[ResearchRow],
    *,
    model_weight: float,
    market_weight: float,
) -> dict[str, float]:
    grouped: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        grouped[row.event_key].append(
            _row_difference(
                row,
                model_weight=model_weight,
                market_weight=market_weight,
            )
        )
    return {
        cluster: statistics.mean(values)
        for cluster, values in grouped.items()
    }


def clustered_brier_uncertainty(
    rows: list[ResearchRow],
    *,
    model_weight: float,
    market_weight: float,
    samples: int,
    seed: int,
) -> dict[str, Any]:
    """City/date cluster bootstrap; bracket rows are never resampled alone."""
    clusters = _cluster_differences(
        rows,
        model_weight=model_weight,
        market_weight=market_weight,
    )
    values = np.asarray(list(clusters.values()), dtype=float)
    if len(values) < 2:
        return {
            "status": "INSUFFICIENT_EVIDENCE",
            "independent_cluster_count": len(values),
            "reason": "At least two independent city/date clusters are required.",
        }
    rng = np.random.default_rng(seed)
    sampled = rng.choice(values, size=(samples, len(values)), replace=True).mean(
        axis=1
    )
    return {
        "status": "COMPLETE",
        "resampling_unit": "event_ticker (one city/date event)",
        "brackets_treated_as_independent": False,
        "independent_cluster_count": len(values),
        "bracket_outcome_count": len(rows),
        "bootstrap_samples": samples,
        "seed": seed,
        "mean_brier_difference": float(values.mean()),
        "median_brier_difference": float(np.median(values)),
        "confidence_interval_90": [
            float(np.quantile(sampled, 0.05)),
            float(np.quantile(sampled, 0.95)),
        ],
        "confidence_interval_95": [
            float(np.quantile(sampled, 0.025)),
            float(np.quantile(sampled, 0.975)),
        ],
        "probability_candidate_beats_market": float(np.mean(sampled < 0)),
        "interpretation": (
            "Negative differences favor the candidate; an interval spanning zero "
            "does not distinguish the candidate from market noise."
        ),
    }


def leave_one_group_out(
    rows: list[ResearchRow],
    *,
    group_name: str,
    group_fn: Callable[[ResearchRow], str],
    model_weight: float,
    market_weight: float,
) -> list[dict[str, Any]]:
    groups = sorted({group_fn(row) for row in rows})
    results: list[dict[str, Any]] = []
    for omitted in groups:
        retained = [row for row in rows if group_fn(row) != omitted]
        differences = _cluster_differences(
            retained,
            model_weight=model_weight,
            market_weight=market_weight,
        )
        results.append(
            {
                "analysis": f"leave_one_{group_name}_out",
                "omitted_group": omitted,
                "independent_cluster_count": len(differences),
                "mean_brier_difference": (
                    statistics.mean(differences.values())
                    if differences
                    else None
                ),
                "candidate_beats_market": (
                    statistics.mean(differences.values()) < 0
                    if differences
                    else None
                ),
            }
        )
    return results


def chronological_comparisons(
    rows: list[ResearchRow],
    *,
    model_weight: float,
    market_weight: float,
) -> list[dict[str, Any]]:
    dates = sorted({row.target_date for row in rows})
    results: list[dict[str, Any]] = []
    for target in dates[1:]:
        evaluation = [row for row in rows if row.target_date == target]
        differences = _cluster_differences(
            evaluation,
            model_weight=model_weight,
            market_weight=market_weight,
        )
        results.append(
            {
                "analysis": "chronological_fold",
                "training_through": (target.fromordinal(target.toordinal() - 1)).isoformat(),
                "validation_date": target.isoformat(),
                "independent_cluster_count": len(differences),
                "mean_brier_difference": statistics.mean(differences.values()),
                "candidate_beats_market": statistics.mean(differences.values()) < 0,
            }
        )
    return results


def stability_analyses(
    rows: list[ResearchRow],
    *,
    model_weight: float,
    market_weight: float,
) -> dict[str, Any]:
    leave_date = leave_one_group_out(
        rows,
        group_name="date",
        group_fn=lambda row: row.target_date.isoformat(),
        model_weight=model_weight,
        market_weight=market_weight,
    )
    leave_city = leave_one_group_out(
        rows,
        group_name="city",
        group_fn=lambda row: row.city,
        model_weight=model_weight,
        market_weight=market_weight,
    )
    chronological = chronological_comparisons(
        rows,
        model_weight=model_weight,
        market_weight=market_weight,
    )
    all_results = leave_date + leave_city + chronological
    comparable = [
        row["candidate_beats_market"]
        for row in all_results
        if row["candidate_beats_market"] is not None
    ]
    return {
        "leave_one_date_out": leave_date,
        "leave_one_city_out": leave_city,
        "chronological_folds": chronological,
        "all_sensitivity_results_favor_candidate": bool(comparable)
        and all(comparable),
    }


def validate_candidate_populations(result: dict[str, Any]) -> list[str]:
    violations: list[str] = []
    if result["wins"] + result["losses"] + result["voids"] != result[
        "settled_trades"
    ]:
        violations.append("execution_win_loss_void_denominator")
    directional_total = (
        result["directional_signal_wins"]
        + result["directional_signal_losses"]
        + result["directional_signal_voids"]
    )
    if directional_total != result["eligible_signals"]:
        violations.append("directional_signal_denominator")
    denominator = result["wins"] + result["losses"]
    expected = result["wins"] / denominator if denominator else None
    if result["win_rate"] != expected:
        violations.append("execution_win_rate")
    if not (
        result["model_event_count"]
        >= result["common_event_count"]
        <= result["market_event_count"]
    ):
        violations.append("brier_common_population")
    return violations
