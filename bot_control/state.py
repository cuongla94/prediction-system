"""Persistent bot-control state: requested/effective mode, enabled/disabled,
kill switch. Fail-closed, same discipline as risk/controls.py's
is_real_money_enabled — if the database is unreachable or has no history
yet, the reported state is the safe default (OFF, disabled, kill switch
off), never inferred as anything more permissive.

The legacy generic mode switch implements only OFF and PAPER. Its
SHADOW/DEMO/LIVE_CANARY/LIVE requests remain audit-visible NOT_IMPLEMENTED
rejections so an old generic call cannot bypass production checks. LIVE is
persisted only by the dedicated `enable_live` path after fresh backend gates.

Worker health (last cycle started/completed/succeeded) is deliberately NOT
tracked here — scripts/run_paper_trading.py already records every run via
monitoring.track_run into `pipeline_runs`, and dashboard/app.py's
_pipeline_status() already reads it. Duplicating that into a second table
would just create two things that could disagree.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from db.connection import get_db
from paper_trading import STRATEGY_VERSION

logger = logging.getLogger(__name__)

# Not a strategy identity that varies — this project has exactly one
# strategy (day-ahead weather-bracket temperature prediction). Kept as an
# explicit constant, not inferred, so it has a stable name to display next
# to STRATEGY_VERSION regardless of how the underlying calibration evolves.
STRATEGY_NAME = "weather-daily-temp"

IMPLEMENTED_MODES = frozenset({"OFF", "PAPER"})
ALL_MODES = frozenset({"OFF", "PAPER", "SHADOW", "DEMO", "LIVE_CANARY", "LIVE"})


class BotControlError(Exception):
    """A request that can't be honored — an unrecognized or unimplemented
    mode, the kill switch being active, etc. `reason_code` is surfaced
    directly by the API layer, not re-derived from the message string."""

    def __init__(self, reason_code: str, message: str):
        self.reason_code = reason_code
        super().__init__(message)


@dataclass(frozen=True)
class BotState:
    effective_mode: str
    enabled: bool
    kill_switch: bool
    kill_switch_reason: str | None
    strategy_name: str | None
    strategy_version: str | None
    updated_at: datetime | None
    actor: str | None
    live_enabled: bool = False


_SAFE_DEFAULT = BotState(
    effective_mode="OFF",
    enabled=False,
    kill_switch=False,
    kill_switch_reason=None,
    strategy_name=None,
    strategy_version=None,
    updated_at=None,
    actor=None,
    live_enabled=False,
)


def get_bot_state() -> BotState:
    """Latest bot_control_events row, or the safe default if none exists yet
    or the database is unreachable. A freshly migrated database with no
    history is NOT evidence of a kill condition — it just hasn't been
    started, so this reads as OFF/disabled, not as killed."""
    db = get_db()
    if db is None:
        return _SAFE_DEFAULT
    try:
        cursor = db.cursor()
        try:
            cursor.execute(
                "select effective_mode, enabled, kill_switch, kill_switch_reason, "
                "strategy_name, strategy_version, created_at, actor "
                "from bot_control_events order by created_at desc limit 1"
            )
            row = cursor.fetchone()
        finally:
            cursor.close()
    except Exception as exc:
        logger.error(f"get_bot_state: {exc}, returning safe default")
        return _SAFE_DEFAULT
    finally:
        db.close()

    if row is None:
        return _SAFE_DEFAULT
    effective_mode, enabled, kill_switch, kill_switch_reason, strategy_name, strategy_version, created_at, actor = row
    return BotState(
        effective_mode=effective_mode,
        enabled=bool(enabled),
        kill_switch=bool(kill_switch),
        kill_switch_reason=kill_switch_reason,
        strategy_name=strategy_name,
        strategy_version=strategy_version,
        updated_at=created_at,
        actor=actor,
        live_enabled=effective_mode == "LIVE" and bool(enabled),
    )


def _record_event(
    *,
    event_type: str,
    requested_mode: str | None,
    effective_mode: str,
    enabled: bool,
    kill_switch: bool,
    kill_switch_reason: str | None,
    strategy_name: str | None,
    strategy_version: str | None,
    actor: str,
    reason_code: str,
    note: str | None,
    detail: str | None = None,
) -> BotState:
    db = get_db()
    if db is None:
        raise BotControlError("DATABASE_UNAVAILABLE", "Could not connect to the database.")
    try:
        cursor = db.cursor()
        try:
            cursor.execute(
                "insert into bot_control_events (event_type, requested_mode, effective_mode, enabled, live_enabled, "
                "kill_switch, kill_switch_reason, strategy_name, strategy_version, actor, reason_code, "
                "note, detail) values (%(event_type)s, %(requested_mode)s, %(effective_mode)s, "
                "%(enabled)s, %(live_enabled)s, %(kill_switch)s, %(kill_switch_reason)s, %(strategy_name)s, "
                "%(strategy_version)s, %(actor)s, %(reason_code)s, %(note)s, %(detail)s) "
                "returning created_at",
                dict(
                    event_type=event_type,
                    requested_mode=requested_mode,
                    effective_mode=effective_mode,
                    enabled=enabled,
                    live_enabled=effective_mode == "LIVE" and enabled,
                    kill_switch=kill_switch,
                    kill_switch_reason=kill_switch_reason,
                    strategy_name=strategy_name,
                    strategy_version=strategy_version,
                    actor=actor,
                    reason_code=reason_code,
                    note=note,
                    detail=detail,
                ),
            )
            created_at = cursor.fetchone()[0]
            db.commit()
        finally:
            cursor.close()
    except BotControlError:
        raise
    except Exception as exc:
        raise BotControlError("DATABASE_ERROR", f"Could not record bot-control event: {exc}") from exc
    finally:
        db.close()

    return BotState(
        effective_mode=effective_mode,
        enabled=enabled,
        kill_switch=kill_switch,
        kill_switch_reason=kill_switch_reason,
        strategy_name=strategy_name,
        strategy_version=strategy_version,
        updated_at=created_at,
        actor=actor,
        live_enabled=effective_mode == "LIVE" and enabled,
    )


def start_bot(
    requested_mode: str,
    *,
    actor: str,
    strategy_version: str = STRATEGY_VERSION,
    note: str | None = None,
) -> BotState:
    """Starts the bot in `requested_mode`. Only OFF and PAPER are
    implemented — any other value is recorded as a rejected request
    (audit-visible, not silently dropped) and raises BotControlError with
    reason_code NOT_IMPLEMENTED, leaving the current state untouched.
    """
    if requested_mode not in ALL_MODES:
        raise BotControlError(
            "INVALID_MODE", f"{requested_mode!r} is not a recognized execution mode."
        )

    current = get_bot_state()
    if current.kill_switch:
        raise BotControlError(
            "KILL_SWITCH_ACTIVE",
            f"The kill switch is active ({current.kill_switch_reason}). "
            "Reset it before starting the bot.",
        )

    if requested_mode not in IMPLEMENTED_MODES:
        _record_event(
            event_type="start_rejected",
            requested_mode=requested_mode,
            effective_mode=current.effective_mode,
            enabled=current.enabled,
            kill_switch=current.kill_switch,
            kill_switch_reason=current.kill_switch_reason,
            strategy_name=STRATEGY_NAME,
            strategy_version=strategy_version,
            actor=actor,
            reason_code="NOT_IMPLEMENTED",
            note=note or (
                f"{requested_mode} has no execution path in this codebase — see DECISIONS.md's "
                "\"Automated execution infrastructure\" decision."
            ),
        )
        raise BotControlError(
            "NOT_IMPLEMENTED",
            f"{requested_mode} has no execution path implemented — only OFF and PAPER are available.",
        )

    return _record_event(
        event_type="start_requested",
        requested_mode=requested_mode,
        effective_mode=requested_mode,
        enabled=(requested_mode != "OFF"),
        kill_switch=False,
        kill_switch_reason=None,
        strategy_name=STRATEGY_NAME,
        strategy_version=strategy_version,
        actor=actor,
        reason_code="OK",
        note=note,
    )


def stop_bot(*, actor: str, note: str | None = None) -> BotState:
    """Disables the bot (no new positions opened on the next cycle) without
    touching the kill switch — Stop and Emergency Stop are deliberately
    different actions (see trigger_kill_switch)."""
    current = get_bot_state()
    return _record_event(
        event_type="stop",
        requested_mode="OFF",
        effective_mode="OFF",
        enabled=False,
        kill_switch=current.kill_switch,
        kill_switch_reason=current.kill_switch_reason,
        strategy_name=current.strategy_name,
        strategy_version=current.strategy_version,
        actor=actor,
        reason_code="OK",
        note=note,
    )


def enable_live(
    *,
    actor: str,
    strategy_version: str = STRATEGY_VERSION,
    note: str | None = None,
) -> BotState:
    """Persist LIVE only after the API layer has completed fresh backend gates.

    Deliberately separate from `start_bot("LIVE")`, which remains rejected, so
    the older generic endpoint cannot bypass production validation.
    """
    current = get_bot_state()
    if current.kill_switch:
        raise BotControlError("KILL_SWITCH_ACTIVE", "The kill switch is active.")
    return _record_event(
        event_type="live_enable",
        requested_mode="LIVE",
        effective_mode="LIVE",
        enabled=True,
        kill_switch=False,
        kill_switch_reason=None,
        strategy_name=STRATEGY_NAME,
        strategy_version=strategy_version,
        actor=actor,
        reason_code="OK",
        note=note,
    )


def disable_live(*, actor: str, note: str | None = None) -> BotState:
    """Stop new live submissions without canceling existing resting orders."""
    current = get_bot_state()
    return _record_event(
        event_type="live_disable",
        requested_mode="OFF",
        effective_mode="OFF",
        enabled=False,
        kill_switch=current.kill_switch,
        kill_switch_reason=current.kill_switch_reason,
        strategy_name=current.strategy_name,
        strategy_version=current.strategy_version,
        actor=actor,
        reason_code="OK",
        note=note,
    )


def trigger_kill_switch(reason: str, *, actor: str) -> BotState:
    """Activates the persistent kill switch: disables the bot and blocks any
    future Start until reset_kill_switch is explicitly called."""
    current = get_bot_state()
    return _record_event(
        event_type="kill",
        requested_mode=None,
        effective_mode="OFF",
        enabled=False,
        kill_switch=True,
        kill_switch_reason=reason,
        strategy_name=current.strategy_name,
        strategy_version=current.strategy_version,
        actor=actor,
        reason_code="KILLED",
        note=reason,
    )


def reset_kill_switch(*, actor: str, note: str | None = None) -> BotState:
    """Clears the kill switch only — does NOT re-enable trading. An operator
    must explicitly call start_bot again after resolving the underlying
    issue; a reset that silently resumed trading would defeat the point of
    requiring an explicit backend reset."""
    current = get_bot_state()
    return _record_event(
        event_type="kill_reset",
        requested_mode=None,
        effective_mode="OFF",
        enabled=False,
        kill_switch=False,
        kill_switch_reason=None,
        strategy_name=current.strategy_name,
        strategy_version=current.strategy_version,
        actor=actor,
        reason_code="OK",
        note=note,
    )


def record_run_once(*, actor: str, detail: str) -> BotState:
    current = get_bot_state()
    return _record_event(
        event_type="run_once",
        requested_mode=None,
        effective_mode=current.effective_mode,
        enabled=current.enabled,
        kill_switch=current.kill_switch,
        kill_switch_reason=current.kill_switch_reason,
        strategy_name=current.strategy_name,
        strategy_version=current.strategy_version,
        actor=actor,
        reason_code="OK",
        note=None,
        detail=detail,
    )


def record_reconcile(*, actor: str, detail: str) -> BotState:
    current = get_bot_state()
    return _record_event(
        event_type="reconcile",
        requested_mode=None,
        effective_mode=current.effective_mode,
        enabled=current.enabled,
        kill_switch=current.kill_switch,
        kill_switch_reason=current.kill_switch_reason,
        strategy_name=current.strategy_name,
        strategy_version=current.strategy_version,
        actor=actor,
        reason_code="OK",
        note=None,
        detail=detail,
    )


def record_refresh_balance(*, actor: str, detail: str) -> BotState:
    current = get_bot_state()
    return _record_event(
        event_type="refresh_balance",
        requested_mode=None,
        effective_mode=current.effective_mode,
        enabled=current.enabled,
        kill_switch=current.kill_switch,
        kill_switch_reason=current.kill_switch_reason,
        strategy_name=current.strategy_name,
        strategy_version=current.strategy_version,
        actor=actor,
        reason_code="OK",
        note=None,
        detail=detail,
    )


def list_recent_events(limit: int = 50) -> list[dict]:
    """Recent bot_control_events rows, newest first — backs the
    /api/trading-bot/activity endpoint. Returns an empty list (not an
    error) when the database is unreachable, matching this module's
    fail-closed-but-don't-crash-the-page convention."""
    db = get_db()
    if db is None:
        return []
    try:
        cursor = db.cursor()
        try:
            cursor.execute(
                "select created_at, event_type, requested_mode, effective_mode, enabled, "
                "kill_switch, kill_switch_reason, actor, reason_code, note, detail "
                "from bot_control_events order by created_at desc limit %s",
                (limit,),
            )
            columns = [desc.name for desc in cursor.description]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]
        finally:
            cursor.close()
    except Exception as exc:
        logger.error(f"list_recent_events: {exc}")
        return []
    finally:
        db.close()
