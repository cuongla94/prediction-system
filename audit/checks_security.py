"""Security, deploy/infra, and decision-log hygiene checks.

Read-only: stat() and file reads, GET requests, and `git` commands that only
inspect (status/rev-parse/ls-remote). Nothing here writes, pushes, or fixes.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path

from .report import Finding, Status

CATEGORY_SECURITY = "Security"
CATEGORY_INFRA = "Deploy/infra"
CATEGORY_DECISIONS = "Decision log"

# Expected mode/owner for secret-bearing paths. Drift here is how a private key
# quietly becomes world-readable after a deploy, a restore, or a manual edit.
EXPECTED_PERMISSIONS = [
    ("/opt/kalshi-prediction-market/.env", 0o600, "kalshi"),
    ("/opt/kalshi-prediction-market/secrets/kalshi_predictions_private.pem", 0o600, "kalshi"),
    ("/etc/nginx/.htpasswd", 0o640, "root"),
]


def _owner(path: Path) -> str | None:
    try:
        import pwd

        return pwd.getpwuid(path.stat().st_uid).pw_name
    except (KeyError, OSError, ImportError):
        return None


def check_permissions(paths=EXPECTED_PERMISSIONS) -> Finding:
    problems: list[str] = []
    evidence: list[str] = []
    unchecked: list[str] = []

    for raw_path, expected_mode, expected_owner in paths:
        path = Path(raw_path)
        try:
            stat_result = path.stat()
        except OSError as exc:
            # Absent or unreadable is NOT a pass — it may be the very drift
            # being looked for, or the audit may lack privilege to see it.
            unchecked.append(f"`{raw_path}` could not be checked ({exc.__class__.__name__})")
            continue
        mode = stat_result.st_mode & 0o777
        owner = _owner(path)
        line = f"`{raw_path}` mode {mode:04o} owner {owner or '?'}"
        # Stricter than expected is fine; looser is the failure.
        if mode & ~expected_mode:
            problems.append(f"{line} — looser than expected {expected_mode:04o}")
        elif expected_owner and owner and owner != expected_owner:
            problems.append(f"{line} — expected owner {expected_owner}")
        else:
            evidence.append(line)

    if problems:
        return Finding(
            CATEGORY_SECURITY, "Secret file permissions", Status.FLAG,
            f"{len(problems)} secret-bearing path(s) have looser permissions than expected.",
            problems + evidence + unchecked,
        )
    if unchecked:
        return Finding(
            CATEGORY_SECURITY, "Secret file permissions", Status.UNKNOWN,
            f"{len(unchecked)} path(s) could not be checked — run the audit as root on the droplet "
            "for full coverage.",
            unchecked + evidence,
        )
    return Finding(
        CATEGORY_SECURITY, "Secret file permissions", Status.PASS,
        "All secret-bearing paths have the expected mode and owner.", evidence,
    )


# Endpoints intentionally reachable without a dashboard session.
KNOWN_UNAUTHENTICATED = {"login", "static"}


def check_unauthenticated_routes(app) -> Finding:
    """Any route reachable without a session beyond the known-safe two.

    Catches a new route added without considering the gate. `before_request`
    covers everything by default, so this is really guarding against someone
    adding an exemption.
    """
    exempt = set(KNOWN_UNAUTHENTICATED)
    routes = sorted(
        {rule.endpoint for rule in app.url_map.iter_rules() if rule.endpoint != "static"}
    )
    unexpected = [r for r in routes if r in exempt and r != "login"]

    # Exercise the gate for real rather than trusting the code by reading it.
    leaked: list[str] = []
    prior = os.environ.get("PASSCODES")
    os.environ["PASSCODES"] = "audit-probe-passcode"
    try:
        client = app.test_client()
        for rule in app.url_map.iter_rules():
            if rule.endpoint in exempt or "GET" not in (rule.methods or set()):
                continue
            if any(arg for arg in rule.arguments):
                continue
            response = client.get(rule.rule)
            if response.status_code == 200:
                leaked.append(f"`{rule.rule}` returned 200 without a session")
    finally:
        if prior is None:
            os.environ.pop("PASSCODES", None)
        else:
            os.environ["PASSCODES"] = prior

    evidence = [f"{len(routes)} routes checked; exempt by design: {', '.join(sorted(exempt))}"]
    if leaked or unexpected:
        return Finding(
            CATEGORY_SECURITY, "Unauthenticated routes", Status.FLAG,
            f"{len(leaked) + len(unexpected)} route(s) are reachable without authentication.",
            leaked + unexpected + evidence,
        )
    return Finding(
        CATEGORY_SECURITY, "Unauthenticated routes", Status.PASS,
        "Every route requires a session except the login page and static assets.", evidence,
    )


def check_dependency_cves(lock_path: Path, *, timeout: int = 30) -> Finding:
    """Installed dependency versions against the OSV.dev vulnerability database.

    OSV is used because it needs no account or API key (pip-audit is not a
    dependency here), and it covers PyPI advisories including CVEs.
    """
    try:
        lock_text = lock_path.read_text()
    except OSError as exc:
        return Finding(
            CATEGORY_SECURITY, "Dependency CVEs", Status.UNKNOWN,
            f"Could not read {lock_path} ({exc.__class__.__name__}).",
        )

    packages = re.findall(r'^name = "([^"]+)"\nversion = "([^"]+)"', lock_text, re.M)
    if not packages:
        return Finding(
            CATEGORY_SECURITY, "Dependency CVEs", Status.UNKNOWN,
            "Could not parse any package versions out of uv.lock.",
        )

    queries = [
        {"package": {"name": name, "ecosystem": "PyPI"}, "version": version}
        for name, version in packages
    ]
    request = urllib.request.Request(
        "https://api.osv.dev/v1/querybatch",
        data=json.dumps({"queries": queries}).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            results = json.load(response).get("results", [])
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        return Finding(
            CATEGORY_SECURITY, "Dependency CVEs", Status.UNKNOWN,
            f"Could not reach OSV.dev ({exc.__class__.__name__}) — vulnerabilities NOT checked.",
        )

    vulnerable: list[str] = []
    for (name, version), result in zip(packages, results, strict=False):
        ids = [v.get("id") for v in (result or {}).get("vulns", []) if v.get("id")]
        if ids:
            vulnerable.append(f"`{name}=={version}` — {', '.join(sorted(ids)[:5])}")

    evidence = [f"{len(packages)} packages queried against OSV.dev"]
    if vulnerable:
        return Finding(
            CATEGORY_SECURITY, "Dependency CVEs", Status.FLAG,
            f"{len(vulnerable)} dependency/ies have known advisories.",
            vulnerable + evidence,
        )
    return Finding(
        CATEGORY_SECURITY, "Dependency CVEs", Status.PASS,
        "No known advisories for any pinned dependency.", evidence,
    )


def check_basic_auth_patterns(log_path=Path("/var/log/nginx/access.log")) -> Finding:
    """Unusual basic-auth activity in nginx's access log.

    Looks for successful (non-401) requests from unfamiliar sources — scanners
    getting 401s are constant background noise and are NOT interesting; someone
    getting through is.
    """
    try:
        lines = log_path.read_text(errors="replace").splitlines()
    except OSError as exc:
        return Finding(
            CATEGORY_SECURITY, "Basic-auth access patterns", Status.UNKNOWN,
            f"Could not read {log_path} ({exc.__class__.__name__}) — run as root for this check.",
        )

    succeeded: Counter[str] = Counter()
    denied: Counter[str] = Counter()
    for line in lines:
        parts = line.split()
        if len(parts) < 9:
            continue
        ip, status = parts[0], parts[8]
        if status == "401":
            denied[ip] += 1
        elif status.startswith(("2", "3")):
            succeeded[ip] += 1

    evidence = [
        f"{len(lines):,} log lines; {len(succeeded)} IP(s) authenticated, "
        f"{len(denied)} IP(s) rejected ({sum(denied.values()):,} 401s)",
    ]
    if succeeded:
        evidence.append(
            "authenticated: " + ", ".join(f"{ip} ({n})" for ip, n in succeeded.most_common(5))
        )
    if denied:
        evidence.append(
            "top rejected: " + ", ".join(f"{ip} ({n})" for ip, n in denied.most_common(3))
        )

    # More than a couple of distinct successful sources on a single-user
    # dashboard is the thing worth a human look.
    if len(succeeded) > 2:
        return Finding(
            CATEGORY_SECURITY, "Basic-auth access patterns", Status.FLAG,
            f"{len(succeeded)} distinct IPs authenticated successfully — expected 1-2 for "
            "single-user access.",
            evidence,
        )
    return Finding(
        CATEGORY_SECURITY, "Basic-auth access patterns", Status.PASS,
        f"{len(succeeded)} IP(s) authenticated; rejected traffic is routine scanning.", evidence,
    )


def check_disk_and_memory(path: str = "/") -> Finding:
    usage = shutil.disk_usage(path)
    disk_pct = usage.used / usage.total * 100
    evidence = [
        f"disk {path}: {usage.used / 1e9:.1f} GB used of {usage.total / 1e9:.1f} GB "
        f"({disk_pct:.0f}%), {usage.free / 1e9:.1f} GB free"
    ]

    mem_available_pct = None
    try:
        meminfo = dict(
            (parts[0].rstrip(":"), int(parts[1]))
            for parts in (line.split() for line in Path("/proc/meminfo").read_text().splitlines())
            if len(parts) >= 2
        )
        total, available = meminfo.get("MemTotal"), meminfo.get("MemAvailable")
        if total and available:
            mem_available_pct = available / total * 100
            evidence.append(
                f"memory: {available / 1024:.0f} MB available of {total / 1024:.0f} MB "
                f"({mem_available_pct:.0f}%)"
            )
    except (OSError, ValueError):
        evidence.append("memory: /proc/meminfo unavailable (not Linux?)")

    problems = []
    if disk_pct > 85:
        problems.append(f"disk {disk_pct:.0f}% full")
    if mem_available_pct is not None and mem_available_pct < 15:
        problems.append(f"only {mem_available_pct:.0f}% memory available")

    if problems:
        return Finding(
            CATEGORY_INFRA, "Disk & memory headroom", Status.FLAG,
            "; ".join(problems), evidence,
        )
    return Finding(
        CATEGORY_INFRA, "Disk & memory headroom", Status.PASS,
        "Disk and memory both have comfortable headroom.", evidence,
    )


def _git(repo: Path, *args: str) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=30
        )
        return proc.returncode, (proc.stdout + proc.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, f"{exc.__class__.__name__}: {exc}"


def check_deploy_state(repo: Path) -> Finding:
    """Working tree cleanliness and whether the deployed commit matches origin/main.

    NOT a literal test push: this audit is read-only, and pushing would mutate
    the repository it is auditing. Verifying that HEAD equals origin/main and the
    tree is clean answers the same question — is what is deployed exactly what
    is committed — without changing anything.
    """
    code, head = _git(repo, "rev-parse", "HEAD")
    if code != 0:
        return Finding(
            CATEGORY_INFRA, "Deploy state", Status.UNKNOWN,
            f"Could not inspect the repository at {repo} — {head}",
        )

    _, status = _git(repo, "status", "--porcelain")
    _, remote = _git(repo, "ls-remote", "origin", "refs/heads/main")
    remote_sha = remote.split()[0] if remote and not remote.startswith(("fatal", "OSError")) else None

    evidence = [f"HEAD {head[:12]}"]
    problems: list[str] = []
    if status:
        dirty = [line for line in status.splitlines() if line.strip()]
        problems.append(f"working tree has {len(dirty)} uncommitted change(s)")
        evidence.extend(f"`{line}`" for line in dirty[:10])
    else:
        evidence.append("working tree clean")

    if remote_sha is None:
        evidence.append("could not reach origin to compare")
        return Finding(
            CATEGORY_INFRA, "Deploy state", Status.UNKNOWN,
            "Could not reach origin/main to confirm the deployed commit.", evidence,
        )
    evidence.append(f"origin/main {remote_sha[:12]}")
    if remote_sha != head:
        problems.append("deployed commit does not match origin/main")

    if problems:
        return Finding(
            CATEGORY_INFRA, "Deploy state", Status.FLAG, "; ".join(problems), evidence
        )
    return Finding(
        CATEGORY_INFRA, "Deploy state", Status.PASS,
        "Working tree is clean and matches origin/main.", evidence,
    )


def check_last_deploy_run(repo_slug: str = "cuongla94/prediction-system", *, timeout: int = 20) -> Finding:
    """The most recent CI/deploy run's conclusion, via the public Actions API."""
    url = f"https://api.github.com/repos/{repo_slug}/actions/runs?per_page=1"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            runs = json.load(response).get("workflow_runs", [])
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        return Finding(
            CATEGORY_INFRA, "Last deploy run", Status.UNKNOWN,
            f"Could not reach the GitHub Actions API ({exc.__class__.__name__}).",
        )
    if not runs:
        return Finding(
            CATEGORY_INFRA, "Last deploy run", Status.UNKNOWN, "No workflow runs found.",
        )

    run = runs[0]
    evidence = [
        f"{run.get('name')} for {run.get('head_sha', '')[:12]} — "
        f"{run.get('status')}/{run.get('conclusion')} at {run.get('created_at')}",
        run.get("html_url", ""),
    ]
    if run.get("conclusion") not in ("success", None):
        return Finding(
            CATEGORY_INFRA, "Last deploy run", Status.FLAG,
            f"The most recent workflow run concluded '{run.get('conclusion')}'.", evidence,
        )
    return Finding(
        CATEGORY_INFRA, "Last deploy run", Status.PASS,
        "The most recent workflow run succeeded.", evidence,
    )


