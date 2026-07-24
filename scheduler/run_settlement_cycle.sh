#!/usr/bin/env bash
# Independent, tighter-cadence companion to run_pipeline.sh: just checks
# pending markets for settlement and runs one paper-trading cycle, without
# the (comparatively expensive, and not something that changes on a 15-30min
# timescale) forecast/price refresh in generate_alerts.py.
#
# Added 2026-07-20 after a real gap: with only the 4x/day main pipeline
# checking settlement, a market that resolved shortly after one check had to
# wait up to ~6h for the next one before paper_trades moved it from "open" to
# "closed" and its cash came back available — see
# kalshi-implementation-progress memory for the live case that surfaced this.
# mark_settled_alerts.py's own settlement check is cheap (one paginated
# get_markets() call per series, ~40 calls total as of the same date, not one
# call per pending ticker — see that script's own docstring for why this
# matters at a tight cadence), so there's no real cost to running it often.
#
# run_paper_trading.py rides along in the same cycle for the same reason it's
# chained after mark_settled_alerts.py in run_pipeline.sh: it needs
# settled_at/actual_outcome already written back to close out matured
# positions and free their cash. It does NOT get fresher entry/exit prices
# out of running more often than generate_alerts.py — those still only
# update on that slower cadence, since that's what actually refreshes
# alerts.market_yes_price. Running it here mainly serves the settlement path.
#
# Neither script here writes anything generate_alerts.py or send_notifications.py
# depend on, so those stay on their own schedule in run_pipeline.sh — no
# reason to run the (comparatively slow, live-ensemble-forecast-fetching)
# generate_alerts.py every 15 minutes just to get faster settlement checks.
#
# --no-sync on both `uv run` calls below matters more here than anywhere else
# in this project, because of this script's */15 cadence: without it `uv run`
# re-locks and re-syncs first, so a deploy landing mid-cycle would have two
# processes mutating .venv at once. See run_pipeline.sh's fuller note.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
UV="$HOME/.local/bin/uv"

# shellcheck source=scheduler/healthcheck.sh
. "$(dirname "${BASH_SOURCE[0]}")/healthcheck.sh"
set -a; [ -f .env ] && . ./.env; set +a
HC="${HEALTHCHECK_SETTLEMENT_URL:-}"
hc_start "$HC"

"$UV" run --no-sync python scripts/mark_settled_alerts.py
settle_status=$?

"$UV" run --no-sync python scripts/run_paper_trading.py

# Same recurring worker path as run_pipeline.sh. When live is OFF this remains
# read/reconciliation-only; no production write method is called.
"$UV" run --no-sync python scripts/run_live_execution.py

if [ "$settle_status" -ne 0 ]; then
  hc_fail "$HC"
else
  hc_success "$HC"
fi
exit "$settle_status"
