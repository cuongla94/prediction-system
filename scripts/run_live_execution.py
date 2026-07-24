"""One idempotent production reconciliation/execution cycle.

This script is invoked by the repository's existing cron wrappers. It does
not install or own a scheduler, and it does not submit unless persisted bot
state is explicitly LIVE and every backend gate passes.
"""

from __future__ import annotations

import sys

from dotenv import load_dotenv

from live_trading.service import run_live_cycle_from_env


def main() -> int:
    load_dotenv()
    result = run_live_cycle_from_env()
    print(
        f"Live cycle: {result.status}; submitted={result.submitted_orders}; "
        f"reconciled={result.reconciled_orders}; canceled={result.canceled_orders}; "
        f"blocker={result.blocker or 'none'}; error={result.error or 'none'}"
    )
    return 1 if result.status == "ERROR" else 0


if __name__ == "__main__":
    sys.exit(main())
