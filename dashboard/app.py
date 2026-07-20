from __future__ import annotations

import json
import os
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv
from flask import Flask, redirect, render_template, request, session, url_for

from edge.calculator import bracket_sum_deviation
from kalshi_client import KalshiAuthError, KalshiClient, Position, market_url, parse_event_date
from paper_trading import STARTING_BANKROLL_USD, cash_reserve_fraction_setting, deployable_cash
from price_feed.cache import get_cached_prices
from sizing.kelly import (
    BracketInput,
    SizeRecommendation,
    kelly_fraction_setting,
    max_event_exposure_setting,
    size_event,
)
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
app.permanent_session_lifetime = timedelta(days=3)


def _valid_passcodes() -> set[str]:
    raw = os.environ.get("PASSCODES", "")
    return {code.strip() for code in raw.split(",") if code.strip()}


@app.before_request
def _require_login():
    # No PASSCODES configured at all means the operator hasn't opted into
    # this gate — don't lock everyone out of a dashboard that was reachable
    # without it a moment ago. Once at least one code is set, everything
    # except the login page itself and static assets requires a session.
    if not _valid_passcodes() or request.endpoint in ("login", "static"):
        return None
    if session.get("authenticated"):
        return None
    return redirect(url_for("login", next=request.path))


@app.route("/login", methods=["GET", "POST"])
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


# The 3 scripts scheduler/run_pipeline.sh runs — used to show "never run" for
# a script with zero rows, not just omit it silently.
_KNOWN_SCRIPTS = ["generate_alerts", "mark_settled_alerts", "run_paper_trading", "send_notifications"]
# scheduler/crontab.example runs the pipeline ~every 6h — flagged stale a
# couple hours past that, not immediately, to leave room for a run simply
# taking a while or a device being asleep, not just "did it fire on the dot."
_STALE_AFTER = timedelta(hours=8)


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


@app.route("/status")
def status():
    latest, history, db_error = _pipeline_status()
    now = datetime.now(UTC)
    stale = {
        script: (now - (run.finished_at or run.started_at)) > _STALE_AFTER
        for script, run in latest.items()
    }
    return render_template(
        "status.html",
        known_scripts=_KNOWN_SCRIPTS,
        latest=latest,
        history=history,
        db_error=db_error,
        stale=stale,
        stale_after_hours=int(_STALE_AFTER.total_seconds() // 3600),
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
    rows = []
    for station in STATIONS.values():
        params = CALIBRATION.get(station.series_ticker)
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
    return render_template(
        "backtest.html",
        db_error=db_error,
        calibration_run=calibration_run,
        noaa_run=noaa_run,
        calibration_rows=_calibration_rows(_parse_json_detail(calibration_run)),
        noaa_rows=noaa_rows,
        noaa_mismatch_lines=noaa_mismatch_lines,
    )


@dataclass(frozen=True)
class PortfolioRow:
    """One real position on the user's actual Kalshi account, enriched with
    the market's own question/outcome text and a live mark-to-market —
    distinct from paper_trades, which is this project's own simulation and
    never touches the real account. Read-only: nothing in this codebase
    places, cancels, or modifies a real order.

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


@app.route("/portfolio")
def portfolio():
    rows, error, fetched_at = _fetch_portfolio()
    total_cost = sum(r.cost_dollars for r in rows)
    known_market_values = [r.market_value_dollars for r in rows if r.market_value_dollars is not None]
    total_market_value = sum(known_market_values) if len(known_market_values) == len(rows) else None
    total_realized_pnl = sum(r.realized_pnl_dollars for r in rows)
    return render_template(
        "portfolio.html",
        error=error,
        rows=rows,
        fetched_at=fetched_at,
        total_cost=total_cost,
        total_market_value=total_market_value,
        total_realized_pnl=total_realized_pnl,
    )


_PAPER_TRADE_COLUMNS = (
    "pt.id, pt.opened_at, pt.market_ticker, pt.event_ticker, pt.series_ticker, pt.city, pt.bracket_label, pt.side, "
    "pt.entry_price, pt.contracts, pt.entry_fee, pt.cost_basis, pt.entry_model_probability, pt.entry_edge, "
    "pt.status, pt.closed_at, pt.close_reason, pt.exit_price, pt.exit_fee, pt.payout, pt.realized_pnl"
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

    def fetch_one(client: KalshiClient, ticker: str) -> tuple[str, float | None]:
        try:
            market = client.get_market(ticker)
            # A market with trading closed (status != "active" — Kalshi's
            # real live-trading value; "open" never actually occurs, see
            # below) — e.g. the settlement result just hasn't posted yet —
            # returns a degenerate yes_bid=0.0/yes_ask=1.0 rather than
            # None/None. That's not a real quote (midpoint 0.50 for a
            # position that was actually trading near 0), so treat
            # not-actively-trading the same as "no quote" rather than
            # trusting whatever bid/ask values are present.
            #
            # Real bug, live 2026-07-19: this previously
            # checked `status != "open"`, based on an assumption that was
            # only ever verified against a *closed* market (which correctly
            # showed status="closed") and never against a genuinely live one.
            # A live market's real status is "active", not "open" — Kalshi's
            # full enum is initialized/inactive/active/closed/determined/
            # disputed/amended/finalized, confirmed against both a live
            # market (status="active", real bid/ask) and the official API
            # docs. The old check matched *zero* real markets, so every REST
            # fallback silently returned no quote — the Redis/WebSocket cache
            # (added the same day, see price_feed/) partially masked this by
            # covering most positions, but anything the cache missed showed
            # "no live quote" instead of a real price, not just for
            # genuinely-closed markets as intended.
            if market.status != "active" or market.yes_bid_dollars is None or market.yes_ask_dollars is None:
                return ticker, None
            return ticker, round((market.yes_bid_dollars + market.yes_ask_dollars) / 2, 4)
        except Exception as exc:
            print(f"  live price fetch failed for {ticker}: {exc.__class__.__name__}: {exc}")
            return ticker, None

    with KalshiClient() as client:
        with ThreadPoolExecutor(max_workers=min(10, len(tickers))) as executor:
            results = executor.map(lambda t: fetch_one(client, t), tickers)
    return {ticker: price for ticker, price in results if price is not None}


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


@app.route("/paper-trading")
def paper_trading():
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

    close_reason_counts: dict[str, int] = defaultdict(int)
    for t in closed_trades:
        close_reason_counts[t.close_reason or "unknown"] += 1

    return render_template(
        "paper_trading.html",
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
    )


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
