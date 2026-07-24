"""Per-activation reporting for circuit-breaker validation against real trade
history — separate from risk/circuit_breakers.py's pure predicates (that
module deliberately stays I/O- and identity-free; this one adds trade
identity and report formatting on top of it, not new breach logic).

Built 2026-07-23 for the Stage 3 circuit-breaker audit. The original
DECISIONS.md table reported one "trips" count per configuration with no way
to see which trade activated it, whether it was a daily-loss or
consecutive-loss activation, or whether two different breaker types tripped
on the same calendar day (silently conflated into a single number either
way). This module produces one row per real activation instead, and the
explicit counts (unique affected days / daily-loss activations /
consecutive-loss activations / unique trading halts) needed to state the
difference precisely — see summarize_activations.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from .circuit_breakers import Trade, consecutive_loss_breached, daily_loss_breached


@dataclass(frozen=True)
class IdentifiedTrade:
    """A closed trade plus enough identity to name it in a report row.
    circuit_breakers.Trade stays minimal (closed_at, realized_pnl, status)
    since the pure predicates never need an id or ticker; this wraps it for
    reporting only, without changing that module's signature."""

    id: int
    market_ticker: str
    closed_at: datetime
    realized_pnl: float
    status: str

    @property
    def as_trade(self) -> Trade:
        return Trade(self.closed_at, self.realized_pnl, self.status)


@dataclass(frozen=True)
class Activation:
    config_name: str
    daily_loss_threshold: float
    consecutive_loss_threshold: int
    activation_type: str  # 'daily_loss' | 'consecutive_loss'
    activation_date: str
    activation_timestamp: datetime | None
    activating_trade_id: int | None
    activating_trade_ticker: str | None
    # For a consecutive-loss activation: bankroll immediately BEFORE the
    # activating trade's own P&L. For a daily-loss activation: the
    # end-of-day bankroll actually used in that day's percentage-threshold
    # math (loss_limit_dollars = -fraction * bankroll) — reported as-used,
    # not some other "before" value the calculation never touched.
    bankroll_before: float
    daily_loss_at_activation: float | None
    consecutive_losses_at_activation: int | None
    reason_code: str
    blocks_new_trades: bool


