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

| Candidate | Events | Wins | Losses | Win rate | Brier | Market Brier | Calibration gap | Gross P&L | Fees | Net P&L | Profit factor | Expectancy | Max drawdown | Holdout Brier | Promotion |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- | --- | --- | --- | --- | ---: | --- |
| current_observation_conditioned | 40 | 18 | 21 | 0.4615 | 0.1118 | 0.1089 | 0.0401 | — | — | — | — | — | — | 0.1193 | REJECTED |
| hard_lower_bound_truncation | 40 | 18 | 21 | 0.4615 | 0.1118 | 0.1089 | 0.0401 | — | — | — | — | — | — | 0.1193 | REJECTED |
| raw_ensemble | 40 | 16 | 23 | 0.4103 | 0.1127 | 0.1089 | 0.0344 | — | — | — | — | — | — | 0.1195 | REJECTED |
| empirical_remaining_day_residual | 40 | 16 | 21 | 0.4324 | 0.1141 | 0.1089 | 0.0302 | — | — | — | — | — | — | 0.1225 | REJECTED |
| logistic_calibration | 40 | 18 | 21 | 0.4615 | 0.1115 | 0.1089 | 0.0395 | — | — | — | — | — | — | 0.1186 | REJECTED |
| isotonic_calibration | 40 | 19 | 20 | 0.4872 | 0.1121 | 0.1089 | 0.0275 | — | — | — | — | — | — | 0.1212 | REJECTED |
| market_prior_weather_update | 40 | 7 | 3 | 0.7000 | 0.1058 | 0.1089 | 0.0513 | — | — | — | — | — | — | 0.1117 | REJECTED |
| conservative_no_trade_filter | 40 | 18 | 15 | 0.5455 | 0.1118 | 0.1089 | 0.0401 | — | — | — | — | — | — | 0.1193 | REJECTED |
| newly_impossible_brackets | 40 | 0 | 0 | — | 0.1118 | 0.1089 | 0.0401 | — | — | — | — | — | — | 0.1193 | REJECTED |
| late_day_observation_filter | 40 | 0 | 0 | — | 0.1118 | 0.1089 | 0.0401 | — | — | — | — | — | — | 0.1193 | REJECTED |
| low_model_disagreement | 40 | 4 | 8 | 0.3333 | 0.1118 | 0.1089 | 0.0401 | — | — | — | — | — | — | 0.1193 | REJECTED |
| market_blend_0.00 | 40 | 18 | 21 | 0.4615 | 0.1118 | 0.1089 | 0.0401 | — | — | — | — | — | — | 0.1193 | REJECTED |
| market_blend_0.10 | 40 | 18 | 21 | 0.4615 | 0.1096 | 0.1089 | 0.0415 | — | — | — | — | — | — | 0.1173 | REJECTED |
| market_blend_0.20 | 40 | 18 | 21 | 0.4615 | 0.1079 | 0.1089 | 0.0405 | — | — | — | — | — | — | 0.1156 | REJECTED |
| market_blend_0.30 | 40 | 17 | 19 | 0.4722 | 0.1066 | 0.1089 | 0.0511 | — | — | — | — | — | — | 0.1141 | REJECTED |
| market_blend_0.50 | 40 | 15 | 18 | 0.4545 | 0.1051 | 0.1089 | 0.0607 | — | — | — | — | — | — | 0.1122 | REJECTED |
| market_blend_0.75 | 40 | 3 | 12 | 0.2000 | 0.1057 | 0.1089 | 0.0556 | — | — | — | — | — | — | 0.1115 | REJECTED |
| market_blend_1.00 | 40 | 0 | 0 | — | 0.1089 | 0.1089 | 0.0302 | — | — | — | — | — | — | 0.1127 | REJECTED |
| individual_gfs | 40 | 0 | 0 | — | — | — | — | — | — | — | — | — | — | — | REJECTED |
| individual_ecmwf | 40 | 0 | 0 | — | — | — | — | — | — | — | — | — | — | — | REJECTED |
| individual_icon | 40 | 0 | 0 | — | — | — | — | — | — | — | — | — | — | — | REJECTED |
| cross_bracket_executable_consistency | 40 | 0 | 0 | — | — | — | — | — | — | — | — | — | — | — | REJECTED |
| remaining_hour_maximum_simulation | 40 | 0 | 0 | — | — | — | — | — | — | — | — | — | — | — | REJECTED |

## Error decomposition

- Forecast-center residuals: {"dimension": "all", "cohort": "all", "sample_size": 32, "mean_error": -0.16494222689074767, "median_error": 0.1365546218487026, "mae": 1.5986081932772593, "rmse": 2.172611176366019, "standard_deviation": 2.2010046704137944, "q10": -2.932436974789863, "q25": -0.76260504201678, "q75": 1.1850840336134532, "q90": 2.1326890756301946, "skew": -0.8024780028189198}
- Probability calibration: {"current_brier": 0.11180105234419165, "calibration_gap": 0.040149698986619346}
- Observation-conditioned impossible rows: 0.
- Tail vs bounded: {"tail_rows": 80, "tail_brier": 0.05601074035136307, "bounded_rows": 160, "bounded_brier": 0.13969620834060595}

## Final holdout and recommendation

Best validation candidate: **market_blend_0.50**. Its untouched holdout Brier is 0.1122 versus market 0.1127.

No candidate is promotable: executable bid/ask/depth history is absent, the sample spans only a few dates, and no forward paper confirmatory period exists.

Forward data required: synchronized executable YES/NO bid and ask, depth, quote timestamps, forecast run/availability timestamps, observation publication and revision timestamps, and a separately accrued forward paper period. Candidate selection must be frozen before that confirmatory period.
