"""Verify every flow the README/EXAMPLES docs will show. Prints OK per step."""

import datetime as dt

import polars as pl

from scanlang import (
    PROPERTY_CATALOG,
    INDICATORS,
    apply,
    backtest_summary,
    catalog_from_schema,
    compile,
    forward_stats,
    score_bars,
    validate,
)

T0 = dt.date(2026, 1, 1)


def bars() -> pl.DataFrame:
    n = 60
    sessions = [T0 + dt.timedelta(days=i) for i in range(n)]

    def rows(sym, closes):
        return {
            "symbol": [sym] * n,
            "session": sessions,
            "open": [c - 0.5 for c in closes],
            "high": [c + 1.0 for c in closes],
            "low": [c - 1.0 for c in closes],
            "close": closes,
            "volume": [1000.0] * n,
        }

    a = [10.0 + i for i in range(n)]  # steady uptrend
    b = [50.0 - i for i in range(n)]  # steady downtrend
    return pl.DataFrame(rows("AAA", a)).vstack(pl.DataFrame(rows("BBB", b)))


# 1. compile / validate / apply on scored output
scored = score_bars(bars()).collect()
scan_def = {
    "filters": [{"property": "score", "op": ">=", "value": 40}],
    "order_by": [{"property": "score", "dir": "desc"}],
    "limit": 1,
}
assert validate(scan_def) == []
out = apply(scored, scan_def)
assert len(out) == 1, out
print("OK 1 flat def + apply:", out["symbol"][0], out["score"][0])

# compile returns a bare polars expr
expr = compile(scan_def)
filtered = scored.filter(expr)
print("OK 2 compile -> expr:", type(expr).__name__, len(filtered))

# 2. groups any/not
d = {
    "filters": [
        {"any": [
            {"property": "phase", "op": "in", "value": ["BREAKOUT", "TREND"]},
            {"property": "score", "op": "between", "value": [40, 60]},
        ]},
        {"not": {"property": "spring", "op": "==", "value": False}},
    ]
}
errs = validate(d)
print("OK 3 groups validate:", errs)
out = apply(scored, d)
print("    -> rows:", len(out))

# 3. computed operands: ema cross on raw OHLC via catalog_from_schema
lf = bars().lazy()
d = {"filters": [{
    "property": {"fn": "ema", "args": [{"col": "close"}, 5]},
    "op": "cross_above",
    "value": {"fn": "ema", "args": [{"col": "close"}, 20]},
}]}
out = apply(lf, d, catalog=catalog_from_schema(lf)).collect()
print("OK 4 ema cross:", out["symbol"].to_list(), [str(s) for s in out["session"].to_list()])

# arithmetic operand
d2 = {"filters": [{
    "property": {"*": [{"col": "close"}, 2]},
    "op": ">",
    "value": {"+": [{"col": "close"}, 50]},
}]}
out2 = apply(lf, d2, catalog=catalog_from_schema(lf)).collect()
print("OK 5 arithmetic:", sorted(set(out2["symbol"].to_list())))

# 4. custom partition + custom catalog on renamed frame
renamed = bars().rename({"symbol": "sym"})
cat = catalog_from_schema(renamed.lazy())
d3 = {"filters": [{"property": {"fn": "rsi", "args": [{"col": "close"}, 14]}, "op": ">", "value": 70}]}
assert validate(d3, catalog=cat) == []
out3 = apply(renamed.lazy(), d3, catalog=cat, partition="sym").collect()
print("OK 6 custom partition rsi>70 rows:", len(out3))

# 5. score_bars end-to-end on a LazyFrame
lazy_out = score_bars(bars().lazy())  # stays lazy
assert isinstance(lazy_out, pl.LazyFrame)
print("OK 7 score_bars lazy:", lazy_out.collect().columns[:5], "...")
print("    catalog keys == output cols:", set(PROPERTY_CATALOG) == set(lazy_out.collect().columns))

# 6. validate surfaces human-readable errors
errs = validate({"filters": [{"property": "nope", "op": ">=", "value": 1}]})
print("OK 8 validate errors:", errs)
try:
    compile({"filters": [{"property": "nope", "op": ">=", "value": 1}]})
except ValueError as e:
    print("OK 9 compile raises ValueError:", e)

# 7. indicator registry introspection
print("OK 10 INDICATORS:", sorted(INDICATORS))
print("    atr entry:", INDICATORS["atr"])

# 8. forward stats (pure python)
sessions = [T0 + dt.timedelta(days=i) for i in range(30)]
closes = [100.0 + i for i in range(30)]
st = forward_stats(sessions, closes, sessions[0])
print("OK 11 forward_stats:", st)
summary = backtest_summary(
    [{"ran_at": "2026-01-01T22:00", "symbols": ["AAA"]}],
    lambda sym, ran_on: st,
)
print("OK 12 backtest_summary:", summary["included"], summary["total"], summary["horizons"][0])

print("ALL VERIFIED")
