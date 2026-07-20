from __future__ import annotations

from dataclasses import dataclass

# The 6 cities this system started with, on Kalshi's older "Daily temperature"
# naming convention ("Highest temperature in {city}"). The 14 cities added
# 2026-07-19 use a newer convention ("{city} High Temperature Daily") and are
# shown as a visually distinct "HIGH TEMPERATURE" label on Kalshi's own site.
# Both settle identically (same NWS CLI report mechanism, same rules-text
# format) — confirmed live, not assumed — this split is purely cosmetic,
# mirroring Kalshi's own per-card label rather than a real structural
# difference. See kalshi-project-scope memory for the full research.
_ORIGINAL_SIX_CITIES = frozenset({"NYC", "Chicago", "Philadelphia", "Austin", "Denver", "Miami"})


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
    metric: str = "max"  # "max" (highest temperature) | "min" (lowest temperature)
    # Days between this alert's own event date and "today," in the station's
    # local calendar — 0 = same-day, 1 = tomorrow (the lead time this
    # project's calibration is actually fit for). None for rows written
    # before this column existed (2026-07-19) or wherever it can't be
    # determined; treated as same-day (the cautious default) by anything
    # that branches on it, same reasoning as the paper-trading bot's
    # same-day exclusion.
    lead_days: int | None = None

    @property
    def is_same_day(self) -> bool:
        """True for lead_days == 0 or unknown (None) — the cautious default,
        matching paper_trading/engine.py::_is_same_day: can't confirm it's
        *not* same-day, so treat it as same-day."""
        return self.lead_days is None or self.lead_days == 0

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
    def metric_label(self) -> str:
        """"highest"/"lowest", for natural-language phrasing — see `metric`."""
        return "lowest" if self.metric == "min" else "highest"

    @property
    def category(self) -> str:
        """Machine-readable category slug, feeding the dashboard's category
        filter and each card's category tag. Three values, matching what
        Kalshi's own site shows per card (see _ORIGINAL_SIX_CITIES above):
        "daily-temperature" (original 6, high), "high-temperature" (the 14
        newer cities, high), "low-temperature" (all 20, low)."""
        if self.metric == "min":
            return "low-temperature"
        return "daily-temperature" if self.city in _ORIGINAL_SIX_CITIES else "high-temperature"

    @property
    def category_label(self) -> str:
        """Human-readable form of `category`, for the card tag/filter text."""
        return {
            "daily-temperature": "Daily temperature",
            "high-temperature": "High temperature",
            "low-temperature": "Low temperature",
        }[self.category]

    @property
    def question(self) -> str:
        """Kalshi-style natural-language phrasing of this bracket, for the card header."""
        label = self.metric_label
        if self.floor_strike is None and self.cap_strike is not None:
            return f"Will the {label} temperature in {self.city} be under {self.cap_strike:g}°?"
        if self.cap_strike is None and self.floor_strike is not None:
            return f"Will the {label} temperature in {self.city} be over {self.floor_strike:g}°?"
        if self.floor_strike is not None and self.cap_strike is not None:
            return (
                f"Will the {label} temperature in {self.city} be "
                f"{self.floor_strike:g}–{self.cap_strike:g}°?"
            )
        return f"Will the {label} temperature in {self.city} be {self.bracket_label}?"


@dataclass(frozen=True)
class ForecastPreview:
    """An informational-only forecast for a date Kalshi hasn't opened a
    market for yet — no bracket structure or market price exists to compute
    a real edge against, so this is a calibrated expected range, not an
    edge/probability. See db/schema.sql's forecast_previews table."""

    series_ticker: str
    city: str
    metric: str
    target_date: str
    lead_days: int
    ensemble_mean: float
    ensemble_std: float
    calibrated_mean: float
    calibrated_std: float
    created_at: str

    @property
    def metric_label(self) -> str:
        return "lowest" if self.metric == "min" else "highest"

    @property
    def range_low(self) -> float:
        return self.calibrated_mean - self.calibrated_std

    @property
    def range_high(self) -> float:
        return self.calibrated_mean + self.calibrated_std
