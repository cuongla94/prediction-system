"""Independent cross-check of Kalshi's own settlement results against NOAA
CDO's GHCND daily max temperature — see weather/noaa_cdo.py's docstring for
why this exists and its current not-yet-live-tested status.

Reuses the exact same settled-market dataset the backtest harness fits and
evaluates against (same date range, same Redis-cached Kalshi fetch), rather
than waiting for new markets to settle in real time — the backtest's ~600
days/city of already-settled history is immediately usable to cross-check the
moment a token exists, instead of needing to accumulate fresh settlements.

For each settled day, finds Kalshi's winning bracket and checks whether NOAA's
independently-measured TMAX (rounded to the nearest whole degree, matching
NWS's own reporting convention) falls inside that same bracket. This is a
membership check, not an exact-value comparison — Kalshi's `result` field
only tells us which bracket won, not the precise temperature, so "NOAA agrees
with the bracket Kalshi says won" is the strongest claim the data supports.

Usage: uv run scripts/validate_against_noaa.py
Requires NOAA_CDO_TOKEN in the environment — get a free one at
https://www.ncdc.noaa.gov/cdo-web/token (just an email address, arrives
instantly).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, timedelta

from dotenv import load_dotenv

from backtest.cache import cached_collect_rows
from backtest.harness import BacktestRow
from kalshi_client import KalshiClient
from monitoring import track_run
from weather.noaa_cdo import NoaaCdoError, fetch_daily_tmax
from weather.probability import temperature_in_bracket
from weather.stations import STATIONS

START_DATE = "2024-10-01"
END_DATE = (date.today() - timedelta(days=1)).isoformat()


@dataclass
class CityResult:
    city: str
    matched: int = 0
    mismatched: int = 0
    no_noaa_data: int = 0
    mismatch_lines: list[str] = field(default_factory=list)
    error: str | None = None


def _find_winner(event_rows: list[BacktestRow]) -> BacktestRow | None:
    winners = [r for r in event_rows if r.actual_outcome]
    if len(winners) != 1:
        # 0: a data gap (void/ambiguous settlement). >1: shouldn't happen for a
        # well-formed ladder. Either way, not something to guess about — skip
        # this event rather than picking a winner arbitrarily.
        return None
    return winners[0]


def validate_city(client: KalshiClient, series_ticker: str, token: str) -> CityResult:
    station = STATIONS[series_ticker]
    result = CityResult(city=station.city)
    print(f"\n=== {station.city} ({series_ticker}, GHCND:{station.ghcnd_id}) ===")

    rows = cached_collect_rows(client, series_ticker, START_DATE, END_DATE)
    if not rows:
        print("  no settled rows, skipping.")
        return result

    by_event: dict[str, list[BacktestRow]] = {}
    for row in rows:
        by_event.setdefault(row.event_ticker, []).append(row)

    winners_by_date: dict[str, BacktestRow] = {}
    for event_rows in by_event.values():
        winner = _find_winner(event_rows)
        if winner is not None:
            winners_by_date[winner.target_date] = winner

    print(f"  {len(winners_by_date)} settled days with a clear winning bracket")
    if not winners_by_date:
        return result

    try:
        noaa_tmax = fetch_daily_tmax(station.ghcnd_id, START_DATE, END_DATE, token)
    except NoaaCdoError as exc:
        print(f"  NOAA CDO request failed: {exc}")
        result.error = str(exc)
        return result

    for target_date, winner in sorted(winners_by_date.items()):
        noaa_value = noaa_tmax.get(target_date)
        if noaa_value is None:
            result.no_noaa_data += 1
            continue
        # GHCND's TMAX is converted server-side from tenths-of-Celsius storage
        # (see weather/noaa_cdo.py) and can land off a whole degree by a
        # fraction purely from that conversion — round before comparing
        # against Kalshi's whole-degree bracket boundaries.
        rounded = round(noaa_value)
        if temperature_in_bracket(rounded, winner.floor_strike, winner.cap_strike):
            result.matched += 1
        else:
            result.mismatched += 1
            result.mismatch_lines.append(
                f"    {target_date}: Kalshi winner={winner.market_ticker} "
                f"(floor={winner.floor_strike}, cap={winner.cap_strike}) vs. "
                f"NOAA TMAX={noaa_value:.1f}°F (rounded {rounded})"
            )

    print(f"  matched={result.matched}  mismatched={result.mismatched}  no_noaa_data={result.no_noaa_data}")
    if result.mismatch_lines:
        print("  mismatches:")
        for line in result.mismatch_lines:
            print(line)

    return result


def main() -> None:
    load_dotenv()
    token = os.environ.get("NOAA_CDO_TOKEN")
    if not token:
        print(
            "NOAA_CDO_TOKEN not set. Get a free token at "
            "https://www.ncdc.noaa.gov/cdo-web/token (just an email address, "
            "arrives instantly) and add it to .env before running this."
        )
        return

    with track_run("validate_against_noaa") as run, KalshiClient() as client:
        results = [validate_city(client, series_ticker, token) for series_ticker in STATIONS]

        total_matched = sum(r.matched for r in results)
        total_mismatched = sum(r.mismatched for r in results)
        total_no_data = sum(r.no_noaa_data for r in results)
        errored_cities = [r.city for r in results if r.error]

        run.summary = (
            f"{total_matched} matched, {total_mismatched} mismatched, "
            f"{total_no_data} no-NOAA-data across {len(STATIONS)} cities"
        )
        if errored_cities:
            run.status = "partial" if (total_matched or total_mismatched) else "failed"
            run.detail = f"NOAA request failed for: {', '.join(errored_cities)}"
        elif total_mismatched:
            # Not a script failure — it did its job finding a real
            # discrepancy — but worth flagging on /status rather than
            # blending into a plain "success" that invites skimming past it.
            run.status = "partial"
            run.detail = f"{total_mismatched} day(s) where NOAA disagrees with Kalshi's settlement result"


if __name__ == "__main__":
    main()
