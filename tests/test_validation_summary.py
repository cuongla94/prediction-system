from __future__ import annotations

from backtest.calibration import trade_stats
from validation.summary import build_validation_summary, fetch_high_edge_cohort, fetch_settled_alert_metrics


class FakeCursor:
    """Canned-response fake, same pattern as tests/test_strategy_integrity_audit.py."""

    def __init__(self, responses: list):
        self._responses = list(responses)
        self._current: list = []

    def execute(self, _sql, _params=None):
        self._current = self._responses.pop(0) if self._responses else []

    def fetchall(self):
        return self._current


def test_fetch_settled_alert_metrics_is_unavailable_when_nothing_settled():
    cur = FakeCursor([[]])
    metrics = fetch_settled_alert_metrics(cur)
    assert metrics["settled_market_count"] is None
    assert metrics["model_brier"] is None
    assert metrics["market_brier"] is None


def test_fetch_settled_alert_metrics_computes_fresh_brier_and_counts():
    rows = [
        ("TICK-1", "EVENT-1", "NYC", 0.90, 0.95, True),
        ("TICK-2", "EVENT-1", "NYC", 0.10, 0.05, False),
        ("TICK-3", "EVENT-2", "CHI", 0.80, 0.50, True),
    ]
    cur = FakeCursor([rows])
    metrics = fetch_settled_alert_metrics(cur)
    assert metrics["settled_market_count"] == 3
    assert metrics["unique_event_count"] == 2
    assert metrics["city_count"] == 2
    assert metrics["model_brier"] is not None
    assert metrics["market_brier"] is not None


def test_fetch_high_edge_cohort_counts_wins_and_losses_by_edge_sign():
    rows = [
        ("TICK-1", 0.20, True),  # positive edge, resolved YES -> win
        ("TICK-2", 0.15, False),  # positive edge, resolved NO -> loss
        ("TICK-3", -0.20, False),  # negative edge, resolved NO -> win
        ("TICK-4", 0.05, True),  # below threshold, excluded
    ]
    cur = FakeCursor([rows])
    wins, losses, total = fetch_high_edge_cohort(cur, edge_threshold=0.10)
    assert total == 3
    assert wins == 2
    assert losses == 1


def test_build_validation_summary_reuses_passed_in_trade_performance_not_a_new_query():
    # Chronological P&L: two wins then a loss.
    performance = trade_stats([10.0, 5.0, -3.0])
    cur = FakeCursor([
        [("TICK-1", "EVENT-1", "NYC", 0.9, 0.95, True)],  # settled alert metrics
        [("TICK-1", 0.20, True)],  # high-edge cohort
    ])
    summary = build_validation_summary(
        cur,
        strategy_name="weather-daily-temp",
        strategy_version="v1-2026-07-23",
        trade_performance=performance,
        model_implied_ev=12.0,
        realized_pnl=12.0,
    )
    assert summary.status == "FAILED"
    assert summary.paper_wins == performance.wins
    assert summary.paper_losses == performance.losses
    assert summary.paper_win_rate == performance.win_rate
    assert summary.profit_factor == performance.profit_factor
    assert summary.max_drawdown == performance.max_drawdown
    assert summary.winning_streak == performance.longest_win_streak
    assert summary.losing_streak == performance.longest_loss_streak
    assert summary.ev_vs_realized_gap == 0.0


def test_ev_vs_realized_gap_is_nonzero_when_they_diverge():
    performance = trade_stats([10.0])
    cur = FakeCursor([[], []])
    summary = build_validation_summary(
        cur,
        strategy_name="weather-daily-temp",
        strategy_version="v1-2026-07-23",
        trade_performance=performance,
        model_implied_ev=50.0,
        realized_pnl=10.0,
    )
    assert summary.ev_vs_realized_gap == 40.0
