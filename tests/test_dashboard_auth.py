"""The dashboard's login gate — specifically that it fails CLOSED.

Separate from tests/test_auth.py, which covers Kalshi API request signing.
"""

from __future__ import annotations

import os

import pytest

from dashboard.app import app


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setitem(app.config, "TESTING", True)
    return app.test_client()


def _set_passcodes(monkeypatch, value: str) -> None:
    monkeypatch.setitem(os.environ, "PASSCODES", value)


# --- fail closed -----------------------------------------------------------


@pytest.mark.parametrize("path", ["/", "/paper-trading", "/portfolio", "/status", "/backtest"])
def test_no_passcodes_configured_refuses_to_serve_any_page(client, monkeypatch, path):
    # This gate used to fail OPEN — with no PASSCODES it served every page
    # unauthenticated. That is dangerous here for an already-observed reason:
    # this project once shipped a bug where .env was silently never loaded
    # (load_dotenv lived only inside KalshiClient.from_env), which under the
    # old behaviour would have quietly made the whole dashboard public on a
    # host that scanners probe for /.env daily.
    _set_passcodes(monkeypatch, "")
    response = client.get(path)
    assert response.status_code == 503, f"{path} served without auth"
    assert b"No PASSCODES configured" in response.data


def test_whitespace_only_passcodes_also_fails_closed(client, monkeypatch):
    # " , " parses to an empty set — must not read as "gate disabled".
    _set_passcodes(monkeypatch, " , ")
    assert client.get("/paper-trading").status_code == 503


def test_static_assets_stay_reachable_so_the_login_page_can_render(client, monkeypatch):
    _set_passcodes(monkeypatch, "")
    assert client.get("/static/style.css").status_code == 200


# --- normal gating ---------------------------------------------------------


@pytest.mark.parametrize("path", ["/", "/paper-trading", "/portfolio", "/status", "/backtest"])
def test_every_route_redirects_to_login_when_unauthenticated(client, monkeypatch, path):
    _set_passcodes(monkeypatch, "123456")
    response = client.get(path)
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_correct_passcode_authenticates(client, monkeypatch):
    _set_passcodes(monkeypatch, "123456,654321")
    assert client.post("/login", data={"passcode": "654321"}).status_code == 302
    assert client.get("/status").status_code == 200


def test_wrong_passcode_is_rejected_without_a_session(client, monkeypatch):
    _set_passcodes(monkeypatch, "123456")
    response = client.post("/login", data={"passcode": "000000"})
    assert response.status_code == 200
    assert b"Incorrect passcode" in response.data
    assert client.get("/status").status_code == 302


def test_logout_clears_the_session(client, monkeypatch):
    _set_passcodes(monkeypatch, "123456")
    client.post("/login", data={"passcode": "123456"})
    assert client.get("/status").status_code == 200
    client.post("/logout")
    assert client.get("/status").status_code == 302


# --- cookie hardening ------------------------------------------------------


def test_session_cookie_is_httponly_and_samesite_lax():
    assert app.config["SESSION_COOKIE_HTTPONLY"] is True
    assert app.config["SESSION_COOKIE_SAMESITE"] == "Lax"


# --- session expiry --------------------------------------------------------


def _forged_session_cookie(age_days: float) -> str:
    """A validly-signed session cookie whose signature timestamp is backdated,
    to test expiry the way a replayed/stolen cookie would exercise it."""
    import hashlib

    import itsdangerous.timed as timed_module
    from flask.sessions import TaggedJSONSerializer
    from itsdangerous import URLSafeTimedSerializer

    serializer = URLSafeTimedSerializer(
        app.secret_key,
        salt="cookie-session",
        serializer=TaggedJSONSerializer(),
        signer_kwargs={"key_derivation": "hmac", "digest_method": hashlib.sha1},
    )
    real_time = timed_module.time.time
    timed_module.time.time = lambda: real_time() - age_days * 86400
    try:
        return serializer.dumps({"_permanent": True, "authenticated": True})
    finally:
        timed_module.time.time = real_time


def test_session_lifetime_is_five_days():
    from datetime import timedelta

    assert app.config["PERMANENT_SESSION_LIFETIME"] == timedelta(days=5)


def test_session_slides_forward_on_use_rather_than_expiring_absolutely():
    # Inactivity-based, not absolute — a near-daily user is never logged out.
    assert app.config["SESSION_REFRESH_EACH_REQUEST"] is True


@pytest.mark.parametrize("age_days", [1, 4.9])
def test_session_within_lifetime_still_authenticates(client, monkeypatch, age_days):
    _set_passcodes(monkeypatch, "123456")
    client.set_cookie("session", _forged_session_cookie(age_days), domain="localhost")
    assert client.get("/status").status_code == 200


@pytest.mark.parametrize("age_days", [5.1, 30])
def test_expired_session_is_rejected_server_side(client, monkeypatch, age_days):
    # The important half: rejection must not depend on the browser honouring
    # Expires. A cookie copied off a machine and replayed later is refused by
    # Flask's own max_age check, not merely dropped client-side.
    _set_passcodes(monkeypatch, "123456")
    client.set_cookie("session", _forged_session_cookie(age_days), domain="localhost")
    response = client.get("/status")
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]
