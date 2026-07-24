from __future__ import annotations

import json
import os
import secrets
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv
from flask import Flask, abort, jsonify, redirect, render_template, request, session, url_for
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

import bot_control
from edge.calculator import bracket_sum_deviation
from kalshi_client import KalshiAuthError, KalshiClient, Position, Settlement, market_url, parse_event_date
from backtest.calibration import trade_stats
from bot_control import (
    ALL_MODES,
    BotControlError,
    disable_live,
    enable_live,
    get_bot_state,
    list_recent_events,
    record_reconcile,
    record_refresh_balance,
    record_run_once,
    reset_kill_switch,
    start_bot,
    stop_bot,
    trigger_kill_switch,
)
from capital.eligibility import evaluate_capital_eligibility
from db.connection import get_db as get_core_db
from live_trading.repository import PostgresLiveRepository
from live_trading.risk import fixed_limits_dict
from live_trading.service import build_live_service
from paper_trading import (
    STARTING_BANKROLL_USD,
    STRATEGY_VERSION,
    cash_reserve_fraction_setting,
    deployable_cash,
)
from price_feed.cache import get_cached_prices
from sizing.kelly import (
    BracketInput,
    SizeRecommendation,
    kelly_fraction_setting,
    max_event_exposure_setting,
    size_event,
)
from monitoring import comparable_trend, summarize
from monitoring.trend import REVISIT_STREAK
from validation.summary import build_validation_summary
from weather.calibration_override import load_override, override_metadata
from weather.calibration_params import CALIBRATION, get_calibration
from weather.nws_observations import fetch_today_extreme
from weather.stations import STATIONS, get_station

from .alert import Alert, ForecastPreview
from .db import get_alerts, get_forecast_previews

# Module-level, not just in __main__: a production WSGI server imports this
# module directly rather than running the __main__ block below, and still
# needs DATABASE_URL loaded before the first request comes in.
load_dotenv()

app = Flask(__name__)
# Falls back to a fresh random key if unset, rather than a hardcoded default —
# a shared, predictable secret would let anyone forge a session cookie. The
# real cost of no FLASK_SECRET_KEY in .env is just that every process restart
# invalidates existing sessions, not a security hole.
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or os.urandom(32)
# Sessions expire after 5 days of INACTIVITY, not 5 days absolute: Flask's
# SESSION_REFRESH_EACH_REQUEST (on by default for permanent sessions) re-issues
# the cookie on every request, so the window slides forward while the dashboard
# is actually being used. Checking in even once every few days never logs you
# out; walking away for most of a week does.
#
# Enforced on BOTH sides, which is the part that matters — the cookie carries an
# Expires so the browser drops it, and Flask independently passes this as
# max_age when unsealing, so a cookie replayed past its lifetime (copied off a
# machine, restored from a backup) is rejected server-side rather than trusted.
# Verified by replaying forged cookies at 1 / 2.9 / 3.1 / 10 days against the
# previous 3-day setting: the first two authenticated, the last two did not.
#
# 5 days is a judgement call inside the 3-7 day band: long enough not to nag a
# near-daily user, short enough that a session forgotten on a borrowed or lost
# device does not stay valid for weeks. Paired with rate limiting on the login
# form (see limiter setup below) to cap brute-force attacks.
app.permanent_session_lifetime = timedelta(days=5)
# Lax, not None: the session cookie shouldn't ride along on cross-site requests.
# Live-control actions use a session-bound CSRF token as well; Lax provides an
# additional browser-level boundary without interfering with normal navigation.
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
# SESSION_COOKIE_SECURE stays False deliberately: the droplet serves plain HTTP
# (no domain, so no Let's Encrypt cert), and setting Secure would stop the
# cookie being sent at all, breaking login entirely. This is a symptom of the
# no-TLS posture, not an independent choice — flip it the moment TLS lands.

# Rate limiting on the login route to prevent brute-force attacks. Uses Redis
# (already live for other purposes) as the storage backend. Initialized with
# default in-memory fallback if Redis is unavailable. The actual throttle is the
# explicit `@limiter.limit("10 per minute")` on the login POST below — 10 attempts
# per minute per IP, allowing normal login retries while blocking rapid automated
# attempts. Updated 2026-07-21 when nginx basic auth was removed — that layer is
# no longer in front of the login form, so app-level throttling is now essential.
#
# NO `default_limits` here, deliberately: a global cap applies to EVERY route,
# including the read-only dashboard pages and the in-place refresh calls they
# make on a timer. A single user leaving /paper-trading open would silently
# exhaust "200 per day" / "50 per hour" and get served flask-limiter's own
# "Too Many Requests / 200 per 1 day" error on their own dashboard (observed
# live 2026-07-22). Brute-force protection belongs only on the login route,
# which has it explicitly — the rest of the dashboard is behind the passcode
# gate and reading it more than 200x/day is normal, not an attack.
# Tests disable rate limiting via app.config["RATELIMIT_ENABLED"] to avoid
# inter-test interference from shared IP address context.
try:
    import redis
    redis_client = redis.from_url(os.environ.get("REDIS_URL", "redis://localhost"))
    limiter = Limiter(
        app=app,
        key_func=get_remote_address,
        storage_uri=os.environ.get("REDIS_URL", "redis://localhost"),
        in_memory_fallback_enabled=True,
    )
except Exception:
    # Fallback: in-memory storage if Redis is unavailable. This is degraded but
    # functional for single-process dev/test; production should ensure Redis is up.
    limiter = Limiter(
        app=app,
        key_func=get_remote_address,
        in_memory_fallback_enabled=True,
    )


def _valid_passcodes() -> set[str]:
    raw = os.environ.get("PASSCODES", "")
    return {code.strip() for code in raw.split(",") if code.strip()}


@app.before_request
def _require_login():
    if request.endpoint in ("login", "static"):
        return None

    # Fail CLOSED when no passcode is configured. This used to return None —
    # i.e. serve every page unauthenticated — on the reasoning that the
    # operator hadn't opted into the gate. That is a bad default here for a
    # specific, already-observed reason: this project has previously shipped a
    # bug where `.env` was silently never loaded at all (load_dotenv was only
    # called inside KalshiClient.from_env, so every other entry point read an
    # empty environment). Under the old behaviour that bug would have quietly
    # turned the whole dashboard public, on a box that opportunistic scanners
    # probe for /.env and /.git/HEAD daily, with nothing visible to say so.
    #
    # Failing closed makes that failure loud and harmless instead: the login
    # page is served and nothing authenticates, which is recoverable over SSH.
    if not _valid_passcodes():
        return render_template(
            "login.html",
            error="No PASSCODES configured — refusing to serve unauthenticated. Check the .env on this host.",
        ), 503

    if session.get("authenticated"):
        return None
    return redirect(url_for("login", next=request.path))


@app.route("/login", methods=["GET", "POST"])
@limiter.limit("10 per minute", methods=["POST"])
def login():
    error = None
    if request.method == "POST":
        code = (request.form.get("passcode") or "").strip()
        if code and code in _valid_passcodes():
            session.permanent = True
            session["authenticated"] = True
            return redirect(request.args.get("next") or url_for("index"))
        error = "Incorrect passcode."
    return render_template("login.html", error=error)


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---- Trading-bot control API -----------------------------------------------
#
# The generic mode API remains PAPER-only so an old `start(mode=LIVE)` call
# cannot bypass production checks. The dedicated live preflight/enable/disable
# routes below are the sole path into persisted production execution.


def _actor() -> str:
    # No per-user identity beyond "holds a valid passcode" exists in this
    # single-shared-login dashboard — remote_addr is the closest thing to a
    # traceable actor, consistent with the rate limiter's own key_func above.
    return f"dashboard:{request.remote_addr}"


def _kalshi_environment() -> str:
    configured = os.environ.get("KALSHI_ENVIRONMENT", "production").strip().lower()
    if configured in {"demo", "sandbox"}:
        return "demo"
    base_url = (
        os.environ.get("KALSHI_BASE_URL")
        or os.environ.get("KALSHI_PRODUCTION_BASE_URL")
        or ""
    )
    if (
        "external-api.kalshi.com" in base_url
        or "api.elections.kalshi.com" in base_url
        or not base_url
    ):
        return "prod"
    if "demo-api.kalshi.co" in base_url:
        return "demo"
    return "unknown"


def _csrf_token() -> str:
    """Session-bound CSRF token for /api/trading-bot/* mutations. No
    flask-wtf/CSRF middleware is installed in this project (the CSRF surface
    was small enough to skip one before this endpoint set existed — see the
    old comment on SESSION_COOKIE_SAMESITE above); this is a minimal
    same-origin anti-forgery control built for this one write surface
    instead of a new dependency. See DECISIONS.md's "Production trading
    dashboard security" decision.
    """
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


app.jinja_env.globals["csrf_token"] = _csrf_token


def _require_csrf() -> None:
    expected = session.get("csrf_token")
    provided = request.headers.get("X-CSRF-Token")
    if not expected or not provided or not secrets.compare_digest(expected, provided):
        abort(403, description="Missing or invalid CSRF token.")


_BOT_ERROR_STATUS_CODES = {
    "INVALID_MODE": 400,
    "NOT_IMPLEMENTED": 501,
    "KILL_SWITCH_ACTIVE": 409,
    "DATABASE_UNAVAILABLE": 503,
    "DATABASE_ERROR": 500,
}


def _latest_balance_snapshot() -> tuple[Decimal | None, datetime | None]:
    """Most recent successfully persisted Refresh Balance result, or
    (None, None) if one has never run. Deliberately does NOT hit Kalshi's
    API on every status poll — a live fetch happens only when the operator
    explicitly presses Refresh Balance (or Reconcile), same
    don't-hammer-the-read-API discipline as this project's price-fetch
    batching elsewhere."""
    for event in list_recent_events(limit=50):
        if event.get("event_type") == "refresh_balance" and event.get("detail"):
            try:
                payload = json.loads(event["detail"])
                as_of = datetime.fromisoformat(payload["as_of"]) if payload.get("as_of") else None
                return Decimal(payload["available_cash"]), as_of
            except (KeyError, ValueError, TypeError):
                continue
    return None, None


