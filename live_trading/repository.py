from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from kalshi_client import Fill, Order

from .domain import LiveOrder, LiveRiskState, OrderIntent, ReconciliationResult

_ORDER_COLUMNS = (
    "id, local_order_id, client_order_id, kalshi_order_id, market_ticker, "
    "event_ticker, event_date, intended_outcome, api_book_side, submitted_yes_price, "
    "model_probability, maximum_acceptable_price, requested_count, filled_count, "
    "remaining_count, average_fill_price, estimated_fees, actual_fees, status, "
    "decision_at, quote_at, weather_data_at, expires_at, reconciliation_status"
)


def _order_from_row(row: tuple[Any, ...]) -> LiveOrder:
    return LiveOrder(*row)


def _order_exposure(
    *,
    status: str,
    intended_outcome: str,
    submitted_yes_price: Decimal,
    requested_count: Decimal,
    filled_count: Decimal,
    remaining_count: Decimal,
    estimated_fees: Decimal,
    actual_fees: Decimal,
) -> Decimal:
    """Worst-case dollars committed by one unsettled bot order/position."""
    yes_price = Decimal(str(submitted_yes_price))
    outcome_price = yes_price if intended_outcome == "YES" else Decimal("1") - yes_price
    requested = Decimal(str(requested_count))
    filled = Decimal(str(filled_count))
    remaining = Decimal(str(remaining_count))
    estimated = Decimal(str(estimated_fees))
    actual = Decimal(str(actual_fees))
    if status in {"SUBMITTING", "UNKNOWN"}:
        at_risk_count = max(requested, filled + remaining)
        unfilled_count = max(at_risk_count - filled, Decimal("0"))
    elif status in {"RESTING", "PARTIAL"}:
        at_risk_count = filled + remaining
        unfilled_count = remaining
    elif status in {"FILLED", "CANCELED"}:
        at_risk_count = filled
        unfilled_count = Decimal("0")
    else:
        return Decimal("0")
    estimated_unfilled_fee = (
        estimated * unfilled_count / requested if requested > 0 else Decimal("0")
    )
    return (
        outcome_price * at_risk_count + actual + estimated_unfilled_fee
    ).quantize(Decimal("0.0001"))


