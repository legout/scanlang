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
| talib `t_*` scalar | `ema`, `rsi`, `atr`, `roc`, `natr`, `slope`, plus the duckdb-only names | Per-partition list CTE, `t_fn` over the lists, `unnest` back to row-aligned output |

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
| `"duckdb"` | Every name in `SQL_INDICATORS` (a strict superset of `INDICATORS`); accepts the talib-only names |

The polars engine never executes the talib-only names — `compile`
raises the same `ValueError` `validate` would. The duckdb engine
executes only what `SQL_INDICATORS` knows about. `compile`, `apply`, and
`apply_sql` all accept the same `engine=` kwarg for consistency; the
`engine` does not change which plan they emit, only which names
validate.

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

`SQL_INDICATORS` is a strict superset of `INDICATORS`: the entries
shared between the two have identical `arg_spec` and `required_cols`.
The duckdb-only entries (`macd`, `bbands_upper`, `bbands_lower`, `adx`,
`aroon`, `cdlengulfing`, `ht_trendline`) exist only in
`SQL_INDICATORS`; they have no polars-builder equivalent.

Multi-output `t_*` functions (`macd`, `bbands`, `aroon`) are narrowed
to one series at the SQL level:

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