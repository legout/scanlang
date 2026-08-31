# scanlang

Screener DSL and scan compiler: signal definitions -> polars pushdown filters.

A scan definition is a plain dict — JSON from a UI, a Python literal from a
notebook — that `scanlang` compiles into a single validated polars predicate.
There is no string interpolation, so there is no injection surface. Filters
run on any eager `DataFrame` or lazy `LazyFrame`; window semantics
(indicators, crosses) are computed per partition, so 10 symbols or 10,000
behave the same.

Status: v0.2. The IR is frozen — see `docs/IR_FREEZE.md` for the exact
contract (additive changes only). Consumers: marketdata-screens (Lab UI),
REPL, jupyter/marimo.

## Install

```sh
uv add scanlang          # or: pip install scanlang
```

Requires Python >= 3.11 and polars >= 1.44. The optional `talib` extra
(`uv add scanlang --optional talib`) is reserved for a future value-parity
indicator module.

## Quickstart

`score_bars` turns OHLCV bars into one scored row per symbol; `apply`
filters/orders/limits that frame with a scan definition. (Data setup elided —
full runnable version in `docs/examples/01_quickstart.py`.)

```python
>>> from scanlang import apply, compile, score_bars, validate
>>> scored = score_bars(bars.lazy()).collect()   # lazy in, lazy out — collect at your edge
>>> scored.select("symbol", "close", "score", "phase")
shape: (2, 4)
┌────────┬───────┬───────┬───────┐
│ symbol ┆ close ┆ score ┆ phase │
│ ---    ┆ ---   ┆ ---   ┆ ---   │
│ str    ┆ f64   ┆ i16   ┆ str   │
╞════════╪═══════╪═══════╪═══════╡
│ AAA    ┆ 69.0  ┆ 60    ┆ BASE  │
│ BBB    ┆ 1.0   ┆ 20    ┆ NONE  │
└────────┴───────┴───────┴───────┘
```

A scan definition is a plain dict. `validate` returns `[]` when it's valid;
`apply` runs it; `compile` hands you the bare polars expression:

```python
>>> scan_def = {
...     "filters": [
...         {"property": "score", "op": ">=", "value": 40},
...         {"any": [
...             {"property": "phase", "op": "in", "value": ["BREAKOUT", "TREND"]},
...             {"not": {"property": "phase", "op": "==", "value": "NONE"}},
...         ]},
...     ],
...     "order_by": [{"property": "score", "dir": "desc"}],
...     "limit": 5,
... }
>>> validate(scan_def)
[]
>>> apply(scored, scan_def).select("symbol", "score", "phase")
shape: (1, 3)
┌────────┬───────┬───────┐
│ symbol ┆ score ┆ phase │
│ ---    ┆ ---   ┆ ---   │
│ str    ┆ f64   ┆ i16   ┆ str   │
╞════════╪═══════╪═══════╡
│ AAA    ┆ 60    ┆ BASE  │
└────────┴───────┴───────┘
>>> expr = compile(scan_def)     # a single polars predicate
```

(Transcript above is machine-generated from a live REPL —
`scripts/gen_repl.py` regenerates it.)

## The scan definition (the IR)

Top level: `{"filters": [node, ...], "order_by": [...], "limit": int}` —
`order_by` and `limit` are optional; a bare `filters` list of leaves is
valid, so today's flat defs keep working.

**Nodes** nest arbitrarily:

- `{"all": [node, ...]}` — AND (nonempty)
- `{"any": [node, ...]}` — OR (nonempty)
- `{"not": {node}}` — unary NOT
- leaf: `{"property": <prop>, "op": <op>, "value": <operand>}`

**Ops:** `>= <= > < == != between in contains`, plus `cross_above` /
`cross_below`, which compile to `a > b AND shift(a,1) <= shift(b,1)` over the
partition (mirrored for below).

**Properties and operands** — a `property` is a catalog column name or a
computed operand; comparison `value`s are operands too (`in`/`between`/
`contains` values stay literal-only):

- bare scalar — literal: `60`, `"BREAKOUT"`, `False`
- `{"col": "close"}` — column ref
- `{"fn": "sma", "args": [operand, ...]}` — indicator call, args recursive
  (`sma(rsi(close,14), 5)` is legal)
- `{"+": [a, b]}`, `"-"`, `"*"`, `"/"` — arithmetic fold (n-ary;
  `{"-": [x]}` negates)

**Indicators** (`INDICATORS`, extensible by insertion): `sma`, `ema`, `rsi`,
`atr`, `rmin`, `rmax`, `shift`. Window ops are computed `.over(partition)`
(default `"symbol"`).

**Validation split:** literal leaves are totally validated — malformed defs
raise `ValueError` from `compile`/`apply` and return error strings from
`validate`, never a polars `ComputeError` at filter time. Computed operands
are structurally validated (known fn, known col, arg types, required cols);
dtype mismatches there surface at collect time.

**Nulls:** comparisons and `not` on null yield null, and filter drops null
rows. Documented behavior, not worked around.

