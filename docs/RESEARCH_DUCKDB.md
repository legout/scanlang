# RESEARCH — DuckDB SQL translation vs filter-in-polars

> **Repository record.** This is the original research notes. The current
> user-facing copy lives at
> [`reference/research-duckdb.md`](reference/research-duckdb.md) in the rendered site.

Handoff item 4 (`docs/HANDOFF.md`). Analysis only. No production code changes.

## TL;DR (read this first)

**Recommendation: keep the polars-only compile/validate/apply path. Do NOT add a scanlang SQL backend.**

Two paths were worth measuring. Both can be made to work, but neither buys what the
scanlang workload actually needs:

| Strategy | Verdict | Why |
|---|---|---|
| Translate compiled `pl.Expr` → SQL via sqlglot | **Reject.** | polars has no public Expr→SQL translator; sqlglot has no `polars` dialect; we'd hand-roll AST→SQL, push every IR feature through the translation, and accept that `ema` / `rsi` need hand-rolled CTEs. Highest effort, smallest upside. |
| Translate **scan_def (the IR dict) directly** to SQL | **Reject for v1.** | Same translation surface as above, plus we lose the polars-lazy path that already works on every source (parquet, hotlake, arrow, eager frame, lab UI). |
| Push the cheap scalar filter into duckdb scan, do windows in polars | **Defer.** | The `score_bars` path needs windows *on the same* rows you filter, so this is only useful for a final-pass filter on a wide scan. Not the common case. |
| Stay on `compile(scan_def) → pl.Expr → apply()` | **Keep.** | Already covers every source type; no translation layer; same compile output drives the lab UI, REPL, notebooks, marimo. |

The intuition that drove this question — "duckdb scans parquet faster, so pushdown
pays" — turned out to be wrong for the *scanlang* workload on the kinds of scans we
actually run (single dataset, filter + window + filter, small result set). See §4.

## 1. What the IR actually is (frozen in `docs/IR_FREEZE.md`)

The IR is a plain dict the Lab UI already serialises. `compile()` turns it into a
single `pl.Expr` predicate. Every feature a translator would need to cover:

- **Groups:** `all` (AND), `any` (OR), `not` (unary); arbitrary nesting; flat list of
  filters at top level.
- **Leaves:** `property / op / value` where `property` is either a catalog string or
  an operand dict; `op ∈ {>=, <=, >, <, ==, !=, between, in, contains, cross_above,
  cross_below}`.
- **Operands:** bare literal | `{"col": name}` | `{"fn": name, "args": [...]}` |
  arithmetic (`+ - * /`, n-ary; unary minus via single-element list).
- **Indicators (v1):** `sma, ema, rsi, atr, rmin, rmax, shift` — every window op
  uses `.over(partition)`. `cross_above/below` lower to
  `(a > b) & (shift(a,1) <= shift(b,1))` over the partition.
- **Frame-side context:** caller passes any `LazyFrame`/`DataFrame`. The
  `partition` column is a parameter. `score_bars()` produces a frame whose columns
  match `PROPERTY_CATALOG`; users also pass raw OHLCV + a custom catalog.

The contract that constrains any translation: nothing is string-interpolated, so
there is no injection surface, but it also means **values must be parameterised,
not concatenated**, in any SQL backend we build.

## 2. Strategy options — what each looks like

### 2.1 IR dict → SQL directly (skip polars Expr entirely)

A new module, say `scanlang.duckdb_compile.compile_sql(scan_def, *, table, partition)
-> (sql, params)`. `apply` variant becomes `apply_sql(con, scan_def, *, table,
partition) -> pl.DataFrame`.

Surface area per IR feature:

