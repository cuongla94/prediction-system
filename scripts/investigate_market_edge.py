"""WHY does the market beat the model, not just by how much? (2026-07-20)

Exploratory, no target fix. The same-day proofs (scripts/run_sameday_proof.py
and its generalization to 3 cities, see kalshi-backtest-findings memory)
established that the market wins at every decision time, in every city tried.
This asks what's actually behind that: is the market "smarter", or is a lot of
its apparent accuracy just being closer to a mechanically-determined answer
late in the day?

Four questions, each answered with real data, not assumed:

1. LOCKED vs UNDETERMINED decomposition. At each decision time, some brackets
   are already mechanically decided by monotonicity alone -- a daily HIGH can
   only go up through the day, so once the reading has crossed a bracket's
   upper edge, that bracket is permanently ruled out no matter what happens
   later; once it's crossed a floor-only bracket's lower edge, that bracket is
   permanently locked in. This is pure arithmetic, not a probability estimate
   (see `classify_lock_status` for the exact boundary logic, which mirrors
   weather.probability.bracket_probability's own +/-0.5 continuity convention).
   Splitting Brier/skill by locked-vs-undetermined answers "how much of the
   market's edge is just tracking a thermometer" directly.

2. Does price/volume move in step with known public data-release times (the
   hourly METAR at :51-:54)? If so, that's evidence of traders reading the
   same public station feed our own pipeline reads, just faster or more
   directly -- not a different information source.

3. Does bid-ask spread just track "how decided does this look" (distance from
   50/50, hours to close), rather than reflecting analytical depth?

4. How does the market's edge look at a decision time BEFORE the target day
   has even started -- the evening before, when essentially none of the day's
   temperature signal exists yet? If the edge is still large there, "the
   market is just closer to the answer" cannot be the whole story.

Reuses scripts.run_sameday_proof.collect_sameday_dataset for the day-ahead
rows/bias/std and IEM readings. Candlesticks are re-fetched here rather than
reused from that module, because this needs three things the shared
Candlestick model deliberately drops (see kalshi_client/models.py's own
docstring on why): trading volume, and a window starting the day BEFORE the
target date rather than at its midnight.

Not part of the live pipeline; writes nothing to the database. Same role as
scripts/forecast_vs_market.py and its own sameday_proof_* predecessors.

Usage: uv run scripts/investigate_market_edge.py [series_ticker]
(defaults to KXHIGHNY; pass e.g. KXHIGHDEN to spot-check another city on the
same four questions -- this is deliberately a CLI arg, not a hardcoded loop
over cities, since each run is a genuine live data pull.)
"""

from __future__ import annotations

import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from datetime import time as dtime
from statistics import median

from dotenv import load_dotenv

from backtest.calibration import market_benchmark
from kalshi_client import KalshiClient
from scripts.run_sameday_proof import collect_sameday_dataset, decision_ts
from weather.calibration_params import get_calibration
from weather.historical_observations import extreme_as_of
from weather.probability import bracket_probability, observation_conditioned_bracket_probability

SERIES_TICKER = "KXHIGHNY"

# (day offset from target date, local standard time) -- extends the existing
# 09:00/12:00/15:00 proof both earlier in the day and into the evening before,
# to answer question 4. day-1 18:00 was confirmed live to have real trading
# volume before committing to this list (markets open ~10:00 the day before).
DECISION_POINTS: tuple[tuple[int, dtime], ...] = (
    (-1, dtime(18, 0)),
    (-1, dtime(21, 0)),
    (0, dtime(0, 0)),
    (0, dtime(6, 0)),
    (0, dtime(9, 0)),
    (0, dtime(12, 0)),
    (0, dtime(15, 0)),
)


@dataclass(frozen=True)
class RawCandle:
    end_ts: int
    bid: float | None
    ask: float | None
    volume: float


def _label(point: tuple[int, dtime]) -> str:
    day_offset, t = point
    prefix = "day-1 " if day_offset == -1 else ""
    return f"{prefix}{t.strftime('%H:%M')}"


