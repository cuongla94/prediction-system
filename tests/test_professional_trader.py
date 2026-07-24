from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from kalshi_client import MarketOrderbook, OrderbookLevel
from trading_readiness.professional import (
    AccountState,
    DecisionContext,
    InformationEventType,
    PositionState,
    ReviewClassification,
    ThesisType,
    TraderAction,
    bracket_is_impossible,
    build_contract_truth,
    build_market_state,
    build_weather_state,
    detect_information_events,
    make_information_event,
    make_journal_event,
    material_action_alert,
    review_decision_process,
    select_professional_decision,
)
from trading_readiness.professional_freeze import (
    frozen_professional_strategy,
    persist_professional_freeze,
)
from trading_readiness.professional_collector import _action_alert_payload
from weather.stations import STATIONS

NOW = datetime(2026, 7, 24, 18, 0, tzinfo=UTC)


def _alert() -> dict:
    return {
        "event_ticker": "KXHIGHNY-26JUL25",
        "market_ticker": "KXHIGHNY-26JUL25-B90.5",
        "series_ticker": "KXHIGHNY",
        "city": "NYC",
        "metric": "max",
        "floor_strike": "90",
        "cap_strike": "91",
        "market_status": "active",
        "close_time": NOW + timedelta(hours=6),
        "observed_so_far": "85",
        "observation_event_time": NOW - timedelta(minutes=3),
        "observation_publication_time": NOW - timedelta(minutes=2),
        "observation_collector_received_time": NOW - timedelta(minutes=1),
        "forecast_run_time": NOW - timedelta(hours=2),
        "forecast_availability_time": NOW - timedelta(hours=1),
        "ensemble_mean": "92",
        "ensemble_std": "2",
    }


def _contract():
    return build_contract_truth(
        _alert(), station=STATIONS["KXHIGHNY"], market=None
    )


def _weather():
    return build_weather_state(_alert(), _contract(), decision_time=NOW)


def _book(*, side: str = "YES", quantity: str = "5") -> MarketOrderbook:
    if side == "YES":
        yes_bid, no_bid = "0.38", "0.60"
    else:
        yes_bid, no_bid = "0.60", "0.38"
    return MarketOrderbook(
        ticker=_alert()["market_ticker"],
        yes_bids=(OrderbookLevel(Decimal(yes_bid), Decimal(quantity)),),
        no_bids=(OrderbookLevel(Decimal(no_bid), Decimal(quantity)),),
        raw={},
    )


def _market(*, side: str = "YES", quantity: str = "5"):
    return build_market_state(
        _book(side=side, quantity=quantity),
        source_time=NOW - timedelta(seconds=2),
        receipt_time=NOW - timedelta(seconds=1),
        decision_time=NOW,
        close_time=NOW + timedelta(hours=6),
        market_status="active",
        maximum_yes_price=Decimal("0.90"),
        maximum_no_price=Decimal("0.90"),
        last_trade=Decimal("0.99"),
    )


def _account(**changes):
    return replace(
        AccountState(
            available_cash=Decimal("10"),
            reconciliation_healthy=True,
            forward_evidence_sufficient=False,
        ),
        **changes,
    )


def _event(
    event_type: InformationEventType = InformationEventType.FORECAST_REVISED,
    *,
    receipt: datetime = NOW - timedelta(seconds=10),
):
    return make_information_event(
        event_type=event_type,
        source="test",
        source_event_time=receipt - timedelta(seconds=20),
        source_publication_time=receipt - timedelta(seconds=5),
        collector_receipt_time=receipt,
        processing_time=NOW,
        event_ticker=_alert()["event_ticker"],
        market_ticker=_alert()["market_ticker"],
        previous_value="91",
        new_value="92",
        material=True,
    )


def _context(
    *,
    side: str = "YES",
    trigger=None,
    position: PositionState = PositionState(),
    **changes,
):
    values = {
        "contract": _contract(),
        "weather": _weather(),
        "market": _market(side=side),
        "account": _account(),
        "strategy_name": "weather-daily-temp",
        "strategy_version": "v1-2026-07-23",
        "candidate_version": "forward-test-v1",
        "decision_time": NOW,
        "model_yes_probability": (
            Decimal("0.70") if side == "YES" else Decimal("0.20")
        ),
        "market_yes_probability": (
            Decimal("0.40") if side == "YES" else Decimal("0.60")
        ),
        "working_yes_probability": (
            Decimal("0.70") if side == "YES" else Decimal("0.20")
        ),
        "uncertainty_indicator": "TEST_FIXTURE",
        "confidence_level": "MEDIUM",
        "probability_method_version": "normal-v4",
        "triggering_event": trigger,
        "signal_first_appeared": trigger is None,
        "position": position,
        "prospective_paper_only": True,
    }
    values.update(changes)
    return DecisionContext(**values)


