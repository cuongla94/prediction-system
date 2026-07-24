# Real-Trading Readiness Gap Investigation

Overall conclusion: **NOT_READY_FOR_REAL_TRADING**

The configured live strategy remains unchanged and historically **FAILED**. No candidate was promoted and automatic promotion is prohibited.

## Report corrections

- The old `market_blend_weight` was the market weight. The implementation was not reversed; the label was ambiguous.
- Blend candidates now expose both `model_weight` and `market_weight`.
- Probability-scored outcomes, independent city/date clusters, eligible directional signals, submitted paper orders, fills, settled trades, wins, losses, voids, and no-trade events are distinct populations.
- Historical directional signal outcomes are no longer presented as executed trade results.
- Every Brier comparison reports its matched common population and exclusions.

## Clustered uncertainty

- Mean candidate-minus-market Brier difference: 0.013575.
- Median cluster difference: 0.012181.
- 90% interval: [0.009058009405794591, 0.018115748194686875].
- 95% interval: [0.008206436186787298, 0.018965445719036297].
- Probability candidate beats market: 0.000.
- Independent city/date clusters: 39.
- Distinguishable from noise: **False**.

## Current readiness gates

- DATA_INTEGRITY: **INSUFFICIENT_EVIDENCE**
- FORECAST_SKILL: **INSUFFICIENT_EVIDENCE**
- MARKET_INCREMENTAL_VALUE: **FAIL**
- EXECUTION_EVIDENCE: **INSUFFICIENT_EVIDENCE**
- FORWARD_CONFIRMATION: **INSUFFICIENT_EVIDENCE**
- OPERATIONAL_SAFETY: **PASS**

## Frozen confirmatory candidates

- `forward-blend-model-0.50-market-0.50-v1`: model_weight=0.50, market_weight=0.50.
- `forward-blend-model-0.25-market-0.75-v1`: model_weight=0.25, market_weight=0.75.

Candidate A is the prior investigation's best validation blend. Candidate B is the already-registered, simple stronger-market blend that posted the lowest exploratory holdout Brier; it is frozen as a single confirmatory alternative, not promoted.

## Forward collection

- Schema deployed: True.
- Calendar days: 1.
- Independent events: 40.
- Eligible paper trades: 0.
- Settled eligible paper trades: 0.
- Next action: Continue prospective collection without changing frozen parameters.

The collector records REST orderbook baselines on initial connection, reconnect, process restart, and sequence gaps; WebSocket snapshots, deltas, trades, lifecycle events, and authenticated fills; source and receipt timestamps; full configured depth; and conservative immediate-taker paper outcomes. A last trade alone never creates a fill.

## Conditions before reconsidering real trading

- At least 60 forward calendar days.
- At least 100 independent city/date events.
- At least 100 settled eligible prospective paper trades.
- At least 5 cities and 2 forecast horizons with no unresolved integrity violations.
- Candidate beats the current weather model and adds information beyond market.
- Positive fee-aware net expectancy, profit factor above 1, and drawdown within the approved limit under conservative fills.
- Evidence is not concentrated in one city/date and frozen parameters remain unchanged.
- Explicit human review; there is no automatic promotion path.
