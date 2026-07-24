from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, fields
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Mapping

from kalshi_client import Market, MarketOrderbook
from live_trading.risk import estimated_taker_fee
from weather.stations import Station


class TraderAction(StrEnum):
    DO_NOT_TRADE = "DO_NOT_TRADE"
    WATCH = "WATCH"
    BUY_YES = "BUY_YES"
    BUY_NO = "BUY_NO"
    HOLD = "HOLD"
    EXIT = "EXIT"
    REBUY_YES = "REBUY_YES"
    REBUY_NO = "REBUY_NO"


class InformationEventType(StrEnum):
    FORECAST_RELEASED = "FORECAST_RELEASED"
    FORECAST_REVISED = "FORECAST_REVISED"
    OBSERVATION_PUBLISHED = "OBSERVATION_PUBLISHED"
    OBSERVATION_REVISED = "OBSERVATION_REVISED"
    NEW_DAILY_HIGH = "NEW_DAILY_HIGH"
    NEW_DAILY_LOW = "NEW_DAILY_LOW"
    BRACKET_BECAME_IMPOSSIBLE = "BRACKET_BECAME_IMPOSSIBLE"
    MODEL_DISAGREEMENT_CHANGED = "MODEL_DISAGREEMENT_CHANGED"
    MARKET_PRICE_MOVED = "MARKET_PRICE_MOVED"
    ORDERBOOK_LIQUIDITY_CHANGED = "ORDERBOOK_LIQUIDITY_CHANGED"
    MARKET_STATUS_CHANGED = "MARKET_STATUS_CHANGED"
    ORDER_ACCEPTED = "ORDER_ACCEPTED"
    ORDER_PARTIALLY_FILLED = "ORDER_PARTIALLY_FILLED"
    ORDER_FILLED = "ORDER_FILLED"
    ORDER_CANCELED = "ORDER_CANCELED"
    SETTLEMENT_PUBLISHED = "SETTLEMENT_PUBLISHED"
    SETTLEMENT_REVISED = "SETTLEMENT_REVISED"


class ThesisType(StrEnum):
    NEW_FORECAST_INFORMATION = "NEW_FORECAST_INFORMATION"
    NEW_OBSERVATION_INFORMATION = "NEW_OBSERVATION_INFORMATION"
    MARKET_MOVED_TOO_FAR = "MARKET_MOVED_TOO_FAR"
    MARKET_HAS_NOT_REACTED = "MARKET_HAS_NOT_REACTED"
    MODEL_MARKET_DISAGREEMENT = "MODEL_MARKET_DISAGREEMENT"
    BRACKET_IMPOSSIBILITY = "BRACKET_IMPOSSIBILITY"
    REMAINING_DAY_REASSESSMENT = "REMAINING_DAY_REASSESSMENT"
    NO_IDENTIFIABLE_INFORMATION_ADVANTAGE = (
        "NO_IDENTIFIABLE_INFORMATION_ADVANTAGE"
    )


class ReviewClassification(StrEnum):
    GOOD_DECISION_GOOD_OUTCOME = "GOOD_DECISION_GOOD_OUTCOME"
    GOOD_DECISION_BAD_OUTCOME = "GOOD_DECISION_BAD_OUTCOME"
    BAD_DECISION_GOOD_OUTCOME = "BAD_DECISION_GOOD_OUTCOME"
    BAD_DECISION_BAD_OUTCOME = "BAD_DECISION_BAD_OUTCOME"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


TRADE_ACTIONS = frozenset(
    {
        TraderAction.BUY_YES,
        TraderAction.BUY_NO,
        TraderAction.REBUY_YES,
        TraderAction.REBUY_NO,
    }
)


def _utc(value: datetime | str | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _decimal(value: Any) -> Decimal | None:
    return Decimal(str(value)) if value is not None else None


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, Mapping):
        return {key: _jsonable(item) for key, item in value.items()}
    return value


def _immutable_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType(
        {
            key: (
                _immutable_mapping(item)
                if isinstance(item, Mapping)
                else tuple(
                    _immutable_mapping(child)
                    if isinstance(child, Mapping)
                    else child
                    for child in item
                )
                if isinstance(item, (list, tuple))
                else item
            )
            for key, item in value.items()
        }
    )


@dataclass(frozen=True)
class ContractTruth:
    event_ticker: str
    market_ticker: str
    city: str
    official_weather_station: str | None
    station_identifier: str | None
    target_date: date | None
    target_variable: str
    settlement_source: str | None
    settlement_timezone: str | None
    observation_period: str | None
    rounding_convention: str | None
    bracket_lower_boundary: Decimal | None
    bracket_upper_boundary: Decimal | None
    bracket_open_ended: bool
    market_open_time: datetime | None
    market_close_time: datetime | None
    expected_settlement_time: datetime | None
    market_status: str | None
    settlement_may_be_revised: bool
    critical_issues: tuple[str, ...]

    @property
    def status(self) -> str:
        return "CONTRACT_TRUTH_UNCLEAR" if self.critical_issues else "CLEAR"

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(asdict(self))


def build_contract_truth(
    alert: Mapping[str, Any],
    *,
    station: Station | None,
    market: Market | None,
) -> ContractTruth:
    metric = alert.get("metric") or (station.metric if station else None)
    target_variable = {
        "max": "daily high",
        "min": "daily low",
    }.get(metric, "other supported climate variable")
    floor = _decimal(
        market.floor_strike if market else alert.get("floor_strike")
    )
    cap = _decimal(
        market.cap_strike if market else alert.get("cap_strike")
    )
    raw = market.raw if market else {}
    try:
        from kalshi_client import parse_event_date

        target_date = parse_event_date(str(alert.get("event_ticker") or ""))
    except (TypeError, ValueError):
        target_date = None
    status = market.status if market else alert.get("market_status")
    close_time = _utc(
        (market.close_time if market else None) or alert.get("close_time")
    )
    source = station.settlement_source_url if station else None
    timezone = station.standard_time_timezone if station else None
    issues: list[str] = []
    if station is None:
        issues.append("UNKNOWN_SETTLEMENT_STATION")
    if source is None:
        issues.append("UNKNOWN_SETTLEMENT_SOURCE")
    if timezone is None:
        issues.append("UNKNOWN_SETTLEMENT_TIMEZONE")
    if target_date is None:
        issues.append("UNKNOWN_TARGET_DATE")
    if floor is None and cap is None:
        issues.append("UNCLEAR_BRACKET_BOUNDARIES")
    if floor is not None and cap is not None and floor >= cap:
        issues.append("INCONSISTENT_BRACKET_BOUNDARIES")
    if close_time is None:
        issues.append("UNKNOWN_MARKET_CLOSE_TIME")
    if not status:
        issues.append("UNKNOWN_MARKET_STATUS")
    return ContractTruth(
        event_ticker=str(alert.get("event_ticker") or ""),
        market_ticker=str(alert.get("market_ticker") or ""),
        city=str(alert.get("city") or (station.city if station else "")),
        official_weather_station=(
            f"NWS {station.nws_station_id}" if station else None
        ),
        station_identifier=station.nws_station_id if station else None,
        target_date=target_date,
        target_variable=target_variable,
        settlement_source=source,
        settlement_timezone=timezone,
        observation_period=(
            f"station-local climatological day in {timezone}"
            if timezone
            else None
        ),
        rounding_convention=(
            "official whole-degree Fahrenheit daily extreme"
            if metric in {"max", "min"}
            else None
        ),
        bracket_lower_boundary=floor,
        bracket_upper_boundary=cap,
        bracket_open_ended=(floor is None) != (cap is None),
        market_open_time=_utc(raw.get("open_time")),
        market_close_time=close_time,
        expected_settlement_time=_utc(
            raw.get("expected_expiration_time")
            or raw.get("expiration_time")
        ),
        market_status=status,
        settlement_may_be_revised=True,
        critical_issues=tuple(issues),
    )


