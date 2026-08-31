# Examples

Every script in `docs/examples/` is runnable, asserts its output, and is
paired with a verified walkthrough in
[`examples-walkthrough.md`](examples-walkthrough.md).

Run any example with your project interpreter:

```sh
.venv/bin/python docs/examples/01_quickstart.py
```

## By what you'll learn

### Tutorial-level

| Script | What you'll see |
| --- | --- |
| [`01_quickstart.py`](https://github.com/legout/scanlang/blob/master/docs/examples/01_quickstart.py) | score_bars + validate + apply (lazy) |
| [`06_eager_quickstart.py`](https://github.com/legout/scanlang/blob/master/docs/examples/06_eager_quickstart.py) | same quickstart, eager DataFrame in |
| [`07_lazy_vs_sync.py`](https://github.com/legout/scanlang/blob/master/docs/examples/07_lazy_vs_sync.py) | four modes — eager, lazy, piped, renamed |

### Notebooks

| Notebook | What you'll see |
| --- | --- |
| [`01_first_scan.ipynb`](https://github.com/legout/scanlang/blob/master/docs/notebooks/01_first_scan.ipynb) | the same first scan, Jupyter / nbformat — `uv run jupyter nbconvert --execute --to notebook --inplace docs/notebooks/01_first_scan.ipynb` |
| [`02_first_scan_marimo.py`](https://github.com/legout/scanlang/blob/master/docs/notebooks/02_first_scan_marimo.py) | the same first scan, marimo reactive cells — `uv run marimo export html docs/notebooks/02_first_scan_marimo.py -o /tmp/scanlang-marimo.html --force` |

See the [Notebooks reference](notebooks.md) for the full execution
recipe and what each engine is good for.

### How-to: groups & operands

| Script | What you'll see |
| --- | --- |
| [`02_groups.py`](https://github.com/legout/scanlang/blob/master/docs/examples/02_groups.py) | flat defs + all/any/not groups |
| [`03_computed_operands.py`](https://github.com/legout/scanlang/blob/master/docs/examples/03_computed_operands.py) | column refs, indicators, arithmetic, crosses |

### How-to: catalogs & indicators

| Script | What you'll see |
| --- | --- |
| [`04_custom_partition_and_registry.py`](https://github.com/legout/scanlang/blob/master/docs/examples/04_custom_partition_and_registry.py) | custom partition, custom catalog, extending INDICATORS |

### How-to: scoring + stats

| Script | What you'll see |
| --- | --- |
| [`05_score_and_stats.py`](https://github.com/legout/scanlang/blob/master/docs/examples/05_score_and_stats.py) | apply on LazyFrame + forward_stats / backtest_summary |

## Annotated walkthrough

[`examples-walkthrough.md`](examples-walkthrough.md) walks through each
script with the verified output it prints and the assertion that locks
the behaviour in. The walkthrough is generated against the current code
in CI (every script asserts its output in `if __name__ == "__main__"`).

## Fixture

Every example uses the same two-symbol OHLCV frame:

- `AAA`: 60-day linear uptrend (10 -> 69)
- `BBB`: 60-day linear downtrend (60 -> 1)
- synthetic open/high/low/volume

Output columns (where applicable) mirror `PROPERTY_CATALOG`. Assertions
in each `if __name__ == "__main__"` block lock the example's behavior;
the docs and the code can't drift.

## Where to next

- [First scan in 5 minutes](../tutorials/first-scan.md)
- [DSL basics](../tutorials/dsl-basics.md)
- [API reference](api.md)
