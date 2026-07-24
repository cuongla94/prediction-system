from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import httpx

from bot_control import BotState
from kalshi_client import KalshiAPIError
from kalshi_client.models import Balance, OrderAcknowledgement
from live_trading import LiveOrder, LiveRiskState, ReconciliationResult
from live_trading.service import LiveExecutionService, deterministic_client_order_id

NOW = datetime(2026, 7, 23, 18, 0, tzinfo=UTC)


def _state(*, live=True, killed=False):
    return BotState(
        effective_mode="LIVE" if live else "OFF",
        enabled=live,
        kill_switch=killed,
        kill_switch_reason="test" if killed else None,
        strategy_name="weather-daily-temp",
        strategy_version="v1-2026-07-23",
        updated_at=NOW,
        actor="test",
        live_enabled=live,
    )


def _signal_rows():
    common = {
        "created_at": NOW,
        "series_ticker": "KXHIGHNY",
        "event_ticker": "KXHIGHNY-26JUL23",
        "close_time": NOW + timedelta(hours=4),
        "weather_data_at": NOW,
        "lead_days": 0,
        "professional_decision_id": "professional-decision-1",
        "professional_strategy_version": "v1-2026-07-23",
        "professional_action": "BUY_YES",
        "production_order_allowed": True,
        "professional_checklist": {
            "CONTRACT": {
                "settlement_truth_complete": True,
                "station_correct": True,
                "bracket_boundaries_clear": True,
                "market_open": True,
            },
            "INFORMATION": {"point_in_time_valid": True},
            "PROBABILITY": {
                "probabilities_valid": True,
                "impossible_outcomes_zeroed": True,
            },
            "PRICE": {"executable_and_within_limit": True},
            "THESIS": {"specific_information_advantage": True},
            "RISK": {"risk_checks_pass": True},
        },
    }
    return [
        {
            **common,
            "id": 10,
            "market_ticker": "KXHIGHNY-26JUL23-T90",
            "model_probability": 0.70,
            "market_yes_price": 0.50,
            "edge": 0.20,
            "fee_adjusted_threshold": 0.04,
            "is_actionable": True,
        },
        {
            **common,
            "id": 11,
            "market_ticker": "KXHIGHNY-26JUL23-T91",
            "model_probability": 0.30,
            "market_yes_price": 0.30,
            "edge": 0.00,
            "fee_adjusted_threshold": 0.04,
            "is_actionable": False,
        },
    ]


def _live_order(intent, *, status="PENDING", kalshi_order_id=None):
    return LiveOrder(
        id=1,
        local_order_id=intent.local_order_id,
        client_order_id=intent.client_order_id,
        kalshi_order_id=kalshi_order_id,
        market_ticker=intent.signal.market_ticker,
        event_ticker=intent.signal.event_ticker,
        event_date=intent.signal.event_date,
        intended_outcome=intent.signal.intended_outcome,
        api_book_side=intent.api_book_side,
        submitted_yes_price=intent.submitted_yes_price,
        model_probability=intent.signal.model_probability,
        maximum_acceptable_price=intent.signal.maximum_acceptable_price,
        requested_count=intent.requested_count,
        filled_count=Decimal("0"),
        remaining_count=intent.requested_count,
        average_fill_price=None,
        estimated_fees=intent.estimated_fees,
        actual_fees=Decimal("0"),
        status=status,
        decision_at=intent.signal.decision_at,
        quote_at=intent.signal.quote_at,
        weather_data_at=intent.signal.weather_data_at,
        expires_at=intent.expires_at,
        reconciliation_status="PENDING",
    )


