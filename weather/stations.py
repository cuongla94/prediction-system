"""Per-city settlement station config for the six in-scope daily-high-temp series.

nws_station_id / nws_office come straight from each series' `settlement_sources`
field (live-checked 2026-07-17 via `get_series` — see kalshi-api-gotchas memory),
not guessed from "which airport serves this city": Chicago settles off Midway
(MDW), not O'Hare, which wouldn't have been obvious by assumption. Coordinates
are the station's own location, not a city-center geocode, so Open-Meteo pulls
the forecast for the actual point NWS measures rather than a nearby proxy.

standard_time_timezone is a fixed UTC offset ("Etc/GMT+N"), not the city's IANA
zone. NWS reports the climatological day in local *standard* time year-round, even
during DST — passing Open-Meteo the DST-aware zone (e.g. "America/New_York") would
bucket "daily max" using clock-time midnight instead, a boundary shifted an hour
from what NWS actually uses. Confirmed live 2026-07-17 that Open-Meteo honors a
fixed "Etc/GMT+N" offset distinctly from the DST-aware zone (different
utc_offset_seconds in the response) — the two happened to agree for that day's
max because it landed at 7pm, far from either midnight, but an overnight spike near
the boundary could resolve to the wrong calendar day if the DST-aware zone were used.
Note Etc/GMT signs are inverted from normal convention: Etc/GMT+5 means UTC-5 (EST).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Station:
    city: str
    series_ticker: str
    nws_station_id: str
    nws_office: str
    latitude: float
    longitude: float
    standard_time_timezone: str


STATIONS: dict[str, Station] = {
    "KXHIGHNY": Station("NYC", "KXHIGHNY", "NYC", "OKX", 40.7794, -73.9692, "Etc/GMT+5"),
    "KXHIGHCHI": Station("Chicago", "KXHIGHCHI", "MDW", "LOT", 41.7868, -87.7522, "Etc/GMT+6"),
    "KXHIGHPHIL": Station(
        "Philadelphia", "KXHIGHPHIL", "PHL", "PHI", 39.8721, -75.2411, "Etc/GMT+5"
    ),
    "KXHIGHAUS": Station("Austin", "KXHIGHAUS", "AUS", "EWX", 30.1975, -97.6664, "Etc/GMT+6"),
    "KXHIGHDEN": Station("Denver", "KXHIGHDEN", "DEN", "BOU", 39.8561, -104.6737, "Etc/GMT+7"),
    "KXHIGHMIA": Station("Miami", "KXHIGHMIA", "MIA", "MFL", 25.7959, -80.2870, "Etc/GMT+5"),
}


def get_station(series_ticker: str) -> Station:
    try:
        return STATIONS[series_ticker]
    except KeyError:
        raise KeyError(
            f"No station config for {series_ticker!r}. Confirm the series' "
            "settlement_sources via KalshiClient.get_series() before adding one — "
            "don't guess the airport/station from the city name alone."
        ) from None
