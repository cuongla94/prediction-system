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
    "is_actionable, status, settled_at, actual_high_temp, actual_outcome"
)


def get_alerts() -> tuple[list[Alert], str | None]:
    """Returns (alerts, demo_reason). demo_reason is None when reading a real DB."""
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        return demo_alerts(), "DATABASE_URL isn't set — showing sample data."

    try:
        with psycopg.connect(database_url, connect_timeout=5) as conn, conn.cursor() as cur:
            cur.execute(
                f"select {_ALERT_COLUMNS} from alerts "
                "order by is_actionable desc, abs(edge) desc, created_at desc"
            )
            rows = cur.fetchall()
            columns = [desc.name for desc in cur.description]
        return [Alert(**dict(zip(columns, row, strict=True))) for row in rows], None
    except psycopg.OperationalError as exc:
        return demo_alerts(), f"Couldn't connect to DATABASE_URL ({exc.__class__.__name__}) — showing sample data."