def _capital_eligibility_snapshot() -> dict:
    available_cash, as_of = _latest_balance_snapshot()
    return evaluate_capital_eligibility(
        environment=_kalshi_environment(), available_cash=available_cash, balance_as_of=as_of,
    ).to_dict()


def _live_persistence_snapshot() -> dict:
    defaults = {
        "active_bot_orders": 0,
        "bot_open_exposure": Decimal("0"),
        "daily_bot_realized_pnl": Decimal("0"),
        "last_cycle_at": None,
        "last_cycle_status": None,
        "last_execution_error": None,
        "last_reconciliation": None,
        "reconciliation_healthy": False,
        "available_cash": None,
        "latest_weather_data": None,
        "latest_market_data": None,
        "worker_healthy": False,
        "recent_live_orders": [],
    }
    connection = get_core_db()
    if connection is None:
        return defaults
    try:
        repository = PostgresLiveRepository(connection)
        summary = repository.status_summary()
        reconciliation = repository.latest_reconciliation()
        timestamps = repository.readiness_timestamps()
        recent_orders = repository.recent_orders(limit=12)
        for order in recent_orders:
            for key, value in list(order.items()):
                if isinstance(value, datetime):
                    order[key] = value.isoformat()
                elif isinstance(value, Decimal):
                    order[key] = str(value)
        return {
            **defaults,
            **summary,
            "last_reconciliation": reconciliation["finished_at"] if reconciliation else None,
            "reconciliation_healthy": bool(reconciliation and reconciliation["healthy"]),
            "available_cash": reconciliation["available_cash"] if reconciliation else None,
            "latest_weather_data": timestamps["weather_data_at"],
            "latest_market_data": timestamps["market_data_at"],
            "worker_healthy": repository.worker_healthy(),
            "recent_live_orders": recent_orders,
        }
    except Exception:
        return defaults
    finally:
        connection.close()


def _bot_status_payload() -> dict:
    state = get_bot_state()
    live = _live_persistence_snapshot()
    available_cash = live["available_cash"]
    balance_as_of = live["last_reconciliation"]
    if available_cash is None:
        available_cash, balance_as_of = _latest_balance_snapshot()
    eligibility = evaluate_capital_eligibility(
        environment=_kalshi_environment(),
        available_cash=available_cash,
        balance_as_of=balance_as_of,
        reconciliation_healthy=live["reconciliation_healthy"],
    ).to_dict()
    blockers: list[str] = []
    if not eligibility["eligible"]:
        blockers.append(eligibility["reason_code"])
    if state.kill_switch:
        blockers.append("KILL_SWITCH_ACTIVE")
    if not live["reconciliation_healthy"]:
        blockers.append("RECONCILIATION_REQUIRED")
    if not live["worker_healthy"]:
        blockers.append("WORKER_UNHEALTHY")
    now = datetime.now(UTC)
    if (
        live["latest_weather_data"] is None
        or now - live["latest_weather_data"] > timedelta(minutes=30)
    ):
        blockers.append("STALE_WEATHER_DATA")
    if (
        live["latest_market_data"] is None
        or now - live["latest_market_data"] > timedelta(minutes=30)
    ):
        blockers.append("STALE_KALSHI_QUOTES")
    if live["last_execution_error"]:
        status = "ERROR"
    elif blockers:
        status = "BLOCKED"
    elif state.live_enabled:
        status = "RUNNING"
    else:
        status = "STOPPED"
    return dict(
        live_enabled=state.live_enabled,
        status=status,
        available_cash=eligibility["available_cash"],
        capital_eligible=eligibility["eligible"],
        blockers=blockers,
        primary_blocker=blockers[0] if blockers else None,
        last_successful_cycle=(
            live["last_cycle_at"] if live["last_cycle_status"] == "RUNNING" else None
        ),
        bot_open_exposure=str(live["bot_open_exposure"]),
        daily_bot_realized_pnl=str(live["daily_bot_realized_pnl"]),
        active_bot_orders=live["active_bot_orders"],
        kill_switch=state.kill_switch,
        kill_switch_reason=state.kill_switch_reason,
        last_reconciliation=live["last_reconciliation"],
        latest_weather_data=live["latest_weather_data"],
        latest_market_data=live["latest_market_data"],
        worker_healthy=live["worker_healthy"],
        recent_live_orders=live["recent_live_orders"],
        most_recent_execution_error=live["last_execution_error"],
        fixed_risk_limits=fixed_limits_dict(),
        # Compatibility fields used by the paper-control strip.
        requested_mode=state.effective_mode,
        effective_mode=state.effective_mode,
        environment=_kalshi_environment(),
        enabled=state.enabled,
        strategy_name=state.strategy_name or bot_control.STRATEGY_NAME,
        strategy_version=state.strategy_version or STRATEGY_VERSION,
        strategy_validation_status="FAILED",
        capital_eligibility=eligibility,
        production_activation_status=status,
        updated_at=state.updated_at.isoformat() if state.updated_at else None,
        actor=state.actor,
        active_risk_limits=fixed_limits_dict(),
    )


@app.route("/api/trading-bot/status", methods=["GET"])
def api_bot_status():
    return jsonify(_bot_status_payload())


@app.route("/api/trading-bot/activity", methods=["GET"])
def api_bot_activity():
    events = list_recent_events(limit=50)
    for event in events:
        if event.get("created_at"):
            event["created_at"] = event["created_at"].isoformat()
    return jsonify(events=events)


@app.route("/api/trading-bot/start", methods=["POST"])
def api_bot_start():
    _require_csrf()
    payload = request.get_json(silent=True) or {}
    requested_mode = (payload.get("mode") or "").strip().upper()
    if requested_mode not in ALL_MODES:
        return jsonify(error=f"mode must be one of {sorted(ALL_MODES)}", reason_code="INVALID_MODE"), 400
    try:
        start_bot(requested_mode, actor=_actor(), note=payload.get("note"))
    except BotControlError as exc:
        status_code = _BOT_ERROR_STATUS_CODES.get(exc.reason_code, 400)
        return jsonify(error=str(exc), reason_code=exc.reason_code, **_bot_status_payload()), status_code
    return jsonify(_bot_status_payload())


@app.route("/api/trading-bot/stop", methods=["POST"])
def api_bot_stop():
    _require_csrf()
    stop_bot(actor=_actor())
    return jsonify(_bot_status_payload())


@app.route("/api/trading-bot/run-once", methods=["POST"])
def api_bot_run_once():
    _require_csrf()
    state = get_bot_state()
    try:
        if state.live_enabled:
            built = build_live_service()
            if built is None:
                return jsonify(error="Live database is unavailable."), 503
            service, connection = built
            try:
                result = service.run_cycle(state)
            finally:
                service.client.close()
                connection.close()
            if result.status == "ERROR":
                return jsonify(error=result.error, **_bot_status_payload()), 500
        else:
            import scripts.run_paper_trading as run_paper_trading

            run_paper_trading.main()
    except Exception as exc:
        return jsonify(error=f"Cycle failed: {exc.__class__.__name__}: {exc}"), 500
    record_run_once(actor=_actor(), detail="Manual one-off cycle triggered from the dashboard.")
    return jsonify(_bot_status_payload())


@app.route("/api/trading-bot/reconcile", methods=["POST"])
def api_bot_reconcile():
    _require_csrf()
    built = build_live_service()
    if built is None:
        return jsonify(error="Could not reconcile: database unavailable."), 503
    service, connection = built
    try:
        result = service.reconcile(actor=_actor())
    except Exception as exc:
        return jsonify(error=f"Could not reconcile: {exc.__class__.__name__}: {exc}"), 502
    finally:
        service.client.close()
        connection.close()
    detail = (
        f"{result.reconciled_orders} bot order(s), {result.fills} fill(s), "
        f"{result.positions} position(s), {result.settlements} settlement(s); "
        f"healthy={result.healthy}."
    )
    record_reconcile(actor=_actor(), detail=detail)
    return jsonify(_bot_status_payload())


@app.route("/api/trading-bot/refresh-balance", methods=["POST"])
def api_bot_refresh_balance():
    _require_csrf()
    if _kalshi_environment() != "prod":
        return jsonify(error="Production balance requires the production Kalshi environment."), 409
    try:
        with KalshiClient.from_env() as client:
            balance = client.get_balance()
    except Exception as exc:
        return jsonify(error=f"Could not fetch balance: {exc.__class__.__name__}: {exc}"), 502
    detail = json.dumps({
        "available_cash": str(balance.available_dollars),
        "as_of": balance.as_of.isoformat() if balance.as_of else None,
    })
    record_refresh_balance(actor=_actor(), detail=detail)
    return jsonify(_bot_status_payload())


@app.route("/api/trading-bot/emergency-stop", methods=["POST"])
@app.route("/api/trading-bot/kill", methods=["POST"])
def api_bot_kill():
    _require_csrf()
    payload = request.get_json(silent=True) or {}
    reason = (payload.get("reason") or "manual emergency stop from the dashboard").strip()
    trigger_kill_switch(reason, actor=_actor())
    cancelled_orders = 0
    cancellation_error = None
    built = build_live_service()
    if built is not None:
        service, connection = built
        try:
            cancelled_orders = service.cancel_bot_orders(force=True, actor=_actor())
        except Exception as exc:
            cancellation_error = f"{exc.__class__.__name__}: {exc}"
        finally:
            service.client.close()
            connection.close()
    result = _bot_status_payload()
    result["cancelled_orders"] = cancelled_orders
    result["cancelled_orders_note"] = (
        "Only persisted bot-owned resting orders were eligible for cancellation; "
        "manual Kalshi orders were not touched."
    )
    if cancellation_error:
        result["cancellation_error"] = cancellation_error
    return jsonify(result)


