# Language

A screen can be written as one line of text or as a plain Python dict. The text
form parses to the dict form; store the dict as JSON when a screen needs to be
persisted.

## Text syntax

```python
from scanlang import parse

scan_def = parse("ema(20) > ema(50) and rsi(close, 14) > 70")
```

### Comparisons

| Syntax | Meaning |
| --- | --- |
| `>`, `>=`, `<`, `<=`, `==`, `!=` | compare two operands |
| `between [lo, hi]` | closed numeric range |
| `in [value, ...]` | membership in a non-empty list |
| `cross_above(left, right)` | crosses from at-or-below to above |
| `cross_below(left, right)` | crosses from at-or-above to below |

`=` is an alias for `==`.

### Boolean logic

Use `not`, `and`, and `or`. `not` binds most tightly, then `and`, then `or`.
Use parentheses when the grouping is not obvious.

```python
parse("phase in [BREAKOUT, TREND] and (rsi(close, 14) > 70 or spring)")
parse("not near_52w_low")
```

A bare boolean column, such as `spring`, means that the column is true.

### Operands

An operand can be:

| Form | Example |
| --- | --- |
| number or string literal | `70`, `'BREAKOUT'` |
| column | `close` |
| shifted column | `close[1]` or `close(1)` |
| indicator | `ema(20)`, `rsi(close, 14)` |
| arithmetic | `close / 2`, `rmin(20) + 5` |

`ema(20)`, `sma(20)`, `rmin(20)`, and `rmax(20)` use `close` by default.
`min` and `max` are aliases for `rmin` and `rmax`. Indicators can nest:

```python
parse("sma(rsi(close, 14), 5) > close")
```

See [Indicators](indicators.md) for available names and signatures.

## Dict shape

The dict is the intermediate representation shared by both engines:

```python
{
    "filters": [node, ...],
    "order_by": [{"property": "score", "dir": "desc"}],
    "limit": 20,
}
```

Only `filters` is required. A flat list is an implicit AND. `order_by` accepts
catalog property names only; computed expressions are not supported there.

The text parser supports comparisons, `between`, `in`, and crossovers. The dict
form also supports `contains` with a literal substring value.

A leaf has a property, operator, and value:

```python
{
    "property": {"fn": "ema", "args": [{"col": "close"}, 20]},
    "op": ">",
    "value": {"col": "close"},
}
```

Operands use these dict forms:

- `{"col": "close"}` for a column;
- `{"fn": "sma", "args": [{"col": "close"}, 20]}` for an indicator;
- `{ "+": [left, right] }`, or `"-"`, `"*"`, `"/"` for arithmetic;
- a number, string, or boolean for a literal.

## Groups and crossovers

Use groups when conditions need explicit boolean structure:

```python
{
    "filters": [{
        "any": [
            {"property": "phase", "op": "==", "value": "BREAKOUT"},
            {"not": {"property": "spring", "op": "==", "value": True}},
        ]
    }]
}
```

- `{"all": [node, ...]}` means AND.
- `{"any": [node, ...]}` means OR.
- `{"not": node}` means NOT.

Groups must not be empty.

A crossover is a transition, not just the current comparison:

```python
{
    "filters": [{
        "property": {"fn": "ema", "args": [{"col": "close"}, 20]},
        "op": "cross_above",
        "value": {"fn": "ema", "args": [{"col": "close"}, 50]},
    }]
}
```

`cross_above` means:

```text
left > right AND previous(left) <= previous(right)
```

`cross_below` reverses those comparisons. Previous values are calculated
separately for each partition, normally each `symbol`.

## Validation and errors

Parse at the input boundary, then validate before running:

```python
from scanlang import apply, parse, validate

scan_def = parse("ema(20) > ema(50)")
errors = validate(scan_def)
if errors:
    raise ValueError(errors[0])
rows = apply(frame, scan_def)
```

`parse` raises `SyntaxError` when text cannot be converted to a dict. The error
includes a one-based position:

```python
parse("ema > ema")
# SyntaxError: unknown column 'ema' at position 1
```

`validate` returns a list for a parsed dict. It checks names, argument counts,
literal types, required columns, operators, and group structure. For example:

```python
validate(parse("sma(close, 20, 7) > 5"))
# ["filters[0].property: 'sma' takes 2 args, got 3"]
```

The available indicator names depend on the engine. Pass
`engine="duckdb"` to validate a DuckDB screen; see [API](reference/api.md)
for the callable reference.
