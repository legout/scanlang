# scanlang

[![PyPI](https://img.shields.io/pypi/v/scanlang)](https://pypi.org/project/scanlang/)
[![Python](https://img.shields.io/pypi/pyversions/scanlang)](https://pypi.org/project/scanlang/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Status: pre-alpha](https://img.shields.io/badge/status-pre--alpha-orange)](https://pypi.org/project/scanlang/)

Screener DSL and scan compiler: signal definitions -> polars pushdown filters.

A scan definition is a plain dict (JSON from a UI, a Python literal from a
notebook) that `scanlang` compiles into one validated polars predicate.
Nothing is string-interpolated, so there is no injection surface. Filters
run on any eager `DataFrame` or lazy `LazyFrame`; window semantics
(indicators, crosses) are computed per partition, so 10 symbols or 10,000
behave the same.

Status: **v0.2, pre-alpha**. The IR is frozen — changes are additive only.
User-facing spec: [`docs/reference/ir-freeze.md`](docs/reference/ir-freeze.md)
(repository record: [`docs/IR_FREEZE.md`](docs/IR_FREEZE.md)).

## Install

```sh
uv add scanlang          # or: pip install scanlang
```

Requires Python >= 3.11 and polars >= 1.44. The optional `talib` extra
(`uv add scanlang --optional talib`) is reserved for a future value-parity
indicator module.

## Quickstart

```python
import polars as pl
from scanlang import apply, score_bars, validate

# score OHLCV bars (lazy in, lazy out — collect at your edge)
scored = score_bars(bars.lazy()).collect()

scan_def = {
    "filters": [{"property": "score", "op": ">=", "value": 40}],
    "order_by": [{"property": "score", "dir": "desc"}],
    "limit": 5,
}

validate(scan_def)   # [] when valid; never raises
picks = apply(scored, scan_def)
print(picks.select("symbol", "score", "phase"))
```

Prefer eager frames end-to-end? See
[`docs/examples/07_lazy_vs_sync.py`](docs/examples/07_lazy_vs_sync.py).
Prefer a one-liner over the dict? Use the text DSL:

```python
from scanlang import parse, validate
ir = parse("ema(20) > ema(50) and rsi(14) > 70")
validate(ir)   # []
```

## How it works

```
scan def (dict or text)               <-- user / UI / REPL
  parse()   -> dict        (text DSL only, v0.2+)
  validate() -> list[str]   (empty == valid; total on literal leaves)
  compile() -> pl.Expr      (single validated predicate)
  apply()   -> frame        (filter + order_by + limit;
                             eager in -> eager out; lazy in -> lazy out)
```

The IR is the single contract between callers and `scanlang`. Nested
boolean groups and computed operands are total: malformed defs raise
`ValueError` from `compile`/`apply` and return error strings from
`validate` — never a `polars.ComputeError` at filter time. Full spec:
[`docs/reference/ir-freeze.md`](docs/reference/ir-freeze.md)
(repository record: [`docs/IR_FREEZE.md`](docs/IR_FREEZE.md)).

## Reference

| Function | Purpose |
| --- | --- |
| `compile(scan_def, *, catalog=PROPERTY_CATALOG, partition="symbol")` | scan def -> one polars predicate |
| `validate(scan_def, *, catalog=...)` | `list[str]` of errors; empty = valid |
| `apply(frame, scan_def, *, catalog=..., partition=...)` | filter + order_by + limit (eager or lazy) |
| `parse(text, *, catalog=...)` | text DSL -> scan def dict (`SyntaxError` on bad syntax) |
| `catalog_from_schema(frame)` | polars schema -> catalog; unmapped dtypes skipped |
| `score_bars(bars, *, min_bars=30, freshness_days=5)` | phase/scan scoring over OHLCV; lazy in, lazy out |
| `forward_stats` / `backtest_summary` (+ `HORIZONS`) | forward-return evidence for a scan's past runs |
| `INDICATORS` | extensible registry: name -> `(arg_spec, builder, required_cols)` |

Full reference: [`docs/reference/api.md`](docs/reference/api.md).

### Operators

| Op | Operands | Notes |
| --- | --- | --- |
| `>=  <=  >  <  ==  !=` | scalar or operand | standard comparisons |
| `between` | `[lo, hi]` literal-only | closed interval |
| `in` | nonempty list, literal-only | membership |
| `contains` | string literal only | substring (str columns) |
| `cross_above` / `cross_below` | operand on both sides | per-partition: `a>b AND prev_a<=prev_b` |

### Indicators (built-in)

| Name | Args | Required cols | Window |
| --- | --- | --- | --- |
| `sma` / `ema` / `rsi` / `rmin` / `rmax` / `shift` | `(expr, n)` | — | rolling / ewm / shift over partition |
| `atr` | `(n,)` | `high, low, close` | rolling TR mean over partition |

Extend the registry by inserting entries; see
[`docs/how-to/extend-indicators.md`](docs/how-to/extend-indicators.md).

## Examples

Runnable scripts in [`docs/examples/`](docs/examples/), each verified:

| Script | What it shows |
| --- | --- |
| `01_quickstart.py` | score_bars + validate + apply (lazy) |
| `02_groups.py` | flat defs + all/any/not groups |
| `03_computed_operands.py` | column refs, indicators, arithmetic, crosses |
| `04_custom_partition_and_registry.py` | custom partition, extending INDICATORS |
| `05_score_and_stats.py` | apply on LazyFrame + forward_stats / backtest_summary |
| `06_eager_quickstart.py` | same quickstart, eager DataFrame in |
| `07_lazy_vs_sync.py` | four modes — eager, lazy, piped, renamed column |

```sh
.venv/bin/python docs/examples/01_quickstart.py
```

Annotated walkthroughs with verified output:
[`docs/reference/examples-walkthrough.md`](docs/reference/examples-walkthrough.md).

## Docs

Rendered site: `uv run --group docs zensical build` (config:
[`zensical.toml`](zensical.toml)). Sections follow Diataxis —
Tutorials / How-to / Explanation / Reference. Why compiled filters stay
in polars (not SQL): [`docs/reference/research-duckdb.md`](docs/reference/research-duckdb.md)
(repository record: [`docs/RESEARCH_DUCKDB.md`](docs/RESEARCH_DUCKDB.md)).

## Development

```sh
uv sync                                      # create .venv
.venv/bin/python -m pytest tests/ -q         # tests
.venv/bin/python -m ruff check src tests     # lint
```

## License

[MIT](LICENSE)
