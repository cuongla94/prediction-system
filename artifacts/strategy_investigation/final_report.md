# Climate Strategy Investigation

Status: **COMPLETE_NO_PROMOTION**

The configured live strategy remains unchanged and historically **FAILED**. No candidate was promoted.

## Confirmed findings

- Current validation Brier: 0.1118.
- Matched market Brier: 0.1089.
- Market incremental-information holdout result: model adds value = False.
- Stored model probabilities remain materially less accurate than captured market probabilities; model-center error, calibration error, bracket behavior, and observation state all contribute.
- Executable spread/slippage and unfilled-order effects cannot be measured from the collected database because synchronized bid/ask/depth history is absent.

## Candidate comparison

Probability, filtering, and execution are separate populations. Directional W/L describes settled historical signal direction only; executed trade W/L remains zero until prospective paper orders are observed.

| Candidate | Model wt | Market wt | Probability-scored markets | City/date clusters | Eligible signals | Directional W/L | No-trade clusters | Submitted / filled / settled paper | Trade W/L/V | Model Brier | Market Brier | Common population | Holdout Brier | Promotion |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| current_observation_conditioned | — | — | 240 | 40 | 39 | 18/21 | 1 | 0/0/0 | 0/0/0 | 0.1118 | 0.1089 | 240 | 0.1193 | REJECTED |
| hard_lower_bound_truncation | — | — | 240 | 40 | 39 | 18/21 | 1 | 0/0/0 | 0/0/0 | 0.1118 | 0.1089 | 240 | 0.1193 | REJECTED |
| raw_ensemble | — | — | 240 | 40 | 39 | 16/23 | 1 | 0/0/0 | 0/0/0 | 0.1127 | 0.1089 | 240 | 0.1195 | REJECTED |
| empirical_remaining_day_residual | — | — | 240 | 40 | 37 | 16/21 | 3 | 0/0/0 | 0/0/0 | 0.1141 | 0.1089 | 240 | 0.1225 | REJECTED |
| logistic_calibration | — | — | 240 | 40 | 39 | 18/21 | 1 | 0/0/0 | 0/0/0 | 0.1115 | 0.1089 | 240 | 0.1186 | REJECTED |
| isotonic_calibration | — | — | 240 | 40 | 39 | 19/20 | 1 | 0/0/0 | 0/0/0 | 0.1121 | 0.1089 | 240 | 0.1212 | REJECTED |
| market_prior_weather_update | — | — | 240 | 40 | 10 | 7/3 | 30 | 0/0/0 | 0/0/0 | 0.1058 | 0.1089 | 240 | 0.1117 | REJECTED |
| conservative_no_trade_filter | — | — | 240 | 40 | 33 | 18/15 | 7 | 0/0/0 | 0/0/0 | 0.1118 | 0.1089 | 240 | 0.1193 | REJECTED |
| newly_impossible_brackets | — | — | 240 | 40 | 0 | 0/0 | 40 | 0/0/0 | 0/0/0 | 0.1118 | 0.1089 | 240 | 0.1193 | REJECTED |
| late_day_observation_filter | — | — | 240 | 40 | 0 | 0/0 | 40 | 0/0/0 | 0/0/0 | 0.1118 | 0.1089 | 240 | 0.1193 | REJECTED |
| low_model_disagreement | — | — | 240 | 40 | 12 | 4/8 | 28 | 0/0/0 | 0/0/0 | 0.1118 | 0.1089 | 240 | 0.1193 | REJECTED |
| blend_model_1.00_market_0.00 | 1.0000 | 0.0000 | 240 | 40 | 39 | 18/21 | 1 | 0/0/0 | 0/0/0 | 0.1118 | 0.1089 | 240 | 0.1193 | REJECTED |
| blend_model_0.90_market_0.10 | 0.9000 | 0.1000 | 240 | 40 | 39 | 18/21 | 1 | 0/0/0 | 0/0/0 | 0.1096 | 0.1089 | 240 | 0.1173 | REJECTED |
| blend_model_0.80_market_0.20 | 0.8000 | 0.2000 | 240 | 40 | 39 | 18/21 | 1 | 0/0/0 | 0/0/0 | 0.1079 | 0.1089 | 240 | 0.1156 | REJECTED |
| blend_model_0.70_market_0.30 | 0.7000 | 0.3000 | 240 | 40 | 36 | 17/19 | 4 | 0/0/0 | 0/0/0 | 0.1066 | 0.1089 | 240 | 0.1141 | REJECTED |
| blend_model_0.50_market_0.50 | 0.5000 | 0.5000 | 240 | 40 | 33 | 15/18 | 7 | 0/0/0 | 0/0/0 | 0.1051 | 0.1089 | 240 | 0.1122 | REJECTED |
| blend_model_0.25_market_0.75 | 0.2500 | 0.7500 | 240 | 40 | 15 | 3/12 | 25 | 0/0/0 | 0/0/0 | 0.1057 | 0.1089 | 240 | 0.1115 | REJECTED |
| blend_model_0.00_market_1.00 | 0.0000 | 1.0000 | 240 | 40 | 0 | 0/0 | 40 | 0/0/0 | 0/0/0 | 0.1089 | 0.1089 | 240 | 0.1127 | REJECTED |
| individual_gfs | — | — | 0 | 40 | 0 | 0/0 | 40 | 0/0/0 | 0/0/0 | — | — | 0 | — | REJECTED |
| individual_ecmwf | — | — | 0 | 40 | 0 | 0/0 | 40 | 0/0/0 | 0/0/0 | — | — | 0 | — | REJECTED |
| individual_icon | — | — | 0 | 40 | 0 | 0/0 | 40 | 0/0/0 | 0/0/0 | — | — | 0 | — | REJECTED |
| cross_bracket_executable_consistency | — | — | 0 | 40 | 0 | 0/0 | 40 | 0/0/0 | 0/0/0 | — | — | 0 | — | REJECTED |
| remaining_hour_maximum_simulation | — | — | 0 | 40 | 0 | 0/0 | 40 | 0/0/0 | 0/0/0 | — | — | 0 | — | REJECTED |

## Error decomposition

- Forecast-center residuals: {"dimension": "all", "cohort": "all", "sample_size": 32, "mean_error": -0.16494222689074767, "median_error": 0.1365546218487026, "mae": 1.5986081932772593, "rmse": 2.172611176366019, "standard_deviation": 2.2010046704137944, "q10": -2.932436974789863, "q25": -0.76260504201678, "q75": 1.1850840336134532, "q90": 2.1326890756301946, "skew": -0.8024780028189198}
- Probability calibration: {"current_brier": 0.11180105234419165, "calibration_gap": 0.040149698986619346}
- Observation-conditioned impossible rows: 0.
- Tail vs bounded: {"tail_rows": 80, "tail_brier": 0.05601074035136307, "bounded_rows": 160, "bounded_brier": 0.13969620834060595}

## Final holdout and recommendation

Best validation candidate: **blend_model_0.50_market_0.50**. Its untouched holdout Brier is 0.1122 versus market 0.1127.

No candidate is promotable: executable bid/ask/depth history is absent, the sample spans only a few dates, and no forward paper confirmatory period exists.

Forward data required: synchronized executable YES/NO bid and ask, depth, quote timestamps, forecast run/availability timestamps, observation publication and revision timestamps, and a separately accrued forward paper period. Candidate selection must be frozen before that confirmatory period.
