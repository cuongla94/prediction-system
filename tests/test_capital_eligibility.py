from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from capital.eligibility import evaluate_capital_eligibility

_NOW = datetime(2026, 7, 23, 12, 0, 0, tzinfo=UTC)
_FRESH = _NOW - timedelta(minutes=1)


def _eval(available_cash, **kwargs):
    kwargs.setdefault("balance_as_of", _FRESH)
    kwargs.setdefault("now", _NOW)
    return evaluate_capital_eligibility(available_cash=available_cash, **kwargs)


def test_zero_is_blocked():
    result = _eval(Decimal("0.00"))
    assert result.eligible is False
    assert result.reason_code == "INSUFFICIENT_KALSHI_CAPITAL"


def test_four_ninety_nine_is_blocked():
    result = _eval(Decimal("4.99"))
    assert result.eligible is False
    assert result.reason_code == "INSUFFICIENT_KALSHI_CAPITAL"


def test_exactly_five_dollars_is_blocked():
    """The gate is strictly exclusive -- $5.00 itself does not pass."""
    result = _eval(Decimal("5.00"))
    assert result.eligible is False
    assert result.reason_code == "INSUFFICIENT_KALSHI_CAPITAL"


def test_five_oh_one_passes():
    result = _eval(Decimal("5.01"))
    assert result.eligible is True
    assert result.reason_code == "OK"


def test_large_balance_passes():
    result = _eval(Decimal("1000.00"))
    assert result.eligible is True


def test_stale_balance_is_blocked_even_if_amount_would_pass():
    result = _eval(Decimal("100.00"), balance_as_of=_NOW - timedelta(hours=2))
    assert result.eligible is False
    assert result.reason_code == "KALSHI_BALANCE_STALE"


def test_missing_balance_as_of_is_treated_as_stale():
    result = _eval(Decimal("100.00"), balance_as_of=None)
    assert result.eligible is False
    assert result.reason_code == "KALSHI_BALANCE_STALE"


def test_unavailable_balance_is_blocked():
    result = _eval(None)
    assert result.eligible is False
    assert result.reason_code == "KALSHI_BALANCE_UNAVAILABLE"


def test_negative_balance_is_malformed():
    result = _eval(Decimal("-1.00"))
    assert result.eligible is False
    assert result.reason_code == "KALSHI_BALANCE_MALFORMED"


def test_unreconciled_state_is_blocked_even_with_ample_cash():
    result = _eval(Decimal("100.00"), reconciliation_healthy=False)
    assert result.eligible is False
    assert result.reason_code == "KALSHI_RECONCILIATION_UNHEALTHY"


def test_top_up_needed_is_display_only_and_never_changes_the_boundary():
    result = _eval(Decimal("4.75"))
    assert result.top_up_needed == Decimal("0.26")  # 5.01 - 4.75
    # The eligibility decision itself must never be based on top_up_needed —
    # only on the strict > $5.00 comparison.
    assert result.eligible is False
    assert result.comparison == "greater_than"
    assert result.minimum_available_cash_exclusive == Decimal("5.00")


def test_eligible_result_has_no_top_up_needed():
    result = _eval(Decimal("10.00"))
    assert result.top_up_needed is None


def test_to_dict_matches_expected_schema_shape():
    result = _eval(Decimal("4.75"))
    d = result.to_dict()
    assert d["environment"] == "prod"
    assert d["available_cash"] == "4.75"
    assert d["minimum_available_cash_exclusive"] == "5.00"
    assert d["comparison"] == "greater_than"
    assert d["eligible"] is False
    assert d["reason_code"] == "INSUFFICIENT_KALSHI_CAPITAL"
    assert "5.00" in d["message"]
