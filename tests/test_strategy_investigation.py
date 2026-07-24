from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

from strategy_research.investigation import (
    CandidateSpec,
    ResearchRow,
    candidate_specs,
    chronological_partitions,
    evaluate_candidate,
    validate_no_lookahead,
)
from dashboard.app import _strategy_investigation_summary


def _rows() -> list[ResearchRow]:
    rows = []
    base = date(2026, 7, 18)
    for day_index in range(6):
        target = base + timedelta(days=day_index)
        decision = datetime.combine(target, datetime.min.time(), tzinfo=UTC)
        event = f"EVENT-{target.isoformat()}"
        for bracket in range(2):
            outcome = bracket == day_index % 2
            model = 0.75 if outcome else 0.25
            market = 0.60 if outcome else 0.40
            rows.append(
                ResearchRow(
                    alert_id=day_index * 2 + bracket,
                    decision_as_of_utc=decision,
                    event_ticker=event,
                    market_ticker=f"{event}-{bracket}",
                    series_ticker="KXHIGHNY",
                    city="NYC",
                    station="NYC",
                    target_date=target,
                    floor_strike=80 + bracket,
                    cap_strike=80 + bracket,
                    settled_winner=outcome,
                    settled_at=decision + timedelta(days=1),
                    actual_high_temp=80 + (day_index % 2),
                    model_version="normal-v4-observation-conditioned",
                    ensemble_mean=80,
                    ensemble_std=2,
                    observed_so_far=79,
                    lead_days=0,
                    model_probability=model,
                    market_probability=market,
                    edge=model - market,
                    fee_adjusted_threshold=0.03,
                    quote_time=decision,
                    weather_captured_at=decision,
                )
            )
    return rows


def test_no_lookahead_accepts_features_captured_by_decision_time():
    assert validate_no_lookahead(_rows()) == []


def test_no_lookahead_rejects_future_quote():
    row = _rows()[0]
    future = replace(row, quote_time=row.decision_as_of_utc + timedelta(seconds=1))
    assert validate_no_lookahead([future]) == [f"{row.market_ticker}:quote_time"]


def test_chronological_folds_keep_event_dates_together_and_holdout_newest():
    folds, preholdout, holdout = chronological_partitions(_rows())
    assert max(row.target_date for row in preholdout) < min(
        row.target_date for row in holdout
    )
    for training, validation in folds:
        assert max(row.target_date for row in training) < min(
            row.target_date for row in validation
        )
        assert not ({row.event_key for row in training} & {row.event_key for row in validation})


def test_candidate_metrics_report_wins_losses_brier_and_null_execution_pnl():
    rows = _rows()
    result = evaluate_candidate(
        CandidateSpec("current", "test-v1", "current"),
        rows[:8],
        rows[8:],
    )
    assert result["independent_city_date_clusters"] == 2
    assert result["eligible_signals"] <= 2
    assert (
        result["directional_signal_wins"]
        + result["directional_signal_losses"]
        == result["eligible_signals"]
    )
    assert result["settled_trades"] == 0
    assert result["wins"] == result["losses"] == result["voids"] == 0
    assert result["brier_score"] is not None
    assert result["market_brier_score"] is not None
    assert result["calibration_gap"] is not None
    assert result["gross_pnl"] is None
    assert result["fees"] is None
    assert result["net_pnl"] is None
    assert result["profit_factor"] is None
    assert result["expectancy"] is None
    assert result["maximum_drawdown"] is None
    assert result["execution_basis"] == "FORECAST_SKILL_ONLY"


def test_calibration_training_period_precedes_validation_period():
    rows = _rows()
    result = evaluate_candidate(
        CandidateSpec("logistic", "test-logistic-v1", "logistic"),
        rows[:8],
        rows[8:],
    )
    assert result["training_period"].endswith("2026-07-21")
    assert result["validation_period"].startswith("2026-07-22")


def test_all_candidates_require_explicit_promotion_and_start_unpromoted():
    assert candidate_specs()
    assert all(spec.status != "LIVE_CANDIDATE" for spec in candidate_specs())
    names = {spec.name for spec in candidate_specs()}
    assert "hard_lower_bound_truncation" in names
    assert "empirical_remaining_day_residual" in names
    assert "remaining_hour_maximum_simulation" in names


def test_canonical_row_never_claims_midpoint_is_executable():
    payload = _rows()[0].to_csv_dict()
    assert payload["executable_yes_price"] is None
    assert payload["executable_no_price"] is None
    assert payload["execution_basis"] == "FORECAST_SKILL_ONLY"


def test_saved_investigation_summary_is_unpromoted_and_dashboard_ready():
    summary = _strategy_investigation_summary()
    assert summary is not None
    assert summary["investigation_status"] == "COMPLETE_NO_PROMOTION"
    assert summary["promotion_status"] == "REJECTED"
    assert summary["net_pnl"] is None
