# kalshi-prediction-market

Weather-signal and tightly constrained automated-execution system for Kalshi's
daily temperature markets. It compares ensemble forecasts (Open-Meteo) with
Kalshi prices, surfaces reviewable alerts, and can submit one-contract
production limit orders only through a separately enabled, persisted live path.

Coverage: 20 cities across 40 configured high/low temperature series.

**Current strategy status: FAILED / NOT PROMOTED.** The latest point-in-time
investigation found no defensible executable edge and left the configured live
strategy unchanged. The execution infrastructure is operational code, not an
endorsement of the strategy. Live enablement stays blocked unless every
production, capital, reconciliation, data-freshness, kill-switch, and risk gate
passes.

## Setup

```bash
uv sync
cp .env.example .env   # fill in real values — see below
```

Series/event/market discovery is public and needs no credentials. Fill in the
rest of `.env` as each piece becomes relevant:

- `KALSHI_API_KEY_ID` / `KALSHI_PRIVATE_KEY_PATH` — required for authenticated
  portfolio and order endpoints. Keep the `.pem` outside the repo or under a
  gitignored path (for example `secrets/`) and never commit it.
- `KALSHI_ENVIRONMENT` — `production` or `demo`. Live enablement requires
  `production` and verifies that the client actually targets Kalshi production.
  `KALSHI_PRODUCTION_BASE_URL` and `KALSHI_DEMO_BASE_URL` may override the
  environment defaults; `KALSHI_BASE_URL` remains a compatibility override.
- `KALSHI_SUBACCOUNT` — optional numeric subaccount header.
- `DATABASE_URL` — Postgres connection string (Supabase or otherwise). Without
  it, the dashboard falls back to a frozen demo snapshot instead of failing.
  Run `db/schema.sql` once against a fresh database (Supabase SQL editor, or
  `psql "$DATABASE_URL" -f db/schema.sql`) before the pipeline can write to it.
- `DATABASE_SSL_CA_FILE` — CA cert for the DB connection, if your provider
  requires it. Same rule as the Kalshi key: gitignored path, never committed.
- `REDIS_URL` / `REDIS_KEY_PREFIX` / `REDIS_DEFAULT_TTL_SECONDS` — optional.
  Only `backtest/cache.py` uses this, to avoid re-fetching months of immutable
  historical data on every backtest run. Nothing else needs it; everything
  falls back to an uncached fetch if it's unset.

All entry points (`scripts/*.py`, `dashboard/app.py`) call `load_dotenv()`
themselves — no need to `export` these into your shell manually.

## Structure

- `kalshi_client/` — Kalshi REST client: RSA-PSS signing, current V2 market and
  portfolio reads, create/cancel order calls, order-book conversion, response
  models, fees, URLs, and ticker parsing.
- `live_trading/` — persisted bot-owned order lifecycle, deterministic
  idempotency, account reconciliation, fixed risk, and one-cycle execution
  service. Manual account orders remain outside bot ownership.
- `weather/` — Open-Meteo ensemble client (`open_meteo.py`), historical
  Previous-Runs client for backtesting (`historical_forecast.py`), per-city NWS
  station config (`stations.py`), the probability engine (`probability.py`) and
  its fitted bias/std correction (`calibration_params.py`).
- `edge/` — model-vs-market edge calculator (`calculator.py`).
- `db/schema.sql` — Postgres schema for alerts, snapshots, bot control, and the
  live order/event/fill/reconciliation/execution audit trail.
- `backtest/` — harness (`harness.py`) and calibration diagnostics
  (`calibration.py`) used to validate the probability engine against real
  settled markets, plus a Redis cache (`cache.py`) so re-running a backtest
  doesn't re-fetch months of immutable historical data every time.
- `strategy_research/` and `artifacts/strategy_investigation/` — canonical
  point-in-time investigation, no-lookahead checks, candidate registry,
  chronological folds, final holdout, and reproducible evidence.
- `trading_readiness/` and `artifacts/trading_readiness/` — corrected metric
  populations, city/date-clustered uncertainty, explicit readiness gates,
  immutable confirmatory candidates, append-only orderbook evidence, and
  conservative prospective paper execution.
- `dashboard/` — Flask app showing alert cards (rules text, kid-readable stat
  tooltips, a calibration-status banner). Reads the latest alert per market
  from Postgres; falls back to a real-but-frozen demo snapshot if `DATABASE_URL`
  is unset or unreachable.
- `scheduler/` — cron wrapper (`run_pipeline.sh`, chains generate + settle)
  and an example crontab (`crontab.example`) for running unattended.
- `scripts/` — entry points; see Commands below.
- `tests/` — unit tests (`uv run pytest`).

## Commands

```bash
uv run pytest                              # run tests
uv run ruff check .                        # lint
uv run scripts/discover_markets.py         # public smoke test: series -> event -> brackets, no credentials needed
uv run scripts/forecast_vs_market.py       # one city's calibrated model vs. live Kalshi prices
uv run scripts/generate_alerts.py          # full pipeline, all 6 cities; writes to Postgres if DATABASE_URL is set
uv run scripts/mark_settled_alerts.py      # checks pending alerts against Kalshi, writes back settled_at/actual_outcome
uv run scripts/run_backtest.py             # backtests the probability engine against real settled markets
uv run scripts/run_strategy_investigation.py # read-only research run; never promotes a strategy
uv run scripts/run_trading_readiness_report.py # read-only readiness audit/artifacts
uv run scripts/run_professional_trader_report.py # read-only professional journal status/freeze
uv run scripts/run_forward_evidence_collector.py --mode once # append prospective paper decisions
uv run scripts/run_forward_evidence_collector.py --mode stream # persistent orderbook/trade/fill evidence
uv run scripts/run_live_execution.py       # one reconciled cycle; submits only when explicitly enabled
uv run python -m dashboard.app             # dashboard dev server (also registered in .claude/launch.json)
```