def bracket_is_impossible(
    *,
    metric: str,
    observed_extreme: Decimal | None,
    lower_boundary: Decimal | None,
    upper_boundary: Decimal | None,
) -> bool:
    if observed_extreme is None:
        return False
    if metric == "max":
        return (
            upper_boundary is not None
            and observed_extreme >= upper_boundary
        )
    if metric == "min":
        return (
            lower_boundary is not None
            and observed_extreme < lower_boundary
        )
    return False


@dataclass(frozen=True)
class WeatherState:
    latest_official_observation: Decimal | None
    observation_event_time: datetime | None
    observation_publication_time: datetime | None
    collector_receipt_time: datetime | None
    observed_daily_extreme: Decimal | None
    prior_observation: Decimal | None
    recent_temperature_change: Decimal | None
    seconds_since_last_observation: int | None
    distance_to_lower_boundary: Decimal | None
    distance_to_upper_boundary: Decimal | None
    bracket_impossible: bool
    bracket_reachable: bool
    remaining_observation_hours: Decimal | None
    remaining_daylight_hours: Decimal | None
    latest_forecast: Decimal | None
    forecast_run_time: datetime | None
    forecast_availability_time: datetime | None
    forecast_age_seconds: int | None
    forecast_provider_model: str
    ensemble_mean: Decimal | None
    ensemble_spread: Decimal | None
    model_disagreement: Decimal | None
    known_station_model_bias: Decimal | None
    forecast_revision: Decimal | None
    stale: bool

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(asdict(self))


def build_weather_state(
    alert: Mapping[str, Any],
    contract: ContractTruth,
    *,
    decision_time: datetime,
    previous: Mapping[str, Any] | None = None,
    maximum_observation_age_seconds: int = 1800,
    maximum_forecast_age_seconds: int = 43200,
) -> WeatherState:
    now = _utc(decision_time)
    assert now is not None
    observation = _decimal(alert.get("observed_so_far"))
    prior_observation = _decimal(
        previous.get("observed_daily_extreme") if previous else None
    )
    event_time = _utc(alert.get("observation_event_time"))
    forecast_availability = _utc(alert.get("forecast_availability_time"))
    ensemble_mean = _decimal(alert.get("ensemble_mean"))
    previous_forecast = _decimal(
        previous.get("ensemble_mean") if previous else None
    )
    observation_age = (
        int((now - event_time).total_seconds()) if event_time else None
    )
    forecast_age = (
        int((now - forecast_availability).total_seconds())
        if forecast_availability
        else None
    )
    metric = str(alert.get("metric") or "max")
    remaining_hours = (
        Decimal(
            str(
                max(
                    0.0,
                    (contract.market_close_time - now).total_seconds()
                    / 3600,
                )
            )
        ).quantize(Decimal("0.01"))
        if contract.market_close_time
        else None
    )
    impossible = bracket_is_impossible(
        metric=metric,
        observed_extreme=observation,
        lower_boundary=contract.bracket_lower_boundary,
        upper_boundary=contract.bracket_upper_boundary,
    )
    return WeatherState(
        latest_official_observation=observation,
        observation_event_time=event_time,
        observation_publication_time=_utc(
            alert.get("observation_publication_time")
        ),
        collector_receipt_time=_utc(
            alert.get("observation_collector_received_time")
        ),
        observed_daily_extreme=observation,
        prior_observation=prior_observation,
        recent_temperature_change=(
            observation - prior_observation
            if observation is not None and prior_observation is not None
            else None
        ),
        seconds_since_last_observation=observation_age,
        distance_to_lower_boundary=(
            observation - contract.bracket_lower_boundary
            if observation is not None
            and contract.bracket_lower_boundary is not None
            else None
        ),
        distance_to_upper_boundary=(
            contract.bracket_upper_boundary - observation
            if observation is not None
            and contract.bracket_upper_boundary is not None
            else None
        ),
        bracket_impossible=impossible,
        bracket_reachable=not impossible,
        remaining_observation_hours=remaining_hours,
        remaining_daylight_hours=None,
        latest_forecast=ensemble_mean,
        forecast_run_time=_utc(alert.get("forecast_run_time")),
        forecast_availability_time=forecast_availability,
        forecast_age_seconds=forecast_age,
        forecast_provider_model="stored ensemble",
        ensemble_mean=ensemble_mean,
        ensemble_spread=_decimal(alert.get("ensemble_std")),
        model_disagreement=_decimal(alert.get("ensemble_std")),
        known_station_model_bias=_decimal(alert.get("known_bias")),
        forecast_revision=(
            ensemble_mean - previous_forecast
            if ensemble_mean is not None and previous_forecast is not None
            else None
        ),
        stale=(
            (
                observation_age is not None
                and observation_age > maximum_observation_age_seconds
            )
            or (
                forecast_age is not None
                and forecast_age > maximum_forecast_age_seconds
            )
        ),
    )


@dataclass(frozen=True)
class MarketState:
    buy_yes_price: Decimal | None
    buy_no_price: Decimal | None
    exit_yes_price: Decimal | None
    exit_no_price: Decimal | None
    spread: Decimal | None
    best_yes_quantity: Decimal
    best_no_quantity: Decimal
    depth_yes_through_maximum: Decimal
    depth_no_through_maximum: Decimal
    last_trade: Decimal | None
    quote_source_time: datetime | None
    quote_receipt_time: datetime
    quote_age_seconds: int | None
    recent_yes_price_change: Decimal | None
    volume: Decimal | None
    open_interest: Decimal | None
    seconds_until_close: int | None
    market_status: str | None
    exchange_active: bool
    exchange_trading_active: bool
    stale: bool
    crossed_or_impossible: bool
    missing_opposing_levels: bool

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(asdict(self))


