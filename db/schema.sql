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
