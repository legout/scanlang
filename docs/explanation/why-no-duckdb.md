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

## When would we revisit

If a consumer shows up that has the data in duckdb and a polars
backend is genuinely expensive — usually because they want to push a
scan into an existing duckdb instance that's aggregating across many
parquet directories, or they want SQL output to feed into another SQL
pipeline — we'd look at it again. The IR was designed additive; a
duckdb backend can be a separate package that consumes the same dict.

For now: one backend, one IR, one set of semantics.

## Where to next

- [Lazy contract](lazy-contract.md) — what `compile` returns and where
  it runs
- [IR design](ir-design.md) — the additive-only contract
