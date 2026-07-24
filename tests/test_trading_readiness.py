from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from kalshi_client import MarketOrderbook, OrderbookLevel
from strategy_research.investigation import (
    CandidateSpec,
    ResearchRow,
    evaluate_candidate,
    matched_brier_comparison,
)
from trading_readiness.collector import ForwardEvidenceCollector
from trading_readiness.config import ReadinessConfig
from trading_readiness.execution import conservative_fill
from trading_readiness.freeze import (
    frozen_candidates,
    persist_freeze_manifest,
)
from trading_readiness.metrics import (
    blend_probability,
    clustered_brier_uncertainty,
    validate_candidate_populations,
)
from trading_readiness.readiness import build_readiness_report
from trading_readiness.stream import OrderbookState


def _research_rows() -> list[ResearchRow]:
    rows: list[ResearchRow] = []
    for city_index, city in enumerate(("NYC", "Chicago", "Miami", "Denver")):
        target = date(2026, 7, 20 + city_index // 2)
        decision = datetime(2026, 7, 19 + city_index // 2, tzinfo=UTC)
        event = f"{city}-{target.isoformat()}"
        for bracket in range(2):
            outcome = bracket == city_index % 2
            rows.append(
                ResearchRow(
                    alert_id=len(rows) + 1,
                    decision_as_of_utc=decision,
                    event_ticker=event,
                    market_ticker=f"{event}-{bracket}",
                    series_ticker="KXHIGHNY",
                    city=city,
                    station=city,
                    target_date=target,
                    floor_strike=80 + bracket,
                    cap_strike=81 + bracket,
                    settled_winner=outcome,
                    settled_at=decision + timedelta(days=2),
                    actual_high_temp=80,
                    model_version="normal-v4-observation-conditioned",
                    ensemble_mean=80,
                    ensemble_std=2,
                    observed_so_far=None,
                    lead_days=1,
                    model_probability=0.7 if outcome else 0.3,
                    market_probability=0.6 if outcome else 0.4,
                    edge=0.1 if outcome else -0.1,
                    fee_adjusted_threshold=0.03,
                    quote_time=decision,
                    weather_captured_at=decision,
                )
            )
    return rows


def _book() -> MarketOrderbook:
    return MarketOrderbook(
        ticker="TICKER",
        yes_bids=(OrderbookLevel(Decimal("0.40"), Decimal("2")),),
        no_bids=(
            OrderbookLevel(Decimal("0.30"), Decimal("0.5")),
            OrderbookLevel(Decimal("0.20"), Decimal("1.0")),
        ),
        raw={},
    )


def test_blend_weight_semantics_have_unambiguous_endpoints():
    assert (
        blend_probability(
            0.8, 0.3, model_weight=0.0, market_weight=1.0
        )
        == 0.3
    )
    assert (
        blend_probability(
            0.8, 0.3, model_weight=1.0, market_weight=0.0
        )
        == 0.8
    )


def test_candidate_blend_uses_model_and_market_weights():
    row = _research_rows()[0]
    pure_market = evaluate_candidate(
        CandidateSpec(
            "pure_market",
            "pure-market-v1",
            "blend",
            model_weight=0.0,
            market_weight=1.0,
        ),
        _research_rows(),
        [row],
    )
    assert pure_market["brier_score"] == pure_market["market_brier_score"]


def test_brier_comparison_requires_identical_common_population():
    result = matched_brier_comparison(
        [0.8, None, 0.3],
        [0.7, 0.5, None],
        [True, False, True],
    )
    assert result["model_event_count"] == 2
    assert result["market_event_count"] == 2
    assert result["common_event_count"] == 1
    assert result["excluded_model_events"] == 2
    assert result["excluded_market_events"] == 2


def test_execution_and_directional_denominators_are_separate():
    rows = _research_rows()
    result = evaluate_candidate(
        CandidateSpec("current", "current-v1", "current"),
        rows[:4],
        rows[4:],
    )
    assert validate_candidate_populations(result) == []
    assert result["settled_trades"] == 0
    assert result["wins"] + result["losses"] + result["voids"] == 0
    assert (
        result["directional_signal_wins"]
        + result["directional_signal_losses"]
        + result["directional_signal_voids"]
        == result["eligible_signals"]
    )


def test_zero_trade_candidate_retains_probability_metrics_only():
    rows = _research_rows()
    result = evaluate_candidate(
        CandidateSpec("no_trade", "no-trade-v1", "impossible"),
        rows[:4],
        rows[4:],
    )
    assert result["brier_score"] is not None
    assert result["eligible_signals"] == 0
    assert result["submitted_paper_orders"] == 0
    assert result["settled_trades"] == 0
    assert result["win_rate"] is None


def test_clustered_uncertainty_resamples_city_date_events_not_brackets():
    result = clustered_brier_uncertainty(
        _research_rows(),
        model_weight=0.5,
        market_weight=0.5,
        samples=2_000,
        seed=7,
    )
    assert result["independent_cluster_count"] == 4
    assert result["bracket_outcome_count"] == 8
    assert result["brackets_treated_as_independent"] is False
    assert len(result["confidence_interval_95"]) == 2
    assert 0 <= result["probability_candidate_beats_market"] <= 1


def test_candidate_freeze_manifest_is_immutable(tmp_path):
    timestamp = datetime(2026, 7, 24, tzinfo=UTC)
    candidates = frozen_candidates(
        freeze_timestamp=timestamp,
        code_hash="abc",
    )
    path = tmp_path / "manifest.json"
    persist_freeze_manifest(path, candidates)
    persist_freeze_manifest(path, candidates)
    changed = (
        replace(candidates[0], signal_threshold=0.06),
        candidates[1],
    )
    with pytest.raises(ValueError, match="immutable"):
        persist_freeze_manifest(path, changed)
    assert len({candidate.strategy_version for candidate in candidates}) == 2
    assert all(
        candidate.promotion_status == "FROZEN_RESEARCH"
        and candidate.automatic_promotion_allowed is False
        for candidate in candidates
    )


def test_conservative_fill_uses_visible_depth_and_records_partial_and_no_fill():
    full = conservative_fill(
        _book(),
        outcome="YES",
        limit_price=Decimal("0.80"),
        requested_quantity=Decimal("1"),
        book_is_fresh=True,
        market_is_active=True,
    )
    assert full.status == "FILLED"
    assert full.filled_quantity == Decimal("1")
    assert full.weighted_fill_price == Decimal("0.7500")

    partial = conservative_fill(
        _book(),
        outcome="YES",
        limit_price=Decimal("0.80"),
        requested_quantity=Decimal("2"),
        book_is_fresh=True,
        market_is_active=True,
    )
    assert partial.status == "PARTIAL_FILL"
    assert partial.filled_quantity == Decimal("1.5")

    no_fill = conservative_fill(
        _book(),
        outcome="YES",
        limit_price=Decimal("0.60"),
        requested_quantity=Decimal("1"),
        book_is_fresh=True,
        market_is_active=True,
    )
    assert no_fill.status == "NO_FILL"
    assert no_fill.filled_quantity == 0


def test_orderbook_timestamp_semantics_and_stale_detection():
    received = datetime(2026, 7, 24, 0, 0, 10, tzinfo=UTC)
    state = OrderbookState.from_websocket_snapshot(
        {
            "type": "orderbook_snapshot",
            "seq": 4,
            "msg": {
                "market_ticker": "TICKER",
                "yes_dollars_fp": [["0.40", "2"]],
                "no_dollars_fp": [["0.70", "1"]],
                "ts_ms": int(
                    datetime(2026, 7, 24, tzinfo=UTC).timestamp() * 1000
                ),
            },
        },
        received_at=received,
        use_yes_price=True,
    )
    assert state.source_publish_time < state.collector_received_time
    assert state.as_orderbook().best_no_bid == Decimal("0.30")
    diagnostics = state.diagnostics(
        now=received + timedelta(seconds=31),
        stale_after=timedelta(seconds=30),
        delayed_after=timedelta(seconds=5),
    )
    assert diagnostics["stale"] is True
    assert diagnostics["delayed_local_receipt"] is True


class _FakeRepository:
    def __init__(self):
        self.orderbooks = []
        self.messages = []

    def append_orderbook(self, state, **kwargs):
        self.orderbooks.append((state.as_orderbook(), kwargs))
        return len(self.orderbooks)

    def append_stream_message(self, message, **kwargs):
        self.messages.append((message, kwargs))


class _FakeClient:
    def __init__(self):
        self.calls = 0

    def get_orderbooks(self, tickers):
        self.calls += 1
        return [replace(_book(), ticker=ticker) for ticker in tickers]


def test_orderbook_snapshots_are_append_only_and_sequence_gap_rest_recovers():
    repository = _FakeRepository()
    client = _FakeClient()
    now = datetime(2026, 7, 24, tzinfo=UTC)
    collector = ForwardEvidenceCollector(
        client=client,
        repository=repository,
        candidates=(),
        config=ReadinessConfig(),
        now=lambda: now,
    )
    collector.capture_rest_books(["TICKER"])
    collector.capture_rest_books(["TICKER"])
    assert len(repository.orderbooks) == 2

    collector.sequence_by_subscription[1] = 4
    result = collector.process_websocket_message(
        {
            "type": "orderbook_delta",
            "sid": 1,
            "seq": 6,
            "msg": {
                "market_ticker": "TICKER",
                "side": "yes",
                "price_dollars": "0.40",
                "delta_fp": "1",
                "ts_ms": int(now.timestamp() * 1000),
            },
        }
    )
    assert result == "RECOVERED_SEQUENCE_GAP"
    assert repository.orderbooks[-1][1]["source"] == "REST_RECOVERY"
    assert repository.messages[-1][1]["gap_detected"] is True


def test_forward_paper_events_keep_candidate_versions_separate():
    timestamp = datetime(2026, 7, 24, tzinfo=UTC)
    candidates = frozen_candidates(
        freeze_timestamp=timestamp,
        code_hash="abc",
    )
    collector = ForwardEvidenceCollector(
        client=_FakeClient(),
        repository=_FakeRepository(),
        candidates=candidates,
        config=ReadinessConfig(),
        now=lambda: timestamp,
    )
    decision = {
        "market_ticker": "TICKER",
        "selected_side": "YES",
        "maximum_acceptable_price": Decimal("0.80"),
        "intended_quantity": Decimal("1"),
        "rejection_reason": None,
    }
    diagnostics = {
        "stale": False,
        "crossed_or_impossible": False,
        "missing_opposing_levels": False,
        "delayed_local_receipt": False,
    }
    events = [
        collector._paper_event(
            1,
            decision,
            candidate,
            _book(),
            diagnostics,
            "active",
        )
        for candidate in candidates
    ]
    assert {event["candidate_version"] for event in events} == {
        candidate.strategy_version for candidate in candidates
    }


def test_readiness_gates_stay_separate_and_never_auto_promote():
    report = build_readiness_report(
        no_lookahead_passed=True,
        populations_consistent=True,
        bracket_mapping_verified=True,
        candidate_beats_current_model=True,
        calibration_gap=0.04,
        uncertainty={"confidence_interval_95": [-0.01, 0.02]},
        stability={"all_sensitivity_results_favor_candidate": False},
        incremental_model_adds_value=False,
        chronological_holdout_improves_market=True,
        forward_status={
            "candidate_frozen": True,
            "manifest_immutable": True,
            "schema_deployed": False,
        },
        operational_checks={"idempotency": True},
        config=ReadinessConfig(),
    )
    assert set(report["gates"]) == {
        "DATA_INTEGRITY",
        "FORECAST_SKILL",
        "MARKET_INCREMENTAL_VALUE",
        "EXECUTION_EVIDENCE",
        "FORWARD_CONFIRMATION",
        "OPERATIONAL_SAFETY",
    }
    assert report["gates"]["MARKET_INCREMENTAL_VALUE"]["status"] == "FAIL"
    assert report["gates"]["EXECUTION_EVIDENCE"]["status"] == "NOT_AVAILABLE"
    assert report["automatic_promotion_allowed"] is False
    assert report["configured_live_strategy_changed"] is False