def build_market_state(
    orderbook: MarketOrderbook,
    *,
    source_time: datetime | None,
    receipt_time: datetime,
    decision_time: datetime,
    close_time: datetime | None,
    market_status: str | None,
    maximum_yes_price: Decimal = Decimal("1"),
    maximum_no_price: Decimal = Decimal("1"),
    last_trade: Decimal | None = None,
    previous_yes_price: Decimal | None = None,
    volume: Decimal | None = None,
    open_interest: Decimal | None = None,
    exchange_active: bool = True,
    exchange_trading_active: bool = True,
    maximum_age_seconds: int = 30,
) -> MarketState:
    now = _utc(decision_time)
    received = _utc(receipt_time)
    published = _utc(source_time)
    assert now is not None and received is not None
    yes_asks = orderbook.asks_for("YES")
    no_asks = orderbook.asks_for("NO")
    yes_price = yes_asks[0].price if yes_asks else None
    no_price = no_asks[0].price if no_asks else None
    quote_age = int((now - received).total_seconds())
    crossed = (
        yes_price is not None
        and orderbook.best_yes_bid is not None
        and orderbook.best_yes_bid >= yes_price
    ) or (
        no_price is not None
        and orderbook.best_no_bid is not None
        and orderbook.best_no_bid >= no_price
    )
    return MarketState(
        buy_yes_price=yes_price,
        buy_no_price=no_price,
        exit_yes_price=orderbook.best_yes_bid,
        exit_no_price=orderbook.best_no_bid,
        spread=orderbook.yes_spread,
        best_yes_quantity=yes_asks[0].quantity if yes_asks else Decimal("0"),
        best_no_quantity=no_asks[0].quantity if no_asks else Decimal("0"),
        depth_yes_through_maximum=sum(
            (
                level.quantity
                for level in yes_asks
                if level.price <= maximum_yes_price
            ),
            Decimal("0"),
        ),
        depth_no_through_maximum=sum(
            (
                level.quantity
                for level in no_asks
                if level.price <= maximum_no_price
            ),
            Decimal("0"),
        ),
        last_trade=last_trade,
        quote_source_time=published,
        quote_receipt_time=received,
        quote_age_seconds=quote_age,
        recent_yes_price_change=(
            yes_price - previous_yes_price
            if yes_price is not None and previous_yes_price is not None
            else None
        ),
        volume=volume,
        open_interest=open_interest,
        seconds_until_close=(
            max(0, int((_utc(close_time) - now).total_seconds()))
            if close_time
            else None
        ),
        market_status=market_status,
        exchange_active=exchange_active,
        exchange_trading_active=exchange_trading_active,
        stale=quote_age > maximum_age_seconds,
        crossed_or_impossible=bool(crossed),
        missing_opposing_levels=yes_price is None or no_price is None,
    )


@dataclass(frozen=True)
class AccountState:
    current_position_side: str | None = None
    average_entry_price: Decimal | None = None
    contracts_held: Decimal = Decimal("0")
    executable_exit_value: Decimal | None = None
    realized_pnl: Decimal = Decimal("0")
    unrealized_pnl: Decimal = Decimal("0")
    fees_paid: Decimal = Decimal("0")
    open_bot_owned_orders: int = 0
    available_cash: Decimal | None = None
    bot_market_exposure: Decimal = Decimal("0")
    manual_market_exposure: Decimal = Decimal("0")
    event_date_exposure: Decimal = Decimal("0")
    total_bot_exposure: Decimal = Decimal("0")
    total_account_exposure: Decimal = Decimal("0")
    daily_realized_loss: Decimal = Decimal("0")
    consecutive_settled_losses: int = 0
    kill_switch: bool = False
    reconciliation_healthy: bool = False
    has_unknown_order: bool = False
    forward_evidence_sufficient: bool = False

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(asdict(self))


@dataclass(frozen=True)
class InformationEvent:
    information_event_id: str
    event_type: InformationEventType
    source: str
    source_event_time: datetime | None
    source_publication_time: datetime | None
    collector_receipt_time: datetime
    processing_time: datetime
    event_ticker: str
    market_ticker: str
    previous_value: Any
    new_value: Any
    material: bool
    related_decision_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(asdict(self))


@dataclass(frozen=True)
class MaterialityConfig:
    forecast_degrees: Decimal = Decimal("0.50")
    probability_points: Decimal = Decimal("0.02")
    spread_points: Decimal = Decimal("0.02")
    quantity_contracts: Decimal = Decimal("1")
    model_disagreement_degrees: Decimal = Decimal("0.50")


def make_information_event(
    *,
    event_type: InformationEventType,
    source: str,
    source_event_time: datetime | None,
    source_publication_time: datetime | None,
    collector_receipt_time: datetime,
    processing_time: datetime,
    event_ticker: str,
    market_ticker: str,
    previous_value: Any,
    new_value: Any,
    material: bool,
    related_decision_id: str | None = None,
) -> InformationEvent:
    canonical = json.dumps(
        _jsonable(
            {
                "type": event_type,
                "event": event_ticker,
                "market": market_ticker,
                "source_event_time": source_event_time,
                "source_publication_time": source_publication_time,
                "collector_receipt_time": collector_receipt_time,
                "previous": previous_value,
                "new": new_value,
            }
        ),
        sort_keys=True,
        separators=(",", ":"),
    )
    return InformationEvent(
        information_event_id=hashlib.sha256(canonical.encode()).hexdigest(),
        event_type=event_type,
        source=source,
        source_event_time=_utc(source_event_time),
        source_publication_time=_utc(source_publication_time),
        collector_receipt_time=_utc(collector_receipt_time),
        processing_time=_utc(processing_time),
        event_ticker=event_ticker,
        market_ticker=market_ticker,
        previous_value=previous_value,
        new_value=new_value,
        material=material,
        related_decision_id=related_decision_id,
    )


