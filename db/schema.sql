-- Run this against Supabase's SQL editor (or `psql "$DATABASE_URL" -f db/schema.sql`)
-- once DATABASE_URL is configured.

create table if not exists alerts (
    id bigint generated always as identity primary key,
    created_at timestamptz not null default now(),
    series_ticker text not null,
    event_ticker text not null,
    market_ticker text not null,
    city text not null,
    bracket_label text not null,
    floor_strike double precision,
    cap_strike double precision,
    model_probability double precision not null,
    ensemble_mean double precision,
    ensemble_std double precision,
    -- Tag for which version of the probability engine produced this row, so
    -- alerts from before/after a calibration fix can be told apart later.
    model_version text not null default 'normal-v1',
    -- False until the backtest harness (build step 4) has checked this model
    -- version against real NWS outcomes. The dashboard shows a warning banner
    -- for any unvalidated alert — don't flip this without that check having run.
    calibration_validated boolean not null default false,
    market_yes_price double precision not null,
    edge double precision not null,
    fee_adjusted_threshold double precision not null,
    rules_primary text not null,
    rules_secondary text,
    kalshi_url text not null,
    is_actionable boolean not null default false,
    status text not null default 'open',
    -- Populated once NWS reports the actual settlement, so this table doubles as
    -- the "alerts-and-outcomes" record the backtest harness reads from.
    settled_at timestamptz,
    actual_high_temp double precision,
    actual_outcome boolean
);

create index if not exists alerts_created_at_idx on alerts (created_at desc);
create index if not exists alerts_event_ticker_idx on alerts (event_ticker);
create index if not exists alerts_unsettled_idx on alerts (event_ticker) where settled_at is null;

