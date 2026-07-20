"""Has either trading-mechanics revisit trigger fired?

Reuses monitoring/trend.py rather than reimplementing the rule, so the audit,
scripts/calibration_trend.py and the /backtest panel can never disagree about
whether the pause still stands.
"""

from __future__ import annotations

from monitoring.trend import REVISIT_STREAK, build_trend, summarize

from .report import Finding, Status

CATEGORY_DECISIONS = "Decision log"


def check_revisit_trigger(cur) -> Finding:
    """The pause on trading-mechanics work is conditional, not indefinite.

    STRONG trigger: pooled skill vs market turns positive — the only condition
    that justifies resuming trading-mechanics work, and it still wants
    run_backtest.py to confirm TRADEABLE first.
    WEAK trigger: REVISIT_STREAK consecutive improving fits while still
    negative — means "go look at the forecasting core", not "resume trading".

    A FLAG here is good news, not a defect: it means something changed and is
    worth a human look.
    """
    cur.execute(
        "select id, started_at, detail from pipeline_runs "
        "where script = 'fit_calibration_params' and status in ('success', 'partial') "
        "order by started_at"
    )
    points = [p for p in build_trend(cur.fetchall()) if p.comparable]
    summary = summarize(points)

    evidence = [f"{len(points)} comparable recalibration run(s) recorded"]
    if summary.latest is None:
        evidence.append("no run yet carries a market benchmark (runs before 2026-07-20 predate it)")
        return Finding(
            CATEGORY_DECISIONS, "Trading-mechanics revisit trigger", Status.PASS,
            "Neither trigger has fired — no run carries a market benchmark yet, so the pause "
            "stands. The first weekly auto-recalibration records one.",
            evidence,
        )

    latest = summary.latest
    evidence.append(
        f"latest skill vs market {latest.skill_vs_market:+.4f} "
        f"(model {latest.brier_model} vs market {latest.brier_market}, n={latest.n_market_rows})"
    )
    if summary.skill_delta is not None:
        evidence.append(f"week-over-week {summary.skill_delta:+.4f}")
    evidence.append(f"consecutive improving runs: {summary.improving_streak} (weak bar: {REVISIT_STREAK})")

    if latest.skill_vs_market > 0:
        return Finding(
            CATEGORY_DECISIONS, "Trading-mechanics revisit trigger", Status.FLAG,
            "STRONG trigger fired — pooled skill vs market is POSITIVE, meaning the model beat "
            "the market on held-out rows. Re-run scripts/run_backtest.py and confirm the gate "
            "reports TRADEABLE before acting on it.",
            evidence,
        )
    if summary.improving_streak >= REVISIT_STREAK:
        return Finding(
            CATEGORY_DECISIONS, "Trading-mechanics revisit trigger", Status.FLAG,
            f"WEAK trigger fired — {summary.improving_streak} consecutive improving runs "
            f"(bar: {REVISIT_STREAK}), though skill is still negative. Worth investigating the "
            "forecasting core; NOT on its own a reason to resume trading-mechanics work.",
            evidence,
        )
    return Finding(
        CATEGORY_DECISIONS, "Trading-mechanics revisit trigger", Status.PASS,
        "Neither trigger has fired — skill is still negative and not on a sustained improving "
        "streak. Trading-mechanics work stays paused.",
        evidence,
    )
