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
