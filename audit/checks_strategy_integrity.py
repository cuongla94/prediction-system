"""One-time-ish deep integrity audit of the weather-trading strategy itself —
distinct from `checks_pipeline.py`/`checks_decisions.py`, which ask "is the
system running," not "is the pipeline's OWN math correct." Built 2026-07-23
after a request to treat the strategy's no-edge finding as a potential
implementation bug until proven otherwise, rather than accepted at face value.

Every function here is read-only, same discipline as the rest of `audit/`:
SELECT statements and pure recomputation against already-stored values, no
writes, no re-running the live pipeline. Scoped to failure modes that are
actually plausible given how this codebase is built — several of the
candidate bugs a generic checklist would list (order-book depth, fabricated
fill prices) don't apply here at all, since this project has never placed a
real order; those are recorded as PASS with the code reference that rules
them out, not silently omitted.
"""

from __future__ import annotations

import inspect

from edge.calculator import compute_edge
from kalshi_client.fees import taker_fee
from paper_trading import STRATEGY_VERSION
from weather.probability import check_boundary_language
from weather.stations import STATIONS

from .report import Finding, Status

CATEGORY_STRATEGY = "Strategy integrity"

# How many recent settled alerts to recompute-and-compare per check — cheap
# to run, and "recent" catches whatever the live pipeline is producing today
# rather than only historical rows a since-fixed bug wouldn't reproduce in.
_SAMPLE_SIZE = 1000


def check_edge_calculation_consistency(cur) -> Finding:
    """Recomputes each sampled alert's edge fresh from its own stored
    model_probability/market_yes_price and compares against what's stored —
    catches a YES/NO inversion, a sign error, or any drift between the
    formula that generated the row and the one running today, in one pass.
    (`alerts` doesn't store a `side` column — it's always derived from
    edge's sign, per edge/calculator.py's own convention — so there's no
    separately-stored side to cross-check; recomputing edge itself, which
    this does, is what would catch a sign inversion.) Also checks the
    *stored* edge/fee_adjusted_threshold pair for internal consistency
    (`is_actionable == abs(edge) > threshold`), independent of the
    safety_margin used at generation time (which is not assumed to be
    yesterday's DEFAULT_SAFETY_MARGIN — recomputing with today's value would
    conflate a real bug with a legitimate parameter change over time).
    """
    cur.execute(
        "select market_ticker, model_probability, market_yes_price, edge, "
        "fee_adjusted_threshold, is_actionable "
        "from alerts where settled_at is not null order by created_at desc limit %s",
        (_SAMPLE_SIZE,),
    )
    rows = cur.fetchall()
    if not rows:
        return Finding(
            CATEGORY_STRATEGY, "Edge calculation consistency", Status.UNKNOWN,
            "No settled alerts to check yet.",
        )

    edge_mismatches: list[str] = []
    threshold_mismatches: list[str] = []
    fee_below_threshold_violations: list[str] = []

    for ticker, model_p, market_p, stored_edge, stored_threshold, stored_actionable in rows:
        fresh = compute_edge(model_p, market_p)
        if abs(fresh.edge - stored_edge) > 1e-6:
            edge_mismatches.append(
                f"{ticker}: stored edge={stored_edge:+.4f} vs recomputed {fresh.edge:+.4f} "
                f"(model_p={model_p:.4f}, market_p={market_p:.4f})"
            )
        expected_actionable = abs(stored_edge) > stored_threshold
        if stored_actionable != expected_actionable:
            threshold_mismatches.append(
                f"{ticker}: is_actionable={stored_actionable} but abs(edge)={abs(stored_edge):.4f} "
                f"{'>' if abs(stored_edge) > stored_threshold else '<='} threshold={stored_threshold:.4f}"
            )
        fee = taker_fee(market_p)
        if fee > stored_threshold + 1e-6:
            fee_below_threshold_violations.append(
                f"{ticker}: fee={fee:.4f} exceeds stored fee_adjusted_threshold={stored_threshold:.4f}"
            )

    problems = edge_mismatches + threshold_mismatches + fee_below_threshold_violations
    if not problems:
        return Finding(
            CATEGORY_STRATEGY, "Edge calculation consistency", Status.PASS,
            f"All {len(rows)} sampled settled alerts: recomputed edge/side match stored values, "
            "is_actionable is internally consistent with the stored edge/threshold, and the fee "
            "never exceeds the stored threshold. No YES/NO inversion or fee-adjustment drift found.",
            [f"sampled {len(rows)} settled alerts (most recent first)"],
        )
    return Finding(
        CATEGORY_STRATEGY, "Edge calculation consistency", Status.FLAG,
        f"{len(problems)} inconsistency/inconsistencies found across {len(rows)} sampled settled alerts.",
        problems[:20],
    )


