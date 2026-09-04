# Indicators

Built-in indicators live in [`INDICATORS`](../reference/api.md) and can
be extended by insertion. Each entry is `(arg_spec, builder, required_cols)`:

- `arg_spec` — tuple of `"expr"` (any operand) or `"int"` (literal int >= 1)
- `builder(*parsed, partition) -> pl.Expr` — your polars expression,
  window ops via `.over(partition)`
- `required_cols` — column names the catalog must have

## Engine availability

Indicators run on one or both backends. The polars backend is the
default and supports everything in `INDICATORS`. The duckdb backend
adds the rest through [`SQL_INDICATORS`](../reference/duckdb-backend.md),
which is a strict superset of `INDICATORS`.

| Indicator | polars | duckdb | Lowering on duckdb |
| --- | --- | --- | --- |
| `sma` | yes | yes | native window (exact) |
| `rmin`, `rmax`, `shift` | yes | yes | native window (exact) |
| `ema` | yes (Wilder-style seed) | yes | talib `t_ema` (SMA-seeded) |
| `rsi` | yes (Wilder smoothing) | yes | talib `t_rsi` (Wilder, SMA-seeded) |
| `atr` | yes (Wilder smoothing) | yes | talib `t_atr` (Wilder, SMA-seeded) |
| `adr` | yes | yes | native two-step window (exact) |
| `roc` | yes | yes | talib `t_roc` |
| `natr` | yes | yes | talib `t_natr` |
| `slope` | yes | yes | talib `t_linearreg_slope` |
| `rs_ratio`, `rs_momentum` | yes | yes | list-tier `t_ema`/`t_mom` + window-tier z (exact) |
| `macd` | — | yes | talib `t_macd` (narrowed to the MACD line) |
| `bbands_upper`, `bbands_lower` | — | yes | talib `t_bbands` (two entries; middle band is `sma`) |
| `adx` | — | yes | talib `t_adx` |
| `aroon` | — | yes | talib `t_aroon` (the up line) |
| `cdlengulfing` | — | yes | talib `t_cdlengulfing` (0/100 talib integer, match detected) |
| `ht_trendline` | — | yes | talib `t_ht_trendline` |

The polars builders for `ema`, `rsi`, and `atr` use the same recursion
as TA-Lib; only the seed differs. TA-Lib seeds EMA/RSI/ATR with an SMA
of the first `n` values; polars `ewm_mean(adjust=False)` seeds from
the first value. Exact match is not expr-expressible, so the contract
is: values **converge after ~4×n bars** (the benchmark measures full
agreement within 0.01 by ~7.6n). Early-window divergence is expected;
the warm-up rows are excluded from scan hits by the count-guard or by
polars' own null propagation, so the convergence contract is enough
for mature-bar scans.

## Built-in

| Name | Args | Required cols | Window |
| --- | --- | --- | --- |
| `sma` | `(expr, n)` | — | rolling mean over partition |
| `ema` | `(expr, n)` | — | ewm mean (adjust=False) over partition |
| `rsi` | `(expr, n)` | — | Wilder-smoothed gain/loss ratio over partition |
| `atr` | `(n,)` | `high, low, close` | Wilder-smoothed true range over partition |
| `adr` | `(expr, n)` | `high, low, close` | sma(TR/close·100) over partition |
| `roc` | `(expr, n)` | — | `(expr / shift(expr, n) − 1) · 100` over partition |
| `natr` | `(expr, n)` | `high, low, close` | `atr / close · 100` over partition |
| `slope` | `(expr, n)` | — | rolling OLS slope of `expr` vs window position |
| `rmin` | `(expr, n)` | — | rolling min over partition |
| `rmax` | `(expr, n)` | — | rolling max over partition |
| `shift` | `(expr, n)` | — | shift over partition |
| `rs_ratio` | `(expr, n)` | — | trailing z of EMA(5) of `expr`, re-centered at 100 |
| `rs_momentum` | `(expr, n)` | — | trailing z of EMA(3) of the 4-bar ROC of `expr`, re-centered at 100 |

### `sma(expr, n)`

```python
{"fn": "sma", "args": [{"col": "close"}, 20]}
```

`e.rolling_mean(n).over(partition)`. First `n-1` bars per partition are
null; the filter drops them.

### `ema(expr, n)`

```python
{"fn": "ema", "args": [{"col": "close"}, 5]}
```

`e.ewm_mean(span=n, adjust=False).over(partition)`. The `adjust=False`
matches TA-Lib / TradingView defaults. TA-Lib seeds with an SMA of the
first `n` values; polars seeds from the first value — the recursions
match, so values converge after ~4n bars. Early-window rows diverge by
design (the accepted warm-up contract; see the alignment note above).

### `rsi(expr, n)`

```python
{"fn": "rsi", "args": [{"col": "close"}, 14]}
```