def build_activation_report(
    trades: list[IdentifiedTrade],
    config_name: str,
    daily_loss_fraction: float,
    max_consecutive_losses: int,
    starting_bankroll: float,
) -> list[Activation]:
    """Chronologically ordered trades in, one Activation row per real
    breaker trip out. Daily-loss and consecutive-loss activations are
    reported SEPARATELY (never merged into one count) and each is tied to
    the specific trade or day that caused it.

    Daily-loss activations are evaluated once per calendar day, at day
    rollover, against that day's full closed-trade pool and the bankroll
    accumulated through the end of that day — the same evaluation timing
    scripts/validate_circuit_breakers.py's day-loop already uses, matched
    here rather than reimplemented differently (a different timing
    convention between the report and the CLI tool would itself be a
    reporting-aggregation bug).

    Consecutive-loss activations are evaluated after EVERY closed trade, not
    just at day boundaries — a streak can cross midnight, and the moment it
    first reaches the threshold is a specific trade, not "the end of some
    day." Once a streak has crossed the threshold, it does not re-fire on
    every further loss in the same unbroken streak — only the trade that
    first reached it counts as the activation.
    """
    closed = sorted((t for t in trades if t.status == "closed"), key=lambda t: t.closed_at)

    activations: list[Activation] = []

    # --- Consecutive-loss pass ---
    bankroll = starting_bankroll
    streak = 0
    streak_already_activated = False
    for i, t in enumerate(closed):
        if t.realized_pnl < 0:
            streak += 1
        else:
            streak = 0
            streak_already_activated = False

        # The breach DECISION is delegated to consecutive_loss_breached
        # itself (reused, not reimplemented) — `streak` here is only a
        # display count, kept in a forward pass for convenience; it is not
        # what decides whether this trade activates the breaker.
        if consecutive_loss_breached(closed[: i + 1], max_consecutive_losses) and not streak_already_activated:
            activations.append(
                Activation(
                    config_name=config_name,
                    daily_loss_threshold=daily_loss_fraction,
                    consecutive_loss_threshold=max_consecutive_losses,
                    activation_type="consecutive_loss",
                    activation_date=t.closed_at.date().isoformat(),
                    activation_timestamp=t.closed_at,
                    activating_trade_id=t.id,
                    activating_trade_ticker=t.market_ticker,
                    bankroll_before=round(bankroll, 2),
                    daily_loss_at_activation=None,
                    consecutive_losses_at_activation=streak,
                    reason_code="CONSECUTIVE_LOSS_LIMIT",
                    blocks_new_trades=True,
                )
            )
            streak_already_activated = True

        bankroll += t.realized_pnl

    # --- Daily-loss pass ---
    def _check_day(day: date | None, trades_today: list[Trade], bankroll_at_check: float) -> None:
        if not trades_today or day is None:
            return
        if daily_loss_breached(trades_today, bankroll_at_check, daily_loss_fraction):
            daily_pnl = sum(t.realized_pnl for t in trades_today)
            activations.append(
                Activation(
                    config_name=config_name,
                    daily_loss_threshold=daily_loss_fraction,
                    consecutive_loss_threshold=max_consecutive_losses,
                    activation_type="daily_loss",
                    activation_date=day.isoformat(),
                    activation_timestamp=trades_today[-1].closed_at,
                    activating_trade_id=None,  # caused by the day's whole pool, not one trade
                    activating_trade_ticker=None,
                    bankroll_before=round(bankroll_at_check, 2),
                    daily_loss_at_activation=round(daily_pnl, 2),
                    consecutive_losses_at_activation=None,
                    reason_code="DAILY_LOSS_LIMIT",
                    blocks_new_trades=True,
                )
            )

    bankroll = starting_bankroll
    current_date: date | None = None
    today_trades: list[Trade] = []
    for t in closed:
        trade_date = t.closed_at.date()
        if trade_date != current_date:
            _check_day(current_date, today_trades, bankroll)
            current_date = trade_date
            today_trades = []
        bankroll += t.realized_pnl
        today_trades.append(t.as_trade)
    _check_day(current_date, today_trades, bankroll)

    activations.sort(key=lambda a: (a.activation_timestamp is None, a.activation_timestamp))
    return activations


def dedupe_activations(activations: list[Activation]) -> list[Activation]:
    """Keeps only the first occurrence per (config, date, type) key. Proves
    that persisting the same activation twice — a retried write, an
    overlapping evaluation, a duplicate cron trigger — doesn't create two
    report rows for what is really one activation event."""
    seen: set[tuple[str, str, str]] = set()
    result: list[Activation] = []
    for a in activations:
        key = (a.config_name, a.activation_date, a.activation_type)
        if key in seen:
            continue
        seen.add(key)
        result.append(a)
    return result


def summarize_activations(activations: list[Activation]) -> dict[str, int]:
    """Explicit counts that must never be conflated (see this module's own
    docstring for why the original DECISIONS.md table's single "trips"
    number was ambiguous): unique calendar days with ANY activation, daily-
    loss activations, consecutive-loss activations, and unique trading
    halts (one halt per day that had at least one blocking activation, even
    if both breaker types fired that same day — a halted account is halted
    once, not twice, regardless of how many reasons applied)."""
    unique_days = {a.activation_date for a in activations}
    daily = [a for a in activations if a.activation_type == "daily_loss"]
    consecutive = [a for a in activations if a.activation_type == "consecutive_loss"]
    halts = {a.activation_date for a in activations if a.blocks_new_trades}
    return {
        "unique_affected_days": len(unique_days),
        "daily_loss_activations": len(daily),
        "consecutive_loss_activations": len(consecutive),
        "unique_trading_halts": len(halts),
    }
