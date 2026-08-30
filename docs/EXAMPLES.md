# Examples

Six worked examples, all runnable and all verified against v0.1: the scripts
in `docs/examples/` print the output shown here and assert it, so docs and
behavior can't drift. Each block marked `# cell:` pastes as one cell into
jupyter/marimo; the scripts themselves are plain `python` files.

Run them with your project interpreter:

```sh
.venv/bin/python docs/examples/01_quickstart.py
```

The fixture in every example is the same two-symbol OHLCV frame: `AAA` a
60-day linear uptrend (10 -> 69), `BBB` a 60-day linear downtrend
(60 -> 1), both with synthetic open/high/low/volume.

## 1. Quickstart: score, validate, apply

`docs/examples/01_quickstart.py`

`score_bars` scores every symbol's latest bar (lazy in, lazy out — collect at
your edge); a scan definition is a plain dict; `validate` returns `[]` when
it's valid; `apply` = filter + order_by + limit.

```python
scored = score_bars(bars().lazy()).collect()

scan_def = {
    "filters": [{"property": "score", "op": ">=", "value": 40}],
    "order_by": [{"property": "score", "dir": "desc"}],
    "limit": 1,
}
print("errors:", validate(scan_def))       # []
result = apply(scored, scan_def)
```

Output:

```
shape: (2, 5)
┌────────┬────────────┬───────┬───────┬───────┐
│ symbol ┆ session    ┆ close ┆ score ┆ phase │
│ ---    ┆ ---        ┆ ---   ┆ ---   ┆ ---   │
│ str    ┆ date       ┆ f64   ┆ i16   ┆ str   │
╞════════╪════════════╪═══════╪═══════╪═══════╡
│ AAA    ┆ 2026-03-01 ┆ 69.0  ┆ 60    ┆ BASE  │
│ BBB    ┆ 2026-03-01 ┆ 1.0   ┆ 20    ┆ NONE  │
└────────┴────────────┴───────┴───────┴───────┘
errors: []
shape: (1, 3)
┌────────┬───────┬───────┐
│ symbol ┆ score ┆ phase │
│ ---    ┆ ---   ┆ ---   │
│ str    ┆ i16   ┆ str   │
╞════════╪═══════╪═══════╡
│ AAA    ┆ 60    ┆ BASE  │
└────────┴───────┴───────┘
```

(Both symbols are scored but only AAA clears 40.)

## 2. Flat defs and all/any/not groups

`docs/examples/02_groups.py`

A bare `filters` list of leaves is ANDed — today's flat definitions are
unchanged. Nest `any` for OR, `not` for negation, arbitrarily deep:

```python
flat = {
    "filters": [
        {"property": "score", "op": ">=", "value": 30},
        {"property": "phase", "op": "!=", "value": "NONE"},
    ],
}
grouped = {
    "filters": [
        {"any": [
            {"property": "phase", "op": "in", "value": ["BREAKOUT", "TREND"]},
            {"property": "score", "op": "between", "value": [55, 100]},
        ]},
        {"not": {"property": "spring", "op": "==", "value": True}},
    ],
}
```

Output (both defs keep only AAA — the uptrend; BBB scores 20 and its phase
is NONE):

```
flat errors: []
shape: (1, 3)
┌────────┬───────┬───────┐
│ symbol ┆ score ┆ phase │
│ ---    ┆ ---   ┆ ---   │
│ str    ┆ i16   ┆ str   │
╞════════╪═══════╪═══════╡
│ AAA    ┆ 60    ┆ BASE  │
└────────┴───────┴───────┘
grouped errors: []
shape: (1, 3)
... same table ...
```

Note the leaves inside groups use the same leaf shape as the top level.
`between` bounds are inclusive, and the script asserts every hit row
satisfies the predicates independently of scanlang.

## 3. Computed operands: indicators, arithmetic, crosses

`docs/examples/03_computed_operands.py`

Any `property` (and any cross `value`) may be a computed operand: column
ref, indicator call, arithmetic fold — recursively. Here everything runs on
raw OHLCV with a catalog derived from the schema; no `score_bars` involved:

```python
catalog = catalog_from_schema(lf)          # any LazyFrame -> catalog

ema_cross = {"filters": [{
    "property": {"fn": "ema", "args": [{"col": "close"}, 5]},
    "op": "cross_above",
    "value": {"fn": "ema", "args": [{"col": "close"}, 20]},
}]}
crossed = apply(lf, ema_cross, catalog=catalog).collect()
```

Output — a linear uptrend crosses exactly once (bar 1); the downtrend never:

```
shape: (1, 3)
┌────────┬────────────┬───────┐
│ symbol ┆ session    ┆ close │
│ ---    ┆ ---        ┆ ---   │
│ str    ┆ date       ┆ f64   │
╞════════╪════════════╪═══════╡
│ AAA    ┆ 2026-01-02 ┆ 11.0  │
└────────┴────────────┴───────┘
```

