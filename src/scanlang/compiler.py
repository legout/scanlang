"""Compile scan-definition dicts into polars predicates.

A scan definition is a plain dict (JSON from the Lab UI, REPL, notebooks)::

    {"filters": [
        {"property": "rsi", "op": ">=", "value": 60},
        {"property": "phase", "op": "in", "value": ["BREAKOUT"]},
        {"any": [
            {"property": "score", "op": "between", "value": [50, 90]},
            {"not": {"property": "spring", "op": "==", "value": False}},
        ]},
        # computed operands: {"col": x} refs, {"fn": name, "args": [...]}
        # indicators, {"+": [a, b]} arithmetic; scalars stay literals
        {"property": {"fn": "ema", "args": [{"col": "close"}, 5]},
         "op": "cross_above",
         "value": {"fn": "ema", "args": [{"col": "close"}, 20]}},
     ],
     "order_by": [{"property": "score", "dir": "desc"}],
     "limit": 50}

See docs/IR_FREEZE.md for the full contract. Nothing is string-interpolated,
so there is no injection surface. Validation is total for literal leaves
(never a polars ComputeError at filter time) and structural for computed
operands — dtype mismatches there surface at collect time.
"""

from __future__ import annotations

import datetime as dt
import operator
from functools import reduce

import polars as pl

from scanlang.indicators import INDICATORS

__all__ = ["PROPERTY_CATALOG", "apply", "catalog_from_schema", "compile", "validate"]

# Mirrors scoring.score_bars() output columns. dtype: str, int, float, bool, date.
PROPERTY_CATALOG: dict[str, dict[str, str]] = {
    "symbol": {"label": "Symbol", "dtype": "str"},
    "session": {"label": "Session", "dtype": "date"},
    "close": {"label": "Close", "dtype": "float"},
    "score": {"label": "Score", "dtype": "int"},
    "phase": {"label": "Phase", "dtype": "str"},
    "vol_ratio": {"label": "Vol Ratio", "dtype": "float"},
    "atr_ratio": {"label": "ATR Ratio", "dtype": "float"},
    "rsi": {"label": "RSI", "dtype": "float"},
    "acc_score": {"label": "Accumulation", "dtype": "float"},
    "spring": {"label": "Spring", "dtype": "bool"},
    "ema_stack": {"label": "EMA Stack", "dtype": "bool"},
    "recent_cross": {"label": "Recent Cross", "dtype": "bool"},
    "upper_wick_pct": {"label": "Upper Wick %", "dtype": "float"},
    "near_52w_low": {"label": "Near 52w Low", "dtype": "bool"},
    "bars": {"label": "Bars", "dtype": "int"},
}

# op -> builder(lhs, rhs) -> pl.Expr
_OPS = {
    ">=": lambda col, v: col >= v,
    "<=": lambda col, v: col <= v,
    ">": lambda col, v: col > v,
    "<": lambda col, v: col < v,
    "==": lambda col, v: col == v,
    "!=": lambda col, v: col != v,
    "between": lambda col, v: col.is_between(v[0], v[1], closed="both"),
    "in": lambda col, v: col.is_in(v),
    "contains": lambda col, v: col.str.contains(v, literal=True),
}
_CROSS = ("cross_above", "cross_below")
_ARITH = {
    "+": operator.add,
    "-": operator.sub,
    "*": operator.mul,
    "/": operator.truediv,
}
_LIST_OPS = ("in", "between", "contains")


def _ok_date(v) -> bool:
    if not isinstance(v, str):
        return False
    try:
        dt.date.fromisoformat(v)
    except ValueError:
        return False
    return True


def _is(dtype: str, value) -> bool:
    """JSON value type-check against a catalog dtype (bool is not int)."""
    if dtype == "int":
        return isinstance(value, int) and not isinstance(value, bool)
    if dtype == "float":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if dtype == "bool":
        return isinstance(value, bool)
    return isinstance(value, str)  # str and date both arrive as JSON strings