def detect_information_events(
    *,
    previous: Mapping[str, Any] | None,
    current: Mapping[str, Any],
    event_ticker: str,
    market_ticker: str,
    processing_time: datetime,
    config: MaterialityConfig = MaterialityConfig(),
) -> tuple[InformationEvent, ...]:
    previous = previous or {}
    emitted: list[InformationEvent] = []

    def add(
        event_type: InformationEventType,
        key: str,
        *,
        source: str,
        threshold: Decimal | None = None,
        always_material: bool = False,
    ) -> None:
        old = previous.get(key)
        new = current.get(key)
        if new is None or old == new:
            return
        if threshold is None:
            material = always_material
        elif old is None:
            material = True
        else:
            material = abs(_decimal(new) - _decimal(old)) >= threshold
        emitted.append(
            make_information_event(
                event_type=event_type,
                source=source,
                source_event_time=_utc(
                    current.get(f"{key}_event_time")
                    or current.get("source_event_time")
                ),
                source_publication_time=_utc(
                    current.get(f"{key}_publication_time")
                    or current.get("source_publication_time")
                ),
                collector_receipt_time=_utc(
                    current.get(f"{key}_receipt_time")
                    or current.get("collector_receipt_time")
                    or processing_time
                ),
                processing_time=processing_time,
                event_ticker=event_ticker,
                market_ticker=market_ticker,
                previous_value=old,
                new_value=new,
                material=material,
            )
        )

    add(
        (
            InformationEventType.FORECAST_RELEASED
            if previous.get("forecast_value") is None
            else InformationEventType.FORECAST_REVISED
        ),
        "forecast_value",
        source="weather_forecast",
        threshold=config.forecast_degrees,
    )
    add(
        (
            InformationEventType.OBSERVATION_PUBLISHED
            if previous.get("observation_value") is None
            else InformationEventType.OBSERVATION_REVISED
        ),
        "observation_value",
        source="official_observation",
        threshold=Decimal("0.1"),
    )
    metric = current.get("metric")
    if current.get("observed_daily_extreme") != previous.get(
        "observed_daily_extreme"
    ):
        add(
            (
                InformationEventType.NEW_DAILY_LOW
                if metric == "min"
                else InformationEventType.NEW_DAILY_HIGH
            ),
            "observed_daily_extreme",
            source="official_observation",
            always_material=True,
        )
    if (
        current.get("bracket_impossible") is True
        and previous.get("bracket_impossible") is not True
    ):
        add(
            InformationEventType.BRACKET_BECAME_IMPOSSIBLE,
            "bracket_impossible",
            source="derived_contract_state",
            always_material=True,
        )
    add(
        InformationEventType.MODEL_DISAGREEMENT_CHANGED,
        "model_disagreement",
        source="weather_forecast",
        threshold=config.model_disagreement_degrees,
    )
    add(
        InformationEventType.MARKET_PRICE_MOVED,
        "executable_yes_price",
        source="kalshi_orderbook",
        threshold=config.probability_points,
    )
    add(
        InformationEventType.ORDERBOOK_LIQUIDITY_CHANGED,
        "available_quantity",
        source="kalshi_orderbook",
        threshold=config.quantity_contracts,
    )
    add(
        InformationEventType.ORDERBOOK_LIQUIDITY_CHANGED,
        "spread",
        source="kalshi_orderbook",
        threshold=config.spread_points,
    )
    add(
        InformationEventType.MARKET_STATUS_CHANGED,
        "market_status",
        source="kalshi_market_lifecycle",
        always_material=True,
    )
    return tuple(emitted)


@dataclass(frozen=True)
class PositionState:
    side: str | None = None
    contracts: Decimal = Decimal("0")
    original_thesis: ThesisType | None = None
    thesis_invalidated: bool = False
    entry_information_event_id: str | None = None
    prior_entry_decision_id: str | None = None
    prior_exit_decision_id: str | None = None
    prior_exit_reason: str | None = None
    prior_exit_information_event_id: str | None = None

    @property
    def is_open(self) -> bool:
        return self.side in {"YES", "NO"} and self.contracts > 0

    @property
    def was_closed(self) -> bool:
        return (
            not self.is_open
            and self.prior_entry_decision_id is not None
            and self.prior_exit_decision_id is not None
        )


@dataclass(frozen=True)
class DecisionPolicy:
    margin_of_safety: Decimal = Decimal("0.05")
    maximum_spread: Decimal = Decimal("0.10")
    intended_quantity: Decimal = Decimal("1")
    maximum_order_size: Decimal = Decimal("1")


@dataclass(frozen=True)
class DecisionContext:
    contract: ContractTruth
    weather: WeatherState
    market: MarketState
    account: AccountState
    strategy_name: str
    strategy_version: str
    candidate_version: str
    decision_time: datetime
    model_yes_probability: Decimal
    market_yes_probability: Decimal
    working_yes_probability: Decimal
    uncertainty_indicator: str
    confidence_level: str
    probability_method_version: str
    triggering_event: InformationEvent | None = None
    signal_first_appeared: bool = False
    parent_decision_id: str | None = None
    position: PositionState = PositionState()
    risk_blockers: tuple[str, ...] = ()
    prospective_paper_only: bool = True
    expected_slippage: Decimal = Decimal("0")


@dataclass(frozen=True)
class TraderDecisionSnapshot:
    decision_id: str
    parent_decision_id: str | None
    event_ticker: str
    market_ticker: str
    strategy_name: str
    strategy_version: str
    candidate_version: str
    decision_time: datetime
    triggering_information_event_id: str | None
    information_as_of: Mapping[str, Any]
    probability: Mapping[str, Any]
    execution: Mapping[str, Any]
    thesis: Mapping[str, Any]
    action: TraderAction
    decision_reason_code: str
    net_edge_after_costs: Decimal | None
    risk_amount: Decimal
    maximum_loss: Decimal
    expected_value: Decimal | None
    confidence_level: str
    blockers: tuple[str, ...]
    next_review_trigger: str
    pretrade_checklist: Mapping[str, Any]
    production_order_allowed: bool
    immutable: bool = True

    def __post_init__(self) -> None:
        for name in (
            "information_as_of",
            "probability",
            "execution",
            "thesis",
            "pretrade_checklist",
        ):
            object.__setattr__(
                self, name, _immutable_mapping(getattr(self, name))
            )

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(
            {field.name: getattr(self, field.name) for field in fields(self)}
        )


def snapshot_from_dict(value: Mapping[str, Any]) -> TraderDecisionSnapshot:
    return TraderDecisionSnapshot(
        decision_id=str(value["decision_id"]),
        parent_decision_id=value.get("parent_decision_id"),
        event_ticker=str(value["event_ticker"]),
        market_ticker=str(value["market_ticker"]),
        strategy_name=str(value["strategy_name"]),
        strategy_version=str(value["strategy_version"]),
        candidate_version=str(value["candidate_version"]),
        decision_time=_utc(value["decision_time"]),
        triggering_information_event_id=value.get(
            "triggering_information_event_id"
        ),
        information_as_of=dict(value["information_as_of"]),
        probability=dict(value["probability"]),
        execution=dict(value["execution"]),
        thesis=dict(value["thesis"]),
        action=TraderAction(value["action"]),
        decision_reason_code=str(value["decision_reason_code"]),
        net_edge_after_costs=_decimal(value.get("net_edge_after_costs")),
        risk_amount=_decimal(value["risk_amount"]) or Decimal("0"),
        maximum_loss=_decimal(value["maximum_loss"]) or Decimal("0"),
        expected_value=_decimal(value.get("expected_value")),
        confidence_level=str(value["confidence_level"]),
        blockers=tuple(value.get("blockers") or ()),
        next_review_trigger=str(value["next_review_trigger"]),
        pretrade_checklist=dict(value["pretrade_checklist"]),
        production_order_allowed=bool(value["production_order_allowed"]),
        immutable=bool(value.get("immutable", True)),
    )


