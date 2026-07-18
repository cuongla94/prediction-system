from __future__ import annotations

import os
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
from dotenv import load_dotenv
from flask import Flask, render_template

from edge.calculator import bracket_sum_deviation
from kalshi_client import parse_event_date
from sizing.kelly import (
    BracketInput,
    SizeRecommendation,
    kelly_fraction_setting,
    max_event_exposure_setting,
    size_event,
)
from weather.calibration_params import get_calibration
from weather.open_meteo import fetch_current_temperature
from weather.stations import STATIONS

from .alert import Alert
from .db import get_alerts

# Module-level, not just in __main__: a production WSGI server imports this
# module directly rather than running the __main__ block below, and still
# needs DATABASE_URL loaded before the first request comes in.
load_dotenv()

app = Flask(__name__)


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


app.jinja_env.globals["reasoning_text"] = _reasoning_text


def _actionable_alerts(alerts: list[Alert]) -> list[Alert]:
    """Every actionable alert, strongest edge first — feeds the bell panel.

    Deliberately not capped at a top-N or sorted by recommended stake the
    way the (now-removed) ranking section was: the bell is a full "what's
    currently worth a look" list, not a curated shortlist, and each row
    already shows its own win-chance/edge for the user to weigh directly.
    """
    return sorted((a for a in alerts if a.is_actionable), key=lambda a: -abs(a.edge))


def _fetch_current_temps(cities: list[str]) -> dict[str, tuple[float, str]]:
    """Right-now temperature per city, fetched concurrently since these are
    independent I/O calls and doing them sequentially would add up to 6x the
    latency to every page load. Best-effort: a city that fails (network
    hiccup, Open-Meteo downtime) is just omitted rather than breaking the
    whole dashboard — this is a sanity-check overlay, not load-bearing data.
    """
    stations_by_city = {s.city: s for s in STATIONS.values() if s.city in cities}

    def fetch_one(city: str) -> tuple[str, tuple[float, str] | None]:
        station = stations_by_city[city]
        try:
            return city, fetch_current_temperature(station.latitude, station.longitude, station.standard_time_timezone)
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            print(f"  current-temp fetch failed for {city}: {exc.__class__.__name__}: {exc}")
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
_KNOWN_SCRIPTS = ["generate_alerts", "mark_settled_alerts", "send_notifications"]
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


@app.route("/")
def index():
    alerts, demo_reason = get_alerts()
    any_unvalidated = any(not a.calibration_validated for a in alerts)
    events = _group_by_event(alerts)
    all_cities = sorted({a.city for a in alerts})
    current_temps = _fetch_current_temps(all_cities)
    return render_template(
        "index.html",
        events=events,
        demo_reason=demo_reason,
        any_unvalidated=any_unvalidated,
        kelly_fraction=kelly_fraction_setting(),
        max_event_exposure=max_event_exposure_setting(),
        actionable_alerts=_actionable_alerts(alerts),
        current_temps=current_temps,
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
