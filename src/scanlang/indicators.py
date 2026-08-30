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
"""

from __future__ import annotations

from collections.abc import Callable

import polars as pl

__all__ = ["INDICATORS"]


def _rsi(e: pl.Expr, n: int, partition: str) -> pl.Expr:
    delta = e.diff().over(partition)
    gain = delta.clip(lower_bound=0).rolling_mean(n).over(partition)
    loss = (-delta.clip(upper_bound=0)).rolling_mean(n).over(partition)
    return (100 - 100 / (1 + gain / loss)).fill_null(50.0)


def _atr(n: int, partition: str) -> pl.Expr:
    pc = pl.col("close").shift(1).over(partition)
    tr = pl.max_horizontal(
        pl.col("high") - pl.col("low"),
        (pl.col("high") - pc).abs(),
        (pc - pl.col("low")).abs(),
    )
    return tr.rolling_mean(n).over(partition)


# name -> (arg_spec, builder, required_cols)
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