def _information_thesis(context: DecisionContext) -> ThesisType:
    event = context.triggering_event
    if event:
        if event.event_type in {
            InformationEventType.FORECAST_RELEASED,
            InformationEventType.FORECAST_REVISED,
        }:
            return ThesisType.NEW_FORECAST_INFORMATION
        if event.event_type in {
            InformationEventType.OBSERVATION_PUBLISHED,
            InformationEventType.OBSERVATION_REVISED,
            InformationEventType.NEW_DAILY_HIGH,
            InformationEventType.NEW_DAILY_LOW,
        }:
            return ThesisType.NEW_OBSERVATION_INFORMATION
        if event.event_type == InformationEventType.BRACKET_BECAME_IMPOSSIBLE:
            return ThesisType.BRACKET_IMPOSSIBILITY
        if event.event_type == InformationEventType.MARKET_PRICE_MOVED:
            return ThesisType.MARKET_MOVED_TOO_FAR
    if context.signal_first_appeared:
        return ThesisType.MODEL_MARKET_DISAGREEMENT
    return ThesisType.NO_IDENTIFIABLE_INFORMATION_ADVANTAGE


def _timing_blockers(context: DecisionContext) -> list[str]:
    blockers: list[str] = []
    decision_time = _utc(context.decision_time)
    assert decision_time is not None
    timestamps = {
        "FUTURE_FORECAST": context.weather.forecast_availability_time,
        "FUTURE_OBSERVATION": context.weather.observation_publication_time,
        "FUTURE_MARKET_PRICE": context.market.quote_receipt_time,
    }
    for reason, value in timestamps.items():
        if value and _utc(value) > decision_time:
            blockers.append(reason)
    if context.weather.stale:
        blockers.append("DATA_STALE")
    if context.market.stale:
        blockers.append("QUOTE_STALE")
    if (
        context.triggering_event
        and context.triggering_event.event_type
        in {
            InformationEventType.SETTLEMENT_PUBLISHED,
            InformationEventType.SETTLEMENT_REVISED,
        }
    ):
        blockers.append("SETTLEMENT_INFORMATION_LEAKAGE")
    return blockers


def _base_blockers(
    context: DecisionContext,
    *,
    side: str,
    entry_price: Decimal | None,
    entry_quantity: Decimal,
    policy: DecisionPolicy,
) -> tuple[list[str], list[str]]:
    hard = list(context.contract.critical_issues)
    hard.extend(_timing_blockers(context))
    if context.contract.market_status not in {"active", "open"}:
        hard.append("MARKET_CLOSED")
    if not context.market.exchange_active or not context.market.exchange_trading_active:
        hard.append("EXCHANGE_INACTIVE")
    if context.market.crossed_or_impossible:
        hard.append("INVALID_ORDERBOOK")
    if context.market.missing_opposing_levels:
        hard.append("MISSING_OPPOSING_LEVELS")
    if not (
        Decimal("0") <= context.model_yes_probability <= Decimal("1")
        and Decimal("0") <= context.market_yes_probability <= Decimal("1")
        and Decimal("0") <= context.working_yes_probability <= Decimal("1")
    ):
        hard.append("INVALID_PROBABILITY")
    if (
        context.weather.bracket_impossible
        and context.working_yes_probability != 0
    ):
        hard.append("IMPOSSIBLE_BRACKET_NONZERO_PROBABILITY")
    if context.account.kill_switch:
        hard.append("KILL_SWITCH_ACTIVE")
    if context.account.has_unknown_order:
        hard.append("UNKNOWN_ORDER")
    if not context.account.reconciliation_healthy:
        hard.append("RECONCILIATION_REQUIRED")
    estimated_maximum_loss = (
        entry_price * policy.intended_quantity
        + estimated_taker_fee(entry_price, policy.intended_quantity)
        if entry_price is not None
        else None
    )
    if (
        context.account.available_cash is None
        or context.account.available_cash <= Decimal("5")
        or (
            estimated_maximum_loss is not None
            and context.account.available_cash < estimated_maximum_loss
        )
    ):
        hard.append("CAPITAL_INSUFFICIENT")
    hard.extend(context.risk_blockers)
    soft: list[str] = []
    if entry_price is None:
        soft.append("QUANTITY_UNAVAILABLE")
    if entry_quantity < policy.intended_quantity:
        soft.append("LIQUIDITY_INSUFFICIENT")
    if (
        context.market.spread is not None
        and context.market.spread > policy.maximum_spread
    ):
        soft.append("SPREAD_TOO_WIDE")
    if policy.intended_quantity > policy.maximum_order_size:
        hard.append("MAXIMUM_ORDER_SIZE")
    if context.account.open_bot_owned_orders:
        hard.append("DUPLICATE_OR_RESTING_ORDER")
    if context.account.current_position_side == side:
        hard.append("EQUIVALENT_POSITION_EXISTS")
    return list(dict.fromkeys(hard)), list(dict.fromkeys(soft))


