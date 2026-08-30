"""Forward-return evidence for a scan's past runs. Pure — no frame deps."""

from __future__ import annotations

import datetime as dt
from bisect import bisect_left

__all__ = ["HORIZONS", "backtest_summary", "forward_stats"]

HORIZONS = (("5d", 5), ("10d", 10), ("20d", 20))


def forward_stats(
    sessions: list[dt.date], closes: list[float], ran_on: dt.date
) -> dict[str, float] | None:
    """+5/+10/+20d return of the run's entry price vs the latest lake close.

    Entry anchors at the first session on/after ``ran_on`` (a scan picked
    symbols at that day's close; the next session is the first tradable).
    ``sessions``/``closes`` are ascending and aligned. Returns None when the
    20d forward window hasn't elapsed in the lake yet (fresh run) or the run
    predates the lake window — the caller excludes the run.
    """
    i = bisect_left(sessions, ran_on)
    if i + HORIZONS[-1][1] >= len(closes):
        return None
    entry = closes[i]
    return {label: (closes[i + n] / entry - 1) * 100 for label, n in HORIZONS}


def backtest_summary(runs: list[dict], stats_fn) -> dict:
    """Aggregate forward returns across a scan's runs: hit-rate + avg per horizon.

    ``runs`` are run records (``ran_at`` text + ``symbols`` list).
    ``stats_fn(symbol, ran_on)`` returns ``{label: return%}`` or None when the
    symbol has no evaluable bars. Runs whose forward window hasn't elapsed are
    excluded — caller surfaces "n included / m total".
    """
    total = len(runs)
    picks = 0
    per: dict[str, list[float]] = {label: [] for label, _ in HORIZONS}
    for run in runs:
        ran_on = dt.date.fromisoformat((run["ran_at"] or " ")[:10])
        for symbol in run["symbols"]:
            st = stats_fn(symbol, ran_on)
            if st is None:
                continue
            picks += 1
            for label, ret in st.items():
                per[label].append(ret)
    horizons = [
        (
            label,
            100.0 * sum(r > 0 for r in rets) / len(rets) if rets else 0.0,
            sum(rets) / len(rets) if rets else 0.0,
            len(rets),
        )
        for label, rets in per.items()
    ]
    return {"included": picks, "total": total, "horizons": horizons}