# --- operand parsing -------------------------------------------------------


def _operand(spec, *, catalog: dict, partition: str) -> pl.Expr:
    """Operand dict/scalar -> pl.Expr. Assumes validate() already passed."""
    if isinstance(spec, dict):
        if "col" in spec:
            return pl.col(spec["col"])
        if "fn" in spec:
            arg_spec, builder, _req = INDICATORS[spec["fn"]]
            parsed = [
                a if tag == "int" else _operand(a, catalog=catalog, partition=partition)
                for tag, a in zip(arg_spec, spec["args"])
            ]
            return builder(*parsed, partition=partition)
        key = next(k for k in spec if k in _ARITH)
        vals = [_operand(a, catalog=catalog, partition=partition) for a in spec[key]]
        if len(vals) == 1:  # unary fold; freeze names only negate: {"-": [x]}
            return -vals[0] if key == "-" else vals[0]
        return reduce(_ARITH[key], vals)
    return pl.lit(spec)


def _operand_errors(spec, where: str, catalog: dict, errors: list[str]) -> None:
    if spec is None:
        errors.append(f"{where}: operand must not be null")
    elif isinstance(spec, (bool, int, float, str)):
        pass  # literal
    elif not isinstance(spec, dict):
        errors.append(f"{where}: operand must be scalar, col, fn, or arithmetic, got {spec!r}")
    elif "col" in spec:
        name = spec["col"]
        if not isinstance(name, str) or name not in catalog:
            errors.append(f"{where}.col: unknown column: {name!r}")
    elif "fn" in spec:
        name = spec["fn"]
        entry = INDICATORS.get(name) if isinstance(name, str) else None
        if entry is None:
            errors.append(f"{where}.fn: unknown indicator: {name!r}")
            return
        arg_spec, _builder, required = entry
        args = spec.get("args")
        if not isinstance(args, list) or len(args) != len(arg_spec):
            got = len(args) if isinstance(args, list) else args
            errors.append(f"{where}: {name!r} takes {len(arg_spec)} args, got {got}")
            return
        for i, (tag, a) in enumerate(zip(arg_spec, args)):
            if tag == "int":
                if not isinstance(a, int) or isinstance(a, bool) or a < 1:
                    errors.append(f"{where}.args[{i}]: must be an int >= 1, got {a!r}")
            else:
                _operand_errors(a, f"{where}.args[{i}]", catalog, errors)
        for col in required:
            if col not in catalog:
                errors.append(f"{where}: indicator {name!r} requires column {col!r}")
    elif len(ks := [k for k in spec if k in _ARITH]) == 1:
        vals = spec[ks[0]]
        if not isinstance(vals, list) or not vals:
            errors.append(f"{where}.{ks[0]} must be a nonempty list")
        elif len(vals) == 1 and ks[0] != "-":
            errors.append(f"{where}.{ks[0]} must have >= 2 operands")
        else:
            for i, a in enumerate(vals):
                _operand_errors(a, f"{where}.{ks[0]}[{i}]", catalog, errors)
    else:
        errors.append(f"{where}: operand must be col, fn, or arithmetic, got keys {sorted(spec)}")


# --- node validation -------------------------------------------------------


def _val_ok(dtype: str, value) -> bool:
    return _ok_date(value) if dtype == "date" else _is(dtype, value)


