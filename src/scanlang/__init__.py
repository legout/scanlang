"""Screener DSL and scan compiler.

Signal dict (IR) -> validated polars expressions -> lazy pushdown over any
LazyFrame source. Optional text DSL parses to the same IR.
"""

__version__ = "0.1.0"