def fetch_raw_candles(client: KalshiClient, series_ticker: str, market_ticker: str, start_ts: int, end_ts: int) -> list[RawCandle]:
    """The three fields kalshi_client.models.Candlestick deliberately drops
    (volume, and everything needed for a pre-target-date window) fetched
    directly, reusing the exact same endpoint get_candlesticks calls."""
    data = client._request(
        "GET",
        f"/series/{series_ticker}/markets/{market_ticker}/candlesticks",
        params={"start_ts": start_ts, "end_ts": end_ts, "period_interval": 1},
    )
    out = []
    for c in data["candlesticks"]:
        bid = (c.get("yes_bid") or {}).get("close_dollars")
        ask = (c.get("yes_ask") or {}).get("close_dollars")
        vol = c.get("volume_fp")
        out.append(RawCandle(
            end_ts=c["end_period_ts"],
            bid=float(bid) if bid is not None else None,
            ask=float(ask) if ask is not None else None,
            volume=float(vol) if vol is not None else 0.0,
        ))
    return out


def price_as_of(candles: list[RawCandle], ts: int) -> float | None:
    candidates = [c for c in candles if c.end_ts <= ts]
    if not candidates:
        return None
    candle = max(candidates, key=lambda c: c.end_ts)
    if candle.bid is None or candle.ask is None:
        return None
    return (candle.bid + candle.ask) / 2


def spread_as_of(candles: list[RawCandle], ts: int) -> float | None:
    candidates = [c for c in candles if c.end_ts <= ts]
    if not candidates:
        return None
    candle = max(candidates, key=lambda c: c.end_ts)
    if candle.bid is None or candle.ask is None:
        return None
    return candle.ask - candle.bid


def classify_lock_status(
    observed_so_far: float | None, metric: str, floor_strike: float | None, cap_strike: float | None
) -> str:
    """Pure monotonicity, no probability involved -- "locked" here means
    arithmetically guaranteed by the definition of a daily max/min, not merely
    likely. Mirrors weather.probability.bracket_probability's own +/-0.5
    continuity boundary (a whole-degree NWS reading; "less than 79" excludes
    79 itself, boundary at 78.5; "between 79-80" spans 78.5-80.5).

    metric="max" (final = max(observed_so_far, rest_of_day), non-decreasing):
      - floor-only ("> floor"): LOCKED_TRUE once observed > floor+0.5 -- once
        exceeded it can never un-exceed. LOCKED_FALSE is never knowable
        mid-day this way (it could still rise past floor later).
      - cap-only ("< cap"): LOCKED_FALSE once observed >= cap-0.5 -- once at
        or past the cutoff it can only go higher. LOCKED_TRUE is never
        knowable mid-day (still time to rise past the cutoff).
      - between: LOCKED_FALSE once observed >= cap+0.5 (busted above; can't
        come back down). LOCKED_TRUE is never knowable mid-day -- staying
        below the top is never guaranteed until the day is actually over.
    metric="min" is the exact mirror (final is non-increasing).

    Returns "LOCKED_TRUE", "LOCKED_FALSE", or "UNDETERMINED".
    """
    if metric not in ("min", "max"):
        raise ValueError(f"metric must be 'min' or 'max', got {metric!r}")
    if observed_so_far is None:
        return "UNDETERMINED"

    if metric == "max":
        if floor_strike is None and cap_strike is not None:
            return "LOCKED_FALSE" if observed_so_far >= cap_strike - 0.5 else "UNDETERMINED"
        if cap_strike is None and floor_strike is not None:
            return "LOCKED_TRUE" if observed_so_far > floor_strike + 0.5 else "UNDETERMINED"
        if floor_strike is not None and cap_strike is not None:
            return "LOCKED_FALSE" if observed_so_far >= cap_strike + 0.5 else "UNDETERMINED"
    else:  # min
        if floor_strike is None and cap_strike is not None:
            return "LOCKED_TRUE" if observed_so_far < cap_strike - 0.5 else "UNDETERMINED"
        if cap_strike is None and floor_strike is not None:
            return "LOCKED_FALSE" if observed_so_far <= floor_strike + 0.5 else "UNDETERMINED"
        if floor_strike is not None and cap_strike is not None:
            return "LOCKED_FALSE" if observed_so_far <= floor_strike - 0.5 else "UNDETERMINED"
    raise ValueError("Market has neither floor_strike nor cap_strike set.")


