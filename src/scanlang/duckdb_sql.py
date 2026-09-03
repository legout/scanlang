"""duckdb SQL backend: compile scan defs into parameterized SQL (talib ``t_*`` form).

[`compile_sql`](#scanlang.duckdb_sql.compile_sql) turns a scan-def dict into
``(sql, params)``; [`apply_sql`](#scanlang.duckdb_sql.apply_sql) runs it on a
``duckdb`` connection and returns an eager ``pl.DataFrame`` (duckdb has no
polars-lazy plan). Same IR and validation as the polars engine —
[`validate`](api.md#scanlang.compiler.validate) runs unchanged first, so error
strings are identical. Injection contract is the same as ``compile()``:
nothing user-controlled is string-interpolated — literals bind as ``?``
params, identifiers are double-quoted, and ``relation`` must be a plain
identifier (``[A-Za-z_][A-Za-z0-9_]*``, never a path/URL — register a view
first).

Indicator lowering, two tiers ([`SQL_INDICATORS`](#scanlang.duckdb_sql.SQL_INDICATORS)
mirrors the ``INDICATORS`` entry contract; ``INDICATORS`` stays polars-only):

- ``sma/rmin/rmax/shift`` -> native window functions over
  ``(partition, order_column)``, with a ``count``-guard ``CASE`` so warm-up
  rows are NULL exactly like polars ``rolling_*``.
- ``ema/rsi/atr`` -> community talib ``t_*`` scalar form: per-partition list
  CTE, ``t_fn`` over the lists, unnest back (the benchmark's fastest form;
  ``ta_*`` window functions are 30-35x slower and are not used). ``t_*``
  front-pads its result to input length, so unnest against the session list
  is row-aligned. Warm-up rows come back NULL until the lookback fills —
  unlike the polars engine's ``ema`` (``ewm_mean(adjust=False)``), which
  emits from bar 0, so pre-lookback rows diverge for ``ema`` by design
  (the accepted warm-up contract, Q1 of the 2026-09-02 plan; hit-set
  equality is therefore only claimed for sma-family scans).

Nested computed operands (``sma(rsi(close, 14), 5)``) stage as successive
row-aligned CTEs — one column per indicator call. The probe answer
(duckdb 1.5.5 + talib community extension, verified 2026-09-03) is: **list
nesting WORKS** — ``t_sma(t_rsi(list(close ORDER BY session), 14), 5)``
returns a list — but staged CTEs are used anyway: ``cross_*`` needs lag
staging regardless (window functions cannot nest in duckdb), and
per-indicator columns keep every builder trivial.

Probe results, restated for maintainers: ``list()`` keeps nulls (unnest
zips 1:1); ``?`` params are accepted in window frame bounds, ``lag``
offsets, and ``t_*`` periods (bare ``?`` binds as DATE in untyped context —
the builders CAST to INTEGER); no pyarrow is required (``fetchall()``).

Known corner: ``sma`` over a column with interior nulls diverges from polars
in the affected windows (duckdb ``AVG`` skips nulls, polars null-propagates),
and a perfectly flat series gets RSI 0 from TA-Lib where the polars engine's
zero-loss guard pins 100 (the 0/0 case is undefined; each engine documents
its choice). Complete frames — the contract's caller-sorted OHLCV — are exact.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Callable

import polars as pl

from scanlang.compiler import PROPERTY_CATALOG, _collect

__all__ = ["SQL_INDICATORS", "apply_sql", "compile_sql"]

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_CMP = {">=": ">=", "<=": "<=", ">": ">", "<": "<", "==": "=", "!=": "<>"}
_ARITH_KEYS = ("+", "-", "*", "/")
_SQL_TYPES = {"str": "VARCHAR", "int": "BIGINT", "float": "DOUBLE", "bool": "BOOLEAN", "date": "DATE"}
_WINDOW = frozenset(("sma", "rmin", "rmax", "shift"))  # rest -> t_* scalar tier


def _q(name: str) -> str:
    """Quote an identifier (catalog/caller-supplied names are never interpolated)."""
    return '"' + name.replace('"', '""') + '"'


def _as_date(dtype: str | None, v):
    """Bind date literals as datetime.date (validate() proved parseability)."""
    if dtype == "date" and isinstance(v, str):
        return dt.date.fromisoformat(v)
    return v


# --- indicator SQL builders -------------------------------------------------
#
# Signature: builder(x, n, partition, order_column, params) -> SQL expression.
# ``x`` is the compiled operand SQL (None for atr, which takes no expr arg).
# Builders append their own params in string-occurrence order; ``params`` is
# positional ('?') and must stay in lockstep with the assembled SQL.


def _win(x: str, n: int, p: str, o: str, params: list, agg: str) -> str:
    """Rolling AVG/MIN/MAX, NULL until n rows — matches polars rolling_* warm-up.

    The frame is written twice, so any params embedded in ``x`` (e.g. an
    arithmetic operand with a literal) repeat with it.
    """
    frame = f"PARTITION BY {_q(p)} ORDER BY {_q(o)} ROWS BETWEEN ? PRECEDING AND CURRENT ROW"
    xp = params[len(params) - x.count("?") :] if "?" in x else []
    params += [n - 1, n, *xp, n - 1]
    return f"CASE WHEN count({x}) OVER ({frame}) = ? THEN {agg}({x}) OVER ({frame}) END"


def _lag(x: str, n: int, p: str, o: str, params: list) -> str:
    params.append(n)
    return f"lag({x}, ?) OVER (PARTITION BY {_q(p)} ORDER BY {_q(o)})"


def _tcall(fn: str, x: str, n: int, o: str, params: list) -> str:
    params.append(n)
    # CAST: duckdb binds bare ? params as DATE when the context can't infer
    return f"{fn}(list({x} ORDER BY {_q(o)}), CAST(? AS INTEGER))"


def _tatr(x, n: int, p: str, o: str, params: list) -> str:
    params.append(n)
    cols = (f"list({_q(c)} ORDER BY {_q(o)})" for c in ("high", "low", "close"))
    return f"t_atr({', '.join(cols)}, CAST(? AS INTEGER))"


# name -> (arg_spec, sql_builder, required_cols) — mirrors INDICATORS' contract.
# The entry shape is the extension point for the SQL engine.
SQL_INDICATORS: dict[str, tuple[tuple[str, ...], Callable, tuple[str, ...]]] = {
    "sma": (("expr", "int"), lambda x, n, p, o, pa: _win(x, n, p, o, pa, "AVG"), ()),
    "rmin": (("expr", "int"), lambda x, n, p, o, pa: _win(x, n, p, o, pa, "MIN"), ()),
    "rmax": (("expr", "int"), lambda x, n, p, o, pa: _win(x, n, p, o, pa, "MAX"), ()),
    "shift": (("expr", "int"), _lag, ()),
    "ema": (("expr", "int"), lambda x, n, p, o, pa: _tcall("t_ema", x, n, o, pa), ()),
    "rsi": (("expr", "int"), lambda x, n, p, o, pa: _tcall("t_rsi", x, n, o, pa), ()),
    "atr": (("int",), _tatr, ("high", "low", "close")),
}

"""SQL indicator registry: the ``{"fn": name}`` lowering table for this module.

