from __future__ import annotations

import os
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class ReadinessConfig:
    """One source for provisional forward-confirmation evidence gates."""

    minimum_calendar_days: int = 60
    minimum_independent_events: int = 100
    minimum_settled_eligible_paper_trades: int = 100
    minimum_cities: int = 5
    minimum_forecast_horizons: int = 2
    maximum_calibration_gap: float = 0.05
    maximum_orderbook_age_seconds: int = 30
    maximum_source_receipt_delay_seconds: int = 10
    configured_depth_levels: int = 100
    intended_quantity: float = 1.0
    maximum_confirmatory_drawdown_dollars: float = 3.0
    bootstrap_samples: int = 20_000
    bootstrap_seed: int = 20260724

    def __post_init__(self) -> None:
        if not 0 <= self.configured_depth_levels <= 100:
            raise ValueError("configured_depth_levels must be between 0 and 100")
        for name in (
            "minimum_calendar_days",
            "minimum_independent_events",
            "minimum_settled_eligible_paper_trades",
            "minimum_cities",
            "minimum_forecast_horizons",
            "maximum_orderbook_age_seconds",
            "maximum_source_receipt_delay_seconds",
            "bootstrap_samples",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        if self.intended_quantity <= 0:
            raise ValueError("intended_quantity must be positive")

    @classmethod
    def from_env(cls) -> "ReadinessConfig":
        defaults = cls()
        return cls(
            minimum_calendar_days=int(
                os.environ.get(
                    "READINESS_MIN_CALENDAR_DAYS",
                    defaults.minimum_calendar_days,
                )
            ),
            minimum_independent_events=int(
                os.environ.get(
                    "READINESS_MIN_INDEPENDENT_EVENTS",
                    defaults.minimum_independent_events,
                )
            ),
            minimum_settled_eligible_paper_trades=int(
                os.environ.get(
                    "READINESS_MIN_SETTLED_TRADES",
                    defaults.minimum_settled_eligible_paper_trades,
                )
            ),
            minimum_cities=int(
                os.environ.get("READINESS_MIN_CITIES", defaults.minimum_cities)
            ),
            minimum_forecast_horizons=int(
                os.environ.get(
                    "READINESS_MIN_FORECAST_HORIZONS",
                    defaults.minimum_forecast_horizons,
                )
            ),
            maximum_calibration_gap=float(
                os.environ.get(
                    "READINESS_MAX_CALIBRATION_GAP",
                    defaults.maximum_calibration_gap,
                )
            ),
            maximum_orderbook_age_seconds=int(
                os.environ.get(
                    "READINESS_MAX_ORDERBOOK_AGE_SECONDS",
                    defaults.maximum_orderbook_age_seconds,
                )
            ),
            maximum_source_receipt_delay_seconds=int(
                os.environ.get(
                    "READINESS_MAX_SOURCE_RECEIPT_DELAY_SECONDS",
                    defaults.maximum_source_receipt_delay_seconds,
                )
            ),
            configured_depth_levels=int(
                os.environ.get(
                    "READINESS_ORDERBOOK_DEPTH_LEVELS",
                    defaults.configured_depth_levels,
                )
            ),
            intended_quantity=float(
                os.environ.get(
                    "READINESS_INTENDED_QUANTITY",
                    defaults.intended_quantity,
                )
            ),
            maximum_confirmatory_drawdown_dollars=float(
                os.environ.get(
                    "READINESS_MAX_DRAWDOWN_DOLLARS",
                    defaults.maximum_confirmatory_drawdown_dollars,
                )
            ),
            bootstrap_samples=int(
                os.environ.get(
                    "READINESS_BOOTSTRAP_SAMPLES",
                    defaults.bootstrap_samples,
                )
            ),
            bootstrap_seed=int(
                os.environ.get(
                    "READINESS_BOOTSTRAP_SEED",
                    defaults.bootstrap_seed,
                )
            ),
        )

    def to_dict(self) -> dict:
        return asdict(self)