def _checklist(
    context: DecisionContext,
    *,
    thesis: ThesisType,
    entry_price: Decimal | None,
    entry_quantity: Decimal,
    net_edge: Decimal | None,
    maximum_price: Decimal | None,
    blockers: tuple[str, ...],
) -> dict[str, Any]:
    contract_ok = context.contract.status == "CLEAR"
    timing_ok = not _timing_blockers(context)
    probability_ok = all(
        Decimal("0") <= value <= Decimal("1")
        for value in (
            context.model_yes_probability,
            context.market_yes_probability,
            context.working_yes_probability,
        )
    )
    price_ok = (
        entry_price is not None
        and maximum_price is not None
        and entry_price <= maximum_price
        and entry_quantity > 0
    )
    thesis_ok = thesis != ThesisType.NO_IDENTIFIABLE_INFORMATION_ADVANTAGE
    risk_ok = (
        context.account.reconciliation_healthy
        and not context.account.kill_switch
        and not context.account.has_unknown_order
        and not context.risk_blockers
    )
    return {
        "CONTRACT": {
            "settlement_truth_complete": contract_ok,
            "station_correct": context.contract.station_identifier is not None,
            "bracket_boundaries_clear": not any(
                "BRACKET" in issue for issue in context.contract.critical_issues
            ),
            "market_open": context.contract.market_status in {"active", "open"},
        },
        "INFORMATION": {
            "triggering_information_event": (
                context.triggering_event.event_type.value
                if context.triggering_event
                else None
            ),
            "source_publication_time": (
                context.triggering_event.source_publication_time
                if context.triggering_event
                else None
            ),
            "collector_receipt_time": (
                context.triggering_event.collector_receipt_time
                if context.triggering_event
                else None
            ),
            "point_in_time_valid": timing_ok,
        },
        "PROBABILITY": {
            "fair_yes_probability": context.working_yes_probability,
            "uncertainty": context.uncertainty_indicator,
            "cohort_reliability": context.confidence_level,
            "probabilities_valid": probability_ok,
            "impossible_outcomes_zeroed": (
                not context.weather.bracket_impossible
                or context.working_yes_probability == 0
            ),
        },
        "PRICE": {
            "executable_entry_price": entry_price,
            "spread": context.market.spread,
            "quantity_available": entry_quantity,
            "net_edge": net_edge,
            "maximum_acceptable_price": maximum_price,
            "executable_and_within_limit": price_ok,
        },
        "THESIS": {
            "classification": thesis.value,
            "specific_information_advantage": thesis_ok,
        },
        "RISK": {
            "available_cash": context.account.available_cash,
            "total_account_exposure": context.account.total_account_exposure,
            "daily_realized_loss": context.account.daily_realized_loss,
            "consecutive_losses": context.account.consecutive_settled_losses,
            "kill_switch_clear": not context.account.kill_switch,
            "reconciliation_healthy": context.account.reconciliation_healthy,
            "risk_checks_pass": risk_ok,
        },
        "ACTION": {
            "blockers": blockers,
            "prospective_paper_only": context.prospective_paper_only,
        },
    }


