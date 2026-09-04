# API

The task pages explain how to use these objects:
[Use it](../use.md), [Language](../language.md), [Indicators](../indicators.md),
and [More](../more.md). This page is the callable reference.

## Compile and run polars scans

```python
from scanlang import apply, compile, validate
```

::: scanlang.compiler.compile
    options:
      show_root_heading: true
      heading_level: 3

::: scanlang.compiler.validate
    options:
      show_root_heading: true
      heading_level: 3

::: scanlang.compiler.apply
    options:
      show_root_heading: true
      heading_level: 3

## Catalogs

Use `PROPERTY_CATALOG` for scored frames or
`catalog_from_schema` for raw/custom frames. See [Custom schemas and
partitions](../more.md#custom-schemas-and-partitions).

::: scanlang.compiler.catalog_from_schema
    options:
      show_root_heading: true
      heading_level: 3

::: scanlang.compiler.PROPERTY_CATALOG
    options:
      show_root_heading: true
      heading_level: 3

## Text DSL

`parse` returns the scan-definition dict consumed by both engines. See
[Language](../language.md) for syntax and validation rules.

::: scanlang.dsl.parse
    options:
      show_root_heading: true
      heading_level: 3

## Scoring and stats

See [Scoring](../more.md#scoring) and [Stats](../more.md#stats) for
examples and defaults.

::: scanlang.scoring.score_bars
    options:
      show_root_heading: true
      heading_level: 3

::: scanlang.stats.HORIZONS
    options:
      show_root_heading: true
      heading_level: 3

::: scanlang.stats.forward_stats
    options:
      show_root_heading: true
      heading_level: 3

::: scanlang.stats.backtest_summary
    options:
      show_root_heading: true
      heading_level: 3

## Registries

See [Indicators](../indicators.md) for availability, signatures, and semantics.

::: scanlang.indicators.INDICATORS
    options:
      show_root_heading: true
      heading_level: 3

## DuckDB

`apply_sql` runs a scan on an existing DuckDB connection and returns an eager
Polars `DataFrame`. Pass a table or view name as `relation`; it is not a file
path. See [Use it](../use.md#run-it) for setup.

::: scanlang.duckdb_sql.compile_sql
    options:
      show_root_heading: true
      heading_level: 3

::: scanlang.duckdb_sql.apply_sql
    options:
      show_root_heading: true
      heading_level: 3

::: scanlang.duckdb_sql.SQL_INDICATORS
    options:
      show_root_heading: true
      heading_level: 3
