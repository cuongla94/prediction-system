from __future__ import annotations

from typing import Any

from .config import ReadinessConfig

STATUSES = {"PASS", "FAIL", "INSUFFICIENT_EVIDENCE", "NOT_AVAILABLE"}


def _gate(name: str, checks: list[dict[str, Any]]) -> dict[str, Any]:
    for check in checks:
        if check["status"] not in STATUSES:
            raise ValueError(f"Invalid readiness status for {check['name']}")
    statuses = {check["status"] for check in checks}
    if "FAIL" in statuses:
        status = "FAIL"
    elif "NOT_AVAILABLE" in statuses:
        status = "NOT_AVAILABLE"
    elif "INSUFFICIENT_EVIDENCE" in statuses:
        status = "INSUFFICIENT_EVIDENCE"
    else:
        status = "PASS"
    return {"gate": name, "status": status, "checks": checks}


def build_readiness_report(
    *,
    no_lookahead_passed: bool,
    populations_consistent: bool,
    bracket_mapping_verified: bool,
    candidate_beats_current_model: bool,
    calibration_gap: float | None,
    uncertainty: dict[str, Any],
    stability: dict[str, Any],
    incremental_model_adds_value: bool,
    chronological_holdout_improves_market: bool,
    forward_status: dict[str, Any],
    operational_checks: dict[str, bool],
    config: ReadinessConfig,
) -> dict[str, Any]:
    confidence_interval = uncertainty.get("confidence_interval_95")
    confidence_distinguishes = bool(
        confidence_interval
        and confidence_interval[0] < 0
        and confidence_interval[1] < 0
    )
    schema_deployed = bool(forward_status.get("schema_deployed"))
    decisions = int(forward_status.get("decision_count", 0))
    data_not_started = not schema_deployed or decisions == 0

    def prospective_status(complete: bool, *, unavailable: bool = False) -> str:
        if unavailable or data_not_started:
            return "NOT_AVAILABLE"
        return "PASS" if complete else "INSUFFICIENT_EVIDENCE"

    def forward_threshold_status(complete: bool) -> str:
        if data_not_started:
            return "NOT_AVAILABLE"
        return "PASS" if complete else "INSUFFICIENT_EVIDENCE"

    gates = [
        _gate(
            "DATA_INTEGRITY",
            [
                {
                    "name": "point_in_time_forecast_availability",
                    "status": prospective_status(
                        bool(
                            forward_status.get(
                                "forecast_availability_complete"
                            )
                        )
                    ),
                    "reason": (
                        "Historical source timestamps are absent; this gate uses "
                        "prospective collector receipt/availability evidence."
                    ),
                },
                {
                    "name": "point_in_time_observation_availability",
                    "status": prospective_status(
                        bool(
                            forward_status.get(
                                "observation_availability_complete"
                            )
                        )
                    ),
                    "reason": (
                        "Same-day decisions require an observation event and "
                        "collector-receipt timestamp; future-day observations are N/A."
                    ),
                },
                {
                    "name": "point_in_time_market_quote_availability",
                    "status": prospective_status(
                        forward_status.get("valid_orderbook_snapshots", 0) > 0
                    ),
                    "reason": (
                        "Historical synchronized executable orderbooks were not stored; "
                        "prospective REST/stream snapshots are required."
                    ),
                },
                {
                    "name": "no_settlement_leakage",
                    "status": "PASS" if no_lookahead_passed else "FAIL",
                },
                {
                    "name": "correct_bracket_mapping",
                    "status": "PASS" if bracket_mapping_verified else "FAIL",
                },
                {
                    "name": "consistent_candidate_populations",
                    "status": "PASS" if populations_consistent else "FAIL",
                },
            ],
        ),
        _gate(
            "FORECAST_SKILL",
            [
                {
                    "name": "improves_current_weather_model_out_of_sample",
                    "status": "PASS" if candidate_beats_current_model else "FAIL",
                },
                {
                    "name": "acceptable_calibration",
                    "status": (
                        "PASS"
                        if calibration_gap is not None
                        and calibration_gap <= config.maximum_calibration_gap
                        else "FAIL"
                        if calibration_gap is not None
                        else "NOT_AVAILABLE"
                    ),
                    "value": calibration_gap,
                    "maximum": config.maximum_calibration_gap,
                },
                {
                    "name": "no_severe_city_date_instability",
                    "status": (
                        "PASS"
                        if stability.get(
                            "all_sensitivity_results_favor_candidate"
                        )
                        else "INSUFFICIENT_EVIDENCE"
                    ),
                },
            ],
        ),
        _gate(
            "MARKET_INCREMENTAL_VALUE",
            [
                {
                    "name": "adds_information_beyond_market",
                    "status": "PASS" if incremental_model_adds_value else "FAIL",
                },
                {
                    "name": "chronological_holdout_improves_market",
                    "status": (
                        "PASS" if chronological_holdout_improves_market else "FAIL"
                    ),
                },
                {
                    "name": "confidence_not_plainly_noise",
                    "status": "PASS" if confidence_distinguishes else "FAIL",
                    "confidence_interval_95": confidence_interval,
                },
            ],
        ),
        _gate(
            "EXECUTION_EVIDENCE",
            [
                {
                    "name": "synchronized_executable_orderbooks",
                    "status": prospective_status(
                        forward_status.get("valid_orderbook_snapshots", 0) > 0
                    ),
                },
                {
                    "name": "yes_no_prices_spread_depth_timestamps",
                    "status": prospective_status(
                        forward_status.get("valid_orderbook_snapshots", 0) > 0
                    ),
                },
                {
                    "name": "realistic_fill_simulation",
                    "status": prospective_status(
                        forward_status.get("paper_execution_events", 0) > 0
                    ),
                },
                {
                    "name": "fees_and_unfilled_orders",
                    "status": prospective_status(
                        forward_status.get("paper_execution_events", 0) > 0
                        and forward_status.get("no_fill_events", 0) > 0
                    ),
                },
                {
                    "name": "partial_fill_handling",
                    "status": prospective_status(
                        forward_status.get("paper_execution_events", 0) > 0
                    ),
                    "observed_partial_fills": forward_status.get(
                        "partial_fill_events", 0
                    ),
                },
            ],
        ),
        _gate(
            "FORWARD_CONFIRMATION",
            [
                {
                    "name": "candidate_frozen_before_collection",
                    "status": (
                        "PASS"
                        if forward_status.get("candidate_frozen")
                        else "NOT_AVAILABLE"
                    ),
                },
                {
                    "name": "minimum_calendar_period",
                    "status": (
                        forward_threshold_status(
                            forward_status.get("calendar_days", 0)
                            >= config.minimum_calendar_days
                        )
                    ),
                    "current": forward_status.get("calendar_days", 0),
                    "required": config.minimum_calendar_days,
                },
                {
                    "name": "minimum_independent_events",
                    "status": (
                        forward_threshold_status(
                            forward_status.get("independent_events", 0)
                            >= config.minimum_independent_events
                        )
                    ),
                    "current": forward_status.get("independent_events", 0),
                    "required": config.minimum_independent_events,
                },
                {
                    "name": "minimum_settled_eligible_paper_trades",
                    "status": (
                        forward_threshold_status(
                            forward_status.get(
                                "settled_eligible_paper_trades", 0
                            )
                            >= config.minimum_settled_eligible_paper_trades
                        )
                    ),
                    "current": forward_status.get(
                        "settled_eligible_paper_trades", 0
                    ),
                    "required": config.minimum_settled_eligible_paper_trades,
                },
                {
                    "name": "minimum_city_coverage",
                    "status": forward_threshold_status(
                        forward_status.get("cities", 0)
                        >= config.minimum_cities
                    ),
                    "current": forward_status.get("cities", 0),
                    "required": config.minimum_cities,
                },
                {
                    "name": "minimum_forecast_horizon_coverage",
                    "status": forward_threshold_status(
                        forward_status.get("forecast_horizons", 0)
                        >= config.minimum_forecast_horizons
                    ),
                    "current": forward_status.get("forecast_horizons", 0),
                    "required": config.minimum_forecast_horizons,
                },
                {
                    "name": "no_unresolved_data_integrity_violations",
                    "status": forward_threshold_status(
                        forward_status.get("integrity_violations", 0) == 0
                    ),
                    "current": forward_status.get("integrity_violations", 0),
                },
                {
                    "name": "positive_fee_aware_execution_performance",
                    "status": forward_threshold_status(
                        bool(
                            forward_status.get(
                                "candidate_performance_gate_passed"
                            )
                        )
                    ),
                },
                {
                    "name": "not_concentrated_in_one_city_or_date",
                    "status": forward_threshold_status(
                        bool(
                            forward_status.get(
                                "concentration_acceptable"
                            )
                        )
                    ),
                },
                {
                    "name": "conservative_fill_assumptions",
                    "status": forward_threshold_status(
                        bool(
                            forward_status.get(
                                "conservative_fill_assumptions"
                            )
                        )
                    ),
                },
                {
                    "name": "no_retrospective_parameter_changes",
                    "status": (
                        "PASS"
                        if forward_status.get("manifest_immutable")
                        else "NOT_AVAILABLE"
                    ),
                },
            ],
        ),
        _gate(
            "OPERATIONAL_SAFETY",
            [
                {
                    "name": name,
                    "status": "PASS" if passed else "FAIL",
                }
                for name, passed in operational_checks.items()
            ]
            + [
                {
                    "name": "deployed_persistence_schema",
                    "status": (
                        "PASS"
                        if forward_status.get("schema_deployed")
                        else "NOT_AVAILABLE"
                    ),
                }
            ],
        ),
    ]
    ready = all(gate["status"] == "PASS" for gate in gates)
    return {
        "overall_conclusion": (
            "READY_FOR_EXPLICIT_REVIEW" if ready else "NOT_READY_FOR_REAL_TRADING"
        ),
        "automatic_promotion_allowed": False,
        "configured_live_strategy_changed": False,
        "gates": {gate["gate"]: gate for gate in gates},
        "next_required_action": (
            "Deploy the append-only evidence schema and run the forward collector; "
            "then accrue the frozen 60-day/100-event/100-settled-trade period."
        ),
    }
