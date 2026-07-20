"""Simulated ("paper") trading against live Kalshi prices — never a real order,
never real money. Exists to answer one question with real numbers instead of a
backtest: if a bot mechanically followed this system's own signals with a real
(simulated) bankroll, would it actually make money?

Three pure decision functions, each fed real data by scripts/run_paper_trading.py
rather than doing any I/O themselves (same separation sizing/kelly.py and
edge/calculator.py already use, for the same reason: testable without a DB).

- `plan_new_positions`: which actionable alerts to open a position on this
  cycle, sized via the exact same quarter-Kelly + event-cap logic
  (sizing/kelly.py) already shown on the dashboard, scaled to a dollar
  bankroll instead of a bankroll-agnostic percentage.
- `plan_exits`: closes a position early once the edge that justified opening
  it is gone — i.e. the market has caught up to (or passed) what the model
  thought. Deliberately tied to the model's own signal rather than an
  unrelated technical/momentum rule: the point of this bot is testing the
  prediction system end to end (entries *and* exits), not layering a separate
  trading strategy on top that would confound the measurement.
- `plan_settlements`: closes whatever's still open once its market actually
  resolves, at Kalshi's real result — $1/contract on a win, $0 on a loss, no
  fee (Kalshi doesn't charge one for a contract expiring on its own).

No position ever re-opens once closed (market_ticker is the natural key —
each bracket/day only trades once, there's nothing to re-enter).
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from kalshi_client import parse_event_date
from kalshi_client.fees import taker_fee
from sizing.kelly import BracketInput, recommended_dollars, size_event
from weather.stations import STATIONS

# A round, arbitrary starting point picked by the user for this simulation —
# not derived from anything. Real bankroll at any moment is
# 100 + sum(realized_pnl of closed trades) - sum(cost_basis of open trades),
# computed by the caller from the paper_trades table, not tracked here.
STARTING_BANKROLL_USD = 100.0

# Below this price, a contract pays out at a huge multiple (a 0.5c YES pays
# ~199x) — which sounds great, but Kalshi's own fee-adjusted threshold
# shrinks toward zero at the same extremes (fee = 7% * price * (1-price)),
# so "clears the threshold" stops meaning much right where a probability
# estimate is least trustworthy (a small absolute miscalibration is a huge
# *relative* one this close to 0 or 1). Confirmed live 2026-07-19: without
# this floor, the bot was buying 1000+ contracts of half-cent brackets —
# technically +EV per the model, but a lottery-ticket risk profile, not
# "carefully picking the better trades." Applies to the price of the side
# actually being bought, not raw market_yes_price.
DEFAULT_MIN_ENTRY_PRICE = 0.05

# Mirrors DEFAULT_MIN_ENTRY_PRICE's own reasoning at the opposite extreme:
# Kalshi's fee-adjusted threshold shrinks toward zero near 100% too, so a
# price this high leaves little real room for edge after fees even when the
# model agrees strongly — same "clears the threshold stops meaning much
# here" caveat, just the other tail. Applies to the price of the side
# actually being bought, same as the floor.
DEFAULT_MAX_ENTRY_PRICE = 0.95

# Kalshi's fee-adjusted threshold (edge/calculator.py) is already the bar
# for "worth a look" on the dashboard — a human reviewing every trade can
# reasonably act on any edge past that bar. A bot spending real (simulated)
# capital automatically, with no human check per trade, warrants a higher
# bar: require a meaningfully large edge, not just "technically above a fee
# threshold that's sometimes a fraction of a percent."
DEFAULT_MIN_EDGE_TO_TRADE = 0.05

# Exiting early costs a real (simulated) taker fee — exiting the instant the
# edge crosses exactly zero means paying that fee for a one-tick wobble that
# could just as easily wobble back. Require the market to have moved
# meaningfully *past* our estimate, not just reached it, before it's worth
# paying to get out. This is a deliberate buffer around plan_exits' old
# `edge_on_our_side <= 0` rule, not a new concept.
DEFAULT_EXIT_EDGE_BUFFER = 0.03

# Within this many minutes of a market's own close_time, hold rather than
# exit early even if the edge has closed — the position resolves on its own
# very soon either way, and an early exit right before natural settlement
# just pays a fee for no real benefit (see plan_settlements: settlement
# itself is always fee-free). This is the "leave it till the timer is up"
# behavior — the bot only acts (buy or sell) when there's real time left for
# that decision to matter, otherwise it waits for the real result.
DEFAULT_HOLD_NEAR_SETTLEMENT_MINUTES = 30.0

# Fraction of the *total* bankroll (starting + all-time realized P&L, not
# just currently-idle cash — see deployable_cash) permanently held back from
# new positions. Added 2026-07-19 after the bot spent every cent of a $100
# reset within a single cycle, then a second, correlated batch (one night's
# Low Temperature brackets across many cities) put it deep underwater again
# with nothing left to fund anything else. A handful of correlated city-day
# bets is not a large sample (see the original 0/57 loss) — some batches will
# go badly by chance, and with zero reserve one bad night can fully exhaust
# the bankroll in one shot, exactly as already happened twice. 25% is a
# round, moderately conservative starting point, not derived from a
# backtest — configurable so it can be tuned once there's real data on how
# often "the whole reserve would have been needed."
DEFAULT_CASH_RESERVE_FRACTION = 0.25

# By the time a market is still open on the day its own bracket settles,
# part of that day's high/low is often already realized — the temperature
# has already partly happened, and Kalshi's price already reflects whatever
# the market knows about that (crowd-sourced, order flow, whatever) that a
# morning ensemble forecast doesn't. Betting a day-ahead-style edge against a
# same-day market is arguing against information you don't have, not just a
# thinner edge. Excluded by default, not just discouraged — see
# kalshi-implementation-progress memory for the external review this came
# from.
DEFAULT_EXCLUDE_SAME_DAY = True

# Fraction of the *total* bankroll allowed into new positions that share one
# target date, across every city, in a single cycle. A cap on correlated
# same-day exposure that sizing/kelly.py's per-event cap (MAX_EVENT_EXPOSURE)
# structurally can't see: the event cap correctly stops one city's ladder
# from over-concentrating, but a single weather pattern moves many cities'
# brackets the same direction on the same night, so "one bad night" can still
# be most of the bankroll even with every event individually capped — which
# is exactly the shape of the 0/57 wipeout. Keyed by target date because
# that's the real correlation unit: all the cities' brackets for the same
# calendar day are one bet on one synoptic pattern, not N independent ones.
# Seeded with already-open positions' exposure per date (see
# plan_new_positions' `existing_exposure_by_date`), so it's a standing cap
# across cycles, not just within a single one. 0.35 is a moderately
# conservative starting point — meaningfully tighter than the ~75% one night
# could otherwise consume after the 25% reserve — not derived from a
# backtest; configurable, same as the other risk knobs here.
DEFAULT_MAX_DAY_EXPOSURE_FRACTION = 0.35

# Take-profit: sell a *winning* position early once its realizable gain (after
# the exit fee) reaches this fraction of cost basis, even when the model's own
# edge still favors holding. Off by default (0.0 disables it) on purpose. The
# bot's core early-exit rule (plan_exits' `edge_closed`) is EV-maximizing — it
# holds a winner as long as the model still thinks the price is on our side —
# and banking a gain before then trades expected value for lower variance.
# That's a real, deliberate risk-preference choice, not a strict improvement,
# so it stays opt-in rather than silently changing what this bot is meant to
# measure (whether the signals themselves make money). When enabled, its use
# is to damp the swing of a correlated-loss night by locking in the winners
# that partly offset it — a complement to DEFAULT_MAX_DAY_EXPOSURE_FRACTION,
# not a replacement.
DEFAULT_TAKE_PROFIT_FRACTION = 0.0


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off")


def min_entry_price_setting() -> float:
    return _env_float("PAPER_TRADING_MIN_ENTRY_PRICE", DEFAULT_MIN_ENTRY_PRICE)


def max_entry_price_setting() -> float:
    return _env_float("PAPER_TRADING_MAX_ENTRY_PRICE", DEFAULT_MAX_ENTRY_PRICE)


def min_edge_setting() -> float:
    return _env_float("PAPER_TRADING_MIN_EDGE", DEFAULT_MIN_EDGE_TO_TRADE)


def exit_edge_buffer_setting() -> float:
    return _env_float("PAPER_TRADING_EXIT_EDGE_BUFFER", DEFAULT_EXIT_EDGE_BUFFER)


def hold_near_settlement_minutes_setting() -> float:
    return _env_float("PAPER_TRADING_HOLD_NEAR_SETTLEMENT_MINUTES", DEFAULT_HOLD_NEAR_SETTLEMENT_MINUTES)


def cash_reserve_fraction_setting() -> float:
    return _env_float("PAPER_TRADING_CASH_RESERVE_FRACTION", DEFAULT_CASH_RESERVE_FRACTION)


def exclude_same_day_setting() -> bool:
    return _env_bool("PAPER_TRADING_EXCLUDE_SAME_DAY", DEFAULT_EXCLUDE_SAME_DAY)


def max_day_exposure_fraction_setting() -> float:
    return _env_float("PAPER_TRADING_MAX_DAY_EXPOSURE", DEFAULT_MAX_DAY_EXPOSURE_FRACTION)


def take_profit_fraction_setting() -> float:
    return _env_float("PAPER_TRADING_TAKE_PROFIT_FRACTION", DEFAULT_TAKE_PROFIT_FRACTION)


def event_date_key(event_ticker: str) -> str:
    """The correlation-grouping key for cross-city day exposure: an event's own
    calendar date (ISO), so every city's brackets for the same day share one
    budget in plan_new_positions. Falls back to the raw event_ticker for an
    unparseable one, so such an event caps only against itself rather than
    silently joining — or corrupting — a real date bucket."""
    try:
        return parse_event_date(event_ticker).isoformat()
    except ValueError:
        return event_ticker


def _is_same_day(alert: OpenAlert, *, now: datetime) -> bool:
    """True if `alert`'s own event date is "today" in that station's local
    standard time (the same fixed-offset convention weather/stations.py
    already uses for day-boundary matching elsewhere — not a DST-shifting
    wall clock). An unresolvable station (shouldn't happen in practice,
    every real alert comes from generate_alerts.py's own STATIONS-driven
    pipeline) is treated as same-day too — can't confirm it's *not* today,
    and the whole point of this filter is caution, not the reverse.
    """
    station = STATIONS.get(alert.series_ticker)
    if station is None:
        return True
    try:
        event_date = parse_event_date(alert.event_ticker)
    except ValueError:
        return True
    local_today = now.astimezone(ZoneInfo(station.standard_time_timezone)).date()
    return event_date == local_today


def deployable_cash(cash_available: float, total_bankroll: float, *, reserve_fraction: float | None = None) -> float:
    """How much of `cash_available` the bot may actually spend on new
    positions this cycle — `cash_available` minus a reserve floor pinned to
    a fraction of `total_bankroll` (starting bankroll + all-time realized
    P&L since the last reset), not a fraction of `cash_available` itself.

    Pinning to the *total* bankroll rather than currently-idle cash matters:
    a fraction-of-cash_available reserve shrinks every time the bot opens a
    position (since that's exactly what reduces cash_available), so it would
    only ever slow a single-cycle wipeout, not actually guarantee anything
    survives across cycles. Pinning to the total bankroll instead means the
    floor only moves when real money is won or lost, not when it's simply
    reallocated into a new position — so a run of correlated losses can
    still burn through everything *above* the reserve, but never the reserve
    itself, in any single cycle. Never negative: if cash_available has
    already dropped below the floor (a previous cycle overspent it, or
    losses have shrunk cash below where a shrinking total_bankroll's reserve
    now sits), there's simply nothing left to deploy until cash recovers.
    """
    reserve_fraction_val = cash_reserve_fraction_setting() if reserve_fraction is None else reserve_fraction
    reserve_floor = total_bankroll * reserve_fraction_val
    return max(0.0, cash_available - reserve_floor)


@dataclass(frozen=True)
class OpenAlert:
    """Minimal view of one bracket's current signal, enough to decide whether
    to open or exit a paper position. Deliberately not the dashboard's
    `Alert` dataclass, to keep this module free of a dashboard dependency."""

    market_ticker: str
    event_ticker: str
    series_ticker: str
    city: str
    bracket_label: str
    side: str
    model_probability: float
    market_yes_price: float
    edge: float
    is_actionable: bool
    close_time: datetime | None = None


@dataclass(frozen=True)
class NewPosition:
    market_ticker: str
    event_ticker: str
    series_ticker: str
    city: str
    bracket_label: str
    side: str
    entry_price: float
    contracts: int
    entry_fee: float
    cost_basis: float
    entry_model_probability: float
    entry_edge: float


@dataclass(frozen=True)
class OpenPosition:
    """A currently-open paper_trades row, enough to decide whether to exit or
    settle it — not the full row, just what these decisions need."""

    id: int
    market_ticker: str
    side: str
    contracts: int
    cost_basis: float


@dataclass(frozen=True)
class SettledPosition:
    id: int
    market_ticker: str
    side: str
    contracts: int
    cost_basis: float


@dataclass(frozen=True)
class ExitDecision:
    id: int
    exit_price: float
    exit_fee: float
    payout: float
    realized_pnl: float
    close_reason: str


def plan_new_positions(
    alerts: list[OpenAlert],
    already_traded: set[str],
    cash_available: float,
    *,
    kelly_fraction: float | None = None,
    max_event_exposure: float | None = None,
    min_entry_price: float | None = None,
    max_entry_price: float | None = None,
    min_edge: float | None = None,
    exclude_same_day: bool | None = None,
    max_correlated_exposure: float | None = None,
    existing_exposure_by_date: dict[str, float] | None = None,
    now: datetime | None = None,
) -> list[NewPosition]:
    """Sizes and selects new positions for this cycle's actionable alerts.

    Filters beyond the dashboard's own `is_actionable` narrow this down to
    trades actually worth risking capital on unattended (see
    `DEFAULT_MIN_ENTRY_PRICE`/`DEFAULT_MAX_ENTRY_PRICE`/
    `DEFAULT_MIN_EDGE_TO_TRADE`/`DEFAULT_EXCLUDE_SAME_DAY` above for why):
    skip anything priced below `min_entry_price` or above `max_entry_price`
    on the side being bought (avoids lottery-ticket long-shots at either
    extreme), skip anything with less than `min_edge` of model-vs-market
    disagreement (a human glancing at the dashboard can act on a thin edge;
    a bot spending capital automatically shouldn't), and — unless
    `exclude_same_day` is explicitly False — skip anything whose own event
    date is "today" in that station's local time (part of the day's
    high/low is often already realized by the time this runs, so the
    market's price already reflects information a morning forecast doesn't
    have).

    Processes event-by-event, since sizing/kelly.py's event cap only makes
    sense computed across one event's brackets together — but events
    themselves are visited strongest-signal-first (by each event's best
    |edge|), and a single running cash pool is depleted across the *whole*
    batch (not reset per event), so when cash is scarce the best
    opportunities get funded before weaker ones, rather than whichever
    event happened to iterate first.

    `max_correlated_exposure` (when set) is a hard dollar cap on total new
    exposure per *target date* across every city — the cross-city
    correlated-day cap the per-event cap can't see (see
    `DEFAULT_MAX_DAY_EXPOSURE_FRACTION`). `existing_exposure_by_date` seeds
    that per-date tally with already-open positions' cost basis (keyed by
    `event_date_key`), making it a standing cap across cycles rather than one
    that resets every run. Because events are visited strongest-edge-first
    and share the running per-date budget, the best brackets in a crowded day
    get funded before weaker ones once the day's cap binds. Left None, this
    cap is simply not applied and behavior is unchanged.

    The Kelly percentage itself is computed against the stable `cash_available`
    snapshot for the whole cycle (matching how the dashboard sizes against a
    snapshot bankroll), not the shrinking running total — only affordability
    is checked against the running total. A bracket alert already present in
    `already_traded` (has a paper_trades row, any status) is skipped; there's
    no re-entry.
    """
    kelly_fraction_val = kelly_fraction
    max_event_exposure_val = max_event_exposure
    min_entry_price_val = min_entry_price_setting() if min_entry_price is None else min_entry_price
    max_entry_price_val = max_entry_price_setting() if max_entry_price is None else max_entry_price
    min_edge_val = min_edge_setting() if min_edge is None else min_edge
    exclude_same_day_val = exclude_same_day_setting() if exclude_same_day is None else exclude_same_day
    now_val = now if now is not None else datetime.now(UTC)
    remaining_cash = cash_available
    # Per-target-date exposure already committed (open positions this seeds in,
    # plus whatever this cycle adds below). Only consulted when
    # max_correlated_exposure is set; maintained unconditionally so the two
    # code paths don't diverge.
    deployed_by_date: dict[str, float] = dict(existing_exposure_by_date or {})
    positions: list[NewPosition] = []

    by_event: dict[str, list[OpenAlert]] = {}
    for alert in alerts:
        if alert.market_ticker in already_traded or not alert.is_actionable:
            continue
        if abs(alert.edge) < min_edge_val:
            continue
        if exclude_same_day_val and _is_same_day(alert, now=now_val):
            continue
        by_event.setdefault(alert.event_ticker, []).append(alert)

    events_by_strength = sorted(
        by_event.values(), key=lambda event_alerts: max(abs(a.edge) for a in event_alerts), reverse=True
    )

    for event_alerts in events_by_strength:
        bracket_inputs = [
            BracketInput(a.market_ticker, a.model_probability, a.market_yes_price, a.side, a.is_actionable)
            for a in event_alerts
        ]
        sizing = size_event(
            bracket_inputs, kelly_fraction=kelly_fraction_val, max_event_exposure=max_event_exposure_val
        )

        for alert in event_alerts:
            if remaining_cash <= 0:
                continue
            rec = sizing[alert.market_ticker]
            if rec.recommended_fraction <= 0:
                continue

            price = alert.market_yes_price if alert.side == "YES" else 1 - alert.market_yes_price
            if price < min_entry_price_val or price > max_entry_price_val:
                continue

            # Room left in this alert's target-date budget after already-open
            # and this-cycle positions for the same day. None when the
            # cross-city cap is disabled, in which case only cash constrains.
            day_key = event_date_key(alert.event_ticker)
            day_room = None
            if max_correlated_exposure is not None:
                day_room = round(max_correlated_exposure - deployed_by_date.get(day_key, 0.0), 4)
                if day_room <= 0:
                    continue

            budget = min(recommended_dollars(rec.recommended_fraction, cash_available), remaining_cash)
            if day_room is not None:
                budget = min(budget, day_room)
            contracts = math.floor(budget / price)
            if contracts < 1:
                continue

            entry_fee = taker_fee(price, contracts)
            cost_basis = round(contracts * price + entry_fee, 4)
            if cost_basis > remaining_cash or (day_room is not None and cost_basis > day_room):
                # The fee pushed it just over what's left (cash) or over the
                # day budget — try one fewer contract rather than dropping the
                # trade outright.
                contracts -= 1
                if contracts < 1:
                    continue
                entry_fee = taker_fee(price, contracts)
                cost_basis = round(contracts * price + entry_fee, 4)
                if cost_basis > remaining_cash or (day_room is not None and cost_basis > day_room):
                    continue

            positions.append(
                NewPosition(
                    market_ticker=alert.market_ticker,
                    event_ticker=alert.event_ticker,
                    series_ticker=alert.series_ticker,
                    city=alert.city,
                    bracket_label=alert.bracket_label,
                    side=alert.side,
                    entry_price=price,
                    contracts=contracts,
                    entry_fee=entry_fee,
                    cost_basis=cost_basis,
                    entry_model_probability=alert.model_probability,
                    entry_edge=alert.edge,
                )
            )
            remaining_cash = round(remaining_cash - cost_basis, 4)
            deployed_by_date[day_key] = round(deployed_by_date.get(day_key, 0.0) + cost_basis, 4)

    return positions


def plan_exits(
    positions: list[OpenPosition],
    current_by_ticker: dict[str, OpenAlert],
    *,
    now: datetime | None = None,
    exit_edge_buffer: float | None = None,
    hold_near_settlement_minutes: float | None = None,
    take_profit_fraction: float | None = None,
) -> list[ExitDecision]:
    """Decision per open position: HOLD (default — the edge still favors our
    side and we're not banking a gain, or we're too close to settlement for
    an early exit to be worth its fee), SELL for `edge_closed` (the edge has
    moved meaningfully past our side, with real time left for that to
    matter), SELL for `take_profit` (only when that's enabled — a winner
    whose realizable gain has reached the take-profit threshold even though
    the edge would still hold it, see `DEFAULT_TAKE_PROFIT_FRACTION`), or
    leave it for `plan_settlements` once the market actually resolves ("leave
    it till the timer is up"). A ticker with no current alert this cycle
    (e.g. it dropped out of the open-events window) is left alone here
    regardless; settlement will catch it once it resolves.

    `edge_closed` takes precedence over `take_profit` when both would fire, so
    a sell driven by the model turning against us is labelled for that reason
    rather than for the incidental gain — take-profit exists specifically for
    the case `edge_closed` does *not* catch (up on the position, but the model
    still likes it).
    """
    now = now if now is not None else datetime.now(UTC)
    buffer_val = exit_edge_buffer_setting() if exit_edge_buffer is None else exit_edge_buffer
    hold_minutes_val = (
        hold_near_settlement_minutes_setting() if hold_near_settlement_minutes is None else hold_near_settlement_minutes
    )
    take_profit_val = take_profit_fraction_setting() if take_profit_fraction is None else take_profit_fraction

    decisions = []
    for pos in positions:
        current = current_by_ticker.get(pos.market_ticker)
        if current is None:
            continue

        if current.close_time is not None and current.close_time - now <= timedelta(minutes=hold_minutes_val):
            continue  # close enough to settlement that an early exit isn't worth the fee — hold for the real result

        exit_price = current.market_yes_price if pos.side == "YES" else 1 - current.market_yes_price
        exit_fee = taker_fee(exit_price, pos.contracts)
        payout = round(pos.contracts * exit_price - exit_fee, 4)
        realized_pnl = round(payout - pos.cost_basis, 4)

        edge_on_our_side = current.edge if pos.side == "YES" else -current.edge
        if edge_on_our_side <= -buffer_val:
            close_reason = "edge_closed"  # market moved meaningfully past our side
        elif take_profit_val > 0 and pos.cost_basis > 0 and realized_pnl >= take_profit_val * pos.cost_basis:
            close_reason = "take_profit"  # up enough to bank it, even though the edge would still hold
        else:
            continue  # still favors our side and not enough gain to lock in — keep holding

        decisions.append(
            ExitDecision(
                id=pos.id,
                exit_price=exit_price,
                exit_fee=exit_fee,
                payout=payout,
                realized_pnl=realized_pnl,
                close_reason=close_reason,
            )
        )
    return decisions


def plan_settlements(
    positions: list[SettledPosition], outcomes_by_ticker: dict[str, bool]
) -> list[ExitDecision]:
    """Closes whatever's still open once its market has actually settled.
    `outcomes_by_ticker` maps market_ticker -> True if that bracket resolved
    Yes. A position not present in `outcomes_by_ticker` hasn't settled yet and
    is left alone. No fee here — Kalshi doesn't charge one for a contract
    resolving on its own, only for placing an order."""
    decisions = []
    for pos in positions:
        if pos.market_ticker not in outcomes_by_ticker:
            continue
        resolved_yes = outcomes_by_ticker[pos.market_ticker]
        won = resolved_yes if pos.side == "YES" else not resolved_yes
        payout = float(pos.contracts) if won else 0.0
        decisions.append(
            ExitDecision(
                id=pos.id,
                exit_price=1.0 if won else 0.0,
                exit_fee=0.0,
                payout=payout,
                realized_pnl=round(payout - pos.cost_basis, 4),
                close_reason="settled_win" if won else "settled_loss",
            )
        )
    return decisions
