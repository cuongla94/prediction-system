# kalshi-prediction-market

Semi-automated signal system for Kalshi's daily temperature markets. Compares
weather ensemble forecasts (Open-Meteo) against Kalshi prices, surfaces edge
above fees + a safety margin as reviewable alerts on a dashboard. No
auto-execution — a human clicks through to Kalshi and trades manually.

Cities: NYC, Chicago, Philadelphia, Austin, Denver, Miami.

**Current status:** pipeline (steps 1-4) and dashboard (step 5) are built and
backtested; the probability model has a validated bias correction but is
**not validated for live trading** — see the dashboard's warning banner and
`kalshi-backtest-findings` notes for specifics. Scheduler (step 6) runs via
cron; nothing is deployed to a server yet.

## Setup

```bash
uv sync
cp .env.example .env   # fill in real values — see below
```

Series/event/market discovery is public and needs no credentials. Fill in the
rest of `.env` as each piece becomes relevant:

- `KALSHI_API_KEY_ID` / `KALSHI_PRIVATE_KEY_PATH` — only needed for
  authenticated (portfolio/trading) endpoints, which nothing here calls yet
  (no auto-execution is planned). Keep the `.pem` outside the repo or under a
  gitignored path (e.g. `secrets/`) — never commit it.
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

- `kalshi_client/` — Kalshi REST client: RSA-PSS request signing (`auth.py`),
  the client itself (`client.py`, including `get_historical_markets` for data
  older than Kalshi's live/historical cutoff), response models (`models.py`),
  fees (`fees.py`), market URLs (`urls.py`), ticker date parsing (`tickers.py`).
- `weather/` — Open-Meteo ensemble client (`open_meteo.py`), historical
  Previous-Runs client for backtesting (`historical_forecast.py`), per-city NWS
  station config (`stations.py`), the probability engine (`probability.py`) and
  its fitted bias/std correction (`calibration_params.py`).
- `edge/` — model-vs-market edge calculator (`calculator.py`).
- `db/schema.sql` — Postgres schema: `alerts`, `forecast_pulls`, `price_snapshots`.
- `backtest/` — harness (`harness.py`) and calibration diagnostics
  (`calibration.py`) used to validate the probability engine against real
  settled markets, plus a Redis cache (`cache.py`) so re-running a backtest
  doesn't re-fetch months of immutable historical data every time.
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
uv run python -m dashboard.app             # dashboard dev server (also registered in .claude/launch.json)
```

## Scheduler

`scheduler/run_pipeline.sh` chains `scripts/generate_alerts.py` (new alerts)
and `scripts/mark_settled_alerts.py` (write back outcomes for pending ones) —
each runs regardless of whether the other failed, since they're independent
concerns. `scheduler/crontab.example` runs it four times a day (~4-5 hours
after each GFS/ICON model init), aligned to when Open-Meteo has actually
ingested the new run; the settlement check is cheap enough to just ride along
on the same cadence rather than needing its own. Install with
`crontab scheduler/crontab.example` after editing the placeholder path — cron
needs an absolute path to both the script and the log file. Not running
anywhere yet; this is ready to install once there's a host (the plan is a
DigitalOcean droplet) to install it on.

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