@app.route("/api/trading-bot/live/enable", methods=["POST"])
def api_bot_live_enable():
    _require_csrf()
    payload = request.get_json(silent=True) or {}
    if payload.get("confirmation") != "ENABLE LIVE TRADING":
        return jsonify(
            error="Type exactly ENABLE LIVE TRADING to confirm.",
            reason_code="CONFIRMATION_REQUIRED",
            **_bot_status_payload(),
        ), 400
    if _kalshi_environment() != "prod":
        return jsonify(
            error="Live automation is production-only.",
            reason_code="PRODUCTION_ENVIRONMENT_REQUIRED",
            **_bot_status_payload(),
        ), 409
    built = build_live_service()
    if built is None:
        return jsonify(error="Live database is unavailable."), 503
    service, connection = built
    try:
        reconciliation = service.reconcile(actor=_actor())
        blockers = service.enablement_blockers(get_bot_state(), reconciliation)
        if blockers:
            response = _bot_status_payload()
            response.update(
                error="Live automation is blocked.",
                reason_code=blockers[0],
                blockers=blockers,
            )
            return jsonify(response), 409
        enable_live(
            actor=_actor(),
            strategy_version=STRATEGY_VERSION,
            note=(
                "Owner confirmed ENABLE LIVE TRADING after fresh balance, "
                "reconciliation, credentials, data, exchange, and worker checks."
            ),
        )
    except BotControlError as exc:
        return jsonify(error=str(exc), reason_code=exc.reason_code), 409
    except Exception as exc:
        return jsonify(error=f"Could not enable live automation: {exc.__class__.__name__}: {exc}"), 502
    finally:
        service.client.close()
        connection.close()
    return jsonify(_bot_status_payload())


@app.route("/api/trading-bot/live/preflight", methods=["POST"])
def api_bot_live_preflight():
    """Fresh, backend-only readiness check performed before showing the modal."""
    _require_csrf()
    if _kalshi_environment() != "prod":
        return jsonify(
            error="Live automation is production-only.",
            reason_code="PRODUCTION_ENVIRONMENT_REQUIRED",
        ), 409
    built = build_live_service()
    if built is None:
        return jsonify(error="Live database is unavailable."), 503
    service, connection = built
    try:
        reconciliation = service.reconcile(actor=_actor())
        record_reconcile(
            actor=_actor(),
            detail=(
                "Live preflight: "
                f"healthy={reconciliation.healthy}, cash={reconciliation.available_cash}."
            ),
        )
        blockers = service.enablement_blockers(get_bot_state(), reconciliation)
        response = _bot_status_payload()
        response["available_cash"] = str(reconciliation.available_cash)
        response["blockers"] = blockers
        response["primary_blocker"] = blockers[0] if blockers else None
        if blockers:
            response.update(error="Live automation is blocked.", reason_code=blockers[0])
            return jsonify(response), 409
        return jsonify(response)
    except Exception as exc:
        return jsonify(error=f"Live preflight failed: {exc.__class__.__name__}: {exc}"), 502
    finally:
        service.client.close()
        connection.close()


@app.route("/api/trading-bot/live/disable", methods=["POST"])
def api_bot_live_disable():
    _require_csrf()
    disable_live(
        actor=_actor(),
        note="Live automation disabled; reconciliation and settlement processing continue.",
    )
    return jsonify(_bot_status_payload())


@app.route("/api/trading-bot/orders", methods=["GET"])
def api_bot_orders():
    connection = get_core_db()
    if connection is None:
        return jsonify(orders=[], error="Database unavailable."), 503
    try:
        orders = PostgresLiveRepository(connection).recent_orders(limit=100)
        for order in orders:
            for key, value in list(order.items()):
                if isinstance(value, (datetime, Decimal)):
                    order[key] = value.isoformat() if isinstance(value, datetime) else str(value)
        return jsonify(orders=orders)
    finally:
        connection.close()


@app.route("/api/trading-bot/reset-kill-switch", methods=["POST"])
def api_bot_reset_kill_switch():
    _require_csrf()
    reset_kill_switch(actor=_actor())
    return jsonify(_bot_status_payload())


@dataclass(frozen=True)
class EventGroup:
    """One event's full bracket ladder, grouped together rather than scattered
    across the page by edge size — same-day/same-city brackets are correlated
    (all bets on one underlying temperature), not independent picks, and
    showing them apart invites reading them as a menu to choose from."""

    event_ticker: str
    city: str
    date_label: str
    alerts: list[Alert]
    actionable_count: int
    sum_deviation: float
    sizing: dict[str, SizeRecommendation]
    close_time: str | None
    trading_status: str

    @property
    def max_abs_edge(self) -> float:
        return max(abs(a.edge) for a in self.alerts)

    @property
    def total_recommended_fraction(self) -> float:
        return sum(r.recommended_fraction for r in self.sizing.values())

    @property
    def kalshi_url(self) -> str:
        # Every bracket in an event links to the same event-level ladder page
        # on Kalshi (kalshi_client.market_url takes an event_ticker, not a
        # per-market one) — confirmed by reading generate_alerts.py rather
        # than assumed, since a wrong assumption here would silently point a
        # "Trade" button at the wrong market.
        return self.alerts[0].kalshi_url

    @property
    def metric_label(self) -> str:
        return self.alerts[0].metric_label

    @property
    def lead_days(self) -> int | None:
        return self.alerts[0].lead_days

    @property
    def is_same_day(self) -> bool:
        return self.alerts[0].is_same_day

    @property
    def category(self) -> str:
        return self.alerts[0].category

    @property
    def category_label(self) -> str:
        return self.alerts[0].category_label


def _group_by_event(alerts: list[Alert]) -> list[EventGroup]:
    by_event: dict[str, list[Alert]] = defaultdict(list)
    for alert in alerts:
        by_event[alert.event_ticker].append(alert)

    kelly_fraction = kelly_fraction_setting()
    max_event_exposure = max_event_exposure_setting()

    groups = []
    for event_ticker, event_alerts in by_event.items():
        # Ladder order (matches Kalshi's own bracket display), not edge size —
        # None (an open-ended tail bracket's missing floor/cap) sorts first.
        event_alerts.sort(key=lambda a: a.floor_strike if a.floor_strike is not None else float("-inf"))
        try:
            date_label = parse_event_date(event_ticker).strftime("%b %-d, %Y")
        except ValueError:
            date_label = event_ticker
        sizing = size_event(
            [
                BracketInput(
                    a.market_ticker, a.model_probability, a.market_yes_price, a.side, a.is_actionable
                )
                for a in event_alerts
            ],
            kelly_fraction=kelly_fraction,
            max_event_exposure=max_event_exposure,
        )
        close_times = [a.close_time for a in event_alerts if a.close_time is not None]
        # Every bracket in one event closes at essentially the same time
        # (Kalshi closes the whole day's ladder together), but take the
        # earliest if they ever differ slightly — a countdown/status should
        # never show more time or more openness than actually remains on any
        # bracket in the event.
        close_time = min(close_times) if close_times else None
        if close_time is None:
            trading_status = "unknown"
        else:
            trading_status = "closed" if datetime.fromisoformat(close_time) <= datetime.now(UTC) else "open"
        groups.append(
            EventGroup(
                event_ticker=event_ticker,
                city=event_alerts[0].city,
                date_label=date_label,
                alerts=event_alerts,
                actionable_count=sum(1 for a in event_alerts if a.is_actionable),
                sum_deviation=bracket_sum_deviation([a.market_yes_price for a in event_alerts]),
                sizing=sizing,
                close_time=close_time,
                trading_status=trading_status,
            )
        )
    groups.sort(key=lambda g: (g.actionable_count == 0, -g.max_abs_edge))
    return groups


def _reasoning_text(alert: Alert) -> str:
    """Plain-language walk-through of how model_probability was actually
    computed, for the Details modal's "Why this probability?" toggle —
    reconstructed from stored fields rather than a canned template, so it
    stays honest about what the model did and didn't do for this alert."""
    if alert.ensemble_mean is None:
        return "No forecast detail was recorded for this alert."

    parts = [
        f"Today's raw weather forecast for {alert.city} centers on "
        f"{alert.ensemble_mean:.1f}°F."
    ]

    params = None
    try:
        month = parse_event_date(alert.event_ticker).month
        params = get_calibration(alert.series_ticker)
    except (ValueError, KeyError):
        pass

    if params is not None:
        bias = params.bias_for_month(month)
        corrected_mean = alert.ensemble_mean + bias
        if abs(bias) >= 0.1:
            if bias > 0:
                parts.append(
                    f"Around this time of year, {alert.city} forecasts like this one have "
                    f"tended to run about {bias:.1f}° too cold, so we shift our guess up to "
                    f"{corrected_mean:.1f}°F."
                )
            else:
                parts.append(
                    f"Around this time of year, {alert.city} forecasts like this one have "
                    f"tended to run about {abs(bias):.1f}° too warm, so we shift our guess "
                    f"down to {corrected_mean:.1f}°F."
                )
        parts.append(
            f"Day to day, forecasts like this have typically been off by about "
            f"{params.std:.1f}° in either direction, so we spread our guess across a range "
            "instead of betting on one exact number."
        )

    parts.append(
        f"This bracket covers {alert.bracket_label}. Weighing that range against our "
        f"adjusted guess and its spread gives a {alert.model_probability * 100:.0f}% chance "
        "— the \"Model\" number above."
    )
    return " ".join(parts)


def _settlement_source_url(alert: Alert) -> str:
    return get_station(alert.series_ticker).settlement_source_url


