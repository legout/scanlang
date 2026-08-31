# Extend INDICATORS

The built-in indicators (`sma, ema, rsi, atr, rmin, rmax, shift`) cover
the common TA needs but are deliberately small. Adding your own is a
one-line insertion into the `INDICATORS` dict — no subclassing, no
plugin registration, no entry-point dance.

## The entry shape

```python
INDICATORS["your_name"] = (arg_spec, builder, required_cols)
```

Each part:

- **`arg_spec`**: tuple with one tag per positional arg. `"expr"` means
  "any operand" (column ref, nested indicator call, arithmetic, scalar);
  `"int"` means "literal int >= 1".
- **`builder(*parsed, partition) -> pl.Expr`**: your polars expression.
  Every window op must use `.over(partition)`.
- **`required_cols`**: tuple of column names that must exist in the
  catalog (empty tuple if none).

Example — `stdev(close, 20)` for a z-score:

```python
from scanlang import INDICATORS

def _stdev(e: pl.Expr, n: int, partition: str) -> pl.Expr:
    return e.rolling_std(n).over(partition)

INDICATORS["stdev"] = (("expr", "int"), _stdev, ())
```

That's the whole extension. The new indicator is now usable from both
the dict IR (`{"fn": "stdev", "args": [{"col": "close"}, 20]}`) and the
text DSL (`stdev(close, 20)`).

## A worked example — z-score > 0.5

```python
from scanlang import INDICATORS, apply, validate

# register once at module import
if "stdev" not in INDICATORS:
    INDICATORS["stdev"] = (("expr", "int"),
                           lambda e, n, partition: e.rolling_std(n).over(partition),
                           ())

z_score = {
    "filters": [{
        "property": {"/": [
            {"-": [{"col": "close"},
                   {"fn": "sma", "args": [{"col": "close"}, 20]}]},
            {"fn": "stdev", "args": [{"col": "close"}, 20]},
        ]},
        "op": ">",
        "value": 0.5,
    }],
}

print(validate(z_score, catalog=catalog))  # []
hits = apply(lf, z_score, catalog=catalog, partition="ticker").collect()
```

`run: .venv/bin/python docs/examples/04_custom_partition_and_registry.py`

## Rules to respect

1. **Window ops need `.over(partition)`** — without it, your rolling
   function aggregates across the entire frame and your scan breaks per
   partition. Indicators that aren't windows (e.g. a one-shot
   expression) can omit it, but you'll rarely write those.
2. **`required_cols` is checked at validate time** — if your indicator
   needs `high, low, close` and the catalog doesn't have them,
   `validate()` returns `"indicator 'foo' requires column 'high'"`. Use
   this; it's much friendlier than a polars error at collect time.
3. **Arg types are checked** — `"int"` slots must be a `int >= 1` and not
   `bool` (`True`/`False` are `int` in Python; the check is explicit).
   `"expr"` slots are passed through `_operand` and recursively
   validated.
4. **Insert is idempotent** — guard with `if "name" not in INDICATORS`
   to make repeat-import safe. Re-registering overwrites; the new
   builder is what compiles.

## A more realistic entry — RSI with Wilder smoothing

```python
def _wilder_rsi(e: pl.Expr, n: int, partition: str) -> pl.Expr:
    delta = e.diff().over(partition)
    # Wilder smoothing: ema(alpha=1/n)
    avg_gain = delta.clip(lower_bound=0).ewm_mean(alpha=1.0 / n, adjust=False).over(partition)
    avg_loss = (-delta.clip(upper_bound=0)).ewm_mean(alpha=1.0 / n, adjust=False).over(partition)
    return (100 - 100 / (1 + avg_gain / avg_loss)).fill_null(50.0)

INDICATORS["wilder_rsi"] = (("expr", "int"), _wilder_rsi, ())
```

Use it the same way: `{"fn": "wilder_rsi", "args": [{"col": "close"}, 14]}`.

## Where to next

- [Indicators reference](../reference/indicators.md) — built-in indicator
  reference (sigs, required cols)
- [IR design](../explanation/ir-design.md) — the registry contract as part
  of the IR freeze
- [Custom catalog + partition](custom-catalog-partition.md) — when your
  frame isn't a `score_bars` output
