"""Records one `pipeline_runs` row per script invocation, so the dashboard's
/status page can answer "is our system running well, any errors" from real
execution history instead of just inferring it from how fresh the newest
alert looks.

Usage:
    with track_run("generate_alerts") as run:
        ...do the work...
        run.summary = "36 alerts, 24 actionable across 6 cities"
        run.status = "partial"  # only if you need to override the default

Status defaults to "success" if the block exits normally, or "failed" (with
`detail` set to the exception) if it raises — the caller only needs to set
`status`/`summary` explicitly for a partial-success case (some cities failed
but the run still produced useful output) that wouldn't otherwise raise.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Iterator


@dataclass
class RunState:
    status: str = "success"
    summary: str | None = None
    detail: str | None = None


@contextmanager
def track_run(script_name: str) -> Iterator[RunState]:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        # No database, nothing to track against — the caller's work still
        # runs, this just can't record it.
        yield RunState()
        return

    import psycopg

    conn = psycopg.connect(database_url)
    run_id: int | None = None
    state = RunState()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "insert into pipeline_runs (script, status) values (%s, 'running') returning id",
                (script_name,),
            )
            run_id = cur.fetchone()[0]
        conn.commit()

        yield state
    except Exception as exc:
        state.status = "failed"
        state.detail = f"{exc.__class__.__name__}: {exc}"
        raise
    finally:
        if run_id is not None:
            with conn.cursor() as cur:
                cur.execute(
                    "update pipeline_runs set finished_at = %s, status = %s, summary = %s, "
                    "detail = %s where id = %s",
                    (datetime.now(UTC), state.status, state.summary, state.detail, run_id),
                )
            conn.commit()
        conn.close()
