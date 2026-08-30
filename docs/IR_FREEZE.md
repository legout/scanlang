# scanlang IR freeze — 2026-08-30

Confirmed design contract. Additive changes only; no breaking IR changes without a
new freeze session. Consumers: marketdata-screens (Lab UI), REPL, jupyter/marimo.

## Signal dict (the IR)

Top level (a bare `filters` list of leaves is valid — today's flat defs unchanged):

```
{"filters": [node, ...], "order_by": [{"property": ..., "dir": "asc"|"desc"}], "limit": int}
```

Node = group or leaf. Groups:

- `{"all": [node, ...]}` — AND (nonempty)
- `{"any": [node, ...]}` — OR (nonempty)
- `{"not": {node}}` — unary NOT

Leaf: `{"property": <prop>, "op": <op>, "value": <operand>}`

- `property`: column name string (catalog-validated) **or** operand object (computed LHS)
- ops: `>= <= > < == != between in contains` plus `cross_above` / `cross_below`
  (compile to `a > b AND shift(a,1) <= shift(b,1)` over the partition; mirrored for below)
- `order_by`: catalog property names only — no operand expressions

Operands (valid wherever `<operand>` appears; `in`/`between`/`contains` values stay
literal-only):

- bare scalar (int/float/str/bool) = literal
- `{"col": "name"}` = column ref (catalog-validated)
- `{"fn": "sma", "args": [operand, ...]}` = indicator call, args recursive
  (`sma(rsi(close,14),5)` legal)
- `{"+": [a, b]}` / `"-"` / `"*"` / `"/"` = arithmetic fold (binary or n-ary;
  `{"-": [x]}` = negate)

## Indicators

`INDICATORS: dict[name -> (arg_spec, builder, required_cols)]` — public module-level
dict; consumers extend by insertion. The entry shape is the contract.

- `arg_spec`: tuple with one tag per positional arg — `"expr"` (any operand) or
  `"int"` (literal int >= 1)
- `builder(*parsed, partition) -> pl.Expr` — polars-native, window ops `.over(partition)`
- `required_cols`: column names that must exist in the catalog (e.g. `atr` needs
  `high, low, close`)

v1 set: `sma(col, n)`, `ema(col, span)`, `rsi(col, n)`, `atr(n)`, `rmin(col, n)`,
`rmax(col, n)`, `shift(expr, n)`. Future `scanlang.talib` optional module (matches the
pyproject `talib` extra) populates the same dict — exact-value parity on collected
results; it cannot participate in lazy pushdown.

## compile / validate / apply

- `compile(scan_def, *, catalog=PROPERTY_CATALOG, partition="symbol") -> pl.Expr`
- `validate(scan_def, *, catalog=PROPERTY_CATALOG) -> list[str]` — empty = valid;
  human-facing strings keyed to fields (Lab UI surfaces them inline)
- `apply(frame, scan_def, *, catalog=..., partition=...)` — filter + order_by +
  limit; works on eager `DataFrame` and `LazyFrame` alike (collect at your edge)
- `catalog_from_schema(frame)` — polars schema → catalog dict; any LazyFrame usable
  in one line. Unmappable dtypes are skipped.
- `PROPERTY_CATALOG` — default catalog mirroring `score_bars()` output columns.

Caller contract: the frame is sorted `(partition, time)` ascending. Nonstandard
column names are renamed at the caller's edge (`lf.rename({"date": "session"})`).

Validation split contract:

- **Total** for literal leaves: any malformed def surfaces as `ValueError` from
  `compile`/`apply` and error strings from `validate` — never a polars ComputeError
  at filter time. Dtypes checked against the catalog (bool is not int).
- **Structural** for computed operands: known fn, known col, arg count/type,
  required cols. Dtype mismatches (e.g. `close > sma(symbol, 5)`) surface as polars
  errors at collect time.

Null semantics: comparisons and `not` on null yield null → row dropped by filter.
Documented behavior, not worked around.

Evolution rule: new capabilities = additive keys; old consumers ignore unknown keys.
No version field.

## Modules

- `score_bars(bars: LazyFrame) -> LazyFrame` — phase/scan scoring over OHLCV,
  fixed input schema `symbol, session, open, high, low, close, volume` (sorted
  `symbol, session`), output columns = `PROPERTY_CATALOG` keys. Eager frames work
  too (same method surface); collect at the caller's edge.
- `fetch_recent_bars` → hotlake (not scanlang). `scan()` composition → integrator.
- `forward_stats` + `backtest_summary` (+ `HORIZONS`) — forward-return evidence;
  pure, no frame deps.
- `scan_def_from_signals`, `preview`, `spark_points` stay in marketdata-screens.
- Text DSL front-end: frozen (section below) — `scanlang.dsl.parse(text)` emits
  this same IR.

## Open (outside this freeze)

- duckdb SQL translation of compiled exprs (sqlglot) vs filter-in-polars — research
  task, handoff item 4. **Closed 2026-08-30**: keep polars-only, no SQL backend
  (see docs/RESEARCH_DUCKDB.md).

## Text DSL front-end (frozen 2026-08-30, second session)

`parse(text) -> scan_def` (returns `{"filters": [...]}` only; order_by/limit stay
dict-side). Pure stdlib tokenizer + recursive-descent in `scanlang/dsl.py`. Grammar:

```
expr    := term (('AND'|'&&') term)*   -> {"all":[...]}
        | term ('OR'|'||') term*       -> {"any":[...]}
term    := 'NOT' term | '(' expr ')' | comparison | bool-col
comparison := arith (op arith)?    op: > < >= <= == != =  (between/in below)
arith   := mul (('+'|'-') mul)*    -> {"+"/"-" n-ary}
mul     := atom (('*'|'/') atom)*
atom    := number | 'string' | BAREWORD | call | atom '[' int ']'
call    := name '(' args ')' ('[' int ']')?
args    := atom (',' atom)*
```

Rules:

- AND binds tighter than OR; both case-insensitive; `NOT` unary.
- BAREWORD: catalog bool column -> `col == true` leaf; otherwise column ref.
  Non-bool bare operand without comparison = parse error.
- `name(...)` where name is in INDICATORS -> indicator call; else column with
  lookback: `close(1)` -> `shift(close,1)`. Postfix `atom[n]` -> `shift(expr,n)`
  (Pine-style, canonical). Both spellings accepted.
- Default column: first-arg-number inserts `close` (ema/sma/rmin/rmax);
  `rsi(14)`/`atr(14)` unchanged; explicit column stays `sma(volume,20)`.
- `min(n)`/`max(n)` sugar -> `rmin`/`rmax` (default close).
- `=` -> `==`. `between [lo, hi]` -> between op; `in [A, B, 'x']` -> in op
  (barewords become strings).
- `cross_above(a, b)` / `cross_below(a, b)` -> cross ops.
- Indicator args/lookbacks validate through the existing IR validate(); no new
  error contract — parse errors are SyntaxError with position, validation errors
  stay in validate().
- Not Pine: no vars/loops/security calls — expression subset only. Full Pine is
  a compiler project, out of scope.
- order_by stays catalog-props-only (old corpus's `order: close/min(21)` is a
  future additive key, not v1).
