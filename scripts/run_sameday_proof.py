"""Thin same-day backtest PROOF (2026-07-20) — scores
observation_conditioned_bracket_probability against the market on real
intraday snapshots, which scripts/run_backtest.py's gate has never done (it
backtests at lead_days=1 with one end-of-window price per market, never an
intraday one; see the kalshi-no-edge-root-cause memory for why that gate is
structurally blind to this fix).

One city (KXHIGHNY — the series already confirmed live to support
candlesticks, and one of only two stations, with KDEN, already documented as
single-feed and therefore free of the METAR/5-minute-feed mixing risk
weather/nws_observations.py guards against, keeping the observation side of
this proof from carrying its own confound). Three fixed decision times,
station standard time, pre-registered before running this for the reasons
recorded in this session's chat transcript (not re-derived here to avoid
drifting from what was actually committed to beforehand):
  - 09:00 — before the diurnal ramp; a low-information control. Conditioning
    should do ~nothing here, which is the baseline the other two are read
    against.
  - 12:00 — after the morning ramp, afternoon peak still ahead. Moderate
    information.
  - 15:00 — NYC's climatological daily-high window is typically mid-
    afternoon, so by here the observed reading is usually close to (or
    already at) the eventual max. High information — the hypothesis under
    test is that the shortfall against the market is *largest* here, since
    observation_conditioned_bracket_probability truncates from the observed
    value but does not re-center or shrink `scale` for how much of the day's
    uncertainty has already resolved (remaining_scale_fraction stays at its
    unshrunk default of 1.0).

Deliberately isolates ONE variable: `loc`/`scale` are held at exactly the
same day-ahead-calibrated fit run_backtest.py's own `normal` variant already
uses (same collect_rows/split_by_date/fit_empirical_normal calls, same
70/30 chronological split) — only whether observation_conditioned_bracket_
probability truncates that same distribution varies. This measures the
marginal value of intraday conditioning specifically, not conflated with
reconstructing a live same-day ensemble forecast (a separate, larger build,
deferred unless this passes).

Not part of the live pipeline and does not write to the database or
pipeline_runs — a one-off diagnostic script, same role as
scripts/forecast_vs_market.py.

Usage: uv run scripts/run_sameday_proof.py
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from datetime import time as dtime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from backtest.calibration import MarketBenchmark, market_benchmark
from backtest.cache import cached_collect_rows
from backtest.harness import BacktestRow, fit_empirical_normal, split_by_date
from kalshi_client import Candlestick, KalshiClient
from weather.historical_observations import extreme_as_of, fetch_asos_temperatures
from weather.probability import bracket_probability, observation_conditioned_bracket_probability
from weather.stations import STATIONS, Station

SERIES_TICKER = "KXHIGHNY"
FIT_START_DATE = "2024-10-01"  # matches run_backtest.py's START_DATE
PROOF_WINDOW_DAYS = 18  # ~2.5 weeks, out of the tail of the eval split
DECISION_TIMES: tuple[dtime, ...] = (dtime(9, 0), dtime(12, 0), dtime(15, 0))


@dataclass(frozen=True)
class SameDayDataset:
    """Everything a same-day scoring pass needs, collected once. Split out of
    main() so a second analysis (scripts/fit_remaining_scale.py, built to
    reuse this proof's data rather than re-pulling it) can import
    collect_sameday_dataset() directly instead of duplicating ~80 lines of
    fetch/pagination logic that would then be free to drift out of sync with
    what this proof actually scored."""

    station: Station
    tz: ZoneInfo
    normal_bias: float
    normal_std: float
    proof_dates: set[str]
    proof_rows: list[BacktestRow]
    readings: list[tuple[datetime, float]]
    candles_by_market: dict[str, list[Candlestick]]


def decision_ts(target_date: date, decision_time: dtime, tz: ZoneInfo) -> int:
    return int(datetime.combine(target_date, decision_time, tzinfo=tz).timestamp())


def price_as_of(candles: list[Candlestick], ts: int) -> float | None:
    """Yes-price (bid/ask midpoint, matching generate_alerts.py's own market-
    price convention) as of the last candle that closed at or before `ts` —
    never a candle still in progress at that instant, so this can't leak a
    price a trader deciding right then couldn't have seen."""
    candidates = [c for c in candles if c.end_period_ts <= ts]
    if not candidates:
        return None
    candle = max(candidates, key=lambda c: c.end_period_ts)
    if candle.yes_bid_close_dollars is None or candle.yes_ask_close_dollars is None:
        return None
    return (candle.yes_bid_close_dollars + candle.yes_ask_close_dollars) / 2


def print_result(label: str, bench: MarketBenchmark | None) -> None:
    if bench is None:
        print(f"  {label:>26}: no matched snapshots to score")
        return
    verdict = "BEATS MARKET" if bench.beats_market else "no edge"
    print(
        f"  {label:>26}: Brier {bench.brier_model:.4f} vs market {bench.brier_market:.4f} "
        f"(skill {bench.skill_score:+.4f}, n={bench.n}) -> {verdict}"
    )


def collect_sameday_dataset(series_ticker: str = SERIES_TICKER) -> SameDayDataset:
    station = STATIONS[series_ticker]
    tz = ZoneInfo(station.standard_time_timezone)
    end_date = (date.today() - timedelta(days=1)).isoformat()

    with KalshiClient() as client:
        print(f"Collecting day-ahead rows for {station.city} ({series_ticker}), {FIT_START_DATE}..{end_date}...")
        rows = cached_collect_rows(client, series_ticker, FIT_START_DATE, end_date, lead_days=1)
        fit_rows, eval_rows = split_by_date(rows, fit_fraction=0.7)
        normal_bias, normal_std = fit_empirical_normal(fit_rows)
        print(
            f"  fit on {len({r.target_date for r in fit_rows})} days "
            f"(chronologically before every day in the proof window below): "
            f"bias={normal_bias:+.2f}F std={normal_std:.2f}F"
        )

        eval_dates = sorted({r.target_date for r in eval_rows})
        proof_dates = set(eval_dates[-PROOF_WINDOW_DAYS:])
        proof_rows: list[BacktestRow] = [r for r in eval_rows if r.target_date in proof_dates]
        print(
            f"  same-day proof window: {min(proof_dates)}..{max(proof_dates)} "
            f"({len(proof_dates)} days, {len(proof_rows)} bracket-rows), out of sample vs. the fit above."
        )

        print(f"\nFetching IEM ASOS observations for station {station.nws_station_id}...")
        readings = fetch_asos_temperatures(
            station.nws_station_id, station.standard_time_timezone, min(proof_dates), max(proof_dates)
        )
        print(f"  {len(readings)} hourly/special METAR readings.")

        print(f"\nFetching intraday candlesticks for {len({r.market_ticker for r in proof_rows})} markets...")
        candles_by_market: dict[str, list[Candlestick]] = {}
        for i, row in enumerate(proof_rows):
            if row.market_ticker in candles_by_market:
                continue
            target = date.fromisoformat(row.target_date)
            day_start = decision_ts(target, dtime.min, tz)
            day_end = decision_ts(target, dtime(23, 59), tz)
            try:
                candles_by_market[row.market_ticker] = client.get_candlesticks(
                    series_ticker, row.market_ticker, day_start, day_end, period_interval=1
                )
            except Exception as exc:
                print(f"  {row.market_ticker}: candlesticks failed ({exc.__class__.__name__}: {exc}), skipping.")
                candles_by_market[row.market_ticker] = []
            if (i + 1) % 20 == 0:
                print(f"  ...{i + 1}/{len(proof_rows)}")

    return SameDayDataset(
        station=station,
        tz=tz,
        normal_bias=normal_bias,
        normal_std=normal_std,
        proof_dates=proof_dates,
        proof_rows=proof_rows,
        readings=readings,
        candles_by_market=candles_by_market,
    )


def score_decision_time(
    dataset: SameDayDataset,
    decision_time: dtime,
    remaining_scale_fraction: float = 1.0,
    loc_offset: float = 0.0,
) -> dict[str, MarketBenchmark | None | int]:
    """Score both variants at one decision time. `remaining_scale_fraction`
    defaults to 1.0 (this proof's original, unshrunk behavior) — passing a
    fitted value here is scripts/fit_remaining_scale.py's whole point.
    `loc_offset` defaults to 0.0 (this proof's original center) — a nonzero
    value shifts every row's `loc` by the same flat amount, for testing a
    day-ahead bias correction (scripts/debias_and_refit_shrinkage.py) the
    same uniform way weather.calibration_params already applies a flat
    per-city bias, not a per-day one."""
    normal_preds: list[float] = []
    cond_preds: list[float] = []
    market_prices: list[float] = []
    outcomes: list[bool] = []
    skipped_no_price = 0

    for row in dataset.proof_rows:
        target = date.fromisoformat(row.target_date)
        ts = decision_ts(target, decision_time, dataset.tz)
        market_price = price_as_of(dataset.candles_by_market.get(row.market_ticker, []), ts)
        if market_price is None:
            skipped_no_price += 1
            continue

        observed = extreme_as_of(dataset.readings, target, decision_time, dataset.station.metric)
        loc = row.forecast_mean + dataset.normal_bias + loc_offset
        normal_pred = bracket_probability(loc, dataset.normal_std, row.floor_strike, row.cap_strike)
        cond_pred = (
            normal_pred
            if observed is None
            else observation_conditioned_bracket_probability(
                loc,
                dataset.normal_std,
                row.floor_strike,
                row.cap_strike,
                dataset.station.metric,
                observed,
                remaining_scale_fraction=remaining_scale_fraction,
            )
        )

        normal_preds.append(normal_pred)
        cond_preds.append(cond_pred)
        market_prices.append(market_price)
        outcomes.append(row.actual_outcome)

    return {
        "n_skipped_no_price": skipped_no_price,
        "normal": market_benchmark(normal_preds, market_prices, outcomes),
        "conditioned": market_benchmark(cond_preds, market_prices, outcomes),
    }


def main() -> None:
    load_dotenv()
    dataset = collect_sameday_dataset()

    results = {decision_time: score_decision_time(dataset, decision_time) for decision_time in DECISION_TIMES}

    print(
        f"\n=== SAME-DAY PROOF: {dataset.station.city}, {len(dataset.proof_dates)} days, "
        f"decision times {[t.strftime('%H:%M') for t in DECISION_TIMES]} local standard time ===\n"
    )
    for decision_time, result in results.items():
        print(f"--- {decision_time.strftime('%H:%M')} ({result['n_skipped_no_price']} rows skipped, no matched price) ---")
        print_result("normal (unconditional)", result["normal"])  # type: ignore[arg-type]
        print_result("observation-conditioned", result["conditioned"])  # type: ignore[arg-type]
        print()


if __name__ == "__main__":
    main()
