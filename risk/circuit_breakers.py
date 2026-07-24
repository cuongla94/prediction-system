"""Circuit breakers for real-money trading: daily/consecutive loss limits.

Pure decision functions, no I/O — suitable for unit testing and running against
historical data to validate thresholds before they're needed live.
"""

from __future__ import annotations

from datetime import datetime
from typing import NamedTuple

# Production thresholds (validated against 82 historical paper trades, 2026-07-01 to 2026-07-21).
# These are moderate: catch both major loss days (2026-07-19 -$112.86, 2026-07-20 -$69.24)
# without being over-reactive. See DECISIONS.md for full validation data and methodology.
DEFAULT_DAILY_LOSS_FRACTION = 0.10  # 10% of bankroll
DEFAULT_MAX_CONSECUTIVE_LOSSES = 5  # losing trades in a row
# NOTE: consecutive-loss threshold is mechanically tested (unit tests cover all paths)
# but not empirically validated — the 82-trade sample has no streaks ≥ 5 losses.
# Should be re-validated against a longer paper-trade sample before full production deployment.


class Trade(NamedTuple):
    """Minimal trade record for circuit-breaker logic."""

    closed_at: datetime
    realized_pnl: float
    status: str  # 'open' | 'closed'


def daily_loss_breached(
    trades_closed_today: list[Trade], bankroll: float, loss_limit_fraction: float
) -> bool:
    """Whether today's realized losses exceed a configurable fraction of bankroll.

    Args:
        trades_closed_today: trades that settled today (closed_at same calendar date)
        bankroll: current total bankroll (e.g., starting capital + all prior P&L)
        loss_limit_fraction: max daily loss as a fraction of bankroll (e.g., 0.05 for 5%)

    Returns:
        True if today's realized P&L ≤ -(loss_limit_fraction * bankroll).
        Returns False if bankroll is non-positive (catastrophic loss already occurred;
        circuit breaker should have fired earlier).
    """
    if not trades_closed_today or bankroll <= 0:
        # If bankroll is zero or negative, the account is already catastrophically
        # underwater. The breaker should have fired on a prior day. Don't trigger
        # false positives on positive P&L when bankroll is negative.
        return False

    today_pnl = sum(t.realized_pnl for t in trades_closed_today)
    loss_limit_dollars = -loss_limit_fraction * bankroll

    return today_pnl <= loss_limit_dollars


def consecutive_loss_breached(trades: list[Trade], max_consecutive_losses: int) -> bool:
    """Whether a losing streak has reached a configurable limit.

    Counts consecutive trades with realized_pnl < 0 (losses). A win resets the counter.

    Args:
        trades: trades in order (typically sorted by closed_at, oldest first)
        max_consecutive_losses: max consecutive losing trades before breaching

    Returns:
        True if current consecutive-loss streak ≥ max_consecutive_losses
    """
    if not trades or max_consecutive_losses <= 0:
        return False

    # Count from the most recent trade backward
    consecutive_losses = 0
    for trade in reversed(trades):
        if trade.status == "closed" and trade.realized_pnl < 0:
            consecutive_losses += 1
            if consecutive_losses >= max_consecutive_losses:
                return True
        else:
            # Win or open position resets the counter
            break

    return False


def solvency_breached(available_cash: float, effective_bankroll: float) -> bool:
    """Absolute guard, independent of the percentage math above: true whenever
    either figure is non-positive.

    daily_loss_breached() already returns False when bankroll <= 0 — that is
    deliberately "this specific percentage check can't be evaluated without
    inverting the comparison direction," not "trading is safe" (see its own
    docstring). This is the function that must actually stop everything in
    that state. A caller that checks only daily_loss_breached/
    consecutive_loss_breached and treats a non-positive bankroll as "no
    breach, proceed" has reproduced exactly the false-negative this function
    exists to close — always call this one too, and treat either check
    tripping as a stop.
    """
    return available_cash <= 0 or effective_bankroll <= 0


def circuit_breaker_verdict(
    trades_closed_today: list[Trade],
    all_trades: list[Trade],
    bankroll: float,
    daily_loss_fraction: float,
    max_consecutive_losses: int,
    *,
    available_cash: float | None = None,
) -> tuple[bool, str]:
    """Combined circuit-breaker check: returns (breached, reason).

    Args:
        trades_closed_today: trades that settled today
        all_trades: all trades in order (for streak counting)
        bankroll: current total bankroll (used as the "effective bankroll"
            side of the solvency guard below, in addition to the percentage
            checks)
        daily_loss_fraction: daily loss limit as a fraction of bankroll
        max_consecutive_losses: consecutive-loss limit
        available_cash: optional current liquid cash. When provided, the
            absolute solvency guard (solvency_breached) is checked FIRST,
            ahead of the percentage-based breakers — see that function's
            docstring for why a non-positive bankroll leaves the percentage
            checks unable to answer this question at all, not just harder to
            trigger. Kept optional (default None, guard skipped) so this
            stays a backward-compatible addition for existing callers that
            predate this parameter and have no cash figure to pass — a real
            caller should always pass it.

    Returns:
        (True, reason) if any breaker is tripped; (False, "") otherwise
    """
    if available_cash is not None and solvency_breached(available_cash, bankroll):
        return (
            True,
            f"solvency_breached: available_cash=${available_cash:.2f}, bankroll=${bankroll:.2f} "
            "— nonpositive capital, all new orders blocked",
        )

    if daily_loss_breached(trades_closed_today, bankroll, daily_loss_fraction):
        today_pnl = sum(t.realized_pnl for t in trades_closed_today)
        return (
            True,
            f"daily_loss_breached: ${today_pnl:.2f} today exceeds limit "
            f"${-daily_loss_fraction * bankroll:.2f}",
        )

    if consecutive_loss_breached(all_trades, max_consecutive_losses):
        return (
            True,
            f"consecutive_loss_breached: {max_consecutive_losses}+ losses in a row",
        )

    return (False, "")
