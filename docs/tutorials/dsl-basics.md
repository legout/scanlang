# DSL basics

The scan definition (the "IR") is a plain dict. This page covers every
shape you'll write by hand or generate from a UI — operators, operands,
nested groups, and cross ops. For the one-line text equivalent, see
[Scan from text](../how-to/scan-from-text.md).

## The top-level shape

```python
{
    "filters": [node, ...],                 # required
    "order_by": [{"property": ..., "dir": "asc" | "desc"}, ...],   # optional
    "limit": 5,                              # optional
}
```

A bare `filters` list of leaves is valid: today's flat defs keep working.

## Nodes — three nested groups, plus leaves

- `{"all": [node, ...]}` — AND (nonempty)
- `{"any": [node, ...]}` — OR (nonempty)
- `{"not": node}` — unary NOT
- leaf: `{"property": ..., "op": ..., "value": ...}`

Groups nest arbitrarily. `{"property": "phase", "op": "in", "value": ["BREAKOUT", "TREND"]}`
ANDed with `{"not": {"property": "spring", "op": "==", "value": true}}` is
just a list with two leaves at the top level — the implicit AND.

```python
# (phase in BREAKOUT/TREND) AND (NOT spring) AND (score between 55 and 100)
{
    "filters": [
        {"property": "phase", "op": "in", "value": ["BREAKOUT", "TREND"]},
        {"not": {"property": "spring", "op": "==", "value": True}},
        {"property": "score", "op": "between", "value": [55, 100]},
    ],
}
```

## Operators

| Op | Value type | Notes |
| --- | --- | --- |
| `>=  <=  >  <  ==  !=` | scalar or operand | standard comparisons |
| `between` | `[lo, hi]` literal-only | closed interval |
| `in` | nonempty list, literal-only | membership |
| `contains` | string literal only | substring (str columns) |
| `cross_above` / `cross_below` | operand on both sides | per-partition edge-cross |

Literal-only means `in`/`between`/`contains` cannot have computed operands
on the right side. `phase in [ema(20), sma(50)]` is invalid; use a regular
comparison for that.

`cross_above(a, b)` compiles to `a > b AND shift(a,1) <= shift(b,1)` over
the partition (mirrored for `cross_below`). Use it for golden crosses,
breakouts, etc. — anything where you want "did it just happen" rather
than "is it true now".

## Operands

Anywhere you see `<operand>` (the `property` or the `value` of a leaf, the
args of an arithmetic fold, the args of an indicator call), you can write:

- a **bare scalar literal**: `40`, `"BREAKOUT"`, `False`
- `{"col": "name"}` — a column reference (catalog-validated)
- `{"fn": "sma", "args": [operand, ...]}` — an indicator call, args are
  operands themselves, so `sma(rsi(close, 14), 5)` is legal
- `{"+": [a, b]}` / `"-"` / `"*"` / `"/"` — arithmetic fold; n-ary, with
  `{"-": [x]}` meaning negation

```python
# close < rmin(close, 10) / 2  — half of the 10-bar low
{
    "property": {"col": "close"},
    "op": "<",
    "value": {"/": [{"fn": "rmin", "args": [{"col": "close"}, 10]}, 2]},
}
```

Built-in indicators: `sma, ema, rsi, atr, rmin, rmax, shift`. See
[Indicators reference](../reference/indicators.md) for the exact arg
spec. Extend the registry by inserting entries — see
[Extend INDICATORS](../how-to/extend-indicators.md).

## `order_by` is property-only

`order_by` keys are catalog property names — column names, not operand
expressions. There is no `order_by: rsi(close, 14) desc`; that's a
future additive key, not in the current IR.

```python
"order_by": [{"property": "score", "dir": "desc"}]
```

## A worked example

EMA cross with volume confirmation, ordered by score, top 5:

```python
scan_def = {
    "filters": [
        {
            "property": {"fn": "ema", "args": [{"col": "close"}, 5]},
            "op": "cross_above",
            "value": {"fn": "ema", "args": [{"col": "close"}, 20]},
        },
        {"property": "vol_ratio", "op": ">", "value": 1.5},
    ],
    "order_by": [{"property": "score", "dir": "desc"}],
    "limit": 5,
}
```

Run it: `apply(scored, scan_def)` (or with a `LazyFrame` directly).

## Validation: total on literal leaves, structural on computed operands

- **Total** for literal leaves: bad dtype, unknown property, unknown
  operator — all surface as `ValueError` from `compile`/`apply` and as
  error strings from `validate`. Never a `polars.ComputeError` at filter
  time.
- **Structural** for computed operands: known fn, known col, arg
  count/type, required cols. Dtype mismatches there (e.g.
  `close > sma(symbol, 5)`) surface as polars errors at collect time —
  the operand tree is structurally fine, but the join key is wrong.

See [Validation split](../explanation/validation-split.md) for the
rationale.

## Where to next

- [Scan from text](../how-to/scan-from-text.md) — the one-line text DSL
  that parses to the same IR
- [Custom catalog + partition](../how-to/custom-catalog-partition.md) —
  when `symbol` is `ticker` and your frame has its own schema
- [IR design](../explanation/ir-design.md) — the full contract
