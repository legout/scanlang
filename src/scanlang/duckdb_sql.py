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
mirrors the ``INDICATORS`` entry contract and is a superset of it; ``INDICATORS``
stays polars-only):

- ``sma/rmin/rmax/shift/adr`` -> native window functions over
  ``(partition, order_column)``, with a ``count``-guard ``CASE`` so warm-up
  rows are NULL exactly like polars ``rolling_*``. ``adr`` = sma of
  TR/close*100 — a two-step window (TR materialized in a subquery, then
  averaged), exact on both engines.
- ``ema/rsi/atr/roc/natr/slope`` -> community talib ``t_*`` scalar form:
  per-partition list CTE, ``t_fn`` over the lists, unnest back (the
  benchmark's fastest form; ``ta_*`` window functions are 30-35x slower and
  are not used). ``t_*`` front-pads its result to input length, so unnest
  against the session list is row-aligned. Warm-up rows come back NULL until
  the lookback fills — unlike the polars engine's ``ema``
  (``ewm_mean(adjust=False)``), which emits from bar 0, so pre-lookback rows
  diverge for ``ema`` by design (the accepted warm-up contract, Q1 of the
  2026-09-02 plan; hit-set equality is therefore only claimed for
  sma-family scans).
- duckdb-only (talib extension, no polars builder):
  ``macd`` (the MACD line), ``bbands_upper``/``bbands_lower`` (two entries —
  bands are scanned as thresholds; the middle band is just ``sma``),
  ``aroon`` (the up line), ``cdlengulfing`` (0/1 talib integer),
  ``ht_trendline``, and ``stoch_k``/``stoch_d`` (the slow %K/%D lines —
  the first ``("int", "int", "int")`` arg_spec: fast-k, slow-k, slow-d
  periods; ma_type slots stay at the talib default 0). Multi-output
  ``t_*`` functions return lists of structs;
  the builders narrow them to one struct field in SQL.
  [`validate(..., engine="duckdb")`](api.md#scanlang.compiler.validate)
  accepts these names — the polars engine rejects them. ``adx`` and
  ``kama`` are dual-engine names: their ``t_adx``/``t_kama`` lowerings
  live here AND exact TA-Lib parity builders are registered in
  ``INDICATORS`` (the ``talib`` extra, group_by/map_groups seam), so they
  validate and execute on both engines.

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
from scanlang.indicators import _RS_MOM_LOOKBACK, _RS_MOM_SPAN, _RS_SPAN

__all__ = ["SQL_INDICATORS", "apply_sql", "compile_sql"]

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_CMP = {">=": ">=", "<=": "<=", ">": ">", "<": "<", "==": "=", "!=": "<>"}
_ARITH_KEYS = ("+", "-", "*", "/")
_SQL_TYPES = {"str": "VARCHAR", "int": "BIGINT", "float": "DOUBLE", "bool": "BOOLEAN", "date": "DATE"}
_WINDOW = frozenset(("sma", "rmin", "rmax", "shift", "adr"))  # rest -> t_* scalar tier


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


def _tcol(x, n, p: str, o: str, params: list, cols: tuple[str, ...]) -> str:
    """t_* over the given base columns (no period when ``n`` is None)."""
    if n is not None:
        params.append(n)
        tail = ", CAST(? AS INTEGER)"
    else:
        tail = ""
    return f"{x}({', '.join(f'list({_q(c)} ORDER BY {_q(o)})' for c in cols)}{tail})"


def _roc(x, n: int, p: str, o: str, params: list) -> str:
    params.append(n)
    return f"t_roc(list({x} ORDER BY {_q(o)}), CAST(? AS INTEGER))"


def _slope_sql(x, n: int, p: str, o: str, params: list) -> str:
    params.append(n)
    return f"t_linearreg_slope(list({x} ORDER BY {_q(o)}), CAST(? AS INTEGER))"


def _natr(x, n: int, p: str, o: str, params: list) -> str:
    # talib NATR normalizes by close; the leading expr is grammar-uniform.
    params.append(n)
    cols = ", ".join(f"list({_q(c)} ORDER BY {_q(o)})" for c in ("high", "low", "close"))
    return f"t_natr({cols}, CAST(? AS INTEGER))"


def _macd(x, n, p: str, o: str, params: list) -> str:
    # n = fast period (slow/signal stay at talib defaults 26/9); the scan-level
    # name is the MACD line = struct field 'macd'. fn() wraps t_* output in
    # unnest; a LIST(STRUCT) cannot be field-narrowed directly, so the raw
    # list is aliased inner (_rawN) and narrowed outer (see _emit_t).
    params.extend((n, 26, 9))
    return (
        "{'macd': unnest(t_macd(list("
        f"{_q('close')} ORDER BY {_q(o)}), "
        "CAST(? AS INTEGER), CAST(? AS INTEGER), CAST(? AS INTEGER)))['macd']}"
    )


def _bband(side: str):
    def build(x, n, p: str, o: str, params: list) -> str:
        params.extend((n, 2.0, 2.0, 0))
        return (
            f"{{'{side}': unnest(t_bbands(list({_q('close')} ORDER BY {_q(o)}), "
            "CAST(? AS INTEGER), CAST(? AS DOUBLE), CAST(? AS DOUBLE), CAST(? AS INTEGER)))"
            f"['{side}']}}"
        )

    return build


def _aroon(x, n: int, p: str, o: str, params: list) -> str:
    params.append(n)
    cols = (f"list({_q(c)} ORDER BY {_q(o)})" for c in ("high", "low"))
    return (
        "{'aroon_up': unnest(t_aroon("
        f"{', '.join(cols)}, CAST(? AS INTEGER)))['aroon_up']}}"
    )


def _stoch(side: str):
    def build(x, n, p: str, o: str, params: list) -> str:
        # n = (fastk, slowk, slowd); ma_type slots stay at talib default 0.
        # fn() passes n as a list for multi-tag arg_specs (see the compiler).
        fastk, slowk, slowd = n
        params.extend((fastk, slowk, 0, slowd, 0))
        cols = (f"list({_q(c)} ORDER BY {_q(o)})" for c in ("high", "low", "close"))
        return (
            f"{{'{side}': unnest(t_stoch("
            f"{', '.join(cols)}, "
            "CAST(? AS INTEGER), CAST(? AS INTEGER), CAST(? AS INTEGER), "
            "CAST(? AS INTEGER), CAST(? AS INTEGER)))"
            f"['{side}']}}"
        )

    return build


def _raw_tfn(list_frag: str) -> str:
    """Raw ``t_*`` call inside a ``{'field': ...}`` list_frag (for the inner alias)."""
    i = list_frag.index("unnest(") + len("unnest(")
    j = list_frag.rindex(")['")  # closes unnest before the field bracket
    return list_frag[i:j]


def _raw_field(list_frag: str) -> str:
    """The ``['field']`` tail of a ``{'field': unnest(...)['field']}`` list_frag."""
    i = list_frag.rindex(")") + 1  # right after the unnest close-paren
    j = list_frag.rindex("']}")
    return list_frag[i : j + 2]


def _adx(x, n: int, p: str, o: str, params: list) -> str:
    return _tcol("t_adx", n, p, o, params, ("high", "low", "close"))


def _kama(x, n: int, p: str, o: str, params: list) -> str:
    return _tcol("t_kama", n, p, o, params, ("close",))


def _cdlengulfing(x, n, p: str, o: str, params: list) -> str:
    return _tcol("t_cdlengulfing", None, p, o, params, ("open", "high", "low", "close"))


def _ht_trendline(x, n, p: str, o: str, params: list) -> str:
    return _tcol("t_ht_trendline", None, p, o, params, ("close",))


def _adr(x, n: int, p: str, o: str, params: list) -> str:
    """ADR = sma(TR/close*100), two-step native window (exact cross-engine).

    The leading expr is accepted for close-default grammar uniformity; the
    measure itself is always TR over close (like talib NATR normalizes by
    close). Window functions cannot nest, so TR (needs lag(close)) is emitted
    as its own stage: ``fn()`` detects ``name == "adr"`` and emits an extra
    window CTE before averaging. Bar 0's lag is null -> TR null; the
    count-guard skips the first n-1 rows exactly like polars rolling_mean
    warm-up, and by row n-1 bar 0 has left every window, so the null never
    reaches a live value. avg/count skip nulls identically on both engines.
    """
    frame = f"PARTITION BY {_q(p)} ORDER BY {_q(o)} ROWS BETWEEN ? PRECEDING AND CURRENT ROW"
    params += [n - 1, n, n - 1]  # frame appears twice: count-guard + avg
    return f"CASE WHEN count(_tr) OVER ({frame}) = ? THEN avg(_tr / {_q('close')} * 100.0) OVER ({frame}) END"


def _adr_tr(p: str, o: str, params: list) -> str:
    """Stage 1 of adr: raw true range per row (null pc at bar 0 propagates)."""
    pc = f"lag({_q('close')}, 1) OVER (PARTITION BY {_q(p)} ORDER BY {_q(o)})"
    return (
        f"greatest({_q('high')} - {_q('low')}, "
        f"abs({_q('high')} - {pc}), abs({pc} - {_q('low')}))"
    )


# Temporal z-score normalization (rs_ratio / rs_momentum) — polars builders
# live in scanlang.indicators (_RS_* constants). Each emits TWO stages via
# ``_Gen._emit_rs`` (see its docstring for the probed duckdb constraints):
# stage A (list tier) smooths warm-up-safe, stage B (window tier) z-scores.
def _rs_smooth(span: int) -> str:
    """Stage-A smoothing over the fixed aliases _rs_r (raw) / _rs_nn (stripped).

    ``t_ema`` reads NULLs as zero and seeds from the first input, so the null
    warm-up prefix is stripped before the call and re-padded by PREPENDING
    the counted nulls (list_resize pads at the tail, which would shift the
    series). The empty-list CASE guards a talib internal error on ``t_ema([])``.
    """
    return (
        "CASE WHEN len(_rs_nn) = 0 THEN list_resize(CAST([NULL] AS DOUBLE[]), len(_rs_r)) "
        f"ELSE list_concat(list_resize(CAST([NULL] AS DOUBLE[]), len(_rs_r) - len(_rs_nn)), "
        f"t_ema(_rs_nn, {span})) END"
    )


def _rs_z(col: str, n: int, p: str, o: str) -> str:
    """Stage-B z-score of ``col``: trailing population z, re-center 100, clamp.

    The clamp sits INSIDE the CASE — duckdb greatest/least SKIP NULLs, so a
    clamp wrapped around the CASE would turn the warm-up NULL into 80.0.
    Literals, not bound params: the binder drops bare ``?`` params in window
    frames when other CAST(?) params exist (probed duckdb 1.5.5).
    """
    w = f"PARTITION BY {_q(p)} ORDER BY {_q(o)} ROWS BETWEEN {n - 1} PRECEDING AND CURRENT ROW"
    return (
        f"CASE WHEN count({_q(col)}) OVER ({w}) < {n} THEN NULL "
        f"WHEN stddev_pop({_q(col)}) OVER ({w}) < 1e-06 THEN 100.0 "
        f"ELSE greatest(80.0, least(120.0, 100.0 + 5.0 * ({_q(col)} - avg({_q(col)}) OVER ({w})) "
        f"/ stddev_pop({_q(col)}) OVER ({w}))) END"
    )


def _rs_smooth_router(x, n, p, o, params):
    """Placeholder — ``rs_ratio``/``rs_momentum`` lower via ``_Gen._emit_rs``.

    ``fn()`` special-cases these names BEFORE dispatching to the registry
    builder (the stage-A list CTE needs the operand fragment plus a deeper
    SELECT nest), so this is never invoked; it exists only so the registry
    entry keeps the standard 3-tuple shape.
    """
    raise NotImplementedError("rs_* lower via _Gen._emit_rs")  # pragma: no cover


# name -> (arg_spec, sql_builder, required_cols) — mirrors INDICATORS' contract.
# The entry shape is the extension point for the SQL engine.
#
# duckdb-only entries (macd, bbands, aroon, cdlengulfing, ht_trendline,
# stoch_k/stoch_d) have no polars builder: validate(engine="duckdb")
# accepts them, the polars engine rejects them. ``adx`` and ``kama`` are
# dual-engine — they ALSO have INDICATORS parity builders (the talib
# extra's map_groups seam, exact TA-Lib values).
# Multi-output t_* functions are narrowed to one series
# at the SQL level: macd -> the MACD line (fast EMA - slow EMA, the
# conventional "MACD" value; signal/hist are derived from it), bbands ->
# upper AND lower as two entries (bands are scanned as thresholds; no single
# "primary" band), aroon -> aroon_up (the trend-strength signal; aroon_down
# is its mirror for short setups, add as its own entry if ever needed),
# stoch -> slowk AND slowd as two entries (stoch_k/stoch_d).
SQL_INDICATORS: dict[str, tuple[tuple[str, ...], Callable, tuple[str, ...]]] = {
    "sma": (("expr", "int"), lambda x, n, p, o, pa: _win(x, n, p, o, pa, "AVG"), ()),
    "rmin": (("expr", "int"), lambda x, n, p, o, pa: _win(x, n, p, o, pa, "MIN"), ()),
    "rmax": (("expr", "int"), lambda x, n, p, o, pa: _win(x, n, p, o, pa, "MAX"), ()),
    "shift": (("expr", "int"), _lag, ()),
    "ema": (("expr", "int"), lambda x, n, p, o, pa: _tcall("t_ema", x, n, o, pa), ()),
    "rsi": (("expr", "int"), lambda x, n, p, o, pa: _tcall("t_rsi", x, n, o, pa), ()),
    "atr": (("int",), _tatr, ("high", "low", "close")),
    "adr": (("expr", "int"), _adr, ("high", "low", "close")),
    "roc": (("expr", "int"), _roc, ()),
    "natr": (("expr", "int"), _natr, ("high", "low", "close")),
    "slope": (("expr", "int"), _slope_sql, ()),
    # --- 0.4.0 single-output TA-Lib parity set (mirrors the INDICATORS entries) ---
    "wma": (("expr", "int"), lambda x, n, p, o, pa: _tcall("t_wma", x, n, o, pa), ()),
    "dema": (("expr", "int"), lambda x, n, p, o, pa: _tcall("t_dema", x, n, o, pa), ()),
    "tema": (("expr", "int"), lambda x, n, p, o, pa: _tcall("t_tema", x, n, o, pa), ()),
    "trima": (("expr", "int"), lambda x, n, p, o, pa: _tcall("t_trima", x, n, o, pa), ()),
    "mom": (("expr", "int"), lambda x, n, p, o, pa: _tcall("t_mom", x, n, o, pa), ()),
    "midprice": (("int",), lambda x, n, p, o, pa: _tcol("t_midprice", n, p, o, pa, ("high", "low")), ("high", "low")),
    "cci": (("int",), lambda x, n, p, o, pa: _tcol("t_cci", n, p, o, pa, ("high", "low", "close")), ("high", "low", "close")),
    "willr": (("int",), lambda x, n, p, o, pa: _tcol("t_willr", n, p, o, pa, ("high", "low", "close")), ("high", "low", "close")),
    # dummy-int precedent (ht_trendline): t_trange takes no period; n is ignored
    "trange": (("int",), lambda x, n, p, o, pa: _tcol("t_trange", None, p, o, pa, ("high", "low", "close")), ("high", "low", "close")),
    "ad": ((), lambda x, n, p, o, pa: _tcol("t_ad", None, p, o, pa, ("high", "low", "close", "volume")), ("high", "low", "close", "volume")),
    "kama": (("int",), _kama, ("close",)),
    # duckdb-only (talib extension): not in INDICATORS
    "macd": (("int",), _macd, ("close",)),
    "bbands_upper": (("int",), _bband("upper"), ("close",)),
    "bbands_lower": (("int",), _bband("lower"), ("close",)),
    "adx": (("int",), _adx, ("high", "low", "close")),
    "aroon": (("int",), _aroon, ("high", "low")),
    "cdlengulfing": (("int",), _cdlengulfing, ("open", "high", "low", "close")),
    "ht_trendline": (("int",), _ht_trendline, ("close",)),
    "stoch_k": (("int", "int", "int"), _stoch("slowk"), ("high", "low", "close")),
    "stoch_d": (("int", "int", "int"), _stoch("slowd"), ("high", "low", "close")),
    # temporal z-score RS normalization (special two-stage lowering in fn())
    "rs_ratio": (("expr", "int"), _rs_smooth_router, ()),
    "rs_momentum": (("expr", "int"), _rs_smooth_router, ()),
}

"""SQL indicator registry: the ``{"fn": name}`` lowering table for this module.

Same entry shape as ``scanlang.indicators.INDICATORS`` (arg_spec, builder,
required_cols), but builders emit SQL fragments instead of ``pl.Expr`` and
take ``(x, n, partition, order_column, params)``. Extend by insertion —
``SQL_INDICATORS["roc"] = (("int",), builder, ())``.

This registry is a superset of ``INDICATORS``: the talib-only names
``macd``, ``bbands_upper``, ``bbands_lower``, ``aroon``,
``cdlengulfing``, ``ht_trendline``, ``stoch_k``, and ``stoch_d`` exist
here and nowhere in ``INDICATORS`` — they run only on the duckdb engine
(the community talib extension provides the ``t_*`` functions).
[``validate(..., engine="duckdb")``](api.md#scanlang.compiler.validate)
accepts them; the polars engine rejects them with
``indicator 'aroon' requires engine='duckdb'``. All other names mirror an
``INDICATORS`` entry 1:1 (same arg_spec, same required_cols) — ``adx`` and
``kama`` included, via their dual-engine INDICATORS parity builders (the
``talib`` extra's group_by/map_groups seam).
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

    def _emit_adr(self, expr: str, alias: str) -> None:
        """adr's two window stages: TR materialization, then the guarded average.

        Stage A projects only (partition, order, _tr) — the lag inside TR needs
        every row of ``prev``, but the count-guard avg must not see lag rows
        shift the window (ROWS framing over the same row set is what aligns it
        with polars rolling_mean). Stage B is a normal window CTE: SELECT *
        carries ``close`` (the avg needs it) and every earlier column/alias
        forward. Params for TR render first (stage A precedes stage B in the
        final WITH), so the builder's params append in text order.
        """

        def emit() -> None:
            # both stages append synchronously: calling _emit_w here would
            # append to self.pending mid-flush, pushing the window CTE behind
            # later t-tier CTEs (which don't carry _tr -> binder error).
            # Stage numbers come from self.stage AT FLUSH TIME (walk time is 0).
            self.stage += 1
            # builder already appended this stage's params (walk time) — the
            # TR text renders through the same sink so ?-counts stay aligned.
            self.ctes.append(
                f"s{self.stage} AS (SELECT *, {_adr_tr(self.p, self.o, self.params)} AS _tr "
                f"FROM {self.prev})"
            )
            self.prev = f"s{self.stage}"
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
        frag: list[str | None] = [list_frag]

        def emit() -> None:
            list_frag = frag[0]
            bases = sorted(self.cols - {self.p, self.o})
            inner = [f"list({_q(c)} ORDER BY {_q(self.o)}) AS _b{i}" for i, c in enumerate(bases)]
            outer = [f"unnest(_b{i}) AS {_q(c)}" for i, c in enumerate(bases)]
            for i, a in enumerate(self.computed):
                inner.append(f"list({_q(a)} ORDER BY {_q(self.o)}) AS _c{i}")
                outer.append(f"unnest(_c{i}) AS {_q(a)}")
            # A struct literal as ``list_frag`` (`{'macd': unnest(...)['macd']}`,
            # multi-output narrowing) makes duckdb drop every sibling SELECT
            # item of the projection it appears in — including other fns'
            # ``_v{n}`` aliases. So the raw t_* LIST is aliased in the inner
            # SELECT under ``_rawN`` and the narrowing moves to the outer
            # SELECT as ``unnest(_rawN)['field']``; the usual
            # ``_v{n}``/``unnest(_v{n})`` pair is skipped entirely.
            if list_frag.lstrip().startswith("{"):
                raw = f"_raw{self.n}"
                inner.append(f"{_raw_tfn(list_frag)} AS {raw}")
                outer.append(f"unnest({raw}){_raw_field(list_frag)} AS {alias}")
                list_frag = None
            self.stage += 1
            name = f"s{self.stage}"
            frags = inner + ([list_frag] if list_frag else [])
            outs = outer + ([unnest_frag] if list_frag else [])
            self.ctes.append(
                f"{name} AS (SELECT {_q(self.p)}, unnest(_o) AS {_q(self.o)}, {', '.join(outs)} "
                f"FROM (SELECT {_q(self.p)}, "
                f"list({_q(self.o)} ORDER BY {_q(self.o)}) AS _o, "
                f"{', '.join(frags)} "
                f"FROM {self.prev} GROUP BY {_q(self.p)}))"
            )
            self.prev = name
            self.computed.append(alias)

        self.pending.append(emit)

    def _emit_rs(self, x: str, name: str, n: int, alias: str) -> None:
        """rs_ratio/rs_momentum: list-tier smoothing CTE + window-tier z CTE.

        ``x`` (the operand fragment) is never None here — arg_spec is
        ``("expr", "int")`` and validate() guarantees both args. It renders
        ONCE, inner (its base/alias columns are only visible there), as
        ``list(x ORDER BY o) AS _rs_r``. Stage A mirrors ``_emit_t``
        (deferred; reads ``self.cols``/``self.computed`` at flush time) but
        nests the projection one SELECT deeper: the smoothing chain (_rs_nn
        strip -> t_ema -> null re-pad) consumes the aliases via duckdb's
        lateral same-SELECT-list resolution, and ``_rs_r`` itself rides
        outward through an extra unnest when ``x`` references an alias
        (rs_momentum over rs_ratio) so later CTEs keep seeing it. Stage B is
        the plain window CTE (count-guard warm-up NULLs, population std, eps
        pin, inside-the-CASE clamp).
        """
        smooth = _rs_smooth(_RS_SPAN)
        strip = "list_filter(_rs_r, y -> y IS NOT NULL) AS _rs_nn"
        if name == "rs_momentum":
            # t_mom zero-seeds its warm-up AND reads NULL inputs as zero, so
            # every slot gates on the source value n bars back existing.
            smooth = (
                f"list_transform(t_mom(_rs_r, {_RS_MOM_LOOKBACK}), (m, i) -> "
                f"CASE WHEN list_extract(_rs_r, i + 1 - {_RS_MOM_LOOKBACK}) IS NULL "
                f'THEN NULL ELSE m END) AS _rs_r2, '
                f"list_filter(_rs_r2, y -> y IS NOT NULL) AS _rs_n2, "
                + _rs_smooth(_RS_MOM_SPAN).replace("_rs_nn", "_rs_n2").replace("_rs_r", "_rs_r2")
            )
            strip = ""

        def emit() -> None:
            bases = sorted(self.cols - {self.p, self.o})
            inner0 = [f"list({_q(c)} ORDER BY {_q(self.o)}) AS _b{i}" for i, c in enumerate(bases)]
            outer0 = [f"unnest(_b{i}) AS {_q(c)}" for i, c in enumerate(bases)]
            inner0.append(f"list({_q(self.o)} ORDER BY {_q(self.o)}) AS _o")
            # operand fragment renders ONCE, inner (base columns live there)
            inner0.append(f"list({x} ORDER BY {_q(self.o)}) AS _rs_r")
            for i, a in enumerate(self.computed):
                inner0.append(f"list({_q(a)} ORDER BY {_q(self.o)}) AS _c{i}")
                outer0.append(f"unnest(_c{i}) AS {_q(a)}")
            # _rs_r rides outward too: harmless for base-column operands, and
            # alias operands (rs_momentum over rs_ratio) stay visible later
            outer0.append("unnest(_rs_r) AS _rs_r")
            self.stage += 1
            name_a = f"s{self.stage}"
            # the middle carries, as PLAIN refs, everything the outer unnests
            # (lateral refs don't span nesting levels): _o, _b{i}, _rs_r —
            # plus strip's _rs_nn, consumed by smooth at this level
            mid = ", ".join(["_o"] + [f"_b{i}" for i in range(len(bases))] + ["_rs_r"])
            carry = ", ".join(f"_c{i}" for i in range(len(self.computed)))
            if carry:
                mid += f", {carry}"
            if strip:
                mid += f", {strip}"
            self.ctes.append(
                f"{name_a} AS (SELECT {_q(self.p)}, unnest(_o) AS {_q(self.o)}, {', '.join(outer0)}, "
                f"unnest(_v0) AS {alias} "
                f"FROM (SELECT {_q(self.p)}, {mid}, {smooth} AS _v0 "
                f"FROM (SELECT {_q(self.p)}, "
                f"{', '.join(inner0)} "
                f"FROM {self.prev} GROUP BY {_q(self.p)})))"
            )
            self.prev = name_a
            self.computed.append(alias)
            self.stage += 1
            name_b = f"s{self.stage}"
            # stage B: plain window CTE. Replaces the pre-z column outright
            # (a second ``AS alias`` would produce a c0_1 duplicate and the
            # WHERE would bind the pre-z column) and CARRIES every other base
            # column/alias forward — later CTEs and the WHERE bind against the
            # last CTE, so a projecting stage B breaks rs_momentum over
            # rs_ratio (its stage A re-lists ``rs``/``c0``).
            keep = sorted(self.cols - {self.p, self.o}) + [a for a in self.computed if a != alias]
            sel = [_q(self.p), _q(self.o)] + [_q(c) for c in keep] + [
                f"{_rs_z(alias, n, self.p, self.o)} AS {alias}"
            ]
            self.ctes.append(f"{name_b} AS (SELECT {', '.join(sel)} FROM {self.prev})")
            self.prev = name_b

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
        pos = []
        ints: list[int] = []
        outer_sink, self.sink = self.sink, self.params  # fn args render in CTE text
        try:
            for tag, a in zip(arg_spec, spec["args"]):
                if tag == "int":
                    ints.append(a)
                else:
                    pos.append(self.operand(a))
            n = ints[0] if len(ints) == 1 else tuple(ints)
            alias = f"c{self.n}"
            if name in ("rs_ratio", "rs_momentum"):
                # two-stage lowering: list-tier smoothing (nested projection —
                # the operand fragment must render once, under its own alias)
                # + window-tier z. Consumes pos[0] directly; the builder slot
                # in the registry is a placeholder that is never called.
                self._emit_rs(pos[0] if pos else None, name, ints[0], alias)
                self.n += 1
                return alias
            expr = builder(pos[0] if pos else None, n, self.p, self.o, self.params)
        finally:
            self.sink = outer_sink
        if name in _WINDOW:
            if name == "adr":
                # adr's TR needs lag(close) — window functions cannot nest, so
                # it materializes as its own CTE before the guarded average.
                self._emit_adr(f"{expr} AS {alias}", alias)
            else:
                self._emit_w(f"{expr} AS {alias}", alias)
        else:
            self._emit_t(f"{expr} AS _v{self.n}", f"unnest(_v{self.n}) AS {alias}", alias)
            # duckdb quirk (probed): a struct literal in the inner projection
            # (`{'macd': ...}` multi-output narrowing) makes it drop every
            # sibling item, including other fns' `_v{n}` aliases. The emit
            # above splits the struct case out (raw alias inner, unnest+narrow
            # outer) so `_v{n}` is never referenced in the outer SELECT.
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
    engine: str = "duckdb",
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
        engine: Which indicator registry validates ``{"fn": ...}`` names;
            ``"duckdb"`` (default) is the only engine that can execute the
            generated SQL — the kwarg exists for API consistency with
            ``validate`` and ``compile``.

    Returns:
        ``(sql, params)`` — run with ``con.execute(sql, params)``.

    Raises:
        ValueError: on validation failure (same first-error message as
            ``compile()``) or a non-identifier ``relation``.
    """
    errors = _collect(scan_def, catalog=catalog, engine=engine)
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

    Validates with ``engine="duckdb"`` (so talib-only indicator names are
    accepted), ensures the community talib extension on the connection
    (``INSTALL talib FROM community; LOAD talib`` — idempotent, cached after
    the first call), executes the compiled SQL, and collects eagerly (duckdb
    has no polars-lazy plan). ``duckdb`` itself is never imported here — pass
    a connection, so scanlang without the ``duckdb`` extra still imports.

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