# Below this many settled days behind a series' calibration, an alert card
# flags its history as thin (see weather/calibration_params.py fit_days). The
# original 6 High Temperature cities sit at 420+, while the 14 newer cities
# and every Low Temperature series are far below — so the identical "98% win
# chance" is a much weaker claim on a thin series, and the card should say so.
_THIN_SAMPLE_DAYS = 200


def _calibration_fit_days(alert: Alert) -> int | None:
    """settled-day count behind this alert's city/metric calibration, or None
    for a series with no fitted params (shouldn't happen for a live alert, but
    degrade to "no badge" rather than raising)."""
    try:
        return get_calibration(alert.series_ticker).fit_days
    except KeyError:
        return None


# All timestamps are stored/read as UTC-aware (confirmed live: psycopg
# returns timezone.utc for timestamptz columns) — this is the one place that
# converts for display. Real IANA zone, not a fixed UTC-7 offset, so it
# correctly becomes PST once the calendar crosses into standard time rather
# than silently mislabeling every timestamp "PDT" from November on.
_PACIFIC_TZ = ZoneInfo("America/Los_Angeles")


def _pacific(dt: datetime | None, fmt: str = "%b %-d, %Y %H:%M %Z") -> str:
    if dt is None:
        return "—"
    return dt.astimezone(_PACIFIC_TZ).strftime(fmt)


app.jinja_env.globals["reasoning_text"] = _reasoning_text
app.jinja_env.globals["settlement_source_url"] = _settlement_source_url
app.jinja_env.globals["calibration_fit_days"] = _calibration_fit_days
app.jinja_env.globals["THIN_SAMPLE_DAYS"] = _THIN_SAMPLE_DAYS
app.jinja_env.filters["pacific"] = _pacific


def _actionable_alerts(alerts: list[Alert]) -> list[Alert]:
    """Every actionable alert, strongest edge first — feeds the bell panel.

    Deliberately not capped at a top-N or sorted by recommended stake the
    way the (now-removed) ranking section was: the bell is a full "what's
    currently worth a look" list, not a curated shortlist, and each row
    already shows its own win-chance/edge for the user to weigh directly.
    """
    return sorted((a for a in alerts if a.is_actionable), key=lambda a: -abs(a.edge))


def _fetch_today_extremes(events: list[EventGroup], metric: str) -> dict[str, tuple[float, str]]:
    """Today's actual low or high so far, per city with a matching-metric event
    on the page — real NWS station observations (the same station Kalshi
    itself settles against), not a forecast. This is what every card's live
    reading shows now, for all three categories (Low/High/Daily Temperature):
    by the time anyone's looking at the dashboard, "the temperature right
    now" often says little about that day's eventual low or high — a
    mid-afternoon reading doesn't tell you the overnight low already
    happened, and a cooling evening reading doesn't tell you the day's peak
    already passed. "The extreme so far, from the real settlement station"
    is the useful sanity check either way. Best-effort: a city that fails
    (network hiccup, station outage) is just omitted, not a broken page.
    """
    matching_cities = {event.city for event in events if event.alerts[0].metric == metric and event.is_same_day}
    stations_by_city = {s.city: s for s in STATIONS.values() if s.city in matching_cities}

    def fetch_one(city: str) -> tuple[str, tuple[float, str] | None]:
        station = stations_by_city[city]
        try:
            return city, fetch_today_extreme(station.nws_station_id, metric, station.standard_time_timezone)
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            print(f"  today-{metric} fetch failed for {city}: {exc.__class__.__name__}: {exc}")
            return city, None

    if not stations_by_city:
        return {}
    with ThreadPoolExecutor(max_workers=len(stations_by_city)) as executor:
        results = executor.map(fetch_one, stations_by_city.keys())
    return {city: value for city, value in results if value is not None}


@dataclass(frozen=True)
class PipelineRun:
    script: str
    started_at: datetime
    finished_at: datetime | None
    status: str
    summary: str | None
    detail: str | None


# The scripts scheduler/{run_pipeline,run_settlement_cycle,run_recalibration}.sh
# run — used to show "never run" for a script with zero rows, not just omit it
# silently.
_KNOWN_SCRIPTS = [
    "generate_alerts",
    "mark_settled_alerts",
    "run_paper_trading",
    "send_notifications",
    "fit_calibration_params",
    "refresh_same_day_observations",
]
# Two different cadences, not one — added 2026-07-20 after a real gap this
# single flat threshold hid: generate_alerts/send_notifications still run via
# run_pipeline.sh's ~6h cadence, but mark_settled_alerts/run_paper_trading
# moved to run_settlement_cycle.sh's 15-minute cadence the same day (see that
# script's docstring). A flat 8h threshold couldn't tell "the settlement cron
# stopped firing an hour ago" from "totally normal" — which is exactly what
# happened: the new cron entry didn't actually start firing on schedule until
# hours after it was believed deployed, and nothing here would have caught
# that short of manually diffing pipeline_runs timestamps. 30 minutes is 2x
# the 15-min cadence, the same "a couple cycles of buffer, not zero" margin
# the 8h/~6h ratio already used for the slower pair.
#
# fit_calibration_params was added to this list 2026-07-20 alongside the weekly
# recalibration cron, for precisely the reason the 15-min gap above was missed:
# a job nothing watches is a job that can stop firing unnoticed. A weekly job is
# the *easiest* kind to lose silently — at that cadence a stall looks identical
# to "it just hasn't run yet" for days. 9 days is one full cycle of slack past
# its 7-day period, so a single skipped Sunday flags rather than needing two.
_STALE_AFTER: dict[str, timedelta] = {
    "generate_alerts": timedelta(hours=8),
    "send_notifications": timedelta(hours=8),
    "mark_settled_alerts": timedelta(minutes=30),
    "run_paper_trading": timedelta(minutes=30),
    "fit_calibration_params": timedelta(days=9),
    # Same 15m cadence and 2x-cadence margin as mark_settled_alerts/
    # run_paper_trading above, added 2026-07-20 alongside
    # scheduler/run_observation_refresh.sh for the same reason: a job nothing
    # watches is a job whose cron entry can silently stop firing.
    "refresh_same_day_observations": timedelta(minutes=30),
}
_DEFAULT_STALE_AFTER = timedelta(hours=8)
# The cadence each script is actually scheduled at (scheduler/*.sh) — distinct
# from _STALE_AFTER, which is that cadence plus buffer. Shown on /status so
# "expected every 15m" and "stale after 30m" read as the two different
# numbers they are, not one value doing double duty.
_CADENCE = {
    "generate_alerts": "~6h",
    "send_notifications": "~6h",
    "mark_settled_alerts": "15m",
    "run_paper_trading": "15m",
    # A duration, not the word "weekly" — the template renders this as
    # "Expected every {x}", so a bare adverb produced "Expected every weekly".
    "fit_calibration_params": "7d",
    "refresh_same_day_observations": "15m",
}


def _pipeline_status() -> tuple[dict[str, PipelineRun], list[PipelineRun], str | None]:
    """Returns (latest run per known script, recent history, error) — error is
    set instead of raising if the DB is unreachable, same fallback spirit as
    db.py's demo-data path (this page should degrade gracefully, not 500)."""
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        return {}, [], "DATABASE_URL isn't set."

    import psycopg

    try:
        with psycopg.connect(database_url, connect_timeout=5) as conn, conn.cursor() as cur:
            cur.execute(
                "select distinct on (script) script, started_at, finished_at, status, summary, detail "
                "from pipeline_runs order by script, started_at desc"
            )
            latest = {row[0]: PipelineRun(*row) for row in cur.fetchall()}

            cur.execute(
                "select script, started_at, finished_at, status, summary, detail "
                "from pipeline_runs order by started_at desc limit 30"
            )
            history = [PipelineRun(*row) for row in cur.fetchall()]
        return latest, history, None
    except psycopg.OperationalError as exc:
        return {}, [], f"Couldn't connect to DATABASE_URL ({exc.__class__.__name__})."


def _automated_trading_context() -> dict:
    """Additional fields for the Automated Trading panel beyond what
    _paper_trading_context/_bot_status_payload already computed: worker
    health reuses _pipeline_status() (the exact same source /status reads,
    so the two pages can never disagree about whether the worker is
    healthy), and reconciliation/refresh-balance history reuses
    bot_control's own event log rather than a new table."""
    latest, _, _ = _pipeline_status()
    worker_run = latest.get("run_paper_trading")

    events = list_recent_events(limit=50)
    last_reconcile = next((e for e in events if e["event_type"] == "reconcile"), None)
    last_refresh = next((e for e in events if e["event_type"] == "refresh_balance"), None)

    return dict(
        worker_last_started=worker_run.started_at if worker_run else None,
        worker_last_finished=worker_run.finished_at if worker_run else None,
        worker_last_status=worker_run.status if worker_run else None,
        worker_last_summary=worker_run.summary if worker_run else None,
        last_reconciliation=last_reconcile["created_at"] if last_reconcile else None,
        last_reconciliation_detail=last_reconcile["detail"] if last_reconcile else None,
        last_balance_refresh=last_refresh["created_at"] if last_refresh else None,
        recent_bot_events=events[:12],
    )


@app.route("/audit/strategy-integrity")
def strategy_integrity_audit():
    """Renders logs/audit/strategy-integrity-latest.md (written by
    scripts/run_strategy_integrity_audit.py) so the compact validation
    card's "View integrity audit" link goes somewhere real instead of a
    dead link — this project has never had a route for it before."""
    report_path = Path(__file__).resolve().parent.parent / "logs/audit/strategy-integrity-latest.md"
    try:
        report_text = report_path.read_text()
    except OSError:
        report_text = None
    return render_template("strategy_integrity_audit.html", report_text=report_text, report_path=str(report_path))


