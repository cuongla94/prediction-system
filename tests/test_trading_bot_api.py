"""The /api/trading-bot/* control surface: authentication, CSRF, and mode
validation. Mutating endpoints are tested with bot_control's real functions
monkeypatched out (dashboard.app's imported names) so these tests never
write to the real bot_control_events table — same discipline as this
project's other unit tests, which fake the persistence layer rather than
touch live infrastructure. Read-only GET /status is left hitting whatever
DATABASE_URL is configured, same as tests/test_dashboard_auth.py already
does for GET /status (the HTML page) — get_bot_state() fails closed and
returns a safe default when no database is reachable.
"""

from __future__ import annotations

import os

import pytest

from bot_control import BotControlError, BotState
from dashboard.app import app

_SAFE_STATE = BotState(
    effective_mode="OFF",
    enabled=False,
    kill_switch=False,
    kill_switch_reason=None,
    strategy_name="weather-daily-temp",
    strategy_version="v1-2026-07-23",
    updated_at=None,
    actor=None,
)

_MUTATING_ENDPOINTS = [
    "/api/trading-bot/start",
    "/api/trading-bot/stop",
    "/api/trading-bot/run-once",
    "/api/trading-bot/reconcile",
    "/api/trading-bot/refresh-balance",
    "/api/trading-bot/kill",
    "/api/trading-bot/reset-kill-switch",
    "/api/trading-bot/live/enable",
    "/api/trading-bot/live/preflight",
    "/api/trading-bot/live/disable",
    "/api/trading-bot/emergency-stop",
]


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setitem(app.config, "TESTING", True)
    monkeypatch.setitem(app.config, "RATELIMIT_ENABLED", False)
    return app.test_client()


def _set_passcodes(monkeypatch, value: str) -> None:
    monkeypatch.setitem(os.environ, "PASSCODES", value)


def _login(client, monkeypatch, code: str = "123456") -> None:
    # Sets the session directly rather than POSTing /login, deliberately —
    # /login is rate-limited (10/min via Flask-Limiter against real Redis
    # storage shared across this whole process), and this file's many
    # parametrized tests each need their own authenticated session. Setting
    # RATELIMIT_ENABLED=False in the fixture covers /login itself if a test
    # calls it directly, but doesn't change that a real POST there is
    # unnecessary work here — this file only cares about what happens AFTER
    # authentication. _set_passcodes still runs so _require_login's "no
    # PASSCODES configured" fail-closed branch doesn't trigger.
    _set_passcodes(monkeypatch, code)
    with client.session_transaction() as sess:
        sess["authenticated"] = True


def _with_csrf(client) -> str:
    with client.session_transaction() as sess:
        sess["csrf_token"] = "test-token"
    return "test-token"


def _stub_status_dependencies(monkeypatch, state: BotState = _SAFE_STATE) -> None:
    """Avoids any real database access from _bot_status_payload() (which
    every mutating endpoint returns in its response body)."""
    monkeypatch.setattr("dashboard.app.get_bot_state", lambda: state)
    monkeypatch.setattr("dashboard.app.list_recent_events", lambda limit=50: [])


# --- authentication ----------------------------------------------------------


@pytest.mark.parametrize("path", _MUTATING_ENDPOINTS)
def test_mutating_endpoints_require_authentication(client, monkeypatch, path):
    _set_passcodes(monkeypatch, "123456")
    response = client.post(path)
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_status_endpoint_requires_authentication(client, monkeypatch):
    _set_passcodes(monkeypatch, "123456")
    assert client.get("/api/trading-bot/status").status_code == 302


def test_activity_endpoint_requires_authentication(client, monkeypatch):
    _set_passcodes(monkeypatch, "123456")
    assert client.get("/api/trading-bot/activity").status_code == 302


# --- CSRF ---------------------------------------------------------------------


@pytest.mark.parametrize("path", _MUTATING_ENDPOINTS)
def test_mutating_endpoints_reject_missing_csrf_token(client, monkeypatch, path):
    _login(client, monkeypatch)
    _stub_status_dependencies(monkeypatch)
    response = client.post(path)
    assert response.status_code == 403


@pytest.mark.parametrize("path", _MUTATING_ENDPOINTS)
def test_mutating_endpoints_reject_wrong_csrf_token(client, monkeypatch, path):
    _login(client, monkeypatch)
    _with_csrf(client)
    _stub_status_dependencies(monkeypatch)
    response = client.post(path, headers={"X-CSRF-Token": "wrong-token"})
    assert response.status_code == 403


# --- start: mode validation ---------------------------------------------------


def test_start_paper_succeeds_and_returns_effective_mode(client, monkeypatch):
    _login(client, monkeypatch)
    token = _with_csrf(client)
    paper_state = BotState(
        effective_mode="PAPER", enabled=True, kill_switch=False, kill_switch_reason=None,
        strategy_name="weather-daily-temp", strategy_version="v1", updated_at=None, actor="test",
    )
    monkeypatch.setattr("dashboard.app.start_bot", lambda *a, **kw: paper_state)
    _stub_status_dependencies(monkeypatch, paper_state)

    response = client.post(
        "/api/trading-bot/start", json={"mode": "PAPER"}, headers={"X-CSRF-Token": token}
    )
    assert response.status_code == 200
    data = response.get_json()
    assert data["effective_mode"] == "PAPER"
    assert data["enabled"] is True


