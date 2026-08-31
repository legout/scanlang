"""Forward-return evidence for a scan's past runs. Pure — no frame deps."""

from __future__ import annotations

import datetime as dt
from bisect import bisect_left
from collections.abc import Callable

__all__ = ["HORIZONS", "backtest_summary", "forward_stats"]

HORIZONS = (("5d", 5), ("10d", 10), ("20d", 20))


def forward_stats(
    sessions: list[dt.date], closes: list[float], ran_on: dt.date
) -> dict[str, float] | None:
    """Forward returns for a single run's entry point over ``HORIZONS``.

    Anchors entry at the first session on/after ``ran_on`` (a scan picked
    symbols at that day's close; the next session is the first tradable)
    and returns the +N-day return for each horizon in
    :data:`scanlang.stats.HORIZONS`.

    Args:
        sessions: Ascending list of trading sessions.
        closes: Close prices aligned 1:1 with ``sessions``.
        ran_on: The day the scan ran (ISO ``date``).

    Returns:
        ``{label: return%}`` for each horizon in
        :data:`scanlang.stats.HORIZONS` (default ``{"5d", "10d",
        "20d"}`), or ``None`` when the longest forward window hasn't
        elapsed yet (a fresh run) or the run predates the lake window.

    Examples:
        >>> import datetime as dt
        >>> sessions = [dt.date(2026, 1, 1) + dt.timedelta(days=i) for i in range(30)]
        >>> closes = [100.0 + i for i in range(30)]
        >>> forward_stats(sessions, closes, dt.date(2026, 1, 1))
        {'5d': 5.0, '10d': 10.0, '20d': 20.0}
    """
    i = bisect_left(sessions, ran_on)
    if i + HORIZONS[-1][1] >= len(closes):
        return None
    entry = closes[i]
    return {label: (closes[i + n] / entry - 1) * 100 for label, n in HORIZONS}


def backtest_summary(runs: list[dict], stats_fn: Callable) -> dict:
    """Aggregate forward returns across a scan's past runs.

    For each run, calls ``stats_fn(symbol, ran_on)`` and accumulates the
    results into per-horizon hit-rate and average-return stats.

    Args:
        runs: Run records. Each entry is ``{"ran_at": <ISO datetime
            string>, "symbols": [<symbol>, ...]}``. The date prefix of
            ``ran_at`` (first 10 chars) is parsed as the run date.
        stats_fn: ``(symbol, ran_on) -> {label: return%} | None``. Called
            once per (run, symbol). Return ``None`` to skip (the symbol
            has no evaluable bars for that run).

    Returns:
        ``{"included": <int>, "total": <int>, "horizons": [(label,
        hit_rate%, avg_return%, n), ...]}``. ``included`` is the count
        of picks whose forward window elapsed; ``total`` is the total
        picks across runs; ``horizons`` is keyed on
        :data:`scanlang.stats.HORIZONS`.

    Examples:
        >>> import datetime as dt
        >>> sessions = [dt.date(2026, 1, 1) + dt.timedelta(days=i) for i in range(30)]
        >>> closes = [100.0 + i for i in range(30)]
        >>> runs = [{"ran_at": "2026-01-01T22:00", "symbols": ["A"]}]
        >>> summary = backtest_summary(
        ...     runs, lambda s, d: forward_stats(sessions, closes, d)
        ... )
        >>> summary["included"]
        1
        >>> summary["horizons"][0][1] > 0  # hit rate
        True
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
