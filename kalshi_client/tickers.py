from __future__ import annotations

import re
from datetime import date

_EVENT_DATE_RE = re.compile(r"-(\d{2})([A-Z]{3})(\d{2})$")
_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


def parse_event_date(event_ticker: str) -> date:
    """Extracts the settlement date from a ticker like 'KXHIGHNY-26JUL18' -> 2026-07-18.

    Prefer this over `Event.strike_date` when you need the date reliably: strike_date
    is None on some live events (seen on Philadelphia/Denver) while every event
    ticker observed so far carries the date in this suffix.
    """
    match = _EVENT_DATE_RE.search(event_ticker)
    if not match:
        raise ValueError(f"Can't parse a date from event ticker {event_ticker!r}")
    yy, mon, dd = match.groups()
    if mon not in _MONTHS:
        raise ValueError(f"Unrecognized month abbreviation {mon!r} in event ticker {event_ticker!r}")
    return date(2000 + int(yy), _MONTHS[mon], int(dd))
