from __future__ import annotations

import csv
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .professional_freeze import (
    frozen_professional_strategy,
    persist_professional_freeze,
    professional_code_hash,
)


def _table_exists(connection: Any, name: str) -> bool:
    with connection.cursor() as cursor:
        cursor.execute("select to_regclass(%s)", (name,))
        return cursor.fetchone()[0] is not None


def _count_rows(connection: Any, query: str) -> list[dict[str, Any]]:
    with connection.cursor() as cursor:
        cursor.execute(query)
        columns = [item.name for item in cursor.description]
        return [
            dict(zip(columns, row, strict=True)) for row in cursor.fetchall()
        ]


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("status,count\nNOT_AVAILABLE,0\n")
        return
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=list(rows[0]), lineterminator="\n"
        )
        writer.writeheader()
        writer.writerows(rows)


def run_professional_report(
    connection: Any,
    output_dir: Path,
    *,
    root: Path,
    now: datetime | None = None,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = (now or datetime.now(UTC)).astimezone(UTC)
    freeze = frozen_professional_strategy(
        frozen_at=timestamp,
        code_hash=professional_code_hash(root),
    )
    manifest = persist_professional_freeze(
        output_dir / "strategy_freeze_manifest.json", freeze
    )
    schema_deployed = all(
        _table_exists(connection, table)
        for table in (
            "professional_strategy_freezes",
            "professional_contract_truth",
            "professional_information_events",
            "professional_decision_snapshots",
            "professional_journal_events",
            "professional_post_trade_reviews",
            "professional_information_reactions",
            "professional_action_alerts",
        )
    )
    if schema_deployed:
        actions = _count_rows(
            connection,
            "select action, count(*)::integer as count "
            "from professional_decision_snapshots group by action "
            "order by action",
        )
        information = _count_rows(
            connection,
            "select event_type, material, count(*)::integer as count "
            "from professional_information_events "
            "group by event_type, material order by event_type, material",
        )
        journal = _count_rows(
            connection,
            "select record_type, count(*)::integer as count "
            "from professional_journal_events group by record_type "
            "order by record_type",
        )
        reviews = _count_rows(
            connection,
            "select classification, count(*)::integer as count "
            "from professional_post_trade_reviews group by classification "
            "order by classification",
        )
        decisions_by_thesis = _count_rows(
            connection,
            "select thesis_type, action, count(*)::integer as count "
            "from professional_decision_snapshots "
            "group by thesis_type, action order by thesis_type, action",
        )
        decisions_by_information = _count_rows(
            connection,
            "select coalesce(i.event_type, 'INITIAL_CANDIDATE_SIGNAL') "
            "as information_event_type, p.action, "
            "count(*)::integer as count "
            "from professional_decision_snapshots p "
            "left join professional_information_events i "
            "on i.information_event_id = "
            "p.triggering_information_event_id "
            "group by information_event_type, p.action "
            "order by information_event_type, p.action",
        )
        review_cohorts = _count_rows(
            connection,
            "select p.contract_truth->>'city' as city, "
            "p.contract_truth->>'station_identifier' as station, "
            "greatest(0, "
            "(p.contract_truth->>'target_date')::date "
            "- p.decision_time::date) as forecast_horizon_days, "
            "coalesce(i.event_type, 'INITIAL_CANDIDATE_SIGNAL') "
            "as information_event_type, p.thesis_type, p.action, "
            "p.strategy_version, r.classification, "
            "count(*)::integer as count "
            "from professional_post_trade_reviews r "
            "join professional_decision_snapshots p "
            "on p.decision_id = r.decision_id "
            "left join professional_information_events i "
            "on i.information_event_id = "
            "p.triggering_information_event_id "
            "group by city, station, forecast_horizon_days, "
            "information_event_type, p.thesis_type, p.action, "
            "p.strategy_version, r.classification "
            "order by city, station, forecast_horizon_days",
        )
        execution = _count_rows(
            connection,
            "with settled as ("
            " select net_pnl, estimated_fee, created_at, id, "
            " sum(net_pnl) over (order by created_at, id) as running_pnl "
            " from forward_paper_order_events "
            " where event_type in ('SETTLED_WIN','SETTLED_LOSS')"
            "), drawdowns as ("
            " select running_pnl - greatest(0, max(running_pnl) over "
            "(order by created_at, id)) as drawdown from settled"
            ") select "
            "(select count(*)::integer from professional_journal_events "
            "where record_type = 'INTENDED_ORDER') as intended_orders, "
            "count(*) filter (where event_type in ('FILLED','PARTIAL_FILL'))"
            "::integer as observable_fills, "
            "count(*) filter (where event_type in ('FILLED','PARTIAL_FILL'))"
            "::integer as actual_simulated_fills, "
            "count(*) filter (where event_type = 'NO_FILL')::integer as no_fills, "
            "count(*) filter (where event_type in "
            "('SETTLED_WIN','SETTLED_LOSS','VOID'))::integer as settlements, "
            "count(*) filter (where event_type = 'SETTLED_WIN')::integer as wins, "
            "count(*) filter (where event_type = 'SETTLED_LOSS')::integer as losses, "
            "coalesce(sum(net_pnl + estimated_fee) filter (where event_type in "
            "('SETTLED_WIN','SETTLED_LOSS')), 0) as gross_pnl, "
            "coalesce(sum(net_pnl) filter (where event_type in "
            "('SETTLED_WIN','SETTLED_LOSS')), 0) as net_pnl, "
            "coalesce(sum(estimated_fee) filter (where event_type in "
            "('SETTLED_WIN','SETTLED_LOSS')), 0) as fees, "
            "avg(net_pnl) filter (where event_type in "
            "('SETTLED_WIN','SETTLED_LOSS')) as expectancy, "
            "case when abs(coalesce(sum(net_pnl) filter "
            "(where event_type = 'SETTLED_LOSS'), 0)) = 0 then null "
            "else coalesce(sum(net_pnl) filter "
            "(where event_type = 'SETTLED_WIN'), 0) "
            "/ abs(sum(net_pnl) filter "
            "(where event_type = 'SETTLED_LOSS')) end as profit_factor, "
            "(select min(drawdown) from drawdowns) as maximum_drawdown "
            "from forward_paper_order_events",
        )[0]
        reaction_samples = _count_rows(
            connection,
            "select count(*)::integer as count "
            "from professional_information_reactions",
        )[0]["count"]
        contract_unclear = _count_rows(
            connection,
            "select count(*)::integer as count "
            "from professional_contract_truth "
            "where status = 'CONTRACT_TRUTH_UNCLEAR'",
        )[0]["count"]
    else:
        actions = []
        information = []
        journal = []
        reviews = []
        decisions_by_thesis = []
        decisions_by_information = []
        review_cohorts = []
        execution = {
            "intended_orders": 0,
            "observable_fills": 0,
            "actual_simulated_fills": 0,
            "no_fills": 0,
            "settlements": 0,
            "wins": 0,
            "losses": 0,
            "gross_pnl": None,
            "net_pnl": None,
            "fees": None,
            "expectancy": None,
            "profit_factor": None,
            "maximum_drawdown": None,
        }
        reaction_samples = 0
        contract_unclear = 0
    action_counts = {row["action"]: row["count"] for row in actions}
    confirmed_missing_data = []
    if not schema_deployed:
        confirmed_missing_data.append(
            "The deployed database does not contain the professional journal schema."
        )
    if not information:
        confirmed_missing_data.append(
            "No prospective material information events have been collected."
        )
    if not action_counts:
        confirmed_missing_data.append(
            "No point-in-time professional decision snapshots have been collected."
        )
    if reaction_samples == 0:
        confirmed_missing_data.append(
            "No exact information-to-orderbook reaction samples are available."
        )
    if not reviews:
        confirmed_missing_data.append(
            "No settled post-trade process-quality reviews are available."
        )
    if not any(
        action_counts.get(action, 0)
        for action in ("HOLD", "EXIT", "REBUY_YES", "REBUY_NO")
    ):
        confirmed_missing_data.append(
            "No prospective HOLD, EXIT, or re-entry evidence is available."
        )
    status = {
        "status": (
            "COLLECTING_PROSPECTIVE_PAPER_EVIDENCE"
            if schema_deployed
            else "NOT_STARTED_SCHEMA_NOT_DEPLOYED"
        ),
        "schema_deployed": schema_deployed,
        "prospective_paper_only": True,
        "configured_live_strategy_changed": False,
        "automatic_promotion_allowed": False,
        "production_order_submitted": False,
        "decision_policy_version": freeze.decision_policy_version,
        "base_strategy_version": freeze.base_strategy_version,
        "information_events": sum(row["count"] for row in information),
        "decision_snapshots": sum(action_counts.values()),
        "watch_decisions": action_counts.get("WATCH", 0),
        "do_not_trade_decisions": action_counts.get("DO_NOT_TRADE", 0),
        "buy_yes_decisions": action_counts.get("BUY_YES", 0),
        "buy_no_decisions": action_counts.get("BUY_NO", 0),
        "hold_decisions": action_counts.get("HOLD", 0),
        "exit_decisions": action_counts.get("EXIT", 0),
        "rebuy_decisions": action_counts.get("REBUY_YES", 0)
        + action_counts.get("REBUY_NO", 0),
        "contract_truth_unclear_records": contract_unclear,
        "reaction_samples": reaction_samples,
        "execution": execution,
        "confirmed_missing_data": confirmed_missing_data,
        "next_required_action": (
            "Continue prospective collection without tuning."
            if schema_deployed
            else (
                "Apply db/schema.sql, keep the frozen policy unchanged, and "
                "enable the existing forward evidence collector."
            )
        ),
    }
    (output_dir / "professional_trader_status.json").write_text(
        json.dumps(status, indent=2, sort_keys=True, default=str) + "\n"
    )
    (output_dir / "journal_integrity.json").write_text(
        json.dumps(
            {
                "append_only_database_triggers_defined": True,
                "schema_deployed": schema_deployed,
                "journal_record_counts": journal,
                "post_trade_review_counts": reviews,
                "manual_and_bot_activity_separated": True,
                "production_orders_allowed_by_forward_cohort": False,
            },
            indent=2,
            sort_keys=True,
            default=str,
        )
        + "\n"
    )
    _write_csv(output_dir / "decision_counts.csv", actions)
    _write_csv(output_dir / "information_event_counts.csv", information)
    _write_csv(
        output_dir / "decisions_by_thesis.csv", decisions_by_thesis
    )
    _write_csv(
        output_dir / "decisions_by_information_event.csv",
        decisions_by_information,
    )
    _write_csv(
        output_dir / "post_trade_reviews_by_cohort.csv", review_cohorts
    )
    report = f"""# Professional Climate Trader Decision System

Overall status: **{status['status']}**

## Frozen scope

- Base strategy: `{freeze.base_strategy_name}` / `{freeze.base_strategy_version}`.
- Decision policy: `{freeze.decision_policy_version}`.
- Configured live strategy changed: **False**.
- Automatic promotion: **prohibited**.
- Execution scope: **prospective paper only**.

## Decision pipeline

One coherent `SEE → HEAR → THINK → ACT → REVIEW` chain now records contract
truth, point-in-time weather and executable orderbooks, material information,
model/market/final probabilities, thesis and counterargument, action,
pre-trade checklist, simulated execution, settlement, and process-quality
review.

Supported actions are `DO_NOT_TRADE`, `WATCH`, `BUY_YES`, `BUY_NO`, `HOLD`,
`EXIT`, `REBUY_YES`, and `REBUY_NO`. Re-entry requires a new information event;
a price decline alone is rejected.

## Existing components reused

- The configured climate and observation-conditioned probability pipeline.
- Existing strategy/model version fields and frozen forward candidates.
- `KalshiClient`, centralized opposing-bid-to-ask conversion, authenticated
  orderbook collection, portfolio reads, and exchange-status reads.
- Existing one-contract limit-order path, reconciliation, bot controls,
  capital eligibility, fixed risk limits, scheduler, and paper execution.
- Existing Alerts/market detail, Portfolio, and Backtest surfaces.

No second forecasting engine, scheduler, Kalshi client, portfolio framework,
or production order path was added.

## Implemented behavior

- Contract truth stores settlement station/source/timezone, observation and
  rounding rules, bracket bounds/tails, lifecycle times/status, and revision
  risk. Missing critical fields force `CONTRACT_TRUTH_UNCLEAR`.
- Information events distinguish forecast/observation changes, new daily
  extremes, bracket impossibility, disagreement, executable price,
  liquidity/spread, lifecycle, order, and settlement changes with source,
  publication, receipt, and processing times.
- Immutable decision snapshots retain information-as-of, model/market/working
  probabilities, executable prices/depth, fee/slippage-adjusted economics,
  written thesis, counterargument, invalidation, checklist, blockers, and
  next review.
- Entries require an information thesis, executable depth, positive
  fee-adjusted edge above the frozen margin, a limit price, current account
  reconciliation, available capital, and the existing fixed risk checks.
- HOLD requires an intact thesis and positive remaining edge at the executable
  exit price. EXIT is a full-position action driven by thesis, edge, data,
  liquidity, or risk changes.
- Re-entry creates a new decision and requires a new material weather
  information event after entry/exit. The same event, a lower price alone, or
  an active risk condition cannot create another entry.
- Only material trade/exit/risk/data/reconciliation/settlement-review actions
  create concise action-alert records.

## Persistence and execution safety

`db/schema.sql` defines append-only freezes, contract truth, information
events, 10s/30s/1m/5m/15m reaction samples, decision snapshots, journal
events, post-trade reviews, and action alerts. The journal connects
information → decision → intended order → simulated actual order → fill →
position review/exit/re-entry → settlement → review.

The existing live signal query now requires a persisted matching professional
decision, matching configured strategy version and side, explicit
`production_order_allowed=true`, and every required checklist answer before
the existing order path can receive a signal. The frozen cohort always records
`production_order_allowed=false`.

## User interface

The existing alert-details view contains one compact Professional trader view
with the requested headline fields and five expandable explanations. The
existing Portfolio live-automation panel adds only current bot action,
open-position thesis, last decision, and next review.

## Primary implementation files

- `trading_readiness/professional.py`
- `trading_readiness/professional_collector.py`
- `trading_readiness/professional_freeze.py`
- `trading_readiness/professional_report.py`
- `scripts/run_professional_trader_report.py`
- `scripts/run_forward_evidence_collector.py`
- `live_trading/repository.py`
- `live_trading/service.py`
- `db/schema.sql`
- `dashboard/app.py`
- `dashboard/templates/_alert_card.html`
- `dashboard/templates/_portfolio_panel.html`
- `dashboard/static/style.css`
- `scheduler/run_pipeline.sh`
- `tests/test_professional_trader.py`
- `tests/test_live_execution.py`
- `tests/test_portfolio_page_layout.py`

## Current evidence

- Information events: {status['information_events']}.
- Decision snapshots: {status['decision_snapshots']}.
- WATCH: {status['watch_decisions']}.
- DO_NOT_TRADE: {status['do_not_trade_decisions']}.
- BUY YES / BUY NO: {status['buy_yes_decisions']} / {status['buy_no_decisions']}.
- HOLD / EXIT / REBUY: {status['hold_decisions']} / {status['exit_decisions']} / {status['rebuy_decisions']}.
- Information-reaction samples: {status['reaction_samples']}.
- Intended orders: {execution['intended_orders']}.
- Observable fills / simulated fills / no-fills: {execution['observable_fills']} / {execution['actual_simulated_fills']} / {execution['no_fills']}.
- Settlements: {execution['settlements']}.
- Settled wins / losses: {execution['wins']} / {execution['losses']}.
- Gross P&L / fees / net P&L: {execution['gross_pnl']} / {execution['fees']} / {execution['net_pnl']}.
- Expectancy / profit factor / maximum drawdown: {execution['expectancy']} / {execution['profit_factor']} / {execution['maximum_drawdown']}.

## Confirmed missing evidence

{chr(10).join(f"- {item}" for item in confirmed_missing_data) or "- None."}

## Safety conclusion

Passing the account capital gate does not authorize trading. Professional
snapshots default to prospective-paper scope, and production submission
requires a separate persisted checklist with `production_order_allowed=true`.
The current frozen cohort cannot set that value.

No production order was submitted by implementation, reporting, or automated
tests. The configured live strategy remains `{freeze.base_strategy_version}`;
it was not changed or promoted.

Next action: {status['next_required_action']}
"""
    (output_dir / "final_report.md").write_text(report)
    return {
        "status": status,
        "manifest": manifest,
        "actions": actions,
        "information": information,
    }
