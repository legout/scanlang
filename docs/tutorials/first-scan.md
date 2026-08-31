# First scan in 5 minutes

Goal: go from "I have some OHLCV bars" to "I have the symbols my scan
picks" — in five minutes. No background theory, no exhaustive options —
just the happy path with one decision you'll need to make along the way.

## What you need

- A `polars.DataFrame` (or `LazyFrame`) with the standard OHLCV columns:
  `symbol, session, open, high, low, close, volume`
- The frame sorted `(symbol, session)` ascending — `scanlang` doesn't
  sort for you, the caller contract assumes you already did

If your columns are named differently, rename them at your edge
(`df.rename({"date": "session"})`) before scoring.

Want a runnable version of this tutorial? Two notebooks cover the same
happy path on the same fixture:

- [`01_first_scan.ipynb`](../notebooks/01_first_scan.ipynb) —
  Jupyter / nbconvert-friendly
- [`02_first_scan_marimo.py`](../notebooks/02_first_scan_marimo.py) —
  marimo-friendly (reactive cells)

Both are checked in and CI-verified.

## Step 1 — Build a tiny OHLCV frame

Everything below uses the same two-symbol fixture as
`docs/examples/*.py` and the notebooks, so you can copy this whole
page into a REPL or a single Python file:

```python
import datetime as dt

import polars as pl

from scanlang import apply, score_bars, validate

# 60 trading days, two symbols, deterministic OHLCV
T0 = dt.date(2026, 1, 1)


def bars() -> pl.DataFrame:
    n = 60
    sessions = [T0 + dt.timedelta(days=i) for i in range(n)]

    def rows(sym, closes):
        return {
            "symbol": [sym] * n,
            "session": sessions,
            "open": [c - 0.5 for c in closes],
            "high": [c + 1.0 for c in closes],
            "low": [c - 1.0 for c in closes],
            "close": closes,
            "volume": [1000.0] * n,
        }

    uptrend = [10.0 + i for i in range(n)]
    downtrend = [60.0 - i for i in range(n)]
    return pl.DataFrame(rows("AAA", uptrend)).vstack(
        pl.DataFrame(rows("BBB", downtrend))
    )
```

If you have your own data, skip the fixture and use your `df` /
`bars()` instead — the rest of the page works unchanged.

## Step 2 — Score the bars

`score_bars` turns every symbol's latest bar into one scored row plus
a phase label (`CLIMAX`, `TREND`, `BREAKOUT`, `BASE`, `NONE`). It is
lazy in and lazy out: collect at the boundary where you actually need
a frame.

```python
df = bars().lazy()           # or your own LazyFrame
scored = score_bars(df).collect()
print(scored.select("symbol", "score", "phase"))
```

`score_bars` drops symbols with too little history (default
`min_bars=30`) or whose latest bar is too stale (`freshness_days=5`),
so the output has exactly one row per recent symbol.

## Step 3 — Write a scan definition as a dict

A scan definition is a plain dict. The only required key is `filters`;
`order_by` and `limit` are optional.

```python
scan_def = {
    "filters": [
        {"property": "score", "op": ">=", "value": 40},
        {"property": "phase", "op": "in", "value": ["BREAKOUT", "TREND"]},
    ],
    "order_by": [{"property": "score", "dir": "desc"}],
    "limit": 5,
}

print(validate(scan_def))   # []  — empty list means valid
```

If `validate` returns anything other than `[]`, the dict has a
structural problem (unknown property, wrong operator, mismatched
dtype). The error strings are keyed to the offending field, so a UI
can highlight them inline. No `ValueError` is raised here;
`compile`/`apply` do raise.

## Step 4 — Apply it

`apply` is `filter + order_by + limit` on a frame — eager or lazy,
mirrors the input shape.

```python
picks = apply(scored, scan_def)
print(picks.select("symbol", "score", "phase"))
```

If you skipped the `.collect()` after `score_bars`, that's fine:
`apply` is shape-preserving, so `picks` is still a `LazyFrame`. Add
one `.collect()` at your edge (display, csv write, feed to
`backtest_summary`) and nowhere else.

## Decision point: where does `.collect()` live?

The "lazy contract" (see [Explanation](../explanation/lazy-contract.md))
is that you collect once, at the boundary, not earlier. Three
reasonable patterns:

```python
# pattern A — eager all the way (notebook, REPL, small data)
picks = apply(score_bars(df).collect(), scan_def)         # DataFrame out

# pattern B — stay lazy until the last step
picks = apply(score_bars(df), scan_def).collect()         # one collect

# pattern C — pipe into a bigger plan
picks = apply(score_bars(df), scan_def)
joined = picks.join(other_table, on="symbol").with_columns(...)  # lazy
display(joined.collect())                                    # one collect
```

There is no wrong choice between A, B, and C — pick the one where
your `.collect()` count matches your data's natural edges.

## Where to next

- Notebooks: [`01_first_scan.ipynb`](../notebooks/01_first_scan.ipynb)
  or [`02_first_scan_marimo.py`](../notebooks/02_first_scan_marimo.py)
  — the same scan, ready to execute
- Tutorial: [DSL basics](dsl-basics.md) — all the operators, operands,
  and groups you'll need beyond the happy path
- How-to: [Eager vs lazy frames](../how-to/eager-frames.md) — the
  four operating modes with runnable examples
- Explanation: [IR design](../explanation/ir-design.md) — the exact
  contract if you want to write the dict by hand or generate it from a UI