"""Backtest harness: replays the probability engine against historical settled
Kalshi markets, using Open-Meteo's Previous Runs API for what would have been
forecast at the time — not hindsight/reanalysis data, which would leak the
answer into the test and make the result meaningless.

Ground truth is Kalshi's own settlement `result` field, itself sourced from the
NWS Climatological Report per each market's rules_primary (see
kalshi-api-gotchas memory). An independent numeric cross-check via NOAA CDO
directly would strengthen this further — GHCND station IDs for all 6 cities are
already resolved (kalshi-implementation-progress memory) — but that needs an API
token that hasn't been obtained yet, so it's not wired up here.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from scipy import stats

from kalshi_client import KalshiClient, parse_event_date
from kalshi_client.models import Market
from weather.historical_forecast import fetch_historical_daily
from weather.stations import STATIONS


def _repair_missing_strikes(markets: list[Market]) -> list[Market]:
    """Recovers floor_strike/cap_strike for markets where Kalshi's historical
    API returns both as null, using the market's own ticker suffix plus its
    settled siblings in the same event.

    Confirmed live 2026-07-18 via scripts/validate_against_noaa.py: exactly
    25 events per city, identical dates (2025-01-16 through 2025-02-09) in
    all 6 cities — a contained, Kalshi-side historical-data gap, not random
    noise — and in every observed case it's specifically the *winning*
    market in the ladder that comes back with floor_strike=cap_strike=None,
    while every losing sibling in the same event has valid values. Since
    every bracket ticker in this codebase already encodes its own strike
    (e.g. "...-B35.5" -> floor=35, cap=36, the same convention
    Market.bracket_label relies on) and Kalshi's own historical archive kept
    that ticker intact even where it dropped the structured fields, the
    value doesn't have to be discarded — it can be read back out of the
    ticker itself. "B" (between) tickers are self-contained: the number is
    always the ladder-relative midpoint, floor = value-0.5, cap = value+0.5,
    with no ambiguity. "T" (tail) tickers need sibling context: a ticker
    like "T44" could mean floor=44 (a high tail, "greater than 44") or
    cap=44 (a low tail, "less than 44") depending on which end of that
    specific ladder it's on — resolved by comparing against the min/max of
    whatever floor/cap values its siblings in the same event actually have.
    Markets are frozen, so repaired ones are new instances via
    dataclasses.replace(), not mutated in place.
    """
    known_strikes = [
        v for m in markets for v in (m.floor_strike, m.cap_strike) if v is not None
    ]
    if not known_strikes:
        return markets  # nothing to cross-reference a tail ticker against

    low_bound = min(known_strikes)
    high_bound = max(known_strikes)

    repaired = []
    for market in markets:
        if market.floor_strike is not None or market.cap_strike is not None:
            repaired.append(market)
            continue
        suffix = market.ticker.rsplit("-", 1)[-1]
        try:
            if suffix.startswith("B"):
                midpoint = float(suffix[1:])
                market = replace(market, floor_strike=midpoint - 0.5, cap_strike=midpoint + 0.5)
            elif suffix.startswith("T"):
                value = float(suffix[1:])
                if value >= high_bound:
                    market = replace(market, floor_strike=value, cap_strike=None)
                elif value <= low_bound:
                    market = replace(market, floor_strike=None, cap_strike=value)
                # else: value falls inside the known range instead of at
                # either end — doesn't match how a tail bracket should
                # relate to the rest of its ladder, so don't guess.
        except ValueError:
            pass  # unparseable suffix — leave the market as-is
        repaired.append(market)
    return repaired


def _safe_event_date(event_ticker: str) -> str | None:
    """parse_event_date, but returns None instead of raising — used to sort a
    page of historical results by date when deciding whether to keep paging."""
    try:
        return parse_event_date(event_ticker).isoformat()
    except ValueError:
        return None


@dataclass(frozen=True)
class BacktestRow:
    city: str
    series_ticker: str
    event_ticker: str
    market_ticker: str
    target_date: str
    forecast_mean: float
    forecast_spread: float
    n_models: int
    actual_outcome: bool
    last_price: float | None
    floor_strike: float | None
    cap_strike: float | None
    approx_actual_temp: float | None


def collect_rows(
    client: KalshiClient,
    series_ticker: str,
    start_date: str,
    end_date: str,
    lead_days: int = 1,
) -> list[BacktestRow]:
    station = STATIONS[series_ticker]
    forecasts = fetch_historical_daily(
        station.latitude,
        station.longitude,
        station.standard_time_timezone,
        start_date,
        end_date,
        metric=station.metric,
        lead_days=lead_days,
    )

    settled_markets = []

    # Live tier: recent settled markets (roughly the last ~2 months — see
    # GET /historical/cutoff). Older markets stop appearing here entirely.
    cursor = None
    while True:
        markets, cursor = client.get_markets(
            series_ticker=series_ticker, status="settled", limit=200, cursor=cursor
        )
        settled_markets.extend(markets)
        if not cursor:
            break

    # Historical tier: everything past that cutoff. Neither endpoint supports a
    # date-range filter, so this pages until the requested start_date is covered
    # or a generous page bound is hit, rather than fetching the entire multi-year
    # archive unconditionally.
    cursor = None
    for _ in range(20):
        markets, cursor = client.get_historical_markets(
            series_ticker=series_ticker, limit=1000, cursor=cursor
        )
        if not markets:
            break
        settled_markets.extend(markets)
        oldest_seen = min(
            (m.event_ticker for m in markets if _safe_event_date(m.event_ticker)),
            key=lambda t: _safe_event_date(t),  # type: ignore[arg-type]
            default=None,
        )
        if not cursor or (oldest_seen and _safe_event_date(oldest_seen) <= start_date):
            break

    by_event: dict[str, list] = {}
    for market in settled_markets:
        by_event.setdefault(market.event_ticker, []).append(market)

    rows: list[BacktestRow] = []
    for event_ticker, markets in by_event.items():
        markets = _repair_missing_strikes(markets)
        try:
            event_date = parse_event_date(event_ticker)
        except ValueError:
            continue
        date_str = event_date.isoformat()
        if not (start_date <= date_str <= end_date):
            continue

        model_forecasts = forecasts.get(date_str)
        if not model_forecasts or len(model_forecasts) < 2:
            continue
        values = list(model_forecasts.values())
        forecast_mean = sum(values) / len(values)
        # Cross-model disagreement for this specific day — a candidate signal
        # for day-to-day (heteroscedastic) uncertainty, since a single fixed
        # scale can't tell a day the models agree on from one they don't.
        forecast_spread = (sum((v - forecast_mean) ** 2 for v in values) / len(values)) ** 0.5

        winner = next((m for m in markets if m.raw.get("result") == "yes"), None)
        approx_actual_temp = None
        if winner is not None and winner.floor_strike is not None and winner.cap_strike is not None:
            # A "between" bracket win pins the actual value to within half a
            # degree; tail-bracket wins (T-something) only give an inequality,
            # so approx_actual_temp stays None for those and they're excluded
            # from std-fitting later, though still usable for calibration
            # (which only needs win/lose, not the numeric value).
            approx_actual_temp = (winner.floor_strike + winner.cap_strike) / 2

        for market in markets:
            result = market.raw.get("result")
            if result not in ("yes", "no"):
                continue
            rows.append(
                BacktestRow(
                    city=station.city,
                    series_ticker=series_ticker,
                    event_ticker=event_ticker,
                    market_ticker=market.ticker,
                    target_date=date_str,
                    forecast_mean=forecast_mean,
                    forecast_spread=forecast_spread,
                    n_models=len(model_forecasts),
                    actual_outcome=(result == "yes"),
                    last_price=market.last_price_dollars,
                    floor_strike=market.floor_strike,
                    cap_strike=market.cap_strike,
                    approx_actual_temp=approx_actual_temp,
                )
            )
    return rows


def split_by_date(
    rows: list[BacktestRow], fit_fraction: float = 0.7
) -> tuple[list[BacktestRow], list[BacktestRow]]:
    """Splits chronologically by unique date, not row count or random shuffle —
    brackets from the same day must never end up split across fit and eval (that
    would leak information), and a chronological split mimics how the system
    would actually be used: fit on the past, evaluate on more recent unseen days.
    """
    dates = sorted({row.target_date for row in rows})
    split_index = round(len(dates) * fit_fraction)
    fit_dates = set(dates[:split_index])
    fit_rows = [row for row in rows if row.target_date in fit_dates]
    eval_rows = [row for row in rows if row.target_date not in fit_dates]
    return fit_rows, eval_rows


def collect_dated_residuals(rows: list[BacktestRow]) -> list[tuple[str, float]]:
    """(date, actual - forecast_mean) pairs, one per day, not per bracket — a
    6-bracket event must not count 6x. Pooled across whichever days have a
    usable point-estimate of the actual temperature: tail-bracket wins only
    give an inequality, not a value, so those days are excluded (see
    BacktestRow.approx_actual_temp).
    """
    seen_dates: set[str] = set()
    result: list[tuple[str, float]] = []
    for row in rows:
        if row.approx_actual_temp is None or row.target_date in seen_dates:
            continue
        seen_dates.add(row.target_date)
        result.append((row.target_date, row.approx_actual_temp - row.forecast_mean))
    return result


def collect_residuals(rows: list[BacktestRow]) -> list[float]:
    """collect_dated_residuals(), without the dates — see that function for
    what's excluded and why."""
    return [residual for _, residual in collect_dated_residuals(rows)]