The same example drives an arithmetic operand (`close < rmin(close,10)/2` —
zero hits here, asserted) and a fully nested def, `sma(close + rsi(close,14), 5) > close`
(112 hits, asserted), showing operand recursion:

```python
nested = {"filters": [{
    "property": {"fn": "sma", "args": [
        {"+": [{"col": "close"}, {"fn": "rsi", "args": [{"col": "close"}, 14]}]}, 5]},
    "op": ">",
    "value": {"col": "close"},
}]}
```

## 4. Custom partition, custom catalog, extending the registry

`docs/examples/04_custom_partition_and_registry.py`

scanlang isn't tied to `score_bars` output or the name `symbol`. Rename at
your edge, derive the catalog, point `partition` at your group column —
every window op then computes per ticker:

```python
lf = bars().rename({"symbol": "ticker"}).lazy()
catalog = catalog_from_schema(lf)

rsi_hot = {"filters": [{
    "property": {"fn": "rsi", "args": [{"col": "close"}, 14]},
    "op": ">", "value": 70,
}]}
hot = apply(lf, rsi_hot, catalog=catalog, partition="ticker").collect()
```

Output — the uptrend clears RSI 70 from bar 15 on (46 rows); the downtrend's
RSI is pinned at 0:

```
shape: (1, 2)
┌────────┬─────┐
│ ticker ┆ len │
│ ---    ┆ --- │
│ str    ┆ u32 │
╞════════╪═════╡
│ AAA    ┆ 46  │
└────────┴─────┘
```

The example then extends `INDICATORS` (insertion is the extension contract)
with `stdev` and screens on a z-score:

```python
def _stdev(e: pl.Expr, n: int, partition: str) -> pl.Expr:
    return e.rolling_std(n).over(partition)

INDICATORS["stdev"] = (("expr", "int"), _stdev, ())

z_score = {"filters": [{
    "property": {"/": [
        {"-": [{"col": "close"}, {"fn": "sma", "args": [{"col": "close"}, 20]}]},
        {"fn": "stdev", "args": [{"col": "close"}, 20]},
    ]},
    "op": ">", "value": 0.5,
}]}
# 41 rows, all AAA — asserted
```

Registry entry shape: `(arg_spec, builder, required_cols)` where `arg_spec`
tags each positional arg `"expr"` or `"int"`, and `required_cols` are
catalog-checked up front (`atr` requires `high, low, close`).

## 5. score_bars + apply end-to-end, then forward stats

`docs/examples/05_score_and_stats.py`

`apply` works directly on a `LazyFrame` — the scoring pipeline never has to
materialize before the screen:

```python
scan_def = {
    "filters": [{"property": "score", "op": ">=", "value": 40}],
    "order_by": [{"property": "score", "dir": "desc"}],
    "limit": 5,
}
picks = apply(score_bars(bars()), scan_def).collect()
```

Then the pure-Python stats helpers answer "what did a past run of this scan
actually do?": `forward_stats` computes +5/+10/+20d returns of the run's
entry vs the latest close, `backtest_summary` aggregates hit rate and average
per horizon (`HORIZONS`). This run's single pick (AAA at day 30, in a lake
that kept rising):

```
included: 1 of 1 picks
  5d: hit rate 100%, avg +3.8% (n=1)
  10d: hit rate 100%, avg +7.7% (n=1)
  20d: hit rate 100%, avg +15.4% (n=1)
```

Runs whose 20d window hasn't elapsed return `None` from `forward_stats` and
are excluded by `backtest_summary` — surface that as "n included / m total".

## 6. Malformed defs: errors, not crashes

Back in the REPL — validation is total for literal leaves, so bad input
surfaces as data, never as a polars `ComputeError` mid-scan:

```python
>>> from scanlang import compile, validate
>>> validate({"filters": [{"property": "nope", "op": ">=", "value": 1}]})
["filters[0]: unknown property: 'nope'"]
>>> compile({"filters": [{"property": "nope", "op": ">=", "value": 1}]})
Traceback (most recent call last):
  ...
ValueError: filters[0]: unknown property: 'nope'
>>> validate({"filters": [{"property": {"fn": "ema", "args": [{"col": "close"}, "x"]}, "op": ">", "value": 1}]})
["filters[0].property.args[1]: must be an int >= 1, got 'x'"]
```

(All three outputs are pinned by `tests/test_scanlang.py` —
`test_validate_total_for_literals` and `test_validate_structural_for_computed`.)
Computed operands get structural validation: unknown indicator, wrong arg
count, non-int window, missing required columns — all caught before any
frame is touched. Dtype mismatches *inside* computed operands (e.g.
`sma("symbol", 5)`) are the one thing that surfaces at collect time, by
design.

## Where to go next

- `docs/IR_FREEZE.md` — the full contract, including the validation split,
  null semantics, and the additive-only evolution rule
- `README.md` — API table and quickstart
- `docs/RESEARCH_DUCKDB.md` — why compiled filters stay in polars
