from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Callable

from psycopg.types.json import Jsonb

from kalshi_client import MarketOrderbook, parse_event_date
from kalshi_client.fees import taker_fee
from weather.stations import STATIONS

from .config import ReadinessConfig
from .execution import conservative_fill
from .freeze import FrozenCandidate
from .metrics import blend_probability
from .stream import OrderbookState, SequenceGap

WS_PRODUCTION_URL = "wss://external-api-ws.kalshi.com/trade-api/ws/v2"
WS_DEMO_URL = "wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2"


class PostgresEvidenceRepository:
    """Append-only persistence for confirmatory evidence and paper events."""

    def __init__(self, connection: Any):
        self.connection = connection

    def watched_tickers(self) -> list[str]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select distinct market_ticker from alerts "
                "where settled_at is null and close_time > now() "
                "order by market_ticker"
            )
            return [row[0] for row in cursor.fetchall()]

    def latest_alerts(self) -> list[dict[str, Any]]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select distinct on (market_ticker) id, created_at, series_ticker, "
                "event_ticker, market_ticker, city, model_version, ensemble_mean, "
                "ensemble_std, observed_so_far, model_probability, lead_days, close_time "
                ", forecast_run_time, forecast_availability_time, "
                "observation_event_time, observation_publication_time, "
                "observation_collector_received_time "
                "from alerts where settled_at is null and close_time > now() "
                "order by market_ticker, created_at desc"
            )
            columns = [item.name for item in cursor.description]
            return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]

    def insert_freezes(self, candidates: tuple[FrozenCandidate, ...]) -> None:
        with self.connection.cursor() as cursor:
            for candidate in candidates:
                cursor.execute(
                    "insert into forward_candidate_freezes "
                    "(strategy_name, strategy_version, model_weight, market_weight, "
                    "calibration_method, signal_threshold, no_trade_filters, "
                    "maximum_acceptable_price_logic, frozen_at, confirmatory_period_start, "
                    "required_independent_event_count, code_config_hash) "
                    "values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                    "on conflict (strategy_version) do nothing",
                    (
                        candidate.strategy_name,
                        candidate.strategy_version,
                        candidate.model_weight,
                        candidate.market_weight,
                        candidate.calibration_method,
                        candidate.signal_threshold,
                        Jsonb(list(candidate.no_trade_filters)),
                        candidate.maximum_acceptable_price_logic,
                        candidate.candidate_freeze_timestamp,
                        candidate.confirmatory_period_start,
                        candidate.required_independent_event_count,
                        candidate.code_config_hash,
                    ),
                )

    def append_orderbook(
        self,
        state: OrderbookState,
        *,
        source: str,
        market_status: str | None,
        volume: Decimal | None = None,
        open_interest: Decimal | None = None,
        last_trade: Decimal | None = None,
        sequence_gap: bool = False,
        diagnostics: dict[str, bool],
    ) -> int:
        book = state.as_orderbook()
        levels = {
            "yes_bids": [
                {"price": str(level.price), "quantity": str(level.quantity)}
                for level in book.yes_bids
            ],
            "no_bids": [
                {"price": str(level.price), "quantity": str(level.quantity)}
                for level in book.no_bids
            ],
        }
        with self.connection.cursor() as cursor:
            cursor.execute(
                "insert into forward_orderbook_snapshots "
                "(market_ticker, source, source_publish_time, collector_received_time, "
                "sequence_number, recovery_reason, best_yes_bid, best_yes_ask, "
                "best_no_bid, best_no_ask, spread, depth_levels, last_trade, "
                "market_status, volume, open_interest, stale, crossed_or_impossible, "
                "missing_opposing_levels, sequence_gap, delayed_local_receipt) "
                "values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
                "%s, %s, %s, %s, %s, %s, %s, %s) returning id",
                (
                    state.ticker,
                    source,
                    state.source_publish_time,
                    state.collector_received_time,
                    state.last_sequence,
                    state.recovery_reason,
                    book.best_yes_bid,
                    book.best_yes_ask,
                    book.best_no_bid,
                    book.best_no_ask,
                    book.yes_spread,
                    Jsonb(levels),
                    last_trade,
                    market_status,
                    volume,
                    open_interest,
                    diagnostics["stale"],
                    diagnostics["crossed_or_impossible"],
                    diagnostics["missing_opposing_levels"],
                    sequence_gap,
                    diagnostics["delayed_local_receipt"],
                ),
            )
            return cursor.fetchone()[0]

    def append_stream_message(
        self,
        message: dict[str, Any],
        *,
        received_at: datetime,
        gap_detected: bool,
    ) -> None:
        payload = message.get("msg") or {}
        with self.connection.cursor() as cursor:
            cursor.execute(
                "insert into forward_market_messages "
                "(message_type, subscription_id, sequence_number, market_ticker, "
                "source_publish_time, collector_received_time, gap_detected, payload) "
                "values (%s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    message.get("type", "unknown"),
                    message.get("sid"),
                    message.get("seq"),
                    payload.get("market_ticker"),
                    _payload_time(payload),
                    received_at,
                    gap_detected,
                    Jsonb(message),
                ),
            )

    def append_decision(self, decision: dict[str, Any]) -> int:
        columns = list(decision)
        placeholders = ", ".join(f"%({column})s" for column in columns)
        with self.connection.cursor() as cursor:
            cursor.execute(
                f"insert into forward_evidence_decisions "
                f"({', '.join(columns)}) values ({placeholders}) returning id",
                {
                    **decision,
                    "forecast_values": Jsonb(decision["forecast_values"]),
                },
            )
            return cursor.fetchone()[0]

    def append_paper_event(self, event: dict[str, Any]) -> None:
        columns = list(event)
        placeholders = ", ".join(f"%({column})s" for column in columns)
        with self.connection.cursor() as cursor:
            cursor.execute(
                f"insert into forward_paper_order_events "
                f"({', '.join(columns)}) values ({placeholders})",
                event,
            )

    def unsettled_paper_fills(self) -> list[dict[str, Any]]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select d.id, d.market_ticker, d.selected_side, e.candidate_version, "
                "e.requested_quantity, e.filled_quantity, e.weighted_fill_price, "
                "e.estimated_fee from forward_evidence_decisions d "
                "join lateral (select * from forward_paper_order_events pe "
                "where pe.decision_row_id = d.id order by pe.created_at desc limit 1) e "
                "on true where e.event_type in ('FILLED', 'PARTIAL_FILL')"
            )
            columns = [item.name for item in cursor.description]
            return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


