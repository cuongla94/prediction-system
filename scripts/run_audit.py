"""Recurring read-only system audit.

Runs every check in audit/, writes one dated markdown report, and changes
NOTHING. That is the whole design: an auditor that fixes things destroys the
evidence of what was wrong, and quietly converts "here is a problem" into
"here is a problem I already had an opinion about". This reports; you decide.

Exit code is 0 even when checks flag — a flag is a finding to read, not a
failure of the audit itself. Only an audit that could not run exits non-zero,
so the dead-man's-switch distinguishes "audit says something is wrong" from
"the audit never ran".

Usage: uv run scripts/run_audit.py [--output-dir logs/audit]
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent

from audit.checks_decisions import check_revisit_trigger  # noqa: E402
from audit.checks_pipeline import (  # noqa: E402
    check_alerts_growth,
    check_cron_cadence,
    check_failed_runs,
    check_healthcheck_delivery,
    check_schema_drift,
    check_settled_trades_against_kalshi,
    check_stuck_runs,
)
from audit.checks_security import (  # noqa: E402
    check_basic_auth_patterns,
    check_decision_log,
    check_dependency_cves,
    check_deploy_state,
    check_disk_and_memory,
    check_last_deploy_run,
    check_permissions,
    check_unauthenticated_routes,
)
from audit.report import Finding, Status, render_report  # noqa: E402

# DECISIONS.md lives in the repo, not in a local memory directory, specifically
# so this check works on the droplet too. Pointed at a laptop-only path it would
# report UNKNOWN every single week on the production host — and a permanent
# UNKNOWN is worse than no check, because it trains you to ignore the section
# it appears in.
DECISIONS_PATH = Path(os.environ.get("AUDIT_DECISIONS_PATH", REPO_ROOT / "DECISIONS.md"))


def _guard(category: str, name: str, fn, *args, **kwargs) -> Finding:
    """Run one check, converting an unexpected crash into an UNKNOWN finding.

    One broken check must not take down the whole report — and it must not be
    silently omitted either, which would read as "not a problem".
    """
    try:
        return fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 - deliberately broad; see docstring
        return Finding(
            category, name, Status.UNKNOWN,
            f"Check raised {exc.__class__.__name__}: {exc}",
        )


def collect_findings() -> list[Finding]:
    findings: list[Finding] = []
    database_url = os.environ.get("DATABASE_URL")

    if not database_url:
        findings.append(Finding(
            "Pipeline health", "Database reachable", Status.UNKNOWN,
            "DATABASE_URL is not set — every database-backed check was skipped.",
        ))
    else:
        import psycopg

        ca = os.environ.get("DATABASE_SSL_CA_FILE")
        connect_kwargs = {"connect_timeout": 10}
        if ca and Path(ca).exists():
            connect_kwargs |= {"sslmode": "verify-full", "sslrootcert": ca}
        try:
            with psycopg.connect(database_url, **connect_kwargs) as conn, conn.cursor() as cur:
                findings.append(_guard("Pipeline health", "Stuck runs", check_stuck_runs, cur))
                findings.append(_guard("Pipeline health", "Failed/partial runs", check_failed_runs, cur))
                findings.append(_guard("Pipeline health", "Cron cadence", check_cron_cadence, cur))
                findings.append(_guard("Data integrity", "Alerts growth vs retention", check_alerts_growth, cur))
                findings.append(_guard(
                    "Data integrity", "Schema drift", check_schema_drift, cur,
                    (REPO_ROOT / "db/schema.sql").read_text(),
                ))
                findings.append(_guard(
                    "Decision log", "Trading-mechanics revisit trigger", check_revisit_trigger, cur,
                ))

                # Needs both the DB and Kalshi; skipped cleanly if credentials
                # are absent rather than reported as a pass.
                try:
                    from kalshi_client import KalshiClient

                    with KalshiClient() as client:
                        findings.append(_guard(
                            "Data integrity", "Settled trades vs Kalshi",
                            check_settled_trades_against_kalshi, cur, client,
                        ))
                except Exception as exc:  # noqa: BLE001
                    findings.append(Finding(
                        "Data integrity", "Settled trades vs Kalshi", Status.UNKNOWN,
                        f"Could not reach Kalshi ({exc.__class__.__name__}: {exc}).",
                    ))
        except Exception as exc:  # noqa: BLE001
            findings.append(Finding(
                "Pipeline health", "Database reachable", Status.UNKNOWN,
                f"Could not connect to the database ({exc.__class__.__name__}: {exc}).",
            ))

    findings.append(_guard(
        "Pipeline health", "Healthcheck delivery", check_healthcheck_delivery, dict(os.environ),
    ))
    findings.append(_guard("Security", "Secret file permissions", check_permissions))

    try:
        from dashboard.app import app

        findings.append(_guard(
            "Security", "Unauthenticated routes", check_unauthenticated_routes, app,
        ))
    except Exception as exc:  # noqa: BLE001
        findings.append(Finding(
            "Security", "Unauthenticated routes", Status.UNKNOWN,
            f"Could not import the dashboard app ({exc.__class__.__name__}: {exc}).",
        ))

    findings.append(_guard(
        "Security", "Dependency CVEs", check_dependency_cves, REPO_ROOT / "uv.lock",
    ))
    findings.append(_guard("Security", "Basic-auth access patterns", check_basic_auth_patterns))
    findings.append(_guard("Deploy/infra", "Disk & memory headroom", check_disk_and_memory))
    findings.append(_guard("Deploy/infra", "Deploy state", check_deploy_state, REPO_ROOT))
    findings.append(_guard("Deploy/infra", "Last deploy run", check_last_deploy_run))
    findings.append(_guard("Decision log", "DECIDED items intact", check_decision_log, DECISIONS_PATH))
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
    dated = out_dir / f"audit-{now:%Y-%m-%d}.md"
    dated.write_text(report)
    # Stable path so "the latest audit" is always findable without globbing.
    (out_dir / "latest.md").write_text(report)

    flagged = sum(1 for f in findings if f.status is Status.FLAG)
    unknown = sum(1 for f in findings if f.status is Status.UNKNOWN)
    print(f"Audit complete: {len(findings)} checks, {flagged} flagged, {unknown} unknown")
    print(f"  {dated}")
    print(f"  {out_dir / 'latest.md'}")
    for finding in findings:
        if finding.is_actionable:
            print(f"  [{finding.status.value}] {finding.category} — {finding.check}: {finding.summary}")

    if args.stdout:
        print()
        print(report)
    # 0 even with flags: a flag is a finding, not an audit failure.
    return 0


if __name__ == "__main__":
    sys.exit(main())
