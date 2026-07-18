from __future__ import annotations

from kalshi_client.fees import maker_fee, taker_fee


def test_taker_fee_peaks_at_fifty_cents():
    assert taker_fee(0.50) > taker_fee(0.30)
    assert taker_fee(0.50) > taker_fee(0.70)


def test_taker_fee_shrinks_toward_extremes():
    assert taker_fee(0.01) < taker_fee(0.10) < taker_fee(0.50)
    assert taker_fee(0.99) < taker_fee(0.90) < taker_fee(0.50)


def test_maker_fee_is_quarter_of_taker():
    assert maker_fee(0.40) == round(taker_fee(0.40) / 4, 4)


def test_fee_scales_with_contract_count():
    assert taker_fee(0.40, contracts=10) == round(taker_fee(0.40) * 10, 4)
