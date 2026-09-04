# More

Secondary features and runnable examples live here. Start with [Use it](use.md)
for the main scan path.

## Scoring

`score_bars` turns OHLCV history into one scored row per symbol. It returns a
lazy frame and expects data sorted by `symbol` and `session`.

```python
from scanlang import apply, score_bars

scan_def = {
    "filters": [{"property": "score", "op": ">=", "value": 40}],
    "order_by": [{"property": "score", "dir": "desc"}],
    "limit": 5,
}

picks = apply(score_bars(bars), scan_def).collect()
```

Defaults:

- at least 30 bars per symbol;
- the latest bar must be within five days of the frame's latest session.

Change them when needed:

```python
scored = score_bars(bars, min_bars=50, freshness_days=2)
```

The output contains the fields in `PROPERTY_CATALOG`, plus `bars`, the number
of input bars for each symbol. In full, the output fields are `symbol`,
`session`, `close`, `score`, `phase`, `vol_ratio`, `atr_ratio`, `rsi`,
`acc_score`, `spring`, `ema_stack`, `recent_cross`, `upper_wick_pct`,
`near_52w_low`, and `bars`. See [API](reference/api.md) for the complete
catalog and scoring signature.

## Stats

`forward_stats` calculates percentage returns after a past scan run. The entry
is the first session on or after `ran_on`; a missing forward window is `None`.

`forward_stats(sessions, closes, ran_on)` takes an ascending list of trading
session dates, an equal-length list of close prices, and the scan date. It
returns percentage returns for the configured horizons.

```python
from scanlang import forward_stats

stats = forward_stats(sessions, closes, ran_on)
# {"5d": 1.2, "10d": 3.4, "20d": 7.1}
```

`backtest_summary` aggregates those results:

```python
from scanlang import backtest_summary

summary = backtest_summary(runs, stats_fn)
for label, hit_rate, average_return, sample_count in summary["horizons"]:
    print(label, hit_rate, average_return, sample_count)
```

`stats_fn` has the shape `(symbol, ran_on) -> dict | None`. Each run is a dict
with an ISO `ran_at` timestamp and a `symbols` list:

```python
runs = [{"ran_at": "2026-01-01T22:00", "symbols": ["AAA", "BBB"]}]
```

Each horizon is a
`(label, hit_rate_percent, average_return_percent, sample_count)` tuple.

## Custom schemas and partitions

For raw or custom frames, derive a catalog from the schema and pass the column
that defines a partition:

```python
from scanlang import apply, catalog_from_schema, validate

bars = bars.rename({"symbol": "ticker"})
catalog = catalog_from_schema(bars)
scan_def = {
    "filters": [{"property": "close", "op": ">", "value": 50}]
}

hits = apply(bars, scan_def, catalog=catalog, partition="ticker")
```

Windows and crossovers now reset at `ticker`. Keep the frame sorted by
partition and session. `catalog_from_schema` maps Polars string, numeric,
boolean, date, and datetime fields; skipped dtypes can be added by hand.

To combine scored fields with custom fields:

```python
from scanlang import PROPERTY_CATALOG

catalog = {**PROPERTY_CATALOG, **catalog_from_schema(scored)}
```

Validate against the same catalog before applying the screen:

```python
errors = validate(scan_def, catalog=catalog)
```

## Extend a registry

Add a polars indicator as one registry entry:

```python
import polars as pl

from scanlang import INDICATORS


def stdev(e: pl.Expr, n: int, partition: str) -> pl.Expr:
    return e.rolling_std(n).over(partition)


if "stdev" not in INDICATORS:
    INDICATORS["stdev"] = (("expr", "int"), stdev, ())
```

The tuple is `(arg_spec, builder, required_cols)`:

- `arg_spec` uses `"expr"` for an operand and `"int"` for an integer `>= 1`;
- `builder` receives the parsed arguments and `partition`, and returns a
  `pl.Expr`;
- `required_cols` lists catalog columns required by the builder.

Builders must put `.over(partition)` on every window expression. Use an
idempotent insertion guard when the module can be imported more than once, and
test warm-up rows, partition boundaries, and one composed scan.

For a DuckDB-only indicator, add the same tuple shape to `SQL_INDICATORS`, with
a builder that returns a SQL fragment. See [Indicators](indicators.md) for the
registry contract.

## Examples and notebooks

All examples use the same deterministic two-symbol OHLCV fixture. Run a script
from the repository root with `uv run python`:

| Script | Covers |
| --- | --- |
| [`01_quickstart.py`](https://github.com/legout/scanlang/blob/master/docs/examples/01_quickstart.py) | `score_bars`, `validate`, `apply` |
| [`02_groups.py`](https://github.com/legout/scanlang/blob/master/docs/examples/02_groups.py) | `all`, `any`, `not` |
| [`03_computed_operands.py`](https://github.com/legout/scanlang/blob/master/docs/examples/03_computed_operands.py) | indicators, arithmetic, crossovers |
| [`04_custom_partition_and_registry.py`](https://github.com/legout/scanlang/blob/master/docs/examples/04_custom_partition_and_registry.py) | custom catalog, partition, indicators |
| [`05_score_and_stats.py`](https://github.com/legout/scanlang/blob/master/docs/examples/05_score_and_stats.py) | scoring, forward returns, summaries |
| [`06_eager_quickstart.py`](https://github.com/legout/scanlang/blob/master/docs/examples/06_eager_quickstart.py) | eager input |
| [`07_lazy_vs_sync.py`](https://github.com/legout/scanlang/blob/master/docs/examples/07_lazy_vs_sync.py) | eager, lazy, piped, renamed frames |

For example:

```sh
uv run python docs/examples/01_quickstart.py
```

The notebooks use the same fixture:

- [`01_first_scan.ipynb`](https://github.com/legout/scanlang/blob/master/docs/notebooks/01_first_scan.ipynb) for Jupyter:

  ```sh
  uv run jupyter nbconvert --execute --to notebook --inplace \
    docs/notebooks/01_first_scan.ipynb
  ```

- [`02_first_scan_marimo.py`](https://github.com/legout/scanlang/blob/master/docs/notebooks/02_first_scan_marimo.py) for marimo:

  ```sh
  uv run marimo edit docs/notebooks/02_first_scan_marimo.py
  ```

The longer [examples walkthrough](reference/examples-walkthrough.md) and the
original concept/how-to pages remain available by direct link, but are not in
the main navigation.
