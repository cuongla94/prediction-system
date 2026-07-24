"""Unit tests for circuit-breaker logic."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from risk.circuit_breakers import (
    Trade,
    circuit_breaker_verdict,
    consecutive_loss_breached,
    daily_loss_breached,
    solvency_breached,
)


@pytest.fixture
def sample_trades():
    """Fixture: sample trades for testing."""
    base_time = datetime(2026, 7, 21, 12, 0, 0, tzinfo=timezone.utc)
    return [
        Trade(base_time - timedelta(hours=3), -50.0, "closed"),  # loss
        Trade(base_time - timedelta(hours=2), 100.0, "closed"),  # win
        Trade(base_time - timedelta(hours=1), -30.0, "closed"),  # loss
    ]


class TestDailyLossBreach:
    def test_no_trades(self):
        """No trades closed today → not breached."""
        assert not daily_loss_breached([], bankroll=1000, loss_limit_fraction=0.05)

    def test_small_loss_not_breached(self):
        """Loss smaller than limit → not breached."""
        today_time = datetime.now(timezone.utc)
        trades = [Trade(today_time, -40.0, "closed")]
        # Limit is 5% of $1000 = $50, actual loss is $40 → not breached
        assert not daily_loss_breached(trades, bankroll=1000, loss_limit_fraction=0.05)

    def test_exact_loss_limit_breached(self):
        """Loss exactly at limit → breached (≤ comparison)."""
        today_time = datetime.now(timezone.utc)
        trades = [Trade(today_time, -50.0, "closed")]
        # Limit is 5% of $1000 = $50, actual loss is exactly $50 → breached
        assert daily_loss_breached(trades, bankroll=1000, loss_limit_fraction=0.05)

    def test_large_loss_breached(self):
        """Loss larger than limit → breached."""
        today_time = datetime.now(timezone.utc)
        trades = [Trade(today_time, -75.0, "closed")]
        # Limit is 5% of $1000 = $50, actual loss is $75 → breached
        assert daily_loss_breached(trades, bankroll=1000, loss_limit_fraction=0.05)

    def test_mixed_trades_pooled(self):
        """Multiple trades: realized P&L pooled, checked against limit."""
        today_time = datetime.now(timezone.utc)
        trades = [
            Trade(today_time, -100.0, "closed"),
            Trade(today_time, 60.0, "closed"),
        ]
        # Total P&L = -$40, limit is $50 → not breached
        assert not daily_loss_breached(trades, bankroll=1000, loss_limit_fraction=0.05)

        # Same trades, tighter limit: 2% of $1000 = $20 → breached
        assert daily_loss_breached(trades, bankroll=1000, loss_limit_fraction=0.02)

    def test_negative_bankroll_returns_false(self):
        """Negative bankroll (catastrophic loss) → not breached (fail-closed).

        When bankroll goes negative after massive prior losses, the account is
        already underwater. The breaker should have fired on an earlier day.
        Don't trigger false positives on positive P&L today due to negative math.
        """
        today_time = datetime.now(timezone.utc)
        trades = [Trade(today_time, 50.0, "closed")]  # positive P&L today
        # Even with a small loss today, negative bankroll always returns False
        assert not daily_loss_breached(trades, bankroll=-82.10, loss_limit_fraction=0.10)

    def test_zero_bankroll_returns_false(self):
        """Zero bankroll (account exhausted) → not breached."""
        today_time = datetime.now(timezone.utc)
        trades = [Trade(today_time, -10.0, "closed")]
        assert not daily_loss_breached(trades, bankroll=0, loss_limit_fraction=0.10)


class TestConsecutiveLossBreach:
    def test_no_trades(self):
        """No trades → not breached."""
        assert not consecutive_loss_breached([], max_consecutive_losses=3)

    def test_all_wins(self):
        """All winning trades → not breached."""
        base_time = datetime(2026, 7, 21, 12, 0, 0, tzinfo=timezone.utc)
        trades = [
            Trade(base_time, 50.0, "closed"),
            Trade(base_time + timedelta(hours=1), 75.0, "closed"),
        ]
        assert not consecutive_loss_breached(trades, max_consecutive_losses=2)

    def test_win_resets_streak(self):
        """A win in the middle resets the streak."""
        base_time = datetime(2026, 7, 21, 12, 0, 0, tzinfo=timezone.utc)
        trades = [
            Trade(base_time, -50.0, "closed"),
            Trade(base_time + timedelta(hours=1), 100.0, "closed"),  # reset
            Trade(base_time + timedelta(hours=2), -30.0, "closed"),
            Trade(base_time + timedelta(hours=3), -20.0, "closed"),
        ]
        # Streak is 2, limit is 3 → not breached
        assert not consecutive_loss_breached(trades, max_consecutive_losses=3)
        # Streak is 2, limit is 2 → breached
        assert consecutive_loss_breached(trades, max_consecutive_losses=2)

    def test_exact_streak_limit(self):
        """Exactly hitting the limit → breached (≥ comparison)."""
        base_time = datetime(2026, 7, 21, 12, 0, 0, tzinfo=timezone.utc)
        trades = [
            Trade(base_time, -50.0, "closed"),
            Trade(base_time + timedelta(hours=1), -30.0, "closed"),
            Trade(base_time + timedelta(hours=2), -20.0, "closed"),
        ]
        # Streak is 3, limit is 3 → breached
        assert consecutive_loss_breached(trades, max_consecutive_losses=3)

    def test_open_positions_not_counted(self):
        """Open positions are ignored (only 'closed' count)."""
        base_time = datetime(2026, 7, 21, 12, 0, 0, tzinfo=timezone.utc)
        trades = [
            Trade(base_time, -50.0, "closed"),
            Trade(base_time + timedelta(hours=1), -30.0, "open"),  # ignored
            Trade(base_time + timedelta(hours=2), -20.0, "closed"),
        ]
        # Only looking at closed: [-50, -20], streak is 2
        assert not consecutive_loss_breached(trades, max_consecutive_losses=3)


class TestCircuitBreakerVerdict:
    def test_both_clear(self, sample_trades):
        """Neither daily nor consecutive limit breached."""
        today_time = datetime.now(timezone.utc)
        today_trades = [Trade(today_time, -30.0, "closed")]
        breached, reason = circuit_breaker_verdict(
            today_trades,
            sample_trades,
            bankroll=1000,
            daily_loss_fraction=0.10,  # 10% = $100
            max_consecutive_losses=5,
        )
        assert not breached
        assert reason == ""

    def test_daily_loss_triggers(self):
        """Daily loss exceeds limit."""
        today_time = datetime.now(timezone.utc)
        today_trades = [Trade(today_time, -120.0, "closed")]
        breached, reason = circuit_breaker_verdict(
            today_trades,
            [],
            bankroll=1000,
            daily_loss_fraction=0.10,  # 10% = $100
            max_consecutive_losses=5,
        )
        assert breached
        assert "daily_loss_breached" in reason
        assert "-120" in reason  # shows the actual loss

    def test_consecutive_loss_triggers(self):
        """Consecutive loss limit exceeded."""
        base_time = datetime(2026, 7, 21, 12, 0, 0, tzinfo=timezone.utc)
        trades = [
            Trade(base_time, -50.0, "closed"),
            Trade(base_time + timedelta(hours=1), -30.0, "closed"),
            Trade(base_time + timedelta(hours=2), -20.0, "closed"),
        ]
        breached, reason = circuit_breaker_verdict(
            [],
            trades,
            bankroll=1000,
            daily_loss_fraction=0.50,  # high, won't trigger
            max_consecutive_losses=2,
        )
        assert breached
        assert "consecutive_loss_breached" in reason


class TestMonotonicity:
    """Stage 3 circuit-breaker audit (2026-07-23): a looser configuration
    (larger threshold) must never trip more easily, sooner, or on strictly
    more days/trades than a stricter one (smaller threshold), for the same
    chronologically ordered trade population. DECISIONS.md's circuit-breaker
    table implicitly claimed this ordering (conservative >= moderate >= loose
    in trip count) without it ever being directly tested — these tests close
    that gap.
    """

    def test_daily_loss_stricter_threshold_breaches_whenever_looser_does(self):
        """For the same (today's trades, bankroll), if a looser (larger)
        daily-loss fraction breaches, a stricter (smaller) fraction must
        also breach — lowering the threshold can only make daily-loss
        breaches MORE common, never fewer."""
        bankroll = 1000.0
        scenarios = [
            [Trade(datetime.now(timezone.utc), -30.0, "closed")],
            [Trade(datetime.now(timezone.utc), -80.0, "closed")],
            [Trade(datetime.now(timezone.utc), -150.0, "closed")],
            [Trade(datetime.now(timezone.utc), 40.0, "closed")],
        ]
        strict, loose = 0.05, 0.20
        for today_trades in scenarios:
            loose_breached = daily_loss_breached(today_trades, bankroll, loose)
            strict_breached = daily_loss_breached(today_trades, bankroll, strict)
            if loose_breached:
                assert strict_breached, (
                    f"loose threshold {loose:.0%} breached on "
                    f"P&L={sum(t.realized_pnl for t in today_trades)} but stricter {strict:.0%} "
                    "did not — threshold direction is inverted"
                )

    def test_daily_loss_first_activation_day_not_delayed_by_stricter_threshold(self):
        """Across a multi-day sequence, the first day a stricter (smaller)
        daily-loss threshold breaches must be no later than the first day a
        looser (larger) one breaches."""
        bankroll = 1000.0
        daily_pnls = [-20.0, -10.0, -60.0, 5.0, -90.0]  # one "day" of net P&L each
        strict, loose = 0.03, 0.08

        def first_breach_day(fraction):
            for day_index, pnl in enumerate(daily_pnls):
                today_trades = [Trade(datetime.now(timezone.utc), pnl, "closed")]
                if daily_loss_breached(today_trades, bankroll, fraction):
                    return day_index
            return None

        strict_day = first_breach_day(strict)
        loose_day = first_breach_day(loose)
        assert loose_day is not None, "test fixture should trip the loose threshold at least once"
        assert strict_day is not None
        assert strict_day <= loose_day

    def test_consecutive_loss_stricter_threshold_breaches_whenever_looser_does(self):
        """For the same trade history, a stricter (smaller) max_consecutive_losses
        must breach whenever a looser (larger) one does."""
        base_time = datetime(2026, 7, 21, 12, 0, 0, tzinfo=timezone.utc)
        trades = [Trade(base_time + timedelta(hours=i), -10.0, "closed") for i in range(6)]
        strict, loose = 3, 5
        for prefix_len in range(1, len(trades) + 1):
            prefix = trades[:prefix_len]
            loose_breached = consecutive_loss_breached(prefix, loose)
            strict_breached = consecutive_loss_breached(prefix, strict)
            if loose_breached:
                assert strict_breached

    def test_consecutive_loss_first_activation_not_delayed_by_stricter_threshold(self):
        """The first trade index at which a stricter streak limit trips must
        be no later than where a looser one trips."""
        base_time = datetime(2026, 7, 21, 12, 0, 0, tzinfo=timezone.utc)
        trades = [Trade(base_time + timedelta(hours=i), -10.0, "closed") for i in range(7)]
        strict, loose = 2, 6

        def first_breach_index(max_losses):
            for i in range(1, len(trades) + 1):
                if consecutive_loss_breached(trades[:i], max_losses):
                    return i
            return None

        strict_index = first_breach_index(strict)
        loose_index = first_breach_index(loose)
        assert loose_index is not None
        assert strict_index is not None
        assert strict_index <= loose_index

    def test_open_trades_never_count_as_settled_losses(self):
        """An open (unresolved) position, however negative its unrealized
        P&L, must not by itself trip the consecutive-loss breaker — only
        settled ('closed') trades count as wins or losses."""
        base_time = datetime(2026, 7, 21, 12, 0, 0, tzinfo=timezone.utc)
        trades = [Trade(base_time + timedelta(hours=i), -500.0, "open") for i in range(10)]
        assert not consecutive_loss_breached(trades, max_consecutive_losses=1)

    def test_open_trades_excluded_from_daily_loss_too(self):
        """daily_loss_breached pools whatever list it's given — callers are
        responsible for pre-filtering to closed trades before calling it
        (see validate_circuit_breakers.py's day-boundary logic). This test
        documents that contract explicitly: an open trade slipped past the
        filter would otherwise inflate today's realized loss."""
        bankroll = 1000.0
        today_time = datetime.now(timezone.utc)
        trades = [
            Trade(today_time, -500.0, "open"),  # must be excluded by the caller
            Trade(today_time, -10.0, "closed"),
        ]
        closed_only = [t for t in trades if t.status == "closed"]
        # A 5% ($50) limit would breach if the open trade's -500 leaked in.
        assert not daily_loss_breached(closed_only, bankroll, loss_limit_fraction=0.05)


class TestSolvencyGuard:
    def test_nonpositive_cash_breached(self):
        assert solvency_breached(available_cash=0.0, effective_bankroll=100.0)
        assert solvency_breached(available_cash=-5.0, effective_bankroll=100.0)

    def test_nonpositive_bankroll_breached(self):
        assert solvency_breached(available_cash=50.0, effective_bankroll=0.0)
        assert solvency_breached(available_cash=50.0, effective_bankroll=-1.0)

    def test_both_positive_not_breached(self):
        assert not solvency_breached(available_cash=50.0, effective_bankroll=100.0)

    def test_verdict_checks_solvency_first_when_cash_provided(self):
        """circuit_breaker_verdict's optional available_cash param triggers the
        solvency guard ahead of the percentage breakers — a nonpositive
        bankroll would otherwise make daily_loss_breached return False (see
        its own docstring), silently reading as "no breach.\""""
        breached, reason = circuit_breaker_verdict(
            trades_closed_today=[],
            all_trades=[],
            bankroll=-10.0,
            daily_loss_fraction=0.10,
            max_consecutive_losses=5,
            available_cash=-2.0,
        )
        assert breached
        assert "solvency_breached" in reason

    def test_verdict_omits_solvency_check_when_cash_not_provided(self):
        """Backward compatible: existing callers that don't pass available_cash
        get exactly the old percentage-only behavior."""
        breached, reason = circuit_breaker_verdict(
            trades_closed_today=[],
            all_trades=[],
            bankroll=-10.0,
            daily_loss_fraction=0.10,
            max_consecutive_losses=5,
        )
        assert not breached
        assert reason == ""