## Production automation

The only live-enable path is the **Live automation** control inside the
existing **Your Kalshi portfolio** tab. It performs a fresh reconciliation,
then requires the exact confirmation `ENABLE LIVE TRADING`. Available
production cash must be strictly greater than `$5.00`.

Fixed, non-configurable limits are one contract, `$1` per order, `$1` open per
market, `$2` per event/date, `$5` total open exposure, `$2` daily realized-loss
stop, `$3` mark-to-market stop, and a two-consecutive-loss stop. Orders are
limit-only. OFF mode continues reconciliation and settlement work but cannot
submit.

The recurring execution cycle is called by the existing scheduler scripts; do
not install a second scheduler. Apply the current `db/schema.sql` before
enabling so the live audit tables exist. Emergency stop cancels only bot-owned
orders and never touches manual account orders.

## Real-trading readiness evidence

The strategy remains `FAILED` and is not ready for real-money automation. The
readiness investigation froze exactly two research candidates:

- `forward-blend-model-0.50-market-0.50-v1`
- `forward-blend-model-0.25-market-0.75-v1`

`model_weight` and `market_weight` are literal coefficients and sum to one.
The older `market_blend_weight` was the market coefficient; the implementation
was correct but its label was ambiguous.

Before collecting forward evidence:

1. Apply the current `db/schema.sql`.
2. Run `scripts/run_trading_readiness_report.py` to create the immutable freeze
   manifest.
3. Set `FORWARD_EVIDENCE_ENABLED=1`. The production deployment uses the
   tracked `deploy/forward-evidence.env` switch so the setting is auditable and
   shared by the existing systemd price feed and cron pipeline.
4. Restart the existing `kalshi-price-feed` service and leave the existing
   pipeline schedule installed.

The existing price-feed service then uses Kalshi's authenticated V2 WebSocket
for orderbook snapshots/deltas, ticker, trades, lifecycle, and fills. REST full
books are authoritative after initial connection, reconnect, sequence gaps, and
process restart. The existing pipeline appends prospective candidate decisions.
Neither path calls create/cancel order.

The provisional reconsideration gate is at least 60 forward calendar days, 100
independent city/date events, and 100 settled eligible paper trades, with
positive fee-aware expectancy, profit factor above one, approved drawdown,
stable city/date results, no unresolved integrity violations, and explicit
human review. There is no automatic promotion.

## Professional climate-trader decisions

The existing forward collector also drives one focused
`SEE → HEAR → THINK → ACT → REVIEW` pipeline. It does not replace the
forecasting model. It records explicit contract truth, point-in-time weather,
executable orderbook prices, real account/risk state, material information
events, immutable decisions, written theses/counterarguments/invalidation,
and post-settlement process reviews.

The only decision states are `DO_NOT_TRADE`, `WATCH`, `BUY_YES`, `BUY_NO`,
`HOLD`, `EXIT`, `REBUY_YES`, and `REBUY_NO`. A lower price by itself cannot
cause a re-entry. Research decisions remain prospective-paper-only and cannot
be promoted automatically.

The alert-details modal reuses the existing market surface for a compact
Professional trader view. The Kalshi Portfolio tab adds only current bot
action, open-position thesis, last decision, and next review. Full evidence
stays in `artifacts/professional_trader/`.

Before collection, apply `db/schema.sql`, run both read-only report commands
to create/verify the two immutable manifests, and then enable the existing
forward collector. Live execution additionally requires a persisted
professional checklist with explicit production permission; the frozen
research cohort cannot produce that permission.

## Scheduler

`scheduler/run_pipeline.sh` chains alert generation, settlement, and one live
execution cycle. Each stage is isolated so one failure does not suppress the
other safety work. `scheduler/crontab.example` runs it four times a day (~4-5 hours
after each GFS/ICON model init), aligned to when Open-Meteo has actually
ingested the new run; the settlement check is cheap enough to just ride along
on the same cadence rather than needing its own. Install with
`crontab scheduler/crontab.example` after editing the placeholder path — cron
needs an absolute path to both the script and the log file. The live cycle is
inert unless the dedicated production enablement state is on and every gate
passes.

Without `mark_settled_alerts.py` running regularly, the dashboard's "latest
alert per market" query has nothing to exclude old, resolved markets —
each day's alerts use distinct tickers, so they'd otherwise accumulate
forever instead of being filtered out once settled.

## Deployment

See `deploy/README.md` for the full droplet setup runbook — creating the
droplet, getting the code and secrets onto it, and starting the dashboard
(via gunicorn + systemd, `deploy/kalshi-dashboard.service`) and the cron
schedule. `dashboard/app.py`'s dev server has `debug=True` off by default
(opt in locally with `FLASK_DEBUG=1`) — the Werkzeug debugger allows
arbitrary code execution and must never be on anywhere reachable beyond
localhost.

## Notes

- Ticker hierarchy (series → event → market) is always resolved at runtime via
  the API, never hardcoded — Kalshi has changed ticker conventions before, and
  dead/legacy series with plausible-looking titles coexist with live ones.
- Settlement ground truth is the NWS Daily Climate Report for the market's
  station, which reports in local *standard* time even during DST — handled
  via a fixed UTC offset per city in `weather/stations.py`, not the city's
  regular (DST-aware) timezone.
- The probability engine's bias/std correction (`weather/calibration_params.py`)
  is a fitted snapshot from one backtest run, not a permanent constant —
  re-run `scripts/run_backtest.py` periodically and update it as more settled
  days accumulate.
