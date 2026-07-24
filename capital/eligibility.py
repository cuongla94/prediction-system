"""Authoritative production capital-eligibility gate.

The dashboard preflight, live enablement, and immediate pre-submit balance
check all use this one Decimal-based computation so the strict $5.00 boundary
cannot drift between call sites.

Uses Decimal throughout for the same reason every other money-handling
module in this project does (kalshi_client.fees, paper_trading.engine): a
$5.00 boundary comparison is exactly the kind of case where float rounding
could silently flip a pass/fail.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal

# The authoritative rule is strictly `available_cash > MINIMUM_AVAILABLE_CASH_EXCLUSIVE`.
# $5.00 itself is NOT eligible — "exclusive" in the name is load-bearing.
MINIMUM_AVAILABLE_CASH_EXCLUSIVE = Decimal("5.00")

# Display-only: the smallest balance that WOULD pass, used only to compute a
# "how much more do you need" figure for the UI. Never used in the
# eligibility decision itself — that's always the strict `>` comparison above.
_MINIMUM_DISPLAY_BALANCE = Decimal("5.01")

# A balance older than this is treated as stale rather than authoritative —
# matches this project's other "don't trust old data for a live decision"
# conventions (e.g. observed_so_far's freshness handling in
# weather/probability.py).
MAX_BALANCE_AGE = timedelta(minutes=15)

ReasonCode = Literal[
    "OK",
    "KALSHI_BALANCE_UNAVAILABLE",
    "KALSHI_BALANCE_MALFORMED",
    "KALSHI_BALANCE_STALE",
    "KALSHI_RECONCILIATION_UNHEALTHY",
    "INSUFFICIENT_KALSHI_CAPITAL",
]


@dataclass(frozen=True)
class CapitalEligibility:
    environment: str
    available_cash: Decimal | None
    minimum_available_cash_exclusive: Decimal
    comparison: str
    eligible: bool
    balance_as_of: datetime | None
    balance_fresh: bool
    reconciliation_healthy: bool
    reason_code: ReasonCode
    message: str
    top_up_needed: Decimal | None

    def to_dict(self) -> dict:
        return {
            "environment": self.environment,
            "available_cash": str(self.available_cash) if self.available_cash is not None else None,
            "minimum_available_cash_exclusive": str(self.minimum_available_cash_exclusive),
            "comparison": self.comparison,
            "eligible": self.eligible,
            "balance_as_of": self.balance_as_of.isoformat() if self.balance_as_of else None,
            "balance_fresh": self.balance_fresh,
            "reconciliation_healthy": self.reconciliation_healthy,
            "reason_code": self.reason_code,
            "message": self.message,
            "top_up_needed": str(self.top_up_needed) if self.top_up_needed is not None else None,
        }


def _blocked(
    environment: str,
    reason_code: ReasonCode,
    message: str,
    *,
    available_cash: Decimal | None = None,
    balance_as_of: datetime | None = None,
    balance_fresh: bool = False,
    reconciliation_healthy: bool = True,
) -> CapitalEligibility:
    return CapitalEligibility(
        environment=environment,
        available_cash=available_cash,
        minimum_available_cash_exclusive=MINIMUM_AVAILABLE_CASH_EXCLUSIVE,
        comparison="greater_than",
        eligible=False,
        balance_as_of=balance_as_of,
        balance_fresh=balance_fresh,
        reconciliation_healthy=reconciliation_healthy,
        reason_code=reason_code,
        message=message,
        top_up_needed=None,
    )


def evaluate_capital_eligibility(
    *,
    environment: str = "prod",
    available_cash: Decimal | None,
    balance_as_of: datetime | None,
    reconciliation_healthy: bool = True,
    now: datetime | None = None,
) -> CapitalEligibility:
    """The single authoritative eligibility computation — every caller (a
    future API endpoint, the dashboard panel, any future gate) should go
    through this, so there is exactly one place that can get the $5.00
    boundary wrong, not one per caller.

    `available_cash` must be the latest successfully reconciled production
    available-cash balance (Kalshi's own GET /portfolio/balance,
    `balance_dollars` field — see kalshi_client.models.Balance /
    KalshiClient.get_balance). Never portfolio value, open-position market
    value, paper cash, demo cash, a stale cached figure, or (especially) a
    frontend-submitted value — this function has no way to enforce that on
    its own, so callers own that responsibility; it is documented here
    because getting it wrong here would defeat the entire point of the gate.
    """
    now = now or datetime.now(UTC)

    if available_cash is None:
        return _blocked(
            environment, "KALSHI_BALANCE_UNAVAILABLE",
            "Could not fetch the current Kalshi available-cash balance.",
        )
    if available_cash < 0:
        return _blocked(
            environment, "KALSHI_BALANCE_MALFORMED",
            f"Kalshi returned a malformed available-cash balance (${available_cash}).",
            available_cash=available_cash,
        )
    if not reconciliation_healthy:
        return _blocked(
            environment, "KALSHI_RECONCILIATION_UNHEALTHY",
            "Local state has not been successfully reconciled against the real Kalshi account.",
            available_cash=available_cash, balance_as_of=balance_as_of, reconciliation_healthy=False,
        )
    if balance_as_of is None or (now - balance_as_of) > MAX_BALANCE_AGE:
        return _blocked(
            environment, "KALSHI_BALANCE_STALE",
            "The most recent available-cash balance is too old to trust for a live decision.",
            available_cash=available_cash, balance_as_of=balance_as_of,
        )

    eligible = available_cash > MINIMUM_AVAILABLE_CASH_EXCLUSIVE
    if eligible:
        return CapitalEligibility(
            environment=environment,
            available_cash=available_cash,
            minimum_available_cash_exclusive=MINIMUM_AVAILABLE_CASH_EXCLUSIVE,
            comparison="greater_than",
            eligible=True,
            balance_as_of=balance_as_of,
            balance_fresh=True,
            reconciliation_healthy=True,
            reason_code="OK",
            message=f"Available cash (${available_cash}) exceeds the $5.00 minimum.",
            top_up_needed=None,
        )

    top_up_needed = max(Decimal("0"), _MINIMUM_DISPLAY_BALANCE - available_cash)
    return CapitalEligibility(
        environment=environment,
        available_cash=available_cash,
        minimum_available_cash_exclusive=MINIMUM_AVAILABLE_CASH_EXCLUSIVE,
        comparison="greater_than",
        eligible=False,
        balance_as_of=balance_as_of,
        balance_fresh=True,
        reconciliation_healthy=True,
        reason_code="INSUFFICIENT_KALSHI_CAPITAL",
        message=(
            f"Automated live trading requires more than $5.00 in available Kalshi cash. "
            f"Current available cash is ${available_cash}."
        ),
        top_up_needed=top_up_needed,
    )
