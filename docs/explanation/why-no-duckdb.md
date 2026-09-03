# Why no duckdb

`scanlang` compiles scan definitions to a single polars predicate
expression and runs filters in polars. It does NOT also compile to
duckdb SQL, even though duckdb is a natural fit for the use case
(lazy pushdown over a columnar store). This page is the why.

## The question

`scanlang` runs against OHLCV bars stored in polars / parquet /
arrow. duckdb is also happy against all three. Both are columnar, both
support predicate pushdown, both can lazy-scan a directory of
parquet files. Could we compile the same scan to both and pick a
backend at runtime?

Research task, closed 2026-08-30: full notes at
[Research: duckdb reference](../reference/research-duckdb.md); the
historical repository record is at
[`docs/RESEARCH_DUCKDB.md`](https://github.com/legout/scanlang/blob/master/docs/RESEARCH_DUCKDB.md).

## The short version

duckdb SQL translation would buy us:

- SQL backend against a database / data warehouse

…and cost us:

- A second IR-to-frontend compiler with its own bugs and dtypes
- A runtime backend-selection policy ("when is duckdb better?")
- Divergent semantics between the two backends
- A user mental model that requires understanding two execution modes

For the actual consumer (marketdata-screens Lab UI, REPL, notebooks
on polars `LazyFrame`s), duckdb adds nothing — they're already on
polars. The "SQL backend against a warehouse" use case isn't on the
near-term roadmap.

## The long version

### duckdb gives us SQL

If users want SQL, they can write SQL. `scanlang`'s job is to be a
better DSL, not a better SQL generator. The DSL is a hand-held way to
write "the screen you'd write in pandas if you were willing to write
the screen in pandas" — they don't want SQL, they want a filter.

### duckdb requires a second frontend

`compile` returns `pl.Expr`. To get duckdb SQL, we'd need to walk the
same IR and emit `WHERE ...`. Both walks are correctness-critical;
every indicator / operator / cross op needs to be in both. The
[validation split](validation-split.md) gets harder — `validate` would
have to know whether a definition is portable (yes), portable with a
hint (slow), or polars-only (`talib` extension).

### polars pushdown is enough

The original concern with "just polars" was that scans against parquet
wouldn't push down. That's wrong: polars pushdown works for filters,
selects, slice, projection. The compiled `pl.Expr` becomes a node in
the `LazyFrame` plan; `sink_parquet` / `collect` execute it as one
optimized query against the underlying source.

### cross_above / indicator semantics are polars-shaped

The compiled predicates use polars-native expressions — `.over(partition)`
for windows, `rolling_mean`, `ewm_mean`. duckdb doesn't have all of
those natively; window functions in duckdb are SQL-shaped (`OVER
(PARTITION BY ... ORDER BY ... ROWS BETWEEN ... PRECEDING ...)`).
Maintaining semantic equivalence (especially for RSI's null filling,
EMA's `adjust=False`, ATR's TR formula) is a research project of its
own.

### Consumer drivers

The marketdata-screens Lab UI runs in a polars `LazyFrame` world
(hotlake polars, parquet scans, `.over("symbol")` everywhere). A duckdb
backend would be on a different code path with no callsite.

## Revisited 2026-09

The verdict above held for almost a year. The release of the community
[talib extension for duckdb](https://github.com/duckdb/extension_talib)
changed the math:

- The hand-rolled CTE cost the old verdict warned about (ema, rsi, atr)
  no longer applies. The extension exposes `t_*` scalar functions that
  take a list and return a list, in one O(N) pass per symbol.
- The benchmark report
  ([`research/talib_benchmark_2026-09-02.md`](https://github.com/legout/marketdata-screens/blob/master/research/talib_benchmark_2026-09-02.md),
  marketdata-screens repo) measured the indicator creation cost on the
  full 25.7M-row lake universe. The `t_*` scalar form is the fastest
  engine measured: **3.8 s vs 6.0 s for polars native**, vs 4.6 s for
  polars + py-talib. Cross-engine value parity is high where both
  engines emit: RSI agrees to <0.5 points everywhere measured, and the
  Minervini Trend Template scanner matched exactly (493 = 493 hits).
- `scanlang` 0.3.0 adds the `duckdb_sql` module: a second backend that
  compiles the same scan-def IR to parameterized SQL, lowers indicators
  in two tiers (native window for sma/rmin/rmax/shift, `t_*` scalar for
  the rest), and runs them through the connection's loaded talib
  extension. See [duckdb backend reference](../reference/duckdb-backend.md)
  and [how-to: duckdb backend](../how-to/duckdb-backend.md).

The `ta_*` window-aggregate form was benchmarked too and rejected for
scans: it recomputes per frame and runs 30-35× slower than the `t_*`
scalar form (125 s vs 3.8 s on the full universe). Dashboard-style
one-bar responses are its only viable use case; the SQL backend does not
use it.

The "polars has no Expr→SQL translator" argument is unchanged, but it
no longer matters — `scanlang` compiles the IR dict directly, never
polars expressions. What changed is that compiling to SQL is now
cheaper than compiling to polars on the full universe, and exact-value
indicators (ht_trendline, the candle patterns, aroon at speed) that
have no polars-builder equivalent are now first-class.

## When would we revisit (now-obsolete framing)

If a consumer shows up that has the data in duckdb and a polars
backend is genuinely expensive — usually because they want to push a
scan into an existing duckdb instance that's aggregating across many
parquet directories, or they want SQL output to feed into another SQL
pipeline — we'd look at it again. The IR was designed additive; a
duckdb backend can be a separate package that consumes the same dict.

**Status as of 2026-09-02:** the consumer was the marketdata-screens
lake, the scan-def-to-SQL backend is now in the repo, and the
`duckdb_sql` module supersedes the paragraphs above. The remainder of
the page still describes the original reasoning accurately, but the
conclusion ("no SQL backend") is the one that changed.

## Where to next

- [duckdb backend reference](../reference/duckdb-backend.md) — the
  `duckdb_sql` module that supersedes this verdict
- [how-to: duckdb backend](../how-to/duckdb-backend.md) — install and
  run a scan against the community talib extension
- [Lazy contract](lazy-contract.md) — what `compile` returns and where
  it runs
- [IR design](ir-design.md) — the additive-only contract
