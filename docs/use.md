# Use it

Write a screen as text, parse it to a dict, run that dict on polars or DuckDB.

## Install

```sh
uv add scanlang
# or: pip install scanlang
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

## Write a screen

```python
from scanlang import parse, validate

scan_def = parse("ema(20) > ema(50) and rsi(close, 14) > 70")
validate(scan_def)   # [] means valid
```

`validate` returns a list of errors. It does not raise. `parse` raises
`SyntaxError` if the text is not a screen. A parsed dict that is still
invalid fails in `validate`.

See [Language](language.md) for syntax, operators, and the dict shape.

## Store the dict

`parse` produced this dict. Serialize it (for example with `json.dumps`) and
persist the result, not the original text.

```python
{
    "filters": [{
        "all": [
            {
                "property": {"fn": "ema", "args": [{"col": "close"}, 20]},
                "op": ">",
                "value": {"fn": "ema", "args": [{"col": "close"}, 50]},
            },
            {
                "property": {"fn": "rsi", "args": [{"col": "close"}, 14]},
                "op": ">",
                "value": 70,
            },
        ]
    }]
}
```

## Run it

| Data | Engine |
| --- | --- |
| a polars `DataFrame` or `LazyFrame` | polars |
| already in DuckDB | DuckDB |

A screen shared between both engines can use only indicators available in the
polars column of [Indicators](indicators.md). DuckDB adds extra names, but
polars cannot run those screens.

=== "polars"

    ```python
    from scanlang import apply

    picks = apply(bars, scan_def)
    ```

    `apply` keeps the input shape (`DataFrame` or `LazyFrame`). Collect
    when you need rows.

=== "DuckDB"

    ```python
    import duckdb
    from scanlang import validate
    from scanlang.duckdb_sql import apply_sql

    bars.write_parquet("bars.parquet")
    con = duckdb.connect()
    con.execute("CREATE VIEW bars AS SELECT * FROM 'bars.parquet'")

    assert validate(scan_def, engine="duckdb") == []
    hits = apply_sql(con, scan_def, relation="bars")
    ```

    `relation` is a view or table name, not a file path. `apply_sql`
    returns an eager `DataFrame` and loads DuckDB's community `talib`
    extension on the connection.

See [Indicators](indicators.md) for names and engine availability.
Scoring, stats, and custom catalogs are on [More](more.md).
