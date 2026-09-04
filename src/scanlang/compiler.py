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

import copy
import datetime as dt
import operator
from collections.abc import Callable
from functools import reduce

import polars as pl

from scanlang.indicators import INDICATORS

__all__ = ["PROPERTY_CATALOG", "apply", "catalog_from_schema", "compile", "validate"]


def _sql_indicators() -> dict:
    """SQL_INDICATORS, imported lazily (duckdb_sql imports this module)."""
    from scanlang.duckdb_sql import SQL_INDICATORS

    return SQL_INDICATORS

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

"""Default property catalog: what ``score_bars`` output can be scanned on.

Maps property name -> ``{"label": <display name>, "dtype": <one of str,
int, float, bool, date>}``. ``date`` values arrive as ISO date strings.
This is the default ``catalog=`` for [`compile`](api.md#scanlang.compiler.compile),
[`validate`](api.md#scanlang.compiler.validate), and [`apply`](api.md#scanlang.compiler.apply);
extend with your own dict (or [`catalog_from_schema`](api.md#scanlang.compiler.catalog_from_schema))
to scan on other columns.
"""

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
            entry = INDICATORS.get(spec["fn"])
            if entry is None:
                # validated only under engine="duckdb" (SQL_INDICATORS name):
                # the polars compiler has no lowering for it — compile_sql/apply_sql
                raise ValueError(
                    f"indicator {spec['fn']!r} is SQL-only — compile()/apply() lower "
                    "polars predicates; use compile_sql()/apply_sql()"
                )
            arg_spec, builder, _req = entry
            if sum(tag == "int" for tag in arg_spec) > 1:
                # multiple int slots (stoch_k/stoch_d): the builder gets one
                # list of all of them in place of the individual ints
                ints = [a for tag, a in zip(arg_spec, spec["args"]) if tag == "int"]
                parsed = [
                    ints if tag == "int" else _operand(a, catalog=catalog, partition=partition)
                    for tag, a in zip(arg_spec, spec["args"])
                ]
            else:
                parsed = [
                    a if tag == "int" else _operand(a, catalog=catalog, partition=partition)
                    for tag, a in zip(arg_spec, spec["args"])
                ]
            try:
                built = builder(*parsed, partition=partition)
            except ImportError as e:
                # optional talib parity builders (adx) — the extra is not installed
                raise ValueError(
                    f"indicator {spec['fn']!r} requires the optional 'talib' extra "
                    "(pip install 'scanlang[talib]')"
                ) from e
            if not isinstance(built, pl.Expr):
                # talib parity builders (adx) return DataFrame -> DataFrame callables
                # for group_by(partition, maintain_order=True).map_groups — eager
                # only. apply() pre-stages the reserved ``__adx`` column; a bare
                # compile() targets that column (apply() compiles a rewritten
                # deep copy of the scan def), so this still validates AND compiles.
                return pl.col("__adx")
            return built
        key = next(k for k in spec if k in _ARITH)
        vals = [_operand(a, catalog=catalog, partition=partition) for a in spec[key]]
        if len(vals) == 1:  # unary fold; freeze names only negate: {"-": [x]}
            return -vals[0] if key == "-" else vals[0]
        return reduce(_ARITH[key], vals)
    return pl.lit(spec)


def _operand_errors(spec, where: str, catalog: dict, errors: list[str], engine: str) -> None:
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
        sql = _sql_indicators()
        entry = None
        if isinstance(name, str):
            if name in INDICATORS:
                entry = INDICATORS[name]
            elif name in sql:
                entry = sql[name]
                if engine != "duckdb":
                    errors.append(
                        f"{where}: indicator {name!r} requires engine='duckdb'"
                    )
        if entry is None:
            if not isinstance(name, str) or (name not in INDICATORS and name not in sql):
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
                _operand_errors(a, f"{where}.args[{i}]", catalog, errors, engine)
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
                _operand_errors(a, f"{where}.{ks[0]}[{i}]", catalog, errors, engine)
    else:
        errors.append(f"{where}: operand must be col, fn, or arithmetic, got keys {sorted(spec)}")


# --- node validation -------------------------------------------------------


def _val_ok(dtype: str, value) -> bool:
    return _ok_date(value) if dtype == "date" else _is(dtype, value)


