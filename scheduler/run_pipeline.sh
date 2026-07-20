#!/usr/bin/env bash
# Wrapper for cron: resolves the repo root relative to this script (so the
# crontab entry doesn't need to hardcode it twice), then runs the pipeline
# steps through uv, which manages the venv itself. No `set -e` — each step's
# exit code is checked explicitly so one failing doesn't prevent the others
# from running; they're independent concerns (generating new alerts,
# checking old ones for settlement, notifying about new signals). Output
# goes to stdout/stderr — redirect that in the crontab entry (see
# crontab.example) rather than duplicating logging logic here.
#
# Uses $HOME/.local/bin/uv, not a bare `uv`, on purpose — confirmed live
# 2026-07-18 on the actual droplet that cron (and any other non-login,
# non-interactive invocation, e.g. `sudo -u kalshi ...`) does NOT have
# ~/.local/bin on PATH even though $HOME itself resolves correctly. A bare
# `uv` here would silently fail every single cron run with "command not
# found" and nobody would notice until checking the logs by hand.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
UV="$HOME/.local/bin/uv"

"$UV" run python scripts/generate_alerts.py
generate_status=$?

"$UV" run python scripts/mark_settled_alerts.py
settle_status=$?

# Must run after mark_settled_alerts.py (needs settled_at/actual_outcome
# written back to close out matured paper positions) and generate_alerts.py
# (needs this cycle's fresh prices for exit/entry decisions). Simulated only —
# never touches Kalshi's real order-placement API — so a failure here isn't a
# pipeline failure either, same reasoning as send_notifications.py below.
"$UV" run python scripts/run_paper_trading.py

# Must run after generate_alerts.py, since it notifies about whatever that
# step just inserted. Always exits 0 itself (missing credentials/a failed
# send aren't pipeline failures — see the script), so it isn't part of the
# exit-code check below, but a hard crash here would still surface via cron.
"$UV" run python scripts/send_notifications.py

if [ "$generate_status" -ne 0 ] || [ "$settle_status" -ne 0 ]; then
  exit 1
fi