## Text DSL

The same scan definition can be written as a one-line expression.
`parse` (v0.2) turns text into the IR dict — tokenizer + recursive-descent
parser, pure stdlib, errors are `SyntaxError` with a 1-based position:

```python
>>> from scanlang import parse
>>> parse("ema(20) > ema(50)")
{'filters': [{'property': {'fn': 'ema', 'args': [{'col': 'close'}, 20]},
              'op': '>',
              'value': {'fn': 'ema', 'args': [{'col': 'close'}, 50]}}]}
```

More shapes:

```python
parse("cross_above(ema(20), ema(50))")            # golden cross, one line
parse("close > sma(200, close(22)) and rsi(14) > 70")
#   AND binds tighter than OR; sma's second arg is corpus order:
#   sma(200, close(22)) -> sma(shift(close, 22), 200)
parse("phase in [BREAKOUT, TREND] or close between [50, 70]")
parse("spring and not near_52w_low")              # bareword bool -> == true
```

Rules worth knowing: a lone number on `ema/sma/rmin/rmax` implies `close`
(`ema(20)` = `ema(close, 20)`; `rsi(14)`/`atr(14)` are already correct);
history has two spellings — `close(22)` and Pine-style postfix
`close[22]` — both normalize to `shift(close, 22)`; `=` means `==`;
`min(n)`/`max(n)` sugar to `rmin`/`rmax`. Parse errors (bad syntax, unknown
column) raise `SyntaxError` with position; semantic errors — wrong arg
counts, bad windows — stay in `validate()`:

```python
>>> validate(parse("sma(close, 20, 7) > 5"))
["filters[0].property: 'sma' takes 2 args, got 3"]
```

## Any LazyFrame, any catalog

`score_bars` output mirrors `PROPERTY_CATALOG`, but nothing is tied to it:
derive a catalog from any frame's schema and point `partition` at your group
column.

```python
>>> import polars as pl
>>> from scanlang import apply, catalog_from_schema
>>> lf = bars.rename({"symbol": "ticker"}).lazy()          # rename at your edge
>>> cat = catalog_from_schema(lf)                          # schema -> catalog
>>> rsi_hot = {"filters": [{
...     "property": {"fn": "rsi", "args": [{"col": "close"}, 14]},
...     "op": ">", "value": 70,
... }]}
>>> apply(lf, rsi_hot, catalog=cat, partition="ticker").collect().head(3)
shape: (3, 3)
┌────────┬────────────┬───────┐
│ ticker ┆ session    ┆ close │
│ ---    ┆ ---        ┆ ---   │
│ str    ┆ date       ┆ f64   │
╞════════╪════════════╪═══════╡
│ AAA    ┆ 2026-01-15 ┆ 24.0  │
│ AAA    ┆ 2026-01-16 ┆ 25.0  │
│ AAA    ┆ 2026-01-17 ┆ 26.0  │
└────────┴────────────┴───────┘
# 46 rows total: the uptrend clears RSI 70 from bar 15 on; the downtrend never
```

## API

| Function | Purpose |
| --- | --- |
| `compile(scan_def, *, catalog=PROPERTY_CATALOG, partition="symbol")` | scan def -> one polars predicate `Expr` |
| `validate(scan_def, *, catalog=...)` | `list[str]` of errors; empty = valid |
| `apply(frame, scan_def, *, catalog=..., partition=...)` | filter + order_by + limit (eager or lazy) |
| `catalog_from_schema(frame)` | polars schema -> catalog dict; unmapped dtypes skipped |
| `score_bars(bars, *, min_bars=30, freshness_days=5)` | phase/scan scoring over OHLCV; lazy in, lazy out |
| `forward_stats` / `backtest_summary` (+ `HORIZONS`) | forward-return evidence for a scan's past runs |

Caller contract: the frame is sorted `(partition, time)` ascending.
Nonstandard column names are renamed at your edge (`lf.rename({"date": "session"})`).

## Examples

Runnable scripts in `docs/examples/` (each block is a notebook cell if you
paste into marimo/jupyter):

1. `01_quickstart.py` — score_bars + validate + apply
2. `02_groups.py` — flat defs, all/any/not groups
3. `03_computed_operands.py` — col refs, indicators, arithmetic, EMA cross
4. `04_custom_partition_and_registry.py` — custom catalog + partition, extending INDICATORS
5. `05_score_and_stats.py` — apply on a LazyFrame + forward_stats/backtest_summary

Run them with `.venv/bin/python docs/examples/01_quickstart.py` (or your
project interpreter). `docs/EXAMPLES.md` walks through them with real output.

## Docs

- `docs/IR_FREEZE.md` — the frozen IR contract (spec)
- `docs/EXAMPLES.md` — annotated walkthroughs with verified output
- `docs/RESEARCH_DUCKDB.md` — why compile targets polars, not SQL

## Development

```sh
uv sync                                   # create .venv
.venv/bin/python -m pytest tests/ -q      # tests
.venv/bin/python -m ruff check src tests  # lint
```

## License

MIT