def fit_monthly_bias(rows: list[BacktestRow], min_samples: int = 10) -> dict[int, float]:
    """Mean residual bias per calendar month (1-12), not one flat number.

    A single global bias can hide real seasonal variation — confirmed
    2026-07-18: NYC's day-1-ahead forecast runs +2.7F cold in January but
    -1.6F warm in July, a full sign flip a flat correction can't represent
    (see kalshi-backtest-findings memory for the full per-city breakdown).
    Months with fewer than `min_samples` residuals are omitted entirely
    rather than returning a noisy estimate — callers should fall back to the
    overall (non-seasonal) bias from fit_empirical_normal for those.
    """
    by_month: dict[int, list[float]] = {}
    for date, residual in collect_dated_residuals(rows):
        month = int(date.split("-")[1])
        by_month.setdefault(month, []).append(residual)
    return {
        month: sum(residuals) / len(residuals)
        for month, residuals in by_month.items()
        if len(residuals) >= min_samples
    }


def fit_empirical_normal(rows: list[BacktestRow]) -> tuple[float, float]:
    """Mean bias and std of the forecast-mean residuals — checks whether
    forecast_mean runs systematically hot/cold rather than assuming it's
    unbiased, in addition to fitting the spread. A candidate replacement for the
    live pipeline's pooled-live-ensemble std, which is suspected of running too
    wide — see the 2026-07-17 finding in kalshi-implementation-progress memory.
    """
    residuals = collect_residuals(rows)
    if len(residuals) < 2:
        raise ValueError(f"Need at least 2 usable days to fit a distribution, got {len(residuals)}.")
    mean_bias = sum(residuals) / len(residuals)
    variance = sum((r - mean_bias) ** 2 for r in residuals) / (len(residuals) - 1)
    return mean_bias, variance**0.5


def fit_student_t(rows: list[BacktestRow]) -> tuple[float, float, float]:
    """(df, loc, scale) via MLE on the same residuals fit_empirical_normal uses.

    Not a reuse of the normal fit's std — a Student's t `scale` isn't its
    standard deviation except as df -> infinity, so it has to be fit directly
    against the residuals rather than derived from the normal std.
    """
    residuals = collect_residuals(rows)
    if len(residuals) < 3:
        raise ValueError(f"Need at least 3 usable days to fit a Student's t, got {len(residuals)}.")
    df, loc, scale = stats.t.fit(residuals)
    return df, loc, scale
