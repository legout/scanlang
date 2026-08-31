"""Quickstart in eager mode: plain DataFrame in, apply, print.

Run:  .venv/bin/python docs/examples/06_eager_quickstart.py

The same API works on eager DataFrames. score_bars returns a LazyFrame;
call `.collect()` once at your edge, then pass the eager frame to `apply`.
Useful in notebooks, REPLs, and small one-off scripts where laziness adds
no value.
"""

import datetime as dt

import polars as pl

from scanlang import apply, score_bars, validate

# cell: a small OHLCV frame (sorted symbol, session — the caller contract)
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


# cell: eager frame in. score_bars accepts DataFrame, returns LazyFrame —
# collect once at your edge, then stay in eager-land.
df = bars()  # pl.DataFrame
scored = score_bars(df).collect()  # eager DataFrame from here on
print(scored.select("symbol", "session", "close", "score", "phase"))

# cell: a scan definition is a plain dict — validate() returns [] when valid
scan_def = {
    "filters": [{"property": "score", "op": ">=", "value": 40}],
    "order_by": [{"property": "score", "dir": "desc"}],
    "limit": 5,
}
print("errors:", validate(scan_def))  # []

# cell: apply runs on the eager frame — no .collect() needed downstream
result = apply(scored, scan_def)  # eager in -> eager out
print(result.select("symbol", "score", "phase"))

if __name__ == "__main__":
    assert validate(scan_def) == []
    # only AAA clears score>=40 in this two-symbol frame
    assert result.height == 1
    assert result["symbol"][0] == "AAA"
    assert result["score"][0] == scored["score"].max()
    print("06_eager_quickstart OK")