@app.route("/status")
def status():
    latest, history, db_error = _pipeline_status()
    now = datetime.now(UTC)
    stale = {
        script: (now - (run.finished_at or run.started_at)) > _STALE_AFTER.get(script, _DEFAULT_STALE_AFTER)
        for script, run in latest.items()
    }
    # Runs stuck in 'running' long past any plausible duration. This is a
    # genuinely different failure from staleness, and neither the staleness
    # flags above nor the healthchecks.io dead-man's-switch covers it:
    #
    # - staleness asks "has a NEW run started recently", so a later run
    #   succeeding hides an earlier one that never finished;
    # - the dead-man's-switch only watches the cron wrappers, so a script run
    #   by hand and interrupted is invisible to it.
    #
    # That is exactly what happened to generate_alerts id=20 (started
    # 2026-07-19 01:42 UTC off-cron, killed mid-run before track_run could
    # write finished_at) and it sat unnoticed for two days while every
    # scheduled run around it succeeded. This surfaces that case directly.
    stuck = _stuck_runs()

    def _label(delta: timedelta) -> str:
        # Days matter now that the weekly recalibration job is tracked here —
        # rendering its 9-day threshold as "216h" is technically correct and
        # completely unreadable.
        minutes = int(delta.total_seconds() // 60)
        if minutes < 60:
            return f"{minutes}m"
        hours = minutes // 60
        return f"{hours}h" if hours < 48 else f"{hours // 24}d"

    return render_template(
        "status.html",
        known_scripts=_KNOWN_SCRIPTS,
        latest=latest,
        history=history,
        db_error=db_error,
        stale=stale,
        stale_after={script: _label(_STALE_AFTER.get(script, _DEFAULT_STALE_AFTER)) for script in _KNOWN_SCRIPTS},
        cadence=_CADENCE,
        stuck=stuck,
        stuck_after=_label(_STUCK_AFTER),
    )


@dataclass(frozen=True)
class CalibrationRow:
    city: str
    series_ticker: str
    metric_label: str  # "High" | "Low" — every city has both now
    using: str  # "monthly" | "flat"
    overall_bias: float
    monthly_bias: dict | None
    monthly_bias_range: tuple[float, float] | None
    std: float
    fit_date: str
    fit_days: int
    flat_brier: float | None
    monthly_brier: float | None


# Longer than any real run. The heaviest job here (the weekly recalibration,
# walking ~2 years of settled markets across 40 series on one vCPU) finishes
# well inside this even on a cold cache, so anything still 'running' past it
# did not finish -- it died without track_run's exit handler getting to write
# a status.
_STUCK_AFTER = timedelta(hours=3)


def _stuck_runs() -> list[PipelineRun]:
    """Runs left in 'running' long enough that they must have died.

    Deliberately not folded into `_pipeline_status`, which returns only the
    latest run per script: a stuck run is usually NOT the latest one (a later
    scheduled run succeeds on top of it), so it is invisible to any
    latest-row-per-script view. That is precisely how a two-day-old zombie went
    unnoticed while /status showed everything green.
    """
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        return []

    import psycopg

    try:
        with psycopg.connect(database_url, connect_timeout=5) as conn, conn.cursor() as cur:
            cur.execute(
                "select script, started_at, finished_at, status, summary, detail "
                "from pipeline_runs where status = 'running' and started_at < %s "
                "order by started_at",
                (datetime.now(UTC) - _STUCK_AFTER,),
            )
            return [PipelineRun(*row) for row in cur.fetchall()]
    except psycopg.OperationalError:
        return []


def _calibration_runs() -> list[tuple]:
    """Every recorded recalibration run, for the week-over-week trend.

    Deliberately a separate query from `_pipeline_status`, which returns only
    the latest run per script — a trend needs the whole history, and the point
    of this panel is that "is the gap closing?" gets answered from accumulated
    evidence rather than a fresh manual audit. Returns [] on any DB trouble,
    matching this page's degrade-don't-500 behaviour.
    """
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        return []

    import psycopg

    try:
        with psycopg.connect(database_url, connect_timeout=5) as conn, conn.cursor() as cur:
            cur.execute(
                "select id, started_at, detail from pipeline_runs "
                "where script = 'fit_calibration_params' and status in ('success', 'partial') "
                "order by started_at"
            )
            return cur.fetchall()
    except psycopg.OperationalError:
        return []


def _parse_json_detail(run: PipelineRun | None) -> list | None:
    """fit_calibration_params/validate_against_noaa store their per-city
    results as a JSON list in pipeline_runs.detail (see those scripts) rather
    than prose, specifically so this page can render real numbers without a
    human re-running the script and reading stdout. None on anything
    unexpected (never run yet, or an old row from before this existed)."""
    if run is None or not run.detail:
        return None
    try:
        return json.loads(run.detail)
    except (json.JSONDecodeError, TypeError):
        return None


def _calibration_rows(calibration_detail: list[dict] | None) -> list[CalibrationRow]:
    # Keyed by series_ticker, not city: every city has both a High and a Low
    # Temperature series now, so a city-only key can't tell the two rows one
    # city produces apart — see fit_calibration_params.py's own comment on
    # this same real bug, found and fixed together. Falls back to a
    # city-keyed lookup for a `detail` blob stored before series_ticker was
    # added there, so an unrefreshed row degrades to "no Brier score yet"
    # rather than crashing.
    brier_by_series = {row["series_ticker"]: row for row in (calibration_detail or []) if "series_ticker" in row}
    brier_by_city_fallback = {row["city"]: row for row in (calibration_detail or [])}
    # What is ACTUALLY in force, not the committed baseline. Since 2026-07-20 a
    # weekly cron can write an untracked JSON override that get_calibration()
    # prefers (see weather/calibration_override.py), so reading the CALIBRATION
    # dict directly here would show numbers the live model isn't using — the
    # precise failure mode the override design has to avoid, on the one page
    # whose entire job is answering "what is this thing calibrated to."
    override = load_override()
    rows = []
    for station in STATIONS.values():
        params = override.get(station.series_ticker) or CALIBRATION.get(station.series_ticker)
        if params is None:
            continue
        brier = brier_by_series.get(station.series_ticker) or brier_by_city_fallback.get(station.city)
        rows.append(
            CalibrationRow(
                city=station.city,
                series_ticker=station.series_ticker,
                metric_label="Low" if station.metric == "min" else "High",
                using="monthly" if params.monthly_bias is not None else "flat",
                overall_bias=params.overall_bias,
                monthly_bias=params.monthly_bias,
                monthly_bias_range=(
                    (min(params.monthly_bias.values()), max(params.monthly_bias.values()))
                    if params.monthly_bias
                    else None
                ),
                std=params.std,
                fit_date=params.fit_date,
                fit_days=params.fit_days,
                flat_brier=brier["flat_brier"] if brier else None,
                monthly_brier=brier["monthly_brier"] if brier else None,
            )
        )
    # City+metric pairs sit adjacent (NYC High, NYC Low, Chicago High, ...)
    # rather than STATIONS' own iteration order (all High series, then all
    # Low) — the latter reads as if there are two unrelated "NYC" rows far
    # apart in the table instead of clearly a paired high/low set.
    rows.sort(key=lambda r: (r.city, r.metric_label))
    return rows


@app.route("/backtest")
def backtest():
    latest, _, db_error = _pipeline_status()
    calibration_run = latest.get("fit_calibration_params")
    noaa_run = latest.get("validate_against_noaa")
    noaa_rows = _parse_json_detail(noaa_run) or []
    noaa_mismatch_lines = [line for row in noaa_rows for line in (row.get("mismatch_lines") or [])]
    # run_backtest.py stores its per-city Brier + pooled reliability diagram as
    # a JSON dict (not a list, unlike the two above) in pipeline_runs.detail —
    # see build_backtest_detail. It isn't a scheduled pipeline script, so it's
    # not in _KNOWN_SCRIPTS, but _pipeline_status returns the latest run of
    # every script that has ever run, including this one.
    backtest_run = latest.get("run_backtest")
    reliability = _parse_json_detail(backtest_run)
    trend = summarize(comparable_trend(_calibration_runs()))
    # Which series' live params differ from the committed baseline, so the page
    # can say so explicitly rather than leaving the divergence invisible.
    override = load_override()
    override_diff = sorted(
        STATIONS[t].city + (" Low" if STATIONS[t].metric == "min" else " High")
        for t, p in override.items()
        if t in STATIONS and CALIBRATION.get(t) != p
    )
    return render_template(
        "backtest.html",
        db_error=db_error,
        calibration_run=calibration_run,
        noaa_run=noaa_run,
        calibration_rows=_calibration_rows(_parse_json_detail(calibration_run)),
        noaa_rows=noaa_rows,
        noaa_mismatch_lines=noaa_mismatch_lines,
        backtest_run=backtest_run,
        reliability=reliability if isinstance(reliability, dict) else None,
        override_meta=override_metadata(),
        override_diff=override_diff,
        trend=trend,
        revisit_streak=REVISIT_STREAK,
        investigation=_strategy_investigation_summary(),
    )


def _strategy_investigation_summary() -> dict | None:
    """Load the reproducible research headline without requiring a DB query."""
    path = (
        Path(__file__).resolve().parents[1]
        / "artifacts"
        / "strategy_investigation"
        / "final_summary.json"
    )
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


@dataclass(frozen=True)
class PortfolioRow:
    """One real position on the user's actual Kalshi account, enriched with
    the market's own question/outcome text and a live mark-to-market —
    distinct from paper_trades, which is this project's own simulation and
    never touches the real account. Automated bot orders are separately
    identified and persisted by `live_trading`.

    `cost_dollars` is Kalshi's own `market_exposure_dollars` field (their
    docs call it "aggregate position cost") — confirmed live 2026-07-19 this
    matches kalshi.com's own displayed "Cost" column exactly for an
    untouched position. It can diverge slightly for a position with a
    partial-close history (real trading activity beyond a single buy), since
    the website's simplified avg-price display and the API's proper
    remaining-cost-basis accounting aren't guaranteed to reconcile perfectly
    after a partial sale — confirmed on one real position with a non-zero
    realized_pnl_dollars. Not a bug in this code; a real accounting-method
    nuance on Kalshi's side.
    """

    ticker: str
    title: str
    outcome_label: str
    side: str
    contracts: float
    cost_dollars: float
    market_value_dollars: float | None
    total_return_dollars: float | None
    realized_pnl_dollars: float
    fees_paid_dollars: float
    last_updated_ts: datetime | None
    kalshi_url: str | None

    @property
    def total_return_pct(self) -> float | None:
        if self.total_return_dollars is None or self.cost_dollars == 0:
            return None
        return self.total_return_dollars / self.cost_dollars


def _enrich_position(client: KalshiClient, position: Position) -> PortfolioRow:
    """Best-effort: a title/link/price lookup failing for one position (a
    settled market aging out of the live endpoint, a network hiccup) still
    shows that position with its bare ticker and no live price rather than
    dropping the row or failing the whole page — same spirit as
    _fetch_prices_via_rest."""
    title = ""
    outcome_label = ""
    kalshi_url = None
    market_value = None
    total_return = None
    try:
        market = client.get_market(position.ticker)
        title = market.title
        outcome_label = market.yes_sub_title if position.side == "yes" else market.no_sub_title
        # Kalshi's own "Market value" column uses last *traded* price, not a
        # bid/ask midpoint (unlike this project's own alerts/paper-trading
        # mark-to-market convention) — confirmed live 2026-07-19: contracts x
        # last_price_dollars reproduced kalshi.com's displayed Market value
        # exactly across every real position checked. Using bid/ask here
        # would silently stop matching the number the user is actually
        # looking at on kalshi.com.
        if market.last_price_dollars is not None:
            market_value = round(position.contracts * market.last_price_dollars, 4)
            total_return = round(market_value - position.market_exposure_dollars, 4)
        # Series ticker isn't on Position or Market directly, but every
        # Kalshi ticker hierarchy is series-event-market joined by "-", and
        # event_ticker is already the series ticker plus exactly one more
        # "-"-joined segment (confirmed live 2026-07-19 for weather tickers,
        # same convention holds here: event_ticker
        # "KXWCFINISHINGORDER-26" rsplits to series "KXWCFINISHINGORDER").
        series_ticker = market.event_ticker.rsplit("-", 1)[0]
        series = client.get_series(series_ticker)
        kalshi_url = market_url(series_ticker, series.title, market.event_ticker)
    except Exception as exc:
        print(f"  couldn't enrich position {position.ticker}: {exc.__class__.__name__}: {exc}")
    return PortfolioRow(
        ticker=position.ticker,
        title=title,
        outcome_label=outcome_label,
        side=position.side,
        contracts=position.contracts,
        cost_dollars=position.market_exposure_dollars,
        market_value_dollars=market_value,
        total_return_dollars=total_return,
        realized_pnl_dollars=position.realized_pnl_dollars,
        fees_paid_dollars=position.fees_paid_dollars,
        last_updated_ts=datetime.fromisoformat(position.last_updated_ts) if position.last_updated_ts else None,
        kalshi_url=kalshi_url,
    )


def _fetch_portfolio() -> tuple[list[PortfolioRow], str | None, datetime]:
    """Real positions straight from the Kalshi account these KALSHI_API_KEY_ID
    /KALSHI_PRIVATE_KEY_PATH credentials belong to — same credentials this
    project has always used for read-only market discovery, now also used
    for the one read-only portfolio endpoint. Live on every page load (no
    cache yet — real position counts are small enough for this project's own
    use that the added latency of a handful of enrichment calls is a
    reasonable trade-off for always-current data; revisit if that stops
    being true). Returns a `fetched_at` timestamp alongside the rows so the
    page can show visibly when this specific load actually talked to Kalshi,
    distinct from each row's own `last_updated_ts` (which is Kalshi's record
    of when *that position* last changed, not when we last checked it)."""
    fetched_at = datetime.now(UTC)
    try:
        with KalshiClient.from_env() as client:
            positions = client.get_positions()
            if not positions:
                return [], None, fetched_at
            with ThreadPoolExecutor(max_workers=min(10, len(positions))) as executor:
                rows = list(executor.map(lambda p: _enrich_position(client, p), positions))
        rows.sort(key=lambda r: -r.cost_dollars)
        return rows, None, fetched_at
    except KalshiAuthError:
        return [], "KALSHI_API_KEY_ID / KALSHI_PRIVATE_KEY_PATH aren't configured for portfolio access.", fetched_at
    except Exception as exc:
        return [], f"Couldn't fetch your Kalshi portfolio ({exc.__class__.__name__}): {exc}", fetched_at


def _is_weather_ticker(ticker: str) -> bool:
    """Whether a settled market is one of this project's own weather series
    (high/low daily temperature, `KXHIGH*`/`KXLOWT*`). Everything else on the
    real account — the FIFA World Cup, NBA, etc. markets it's actually been
    traded on — is discretionary, nothing this system ever produced a signal
    for. Used to answer "how much of the real loss was the weather model vs.
    unrelated bets."""
    return ticker.startswith("KXHIGH") or ticker.startswith("KXLOWT")


def _settlement_summary(settlements: list[Settlement]) -> dict:
    """Weather-vs-other realized-P&L breakdown of settled real trades — pure,
    so it's unit-testable. Answers, in dollars, where the real account's money
    actually went: this project's weather model, or discretionary bets on
    unrelated markets."""

    def agg(rows: list[Settlement]) -> dict:
        return dict(
            n=len(rows),
            wins=sum(1 for s in rows if s.won),
            cost=round(sum(s.cost_dollars for s in rows), 2),
            revenue=round(sum(s.revenue_dollars for s in rows), 2),
            fees=round(sum(s.fee_dollars for s in rows), 2),
            net=round(sum(s.net_pnl_dollars for s in rows), 2),
        )

    weather = [s for s in settlements if _is_weather_ticker(s.ticker)]
    other = [s for s in settlements if not _is_weather_ticker(s.ticker)]
    return {"weather": agg(weather), "other": agg(other), "total": agg(settlements)}


def _fetch_settlements() -> tuple[list[Settlement], str | None]:
    """Real settled-market history from the Kalshi account (read-only, GET
    /portfolio/settlements). Most-recent first. Degrades to an empty list plus
    an error string rather than failing the whole Portfolio page."""
    try:
        with KalshiClient.from_env() as client:
            settlements = client.get_settlements()
        settlements.sort(key=lambda s: s.settled_time or "", reverse=True)
        return settlements, None
    except KalshiAuthError:
        return [], "KALSHI_API_KEY_ID / KALSHI_PRIVATE_KEY_PATH aren't configured for portfolio access."
    except Exception as exc:
        return [], f"Couldn't fetch your Kalshi trade history ({exc.__class__.__name__}): {exc}"


def _portfolio_context() -> dict:
    """Template vars for the "Your Kalshi portfolio" tab — the user's real,
    read-only Kalshi account positions and settled trade history. Split out of
    the old /portfolio route 2026-07-22 so the combined /portfolio page can
    render this tab alongside the paper-trading tab in one request."""
    rows, error, fetched_at = _fetch_portfolio()
    total_cost = sum(r.cost_dollars for r in rows)
    known_market_values = [r.market_value_dollars for r in rows if r.market_value_dollars is not None]
    total_market_value = sum(known_market_values) if len(known_market_values) == len(rows) else None
    total_realized_pnl = sum(r.realized_pnl_dollars for r in rows)
    settlements, settlements_error = _fetch_settlements()
    return dict(
        error=error,
        rows=rows,
        fetched_at=fetched_at,
        total_cost=total_cost,
        total_market_value=total_market_value,
        total_realized_pnl=total_realized_pnl,
        settlements=settlements,
        settlements_error=settlements_error,
        settlement_summary=_settlement_summary(settlements),
        is_weather_ticker=_is_weather_ticker,
    )


_PAPER_TRADE_COLUMNS = (
    "pt.id, pt.opened_at, pt.market_ticker, pt.event_ticker, pt.series_ticker, pt.city, pt.bracket_label, pt.side, "
    "pt.entry_price, pt.contracts, pt.entry_fee, pt.cost_basis, pt.entry_model_probability, pt.entry_edge, "
    "pt.status, pt.closed_at, pt.close_reason, pt.exit_price, pt.exit_fee, pt.payout, pt.realized_pnl, "
    "pt.strategy_version"
)


@dataclass(frozen=True)
class PaperTrade:
    id: int
    opened_at: datetime
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
    status: str
    closed_at: datetime | None
    close_reason: str | None
    exit_price: float | None
    exit_fee: float | None
    payout: float | None
    realized_pnl: float | None
    # Stamped at open time from paper_trading/engine.py::STRATEGY_VERSION.
    # None for rows opened before this column existed (2026-07-23) — can't be
    # reconstructed after the fact, same as alerts.lead_days' own null rows.
    strategy_version: str | None
    # Both from the `alerts` table (see _fetch_paper_trades' join), not
    # paper_trades columns themselves — paper_trades never stored either,
    # but every position was opened from a real alerts row for the same
    # market_ticker, which already has them (see generate_alerts.py). None
    # only if that alerts row is somehow gone, not expected in practice.
    kalshi_url: str | None
    close_time: datetime | None

    @property
    def predicted_ev(self) -> float:
        """Expected $ P&L at entry, from the model's own stated probability
        — the predicted half of "predicted vs. realized EV," the comparison
        that actually checks whether the model's confidence is honest. Win
        rate doesn't do this: a bracket bought at 90c with the model at 85%
        and one bought at 40c with the model at 50% can carry the exact same
        edge and the exact same expected value, at wildly different win
        rates — see the /paper-trading page's own note on this, added
        2026-07-19 after an external strategy review flagged win rate as a
        gameable, misleading headline metric for exactly this reason.
        `entry_model_probability` is always P(bracket resolves YES),
        regardless of which side was actually bought (same convention as
        dashboard/alert.py's Alert.model_probability) — flip it for a NO
        position before using it here."""
        predicted_win_prob = self.entry_model_probability if self.side == "YES" else 1 - self.entry_model_probability
        return round(predicted_win_prob * self.contracts - self.cost_basis, 4)


def _latest_bankroll_reset() -> datetime | None:
    """Most recent reset_at, or None if the bankroll has never been reset.
    Realized P&L from before this point stops counting toward "cash
    available" (see paper_trading route below) — but every paper_trades row
    stays in the table and visible in the trade-history UI regardless; a
    reset only changes the bankroll math, never deletes history."""
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        return None
    import psycopg

    try:
        with psycopg.connect(database_url, connect_timeout=5) as conn, conn.cursor() as cur:
            cur.execute("select reset_at from bankroll_resets order by reset_at desc limit 1")
            row = cur.fetchone()
            return row[0] if row else None
    except psycopg.OperationalError:
        return None


def _fetch_paper_trades() -> tuple[list[PaperTrade], str | None]:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        return [], "DATABASE_URL isn't set."

    import psycopg

    try:
        with psycopg.connect(database_url, connect_timeout=5) as conn, conn.cursor() as cur:
            # kalshi_url/close_time aren't stored on paper_trades itself —
            # every position was opened from a real alerts row for the same
            # market_ticker, and that row already has both (generate_alerts.py),
            # so a lateral join reuses them instead of a fresh API call per
            # row. Same "latest snapshot per ticker" pattern the rest of the
            # codebase uses (DISTINCT ON elsewhere), just expressed as a
            # correlated subquery since this is a join, not a top-level query.
            cur.execute(
                f"select {_PAPER_TRADE_COLUMNS}, a.kalshi_url, a.close_time "
                "from paper_trades pt "
                "left join lateral ("
                "  select kalshi_url, close_time from alerts"
                "  where alerts.market_ticker = pt.market_ticker"
                "  order by created_at desc limit 1"
                ") a on true "
                "order by pt.opened_at desc"
            )
            trades = [PaperTrade(*row) for row in cur.fetchall()]
        return trades, None
    except psycopg.OperationalError as exc:
        return [], f"Couldn't connect to DATABASE_URL ({exc.__class__.__name__})."


def _fetch_current_prices(tickers: list[str]) -> dict[str, float]:
    """Live yes-price (bid/ask midpoint, same convention generate_alerts.py
    uses for market_yes_price — see its comment) per still-open market
    ticker. Checks price_feed's Redis cache first (kept warm by the
    WebSocket subscriber, scripts/run_price_feed.py — near-real-time and no
    REST calls at all when it's running) and only falls back to a direct
    REST fetch for whatever's missing: the subscriber not running, Redis
    unset, or a ticker it hasn't gotten an update for yet. Either path is
    best-effort — a ticker that fails both is just omitted, and the caller
    falls back to cost basis for it rather than the whole page failing.
    """
    if not tickers:
        return {}

    cached = get_cached_prices(tickers)
    missing = [t for t in tickers if t not in cached]
    if not missing:
        return cached
    return {**cached, **_fetch_prices_via_rest(missing)}


def _fetch_prices_via_rest(tickers: list[str]) -> dict[str, float]:
    if not tickers:
        return {}

    # Extract unique series_tickers from market tickers (e.g., "KXHIGHNY-26JUL20-B79.5" → "KXHIGHNY")
    series_tickers = set()
    for ticker in tickers:
        series_ticker = ticker.split("-")[0]
        series_tickers.add(series_ticker)

    results = {}
    try:
        with KalshiClient() as client:
            for series_ticker in series_tickers:
                # Fetch all markets for this series in one call, then filter to only the ones we need
                markets, _ = client.get_markets(series_ticker=series_ticker, limit=200)
                for market in markets:
                    if market.ticker in tickers:
                        # A market with trading closed (status != "active" — Kalshi's
                        # real live-trading value; "open" never actually occurs, see
                        # below) — e.g. the settlement result just hasn't posted yet —
                        # returns a degenerate yes_bid=0.0/yes_ask=1.0 rather than
                        # None/None. That's not a real quote (midpoint 0.50 for a
                        # position that was actually trading near 0), so treat
                        # not-actively-trading the same as "no quote" rather than
                        # trusting whatever bid/ask values are present.
                        if (
                            market.status == "active"
                            and market.yes_bid_dollars is not None
                            and market.yes_ask_dollars is not None
                        ):
                            results[market.ticker] = round(
                                (market.yes_bid_dollars + market.yes_ask_dollars) / 2, 4
                            )
    except Exception as exc:
        print(f"  live price fetch failed: {exc.__class__.__name__}: {exc}")

    return results


def _mark_to_market(trade: PaperTrade, current_prices: dict[str, float]) -> tuple[float, float | None]:
    """(current_value, unrealized_pnl) for one open position. Falls back to
    cost basis (zero unrealized P&L) when a live price wasn't available for
    this ticker this page load — an honest "unknown right now", not a
    fabricated zero-change guess presented as real."""
    live_price = current_prices.get(trade.market_ticker)
    if live_price is None:
        return trade.cost_basis, None
    current_yes_price = live_price if trade.side == "YES" else 1 - live_price
    current_value = round(trade.contracts * current_yes_price, 4)
    return current_value, round(current_value - trade.cost_basis, 4)


_FAR_FUTURE = datetime.max.replace(tzinfo=UTC)


def _close_time_sort_key(row: dict) -> datetime:
    # A missing close_time (alerts row somehow gone) sorts last, not first —
    # "unknown when this closes" shouldn't outrank a position with a real,
    # soon deadline for the user's attention.
    return row["trade"].close_time or _FAR_FUTURE


def _group_positions_by_city(open_position_rows: list[dict]) -> list[dict]:
    """Open positions grouped by city, sorted soonest-to-close-first — the
    point of surfacing this is "what needs my attention before its timer
    runs out," which cost basis doesn't answer (a small position closing in
    10 minutes is more time-sensitive than a large one closing tomorrow).
    Rows within a group are sorted the same way, soonest first.
    """
    by_city: dict[str, list[dict]] = defaultdict(list)
    for row in open_position_rows:
        by_city[row["trade"].city].append(row)

    groups = []
    for city, rows in by_city.items():
        rows = sorted(rows, key=_close_time_sort_key)
        cost_basis_total = sum(r["trade"].cost_basis for r in rows)
        current_value_total = sum(r["current_value"] for r in rows)
        known_pnls = [r["unrealized_pnl"] for r in rows if r["unrealized_pnl"] is not None]
        # One link per city row, even though a city can span multiple
        # events (different days, or both high- and low-temperature) — the
        # freshest-opened position is the most likely to still be an
        # actually-live market to click through to right now, vs. an older
        # one that may have already closed on Kalshi's side.
        newest = max(rows, key=lambda r: r["trade"].opened_at)
        groups.append(
            dict(
                city=city,
                rows=rows,
                cost_basis_total=cost_basis_total,
                current_value_total=current_value_total,
                unrealized_pnl_total=sum(known_pnls) if known_pnls else None,
                kalshi_url=newest["trade"].kalshi_url,
                earliest_close_time=rows[0]["trade"].close_time,
            )
        )
    groups.sort(key=lambda g: g["earliest_close_time"] or _FAR_FUTURE)
    return groups


def _group_closed_by_date(closed_trades: list[PaperTrade]) -> list[dict]:
    """Closed trades grouped by the calendar day their underlying weather
    event was about (parsed from event_ticker via the same helper
    _group_by_event uses for date_label), not by whenever closed_at happens
    to fall — these are daily markets, so "what did we trade for July 19"
    across every city is the meaningful unit, not an administrative
    close timestamp. Sorted most-recent-first, unlike the city grouping's
    exposure sort, since recency is what matters for a trade-history log.
    """
    by_date: dict[str, list[PaperTrade]] = defaultdict(list)
    labels: dict[str, str] = {}
    for t in closed_trades:
        try:
            event_date = parse_event_date(t.event_ticker)
            date_key = event_date.strftime("%Y-%m-%d")
            labels[date_key] = event_date.strftime("%b %-d, %Y")
        except ValueError:
            date_key = t.event_ticker
            labels[date_key] = t.event_ticker
        by_date[date_key].append(t)

    groups = []
    for date_key, date_trades in by_date.items():
        realized_pnl_total = sum(t.realized_pnl or 0 for t in date_trades)
        groups.append(
            dict(
                date_key=date_key,
                date_label=labels[date_key],
                trades=date_trades,
                realized_pnl_total=realized_pnl_total,
            )
        )
    groups.sort(key=lambda g: g["date_key"], reverse=True)
    return groups


def _fetch_validation_summary(trade_performance, predicted_ev_total: float, realized_pnl_total: float):
    """Builds the compact validation card's data (validation/summary.py),
    reusing trade_performance/predicted_ev_total/realized_pnl_total that
    _paper_trading_context already computed rather than requerying
    paper_trades a second time. Returns None if the database is
    unreachable — the template renders an honest "unavailable" state, not a
    fabricated number."""
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        return None
    import psycopg

    try:
        with psycopg.connect(database_url, connect_timeout=5) as conn, conn.cursor() as cur:
            return build_validation_summary(
                cur,
                strategy_name=bot_control.STRATEGY_NAME,
                strategy_version=STRATEGY_VERSION,
                trade_performance=trade_performance,
                model_implied_ev=predicted_ev_total,
                realized_pnl=realized_pnl_total,
            )
    except psycopg.OperationalError:
        return None


def _paper_trading_context() -> dict:
    """Template vars for the paper-trading bot tab. Split out of the old
    /paper-trading route 2026-07-22 so the combined /portfolio page can render
    this tab alongside the real-Kalshi-account tab in one request."""
    fetched_at = datetime.now(UTC)
    trades, db_error = _fetch_paper_trades()
    open_trades = [t for t in trades if t.status == "open"]
    closed_trades = [t for t in trades if t.status == "closed"]

    # A reset only changes which realized P&L counts toward "cash available"
    # right now — every row stays in `trades`/`closed_trades` and the trade
    # history below still shows all of it. Comparing datetime to an ISO
    # string works directly since Alert-style rows already normalize
    # timestamps that way; PaperTrade keeps them as real datetimes.
    latest_reset = _latest_bankroll_reset()
    closed_since_reset = (
        [t for t in closed_trades if latest_reset is None or (t.closed_at and t.closed_at > latest_reset)]
    )
    days_since_reset = (fetched_at - latest_reset).days if latest_reset else None

    realized_pnl_total = sum(t.realized_pnl or 0 for t in closed_since_reset)
    # Same scope as realized_pnl_total (closed_since_reset) so the two are a
    # fair comparison — what did we expect these specific settled trades to
    # earn, vs. what did they actually earn. See PaperTrade.predicted_ev.
    predicted_ev_total = sum(t.predicted_ev for t in closed_since_reset)
    open_cost_basis_total = sum(t.cost_basis for t in open_trades)
    cash_available = STARTING_BANKROLL_USD + realized_pnl_total - open_cost_basis_total

    # Same total_bankroll basis scripts/run_paper_trading.py pins the reserve
    # to (starting + all-time realized P&L, not cash_available itself) —
    # kept in sync deliberately so this page shows exactly what the bot's
    # own next cycle will actually hold back, not an approximation of it.
    total_bankroll = STARTING_BANKROLL_USD + realized_pnl_total
    cash_reserve_fraction = cash_reserve_fraction_setting()
    cash_deployable = deployable_cash(cash_available, total_bankroll)
    cash_reserve_held = round(cash_available - cash_deployable, 2)

    current_prices = _fetch_current_prices([t.market_ticker for t in open_trades])
    open_position_rows = []
    open_value_total = 0.0
    unrealized_pnl_total = 0.0
    unrealized_pnl_known = False
    for t in open_trades:
        current_value, unrealized_pnl = _mark_to_market(t, current_prices)
        open_value_total += current_value
        if unrealized_pnl is not None:
            unrealized_pnl_total += unrealized_pnl
            unrealized_pnl_known = True
        open_position_rows.append(dict(trade=t, current_value=current_value, unrealized_pnl=unrealized_pnl))

    portfolio_value = cash_available + open_value_total

    wins = [t for t in closed_since_reset if (t.realized_pnl or 0) > 0]
    win_rate = (len(wins) / len(closed_since_reset)) if closed_since_reset else None

    # trade_stats needs chronological (oldest-first) order for drawdown/streaks
    # to mean anything — _fetch_paper_trades returns newest-first for display,
    # so re-sort just for this. Same closed_since_reset scope as realized_pnl_total
    # above, for the same reason (a reset shouldn't count pre-reset trades).
    chronological_since_reset = sorted(
        closed_since_reset, key=lambda t: t.closed_at or datetime.min.replace(tzinfo=UTC)
    )
    trade_performance = trade_stats([t.realized_pnl or 0.0 for t in chronological_since_reset])

    close_reason_counts: dict[str, int] = defaultdict(int)
    for t in closed_trades:
        close_reason_counts[t.close_reason or "unknown"] += 1

    # "N of M" scoped to all trades, not closed_since_reset — a bankroll reset
    # and a strategy-version bump are independent events (see STRATEGY_VERSION's
    # own docstring in paper_trading/engine.py), so this answers "how much of
    # the visible history reflects the config actually running right now,"
    # regardless of reset boundaries.
    trades_on_current_version = sum(1 for t in trades if t.strategy_version == STRATEGY_VERSION)
    validation_summary = _fetch_validation_summary(trade_performance, predicted_ev_total, realized_pnl_total)

    # Automated Trading panel fields — both reuse data this function already
    # collected rather than requerying paper_trades a second time.
    today = fetched_at.date()
    daily_realized_pnl = sum(
        t.realized_pnl or 0 for t in closed_since_reset if t.closed_at and t.closed_at.date() == today
    )
    # trade_performance.current_streak is negative during a losing streak —
    # same convention the compact validation card's "Streaks" stat already
    # reads, just unsigned here for direct display as a loss count.
    consecutive_bot_losses = max(0, -trade_performance.current_streak)

    return dict(
        db_error=db_error,
        fetched_at=fetched_at,
        starting_bankroll=STARTING_BANKROLL_USD,
        cash_available=cash_available,
        cash_deployable=cash_deployable,
        cash_reserve_held=cash_reserve_held,
        cash_reserve_fraction=cash_reserve_fraction,
        latest_reset=latest_reset,
        days_since_reset=days_since_reset,
        open_city_groups=_group_positions_by_city(open_position_rows),
        closed_date_groups=_group_closed_by_date(closed_trades),
        portfolio_value=portfolio_value,
        open_cost_basis_total=open_cost_basis_total,
        unrealized_pnl_total=unrealized_pnl_total if unrealized_pnl_known else None,
        realized_pnl_total=realized_pnl_total,
        predicted_ev_total=predicted_ev_total,
        open_position_rows=open_position_rows,
        closed_trades=closed_trades,
        closed_since_reset=closed_since_reset,
        win_rate=win_rate,
        wins=len(wins),
        losses=len(closed_since_reset) - len(wins),
        close_reason_counts=dict(close_reason_counts),
        trade_performance=trade_performance,
        strategy_version=STRATEGY_VERSION,
        trades_on_current_version=trades_on_current_version,
        trades_total=len(trades),
        validation_summary=validation_summary,
        bot_status=_bot_status_payload(),
        daily_realized_pnl=daily_realized_pnl,
        consecutive_bot_losses=consecutive_bot_losses,
    )


@app.route("/portfolio")
def portfolio():
    """Combined Portfolio page: the paper-trading bot and the real Kalshi
    account, as two tabs (portfolio.html). Merged 2026-07-22 from the former
    separate /portfolio and /paper-trading pages. Both tab contexts are built
    every request so _live_refresh.html keeps both fresh; `fetched_at` is
    shared (portfolio's wins the dict merge — sub-second difference, both mean
    "this page load")."""
    return render_template(
        "portfolio.html",
        **{**_paper_trading_context(), **_portfolio_context(), **_automated_trading_context()},
    )


@app.route("/paper-trading")
def paper_trading():
    """Kept as a redirect so existing links/bookmarks and url_for('paper_trading')
    (e.g. backtest.html's validation-bar link) still resolve after the
    2026-07-22 merge into the tabbed /portfolio page."""
    return redirect(url_for("portfolio") + "#paper")


def _group_previews_by_date(previews: list[ForecastPreview]) -> list[dict]:
    """One group per target_date, each city/metric sorted together — mirrors
    _group_by_event's shape (date_label, rows) so index.html's "Looking
    ahead" section reads the same way the tradeable sections above it do."""
    by_date: dict[str, list[ForecastPreview]] = defaultdict(list)
    for preview in previews:
        by_date[preview.target_date].append(preview)

    groups = []
    for target_date, rows in sorted(by_date.items()):
        rows.sort(key=lambda p: (p.city, p.metric))
        try:
            date_label = datetime.fromisoformat(target_date).strftime("%b %-d, %Y")
        except ValueError:
            date_label = target_date
        groups.append({"target_date": target_date, "date_label": date_label, "lead_days": rows[0].lead_days, "rows": rows})
    return groups


@app.route("/")
def index():
    fetched_at = datetime.now(UTC)
    alerts, demo_reason = get_alerts()
    any_unvalidated = any(not a.calibration_validated for a in alerts)
    events = _group_by_event(alerts)
    # Split by lead time rather than leaving one flat list — tomorrow's
    # events (lead_days == 1) are the ones this project's calibration is
    # actually fit for, and are what let a trade get placed before the
    # crowd; today's (lead_days == 0, or unknown) are same-day and reused
    # from that same calibration as a best-available approximation, not a
    # separately validated one. See kalshi-implementation-progress memory,
    # 2026-07-19/20 entries.
    tomorrow_events = [e for e in events if e.lead_days == 1]
    today_events = [e for e in events if e.lead_days != 1]
    today_lows = _fetch_today_extremes(events, "min")
    today_highs = _fetch_today_extremes(events, "max")
    preview_groups = _group_previews_by_date(get_forecast_previews())
    return render_template(
        "index.html",
        today_events=today_events,
        tomorrow_events=tomorrow_events,
        preview_groups=preview_groups,
        demo_reason=demo_reason,
        any_unvalidated=any_unvalidated,
        kelly_fraction=kelly_fraction_setting(),
        max_event_exposure=max_event_exposure_setting(),
        actionable_alerts=_actionable_alerts(alerts),
        today_lows=today_lows,
        today_highs=today_highs,
        fetched_at=fetched_at,
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    # debug=True enables Werkzeug's interactive debugger, which allows
    # arbitrary code execution from anything that can reach this port — off
    # by default, opt in locally with FLASK_DEBUG=1. Production doesn't run
    # this __main__ block at all (see deploy/kalshi-dashboard.service, which
    # runs gunicorn directly), but this stays safe either way.
    debug = os.environ.get("FLASK_DEBUG", "").lower() in ("1", "true", "yes")
    app.run(host="127.0.0.1", port=port, debug=debug)
