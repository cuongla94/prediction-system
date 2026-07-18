from __future__ import annotations


def taker_fee(price: float, contracts: int = 1) -> float:
    """Kalshi's taker fee per contract: 7% * price * (1 - price). Price in [0, 1]."""
    return round(0.07 * price * (1 - price) * contracts, 4)


def maker_fee(price: float, contracts: int = 1) -> float:
    """Resting maker orders pay roughly a quarter of the taker fee."""
    return round(taker_fee(price, contracts) / 4, 4)
