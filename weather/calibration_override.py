"""Live fitted calibration that survives a deploy.

`weather/calibration_params.py` is *generated* — `scripts/fit_calibration_params.py`
rewrites the whole file — and it is also git-tracked. That combination broke the
moment push-to-deploy landed (2026-07-20): the droplet's deploy does
`git reset --hard`, so a scheduled refit writing into that tracked file would be
silently reverted by the next push to main. A weekly recalibration that quietly
undoes itself is worse than none, because nothing would surface it.

So the scheduled fit also writes `calibration_params_fitted.json` next to this
module, which is gitignored and therefore untouched by `git reset --hard`
(that command only rewrites *tracked* files; untracked ones are left alone —
`git clean` is what removes those, and no deploy step runs it).

Precedence: this override wins when present and parseable; the generated
`CALIBRATION` dict is the fallback. Keeping the generated module as a committed
baseline is deliberate, not redundant:

- it is a reviewed, known-good floor if the JSON is missing or corrupt
- a local `uv run scripts/fit_calibration_params.py` still produces a readable
  `git diff` you can inspect and commit to *promote* a fit to the baseline
- `dashboard/app.py`'s /backtest page compares the two and flags divergence, so
  "what is actually running right now" stays answerable rather than becoming an
  invisible property of one host's filesystem

Failure is always soft. A missing, unreadable, or malformed JSON falls back to
the committed baseline and warns once — this sits directly in the probability
path (`weather/probability.py` calls `get_calibration` per bracket), and a
scheduled job writing a bad file must never be able to take pricing down.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - import cycle guard, typing only
    from .calibration_params import CalibrationParams

OVERRIDE_FILENAME = "calibration_params_fitted.json"
OVERRIDE_PATH = Path(__file__).with_name(OVERRIDE_FILENAME)

# Cache keyed on the file's mtime rather than a plain lru_cache. The dashboard
# runs under gunicorn as a long-lived process, so a plain "read once at import"
# cache would keep serving last week's numbers until someone restarted the
# service — the cron scripts wouldn't notice (fresh process each run) but the
# UI would silently disagree with them. Re-reading on mtime change costs one
# stat() per call and keeps the two consistent.
_cache: tuple[float, dict[str, CalibrationParams]] | None = None
_warned: set[str] = set()


def _warn_once(message: str) -> None:
    """Warn at most once per distinct message per process — this is called from
    a per-bracket hot path, and a broken file would otherwise emit thousands of
    identical lines into the cron logs."""
    if message not in _warned:
        _warned.add(message)
        print(f"WARNING: {message}")


def _parse_params(raw: dict[str, Any]) -> CalibrationParams:
    from .calibration_params import CalibrationParams

    monthly = raw.get("monthly_bias")
    return CalibrationParams(
        overall_bias=float(raw["overall_bias"]),
        # JSON object keys are always strings; CalibrationParams.bias_for_month
        # looks up by int, so a missed conversion here would silently degrade
        # every monthly-bias city to its flat bias instead of erroring.
        monthly_bias={int(month): float(bias) for month, bias in monthly.items()} if monthly else None,
        std=float(raw["std"]),
        fit_date=str(raw.get("fit_date", "unknown")),
        fit_days=int(raw.get("fit_days", 0)),
    )


def load_override(path: Path | None = None) -> dict[str, CalibrationParams]:
    """Fitted params from the JSON override, or an empty dict if there is no
    usable one. Never raises — see the module docstring on why this has to fail
    soft."""
    global _cache
    target = path or OVERRIDE_PATH

    try:
        mtime = target.stat().st_mtime
    except OSError:
        # No override file at all is the normal, expected case (fresh checkout,
        # CI, any dev machine that has never run the fit). Not worth a warning.
        if path is None:
            _cache = None
        return {}

    if path is None and _cache is not None and _cache[0] == mtime:
        return _cache[1]

    try:
        blob = json.loads(target.read_text())
        parsed = {ticker: _parse_params(raw) for ticker, raw in blob["params"].items()}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        _warn_once(f"{target} is unreadable or malformed ({exc.__class__.__name__}: {exc}); using committed baseline.")
        if path is None:
            _cache = (mtime, {})
        return {}

    if path is None:
        _cache = (mtime, parsed)
    return parsed


def write_override(
    params_by_ticker: dict[str, CalibrationParams],
    *,
    fit_date: str,
    start_date: str,
    end_date: str,
    path: Path | None = None,
) -> Path:
    """Write the override atomically (temp file + `os.replace`).

    Atomicity matters here specifically: the weekly fit runs on the same box
    that is serving the dashboard and running the */15 settlement cron, so a
    plain truncate-and-write leaves a window where a concurrent reader sees a
    half-written file. `os.replace` is atomic on POSIX, so readers observe
    either the old file or the new one, never a partial one.
    """
    target = path or OVERRIDE_PATH
    payload = {
        "fit_date": fit_date,
        "start_date": start_date,
        "end_date": end_date,
        "params": {
            ticker: {
                "overall_bias": p.overall_bias,
                "monthly_bias": p.monthly_bias,
                "std": p.std,
                "fit_date": p.fit_date,
                "fit_days": p.fit_days,
            }
            for ticker, p in params_by_ticker.items()
        },
    }
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, target)
    return target


def override_metadata(path: Path | None = None) -> dict[str, Any] | None:
    """The override's own fit_date/start_date/end_date, for the dashboard's
    divergence banner. None when there's no usable override."""
    target = path or OVERRIDE_PATH
    try:
        blob = json.loads(target.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(blob, dict) or "params" not in blob:
        return None
    return {
        "fit_date": blob.get("fit_date"),
        "start_date": blob.get("start_date"),
        "end_date": blob.get("end_date"),
        "series_count": len(blob.get("params") or {}),
        "path": str(target),
    }


def clear_cache() -> None:
    """Drop the mtime cache. For tests, and for any caller that has just
    rewritten the override in-process and wants the next read to see it."""
    global _cache
    _cache = None
    _warned.clear()