def test_contract_truth_maps_station_timezone_and_bracket_boundaries():
    truth = _contract()
    assert truth.status == "CLEAR"
    assert truth.station_identifier == "NYC"
    assert truth.settlement_timezone == "Etc/GMT+5"
    assert truth.bracket_lower_boundary == Decimal("90")
    assert truth.bracket_upper_boundary == Decimal("91")
    assert truth.bracket_open_ended is False


def test_contract_truth_supports_open_tail_and_blocks_unclear_contract():
    tail = _alert()
    tail["cap_strike"] = None
    truth = build_contract_truth(
        tail, station=STATIONS["KXHIGHNY"], market=None
    )
    assert truth.bracket_open_ended is True
    unclear = build_contract_truth(_alert(), station=None, market=None)
    decision = select_professional_decision(
        _context(contract=unclear)
    )
    assert decision.action == TraderAction.DO_NOT_TRADE
    assert "UNKNOWN_SETTLEMENT_STATION" in decision.blockers


def test_observed_extreme_makes_high_bracket_impossible():
    assert bracket_is_impossible(
        metric="max",
        observed_extreme=Decimal("91"),
        lower_boundary=Decimal("90"),
        upper_boundary=Decimal("91"),
    )
    impossible_weather = replace(_weather(), bracket_impossible=True)
    decision = select_professional_decision(
        _context(
            weather=impossible_weather,
            working_yes_probability=Decimal("0.20"),
        )
    )
    assert decision.action == TraderAction.DO_NOT_TRADE
    assert (
        "IMPOSSIBLE_BRACKET_NONZERO_PROBABILITY" in decision.blockers
    )


def test_information_events_preserve_publication_and_receipt_timestamps():
    event = _event()
    assert event.source_publication_time < event.collector_receipt_time
    assert event.collector_receipt_time < event.processing_time
    assert event.to_dict()["source_publication_time"].endswith("+00:00")


def test_forecast_revision_new_high_impossible_and_materiality_are_detected():
    events = detect_information_events(
        previous={
            "forecast_value": Decimal("90"),
            "observation_value": Decimal("84"),
            "observed_daily_extreme": Decimal("84"),
            "bracket_impossible": False,
            "model_disagreement": Decimal("1"),
            "executable_yes_price": Decimal("0.40"),
            "available_quantity": Decimal("5"),
            "spread": Decimal("0.02"),
            "market_status": "active",
        },
        current={
            "forecast_value": Decimal("91"),
            "observation_value": Decimal("85"),
            "observed_daily_extreme": Decimal("85"),
            "bracket_impossible": True,
            "model_disagreement": Decimal("1.1"),
            "executable_yes_price": Decimal("0.41"),
            "available_quantity": Decimal("5"),
            "spread": Decimal("0.03"),
            "market_status": "active",
            "metric": "max",
            "collector_receipt_time": NOW,
        },
        event_ticker=_alert()["event_ticker"],
        market_ticker=_alert()["market_ticker"],
        processing_time=NOW,
    )
    types = {event.event_type for event in events}
    assert InformationEventType.FORECAST_REVISED in types
    assert InformationEventType.NEW_DAILY_HIGH in types
    assert InformationEventType.BRACKET_BECAME_IMPOSSIBLE in types
    price = next(
        event
        for event in events
        if event.event_type == InformationEventType.MARKET_PRICE_MOVED
    )
    assert price.material is False


def test_decision_snapshot_is_deeply_immutable_and_deterministic():
    first = select_professional_decision(_context())
    second = select_professional_decision(_context())
    assert first.decision_id == second.decision_id
    with pytest.raises(FrozenInstanceError):
        first.action = TraderAction.WATCH
    with pytest.raises(TypeError):
        first.thesis["summary"] = "rewritten after the outcome"


def test_professional_forward_policy_is_immutable():
    freeze = frozen_professional_strategy(
        frozen_at=NOW, code_hash="test-hash"
    )
    with pytest.raises(TypeError):
        freeze.policy_config["margin_of_safety"] = "0"
    assert freeze.automatic_promotion_allowed is False
    assert freeze.configured_live_strategy_changed is False


