# Use it

Bind a Polars frame once and apply text screens directly. Parse to a dict only
when you need to store a screen or run it with DuckDB.

## Install

```sh
uv add scanlang
# or: pip install scanlang
```

For Polars indicators backed by TA-Lib:

```sh
uv add 'scanlang[talib]'
# or: pip install 'scanlang[talib]'
```

For DuckDB support:

```sh
uv add 'scanlang[duckdb]'
# or: pip install 'scanlang[duckdb]'
```

Python 3.11+ and Polars 1.44+ are required.

## Data

Bars need these columns, sorted by `symbol` then `session`:

```text
symbol, session, open, high, low, close, volume
```

Rename `date` to `session` at the boundary if needed.

```python
import datetime as dt

import polars as pl

T0 = dt.date(2026, 1, 1)
n = 60
sessions = [T0 + dt.timedelta(days=i) for i in range(n)]


def rows(symbol, closes):
    return pl.DataFrame({
        "symbol": [symbol] * n,
        "session": sessions,
        "open": [c - 0.5 for c in closes],
        "high": [c + 1.0 for c in closes],
        "low": [c - 1.0 for c in closes],
        "close": closes,
        "volume": [1000.0] * n,
    })

bars = rows("AAA", [10.0 + i for i in range(n)]).vstack(
    rows("BBB", [60.0 - i for i in range(n)])
)
```

## Apply a screen

`Scan` derives the catalog from the frame, parses text with that catalog, and
reuses both for validation and execution:

```python
from scanlang import Scan

screen = "ema(20) > ema(50) and rsi(close, 14) > 70"
sl = Scan(bars)
hits = sl.apply(screen)
```

The bound state stays available:

- `sl.data`: the original `DataFrame` or `LazyFrame`;
- `sl.catalog`: columns and dtypes derived from that frame;
- `sl.result`: the latest result, preserving eager or lazy input shape.

`Scan.apply` raises `SyntaxError` for invalid text and `ValueError` for a screen
that does not validate. See [Language](language.md) for syntax and operators.

## Store a screen

Call `parse` when you need the plain dict for JSON storage or DuckDB. Pass the
same bound catalog to `validate` when the screen uses raw or custom columns:

```python
from scanlang import parse, validate

scan_def = parse(screen, catalog=sl.catalog)
assert validate(scan_def, catalog=sl.catalog) == []
```

Serialize `scan_def` with `json.dumps`; it is the stable storage form. The text
is the authoring form.

## Run it

| Data | Engine |
| --- | --- |
| a polars `DataFrame` or `LazyFrame` | polars |
| already in DuckDB | DuckDB |

A screen shared between both engines can use only indicators marked for both
polars and DuckDB on [Indicators](indicators.md). Each engine also has names
the other cannot run.

=== "polars"

    ```python
    from scanlang import Scan

    sl = Scan(bars.lazy())
    picks = sl.apply(screen)
    hits = picks.collect()
    ```

    `Scan.apply` keeps the input shape (`DataFrame` or `LazyFrame`). Collect
    when you need rows.

=== "DuckDB"

    ```python
    import duckdb
    from scanlang import validate
    from scanlang.duckdb_sql import apply_sql

    bars.write_parquet("bars.parquet")
    con = duckdb.connect()
    con.execute("CREATE VIEW bars AS SELECT * FROM 'bars.parquet'")

    assert validate(scan_def, catalog=sl.catalog, engine="duckdb") == []
    hits = apply_sql(
        con, scan_def, relation="bars", catalog=sl.catalog
    )
    ```

    `relation` is a view or table name, not a file path. `apply_sql`
    returns an eager `DataFrame` and loads DuckDB's community `talib`
    extension on the connection.

## Inspect indicator values

After `apply`, `sl.materialized` returns the original frame plus one column per
native Polars indicator in the last screen:

```python
sl.apply("ema(20) > ema(50) and rsi(close, 14) > 70")
features = sl.materialized
features.select("symbol", "session", "ema_20", "ema_50", "rsi_14")
```

Columns use `fn_n` aliases. Exact repeats collapse; if the same function and
window are applied to different inputs, only the first `fn_n` alias is kept.
Arithmetic expressions stay inside the predicate. TA-Lib seam indicators such
as `adx` and `macd` cannot be materialized because they use eager per-partition
staging; use `apply` to filter with them.

For one-off use without `Scan`:

```python
from scanlang import materialize

features = materialize(bars, screen)
```

See [Indicators](indicators.md) for names and engine availability.
Scoring, stats, and custom catalogs are on [More](more.md).
