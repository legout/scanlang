# Scan from text

`scanlang.dsl.parse(text)` (v0.2+) turns a one-line expression into the
same IR dict that `apply` accepts. Pure stdlib tokenizer + recursive
descent parser; parse errors raise `SyntaxError` with a 1-based position.

## The basic shapes

```python
from scanlang import parse, validate

parse("ema(20) > ema(50)")
# {'filters': [{'property': {'fn': 'ema', 'args': [{'col': 'close'}, 20]},
#               'op': '>',
#               'value': {'fn': 'ema', 'args': [{'col': 'close'}, 50]}}]}

parse("cross_above(ema(20), ema(50))")                          # golden cross
parse("close > sma(200, close(22)) and rsi(14) > 70")           # AND binds tighter
parse("phase in [BREAKOUT, TREND] or close between [50, 70]")   # OR / BETWEEN / IN
parse("spring and not near_52w_low")                            # bareword bool == true
parse("min(21) > rmax(252)")                                    # min/max sugar
parse("close[22] < sma(200)")                                   # Pine-style lookback
```

`parse` only returns the `filters` part. `order_by` and `limit` stay on
the dict side — combine the two:

```python
scan_def = {
    **parse("ema(20) > ema(50)"),
    "order_by": [{"property": "score", "dir": "desc"}],
    "limit": 5,
}
```

## Rules worth knowing

- **AND binds tighter than OR**. `a or b and c` parses as `a or (b and c)`.
- **`=` is `==`**. `=` is the SQL-style sugar.
- **A lone number on `ema`/`sma`/`rmin`/`rmax` implies `close`**:
  `ema(20)` is `ema(close, 20)`. `rsi(14)` and `atr(14)` are already
  correct because their arg tag is `"int"`.
- **`sma(200, close(22))` is corpus order, not (expr, n)**. It
  normalizes to canonical `sma(shift(close, 22), 200)` (the close-column
  ARGS come first, the window comes second). Same for `max(252, close)` ->
  `rmax(close, 252)`.
- **`close(1)` and `close[1]`** are both `shift(close, 1)`. The
  postfix `[n]` form is Pine-style; the function-call form is the
  historical corpus form.
- **`min(n)` / `max(n)` sugar** -> `rmin` / `rmax` (default close).
- **Bareword bool column** -> `col == true` leaf. `spring` is
  `{"property": "spring", "op": "==", "value": True}`.
- **Bareword non-bool column** without comparison is a parse error —
  the parser refuses to invent a comparison.

## Parse errors vs validation errors

The split mirrors the IR validation split:

- **Parse errors** (`SyntaxError` with position): bad tokens, unmatched
  parens, unknown column in a bareword, cross call in a wrong slot.
  These are syntax problems — the text can't even be shaped into an IR.
- **Validation errors** (`list[str]` from `validate`): wrong arg counts,
  bad window sizes, bad dtype values. These are semantic — the parse
  succeeded, but the resulting IR is malformed.

```python
>>> parse("sma(close, 20, 7) > 5")   # parses
{'filters': [{...}]}
>>> validate(parse("sma(close, 20, 7) > 5"))
["filters[0].property: 'sma' takes 2 args, got 3"]
```

```python
>>> parse("ema > ema")
# SyntaxError: unknown column 'ema' at position 1
```

## When to use the text DSL

- **REPL / quick iteration**: type a one-liner, paste it back as dict
  if it sticks.
- **User-facing syntax**: a UI that lets users type `"ema(20) > ema(50)"`
  directly without exposing the dict structure.
- **Tests**: a string is easier to read than a deeply nested dict with
  three levels of operand objects.
- **Not** for production storage — the IR dict is the durable form.
  Parse once at the boundary, store the dict.

## Where to next

- [DSL basics](../tutorials/dsl-basics.md) — the dict IR in depth
- [IR design](../explanation/ir-design.md) — the text-DSL section is
  frozen as part of the IR
- [API reference](../reference/api.md) — `parse` signature
