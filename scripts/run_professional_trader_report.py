"""Read-only professional-trader evidence report and freeze generation."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg
from dotenv import load_dotenv

from trading_readiness.professional_report import run_professional_report


def main() -> int:
    load_dotenv()
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("DATABASE_URL is required.")
        return 1
    root = Path(__file__).resolve().parents[1]
    output = root / "artifacts" / "professional_trader"
    with psycopg.connect(database_url, connect_timeout=10) as connection:
        connection.execute("set transaction read only")
        result = run_professional_report(
            connection,
            output,
            root=root,
        )
    print(
        f"Professional trader: {result['status']['status']}; "
        f"decisions={result['status']['decision_snapshots']}; "
        f"production_order_submitted=false."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
