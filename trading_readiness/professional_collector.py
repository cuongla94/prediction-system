from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Callable, Mapping

from psycopg.types.json import Jsonb

from kalshi_client import MarketOrderbook, OrderbookLevel
from live_trading.repository import PostgresLiveRepository
from live_trading.risk import estimated_taker_fee, validate_fixed_limits
from weather.stations import STATIONS

from .professional import (
    AccountState,
    DecisionContext,
    InformationEvent,
    JournalEvent,
    PositionState,
    PostTradeReview,
    ThesisType,
    TraderAction,
    TraderDecisionSnapshot,
    build_contract_truth,
    build_market_state,
    build_weather_state,
    detect_information_events,
    make_journal_event,
    material_action_alert,
    review_decision_process,
    select_professional_decision,
    snapshot_from_dict,
)
from .professional_freeze import ProfessionalStrategyFreeze


def _json_value(value: Any) -> Jsonb:
    return Jsonb(value)


def _action_alert_payload(
    snapshot: TraderDecisionSnapshot,
) -> dict[str, Any]:
    """Return a plain JSON-safe view of an immutable decision snapshot."""
    value = snapshot.to_dict()
    action = snapshot.action
    execution = value["execution"]
    executable_price = execution["executable_exit_price"]
    if action != TraderAction.EXIT:
        executable_price = (
            execution["buy_yes_price"]
            if action in {TraderAction.BUY_YES, TraderAction.REBUY_YES}
            else execution["buy_no_price"]
        )
    return {
        "market": value["market_ticker"],
        "action": value["action"],
        "thesis": value["thesis"]["summary"],
        "new_information": value["thesis"]["new_information"],
        "fair_probability": value["probability"][
            "final_working_yes_probability"
        ],
        "executable_price": executable_price,
        "net_edge": value["net_edge_after_costs"],
        "maximum_loss": value["maximum_loss"],
        "blockers": value["blockers"],
        "reason_code": value["decision_reason_code"],
        "decision_id": value["decision_id"],
        "prospective_paper_only": not value["production_order_allowed"],
    }


def _depth_orderbook(ticker: str, value: Mapping[str, Any]) -> MarketOrderbook:
    def levels(name: str) -> tuple[OrderbookLevel, ...]:
        return tuple(
            OrderbookLevel(
                price=Decimal(str(row["price"])),
                quantity=Decimal(str(row["quantity"])),
            )
            for row in value.get(name, [])
        )

    return MarketOrderbook(
        ticker=ticker,
        yes_bids=levels("yes_bids"),
        no_bids=levels("no_bids"),
        raw=dict(value),
    )