class ForwardEvidenceCollector:
    def __init__(
        self,
        *,
        client: Any,
        repository: Any,
        candidates: tuple[FrozenCandidate, ...],
        config: ReadinessConfig,
        now: Callable[[], datetime] | None = None,
    ):
        self.client = client
        self.repository = repository
        self.candidates = candidates
        self.config = config
        self.now = now or (lambda: datetime.now(UTC))
        self.books: dict[str, OrderbookState] = {}
        self.sequence_by_subscription: dict[int, int] = {}
        self.market_status: dict[str, str] = {}

    def _diagnostics(self, state: OrderbookState) -> dict[str, bool]:
        return state.diagnostics(
            now=self.now(),
            stale_after=timedelta(
                seconds=self.config.maximum_orderbook_age_seconds
            ),
            delayed_after=timedelta(
                seconds=self.config.maximum_source_receipt_delay_seconds
            ),
        )

    def capture_rest_books(
        self,
        tickers: list[str],
        *,
        recovery_reason: str | None = None,
    ) -> dict[str, int]:
        if not tickers:
            return {}
        books: list[MarketOrderbook] = []
        for start in range(0, len(tickers), 100):
            batch = tickers[start : start + 100]
            try:
                books.extend(self.client.get_orderbooks(batch))
            except Exception:
                books.extend(
                    self.client.get_orderbook(
                        ticker,
                        depth=self.config.configured_depth_levels,
                    )
                    for ticker in batch
                )
        identifiers: dict[str, int] = {}
        for book in books:
            try:
                market = self.client.get_market(book.ticker)
                market_status = market.status
            except Exception:
                market = None
                market_status = None
            if market_status:
                self.market_status[book.ticker] = market_status
            state = OrderbookState.from_rest(
                book,
                received_at=self.now(),
                recovery_reason=recovery_reason,
            )
            self.books[book.ticker] = state
            identifiers[book.ticker] = self.repository.append_orderbook(
                state,
                source="REST_RECOVERY" if recovery_reason else "REST_INITIAL",
                market_status=market_status,
                volume=(
                    _decimal_or_none(market.raw.get("volume_fp"))
                    if market
                    else None
                ),
                open_interest=(
                    _decimal_or_none(market.raw.get("open_interest_fp"))
                    if market
                    else None
                ),
                last_trade=(
                    Decimal(str(market.last_price_dollars))
                    if market and market.last_price_dollars is not None
                    else None
                ),
                sequence_gap=bool(recovery_reason),
                diagnostics=self._diagnostics(state),
            )
        return identifiers

    def process_websocket_message(self, message: dict[str, Any]) -> str:
        received_at = self.now()
        message_type = message.get("type")
        payload = message.get("msg") or {}
        ticker = payload.get("market_ticker")
        gap_detected = self._sequence_gap(message)
        if message_type == "ticker" and ticker:
            yes_bid = payload.get("yes_bid_dollars")
            yes_ask = payload.get("yes_ask_dollars")
            ts_ms = payload.get("ts_ms")
            if yes_bid is not None and yes_ask is not None and ts_ms is not None:
                from price_feed.cache import set_cached_price

                set_cached_price(
                    ticker,
                    round((float(yes_bid) + float(yes_ask)) / 2, 4),
                    int(ts_ms),
                )
        if message_type in {"market_lifecycle_v2", "market_lifecycle"} and ticker:
            lifecycle = payload.get("event_type") or payload.get("status")
            status_map = {
                "activated": "active",
                "deactivated": "inactive",
                "determined": "determined",
                "settled": "finalized",
            }
            if lifecycle in status_map:
                self.market_status[ticker] = status_map[lifecycle]
        if message_type == "orderbook_snapshot":
            state = OrderbookState.from_websocket_snapshot(
                message,
                received_at=received_at,
                use_yes_price=True,
            )
            self.books[state.ticker] = state
            self.repository.append_orderbook(
                state,
                source="WEBSOCKET_SNAPSHOT",
                market_status=self.market_status.get(state.ticker),
                diagnostics=self._diagnostics(state),
            )
        elif message_type == "orderbook_delta" and ticker:
            state = self.books.get(ticker)
            if gap_detected:
                self.capture_rest_books(
                    [ticker], recovery_reason="SEQUENCE_GAP"
                )
            elif state is None:
                self.capture_rest_books(
                    [ticker], recovery_reason="DELTA_WITHOUT_BASELINE"
                )
                gap_detected = True
            else:
                try:
                    state.apply_delta(
                        message,
                        received_at=received_at,
                        use_yes_price=True,
                        enforce_sequence=False,
                    )
                except SequenceGap:
                    gap_detected = True
                    self.capture_rest_books(
                        [ticker], recovery_reason="SEQUENCE_GAP"
                    )
                else:
                    self.repository.append_orderbook(
                        state,
                        source="WEBSOCKET_DELTA",
                        market_status=self.market_status.get(state.ticker),
                        diagnostics=self._diagnostics(state),
                    )
        self.repository.append_stream_message(
            message,
            received_at=received_at,
            gap_detected=gap_detected,
        )
        return "RECOVERED_SEQUENCE_GAP" if gap_detected else "RECORDED"

    def _sequence_gap(self, message: dict[str, Any]) -> bool:
        if message.get("type") not in {
            "orderbook_snapshot",
            "orderbook_delta",
        }:
            return False
        sid = message.get("sid")
        sequence = message.get("seq")
        if sid is None or sequence is None:
            return False
        sid = int(sid)
        sequence = int(sequence)
        previous = self.sequence_by_subscription.get(sid)
        self.sequence_by_subscription[sid] = sequence
        return previous is not None and sequence != previous + 1

    def collect_decisions(self) -> dict[str, int]:
        settled = self.settle_paper_results()
        alerts = self.repository.latest_alerts()
        snapshot_ids = self.capture_rest_books(
            sorted({row["market_ticker"] for row in alerts}),
            recovery_reason="PROCESS_RESTART",
        )
        counters = {
            "decisions": 0,
            "eligible": 0,
            "paper_events": 0,
            "settled": settled,
        }
        for alert in alerts:
            ticker = alert["market_ticker"]
            state = self.books.get(ticker)
            if state is None:
                continue
            book = state.as_orderbook()
            diagnostics = self._diagnostics(state)
            if book.best_yes_bid is None or book.best_yes_ask is None:
                market_probability = None
            else:
                market_probability = (
                    book.best_yes_bid + book.best_yes_ask
                ) / Decimal("2")
            for candidate in self.candidates:
                decision = self._decision(
                    alert,
                    candidate,
                    state,
                    snapshot_ids[ticker],
                    market_probability,
                    diagnostics,
                )
                decision_id = self.repository.append_decision(decision)
                counters["decisions"] += 1
                if decision["rejection_reason"] is None:
                    counters["eligible"] += 1
                fill_event = self._paper_event(
                    decision_id,
                    decision,
                    candidate,
                    book,
                    diagnostics,
                    self.market_status.get(decision["market_ticker"]),
                )
                self.repository.append_paper_event(fill_event)
                counters["paper_events"] += 1
        return counters

    def settle_paper_results(self) -> int:
        settled = 0
        for row in self.repository.unsettled_paper_fills():
            try:
                market = self.client.get_market(row["market_ticker"])
            except Exception:
                continue
            result = (market.raw.get("result") or "").lower()
            if market.status != "finalized" or result not in {"yes", "no"}:
                continue
            held = row["selected_side"].lower()
            won = held == result
            filled = Decimal(str(row["filled_quantity"]))
            price = Decimal(str(row["weighted_fill_price"]))
            fee = Decimal(str(row["estimated_fee"]))
            net_pnl = (
                (filled if won else Decimal("0")) - filled * price - fee
            ).quantize(Decimal("0.0001"))
            self.repository.append_paper_event(
                {
                    "decision_row_id": row["id"],
                    "candidate_version": row["candidate_version"],
                    "event_type": "SETTLED_WIN" if won else "SETTLED_LOSS",
                    "requested_quantity": row["requested_quantity"],
                    "filled_quantity": filled,
                    "weighted_fill_price": price,
                    "estimated_fee": fee,
                    "settlement_result": result.upper(),
                    "net_pnl": net_pnl,
                    "reason": "KALSHI_FINALIZED_MARKET_RESULT",
                    "created_at": self.now(),
                }
            )
            settled += 1
        return settled

    def _decision(
        self,
        alert: dict[str, Any],
        candidate: FrozenCandidate,
        state: OrderbookState,
        snapshot_id: int,
        market_probability: Decimal | None,
        diagnostics: dict[str, bool],
    ) -> dict[str, Any]:
        decision_time = self.now()
        model_probability = Decimal(str(alert["model_probability"]))
        final_probability: Decimal | None = None
        side: str | None = None
        executable_price: Decimal | None = None
        maximum_price: Decimal | None = None
        fee_adjusted_edge: Decimal | None = None
        rejection: str | None = None
        if not Decimal("0") <= model_probability <= Decimal("1"):
            rejection = "INVALID_MODEL_PROBABILITY"
        elif market_probability is None:
            rejection = "MISSING_OPPOSING_LEVELS"
        elif not Decimal("0") <= market_probability <= Decimal("1"):
            rejection = "INVALID_MARKET_PROBABILITY"
        else:
            final_probability = Decimal(
                str(
                    blend_probability(
                        float(model_probability),
                        float(market_probability),
                        model_weight=candidate.model_weight,
                        market_weight=candidate.market_weight,
                    )
                )
            )
            edge = final_probability - market_probability
            side = "YES" if edge >= 0 else "NO"
            side_probability = (
                final_probability
                if side == "YES"
                else Decimal("1") - final_probability
            )
            asks = state.as_orderbook().asks_for(side)
            executable_price = asks[0].price if asks else None
            if executable_price is None:
                rejection = "NO_EXECUTABLE_DEPTH"
            else:
                fee = Decimal(str(taker_fee(float(executable_price))))
                maximum_price = (
                    side_probability
                    - Decimal(str(candidate.signal_threshold))
                    - fee
                )
                fee_adjusted_edge = (
                    side_probability - executable_price - fee
                )
                if any(diagnostics.values()):
                    rejection = next(
                        name.upper()
                        for name, present in diagnostics.items()
                        if present
                    )
                elif executable_price > maximum_price:
                    rejection = "PRICE_ABOVE_MAXIMUM"
                else:
                    visible_quantity = sum(
                        level.quantity
                        for level in asks
                        if level.price <= maximum_price
                    )
                    if visible_quantity < Decimal(
                        str(self.config.intended_quantity)
                    ):
                        rejection = "INSUFFICIENT_VISIBLE_DEPTH"
        stable_key = (
            f"{candidate.strategy_version}|{alert['id']}|{snapshot_id}|"
            f"{decision_time.isoformat()}"
        )
        station = STATIONS.get(alert["series_ticker"])
        return {
            "decision_id": hashlib.sha256(stable_key.encode()).hexdigest(),
            "event_ticker": alert["event_ticker"],
            "market_ticker": alert["market_ticker"],
            "city": alert["city"],
            "station": station.nws_station_id if station else "unknown",
            "target_date": parse_event_date(alert["event_ticker"]),
            "strategy_version": alert["model_version"],
            "candidate_version": candidate.strategy_version,
            "forecast_model": "stored_ensemble",
            "forecast_run_time": alert["forecast_run_time"],
            "forecast_availability_time": alert[
                "forecast_availability_time"
            ],
            "forecast_values": {
                "ensemble_mean": alert["ensemble_mean"],
                "ensemble_std": alert["ensemble_std"],
                "lead_days": alert["lead_days"],
            },
            "observation_event_time": alert["observation_event_time"],
            "observation_publication_time": alert[
                "observation_publication_time"
            ],
            "observation_collector_received_time": alert[
                "observation_collector_received_time"
            ],
            "observation_revision": None,
            "observed_high_at_decision": alert["observed_so_far"],
            "orderbook_snapshot_id": snapshot_id,
            "model_probability": model_probability,
            "market_probability": market_probability,
            "final_candidate_probability": final_probability,
            "selected_side": side,
            "maximum_acceptable_price": maximum_price,
            "fee_adjusted_edge": fee_adjusted_edge,
            "rejection_reason": rejection,
            "intended_quantity": Decimal(str(self.config.intended_quantity)),
            "event_time": alert["close_time"],
            "source_publish_time": state.source_publish_time,
            "collector_received_time": state.collector_received_time,
            "decision_time": decision_time,
        }

    def _paper_event(
        self,
        decision_row_id: int,
        decision: dict[str, Any],
        candidate: FrozenCandidate,
        book: MarketOrderbook,
        diagnostics: dict[str, bool],
        market_status: str | None,
    ) -> dict[str, Any]:
        if decision["rejection_reason"] is not None:
            status = "INELIGIBLE"
            fill = None
            reason = decision["rejection_reason"]
        else:
            fill = conservative_fill(
                book,
                outcome=decision["selected_side"],
                limit_price=decision["maximum_acceptable_price"],
                requested_quantity=decision["intended_quantity"],
                book_is_fresh=not diagnostics["stale"],
                market_is_active=market_status in {"active", "open"},
            )
            status = fill.status
            reason = fill.reason
        return {
            "decision_row_id": decision_row_id,
            "candidate_version": candidate.strategy_version,
            "event_type": status,
            "requested_quantity": decision["intended_quantity"],
            "filled_quantity": (
                fill.filled_quantity if fill else Decimal("0")
            ),
            "weighted_fill_price": (
                fill.weighted_fill_price if fill else None
            ),
            "estimated_fee": fill.estimated_fee if fill else Decimal("0"),
            "settlement_result": None,
            "net_pnl": None,
            "reason": reason,
            "created_at": self.now(),
        }


