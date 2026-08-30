"""Computed operands: column refs, indicators, arithmetic, cross ops.

Run:  .venv/bin/python docs/examples/03_computed_operands.py

Any `property` or (cross) `value` may be an operand: a bare scalar literal,
{"col": name}, {"fn": indicator, "args": [...]} (recursively nestable), or an
arithmetic fold {"+"/"-"/"*"/"/": [operand, ...]}. `in`/`between`/`contains`
values stay literal-only.
"""

import datetime as dt

import polars as pl

from scanlang import apply, catalog_from_schema, validate

# cell: raw OHLCV bars (no score_bars) — build a catalog straight from the schema
T0 = dt.date(2026, 1, 1)


def bars() -> pl.LazyFrame:
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

    uptrend = [10.0 + i for i in range(n)]
    downtrend = [60.0 - i for i in range(n)]
    return pl.DataFrame(rows("AAA", uptrend)).vstack(pl.DataFrame(rows("BBB", downtrend))).lazy()


lf = bars()
catalog = catalog_from_schema(lf)  # any LazyFrame -> catalog in one line

# cell: EMA cross — ema(close,5) crosses above ema(close,20), per symbol
ema_cross = {
    "filters": [{
        "property": {"fn": "ema", "args": [{"col": "close"}, 5]},
        "op": "cross_above",
        "value": {"fn": "ema", "args": [{"col": "close"}, 20]},
    }],
}
print("errors:", validate(ema_cross, catalog=catalog))  # []
crossed = apply(lf, ema_cross, catalog=catalog).collect()
print(crossed.select("symbol", "session", "close"))

# cell: arithmetic operands — close below half its 10-bar low: close < rmin(close,10)/2
cheap = {
    "filters": [{
        "property": {"col": "close"},
        "op": "<",
        "value": {"/": [{"fn": "rmin", "args": [{"col": "close"}, 10]}, 2]},
    }],
}
cheap_hits = apply(lf, cheap, catalog=catalog).collect()
print("cheap rows:", cheap_hits.height)

# cell: all of it nests — sma of an arithmetic of an rsi
nested = {
    "filters": [{
        "property": {"fn": "sma", "args": [{"+": [{"col": "close"}, {"fn": "rsi", "args": [{"col": "close"}, 14]}]}, 5]},
        "op": ">",
        "value": {"col": "close"},
    }],
}
print("nested errors:", validate(nested, catalog=catalog))  # []
print("nested hits:", apply(lf, nested, catalog=catalog).select(pl.len()).collect().item())

if __name__ == "__main__":
    # linear uptrend crosses exactly once (bar 1); a downtrend never
    assert crossed["symbol"].to_list() == ["AAA"]
    assert crossed["session"].to_list() == [T0 + dt.timedelta(days=1)]
    # close < rmin(close,10)/2 is false everywhere here (closes never halve)
    assert cheap_hits.height == 0
    # nested def is structurally valid and returns an int count
    count = apply(lf, nested, catalog=catalog).select(pl.len()).collect().item()
    assert isinstance(count, int) and count >= 0
    print("03_computed_operands OK")
