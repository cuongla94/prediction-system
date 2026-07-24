# Professional Climate Trader Decision System

Overall status: **COLLECTING_PROSPECTIVE_PAPER_EVIDENCE**

## Frozen scope

- Base strategy: `weather-daily-temp` / `v1-2026-07-23`.
- Decision policy: `professional-trader-v2-2026-07-24`.
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

- Information events: 1838.
- Decision snapshots: 3676.
- WATCH: 0.
- DO_NOT_TRADE: 3676.
- BUY YES / BUY NO: 0 / 0.
- HOLD / EXIT / REBUY: 0 / 0 / 0.
- Information-reaction samples: 12866.
- Intended orders: 0.
- Observable fills / simulated fills / no-fills: 0 / 0 / 0.
- Settlements: 0.
- Settled wins / losses: 0 / 0.
- Gross P&L / fees / net P&L: 0 / 0 / 0.
- Expectancy / profit factor / maximum drawdown: None / None / None.

## Confirmed missing evidence

- No settled post-trade process-quality reviews are available.
- No prospective HOLD, EXIT, or re-entry evidence is available.

## Safety conclusion

Passing the account capital gate does not authorize trading. Professional
snapshots default to prospective-paper scope, and production submission
requires a separate persisted checklist with `production_order_allowed=true`.
The current frozen cohort cannot set that value.

No production order was submitted by implementation, reporting, or automated
tests. The configured live strategy remains `v1-2026-07-23`;
it was not changed or promoted.

Next action: Continue prospective collection without tuning.
