"""Session-style API: bind a frame once, apply text or dict screens.

    from scanlang import Scan

    sl = Scan(bars)                                  # catalog derived from bars
    sl.apply("close > 4 and sma(20) > sma(50)")      # str parsed internally
    sl.result                                         # last apply() output
    sl.materialized                                   # bars + indicator columns

The functional API (``parse``/``validate``/``apply``) stays canonical; this
module only removes the repeated ``catalog=`` threading.
"""

from __future__ import annotations

import polars as pl

from scanlang.compiler import INDICATORS, _operand, apply, catalog_from_schema
from scanlang.dsl import parse

__all__ = ["Scan", "materialize"]


def _fn_exprs(spec: dict, *, catalog: dict, partition: str, exprs: list, seen: set) -> None:
    """Walk a scan def, appending one aliased builder Expr per fn node."""
    if not isinstance(spec, dict):
        return
    if "fn" in spec:
        arg_spec, builder, _req = INDICATORS[spec["fn"]]
        parsed = [
            a if tag == "int" else _operand(a, catalog=catalog, partition=partition)
            for tag, a in zip(arg_spec, spec["args"])
        ]
        n = next((a for tag, a in zip(arg_spec, spec["args"]) if tag == "int"), "")
        alias = f"{spec['fn']}_{n}"
        if alias not in seen:
            built = builder(*parsed, partition=partition)
            if not isinstance(built, pl.Expr):
                raise ValueError(
                    f"indicator {spec['fn']!r} (talib seam) cannot be materialized; use apply()"
                )
            seen.add(alias)
            exprs.append(built.alias(alias))
        for a in spec["args"]:
            _fn_exprs(a, catalog=catalog, partition=partition, exprs=exprs, seen=seen)
    elif "filters" in spec:
        for node in spec["filters"]:
            _fn_exprs(node, catalog=catalog, partition=partition, exprs=exprs, seen=seen)
    elif "all" in spec or "any" in spec:
        for node in spec.get("all") or spec.get("any") or []:
            _fn_exprs(node, catalog=catalog, partition=partition, exprs=exprs, seen=seen)
    elif "not" in spec:
        _fn_exprs(spec["not"], catalog=catalog, partition=partition, exprs=exprs, seen=seen)
    elif "property" in spec:
        _fn_exprs(spec["property"], catalog=catalog, partition=partition, exprs=exprs, seen=seen)
        if spec.get("value") is not None:
            _fn_exprs(spec["value"], catalog=catalog, partition=partition, exprs=exprs, seen=seen)


def materialize(
    frame: pl.DataFrame | pl.LazyFrame,
    scan_def: dict | str,
    *,
    catalog: dict | None = None,
    partition: str = "symbol",
) -> pl.DataFrame | pl.LazyFrame:
    """Return ``frame`` plus one column per indicator fn node in the screen.

    Columns are named ``fn_n`` (e.g. ``sma_20``); repeats collapse. Text
    screens are parsed with the same catalog. Arithmetic operands stay in the
    predicate (they are not indicator nodes). Talib seam indicators
    (eager map_groups builders, e.g. ``adx``) raise — only ``apply`` stages
    them.
    """
    if catalog is None:
        catalog = catalog_from_schema(frame)
    if isinstance(scan_def, str):
        scan_def = parse(scan_def, catalog=catalog)
    exprs: list[pl.Expr] = []
    _fn_exprs(scan_def, catalog=catalog, partition=partition, exprs=exprs, seen=set())
    return frame.with_columns(exprs)


class Scan:
    """A frame bound to its catalog; screens parse and run without re-threading.

    Attributes:
        data: the registered frame (eager or lazy, unchanged).
        catalog: derived via ``catalog_from_schema`` unless given.
        result: the last ``apply()`` output; ``None`` before the first call.
        materialized: ``data`` plus one column per indicator in the last screen
            (recomputed per access; raises before the first ``apply()``).
    """

    def __init__(self, frame: pl.DataFrame | pl.LazyFrame, *, partition: str = "symbol", catalog: dict | None = None):
        self.data = frame
        self.partition = partition
        self.catalog = catalog if catalog is not None else catalog_from_schema(frame)
        self.result = None
        self._scan: dict | None = None

    def apply(self, scan_def: dict | str, **kwargs):
        """Run a screen (str is parsed with ``self.catalog``); store ``result``."""
        if isinstance(scan_def, str):
            scan_def = parse(scan_def, catalog=self.catalog)
        self._scan = scan_def
        self.result = apply(
            self.data, scan_def, catalog=self.catalog, partition=self.partition, **kwargs
        )
        return self.result

    @property
    def materialized(self):
        if self._scan is None:
            raise ValueError("apply() a screen before reading .materialized")
        return materialize(
            self.data, self._scan, catalog=self.catalog, partition=self.partition
        )