def _leaf_errors(f, where: str, catalog: dict, errors: list[str], engine: str) -> None:
    prop = f.get("property")
    op = f.get("op")
    computed_lhs = isinstance(prop, dict)
    spec = catalog.get(prop) if isinstance(prop, str) else None
    if computed_lhs:
        _operand_errors(prop, f"{where}.property", catalog, errors, engine)
    elif spec is None:
        errors.append(f"{where}: unknown property: {prop!r}")
    if op not in _OPS and op not in _CROSS:
        errors.append(f"{where}: unknown operator: {op!r}")
        return
    value = f.get("value")
    if op in _CROSS:
        _operand_errors(value, f"{where}.value", catalog, errors, engine)
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
        _operand_errors(value, f"{where}.value", catalog, errors, engine)
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


def _node_errors(node, where: str, catalog: dict, errors: list[str], engine: str) -> None:
    if not isinstance(node, dict):
        errors.append(f"{where}: node must be an object, got {node!r}")
    elif "all" in node or "any" in node:
        key = "all" if "all" in node else "any"
        kids = node[key]
        if not isinstance(kids, list) or not kids:
            errors.append(f"{where}.{key} must be a nonempty list")
        else:
            for i, kid in enumerate(kids):
                _node_errors(kid, f"{where}.{key}[{i}]", catalog, errors, engine)
    elif "not" in node:
        if not isinstance(node["not"], dict):
            errors.append(f"{where}.not must be an object")
        else:
            _node_errors(node["not"], f"{where}.not", catalog, errors, engine)
    else:
        _leaf_errors(node, where, catalog, errors, engine)


def _collect(scan_def, *, catalog: dict, engine: str = "polars") -> list[str]:
    """Collect every validation error in the definition (empty list = valid)."""
    if not isinstance(scan_def, dict):
        return [f"scan definition must be an object, got {scan_def!r}"]
    errors: list[str] = []
    filters = scan_def.get("filters") or []
    if not isinstance(filters, list):
        errors.append("filters must be a list")
    else:
        for i, node in enumerate(filters):
            _node_errors(node, f"filters[{i}]", catalog, errors, engine)
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


def validate(scan_def: dict, *, catalog: dict = PROPERTY_CATALOG, engine: str = "polars") -> list[str]:
    """Validate a scan definition against the catalog without compiling it.

    Returns a list of error strings (empty = valid). Each error is keyed to
    the offending field path (``"filters[0].value"``, ``"order_by[1].dir"``,
    ``"limit"``) so a UI can highlight inline. Total on literal leaves
    (dtype-checked, never a polars error at filter time); structural on
    computed operands (a wrong-dtype join surfaces at collect time).

    With ``engine="duckdb"`` indicator names that exist only in
    ``scanlang.duckdb_sql.SQL_INDICATORS`` (``macd``, ``bbands_upper``,
    ``bbands_lower``, ``aroon``, ``cdlengulfing``,
    ``ht_trendline``, ``stoch_k``, ``stoch_d``) validate OK; under the
    default ``engine="polars"`` they produce ``indicator 'aroon' requires
    engine='duckdb'``. ``adx`` and ``kama`` are dual-engine: they have
    ``INDICATORS`` parity builders (the ``talib`` extra, applied via
    group_by/map_groups over the partition) as well as ``SQL_INDICATORS``
    ``t_adx``/``t_kama`` lowerings, so they validate under both engines
    without the duckdb-only error.

    Args:
        scan_def: A scan-def dict (``{"filters": [...], "order_by": ...,
            "limit": ...}``) or any object that has the same shape (UI
            drafts, JSON from the Lab, parser output).
        catalog: Property -> ``{"label", "dtype"}`` mapping. Default
            ``PROPERTY_CATALOG`` (mirrors ``score_bars`` output).
        engine: ``"polars"`` (default) or ``"duckdb"``. Selects which
            indicator registry defines the accepted ``{"fn": ...}`` names.

    Returns:
        A list of error strings. Empty list == valid.

    Examples:
        >>> validate({"filters": [{"property": "score", "op": ">=", "value": 40}]})
        []
        >>> validate({"filters": [{"property": "score", "op": "~=", "value": 40}]})
        ["filters[0]: unknown operator: '~='"]
    """
    return _collect(scan_def, catalog=catalog, engine=engine)


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
        # a constant has no previous bar — shifting it nulls every row and
        # silently drops all hits; only column-bearing operands lag. Covers
        # literals AND constant-folding arithmetic ({"+": [10.5, 0.5]}).
        prev_lhs = lhs.shift(1).over(partition) if lhs.meta.root_names() else lhs
        prev_rhs = rhs.shift(1).over(partition) if rhs.meta.root_names() else rhs
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


