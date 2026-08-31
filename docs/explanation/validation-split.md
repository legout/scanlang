# Validation split

`scanlang` validates scan definitions in two tiers:

1. **Total** for literal leaves — bad dtype, unknown property, unknown
   operator. `validate()` returns a `list[str]` (empty = valid);
   `compile()` / `apply()` raise `ValueError`. Never a
   `polars.ComputeError` at filter time.
2. **Structural** for computed operands — known fn, known column, arg
   count / type, required cols. Dtype mismatches there (e.g.
   `close > sma(symbol, 5)`) surface as polars errors at collect time,
   because the operand tree is structurally fine but the join is wrong.

## Why split?

Computed operands need to compute *something* — their dtype is the
result of evaluating the indicator / arithmetic chain. We can't know
that dtype without executing the expression, which means executing it
on actual data. Doing that during `validate()` would force a
`.collect()` (or at least a polars schema probe) just to check
correctness, which violates the lazy contract.

So we validate what we *can* know statically:

- Is the indicator in `INDICATORS`?
- Does it have the right number of args?
- Are the literal args (windows, lookbacks) `int >= 1` and not `bool`?
- Are the required columns present in the catalog?
- For arithmetic folds: are the operands themselves recursively valid?
- For `{"col": "x"}`: is `x` in the catalog?

We do NOT validate:

- Whether `close > sma(symbol, 5)` makes sense (join key vs numeric)
- Whether `atr(14)` works on a frame with no `low` column (we DID
  validate this — `required_cols` — but if `low` is in the catalog and
  not the frame, polars will catch it at collect time)
- Whether a custom indicator's polars expression compiles against the
  catalog's dtypes

Those last three are runtime concerns. They belong at collect time, not
validate time, because they're about the frame — and the frame might
not exist when `validate()` is called (a UI validating a dict before
the user picks a dataset).

## What `validate` returns

```python
validate(scan_def) -> list[str]
# []                                    # valid
# ["filters[0]: unknown operator: '~='"]
# ["filters[1].value: 'sma' takes 2 args, got 3"]
# ["filters[0].value: 'between' needs [lo, hi]"]
```

Each error string is keyed to a field path (`filters[i].value`,
`order_by[i].property`, `limit`). UI can highlight inline. The first
character is always the path; the rest is a sentence.

## What `compile` and `apply` do on a bad def

`compile(scan_def)` calls `validate(scan_def)` internally and raises
`ValueError` with the FIRST error string if validation fails.
`apply(frame, scan_def)` calls `compile` internally, so same
behaviour. There is no "lenient mode" — if validation fails, the call
fails. The UI workflow is:

```python
errors = validate(scan_def)
if errors:
    display_inline(errors)
else:
    picks = apply(frame, scan_def)   # always succeeds (modulo collect-time polars errors)
```

## What the structural tier catches (and doesn't)

Catches at validate time:

- Unknown indicator name (`{"fn": "foo", ...}`) — `unknown indicator: 'foo'`
- Wrong arg count (`{"fn": "sma", "args": [a, b, c]}`) — `'sma' takes 2 args, got 3`
- Non-int window (`{"fn": "sma", "args": [{"col": "close"}, "20"]}`) — `must be an int >= 1`
- Missing required col (`{"fn": "atr", "args": [14]}` without `high`/`low`/`close`) — `requires column 'high'`
- Unknown column in `{"col": "x"}` — `unknown column: 'x'`

Does NOT catch (will surface at collect time as polars errors):

- Wrong dtype join (`close > sma(symbol, 5)` — symbol is a string column)
- Custom indicator that returns the wrong shape for the op
- Frame-level errors (column doesn't exist on the actual frame)

## Where to next

- [IR design](ir-design.md) — the contract
- [Lazy contract](lazy-contract.md) — why we don't `.collect()` during
  validation
- [Null semantics](null-semantics.md) — the other runtime layer