Standard RSI on the delta of `expr`: Wilder-smoothed gain / (gain +
loss), scaled to 0-100, with nulls filled to 50.0. Same warm-up
behavior as `ema`: SMA-seeded vs first-value-seeded, values converge
after ~4n. TA-Lib RSI agrees to <0.5 points everywhere measured at
mature bars.

### `atr(n)`

```python
{"fn": "atr", "args": [14]}
```

True range over `high, low, close`: `max(high-low, |high-pc|, |low-pc|)`
where `pc` is the previous close, then Wilder-smoothed over the
partition. Requires `high, low, close` in the catalog. Same warm-up
contract as `ema` / `rsi`.

### `adr(expr, n)`

```python
{"fn": "adr", "args": [{"col": "close"}, 14]}
```

Average daily range: `sma(TR / close · 100, n)`. The leading expr is
accepted for grammar uniformity; the measure is always TR over `close`
(TA-Lib's `NATR` normalizes the same way). Bar 0 has no previous
close, so its TR is `high − low` (null `pc` propagates); the
count-guard skips the first `n-1` rows, and by row `n-1` bar 0 has
left every window, so the null never reaches a live value.

### `roc(expr, n)`

```python
{"fn": "roc", "args": [{"col": "close"}, 60]}
```

`(expr / shift(expr, n) − 1) · 100`. Bar `n` of every partition is
null (no prior `n`-bar close); the count-guard drops it.

### `natr(expr, n)`

```python
{"fn": "natr", "args": [{"col": "close"}, 14]}
```

Normalized ATR: `atr(n) / close · 100`. Same warm-up as `atr`; close
appears in the formula so `high, low, close` are required.

### `slope(expr, n)`

```python
{"fn": "slope", "args": [{"col": "close"}, 10]}
```

OLS slope of `expr` over a window of `n` bars (window position 0..n-1
on x, value on y). Closed-form via rolling sums (polars has no
`rolling_corr`); flat windows pin 0.

### `rmin(expr, n)` / `rmax(expr, n)`

```python
{"fn": "rmin", "args": [{"col": "close"}, 20]}
{"fn": "rmax", "args": [{"col": "close"}, 252]}
```

Rolling min / max over the partition.

### `shift(expr, n)`

```python
{"fn": "shift", "args": [{"col": "close"}, 1]}
```

`e.shift(n).over(partition)`. Used internally by the text DSL for the
postfix `[n]` syntax (`close[1]`) and the column-call syntax
(`close(1)`).

### `rs_ratio(expr, n)` / `rs_momentum(expr, n)`

```python
{"fn": "rs_ratio", "args": [{"col": "rs"}, 26]}
{"fn": "rs_momentum", "args": [{"fn": "rs_ratio", "args": [{"col": "rs"}, 26]}, 13]}
```

Temporal z-score normalization for RS ratings (IBD-style presentation).
RS ratings are already cross-sectional percentiles, so each series is
z-scored against its OWN trailing history: a trailing population z over
`n` smoothed values, re-centered at 100 (scale 5), clamped to
[80, 120]. `rs_ratio` smooths with EMA(5); `rs_momentum` takes the
4-bar ROC of the *normalized* ratio (feed it `rs_ratio` output, as
above) and smooths with EMA(3). Warm-up: null until the first `n`
smoothed values exist; a zero-variance window pins exactly 100.0. The
smoothing chain (span 5 / 3) aligns with TA-Lib's SMA-seeded `t_ema`,
so both engines agree exactly at mature bars (verified cross-engine in
`tests/test_rs_indicators.py`).

## Extending

See [Extend INDICATORS](../how-to/extend-indicators.md) for the full
extension recipe, including arg-type rules, `.over(partition)`,
`required_cols`, and idempotent registration.

To add an indicator that only the duckdb backend can execute, insert
into [`SQL_INDICATORS`](../reference/duckdb-backend.md) instead — same
`(arg_spec, builder, required_cols)` shape, but the builder emits a
SQL fragment. `validate(scan_def, engine="duckdb")` accepts the new
name; the polars engine rejects it.

## Validation errors

| Error string | Means |
| --- | --- |
| `"unknown indicator: '<name>'"` | name not in the active registry |
| `"'<name>' takes <n> args, got <m>"` | arg count mismatch |
| `"args[i]: must be an int >= 1, got <v>"` | window slot got a non-int (or bool, or zero) |
| `"indicator '<name>' requires column '<col>'"` | required_col missing from catalog |
| `"indicator '<name>' requires engine='duckdb'"` | name exists only in `SQL_INDICATORS`; default `engine="polars"` rejected it; pass `engine="duckdb"` |

## Where to next

- [Extend INDICATORS](../how-to/extend-indicators.md) — the registry
  extension recipe
- [duckdb backend reference](../reference/duckdb-backend.md) — the
  `SQL_INDICATORS` shape and the two-tier lowering
- [IR design](../explanation/ir-design.md) — the registry contract as
  part of the IR freeze