def main() -> None:
    load_dotenv()
    series_ticker = sys.argv[1] if len(sys.argv) > 1 else SERIES_TICKER
    dataset = collect_sameday_dataset(series_ticker)
    tz = dataset.tz
    calibration = get_calibration(series_ticker)

    print(f"\nFetching WIDE candlesticks (day-1 00:00 .. day 23:59, 1-min, +volume) "
          f"for {len(dataset.proof_rows)} rows / "
          f"{len({r.market_ticker for r in dataset.proof_rows})} markets...")
    wide_candles: dict[str, list[RawCandle]] = {}
    with KalshiClient() as client:
        for i, row in enumerate(dataset.proof_rows):
            if row.market_ticker in wide_candles:
                continue
            target = date.fromisoformat(row.target_date)
            start = int(datetime.combine(target - timedelta(days=1), dtime.min, tzinfo=tz).timestamp())
            stop = int(datetime.combine(target, dtime(23, 59), tzinfo=tz).timestamp())
            try:
                wide_candles[row.market_ticker] = fetch_raw_candles(client, series_ticker, row.market_ticker, start, stop)
            except Exception as exc:  # noqa: BLE001
                print(f"  {row.market_ticker}: fetch failed ({exc.__class__.__name__}: {exc}), skipping.")
                wide_candles[row.market_ticker] = []
            if (i + 1) % 20 == 0:
                print(f"  ...{i + 1}/{len(dataset.proof_rows)}")

    # ============================================================
    # Q1: locked vs undetermined decomposition
    # ============================================================
    print(f"\n{'=' * 78}\nQ1: LOCKED (mechanically decided) vs UNDETERMINED, per decision point\n{'=' * 78}")
    header = f"{'decision':<10} {'group':<13} {'n':>4} {'model':>8} {'market':>8} {'skill':>9}  share of n"
    print(header)
    print("-" * len(header))
    for day_offset, t in DECISION_POINTS:
        by_group: dict[str, dict[str, list]] = defaultdict(lambda: {"model": [], "market": [], "outcome": []})
        for row in dataset.proof_rows:
            target = date.fromisoformat(row.target_date)
            ts = decision_ts(target + timedelta(days=day_offset), t, tz)
            price = price_as_of(wide_candles.get(row.market_ticker, []), ts)
            if price is None:
                continue
            # Only a decision point ON the target date (day_offset == 0) can have
            # an observation at all -- day-1 points are, by construction, before
            # the target day's temperature record exists.
            observed = extreme_as_of(dataset.readings, target, t, dataset.station.metric) if day_offset == 0 else None
            loc = row.forecast_mean + calibration.bias_for_month(target.month)
            model_pred = (
                bracket_probability(loc, dataset.normal_std, row.floor_strike, row.cap_strike)
                if observed is None
                else observation_conditioned_bracket_probability(
                    loc, dataset.normal_std, row.floor_strike, row.cap_strike, dataset.station.metric, observed
                )
            )

            lock = classify_lock_status(observed, dataset.station.metric, row.floor_strike, row.cap_strike)
            by_group[lock]["model"].append(model_pred)
            by_group[lock]["market"].append(price)
            by_group[lock]["outcome"].append(row.actual_outcome)
            by_group["ALL"]["model"].append(model_pred)
            by_group["ALL"]["market"].append(price)
            by_group["ALL"]["outcome"].append(row.actual_outcome)

        total_n = len(by_group["ALL"]["outcome"])
        for group in ("ALL", "LOCKED_TRUE", "LOCKED_FALSE", "UNDETERMINED"):
            g = by_group.get(group)
            if not g or not g["outcome"]:
                print(f"{_label((day_offset, t)):<10} {group:<13}    0        -        -        -   -")
                continue
            bench = market_benchmark(g["model"], g["market"], g["outcome"])
            if bench is None:
                continue
            share = f"{100 * bench.n / total_n:.0f}%" if total_n else "-"
            print(
                f"{_label((day_offset, t)):<10} {group:<13} {bench.n:>4} {bench.brier_model:>8.4f} "
                f"{bench.brier_market:>8.4f} {bench.skill_score:>+9.4f}  {share}"
            )
        print()

    # ============================================================
    # Q2: price movement / volume vs minute-of-hour (METAR clustering)
    # ============================================================
    print(f"\n{'=' * 78}\nQ2: mean |price change| and volume by minute-of-hour (target-day only)\n{'=' * 78}")
    move_by_minute: dict[int, list[float]] = defaultdict(list)
    vol_by_minute: dict[int, list[float]] = defaultdict(list)
    for row in dataset.proof_rows:
        target = date.fromisoformat(row.target_date)
        day_start = decision_ts(target, dtime.min, tz)
        day_end = decision_ts(target, dtime(23, 59), tz)
        candles = sorted(
            (c for c in wide_candles.get(row.market_ticker, []) if day_start <= c.end_ts <= day_end),
            key=lambda c: c.end_ts,
        )
        prev_mid = None
        for c in candles:
            minute = datetime.fromtimestamp(c.end_ts, tz).minute
            if c.bid is not None and c.ask is not None:
                mid = (c.bid + c.ask) / 2
                if prev_mid is not None:
                    move_by_minute[minute].append(abs(mid - prev_mid))
                prev_mid = mid
            vol_by_minute[minute].append(c.volume)

    print(f"{'minute':>6} {'mean|Δprice|':>12} {'n':>6}   {'mean vol':>10} {'n':>6}")
    for minute in range(0, 60, 5):
        window = [m for m in range(minute, minute + 5)]
        moves = [v for m in window for v in move_by_minute.get(m, [])]
        vols = [v for m in window for v in vol_by_minute.get(m, [])]
        flag = "  <- hourly METAR window" if minute == 50 else ""
        mean_move = sum(moves) / len(moves) if moves else 0.0
        mean_vol = sum(vols) / len(vols) if vols else 0.0
        print(f"{minute:>3}-{minute + 4:<3} {mean_move:>12.5f} {len(moves):>6}   {mean_vol:>10.2f} {len(vols):>6}{flag}")

    # ============================================================
    # Q3: bid-ask spread vs hours-to-close and vs "decidedness"
    # ============================================================
    print(f"\n{'=' * 78}\nQ3: bid-ask spread vs hours-to-close and vs |price - 0.5|\n{'=' * 78}")
    spread_by_hours_bucket: dict[int, list[float]] = defaultdict(list)
    spread_by_decided_bucket: dict[int, list[float]] = defaultdict(list)
    for row in dataset.proof_rows:
        target = date.fromisoformat(row.target_date)
        day_start = decision_ts(target, dtime.min, tz)
        day_end = decision_ts(target, dtime(23, 59), tz)
        candles = sorted(
            (c for c in wide_candles.get(row.market_ticker, []) if day_start <= c.end_ts <= day_end),
            key=lambda c: c.end_ts,
        )
        for c in candles:
            if c.bid is None or c.ask is None:
                continue
            spread = c.ask - c.bid
            hours_to_close = (day_end - c.end_ts) / 3600
            hours_bucket = min(int(hours_to_close), 23)
            spread_by_hours_bucket[hours_bucket].append(spread)
            mid = (c.bid + c.ask) / 2
            decided = abs(mid - 0.5)
            decided_bucket = min(int(decided * 10), 5)  # 0=coin-flip .. 5=near-certain
            spread_by_decided_bucket[decided_bucket].append(spread)

    print("spread vs hours-to-close (0h = right before close):")
    for h in sorted(spread_by_hours_bucket, reverse=True):
        vals = spread_by_hours_bucket[h]
        print(f"  {h:>2}h left: median spread {median(vals):.4f}  (n={len(vals)})")
    print("\nspread vs |price - 0.5| (0 = coin-flip, 0.5 = certain):")
    for b in sorted(spread_by_decided_bucket):
        vals = spread_by_decided_bucket[b]
        print(f"  |mid-0.5| in [{b/10:.1f},{(b+1)/10:.1f}): median spread {median(vals):.4f}  (n={len(vals)})")


if __name__ == "__main__":
    main()