@pytest.mark.parametrize(
    ("timestamp_change", "blocker"),
    (
        ("forecast_availability_time", "FUTURE_FORECAST"),
        ("observation_publication_time", "FUTURE_OBSERVATION"),
    ),
)
def test_future_weather_information_is_blocked(
    timestamp_change: str, blocker: str
):
    weather = replace(
        _weather(), **{timestamp_change: NOW + timedelta(seconds=1)}
    )
    decision = select_professional_decision(_context(weather=weather))
    assert decision.action == TraderAction.DO_NOT_TRADE
    assert blocker in decision.blockers


def test_future_market_price_and_settlement_leakage_are_blocked():
    market = replace(
        _market(), quote_receipt_time=NOW + timedelta(seconds=1)
    )
    future_quote = select_professional_decision(_context(market=market))
    assert "FUTURE_MARKET_PRICE" in future_quote.blockers
    leaked = select_professional_decision(
        _context(
            trigger=_event(InformationEventType.SETTLEMENT_PUBLISHED),
            signal_first_appeared=False,
        )
    )
    assert leaked.action == TraderAction.DO_NOT_TRADE
    assert "SETTLEMENT_INFORMATION_LEAKAGE" in leaked.blockers


def test_buy_yes_uses_executable_ask_includes_fees_and_fixed_quantity():
    decision = select_professional_decision(_context())
    assert decision.action == TraderAction.BUY_YES
    assert decision.execution["buy_yes_price"] == Decimal("0.40")
    assert decision.execution["buy_yes_price"] != Decimal("0.99")
    assert decision.execution["estimated_fees"] > 0
    assert (
        decision.net_edge_after_costs
        < decision.execution["gross_edge"]
    )
    assert decision.execution["order_quantity"] == Decimal("1")
    assert (
        decision.execution["buy_yes_price"]
        <= decision.execution["maximum_acceptable_entry_price"]
    )
    assert decision.production_order_allowed is False


def test_buy_no_uses_executable_opposing_book():
    decision = select_professional_decision(_context(side="NO"))
    assert decision.action == TraderAction.BUY_NO
    assert decision.execution["buy_no_price"] == Decimal("0.40")


def test_watch_and_do_not_trade_are_explicit_retained_states():
    wide = replace(_market(), spread=Decimal("0.20"))
    watch = select_professional_decision(_context(market=wide))
    assert watch.action == TraderAction.WATCH
    assert watch.decision_reason_code == "SPREAD_TOO_WIDE"
    stale = select_professional_decision(
        _context(weather=replace(_weather(), stale=True))
    )
    assert stale.action == TraderAction.DO_NOT_TRADE
    assert "DATA_STALE" in stale.blockers


def test_entry_blocks_duplicate_reconciliation_and_missing_quantity():
    duplicate = select_professional_decision(
        _context(account=_account(open_bot_owned_orders=1))
    )
    assert duplicate.action == TraderAction.DO_NOT_TRADE
    assert "DUPLICATE_OR_RESTING_ORDER" in duplicate.blockers
    unreconciled = select_professional_decision(
        _context(account=_account(reconciliation_healthy=False))
    )
    assert "RECONCILIATION_REQUIRED" in unreconciled.blockers
    undercapitalized = select_professional_decision(
        _context(account=_account(available_cash=Decimal("5.00")))
    )
    assert "CAPITAL_INSUFFICIENT" in undercapitalized.blockers
    no_depth = select_professional_decision(
        _context(market=_market(quantity="0"))
    )
    assert no_depth.action == TraderAction.WATCH
    assert "LIQUIDITY_INSUFFICIENT" in no_depth.blockers


def test_hold_requires_remaining_edge_and_valid_thesis():
    position = PositionState(
        side="YES",
        contracts=Decimal("1"),
        original_thesis=ThesisType.NEW_FORECAST_INFORMATION,
    )
    decision = select_professional_decision(
        _context(
            trigger=_event(),
            position=position,
            signal_first_appeared=False,
        )
    )
    assert decision.action == TraderAction.HOLD
    assert decision.decision_reason_code == "THESIS_REMAINS_VALID"


