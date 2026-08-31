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

v1 indicators: `sma, ema, rsi, atr, rmin, rmax, shift`. A future
`scanlang.talib` optional module (matches the pyproject `talib` extra)
populates the same dict for exact-value parity on collected results —
it cannot participate in lazy pushdown.

## compile / validate / apply

| Function | Purpose |
| --- | --- |
| `compile(scan_def, *, catalog=PROPERTY_CATALOG, partition="symbol")` | scan def -> one polars predicate |
| `validate(scan_def, *, catalog=...)` | `list[str]` of errors; empty = valid |
| `apply(frame, scan_def, *, catalog=..., partition=...)` | filter + order_by + limit (eager or lazy) |
| `catalog_from_schema(frame)` | polars schema -> catalog dict |

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
- [Why no duckdb](why-no-duckdb.md) — the backend question we closed
