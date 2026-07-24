# Blend semantics

`final_probability = model_weight × weather_model_probability + market_weight × market_probability`.

- `model_weight=0.00`, `market_weight=1.00`: pure market.
- `model_weight=1.00`, `market_weight=0.00`: pure weather model.

The earlier `market_blend_weight` implementation was not reversed: it was the market weight. The field and candidate names were ambiguous and have been replaced with explicit model and market weights.