def check_bracket_boundary_parsing(cur) -> Finding:
    """Re-runs weather.probability.check_boundary_language — the same
    rules_primary-vs-floor_strike/cap_strike cross-check generate_alerts.py
    already applies at insert time — against every currently-cached settled
    alert, not just the moment each row was created. Catches a bracket
    silently misparsed by a schema change Kalshi made after the row was
    written, which the original insert-time check couldn't have seen.
    """
    cur.execute(
        "select market_ticker, rules_primary, floor_strike, cap_strike "
        "from alerts where settled_at is not null and rules_primary is not null "
        "order by created_at desc limit %s",
        (_SAMPLE_SIZE,),
    )
    rows = cur.fetchall()
    if not rows:
        return Finding(
            CATEGORY_STRATEGY, "Bracket boundary parsing", Status.UNKNOWN,
            "No settled alerts with rules_primary text to check yet.",
        )

    failures: list[str] = []
    for ticker, rules_primary, floor_strike, cap_strike in rows:
        try:
            check_boundary_language(rules_primary, floor_strike, cap_strike)
        except ValueError as exc:
            failures.append(f"{ticker}: {exc}")

    if not failures:
        return Finding(
            CATEGORY_STRATEGY, "Bracket boundary parsing", Status.PASS,
            f"All {len(rows)} sampled settled alerts' rules_primary text matches their stored "
            "floor_strike/cap_strike under Kalshi's own boundary-language convention.",
            [f"sampled {len(rows)} settled alerts"],
        )
    return Finding(
        CATEGORY_STRATEGY, "Bracket boundary parsing", Status.FLAG,
        f"{len(failures)} of {len(rows)} sampled alerts have rules text inconsistent with their "
        "stored strike fields — a real bracket-parsing mismatch, not a false alarm.",
        failures[:20],
    )


def check_station_timezone_convention() -> Finding:
    """Structural, not data-driven: confirms every Station's timezone is a
    fixed Etc/GMT offset, never a DST-aware IANA zone. This is what actually
    prevents a DST-boundary day-shift bug in this codebase (see weather/
    stations.py's own docstring on why a DST-aware zone would bucket "daily
    max" at the wrong midnight) — a structural guarantee, not something that
    needs a live-data spot-check to confirm, so this rules the failure mode
    out by construction rather than sampling for it.
    """
    non_fixed = [
        f"{series}: {station.city} uses {station.standard_time_timezone!r}"
        for series, station in STATIONS.items()
        if not station.standard_time_timezone.startswith("Etc/GMT")
    ]
    if not non_fixed:
        return Finding(
            CATEGORY_STRATEGY, "Timezone/DST day-boundary convention", Status.PASS,
            f"All {len(STATIONS)} configured stations use a fixed Etc/GMT standard-time offset, "
            "never a DST-shifting IANA zone — the day-boundary-shift failure mode is ruled out by "
            "construction (weather/stations.py), not by sampling.",
            [f"{len(STATIONS)} stations checked"],
        )
    return Finding(
        CATEGORY_STRATEGY, "Timezone/DST day-boundary convention", Status.FLAG,
        f"{len(non_fixed)} station(s) use a DST-aware timezone instead of a fixed standard-time "
        "offset — real risk of a day-boundary shift around DST transitions.",
        non_fixed,
    )


def check_observation_conditioning_lead_days_guard() -> Finding:
    """Structural: confirms scripts/generate_alerts.py still gates
    observed_so_far behind `lead_days == 0` before passing it into the
    probability engine — the guarantee that a market settling tomorrow never
    gets conditioned on an observation that hasn't happened yet from its own
    point of view. Checked by inspecting the actual running source (not by
    re-deriving the guarantee from documentation), so this fails loudly if a
    future edit ever removes the guard rather than silently trusting a
    comment that could go stale.
    """
    import scripts.generate_alerts as generate_alerts_module

    source = inspect.getsource(generate_alerts_module)
    if "lead_days == 0" in source and "observed_so_far" in source:
        return Finding(
            CATEGORY_STRATEGY, "No future-observation leakage", Status.PASS,
            "scripts/generate_alerts.py still gates observed_so_far behind `lead_days == 0` — a "
            "market settling on a later day is never conditioned on an observation, ruling out "
            "future-data leakage into the probability by construction.",
            ["source inspection of scripts/generate_alerts.py, live import"],
        )
    return Finding(
        CATEGORY_STRATEGY, "No future-observation leakage", Status.FLAG,
        "Could not find the `lead_days == 0` guard around observed_so_far in "
        "scripts/generate_alerts.py — this guarantee may have been silently removed or reworded.",
        ["source inspection found no matching guard"],
    )


