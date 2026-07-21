#!/usr/bin/env bash
# Tight-cadence companion to run_pipeline.sh, added 2026-07-20 after
# scripts/measure_pipeline_latency.py found a real, measured cost from
# generate_alerts.py's only cadence (0 5,11,17,23 UTC, every 6 hours): a
# same-day bracket's observed-so-far can sit up to ~3-6h stale relative to a
# live decision instant, and NYC's own 15:00 same-day-proof numbers showed
# that staleness alone degrading model Brier from 0.0790 to 0.1034 -- a real,
# recoverable mechanical cost, not a modeling one (Denver: 0.0951 -> 0.1043).
# The live NWS feed itself is not the bottleneck (confirmed live: a fresh
# METAR is available within minutes); the bottleneck was purely how often
# this project looked at it.
#
# Deliberately its own script, not folded into run_settlement_cycle.sh, which
# explicitly documents that neither of ITS steps writes anything
# generate_alerts.py's own output depends on -- this one does, on purpose
# (fresher alerts.observed_so_far/model_probability/market_yes_price for
# lead_days=0 rows), so keeping it separate keeps that existing doc-comment
# true rather than silently stale.
#
# Cheap for the same reason mark_settled_alerts.py's batching is: one NWS call
# and one Kalshi get_markets() call per city, no Open-Meteo refetch --
# scripts/refresh_same_day_observations.py reuses the ensemble_mean/std
# already sitting in the alerts table from generate_alerts.py's last real run
# instead of re-deriving it.
#
# --no-sync for the same reason as every other tight-cadence wrapper here:
# cron gives no PATH (hence the absolute $UV path), and deploying is the only
# thing that should sync .venv -- see run_pipeline.sh's fuller note.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
UV="$HOME/.local/bin/uv"

# shellcheck source=scheduler/healthcheck.sh
. "$(dirname "${BASH_SOURCE[0]}")/healthcheck.sh"
set -a; [ -f .env ] && . ./.env; set +a
HC="${HEALTHCHECK_OBSERVATION_REFRESH_URL:-}"
hc_start "$HC"

"$UV" run --no-sync python scripts/refresh_same_day_observations.py
status=$?

if [ "$status" -ne 0 ]; then
  hc_fail "$HC"
else
  hc_success "$HC"
fi
exit "$status"