-- Added 2026-07-18 for the dashboard's countdown-to-close timer. `create table
-- if not exists` above only applies on a fresh database — this alter is what
-- actually reaches an already-deployed `alerts` table, and is itself
-- idempotent (safe to re-run schema.sql against a database that already has
-- the column).
alter table alerts add column if not exists close_time timestamptz;

-- Added 2026-07-18 for scripts/send_notifications.py — a new row is inserted
-- every pipeline run even for a bracket that's been actionable for days, so
-- this can't just check "is_actionable on the latest row"; it needs to know
-- whether a push notification already went out for this market today,
-- tracked by stamping the row that triggered the send.
alter table alerts add column if not exists notified_at timestamptz;

-- Added 2026-07-19 for Low Temperature support (weather/stations.py's
-- Station.metric) — stored directly rather than derived from series_ticker at
-- display time, same reasoning as city/bracket_label already being stored
-- columns instead of re-derived: it's authoritative at insert time (from
-- STATIONS), not something to reconstruct via string-matching later.
-- Default 'max' backfills existing rows correctly, since every row before
-- this column existed was daily-high-temperature.
alter table alerts add column if not exists metric text not null default 'max';

-- Added 2026-07-19 so generate_alerts.py can generate (and the dashboard can
-- group/label) an alert for every currently-open event per city, not just
-- "whichever's soonest" — 0 = today's event (same-day; the forecast lead time
-- this project's calibration was never actually fit for, see
-- kalshi-implementation-progress memory), 1 = tomorrow's (day-ahead; what the
-- live calibration in weather/calibration_params.py was fit and validated
-- against). Nullable: existing rows predate this distinction and can't be
-- reconstructed after the fact (their event's date minus their created_at's
-- date isn't reliably the same thing, since created_at is wall-clock UTC, not
-- the station-local date the pipeline actually reasoned in).
alter table alerts add column if not exists lead_days integer;

-- Added 2026-07-20: the station's already-recorded extreme for the target day
-- at the moment this row was priced (weather/nws_observations.py), or null
-- when there was nothing to condition on (lead_days > 0, or the observations
-- fetch failed). Stored rather than re-derived because it is *not*
-- reconstructable after the fact — it's a point-in-time reading, and by
-- tomorrow the same query returns the finished day's extreme instead of what
-- was known at pricing time. Without it there's no way to audit whether
-- model_probability was conditioned correctly, which is precisely the check
-- that was missing when the no-edge failure went undetected (see
-- kalshi-no-edge-root-cause memory).
alter table alerts add column if not exists observed_so_far double precision;

-- Point-in-time provenance required by the frozen forward-confirmation
-- experiment. Upstreams do not always expose a source publication or model
-- run timestamp; those fields remain null when absent, while collector receipt
-- time is always stored as the conservative "known no earlier than" boundary.
alter table alerts add column if not exists forecast_run_time timestamptz;
alter table alerts add column if not exists forecast_availability_time timestamptz;
alter table alerts add column if not exists observation_event_time timestamptz;
alter table alerts add column if not exists observation_publication_time timestamptz;
alter table alerts add column if not exists observation_collector_received_time timestamptz;

-- Added 2026-07-18 for the dashboard's /status page (monitoring/run_tracker.py)
-- — answers "is our system running well, any errors" from real execution
-- history rather than just inferring it from alerts.created_at freshness.
-- One row per script invocation, updated in place from 'running' to a final
-- status once that invocation finishes (see monitoring/run_tracker.py).
create table if not exists pipeline_runs (
    id bigint generated always as identity primary key,
    script text not null,
    started_at timestamptz not null default now(),
    finished_at timestamptz,
    status text not null default 'running',  -- running | success | partial | failed
    summary text,
    detail text
);

create index if not exists pipeline_runs_started_at_idx on pipeline_runs (started_at desc);
create index if not exists pipeline_runs_script_idx on pipeline_runs (script, started_at desc);

-- One row per ensemble fetch (summary stats, not every member — the probability
-- engine only needs mean/std downstream, and storing 100+ raw members per pull
-- isn't worth the space until something actually needs per-member data).
create table if not exists forecast_pulls (
    id bigint generated always as identity primary key,
    created_at timestamptz not null default now(),
    series_ticker text not null,
    city text not null,
    target_date date not null,
    ensemble_mean double precision not null,
    ensemble_std double precision not null,
    member_count integer not null,
    models_used text not null
);

create index if not exists forecast_pulls_target_date_idx on forecast_pulls (series_ticker, target_date);

-- One row per price check per market, so price movement over time is
-- reconstructable (e.g. "what was the price when this alert fired").
create table if not exists price_snapshots (
    id bigint generated always as identity primary key,
    created_at timestamptz not null default now(),
    market_ticker text not null,
    yes_bid double precision,
    yes_ask double precision,
    last_price double precision
);

create index if not exists price_snapshots_market_ticker_idx on price_snapshots (market_ticker, created_at desc);

-- Added 2026-07-18 for the paper-trading bot (paper_trading/, scripts/run_paper_trading.py).
-- One row per simulated position, from open to close — never real money, no
-- connection to Kalshi's order-placement API. market_ticker is unique: each
-- market only ever gets one paper position (no re-entry once closed, since
-- the same bracket/day never reopens for trading after that).
create table if not exists paper_trades (
    id bigint generated always as identity primary key,
    opened_at timestamptz not null default now(),
    market_ticker text not null unique,
    event_ticker text not null,
    series_ticker text not null,
    city text not null,
    bracket_label text not null,
    side text not null,  -- 'YES' | 'NO'
    entry_price double precision not null,
    contracts integer not null,
    entry_fee double precision not null,
    cost_basis double precision not null,  -- contracts * entry_price + entry_fee; what left the bankroll
    entry_model_probability double precision not null,
    entry_edge double precision not null,
    status text not null default 'open',  -- 'open' | 'closed'
    closed_at timestamptz,
    -- 'settled_win' | 'settled_loss' | 'edge_closed' (sold early once the
    -- model's own edge for this side reversed past a buffer) | 'take_profit'
    -- (sold a winner early at a gain threshold, only when that opt-in rule is
    -- enabled) — see paper_trading/engine.py.
    close_reason text,
    exit_price double precision,
    exit_fee double precision,
    payout double precision,  -- what came back: contracts*$1 (settled win), $0 (settled loss), or contracts*exit_price - exit_fee (early exit)
    realized_pnl double precision  -- payout - cost_basis; null while still open
);

create index if not exists paper_trades_status_idx on paper_trades (status);

-- Added 2026-07-23 for the strategy-integrity follow-up roadmap. paper_trades'
-- history mixes different cash-reserve settings, resets, and sizing-logic
-- changes over time with no way to tell which rows were opened under which
-- config. Stamped once at open time from paper_trading/engine.py::STRATEGY_VERSION
-- (a hand-bumped constant, not a git hash — see that module) so later analysis
-- can filter to "trades under the current locked config" instead of the whole
-- messy history. Nullable: existing rows predate this tag and can't be
-- reconstructed after the fact.
alter table paper_trades add column if not exists strategy_version text;

-- Added 2026-07-19 — the bot lost its entire starting bankroll on its first
-- batch of real settlements (0 of 57, -$100). Rather than deleting that
-- history to give it a clean $100 again, a reset just marks a point in time:
-- realized P&L before the latest reset stops counting toward "cash
-- available" (see dashboard/app.py and scripts/run_paper_trading.py's
-- _current_cash), but every row in paper_trades stays untouched and visible.
create table if not exists bankroll_resets (
    id bigint generated always as identity primary key,
    reset_at timestamptz not null default now(),
    note text
);

-- Added 2026-07-19/20 for the dashboard's "Looking ahead" section — an
-- informational-only forecast for a date Kalshi hasn't opened a tradeable
-- market for yet (typically 2 days out), so there's no bracket structure or
-- market price to compute a real edge against, unlike `alerts`. Reuses the
-- SAME lead=1 calibration in weather/calibration_params.py as a rough
-- approximation (this hasn't been separately backtested/validated at a
-- 2-day lead time) — calibrated_mean/calibrated_std should always be shown
-- with a clear "exploratory, not a trading signal" caveat, same spirit as
-- everything else this dashboard flags as less-validated. Same insert-only,
-- read-latest-via-DISTINCT-ON pattern as `alerts`, for consistency — not
-- because this data needs backtesting history the way alerts does.
create table if not exists forecast_previews (
    id bigint generated always as identity primary key,
    created_at timestamptz not null default now(),
    series_ticker text not null,
    city text not null,
    metric text not null,
    target_date date not null,
    lead_days integer not null,
    ensemble_mean double precision not null,
    ensemble_std double precision not null,
    calibrated_mean double precision not null,
    calibrated_std double precision not null
);

create index if not exists forecast_previews_lookup_idx on forecast_previews (series_ticker, target_date, created_at desc);

-- Added 2026-07-21 — master toggle for real-money trading, with audit log.
-- Append-only: new rows record state changes with timestamp and reason, preserving
-- full history of who enabled/disabled and when.
create table if not exists trading_controls (
    id bigint generated always as identity primary key,
    real_money_trading_enabled boolean not null default false,
    updated_at timestamptz not null default now(),
    updated_by text,        -- who flipped it (e.g., session passcode label)
    note text                -- why (e.g., "edge cleared Stage 1")
);

create index if not exists trading_controls_updated_at_idx on trading_controls (updated_at desc);

-- Added 2026-07-23 for Stage 3's persistent bot-control state
-- (bot_control/state.py). Same append-only, full-snapshot-per-row pattern
-- as trading_controls above (not a diff-per-row log): every insert carries
-- the COMPLETE resulting state, so "current state" is always just the
-- latest row by created_at, and no separate reconstruction/replay step is
-- needed to answer "what is the state right now." requested_mode is
-- recorded even when rejected (effective_mode stays whatever it already
-- was) so a request for an unimplemented mode is an audit-visible event,
-- not a silently dropped one. Only 'OFF' and 'PAPER' are implemented
-- execution modes through the legacy generic endpoint. LIVE is recorded only
-- by the dedicated production enablement path after all backend gates pass;
-- generic LIVE requests remain NOT_IMPLEMENTED so they cannot bypass it.
create table if not exists bot_control_events (
    id bigint generated always as identity primary key,
    created_at timestamptz not null default now(),
    event_type text not null,  -- 'start_requested' | 'start_rejected' | 'stop' | 'run_once' | 'kill' | 'kill_reset' | 'reconcile' | 'refresh_balance'
    requested_mode text,       -- OFF | PAPER | SHADOW | DEMO | LIVE_CANARY | LIVE
    effective_mode text not null default 'OFF',
    enabled boolean not null default false,
    kill_switch boolean not null default false,
    kill_switch_reason text,
    strategy_name text,
    strategy_version text,
    actor text,                -- who/what triggered this (session passcode label, 'cron', etc.)
    reason_code text,          -- e.g. 'OK' | 'NOT_IMPLEMENTED' | 'ALREADY_RUNNING'
    note text,
    detail text                -- free-form summary (e.g. reconcile/refresh-balance result)
);

create index if not exists bot_control_events_created_at_idx on bot_control_events (created_at desc);
alter table bot_control_events add column if not exists live_enabled boolean not null default false;

-- Added 2026-07-23 for narrowly scoped, bot-owned Kalshi production orders.
-- Manual Kalshi orders never get a row here and are therefore never eligible
-- for bot cancellation or bot P&L attribution.
create table if not exists live_orders (
    id bigint generated always as identity primary key,
    local_order_id text not null unique,
    signal_id text not null,
    decision_id text not null,
    strategy_name text not null,
    strategy_version text not null,
    market_ticker text not null,
    event_ticker text not null,
    event_date date not null,
    client_order_id text not null unique,
    kalshi_order_id text unique,
    intended_outcome text not null check (intended_outcome in ('YES', 'NO')),
    api_book_side text not null check (api_book_side in ('bid', 'ask')),
    submitted_yes_price numeric(18, 4) not null,
    model_probability numeric(18, 8) not null,
    maximum_acceptable_price numeric(18, 4) not null,
    requested_count numeric(18, 2) not null,
    filled_count numeric(18, 2) not null default 0,
    remaining_count numeric(18, 2) not null default 0,
    average_fill_price numeric(18, 4),
    estimated_fees numeric(18, 4) not null default 0,
    actual_fees numeric(18, 4) not null default 0,
    status text not null default 'PENDING'
        check (status in (
            'PENDING', 'SUBMITTING', 'UNKNOWN', 'RESTING', 'PARTIAL',
            'FILLED', 'CANCELED', 'REJECTED', 'SETTLED'
        )),
    bot_owned boolean not null default true check (bot_owned),
    decision_at timestamptz not null,
    quote_at timestamptz not null,
    weather_data_at timestamptz not null,
    expires_at timestamptz,
    created_at timestamptz not null default now(),
    submitted_at timestamptz,
    acknowledged_at timestamptz,
    filled_at timestamptz,
    canceled_at timestamptz,
    settled_at timestamptz,
    settlement_result text,
    realized_pnl numeric(18, 4),
    mark_to_market_pnl numeric(18, 4) not null default 0,
    error_code text,
    error_detail text,
    reconciliation_status text not null default 'PENDING',
    last_reconciled_at timestamptz,
    unique (
        strategy_version, decision_id, market_ticker, intended_outcome,
        api_book_side, submitted_yes_price, requested_count, decision_at
    )
);

create index if not exists live_orders_status_idx on live_orders (status, created_at);
create index if not exists live_orders_event_idx on live_orders (event_ticker, event_date);

create table if not exists live_order_events (
    id bigint generated always as identity primary key,
    live_order_id bigint not null references live_orders(id),
    created_at timestamptz not null default now(),
    from_status text,
    to_status text not null,
    event_type text not null,
    actor text not null,
    detail text
);

create index if not exists live_order_events_order_idx
    on live_order_events (live_order_id, created_at);

create table if not exists live_order_fills (
    id bigint generated always as identity primary key,
    live_order_id bigint not null references live_orders(id),
    kalshi_fill_id text not null unique,
    kalshi_order_id text not null,
    count numeric(18, 2) not null,
    yes_price numeric(18, 4) not null,
    fee numeric(18, 4) not null default 0,
    filled_at timestamptz,
    created_at timestamptz not null default now()
);

create table if not exists live_reconciliation_runs (
    id bigint generated always as identity primary key,
    started_at timestamptz not null default now(),
    finished_at timestamptz,
    healthy boolean not null default false,
    available_cash numeric(18, 4),
    local_order_count integer not null default 0,
    remote_bot_order_count integer not null default 0,
    fill_count integer not null default 0,
    position_count integer not null default 0,
    settlement_count integer not null default 0,
    mismatch_count integer not null default 0,
    detail text,
    actor text not null
);

create index if not exists live_reconciliation_runs_started_idx
    on live_reconciliation_runs (started_at desc);

create table if not exists live_execution_cycles (
    id bigint generated always as identity primary key,
    started_at timestamptz not null default now(),
    finished_at timestamptz,
    status text not null default 'running',
    submitted_orders integer not null default 0,
    reconciled_orders integer not null default 0,
    canceled_orders integer not null default 0,
    blocker text,
    error_detail text,
    summary text
);

create index if not exists live_execution_cycles_started_idx
    on live_execution_cycles (started_at desc);

-- Added 2026-07-24 for the focused real-trading-readiness experiment.
-- These tables are deliberately narrow and append-only: they preserve the
-- exact point-in-time inputs, stream recovery evidence, frozen candidate
-- configuration, and conservative paper outcomes needed to decide whether
-- the failed climate strategy ever earns reconsideration. They never contain
-- or trigger a production order.
create table if not exists forward_candidate_freezes (
    id bigint generated always as identity primary key,
    strategy_name text not null,
    strategy_version text not null unique,
    model_weight numeric(9, 8) not null,
    market_weight numeric(9, 8) not null,
    calibration_method text not null,
    signal_threshold numeric(9, 8) not null,
    no_trade_filters jsonb not null,
    maximum_acceptable_price_logic text not null,
    frozen_at timestamptz not null,
    confirmatory_period_start timestamptz not null,
    required_independent_event_count integer not null,
    code_config_hash text not null,
    created_at timestamptz not null default now(),
    check (abs(model_weight + market_weight - 1.0) < 0.00000001)
);

create table if not exists forward_orderbook_snapshots (
    id bigint generated always as identity primary key,
    market_ticker text not null,
    source text not null check (
        source in (
            'REST_INITIAL', 'REST_RECOVERY', 'WEBSOCKET_SNAPSHOT',
            'WEBSOCKET_DELTA'
        )
    ),
    source_publish_time timestamptz,
    collector_received_time timestamptz not null,
    sequence_number bigint,
    recovery_reason text,
    best_yes_bid numeric(18, 6),
    best_yes_ask numeric(18, 6),
    best_no_bid numeric(18, 6),
    best_no_ask numeric(18, 6),
    spread numeric(18, 6),
    depth_levels jsonb not null,
    last_trade numeric(18, 6),
    market_status text,
    volume numeric(24, 6),
    open_interest numeric(24, 6),
    stale boolean not null,
    crossed_or_impossible boolean not null,
    missing_opposing_levels boolean not null,
    sequence_gap boolean not null,
    delayed_local_receipt boolean not null,
    created_at timestamptz not null default now()
);

create index if not exists forward_orderbook_snapshots_lookup_idx
    on forward_orderbook_snapshots (market_ticker, collector_received_time desc);

create table if not exists forward_market_messages (
    id bigint generated always as identity primary key,
    message_type text not null,
    subscription_id bigint,
    sequence_number bigint,
    market_ticker text,
    source_publish_time timestamptz,
    collector_received_time timestamptz not null,
    gap_detected boolean not null default false,
    payload jsonb not null,
    created_at timestamptz not null default now()
);

create index if not exists forward_market_messages_lookup_idx
    on forward_market_messages (market_ticker, collector_received_time desc);

create table if not exists forward_evidence_decisions (
    id bigint generated always as identity primary key,
    decision_id text not null unique,
    event_ticker text not null,
    market_ticker text not null,
    city text not null,
    station text not null,
    target_date date not null,
    strategy_version text not null,
    candidate_version text not null references forward_candidate_freezes(strategy_version),
    forecast_model text not null,
    forecast_run_time timestamptz,
    forecast_availability_time timestamptz,
    forecast_values jsonb not null,
    observation_event_time timestamptz,
    observation_publication_time timestamptz,
    observation_collector_received_time timestamptz,
    observation_revision text,
    observed_high_at_decision numeric(18, 6),
    orderbook_snapshot_id bigint not null references forward_orderbook_snapshots(id),
    model_probability numeric(18, 10) not null,
    market_probability numeric(18, 10),
    final_candidate_probability numeric(18, 10),
    selected_side text check (selected_side in ('YES', 'NO')),
    maximum_acceptable_price numeric(18, 10),
    fee_adjusted_edge numeric(18, 10),
    rejection_reason text,
    intended_quantity numeric(18, 6) not null,
    event_time timestamptz,
    source_publish_time timestamptz,
    collector_received_time timestamptz not null,
    decision_time timestamptz not null,
    created_at timestamptz not null default now()
);

create index if not exists forward_evidence_decisions_candidate_idx
    on forward_evidence_decisions (candidate_version, decision_time);

create table if not exists forward_paper_order_events (
    id bigint generated always as identity primary key,
    decision_row_id bigint not null references forward_evidence_decisions(id),
    candidate_version text not null references forward_candidate_freezes(strategy_version),
    event_type text not null check (
        event_type in (
            'INELIGIBLE', 'NO_FILL', 'PARTIAL_FILL', 'FILLED',
            'SETTLED_WIN', 'SETTLED_LOSS', 'VOID'
        )
    ),
    requested_quantity numeric(18, 6) not null,
    filled_quantity numeric(18, 6) not null,
    weighted_fill_price numeric(18, 6),
    estimated_fee numeric(18, 6) not null default 0,
    settlement_result text,
    net_pnl numeric(18, 6),
    reason text not null,
    created_at timestamptz not null
);

create index if not exists forward_paper_order_events_decision_idx
    on forward_paper_order_events (decision_row_id, created_at);

-- Enforce append-only evidence at the database boundary. Settlement is a new
-- forward_paper_order_events row; no earlier decision/snapshot/event is edited.
create or replace function prevent_forward_evidence_mutation()
returns trigger language plpgsql as $$
begin
    raise exception 'forward readiness evidence is append-only';
end;
$$;

drop trigger if exists forward_candidate_freezes_append_only
    on forward_candidate_freezes;
create trigger forward_candidate_freezes_append_only
before update or delete on forward_candidate_freezes
for each row execute function prevent_forward_evidence_mutation();

drop trigger if exists forward_orderbook_snapshots_append_only
    on forward_orderbook_snapshots;
create trigger forward_orderbook_snapshots_append_only
before update or delete on forward_orderbook_snapshots
for each row execute function prevent_forward_evidence_mutation();

drop trigger if exists forward_market_messages_append_only
    on forward_market_messages;
create trigger forward_market_messages_append_only
before update or delete on forward_market_messages
for each row execute function prevent_forward_evidence_mutation();

drop trigger if exists forward_evidence_decisions_append_only
    on forward_evidence_decisions;
create trigger forward_evidence_decisions_append_only
before update or delete on forward_evidence_decisions
for each row execute function prevent_forward_evidence_mutation();

drop trigger if exists forward_paper_order_events_append_only
    on forward_paper_order_events;
create trigger forward_paper_order_events_append_only
before update or delete on forward_paper_order_events
for each row execute function prevent_forward_evidence_mutation();

-- Added 2026-07-24 for the focused professional climate-trader decision
-- layer. This extends the existing forward evidence chain; it does not create
-- a second strategy, scheduler, exchange client, or order path. Every table is
-- append-only so later outcomes cannot rewrite what the system knew or why it
-- acted at an earlier decision time.
create table if not exists professional_strategy_freezes (
    id bigint generated always as identity primary key,
    base_strategy_name text not null,
    base_strategy_version text not null,
    decision_policy_version text not null unique,
    probability_method_version text not null,
    policy_config jsonb not null,
    code_config_hash text not null,
    frozen_at timestamptz not null,
    forward_period_start timestamptz not null,
    automatic_promotion_allowed boolean not null default false,
    created_at timestamptz not null default now()
);

create table if not exists professional_contract_truth (
    id bigint generated always as identity primary key,
    contract_truth_id text not null unique,
    event_ticker text not null,
    market_ticker text not null,
    decision_policy_version text not null references
        professional_strategy_freezes(decision_policy_version),
    status text not null check (status in ('CLEAR', 'CONTRACT_TRUTH_UNCLEAR')),
    truth jsonb not null,
    source_market_payload jsonb,
    source_collected_at timestamptz not null,
    created_at timestamptz not null default now()
);

create index if not exists professional_contract_truth_market_idx
    on professional_contract_truth (market_ticker, source_collected_at desc);

create table if not exists professional_information_events (
    id bigint generated always as identity primary key,
    information_event_id text not null unique,
    event_type text not null,
    source text not null,
    source_event_time timestamptz,
    source_publication_time timestamptz,
    collector_receipt_time timestamptz not null,
    processing_time timestamptz not null,
    event_ticker text not null,
    market_ticker text not null,
    previous_value jsonb,
    new_value jsonb,
    material boolean not null,
    related_decision_id text,
    created_at timestamptz not null default now()
);

create index if not exists professional_information_events_market_idx
    on professional_information_events
        (market_ticker, processing_time desc);

create table if not exists professional_information_reactions (
    id bigint generated always as identity primary key,
    information_event_id text not null references
        professional_information_events(information_event_id),
    sample_label text not null,
    target_time timestamptz not null,
    orderbook_snapshot_id bigint references forward_orderbook_snapshots(id),
    observed_at timestamptz,
    executable_yes_price numeric(18, 10),
    executable_no_price numeric(18, 10),
    spread numeric(18, 10),
    yes_depth numeric(18, 6),
    no_depth numeric(18, 6),
    hypothetical_limit_fill_supported boolean,
    created_at timestamptz not null default now(),
    unique (information_event_id, sample_label)
);

create table if not exists professional_decision_snapshots (
    id bigint generated always as identity primary key,
    decision_id text not null unique,
    parent_decision_id text,
    related_alert_id bigint references alerts(id),
    forward_evidence_decision_id bigint references forward_evidence_decisions(id),
    contract_truth_id text not null references
        professional_contract_truth(contract_truth_id),
    triggering_information_event_id text references
        professional_information_events(information_event_id),
    event_ticker text not null,
    market_ticker text not null,
    strategy_name text not null,
    strategy_version text not null,
    candidate_version text not null,
    decision_policy_version text not null references
        professional_strategy_freezes(decision_policy_version),
    action text not null check (
        action in (
            'DO_NOT_TRADE', 'WATCH', 'BUY_YES', 'BUY_NO',
            'HOLD', 'EXIT', 'REBUY_YES', 'REBUY_NO'
        )
    ),
    decision_reason_code text not null,
    thesis_type text not null,
    net_edge_after_costs numeric(18, 10),
    maximum_loss numeric(18, 6) not null,
    confidence_level text not null,
    blockers jsonb not null,
    contract_truth jsonb not null,
    weather_state jsonb not null,
    market_state jsonb not null,
    account_state jsonb not null,
    information_as_of jsonb not null,
    probability jsonb not null,
    execution jsonb not null,
    thesis jsonb not null,
    pretrade_checklist jsonb not null,
    snapshot jsonb not null,
    production_order_allowed boolean not null default false,
    next_review_trigger text not null,
    decision_time timestamptz not null,
    created_at timestamptz not null default now()
);

create index if not exists professional_decision_snapshots_market_idx
    on professional_decision_snapshots
        (market_ticker, candidate_version, decision_time desc);
create index if not exists professional_decision_snapshots_action_idx
    on professional_decision_snapshots (action, decision_time desc);

create table if not exists professional_journal_events (
    id bigint generated always as identity primary key,
    journal_event_id text not null unique,
    event_ticker text not null,
    market_ticker text not null,
    candidate_version text not null,
    record_type text not null check (
        record_type in (
            'INFORMATION_EVENT', 'DECISION_SNAPSHOT', 'INTENDED_ORDER',
            'ACTUAL_ORDER', 'FILL', 'POSITION_REVIEW', 'EXIT', 'REENTRY',
            'SETTLEMENT', 'POST_TRADE_REVIEW'
        )
    ),
    record_id text not null,
    parent_record_type text,
    parent_record_id text,
    event_time timestamptz,
    source_publication_time timestamptz,
    collector_receipt_time timestamptz,
    processing_time timestamptz not null,
    decision_time timestamptz,
    order_time timestamptz,
    fill_time timestamptz,
    settlement_time timestamptz,
    payload jsonb not null,
    created_at timestamptz not null default now()
);

create index if not exists professional_journal_market_idx
    on professional_journal_events
        (market_ticker, candidate_version, processing_time);

create table if not exists professional_post_trade_reviews (
    id bigint generated always as identity primary key,
    review_id text not null unique,
    decision_id text not null references
        professional_decision_snapshots(decision_id),
    market_ticker text not null,
    classification text not null check (
        classification in (
            'GOOD_DECISION_GOOD_OUTCOME',
            'GOOD_DECISION_BAD_OUTCOME',
            'BAD_DECISION_GOOD_OUTCOME',
            'BAD_DECISION_BAD_OUTCOME',
            'INSUFFICIENT_EVIDENCE'
        )
    ),
    settled_outcome text not null,
    process_correct boolean not null,
    outcome_favorable boolean not null,
    settlement_revision boolean not null default false,
    review jsonb not null,
    reviewed_at timestamptz not null,
    created_at timestamptz not null default now()
);

create table if not exists professional_action_alerts (
    id bigint generated always as identity primary key,
    alert_id text not null unique,
    alert_type text not null check (
        alert_type in (
            'TRADE_READY', 'POSITION_INVALIDATED', 'EXIT_RECOMMENDED',
            'REENTRY_READY', 'RISK_BLOCKED', 'DATA_STALE',
            'RECONCILIATION_REQUIRED', 'SETTLED_REVIEW_READY'
        )
    ),
    decision_id text references professional_decision_snapshots(decision_id),
    market_ticker text not null,
    action text not null,
    payload jsonb not null,
    created_at timestamptz not null default now()
);

-- Apply the same database-level immutability rule to the professional journal.
drop trigger if exists professional_strategy_freezes_append_only
    on professional_strategy_freezes;
create trigger professional_strategy_freezes_append_only
before update or delete on professional_strategy_freezes
for each row execute function prevent_forward_evidence_mutation();

drop trigger if exists professional_contract_truth_append_only
    on professional_contract_truth;
create trigger professional_contract_truth_append_only
before update or delete on professional_contract_truth
for each row execute function prevent_forward_evidence_mutation();

drop trigger if exists professional_information_events_append_only
    on professional_information_events;
create trigger professional_information_events_append_only
before update or delete on professional_information_events
for each row execute function prevent_forward_evidence_mutation();

drop trigger if exists professional_information_reactions_append_only
    on professional_information_reactions;
create trigger professional_information_reactions_append_only
before update or delete on professional_information_reactions
for each row execute function prevent_forward_evidence_mutation();

drop trigger if exists professional_decision_snapshots_append_only
    on professional_decision_snapshots;
create trigger professional_decision_snapshots_append_only
before update or delete on professional_decision_snapshots
for each row execute function prevent_forward_evidence_mutation();

drop trigger if exists professional_journal_events_append_only
    on professional_journal_events;
create trigger professional_journal_events_append_only
before update or delete on professional_journal_events
for each row execute function prevent_forward_evidence_mutation();

drop trigger if exists professional_post_trade_reviews_append_only
    on professional_post_trade_reviews;
create trigger professional_post_trade_reviews_append_only
before update or delete on professional_post_trade_reviews
for each row execute function prevent_forward_evidence_mutation();

drop trigger if exists professional_action_alerts_append_only
    on professional_action_alerts;
create trigger professional_action_alerts_append_only
before update or delete on professional_action_alerts
for each row execute function prevent_forward_evidence_mutation();
