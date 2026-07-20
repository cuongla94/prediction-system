from __future__ import annotations

from datetime import UTC, datetime, timedelta


from audit.checks_pipeline import (
    check_alerts_growth,
    check_cron_cadence,
    check_failed_runs,
    check_healthcheck_delivery,
    check_schema_drift,
    check_stuck_runs,
)
from audit.checks_security import check_decision_log, check_disk_and_memory, check_permissions
from audit.report import Finding, Status, render_report

NOW = datetime.now(UTC)


class FakeCursor:
    """Returns canned rows per query, so checks are testable without a database."""

    def __init__(self, responses: list):
        self._responses = list(responses)
        self._current: list = []

    def execute(self, _sql, _params=None):
        self._current = self._responses.pop(0) if self._responses else []

    def fetchall(self):
        return self._current

    def fetchone(self):
        return self._current[0] if self._current else None


# --- report rendering ------------------------------------------------------


def test_report_leads_with_actionable_findings():
    findings = [
        Finding("Cat", "ok", Status.PASS, "fine"),
        Finding("Cat", "bad", Status.FLAG, "broken", ["evidence line"]),
    ]
    report = render_report(findings, generated_at=NOW)
    assert report.index("Needs your attention") < report.index("All checks")
    assert "evidence line" in report
    assert "1 pass · 1 flag · 0 unknown" in report


def test_report_says_so_when_everything_passes():
    report = render_report([Finding("Cat", "ok", Status.PASS, "fine")], generated_at=NOW)
    assert "Nothing. Every check passed." in report


def test_unknown_is_actionable_and_never_reads_as_pass():
    # A check that could not run must not be mistaken for a passing one.
    assert Finding("C", "c", Status.UNKNOWN, "x").is_actionable
    assert not Finding("C", "c", Status.PASS, "x").is_actionable
    report = render_report([Finding("C", "c", Status.UNKNOWN, "could not check")], generated_at=NOW)
    assert "Needs your attention" in report
    assert "UNKNOWN" in report


def test_pipe_characters_in_summary_do_not_break_the_table():
    report = render_report([Finding("C", "c", Status.PASS, "a | b")], generated_at=NOW)
    (row,) = [line for line in report.splitlines() if line.startswith("| C |")]
    # The pipe must be escaped, not raw — an unescaped one would split the cell
    # and shift every column after it.
    assert r"a \| b" in row
    # 5 structural delimiters: leading, 3 between cells, trailing. The escaped
    # pipe is not one of them.
    assert row.count("|") - row.count(r"\|") == 5


# --- pipeline health -------------------------------------------------------


def test_stuck_runs_flags_old_running_rows():
    cur = FakeCursor([[(20, "generate_alerts", NOW - timedelta(days=2))]])
    finding = check_stuck_runs(cur)
    assert finding.status is Status.FLAG
    assert "id=20" in finding.evidence[0]


def test_stuck_runs_passes_when_none():
    assert check_stuck_runs(FakeCursor([[]])).status is Status.PASS


def test_failed_runs_flags_and_counts():
    cur = FakeCursor([[("validate_against_noaa", "failed", 2, NOW), ("run_backtest", "partial", 1, NOW)]])
    finding = check_failed_runs(cur)
    assert finding.status is Status.FLAG
    assert "3 failed/partial" in finding.summary


def test_cron_cadence_flags_a_script_that_never_ran():
    cur = FakeCursor([[(None,)]] * 4)
    finding = check_cron_cadence(cur)
    assert finding.status is Status.FLAG
    assert any("never run" in line for line in finding.evidence)


def test_cron_cadence_passes_when_recent():
    cur = FakeCursor([[(NOW - timedelta(minutes=1),)]] * 4)
    assert check_cron_cadence(cur).status is Status.PASS


def test_cron_cadence_tolerates_a_single_skipped_tick():
    # 30 min gap on a 15-min cadence is within the 3x tolerance.
    cur = FakeCursor([[(NOW - timedelta(minutes=30),)]] * 4)
    assert check_cron_cadence(cur).status is Status.PASS


def test_healthcheck_delivery_is_unknown_not_pass_when_unset():
    # Unset pings no-op silently, so an unconfigured box looks identical to a
    # healthy one. That must never read as PASS.
    finding = check_healthcheck_delivery({})
    assert finding.status is Status.UNKNOWN
    assert len(finding.evidence) == 3


