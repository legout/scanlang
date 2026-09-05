# scanlang

Define a screen once and run it on polars or DuckDB.

[PyPI](https://pypi.org/project/scanlang/) ·
[GitHub](https://github.com/legout/scanlang)

## Install

```sh
uv add scanlang
# or: pip install scanlang
```

For Polars TA-Lib indicators or DuckDB support:

```sh
uv add 'scanlang[talib]'   # Polars TA-Lib seam
uv add 'scanlang[duckdb]'  # DuckDB backend
# pip equivalents: pip install 'scanlang[talib]' / 'scanlang[duckdb]'
```

Python 3.11+ and Polars 1.44+ are required.

## First screen

Input bars must contain `symbol`, `session`, `open`, `high`, `low`, `close`,
and `volume`, sorted by `symbol` and `session`. The examples below use an
eager Polars `DataFrame` named `bars`; call `.lazy()` when the next step can
stay lazy. [Use it](use.md#data) contains a runnable fixture.

```python
screen = "ema(20) > ema(50)"
```

=== "polars"

    ```python
    from scanlang import Scan

    sl = Scan(bars.lazy())       # catalog derived from bars
    picks = sl.apply(screen)     # text parsed internally
    result = picks.collect()
    ```

    `Scan.apply` preserves the input shape. `sl.result` holds its latest output;
    `sl.materialized` exposes native indicator values such as `ema_20` and
    `ema_50` on the original frame.

=== "DuckDB"

    ```python
    import duckdb
    from scanlang import catalog_from_schema, parse, validate
    from scanlang.duckdb_sql import apply_sql

    catalog = catalog_from_schema(bars)
    scan_def = parse(screen, catalog=catalog)
    bars.write_parquet("bars.parquet")
    con = duckdb.connect()
    con.execute("CREATE VIEW bars AS SELECT * FROM 'bars.parquet'")

    assert validate(scan_def, catalog=catalog, engine="duckdb") == []
    result = apply_sql(con, scan_def, relation="bars", catalog=catalog)
    ```

    `relation` is a table or view name, not a file path. DuckDB returns an
    eager Polars `DataFrame` and loads its community `talib` extension.

Call `parse(screen, catalog=sl.catalog)` when you need the plain dict for JSON
storage or DuckDB. See [Use it](use.md) for the complete workflow.

## Find what you need

- [Use it](use.md): prepare data, parse, validate, and run a screen.
- [Language](language.md): text syntax, operators, groups, and dict shape.
- [Indicators](indicators.md): all indicator names, signatures, and semantics.
- [More](more.md): scoring, stats, custom schemas and partitions,
  extensions, examples, and notebooks.
- [API](reference/api.md): generated callable and registry reference.
