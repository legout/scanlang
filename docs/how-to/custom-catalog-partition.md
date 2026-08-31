# Custom catalog + partition

`scanlang` is not tied to `score_bars` output. Any `DataFrame` /
`LazyFrame` that you can describe with a `dict[str, {"label", "dtype"}]`
catalog will compile and run.

Two knobs control this:

- `catalog`: a dict that maps property name -> `{label, dtype}`. The
  default is `PROPERTY_CATALOG`, which mirrors `score_bars` output.
- `partition`: the column that defines a "symbol". Window ops
  (indicators, crosses) compute per partition, so 10 symbols or 10,000
  behave the same. Default `"symbol"`.

## Derive a catalog from a polars schema

```python
import polars as pl
from scanlang import apply, catalog_from_schema, validate

lf = bars  # your LazyFrame, any schema

cat = catalog_from_schema(lf)
# -> {"session": {"label": "session", "dtype": "date"},
#     "close":   {"label": "close",   "dtype": "float"},
#     ...}      # only mappable dtypes (Bool, Int*, UInt*, Float*, String, Date, Datetime)
```

Unmappable dtypes are skipped, not raised on. Add them by hand if you
need them validated by dtype.

## Point `partition` at your own group column

If your group column is `ticker`, not `symbol`:

```python
lf = bars.rename({"symbol": "ticker"})            # rename at your edge
cat = catalog_from_schema(lf)                      # -> "ticker": {"label": "ticker", "dtype": "str"}
scan = {"filters": [{"property": "close", "op": ">", "value": 50}]}
apply(lf, scan, catalog=cat, partition="ticker")   # window ops now per-ticker
```

`partition` is the string passed to polars `.over(partition)` for every
window op. Cross ops and indicators (RSI, EMA, …) all respect it.

## When your catalog needs to merge defaults

`PROPERTY_CATALOG` covers the columns `score_bars` produces (`score`,
`phase`, `vol_ratio`, `rsi`, …). If your frame is the output of
`score_bars` plus your own derived columns, merge:

```python
from scanlang import PROPERTY_CATALOG, catalog_from_schema

scored = score_bars(bars)
scored = scored.with_columns(spread=pl.col("close") - pl.col("sma_50"))
cat = {**PROPERTY_CATALOG, **catalog_from_schema(scored)}
```

The `**scored_schema` overrides win on conflict, so the merge keeps all
of `PROPERTY_CATALOG` and adds the new columns.

## Validating a custom catalog

```python
validate(scan_def, catalog=cat)   # uses your catalog's dtype rules
```

Anything in `scan_def` whose `property` is not in `cat` returns
`"unknown property: '...'"`. Anything in `in`/`between`/`contains` whose
values don't match the catalog dtype returns `"... values must be
<dtype> values"`.

## A worked end-to-end

`docs/examples/04_custom_partition_and_registry.py`:

```python
from scanlang import INDICATORS, apply, catalog_from_schema, validate

# frame uses `ticker` instead of `symbol`
lf = bars  # rename already applied
cat = catalog_from_schema(lf)

# rsi(14) above 70 per ticker
rsi_hot = {
    "filters": [{
        "property": {"fn": "rsi", "args": [{"col": "close"}, 14]},
        "op": ">", "value": 70,
    }],
}
hot = apply(lf, rsi_hot, catalog=cat, partition="ticker").collect()
```

`run: .venv/bin/python docs/examples/04_custom_partition_and_registry.py`

## Common pitfalls

- **Renaming BEFORE score_bars**: `score_bars` hard-codes
  `symbol, session, open, high, low, close, volume`. Rename AFTER scoring,
  or you'll get a `ColumnNotFoundError` at collect time.
- **Adding a column to the frame but not the catalog**: `apply` validates
  against `catalog`, not the frame schema. A scan that uses a new column
  must have it in `catalog`, even if the frame has it.
- **Forgetting `partition`**: window ops default to `.over("symbol")`. If
  your group column is different, every rolling calculation aggregates
  across the ENTIRE frame instead of per symbol.

## Where to next

- [Extend INDICATORS](extend-indicators.md) — add custom indicators
  (z-score, etc.) to the registry
- [API reference](../reference/api.md) — `apply`, `compile`, `validate`
  signatures
- [Examples index](../reference/examples.md) — every runnable example