def select_professional_decision(
    context: DecisionContext,
    *,
    policy: DecisionPolicy = DecisionPolicy(),
) -> TraderDecisionSnapshot:
    thesis = _information_thesis(context)
    model_no = Decimal("1") - context.model_yes_probability
    market_no = Decimal("1") - context.market_yes_probability
    working_no = Decimal("1") - context.working_yes_probability
    desired_side = (
        "YES"
        if context.working_yes_probability - context.market_yes_probability >= 0
        else "NO"
    )
    fair_side = (
        context.working_yes_probability
        if desired_side == "YES"
        else working_no
    )
    entry_price = (
        context.market.buy_yes_price
        if desired_side == "YES"
        else context.market.buy_no_price
    )
    entry_quantity = (
        context.market.depth_yes_through_maximum
        if desired_side == "YES"
        else context.market.depth_no_through_maximum
    )
    fee = (
        estimated_taker_fee(entry_price, policy.intended_quantity)
        if entry_price is not None
        else Decimal("0")
    )
    gross_edge = (
        fair_side - entry_price if entry_price is not None else None
    )
    net_edge = (
        gross_edge - fee - context.expected_slippage
        if gross_edge is not None
        else None
    )
    maximum_price = (
        fair_side
        - policy.margin_of_safety
        - fee
        - context.expected_slippage
        if entry_price is not None
        else None
    )
    hard, soft = _base_blockers(
        context,
        side=desired_side,
        entry_price=entry_price,
        entry_quantity=entry_quantity,
        policy=policy,
    )
    if thesis == ThesisType.NO_IDENTIFIABLE_INFORMATION_ADVANTAGE:
        hard.append("NO_INFORMATION_ADVANTAGE")
    if net_edge is None or net_edge <= 0:
        hard.append("EDGE_BELOW_COSTS")
    elif net_edge < policy.margin_of_safety:
        soft.append("EDGE_BELOW_MARGIN_OF_SAFETY")
    if (
        entry_price is not None
        and maximum_price is not None
        and entry_price > maximum_price
    ):
        soft.append("PRICE_ABOVE_MAXIMUM")

    position = context.position
    if position.is_open:
        held_fair = (
            context.working_yes_probability
            if position.side == "YES"
            else working_no
        )
        held_exit = (
            context.market.exit_yes_price
            if position.side == "YES"
            else context.market.exit_no_price
        )
        remaining_edge = (
            held_fair - held_exit if held_exit is not None else None
        )
        if context.account.kill_switch:
            action = TraderAction.EXIT
            reason = "KILL_SWITCH_TRIGGERED"
        elif context.risk_blockers:
            action = TraderAction.EXIT
            reason = context.risk_blockers[0]
        elif (
            not context.account.reconciliation_healthy
            or context.account.has_unknown_order
        ):
            action = TraderAction.EXIT
            reason = "RISK_LIMIT_TRIGGERED"
        elif context.contract.status != "CLEAR":
            action = TraderAction.EXIT
            reason = "THESIS_INVALIDATED"
        elif position.thesis_invalidated:
            action = TraderAction.EXIT
            reason = "THESIS_INVALIDATED"
        elif held_exit is None:
            action = TraderAction.EXIT
            reason = "LIQUIDITY_DROPPED"
        elif context.weather.stale or context.market.stale:
            action = TraderAction.EXIT
            reason = "DATA_BECAME_STALE"
        elif remaining_edge is not None and remaining_edge <= 0:
            action = TraderAction.EXIT
            reason = "NET_EDGE_GONE"
        else:
            action = TraderAction.HOLD
            reason = "THESIS_REMAINS_VALID"
        selected_entry_price = held_exit
        selected_quantity = position.contracts
        selected_net_edge = remaining_edge
        selected_fee = (
            estimated_taker_fee(held_exit, position.contracts)
            if held_exit is not None
            else Decimal("0")
        )
        maximum_loss = (
            position.contracts
            * (context.account.average_entry_price or Decimal("0"))
        )
        blockers = tuple(hard + soft)
    elif position.was_closed:
        price_only_event = (
            context.triggering_event is not None
            and context.triggering_event.event_type
            in {
                InformationEventType.MARKET_PRICE_MOVED,
                InformationEventType.ORDERBOOK_LIQUIDITY_CHANGED,
            }
        )
        new_information = (
            context.triggering_event is not None
            and context.triggering_event.information_event_id
            != position.entry_information_event_id
            and context.triggering_event.information_event_id
            != position.prior_exit_information_event_id
            and not price_only_event
        )
        risk_exit = (position.prior_exit_reason or "").endswith("TRIGGERED")
        if not new_information:
            action = TraderAction.DO_NOT_TRADE
            reason = "REENTRY_REQUIRES_NEW_INFORMATION"
            hard.append(reason)
        elif risk_exit and (context.risk_blockers or context.account.kill_switch):
            action = TraderAction.DO_NOT_TRADE
            reason = "RISK_EXIT_STILL_ACTIVE"
            hard.append(reason)
        elif hard:
            action = TraderAction.DO_NOT_TRADE
            reason = hard[0]
        elif soft:
            action = TraderAction.WATCH
            reason = soft[0]
        else:
            action = (
                TraderAction.REBUY_YES
                if desired_side == "YES"
                else TraderAction.REBUY_NO
            )
            reason = "NEW_INFORMATION_CREATED_NEW_THESIS"
        selected_entry_price = entry_price
        selected_quantity = policy.intended_quantity
        selected_net_edge = net_edge
        selected_fee = fee
        maximum_loss = (
            (entry_price or Decimal("0")) * policy.intended_quantity + fee
        )
        blockers = tuple(dict.fromkeys(hard + soft))
    else:
        if hard:
            action = TraderAction.DO_NOT_TRADE
            reason = hard[0]
        elif soft:
            action = TraderAction.WATCH
            reason = soft[0]
        else:
            action = (
                TraderAction.BUY_YES
                if desired_side == "YES"
                else TraderAction.BUY_NO
            )
            reason = "ENTRY_RULES_PASSED"
        selected_entry_price = entry_price
        selected_quantity = policy.intended_quantity
        selected_net_edge = net_edge
        selected_fee = fee
        maximum_loss = (
            (entry_price or Decimal("0")) * policy.intended_quantity + fee
        )
        blockers = tuple(dict.fromkeys(hard + soft))

    next_information = (
        "next official station observation or forecast revision"
        if context.contract.target_variable in {"daily high", "daily low"}
        else "next material official source update"
    )
    invalidation = (
        "official observation or forecast revision removes the remaining "
        "fee-adjusted edge, contract truth becomes unclear, data becomes stale, "
        "or a risk gate activates"
    )
    counterargument = (
        "Kalshi may have incorporated the same weather information before our "
        "collector received it, and the current model has not shown stable "
        "incremental value beyond market prices."
    )
    thesis_text = (
        f"{thesis.value.replace('_', ' ').title()}: working fair value for "
        f"{desired_side} differs from executable price after costs."
    )
    checklist = _checklist(
        context,
        thesis=thesis,
        entry_price=selected_entry_price,
        entry_quantity=selected_quantity,
        net_edge=selected_net_edge,
        maximum_price=maximum_price,
        blockers=blockers,
    )
    production_allowed = (
        action in TRADE_ACTIONS
        and not blockers
        and not context.prospective_paper_only
        and context.account.forward_evidence_sufficient
        and context.account.reconciliation_healthy
    )
    if action in TRADE_ACTIONS and context.prospective_paper_only:
        checklist["ACTION"]["live_blocker"] = "FORWARD_EVIDENCE_INSUFFICIENT"

    stable = json.dumps(
        _jsonable(
            {
                "event": context.contract.event_ticker,
                "market": context.contract.market_ticker,
                "strategy": context.strategy_version,
                "candidate": context.candidate_version,
                "parent": context.parent_decision_id,
                "trigger": (
                    context.triggering_event.information_event_id
                    if context.triggering_event
                    else "INITIAL_SIGNAL"
                ),
                "decision_time": context.decision_time,
                "action": action,
            }
        ),
        sort_keys=True,
        separators=(",", ":"),
    )
    decision_id = hashlib.sha256(stable.encode()).hexdigest()
    return TraderDecisionSnapshot(
        decision_id=decision_id,
        parent_decision_id=context.parent_decision_id,
        event_ticker=context.contract.event_ticker,
        market_ticker=context.contract.market_ticker,
        strategy_name=context.strategy_name,
        strategy_version=context.strategy_version,
        candidate_version=context.candidate_version,
        decision_time=_utc(context.decision_time),
        triggering_information_event_id=(
            context.triggering_event.information_event_id
            if context.triggering_event
            else None
        ),
        information_as_of={
            "forecast_timestamp": context.weather.forecast_availability_time,
            "observation_timestamp": context.weather.observation_publication_time,
            "quote_timestamp": context.market.quote_receipt_time,
            "data_freshness_status": (
                "STALE"
                if context.weather.stale or context.market.stale
                else "FRESH"
            ),
            "contract_truth_status": context.contract.status,
        },
        probability={
            "model_yes_probability": context.model_yes_probability,
            "model_no_probability": model_no,
            "market_implied_yes_probability": context.market_yes_probability,
            "market_implied_no_probability": market_no,
            "final_working_yes_probability": context.working_yes_probability,
            "final_working_no_probability": working_no,
            "uncertainty_indicator": context.uncertainty_indicator,
            "probability_method_version": context.probability_method_version,
        },
        execution={
            "buy_yes_price": context.market.buy_yes_price,
            "buy_no_price": context.market.buy_no_price,
            "executable_exit_price": selected_entry_price,
            "estimated_fees": selected_fee,
            "expected_slippage": context.expected_slippage,
            "quantity_available": entry_quantity,
            "maximum_acceptable_entry_price": maximum_price,
            "order_quantity": selected_quantity,
            "gross_edge": gross_edge,
        },
        thesis={
            "classification": thesis.value,
            "summary": thesis_text,
            "new_information": (
                context.triggering_event.to_dict()
                if context.triggering_event
                else {"type": "INITIAL_CANDIDATE_SIGNAL"}
            ),
            "reason_market_may_be_mispriced": thesis_text,
            "strongest_reason_market_may_be_correct": counterargument,
            "information_already_reflected": (
                "current executable orderbook and market-implied probability"
            ),
            "expected_next_information_event": next_information,
            "invalidation_conditions": invalidation,
        },
        action=action,
        decision_reason_code=reason,
        net_edge_after_costs=selected_net_edge,
        risk_amount=maximum_loss,
        maximum_loss=maximum_loss,
        expected_value=(
            selected_net_edge * selected_quantity
            if selected_net_edge is not None
            else None
        ),
        confidence_level=context.confidence_level,
        blockers=blockers,
        next_review_trigger=next_information,
        pretrade_checklist=checklist,
        production_order_allowed=production_allowed,
    )


@dataclass(frozen=True)
class JournalEvent:
    journal_event_id: str
    event_ticker: str
    market_ticker: str
    candidate_version: str
    record_type: str
    record_id: str
    parent_record_type: str | None
    parent_record_id: str | None
    event_time: datetime | None
    source_publication_time: datetime | None
    collector_receipt_time: datetime | None
    processing_time: datetime
    decision_time: datetime | None = None
    order_time: datetime | None = None
    fill_time: datetime | None = None
    settlement_time: datetime | None = None
    payload: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(asdict(self))


