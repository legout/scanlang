# Indicators

Built-in indicators live in [`INDICATORS`](../reference/api.md) and can
be extended by insertion. Each entry is `(arg_spec, builder, required_cols)`:

- `arg_spec` — tuple with one tag per positional arg — `"expr"` (any
  operand: column ref, nested indicator call, arithmetic) or `"int"`
  (literal int >= 1)
- `builder(*parsed, partition) -> pl.Expr` — your polars expression,
  window ops via `.over(partition)`. **Exception:** the
  [TA-Lib parity seam](#ta-lib-parity-seam-multi-output-narrowing) builders
  (adx, kama, macd, bbands_*, aroon, cdlengulfing, the curated candlestick
  set) return a `DataFrame -> DataFrame` callable for
  `group_by(partition, maintain_order=True).map_groups(...)` — eager
  collect required, the `talib` extra required, exact TA-Lib values.
- `required_cols` — column names the catalog must have (e.g. `atr`
  needs `high, low, close`)

## Engine availability

Indicators run on one or both backends. The polars backend is the
default and supports everything in `INDICATORS`. The duckdb backend
adds the rest through [`SQL_INDICATORS`](../reference/duckdb-backend.md)
— a strict superset of `INDICATORS`.

The full table is **generated** from the live registries (single
source of truth, no hand-maintained counts that can drift). Regenerate
after any registry change with:

```sh
uv run python scripts/gen_indicator_availability.py
```

The generator writes:

- `docs/reference/_indicator_availability.md` — the dual-engine subset
  shown below (the names present in both registries)
- `docs/reference/_indicator_availability_full.md` — every name,
  including the three duckdb-only entries (`ht_trendline`,
  `stoch_k`, `stoch_d`)

--8<-- "reference/_indicator_availability.md"

The three duckdb-only names (`ht_trendline`, `stoch_k`, `stoch_d`) live
in the full table only — they have no polars builder (the community
talib extension is the only implementation). The curated candlestick
intersection (26 names: `cdlengulfing` + 25 further patterns in
`_CDL_PARITY`) shares one signature on both engines
(`("int",)`, `("open", "high", "low", "close")`).

### Tier legend

| Tier | Form | Notes |
| --- | --- | --- |
| `native window` | `AVG` / `MIN` / `MAX` / `LAG OVER (... ROWS BETWEEN ? PRECEDING AND CURRENT ROW)` with a `count`-guard | Exact on both engines (the count-guard aligns the warm-up nulls with polars `rolling_*`); the sma-family hit sets are identical for complete frames. |
| `t_*` | Per-partition list CTE, `t_fn(list(col, ...), n)`, `unnest` back to row-aligned output | The duckdb-side lowering form. The polars side mirrors it either via the eager seam (where the same function is exact TA-Lib) or a polars-native rolling/ewm equivalent. |
| `two-step window` | TR materialization CTE + count-guarded average (used by `adr`) | TR needs `lag(close)` and window functions cannot nest, so TR is staged as its own CTE before the average. Exact on both engines. |
| `list CTE (struct-narrowed)` | Per-partition list CTE, multi-output `t_*` returns `LIST(STRUCT)`, the builder narrows to one field | Multi-output talib functions (`macd`, `bbands`, `aroon`, `stoch`); the SQL builder picks one struct field, the polars seam builder picks one numpy slot. |
| `list CTE (two-stage)` | List-tier smoothing CTE + window-tier z-score CTE | Used by `rs_ratio` / `rs_momentum` (temporal z-score normalization); the z stage needs `STDDEV_POP` over a window which cannot nest in a `t_*` list call. |

## Multi-output field names

Multi-output talib functions narrow to **one scalar per scanlang name**
— never a struct through the IR. The approved catalog (no struct
through the IR, one field per name):

| scanlang name | talib function | Field exposed | Field(s) unexposed |
| --- | --- | --- | --- |
| `macd` | `MACD(close, fast, 26, 9)` | `macd` (the MACD line) | `macd_signal`, `macd_hist` (the histogram is `macd − signal`; both derivable) |
| `bbands_upper` | `BBANDS(close, n, 2.0, 2.0, 0)` | `upperband` | `middleband` (= `sma(close, n)`), `lowerband` |
| `bbands_lower` | `BBANDS(close, n, 2.0, 2.0, 0)` | `lowerband` | `middleband`, `upperband` |
| `aroon` | `AROON(high, low, n)` | `aroon_up` | `aroon_down` (its mirror for short setups; add a separate entry if ever needed) |
| `stoch_k` | `STOCH(high, low, close, fastk, slowk, 0, slowd, 0)` | `slowk` | `slowd` (its own scanlang name), `fastk` |
| `stoch_d` | `STOCH(high, low, close, fastk, slowk, 0, slowd, 0)` | `slowd` | `slowk`, `fastk` |

The polars engine stages the eager seam names (`adx`, `kama`, `macd`,
`bbands_upper`, `bbands_lower`, `aroon`, `cdlengulfing`, the curated
candlestick set) via `group_by(partition, maintain_order=True).map_groups(...)`
and pre-materializes the result as `__<name>_0` in the predicate. The
column is always named with the `__<name>_0` prefix so a user column
literally named `adx` (or any other scanlang name) cannot be clobbered.

## Warm-up / null behavior

Indicator warm-up is the bar count before a function emits its first
real value per partition. The contract differs by family:

| Family | Warm-up | Why |
| --- | --- | --- |
| `sma` / `rmin` / `rmax` / `adr` | `n − 1` rows null (count-guard on both engines) | Native window; first `n-1` bars are below the window size. Identical on both engines — sma-family scans have **identical** hit sets. |
| `ema` / `rsi` / `atr` / `natr` / `dema` / `tema` / `trima` | polars: `~4n` (convergence) / duckdb: `n` (SMA-seeded) | TA-Lib seeds with an SMA of the first `n` values; polars `ewm_mean(adjust=False)` seeds from the first value. The recursions match, so values **converge** after ~4n bars (the benchmark measures full agreement within 0.01 by ~7.6n). Early-window divergence is by design. Mature-bar scans see consistent values across engines. |
| `kama` | `n` rows null | Eager seam runs `talib.KAMA` (exact); null for the first `n` bars. |
| `roc` / `mom` | `n` rows null | Bar `n` has no `n`-bar prior reference. |
| `wma` | `n − 1` | Rolling sum needs `n` rows. |
| `macd` | 33 rows (fast=12, slow=26, signal=9) | Combined lookback of slow + signal periods. |
| `adx` | `2n − 1` rows | ADX itself needs `2n-1` for the smoothed DX. |
| `ht_trendline` | 63 rows (default) | Dominant cycle, TA-Lib documented. |
| `cci` / `willr` | `n − 1` rows | Rolling MAD / rolling max-min over `n`. |
| `trange` | 1 row (bar 0) | Previous close at bar 0 is null; the `_tr` polars builder and the `_adr_tr` SQL builder both pin bar 0 to null identically. |
| `slope` | `n − 1` rows | Rolling sums need `n` rows. |
| `midprice` | `n − 1` | Same windowing. |
| `trima` | `n − 1` | Two-stage SMA, both engines identical. |
| `stoch_k` / `stoch_d` | `fastk − 1` | The `t_stoch` warm-up (the lookback is `fastk`). |
| `aroon` | `n` rows | `AROON` looks back `n` bars; the seam's NaN is normalized to null. |
| `bbands_upper` / `bbands_lower` | `n − 1` rows | SMA of length `n` inside BBANDS. |
| `ad` | 0 (cumulative) | No warm-up; cumulative from bar 0 (zero-range bars pin 0.0 to avoid NaN propagation). |
| Candlestick set (`_CDL_PARITY`) | 0 rows (patterns are 2-bar, no warm-up) | Patterns read bar i and bar i−1; the polars seam normalizes NaN to null (it never fires here), the SQL `t_cdl*` warm-up is the leading null. `>= -200` predicate drops both identically. |

**Cross-engine scan equality is therefore only claimed for sma-family
scans on complete frames** (and for the curated candlestick set, where
both engines hit the identical `(symbol, session)` set on every mature
bar — verified in `tests/test_cdl_patterns.py`). For ema/rsi/atr-class
scans, values agree to <0.01 at mature bars; hit sets can still differ
in the warm-up window.

**`flat`-series guards** (preserved on both engines, pinned by
`tests/test_indicators_c3.py`):

- `rsi`: zero-loss (flat) row returns 100, not NaN (NaN would pass a
  polars `>` filter and falsely fire a "RSI > 85" scan).
- `cci`: zero-MAD window returns 0, not NaN (same NaN-passes-filter
  trap).
- `ad`: zero-range (high==low) bar contributes 0.0 to the cumsum (same
  trap; TA-Lib does the same).

## TA-Lib parity seam — multi-output narrowing

The polars engine executes the talib parity names via the same seam
(`group_by(partition, maintain_order=True).map_groups(...)`):

- Eager-only by contract (map_groups has no lazy form), and requires
  the `talib` extra (`uv add 'scanlang[talib]'`).
- Values are **exact TA-Lib**, bar-for-bar. NaN warm-up is normalized
  to `None` so the filter drops it identically to the duckdb engine's
  SQL `t_*` warm-up.
- The same scanlang name covers **both** engines: `INDICATORS[name]`
  for polars, `SQL_INDICATORS[name]` for duckdb. Same `arg_spec`,
  same `required_cols` (asserted by
  `tests/test_duckdb_sql.py::test_sql_registry_superset_of_indicators`).

The seam is used by: `adx`, `kama`, `macd`, `bbands_upper`,
`bbands_lower`, `aroon`, `cdlengulfing`, and the curated candlestick
set (`_CDL_PARITY`, 25 further patterns). The two-tier
(roon-bband) lowerings and the multi-output struct-narrowing are
covered in the [duckdb backend reference](../reference/duckdb-backend.md#two-tier-lowering)
(the SQL side). `stoch_k` / `stoch_d` are **SQL-only by design** (no
polars parity builder — the community talib extension is the
implementation); `ht_trendline` is also SQL-only.

### Empty / talib-less interpreter

`apply()` reports the install hint when the seam name is referenced
but the `talib` extra is missing (e.g. `requires the optional 'talib'
extra`). The registry still validates the scan (`validate()` returns
`[]`) because the entry shape is static and the seam builder imports
`talib` only when its n is bound. See
`tests/test_talib_missing.py` for the contract.

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

The full reference for the parity set (`wma`, `dema`, `tema`, `trima`,
`mom`, `midprice`, `cci`, `willr`, `trange`, `ad`, the multi-output
`macd` / `bbands_upper` / `bbands_lower` / `aroon`, the seam names
`adx` / `kama`, and the curated candlestick set) lives in the
[availability table](#engine-availability) above. Their semantics are
the TA-Lib reference (values are exact cross-engine — see the seam
section).

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
design (the accepted warm-up contract; see [Warm-up / null
behavior](#warm-up-null-behavior) above).

### `rsi(expr, n)`

```python
{"fn": "rsi", "args": [{"col": "close"}, 14]}
```

Standard RSI on the delta of `expr`: Wilder-smoothed gain / (gain +
loss), scaled to 0-100, with nulls filled to 50.0. Same warm-up
behavior as `ema`: SMA-seeded vs first-value-seeded, values converge
after ~4n. TA-Lib RSI agrees to <0.5 points everywhere measured at
mature bars. Flat (zero-loss) row pins 100, not NaN — see the
[warm-up section](#warm-up-null-behavior) above.

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

## Installation extras

scanlang ships extras that gate the two engines' indicator coverage:

| Extra | Pulls | Indicator coverage |
| --- | --- | --- |
| (default) | polars >= 1.44 | The built-in (`sma`, `ema`, `rsi`, `atr`, etc.), `wma`/`dema`/`tema`/`trima`/`mom`/`midprice`/`cci`/`willr`/`trange`/`ad`, and the temporal `rs_*` — all **polars-native** builders. |
| `duckdb` | `duckdb >= 1.5` | The duckdb backend and its full `SQL_INDICATORS` registry (superset of `INDICATORS`). The community talib extension is loaded by `apply_sql` itself; no Python install. |
| `talib` | `ta-lib >= 0.7.1` | The eager parity seam for `adx`, `kama`, `macd`, `bbands_*`, `aroon`, `cdlengulfing`, and the curated candlestick set. Required for those names on the polars engine. |

Install combinations:

```sh
uv add scanlang                      # polars-native only
uv add 'scanlang[duckdb]'            # + duckdb backend
uv add 'scanlang[talib]'             # + talib parity seam (polars)
uv add 'scanlang[duckdb,talib]'      # both (full coverage)
```

On a talib-less interpreter, every registry entry is still
**importable and validatable** (the seam builders import talib only
when their `n` is bound); `apply()` reports the install hint with a
`requires the optional 'talib' extra` error when a seam name is
referenced. See `tests/test_talib_missing.py`.

## Extending

See [Extend INDICATORS](../how-to/extend-indicators.md) for the full
extension recipe, including arg-type rules, `.over(partition)`,
`required_cols`, and idempotent registration.

For a **polars-only** indicator, insert into `INDICATORS`:

```python
from scanlang import INDICATORS

def _stdev(e, n, partition):
    return e.rolling_std(n).over(partition)

if "stdev" not in INDICATORS:                 # idempotent
    INDICATORS["stdev"] = (("expr", "int"), _stdev, ())
```

For a **duckdb-only** indicator (talib `t_*` extension-only function),
insert into `SQL_INDICATORS` instead — same `(arg_spec, builder,
required_cols)` shape, but the builder emits a SQL fragment. The
duckdb engine validates it; the polars engine rejects it with
`indicator '<name>' requires engine='duckdb'`. To also enable it on
the polars engine, add an entry in `INDICATORS` with the **same
`arg_spec` and `required_cols`** (the seam contract), and have the
polars builder return a `DataFrame -> DataFrame` callable (the eager
parity seam pattern). See
[`scanlang/indicators.py:457`](https://github.com/legout/scanlang/blob/master/src/scanlang/indicators.py)
for the `if "<name>" not in INDICATORS:` insertion pattern used by
`adx` / `kama` / `macd` / `bbands_*` / `aroon` / the candlestick set.

The full contract is part of the [IR freeze](../explanation/ir-design.md#indicators-and-engines)
— the entry shape is the public extension point.

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
- [`scripts/gen_indicator_availability.py`](https://github.com/legout/scanlang/blob/master/scripts/gen_indicator_availability.py)
  — regenerates the availability table from the live registries
