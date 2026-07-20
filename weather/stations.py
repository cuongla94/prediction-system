"""Per-city settlement station config for the daily-high-temp and daily-low-temp
series this system tracks.

nws_station_id / nws_office come straight from each series' `settlement_sources`
field (live-checked 2026-07-17 for high-temp, 2026-07-19 for low-temp — see
kalshi-api-gotchas memory), not guessed from "which airport serves this city":
Chicago settles off Midway (MDW), not O'Hare, which wouldn't have been obvious
by assumption. Coordinates are the station's own location, not a city-center
geocode, so Open-Meteo pulls the forecast for the actual point NWS measures
rather than a nearby proxy.

`metric` is `"max"` or `"min"` — matches Open-Meteo's own `temperature_2m_max`/
`temperature_2m_min` daily-variable naming directly, so it can be interpolated
straight into API params (weather/open_meteo.py, weather/historical_forecast.py)
without a translation layer.

**High-temp and low-temp are NOT guaranteed to share a station for the same
city — verified live per-series, not assumed.** For the original 6 cities plus
the 14 added 2026-07-19, every city's high-temp and low-temp series turned out
to settle from the identical station (confirmed by comparing `settlement_sources`
site/issuedby for both) — but don't assume that holds for a *new* city added
later without checking; it's a real per-series fact, not a rule Kalshi
guarantees.

**A live "0 open events" check is not proof a series doesn't exist — corrected
2026-07-19, found the hard way.** `KXLOWTAUS` (Austin's own low-temperature
series) was initially checked, showed zero open events at that moment, and got
misread as "doesn't exist" — Austin's `city="San Antonio"` (`KXLOWTSATX`) was
substituted in as a workaround. That was wrong: `KXLOWTAUS` was just between
daily events at the exact moment of that check, not dead — Kalshi confirmed it
open again minutes later, settling from Austin's own station (`issuedby=AUS`),
identical to `KXHIGHAUS`. Fixed: Austin now correctly uses `KXLOWTAUS`, and San
Antonio is tracked as its own real city (it has genuine high+low markets of its
own, `KXHIGHTSATX`/`KXLOWTSATX`) rather than as an Austin stand-in. **Lesson:
a dormant-looking series needs a second check (or a settlement_sources lookup,
which works regardless of event state) before concluding it's abandoned** —
only trust "dead" for a ticker that's a clear title-duplicate of a known-live
one (see the original 6 cities' dead legacy tickers below), not for a ticker
that's the *only* candidate for a city and simply has no event open right now.

**Expanded 2026-07-19 from 6 cities to 20** — Kalshi's real category tag is a
single unified "Daily temperature" (53 series), not the separate "High
Temperature"/"Low Temperature"/"Daily Temperature" labels a market card
displays (those are a per-card display artifact tied to old-vs-new naming
conventions, not Kalshi's actual category boundary). The 14 newer cities use a
different, symmetric title convention ("{city} High Temperature Daily" /
"{city} Daily Maximum Temperature") than the original 6's "Highest temperature
in {city}" — cosmetic, `rules_primary` phrasing and bracket-ticker format
(`B{floor}.5` / `T{value}`) are identical, so no parsing changes were needed.

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

ghcnd_id is a *different* station identifier from nws_station_id — NOAA's GHCND
(Global Historical Climatology Network Daily) archive, used by the CDO API for
independent settlement validation (weather/noaa_cdo.py), doesn't use NWS's
3-4 letter station codes. Resolved 2026-07-18 from NOAA's public bulk file
ncei.noaa.gov/pub/data/ghcn/daily/ghcnd-stations.txt (no token needed), matched
by lat/lon proximity + name — see kalshi-backtest-findings memory for the full
matching process, including one near-miss: Philadelphia's nearest-by-distance
station (USC00366880, "PHILA AP SNOW") is a real substation but snow-only, not
the one that actually has TMAX data.
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
    ghcnd_id: str
    metric: str  # "max" | "min" — which daily extreme this series settles on

    @property
    def settlement_source_url(self) -> str:
        """The exact NWS Climatological Report (Daily) bulletin Kalshi itself cites as
        the settlement source — confirmed live 2026-07-18 via `get_series().settlement_sources`,
        not guessed: it's `nws_office`/`nws_station_id` (already resolved above) slotted into
        this fixed product.php pattern, same as every other city on Kalshi uses."""
        return (
            f"https://forecast.weather.gov/product.php"
            f"?site={self.nws_office}&product=CLI&issuedby={self.nws_station_id}"
        )


STATIONS: dict[str, Station] = {
    # --- Original 6 cities, high temperature (resolved 2026-07-17) ---
    "KXHIGHNY": Station("NYC", "KXHIGHNY", "NYC", "OKX", 40.7794, -73.9692, "Etc/GMT+5", "USW00094728", "max"),
    "KXHIGHCHI": Station(
        "Chicago", "KXHIGHCHI", "MDW", "LOT", 41.7868, -87.7522, "Etc/GMT+6", "USW00014819", "max"
    ),
    "KXHIGHPHIL": Station(
        "Philadelphia", "KXHIGHPHIL", "PHL", "PHI", 39.8721, -75.2411, "Etc/GMT+5", "USW00013739", "max"
    ),
    "KXHIGHAUS": Station(
        "Austin", "KXHIGHAUS", "AUS", "EWX", 30.1975, -97.6664, "Etc/GMT+6", "USW00013904", "max"
    ),
    "KXHIGHDEN": Station(
        "Denver", "KXHIGHDEN", "DEN", "BOU", 39.8561, -104.6737, "Etc/GMT+7", "USW00003017", "max"
    ),
    "KXHIGHMIA": Station(
        "Miami", "KXHIGHMIA", "MIA", "MFL", 25.7959, -80.2870, "Etc/GMT+5", "USW00012839", "max"
    ),
    # --- Original 6 cities, low temperature (added 2026-07-19). KXLOWTAUS is
    # the real Austin low-temp series — see module docstring for the earlier
    # San Antonio mixup this corrects. ---
    "KXLOWTNYC": Station("NYC", "KXLOWTNYC", "NYC", "OKX", 40.7794, -73.9692, "Etc/GMT+5", "USW00094728", "min"),
    "KXLOWTCHI": Station(
        "Chicago", "KXLOWTCHI", "MDW", "LOT", 41.7868, -87.7522, "Etc/GMT+6", "USW00014819", "min"
    ),
    "KXLOWTPHIL": Station(
        "Philadelphia", "KXLOWTPHIL", "PHL", "PHI", 39.8721, -75.2411, "Etc/GMT+5", "USW00013739", "min"
    ),
    "KXLOWTAUS": Station(
        "Austin", "KXLOWTAUS", "AUS", "EWX", 30.1975, -97.6664, "Etc/GMT+6", "USW00013904", "min"
    ),
    "KXLOWTDEN": Station(
        "Denver", "KXLOWTDEN", "DEN", "BOU", 39.8561, -104.6737, "Etc/GMT+7", "USW00003017", "min"
    ),
    "KXLOWTMIA": Station(
        "Miami", "KXLOWTMIA", "MIA", "MFL", 25.7959, -80.2870, "Etc/GMT+5", "USW00012839", "min"
    ),
    # --- 14 additional cities (added 2026-07-19), high + low temperature.
    # Coordinates confirmed against NOAA's own GHCND station-detail pages
    # (not a third-party estimate) for every one of these. Phoenix has no
    # DST (Arizona doesn't observe it) — Etc/GMT+7 is simply its
    # always-current offset, not a standard-vs-DST distinction like the rest. ---
    "KXHIGHTPHX": Station(
        "Phoenix", "KXHIGHTPHX", "PHX", "PSR", 33.4278, -112.00365, "Etc/GMT+7", "USW00023183", "max"
    ),
    "KXLOWTPHX": Station(
        "Phoenix", "KXLOWTPHX", "PHX", "PSR", 33.4278, -112.00365, "Etc/GMT+7", "USW00023183", "min"
    ),
    "KXHIGHTSFO": Station(
        "San Francisco", "KXHIGHTSFO", "SFO", "MTR", 37.61962, -122.36562, "Etc/GMT+8", "USW00023234", "max"
    ),
    "KXLOWTSFO": Station(
        "San Francisco", "KXLOWTSFO", "SFO", "MTR", 37.61962, -122.36562, "Etc/GMT+8", "USW00023234", "min"
    ),
    "KXHIGHTBOS": Station(
        "Boston", "KXHIGHTBOS", "BOS", "BOX", 42.36057, -71.00975, "Etc/GMT+5", "USW00014739", "max"
    ),
    "KXLOWTBOS": Station(
        "Boston", "KXLOWTBOS", "BOS", "BOX", 42.36057, -71.00975, "Etc/GMT+5", "USW00014739", "min"
    ),
    "KXHIGHTATL": Station(
        "Atlanta", "KXHIGHTATL", "ATL", "FFC", 33.62972, -84.44224, "Etc/GMT+5", "USW00013874", "max"
    ),
    "KXLOWTATL": Station(
        "Atlanta", "KXLOWTATL", "ATL", "FFC", 33.62972, -84.44224, "Etc/GMT+5", "USW00013874", "min"
    ),
    "KXHIGHTLV": Station(
        "Las Vegas", "KXHIGHTLV", "LAS", "VEF", 36.0719, -115.16343, "Etc/GMT+8", "USW00023169", "max"
    ),
    "KXLOWTLV": Station(
        "Las Vegas", "KXLOWTLV", "LAS", "VEF", 36.0719, -115.16343, "Etc/GMT+8", "USW00023169", "min"
    ),
    "KXHIGHTDAL": Station(
        "Dallas", "KXHIGHTDAL", "DFW", "FWD", 32.83839, -96.83583, "Etc/GMT+6", "USW00013960", "max"
    ),
    "KXLOWTDAL": Station(
        "Dallas", "KXLOWTDAL", "DFW", "FWD", 32.83839, -96.83583, "Etc/GMT+6", "USW00013960", "min"
    ),
    "KXHIGHTSEA": Station(
        "Seattle", "KXHIGHTSEA", "SEA", "SEW", 47.44467, -122.31442, "Etc/GMT+8", "USW00024233", "max"
    ),
    "KXLOWTSEA": Station(
        "Seattle", "KXLOWTSEA", "SEA", "SEW", 47.44467, -122.31442, "Etc/GMT+8", "USW00024233", "min"
    ),
    "KXHIGHTDC": Station(
        "Washington DC", "KXHIGHTDC", "DCA", "LWX", 38.84721, -77.03454, "Etc/GMT+5", "USW00013743", "max"
    ),
    "KXLOWTDC": Station(
        "Washington DC", "KXLOWTDC", "DCA", "LWX", 38.84721, -77.03454, "Etc/GMT+5", "USW00013743", "min"
    ),
    "KXHIGHTHOU": Station(
        "Houston", "KXHIGHTHOU", "HOU", "HGX", 29.66, -95.29, "Etc/GMT+6", "USW00012918", "max"
    ),
    "KXLOWTHOU": Station(
        "Houston", "KXLOWTHOU", "HOU", "HGX", 29.66, -95.29, "Etc/GMT+6", "USW00012918", "min"
    ),
    "KXHIGHTOKC": Station(
        "Oklahoma City", "KXHIGHTOKC", "OKC", "OUN", 35.3931, -97.6007, "Etc/GMT+6", "USW00013967", "max"
    ),
    "KXLOWTOKC": Station(
        "Oklahoma City", "KXLOWTOKC", "OKC", "OUN", 35.3931, -97.6007, "Etc/GMT+6", "USW00013967", "min"
    ),
    "KXHIGHTNOLA": Station(
        "New Orleans", "KXHIGHTNOLA", "MSY", "LIX", 29.9893, -90.2548, "Etc/GMT+6", "USW00012916", "max"
    ),
    "KXLOWTNOLA": Station(
        "New Orleans", "KXLOWTNOLA", "MSY", "LIX", 29.9893, -90.2548, "Etc/GMT+6", "USW00012916", "min"
    ),
    "KXHIGHLAX": Station(
        "Los Angeles", "KXHIGHLAX", "LAX", "LOX", 33.9416, -118.4085, "Etc/GMT+8", "USW00023174", "max"
    ),
    "KXLOWTLAX": Station(
        "Los Angeles", "KXLOWTLAX", "LAX", "LOX", 33.9416, -118.4085, "Etc/GMT+8", "USW00023174", "min"
    ),
    "KXHIGHTSATX": Station(
        "San Antonio", "KXHIGHTSATX", "SAT", "EWX", 29.54429, -98.48395, "Etc/GMT+6", "USW00012921", "max"
    ),
    "KXLOWTSATX": Station(
        "San Antonio", "KXLOWTSATX", "SAT", "EWX", 29.54429, -98.48395, "Etc/GMT+6", "USW00012921", "min"
    ),
    "KXHIGHTMIN": Station(
        "Minneapolis", "KXHIGHTMIN", "MSP", "MPX", 44.89, -93.22, "Etc/GMT+6", "USW00014922", "max"
    ),
    "KXLOWTMIN": Station(
        "Minneapolis", "KXLOWTMIN", "MSP", "MPX", 44.89, -93.22, "Etc/GMT+6", "USW00014922", "min"
    ),
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
