from __future__ import annotations

import hashlib
import json
import math
import os
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx

from bot_control import BotState
from capital.eligibility import evaluate_capital_eligibility
from db.connection import get_db
from edge.calculator import DEFAULT_SAFETY_MARGIN
from kalshi_client import (
    KalshiAPIError,
    KalshiClient,
    Order,
    parse_event_date,
    to_event_order_book,
)

from .domain import (
    CycleResult,
    LiveOrder,
    LiveSignal,
    OrderIntent,
    ReconciliationResult,
)
from .repository import PostgresLiveRepository
from .risk import estimated_taker_fee, validate_fixed_limits

MAX_SIGNAL_AGE = timedelta(minutes=30)
MAX_WEATHER_AGE = timedelta(minutes=30)
ORDER_LIFETIME = timedelta(minutes=5)
CLIENT_ORDER_PREFIX = "eae-"
_AMBIGUOUS_EXCEPTIONS = (httpx.TimeoutException, httpx.TransportError)


def deterministic_client_order_id(
    *,
    strategy_version: str,
    decision_id: str,
    ticker: str,
    intended_outcome: str,
    api_side: str,
    price: Decimal,
    count: Decimal,
    decision_at: datetime,
) -> str:
    canonical = json.dumps(
        {
            "strategy_version": strategy_version,
            "decision_id": decision_id,
            "ticker": ticker,
            "intended_outcome": intended_outcome,
            "api_side": api_side,
            "price": format(price, ".4f"),
            "count": format(count, ".2f"),
            "decision_at": decision_at.astimezone(UTC).isoformat(),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return CLIENT_ORDER_PREFIX + hashlib.sha256(canonical.encode()).hexdigest()[:28]


def _remote_status(remote: Order) -> str:
    if remote.status == "resting":
        return "PARTIAL" if remote.fill_count > 0 else "RESTING"
    if remote.status == "executed":
        return "FILLED"
    if remote.status == "canceled":
        return "CANCELED"
    return "UNKNOWN"


def _is_ambiguous_exception(exc: Exception) -> bool:
    return isinstance(exc, _AMBIGUOUS_EXCEPTIONS) or (
        isinstance(exc, KalshiAPIError) and (exc.status_code == 409 or exc.status_code >= 500)
    )


class LiveExecutionService:
    def __init__(
        self,
        *,
        client: Any,
        repository: Any,
        now: Any = None,
    ):
        self.client = client
        self.repository = repository
        self._now = now or (lambda: datetime.now(UTC))

    def reconcile(self, *, actor: str = "worker") -> ReconciliationResult:
        """Reconcile local bot orders against authenticated Kalshi reads.

        Remote orders without a matching local bot record are never modified.
        A remote `eae-*` order with no local record is an integrity mismatch
        that blocks new submissions, not an invitation to invent local state.
        """
        balance = self.client.get_balance()
        remote_orders = self.client.list_orders(limit=1000)
        fills = self.client.list_fills(limit=1000)
        positions = self.client.get_positions()
        settlements = self.client.get_settlements(limit=1000)
        local_orders = self.repository.list_reconcilable_orders()

        by_client = {
            item.client_order_id: item
            for item in remote_orders
            if item.client_order_id
        }
        by_remote_id = {item.order_id: item for item in remote_orders}
        local_by_remote_id = {
            item.kalshi_order_id: item for item in local_orders if item.kalshi_order_id
        }
        mismatches: list[str] = []
        reconciled = 0

        updated_locals: dict[int, LiveOrder] = {}
        for local in local_orders:
            remote = by_client.get(local.client_order_id)
            if remote is None and local.kalshi_order_id:
                remote = by_remote_id.get(local.kalshi_order_id)
            if remote is not None:
                local = self.repository.apply_remote_order(local, remote)
                reconciled += 1
                local_by_remote_id[remote.order_id] = local
            elif local.status in {"SUBMITTING", "UNKNOWN"}:
                # The complete orders/fills query succeeded and found neither
                # identifier. It is now proven safe to retry in a later cycle,
                # never blindly in this reconciliation call.
                local = self.repository.transition(
                    local,
                    "PENDING",
                    "ambiguous_submission_proven_absent",
                    actor=actor,
                    error_code="AMBIGUOUS_PROVEN_ABSENT",
                    reconciliation_status="MATCHED",
                    last_reconciled_at=self._now(),
                )
            elif local.status in {"RESTING", "PARTIAL", "FILLED"}:
                mismatches.append(
                    f"LOCAL_{local.status}_ORDER_MISSING_REMOTE:{local.client_order_id}"
                )
            updated_locals[local.id] = local

        for fill in fills:
            local = local_by_remote_id.get(fill.order_id)
            if local is not None:
                self.repository.record_fill(local, fill)
        for local_id, local in list(updated_locals.items()):
            refreshed = self.repository.refresh_fill_totals(local)
            updated_locals[local_id] = refreshed
            if refreshed.kalshi_order_id:
                local_by_remote_id[refreshed.kalshi_order_id] = refreshed

        settlement_by_ticker = {
            item.ticker: item
            for item in settlements
            if item.market_result in {"yes", "no"}
        }
        for local in updated_locals.values():
            settlement = settlement_by_ticker.get(local.market_ticker)
            if settlement and local.filled_count > 0 and local.status != "SETTLED":
                self.repository.settle_order(
                    local,
                    result=settlement.market_result,
                    settled_at=settlement.settled_time,
                )
                continue
            if local.filled_count > 0 and local.status != "SETTLED":
                try:
                    market = self.client.get_market(local.market_ticker)
                    if local.intended_outcome == "YES":
                        bid = market.yes_bid_dollars
                    else:
                        bid = market.no_bid_dollars
                        if bid is None and market.yes_ask_dollars is not None:
                            bid = 1 - market.yes_ask_dollars
                    if bid is not None:
                        self.repository.update_mark_to_market(local, Decimal(str(bid)))
                except Exception:
                    mismatches.append(f"MARK_TO_MARKET_UNAVAILABLE:{local.client_order_id}")

        local_client_ids = {item.client_order_id for item in local_orders}
        orphaned_bot_orders = [
            item.client_order_id
            for item in remote_orders
            if item.client_order_id
            and item.client_order_id.startswith(CLIENT_ORDER_PREFIX)
            and item.client_order_id not in local_client_ids
        ]
        mismatches.extend(f"REMOTE_BOT_ORDER_MISSING_LOCAL:{item}" for item in orphaned_bot_orders)

        result = ReconciliationResult(
            healthy=not mismatches,
            available_cash=balance.available_dollars,
            balance_as_of=balance.as_of,
            mismatches=tuple(mismatches),
            reconciled_orders=reconciled,
            fills=len(fills),
            positions=len(positions),
            settlements=len(settlements),
        )
        self.repository.record_reconciliation(
            result,
            local_order_count=len(local_orders),
            remote_bot_order_count=sum(
                1
                for item in remote_orders
                if item.client_order_id and item.client_order_id.startswith(CLIENT_ORDER_PREFIX)
            ),
            actor=actor,
        )
        return result

    def enablement_blockers(
        self,
        state: BotState,
        reconciliation: ReconciliationResult,
    ) -> list[str]:
        blockers: list[str] = []
        configured_environment = os.environ.get(
            "KALSHI_ENVIRONMENT", "production"
        ).strip().lower()
        if configured_environment not in {"production", "prod"} or not getattr(
            self.client, "is_production", True
        ):
            blockers.append("PRODUCTION_ENVIRONMENT_REQUIRED")
        capital = evaluate_capital_eligibility(
            environment="prod",
            available_cash=reconciliation.available_cash,
            balance_as_of=reconciliation.balance_as_of,
            reconciliation_healthy=reconciliation.healthy,
            now=self._now(),
        )
        if not capital.eligible:
            blockers.append(capital.reason_code)
        if state.kill_switch:
            blockers.append("KILL_SWITCH_ACTIVE")
        if not reconciliation.healthy:
            blockers.append("RECONCILIATION_REQUIRED")
        timestamps = self.repository.readiness_timestamps()
        market_at = timestamps.get("market_data_at")
        weather_at = timestamps.get("weather_data_at")
        now = self._now()
        if market_at is None or now - market_at > MAX_SIGNAL_AGE:
            blockers.append("STALE_KALSHI_QUOTES")
        if weather_at is None or now - weather_at > MAX_WEATHER_AGE:
            blockers.append("STALE_WEATHER_DATA")
        if not self.repository.worker_healthy():
            blockers.append("WORKER_UNHEALTHY")
        exchange = self.client.get_exchange_status()
        if not exchange.exchange_active or not exchange.trading_active:
            blockers.append("EXCHANGE_NOT_TRADING")
        return list(dict.fromkeys(blockers))

    def _signals(self, strategy_name: str, strategy_version: str) -> list[LiveSignal]:
        rows = self.repository.list_signal_rows()
        sums: dict[str, Decimal] = {}
        for row in rows:
            sums[row["event_ticker"]] = sums.get(
                row["event_ticker"], Decimal("0")
            ) + Decimal(str(row["model_probability"]))
        signals: list[LiveSignal] = []
        for row in rows:
            if not row.get("is_actionable"):
                continue
            probability = Decimal(str(row["model_probability"]))
            edge = Decimal(str(row["edge"]))
            intended = "YES" if edge > 0 else "NO"
            required_action = f"BUY_{intended}"
            checklist = row.get("professional_checklist") or {}
            required_checks = (
                checklist.get("CONTRACT", {}).get(
                    "settlement_truth_complete"
                ),
                checklist.get("CONTRACT", {}).get("station_correct"),
                checklist.get("CONTRACT", {}).get(
                    "bracket_boundaries_clear"
                ),
                checklist.get("CONTRACT", {}).get("market_open"),
                checklist.get("INFORMATION", {}).get("point_in_time_valid"),
                checklist.get("PROBABILITY", {}).get(
                    "probabilities_valid"
                ),
                checklist.get("PROBABILITY", {}).get(
                    "impossible_outcomes_zeroed"
                ),
                checklist.get("PRICE", {}).get(
                    "executable_and_within_limit"
                ),
                checklist.get("THESIS", {}).get(
                    "specific_information_advantage"
                ),
                checklist.get("RISK", {}).get("risk_checks_pass"),
            )
            if (
                not row.get("production_order_allowed")
                or row.get("professional_action") != required_action
                or row.get("professional_strategy_version")
                != strategy_version
                or not row.get("professional_decision_id")
                or not all(value is True for value in required_checks)
            ):
                continue
            outcome_probability = probability if intended == "YES" else Decimal("1") - probability
            threshold = Decimal(str(row["fee_adjusted_threshold"]))
            maximum_price = outcome_probability - threshold
            weather_at = row.get("weather_data_at")
            close_time = row.get("close_time")
            decision_at = row.get("created_at")
            if not weather_at or not close_time or not decision_at:
                continue
            signals.append(
                LiveSignal(
                    signal_id=(
                        f"professional:{row['professional_decision_id']}"
                    ),
                    decision_id=row["professional_decision_id"],
                    strategy_name=strategy_name,
                    strategy_version=strategy_version,
                    market_ticker=row["market_ticker"],
                    event_ticker=row["event_ticker"],
                    event_date=parse_event_date(row["event_ticker"]),
                    decision_at=decision_at,
                    weather_data_at=weather_at,
                    quote_at=decision_at,
                    close_time=close_time,
                    intended_outcome=intended,
                    model_probability=outcome_probability,
                    maximum_acceptable_price=maximum_price,
                    probability_sum=sums[row["event_ticker"]],
                )
            )
        return sorted(
            signals,
            key=lambda item: item.model_probability - item.maximum_acceptable_price,
            reverse=True,
        )

    def _executable_outcome_price(self, signal: LiveSignal) -> tuple[Decimal, Any]:
        market = self.client.get_market(signal.market_ticker)
        if market.status not in {"active", "open"}:
            raise ValueError("MARKET_NOT_OPEN")
        if signal.intended_outcome == "YES":
            price_value = market.yes_ask_dollars
        else:
            price_value = market.no_ask_dollars
            if price_value is None and market.yes_bid_dollars is not None:
                price_value = 1 - market.yes_bid_dollars
        if price_value is None:
            raise ValueError("EXECUTABLE_PRICE_UNAVAILABLE")
        price = Decimal(str(price_value)).quantize(Decimal("0.0001"))
        return price, market

    def submit_one(
        self,
        *,
        state: BotState,
        reconciliation: ReconciliationResult,
    ) -> bool:
        now = self._now()
        for signal in self._signals(
            state.strategy_name or "weather-daily-temp",
            state.strategy_version or "",
        ):
            if signal.strategy_version != (state.strategy_version or ""):
                continue
            if now - signal.decision_at > MAX_SIGNAL_AGE:
                continue
            if now - signal.weather_data_at > MAX_WEATHER_AGE:
                continue
            if signal.close_time <= now:
                continue
            if not signal.model_probability.is_finite() or not (
                Decimal("0") <= signal.model_probability <= Decimal("1")
            ):
                continue
            if not math.isclose(float(signal.probability_sum), 1.0, abs_tol=0.03):
                continue
            try:
                outcome_price, market = self._executable_outcome_price(signal)
            except ValueError:
                continue
            if getattr(market, "close_time", None):
                market_close = datetime.fromisoformat(market.close_time.replace("Z", "+00:00"))
                if market_close <= now:
                    continue
            if outcome_price > signal.maximum_acceptable_price:
                continue
            fee = estimated_taker_fee(outcome_price, Decimal("1"))
            edge = signal.model_probability - outcome_price
            if edge <= fee + Decimal(str(DEFAULT_SAFETY_MARGIN)):
                continue
            converted = to_event_order_book(signal.intended_outcome, outcome_price)
            risk_state = self.repository.risk_state(
                signal.market_ticker, signal.event_ticker, signal.event_date
            )
            verdict = validate_fixed_limits(
                count=Decimal("1"),
                outcome_price=outcome_price,
                estimated_fees=fee,
                state=risk_state,
            )
            if not verdict.allowed:
                continue
            # Capital is queried again immediately before persistence/submission;
            # the balance from cycle-start reconciliation is not reused as a
            # potentially stale authorization.
            fresh_balance = self.client.get_balance()
            fresh_capital = evaluate_capital_eligibility(
                environment="prod",
                available_cash=fresh_balance.available_dollars,
                balance_as_of=fresh_balance.as_of,
                reconciliation_healthy=reconciliation.healthy,
                now=self._now(),
            )
            if not fresh_capital.eligible:
                return False
            if verdict.order_cost_including_fees > fresh_balance.available_dollars:
                continue
            client_order_id = deterministic_client_order_id(
                strategy_version=signal.strategy_version,
                decision_id=signal.decision_id,
                ticker=signal.market_ticker,
                intended_outcome=signal.intended_outcome,
                api_side=converted.book_side,
                price=converted.yes_price,
                count=Decimal("1"),
                decision_at=signal.decision_at,
            )
            intent = OrderIntent(
                local_order_id=str(uuid.uuid4()),
                client_order_id=client_order_id,
                signal=LiveSignal(
                    **{
                        **signal.__dict__,
                        "quote_at": now,
                    }
                ),
                api_book_side=converted.book_side,
                submitted_yes_price=converted.yes_price,
                outcome_price=outcome_price,
                requested_count=Decimal("1"),
                estimated_fees=fee,
                expires_at=min(signal.close_time, now + ORDER_LIFETIME),
            )
            local, created = self.repository.create_intent(intent)
            if not created and local.status != "PENDING":
                continue
            local = self.repository.transition(
                local,
                "SUBMITTING",
                "submission_started",
                actor="worker",
                submitted_at=now,
                reconciliation_status="PENDING",
            )
            try:
                ack = self.client.create_order(
                    ticker=signal.market_ticker,
                    client_order_id=client_order_id,
                    side=converted.book_side,
                    count=Decimal("1"),
                    price=converted.yes_price,
                    time_in_force="good_till_canceled",
                    self_trade_prevention_type="taker_at_cross",
                    cancel_order_on_pause=True,
                    expiration_time=int(intent.expires_at.timestamp()),
                )
            except Exception as exc:
                if _is_ambiguous_exception(exc):
                    self.repository.transition(
                        local,
                        "UNKNOWN",
                        "submission_ambiguous",
                        actor="worker",
                        error_code="AMBIGUOUS_SUBMISSION",
                        error_detail=f"{exc.__class__.__name__}: {exc}",
                        reconciliation_status="REQUIRED",
                    )
                    # Immediate read-only reconciliation; never resubmits.
                    self.reconcile(actor="ambiguous-submission")
                else:
                    self.repository.transition(
                        local,
                        "REJECTED",
                        "submission_rejected",
                        actor="worker",
                        error_code="KALSHI_REJECTED",
                        error_detail=f"{exc.__class__.__name__}: {exc}",
                        reconciliation_status="MATCHED",
                    )
                return False
            status = (
                "FILLED"
                if ack.remaining_count == 0
                else "PARTIAL"
                if ack.fill_count > 0
                else "RESTING"
            )
            self.repository.transition(
                local,
                status,
                "submission_acknowledged",
                actor="worker",
                kalshi_order_id=ack.order_id,
                filled_count=ack.fill_count,
                remaining_count=ack.remaining_count,
                average_fill_price=ack.average_fill_price,
                actual_fees=(
                    (ack.average_fee_paid or Decimal("0")) * ack.fill_count
                ).quantize(Decimal("0.0001")),
                acknowledged_at=now,
                filled_at=now if status == "FILLED" else None,
                reconciliation_status="MATCHED",
                last_reconciled_at=now,
            )
            return True
        return False

    def global_risk_blockers(self) -> list[str]:
        """Evaluate account-wide automatic stops even when no signal qualifies."""
        risk_state = self.repository.risk_state("", "", self._now().date())
        verdict = validate_fixed_limits(
            count=Decimal("0"),
            outcome_price=Decimal("0"),
            estimated_fees=Decimal("0"),
            state=risk_state,
        )
        return list(verdict.blockers)

    def cancel_bot_orders(self, *, force: bool, actor: str) -> int:
        """Cancel only locally persisted bot-owned resting orders."""
        canceled = 0
        now = self._now()
        for local in self.repository.list_active_orders():
            if local.status not in {"RESTING", "PARTIAL"} or not local.kalshi_order_id:
                continue
            should_cancel = force or (local.expires_at is not None and local.expires_at <= now)
            if not should_cancel:
                try:
                    price, market = self._executable_outcome_price(
                        LiveSignal(
                            signal_id="monitor",
                            decision_id="monitor",
                            strategy_name="",
                            strategy_version="",
                            market_ticker=local.market_ticker,
                            event_ticker=local.event_ticker,
                            event_date=local.event_date,
                            decision_at=local.decision_at,
                            weather_data_at=local.weather_data_at,
                            quote_at=local.quote_at,
                            close_time=local.expires_at or now,
                            intended_outcome=local.intended_outcome,
                            model_probability=local.model_probability,
                            maximum_acceptable_price=local.maximum_acceptable_price,
                            probability_sum=Decimal("1"),
                        )
                    )
                    should_cancel = (
                        market.status not in {"active", "open"}
                        or price > local.maximum_acceptable_price
                        or local.model_probability - price
                        <= estimated_taker_fee(price, Decimal("1"))
                        + Decimal(str(DEFAULT_SAFETY_MARGIN))
                    )
                except ValueError:
                    should_cancel = True
            if not should_cancel:
                continue
            try:
                self.client.cancel_order(local.kalshi_order_id)
            except Exception as exc:
                if _is_ambiguous_exception(exc):
                    self.repository.transition(
                        local,
                        "UNKNOWN",
                        "cancellation_ambiguous",
                        actor=actor,
                        error_code="AMBIGUOUS_CANCELLATION",
                        error_detail=f"{exc.__class__.__name__}: {exc}",
                        reconciliation_status="REQUIRED",
                    )
                continue
            self.repository.transition(
                local,
                "CANCELED",
                "order_canceled",
                actor=actor,
                canceled_at=now,
                remaining_count=Decimal("0"),
                reconciliation_status="PENDING",
            )
            canceled += 1
        return canceled

    def run_cycle(self, state: BotState) -> CycleResult:
        if not self.repository.acquire_cycle_lock():
            return CycleResult(status="BLOCKED", blocker="CYCLE_ALREADY_RUNNING")
        cycle_id = self.repository.start_cycle()
        result = CycleResult(status="ERROR", error="cycle did not finish")
        try:
            reconciliation = self.reconcile(actor="worker")
            canceled = self.cancel_bot_orders(force=state.kill_switch, actor="worker")
            if state.kill_switch:
                result = CycleResult(
                    status="BLOCKED",
                    reconciled_orders=reconciliation.reconciled_orders,
                    canceled_orders=canceled,
                    blocker="KILL_SWITCH_ACTIVE",
                )
            elif not state.enabled or state.effective_mode != "LIVE":
                result = CycleResult(
                    status="STOPPED",
                    reconciled_orders=reconciliation.reconciled_orders,
                    canceled_orders=canceled,
                    blocker="LIVE_DISABLED",
                )
            else:
                blockers = self.enablement_blockers(state, reconciliation)
                if not blockers:
                    blockers = self.global_risk_blockers()
                if blockers:
                    if blockers[0] != "PRODUCTION_ENVIRONMENT_REQUIRED":
                        canceled += self.cancel_bot_orders(
                            force=True,
                            actor=f"automatic-stop:{blockers[0]}",
                        )
                    result = CycleResult(
                        status="BLOCKED",
                        reconciled_orders=reconciliation.reconciled_orders,
                        canceled_orders=canceled,
                        blocker=blockers[0],
                    )
                else:
                    submitted = self.submit_one(state=state, reconciliation=reconciliation)
                    result = CycleResult(
                        status="RUNNING",
                        submitted_orders=int(submitted),
                        reconciled_orders=reconciliation.reconciled_orders,
                        canceled_orders=canceled,
                    )
        except Exception as exc:
            result = CycleResult(
                status="ERROR",
                error=f"{exc.__class__.__name__}: {exc}",
            )
        finally:
            self.repository.finish_cycle(cycle_id, result)
            self.repository.release_cycle_lock()
        return result


def build_live_service() -> tuple[LiveExecutionService, Any] | None:
    connection = get_db()
    if connection is None:
        return None
    client = KalshiClient.from_env()
    return (
        LiveExecutionService(
            client=client,
            repository=PostgresLiveRepository(connection),
        ),
        connection,
    )


def run_live_cycle_from_env() -> CycleResult:
    """Run one recurring cycle using backend environment and persisted state."""
    from bot_control import get_bot_state

    built = build_live_service()
    if built is None:
        return CycleResult(status="ERROR", error="DATABASE_UNAVAILABLE")
    service, connection = built
    try:
        return service.run_cycle(get_bot_state())
    finally:
        service.client.close()
        connection.close()
