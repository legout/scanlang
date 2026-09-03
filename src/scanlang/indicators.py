"""Indicator registry: name -> (arg_spec, builder, required_cols).

Each entry:
- ``arg_spec``: tuple with one tag per positional arg — ``"expr"`` (any operand) or
  ``"int"`` (literal int >= 1).
- ``builder(*parsed, partition) -> pl.Expr``: polars-native; every window op uses
  ``.over(partition)``.
- ``required_cols``: columns that must exist in the catalog (e.g. ``atr`` needs
  ``high, low, close``).

Extend by inserting entries; this shape is the contract. A future
``scanlang.talib`` module (pyproject ``talib`` extra) populates the same dict for
exact-value parity on collected results — it cannot participate in lazy pushdown.

Seeding note (``ema``/``rsi``/``atr``): TA-Lib seeds its recursions with an SMA
of the first ``n`` values; polars ``ewm_mean(adjust=False)`` seeds from the
first value. The recursions match, so values converge after warm-up
(typically ~3-4x the period) but diverge in the early window — accepted by
design, see the 2026-09-02 duckdb-backend plan (Q1).
"""

from __future__ import annotations

from collections.abc import Callable

import polars as pl

__all__ = ["INDICATORS"]


def _rsi(e: pl.Expr, n: int, partition: str) -> pl.Expr:
    delta = e.diff().over(partition)
    gain = delta.clip(lower_bound=0).ewm_mean(alpha=1 / n, adjust=False).over(partition)
    loss = (-delta.clip(upper_bound=0)).ewm_mean(alpha=1 / n, adjust=False).over(partition)
    return 100 - 100 / (1 + gain / loss)


def _atr(n: int, partition: str) -> pl.Expr:
    pc = pl.col("close").shift(1).over(partition)
    tr = pl.max_horizontal(
        pl.col("high") - pl.col("low"),
        (pl.col("high") - pc).abs(),
        (pc - pl.col("low")).abs(),
    )
    return tr.ewm_mean(alpha=1 / n, adjust=False).over(partition)


# name -> (arg_spec, builder, required_cols)
#
# Extend by inserting entries; the entry shape is the contract. See
# docs/how-to/extend-indicators.md for the extension recipe.
INDICATORS: dict[str, tuple[tuple[str, ...], Callable, tuple[str, ...]]] = {
    "sma": (
        ("expr", "int"),
        lambda e, n, partition: e.rolling_mean(n).over(partition),
        (),
    ),
    "ema": (
        ("expr", "int"),
        lambda e, span, partition: e.ewm_mean(span=span, adjust=False).over(partition),
        (),
    ),
    "rsi": (("expr", "int"), _rsi, ()),
    "atr": (("int",), _atr, ("high", "low", "close")),
    "rmin": (
        ("expr", "int"),
        lambda e, n, partition: e.rolling_min(n).over(partition),
        (),
    ),
    "rmax": (
        ("expr", "int"),
        lambda e, n, partition: e.rolling_max(n).over(partition),
        (),
    ),
    "shift": (
        ("expr", "int"),
        lambda e, n, partition: e.shift(n).over(partition),
        (),
    ),
}

"""Indicator registry: the ``{"fn": name}`` operand extension point.

Maps indicator name -> ``(arg_spec, builder, required_cols)``:

- ``arg_spec``: one tag per positional arg — ``"expr"`` (any operand:
  column ref, nested indicator, arithmetic) or ``"int"`` (a literal int
  >= 1, i.e. a window length).
- ``builder(*parsed, partition) -> pl.Expr``: called with the parsed
  args in order plus the partition column name; must return a
  polars-native expression, with every window op under ``.over(partition)``
  so scans stay correct per symbol.
- ``required_cols``: catalog columns the builder needs (validated by
  [`validate`](api.md#scanlang.compiler.validate)); empty for most entries.

Registry mutation is the extension point:
``INDICATORS["stdev"] = (("expr", "int"), builder, ())`` — register
idempotently (guard with ``if "stdev" not in INDICATORS``) at import
time. See [Extend INDICATORS](../how-to/extend-indicators.md).
"""