@pytest.mark.parametrize("mode", ["SHADOW", "DEMO", "LIVE_CANARY", "LIVE"])
def test_start_unimplemented_modes_return_501(client, monkeypatch, mode):
    _login(client, monkeypatch)
    token = _with_csrf(client)

    def fake_start_bot(requested_mode, **kwargs):
        raise BotControlError("NOT_IMPLEMENTED", f"{requested_mode} has no execution path implemented.")

    monkeypatch.setattr("dashboard.app.start_bot", fake_start_bot)
    _stub_status_dependencies(monkeypatch)

    response = client.post(
        "/api/trading-bot/start", json={"mode": mode}, headers={"X-CSRF-Token": token}
    )
    assert response.status_code == 501
    assert response.get_json()["reason_code"] == "NOT_IMPLEMENTED"


def test_start_rejects_unrecognized_mode_string_without_calling_start_bot(client, monkeypatch):
    _login(client, monkeypatch)
    token = _with_csrf(client)

    def fail_if_called(*a, **kw):
        raise AssertionError("start_bot should not be called for an invalid mode string")

    monkeypatch.setattr("dashboard.app.start_bot", fail_if_called)

    response = client.post(
        "/api/trading-bot/start", json={"mode": "NOT_A_REAL_MODE"}, headers={"X-CSRF-Token": token}
    )
    assert response.status_code == 400


def test_start_kill_switch_active_returns_409(client, monkeypatch):
    _login(client, monkeypatch)
    token = _with_csrf(client)

    def fake_start_bot(*a, **kw):
        raise BotControlError("KILL_SWITCH_ACTIVE", "The kill switch is active.")

    monkeypatch.setattr("dashboard.app.start_bot", fake_start_bot)
    _stub_status_dependencies(monkeypatch)

    response = client.post(
        "/api/trading-bot/start", json={"mode": "PAPER"}, headers={"X-CSRF-Token": token}
    )
    assert response.status_code == 409


# --- other mutating endpoints, stubbed --------------------------------------


def test_stop_bot_returns_status(client, monkeypatch):
    _login(client, monkeypatch)
    token = _with_csrf(client)
    monkeypatch.setattr("dashboard.app.stop_bot", lambda **kw: _SAFE_STATE)
    _stub_status_dependencies(monkeypatch)
    response = client.post("/api/trading-bot/stop", headers={"X-CSRF-Token": token})
    assert response.status_code == 200


def test_kill_switch_response_notes_no_resting_orders_for_paper(client, monkeypatch):
    _login(client, monkeypatch)
    token = _with_csrf(client)
    killed_state = BotState(
        effective_mode="OFF", enabled=False, kill_switch=True, kill_switch_reason="test",
        strategy_name="weather-daily-temp", strategy_version="v1", updated_at=None, actor="test",
    )
    monkeypatch.setattr("dashboard.app.trigger_kill_switch", lambda reason, **kw: killed_state)
    _stub_status_dependencies(monkeypatch, killed_state)
    response = client.post("/api/trading-bot/kill", headers={"X-CSRF-Token": token})
    assert response.status_code == 200
    data = response.get_json()
    assert data["cancelled_orders"] == 0
    assert data["kill_switch"] is True


def test_reset_kill_switch_returns_status(client, monkeypatch):
    _login(client, monkeypatch)
    token = _with_csrf(client)
    monkeypatch.setattr("dashboard.app.reset_kill_switch", lambda **kw: _SAFE_STATE)
    _stub_status_dependencies(monkeypatch)
    response = client.post("/api/trading-bot/reset-kill-switch", headers={"X-CSRF-Token": token})
    assert response.status_code == 200


# --- status/activity: real (read-only) data ---------------------------------


def test_status_endpoint_returns_expected_shape_when_authenticated(client, monkeypatch):
    _login(client, monkeypatch)
    response = client.get("/api/trading-bot/status")
    assert response.status_code == 200
    data = response.get_json()
    for key in (
        "live_enabled", "status", "available_cash", "capital_eligible", "blockers",
        "primary_blocker", "last_successful_cycle", "bot_open_exposure",
        "daily_bot_realized_pnl", "active_bot_orders", "kill_switch",
        "fixed_risk_limits", "strategy_validation_status",
    ):
        assert key in data, f"missing key: {key}"


def test_status_response_never_contains_kalshi_credentials(client, monkeypatch):
    _login(client, monkeypatch)
    response = client.get("/api/trading-bot/status")
    body = response.get_data(as_text=True)
    assert "PRIVATE KEY" not in body
    assert "KALSHI_API_KEY_ID" not in body


def test_activity_endpoint_returns_events_list(client, monkeypatch):
    _login(client, monkeypatch)
    monkeypatch.setattr("dashboard.app.list_recent_events", lambda limit=50: [])
    response = client.get("/api/trading-bot/activity")
    assert response.status_code == 200
    assert response.get_json() == {"events": []}


def test_live_enable_requires_exact_confirmation(client, monkeypatch):
    _login(client, monkeypatch)
    token = _with_csrf(client)
    _stub_status_dependencies(monkeypatch)
    response = client.post(
        "/api/trading-bot/live/enable",
        json={"confirmation": "enable live trading"},
        headers={"X-CSRF-Token": token},
    )
    assert response.status_code == 400
    assert response.get_json()["reason_code"] == "CONFIRMATION_REQUIRED"


def test_live_disable_persists_backend_state(client, monkeypatch):
    _login(client, monkeypatch)
    token = _with_csrf(client)
    calls = []
    monkeypatch.setattr("dashboard.app.disable_live", lambda **kwargs: calls.append(kwargs))
    _stub_status_dependencies(monkeypatch)
    response = client.post(
        "/api/trading-bot/live/disable",
        headers={"X-CSRF-Token": token},
    )
    assert response.status_code == 200
    assert calls and calls[0]["actor"]
