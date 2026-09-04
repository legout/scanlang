"""Shared deterministic OHLCV fixture for the docs/notebooks examples.

Both `01_first_scan.ipynb` (Jupyter) and `02_first_scan_marimo.py`
(marimo) import from here so the notebooks describe the same raw-first
workflow and scored scan over the same data, byte for byte.

The fixture matches the one in docs/examples/*.py: 60 trading days for
two symbols (`AAA` linear uptrend 10 -> 69, `BBB` linear downtrend
60 -> 1). It's intentionally tiny — no network, no lake, no parquet —
so a new user can read every line.

To run from anywhere, the Jupyter and marimo kernels both add the
notebook's own directory to ``sys.path``, so importing this sibling
file just works.
"""

import datetime as dt

import polars as pl

T0 = dt.date(2026, 1, 1)
N = 60


def bars_eager() -> pl.DataFrame:
    """Return the small OHLCV frame as an eager `pl.DataFrame`."""

    sessions = [T0 + dt.timedelta(days=i) for i in range(N)]

    def rows(sym: str, closes: list[float]) -> dict:
        return {
            "symbol": [sym] * N,
            "session": sessions,
            "open": [c - 0.5 for c in closes],
            "high": [c + 1.0 for c in closes],
            "low": [c - 1.0 for c in closes],
            "close": closes,
            "volume": [1000.0] * N,
        }

    uptrend = [10.0 + i for i in range(N)]
    downtrend = [60.0 - i for i in range(N)]
    return pl.DataFrame(rows("AAA", uptrend)).vstack(
        pl.DataFrame(rows("BBB", downtrend))
    )


def bars_lazy() -> pl.LazyFrame:
    """Return the small OHLCV frame as a `pl.LazyFrame`."""

    return bars_eager().lazy()


# Raw scan used before scoring. It proves the compiler works on OHLCV input.
RAW_SCAN_DEF: dict = {
    "filters": [{
        "property": {"fn": "ema", "args": [{"col": "close"}, 5]},
        "op": ">",
        "value": {"fn": "ema", "args": [{"col": "close"}, 20]},
    }]
}

# Scored scan used after the raw scan. Mirrors the tutorial.
SCORE_SCAN_DEF: dict = {
    "filters": [
        {"property": "score", "op": ">=", "value": 40},
        {"property": "phase", "op": "in", "value": ["BREAKOUT", "TREND", "BASE"]},
    ],
    "order_by": [{"property": "score", "dir": "desc"}],
    "limit": 5,
}

# Compatibility alias for callers of the old notebook fixture.
SCAN_DEF = SCORE_SCAN_DEF