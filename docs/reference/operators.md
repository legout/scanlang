# Operators

Reference for every operator accepted in scan leaves. Every op takes a
`property` (column name or computed operand) and a `value` (whose type
depends on the op).

## Comparison

| Op | Property | Value | Notes |
| --- | --- | --- | --- |
| `>=` | `{"col": ...}` or operand | scalar or operand | |
| `<=` | `{"col": ...}` or operand | scalar or operand | |
| `>`  | `{"col": ...}` or operand | scalar or operand | |
| `<`  | `{"col": ...}` or operand | scalar or operand | |
| `==` | `{"col": ...}` or operand | scalar or operand | dtype-checked |
| `!=` | `{"col": ...}` or operand | scalar or operand | dtype-checked |

`value` may be a literal (scalar), `{"col": x}` (column ref),
`{"fn": ...}` (indicator call), or `{"+": ...}` / `"-"` / `"*"` / `"/"`
(arithmetic fold).

## Set / range — literal-only `value`

| Op | Value | Notes |
| --- | --- | --- |
| `between` | `[lo, hi]` | closed interval, dtype-checked |
| `in` | nonempty list | membership, dtype-checked |
| `contains` | string | substring match, str columns only |

Computed operands on the right of these ops are NOT accepted — `phase
in [ema(20), sma(50)]` is rejected at validate time. Use a regular
comparison if you need that.

## Cross ops — operand `value` required

| Op | Value | Notes |
| --- | --- | --- |
| `cross_above` | operand | `a > b AND shift(a,1) <= shift(b,1)` per partition |
| `cross_below` | operand | `a < b AND shift(a,1) >= shift(b,1)` per partition |

Cross ops accept an operand (same shapes as comparison ops). For
boolean crosses use a column or compare against a constant.

## What compiles to what

```python
# =================== comparisons ===================
{">": col.__gt__}
{"<": col.__lt__}
{">=": col.__ge__}
{"<=": col.__le__}
{"==": col.__eq__}
{"!=": col.__ne__}

# =================== set / range ===================
{"between": col.is_between(v[0], v[1], closed="both")}
{"in":      col.is_in(v)}
{"contains": col.str.contains(v, literal=True)}  # str columns only

# =================== crosses ===================
# cross_above(a, b):
#   lhs > rhs AND lhs.shift(1).over(partition) <= rhs.shift(1).over(partition)
# cross_below(a, b):
#   lhs < rhs AND lhs.shift(1).over(partition) >= rhs.shift(1).over(partition)
```

## Validation errors per operator

| Error string | Means |
| --- | --- |
| `"unknown operator: '<op>'"` | not in the allowed set |
| `"'between' needs [lo, hi]"` | `value` not a 2-element list/tuple |
| `"'between' bounds must be <dtype> values"` | lo/hi don't match the property dtype |
| `"'in' needs a nonempty list of values"` | `value` is empty or not a list |
| `"'in' values must be <dtype> values"` | entries don't match the property dtype |
| `"'contains' needs a string value on a string property"` | dtype is not str, or value is not a str |
| `"computed left side not supported for <op>"` | property is a computed operand AND op is in/between/contains |
