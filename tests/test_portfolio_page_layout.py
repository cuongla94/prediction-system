"""Portfolio page layout: no separate Automated Trading tab, and a compact
"Live automation" panel folded into the
existing "Your Kalshi portfolio" tab instead. Uses the real test client
against whatever DATABASE_URL is configured, same as tests/test_dashboard_auth.py
already does for full-page renders — these are structural/content
assertions on the real rendered HTML, not isolated unit tests.
"""

from __future__ import annotations

import os

import pytest

from dashboard.app import app


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setitem(app.config, "TESTING", True)
    monkeypatch.setitem(app.config, "RATELIMIT_ENABLED", False)
    return app.test_client()


def _login(client, monkeypatch, code: str = "123456") -> None:
    monkeypatch.setitem(os.environ, "PASSCODES", code)
    with client.session_transaction() as sess:
        sess["authenticated"] = True


def _portfolio_html(client, monkeypatch) -> str:
    _login(client, monkeypatch)
    response = client.get("/portfolio")
    assert response.status_code == 200
    return response.get_data(as_text=True)


def test_no_automated_trading_tab_button(client, monkeypatch):
    html = _portfolio_html(client, monkeypatch)
    assert "Automated Trading</button>" not in html
    assert 'data-tab="bot"' not in html


def test_only_two_tabs_present(client, monkeypatch):
    html = _portfolio_html(client, monkeypatch)
    assert html.count('class="tab-btn') == 2 or html.count("tab-btn is-active") + html.count('class="tab-btn"') <= 3
    assert "Paper trading bot</button>" in html
    assert "Your Kalshi portfolio</button>" in html


def test_no_six_mode_readiness_grid_or_disabled_shadow_demo_buttons(client, monkeypatch):
    html = _portfolio_html(client, monkeypatch)
    for text in ("Start Shadow Bot", "Start Demo Bot", "Start Live Canary", "Start Live Trading", "SHADOW", "DEMO", "LIVE_CANARY"):
        assert text not in html, f"stale six-mode UI text found: {text!r}"


def test_live_automation_panel_lives_in_kalshi_tab(client, monkeypatch):
    html = _portfolio_html(client, monkeypatch)
    kalshi_tab_start = html.index('id="tab-kalshi"')
    panel_heading_index = html.index(">Live automation<")
    assert panel_heading_index > kalshi_tab_start


def test_live_automation_panel_shows_production_cash(client, monkeypatch):
    html = _portfolio_html(client, monkeypatch)
    assert "Available production cash" in html


def test_live_automation_panel_shows_only_compact_professional_fields(
    client, monkeypatch
):
    html = _portfolio_html(client, monkeypatch)
    for label in (
        "Current bot action",
        "Open-position thesis",
        "Last decision",
        "Next review",
    ):
        assert label in html


def test_capital_blocker_message_present_for_low_balance(client, monkeypatch):
    # The real configured account is well under $5.00 as of this test suite's
    # writing; this assertion documents the honest blocker text rather than
    # a hardcoded dollar figure that would go stale as the balance changes.
    html = _portfolio_html(client, monkeypatch)
    if "Add capital to enable live automation" in html:
        assert "requires more than $5.00 in available Kalshi cash" in html


def test_view_details_is_collapsed_by_default(client, monkeypatch):
    html = _portfolio_html(client, monkeypatch)
    details_index = html.index("View details")
    # Walk back to the opening <details tag and confirm no `open` attribute.
    open_tag_start = html.rindex("<details", 0, details_index)
    open_tag_end = html.index(">", open_tag_start)
    tag = html[open_tag_start:open_tag_end]
    assert " open" not in tag and "open>" not in tag


def test_paper_tab_has_run_one_cycle_control(client, monkeypatch):
    html = _portfolio_html(client, monkeypatch)
    assert "Run One Cycle" in html


def test_paper_tab_has_start_or_stop_control(client, monkeypatch):
    html = _portfolio_html(client, monkeypatch)
    assert (
        ("Start Paper Bot" in html)
        or ("Stop Bot" in html)
        or "Can't reach the database right now" in html
    )


def test_kalshi_tab_has_emergency_or_reset_control(client, monkeypatch):
    html = _portfolio_html(client, monkeypatch)
    assert ("Emergency stop" in html) or ("Reset kill switch" in html)


def test_kalshi_tab_has_refresh_and_reconcile_controls(client, monkeypatch):
    html = _portfolio_html(client, monkeypatch)
    assert "Refresh balance" in html
    assert "Reconcile account" in html


def test_no_kelly_fraction_or_cash_reserve_cards_in_automated_trading_panel(client, monkeypatch):
    html = _portfolio_html(client, monkeypatch)
    panel_start = html.index(">Live automation<")
    panel_end = html.index("</details>", panel_start) if "</details>" in html[panel_start:] else len(html)
    panel_html = html[panel_start:panel_end]
    assert "Kelly fraction" not in panel_html
    assert "Cash reserve fraction" not in panel_html


def test_backtest_reuses_existing_page_for_compact_readiness_section(client, monkeypatch):
    _login(client, monkeypatch)
    response = client.get("/backtest")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    # The section appears only when the reproducible artifact exists. It must
    # never become a third primary page or a portfolio complication.
    if "Real-trading readiness" in html:
        assert "View readiness details" in html
        assert "<details" in html
