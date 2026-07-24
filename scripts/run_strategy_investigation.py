from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg
from dotenv import load_dotenv

from strategy_research.investigation import run_investigation


def main() -> int:
    load_dotenv()
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("DATABASE_URL is required for the collected-data investigation.")
        return 1
    output_dir = Path(
        os.environ.get(
            "STRATEGY_INVESTIGATION_OUTPUT",
            "artifacts/strategy_investigation",
        )
    )
    with psycopg.connect(database_url, connect_timeout=10) as connection:
        connection.execute("set transaction read only")
        summary = run_investigation(connection, output_dir)
    print(
        f"Investigation {summary['investigation_status']}: "
        f"{summary['input_rows']} canonical rows, "
        f"{summary['independent_events']} independent events, "
        f"best={summary['best_candidate']}, promotion={summary['promotion_status']}."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