| IR feature | duckdb SQL | Notes |
|---|---|---|
| `>=, <=, >, <, ==, !=` | trivially native | — |
| `between` | native (`x BETWEEN a AND b`) | — |
| `in` | native (`x IN (?, ?, ?)`) | params, not literals |
| `contains` | native (`x ILIKE '%' || ? || '%'` or `contains`) | need case-sensitivity flag |
| `cross_above / cross_below` | CTE wrap + 2× `LAG` window | WHERE clause can't host a window fn — required CTE; matches polars lower-form exactly |
| `all / any / not` | AND / OR / NOT | trivial |
| `{"col": name}` | column ref | identifier-only, validated against catalog |
| arithmetic `+ - * /` | native | nested |
| `sma(e, n)` | `AVG(e) OVER (PARTITION BY ... ORDER BY time ROWS BETWEEN n-1 PRECEDING AND CURRENT ROW)` | needs a deterministic ORDER BY column (caller contract already enforces `(partition, time)` sorted) |
| `rmin / rmax` | `MIN / MAX (...) OVER (...)` | native |
| `shift(e, n)` | `LAG(e, n) OVER (...)` or `LEAD` for negative | duckdb has no native negative-shift — would need `ROW_NUMBER() DESC` or accept only positive shifts (current `shift(expr, n)` takes `n ≥ 1`, so we're fine) |
| `ema(col, span)` | **No native EMA window.** | Hand-roll a recursive CTE: `ema_t = alpha*x_t + (1-alpha)*ema_{t-1}`. Recursive CTEs are well-supported by duckdb but must be structured per `ORDER BY` key; not impossible, but a per-indicator chunk of code. |
| `rsi(col, n)` | **No native RSI.** | Hand-roll: `diff()`, clip gains/losses, rolling mean, ratio. Same recursive shape as EMA for the smoothed part. 30-50 lines of SQL per indicator. |
| `atr(n)` | Hand-roll `MAX(high-low, abs(high-prev_close), abs(low-prev_close))` + rolling mean | Uses catalog cols `high, low, close` — already structurally validated by `validate()`. |

The pattern for "indicator → SQL" is: each indicator becomes a CTE that selects
`(symbol, bar, indicator_value)` from the source table, joins back on the partition
key for the WHERE clause. For a filter involving `cross_above(ema(close,5),
ema(close,20))`, you'd need two indicator CTEs, then a join on `(symbol, bar)` for
the final WHERE. For `sma(close, 50) > close` you need one CTE and a self-join. This
is straightforward but tedious, and you re-do it for every IR feature.

What you DON'T get for the effort:

- A different perf profile than polars for the workload scanlang runs (see §4).
- Access to hotlake-only features (hotlake is already a polars-friendly parquet
  store; duckdb reading the same files is no faster on the access patterns we use).

What you DO get:

- The ability to mix scanlang predicates with arbitrary SQL — useful if a future
  hotlake surface hands you a duckdb connection and expects a filter fragment, but
  that hasn't materialised.

Effort: **2-3 weeks** of an experienced engineer to ship the basic feature set
(comparisons + `sma/rmin/rmax/shift/cross_above/below/arithmetic + groups +
indicators `ema/rsi/atr` as CTEs), plus a parallel test suite that proves parity
with the polars path on representative scan_defs. Ongoing burden: every new
indicator added in `INDICATORS` requires a SQL counterpart to keep parity.

### 2.2 Compiled `pl.Expr` → SQL via sqlglot

Premise: keep `compile()` as-is, add a translator that introspects the resulting
`pl.Expr` and emits duckdb SQL.

Reality check:

- **polars has no public Expr→SQL translator.** Confirmed against polars 1.44.1 —
  `explain()` gives a textual plan but not SQL.
- **sqlglot has no `polars` dialect.** Confirmed against sqlglot 30.17.0
  (`sqlglot.parse_one(..., read="polars")` raises `ValueError: Unknown dialect
  'polars'. Did you mean solr?`).
- sqlglot *does* understand duckdb, postgres, snowflake, etc. So a feasible shape
  is: walk the `pl.Expr` tree manually, build a sqlglot expression DAG, call
  `.sql(dialect="duckdb")`. That's a polars-Expr-AST walker we have to write
  ourselves anyway.

This is strictly more work than 2.1 with one extra constraint: it has to recognise
every node type polars' plan can produce, not just the ones `compile()` emits. The
planner-level equivalence is fragile (polars may rewrite `.over(partition)` into
something exotic in a future release; we'd track it).

Verdict: even worse trade than 2.1 — same SQL surface, more code, more release-pin
risk.

### 2.3 Pushdown only the cheap scalar predicate

The narrow win: when a scan starts with `WHERE <cheap>` (a literal-vs-column or
literal-vs-catalog-row comparison) and the rest is windows, push the cheap part
into duckdb's parquet scan via `read_parquet` + `WHERE`, then run windows in
polars on the result.

```python
def apply_hybrid(scan_def, con, parquet_glob, ...):
    cheap_pred = extract_scalar_predicates(scan_def)   # only col/literal ops
    rest_pred  = extract_computed_predicates(scan_def) # windows/indicators
    df = con.execute(f"SELECT ... FROM read_parquet(?) WHERE {cheap_pred_sql}", [parquet_glob]).pl()
    return df.lazy().filter(pl_compile(rest_pred)).collect()
```

Requires partitioning the IR into two disjoint predicate sets (which is doable but
non-trivial — `all`/`any` of mixed predicates must be split by AND/OR distribution),
and a different `validate` path that flags predicates as "pushable" vs "not".

In practice, scanlang's `score_bars()` output is the typical scan target, and the
*interesting* filters are the indicator ones (`rsi > 60`, `sma(close,50) > close`,
`cross_above(ema5, ema20)`). The "cheap" part is usually just `{"property":
"symbol", "op": "in", "value": [...]}`. Not nothing — symbol-list filtering on a
many-symbol parquet is a real win — but not enough to justify a parallel
compilation stack.

Verdict: defer. Revisit if a hotlake-side pattern emerges where users scan with a
huge symbol whitelist *and* a heavy indicator filter; in that case, register the
duckdb view on the parquet and push the whitelist.

### 2.4 What we already have (status quo)

```python
out = scanlang.apply(frame, scan_def)   # frame: LF or DF; duckdb-derived or not
```

- Works on any source: polars `scan_parquet`, polars `read_database` over duckdb,
  `pl.from_arrow`, hotlake's LazyFrame, eager `DataFrame`.
- Compiles once, validates against a catalog, runs lazily.
- No translation, no parallel codepath.
- The same compiled expression is what powers `Lab UI preview()`,
  `marimo` notebooks, REPL.

This is the right baseline. Adding a SQL backend fragments the surface (Lab UI
preview now has to choose which engine to render in) and the win it buys is
small enough that the maintenance tax dominates.

## 3. Per-IR-feature feasibility recap

(Compact. The full mapping is in §2.1.)

| IR feature | Polars native | DuckDB SQL | Notes |
|---|---|---|---|
| `>=,<=,>,<,==,!=` | ✅ | ✅ | trivial |
| `between, in, contains` | ✅ | ✅ | params, no concat |
| `all / any / not` | ✅ | ✅ | AND / OR / NOT |
| arithmetic `+ - * /` | ✅ | ✅ | nested |
| `sma, rmin, rmax` | ✅ `.rolling_X(n).over(p)` | ✅ window `ROWS BETWEEN n-1 PRECEDING AND CURRENT ROW` | order-by-col must be the partition's time column (caller contract) |
| `shift(e, n)` (n≥1) | ✅ | ✅ `LAG(e, n) OVER (...)` | duckdb has no negative-shift; OK for current IR |
| `ema(e, span)` | ✅ `.ewm_mean(span, adjust=False).over(p)` | ⚠️ recursive CTE | doable, 10-20 lines of SQL per indicator |
| `rsi(e, n)` | ✅ via `.diff().clip().rolling_mean()` | ⚠️ recursive CTE | 30-50 lines of SQL |
| `atr(n)` | ✅ via `max_horizontal` + shift | ⚠️ multi-step CTE | 20-30 lines |
| `cross_above / cross_below` | ✅ `(a>b) & shift(a,1)<=shift(b,1)` over p | ⚠️ CTE + 2 LAGs, WHERE-join | works, just verbose |

So: **everything is feasible in duckdb SQL**, but the EMA/RSI/ATR rows are the
expensive ones to maintain. Any new indicator added to `INDICATORS` needs a SQL
counterpart, and they'd diverge unless both paths share a single source-of-truth
(for example: a SQL fragment string per indicator in the same dict).

If we *did* ever want SQL pushdown for performance, the cleanest factoring is a
parallel `INDICATORS_SQL: dict[str, str]` registry where each entry is a CTE
fragment templated with `{expr}` placeholders, and a compiler that walks the IR,
emits one CTE per indicator reference (deduped), then assembles the final
predicate. That's the minimum design that keeps parity maintainable. **It still
isn't justified for v1.**

## 4. Perf intuition (with measured numbers)

The intuition is "duckdb scans parquet fast, pushdown wins". The reality on
**scanlang-shaped** workloads:

- **Single dataset, scalar filter + window(s) + scalar filter.** Tested locally
  with polars 1.44.1 + duckdb 1.5.5 on synthetic 2000 symbols × 1500 bars (3M
  rows, ~145 MB zstd parquet).
  - `pl.scan_parquet(...).filter(col > 50).with_columns(sma).filter(sma > 0)`:
    **~80 ms**
  - duckdb native parquet + window CTE doing the same: **~440 ms** (5.4×
    slower)
- **Cheap scalar filter alone, 95% selectivity.** polars 35 ms vs duckdb 150 ms
  (polars ~4.3× faster).
- **Cheap filter on a sorted small slice (last 5 bars only).** polars 13 ms vs
  duckdb 46 ms.

duckdb wins reliably on:

- Heavy **joins** (multiple tables, including the optional side data hotlake might
  attach).
- **Aggregations over the full table** (`GROUP BY`, `CUBE`, hash aggregates).
- Queries with **cross-engine data sources** (postgres + parquet + csv joined in
  one query).

scanlang's job is the inverse: **filter+window+filter on a single dataset, return
a small result**. That's polars' strongest workload, not duckdb's. The hotlake
parquet files are also columnar and per-symbol ordered — polars' predicate +
projection pushdown already skips most rows.

Caveat: this is microbench on synthetic data. Real hotlake queries could differ.
If a future pattern emerges (e.g. cross-symbol joins, "give me every row where
indicator X crossed in the last 5 sessions" across 50M rows), re-benchmark then.
Until then, **duckdb pushdown is not on the critical path**.

## 5. When this calculus changes (revisit triggers)

- hotlake starts serving data through a **duckdb connection** (not via polars
  `scan_parquet`). Then SQL pushdown matters because the alternative is
  `con.pl().lazy()` (round-trip the entire dataset through arrow).
- Scan volume grows past "what fits in one polars chunk" — single-partition
  LazyFrames stop being a thing; full-table aggregates need duckdb.
- A new indicator joins `INDICATORS` that **duckdb has natively** and polars
  doesn't (e.g. windowed `percentile_cont`, `approx_top_k`). At that point, even a
  partial SQL backend for that one indicator would be cheap.
- Cross-source joins become a real pattern (scanlang filter + side metadata
  table). Then the SQL backend is the natural way to express it.

## 6. Recommendation

**Do nothing for v1.** Keep the existing `compile`/`validate`/`apply` API on top of
`pl.Expr`. Optionally:

- Add **small ergonomic touches** that improve the hot path without a parallel
  codepath: a `partitioned=True` shortcut on `apply`, or `apply_with_stats` that
  returns `(frame, count_before, count_after)`. None of these needs SQL.
- File a future-task note: "if hotlake adopts duckdb-native reads OR scan volume
  grows past polars' single-chunk size, revisit §2.3 (pushdown of cheap
  predicates) before §2.1 (full SQL backend)."

Effort saved: **2-3 engineer-weeks** that would otherwise go to a SQL
backend that benchmarks 5× slower than what we have.

## 7. Effort summary (for completeness)

| Path | Effort | Payoff | Risk |
|---|---|---|---|
| Status quo | 0 | baseline | low |
| §2.3 partial pushdown (later, if needed) | 1-2 weeks | niche — symbol-list filters on parquet | low — keeps polars main path |
| §2.1 full SQL backend | 2-3 weeks + ongoing per indicator | none on current workloads | medium — every new indicator needs SQL |
| §2.2 polars-Expr → SQL | 3-4 weeks + ongoing per polars release | none on current workloads | high — pinned to polars plan shape |