def check_decision_log(memory_path: Path) -> Finding:
    """Decision-log hygiene: the DECIDED block must still be present and intact.

    Its whole purpose is to stop settled trade-offs (TLS accepted, rate limiting
    skipped) being re-reported as fresh discoveries by a later session. If that
    section goes missing, the guard is gone and the re-litigation starts again.
    """
    try:
        text = memory_path.read_text()
    except OSError as exc:
        return Finding(
            CATEGORY_DECISIONS, "DECIDED items intact", Status.UNKNOWN,
            f"Could not read {memory_path} ({exc.__class__.__name__}).",
        )

    required = {
        "DECIDED heading": "DECIDED",
        "TLS acceptance": "TLS: RISK ACCEPTED",
        "rate-limit decision": "rate limiting: SKIPPED",
    }
    missing = [name for name, needle in required.items() if needle.lower() not in text.lower()]
    evidence = [f"{memory_path} — {len(text.splitlines())} lines"]
    if missing:
        return Finding(
            CATEGORY_DECISIONS, "DECIDED items intact", Status.FLAG,
            f"{len(missing)} decision marker(s) missing — settled trade-offs may get re-flagged "
            "as new findings.",
            [f"missing: {m}" for m in missing] + evidence,
        )
    return Finding(
        CATEGORY_DECISIONS, "DECIDED items intact", Status.PASS,
        "Accepted/skipped decisions are still recorded and will not be re-flagged as new.",
        evidence,
    )
