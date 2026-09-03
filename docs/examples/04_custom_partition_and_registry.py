"""Custom catalog + partition on your own frame, and extending INDICATORS.

Run:  .venv/bin/python docs/examples/04_custom_partition_and_registry.py

scanlang is not tied to score_bars output: any LazyFrame works. Rename at
your edge, derive a catalog from the schema, point `partition` at your group
column, and (optionally) insert your own indicators into INDICATORS.
"""

import datetime as dt

import polars as pl

from scanlang import INDICATORS, apply, catalog_from_schema, validate

# cell: your frame — note the group column is `ticker`, not `symbol`
T0 = dt.date(2026, 1, 1)


def bars() -> pl.LazyFrame:
    n = 60
    sessions = [T0 + dt.timedelta(days=i) for i in range(n)]

    def rows(sym, closes):
        return {
            "ticker": [sym] * n,
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
catalog = catalog_from_schema(lf)

# cell: RSI(14) above 70 per ticker — window ops respect partition="ticker"
rsi_hot = {
    "filters": [{
        "property": {"fn": "rsi", "args": [{"col": "close"}, 14]},
        "op": ">",
        "value": 70,
    }],
}
assert validate(rsi_hot, catalog=catalog) == []
hot = apply(lf, rsi_hot, catalog=catalog, partition="ticker").collect()
print(hot.group_by("ticker").agg(pl.len()).sort("ticker"))

# cell: extend the registry — the entry shape is the contract
if "stdev" not in INDICATORS:

    def _stdev(e: pl.Expr, n: int, partition: str) -> pl.Expr:
        return e.rolling_std(n).over(partition)

    INDICATORS["stdev"] = (("expr", "int"), _stdev, ())

z_score = {
    "filters": [{
        "property": {"/": [{"-": [{"col": "close"}, {"fn": "sma", "args": [{"col": "close"}, 20]}]},
                           {"fn": "stdev", "args": [{"col": "close"}, 20]}]},
        "op": ">",
        "value": 0.5,
    }],
}
print("z-score errors:", validate(z_score, catalog=catalog))  # []
z_hits = apply(lf, z_score, catalog=catalog, partition="ticker").collect()
print("z-score rows per ticker:", z_hits.group_by("ticker").agg(pl.len()).sort("ticker").to_dicts())

if __name__ == "__main__":
    # rsi fill_null(50): Wilder smoothing (0.3.0) — monotonic uptrend stays
    # >= 70 after warm-up (59 of 60 rows); downtrend BBB has rsi 0 — it never
    # appears (group_by drops empty groups)
    by_ticker = dict(hot.group_by("ticker").agg(pl.len()).iter_rows())
    assert by_ticker == {"AAA": 59}
    # registry insertion is additive and usable immediately
    assert "stdev" in INDICATORS
    assert z_hits.height > 0
    print("04_custom_partition_and_registry OK")