@pytest.mark.parametrize(
    ("context_changes", "reason"),
    (
        (
            {"position": PositionState(
                side="YES",
                contracts=Decimal("1"),
                thesis_invalidated=True,
            )},
            "THESIS_INVALIDATED",
        ),
        (
            {
                "position": PositionState(
                    side="YES", contracts=Decimal("1")
                ),
                "market": replace(
                    _market(), exit_yes_price=Decimal("0.75")
                ),
            },
            "NET_EDGE_GONE",
        ),
        (
            {
                "position": PositionState(
                    side="YES", contracts=Decimal("1")
                ),
                "risk_blockers": ("RISK_LIMIT_TRIGGERED",),
            },
            "RISK_LIMIT_TRIGGERED",
        ),
    ),
)
def test_exit_is_full_and_thesis_edge_or_risk_driven(
    context_changes: dict, reason: str
):
    decision = select_professional_decision(
        _context(
            trigger=_event(),
            signal_first_appeared=False,
            **context_changes,
        )
    )
    assert decision.action == TraderAction.EXIT
    assert decision.decision_reason_code == reason
    assert decision.execution["order_quantity"] == Decimal("1")


@pytest.mark.parametrize(
    ("side", "expected"),
    (("YES", TraderAction.REBUY_YES), ("NO", TraderAction.REBUY_NO)),
)
def test_rebuy_requires_new_weather_information_and_new_decision(
    side: str, expected: TraderAction
):
    position = PositionState(
        entry_information_event_id="old-entry-event",
        prior_entry_decision_id="old-entry-decision",
        prior_exit_decision_id="old-exit-decision",
    )
    decision = select_professional_decision(
        _context(
            side=side,
            trigger=_event(),
            position=position,
            signal_first_appeared=False,
            parent_decision_id="old-exit-decision",
        )
    )
    assert decision.action == expected
    assert decision.parent_decision_id == "old-exit-decision"
    assert decision.decision_id not in {
        "old-entry-decision",
        "old-exit-decision",
    }
    assert decision.thesis["new_information"]["information_event_id"]


def test_price_decline_same_information_and_active_risk_block_reentry():
    closed = PositionState(
        entry_information_event_id="entry-event",
        prior_entry_decision_id="entry-decision",
        prior_exit_decision_id="exit-decision",
    )
    price_only = select_professional_decision(
        _context(
            trigger=_event(InformationEventType.MARKET_PRICE_MOVED),
            position=closed,
            signal_first_appeared=False,
        )
    )
    assert price_only.action == TraderAction.DO_NOT_TRADE
    assert price_only.decision_reason_code == "REENTRY_REQUIRES_NEW_INFORMATION"
    same_event = _event()
    same = select_professional_decision(
        _context(
            trigger=same_event,
            position=replace(
                closed,
                entry_information_event_id=same_event.information_event_id,
            ),
            signal_first_appeared=False,
        )
    )
    assert same.action == TraderAction.DO_NOT_TRADE
    risk = select_professional_decision(
        _context(
            trigger=same_event,
            position=replace(
                closed,
                prior_exit_reason="RISK_LIMIT_TRIGGERED",
                prior_exit_information_event_id=(
                    same_event.information_event_id
                ),
            ),
            signal_first_appeared=False,
        )
    )
    assert risk.action == TraderAction.DO_NOT_TRADE


def test_journal_chain_is_deterministic_and_preserves_manual_bot_separation():
    information = make_journal_event(
        event_ticker=_alert()["event_ticker"],
        market_ticker=_alert()["market_ticker"],
        candidate_version="v1",
        record_type="INFORMATION_EVENT",
        record_id="info",
        processing_time=NOW,
        payload={"manual_exposure": "2", "bot_exposure": "1"},
    )
    decision = make_journal_event(
        event_ticker=_alert()["event_ticker"],
        market_ticker=_alert()["market_ticker"],
        candidate_version="v1",
        record_type="DECISION_SNAPSHOT",
        record_id="decision",
        parent_record_type="INFORMATION_EVENT",
        parent_record_id=information.record_id,
        processing_time=NOW,
    )
    settlement = make_journal_event(
        event_ticker=_alert()["event_ticker"],
        market_ticker=_alert()["market_ticker"],
        candidate_version="v1",
        record_type="SETTLEMENT",
        record_id="settlement",
        parent_record_type="DECISION_SNAPSHOT",
        parent_record_id=decision.record_id,
        processing_time=NOW,
        settlement_time=NOW,
    )
    assert decision.parent_record_id == "info"
    assert settlement.parent_record_id == "decision"
    assert information.journal_event_id == make_journal_event(
        event_ticker=_alert()["event_ticker"],
        market_ticker=_alert()["market_ticker"],
        candidate_version="v1",
        record_type="INFORMATION_EVENT",
        record_id="info",
        processing_time=NOW + timedelta(hours=1),
        payload={"changed": True},
    ).journal_event_id
    account = _account(
        manual_market_exposure=Decimal("2"),
        bot_market_exposure=Decimal("1"),
        total_account_exposure=Decimal("3"),
    )
    assert account.manual_market_exposure != account.bot_market_exposure