def _leaf_errors(f, where: str, catalog: dict, errors: list[str]) -> None:
    prop = f.get("property")
    op = f.get("op")
    computed_lhs = isinstance(prop, dict)
    spec = catalog.get(prop) if isinstance(prop, str) else None
    if computed_lhs:
        _operand_errors(prop, f"{where}.property", catalog, errors)
    elif spec is None:
        errors.append(f"{where}: unknown property: {prop!r}")
    if op not in _OPS and op not in _CROSS:
        errors.append(f"{where}: unknown operator: {op!r}")
        return
    value = f.get("value")
    if op in _CROSS:
        _operand_errors(value, f"{where}.value", catalog, errors)
    elif op in _LIST_OPS:
        if computed_lhs:
            errors.append(f"{where}: computed left side not supported for {op!r}")
        if spec is None:
            return
        dtype = spec["dtype"]
        if op == "contains":
            if dtype != "str" or not isinstance(value, str):
                errors.append(f"{where}: 'contains' needs a string value on a string property")
        elif op == "between":
            if not isinstance(value, (list, tuple)) or len(value) != 2:
                errors.append(f"{where}: 'between' needs [lo, hi]")
            elif not all(_val_ok(dtype, v) for v in value):
                errors.append(f"{where}: 'between' bounds must be {dtype} values")
        elif not isinstance(value, list) or not value:
            errors.append(f"{where}: 'in' needs a nonempty list of values")
        elif not all(_val_ok(dtype, v) for v in value):
            errors.append(f"{where}: 'in' values must be {dtype} values")
    elif isinstance(value, dict):
        _operand_errors(value, f"{where}.value", catalog, errors)
    elif value is None:
        errors.append(f"{where}: value must not be null")
    elif spec is not None and not _val_ok(spec["dtype"], value):
        dtype = spec["dtype"]
        if dtype == "date":
            errors.append(
                f"{where}: value for {prop!r} (date) must be an ISO date string, got {value!r}"
            )
        else:
            errors.append(f"{where}: value for {prop!r} ({dtype}) must be {dtype}, got {value!r}")


def _node_errors(node, where: str, catalog: dict, errors: list[str]) -> None:
    if not isinstance(node, dict):
        errors.append(f"{where}: node must be an object, got {node!r}")
    elif "all" in node or "any" in node:
        key = "all" if "all" in node else "any"
        kids = node[key]
        if not isinstance(kids, list) or not kids:
            errors.append(f"{where}.{key} must be a nonempty list")
        else:
            for i, kid in enumerate(kids):
                _node_errors(kid, f"{where}.{key}[{i}]", catalog, errors)
    elif "not" in node:
        if not isinstance(node["not"], dict):
            errors.append(f"{where}.not must be an object")
        else:
            _node_errors(node["not"], f"{where}.not", catalog, errors)
    else:
        _leaf_errors(node, where, catalog, errors)


def _collect(scan_def, *, catalog: dict) -> list[str]:
    """Collect every validation error in the definition (empty list = valid)."""
    if not isinstance(scan_def, dict):
        return [f"scan definition must be an object, got {scan_def!r}"]
    errors: list[str] = []
    filters = scan_def.get("filters") or []
    if not isinstance(filters, list):
        errors.append("filters must be a list")
    else:
        for i, node in enumerate(filters):
            _node_errors(node, f"filters[{i}]", catalog, errors)
    order_by = scan_def.get("order_by") or []
    if not isinstance(order_by, list):
        errors.append("order_by must be a list")
    else:
        for ob in order_by:
            if not isinstance(ob, dict):
                errors.append(f"order entry must be an object, got {ob!r}")
                continue
            if ob.get("property") not in catalog:
                errors.append(f"unknown property: {ob.get('property')!r}")
            if ob.get("dir", "asc") not in ("asc", "desc"):
                errors.append(f"unknown direction: {ob.get('dir')!r}")
    limit = scan_def.get("limit")
    if limit is not None and (not isinstance(limit, int) or isinstance(limit, bool) or limit < 0):
        errors.append("limit must be a nonnegative integer")
    return errors


def validate(scan_def, *, catalog: dict = PROPERTY_CATALOG) -> list[str]:
    """Return error strings (empty = valid); human-facing, keyed to fields."""
    return _collect(scan_def, catalog=catalog)


# --- compilation -----------------------------------------------------------