class PostgresProfessionalRepository:
    """Append-only adapter over the existing alerts/forward/live tables."""

    def __init__(self, connection: Any):
        self.connection = connection

    def insert_freeze(self, freeze: ProfessionalStrategyFreeze) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select base_strategy_name, base_strategy_version, "
                "probability_method_version, policy_config, code_config_hash "
                "from professional_strategy_freezes "
                "where decision_policy_version = %s",
                (freeze.decision_policy_version,),
            )
            existing = cursor.fetchone()
            expected = (
                freeze.base_strategy_name,
                freeze.base_strategy_version,
                freeze.probability_method_version,
                freeze.policy_config,
                freeze.code_config_hash,
            )
            if existing is not None:
                normalized = (
                    existing[0],
                    existing[1],
                    existing[2],
                    existing[3],
                    existing[4],
                )
                if normalized != expected:
                    raise ValueError(
                        "Deployed professional strategy freeze is immutable."
                    )
                return
            cursor.execute(
                "insert into professional_strategy_freezes "
                "(base_strategy_name, base_strategy_version, "
                "decision_policy_version, probability_method_version, "
                "policy_config, code_config_hash, frozen_at, "
                "forward_period_start, automatic_promotion_allowed) "
                "values (%s, %s, %s, %s, %s, %s, %s, %s, false)",
                (
                    freeze.base_strategy_name,
                    freeze.base_strategy_version,
                    freeze.decision_policy_version,
                    freeze.probability_method_version,
                    _json_value(dict(freeze.policy_config)),
                    freeze.code_config_hash,
                    freeze.frozen_at,
                    freeze.forward_period_start,
                ),
            )

    def pending_inputs(self) -> list[dict[str, Any]]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select "
                "d.id as forward_evidence_decision_id, d.decision_id as "
                "forward_decision_id, d.event_ticker, d.market_ticker, d.city, "
                "d.target_date, d.strategy_version, d.candidate_version, "
                "d.forecast_model, d.forecast_run_time, "
                "d.forecast_availability_time, d.forecast_values, "
                "d.observation_event_time, d.observation_publication_time, "
                "d.observation_collector_received_time, "
                "d.observed_high_at_decision as observed_so_far, "
                "d.model_probability, d.market_probability, "
                "d.final_candidate_probability, d.selected_side, "
                "d.maximum_acceptable_price, d.fee_adjusted_edge, "
                "d.rejection_reason, d.intended_quantity, d.decision_time, "
                "o.id as orderbook_snapshot_id, o.source_publish_time as "
                "quote_source_time, o.collector_received_time as "
                "quote_receipt_time, o.depth_levels, o.last_trade, "
                "o.market_status, o.volume, o.open_interest, "
                "a.id as related_alert_id, a.series_ticker, a.floor_strike, "
                "a.cap_strike, a.metric, a.rules_primary, a.rules_secondary, "
                "a.close_time, a.ensemble_mean, a.ensemble_std, a.lead_days, "
                "f.strategy_name, f.signal_threshold, "
                "pe.event_type as paper_event_type, "
                "pe.filled_quantity as paper_filled_quantity, "
                "pe.weighted_fill_price as paper_fill_price, "
                "pe.estimated_fee as paper_estimated_fee "
                "from forward_evidence_decisions d "
                "join forward_orderbook_snapshots o "
                "on o.id = d.orderbook_snapshot_id "
                "join forward_candidate_freezes f "
                "on f.strategy_version = d.candidate_version "
                "join lateral (select a1.* from alerts a1 "
                "where a1.market_ticker = d.market_ticker "
                "and a1.created_at <= d.decision_time "
                "order by a1.created_at desc limit 1) a on true "
                "left join lateral (select pe1.* "
                "from forward_paper_order_events pe1 "
                "where pe1.decision_row_id = d.id "
                "order by pe1.created_at desc limit 1) pe on true "
                "where not exists ("
                "select 1 from professional_decision_snapshots p "
                "where p.forward_evidence_decision_id = d.id) "
                "order by d.decision_time, d.id"
            )
            columns = [item.name for item in cursor.description]
            return [
                dict(zip(columns, row, strict=True))
                for row in cursor.fetchall()
            ]

    def latest_basis(
        self, market_ticker: str, candidate_version: str
    ) -> dict[str, Any] | None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select decision_id, triggering_information_event_id, action, "
                "decision_reason_code, weather_state, market_state, thesis "
                "from professional_decision_snapshots "
                "where market_ticker = %s and candidate_version = %s "
                "order by decision_time desc, id desc limit 1",
                (market_ticker, candidate_version),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            return {
                "decision_id": row[0],
                "triggering_information_event_id": row[1],
                "action": row[2],
                "decision_reason_code": row[3],
                "weather_state": row[4],
                "market_state": row[5],
                "thesis": row[6],
            }

    def position_state(
        self, market_ticker: str, candidate_version: str
    ) -> PositionState:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select p.decision_id, p.action, p.decision_reason_code, "
                "p.triggering_information_event_id, p.thesis, "
                "coalesce(pe.event_type, ''), "
                "coalesce(pe.filled_quantity, 0) "
                "from professional_decision_snapshots p "
                "left join lateral (select pe1.* "
                "from forward_paper_order_events pe1 "
                "where pe1.decision_row_id = p.forward_evidence_decision_id "
                "order by pe1.created_at desc limit 1) pe on true "
                "where p.market_ticker = %s and p.candidate_version = %s "
                "order by p.decision_time desc, p.id desc limit 25",
                (market_ticker, candidate_version),
            )
            rows = cursor.fetchall()
        if not rows:
            return PositionState()
        latest_exit = next(
            (row for row in rows if row[1] == TraderAction.EXIT.value),
            None,
        )
        latest_entry = next(
            (
                row
                for row in rows
                if row[1]
                in {
                    TraderAction.BUY_YES.value,
                    TraderAction.BUY_NO.value,
                    TraderAction.REBUY_YES.value,
                    TraderAction.REBUY_NO.value,
                }
            ),
            None,
        )
        if latest_entry is None:
            return PositionState()
        entry_index = rows.index(latest_entry)
        exit_index = rows.index(latest_exit) if latest_exit else None
        entry_after_exit = exit_index is None or entry_index < exit_index
        paper_status = latest_entry[5]
        filled = Decimal(str(latest_entry[6]))
        if (
            entry_after_exit
            and paper_status in {"FILLED", "PARTIAL_FILL"}
            and filled > 0
        ):
            side = (
                "YES"
                if latest_entry[1]
                in {
                    TraderAction.BUY_YES.value,
                    TraderAction.REBUY_YES.value,
                }
                else "NO"
            )
            return PositionState(
                side=side,
                contracts=filled,
                original_thesis=ThesisType(
                    latest_entry[4]["classification"]
                ),
                entry_information_event_id=latest_entry[3],
                prior_entry_decision_id=latest_entry[0],
            )
        return PositionState(
            entry_information_event_id=latest_entry[3],
            prior_entry_decision_id=latest_entry[0],
            prior_exit_decision_id=latest_exit[0] if latest_exit else None,
            prior_exit_reason=latest_exit[2] if latest_exit else None,
            prior_exit_information_event_id=(
                latest_exit[3] if latest_exit else None
            ),
        )

    def bot_account_metrics(
        self, market_ticker: str, event_ticker: str
    ) -> dict[str, Any]:
        from kalshi_client import parse_event_date

        with self.connection.cursor() as cursor:
            cursor.execute(
                "select "
                "coalesce(sum(case when market_ticker = %s and status in "
                "('RESTING','PARTIAL','FILLED') then "
                "greatest(remaining_count, 0) * submitted_yes_price else 0 end), 0), "
                "coalesce(sum(case when event_ticker = %s and status in "
                "('RESTING','PARTIAL','FILLED') then "
                "greatest(remaining_count, 0) * submitted_yes_price else 0 end), 0), "
                "coalesce(sum(case when status in "
                "('RESTING','PARTIAL','FILLED') then "
                "greatest(remaining_count, 0) * submitted_yes_price else 0 end), 0), "
                "count(*) filter (where market_ticker = %s and status in "
                "('PENDING','SUBMITTING','UNKNOWN','RESTING','PARTIAL')), "
                "bool_or(status = 'UNKNOWN') "
                "from live_orders",
                (market_ticker, event_ticker, market_ticker),
            )
            row = cursor.fetchone()
            cursor.execute(
                "select available_cash, healthy "
                "from live_reconciliation_runs "
                "order by finished_at desc limit 1"
            )
            reconciliation = cursor.fetchone()
            cursor.execute(
                "select kill_switch from bot_control_events "
                "order by created_at desc limit 1"
            )
            kill_switch_row = cursor.fetchone()
        risk_state = PostgresLiveRepository(self.connection).risk_state(
            market_ticker,
            event_ticker,
            parse_event_date(event_ticker),
        )
        return {
            "market_exposure": risk_state.market_exposure,
            "event_exposure": risk_state.event_exposure,
            "total_exposure": risk_state.total_exposure,
            "open_bot_orders": int(row[3] or 0),
            "has_unknown_order": risk_state.has_unknown_order,
            "available_cash": (
                Decimal(str(reconciliation[0]))
                if reconciliation and reconciliation[0] is not None
                else None
            ),
            "reconciliation_healthy": (
                risk_state.reconciliation_healthy
                and bool(reconciliation and reconciliation[1])
            ),
            "daily_realized_pnl": risk_state.daily_realized_pnl,
            "daily_mark_to_market_pnl": (
                risk_state.daily_mark_to_market_pnl
            ),
            "consecutive_settled_losses": (
                risk_state.consecutive_settled_losses
            ),
            "risk_state": risk_state,
            "kill_switch": bool(
                kill_switch_row and kill_switch_row[0]
            ),
        }

    def append_contract_truth(
        self,
        *,
        truth: Any,
        decision_policy_version: str,
        source_payload: Mapping[str, Any],
        source_collected_at: datetime,
    ) -> str:
        canonical = json.dumps(
            truth.to_dict(), sort_keys=True, separators=(",", ":")
        )
        truth_id = hashlib.sha256(canonical.encode()).hexdigest()
        with self.connection.cursor() as cursor:
            cursor.execute(
                "insert into professional_contract_truth "
                "(contract_truth_id, event_ticker, market_ticker, "
                "decision_policy_version, status, truth, "
                "source_market_payload, source_collected_at) "
                "values (%s, %s, %s, %s, %s, %s, %s, %s) "
                "on conflict (contract_truth_id) do nothing",
                (
                    truth_id,
                    truth.event_ticker,
                    truth.market_ticker,
                    decision_policy_version,
                    truth.status,
                    _json_value(truth.to_dict()),
                    _json_value(dict(source_payload)),
                    source_collected_at,
                ),
            )
        return truth_id

    def append_information_event(self, event: InformationEvent) -> bool:
        value = event.to_dict()
        with self.connection.cursor() as cursor:
            cursor.execute(
                "insert into professional_information_events "
                "(information_event_id, event_type, source, source_event_time, "
                "source_publication_time, collector_receipt_time, "
                "processing_time, event_ticker, market_ticker, previous_value, "
                "new_value, material, related_decision_id) "
                "values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "on conflict (information_event_id) do nothing",
                (
                    event.information_event_id,
                    event.event_type.value,
                    event.source,
                    event.source_event_time,
                    event.source_publication_time,
                    event.collector_receipt_time,
                    event.processing_time,
                    event.event_ticker,
                    event.market_ticker,
                    _json_value(value["previous_value"]),
                    _json_value(value["new_value"]),
                    event.material,
                    event.related_decision_id,
                ),
            )
            return cursor.rowcount == 1

    def append_snapshot(
        self,
        snapshot: TraderDecisionSnapshot,
        *,
        related_alert_id: int,
        forward_evidence_decision_id: int,
        contract_truth_id: str,
        decision_policy_version: str,
        contract_truth: Mapping[str, Any],
        weather_state: Mapping[str, Any],
        market_state: Mapping[str, Any],
        account_state: Mapping[str, Any],
    ) -> bool:
        value = snapshot.to_dict()
        with self.connection.cursor() as cursor:
            cursor.execute(
                "insert into professional_decision_snapshots "
                "(decision_id, parent_decision_id, related_alert_id, "
                "forward_evidence_decision_id, contract_truth_id, "
                "triggering_information_event_id, event_ticker, market_ticker, "
                "strategy_name, strategy_version, candidate_version, "
                "decision_policy_version, action, decision_reason_code, "
                "thesis_type, net_edge_after_costs, maximum_loss, "
                "confidence_level, blockers, contract_truth, weather_state, "
                "market_state, account_state, information_as_of, probability, "
                "execution, thesis, pretrade_checklist, snapshot, "
                "production_order_allowed, next_review_trigger, decision_time) "
                "values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
                "%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
                "%s, %s, %s, %s, %s, %s) "
                "on conflict (decision_id) do nothing",
                (
                    snapshot.decision_id,
                    snapshot.parent_decision_id,
                    related_alert_id,
                    forward_evidence_decision_id,
                    contract_truth_id,
                    snapshot.triggering_information_event_id,
                    snapshot.event_ticker,
                    snapshot.market_ticker,
                    snapshot.strategy_name,
                    snapshot.strategy_version,
                    snapshot.candidate_version,
                    decision_policy_version,
                    snapshot.action.value,
                    snapshot.decision_reason_code,
                    snapshot.thesis["classification"],
                    snapshot.net_edge_after_costs,
                    snapshot.maximum_loss,
                    snapshot.confidence_level,
                    _json_value(list(snapshot.blockers)),
                    _json_value(dict(contract_truth)),
                    _json_value(dict(weather_state)),
                    _json_value(dict(market_state)),
                    _json_value(dict(account_state)),
                    _json_value(value["information_as_of"]),
                    _json_value(value["probability"]),
                    _json_value(value["execution"]),
                    _json_value(value["thesis"]),
                    _json_value(value["pretrade_checklist"]),
                    _json_value(value),
                    snapshot.production_order_allowed,
                    snapshot.next_review_trigger,
                    snapshot.decision_time,
                ),
            )
            return cursor.rowcount == 1

    def append_journal(self, event: JournalEvent) -> bool:
        value = event.to_dict()
        with self.connection.cursor() as cursor:
            cursor.execute(
                "insert into professional_journal_events "
                "(journal_event_id, event_ticker, market_ticker, "
                "candidate_version, record_type, record_id, "
                "parent_record_type, parent_record_id, event_time, "
                "source_publication_time, collector_receipt_time, "
                "processing_time, decision_time, order_time, fill_time, "
                "settlement_time, payload) "
                "values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
                "%s, %s, %s, %s, %s) "
                "on conflict (journal_event_id) do nothing",
                (
                    event.journal_event_id,
                    event.event_ticker,
                    event.market_ticker,
                    event.candidate_version,
                    event.record_type,
                    event.record_id,
                    event.parent_record_type,
                    event.parent_record_id,
                    event.event_time,
                    event.source_publication_time,
                    event.collector_receipt_time,
                    event.processing_time,
                    event.decision_time,
                    event.order_time,
                    event.fill_time,
                    event.settlement_time,
                    _json_value(value["payload"]),
                ),
            )
            return cursor.rowcount == 1

    def append_action_alert(
        self, alert_type: str, snapshot: TraderDecisionSnapshot
    ) -> bool:
        payload = _action_alert_payload(snapshot)
        alert_id = hashlib.sha256(
            f"{alert_type}|{snapshot.decision_id}".encode()
        ).hexdigest()
        with self.connection.cursor() as cursor:
            cursor.execute(
                "insert into professional_action_alerts "
                "(alert_id, alert_type, decision_id, market_ticker, action, "
                "payload) values (%s, %s, %s, %s, %s, %s) "
                "on conflict (alert_id) do nothing",
                (
                    alert_id,
                    alert_type,
                    snapshot.decision_id,
                    snapshot.market_ticker,
                    snapshot.action.value,
                    _json_value(payload),
                ),
            )
            return cursor.rowcount == 1

    def unreviewed_settlements(self) -> list[dict[str, Any]]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select p.snapshot, pe.settlement_result, pe.estimated_fee, "
                "pe.weighted_fill_price, pe.created_at "
                "from professional_decision_snapshots p "
                "join lateral (select pe1.* "
                "from forward_paper_order_events pe1 "
                "where pe1.decision_row_id = p.forward_evidence_decision_id "
                "and pe1.event_type in ('SETTLED_WIN','SETTLED_LOSS','VOID') "
                "order by pe1.created_at desc limit 1) pe on true "
                "where p.action in ('BUY_YES','BUY_NO','REBUY_YES','REBUY_NO') "
                "and not exists (select 1 "
                "from professional_post_trade_reviews r "
                "where r.decision_id = p.decision_id)"
            )
            columns = [item.name for item in cursor.description]
            return [
                dict(zip(columns, row, strict=True))
                for row in cursor.fetchall()
            ]

    def append_review(self, review: PostTradeReview) -> bool:
        value = review.to_dict()
        with self.connection.cursor() as cursor:
            cursor.execute(
                "insert into professional_post_trade_reviews "
                "(review_id, decision_id, market_ticker, classification, "
                "settled_outcome, process_correct, outcome_favorable, "
                "settlement_revision, review, reviewed_at) "
                "values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "on conflict (review_id) do nothing",
                (
                    review.review_id,
                    review.decision_id,
                    review.market_ticker,
                    review.classification.value,
                    review.settled_outcome,
                    review.process_correct,
                    review.outcome_favorable,
                    review.settlement_revision,
                    _json_value(value),
                    review.reviewed_at,
                ),
            )
            return cursor.rowcount == 1

    def capture_due_reactions(self, now: datetime) -> int:
        samples = (
            ("BEFORE_SOURCE_PUBLICATION", -1),
            ("AT_COLLECTOR_RECEIPT", 0),
            ("AFTER_10_SECONDS", 10),
            ("AFTER_30_SECONDS", 30),
            ("AFTER_1_MINUTE", 60),
            ("AFTER_5_MINUTES", 300),
            ("AFTER_15_MINUTES", 900),
        )
        inserted = 0
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select information_event_id, market_ticker, "
                "source_publication_time, collector_receipt_time "
                "from professional_information_events where material"
            )
            events = cursor.fetchall()
            for event_id, ticker, published, received in events:
                for label, offset in samples:
                    target = (
                        published
                        if offset < 0 and published is not None
                        else received + timedelta(seconds=max(0, offset))
                    )
                    if target is None or target > now:
                        continue
                    cursor.execute(
                        "select 1 from professional_information_reactions "
                        "where information_event_id = %s and sample_label = %s",
                        (event_id, label),
                    )
                    if cursor.fetchone() is not None:
                        continue
                    comparison = "<=" if offset < 0 else ">="
                    ordering = "desc" if offset < 0 else "asc"
                    cursor.execute(
                        "select id, collector_received_time, best_yes_ask, "
                        "best_no_ask, spread, depth_levels "
                        "from forward_orderbook_snapshots "
                        f"where market_ticker = %s and collector_received_time {comparison} %s "
                        f"order by collector_received_time {ordering} limit 1",
                        (ticker, target),
                    )
                    snapshot = cursor.fetchone()
                    if snapshot:
                        depth = snapshot[5] or {}
                        yes_depth = sum(
                            Decimal(str(item["quantity"]))
                            for item in depth.get("no_bids", [])
                        )
                        no_depth = sum(
                            Decimal(str(item["quantity"]))
                            for item in depth.get("yes_bids", [])
                        )
                        values = (
                            snapshot[0],
                            snapshot[1],
                            snapshot[2],
                            snapshot[3],
                            snapshot[4],
                            yes_depth,
                            no_depth,
                            bool(
                                snapshot[2] is not None
                                and snapshot[3] is not None
                                and yes_depth > 0
                                and no_depth > 0
                            ),
                        )
                    else:
                        values = (None, None, None, None, None, None, None, False)
                    cursor.execute(
                        "insert into professional_information_reactions "
                        "(information_event_id, sample_label, target_time, "
                        "orderbook_snapshot_id, observed_at, "
                        "executable_yes_price, executable_no_price, spread, "
                        "yes_depth, no_depth, "
                        "hypothetical_limit_fill_supported) "
                        "values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                        "on conflict (information_event_id, sample_label) do nothing",
                        (event_id, label, target, *values),
                    )
                    inserted += cursor.rowcount
        return inserted