class PostgresLiveRepository:
    """Narrow persistence adapter for bot-owned production orders."""

    _LOCK_KEY = 729_230_723

    def __init__(self, connection: Any):
        self.connection = connection

    def acquire_cycle_lock(self) -> bool:
        with self.connection.cursor() as cursor:
            cursor.execute("select pg_try_advisory_lock(%s)", (self._LOCK_KEY,))
            return bool(cursor.fetchone()[0])

    def release_cycle_lock(self) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute("select pg_advisory_unlock(%s)", (self._LOCK_KEY,))

    def list_signal_rows(self) -> list[dict[str, Any]]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "with latest as ("
                " select distinct on (market_ticker) id, created_at, series_ticker, event_ticker, "
                " market_ticker, model_probability, market_yes_price, edge, fee_adjusted_threshold, "
                " close_time, is_actionable, lead_days "
                " from alerts where settled_at is null "
                " order by market_ticker, created_at desc"
                ") select latest.*, ("
                " select max(fp.created_at) from forecast_pulls fp "
                " where fp.series_ticker = latest.series_ticker"
                ") as weather_data_at from latest where close_time > now()"
            )
            columns = [item.name for item in cursor.description]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]

    def readiness_timestamps(self) -> dict[str, datetime | None]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select max(created_at) from alerts where settled_at is null and close_time > now()"
            )
            market_data_at = cursor.fetchone()[0]
            cursor.execute("select max(created_at) from forecast_pulls")
            weather_data_at = cursor.fetchone()[0]
        return {"market_data_at": market_data_at, "weather_data_at": weather_data_at}

    def worker_healthy(self) -> bool:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select status, finished_at from pipeline_runs "
                "where script = 'run_paper_trading' order by started_at desc limit 1"
            )
            row = cursor.fetchone()
        if not row or row[0] != "success" or row[1] is None:
            return False
        return datetime.now(UTC) - row[1] <= timedelta(minutes=45)

    def create_intent(self, intent: OrderIntent) -> tuple[LiveOrder, bool]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "insert into live_orders (local_order_id, signal_id, decision_id, strategy_name, "
                "strategy_version, market_ticker, event_ticker, event_date, client_order_id, "
                "intended_outcome, api_book_side, submitted_yes_price, model_probability, "
                "maximum_acceptable_price, requested_count, remaining_count, estimated_fees, "
                "decision_at, quote_at, weather_data_at, expires_at) values ("
                "%(local_order_id)s, %(signal_id)s, %(decision_id)s, %(strategy_name)s, "
                "%(strategy_version)s, %(market_ticker)s, %(event_ticker)s, %(event_date)s, "
                "%(client_order_id)s, %(intended_outcome)s, %(api_book_side)s, "
                "%(submitted_yes_price)s, %(model_probability)s, %(maximum_acceptable_price)s, "
                "%(requested_count)s, %(requested_count)s, %(estimated_fees)s, %(decision_at)s, "
                "%(quote_at)s, %(weather_data_at)s, %(expires_at)s"
                ") on conflict (client_order_id) do nothing returning " + _ORDER_COLUMNS,
                {
                    "local_order_id": intent.local_order_id,
                    "signal_id": intent.signal.signal_id,
                    "decision_id": intent.signal.decision_id,
                    "strategy_name": intent.signal.strategy_name,
                    "strategy_version": intent.signal.strategy_version,
                    "market_ticker": intent.signal.market_ticker,
                    "event_ticker": intent.signal.event_ticker,
                    "event_date": intent.signal.event_date,
                    "client_order_id": intent.client_order_id,
                    "intended_outcome": intent.signal.intended_outcome,
                    "api_book_side": intent.api_book_side,
                    "submitted_yes_price": intent.submitted_yes_price,
                    "model_probability": intent.signal.model_probability,
                    "maximum_acceptable_price": intent.signal.maximum_acceptable_price,
                    "requested_count": intent.requested_count,
                    "estimated_fees": intent.estimated_fees,
                    "decision_at": intent.signal.decision_at,
                    "quote_at": intent.signal.quote_at,
                    "weather_data_at": intent.signal.weather_data_at,
                    "expires_at": intent.expires_at,
                },
            )
            row = cursor.fetchone()
            created = row is not None
            if row is None:
                cursor.execute(
                    f"select {_ORDER_COLUMNS} from live_orders where client_order_id = %s",
                    (intent.client_order_id,),
                )
                row = cursor.fetchone()
            order = _order_from_row(row)
            if created:
                cursor.execute(
                    "insert into live_order_events "
                    "(live_order_id, from_status, to_status, event_type, actor, detail) "
                    "values (%s, null, 'PENDING', 'intent_persisted', 'worker', %s)",
                    (order.id, f"decision_id={intent.signal.decision_id}"),
                )
        self.connection.commit()
        return order, created

    def get_by_client_order_id(self, client_order_id: str) -> LiveOrder | None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                f"select {_ORDER_COLUMNS} from live_orders where client_order_id = %s",
                (client_order_id,),
            )
            row = cursor.fetchone()
        return _order_from_row(row) if row else None

    def list_reconcilable_orders(self) -> list[LiveOrder]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                f"select {_ORDER_COLUMNS} from live_orders "
                "where status not in ('REJECTED', 'SETTLED') order by created_at"
            )
            return [_order_from_row(row) for row in cursor.fetchall()]

    def list_active_orders(self) -> list[LiveOrder]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                f"select {_ORDER_COLUMNS} from live_orders "
                "where status in ('PENDING', 'SUBMITTING', 'UNKNOWN', 'RESTING', 'PARTIAL') "
                "order by created_at"
            )
            return [_order_from_row(row) for row in cursor.fetchall()]

    def transition(
        self,
        order: LiveOrder,
        status: str,
        event_type: str,
        *,
        actor: str,
        detail: str | None = None,
        **fields: Any,
    ) -> LiveOrder:
        allowed_fields = {
            "kalshi_order_id",
            "filled_count",
            "remaining_count",
            "average_fill_price",
            "actual_fees",
            "submitted_at",
            "acknowledged_at",
            "filled_at",
            "canceled_at",
            "settled_at",
            "settlement_result",
            "realized_pnl",
            "mark_to_market_pnl",
            "error_code",
            "error_detail",
            "reconciliation_status",
            "last_reconciled_at",
        }
        unknown = set(fields) - allowed_fields
        if unknown:
            raise ValueError(f"Unsupported live-order update fields: {sorted(unknown)}")
        assignments = ["status = %(status)s"]
        params: dict[str, Any] = {"status": status, "id": order.id}
        for field_name, value in fields.items():
            assignments.append(f"{field_name} = %({field_name})s")
            params[field_name] = value
        with self.connection.cursor() as cursor:
            cursor.execute(
                f"update live_orders set {', '.join(assignments)} where id = %(id)s "
                f"returning {_ORDER_COLUMNS}",
                params,
            )
            updated = _order_from_row(cursor.fetchone())
            cursor.execute(
                "insert into live_order_events "
                "(live_order_id, from_status, to_status, event_type, actor, detail) "
                "values (%s, %s, %s, %s, %s, %s)",
                (order.id, order.status, status, event_type, actor, detail),
            )
        self.connection.commit()
        return updated

    def apply_remote_order(self, local: LiveOrder, remote: Order) -> LiveOrder:
        if remote.status == "resting":
            status = "PARTIAL" if remote.fill_count > 0 else "RESTING"
        elif remote.status == "executed":
            status = "FILLED"
        elif remote.status == "canceled":
            status = "CANCELED"
        else:
            status = local.status
        now = datetime.now(UTC)
        return self.transition(
            local,
            status,
            "remote_order_reconciled",
            actor="reconciler",
            kalshi_order_id=remote.order_id,
            filled_count=remote.fill_count,
            remaining_count=remote.remaining_count,
            average_fill_price=(
                remote.yes_price if remote.fill_count > 0 else local.average_fill_price
            ),
            actual_fees=remote.fees_paid,
            acknowledged_at=now,
            filled_at=now if status == "FILLED" else None,
            canceled_at=now if status == "CANCELED" else None,
            reconciliation_status="MATCHED",
            last_reconciled_at=now,
        )

    def record_fill(self, local: LiveOrder, fill: Fill) -> bool:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "insert into live_order_fills "
                "(live_order_id, kalshi_fill_id, kalshi_order_id, count, yes_price, fee, filled_at) "
                "values (%s, %s, %s, %s, %s, %s, %s) "
                "on conflict (kalshi_fill_id) do nothing returning id",
                (
                    local.id,
                    fill.fill_id,
                    fill.order_id,
                    fill.count,
                    fill.yes_price,
                    fill.fee,
                    fill.created_time,
                ),
            )
            created = cursor.fetchone() is not None
        self.connection.commit()
        return created

    def refresh_fill_totals(self, local: LiveOrder) -> LiveOrder:
        """Rebuild count, VWAP, and fees from idempotently persisted fills."""
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select coalesce(sum(count), 0), "
                "case when coalesce(sum(count), 0) > 0 "
                "then sum(count * yes_price) / sum(count) else null end, "
                "coalesce(sum(fee), 0), max(filled_at) "
                "from live_order_fills where live_order_id = %s",
                (local.id,),
            )
            total, average, fees, latest_fill = cursor.fetchone()
        total = Decimal(str(total))
        if total <= 0:
            return local
        average_price = Decimal(str(average)).quantize(Decimal("0.0001"))
        actual_fees = Decimal(str(fees)).quantize(Decimal("0.0001"))
        status = local.status
        if total >= local.requested_count:
            status = "FILLED"
        elif status in {"RESTING", "PARTIAL"}:
            status = "PARTIAL"
        remaining = (
            Decimal("0")
            if status in {"FILLED", "CANCELED"}
            else max(local.requested_count - total, Decimal("0"))
        )
        if (
            total == local.filled_count
            and remaining == local.remaining_count
            and average_price == local.average_fill_price
            and actual_fees == local.actual_fees
            and status == local.status
        ):
            return local
        fields: dict[str, Any] = {
            "filled_count": total,
            "remaining_count": remaining,
            "average_fill_price": average_price,
            "actual_fees": actual_fees,
            "last_reconciled_at": datetime.now(UTC),
        }
        if status == "FILLED":
            fields["filled_at"] = latest_fill or datetime.now(UTC)
        return self.transition(
            local,
            status,
            "fills_reconciled",
            actor="reconciler",
            **fields,
        )

    def settle_order(
        self,
        local: LiveOrder,
        *,
        result: str,
        settled_at: datetime | None,
    ) -> LiveOrder:
        won = result.upper() == local.intended_outcome
        payout = local.filled_count if won else Decimal("0")
        outcome_fill_price = (
            local.average_fill_price
            if local.intended_outcome == "YES"
            else Decimal("1") - (local.average_fill_price or local.submitted_yes_price)
        )
        cost = local.filled_count * (outcome_fill_price or Decimal("0"))
        realized = (payout - cost - local.actual_fees).quantize(Decimal("0.0001"))
        return self.transition(
            local,
            "SETTLED",
            "settlement_reconciled",
            actor="reconciler",
            settlement_result=result.upper(),
            settled_at=settled_at or datetime.now(UTC),
            realized_pnl=realized,
            mark_to_market_pnl=Decimal("0"),
            reconciliation_status="MATCHED",
            last_reconciled_at=datetime.now(UTC),
        )

    def update_mark_to_market(self, local: LiveOrder, current_outcome_bid: Decimal) -> None:
        if local.filled_count <= 0:
            return
        fill_price = (
            local.average_fill_price
            if local.intended_outcome == "YES"
            else Decimal("1") - (local.average_fill_price or local.submitted_yes_price)
        )
        pnl = (
            (current_outcome_bid - (fill_price or Decimal("0"))) * local.filled_count
            - local.actual_fees
        ).quantize(Decimal("0.0001"))
        self.transition(
            local,
            local.status,
            "mark_to_market",
            actor="reconciler",
            mark_to_market_pnl=pnl,
        )

    def risk_state(self, market_ticker: str, event_ticker: str, event_date: date) -> LiveRiskState:
        active_statuses = ("SUBMITTING", "UNKNOWN", "RESTING", "PARTIAL", "FILLED", "CANCELED")
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select market_ticker, event_ticker, event_date, status, intended_outcome, "
                "submitted_yes_price, requested_count, filled_count, remaining_count, "
                "estimated_fees, actual_fees "
                "from live_orders where status = any(%s)",
                (list(active_statuses),),
            )
            rows = cursor.fetchall()
            cursor.execute(
                "select coalesce(sum(realized_pnl), 0) from live_orders "
                "where settled_at >= date_trunc('day', now())"
            )
            daily_realized = Decimal(str(cursor.fetchone()[0]))
            cursor.execute(
                "select coalesce(sum(mark_to_market_pnl), 0) from live_orders "
                "where status in ('PARTIAL', 'FILLED', 'CANCELED') and settled_at is null"
            )
            daily_mtm = Decimal(str(cursor.fetchone()[0]))
            cursor.execute(
                "select realized_pnl from live_orders where status = 'SETTLED' "
                "order by settled_at desc limit 100"
            )
            consecutive_losses = 0
            for (pnl,) in cursor.fetchall():
                if pnl is not None and Decimal(str(pnl)) < 0:
                    consecutive_losses += 1
                else:
                    break
            cursor.execute("select exists(select 1 from live_orders where status = 'UNKNOWN')")
            has_unknown = bool(cursor.fetchone()[0])
            cursor.execute(
                "select healthy from live_reconciliation_runs order by started_at desc limit 1"
            )
            reconciliation_row = cursor.fetchone()
        market_exposure = Decimal("0")
        event_exposure = Decimal("0")
        total_exposure = Decimal("0")
        for row in rows:
            (
                ticker,
                event,
                target_date,
                status,
                outcome,
                yes_price,
                requested,
                filled,
                remaining,
                estimated_fees,
                actual_fees,
            ) = row
            cost = _order_exposure(
                status=status,
                intended_outcome=outcome,
                submitted_yes_price=Decimal(str(yes_price)),
                requested_count=Decimal(str(requested)),
                filled_count=Decimal(str(filled)),
                remaining_count=Decimal(str(remaining)),
                estimated_fees=Decimal(str(estimated_fees)),
                actual_fees=Decimal(str(actual_fees)),
            )
            total_exposure += cost
            if ticker == market_ticker:
                market_exposure += cost
            if event == event_ticker and target_date == event_date:
                event_exposure += cost
        return LiveRiskState(
            market_exposure=market_exposure,
            event_exposure=event_exposure,
            total_exposure=total_exposure,
            daily_realized_pnl=daily_realized,
            daily_mark_to_market_pnl=daily_mtm,
            consecutive_settled_losses=consecutive_losses,
            has_unknown_order=has_unknown,
            reconciliation_healthy=bool(reconciliation_row and reconciliation_row[0]),
        )

    def record_reconciliation(
        self,
        result: ReconciliationResult,
        *,
        local_order_count: int,
        remote_bot_order_count: int,
        actor: str,
    ) -> None:
        now = datetime.now(UTC)
        with self.connection.cursor() as cursor:
            cursor.execute(
                "insert into live_reconciliation_runs "
                "(started_at, finished_at, healthy, available_cash, local_order_count, "
                "remote_bot_order_count, fill_count, position_count, settlement_count, "
                "mismatch_count, detail, actor) values "
                "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    now,
                    now,
                    result.healthy,
                    result.available_cash,
                    local_order_count,
                    remote_bot_order_count,
                    result.fills,
                    result.positions,
                    result.settlements,
                    len(result.mismatches),
                    json.dumps(result.mismatches),
                    actor,
                ),
            )
        self.connection.commit()

    def latest_reconciliation(self) -> dict[str, Any] | None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select finished_at, healthy, available_cash, mismatch_count, detail "
                "from live_reconciliation_runs order by started_at desc limit 1"
            )
            row = cursor.fetchone()
        if not row:
            return None
        return dict(
            finished_at=row[0],
            healthy=bool(row[1]),
            available_cash=Decimal(str(row[2])) if row[2] is not None else None,
            mismatch_count=row[3],
            detail=row[4],
        )

    def start_cycle(self) -> int:
        with self.connection.cursor() as cursor:
            cursor.execute("insert into live_execution_cycles default values returning id")
            cycle_id = cursor.fetchone()[0]
        self.connection.commit()
        return cycle_id

    def finish_cycle(self, cycle_id: int, result: Any) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "update live_execution_cycles set finished_at = now(), status = %s, "
                "submitted_orders = %s, reconciled_orders = %s, canceled_orders = %s, "
                "blocker = %s, error_detail = %s, summary = %s where id = %s",
                (
                    result.status,
                    result.submitted_orders,
                    result.reconciled_orders,
                    result.canceled_orders,
                    result.blocker,
                    result.error,
                    (
                        f"submitted={result.submitted_orders}, "
                        f"reconciled={result.reconciled_orders}, "
                        f"canceled={result.canceled_orders}"
                    ),
                    cycle_id,
                ),
            )
        self.connection.commit()

    def status_summary(self) -> dict[str, Any]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select status, intended_outcome, submitted_yes_price, requested_count, "
                "filled_count, remaining_count, estimated_fees, actual_fees "
                "from live_orders where settled_at is null and status in "
                "('SUBMITTING', 'UNKNOWN', 'RESTING', 'PARTIAL', 'FILLED', 'CANCELED')"
            )
            exposure_rows = cursor.fetchall()
            cursor.execute(
                "select coalesce(sum(realized_pnl), 0) from live_orders "
                "where settled_at >= date_trunc('day', now())"
            )
            daily_pnl = cursor.fetchone()[0]
            cursor.execute(
                "select finished_at, status, error_detail from live_execution_cycles "
                "order by started_at desc limit 1"
            )
            cycle = cursor.fetchone()
        active_count = sum(
            1 for row in exposure_rows if row[0] in {"RESTING", "PARTIAL"}
        )
        exposure = sum(
            (
                _order_exposure(
                    status=row[0],
                    intended_outcome=row[1],
                    submitted_yes_price=Decimal(str(row[2])),
                    requested_count=Decimal(str(row[3])),
                    filled_count=Decimal(str(row[4])),
                    remaining_count=Decimal(str(row[5])),
                    estimated_fees=Decimal(str(row[6])),
                    actual_fees=Decimal(str(row[7])),
                )
                for row in exposure_rows
            ),
            Decimal("0"),
        )
        return {
            "active_bot_orders": active_count,
            "bot_open_exposure": Decimal(str(exposure)),
            "daily_bot_realized_pnl": Decimal(str(daily_pnl)),
            "last_cycle_at": cycle[0] if cycle else None,
            "last_cycle_status": cycle[1] if cycle else None,
            "last_execution_error": cycle[2] if cycle else None,
        }

    def recent_orders(self, limit: int = 25) -> list[dict[str, Any]]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select local_order_id, client_order_id, kalshi_order_id, market_ticker, "
                "intended_outcome, submitted_yes_price, requested_count, filled_count, "
                "remaining_count, status, created_at, error_code, reconciliation_status "
                "from live_orders order by created_at desc limit %s",
                (limit,),
            )
            columns = [item.name for item in cursor.description]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]
