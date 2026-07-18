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

    @property
    def side(self) -> str:
        if self.edge > 0:
            return "YES"
        if self.edge < 0:
            return "NO"
        return "FLAT"
