# scanlang

Define a screen once and run it on polars or DuckDB.

[PyPI](https://pypi.org/project/scanlang/) ·
[GitHub](https://github.com/legout/scanlang)

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

## First screen

Input bars must contain `symbol`, `session`, `open`, `high`, `low`, `close`,
and `volume`, sorted by `symbol` and `session`. The examples below use an
eager Polars `DataFrame` named `bars`; call `.lazy()` when the next step can
stay lazy. [Use it](use.md#data) contains a runnable fixture.

```python
from scanlang import parse, validate

scan_def = parse("ema(20) > ema(50)")
assert validate(scan_def) == []
```

=== "polars"

    ```python
    from scanlang import apply

    picks = apply(bars.lazy(), scan_def)
    result = picks.collect()
    ```

    `apply` preserves the input shape: a `DataFrame` stays eager and a
    `LazyFrame` stays lazy.

=== "DuckDB"

    ```python
    import duckdb
    from scanlang import validate
    from scanlang.duckdb_sql import apply_sql

    bars.write_parquet("bars.parquet")
    con = duckdb.connect()
    con.execute("CREATE VIEW bars AS SELECT * FROM 'bars.parquet'")

    assert validate(scan_def, engine="duckdb") == []
    result = apply_sql(con, scan_def, relation="bars")
    ```

    `relation` is a table or view name, not a file path. DuckDB returns an
    eager Polars `DataFrame` and loads its community `talib` extension.

`scan_def` is a plain dict, so store it as JSON when a screen needs to be
persisted. See [Use it](use.md) for the complete workflow.

## Find what you need

- [Use it](use.md): prepare data, parse, validate, and run a screen.
- [Language](language.md): text syntax, operators, groups, and dict shape.
- [Indicators](indicators.md): all indicator names, signatures, and semantics.
- [More](more.md): scoring, stats, custom schemas and partitions,
  extensions, examples, and notebooks.
- [API](reference/api.md): generated callable and registry reference.