# --- eager-only parity indicators (the talib extra) --------------------------


def _eager_fn_nodes(nodes, found: list[dict]) -> None:
    """Collect every ``{"fn": ...}`` operand dict into ``found`` (recursive).

    Walks exactly what ``_operand`` reaches: logical nodes (``all``/``any``/
    ``not``), the filter's ``property`` and ``value`` operands, fn ``args``
    and arithmetic trees — so ``apply()`` stages every eager builder the
    compiled predicate can reference, in any operand position.
    """
    for nd in nodes or []:
        if not isinstance(nd, dict):
            continue
        if "not" in nd:
            _eager_fn_nodes([nd["not"]], found)
        elif "all" in nd or "any" in nd:
            _eager_fn_nodes(nd.get("all") or nd.get("any"), found)
        else:
            _eager_fn_operands(nd.get("property"), found)
            _eager_fn_operands(nd.get("value"), found)


def _eager_fn_operands(spec, found: list[dict]) -> None:
    """Collect ``{"fn": ...}`` operand dicts under one operand spec."""
    if not isinstance(spec, dict):
        return
    if "fn" in spec:
        found.append(spec)
        args = spec.get("args") or []  # nested fn args recurse
        for a in args:
            _eager_fn_operands(a, found)
    elif key := next((k for k in spec if k in _ARITH), None):
        for a in spec[key]:
            _eager_fn_operands(a, found)


def compile(scan_def: dict, *, catalog: dict = PROPERTY_CATALOG, partition: str = "symbol", engine: str = "polars") -> pl.Expr:
    """Compile a scan definition into a single polars predicate expression.

    ANDs the top-level ``filters`` list into one ``pl.Expr``. Validates the
    definition first — raises ``ValueError`` with the first error string
    if validation fails. The returned expression is shape-preserving: it
    can be folded into a ``filter``, ``with_columns``, or any other polars
    expression.

    Args:
        scan_def: A scan-def dict. Must pass ``validate()``.
        catalog: Property -> ``{"label", "dtype"}`` mapping.
        partition: Column name for window ops (``rsi``, ``ema``, ``atr``,
            ``cross_above``, ...). Every window op becomes ``.over(partition)``.
        engine: Which indicator registry validates ``{"fn": ...}`` names
            (see [`validate`](api.md#scanlang.compiler.validate)). Only
            ``"polars"`` (default) yields a compilable predicate — this is
            the polars engine; the kwarg exists for API consistency with
            ``validate`` and ``compile_sql``.

    Returns:
        A single ``pl.Expr`` predicate. Apply with
        ``frame.filter(compile(scan_def))`` or via
        [`apply`](api.md#scanlang.compiler.apply).

    Raises:
        ValueError: if ``scan_def`` fails validation; the message is the
            first error string from ``validate()``.

    Examples:
        >>> import polars as pl
        >>> expr = compile({"filters": [{"property": "score", "op": ">=", "value": 40}]})
        >>> pl.DataFrame({"score": [10, 50]}).filter(expr).height
        1
    """
    errors = _collect(scan_def, catalog=catalog, engine=engine)
    if errors:
        raise ValueError(errors[0])
    expr = pl.lit(True)
    for node in scan_def.get("filters") or []:
        expr = expr & _compile_node(node, catalog=catalog, partition=partition)
    return expr


