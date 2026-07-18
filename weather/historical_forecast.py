"""What a forecast actually said in the past, at a fixed lead time — for
backtesting, via Open-Meteo's Previous Runs API. Distinct from open_meteo.py,
which pulls the CURRENT live ensemble: this pulls historical DETERMINISTIC model
runs at a specific lead time (no ensemble members are archived historically, only
each model's single deterministic run), which is what avoids lookahead bias — the
value returned for a past date is what the model actually predicted that many
days ahead of time, not a hindsight/reanalysis value.

Verified live 2026-07-17: a single request can span a wide date range (tested
~14.5 months, ~10.6k hourly rows) and returns per-model fields directly, rather
than needing one request per date.
"""

from __future__ import annotations

import httpx

PREVIOUS_RUNS_URL = "https://previous-runs-api.open-meteo.com/v1/forecast"
DEFAULT_MODELS = ("gfs_seamless", "ecmwf_ifs025", "icon_seamless")


def fetch_historical_daily_max(
    latitude: float,
    longitude: float,
    timezone: str,
    start_date: str,
    end_date: str,
    lead_days: int = 1,
    models: tuple[str, ...] = DEFAULT_MODELS,
) -> dict[str, dict[str, float]]:
    """Per-model daily-max point forecasts across a date range, at a fixed lead
    time. Returns {date_iso: {model_name: forecast_value}} — a date is present
    only for models that had coverage for it (older dates may be GFS-only, since
    ECMWF/ICON archives start later).
    """
    field = f"temperature_2m_previous_day{lead_days}"
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "hourly": field,
        "models": ",".join(models),
        "timezone": timezone,
        "start_date": start_date,
        "end_date": end_date,
        "temperature_unit": "fahrenheit",
    }
    response = httpx.get(PREVIOUS_RUNS_URL, params=params, timeout=60.0)
    response.raise_for_status()
    data = response.json()["hourly"]
    times: list[str] = data["time"]

    result: dict[str, dict[str, float]] = {}
    for model in models:
        values = data.get(f"{field}_{model}")
        if not values:
            continue
        for timestamp, value in zip(times, values, strict=True):
            if value is None:
                continue
            date = timestamp.split("T")[0]
            by_model = result.setdefault(date, {})
            by_model[model] = max(value, by_model.get(model, float("-inf")))
    return result
