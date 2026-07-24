# Strategy Investigation Data Quality

- Canonical rows: 720
- Independent events: 120
- Unique markets: 720
- Date range: 2026-07-20..2026-07-22
- Cities: 20
- Missing published-observation value: 480
- Missing ensemble mean/spread: 0
- No-lookahead violations: 0

## Execution evidence

`price_snapshots` and `forecast_pulls` contain no rows. Alerts retain a market midpoint but not synchronized bid/ask, depth, or independent quote timestamps. All candidate trading metrics are therefore labeled `FORECAST_SKILL_ONLY`; P&L, fees, profit factor, expectancy, and drawdown are null rather than fabricated.

## Inventory sources

- alerts: 55878 rows
- forecast_pulls: 0 rows
- price_snapshots: 0 rows
- paper_trades: 157 rows
- pipeline_runs: 1021 rows
- bot_control_events: 8 rows
- live_orders: 0 rows
- live_order_fills: 0 rows
- live_reconciliation_runs: 0 rows
- alerts_coverage: coverage record rows
- metar_observations: 49338 rows
- settlement_outcomes: 42774 rows
- historical_real_trades: 0 rows
- strategy_versions: 3 rows
- prior_backtests_walk_forwards_ablations: None rows