def _payload_time(payload: dict[str, Any]) -> datetime | None:
    if payload.get("ts_ms") is not None:
        return datetime.fromtimestamp(int(payload["ts_ms"]) / 1000, tz=UTC)
    value = payload.get("time") or payload.get("ts")
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=UTC)
    return None


def _decimal_or_none(value: Any) -> Decimal | None:
    return Decimal(str(value)) if value is not None else None


async def run_stream_forever(
    collector: ForwardEvidenceCollector,
    *,
    reconnect_delay_seconds: int = 5,
) -> None:
    """Persistent narrow stream; REST re-baselines every connection/reconnect."""
    import asyncio

    import websockets

    while True:
        try:
            tickers = collector.repository.watched_tickers()
            if not tickers:
                await asyncio.sleep(reconnect_delay_seconds)
                continue
            collector.sequence_by_subscription.clear()
            collector.capture_rest_books(
                tickers, recovery_reason="INITIAL_OR_RECONNECT"
            )
            headers = collector.client.websocket_auth_headers()
            url = (
                WS_PRODUCTION_URL
                if collector.client.is_production
                else WS_DEMO_URL
            )
            async with websockets.connect(
                url, additional_headers=headers
            ) as websocket:
                subscriptions = []
                message_id = 1
                for start in range(0, len(tickers), 100):
                    batch = tickers[start : start + 100]
                    subscriptions.extend(
                        [
                            {
                                "id": message_id,
                                "cmd": "subscribe",
                                "params": {
                                    "channels": ["orderbook_delta"],
                                    "market_tickers": batch,
                                    "use_yes_price": True,
                                },
                            },
                            {
                                "id": message_id + 1,
                                "cmd": "subscribe",
                                "params": {
                                    "channels": ["ticker", "trade"],
                                    "market_tickers": batch,
                                },
                            },
                            {
                                "id": message_id + 2,
                                "cmd": "subscribe",
                                "params": {
                                    "channels": ["market_lifecycle_v2"],
                                    "market_tickers": batch,
                                },
                            },
                        ]
                    )
                    message_id += 3
                subscriptions.append(
                    {
                        "id": message_id,
                        "cmd": "subscribe",
                        "params": {"channels": ["fill"]},
                    }
                )
                for subscription in subscriptions:
                    await websocket.send(json.dumps(subscription))
                async for raw in websocket:
                    try:
                        collector.process_websocket_message(json.loads(raw))
                        collector.repository.connection.commit()
                    except json.JSONDecodeError:
                        continue
        except Exception as exc:
            print(
                f"[readiness-collector] reconnect after "
                f"{exc.__class__.__name__}: {exc}"
            )
            await asyncio.sleep(reconnect_delay_seconds)
