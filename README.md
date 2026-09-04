# scanlang

[![PyPI](https://img.shields.io/pypi/v/scanlang)](https://pypi.org/project/scanlang/)
[![Python](https://img.shields.io/pypi/pyversions/scanlang)](https://pypi.org/project/scanlang/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Status: pre-alpha](https://img.shields.io/badge/status-pre--alpha-orange)](https://pypi.org/project/scanlang/)

Screener DSL and scan compiler: signal definitions to polars pushdown filters.

A scan definition is a plain dict (JSON from a UI, a Python literal from a notebook)
that `scanlang` compiles into one validated polars predicate. Nothing is
string-interpolated, so there is no injection surface. Filters run on eager
`DataFrame` or lazy `LazyFrame`; window semantics are computed per partition.

Status: **v0.3, pre-alpha**. The IR is frozen (additive changes only). For the
design rationale, see [docs/explanation/ir-design.md](docs/explanation/ir-design.md).

## Install

```sh
uv add scanlang              # or: pip install scanlang
```

Requires Python >= 3.11 and polars >= 1.44. The optional `duckdb` extra
(`uv add 'scanlang[duckdb]'` or `pip install 'scanlang[duckdb]'`) pulls
duckdb >= 1.5 and enables the [`scanlang.duckdb_sql`](docs/reference/duckdb-backend.md)
backend, which compiles the same IR to parameterized SQL against the
community talib extension. The optional `talib` extra remains a
placeholder for value-parity indicator helpers.

## Quickstart (eager)

Copy-pasteable end-to-end. Defines its own small OHLCV frame, scores it,
applies a scan, and prints the picks. The full annotated walkthrough lives in
[`docs/examples/01_quickstart.py`](docs/examples/01_quickstart.py).

```python
import polars as pl
from scanlang import apply, score_bars, validate

bars = pl.DataFrame({
    "symbol": ["AAA"] * 30 + ["BBB"] * 30,
    "session": pl.date_range(pl.date(2026, 1, 1), pl.date(2026, 3, 1), interval="1d", eager=True)[:30].to_list() * 2,
    "open":   [10 + i for i in range(30)] + [60 - i for i in range(30)],
    "high":   [11 + i for i in range(30)] + [61 - i for i in range(30)],
    "low":    [9  + i for i in range(30)] + [59 - i for i in range(30)],
    "close":  [10 + i for i in range(30)] + [60 - i for i in range(30)],
    "volume": [1000.0] * 60,
})

scored = score_bars(bars).collect()                    # LazyFrame -> DataFrame at the edge
scan_def = {
    "filters":  [{"property": "score", "op": ">=", "value": 40}],
    "order_by": [{"property": "score", "dir": "desc"}],
    "limit":    5,
}
validate(scan_def)                                      # [] when valid; never raises
print(apply(scored, scan_def).select("symbol", "score", "phase"))
```

The full quickstart script (lazy in, collect at the edge) is
[`docs/examples/01_quickstart.py`](docs/examples/01_quickstart.py). Side-by-side
eager/lazy/piped/renamed modes: [`docs/examples/07_lazy_vs_sync.py`](docs/examples/07_lazy_vs_sync.py).

## Quickstart (text DSL)

Prefer a one-liner over the dict? `parse` turns human syntax into the same
scan dict. The golden-cross form uses `cross_above` so the signal only fires
when the 20-EMA actually crosses above the 50-EMA:

```python
from scanlang import parse, validate

ir = parse("cross_above(ema(20), ema(50)) and rsi(close, 14) > 70")
validate(ir)                                            # [] when valid
```

`ema(20)` and `sma(20)` imply the `close` column; indicators that take an
expression like `rsi` need it spelled out (`rsi(close, 14)`). Full grammar and
operator reference: [`docs/reference/operators.md`](docs/reference/operators.md),
[`docs/how-to/scan-from-text.md`](docs/how-to/scan-from-text.md).

## Eager vs lazy at a glance

| Mode | In | Out | When to use it |
| --- | --- | --- | --- |
| Sync / eager | `pl.DataFrame` | `pl.DataFrame` | Notebook, REPL, small script. Collect at the data edge, then everything stays eager. |
| Lazy end-to-end | `pl.LazyFrame` | `pl.LazyFrame` | Pipeline that pipes into more polars ops. Add `.collect()` once at the end. |
| Mixed | `pl.LazyFrame` (start) -> `pl.DataFrame` (collect once) | `pl.DataFrame` | One `.collect()` at the polars -> non-polars boundary. Don't collect "to be safe" earlier; you lose predicate pushdown. |

`apply` is shape-preserving (eager in -> eager out, lazy in -> lazy out).
`score_bars` is always lazy out: it returns a `LazyFrame` so it can fold into a
bigger polars plan; call `.collect()` at the edge if you want a `DataFrame`.
Full guide: [`docs/how-to/eager-frames.md`](docs/how-to/eager-frames.md).

## Engines at a glance

Two backends consume the same scan-def dict. Pick per data edge.

| Engine | Backend module | When to use it |
| --- | --- | --- |
| polars (default) | `scanlang.apply` | Frame is `pl.DataFrame` / `pl.LazyFrame`; notebook, REPL, small script; lazy pipeline into more polars ops. |
| duckdb (opt-in) | `scanlang.duckdb_sql.apply_sql` | Data already lives in duckdb; need `ht_trendline` / `stoch_k` / `stoch_d` (duckdb-only talib names); full-universe scans where the benchmark shows SQL pushdown beats polars. |

The duckdb backend:

```python
import duckdb
from scanlang import validate
from scanlang.duckdb_sql import apply_sql

con = duckdb.connect()
con.execute("CREATE VIEW bars AS SELECT * FROM 'daily_bars.parquet'")

scan_def = {
    "filters": [
        {"property": {"fn": "macd", "args": [12]}, "op": ">", "value": 0},
        {"property": "score", "op": ">=", "value": 40},
    ],
    "order_by": [{"property": "score", "dir": "desc"}],
    "limit": 20,
}
validate(scan_def, engine="duckdb")     # [] when valid
hits = apply_sql(con, scan_def, relation="bars")
```

`apply_sql` calls `INSTALL talib FROM community; LOAD talib` on the
connection itself. The full module surface, lowering rules, and
`SQL_INDICATORS` registry:
[`docs/reference/duckdb-backend.md`](docs/reference/duckdb-backend.md).
Install and connect walk-through:
[`docs/how-to/duckdb-backend.md`](docs/how-to/duckdb-backend.md).

## Docs (Diataxis)

Build the site locally with `uv run --group docs zensical build` (config:
[`zensical.toml`](zensical.toml)). Follows the [Diataxis](https://diataxis.fr/)
split:

- [Tutorials](docs/tutorials/first-scan.md) - learning-oriented; get to a first scan.
- [How-to guides](docs/how-to/) - task-oriented; solve a specific problem (custom
  catalog/partition, extending indicators, scan from text, score + stats,
  duckdb backend).
- [Explanation](docs/explanation/) - understanding-oriented; IR design, lazy
  contract, null semantics, validation split, and the SQL backend that
  supersedes the "why no duckdb" verdict.
- [Reference](docs/reference/) - information-oriented; API, operators,
  indicators, examples index, notebooks, IR freeze, duckdb backend.

Notebooks: [`01_first_scan.ipynb`](docs/notebooks/01_first_scan.ipynb) (Jupyter)
and [`02_first_scan_marimo.py`](docs/notebooks/02_first_scan_marimo.py) (marimo)
- the same first scan as a runnable notebook. See
[`docs/reference/notebooks.md`](docs/reference/notebooks.md).

## Development

```sh
uv sync --group docs                              # create .venv with zensical
.venv/bin/python -m pytest tests/ -q              # tests
.venv/bin/python -m ruff check src tests          # lint
.venv/bin/zensical serve                          # live-reload docs at :8000
.venv/bin/zensical build                          # static build -> site/
```

## License

[MIT](LICENSE).