def make_journal_event(
    *,
    event_ticker: str,
    market_ticker: str,
    candidate_version: str,
    record_type: str,
    record_id: str,
    processing_time: datetime,
    parent_record_type: str | None = None,
    parent_record_id: str | None = None,
    event_time: datetime | None = None,
    source_publication_time: datetime | None = None,
    collector_receipt_time: datetime | None = None,
    decision_time: datetime | None = None,
    order_time: datetime | None = None,
    fill_time: datetime | None = None,
    settlement_time: datetime | None = None,
    payload: dict[str, Any] | None = None,
) -> JournalEvent:
    stable = json.dumps(
        _jsonable(
            {
                "record_type": record_type,
                "record_id": record_id,
                "parent_record_type": parent_record_type,
                "parent_record_id": parent_record_id,
                "market": market_ticker,
            }
        ),
        sort_keys=True,
        separators=(",", ":"),
    )
    return JournalEvent(
        journal_event_id=hashlib.sha256(stable.encode()).hexdigest(),
        event_ticker=event_ticker,
        market_ticker=market_ticker,
        candidate_version=candidate_version,
        record_type=record_type,
        record_id=record_id,
        parent_record_type=parent_record_type,
        parent_record_id=parent_record_id,
        event_time=_utc(event_time),
        source_publication_time=_utc(source_publication_time),
        collector_receipt_time=_utc(collector_receipt_time),
        processing_time=_utc(processing_time),
        decision_time=_utc(decision_time),
        order_time=_utc(order_time),
        fill_time=_utc(fill_time),
        settlement_time=_utc(settlement_time),
        payload=payload or {},
    )


@dataclass(frozen=True)
class PostTradeReview:
    review_id: str
    decision_id: str
    market_ticker: str
    classification: ReviewClassification
    settled_outcome: str
    process_correct: bool
    outcome_favorable: bool
    probability_at_entry: Decimal
    executable_price_at_entry: Decimal | None
    closing_or_exit_price: Decimal | None
    forecast_error: Decimal | None
    calibration_contribution: Decimal | None
    execution_cost: Decimal | None
    thesis_remained_valid: bool | None
    exit_followed_plan: bool | None
    reentry_used_new_information: bool | None
    data_was_fresh: bool
    rules_bypassed: tuple[str, ...]
    settlement_revision: bool
    reviewed_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(asdict(self))


def review_decision_process(
    snapshot: TraderDecisionSnapshot,
    *,
    settled_outcome: str,
    reviewed_at: datetime,
    closing_or_exit_price: Decimal | None = None,
    forecast_error: Decimal | None = None,
    calibration_contribution: Decimal | None = None,
    execution_cost: Decimal | None = None,
    thesis_remained_valid: bool | None = None,
    exit_followed_plan: bool | None = None,
    reentry_used_new_information: bool | None = None,
    rules_bypassed: tuple[str, ...] = (),
    settlement_revision: bool = False,
) -> PostTradeReview:
    trade_side = (
        "YES"
        if snapshot.action
        in {TraderAction.BUY_YES, TraderAction.REBUY_YES}
        else (
            "NO"
            if snapshot.action
            in {TraderAction.BUY_NO, TraderAction.REBUY_NO}
            else None
        )
    )
    outcome_favorable = trade_side == settled_outcome.upper()
    checklist = snapshot.pretrade_checklist
    process_correct = (
        snapshot.action in TRADE_ACTIONS
        and not rules_bypassed
        and snapshot.net_edge_after_costs is not None
        and snapshot.net_edge_after_costs > 0
        and checklist["CONTRACT"]["settlement_truth_complete"]
        and checklist["INFORMATION"]["point_in_time_valid"]
        and checklist["PROBABILITY"]["probabilities_valid"]
        and checklist["THESIS"]["specific_information_advantage"]
    )
    if trade_side is None:
        classification = ReviewClassification.INSUFFICIENT_EVIDENCE
    elif process_correct and outcome_favorable:
        classification = ReviewClassification.GOOD_DECISION_GOOD_OUTCOME
    elif process_correct:
        classification = ReviewClassification.GOOD_DECISION_BAD_OUTCOME
    elif outcome_favorable:
        classification = ReviewClassification.BAD_DECISION_GOOD_OUTCOME
    else:
        classification = ReviewClassification.BAD_DECISION_BAD_OUTCOME
    canonical = json.dumps(
        {
            "decision_id": snapshot.decision_id,
            "outcome": settled_outcome.upper(),
            "reviewed_at": _utc(reviewed_at).isoformat(),
            "revision": settlement_revision,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return PostTradeReview(
        review_id=hashlib.sha256(canonical.encode()).hexdigest(),
        decision_id=snapshot.decision_id,
        market_ticker=snapshot.market_ticker,
        classification=classification,
        settled_outcome=settled_outcome.upper(),
        process_correct=process_correct,
        outcome_favorable=outcome_favorable,
        probability_at_entry=Decimal(
            str(
                snapshot.probability[
                    (
                        "final_working_yes_probability"
                        if trade_side == "YES"
                        else "final_working_no_probability"
                    )
                ]
            )
        ),
        executable_price_at_entry=_decimal(
            snapshot.execution[
                "buy_yes_price" if trade_side == "YES" else "buy_no_price"
            ]
        ),
        closing_or_exit_price=closing_or_exit_price,
        forecast_error=forecast_error,
        calibration_contribution=calibration_contribution,
        execution_cost=execution_cost,
        thesis_remained_valid=thesis_remained_valid,
        exit_followed_plan=exit_followed_plan,
        reentry_used_new_information=reentry_used_new_information,
        data_was_fresh=(
            snapshot.information_as_of["data_freshness_status"] == "FRESH"
        ),
        rules_bypassed=rules_bypassed,
        settlement_revision=settlement_revision,
        reviewed_at=_utc(reviewed_at),
    )


def material_action_alert(snapshot: TraderDecisionSnapshot) -> str | None:
    if snapshot.action in TRADE_ACTIONS:
        return (
            "REENTRY_READY"
            if snapshot.action
            in {TraderAction.REBUY_YES, TraderAction.REBUY_NO}
            else "TRADE_READY"
        )
    if snapshot.action == TraderAction.EXIT:
        return (
            "POSITION_INVALIDATED"
            if snapshot.decision_reason_code == "THESIS_INVALIDATED"
            else "EXIT_RECOMMENDED"
        )
    if "RECONCILIATION_REQUIRED" in snapshot.blockers:
        return "RECONCILIATION_REQUIRED"
    if any("STALE" in blocker for blocker in snapshot.blockers):
        return "DATA_STALE"
    if snapshot.blockers and any(
        blocker
        in {
            "KILL_SWITCH_ACTIVE",
            "CAPITAL_INSUFFICIENT",
            "MAX_DAILY_REALIZED_LOSS",
            "MAX_DAILY_MARK_TO_MARKET_LOSS",
            "MAXIMUM_ORDER_SIZE",
            "UNKNOWN_ORDER",
        }
        or "RISK" in blocker
        or "EXPOSURE" in blocker
        or "LOSS_LIMIT" in blocker
        for blocker in snapshot.blockers
    ):
        return "RISK_BLOCKED"
    return None
