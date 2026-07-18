"""Open-Meteo ensemble client: per-member daily high temperature forecasts.

Verified live 2026-07-17: requesting multiple `models` pools GFS (as
`ncep_gefs_seamless`), ECMWF (`ecmwf_ifs025_ensemble`), and ICON
(`icon_seamless_eps`) member fields into one response, each named
`temperature_2m_max_memberNN[_modelname]`. Field naming is Open-Meteo's, not
ours — the regex below matches on the `_memberNN` segment rather than hardcoding
model-family suffixes, so it keeps working if Open-Meteo adds or renames a family.
"""

from __future__ import annotations

import re

import httpx

ENSEMBLE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
DEFAULT_MODELS = ("gfs_seamless", "ecmwf_ifs025", "icon_seamless")

_MEMBER_FIELD_RE = re.compile(r"^temperature_2m_max_member\d+")


def fetch_current_temperature(latitude: float, longitude: float, timezone: str) -> tuple[float, str]:
    """Right-now observed temperature (°F) and its observation timestamp (local
    ISO8601, per `timezone`) — a sanity-check display, not a model input.
    Verified live 2026-07-18 against Open-Meteo's standard Forecast API
    (distinct from the ensemble/previous-runs APIs used elsewhere in this
    package): `current=temperature_2m` returns near-real-time data on a
    ~15-minute update interval, not a forecast.
    """
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "current": "temperature_2m",
        "temperature_unit": "fahrenheit",
        "timezone": timezone,
    }
    response = httpx.get(FORECAST_URL, params=params, timeout=15.0)
    response.raise_for_status()
    current = response.json()["current"]
    return current["temperature_2m"], current["time"]


def fetch_daily_max_ensemble(
    latitude: float,
    longitude: float,
    timezone: str,
    forecast_days: int = 3,
    models: tuple[str, ...] = DEFAULT_MODELS,
) -> dict[str, list[float]]:
    """Daily high-temperature ensemble members (°F), pooled across model families.

    Returns {date_iso: [member_value, ...]}. Only individual members are included —
    each model's deterministic control/mean run (the plain `temperature_2m_max`
    field, no member number) is excluded so it doesn't double-weight the central
    scenario on top of members that already span the spread around it.
    """
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "daily": "temperature_2m_max",
        "models": ",".join(models),
        "timezone": timezone,
        "forecast_days": forecast_days,
        "temperature_unit": "fahrenheit",
    }
    response = httpx.get(ENSEMBLE_URL, params=params, timeout=15.0)
    response.raise_for_status()
    data = response.json()["daily"]

    dates: list[str] = data["time"]
    member_fields = [key for key in data if _MEMBER_FIELD_RE.match(key)]
    if not member_fields:
        raise ValueError(
            f"No ensemble member fields in Open-Meteo's response for models={models!r} "
            "— check the models parameter against Open-Meteo's current ensemble API."
        )

    result: dict[str, list[float]] = {date: [] for date in dates}
    for field in member_fields:
        for date, value in zip(dates, data[field], strict=True):
            if value is not None:
                result[date].append(value)
    return result
