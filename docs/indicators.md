# Indicators

`scanlang` has two indicator registries:

- `INDICATORS` contains polars builders.
- `SQL_INDICATORS` contains DuckDB builders and is a superset of the polars
  registry.

## Availability

| Indicator | polars | DuckDB | DuckDB lowering |
| --- | --- | --- | --- |
| `sma` | yes | yes | native window |
| `rmin`, `rmax`, `shift` | yes | yes | native window |
| `ema` | yes | yes | `t_ema` |
| `rsi` | yes | yes | `t_rsi` |
| `atr` | yes | yes | `t_atr` |
| `adr` | yes | yes | staged native windows |
| `roc` | yes | yes | `t_roc` |
| `natr` | yes | yes | `t_natr` |
| `slope` | yes | yes | `t_linearreg_slope` |
| `rs_ratio`, `rs_momentum` | yes | yes | smoothing plus window z-score |
| `macd` | no | yes | `t_macd`, MACD line |
| `bbands_upper`, `bbands_lower` | no | yes | `t_bbands` |
| `adx` | no | yes | `t_adx` |
| `aroon` | no | yes | `t_aroon`, up line |
| `cdlengulfing` | no | yes | `t_cdlengulfing` |
| `ht_trendline` | no | yes | `t_ht_trendline` |

The engine passed to `validate` controls which names are accepted. Use
`validate(scan_def, engine="duckdb")` for DuckDB-only indicators.

## Signatures

`n` is a literal integer greater than or equal to 1. The first argument is an
expression unless noted otherwise. `ema(20)`, `sma(20)`, `rmin(20)`, and
`rmax(20)` use `close` by default.

| Name | Signature | Meaning |
| --- | --- | --- |
| `sma` | `sma(expr, n)` | rolling mean |
| `ema` | `ema(expr, n)` | exponential mean, `adjust=False` |
| `rsi` | `rsi(expr, n)` | Wilder-smoothed RSI, 0 to 100 |
| `atr` | `atr(n)` | Wilder-smoothed true range from high, low, close |
| `adr` | `adr(expr, n)` | average true range / close × 100 |
| `roc` | `roc(expr, n)` | percentage change from `n` bars ago |
| `natr` | `natr(expr, n)` | normalized ATR |
| `slope` | `slope(expr, n)` | rolling OLS slope |
| `rmin` | `rmin(expr, n)` | rolling minimum |
| `rmax` | `rmax(expr, n)` | rolling maximum |
| `shift` | `shift(expr, n)` | previous value |
| `rs_ratio` | `rs_ratio(expr, n)` | EMA(5), then trailing z-score |
| `rs_momentum` | `rs_momentum(expr, n)` | 4-bar ROC, EMA(3), then trailing z-score |
| `macd` | `macd(n)` | DuckDB MACD line |
| `bbands_upper` | `bbands_upper(n)` | DuckDB upper Bollinger band |
| `bbands_lower` | `bbands_lower(n)` | DuckDB lower Bollinger band |
| `adx` | `adx(n)` | DuckDB average directional index |
| `aroon` | `aroon(n)` | DuckDB Aroon up line |
| `cdlengulfing` | `cdlengulfing(n)` | DuckDB engulfing candlestick match |
| `ht_trendline` | `ht_trendline(n)` | DuckDB Hilbert transform trendline |

Indicators can nest:

```python
{"fn": "sma", "args": [
    {"fn": "rsi", "args": [{"col": "close"}, 14]},
    5,
]}
```

## DuckDB-only indicators

DuckDB uses its community `talib` extension for indicators that do not have a
polars builder. Multi-output functions are narrowed to useful scan fields:

- `macd` is the MACD line. Its signal and histogram can be derived from it.
- `bbands_upper` and `bbands_lower` expose the two bands; the middle band is
  `sma`.
- `aroon` exposes the up line.
- `cdlengulfing` returns TA-Lib's `0` or `100` match value.

Use these names with `compile_sql` or `apply_sql`, and validate with
`engine="duckdb"`. The polars engine rejects them.

## Semantics

`ema`, `rsi`, and `atr` use TA-Lib-style recursion in the polars registry.
TA-Lib seeds the first value with an SMA, while polars starts its
`ewm_mean` from the first value. Early values can differ and converge after
warm-up. In the tested benchmark, they agree within `0.01` at about `7.6 × n`.

`rsi` uses Wilder smoothing and fills its initial undefined result with `50.0`.
`atr` uses true range:

```text
max(high - low, abs(high - previous_close), abs(low - previous_close))
```

`rs_ratio` and `rs_momentum` return null until the full trailing z-score window
exists. A zero-variance window returns `100.0`. Results are clamped to
`80.0` through `120.0`.

## Extend a registry

For a polars indicator, add `(arg_spec, builder, required_cols)` to
`INDICATORS`. For a DuckDB-only indicator, add the same shape to
`SQL_INDICATORS`. Keep the engine-specific validation and availability table
in sync.

See [API](reference/api.md) for the registry objects and callable signatures.