def apply(frame: pl.DataFrame | pl.LazyFrame, scan_def: dict, *, catalog: dict = PROPERTY_CATALOG, partition: str = "symbol", engine: str = "polars") -> pl.DataFrame | pl.LazyFrame:
    """Filter + ``order_by`` + ``limit`` a frame by a scan definition.

    Shape-preserving: ``DataFrame`` in -> ``DataFrame`` out,
    ``LazyFrame`` in -> ``LazyFrame`` out. The compiled predicate folds
    into the frame's plan; collect at your edge.

    Args:
        frame: A polars ``DataFrame`` or ``LazyFrame``. Caller sorts
            ``(partition, time)`` ascending — the contract is the caller's,
            not scanlang's.
        scan_def: A scan-def dict. Must pass ``validate()``.
        catalog: Property -> ``{"label", "dtype"}`` mapping.
        partition: Column name for window ops and the sort key.
        engine: Which indicator registry validates ``{"fn": ...}`` names;
            forwarded to ``compile()`` (see its docstring).

    Returns:
        A frame of the same kind as the input, with the scan applied.

    Raises:
        ValueError: if ``scan_def`` fails validation.

    Examples:
        >>> import polars as pl
        >>> df = pl.DataFrame({"symbol": ["A", "B"], "score": [60, 20]})
        >>> apply(df, {"filters": [{"property": "score", "op": ">=", "value": 40}]})
        shape: (1, 2)
        ┌────────┬───────┐
        │ symbol ┆ score │
        │ ---    ┆ ---   │
        │ str    ┆ i64   │
        ╞════════╪═══════╡
        │ A      ┆ 60    │
        └────────┴───────┘
    """
    # talib parity indicators (adx: group_by(partition).map_groups) are
    # eager-only — probe each fn's builder (the full operand grammar, via the
    # deep copy below): a non-Expr return is the seam, so its column is
    # pre-staged here and the predicate compiles against the materialized
    # alias. A LazyFrame input cannot run the seam (no lazy map_groups), so
    # it fails with the install hint instead.
    staged_def = copy.deepcopy(scan_def)  # staging rewrites the copy's fn
    fn_nodes: list[dict] = []  # dicts in place; the caller's scan_def stays intact
    _eager_fn_nodes(staged_def.get("filters") or [], fn_nodes)
    eager: list[tuple[dict, Callable]] = []
    for prop in fn_nodes:
        entry = INDICATORS.get(prop["fn"])
        if entry is None:
            continue  # unknown fn — compile() raises the real error
        arg_spec, builder, _req = entry
        try:
            if sum(tag == "int" for tag in arg_spec) > 1:  # stoch_k/stoch_d: tuple of ints
                ints = [a for tag, a in zip(arg_spec, prop["args"]) if tag == "int"]
                built = builder(
                    *(ints if tag == "int" else pl.col("_") for tag, a in zip(arg_spec, prop["args"])),
                    partition=partition,
                )
            else:
                built = builder(
                    *(a if tag == "int" else pl.col("_") for tag, a in zip(arg_spec, prop["args"])),
                    partition=partition,
                )
        except ImportError:
            continue  # missing talib extra — compile() reports the install hint
        if not isinstance(built, pl.Expr):
            eager.append((prop, built))
    if eager and not isinstance(frame, pl.DataFrame):
        raise ValueError(
            f"indicator {eager[0][0]['fn']!r} requires the optional 'talib' extra "
            "(pip install 'scanlang[talib]') and an eager frame — collect() first"
        )
    staged = frame
    cat = dict(catalog)
    for j, (prop, apply_fn) in enumerate(eager):
        alias = f"__{prop['fn']}_{j}"
        staged = (
            staged.group_by(partition, maintain_order=True)
            .map_groups(lambda g, fn=apply_fn, a=alias: fn(g).rename({"__adx": a}))
        )
        prop.clear()
        prop["col"] = alias
        cat[alias] = {"label": alias, "dtype": "float"}  # talib output is Float64
    out = staged.filter(compile(staged_def, catalog=cat, partition=partition, engine=engine))
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


def catalog_from_schema(frame: pl.DataFrame | pl.LazyFrame) -> dict[str, dict[str, str]]:
    """Build a catalog dict from a polars frame's schema.

    Walks the frame's schema (DataFrame or LazyFrame) and emits
    ``{name: {"label": name, "dtype": <simplified>}}`` for every column
    with a mappable dtype. Dtype mapping:

    - ``Bool``, ``Int*``, ``UInt*`` -> ``"int"`` (bool becomes ``"bool"``)
    - ``Float*`` -> ``"float"``
    - ``String`` -> ``"str"``
    - ``Date``, ``Datetime`` -> ``"date"``

    Unmappable dtypes (lists, structs, categoricals, durations, time,
    object, decimal, ...) are silently skipped. Add them by hand if you
    need them validated.

    Args:
        frame: A polars ``DataFrame`` or ``LazyFrame``. Only the schema is
            inspected — the frame does not need to contain any rows.

    Returns:
        A ``dict[str, {"label", "dtype"}]`` suitable for passing as the
        ``catalog=`` argument to [`apply`](api.md#scanlang.compiler.apply),
        [`compile`](api.md#scanlang.compiler.compile),
        [`validate`](api.md#scanlang.compiler.validate).

    Examples:
        >>> import polars as pl
        >>> catalog_from_schema(pl.DataFrame({"close": [1.0], "score": [60]}))
        {'close': {'label': 'close', 'dtype': 'float'}, 'score': {'label': 'score', 'dtype': 'int'}}
    """
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