def _compile_leaf(f, *, catalog: dict, partition: str) -> pl.Expr:
    prop = f["property"]
    op = f["op"]
    lhs = _operand(prop, catalog=catalog, partition=partition) if isinstance(prop, dict) else pl.col(prop)
    value = f["value"]
    spec = catalog.get(prop) if isinstance(prop, str) else None
    if (
        op in _OPS
        and spec is not None
        and spec["dtype"] == "date"
        and isinstance(value, (str, list, tuple))
    ):
        # date literals arrive as ISO strings; parse so polars compares
        # date-to-date (parseability validated in _leaf_errors)
        value = (
            dt.date.fromisoformat(value)
            if isinstance(value, str)
            else [dt.date.fromisoformat(v) for v in value]
        )
    if op in _CROSS:
        rhs = _operand(f["value"], catalog=catalog, partition=partition)
        prev_lhs, prev_rhs = lhs.shift(1).over(partition), rhs.shift(1).over(partition)
        return (lhs > rhs) & (prev_lhs <= prev_rhs) if op == "cross_above" else (lhs < rhs) & (prev_lhs >= prev_rhs)
    if op in _LIST_OPS:
        # in/between/contains values are validated literal-only: raw scalars,
        # never operand exprs, so the raw value is the compiled form.
        return _OPS[op](lhs, value)
    return _OPS[op](lhs, _operand(value, catalog=catalog, partition=partition))


def _compile_node(node, *, catalog: dict, partition: str) -> pl.Expr:
    if "all" in node:
        return reduce(operator.and_, (_compile_node(n, catalog=catalog, partition=partition) for n in node["all"]))
    if "any" in node:
        return reduce(operator.or_, (_compile_node(n, catalog=catalog, partition=partition) for n in node["any"]))
    if "not" in node:
        return ~_compile_node(node["not"], catalog=catalog, partition=partition)
    return _compile_leaf(node, catalog=catalog, partition=partition)


def compile(scan_def, *, catalog: dict = PROPERTY_CATALOG, partition: str = "symbol") -> pl.Expr:
    """AND all top-level filter nodes into one polars predicate expression."""
    errors = _collect(scan_def, catalog=catalog)
    if errors:
        raise ValueError(errors[0])
    expr = pl.lit(True)
    for node in scan_def.get("filters") or []:
        expr = expr & _compile_node(node, catalog=catalog, partition=partition)
    return expr


def apply(frame, scan_def, *, catalog: dict = PROPERTY_CATALOG, partition: str = "symbol"):
    """Filter + order_by + limit a frame (eager or lazy) by a scan definition."""
    out = frame.filter(compile(scan_def, catalog=catalog, partition=partition))
    order_by = scan_def.get("order_by") or []
    if order_by:
        keys = [ob["property"] for ob in order_by]
        dirs = [ob.get("dir", "asc") == "desc" for ob in order_by]
        out = out.sort(keys, descending=dirs)
    limit = scan_def.get("limit")
    if limit is not None:
        out = out.head(limit)
    return out


# --- catalogs --------------------------------------------------------------


def catalog_from_schema(frame) -> dict[str, dict[str, str]]:
    """polars schema (DataFrame or LazyFrame) -> catalog; unmapped dtypes skipped."""
    mapping = (
        (pl.Boolean, "bool"),
        (pl.Int64, "int"),
        (pl.Int32, "int"),
        (pl.Int16, "int"),
        (pl.Int8, "int"),
        (pl.UInt64, "int"),
        (pl.UInt32, "int"),
        (pl.UInt16, "int"),
        (pl.UInt8, "int"),
        (pl.Float64, "float"),
        (pl.Float32, "float"),
        (pl.String, "str"),
        (pl.Date, "date"),
        (pl.Datetime, "date"),
    )
    return {
        name: {"label": name, "dtype": dtype}
        for name, dt in frame.collect_schema().items()
        for base, dtype in mapping
        if isinstance(dt, base)
    }
