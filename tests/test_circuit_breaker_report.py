from __future__ import annotations

from datetime import datetime, timedelta, timezone

from risk.circuit_breaker_report import (
    IdentifiedTrade,
    build_activation_report,
    dedupe_activations,
    summarize_activations,
)


def _trade(id_, day_offset, hour, pnl, status="closed", base=datetime(2026, 7, 1, tzinfo=timezone.utc)):
    return IdentifiedTrade(
        id=id_,
        market_ticker=f"TICK-{id_}",
        closed_at=base + timedelta(days=day_offset, hours=hour),
        realized_pnl=pnl,
        status=status,
    )


def test_daily_loss_activation_reports_the_right_day_and_bankroll():
    trades = [
        _trade(1, 0, 9, -10.0),
        _trade(2, 0, 10, -50.0),  # day total -60, 6% of 1000 -> breaches a 5% limit
    ]
    activations = build_activation_report(
        trades, "test-config", daily_loss_fraction=0.05, max_consecutive_losses=99, starting_bankroll=1000.0
    )
    daily = [a for a in activations if a.activation_type == "daily_loss"]
    assert len(daily) == 1
    assert daily[0].activation_date == "2026-07-01"
    assert daily[0].daily_loss_at_activation == -60.0
    # Both trades fall on the only (and therefore final) day in this fixture,
    # so the bankroll used in the % check already includes that day's own
    # P&L: 1000 - 60 = 940 — matching validate_circuit_breakers.py's own
    # "final day" handling, which has no next day's rollover to check
    # against before the day's trades are folded in.
    assert daily[0].bankroll_before == 940.0
    assert daily[0].reason_code == "DAILY_LOSS_LIMIT"
    assert daily[0].blocks_new_trades


def test_consecutive_loss_activation_ties_to_the_activating_trade():
    trades = [
        _trade(1, 0, 9, -10.0),
        _trade(2, 0, 10, -10.0),
        _trade(3, 0, 11, -10.0),  # 3rd consecutive loss
    ]
    activations = build_activation_report(
        trades, "test-config", daily_loss_fraction=0.99, max_consecutive_losses=3, starting_bankroll=1000.0
    )
    consecutive = [a for a in activations if a.activation_type == "consecutive_loss"]
    assert len(consecutive) == 1
    assert consecutive[0].activating_trade_id == 3
    assert consecutive[0].activating_trade_ticker == "TICK-3"
    assert consecutive[0].consecutive_losses_at_activation == 3
    # Bankroll before the 3rd trade's own P&L: 1000 - 10 - 10 = 980.
    assert consecutive[0].bankroll_before == 980.0


def test_consecutive_loss_does_not_refire_on_every_trade_in_an_unbroken_streak():
    trades = [_trade(i, 0, i, -10.0) for i in range(1, 8)]  # 7 losses in a row
    activations = build_activation_report(
        trades, "test-config", daily_loss_fraction=0.99, max_consecutive_losses=3, starting_bankroll=1000.0
    )
    consecutive = [a for a in activations if a.activation_type == "consecutive_loss"]
    # Only the trade that FIRST reaches the threshold (the 3rd) activates —
    # not the 4th, 5th, 6th, 7th that merely extend the same streak.
    assert len(consecutive) == 1
    assert consecutive[0].activating_trade_id == 3


def test_win_resets_streak_and_a_new_streak_can_activate_again():
    trades = [
        _trade(1, 0, 1, -10.0),
        _trade(2, 0, 2, -10.0),
        _trade(3, 0, 3, -10.0),  # activates here
        _trade(4, 0, 4, 50.0),  # win resets
        _trade(5, 0, 5, -10.0),
        _trade(6, 0, 6, -10.0),
        _trade(7, 0, 7, -10.0),  # activates again, new streak
    ]
    activations = build_activation_report(
        trades, "test-config", daily_loss_fraction=0.99, max_consecutive_losses=3, starting_bankroll=1000.0
    )
    consecutive = [a for a in activations if a.activation_type == "consecutive_loss"]
    assert [a.activating_trade_id for a in consecutive] == [3, 7]


def test_open_trades_excluded_from_both_activation_types():
    trades = [
        _trade(1, 0, 1, -500.0, status="open"),
        _trade(2, 0, 2, -10.0),
    ]
    activations = build_activation_report(
        trades, "test-config", daily_loss_fraction=0.05, max_consecutive_losses=1, starting_bankroll=1000.0
    )
    # Only trade 2 is closed; -10 alone doesn't breach a 5% ($50) daily limit
    # and doesn't reach a 1-loss streak on its own... actually a single
    # closed loss DOES reach max_consecutive_losses=1, so expect exactly
    # that one consecutive activation and nothing from the open trade.
    assert len(activations) == 1
    assert activations[0].activation_type == "consecutive_loss"
    assert activations[0].activating_trade_id == 2


def test_dedupe_activations_collapses_repeated_calls_to_one_row_per_key():
    trades = [_trade(1, 0, 1, -10.0), _trade(2, 0, 2, -10.0), _trade(3, 0, 3, -10.0)]
    once = build_activation_report(
        trades, "test-config", daily_loss_fraction=0.99, max_consecutive_losses=3, starting_bankroll=1000.0
    )
    duplicated_write = once + once  # simulates the same activation persisted twice
    deduped = dedupe_activations(duplicated_write)
    assert deduped == once


def test_summarize_activations_distinguishes_all_four_counts():
    trades = [
        _trade(1, 0, 1, -60.0),  # day 0: daily-loss activation (6% of 1000)
        _trade(2, 1, 1, -10.0),
        _trade(3, 1, 2, -10.0),
        _trade(4, 1, 3, -10.0),  # day 1: consecutive-loss activation (3rd loss)
    ]
    activations = build_activation_report(
        trades, "test-config", daily_loss_fraction=0.05, max_consecutive_losses=3, starting_bankroll=1000.0
    )
    summary = summarize_activations(activations)
    assert summary["daily_loss_activations"] == 1
    assert summary["consecutive_loss_activations"] == 1
    assert summary["unique_affected_days"] == 2
    assert summary["unique_trading_halts"] == 2


def test_same_day_double_activation_counts_as_one_halt_not_two():
    # Construct a day where BOTH a daily-loss and (via the day's trades
    # crossing the streak threshold) a consecutive-loss activation fire.
    trades = [
        _trade(1, 0, 1, -30.0),
        _trade(2, 0, 2, -30.0),
        _trade(3, 0, 3, -30.0),  # day total -90 (9% of 1000, breaches 5%); also 3rd consecutive loss
    ]
    activations = build_activation_report(
        trades, "test-config", daily_loss_fraction=0.05, max_consecutive_losses=3, starting_bankroll=1000.0
    )
    summary = summarize_activations(activations)
    assert summary["daily_loss_activations"] == 1
    assert summary["consecutive_loss_activations"] == 1
    assert summary["unique_affected_days"] == 1
    assert summary["unique_trading_halts"] == 1
