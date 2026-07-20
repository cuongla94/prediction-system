"""Finding model and markdown rendering for the recurring audit.

Read-only by construction: nothing in `audit/` writes to the database, calls a
mutating API, or edits a file other than the report it emits. The audit reports;
a human decides. That separation is the point — an auditor that silently fixes
things destroys the evidence of what was wrong.

Three outcomes, not two. UNKNOWN exists because "the check could not run" must
never be reported as PASS: the audit runs unattended, and a permission change
or a network blip that silently disabled a security check would otherwise look
identical to that check passing. Same discipline as the backtest gate's
UNTESTED verdict.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum


class Status(str, Enum):
    PASS = "PASS"
    FLAG = "FLAG"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class Finding:
    category: str
    check: str
    status: Status
    summary: str
    # Concrete observations backing the verdict — row counts, timestamps, file
    # modes, API responses. Required in spirit for anything not PASS: a flag
    # without evidence is an opinion, and this report exists to be acted on.
    evidence: list[str] = field(default_factory=list)

    @property
    def is_actionable(self) -> bool:
        return self.status in (Status.FLAG, Status.UNKNOWN)


def _icon(status: Status) -> str:
    return {Status.PASS: "PASS", Status.FLAG: "FLAG", Status.UNKNOWN: "UNKNOWN"}[status]


def render_report(findings: list[Finding], *, generated_at: datetime | None = None) -> str:
    now = generated_at or datetime.now(UTC)
    flagged = [f for f in findings if f.status is Status.FLAG]
    unknown = [f for f in findings if f.status is Status.UNKNOWN]
    passed = [f for f in findings if f.status is Status.PASS]

    lines: list[str] = []
    lines.append(f"# System audit — {now:%Y-%m-%d %H:%M} UTC")
    lines.append("")
    lines.append(
        "Read-only. Nothing here was changed or fixed automatically — this reports, you decide."
    )
    lines.append("")
    lines.append(
        f"**{len(passed)} pass · {len(flagged)} flag · {len(unknown)} unknown** "
        f"({len(findings)} checks)"
    )
    lines.append("")

    if flagged or unknown:
        lines.append("## Needs your attention")
        lines.append("")
        for finding in flagged + unknown:
            lines.append(f"### [{_icon(finding.status)}] {finding.category} — {finding.check}")
            lines.append("")
            lines.append(finding.summary)
            if finding.evidence:
                lines.append("")
                for item in finding.evidence:
                    lines.append(f"- {item}")
            lines.append("")
    else:
        lines.append("## Needs your attention")
        lines.append("")
        lines.append("Nothing. Every check passed.")
        lines.append("")

    lines.append("## All checks")
    lines.append("")
    lines.append("| Category | Check | Result | Summary |")
    lines.append("| --- | --- | --- | --- |")
    for finding in findings:
        summary = finding.summary.replace("|", "\\|").replace("\n", " ")
        lines.append(
            f"| {finding.category} | {finding.check} | {_icon(finding.status)} | {summary} |"
        )
    lines.append("")

    # Evidence for passing checks too, collapsed. A PASS with no visible basis
    # is indistinguishable from a check that did nothing, and this report is
    # meant to be trustworthy without re-deriving it by hand.
    with_evidence = [f for f in passed if f.evidence]
    if with_evidence:
        lines.append("<details>")
        lines.append("<summary>Evidence for passing checks</summary>")
        lines.append("")
        for finding in with_evidence:
            lines.append(f"**{finding.category} — {finding.check}**")
            lines.append("")
            for item in finding.evidence:
                lines.append(f"- {item}")
            lines.append("")
        lines.append("</details>")
        lines.append("")

    return "\n".join(lines)
