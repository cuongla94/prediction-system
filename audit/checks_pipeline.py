"""Pipeline health and data integrity checks.

Every function here is read-only: SELECT statements and GET requests only.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from typing import Any

from .report import Finding, Status

CATEGORY_PIPELINE = "Pipeline health"
CATEGORY_DATA = "Data integrity"

# Matches dashboard/app.py's _STUCK_AFTER. Longer than any real run, including
# the weekly recalibration walking ~2 years of markets on one vCPU.
STUCK_AFTER = timedelta(hours=3)

# Expected cadence per scheduled script, mirroring scheduler/crontab.example.
# The value is the interval; a gap materially longer than this means the cron
# is not firing as intended.
EXPECTED_CADENCE = {
    "generate_alerts": timedelta(hours=6),
    "mark_settled_alerts": timedelta(minutes=15),
    "run_paper_trading": timedelta(minutes=15),
    "fit_calibration_params": timedelta(days=7),
}
# How far past the cadence before flagging. 3x absorbs a slow run or a single
# skipped tick without crying wolf; anything beyond it is a real gap.
CADENCE_TOLERANCE = 3


def check_stuck_runs(cur) -> Finding:
    """Runs left in 'running' long past any plausible duration.

    Distinct from staleness: a later run succeeding hides an earlier one that
    never finished, so a latest-row-per-script view cannot see this. Exactly how
    a two-day-old zombie (generate_alerts id=20) hid while /status showed green.
    """
    cur.execute(
        "select id, script, started_at from pipeline_runs "
        "where status = 'running' and started_at < %s order by started_at",
        (datetime.now(UTC) - STUCK_AFTER,),
    )
    rows = cur.fetchall()
    if not rows:
        return Finding(
            CATEGORY_PIPELINE, "Stuck runs", Status.PASS,
            f"No run has been in 'running' longer than {STUCK_AFTER}.",
        )
    return Finding(
        CATEGORY_PIPELINE, "Stuck runs", Status.FLAG,
        f"{len(rows)} run(s) stuck in 'running' — started but never recorded an outcome, "
        "so the process died without its exit handler running.",
        [f"id={r[0]} `{r[1]}` started {r[2]:%Y-%m-%d %H:%M} UTC "
         f"({(datetime.now(UTC) - r[2]).days}d ago)" for r in rows],
    )


def check_failed_runs(cur, *, window_days: int = 7) -> Finding:
    """Failed or partial runs in the recent window.

    'partial' is included deliberately: run_backtest reports NO EDGE that way,
    and generate_alerts uses it when some cities fail. Those are real signals,
    not noise — a partial that nobody looks at is how a degraded pipeline
    becomes normal.
    """
    since = datetime.now(UTC) - timedelta(days=window_days)
    cur.execute(
        "select script, status, count(*), max(started_at) from pipeline_runs "
        "where status in ('failed', 'partial') and started_at >= %s "
        "group by script, status order by count(*) desc",
        (since,),
    )
    rows = cur.fetchall()
    if not rows:
        return Finding(
            CATEGORY_PIPELINE, "Failed/partial runs", Status.PASS,
            f"No failed or partial runs in the last {window_days} days.",
        )
    return Finding(
        CATEGORY_PIPELINE, "Failed/partial runs", Status.FLAG,
        f"{sum(r[2] for r in rows)} failed/partial run(s) in the last {window_days} days.",
        [f"`{r[0]}` {r[1]} x{r[2]}, most recent {r[3]:%Y-%m-%d %H:%M} UTC" for r in rows],
    )


def check_cron_cadence(cur) -> Finding:
    """Whether each scheduled script is actually firing on its intended cadence.

    Measured from the newest run rather than from cron's own logs, because what
    matters is whether work is landing, not whether cron believes it fired.
    """
    now = datetime.now(UTC)
    evidence: list[str] = []
    late: list[str] = []
    for script, cadence in EXPECTED_CADENCE.items():
        cur.execute(
            "select max(started_at) from pipeline_runs where script = %s", (script,)
        )
        (last,) = cur.fetchone()
        if last is None:
            late.append(f"`{script}` has never run (expected every {cadence})")
            continue
        gap = now - last
        limit = cadence * CADENCE_TOLERANCE
        line = (f"`{script}`: last {last:%Y-%m-%d %H:%M} UTC, {gap} ago "
                f"(expected every {cadence})")
        if gap > limit:
            late.append(line)
        else:
            evidence.append(line)

    if late:
        return Finding(
            CATEGORY_PIPELINE, "Cron cadence", Status.FLAG,
            f"{len(late)} scheduled script(s) have not run within {CADENCE_TOLERANCE}x their cadence.",
            late + evidence,
        )
    return Finding(
        CATEGORY_PIPELINE, "Cron cadence", Status.PASS,
        "Every scheduled script has run within its expected cadence.", evidence,
    )


def check_healthcheck_delivery(env: dict[str, str]) -> Finding:
    """Whether the dead-man's-switch is actually configured.

    Deliberately UNKNOWN rather than PASS when unset. The pings no-op silently
    without a URL, which is the correct behaviour for a fresh checkout but means
    an unconfigured production box has NO out-of-band alerting while looking
    exactly like a healthy one. That gap is the whole reason this check exists.
    """
    expected = {
        "HEALTHCHECK_PIPELINE_URL": "run_pipeline.sh (~6h)",
        "HEALTHCHECK_SETTLEMENT_URL": "run_settlement_cycle.sh (15m)",
        "HEALTHCHECK_RECALIBRATION_URL": "run_recalibration.sh (weekly)",
    }
    missing = [f"`{k}` unset — {desc}" for k, desc in expected.items() if not env.get(k)]
    if not missing:
        return Finding(
            CATEGORY_PIPELINE, "Healthcheck delivery", Status.PASS,
            "All three dead-man's-switch URLs are configured.",
            [f"`{k}` set" for k in expected],
        )
    return Finding(
        CATEGORY_PIPELINE, "Healthcheck delivery", Status.UNKNOWN,
        f"{len(missing)} of {len(expected)} healthcheck URLs are unset — those schedules have "
        "no out-of-band alerting, and their pings are silently skipped.",
        missing,
    )


def check_settled_trades_against_kalshi(cur, client, *, sample_size: int = 10) -> Finding:
    """Spot-check settled paper trades against Kalshi's own current result.

    READ-ONLY against paper_trades — this reads outcomes to verify bookkeeping;
    it does not touch trading logic, sizing, or exits.

    Random sample rather than the most recent N: recent trades cluster in one
    settlement batch, so they would share any batch-wide bug and a systematic
    error could pass repeatedly.
    """
    cur.execute(
        "select t.market_ticker, t.side, t.close_reason, a.series_ticker "
        "from paper_trades t "
        "join lateral (select series_ticker from alerts a where a.market_ticker = t.market_ticker "
        "  order by a.created_at desc limit 1) a on true "
        "where t.close_reason in ('settled_win', 'settled_loss')"
    )
    rows = cur.fetchall()
    if not rows:
        return Finding(
            CATEGORY_DATA, "Settled trades vs Kalshi", Status.PASS,
            "No settled trades to check yet.",
        )

    sample = random.sample(rows, min(sample_size, len(rows)))
    by_series: dict[str, list] = {}
    for row in sample:
        by_series.setdefault(row[3], []).append(row)

    mismatches: list[str] = []
    checked = 0
    unresolved = 0
    for series, items in by_series.items():
        wanted = {r[0] for r in items}
        found: dict[str, str] = {}
        cursor = None
        for _ in range(8):
            markets, cursor = client.get_markets(
                series_ticker=series, status="settled", limit=200, cursor=cursor
            )
            for market in markets:
                if market.ticker in wanted:
                    found[market.ticker] = market.raw.get("result")
            if not cursor or set(found) >= wanted:
                break
        for ticker, side, close_reason, _ in items:
            result = found.get(ticker)
            if result not in ("yes", "no"):
                unresolved += 1
                continue
            checked += 1
            kalshi_won = (result == "yes") if side == "YES" else (result == "no")
            our_won = close_reason == "settled_win"
            if kalshi_won != our_won:
                mismatches.append(
                    f"`{ticker}` side={side}: we recorded {close_reason}, "
                    f"Kalshi says result={result}"
                )

    evidence = [
        f"Sampled {len(sample)} of {len(rows)} settled trades at random",
        f"{checked} verified against Kalshi, {unresolved} not in Kalshi's recent settled window",
    ]
    if mismatches:
        return Finding(
            CATEGORY_DATA, "Settled trades vs Kalshi", Status.FLAG,
            f"{len(mismatches)} of {checked} sampled trades disagree with Kalshi's own result.",
            mismatches + evidence,
        )
    if checked == 0:
        return Finding(
            CATEGORY_DATA, "Settled trades vs Kalshi", Status.UNKNOWN,
            "Could not verify any sampled trade — all had aged out of Kalshi's recent settled window.",
            evidence,
        )
    return Finding(
        CATEGORY_DATA, "Settled trades vs Kalshi", Status.PASS,
        f"All {checked} verified trades match Kalshi's own result.", evidence,
    )


# Supabase free tier. The number that turns table growth into a deadline.
FREE_TIER_BYTES = 500 * 1024 * 1024
# Flag once the projected runway drops below this.
RUNWAY_WARN = timedelta(days=90)


def check_alerts_growth(cur) -> Finding:
    """Alerts-table growth against the Supabase free-tier ceiling.

    Projects from observed daily growth rather than assuming a rate, so it
    tracks reality as the pipeline's cadence or city count changes.
    """
    cur.execute("select pg_total_relation_size('alerts'), pg_database_size(current_database())")
    alerts_bytes, db_bytes = cur.fetchone()
    cur.execute("select count(*) from alerts")
    (row_count,) = cur.fetchone()

    # Growth from complete days only — today is partial and would understate.
    cur.execute(
        "select created_at::date, count(*) from alerts "
        "where created_at::date < current_date group by 1 order by 1"
    )
    daily = cur.fetchall()
    evidence = [
        f"alerts: {row_count:,} rows, {alerts_bytes / 1024 / 1024:.1f} MB",
        f"whole database: {db_bytes / 1024 / 1024:.1f} MB of "
        f"{FREE_TIER_BYTES / 1024 / 1024:.0f} MB free tier",
    ]
    # Drop the earliest day: it is partial by construction (the table only
    # started collecting partway through it) and materially understates the
    # rate. On this data it was 252 rows against a ~1,800/day steady state,
    # which alone stretched the projected runway from ~165 days to ~301 — an
    # optimistic projection is the worst kind of error for a capacity warning.
    usable = daily[1:] if len(daily) > 1 else daily
    if not usable:
        return Finding(
            CATEGORY_DATA, "Alerts growth vs retention", Status.UNKNOWN,
            "Not enough complete days of history to project growth yet.", evidence,
        )

    recent = usable[-7:]
    rows_per_day = sum(n for _, n in recent) / len(recent)
    if len(recent) < 3:
        evidence.append(
            f"NOTE: based on only {len(recent)} complete day(s) — treat as provisional"
        )
    bytes_per_row = alerts_bytes / row_count if row_count else 0
    bytes_per_day = rows_per_day * bytes_per_row
    evidence.append(
        f"growth: {rows_per_day:,.0f} rows/day over the last {len(recent)} complete days "
        f"(~{bytes_per_day / 1024 / 1024:.1f} MB/day, {bytes_per_row:,.0f} bytes/row)"
    )

    if bytes_per_day <= 0:
        return Finding(
            CATEGORY_DATA, "Alerts growth vs retention", Status.UNKNOWN,
            "Could not compute a growth rate.", evidence,
        )

    days_left = (FREE_TIER_BYTES - db_bytes) / bytes_per_day
    runway = timedelta(days=max(days_left, 0))
    evidence.append(f"projected runway to the 500 MB ceiling: ~{runway.days} days")

    if runway < RUNWAY_WARN:
        return Finding(
            CATEGORY_DATA, "Alerts growth vs retention", Status.FLAG,
            f"~{runway.days} days of free-tier headroom left — under the {RUNWAY_WARN.days}-day "
            "threshold. Retention work is proposed but not built (see kalshi-open-followups).",
            evidence,
        )
    return Finding(
        CATEGORY_DATA, "Alerts growth vs retention", Status.PASS,
        f"~{runway.days} days of free-tier headroom at the current growth rate.", evidence,
    )


def check_schema_drift(cur, schema_sql: str) -> Finding:
    """Columns the live database has that db/schema.sql does not mention, or vice versa.

    Deliberately a coarse name-level comparison, not a full DDL diff: the real
    failure mode here is a column added straight to Supabase and never written
    back to schema.sql, so a fresh deploy silently lacks it.
    """
    cur.execute(
        "select table_name, column_name from information_schema.columns "
        "where table_schema = 'public' order by table_name, column_name"
    )
    live: dict[str, set[str]] = {}
    for table, column in cur.fetchall():
        live.setdefault(table, set()).add(column)

    lowered = schema_sql.lower()
    missing: list[str] = []
    for table, columns in sorted(live.items()):
        if table.lower() not in lowered:
            missing.append(f"table `{table}` exists live but is not mentioned in db/schema.sql")
            continue
        for column in sorted(columns):
            if column.lower() not in lowered:
                missing.append(f"`{table}.{column}` exists live but is not in db/schema.sql")

    evidence = [f"{len(live)} tables, {sum(len(c) for c in live.values())} columns compared"]
    if missing:
        return Finding(
            CATEGORY_DATA, "Schema drift", Status.FLAG,
            f"{len(missing)} live schema object(s) absent from db/schema.sql — a fresh deploy "
            "would not reproduce this database.",
            missing + evidence,
        )
    return Finding(
        CATEGORY_DATA, "Schema drift", Status.PASS,
        "Every live table and column appears in db/schema.sql.", evidence,
    )


def summarize_env(env: dict[str, Any]) -> dict[str, str]:
    return {k: str(v) for k, v in env.items() if v is not None}
