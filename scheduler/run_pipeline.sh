#!/usr/bin/env bash
# Wrapper for cron: resolves the repo root relative to this script (so the
# crontab entry doesn't need to hardcode it twice), then runs the pipeline
# steps through uv, which manages the venv itself. No `set -e` — each step's
# exit code is checked explicitly so one failing doesn't prevent the others
# from running; they're independent concerns (generating new alerts,
# checking old ones for settlement, notifying about new signals). Output
# goes to stdout/stderr — redirect that in the crontab entry (see
# crontab.example) rather than duplicating logging logic here.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

uv run python scripts/generate_alerts.py
generate_status=$?

uv run python scripts/mark_settled_alerts.py
settle_status=$?

# Must run after generate_alerts.py, since it notifies about whatever that
# step just inserted. Always exits 0 itself (missing credentials/a failed
# send aren't pipeline failures — see the script), so it isn't part of the
# exit-code check below, but a hard crash here would still surface via cron.
uv run python scripts/send_notifications.py

if [ "$generate_status" -ne 0 ] || [ "$settle_status" -ne 0 ]; then
  exit 1
fi
