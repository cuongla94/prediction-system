"""Dynamic validation summary — the numbers behind the compact validation UI
(see dashboard/templates/_paper_trading_panel.html) and the bot-control
status API. Reuses rather than reimplements: the high-edge-cohort query
shape is the same DISTINCT-ON pattern
audit.checks_strategy_integrity.check_reproduce_high_edge_zero_wins already
uses, the model/market Brier numbers are computed fresh via
backtest.calibration.market_benchmark against the same live settled-alerts
data (not a frozen copy of the original 2026-07-20 finding — this recomputes
against CURRENT data every time it's called, same "reproduce against
current data, not a stale number" discipline as that audit check), and
every paper-trade figure (wins/losses/win_rate/profit_factor/expectancy/
drawdown/streaks/model-implied EV/realized P&L) is passed in from
dashboard/app.py's existing `_paper_trading_context()` computation —
this module does not requery paper_trades itself.
"""

from __future__ import annotations

from dataclasses import dataclass

from backtest.calibration import TradeStats, market_benchmark

# Same edge_threshold check_reproduce_high_edge_zero_wins uses by default —
# kept in step deliberately so "high-edge cohort" always means the same
# threshold everywhere it's reported.
HIGH_EDGE_THRESHOLD = 0.10

STRATEGY_STATUS_FAILED = "FAILED"
STRATEGY_STATUS_DATE = "2026-07-20"
STRATEGY_MODEL_VERSION = "normal-v4-observation-conditioned"

ROOT_CAUSE_SUMMARY = (
    "The probability model did not condition on intraday observed temperature, so "
    "its claimed \"edge\" was measuring its own staleness against a market that could "
    "already see the day's partial result, not real mispricing. Fixed in "
    f"weather/probability.py (observation_conditioned_bracket_probability, live as "
    f"{STRATEGY_MODEL_VERSION}); this status stays FAILED until scripts/run_backtest.py "
    "reports TRADEABLE out-of-sample and a fresh batch of paper trades clears the "
    "sample-size criteria on top of that."
)


@dataclass(frozen=True)
class ValidationSummary:
    status: str
    status_as_of: str
    strategy_name: str
    strategy_version: str
    conclusion: str
    settled_market_count: int | None
    unique_event_count: int | None
    city_count: int | None
    model_brier: float | None
    market_brier: float | None
    high_edge_wins: int
    high_edge_losses: int
    high_edge_settled_total: int
    high_edge_win_rate: float | None
    paper_wins: int
    paper_losses: int
    paper_settled_total: int
    paper_win_rate: float | None
    profit_factor: float | None
    expectancy: float | None
    max_drawdown: float
    winning_streak: int
    losing_streak: int
    model_implied_ev: float
    realized_pnl: float
    ev_vs_realized_gap: float
    root_cause_summary: str


def fetch_settled_alert_metrics(cur) -> dict:
    """One row per settled market (latest snapshot, DISTINCT ON — same
    "latest snapshot per ticker" pattern used throughout this codebase),
    with fresh model-vs-market Brier via market_benchmark. Returns a dict of
    Nones if nothing has settled yet, so callers can render an honest
    "unavailable" state rather than a crash or a fabricated zero.
    """
    cur.execute(
        "select distinct on (market_ticker) market_ticker, event_ticker, city, "
        "model_probability, market_yes_price, actual_outcome "
        "from alerts where settled_at is not null and actual_outcome is not null "
        "order by market_ticker, created_at desc"
    )
    rows = cur.fetchall()
    if not rows:
        return dict(settled_market_count=None, unique_event_count=None, city_count=None, model_brier=None, market_brier=None)

    predictions = [r[3] for r in rows]
    market_prices = [r[4] for r in rows]
    outcomes = [r[5] for r in rows]
    bench = market_benchmark(predictions, market_prices, outcomes)
    return dict(
        settled_market_count=len(rows),
        unique_event_count=len({r[1] for r in rows}),
        city_count=len({r[2] for r in rows}),
        model_brier=bench.brier_model if bench else None,
        market_brier=bench.brier_market if bench else None,
    )


def fetch_high_edge_cohort(cur, edge_threshold: float = HIGH_EDGE_THRESHOLD) -> tuple[int, int, int]:
    """(wins, losses, total) among the most recent settled alert per market
    whose claimed |edge| exceeds `edge_threshold` — the exact cohort
    audit.checks_strategy_integrity.check_reproduce_high_edge_zero_wins
    reproduces, recomputed here for display rather than re-derived from that
    check's Finding text."""
    cur.execute(
        "select distinct on (market_ticker) market_ticker, edge, actual_outcome "
        "from alerts where settled_at is not null and actual_outcome is not null "
        "order by market_ticker, created_at desc"
    )
    high_edge = [(edge, outcome) for _, edge, outcome in cur.fetchall() if abs(edge) > edge_threshold]
    wins = sum(1 for edge, outcome in high_edge if (edge > 0 and outcome) or (edge < 0 and not outcome))
    losses = len(high_edge) - wins
    return wins, losses, len(high_edge)


def build_validation_summary(
    cur,
    *,
    strategy_name: str,
    strategy_version: str,
    trade_performance: TradeStats,
    model_implied_ev: float,
    realized_pnl: float,
) -> ValidationSummary:
    """Assembles the full compact-UI summary. `trade_performance`,
    `model_implied_ev`, `realized_pnl` are the SAME values
    dashboard/app.py::_paper_trading_context() already computes — passed in,
    not requeried, so the compact card and the detailed trade-performance
    section below it can never silently disagree."""
    alert_metrics = fetch_settled_alert_metrics(cur)
    high_edge_wins, high_edge_losses, high_edge_total = fetch_high_edge_cohort(cur)
    high_edge_win_rate = high_edge_wins / high_edge_total if high_edge_total else None

    return ValidationSummary(
        status=STRATEGY_STATUS_FAILED,
        status_as_of=STRATEGY_STATUS_DATE,
        strategy_name=strategy_name,
        strategy_version=strategy_version,
        conclusion="The current strategy has not demonstrated an out-of-sample edge over the market.",
        settled_market_count=alert_metrics["settled_market_count"],
        unique_event_count=alert_metrics["unique_event_count"],
        city_count=alert_metrics["city_count"],
        model_brier=alert_metrics["model_brier"],
        market_brier=alert_metrics["market_brier"],
        high_edge_wins=high_edge_wins,
        high_edge_losses=high_edge_losses,
        high_edge_settled_total=high_edge_total,
        high_edge_win_rate=high_edge_win_rate,
        paper_wins=trade_performance.wins,
        paper_losses=trade_performance.losses,
        paper_settled_total=trade_performance.n,
        paper_win_rate=trade_performance.win_rate,
        profit_factor=trade_performance.profit_factor,
        expectancy=trade_performance.expectancy,
        max_drawdown=trade_performance.max_drawdown,
        winning_streak=trade_performance.longest_win_streak,
        losing_streak=trade_performance.longest_loss_streak,
        model_implied_ev=model_implied_ev,
        realized_pnl=realized_pnl,
        ev_vs_realized_gap=round(model_implied_ev - realized_pnl, 4),
        root_cause_summary=ROOT_CAUSE_SUMMARY,
    )
