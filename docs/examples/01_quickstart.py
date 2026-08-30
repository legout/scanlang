"""Quickstart: score OHLCV bars, then filter/order/limit them with a scan def.

Run:  .venv/bin/python docs/examples/01_quickstart.py

In a marimo notebook, each block below a `# cell` marker becomes one cell.
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


# cell: score every symbol's latest bar (lazy in, lazy out — collect at your edge)
scored = score_bars(bars().lazy()).collect()
print(scored.select("symbol", "session", "close", "score", "phase"))

# cell: a scan definition is a plain dict — validate() returns [] when it's valid
scan_def = {
    "filters": [{"property": "score", "op": ">=", "value": 40}],
    "order_by": [{"property": "score", "dir": "desc"}],
    "limit": 1,
}
print("errors:", validate(scan_def))  # []

# cell: apply = filter + order_by + limit, on eager or lazy frames alike
result = apply(scored, scan_def)
print(result.select("symbol", "score", "phase"))

if __name__ == "__main__":
    assert validate(scan_def) == []
    assert result.height == 1
    assert result["score"][0] == scored["score"].max()
    print("01_quickstart OK")
