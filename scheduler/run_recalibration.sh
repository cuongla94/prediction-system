#!/usr/bin/env bash
# Weekly re-fit of the per-city bias/std calibration.
#
# Added 2026-07-20. The fitted parameters had been a one-off snapshot from
# 2026-07-19 that would drift further out of date every week as new days
# settled — this keeps them moving as real data accrues, which is the whole
# point of the correction being empirical rather than assumed.
#
# WEEKLY, not daily, and that's a deliberate ceiling rather than laziness. The
# thing being fit is a slow-moving seasonal bias measured over hundreds of
# days; re-fitting it daily would change the live model constantly in response
# to single-day noise while the underlying estimate barely moves. It is also
# the most expensive job this project runs — it walks ~2 years of settled
# Kalshi markets across 40 series. Redis (backtest/cache.py, 30-day TTL) makes
# repeat runs cheap, but a cold cache is a genuinely long job on a 1-vCPU box.
#
# Separate from run_pipeline.sh / run_settlement_cycle.sh because it shares
# nothing with them: it produces no alerts, settles nothing, and its output is
# read at probability-computation time rather than by the next pipeline step.
# Chaining it onto either would just make those slower and couple two
# unrelated failure modes.
#
# Uses $HOME/.local/bin/uv and --no-sync for exactly the reasons documented at
# length in run_pipeline.sh — cron has no ~/.local/bin on PATH, and deploying
# is the only thing that should sync the venv.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
UV="$HOME/.local/bin/uv"

"$UV" run --no-sync python scripts/fit_calibration_params.py
exit $?
