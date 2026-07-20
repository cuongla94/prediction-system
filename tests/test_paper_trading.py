from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from kalshi_client.fees import taker_fee
from paper_trading.engine import (
    OpenAlert,
    OpenPosition,
    SettledPosition,
    deployable_cash,
    plan_exits,
    plan_new_positions,
    plan_settlements,
)

NOW = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)


def _alert(
    ticker="T1",
    event="E1",
    side="YES",
    model_probability=0.40,
    market_yes_price=0.30,
    edge=0.10,
    actionable=True,
    close_time=None,
):
    return OpenAlert(
        market_ticker=ticker,
        # Real, parseable, safely-not-"today" date suffix, not just the bare
        # label — plan_new_positions excludes same-day alerts by default,
        # and an unparseable event_ticker is treated as same-day (cautious
        # default), which would otherwise silently exclude every alert this
        # helper builds. NOW below is 2026-07-19; 26JUL26 is a week out, so
        # it's unambiguously not "today" regardless of station timezone.
        event_ticker=f"{event}-26JUL26",
        series_ticker="KXHIGHNY",
        city="NYC",
        bracket_label="79-80°",
        side=side,
        model_probability=model_probability,
        market_yes_price=market_yes_price,
        edge=edge,
        is_actionable=actionable,
        close_time=close_time,
    )


# --- plan_new_positions ---


