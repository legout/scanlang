# Null semantics

`scanlang` follows polars null semantics on the filter pipeline:

- Comparisons on null yield null.
- `not null` yields null.
- `null in [...]` yields null (not `False`).
- The polars filter drops rows where the predicate is null.

## Why we don't work around it

The naive workaround — "treat null as false" — would silently change
the meaning of a scan. Consider:

```python
# "spring or score > 80"
{
    "any": [
        {"property": "spring", "op": "==", "value": True},
        {"property": "score",  "op": ">",  "value": 80},
    ],
}
```

If `spring` is null (the bar didn't compute a spring signal for some
reason — not enough history, a missing input), you almost certainly
want that row to fail the OR, not silently pass because "null is false".

Compare with:

```python
# "not spring"  — this scan wants rows where spring is NOT true
{"not": {"property": "spring", "op": "==", "value": True}}
```

Here a null `spring` should fail (the row is not the absence-of-spring
we wanted; it's an unknown), not pass (because "null is false means
we're done").

Working around nulls (mapping null to a sentinel boolean) collapses
those two cases into the same result. We'd lose the ability to
distinguish "I checked, it's not spring" from "I couldn't check".

So: documented behavior. Null on the predicate side means null on the
filter. Row dropped.

## What this looks like in practice

For `score_bars` output, the columns that can be null are limited:

- `score` and `phase`: never null — `score_bars` filters rows before
  producing them (`min_bars`, `freshness_days`).
- `vol_ratio`, `atr_ratio`, `rsi`, `acc_score`: never null — defaults
  (`1.0`, `1.0`, `50.0`, `0.0`) are filled in during the scoring pass.
- `spring`, `ema_stack`, `recent_cross`, `near_52w_low`: never null —
  boolean columns produced by `.and_` / `.then().otherwise()` chains.
- `upper_wick_pct`: never null — guarded by `_range > 0` (0.0 otherwise).

For user-built scans on raw OHLCV (via `catalog_from_schema`), the null
semantics matter more — a column can be null for any row whose input
was missing. Use standard polars patterns: `fill_null`, `is_null`,
explicit comparisons.

## When to use `is_null` / `is_not_null`

`scanlang` doesn't currently have `is_null` / `is_not_null` operators
— they're additive candidates if a scan needs them. Today, work around
with `between`/`in`/`!=` against sentinel values:

```python
# "close is not null" — sentinel that no real close will hit
{"property": "close", "op": "!=", "value": -1.0}
```

…or filter the nulls out at your edge before calling `apply`.

## Where to next

- [Lazy contract](lazy-contract.md) — how the predicate folds into a
  bigger plan and where `.collect()` lives
- [Validation split](validation-split.md) — when nulls become dtype
  errors at collect time vs total validation up front
- [IR design](ir-design.md) — the contract
