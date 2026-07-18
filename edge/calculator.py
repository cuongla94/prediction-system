"""Model probability vs. market price, net of fees. This is arithmetic on numbers
the weather engine and Kalshi client already produce — it doesn't judge whether
the model itself is any good. See kalshi-architecture-stack memory: nothing this
module labels "actionable" should be presented as a trading signal until the
backtest harness (build step 4) has checked it against real outcomes.
"""

from __future__ import annotations

from dataclasses import dataclass

from kalshi_client.fees import taker_fee

# Placeholder until step 4 can size this against actual historical hit rates
# instead of a guess.
DEFAULT_SAFETY_MARGIN = 0.02


@dataclass(frozen=True)
class EdgeResult:
    model_probability: float
    market_yes_price: float
    fee: float
    safety_margin: float
    threshold: float
    edge: float
    side: str
    is_actionable: bool


def compute_edge(
    model_probability: float,
    market_yes_price: float,
    safety_margin: float = DEFAULT_SAFETY_MARGIN,
) -> EdgeResult:
    """Model probability vs. market-implied probability, net of fees.

    `threshold` is the fee plus a safety margin — an alert needs a real edge past
    breakeven, not just enough to cover the fee exactly.
    """
    fee = taker_fee(market_yes_price)
    threshold = round(fee + safety_margin, 4)
    edge = round(model_probability - market_yes_price, 4)
    side = "YES" if edge > 0 else "NO" if edge < 0 else "FLAT"
    is_actionable = abs(edge) > threshold
    return EdgeResult(
        model_probability=model_probability,
        market_yes_price=market_yes_price,
        fee=fee,
        safety_margin=safety_margin,
        threshold=threshold,
        edge=edge,
        side=side,
        is_actionable=is_actionable,
    )


def bracket_sum_deviation(market_yes_prices: list[float]) -> float:
    """How far an event's summed bracket prices deviate from 1.0.

    A secondary signal, not a trade instruction on its own — same-day/same-city
    brackets are correlated (they're all bets on one underlying temperature), so
    this checks market-wide pricing consistency across an event rather than
    treating each bracket as an independent market.
    """
    return round(sum(market_yes_prices) - 1.0, 4)
