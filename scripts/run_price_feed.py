"""Entry point for the WebSocket price-feed subscriber — runs forever, never
exits on its own. Meant to run under a dedicated systemd unit (see
deploy/kalshi-price-feed.service), not the cron pipeline: this needs to stay
connected continuously, not fire on a schedule.

Usage: uv run scripts/run_price_feed.py
"""

from __future__ import annotations

import asyncio
import os
import sys

from dotenv import load_dotenv

from price_feed.subscriber import run_forever


def main() -> int:
    load_dotenv()
    if os.environ.get("FORWARD_EVIDENCE_ENABLED", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        from scripts.run_forward_evidence_collector import run

        return run("stream")
    asyncio.run(run_forever())
    return 0


if __name__ == "__main__":
    sys.exit(main())
