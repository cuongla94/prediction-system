#!/usr/bin/env bash
# Dead-man's-switch pings for healthchecks.io. Sourced by the scheduler
# wrappers; not executable on its own.
#
# Added 2026-07-20. Until then NOTHING notified out of band: send_notifications.py
# is about actionable trading brackets rather than pipeline health, it reads the
# alerts table and never pipeline_runs.status, and it had never run at all
# (PUSHOVER_* unset, so it early-returns before track_run even records a row).
# Failures were visible only by remembering to open /status.
#
# Why an external dead-man's-switch rather than pushing a notification from
# here: a process running ON this droplet cannot report that the droplet stopped
# running. If the box is powered off, the disk fills, cron itself dies, or the
# network drops, any self-hosted alerter dies with it and the silence is
# indistinguishable from health. Inverting it — an outside service that expects
# a ping on a schedule and emails when one fails to arrive — is the only shape
# that survives its own subject failing. It also needs no new secret beyond a
# URL, whereas Pushover has sat unconfigured since 2026-07-18.
#
# Ping protocol (healthchecks.io):
#   <url>/start  when the run begins   -> lets it measure duration and spot hangs
#   <url>        on success
#   <url>/fail   on failure            -> alerts immediately, no waiting for a timeout
# Missing pings entirely trip the configured grace period, which is what catches
# the box being dead.
#
# Entirely optional: with no URL set every function is a no-op, so a fresh
# checkout or a dev machine behaves exactly as before. Same pattern as
# NOAA_CDO_TOKEN and PUSHOVER_*.
#
# Setup (one manual step, needs a human — the free tier requires signup):
#   1. Create a check at https://healthchecks.io for each schedule below.
#   2. Put its ping URL in the droplet's .env:
#        HEALTHCHECK_PIPELINE_URL=https://hc-ping.com/<uuid>       period 6h,  grace 2h
#        HEALTHCHECK_SETTLEMENT_URL=https://hc-ping.com/<uuid>     period 15m, grace 15m
#        HEALTHCHECK_RECALIBRATION_URL=https://hc-ping.com/<uuid>  period 7d,  grace 1d
#   3. Point the check's alert at email/SMS in healthchecks.io's own settings.
# .env is gitignored, so these URLs never reach the public repo.

# --no-progress-meter rather than -s: still shows real errors on stderr into the
# cron log, so a permanently-misconfigured URL is discoverable rather than
# silently swallowed. --retry survives a transient blip without failing the run.
_hc_ping() {
  local url="$1" suffix="${2:-}"
  [ -n "$url" ] || return 0
  curl --no-progress-meter --max-time 10 --retry 3 --retry-delay 2 \
       -o /dev/null "${url}${suffix}" || true
}

# Never let a monitoring failure break the thing it monitors: every call is
# `|| true` above, so an unreachable healthchecks.io cannot fail the pipeline.
hc_start() { _hc_ping "${1:-}" "/start"; }
hc_success() { _hc_ping "${1:-}" ""; }
hc_fail() { _hc_ping "${1:-}" "/fail"; }
