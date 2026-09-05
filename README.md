# scanlang

[![PyPI](https://img.shields.io/pypi/v/scanlang)](https://pypi.org/project/scanlang/)
[![Python](https://img.shields.io/pypi/pyversions/scanlang)](https://pypi.org/project/scanlang/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Status: pre-alpha](https://img.shields.io/badge/status-pre--alpha-orange)](https://pypi.org/project/scanlang/)

Screener DSL and scan compiler for Polars and DuckDB.

Write screens as text, bind a Polars frame once with `Scan`, or parse them into
plain dicts for storage and DuckDB. Filters preserve eager/lazy shape and
window semantics reset per partition.

Status: **v0.4, pre-alpha**. The IR is frozen (additive changes only). The
public docs are at [legout.github.io/scanlang](https://legout.github.io/scanlang/).

## Install

```sh
uv add scanlang              # or: pip install scanlang
```

Requires Python >= 3.11 and polars >= 1.44. Add the optional extras only for
the path you use:

```sh
uv add 'scanlang[talib]'   # Polars indicators backed by official TA-Lib
uv add 'scanlang[duckdb]'  # DuckDB >= 1.5 backend
```

The DuckDB backend loads the community `talib` extension itself.

## Quickstart

```python
import polars as pl
from scanlang import Scan

bars = pl.DataFrame({
    "symbol": ["AAA"] * 60,
    "session": pl.date_range(
        pl.date(2026, 1, 1), pl.date(2026, 3, 1), interval="1d", eager=True
    ),
    "open":   [10 + i for i in range(60)],
    "high":   [11 + i for i in range(60)],
    "low":    [9 + i for i in range(60)],
    "close":  [10 + i for i in range(60)],
    "volume": [1000.0] * 60,
})

sl = Scan(bars)  # catalog derived once
hits = sl.apply("ema(20) > ema(50)")
features = sl.materialized  # bars + ema_20 + ema_50
```

`Scan.apply` accepts text or a parsed dict. `sl.data`, `sl.catalog`, and
`sl.result` expose the bound frame, derived catalog, and latest result.
`materialized` exposes native Polars indicator nodes from the last screen.
See [Use it](https://legout.github.io/scanlang/use/) for the complete workflow.

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

Two backends consume the same parsed dict. Their registries overlap; neither is
a strict superset. Check the
[indicator table](https://legout.github.io/scanlang/indicators/) before sharing a
screen across engines.

| Engine | Backend module | When to use it |
| --- | --- | --- |
| polars (default) | `scanlang.apply` | Frame is `pl.DataFrame` / `pl.LazyFrame`; notebook, REPL, small script; lazy pipeline into more polars ops. |
| duckdb (opt-in) | `scanlang.duckdb_sql.apply_sql` | Data already lives in duckdb; need `ht_trendline` / `stoch_k` / `stoch_d` (duckdb-only talib names); full-universe scans where the benchmark shows SQL pushdown beats polars. |

The duckdb backend:

```python
import duckdb
from scanlang import parse, validate
from scanlang.duckdb_sql import apply_sql

con = duckdb.connect()
con.execute("CREATE VIEW bars AS SELECT * FROM 'daily_bars.parquet'")

scan_def = {
    **parse("macd(12) > 0 and close > 4"),
    "order_by": [{"property": "close", "dir": "desc"}],
    "limit": 20,
}
assert validate(scan_def, engine="duckdb") == []
hits = apply_sql(con, scan_def, relation="bars")
```

`relation` is a table or view name, not a file path. `apply_sql` loads the
community `talib` extension on the connection.

## Docs

The public site is intentionally six pages:

- [Home](https://legout.github.io/scanlang/): install and first screen
- [Use it](https://legout.github.io/scanlang/use/): `Scan`, persistence, both engines, materialized indicators
- [Language](https://legout.github.io/scanlang/language/): text syntax and stored dict shape
- [Indicators](https://legout.github.io/scanlang/indicators/): names, signatures, engine availability
- [API](https://legout.github.io/scanlang/reference/api/): callable reference
- [More](https://legout.github.io/scanlang/more/): scoring, stats, custom schemas, extensions, examples

Build it locally with `uv run zensical build --clean --strict`.

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