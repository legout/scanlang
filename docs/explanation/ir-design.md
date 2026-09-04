# IR design

The scan definition (the IR) is the single contract between callers and
`scanlang`. This page is the design writeup; the historical freeze note
is at [`docs/IR_FREEZE.md`](https://github.com/legout/scanlang/blob/master/docs/IR_FREEZE.md).

## Top-level shape

```python
{
    "filters":  [node, ...],                                      # required
    "order_by": [{"property": <name>, "dir": "asc" | "desc"}, ...],  # optional
    "limit":    <non-negative int>,                                 # optional
}
```

A bare `filters` list of leaves is valid: today's flat defs keep working.
`order_by` and `limit` are additive conveniences on top.

## Nodes — three groups + leaf

- `{"all": [node, ...]}` — AND (nonempty)
- `{"any": [node, ...]}` — OR (nonempty)
- `{"not": node}` — unary NOT
- leaf: `{"property": ..., "op": ..., "value": ...}`

Groups nest arbitrarily. Implicit AND at the top level keeps the flat
def shape (a list of leaves = AND them).

## Operators

- `>= <= > < == !=` — standard comparisons
- `between` — `[lo, hi]` literal-only, closed interval
- `in` — nonempty list, literal-only, membership
- `contains` — string literal only, substring
- `cross_above` / `cross_below` — operand on both sides;
  `cross_above(a, b)` compiles to `a > b AND shift(a,1) <= shift(b,1)`
  over the partition (mirrored for `cross_below`)

## Operands

Anywhere `<operand>` appears (the `property` or `value` of a leaf, args
of an arithmetic fold, args of an indicator call):

- bare scalar literal — `40`, `"BREAKOUT"`, `False`
- `{"col": "name"}` — column reference (catalog-validated)
- `{"fn": "sma", "args": [operand, ...]}` — indicator call, args are
  operands themselves (so `sma(rsi(close, 14), 5)` is legal)
- `{"+": [a, b]}` / `"-"` / `"*"` / `"/"` — arithmetic fold, n-ary;
  `{"-": [x]}` negates

`in` / `between` / `contains` `value`s stay literal-only — they cannot
import a computed operand on the right side.

## `order_by` is property-only

`order_by` keys are catalog property names — column names, not operand
expressions. There is no "order by rsi(close, 14) desc" in v1; that's a
future additive key, deliberately out of scope.

## INDICATORS — the registry contract

```python
INDICATORS: dict[str, tuple[tuple[str, ...], Callable, tuple[str, ...]]]
#            name -> (arg_spec, builder, required_cols)
```

- `arg_spec` — tuple of one tag per positional arg: `"expr"` (any operand)
  or `"int"` (literal int >= 1).
- `builder(*parsed, partition) -> pl.Expr` — polars-native, every window
  op uses `.over(partition)`.
- `required_cols` — column names that must exist in the catalog (e.g.
  `atr` needs `high, low, close`).

The registry is a public module-level dict; consumers extend by insertion.
The entry shape is the contract — see [Extend INDICATORS](../how-to/extend-indicators.md).

### TA-Lib alignment note

`scanlang` 0.3.0 aligns the polars `INDICATORS` builders to TA-Lib
recursion so cross-engine values converge:

- `ema` already uses the same recursion as TA-Lib (`k = 2 / (n + 1)`);
  only the seed differs.
- `rsi` and `atr` moved from simple rolling means (Cutler) to Wilder
  smoothing (`ewm_mean(alpha = 1/n, adjust=False)`), matching TA-Lib.

TA-Lib seeds EMA/RSI/ATR with an SMA of the first `n` values; polars
`ewm_mean(adjust=False)` seeds from the first value. Exact match is
not expr-expressible, so the contract is: values converge after warm-up
(typically ~4×n bars; the benchmark measures full agreement within 0.01
by ~7.6n), early-window divergence is documented. Warm-up rows are
excluded from scan hits by the count-guard or by polars' own null
propagation, so mature-bar scans see consistent values across engines.

Marketdata-screens `score_bars` values (RSI thresholds 70/85,
`atr_ratio`) shift slightly as a consequence; near-threshold scores
can flip. Called out in the 0.3.0 release notes.

### Indicators and engines

`INDICATORS` is the polars registry. The duckdb
backend has its own [`SQL_INDICATORS`](../reference/duckdb-backend.md)
that is a strict superset: the entries shared between the two have
identical `arg_spec` and `required_cols`, and the duckdb-only names
(`ht_trendline`, `stoch_k`, `stoch_d`) exist only on
the duckdb side. The talib parity names (`macd`, `bbands_upper`,
`bbands_lower`, `adx`, `aroon`, `kama`) are dual-engine: exact TA-Lib
polars builders (the eager map_groups seam) on this side, `t_*`
lowerings on the SQL side. [`validate(scan_def, *,
engine=...)`](../reference/api.md)
accepts the `engine` kwarg to gate which names pass. The full table:
[Indicators reference](../reference/indicators.md).

## compile / validate / apply

| Function | Purpose |
| --- | --- |
| `compile(scan_def, *, catalog=PROPERTY_CATALOG, partition="symbol", engine="polars")` | scan def -> one polars predicate |
| `validate(scan_def, *, catalog=...)` | `list[str]` of errors; empty = valid |
| `apply(frame, scan_def, *, catalog=..., partition=..., engine=...)` | filter + order_by + limit (eager or lazy) |
| `catalog_from_schema(frame)` | polars schema -> catalog dict |

The `engine=` kwarg is additive on `validate`, `compile`, and `apply`
(default `"polars"`). It selects which indicator registry validates
`{"fn": ...}` names — `"polars"` uses `INDICATORS`, `"duckdb"` uses
the strict superset in `SQL_INDICATORS`. It does not change which
plan `compile` emits; the polars path always emits `pl.Expr`. For the
duckdb backend, use [`scanlang.duckdb_sql.compile_sql`](../reference/duckdb-backend.md)
and `apply_sql` to get parameterized SQL.

Caller contract: the frame is sorted `(partition, time)` ascending.
Nonstandard column names are renamed at the caller's edge
(`lf.rename({"date": "session"})`).

Full signatures and behavior live in the [API reference](../reference/api.md).

## Validation split, null semantics

- **Total** for literal leaves (dtype-checked) vs **structural** for computed
  operands (known fn, known col, arg count, required cols). Why the split
  exists: [Validation split](validation-split.md).
- Comparisons and `not` on null yield null; the filter drops the row.
  Why we don't work around it: [Null semantics](null-semantics.md).

## Evolution rule

New capabilities = additive keys. Old consumers ignore unknown keys.
No version field. The full contract lives at
[IR freeze reference](../reference/ir-freeze.md); the historical
freeze record is at
[`docs/IR_FREEZE.md`](https://github.com/legout/scanlang/blob/master/docs/IR_FREEZE.md).

## Text DSL — frozen grammar

`parse(text) -> {"filters": [...]}` (only). Pure stdlib, recursive
descent. Grammar:

```text
expr       := term (('AND'|'&&') term)*                 -> {"all":[...]}
           |  term ('OR'|'||') term*                    -> {"any":[...]}
term       := 'NOT' term | '(' expr ')' | comparison | bool-col
comparison := arith (op arith)?     op: > < >= <= == != =  (between/in below)
arith      := mul (('+'|'-') mul)*                       -> {"+"/"-" n-ary}
mul        := atom (('*'|'/') atom)*
atom       := number | 'string' | BAREWORD | call | atom '[' int ']'
call       := name '(' args ')' ('[' int ']')?
args       := atom (',' atom)*
```

Parse errors raise `SyntaxError` with a 1-based position. Semantic
checks (unknown column, bad arg counts, dtypes) stay in
`scanlang.compiler.validate()`.

Full rules: [Scan from text](../how-to/scan-from-text.md).

## Where to next

- [Lazy contract](lazy-contract.md) — why `apply` is shape-preserving
- [Validation split](validation-split.md) — total vs structural
- [Null semantics](null-semantics.md) — polars null semantics on
  filters
- [Why no duckdb](why-no-duckdb.md) — the verdict the SQL backend
  supersedes
