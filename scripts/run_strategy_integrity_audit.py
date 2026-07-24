"""Deep, read-only integrity audit of the weather-trading strategy itself.

Distinct from scripts/run_audit.py (system/pipeline health, run weekly). This
asks a narrower, harder question: is the strategy's own no-edge finding real,
or is it an artifact of a bug — a YES/NO inversion, a misparsed bracket, a
timezone shift, future-data leakage, double-counted trades, or a fee-threshold
error? Every check in audit/checks_strategy_integrity.py is read-only:
recomputation and comparison against already-stored values, never a rewrite
of live data or a re-run of the pipeline itself.

Changes NOTHING. Same discipline as run_audit.py: an auditor that silently
"fixes" what it finds destroys the evidence of what was wrong.

Usage: uv run scripts/run_strategy_integrity_audit.py [--output-dir logs/audit]
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent

from audit.checks_strategy_integrity import (  # noqa: E402
    check_bracket_boundary_parsing,
    check_edge_calculation_consistency,
    check_no_duplicate_paper_trades,
    check_observation_conditioning_lead_days_guard,
    check_reproduce_high_edge_zero_wins,
    check_station_timezone_convention,
    check_strategy_version_freshness,
)
from audit.report import Finding, Status, render_report  # noqa: E402


def _guard(category: str, name: str, fn, *args, **kwargs) -> Finding:
    """Same wrapper run_audit.py uses: one broken check becomes an UNKNOWN
    finding, not a crashed report — and it must not be silently omitted
    either, which would read as "not a problem"."""
    try:
        return fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 - deliberately broad; see docstring
        return Finding(
            category, name, Status.UNKNOWN,
            f"Check raised {exc.__class__.__name__}: {exc}",
        )


def collect_findings() -> list[Finding]:
    findings: list[Finding] = []
    category = "Strategy integrity"

    # Structural checks need no database — run them regardless of whether the
    # data-driven ones below can connect.
    findings.append(_guard(category, "Timezone/DST day-boundary convention", check_station_timezone_convention))
    findings.append(_guard(
        category, "No future-observation leakage", check_observation_conditioning_lead_days_guard,
    ))

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        findings.append(Finding(
            category, "Data-driven checks", Status.UNKNOWN,
            "DATABASE_URL is not set — every check that needs real settled-alert/paper-trade data was skipped.",
        ))
        return findings

    import psycopg

    ca = os.environ.get("DATABASE_SSL_CA_FILE")
    connect_kwargs = {"connect_timeout": 10}
    if ca and Path(ca).exists():
        connect_kwargs |= {"sslmode": "verify-full", "sslrootcert": ca}
    try:
        with psycopg.connect(database_url, **connect_kwargs) as conn, conn.cursor() as cur:
            findings.append(_guard(category, "Edge calculation consistency", check_edge_calculation_consistency, cur))
            findings.append(_guard(category, "Bracket boundary parsing", check_bracket_boundary_parsing, cur))
            findings.append(_guard(category, "No duplicate paper trades", check_no_duplicate_paper_trades, cur))
            findings.append(_guard(category, "Strategy version freshness", check_strategy_version_freshness, cur))
            findings.append(_guard(
                category, "High-claimed-edge win rate reproduction", check_reproduce_high_edge_zero_wins, cur,
            ))
    except Exception as exc:  # noqa: BLE001
        findings.append(Finding(
            category, "Database reachable", Status.UNKNOWN,
            f"Could not connect to the database ({exc.__class__.__name__}: {exc}).",
        ))

    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", default=str(REPO_ROOT / "logs/audit"))
    parser.add_argument("--stdout", action="store_true", help="also print the report")
    args = parser.parse_args()

    load_dotenv(REPO_ROOT / ".env")

    now = datetime.now(UTC)
    findings = collect_findings()
    report = render_report(findings, generated_at=now)

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    # Distinct filename prefix from run_audit.py's audit-YYYY-MM-DD.md/latest.md
    # so the two audits, run independently, never overwrite each other.
    dated = out_dir / f"strategy-integrity-{now:%Y-%m-%d}.md"
    dated.write_text(report)
    (out_dir / "strategy-integrity-latest.md").write_text(report)

    flagged = sum(1 for f in findings if f.status is Status.FLAG)
    unknown = sum(1 for f in findings if f.status is Status.UNKNOWN)
    print(f"Strategy integrity audit complete: {len(findings)} checks, {flagged} flagged, {unknown} unknown")
    print(f"  {dated}")
    print(f"  {out_dir / 'strategy-integrity-latest.md'}")
    for finding in findings:
        print(f"  [{finding.status.value}] {finding.category} — {finding.check}: {finding.summary}")

    if args.stdout:
        print()
        print(report)
    # 0 even with flags: a flag is a finding to read, not an audit failure.
    return 0


if __name__ == "__main__":
    sys.exit(main())