Same entry shape as ``scanlang.indicators.INDICATORS`` (arg_spec, builder,
required_cols), but builders emit SQL fragments instead of ``pl.Expr`` and
take ``(x, n, partition, order_column, params)``. Extend by insertion —
``SQL_INDICATORS["roc"] = (("expr", "int"), builder, ())``.
"""


# --- compilation ------------------------------------------------------------


class _Gen:
    """Stages the scan as successive row-aligned CTEs; collects '?' params.

    Staging is DEFERRED: walkers append emitters to ``pending`` and
    ``compile_sql`` calls ``flush()`` once, after the whole walk — every CTE
    is then assembled from the FINAL ``cols``/``computed`` sets, so columns
    or aliases discovered after a fn was walked still ride through its CTE
    (mid-walk snapshots were the source of dropped-column binder errors).
    Emitters run in walk order, which is dependency order (an operand's fn
    is always walked before its consumer). Params split by TEXT position:
    ``params`` feeds the CTE region (emitted before the final SELECT),
    ``tail`` feeds WHERE + LIMIT. ``sink`` points at whichever region the
    currently-generated text renders in — operand literals inside ``fn()``
    args and cross operands land in the CTE text, leaf predicates land in
    WHERE — so param order always matches text order.
    """

    def __init__(self, catalog: dict, partition: str, order_column: str):
        self.cat = catalog
        self.p = partition
        self.o = order_column
        self.cols: set[str] = {partition, order_column}  # base columns needed
        self.params: list = []  # CTE-region params
        self.tail: list = []  # WHERE + LIMIT params
        self.sink = self.params
        self.ctes: list[str] = []
        self.prev = "s0"
        self.pending: list[Callable[[], None]] = []  # deferred CTE emitters
        self.computed: list[str] = []  # aliases materialized so far
        self.n = 0  # alias counter
        self.stage = 0

    def flush(self) -> None:
        """Emit every staged CTE, in walk order (called once, after the walk)."""
        for emit in self.pending:
            emit()

    def _emit_w(self, expr: str, alias: str) -> None:
        """Window-tier CTE; ``SELECT *`` carries every earlier column/alias."""
        def emit() -> None:
            self.stage += 1
            self.ctes.append(f"s{self.stage} AS (SELECT *, {expr} FROM {self.prev})")
            self.prev = f"s{self.stage}"
            self.computed.append(alias)

        self.pending.append(emit)

    def _emit_t(self, list_frag: str, unnest_frag: str, alias: str) -> None:
        """t_*-tier CTE: per-partition lists, t_fn over them, unnest back.

        Reads ``self.cols``/``self.computed`` at flush time (final values),
        so sibling filters compiled after this fn still find their columns
        and every earlier alias here.
        """
        def emit() -> None:
            bases = sorted(self.cols - {self.p, self.o})
            inner = [f"list({_q(c)} ORDER BY {_q(self.o)}) AS _b{i}" for i, c in enumerate(bases)]
            outer = [f"unnest(_b{i}) AS {_q(c)}" for i, c in enumerate(bases)]
            for i, a in enumerate(self.computed):
                inner.append(f"list({_q(a)} ORDER BY {_q(self.o)}) AS _c{i}")
                outer.append(f"unnest(_c{i}) AS {_q(a)}")
            self.stage += 1
            name = f"s{self.stage}"
            self.ctes.append(
                f"{name} AS (SELECT {_q(self.p)}, unnest(_o) AS {_q(self.o)}, {', '.join(outer + [unnest_frag])} "
                f"FROM (SELECT {_q(self.p)}, "
                f"list({_q(self.o)} ORDER BY {_q(self.o)}) AS _o, "
                f"{', '.join(inner + [list_frag])} "
                f"FROM {self.prev} GROUP BY {_q(self.p)}))"
            )
            self.prev = name
            self.computed.append(alias)

        self.pending.append(emit)

    def _emit_x(self, lhs: str, rhs: str, xa: str, xb: str, la: str, lb: str) -> None:
        """Cross CTE: materialize both operands, then lag the columns.

        The inner projection renders each operand fragment exactly ONCE (its
        params bind once — repeating the fragment inside ``lag`` would demand
        its params twice); the outer SELECT lags the materialized columns
        (window functions cannot nest, hence the staging). The WHERE
        predicate references only the fresh alias columns, so operand
        literals never re-render outside the CTE.
        """

        def emit() -> None:
            w = f"PARTITION BY {_q(self.p)} ORDER BY {_q(self.o)}"
            self.stage += 1
            self.ctes.append(
                f"s{self.stage} AS (SELECT *, lag({xa}, 1) OVER ({w}) AS {la}, "
                f"lag({xb}, 1) OVER ({w}) AS {lb} "
                f"FROM (SELECT *, {lhs} AS {xa}, {rhs} AS {xb} FROM {self.prev}))"
            )
            self.prev = f"s{self.stage}"
            # all four ride through later t-tier CTEs (WHERE references them)
            self.computed += [xa, xb, la, lb]

        self.pending.append(emit)

    def operand(self, spec) -> str:
        if isinstance(spec, dict):
            if "col" in spec:
                self.cols.add(spec["col"])
                return _q(spec["col"])
            if "fn" in spec:
                return self.fn(spec)
            key = next(k for k in spec if k in _ARITH_KEYS)
            vals = [self.operand(a) for a in spec[key]]
            if len(vals) == 1:  # unary fold; freeze names only negate
                return f"(-{vals[0]})" if key == "-" else vals[0]
            return f"({f' {key} '.join(vals)})"
        self.sink.append(spec)
        return "?"

    def fn(self, spec) -> str:
        name = spec["fn"]
        arg_spec, builder, req = SQL_INDICATORS[name]
        self.cols.update(req)
        pos, n = [], None
        outer_sink, self.sink = self.sink, self.params  # fn args render in CTE text
        try:
            for tag, a in zip(arg_spec, spec["args"]):
                if tag == "int":
                    n = a
                else:
                    pos.append(self.operand(a))
            alias = f"c{self.n}"
            expr = builder(pos[0] if pos else None, n, self.p, self.o, self.params)
        finally:
            self.sink = outer_sink
        if name in _WINDOW:
            self._emit_w(f"{expr} AS {alias}", alias)
        else:
            self._emit_t(f"{expr} AS _v{self.n}", f"unnest(_v{self.n}) AS {alias}", alias)
        self.n += 1
        return alias

    def leaf(self, f) -> str:
        prop, op = f["property"], f["op"]
        cross = op in ("cross_above", "cross_below")
        if isinstance(prop, str):
            self.cols.add(prop)
            lhs, dtype = _q(prop), self.cat[prop]["dtype"]
        else:
            # computed LHS: renders in the cross CTE (params) or in WHERE (tail)
            outer, self.sink = self.sink, self.params if cross else self.tail
            try:
                lhs, dtype = self.operand(prop), None
            finally:
                self.sink = outer
        if cross:
            # offset 1 is compiler-structural (freeze: previous bar)
            xa, xb = f"x{self.n}a", f"x{self.n}b"
            la, lb = f"x{self.n}c", f"x{self.n}d"
            outer, self.sink = self.sink, self.params  # operands render in CTE text
            try:
                rhs = self.operand(f["value"])
                self._emit_x(lhs, rhs, xa, xb, la, lb)
            finally:
                self.sink = outer
            self.n += 1
            if op == "cross_above":
                return f"({xa} > {xb} AND {la} <= {lb})"
            return f"({xa} < {xb} AND {la} >= {lb})"
        # remaining predicate text renders in the final SELECT -> tail region
        outer, self.sink = self.sink, self.tail
        try:
            if op == "between":
                lo, hi = f["value"]
                cast = _SQL_TYPES[dtype or "str"]  # catalog literal (validate())
                self.tail += [_as_date(dtype, lo), _as_date(dtype, hi)]
                return f"({lhs} BETWEEN CAST(? AS {cast}) AND CAST(? AS {cast}))"
            if op == "in":
                vals = [_as_date(dtype, v) for v in f["value"]]
                self.tail += vals
                # CAST: duckdb cannot infer ? types inside an IN list
                elem = f"CAST(? AS {_SQL_TYPES[dtype or 'str']})"
                return f"({lhs} IN ({', '.join([elem] * len(vals))}))"
            if op == "contains":
                self.tail.append(f["value"])
                return f"contains({lhs}, ?)"
            value = f["value"]
            if isinstance(value, dict):
                rhs = self.operand(value)
            else:
                self.tail.append(_as_date(dtype, value))
                rhs = "?"
            return f"({lhs} {_CMP[op]} {rhs})"
        finally:
            self.sink = outer

    def node(self, nd) -> str:
        if "all" in nd:
            return "(" + " AND ".join(self.node(k) for k in nd["all"]) + ")"
        if "any" in nd:
            return "(" + " OR ".join(self.node(k) for k in nd["any"]) + ")"
        if "not" in nd:
            return f"(NOT {self.node(nd['not'])})"
        return self.leaf(nd)


def compile_sql(
    scan_def: dict,
    *,
    relation: str,
    catalog: dict = PROPERTY_CATALOG,
    partition: str = "symbol",
    order_column: str = "session",
) -> tuple[str, list]:
    """Compile a scan definition into parameterized duckdb SQL.

    Validates exactly like [`compile`](api.md#scanlang.compiler.compile)
    (same error strings), then lowers the IR: groups/ops/arithmetic become
    SQL with ``?`` params, indicators stage as row-aligned CTEs (see the
    module docstring). Output columns: ``partition``, ``order_column``, every
    referenced base column, and one ``c<N>`` per indicator call.

    Args:
        scan_def: A scan-def dict. Must pass ``validate()``.
        relation: A plain identifier (``[A-Za-z_][A-Za-z0-9_]*``) naming a
            table or view already attached to the connection — never a
            path/URL. Register one with ``CREATE VIEW ... AS SELECT * FROM
            'file.parquet'`` or a registered frame.
        catalog: Property -> ``{"label", "dtype"}`` mapping.
        partition: Column name for window ops (per-symbol semantics).
        order_column: Column name defining bar order within a partition.

    Returns:
        ``(sql, params)`` — run with ``con.execute(sql, params)``.

    Raises:
        ValueError: on validation failure (same first-error message as
            ``compile()``) or a non-identifier ``relation``.
    """
    errors = _collect(scan_def, catalog=catalog)
    if errors:
        raise ValueError(errors[0])
    if not isinstance(relation, str) or not _IDENT.fullmatch(relation):
        raise ValueError(
            f"relation must be a plain identifier matching [A-Za-z_][A-Za-z0-9_]*, got {relation!r}"
        )
    g = _Gen(catalog, partition, order_column)
    preds = [g.node(nd) for nd in (scan_def.get("filters") or [])]
    g.cols.update(ob["property"] for ob in scan_def.get("order_by") or [])
    g.flush()
    cols = ", ".join(_q(c) for c in sorted(g.cols))
    sql = "WITH s0 AS (SELECT " + cols + f" FROM {relation})"
    if g.ctes:
        sql += ", " + ", ".join(g.ctes)
    sql += f" SELECT * FROM {g.prev}"
    if preds:
        sql += " WHERE " + " AND ".join(preds)
    obs = scan_def.get("order_by") or []
    if obs:
        sql += " ORDER BY " + ", ".join(
            f"{_q(ob['property'])}{' DESC' if ob.get('dir', 'asc') == 'desc' else ' ASC'}" for ob in obs
        )
    if scan_def.get("limit") is not None:
        sql += " LIMIT ?"
        g.tail.append(scan_def["limit"])
    return sql, g.params + g.tail


def apply_sql(
    con,
    scan_def: dict,
    *,
    relation: str,
    catalog: dict = PROPERTY_CATALOG,
    partition: str = "symbol",
    order_column: str = "session",
) -> pl.DataFrame:
    """Run a scan definition on a duckdb connection; returns an eager frame.

    Ensures the community talib extension on the connection (``INSTALL talib
    FROM community; LOAD talib`` — idempotent, cached after the first call),
    executes the compiled SQL, and collects eagerly (duckdb has no
    polars-lazy plan). ``duckdb`` itself is never imported here — pass a
    connection, so scanlang without the ``duckdb`` extra still imports.

    Args:
        con: An open ``duckdb`` connection with ``relation`` attached.
        scan_def: A scan-def dict. Must pass ``validate()``.
        relation: Plain identifier for the scanned table/view.
        catalog: Property -> ``{"label", "dtype"}`` mapping.
        partition: Column name for window ops (per-symbol semantics).
        order_column: Column name defining bar order within a partition.

    Returns:
        An eager ``pl.DataFrame`` of hits (filter + ``order_by`` + ``limit``).

    Raises:
        ValueError: if ``scan_def`` fails validation or ``relation`` is not
            a plain identifier.
    """
    sql, params = compile_sql(
        scan_def, relation=relation, catalog=catalog, partition=partition, order_column=order_column
    )
    con.execute("INSTALL talib FROM community")
    con.execute("LOAD talib")
    res = con.execute(sql, params)
    cols = [d[0] for d in res.description]
    return pl.DataFrame(res.fetchall(), schema=cols, orient="row")
