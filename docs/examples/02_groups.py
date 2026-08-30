"""Flat definitions and all/any/not groups.

Run:  .venv/bin/python docs/examples/02_groups.py

A bare `filters` list of leaves is ANDed (today's flat defs unchanged). Nest
`all` / `any` lists and unary `not` objects for arbitrary boolean logic.
"""

import datetime as dt

import polars as pl

from scanlang import apply, score_bars, validate

# cell: score a two-symbol OHLCV frame
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

    uptrend = [10.0 + i for i in range(n)]
    downtrend = [60.0 - i for i in range(n)]
    return pl.DataFrame(rows("AAA", uptrend)).vstack(pl.DataFrame(rows("BBB", downtrend)))


scored = score_bars(bars()).collect()

# cell: flat def — top-level leaves are ANDed
flat = {
    "filters": [
        {"property": "score", "op": ">=", "value": 30},
        {"property": "phase", "op": "!=", "value": "NONE"},
    ],
}
print("flat errors:", validate(flat))  # []
flat_hits = apply(scored, flat)
print(flat_hits.select("symbol", "score", "phase"))

# cell: groups — (phase in BREAKOUT/TREND OR score between 55..100) AND NOT spring
grouped = {
    "filters": [
        {"any": [
            {"property": "phase", "op": "in", "value": ["BREAKOUT", "TREND"]},
            {"property": "score", "op": "between", "value": [55, 100]},
        ]},
        {"not": {"property": "spring", "op": "==", "value": True}},
    ],
}
print("grouped errors:", validate(grouped))  # []
grouped_hits = apply(scored, grouped)
print(grouped_hits.select("symbol", "score", "phase"))

if __name__ == "__main__":
    assert validate(flat) == [] and validate(grouped) == []
    # every hit satisfies both flat leaves
    assert (flat_hits["score"] >= 30).all() and (flat_hits["phase"] != "NONE").all()
    # every hit satisfies the grouped def
    assert ((grouped_hits["phase"].is_in(["BREAKOUT", "TREND"])) | (grouped_hits["score"].is_between(55, 100))).all()
    assert not grouped_hits["spring"].any()
    # grouped hits are a subset of flat-with-looser-bounds semantics: any-hit rows
    # must appear in the frame
    assert set(grouped_hits["symbol"]) <= set(scored["symbol"])
    print("02_groups OK")
