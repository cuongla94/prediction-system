"""Read-only readiness audit plus reproducible local artifact generation."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg
from dotenv import load_dotenv

from trading_readiness.report import run_readiness_report


def main() -> int:
    load_dotenv()
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("DATABASE_URL is required.")
        return 1
    output_dir = Path(
        os.environ.get(
            "TRADING_READINESS_OUTPUT", "artifacts/trading_readiness"
        )
    )
    with psycopg.connect(database_url, connect_timeout=10) as connection:
        connection.execute("set transaction read only")
        result = run_readiness_report(connection, output_dir)
    print(
        f"Trading readiness: {result['overall_conclusion']}; "
        f"frozen candidates={result['candidate_count']}; "
        f"clusters={result['independent_cluster_count']}."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
