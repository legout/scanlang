# scanlang

Screener DSL and scan compiler: signal definitions -> polars pushdown filters.

A scan definition is a plain dict (JSON from a UI, a Python literal from a
notebook) that `scanlang` compiles into one validated polars predicate.
Nothing is string-interpolated, so there is no injection surface. Filters
run on any eager `DataFrame` or lazy `LazyFrame`; window semantics
(indicators, crosses) are computed per partition, so 10 symbols or 10,000
behave the same.

!!! info "Status: v0.2, pre-alpha"
    The IR is frozen (see [IR design](explanation/ir-design.md)) — changes
    are additive only.

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
[`docs/examples/07_lazy_vs_sync.py`](https://github.com/legout/scanlang/blob/master/docs/examples/07_lazy_vs_sync.py).
Prefer a one-liner over the dict? Use the text DSL:

```python
from scanlang import parse, validate
ir = parse("ema(20) > ema(50) and rsi(14) > 70")
validate(ir)   # []
```

## Where to next

The docs follow [Diataxis](https://diataxis.fr/). Pick by what you want
to do right now:

- **Tutorials** — *learning-oriented*. Get from zero to first scan.
  - [First scan in 5 minutes](tutorials/first-scan.md)
  - [DSL basics](tutorials/dsl-basics.md)
- **How-to guides** — *task-oriented*. Solve a specific problem.
  - [Custom catalog + partition](how-to/custom-catalog-partition.md)
  - [Extend INDICATORS](how-to/extend-indicators.md)
  - [Eager vs lazy frames](how-to/eager-frames.md)
  - [score_bars + stats](how-to/score-bars-stats.md)
  - [Scan from text](how-to/scan-from-text.md)
- **Explanation** — *understanding-oriented*. Why it works this way.
  - [IR design](explanation/ir-design.md)
  - [Lazy contract](explanation/lazy-contract.md)
  - [Null semantics](explanation/null-semantics.md)
  - [Validation split](explanation/validation-split.md)
  - [Why no duckdb](explanation/why-no-duckdb.md)
- **Reference** — *information-oriented*. Look up an exact name or shape.
  - [API](reference/api.md)
  - [Operators](reference/operators.md)
  - [Indicators](reference/indicators.md)
  - [Examples index](reference/examples.md)

## Development

```sh
uv sync --group docs                            # create .venv with zensical
.venv/bin/python -m pytest tests/ -q            # tests
.venv/bin/python -m ruff check src tests         # lint
.venv/bin/zensical serve                        # live-reload docs at :8000
.venv/bin/zensical build                        # static build -> site/
```

## License

[MIT](https://github.com/legout/scanlang/blob/master/LICENSE)
