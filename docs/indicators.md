# Indicators

`scanlang` has two indicator registries:

- `INDICATORS` contains polars builders.
- `SQL_INDICATORS` contains DuckDB builders and is a superset of the polars
  registry.

Most TA-Lib indicators share one name on both engines. The polars builders
run exact TA-Lib values through an eager per-partition seam and need the
optional `talib` extra (`uv add 'scanlang[talib]'`); the DuckDB builders use
the community `talib` extension, loaded by `apply_sql` itself. Only
`ht_trendline`, `stoch_k`, and `stoch_d` remain DuckDB-only.

The full per-name table below is generated from the live registries:

```sh
uv run python scripts/gen_indicator_availability.py
```

--8<-- "reference/_indicator_availability.md"

The three DuckDB-only names (`ht_trendline`, `stoch_k`, `stoch_d`) are in the
full table (`reference/_indicator_availability_full.md`); they have no polars
builder.

For `ema`, `rsi`, and `atr`, the two engines converge after warm-up rather
than agreeing bar-for-bar (TA-Lib SMA seeding vs. first-value seeding). The
parity names (`adx`, `macd`, `bbands_*`, `aroon`, `kama`, `wma`, `dema`,
`tema`, `trima`, `mom`, `midprice`, `cci`, `willr`, `trange`, `ad`, and the
curated candlestick set) are exact TA-Lib values on both engines, bar-for-bar.
See the warm-up table in `reference/indicators.md` for the per-family counts.

## Multi-output field names

Multi-output TA-Lib functions are narrowed to one scalar per scanlang name —
never a struct through the IR:

| scanlang name | TA-Lib function | Field exposed |
| --- | --- | --- |
| `macd` | `MACD(close, fast, 26, 9)` | `macd` (the MACD line; signal and histogram derivable) |
| `bbands_upper` / `bbands_lower` | `BBANDS(close, n, 2.0, 2.0, 0)` | `upperband` / `lowerband` (middle band is `sma`) |
| `aroon` | `AROON(high, low, n)` | `aroon_up` |
| `stoch_k` / `stoch_d` | `STOCH(high, low, close, ...)` | `slowk` / `slowd` |

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

All remaining parity names follow the TA-Lib reference signature
(`macd(n)`, `bbands_upper(n)`, `adx(n)`, `aroon(n)`, `cdlengulfing()`, etc.) — the generated availability table lists
each name's args and required columns.

Indicators can nest:

```python
{"fn": "sma", "args": [
    {"fn": "rsi", "args": [{"col": "close"}, 14]},
    5,
]}
```

## DuckDB-only indicators

`ht_trendline`, `stoch_k`, and `stoch_d` use the community `talib` extension
and have no polars builder. Use these names with `compile_sql` or
`apply_sql`, and validate with `engine="duckdb"`. The polars engine rejects
them with `indicator '<name>' requires engine='duckdb'`.

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
`SQL_INDICATORS`. To make a name dual-engine, register it in both with
identical `arg_spec` and `required_cols`. The generated availability table
picks the change up on the next run.

See [API](reference/api.md) for the registry objects and callable signatures.
