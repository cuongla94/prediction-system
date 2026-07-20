"""Runs one cycle of the paper-trading bot: settle whatever resolved, exit
whatever lost its edge, then open new positions for this cycle's actionable
alerts. Never touches Kalshi's real order-placement API — this only reads
public market data (via the alerts table, already fetched live by
generate_alerts.py this same cycle) and writes to paper_trades.

Must run after generate_alerts.py (needs this cycle's fresh prices) and
mark_settled_alerts.py (needs settled_at/actual_outcome written back) — see
scheduler/run_pipeline.sh.

Usage: uv run scripts/run_paper_trading.py
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime

import psycopg
from dotenv import load_dotenv

from monitoring import track_run
from paper_trading import (
    STARTING_BANKROLL_USD,
    ExitDecision,
    OpenAlert,
    OpenPosition,
    SettledPosition,
    deployable_cash,
    event_date_key,
    max_day_exposure_fraction_setting,
    plan_exits,
    plan_new_positions,
    plan_settlements,
)

_ALERT_COLUMNS = (
    "market_ticker, event_ticker, series_ticker, city, bracket_label, side, "
    "model_probability, market_yes_price, edge, is_actionable"
)


def _fetch_open_alerts(cur: psycopg.Cursor) -> list[OpenAlert]:
    # Latest row per still-open market_ticker — same "one snapshot per
    # market" pattern dashboard/db.py uses, and "side"/"is_actionable" aren't
    # stored columns, so they're recomputed here from edge the same way
    # dashboard/alert.py's Alert.side property does.
    cur.execute(
        "select distinct on (market_ticker) market_ticker, event_ticker, series_ticker, "
        "city, bracket_label, model_probability, market_yes_price, edge, is_actionable, close_time "
        "from alerts where settled_at is null "
        "order by market_ticker, created_at desc"
    )
    alerts = []
    for row in cur.fetchall():
        market_ticker, event_ticker, series_ticker, city, bracket_label, model_probability, market_yes_price, edge, is_actionable, close_time = row
        side = "YES" if edge > 0 else "NO" if edge < 0 else "FLAT"
        alerts.append(
            OpenAlert(
                market_ticker=market_ticker,
                event_ticker=event_ticker,
                series_ticker=series_ticker,
                city=city,
                bracket_label=bracket_label,
                side=side,
                model_probability=model_probability,
                market_yes_price=market_yes_price,
                close_time=close_time,
                edge=edge,
                is_actionable=is_actionable,
            )
        )
    return alerts


def _fetch_open_positions(cur: psycopg.Cursor) -> list[OpenPosition]:
    cur.execute("select id, market_ticker, side, contracts, cost_basis from paper_trades where status = 'open'")
    return [
        OpenPosition(id=id_, market_ticker=ticker, side=side, contracts=contracts, cost_basis=cost_basis)
        for id_, ticker, side, contracts, cost_basis in cur.fetchall()
    ]


def _existing_exposure_by_date(cur: psycopg.Cursor) -> dict[str, float]:
    """Cost basis of still-open positions, grouped by their event's target date
    (event_date_key) — seeds plan_new_positions' cross-city day cap so it
    counts positions opened in earlier cycles too, making the cap a standing
    limit on correlated same-day exposure rather than one that resets every
    run. Called after settlements/exits have closed this cycle's resolved
    positions, so it reflects only what's genuinely still open."""
    cur.execute("select event_ticker, cost_basis from paper_trades where status = 'open'")
    exposure: dict[str, float] = {}
    for event_ticker, cost_basis in cur.fetchall():
        key = event_date_key(event_ticker)
        exposure[key] = round(exposure.get(key, 0.0) + float(cost_basis), 4)
    return exposure


def _fetch_outcomes(cur: psycopg.Cursor, tickers: list[str]) -> dict[str, bool]:
    if not tickers:
        return {}
    cur.execute(
        "select distinct on (market_ticker) market_ticker, actual_outcome from alerts "
        "where market_ticker = any(%s) and settled_at is not null "
        "order by market_ticker, created_at desc",
        (tickers,),
    )
    return {ticker: outcome for ticker, outcome in cur.fetchall() if outcome is not None}


def _apply_closes(conn: psycopg.Connection, decisions: list[ExitDecision]) -> None:
    if not decisions:
        return
    with conn.cursor() as cur:
        for d in decisions:
            cur.execute(
                "update paper_trades set status = 'closed', closed_at = %(closed_at)s, "
                "close_reason = %(close_reason)s, exit_price = %(exit_price)s, "
                "exit_fee = %(exit_fee)s, payout = %(payout)s, realized_pnl = %(realized_pnl)s "
                "where id = %(id)s",
                dict(
                    closed_at=datetime.now(UTC),
                    close_reason=d.close_reason,
                    exit_price=d.exit_price,
                    exit_fee=d.exit_fee,
                    payout=d.payout,
                    realized_pnl=d.realized_pnl,
                    id=d.id,
                ),
            )
    conn.commit()


def _realized_pnl_since_reset(cur: psycopg.Cursor) -> float:
    # A bankroll reset (dashboard/app.py::_latest_bankroll_reset) only
    # changes which realized P&L counts here — every paper_trades row stays
    # untouched. Without this filter, a reset would only ever be cosmetic on
    # the dashboard: the bot itself would still see the pre-reset realized
    # loss and refuse to open anything with $0 "available."
    cur.execute("select reset_at from bankroll_resets order by reset_at desc limit 1")
    reset_row = cur.fetchone()
    reset_at = reset_row[0] if reset_row else None

    cur.execute(
        "select coalesce(sum(realized_pnl), 0) from paper_trades "
        "where status = 'closed' and (%(reset_at)s::timestamptz is null or closed_at > %(reset_at)s)",
        dict(reset_at=reset_at),
    )
    return float(cur.fetchone()[0])