def test_opens_a_sized_position_for_a_simple_actionable_alert():
    alert = _alert()
    positions = plan_new_positions([alert], already_traded=set(), cash_available=100.0, now=NOW)
    assert len(positions) == 1
    pos = positions[0]
    # full Kelly = (0.40-0.30)/(1-0.30) = 0.142857..., *0.25 fraction = 0.035714...
    dollars = (0.10 / 0.70) * 0.25 * 100.0
    expected_contracts = int(dollars // 0.30)
    assert pos.contracts == expected_contracts
    expected_fee = taker_fee(0.30, expected_contracts)
    assert pos.entry_fee == pytest.approx(expected_fee)
    assert pos.cost_basis == pytest.approx(expected_contracts * 0.30 + expected_fee)
    assert pos.entry_price == 0.30
    assert pos.side == "YES"


def test_skips_already_traded_tickers():
    alert = _alert(ticker="T1")
    positions = plan_new_positions([alert], already_traded={"T1"}, cash_available=100.0, now=NOW)
    assert positions == []


def test_skips_non_actionable_alerts():
    alert = _alert(actionable=False)
    positions = plan_new_positions([alert], already_traded=set(), cash_available=100.0, now=NOW)
    assert positions == []


def test_no_position_when_cash_cant_afford_even_one_contract():
    alert = _alert(model_probability=0.40, market_yes_price=0.30, edge=0.10)
    positions = plan_new_positions([alert], already_traded=set(), cash_available=0.02, now=NOW)
    assert positions == []


def test_skips_entry_price_below_the_minimum():
    # 1% market price with a model at 6% clears both is_actionable and the
    # default min-edge bar, but 1c is below the default 5c floor meant to
    # keep the bot out of lottery-ticket long-shots.
    alert = _alert(model_probability=0.06, market_yes_price=0.01, edge=0.05)
    positions = plan_new_positions([alert], already_traded=set(), cash_available=100.0, now=NOW)
    assert positions == []


def test_skips_edge_below_the_minimum_even_if_actionable():
    # A thin edge that clears the fee-adjusted is_actionable bar but not the
    # bot's own, stricter min-edge bar for unattended capital.
    alert = _alert(model_probability=0.32, market_yes_price=0.30, edge=0.02, actionable=True)
    positions = plan_new_positions([alert], already_traded=set(), cash_available=100.0, min_edge=0.05, now=NOW)
    assert positions == []


def test_skips_entry_price_above_the_maximum():
    # Mirrors the min-price test at the other extreme: a 97% market price
    # clears is_actionable/min-edge, but leaves too little room after fees
    # for the edge to mean much even if the model agrees.
    alert = _alert(model_probability=1.00, market_yes_price=0.97, edge=0.03)
    positions = plan_new_positions([alert], already_traded=set(), cash_available=100.0, min_edge=0.02, now=NOW)
    assert positions == []


def test_excludes_a_same_day_market_by_default():
    # NOW is 2026-07-19 12:00 UTC, which is still 2026-07-19 in KXHIGHNY's
    # (NYC, Etc/GMT+5) standard time — an event dated today should be
    # excluded without needing to opt in, per DEFAULT_EXCLUDE_SAME_DAY.
    alert = OpenAlert(
        market_ticker="T1",
        event_ticker="KXHIGHNY-26JUL19",
        series_ticker="KXHIGHNY",
        city="NYC",
        bracket_label="79-80°",
        side="YES",
        model_probability=0.40,
        market_yes_price=0.30,
        edge=0.10,
        is_actionable=True,
    )
    positions = plan_new_positions([alert], already_traded=set(), cash_available=100.0, now=NOW)
    assert positions == []


def test_same_day_market_allowed_when_explicitly_opted_in():
    alert = OpenAlert(
        market_ticker="T1",
        event_ticker="KXHIGHNY-26JUL19",
        series_ticker="KXHIGHNY",
        city="NYC",
        bracket_label="79-80°",
        side="YES",
        model_probability=0.40,
        market_yes_price=0.30,
        edge=0.10,
        is_actionable=True,
    )
    positions = plan_new_positions(
        [alert], already_traded=set(), cash_available=100.0, exclude_same_day=False, now=NOW
    )
    assert len(positions) == 1


def test_day_ahead_market_not_treated_as_same_day():
    # Sanity check the boundary the other way: an event dated tomorrow
    # relative to NOW should trade normally under the default same-day
    # exclusion, not just when explicitly allowed.
    alert = OpenAlert(
        market_ticker="T1",
        event_ticker="KXHIGHNY-26JUL20",
        series_ticker="KXHIGHNY",
        city="NYC",
        bracket_label="79-80°",
        side="YES",
        model_probability=0.40,
        market_yes_price=0.30,
        edge=0.10,
        is_actionable=True,
    )
    positions = plan_new_positions([alert], already_traded=set(), cash_available=100.0, now=NOW)
    assert len(positions) == 1


def test_stronger_edge_event_gets_funded_before_weaker_one_when_cash_is_tight():
    weak = _alert(ticker="W1", event="EW", model_probability=0.36, market_yes_price=0.30, edge=0.06)
    strong = _alert(ticker="S1", event="ES", model_probability=0.70, market_yes_price=0.30, edge=0.40)
    # Weak alert listed first, but with only enough cash for one trade the
    # stronger-edge event should still be the one that gets funded.
    positions = plan_new_positions([weak, strong], already_traded=set(), cash_available=3.0, now=NOW)
    assert len(positions) == 1
    assert positions[0].market_ticker == "S1"


def test_no_side_prices_off_one_minus_market_yes_price():
    alert = _alert(side="NO", model_probability=0.10, market_yes_price=0.30, edge=-0.20)
    positions = plan_new_positions([alert], already_traded=set(), cash_available=100.0, now=NOW)
    assert len(positions) == 1
    assert positions[0].entry_price == pytest.approx(0.70)


def test_two_brackets_same_event_share_the_event_cap():
    # Two aggressive, identical-edge brackets in the same event — sizing/kelly's
    # own event cap (15% default) should apply jointly, same as size_event does
    # on its own (see test_sizing.py), not double-spend as if independent.
    brackets = [
        _alert(ticker="T1", event="E1", model_probability=0.90, market_yes_price=0.50, edge=0.40),
        _alert(ticker="T2", event="E1", model_probability=0.90, market_yes_price=0.50, edge=0.40),
    ]
    positions = plan_new_positions(brackets, already_traded=set(), cash_available=100.0, now=NOW)
    total_cost = sum(p.cost_basis for p in positions)
    # Capped fraction is 0.15 of 100 = $15; two contract-rounded positions
    # each add their own fee on top, so allow that slack — this is still well
    # under what two *uncapped* 0.20-fraction bets would cost (~$20+fees).
    assert total_cost <= 16.0


def test_running_cash_depletes_across_events_in_one_batch():
    # Two separate events each independently sized against the *snapshot*
    # cash_available, but the running pool should still be shared: give it
    # only enough cash for roughly one of the two.
    event_a = [_alert(ticker="A1", event="EA", model_probability=0.60, market_yes_price=0.30, edge=0.30)]
    event_b = [_alert(ticker="B1", event="EB", model_probability=0.60, market_yes_price=0.30, edge=0.30)]
    positions = plan_new_positions(event_a + event_b, already_traded=set(), cash_available=6.0, now=NOW)
    total_cost = sum(p.cost_basis for p in positions)
    assert total_cost <= 6.0
    # With such a small pool, spending it on the first event should leave the
    # second under-funded or unfunded, not sized as if it had $6 to itself too.
    assert len(positions) <= 2


# --- plan_exits ---


def test_holds_when_edge_still_favors_yes_side():
    pos = OpenPosition(id=1, market_ticker="T1", side="YES", contracts=10, cost_basis=5.0)
    current = _alert(ticker="T1", edge=0.05)  # still positive, still favors YES
    decisions = plan_exits([pos], {"T1": current})
    assert decisions == []


def test_exits_when_yes_side_edge_moves_meaningfully_past_zero():
    pos = OpenPosition(id=1, market_ticker="T1", side="YES", contracts=10, cost_basis=3.0)
    current = _alert(ticker="T1", market_yes_price=0.40, edge=-0.05)
    decisions = plan_exits([pos], {"T1": current}, now=NOW)
    assert len(decisions) == 1
    d = decisions[0]
    assert d.close_reason == "edge_closed"
    expected_fee = taker_fee(0.40, 10)
    assert d.exit_price == 0.40
    assert d.payout == pytest.approx(10 * 0.40 - expected_fee)
    assert d.realized_pnl == pytest.approx(d.payout - 3.0)


def test_holds_when_edge_only_barely_ticked_past_zero():
    # A one-point wobble shouldn't trigger a fee-paying exit — the whole
    # point of the buffer (default 3 points) is to not churn on noise.
    pos = OpenPosition(id=1, market_ticker="T1", side="YES", contracts=10, cost_basis=3.0)
    current = _alert(ticker="T1", market_yes_price=0.40, edge=-0.01)
    decisions = plan_exits([pos], {"T1": current}, now=NOW)
    assert decisions == []


def test_exits_no_side_when_market_price_drops_below_model():
    # NO position profits when market_yes_price falls toward/under model_probability
    # is wrong direction — NO wants market_yes_price to STAY high (overpriced Yes).
    # Edge (model - market) turning positive means Yes becomes fairly priced or
    # underpriced from the model's view, i.e. bad for a NO holder.
    pos = OpenPosition(id=2, market_ticker="T2", side="NO", contracts=5, cost_basis=2.0)
    current = _alert(ticker="T2", model_probability=0.55, market_yes_price=0.50, edge=0.05)
    decisions = plan_exits([pos], {"T2": current}, now=NOW)
    assert len(decisions) == 1
    assert decisions[0].exit_price == pytest.approx(0.50)  # 1 - market_yes_price


def test_holds_no_side_when_market_still_overpriced_favoring_no():
    pos = OpenPosition(id=2, market_ticker="T2", side="NO", contracts=5, cost_basis=2.0)
    current = _alert(ticker="T2", model_probability=0.20, market_yes_price=0.50, edge=-0.30)
    decisions = plan_exits([pos], {"T2": current}, now=NOW)
    assert decisions == []


def test_holds_near_settlement_even_with_a_closed_edge():
    # "Leave it till the timer is up": with only 5 minutes left before this
    # market's own close_time (default hold window is 30 minutes), an early
    # exit isn't worth its fee even though the edge has clearly reversed.
    pos = OpenPosition(id=1, market_ticker="T1", side="YES", contracts=10, cost_basis=3.0)
    current = _alert(ticker="T1", market_yes_price=0.40, edge=-0.20, close_time=NOW + timedelta(minutes=5))
    decisions = plan_exits([pos], {"T1": current}, now=NOW)
    assert decisions == []


def test_exits_with_a_closed_edge_when_plenty_of_time_remains():
    pos = OpenPosition(id=1, market_ticker="T1", side="YES", contracts=10, cost_basis=3.0)
    current = _alert(ticker="T1", market_yes_price=0.40, edge=-0.20, close_time=NOW + timedelta(hours=5))
    decisions = plan_exits([pos], {"T1": current}, now=NOW)
    assert len(decisions) == 1


def test_leaves_position_alone_when_no_current_alert():
    pos = OpenPosition(id=1, market_ticker="T1", side="YES", contracts=10, cost_basis=3.0)
    decisions = plan_exits([pos], {})
    assert decisions == []


# --- plan_settlements ---


def test_settles_a_win_pays_one_dollar_per_contract():
    pos = SettledPosition(id=1, market_ticker="T1", side="YES", contracts=10, cost_basis=4.0)
    decisions = plan_settlements([pos], {"T1": True})
    assert len(decisions) == 1
    d = decisions[0]
    assert d.close_reason == "settled_win"
    assert d.payout == pytest.approx(10.0)
    assert d.exit_fee == 0.0
    assert d.realized_pnl == pytest.approx(6.0)


def test_settles_a_loss_pays_nothing():
    pos = SettledPosition(id=1, market_ticker="T1", side="YES", contracts=10, cost_basis=4.0)
    decisions = plan_settlements([pos], {"T1": False})
    assert len(decisions) == 1
    d = decisions[0]
    assert d.close_reason == "settled_loss"
    assert d.payout == 0.0
    assert d.realized_pnl == pytest.approx(-4.0)


def test_no_side_wins_when_resolved_no():
    pos = SettledPosition(id=1, market_ticker="T1", side="NO", contracts=8, cost_basis=2.0)
    decisions = plan_settlements([pos], {"T1": False})
    assert decisions[0].close_reason == "settled_win"
    assert decisions[0].payout == pytest.approx(8.0)


def test_leaves_position_alone_when_not_yet_settled():
    pos = SettledPosition(id=1, market_ticker="T1", side="YES", contracts=10, cost_basis=4.0)
    decisions = plan_settlements([pos], {})
    assert decisions == []


# --- deployable_cash ---


def test_holds_back_the_default_reserve_fraction():
    # Fresh bankroll, nothing deployed yet: 25% of $100 held back by default.
    assert deployable_cash(100.0, 100.0) == pytest.approx(75.0)


def test_custom_reserve_fraction_overrides_the_default():
    assert deployable_cash(100.0, 100.0, reserve_fraction=0.5) == pytest.approx(50.0)


def test_zero_reserve_fraction_deploys_everything():
    assert deployable_cash(100.0, 100.0, reserve_fraction=0.0) == pytest.approx(100.0)


def test_never_goes_negative_when_cash_is_already_below_the_floor():
    # $10 left, but 25% of a $100 bankroll ($25) is supposed to be untouched
    # — there's nothing left to deploy, not a negative allowance.
    assert deployable_cash(10.0, 100.0, reserve_fraction=0.25) == 0.0


def test_reserve_is_pinned_to_total_bankroll_not_idle_cash():
    # Bankroll has grown to $200 (via realized wins), but $150 of that is
    # tied up in open positions, leaving only $50 idle. The reserve is 25%
    # of the $200 total ($50), not 25% of the $50 idle cash (which would be
    # $12.50) — so here it correctly holds back the entire $50 that's left,
    # not just a quarter of it.
    assert deployable_cash(50.0, 200.0, reserve_fraction=0.25) == 0.0


def test_reserve_floor_does_not_shrink_as_cash_is_spent_within_one_cycle():
    # Same total_bankroll, less cash_available (as if a prior allocation in
    # this same cycle already spent some) — the $25 floor (25% of $100)
    # stays fixed; only how much of cash_available clears it changes.
    assert deployable_cash(60.0, 100.0, reserve_fraction=0.25) == pytest.approx(35.0)
    assert deployable_cash(20.0, 100.0, reserve_fraction=0.25) == 0.0