class ProfessionalJournalCollector:
    """SEE → HEAR → THINK → ACT → REVIEW over existing forward evidence."""

    def __init__(
        self,
        *,
        client: Any,
        repository: Any,
        freeze: ProfessionalStrategyFreeze,
        now: Callable[[], datetime] | None = None,
    ):
        self.client = client
        self.repository = repository
        self.freeze = freeze
        self.now = now or (lambda: datetime.now(UTC))

    def _account_states(self) -> tuple[dict[str, Any], Decimal | None, Decimal]:
        try:
            positions = self.client.get_positions()
        except Exception:
            positions = []
        try:
            cash = self.client.get_balance().available_dollars
        except Exception:
            cash = None
        by_ticker = {position.ticker: position for position in positions}
        total_exposure = sum(
            (
                Decimal(str(position.market_exposure_dollars))
                for position in positions
            ),
            Decimal("0"),
        )
        return by_ticker, cash, total_exposure

    def _account_state(
        self,
        row: Mapping[str, Any],
        *,
        market: Any,
        positions: Mapping[str, Any],
        cash: Decimal | None,
        total_account_exposure: Decimal,
    ) -> tuple[AccountState, Any]:
        metrics = self.repository.bot_account_metrics(
            row["market_ticker"], row["event_ticker"]
        )
        position = positions.get(row["market_ticker"])
        position_exposure = (
            Decimal(str(position.market_exposure_dollars))
            if position
            else Decimal("0")
        )
        bot_exposure = metrics["market_exposure"]
        position_side = position.side.upper() if position else None
        exit_price = (
            market.exit_yes_price
            if position_side == "YES"
            else market.exit_no_price
            if position_side == "NO"
            else None
        )
        contracts = (
            Decimal(str(position.contracts))
            if position
            else Decimal("0")
        )
        average_entry = (
            Decimal(str(position.total_traded_dollars)) / contracts
            if position and contracts
            else None
        )
        executable_exit_value = (
            exit_price * contracts if exit_price is not None else None
        )
        account = AccountState(
            current_position_side=position_side,
            average_entry_price=average_entry,
            contracts_held=contracts,
            executable_exit_value=executable_exit_value,
            realized_pnl=(
                Decimal(str(position.realized_pnl_dollars))
                if position
                else Decimal("0")
            ),
            unrealized_pnl=(
                executable_exit_value
                - (average_entry or Decimal("0")) * contracts
                - (
                    Decimal(str(position.fees_paid_dollars))
                    if position
                    else Decimal("0")
                )
                if executable_exit_value is not None
                else Decimal("0")
            ),
            fees_paid=(
                Decimal(str(position.fees_paid_dollars))
                if position
                else Decimal("0")
            ),
            open_bot_owned_orders=metrics["open_bot_orders"],
            available_cash=(
                cash if cash is not None else metrics["available_cash"]
            ),
            bot_market_exposure=bot_exposure,
            manual_market_exposure=max(
                Decimal("0"), position_exposure - bot_exposure
            ),
            event_date_exposure=metrics["event_exposure"],
            total_bot_exposure=metrics["total_exposure"],
            total_account_exposure=total_account_exposure,
            daily_realized_loss=max(
                Decimal("0"), -metrics["daily_realized_pnl"]
            ),
            consecutive_settled_losses=metrics[
                "consecutive_settled_losses"
            ],
            kill_switch=metrics["kill_switch"],
            reconciliation_healthy=metrics["reconciliation_healthy"],
            has_unknown_order=metrics["has_unknown_order"],
            forward_evidence_sufficient=False,
        )
        return account, metrics["risk_state"]

    def _current_basis(
        self,
        row: Mapping[str, Any],
        weather: Any,
        market: Any,
    ) -> dict[str, Any]:
        return {
            "forecast_value": weather.latest_forecast,
            "forecast_value_event_time": weather.forecast_run_time,
            "forecast_value_publication_time": (
                weather.forecast_availability_time
            ),
            "forecast_value_receipt_time": (
                weather.forecast_availability_time
            ),
            "observation_value": weather.latest_official_observation,
            "observation_value_event_time": weather.observation_event_time,
            "observation_value_publication_time": (
                weather.observation_publication_time
            ),
            "observation_value_receipt_time": weather.collector_receipt_time,
            "observed_daily_extreme": weather.observed_daily_extreme,
            "observed_daily_extreme_event_time": (
                weather.observation_event_time
            ),
            "observed_daily_extreme_publication_time": (
                weather.observation_publication_time
            ),
            "observed_daily_extreme_receipt_time": (
                weather.collector_receipt_time
            ),
            "bracket_impossible": weather.bracket_impossible,
            "model_disagreement": weather.model_disagreement,
            "model_disagreement_publication_time": (
                weather.forecast_availability_time
            ),
            "executable_yes_price": market.buy_yes_price,
            "executable_yes_price_event_time": market.quote_source_time,
            "executable_yes_price_publication_time": market.quote_source_time,
            "executable_yes_price_receipt_time": market.quote_receipt_time,
            "available_quantity": market.best_yes_quantity,
            "available_quantity_event_time": market.quote_source_time,
            "available_quantity_receipt_time": market.quote_receipt_time,
            "spread": market.spread,
            "spread_event_time": market.quote_source_time,
            "spread_receipt_time": market.quote_receipt_time,
            "market_status": market.market_status,
            "market_status_event_time": market.quote_source_time,
            "market_status_receipt_time": market.quote_receipt_time,
            "metric": row["metric"],
            "collector_receipt_time": market.quote_receipt_time,
        }

    def collect(self) -> dict[str, int]:
        self.repository.insert_freeze(self.freeze)
        positions, cash, total_exposure = self._account_states()
        try:
            exchange = self.client.get_exchange_status()
            exchange_active = bool(exchange.exchange_active)
            exchange_trading_active = bool(exchange.trading_active)
        except Exception:
            exchange_active = False
            exchange_trading_active = False
        counters = {
            "inputs": 0,
            "contract_truth_records": 0,
            "information_events": 0,
            "material_information_events": 0,
            "decision_snapshots": 0,
            "watch": 0,
            "do_not_trade": 0,
            "buy_yes": 0,
            "buy_no": 0,
            "hold": 0,
            "exit": 0,
            "rebuy": 0,
            "journal_events": 0,
            "alerts": 0,
            "post_trade_reviews": 0,
            "reaction_samples": 0,
        }
        for row in self.repository.pending_inputs():
            counters["inputs"] += 1
            station = STATIONS.get(row["series_ticker"])
            contract = build_contract_truth(row, station=station, market=None)
            previous = self.repository.latest_basis(
                row["market_ticker"], row["candidate_version"]
            )
            previous_weather = previous["weather_state"] if previous else None
            weather = build_weather_state(
                row,
                contract,
                decision_time=row["decision_time"],
                previous=previous_weather,
            )
            book = _depth_orderbook(row["market_ticker"], row["depth_levels"])
            working_yes = Decimal(
                str(
                    row["final_candidate_probability"]
                    if row["final_candidate_probability"] is not None
                    else row["model_probability"]
                )
            )
            if weather.bracket_impossible:
                working_yes = Decimal("0")
            threshold = Decimal(str(row["signal_threshold"]))
            market = build_market_state(
                book,
                source_time=row["quote_source_time"],
                receipt_time=row["quote_receipt_time"],
                decision_time=row["decision_time"],
                close_time=row["close_time"],
                market_status=row["market_status"],
                maximum_yes_price=max(
                    Decimal("0"), working_yes - threshold
                ),
                maximum_no_price=max(
                    Decimal("0"), Decimal("1") - working_yes - threshold
                ),
                last_trade=(
                    Decimal(str(row["last_trade"]))
                    if row["last_trade"] is not None
                    else None
                ),
                previous_yes_price=(
                    Decimal(
                        str(
                            previous["market_state"].get("buy_yes_price")
                        )
                    )
                    if previous
                    and previous["market_state"].get("buy_yes_price")
                    is not None
                    else None
                ),
                volume=(
                    Decimal(str(row["volume"]))
                    if row["volume"] is not None
                    else None
                ),
                open_interest=(
                    Decimal(str(row["open_interest"]))
                    if row["open_interest"] is not None
                    else None
                ),
                exchange_active=exchange_active,
                exchange_trading_active=exchange_trading_active,
            )
            account, risk_state = self._account_state(
                row,
                market=market,
                positions=positions,
                cash=cash,
                total_account_exposure=total_exposure,
            )
            contract_truth_id = self.repository.append_contract_truth(
                truth=contract,
                decision_policy_version=self.freeze.decision_policy_version,
                source_payload={
                    "rules_primary": row["rules_primary"],
                    "rules_secondary": row["rules_secondary"],
                    "orderbook_snapshot_id": row["orderbook_snapshot_id"],
                },
                source_collected_at=row["quote_receipt_time"],
            )
            counters["contract_truth_records"] += 1
            current_basis = self._current_basis(row, weather, market)
            previous_basis = None
            if previous:
                previous_basis = {
                    "forecast_value": previous_weather.get("latest_forecast"),
                    "observation_value": previous_weather.get(
                        "latest_official_observation"
                    ),
                    "observed_daily_extreme": previous_weather.get(
                        "observed_daily_extreme"
                    ),
                    "bracket_impossible": previous_weather.get(
                        "bracket_impossible"
                    ),
                    "model_disagreement": previous_weather.get(
                        "model_disagreement"
                    ),
                    "executable_yes_price": previous["market_state"].get(
                        "buy_yes_price"
                    ),
                    "available_quantity": previous["market_state"].get(
                        "best_yes_quantity"
                    ),
                    "spread": previous["market_state"].get("spread"),
                    "market_status": previous["market_state"].get(
                        "market_status"
                    ),
                }
            events = detect_information_events(
                previous=previous_basis,
                current=current_basis,
                event_ticker=row["event_ticker"],
                market_ticker=row["market_ticker"],
                processing_time=row["decision_time"],
            )
            for event in (item for item in events if not item.material):
                if self.repository.append_information_event(event):
                    counters["information_events"] += 1
                    journal = make_journal_event(
                        event_ticker=row["event_ticker"],
                        market_ticker=row["market_ticker"],
                        candidate_version=row["candidate_version"],
                        record_type="INFORMATION_EVENT",
                        record_id=event.information_event_id,
                        processing_time=row["decision_time"],
                        event_time=event.source_event_time,
                        source_publication_time=event.source_publication_time,
                        collector_receipt_time=event.collector_receipt_time,
                        payload=event.to_dict(),
                    )
                    counters["journal_events"] += int(
                        self.repository.append_journal(journal)
                    )
            material_events = [event for event in events if event.material]
            triggers: list[InformationEvent | None] = material_events
            if not triggers:
                triggers = [None]
            parent_id = previous["decision_id"] if previous else None
            position = self.repository.position_state(
                row["market_ticker"], row["candidate_version"]
            )
            for trigger in triggers:
                desired_side = (
                    "YES"
                    if working_yes
                    - Decimal(
                        str(
                            row["market_probability"]
                            if row["market_probability"] is not None
                            else "0.5"
                        )
                    )
                    >= 0
                    else "NO"
                )
                proposed_price = (
                    market.buy_yes_price
                    if desired_side == "YES"
                    else market.buy_no_price
                )
                risk_blockers: tuple[str, ...] = ()
                if proposed_price is not None and not position.is_open:
                    verdict = validate_fixed_limits(
                        count=Decimal("1"),
                        outcome_price=proposed_price,
                        estimated_fees=estimated_taker_fee(
                            proposed_price, Decimal("1")
                        ),
                        state=risk_state,
                    )
                    risk_blockers = verdict.blockers
                context = DecisionContext(
                    contract=contract,
                    weather=weather,
                    market=market,
                    account=account,
                    strategy_name=row["strategy_name"],
                    strategy_version=row["strategy_version"],
                    candidate_version=row["candidate_version"],
                    decision_time=row["decision_time"],
                    model_yes_probability=Decimal(
                        str(row["model_probability"])
                    ),
                    market_yes_probability=Decimal(
                        str(
                            row["market_probability"]
                            if row["market_probability"] is not None
                            else "0.5"
                        )
                    ),
                    working_yes_probability=working_yes,
                    uncertainty_indicator="FORWARD_EVIDENCE_INSUFFICIENT",
                    confidence_level="LOW",
                    probability_method_version=(
                        self.freeze.probability_method_version
                    ),
                    triggering_event=trigger,
                    signal_first_appeared=previous is None,
                    parent_decision_id=parent_id,
                    position=position,
                    risk_blockers=risk_blockers,
                    prospective_paper_only=True,
                )
                snapshot = select_professional_decision(context)
                linked_trigger = trigger
                if trigger is not None:
                    linked_trigger = replace(
                        trigger,
                        related_decision_id=snapshot.decision_id,
                    )
                    if self.repository.append_information_event(
                        linked_trigger
                    ):
                        counters["information_events"] += 1
                        counters["material_information_events"] += 1
                        information_journal = make_journal_event(
                            event_ticker=row["event_ticker"],
                            market_ticker=row["market_ticker"],
                            candidate_version=row["candidate_version"],
                            record_type="INFORMATION_EVENT",
                            record_id=linked_trigger.information_event_id,
                            processing_time=row["decision_time"],
                            event_time=linked_trigger.source_event_time,
                            source_publication_time=(
                                linked_trigger.source_publication_time
                            ),
                            collector_receipt_time=(
                                linked_trigger.collector_receipt_time
                            ),
                            payload=linked_trigger.to_dict(),
                        )
                        counters["journal_events"] += int(
                            self.repository.append_journal(
                                information_journal
                            )
                        )
                inserted = self.repository.append_snapshot(
                    snapshot,
                    related_alert_id=row["related_alert_id"],
                    forward_evidence_decision_id=row[
                        "forward_evidence_decision_id"
                    ],
                    contract_truth_id=contract_truth_id,
                    decision_policy_version=(
                        self.freeze.decision_policy_version
                    ),
                    contract_truth=contract.to_dict(),
                    weather_state=weather.to_dict(),
                    market_state=market.to_dict(),
                    account_state=account.to_dict(),
                )
                if not inserted:
                    continue
                counters["decision_snapshots"] += 1
                key = snapshot.action.value.lower()
                if key in counters:
                    counters[key] += 1
                elif snapshot.action in {
                    TraderAction.REBUY_YES,
                    TraderAction.REBUY_NO,
                }:
                    counters["rebuy"] += 1
                decision_journal = make_journal_event(
                    event_ticker=row["event_ticker"],
                    market_ticker=row["market_ticker"],
                    candidate_version=row["candidate_version"],
                    record_type="DECISION_SNAPSHOT",
                    record_id=snapshot.decision_id,
                    parent_record_type=(
                        "INFORMATION_EVENT" if trigger else None
                    ),
                    parent_record_id=(
                        linked_trigger.information_event_id
                        if linked_trigger
                        else None
                    ),
                    processing_time=row["decision_time"],
                    decision_time=row["decision_time"],
                    payload=snapshot.to_dict(),
                )
                counters["journal_events"] += int(
                    self.repository.append_journal(decision_journal)
                )
                action_record_type = {
                    TraderAction.HOLD: "POSITION_REVIEW",
                    TraderAction.EXIT: "EXIT",
                    TraderAction.REBUY_YES: "REENTRY",
                    TraderAction.REBUY_NO: "REENTRY",
                }.get(snapshot.action)
                if action_record_type:
                    action_event = make_journal_event(
                        event_ticker=row["event_ticker"],
                        market_ticker=row["market_ticker"],
                        candidate_version=row["candidate_version"],
                        record_type=action_record_type,
                        record_id=hashlib.sha256(
                            (
                                f"{action_record_type}|"
                                f"{snapshot.decision_id}"
                            ).encode()
                        ).hexdigest(),
                        parent_record_type="DECISION_SNAPSHOT",
                        parent_record_id=snapshot.decision_id,
                        processing_time=row["decision_time"],
                        decision_time=row["decision_time"],
                        payload=snapshot.to_dict(),
                    )
                    counters["journal_events"] += int(
                        self.repository.append_journal(action_event)
                    )
                if snapshot.action in {
                    TraderAction.BUY_YES,
                    TraderAction.BUY_NO,
                    TraderAction.REBUY_YES,
                    TraderAction.REBUY_NO,
                }:
                    intended_id = hashlib.sha256(
                        f"paper-intent|{snapshot.decision_id}".encode()
                    ).hexdigest()
                    intended = make_journal_event(
                        event_ticker=row["event_ticker"],
                        market_ticker=row["market_ticker"],
                        candidate_version=row["candidate_version"],
                        record_type="INTENDED_ORDER",
                        record_id=intended_id,
                        parent_record_type="DECISION_SNAPSHOT",
                        parent_record_id=snapshot.decision_id,
                        processing_time=row["decision_time"],
                        decision_time=row["decision_time"],
                        order_time=row["decision_time"],
                        payload={
                            "scope": "PROSPECTIVE_PAPER",
                            "action": snapshot.action.value,
                            "maximum_acceptable_price": snapshot.execution[
                                "maximum_acceptable_entry_price"
                            ],
                            "quantity": snapshot.execution["order_quantity"],
                        },
                    )
                    counters["journal_events"] += int(
                        self.repository.append_journal(intended)
                    )
                    paper_status = row["paper_event_type"]
                    if paper_status:
                        actual_order_id = hashlib.sha256(
                            f"paper-actual|{snapshot.decision_id}".encode()
                        ).hexdigest()
                        actual_order = make_journal_event(
                            event_ticker=row["event_ticker"],
                            market_ticker=row["market_ticker"],
                            candidate_version=row["candidate_version"],
                            record_type="ACTUAL_ORDER",
                            record_id=actual_order_id,
                            parent_record_type="INTENDED_ORDER",
                            parent_record_id=intended_id,
                            processing_time=row["decision_time"],
                            decision_time=row["decision_time"],
                            order_time=row["decision_time"],
                            payload={
                                "scope": "SIMULATED",
                                "status": paper_status,
                                "requested_quantity": (
                                    snapshot.execution["order_quantity"]
                                ),
                            },
                        )
                        counters["journal_events"] += int(
                            self.repository.append_journal(actual_order)
                        )
                    if paper_status in {"FILLED", "PARTIAL_FILL"}:
                        fill_id = hashlib.sha256(
                            f"paper-fill|{snapshot.decision_id}".encode()
                        ).hexdigest()
                        fill = make_journal_event(
                            event_ticker=row["event_ticker"],
                            market_ticker=row["market_ticker"],
                            candidate_version=row["candidate_version"],
                            record_type="FILL",
                            record_id=fill_id,
                            parent_record_type="ACTUAL_ORDER",
                            parent_record_id=actual_order_id,
                            processing_time=row["decision_time"],
                            order_time=row["decision_time"],
                            fill_time=row["decision_time"],
                            payload={
                                "scope": "SIMULATED",
                                "status": paper_status,
                                "filled_quantity": row[
                                    "paper_filled_quantity"
                                ],
                                "fill_price": row["paper_fill_price"],
                                "estimated_fee": row[
                                    "paper_estimated_fee"
                                ],
                            },
                        )
                        counters["journal_events"] += int(
                            self.repository.append_journal(fill)
                        )
                alert_type = material_action_alert(snapshot)
                if alert_type:
                    counters["alerts"] += int(
                        self.repository.append_action_alert(
                            alert_type, snapshot
                        )
                    )
                parent_id = snapshot.decision_id

        for row in self.repository.unreviewed_settlements():
            snapshot = snapshot_from_dict(row["snapshot"])
            outcome = row["settlement_result"]
            if outcome not in {"YES", "NO"}:
                continue
            review = review_decision_process(
                snapshot,
                settled_outcome=outcome,
                reviewed_at=row["created_at"],
                execution_cost=(
                    Decimal(str(row["estimated_fee"]))
                    if row["estimated_fee"] is not None
                    else None
                ),
            )
            if self.repository.append_review(review):
                counters["post_trade_reviews"] += 1
                settlement_id = hashlib.sha256(
                    (
                        f"settlement|{snapshot.decision_id}|"
                        f"{outcome}|{row['created_at'].isoformat()}"
                    ).encode()
                ).hexdigest()
                settlement_journal = make_journal_event(
                    event_ticker=snapshot.event_ticker,
                    market_ticker=snapshot.market_ticker,
                    candidate_version=snapshot.candidate_version,
                    record_type="SETTLEMENT",
                    record_id=settlement_id,
                    parent_record_type="DECISION_SNAPSHOT",
                    parent_record_id=snapshot.decision_id,
                    processing_time=row["created_at"],
                    settlement_time=row["created_at"],
                    payload={
                        "settled_outcome": outcome,
                        "estimated_fee": row["estimated_fee"],
                        "weighted_fill_price": row["weighted_fill_price"],
                    },
                )
                counters["journal_events"] += int(
                    self.repository.append_journal(settlement_journal)
                )
                review_journal = make_journal_event(
                    event_ticker=snapshot.event_ticker,
                    market_ticker=snapshot.market_ticker,
                    candidate_version=snapshot.candidate_version,
                    record_type="POST_TRADE_REVIEW",
                    record_id=review.review_id,
                    parent_record_type="SETTLEMENT",
                    parent_record_id=settlement_id,
                    processing_time=review.reviewed_at,
                    settlement_time=review.reviewed_at,
                    payload=review.to_dict(),
                )
                counters["journal_events"] += int(
                    self.repository.append_journal(review_journal)
                )
                counters["alerts"] += int(
                    self.repository.append_action_alert(
                        "SETTLED_REVIEW_READY", snapshot
                    )
                )
        counters["reaction_samples"] = self.repository.capture_due_reactions(
            self.now()
        )
        return counters