def _current_cash(cur: psycopg.Cursor) -> float:
    realized_pnl_total = _realized_pnl_since_reset(cur)
    cur.execute("select coalesce(sum(cost_basis), 0) from paper_trades where status = 'open'")
    open_cost_basis_total = float(cur.fetchone()[0])
    return float(STARTING_BANKROLL_USD) + realized_pnl_total - open_cost_basis_total


def _total_bankroll(cur: psycopg.Cursor) -> float:
    """Starting bankroll plus all-time realized P&L since the last reset —
    the bot's own accounting of total capital under management, independent
    of how much is currently tied up in open positions. This, not
    `_current_cash`, is what the cash reserve (paper_trading.deployable_cash)
    is pinned to: a reserve computed off `_current_cash` would shrink every
    time a position opens, since that's exactly what reduces cash — pinning
    to the total bankroll instead means the reserve only moves when money is
    actually won or lost, not when it's reallocated into a new position."""
    return float(STARTING_BANKROLL_USD) + _realized_pnl_since_reset(cur)


def main() -> int:
    load_dotenv()
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("DATABASE_URL not set — nothing to trade against.")
        return 0

    settled_count = 0
    exited_count = 0
    opened_count = 0

    with track_run("run_paper_trading") as run, psycopg.connect(database_url) as conn:
        with conn.cursor() as cur:
            open_positions = _fetch_open_positions(cur)

        # 1. Settle whatever resolved since last cycle.
        with conn.cursor() as cur:
            outcomes = _fetch_outcomes(cur, [p.market_ticker for p in open_positions])
        settlement_decisions = plan_settlements(
            [SettledPosition(p.id, p.market_ticker, p.side, p.contracts, p.cost_basis) for p in open_positions],
            outcomes,
        )
        _apply_closes(conn, settlement_decisions)
        settled_count = len(settlement_decisions)
        settled_ids = {d.id for d in settlement_decisions}

        # 2. Exit anything still open whose edge has closed, using this
        # cycle's fresh prices.
        with conn.cursor() as cur:
            open_alerts = _fetch_open_alerts(cur)
        current_by_ticker = {a.market_ticker: a for a in open_alerts}
        still_open = [p for p in open_positions if p.id not in settled_ids]
        exit_decisions = plan_exits(still_open, current_by_ticker)
        _apply_closes(conn, exit_decisions)
        exited_count = len(exit_decisions)

        # 3. Open new positions for this cycle's actionable alerts, sized
        # against whatever cash is left after the above and after holding
        # back the reserve — see deployable_cash's docstring for why the
        # reserve is pinned to total_bankroll, not cash_available itself.
        with conn.cursor() as cur:
            cur.execute("select market_ticker from paper_trades")
            already_traded = {row[0] for row in cur.fetchall()}
            cash_available = _current_cash(cur)
            total_bankroll = _total_bankroll(cur)
            existing_exposure_by_date = _existing_exposure_by_date(cur)

        deployable = deployable_cash(cash_available, total_bankroll)
        reserve_held = round(cash_available - deployable, 2)
        # Cross-city correlated-day cap, as a fraction of total bankroll (not
        # idle cash) — the same total_bankroll basis the reserve is pinned to,
        # so both risk limits move only on real P&L, not on reallocation.
        max_day_exposure = round(total_bankroll * max_day_exposure_fraction_setting(), 4)
        new_positions = plan_new_positions(
            open_alerts,
            already_traded,
            deployable,
            max_correlated_exposure=max_day_exposure,
            existing_exposure_by_date=existing_exposure_by_date,
        )
        if new_positions:
            with conn.cursor() as cur:
                for p in new_positions:
                    cur.execute(
                        "insert into paper_trades (market_ticker, event_ticker, series_ticker, city, "
                        "bracket_label, side, entry_price, contracts, entry_fee, cost_basis, "
                        "entry_model_probability, entry_edge) values (%(market_ticker)s, %(event_ticker)s, "
                        "%(series_ticker)s, %(city)s, %(bracket_label)s, %(side)s, %(entry_price)s, "
                        "%(contracts)s, %(entry_fee)s, %(cost_basis)s, %(entry_model_probability)s, "
                        "%(entry_edge)s)",
                        dict(
                            market_ticker=p.market_ticker,
                            event_ticker=p.event_ticker,
                            series_ticker=p.series_ticker,
                            city=p.city,
                            bracket_label=p.bracket_label,
                            side=p.side,
                            entry_price=p.entry_price,
                            contracts=p.contracts,
                            entry_fee=p.entry_fee,
                            cost_basis=p.cost_basis,
                            entry_model_probability=p.entry_model_probability,
                            entry_edge=p.entry_edge,
                        ),
                    )
            conn.commit()
        opened_count = len(new_positions)

        with conn.cursor() as cur:
            final_cash = _current_cash(cur)

        run.summary = (
            f"{settled_count} settled, {exited_count} exited early, {opened_count} opened, "
            f"${final_cash:.2f} cash available (${reserve_held:.2f} held in reserve)"
        )

    print(
        f"Settled {settled_count}, exited {exited_count} early, opened {opened_count} new. "
        f"Cash available: ${final_cash:.2f} (${reserve_held:.2f} held in reserve)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
