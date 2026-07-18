from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Alert:
    id: int
    created_at: str
    series_ticker: str
    event_ticker: str
    market_ticker: str
    city: str
    bracket_label: str
    floor_strike: float | None
    cap_strike: float | None
    model_probability: float
    ensemble_mean: float | None
    ensemble_std: float | None
    model_version: str
    calibration_validated: bool
    market_yes_price: float
    edge: float
    fee_adjusted_threshold: float
    rules_primary: str
    rules_secondary: str | None
    kalshi_url: str
    is_actionable: bool
    status: str
    settled_at: str | None
    actual_high_temp: float | None
    actual_outcome: bool | None
    close_time: str | None

    @property
    def side(self) -> str:
        if self.edge > 0:
            return "YES"
        if self.edge < 0:
            return "NO"
        return "FLAT"

    @property
    def win_probability(self) -> float:
        """Probability of winning the trade we'd actually recommend (the `side`
        position), not "probability the bracket resolves YES" — those are the
        same number only when side is YES. model_probability always answers
        the latter question regardless of which side has the edge, so a NO
        recommendation needs the complement to show "chance this trade
        pays off" rather than "chance the bracket itself happens."
        """
        if self.side == "NO":
            return 1 - self.model_probability
        return self.model_probability

    @property
    def question(self) -> str:
        """Kalshi-style natural-language phrasing of this bracket, for the card header."""
        if self.floor_strike is None and self.cap_strike is not None:
            return f"Will the highest temperature in {self.city} be under {self.cap_strike:g}°?"
        if self.cap_strike is None and self.floor_strike is not None:
            return f"Will the highest temperature in {self.city} be over {self.floor_strike:g}°?"
        if self.floor_strike is not None and self.cap_strike is not None:
            return (
                f"Will the highest temperature in {self.city} be "
                f"{self.floor_strike:g}–{self.cap_strike:g}°?"
            )
        return f"Will the highest temperature in {self.city} be {self.bracket_label}?"
