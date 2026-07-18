# kalshi-prediction-market

Semi-automated signal system for Kalshi's daily temperature markets. Compares
weather ensemble forecasts (Open-Meteo) against Kalshi prices, surfaces edge
above fees + a safety margin as reviewable alerts on a dashboard. No
auto-execution — a human clicks through to Kalshi and trades manually.

Cities: NYC, Chicago, Philadelphia, Austin, Denver, Miami.

## Setup

```bash
uv sync
cp .env.example .env   # fill in Kalshi credentials if/when you need authenticated endpoints
```

Series/event/market discovery is public and needs no credentials. Credentials
are only required for portfolio/trading endpoints, which this project doesn't
call yet (no auto-execution is planned).

## Structure

- `kalshi_client/` — Kalshi REST client: RSA-PSS request signing (`auth.py`),
  the client itself (`client.py`), response models (`models.py`).
- `tests/` — unit tests (`uv run pytest`).
- `scripts/discover_markets.py` — manual smoke test against live public
  endpoints: resolves a series, finds the next open event, prints each
  bracket's rules text and price.

## Commands

```bash
uv run pytest                          # run tests
uv run ruff check .                    # lint
uv run scripts/discover_markets.py     # e.g. KXHIGHNY by default; pass a series ticker to override
```

## Notes

- Ticker hierarchy (series → event → market) is always resolved at runtime via
  the API, never hardcoded — Kalshi has changed ticker conventions before.
- Settlement ground truth is the NWS Daily Climate Report for the market's
  station, which reports in local *standard* time even during DST. This
  project isn't at the backtesting stage yet, but that boundary matters once
  it is.
