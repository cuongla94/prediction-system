#!/usr/bin/env bash
# Weekly read-only system audit. Reports; fixes nothing.
#
# Runs from ROOT's crontab, not the kalshi user's, and that is deliberate —
# it is the only scheduled job here that does. Several checks are simply
# invisible to kalshi after the 2026-07-20 hardening:
#   - /var/log/nginx/access.log  (basic-auth access patterns) — not readable
#   - /etc/nginx/.htpasswd       (permission drift)           — not readable
#   - .git                       (deploy state)               — root:root 0700
# Running as kalshi would silently degrade those to UNKNOWN, which is honest but
# useless. Root is acceptable precisely because the audit only reads: no writes
# to the database, no mutating API calls, and the single file it touches is its
# own report.
#
# Kept out of run_pipeline.sh / run_settlement_cycle.sh on purpose: this shares
# nothing with them, is slower (it queries Kalshi and OSV.dev over the network),
# and a monitoring job must not be able to delay or fail the thing it monitors.
#
# Reports land in logs/audit/ as audit-YYYY-MM-DD.md plus a stable latest.md.
# That directory is gitignored — the reports name internal paths, IPs and access
# patterns, which do not belong in a public repo.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

# shellcheck source=scheduler/healthcheck.sh
. "$(dirname "${BASH_SOURCE[0]}")/healthcheck.sh"
set -a; [ -f .env ] && . ./.env; set +a
HC="${HEALTHCHECK_AUDIT_URL:-}"
hc_start "$HC"

# Run as the app user so the venv and DB credentials resolve exactly as they do
# everywhere else, but with root's readable-everything view preserved for the
# file/log checks via the pre-collected environment. uv lives under kalshi's
# home; cron gives us no PATH, hence the absolute path (see run_pipeline.sh).
UV="/home/kalshi/.local/bin/uv"
"$UV" run --no-sync python scripts/run_audit.py
status=$?

# A flagged finding is NOT a failure: run_audit.py exits 0 even when it flags,
# so a non-zero here means the audit itself could not run. Keeping those
# distinct is the whole point — otherwise "something is wrong" and "I have no
# idea whether anything is wrong" would page identically.
if [ "$status" -ne 0 ]; then
  hc_fail "$HC"
else
  hc_success "$HC"
fi
exit "$status"