def test_healthcheck_delivery_passes_when_all_configured():
    env = {
        "HEALTHCHECK_PIPELINE_URL": "https://hc-ping.com/a",
        "HEALTHCHECK_SETTLEMENT_URL": "https://hc-ping.com/b",
        "HEALTHCHECK_RECALIBRATION_URL": "https://hc-ping.com/c",
    }
    assert check_healthcheck_delivery(env).status is Status.PASS


# --- data integrity --------------------------------------------------------


def _growth_cursor(daily_rows, *, alerts_bytes, db_bytes, row_count):
    return FakeCursor([
        [(alerts_bytes, db_bytes)],
        [(row_count,)],
        daily_rows,
    ])


def test_alerts_growth_excludes_the_partial_first_day():
    # The first day a table exists is partial by construction. Including it
    # understates growth and inflates the runway -- an optimistic capacity
    # warning is the worst kind. Day 1 = 252 rows, day 2+ = 1800.
    daily = [(datetime(2026, 7, 18).date(), 252)] + [
        (datetime(2026, 7, 18 + i).date(), 1800) for i in range(1, 5)
    ]
    finding = _growth_cursor(daily, alerts_bytes=6_000_000, db_bytes=17_000_000, row_count=3864)
    result = check_alerts_growth(finding)
    rate_line = next(line for line in result.evidence if "rows/day" in line)
    assert "1,800 rows/day" in rate_line, rate_line


def test_alerts_growth_flags_a_short_runway():
    daily = [(datetime(2026, 7, 18 + i).date(), 100_000) for i in range(5)]
    result = check_alerts_growth(
        _growth_cursor(daily, alerts_bytes=400_000_000, db_bytes=480_000_000, row_count=1_000_000)
    )
    assert result.status is Status.FLAG
    assert "headroom" in result.summary


def test_alerts_growth_marks_a_thin_estimate_as_provisional():
    daily = [(datetime(2026, 7, 18).date(), 252), (datetime(2026, 7, 19).date(), 1800)]
    result = check_alerts_growth(
        _growth_cursor(daily, alerts_bytes=6_000_000, db_bytes=17_000_000, row_count=3864)
    )
    assert any("provisional" in line for line in result.evidence)


def test_schema_drift_flags_a_column_missing_from_schema_sql():
    cur = FakeCursor([[("alerts", "id"), ("alerts", "sneaky_new_column")]])
    finding = check_schema_drift(cur, "create table alerts (id bigint);")
    assert finding.status is Status.FLAG
    assert any("sneaky_new_column" in line for line in finding.evidence)


def test_schema_drift_passes_when_everything_is_declared():
    cur = FakeCursor([[("alerts", "id"), ("alerts", "city")]])
    assert check_schema_drift(cur, "create table alerts (id bigint, city text);").status is Status.PASS


# --- security & infra ------------------------------------------------------


def test_permissions_flags_a_world_readable_secret(tmp_path):
    secret = tmp_path / "secret"
    secret.write_text("x")
    secret.chmod(0o644)
    finding = check_permissions([(str(secret), 0o600, None)])
    assert finding.status is Status.FLAG
    assert "looser" in finding.evidence[0]


def test_permissions_accepts_stricter_than_expected(tmp_path):
    secret = tmp_path / "secret"
    secret.write_text("x")
    secret.chmod(0o400)
    assert check_permissions([(str(secret), 0o600, None)]).status is Status.PASS


def test_permissions_unreadable_path_is_unknown_not_pass(tmp_path):
    finding = check_permissions([(str(tmp_path / "absent"), 0o600, None)])
    assert finding.status is Status.UNKNOWN


def test_disk_and_memory_reports_evidence():
    finding = check_disk_and_memory("/")
    assert finding.status in (Status.PASS, Status.FLAG)
    assert any("disk" in line for line in finding.evidence)


def test_decision_log_flags_when_decided_markers_vanish(tmp_path):
    doc = tmp_path / "followups.md"
    doc.write_text("# notes\n\nnothing decided here\n")
    finding = check_decision_log(doc)
    assert finding.status is Status.FLAG
    assert len(finding.evidence) >= 3


def test_decision_log_passes_when_markers_present(tmp_path):
    doc = tmp_path / "followups.md"
    doc.write_text("## DECIDED\n- TLS: RISK ACCEPTED, 2026-07-20\n- Login rate limiting: SKIPPED\n")
    assert check_decision_log(doc).status is Status.PASS


def test_decision_log_missing_file_is_unknown(tmp_path):
    assert check_decision_log(tmp_path / "nope.md").status is Status.UNKNOWN