class FakeRepository:
    def __init__(self):
        self.orders = []
        self.created = []
        self.transitions = []
        self.reconciliations = []
        self.cycle_result = None
        self.duplicate = False
        self.duplicate_status = "RESTING"
        self.risk_state_result = LiveRiskState(reconciliation_healthy=True)

    def acquire_cycle_lock(self):
        return True

    def release_cycle_lock(self):
        pass

    def start_cycle(self):
        return 1

    def finish_cycle(self, _cycle_id, result):
        self.cycle_result = result

    def list_signal_rows(self):
        return _signal_rows()

    def readiness_timestamps(self):
        return {"market_data_at": NOW, "weather_data_at": NOW}

    def worker_healthy(self):
        return True

    def list_reconcilable_orders(self):
        return list(self.orders)

    def list_active_orders(self):
        return list(self.orders)

    def create_intent(self, intent):
        if self.duplicate:
            return _live_order(intent, status=self.duplicate_status), False
        order = _live_order(intent)
        self.orders.append(order)
        self.created.append(intent)
        return order, True

    def transition(self, order, status, event_type, **fields):
        allowed = {key: value for key, value in fields.items() if hasattr(order, key)}
        updated = replace(order, status=status, **allowed)
        self.orders = [updated if item.id == order.id else item for item in self.orders]
        self.transitions.append((event_type, status))
        return updated

    def apply_remote_order(self, local, remote):
        return self.transition(
            local,
            "RESTING",
            "remote_order_reconciled",
            kalshi_order_id=remote.order_id,
            filled_count=remote.fill_count,
            remaining_count=remote.remaining_count,
            reconciliation_status="MATCHED",
        )

    def record_fill(self, _local, _fill):
        return True

    def refresh_fill_totals(self, local):
        return local

    def settle_order(self, local, **_kwargs):
        return self.transition(local, "SETTLED", "settlement_reconciled")

    def update_mark_to_market(self, _local, _price):
        pass

    def record_reconciliation(self, result, **_kwargs):
        self.reconciliations.append(result)

    def risk_state(self, *_args):
        return self.risk_state_result


class FakeClient:
    def __init__(self, *, timeout_on_create=False, create_error=None, cash="5.01"):
        self.timeout_on_create = timeout_on_create
        self.create_error = create_error
        self.cash = Decimal(cash)
        self.balance_calls = 0
        self.create_calls = []
        self.cancel_calls = []
        self.remote_orders = []

    def get_balance(self):
        self.balance_calls += 1
        return Balance(self.cash, NOW, {})

    def list_orders(self, **_kwargs):
        return list(self.remote_orders)

    def list_fills(self, **_kwargs):
        return []

    def get_positions(self):
        return []

    def get_settlements(self, **_kwargs):
        return []

    def get_exchange_status(self):
        return SimpleNamespace(exchange_active=True, trading_active=True)

    def get_market(self, _ticker):
        return SimpleNamespace(
            status="active",
            yes_ask_dollars=0.50,
            no_ask_dollars=0.50,
            yes_bid_dollars=0.49,
            no_bid_dollars=0.49,
            close_time=(NOW + timedelta(hours=4)).isoformat(),
        )

    def create_order(self, **kwargs):
        self.create_calls.append(kwargs)
        if self.timeout_on_create:
            raise httpx.ReadTimeout("ambiguous timeout")
        if self.create_error:
            raise self.create_error
        return OrderAcknowledgement(
            order_id="kalshi-1",
            client_order_id=kwargs["client_order_id"],
            fill_count=Decimal("0"),
            remaining_count=Decimal("1"),
            average_fill_price=None,
            average_fee_paid=None,
            ts_ms=1,
            raw={},
        )

    def cancel_order(self, order_id):
        self.cancel_calls.append(order_id)


def _reconciliation(cash="5.01"):
    return ReconciliationResult(
        healthy=True,
        available_cash=Decimal(cash),
        balance_as_of=NOW,
        mismatches=(),
        reconciled_orders=0,
        fills=0,
        positions=0,
        settlements=0,
    )


def test_client_order_id_is_deterministic_and_changes_with_logical_order():
    kwargs = dict(
        strategy_version="v1",
        decision_id="decision-1",
        ticker="TICKER",
        intended_outcome="YES",
        api_side="bid",
        price=Decimal("0.5000"),
        count=Decimal("1"),
        decision_at=NOW,
    )
    first = deterministic_client_order_id(**kwargs)
    assert first == deterministic_client_order_id(**kwargs)
    assert first != deterministic_client_order_id(**{**kwargs, "price": Decimal("0.5100")})


def test_five_dollars_is_blocked_before_every_submission():
    client = FakeClient(cash="5.00")
    repository = FakeRepository()
    service = LiveExecutionService(client=client, repository=repository, now=lambda: NOW)
    assert service.submit_one(state=_state(), reconciliation=_reconciliation("5.00")) is False
    assert client.create_calls == []
    assert client.balance_calls == 1


