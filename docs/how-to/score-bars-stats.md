# score_bars + stats

The full pipeline: score OHLCV bars, screen with `apply`, then ask "what
did the past runs of this scan actually do?" with `forward_stats` and
`backtest_summary`.

`score_bars` and `apply` are frame-aware; `forward_stats` and
`backtest_summary` are pure — they take plain lists and `datetime.date`,
no polars dependency. The stats helpers answer "did the picks work?"
over your historical lake.

## End-to-end

`docs/examples/05_score_and_stats.py`:

```python
import datetime as dt
from scanlang import apply, backtest_summary, forward_stats, score_bars

scan_def = {
    "filters": [{"property": "score", "op": ">=", "value": 40}],
    "order_by": [{"property": "score", "dir": "desc"}],
    "limit": 5,
}
picks = apply(score_bars(bars), scan_def).collect()

# a past run of this scan — what happened to its picks afterwards?
ran_on = dt.date(2026, 1, 30)
runs = [{"ran_at": str(ran_on) + "T22:00", "symbols": picks["symbol"].to_list()}]

sessions = [...]   # your lake's sessions (list[date])
closes   = [...]   # aligned list[float] for the run's symbol

def stats_fn(symbol: str, ran_on: dt.date) -> dict[str, float] | None:
    return forward_stats(sessions, closes, ran_on)

summary = backtest_summary(runs, stats_fn)
print(summary["included"], "of", summary["total"], "picks")
for label, hit_rate, avg_ret, n in summary["horizons"]:
    print(f"  {label}: hit rate {hit_rate:.0f}%, avg {avg_ret:+.1f}% (n={n})")
```

`run: .venv/bin/python docs/examples/05_score_and_stats.py`

## What `forward_stats` returns

```python
forward_stats(sessions, closes, ran_on) -> dict[str, float] | None
# {"5d": +1.2, "10d": +3.4, "20d": +7.1}    # percentages
# None                                      # fresh run, no 20d forward window yet
```

Entry anchors at the first session on/after `ran_on` (the run picked at
that day's close; the next session is the first tradable). The returns
are `closes[i+n] / closes[i] - 1` expressed as percentages.

`None` means "not enough history to evaluate" — a fresh run, or a run
predating the lake. The caller excludes these.

## What `backtest_summary` returns

```python
backtest_summary(runs, stats_fn) -> dict
# {"included": 17, "total": 20,
#  "horizons": [
#    ("5d",  58.8, 1.2,  17),
#    ("10d", 70.6, 2.7,  17),
#    ("20d", 64.7, 5.1,  17),
#  ]}
```

- `included` — picks with an evaluable forward window
- `total` — picks across all runs
- `horizons` — per `HORIZONS = (("5d", 5), ("10d", 10), ("20d", 20))`:
  `(label, hit_rate%, avg_return%, n)`

`stats_fn` is yours to define — it can read parquet, hit a lake, or pull
from a database. The contract is `(symbol, ran_on) -> dict | None`.

## Scoring parameters

```python
score_bars(bars, *, min_bars=30, freshness_days=5)
```

- `min_bars`: drop symbols with fewer bars. Default 30 is enough for
  EMA(20) and ATR(14); raise it for ATR(14)+EMA(50)-heavy scans.
- `freshness_days`: drop symbols whose latest bar is more than N days
  behind the global max. Default 5.

Output columns are exactly the `PROPERTY_CATALOG` keys, plus `bars`
(symbol bar count). Use them directly in scan definitions without
re-declaring.

## Where to next

- [Eager vs lazy frames](eager-frames.md) — the `apply(score_bars(...),
  scan_def)` pattern in all four modes
- [API reference](../reference/api.md) — exact signatures
- [Examples index](../reference/examples.md)
