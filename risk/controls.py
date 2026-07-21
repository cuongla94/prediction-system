"""Trading control gate: master toggle for real-money trading.

Fails closed by design — if anything is unclear or unavailable, real money is
disabled.
"""

from __future__ import annotations

import logging
from typing import Optional

from db.connection import get_db

logger = logging.getLogger(__name__)


def is_real_money_enabled() -> bool:
    """Check whether real-money trading is enabled.

    Fails **closed** (returns False) in all error cases:
    - Database unreachable
    - Table doesn't exist
    - No rows in the table
    - Multiple conflicting rows (data integrity error)

    Returns:
        True only if exactly one row exists and it has real_money_trading_enabled=True
    """
    try:
        db = get_db()
        if db is None:
            logger.warning("is_real_money_enabled: database unavailable, returning False")
            return False

        cursor = db.cursor()
        try:
            cursor.execute("SELECT real_money_trading_enabled FROM trading_controls")
            rows = cursor.fetchall()
        finally:
            cursor.close()

        # No rows or table doesn't exist → disabled
        if not rows:
            return False

        # More than one row → data integrity error, fail closed
        if len(rows) > 1:
            logger.error(
                f"is_real_money_enabled: expected 0 or 1 rows, got {len(rows)}, "
                "returning False"
            )
            return False

        # Exactly one row — check the flag
        enabled = rows[0][0]
        return bool(enabled)

    except Exception as e:
        logger.error(f"is_real_money_enabled: {e}, returning False")
        return False


def set_real_money_enabled(
    enabled: bool, updated_by: Optional[str] = None, note: Optional[str] = None
) -> bool:
    """Flip the real-money trading toggle.

    Appends a new row to the audit log (does NOT mutate existing rows — history is
    preserved).

    Args:
        enabled: True to enable, False to disable
        updated_by: who made this change (e.g., session passcode label)
        note: reason for the change

    Returns:
        True if the update succeeded, False on error
    """
    try:
        db = get_db()
        if db is None:
            logger.error("set_real_money_enabled: database unavailable")
            return False

        cursor = db.cursor()
        try:
            cursor.execute(
                """
                INSERT INTO trading_controls (real_money_trading_enabled, updated_by, note)
                VALUES (%s, %s, %s)
                """,
                (enabled, updated_by, note),
            )
            db.commit()
            logger.info(
                f"set_real_money_enabled: {enabled} (updated_by={updated_by}, "
                f"note={note})"
            )
            return True
        finally:
            cursor.close()

    except Exception as e:
        logger.error(f"set_real_money_enabled: {e}")
        return False
