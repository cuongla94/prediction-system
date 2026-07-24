from __future__ import annotations

from datetime import UTC, datetime

import pytest

import bot_control.state as bcs
from bot_control.state import (
    BotControlError,
    disable_live,
    enable_live,
    get_bot_state,
    list_recent_events,
    start_bot,
    stop_bot,
)
from bot_control.state import reset_kill_switch, trigger_kill_switch

_ACTIVITY_COLUMNS = [
    "created_at", "event_type", "requested_mode", "effective_mode", "enabled",
    "kill_switch", "kill_switch_reason", "actor", "reason_code", "note", "detail",
]


class _Col:
    def __init__(self, name):
        self.name = name


class FakeCursor:
    """Canned-response fake, same spirit as tests/test_strategy_integrity_audit.py's
    FakeCursor — dispatches on the SQL's opening words rather than parsing it."""

    def __init__(self, conn):
        self._conn = conn
        self._fetchone = None
        self._fetchall: list = []
        self.description = []

    def execute(self, sql, params=None):
        norm = " ".join(sql.split()).lower()
        if norm.startswith("select effective_mode"):
            self._fetchone = self._conn.latest_state_tuple()
        elif norm.startswith("insert into bot_control_events"):
            row = dict(params)
            row["created_at"] = datetime(2026, 7, 23, 12, 0, 0, tzinfo=UTC)
            self._conn.rows.append(row)
            self._fetchone = (row["created_at"],)
        elif norm.startswith("select created_at, event_type"):
            self._fetchall = [tuple(r.get(c) for c in _ACTIVITY_COLUMNS) for r in reversed(self._conn.rows)]
            self.description = [_Col(c) for c in _ACTIVITY_COLUMNS]
        else:
            raise AssertionError(f"unexpected SQL in FakeCursor: {sql}")

    def fetchone(self):
        return self._fetchone

    def fetchall(self):
        return self._fetchall

    def close(self):
        pass


class FakeConnection:
    def __init__(self):
        self.rows: list[dict] = []
        self.committed = False

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.committed = True

    def close(self):
        pass

    def latest_state_tuple(self):
        if not self.rows:
            return None
        r = self.rows[-1]
        return (
            r.get("effective_mode"),
            r.get("enabled"),
            r.get("kill_switch"),
            r.get("kill_switch_reason"),
            r.get("strategy_name"),
            r.get("strategy_version"),
            r.get("created_at"),
            r.get("actor"),
        )


@pytest.fixture
def fake_db(monkeypatch):
    conn = FakeConnection()
    monkeypatch.setattr(bcs, "get_db", lambda: conn)
    return conn


def test_get_bot_state_defaults_to_off_with_no_history(fake_db):
    state = get_bot_state()
    assert state.effective_mode == "OFF"
    assert state.enabled is False
    assert state.kill_switch is False


def test_get_bot_state_returns_safe_default_when_db_unavailable(monkeypatch):
    monkeypatch.setattr(bcs, "get_db", lambda: None)
    state = get_bot_state()
    assert state.effective_mode == "OFF"
    assert state.enabled is False
    assert state.kill_switch is False


def test_start_bot_paper_enables_it(fake_db):
    state = start_bot("PAPER", actor="test-actor")
    assert state.effective_mode == "PAPER"
    assert state.enabled is True
    assert fake_db.committed
    assert get_bot_state().effective_mode == "PAPER"


def test_start_bot_off_is_valid_and_disabled(fake_db):
    state = start_bot("OFF", actor="test-actor")
    assert state.effective_mode == "OFF"
    assert state.enabled is False


@pytest.mark.parametrize("mode", ["SHADOW", "DEMO", "LIVE_CANARY", "LIVE"])
def test_start_bot_rejects_every_unimplemented_mode(fake_db, mode):
    with pytest.raises(BotControlError) as exc_info:
        start_bot(mode, actor="test-actor")
    assert exc_info.value.reason_code == "NOT_IMPLEMENTED"
    # Rejected — current effective state must remain OFF, not silently
    # switch to the unimplemented mode.
    state = get_bot_state()
    assert state.effective_mode == "OFF"
    assert state.enabled is False
    # But the rejection itself is audit-visible, not silently dropped.
    events = list_recent_events()
    assert events[0]["event_type"] == "start_rejected"
    assert events[0]["requested_mode"] == mode
    assert events[0]["reason_code"] == "NOT_IMPLEMENTED"


def test_start_bot_rejects_unrecognized_mode_string(fake_db):
    with pytest.raises(BotControlError) as exc_info:
        start_bot("NOT_A_REAL_MODE", actor="test-actor")
    assert exc_info.value.reason_code == "INVALID_MODE"


def test_stop_bot_disables_without_touching_kill_switch(fake_db):
    start_bot("PAPER", actor="test-actor")
    state = stop_bot(actor="test-actor")
    assert state.effective_mode == "OFF"
    assert state.enabled is False
    assert state.kill_switch is False


def test_kill_switch_disables_and_blocks_further_starts(fake_db):
    start_bot("PAPER", actor="test-actor")
    state = trigger_kill_switch("daily loss limit breached", actor="risk-engine")
    assert state.kill_switch is True
    assert state.enabled is False
    assert state.kill_switch_reason == "daily loss limit breached"

    with pytest.raises(BotControlError) as exc_info:
        start_bot("PAPER", actor="test-actor")
    assert exc_info.value.reason_code == "KILL_SWITCH_ACTIVE"


def test_reset_kill_switch_clears_it_but_does_not_re_enable_trading(fake_db):
    trigger_kill_switch("solvency failure", actor="risk-engine")
    state = reset_kill_switch(actor="operator")
    assert state.kill_switch is False
    assert state.kill_switch_reason is None
    # Explicitly NOT re-enabled — an operator must call start_bot again.
    assert state.enabled is False
    assert state.effective_mode == "OFF"


def test_start_bot_after_kill_reset_works_again(fake_db):
    trigger_kill_switch("solvency failure", actor="risk-engine")
    reset_kill_switch(actor="operator")
    state = start_bot("PAPER", actor="operator")
    assert state.effective_mode == "PAPER"
    assert state.enabled is True


def test_live_enable_and_disable_are_persisted_separately_from_generic_start(fake_db):
    enabled = enable_live(actor="operator")
    assert enabled.effective_mode == "LIVE"
    assert enabled.enabled is True
    assert enabled.live_enabled is True
    assert fake_db.rows[-1]["live_enabled"] is True

    disabled = disable_live(actor="operator")
    assert disabled.effective_mode == "OFF"
    assert disabled.enabled is False
    assert disabled.live_enabled is False
    assert fake_db.rows[-1]["event_type"] == "live_disable"


def test_list_recent_events_returns_empty_list_when_db_unavailable(monkeypatch):
    monkeypatch.setattr(bcs, "get_db", lambda: None)
    assert list_recent_events() == []
