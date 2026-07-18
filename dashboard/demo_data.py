"""Sample alerts shown when DATABASE_URL isn't configured.

Every input here — bracket strikes, prices, rules text, and the ensemble
mean/std — is a real frozen snapshot pulled live from Kalshi and Open-Meteo for
KXHIGHNY-26JUL18 on 2026-07-17. model_probability and edge are computed by the
real engine (weather.probability's calibrated_bracket_probability, using the
bias/std correction fit in the 2026-07-17 backtest — see kalshi-backtest-findings
memory), not hand-picked. It is frozen (doesn't refetch), and it is NOT
calibration-validated for live trading — see the dashboard's warning banner.
"""

from __future__ import annotations

from edge.calculator import compute_edge
from kalshi_client import market_url
from weather.probability import calibrated_bracket_probability

from .alert import Alert

_SERIES_TICKER = "KXHIGHNY"
_SERIES_TITLE = "Highest temperature in NYC"
_EVENT_TICKER = "KXHIGHNY-26JUL18"
_CITY = "NYC"
_KALSHI_URL = market_url(_SERIES_TICKER, _SERIES_TITLE, _EVENT_TICKER)
_SNAPSHOT_TIME = "2026-07-17T23:00:00Z"

# Pooled GFS+ECMWF+ICON ensemble for 2026-07-18, n=119 members.
_ENSEMBLE_MEAN = 81.362185
_ENSEMBLE_STD = 2.864452

_RULES_SECONDARY = (
    "Not all weather data is the same. While checking a source like AccuWeather or "
    "Google Weather may help guide your decision, the official and final value used "
    "to determine this market is the highest temperature as reported by the "
    "corresponding NWS Climatological Report (Daily) linked in the rules above. "
    "Preliminary NWS reporting and measurement methods may be subject to underlying "
    "rounding and conversion nuances. Traders should exercise caution when "
    "interpreting preliminary NWS data."
)

_BRACKETS = [
    dict(
        market_ticker="KXHIGHNY-26JUL18-T79",
        bracket_label="< 79°",
        floor_strike=None,
        cap_strike=79.0,
        yes_bid=0.34,
        yes_ask=0.35,
        rules_primary=(
            "If the highest temperature recorded in Central Park, New York for "
            "July 18, 2026 as reported by the National Weather Service's "
            "Climatological Report (Daily), is less than 79°, then the market "
            "resolves to Yes."
        ),
    ),
    dict(
        market_ticker="KXHIGHNY-26JUL18-B79.5",
        bracket_label="79–80°",
        floor_strike=79.0,
        cap_strike=80.0,
        yes_bid=0.41,
        yes_ask=0.42,
        rules_primary=(
            "If the highest temperature recorded in Central Park, New York for "
            "July 18, 2026 as reported by the National Weather Service's "
            "Climatological Report (Daily), is between 79-80°, then the "
            "market resolves to Yes."
        ),
    ),
    dict(
        market_ticker="KXHIGHNY-26JUL18-B81.5",
        bracket_label="81–82°",
        floor_strike=81.0,
        cap_strike=82.0,
        yes_bid=0.20,
        yes_ask=0.21,
        rules_primary=(
            "If the highest temperature recorded in Central Park, New York for "
            "July 18, 2026 as reported by the National Weather Service's "
            "Climatological Report (Daily), is between 81-82°, then the "
            "market resolves to Yes."
        ),
    ),
    dict(
        market_ticker="KXHIGHNY-26JUL18-B83.5",
        bracket_label="83–84°",
        floor_strike=83.0,
        cap_strike=84.0,
        yes_bid=0.02,
        yes_ask=0.03,
        rules_primary=(
            "If the highest temperature recorded in Central Park, New York for "
            "July 18, 2026 as reported by the National Weather Service's "
            "Climatological Report (Daily), is between 83-84°, then the "
            "market resolves to Yes."
        ),
    ),
    dict(
        market_ticker="KXHIGHNY-26JUL18-B85.5",
        bracket_label="85–86°",
        floor_strike=85.0,
        cap_strike=86.0,
        yes_bid=0.05,
        yes_ask=0.08,
        rules_primary=(
            "If the highest temperature recorded in Central Park, New York for "
            "July 18, 2026 as reported by the National Weather Service's "
            "Climatological Report (Daily), is between 85-86°, then the "
            "market resolves to Yes."
        ),
    ),
    dict(
        market_ticker="KXHIGHNY-26JUL18-T86",
        bracket_label="> 86°",
        floor_strike=86.0,
        cap_strike=None,
        yes_bid=0.01,
        yes_ask=0.02,
        rules_primary=(
            "If the highest temperature recorded in Central Park, New York for "
            "July 18, 2026 as reported by the National Weather Service's "
            "Climatological Report (Daily), is greater than 86°, then the "
            "market resolves to Yes."
        ),
    ),
]


def demo_alerts() -> list[Alert]:
    alerts = []
    for i, bracket in enumerate(_BRACKETS, start=1):
        market_price = round((bracket["yes_bid"] + bracket["yes_ask"]) / 2, 4)
        model_probability = round(
            calibrated_bracket_probability(
                _SERIES_TICKER, _ENSEMBLE_MEAN, bracket["floor_strike"], bracket["cap_strike"]
            ),
            4,
        )
        result = compute_edge(model_probability, market_price)
        alerts.append(
            Alert(
                id=i,
                created_at=_SNAPSHOT_TIME,
                series_ticker=_SERIES_TICKER,
                event_ticker=_EVENT_TICKER,
                market_ticker=bracket["market_ticker"],
                city=_CITY,
                bracket_label=bracket["bracket_label"],
                floor_strike=bracket["floor_strike"],
                cap_strike=bracket["cap_strike"],
                model_probability=model_probability,
                ensemble_mean=_ENSEMBLE_MEAN,
                ensemble_std=_ENSEMBLE_STD,
                model_version="normal-v2-bias-corrected",
                calibration_validated=False,
                market_yes_price=market_price,
                edge=result.edge,
                fee_adjusted_threshold=result.threshold,
                rules_primary=bracket["rules_primary"],
                rules_secondary=_RULES_SECONDARY,
                kalshi_url=_KALSHI_URL,
                is_actionable=result.is_actionable,
                status="open",
                settled_at=None,
                actual_high_temp=None,
                actual_outcome=None,
            )
        )
    alerts.sort(key=lambda a: (not a.is_actionable, -abs(a.edge)))
    return alerts