def test_post_trade_review_separates_process_from_outcome_and_revision():
    good = select_professional_decision(_context())
    bad_outcome = review_decision_process(
        good,
        settled_outcome="NO",
        reviewed_at=NOW + timedelta(days=1),
        settlement_revision=True,
    )
    assert (
        bad_outcome.classification
        == ReviewClassification.GOOD_DECISION_BAD_OUTCOME
    )
    assert bad_outcome.settlement_revision is True
    tampered = replace(
        good,
        pretrade_checklist={
            **good.to_dict()["pretrade_checklist"],
            "CONTRACT": {
                **good.to_dict()["pretrade_checklist"]["CONTRACT"],
                "settlement_truth_complete": False,
            },
        },
    )
    lucky = review_decision_process(
        tampered,
        settled_outcome="YES",
        reviewed_at=NOW + timedelta(days=1),
        rules_bypassed=("CONTRACT_CHECK",),
    )
    assert (
        lucky.classification
        == ReviewClassification.BAD_DECISION_GOOD_OUTCOME
    )
    assert good.action == TraderAction.BUY_YES


def test_only_material_actions_generate_alerts():
    buy = select_professional_decision(_context())
    assert material_action_alert(buy) == "TRADE_READY"
    watch = select_professional_decision(
        _context(market=replace(_market(), spread=Decimal("0.20")))
    )
    assert material_action_alert(watch) is None
    stale = select_professional_decision(
        _context(weather=replace(_weather(), stale=True))
    )
    assert material_action_alert(stale) == "DATA_STALE"


def test_material_action_alert_payload_is_json_serializable():
    snapshot = select_professional_decision(_context())

    payload = _action_alert_payload(snapshot)

    assert payload["action"] == "BUY_YES"
    assert payload["prospective_paper_only"] is True
    assert json.loads(json.dumps(payload))["decision_id"] == snapshot.decision_id


def test_new_professional_policy_archives_the_prior_immutable_manifest(
    tmp_path,
):
    path = tmp_path / "strategy_freeze_manifest.json"
    original = replace(
        frozen_professional_strategy(frozen_at=NOW, code_hash="old-hash"),
        decision_policy_version="professional-trader-v1-2026-07-24",
    )
    persist_professional_freeze(path, original)
    replacement = frozen_professional_strategy(
        frozen_at=NOW + timedelta(minutes=1),
        code_hash="new-hash",
    )

    persist_professional_freeze(path, replacement)

    archive = tmp_path / (
        "strategy_freeze_manifest."
        "professional-trader-v1-2026-07-24.json"
    )
    assert json.loads(archive.read_text())["freeze"]["code_config_hash"] == (
        "old-hash"
    )
    assert json.loads(path.read_text())["freeze"][
        "decision_policy_version"
    ] == replacement.decision_policy_version


def test_same_professional_policy_cannot_change_its_frozen_hash(tmp_path):
    path = tmp_path / "strategy_freeze_manifest.json"
    freeze = frozen_professional_strategy(frozen_at=NOW, code_hash="old-hash")
    persist_professional_freeze(path, freeze)

    with pytest.raises(ValueError, match="immutable"):
        persist_professional_freeze(
            path,
            replace(freeze, code_config_hash="changed-hash"),
        )


def test_schema_defines_append_only_professional_history():
    schema = Path("db/schema.sql").read_text()
    for table in (
        "professional_contract_truth",
        "professional_information_events",
        "professional_information_reactions",
        "professional_decision_snapshots",
        "professional_journal_events",
        "professional_post_trade_reviews",
        "professional_action_alerts",
    ):
        assert f"{table}_append_only" in schema


def test_market_detail_template_has_required_compact_professional_sections():
    template = Path(
        "dashboard/templates/_alert_card.html"
    ).read_text()
    for label in (
        "Professional trader view",
        "Why this decision?",
        "What would change the decision?",
        "Market and weather timeline",
        "Position history",
        "Post-trade review",
    ):
        assert label in template
