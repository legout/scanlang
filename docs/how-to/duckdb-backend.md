# Use the duckdb backend

The default scanlang path is `apply(frame, scan_def)` over polars. The
`duckdb_sql` module is an opt-in second backend for when the data
already lives in duckdb, the indicator you need is in the talib
extension, or the scan is large enough that SQL pushdown beats polars
on the full universe.

## Install

The duckdb backend is a separate extra:

```sh
uv add 'scanlang[duckdb]'              # or: pip install 'scanlang[duckdb]'
```

Pulls `duckdb >= 1.5`. The polars-only path does not need it.

The community talib extension is **not** a Python package — it ships
with duckdb itself and is loaded on the connection by `apply_sql`
itself (`INSTALL talib FROM community; LOAD talib`, idempotent).

## Connect

`apply_sql` accepts any open `duckdb` connection with the table or view
you want to scan already attached:

```python
import duckdb

con = duckdb.connect()
con.execute("CREATE VIEW bars AS SELECT * FROM 'daily_bars.parquet'")
```

For a hotlake-style attach (the marketdata-screens case), register
once and reuse the connection:

```python
con = duckdb.connect()
con.execute("ATTACH 'lake.duckdb' AS lake (READ_ONLY)")
con.execute("USE lake")
con.execute("CREATE VIEW bars AS SELECT * FROM daily_bars")
```

For a remote lake over HTTP, register a view that points at a public
parquet file:

```python
con.execute(
    "CREATE VIEW bars AS SELECT * FROM "
    "'https://example.com/daily_bars.parquet'"
)
```

## Run a scan

Same scan-def dict as the polars backend; only the executor differs:

```python
from scanlang.duckdb_sql import apply_sql

scan_def = {
    "filters": [
        {"property": {"fn": "sma", "args": [{"col": "close"}, 50]},
         "op": ">", "value": {"col": "close"}},
        {"property": "score", "op": ">=", "value": 40},
    ],
    "order_by": [{"property": "score", "dir": "desc"}],
    "limit": 20,
}

hits = apply_sql(con, scan_def, relation="bars")
print(hits)
```

Returns an eager `pl.DataFrame`. `apply_sql` calls
`INSTALL talib FROM community` and `LOAD talib` on the connection
before executing (no-op after the first call). `validate()` runs
first, so validation errors match the polars backend's error strings.

If the scan uses a talib-only name (`macd`, `bbands_upper`,
`bbands_lower`, `adx`, `aroon`, `cdlengulfing`, `ht_trendline`), pass
`engine="duckdb"` to `validate`:

```python
from scanlang import validate

errs = validate(scan_def, engine="duckdb")   # [] when valid
```

## Run the same scan on both engines

The polars backend and the duckdb backend consume the same IR dict —
compile once, run both ways, compare:

```python
import polars as pl
from scanlang import apply, validate
from scanlang.duckdb_sql import apply_sql

scan_def = {"filters": [
    {"property": {"fn": "sma", "args": [{"col": "close"}, 20]},
     "op": ">", "value": {"col": "close"}},
]}

validate(scan_def, engine="duckdb")   # accept talib-only fns if present

df = pl.read_parquet("daily_bars.parquet")
polars_hits = apply(df, scan_def).select("symbol", "session")
duckdb_hits = apply_sql(con, scan_def, relation="bars").select("symbol", "session")
```

For sma-only scans on complete frames, the hit sets are identical
(native window lowering is exact on both engines). For ema/rsi/atr
scans, values agree to <0.01 at mature bars (after ~4n); hit sets can
still differ in the warm-up window. See [indicators reference](indicators.md)
for the warm-up contract.

## When to pick which

| Situation | Backend | Why |
| --- | --- | --- |
| Data is already a polars `DataFrame` / `LazyFrame` | polars | No translation cost; `apply` folds into the lazy plan |
| Data is already in a duckdb lake or warehouse | duckdb | Push the scan into the existing connection; only hits cross the wire |
| You need `macd`, `bbands`, `adx`, `aroon`, `cdlengulfing`, or `ht_trendline` | duckdb | These have no polars-builder equivalent |
| Full-universe scan against a parquet lake (~25M rows) | duckdb | `t_*` scalar form beats polars native on the benchmark (3.8 s vs 6.0 s) |
| Notebooks, REPL, small frame in memory | polars | Eager start, no extension to install |

The polars backend stays the default; the duckdb backend is opt-in per
import and per call. Both engines share the IR, the validation surface,
and the indicator registry contract.

## Where to next

- [duckdb backend reference](../reference/duckdb-backend.md) — the
  full module surface and lowering rules
- [Indicators reference](../reference/indicators.md) — engine
  availability per name (the talib-only ones are duckdb-only)
- [Why no duckdb](../explanation/why-no-duckdb.md) — the verdict the
  backend now supersedes