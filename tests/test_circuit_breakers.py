"""Unit tests for circuit-breaker logic."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from risk.circuit_breakers import Trade, circuit_breaker_verdict, consecutive_loss_breached, daily_loss_breached


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
