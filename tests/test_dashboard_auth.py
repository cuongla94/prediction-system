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
