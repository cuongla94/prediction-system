from __future__ import annotations

import os

import psycopg

from .alert import Alert
from .demo_data import demo_alerts

_ALERT_COLUMNS = (
    "id, created_at, series_ticker, event_ticker, market_ticker, city, "
    "bracket_label, floor_strike, cap_strike, model_probability, ensemble_mean, "
    "ensemble_std, model_version, calibration_validated, market_yes_price, edge, "
    "fee_adjusted_threshold, rules_primary, rules_secondary, kalshi_url, "
    "is_actionable, status, settled_at, actual_high_temp, actual_outcome, close_time"
)


def get_alerts() -> tuple[list[Alert], str | None]:
    """Returns (alerts, demo_reason). demo_reason is None when reading a real DB."""
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        return demo_alerts(), "DATABASE_URL isn't set — showing sample data."

    try:
        with psycopg.connect(database_url, connect_timeout=5) as conn, conn.cursor() as cur:
            # The scheduler appends a new row every run rather than overwriting
            # (so history is preserved for backtesting) — DISTINCT ON collapses
            # that down to the latest snapshot per market before display.
            cur.execute(
                f"select {_ALERT_COLUMNS} from ("
                f"    select distinct on (market_ticker) {_ALERT_COLUMNS}"
                "     from alerts"
                "     where settled_at is null"
                "     order by market_ticker, created_at desc"
                ") latest "
                "order by is_actionable desc, abs(edge) desc, created_at desc"
            )
            rows = cur.fetchall()
            columns = [desc.name for desc in cur.description]
        alerts = []
        for row in rows:
            values = dict(zip(columns, row, strict=True))
            # psycopg returns timestamptz columns as datetime objects, not
            # strings — Alert's type hints (and the JS countdown timer reading
            # close_time) expect ISO8601 strings, so normalize here rather
            # than at every call site.
            if values["close_time"] is not None:
                values["close_time"] = values["close_time"].isoformat()
            alerts.append(Alert(**values))
        return alerts, None
    except psycopg.OperationalError as exc:
        return demo_alerts(), f"Couldn't connect to DATABASE_URL ({exc.__class__.__name__}) — showing sample data."
