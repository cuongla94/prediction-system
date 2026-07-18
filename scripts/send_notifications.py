"""Push a notification for each bracket that just became worth a look.

Run after generate_alerts.py in the pipeline. A new `alerts` row gets
inserted every run even for a bracket that's remained actionable for days
(see kalshi-implementation-progress memory on why the schema works this
way), so "notify on every actionable row" would re-send the same signal
every few hours — this tracks `notified_at` per market and only notifies
once per market per calendar day.

Usage: uv run scripts/send_notifications.py
Requires PUSHOVER_APP_TOKEN and PUSHOVER_USER_KEY in the environment — see
notify/pushover.py for setup.
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, date, datetime
from typing import NamedTuple

from dotenv import load_dotenv

from monitoring import track_run
from notify.pushover import PushoverError, send_notification

_ALERT_COLUMNS = "id, city, bracket_label, edge, model_probability, kalshi_url, is_actionable"


class AlertCandidate(NamedTuple):
    id: int
    city: str
    bracket_label: str
    edge: float
    model_probability: float
    kalshi_url: str
    is_actionable: bool

    @property
    def side(self) -> str:
        if self.edge > 0:
            return "YES"
        if self.edge < 0:
            return "NO"
        return "FLAT"


def find_alerts_needing_notification(
    candidates: list[tuple[AlertCandidate, str]], already_notified_today: set[str]
) -> list[AlertCandidate]:
    """Which alerts should trigger a push notification right now.

    `candidates` is (alert, market_ticker) pairs rather than alerts alone so
    this stays decoupled from any particular row-fetching shape. Only
    actionable alerts, and only ones not already notified about today.
    """
    return [
        alert
        for alert, market_ticker in candidates
        if alert.is_actionable and market_ticker not in already_notified_today
    ]


def _format_message(alert: AlertCandidate) -> tuple[str, str]:
    title = f"{alert.city} {alert.bracket_label}: {alert.side}"
    message = (
        f"Edge {alert.edge * 100:+.1f}%, model {alert.model_probability * 100:.0f}% — "
        "review before trading, nothing here executes itself."
    )
    return title, message


def main() -> int:
    load_dotenv()
    import psycopg

    token = os.environ.get("PUSHOVER_APP_TOKEN")
    user_key = os.environ.get("PUSHOVER_USER_KEY")
    database_url = os.environ.get("DATABASE_URL")
    if not token or not user_key:
        print("PUSHOVER_APP_TOKEN / PUSHOVER_USER_KEY not set — see notify/pushover.py for setup. Skipping.")
        return 0
    if not database_url:
        print("DATABASE_URL not set — nothing to check. Skipping.")
        return 0

    today = date.today().isoformat()
    with track_run("send_notifications") as run, psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            f"select distinct on (market_ticker) market_ticker, {_ALERT_COLUMNS} from alerts "
            "where settled_at is null order by market_ticker, created_at desc"
        )
        rows = cur.fetchall()
        columns = ["market_ticker"] + [c.strip() for c in _ALERT_COLUMNS.split(",")]
        candidates = []
        for row in rows:
            values = dict(zip(columns, row, strict=True))
            market_ticker = values.pop("market_ticker")
            candidates.append((AlertCandidate(**values), market_ticker))

        cur.execute(
            "select distinct market_ticker from alerts "
            "where notified_at is not null and notified_at::date = %s",
            (today,),
        )
        already_notified_today = {r[0] for r in cur.fetchall()}

        ticker_by_id = {alert.id: ticker for alert, ticker in candidates}
        to_notify = find_alerts_needing_notification(candidates, already_notified_today)
        print(f"{len(to_notify)} alert(s) to notify (of {len(candidates)} unsettled, {len(already_notified_today)} already notified today).")

        sent = 0
        failed = 0
        for alert in to_notify:
            title, message = _format_message(alert)
            try:
                send_notification(token, user_key, title, message, url=alert.kalshi_url)
            except PushoverError as exc:
                print(f"  FAILED to notify for {ticker_by_id[alert.id]}: {exc}")
                failed += 1
                continue
            cur.execute("update alerts set notified_at = %s where id = %s", (datetime.now(UTC), alert.id))
            conn.commit()
            sent += 1
            print(f"  Notified: {title}")

        run.summary = f"Sent {sent} of {len(to_notify)} notifications"
        if failed:
            run.status = "partial" if sent else "failed"
            run.detail = f"{failed} notification(s) failed to send"

    print(f"Sent {sent} of {len(to_notify)} notifications.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
