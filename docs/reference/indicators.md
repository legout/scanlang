# Indicators

Built-in indicators live in [`INDICATORS`](../reference/api.md) and can
be extended by insertion. Each entry is `(arg_spec, builder, required_cols)`:

- `arg_spec` — tuple of `"expr"` (any operand) or `"int"` (literal int >= 1)
- `builder(*parsed, partition) -> pl.Expr` — your polars expression,
  window ops via `.over(partition)`
- `required_cols` — column names the catalog must have

## Built-in

| Name | Args | Required cols | Window |
| --- | --- | --- | --- |
| `sma` | `(expr, n)` | — | rolling mean over partition |
| `ema` | `(expr, n)` | — | ewm mean (adjust=False) over partition |
| `rsi` | `(expr, n)` | — | rolling gain/loss ratio, `fill_null(50)` |
| `atr` | `(n,)` | `high, low, close` | rolling TR mean over partition |
| `rmin` | `(expr, n)` | — | rolling min over partition |
| `rmax` | `(expr, n)` | — | rolling max over partition |
| `shift` | `(expr, n)` | — | shift over partition |

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
matches TA-Lib / TradingView defaults; values from bar 1 are non-null
once `ewm` has warmed up.

### `rsi(expr, n)`

```python
{"fn": "rsi", "args": [{"col": "close"}, 14]}
```

Standard RSI on the delta of `expr`: rolling mean of gains / (rolling
mean of gains + rolling mean of losses), scaled to 0-100, with nulls
filled to 50.0. Same window / null behavior as TA-Lib's RSI on default
params.

### `atr(n)`

```python
{"fn": "atr", "args": [14]}
```

True range over `high, low, close`: `max(high-low, |high-pc|, |low-pc|)`
where `pc` is the previous close, then `rolling_mean(n).over(partition)`.
Requires `high, low, close` in the catalog.

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

## Extending

See [Extend INDICATORS](../how-to/extend-indicators.md) for the full
extension recipe, including arg-type rules, `.over(partition)`,
`required_cols`, and idempotent registration.

## Validation errors

| Error string | Means |
| --- | --- |
| `"unknown indicator: '<name>'"` | name not in `INDICATORS` |
| `"'<name>' takes <n> args, got <m>"` | arg count mismatch |
| `"args[i]: must be an int >= 1, got <v>"` | window slot got a non-int (or bool, or zero) |
| `"indicator '<name>' requires column '<col>'"` | required_col missing from catalog |
