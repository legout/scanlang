# Eager vs lazy frames

`apply` is shape-preserving: eager in -> eager out, lazy in -> lazy out.
`score_bars` is lazy in -> lazy out (eager `DataFrame` accepted, coerced
internally). The `polars.LazyFrame` is your friend when the data starts
lazy (parquet scan, sql query, csv stream); the `DataFrame` is your
friend when it's already in memory (notebook, REPL, small script).

## The four modes

`docs/examples/07_lazy_vs_sync.py` runs all four against the same scan:

### Mode A — sync / eager end-to-end

```python
df = bars()                                  # pl.LazyFrame
eager_picks = apply(score_bars(df).collect(), scan_def)
# type(eager_picks) is pl.DataFrame
```

`score_bars` still gets a `LazyFrame` (it calls `.lazy()` itself); you
collect once at the boundary, then everything downstream stays eager.

### Mode B — stay lazy end-to-end

```python
df = bars()                                  # pl.LazyFrame
lazy_plan = apply(score_bars(df), scan_def)
# type(lazy_plan) is pl.LazyFrame
result = lazy_plan.collect()                 # one .collect() at the end
```

`apply` folds into the polars plan. Add `.collect()` when the next step
needs a concrete frame (display, csv write, polars Python API that
doesn't accept a `LazyFrame`).

### Mode C — pipe into a bigger plan

```python
df = bars()
picks = apply(score_bars(df), scan_def)
joined = picks.join(other_table, on="symbol").with_columns(...)
display(joined.collect())                    # one .collect() at the very end
```

This is the lazy payoff: no intermediate materialize, no Python-side
loops, the entire pipeline optimises as a single polars query plan.

### Mode D — renamed partition column

```python
df = bars()                                  # NO rename before score_bars
scored = score_bars(df).rename({"symbol": "ticker"})
cat = {**PROPERTY_CATALOG, "ticker": {"label": "Ticker", "dtype": "str"}}
result = apply(scored, scan_def, catalog=cat, partition="ticker").collect()
```

`score_bars` hard-codes `symbol`. Rename AFTER scoring (or in a polars
expression that preserves the original column under a new alias at the
very end).

## Decision rule

The lazy contract: `.collect()` lives where the next step requires a
concrete frame. Not earlier.

- **Display / csv write / feed to a stats helper**: collect once, just
  before.
- **Pipe into another polars operation**: stay lazy.
- **Mixed**: collect at the boundary between polars and non-polars code.

Forcing `.collect()` "to be safe" loses you the optimizer and the
predicate pushdown. Forcing laziness on a notebook display loop makes
you write `display(... .collect())` everywhere. Pick the one that
matches your data's natural edges.

## What the example actually prints

```text
eager:  shape: (1, 3)   AAA  60  BASE
lazy:   shape: (1, 3)   AAA  60  BASE
piped:  shape: (1, 3)   AAA  60  1000.0
renamed: shape: (1, 2)  AAA  60
```

Same scan, four execution shapes, one `.collect()` per "edge".

## Where to next

- [Lazy contract](../explanation/lazy-contract.md) — why the contract
  exists and what the optimizer does with it
- [score_bars + stats](score-bars-stats.md) — when eager/lazy meets the
  stats helpers
- [Examples index](../reference/examples.md)
- Notebooks: [`01_first_scan.ipynb`](../notebooks/01_first_scan.ipynb)
  (Jupyter) and [`02_first_scan_marimo.py`](../notebooks/02_first_scan_marimo.py)
  (marimo) — the same scan as a runnable notebook
- [Notebooks reference](../reference/notebooks.md)
