"""Screener DSL and scan compiler.

Signal dict (IR) -> validated polars expressions -> lazy pushdown over any
LazyFrame source. Text DSL parses to the same IR (scanlang.dsl.parse).

>>> import polars as pl
>>> from scanlang import compile, apply
>>> scan_def = {"filters": [{"property": "score", "op": ">=", "value": 50}]}
"""

from scanlang.compiler import (
    PROPERTY_CATALOG,
    apply,
    catalog_from_schema,
    compile,
    validate,
)
from scanlang.dsl import parse
from scanlang.indicators import INDICATORS
from scanlang.scoring import score_bars
from scanlang.stats import HORIZONS, backtest_summary, forward_stats

__all__ = [
    "HORIZONS",
    "INDICATORS",
    "PROPERTY_CATALOG",
    "apply",
    "backtest_summary",
    "catalog_from_schema",
    "compile",
    "forward_stats",
    "parse",
    "score_bars",
    "validate",
]

__version__ = "0.1.0"
