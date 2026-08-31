# Lazy contract

`apply` and `score_bars` are lazy-in / lazy-out when given a
`LazyFrame` and eager-in / eager-out when given a `DataFrame`. This is
deliberate — it lets you compose a scan into a bigger polars plan
without forcing a `.collect()` round-trip.

## The contract

```python
def apply(frame, scan_def, *, catalog=PROPERTY_CATALOG, partition="symbol"):
    out = frame.filter(compile(scan_def, catalog=catalog, partition=partition))
    ...
    return out
```

That's the whole story at the function level. The contract:

1. **Shape-preserving**: `DataFrame` in -> `DataFrame` out,
   `LazyFrame` in -> `LazyFrame` out.
2. **No `.collect()` unless asked**: you control where the materialization
   happens.
3. **`compile` returns a `pl.Expr`, not a frame**: fold it into any
   other polars expression (`filter`, `with_columns`, custom lambdas).

## Why this matters

Forcing `.collect()` "to be safe" loses you:

- **Predicate pushdown**: polars won't push your `filter` past a
  parquet scan if the filter has been collected into a Python-side list.
- **Plan optimization**: two `filter`s + one `select` become one
  optimized query, not three Python-side round-trips.
- **Memory**: 10 million rows fit as a `LazyFrame` (cheap), not as a
  `DataFrame` (not cheap).

Forcing laziness on a notebook display loop makes you write
`display(... .collect())` everywhere. That's a tax on small data, not
a gain.

The lazy contract says: put `.collect()` where the next step actually
needs a concrete frame.

## Where `.collect()` belongs

| Next step | Where `.collect()` lives |
| --- | --- |
| `display()` / `print()` | just before |
| `to_csv()` / `to_parquet()` | just before |
| feed to `backtest_summary` (pure Python) | just before |
| feed to another `polars` operation | never; stay lazy |
| a `for` loop over rows | just before, accept the cost |

There is no wrong answer; there is only "where the data flow has a
natural boundary". One `.collect()` per boundary.

## What `compile` gives you

`compile(scan_def) -> pl.Expr` returns a single polars predicate —
fold it into whatever you want:

```python
import polars as pl
from scanlang import compile

pred = compile(scan_def)
# any frame, any source, any shape
df.filter(pred).select(...).collect()
lf.filter(pred).group_by("symbol").agg(...).collect()
sql_result = pl.SQLContext({"bars": lf}).execute(
    f"SELECT * FROM bars WHERE {pred} ORDER BY score DESC LIMIT 5"
).collect()
```

This is the same `pl.Expr` regardless of where you use it. No "compile
to polars" vs "compile to SQL" split — there's only one backend (see
[Why no duckdb](why-no-duckdb.md)).

## What `apply` does NOT do

- **No sorting**: caller sorts the frame. The contract is
  `(partition, time)` ascending. Sorts make scan results unstable
  otherwise.
- **No deduplication**: caller dedupes before scoring if needed.
- **No caching**: a `.collect()` followed by another polars query is on
  you. Use `.lazy().cache()` if you want the lazy caching semantics.

## Worked examples

`docs/examples/07_lazy_vs_sync.py` runs all four modes (eager, lazy,
piped, renamed). Output:

```text
eager:   shape: (1, 3)  AAA  60  BASE
lazy:    shape: (1, 3)  AAA  60  BASE
piped:   shape: (1, 3)  AAA  60  1000.0
renamed: shape: (1, 2)  AAA  60
```

Same scan, four execution shapes.

## Where to next

- [Eager vs lazy frames](../how-to/eager-frames.md) — the four modes
  with runnable code
- [IR design](ir-design.md) — the contract as part of the IR
- [Why no duckdb](why-no-duckdb.md) — the backend choice