def test_five_dollars_and_one_cent_passes_capital_and_submits_one_limit_order():
    client = FakeClient()
    repository = FakeRepository()
    service = LiveExecutionService(client=client, repository=repository, now=lambda: NOW)
    assert service.submit_one(state=_state(), reconciliation=_reconciliation("5.01")) is True
    assert len(client.create_calls) == 1
    assert client.create_calls[0]["count"] == Decimal("1")
    assert client.create_calls[0]["time_in_force"] == "good_till_canceled"
    assert repository.orders[0].status == "RESTING"
    assert client.balance_calls == 1


def test_persisted_professional_checklist_is_required_before_submission():
    client = FakeClient()
    repository = FakeRepository()
    rows = _signal_rows()
    rows[0]["production_order_allowed"] = False
    repository.list_signal_rows = lambda: rows
    service = LiveExecutionService(
        client=client, repository=repository, now=lambda: NOW
    )
    assert (
        service.submit_one(
            state=_state(), reconciliation=_reconciliation("10.00")
        )
        is False
    )
    assert client.create_calls == []


def test_stale_reconciliation_cash_cannot_bypass_immediate_balance_recheck():
    client = FakeClient(cash="5.00")
    repository = FakeRepository()
    service = LiveExecutionService(client=client, repository=repository, now=lambda: NOW)
    assert service.submit_one(state=_state(), reconciliation=_reconciliation("99.00")) is False
    assert client.create_calls == []


def test_duplicate_persisted_intent_never_calls_create_order():
    client = FakeClient()
    repository = FakeRepository()
    repository.duplicate = True
    service = LiveExecutionService(client=client, repository=repository, now=lambda: NOW)
    assert service.submit_one(state=_state(), reconciliation=_reconciliation()) is False
    assert client.create_calls == []


def test_restart_after_persisting_pending_intent_resumes_same_order_once():
    client = FakeClient()
    repository = FakeRepository()
    repository.duplicate = True
    repository.duplicate_status = "PENDING"
    service = LiveExecutionService(client=client, repository=repository, now=lambda: NOW)
    assert service.submit_one(state=_state(), reconciliation=_reconciliation()) is True
    assert len(client.create_calls) == 1


def test_timeout_marks_unknown_reconciles_and_does_not_blindly_retry():
    client = FakeClient(timeout_on_create=True)
    repository = FakeRepository()
    service = LiveExecutionService(client=client, repository=repository, now=lambda: NOW)
    assert service.submit_one(state=_state(), reconciliation=_reconciliation()) is False
    assert len(client.create_calls) == 1
    assert ("submission_ambiguous", "UNKNOWN") in repository.transitions
    assert ("ambiguous_submission_proven_absent", "PENDING") in repository.transitions


def test_duplicate_conflict_is_treated_as_ambiguous_and_reconciled():
    client = FakeClient(create_error=KalshiAPIError(409, "duplicate client order id"))
    repository = FakeRepository()
    service = LiveExecutionService(client=client, repository=repository, now=lambda: NOW)
    assert service.submit_one(state=_state(), reconciliation=_reconciliation()) is False
    assert ("submission_ambiguous", "UNKNOWN") in repository.transitions
    assert repository.reconciliations


def test_disabled_cycle_reconciles_but_never_writes_order():
    client = FakeClient()
    repository = FakeRepository()
    service = LiveExecutionService(client=client, repository=repository, now=lambda: NOW)
    result = service.run_cycle(_state(live=False))
    assert result.status == "STOPPED"
    assert repository.reconciliations
    assert client.create_calls == []


def test_global_daily_loss_stop_is_a_visible_blocked_cycle():
    client = FakeClient()
    repository = FakeRepository()
    repository.risk_state_result = LiveRiskState(
        reconciliation_healthy=True,
        daily_realized_pnl=Decimal("-2.00"),
    )
    service = LiveExecutionService(client=client, repository=repository, now=lambda: NOW)
    result = service.run_cycle(_state())
    assert result.status == "BLOCKED"
    assert result.blocker == "MAX_DAILY_REALIZED_LOSS"
    assert client.create_calls == []


def test_emergency_cancel_targets_only_persisted_bot_owned_order():
    client = FakeClient()
    repository = FakeRepository()
    service = LiveExecutionService(client=client, repository=repository, now=lambda: NOW)
    service.submit_one(state=_state(), reconciliation=_reconciliation())
    repository.orders[0] = replace(
        repository.orders[0], status="RESTING", kalshi_order_id="kalshi-1"
    )
    assert service.cancel_bot_orders(force=True, actor="test") == 1
    assert client.cancel_calls == ["kalshi-1"]
