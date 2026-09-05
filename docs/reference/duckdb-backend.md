# duckdb backend

The `scanlang.duckdb_sql` module is a second backend that compiles the
same scan-def IR into parameterized duckdb SQL and runs it on a
connection. It exists alongside the polars backend (the default);
nothing in the existing `compile` / `apply` path changes. The IR is the
contract; both backends consume it.

## Module surface

::: scanlang.duckdb_sql
    options:
      show_root_toc_entry: true
      members: false

::: scanlang.duckdb_sql.compile_sql
    options:
      show_root_heading: true
      heading_level: 3

::: scanlang.duckdb_sql.apply_sql
    options:
      show_root_heading: true
      heading_level: 3

::: scanlang.duckdb_sql.SQL_INDICATORS
    options:
      show_root_heading: true
      heading_level: 3

## Two-tier lowering

The SQL backend splits indicator lowering into two tiers, selected per
indicator name. The split keeps the cheap indicators fast (native
window functions, no extension needed) and routes the rest through the
talib extension.

| Tier | Indicators | Form |
| --- | --- | --- |
| Native window | `sma`, `rmin`, `rmax`, `shift`, `adr` | `AVG` / `MIN` / `MAX` / `LAG OVER (PARTITION BY ... ORDER BY ... ROWS BETWEEN ...)` with a `count`-guard so warm-up rows are NULL exactly like polars `rolling_*` |
| talib `t_*` scalar | `ema`, `rsi`, `atr`, `roc`, `natr`, `slope`, `macd`, `bbands_upper`, `bbands_lower`, `adx`, `adxr`, `aroon`, `kama`, `ht_trendline`, `stoch_k`, `stoch_d`, `ht_dcperiod`, `ht_dcphase`, `wma`, `dema`, `tema`, `trima`, `mom`, `midprice`, `midpoint`, `cci`, `willr`, `trange`, `ad`, `cmo`, `trix` | Per-partition list CTE, `t_fn` over the lists, `unnest` back to row-aligned output. Multi-output `t_*` functions (`macd`, `bbands`, `aroon`, `stoch`) are struct-narrowed to one field per scanlang name (see [Multi-output field names](indicators.md#multi-output-field-names) in the indicators reference). The community extension exposes no `t_*` for the polars-only wave-2 names (`stochrsi`, `apo`, `ppo`, `mfi`, `adosc`, `ultosc`, `obv`, `t3`, `sar`, `accbands_*`), so those names run polars-side only. |

`adr` is a two-step window (true range needs `lag(close)`, and window
functions cannot nest). The builder stages TR as its own CTE before
the count-guarded average; values agree with polars `rolling_mean` to
the bit on complete frames.

The talib extension's `ta_*` window-aggregate form was benchmarked and
rejected: it runs 30-35× slower than `t_*` scalar on the full lake
universe (125 s vs 3.8 s). It is not used by `scanlang`.

## `relation` identifier rule

`relation` names the table or view to scan; the SQL backend never
interpolates it, but it does need a parseable SQL identifier so the
string lands in the generated text after a `FROM`:

```
[A-Za-z_][A-Za-z0-9_]*
```

Path or URL strings are rejected at `compile_sql` time. Register a view
or table first:

```python
con.execute("CREATE VIEW bars AS SELECT * FROM 'daily_bars.parquet'")
sql, params = compile_sql(scan_def, relation="bars")
```

For a hotlake-style attach, register once and reuse the connection:

```python
con.execute("ATTACH 'lake.duckdb' AS lake (READ_ONLY)")
con.execute("USE lake")
con.execute("CREATE VIEW bars AS SELECT * FROM daily_bars")
hits = apply_sql(con, scan_def, relation="bars")
```

## `params` contract

`compile_sql` returns `(sql, params)` — run with
`con.execute(sql, params)`. Every literal the scan touches binds as a
`?` parameter:

- All literal leaves (`>=`, `<`, `between`, `in`, `contains`)
- All window frame bounds (`ROWS BETWEEN ? PRECEDING …`)
- All `lag(?, 1)` offsets
- All `t_*` lookback periods
- `LIMIT ?` on the tail

Identifier names (catalog properties, `partition`, `order_column`) are
double-quoted in the generated text. No user-controlled string is
string-interpolated into the SQL — same contract as `compile()`.

`?` inside an `IN` list or a `BETWEEN` range is `CAST`-ed to the
catalog dtype, because duckdb cannot infer `?` types in those contexts.
Bare `?` elsewhere is wrapped in `CAST(? AS INTEGER)` for the `t_*`
lookback, because the same bare-`?`-is-typed-as-DATE gotcha hits window
frames.

## Engine-aware `validate()` semantics

`validate(scan_def, *, engine=...)` accepts an additive `engine`
keyword:

| Engine | Indicator names it accepts |
| --- | --- |
| `"polars"` (default) | Every name in `INDICATORS`; rejects talib-only names with `indicator '<name>' requires engine='duckdb'` |
| `"duckdb"` | Every name in `SQL_INDICATORS` (the shared names plus the duckdb-only `ht_trendline`, `stoch_k`, `stoch_d`); accepts the talib-only names. The eleven polars-only wave-2 names are rejected here — no SQL lowering exists. |

The polars engine never executes the duckdb-only names (`ht_trendline`,
`stoch_k`, `stoch_d`) — they have no entry in `INDICATORS`. `compile` and
`apply` still emit polars plans only: when `engine="duckdb"` widens
name validation to include a duckdb-only name, the `INDICATORS`
lowering path has no builder for it and raises `KeyError: '<name>'`
at `compiler._operand` (`INDICATORS[spec["fn"]]` lookup) — no
`ValueError`, no plan. The duckdb-only names must therefore route
through `compile_sql` / `apply_sql` — the SQL backend is the only path
that can plan and execute them. The formerly duckdb-only names (`macd`,
`bbands_*`, `adx`, `aroon`, `kama`, `cdlengulfing`, plus the curated
`_CDL_PARITY` candlestick set) now have `INDICATORS` parity builders
(the talib extra's eager map_groups seam) and run on both engines — as
do the wave-2 names with a community-extension `t_*` (`adxr`, `cmo`,
`trix`, `midpoint`, `ht_dcperiod`, `ht_dcphase`).
`compile`, `apply`, and `apply_sql`
all accept the same `engine=` kwarg for consistency; the engine only
widens the name allowlist, it does not retarget polars to emit SQL.

## `SQL_INDICATORS` registry shape

Same `(arg_spec, builder, required_cols)` triple as `INDICATORS`, but
builders emit SQL fragments instead of `pl.Expr`. The builder signature
is:

```python
builder(x, n, partition, order_column, params) -> str
```

`x` is the compiled operand SQL (`None` for indicators that take no
expression arg, like `atr`, `macd`, `adx`). `n` is the lookback period
(`None` for fixed-lookback patterns like `cdlengulfing`). Builders
append their own `?` params in string-occurrence order; `params` is
positional and must stay in lockstep with the assembled SQL.

`SQL_INDICATORS` shares the `(arg_spec, builder,
required_cols)` triple with `INDICATORS`, but
builders emit SQL fragments instead of `pl.Expr`. The registries
overlap on the shared names — which have identical `arg_spec` and
`required_cols` — but neither contains the other: the duckdb-only
entries (`ht_trendline`,
`stoch_k`, `stoch_d`) exist only in
`SQL_INDICATORS`; they have no polars-builder equivalent. Eleven
wave-2 names (`ultosc`, `obv`, `mfi`, `adosc`, `stochrsi`, `apo`,
`ppo`, `t3`, `sar`, `accbands_upper`, `accbands_lower`) are
polars-only (the seam names
`macd`, `bbands_upper`, `bbands_lower`, `aroon`, `adx`, `kama`,
`cdlengulfing`, and the `_CDL_PARITY` candlestick set
are shared).

Multi-output `t_*` functions (`macd`, `bbands`, `aroon`, `stoch`) are
narrowed to one series at the SQL level — the same field the polars
seam builders select:

- `macd` -> the MACD line (`fast EMA - slow EMA`); signal and
  histogram are derived from it
- `bbands_upper`, `bbands_lower` -> two registry entries; the middle
  band is just `sma`
- `aroon` -> `aroon_up`; `aroon_down` is its mirror for short setups
  (add a separate entry if ever needed)

## Relation between `compile` and `compile_sql`

Both functions walk the IR dict and emit a single predicate. The
output shapes differ:

| Function | Output | Use |
| --- | --- | --- |
| `compile(scan_def, *, engine="polars")` | `pl.Expr` | fold into a `LazyFrame` plan with `apply` |
| `compile_sql(scan_def, *, relation, engine="duckdb")` | `(sql, params)` | run on a duckdb connection via `apply_sql` |

The polars path stays lazy end-to-end; the duckdb path is eager (duckdb
has no polars-lazy plan), and `apply_sql` always returns a
`pl.DataFrame`. Same scan-def, two execution shapes — choose per data
edge.

## Where to next

- [how-to: duckdb backend](../how-to/duckdb-backend.md) — install,
  connect, run a scan both engines
- [Indicators reference](indicators.md) — polars-side indicator table
- [API reference](api.md) — `scanlang.compiler.compile` /
  `apply` / `validate` with the `engine=` kwarg