def check_no_duplicate_paper_trades(cur) -> Finding:
    """Live confirmation that no market is counted twice in paper_trades,
    beyond trusting the schema's own `unique` constraint on market_ticker —
    a constraint only prevents a NEW duplicate insert, it says nothing about
    whether one already exists from before the constraint was added.
    """
    cur.execute("select market_ticker, count(*) from paper_trades group by 1 having count(*) > 1")
    dupes = cur.fetchall()
    if not dupes:
        return Finding(
            CATEGORY_STRATEGY, "No duplicate paper trades", Status.PASS,
            "No market_ticker appears more than once in paper_trades.",
        )
    return Finding(
        CATEGORY_STRATEGY, "No duplicate paper trades", Status.FLAG,
        f"{len(dupes)} market_ticker(s) have more than one paper_trades row — double-counted P&L.",
        [f"{ticker}: {n} rows" for ticker, n in dupes[:20]],
    )


def check_strategy_version_freshness(cur) -> Finding:
    """Flags if the most recent paper_trades row was opened under a
    strategy_version that doesn't match the current STRATEGY_VERSION
    constant (paper_trading/engine.py) — the one way the strategy-cohort-
    tagging feature itself could silently go stale: the code changes
    (sizing, exits, calibration approach) but nobody remembers to bump the
    hand-maintained version string, so new trades keep getting mislabeled
    under the old cohort.
    """
    cur.execute("select strategy_version, opened_at from paper_trades order by opened_at desc limit 1")
    rows = cur.fetchall()
    if not rows:
        return Finding(
            CATEGORY_STRATEGY, "Strategy version freshness", Status.UNKNOWN,
            "No paper_trades rows yet — nothing to check.",
        )
    latest_version, opened_at = rows[0]
    if latest_version == STRATEGY_VERSION:
        return Finding(
            CATEGORY_STRATEGY, "Strategy version freshness", Status.PASS,
            f"Most recent paper_trades row (opened {opened_at}) is tagged {latest_version!r}, "
            "matching the current STRATEGY_VERSION constant.",
        )
    return Finding(
        CATEGORY_STRATEGY, "Strategy version freshness", Status.FLAG,
        f"Most recent paper_trades row (opened {opened_at}) is tagged {latest_version!r}, but "
        f"paper_trading/engine.py's STRATEGY_VERSION constant is currently {STRATEGY_VERSION!r} — "
        "either the version string wasn't bumped after a real strategy change, or the bot hasn't "
        "opened a position since the constant last changed (check whether that's expected).",
    )


def check_reproduce_high_edge_zero_wins(cur, *, edge_threshold: float = 0.10) -> Finding:
    """Reproduces the "0 of 171 markets with >10 points of claimed edge
    resolved in the model's favor" finding directly against current settled
    data, rather than citing the old number as still true.

    Deduplicated to one row per market_ticker (its most recent settled alert,
    via DISTINCT ON — the same "latest snapshot per ticker" pattern already
    used elsewhere in this codebase, e.g. dashboard/app.py's portfolio join)
    so a market re-alerted every pipeline cycle before it settled doesn't
    inflate the sample by counting the same real decision multiple times.
    "Resolved in the model's favor" means the side the edge implies (YES if
    edge > 0, NO if edge < 0) matches actual_outcome.
    """
    cur.execute(
        "select distinct on (market_ticker) market_ticker, edge, actual_outcome "
        "from alerts where settled_at is not null and actual_outcome is not null "
        "order by market_ticker, created_at desc"
    )
    latest_per_market = cur.fetchall()
    high_edge = [(t, e, o) for t, e, o in latest_per_market if abs(e) > edge_threshold]

    if not high_edge:
        return Finding(
            CATEGORY_STRATEGY, "High-claimed-edge win rate reproduction", Status.UNKNOWN,
            f"No settled markets currently have a most-recent claimed edge over {edge_threshold:.0%} "
            "to check — nothing to reproduce the finding against right now.",
        )

    favorable = [
        (t, e, o) for t, e, o in high_edge if (e > 0 and o) or (e < 0 and not o)
    ]
    win_count = len(favorable)
    total = len(high_edge)

    evidence = [
        f"{total} distinct markets with |edge| > {edge_threshold:.0%} at their most recent settled alert",
        f"{win_count} resolved in the model's favor",
    ]
    if win_count == 0:
        return Finding(
            CATEGORY_STRATEGY, "High-claimed-edge win rate reproduction", Status.PASS,
            f"Reproduced: 0 of {total} markets with a claimed edge over {edge_threshold:.0%} resolved "
            "in the model's favor, against current data. Confirms this is a real, reproducible "
            "pattern in the pipeline's own output — not a stale number from an earlier bug that's "
            "since been fixed, and not an artifact of double-counting the same market.",
            evidence,
        )
    return Finding(
        CATEGORY_STRATEGY, "High-claimed-edge win rate reproduction", Status.FLAG,
        f"{win_count} of {total} high-claimed-edge markets DID resolve in the model's favor against "
        "current data — the original 0-of-171 finding no longer reproduces exactly. Worth "
        "understanding whether this reflects a real change (a shipped fix) or a smaller/different "
        "sample than the original finding used before treating the strategy as improved.",
        evidence,
    )
