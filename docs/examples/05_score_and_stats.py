"""score_bars + apply end-to-end, then forward_stats / backtest_summary.

Run:  .venv/bin/python docs/examples/05_score_and_stats.py

The stats helpers are pure Python — no frame deps — and answer "what did
this scan's past picks actually do?" over your lake history.
"""

import datetime as dt

import polars as pl

from scanlang import apply, backtest_summary, forward_stats, score_bars

# cell: OHLCV bars for two symbols
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


# cell: score (lazy) and screen with apply directly on the LazyFrame
scan_def = {
    "filters": [{"property": "score", "op": ">=", "value": 40}],
    "order_by": [{"property": "score", "dir": "desc"}],
    "limit": 5,
}
picks = apply(score_bars(bars()), scan_def).collect()
print(picks.select("symbol", "session", "close", "score", "phase"))

# cell: a past run of this scan — what happened to its picks afterwards?
ran_on = T0 + dt.timedelta(days=30)
runs = [{"ran_at": str(ran_on) + "T22:00", "symbols": picks["symbol"].to_list()}]

sessions = [T0 + dt.timedelta(days=i) for i in range(60)]
closes = [100.0 + i for i in range(60)]  # AAA's entry continues rising


def stats_fn(symbol: str, ran_on: dt.date) -> dict[str, float] | None:
    return forward_stats(sessions, closes, ran_on)


summary = backtest_summary(runs, stats_fn)
print("included:", summary["included"], "of", summary["total"], "picks")
for label, hit_rate, avg_ret, n in summary["horizons"]:
    print(f"  {label}: hit rate {hit_rate:.0f}%, avg {avg_ret:+.1f}% (n={n})")

if __name__ == "__main__":
    # only AAA clears score>=40 — BBB's downtrend scores 20
    assert picks.height == 1
    assert picks["score"].is_sorted(descending=True)
    s = summary["horizons"][0]
    assert s[0] == "5d" and s[3] == 1
    assert abs(s[2] - (closes[35] / closes[30] - 1) * 100) < 1e-9
    print("05_score_and_stats OK")
