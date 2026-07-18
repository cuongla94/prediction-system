from __future__ import annotations

import re


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def market_url(series_ticker: str, series_title: str, event_ticker: str) -> str:
    """Direct link to an event's bracket ladder on kalshi.com.

    Pattern: kalshi.com/markets/{series_ticker}/{slug}/{event_ticker}, all lowercase.
    The slug isn't a documented API field — it's derived from the series title, which
    matches Kalshi's own URLs for every case checked so far, but isn't guaranteed for
    titles with unusual punctuation. Verify against the live site if a link 404s.
    """
    return (
        f"https://kalshi.com/markets/{series_ticker.lower()}/"
        f"{slugify(series_title)}/{event_ticker.lower()}"
    )
