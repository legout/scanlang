"""Lazy vs sync (eager): collect at the edge, or not at all.

Run:  .venv/bin/python docs/examples/07_lazy_vs_sync.py

`score_bars` is lazy in / lazy out; `apply` is shape-preserving (frames in,
frames out, lazy in -> lazy out, eager in -> eager out). The same scan
runs both ways — pick one based on where the data lives:

- eager DataFrame already in memory -> stay eager
- LazyFrame from a file or query -> stay lazy, .collect() at your edge
- pipe into a bigger polars plan -> stay lazy, fold into the plan

`.collect()` is the boundary. Apply it where the next step would consume
a concrete frame (display, csv, feed to a stats helper), not earlier.
"""

import datetime as dt

import polars as pl

from scanlang import apply, score_bars, validate

# cell: shared fixture — same bars() the other examples use
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


scan_def = {
    "filters": [{"property": "score", "op": ">=", "value": 40}],
    "order_by": [{"property": "score", "dir": "desc"}],
    "limit": 5,
}
assert validate(scan_def) == []

# --- mode A: sync / eager ----------------------------------------------------
# score_bars(bars().lazy()) -> LazyFrame. .collect() at the edge -> DataFrame.
# apply keeps it eager from there.
df = bars()  # pl.LazyFrame
eager_picks = apply(score_bars(df).collect(), scan_def)
assert isinstance(eager_picks, pl.DataFrame)
print("eager:", eager_picks.select("symbol", "score", "phase"))

# --- mode B: stay lazy end-to-end -------------------------------------------
# skip .collect() — apply folds into the polars plan. One .collect() at the end.
df = bars()
lazy_plan = apply(score_bars(df), scan_def)  # still a LazyFrame
assert isinstance(lazy_plan, pl.LazyFrame)
print("lazy:", lazy_plan.select("symbol", "score", "phase").collect())

# --- mode C: defer entirely into a bigger pipeline --------------------------
# downstream join / sink / group_by stays lazy — no materialize round-trip.
df = bars()
picks = apply(score_bars(df), scan_def)
joined = picks.join(
    df.select("symbol", "session", "volume"),
    on=["symbol", "session"],
    how="inner",
)
print("piped (lazy, collected once):", joined.select("symbol", "score", "volume").collect().head(2))

# --- mode D: bespoke catalog, LazyFrame in ----------------------------------
# score_bars hard-codes the columns `symbol`, `session`, ..., `volume`. The
# rename lives OUTSIDE score_bars — apply scoring, then rename at your edge.
# PROPERTY_CATALOG still applies (score/phase etc. are unchanged); the new
# partition column name needs to be added to the catalog so scans can
# reference it (here we don't reference it — just show the wiring).
from scanlang import PROPERTY_CATALOG

df = bars()  # note: NO rename before score_bars
scored = score_bars(df).rename({"symbol": "ticker"})  # rename post-score
cat = {**PROPERTY_CATALOG, "ticker": {"label": "Ticker", "dtype": "str"}}
lazy_plan = apply(
    scored,
    {"filters": [{"property": "score", "op": ">=", "value": 40}]},
    catalog=cat,
    partition="ticker",
)
print("renamed (lazy):", lazy_plan.select("ticker", "score").collect())

if __name__ == "__main__":
    # all three modes reach the same scan, just at different boundaries
    assert eager_picks.height == 1
    assert eager_picks["symbol"][0] == "AAA"
    print("07_lazy_vs_sync OK")
