"""Golden cross-engine suite: same scan_defs through apply() (polars) and apply_sql().

sma-family columns identical, ema/rsi/atr abs diff < 0.01 at mature bars
(after 4*n, per the 2026-09-02 duckdb-backend plan Q1), hit sets equal for
sma-only scans, golden-cross hits equal in the mature window. Whole module
skips when duckdb is not importable (the talib community extension is ensured
by apply_sql itself).
"""

import datetime as dt

import polars as pl
import pytest

from scanlang import apply, catalog_from_schema, validate
from scanlang.compiler import compile
from scanlang.indicators import INDICATORS

duckdb = pytest.importorskip("duckdb")

from scanlang.duckdb_sql import SQL_INDICATORS, apply_sql, compile_sql

T0 = dt.date(2026, 1, 1)
N = 300
# rsi/atr convergence to <0.01 is measured at ~7.6n bars (see scanlang.indicators
# docstring; the plan's 4n is the optimistic bound). 7.6 * 14 ~= 106, +margin.
MATURE = 112


def _bars() -> pl.DataFrame:
    """Deterministic 3-symbol OHLCV frame: uptrend, oscillator, sawtooth (sorted symbol, session)."""
    a = [10.0 + 0.05 * i for i in range(N)]
    b = [50.0 + 8.0 * (i % 9 - 4) + 0.1 * i for i in range(N)]
    c = [30.0 + 0.25 * (i % 7) for i in range(N)]  # slow sawtooth (not flat: 0/0 rsi corner)
    sessions = [T0 + dt.timedelta(days=i) for i in range(N)]
    return pl.concat(
        [
            pl.DataFrame(
                {
                    "symbol": [sym] * N,
                    "session": sessions,
                    "open": [x - 0.2 for x in closes],
                    "high": [x + 1.0 for x in closes],
                    "low": [x - 1.0 for x in closes],
                    "close": closes,
                    "volume": [1000.0] * N,
                }
            )
            for sym, closes in (("AAA", a), ("BBB", b), ("CCC", c))
        ]
    )


@pytest.fixture(scope="module")
def con():
    path = "/tmp/scanlang_golden_bars.parquet"
    _bars().write_parquet(path)
    con = duckdb.connect()
    con.execute(f"CREATE VIEW bars AS SELECT * FROM '{path}'")
    yield con
    con.close()


def _sql_hits(con, d: dict, cols: tuple[str, ...] = ("symbol", "session"), **kw) -> list[tuple]:
    out = apply_sql(con, d, relation="bars", **kw)
    return out.select(cols).sort(cols).rows()


def _pl_hits(df: pl.DataFrame, d: dict, cols: tuple[str, ...] = ("symbol", "session"), **kw) -> list[tuple]:
    return [
        tuple(r)
        for r in apply(df, d, catalog=catalog_from_schema(df), **kw)
        .select(cols)
        .sort(cols)
        .rows()
    ]


def test_sma_family_identical(con):
    """sma/rmin/rmax/shift are exact on both engines (native window lowering)."""
    df = _bars()
    builders = {
        "sma": lambda n: INDICATORS["sma"][1](pl.col("close"), n, "symbol"),
        "rmin": lambda n: INDICATORS["rmin"][1](pl.col("close"), n, "symbol"),
        "rmax": lambda n: INDICATORS["rmax"][1](pl.col("close"), n, "symbol"),
        "shift": lambda n: INDICATORS["shift"][1](pl.col("close"), n, "symbol"),
    }
    for name, build in builders.items():
        n = 20
        d = {"filters": [{"property": {"fn": name, "args": [{"col": "close"}, n]}, "op": ">=", "value": 0}]}
        sql = apply_sql(con, d, relation="bars", catalog=catalog_from_schema(df))
        ref = df.with_columns(build(n).alias("v")).filter(pl.col("v") >= 0)
        got = dict(zip(zip(sql["symbol"], sql["session"], strict=True), sql["c0"], strict=True))
        want = dict(zip(zip(ref["symbol"], ref["session"], strict=True), ref["v"], strict=True))
        assert set(got) == set(want), name
        for k, value in got.items():
            assert value == pytest.approx(want[k]), (name, k)


def test_ema_rsi_atr_converge(con):
    """TA-Lib-seeded t_* values converge to the polars Wilder recursion after 4*n bars."""
    df = _bars()
    ind = {
        "ema": {"fn": "ema", "args": [{"col": "close"}, 14]},
        "rsi": {"fn": "rsi", "args": [{"col": "close"}, 14]},
        "atr": {"fn": "atr", "args": [14]},
    }
    d = {"filters": [{"property": spec, "op": ">=", "value": 0} for spec in ind.values()]}
    sql = apply_sql(con, d, relation="bars", catalog=catalog_from_schema(df))
    ref = df.with_columns(
        INDICATORS["ema"][1](pl.col("close"), 14, "symbol").alias("ema"),
        INDICATORS["rsi"][1](pl.col("close"), 14, "symbol").alias("rsi"),
        INDICATORS["atr"][1](14, "symbol").alias("atr"),
    )
    idx = {(s, sess): i for i, (s, sess) in enumerate(zip(ref["symbol"], ref["session"], strict=True))}
    alias = {"ema": "c0", "rsi": "c1", "atr": "c2"}
    for col in ind:
        got = dict(zip(zip(sql["symbol"], sql["session"], strict=True), sql[alias[col]], strict=True))
        for (sym, sess), v in got.items():
            bar = (sess - T0).days
            if bar < MATURE:
                continue
            w = ref["atr"][idx[(sym, sess)]] if col == "atr" else ref[col][idx[(sym, sess)]]
            assert v is not None and w is not None, (col, sym, sess)
            assert abs(v - w) < 0.01, (col, sym, sess, v, w)


def test_hits_equal_sma_only_scan(con):
    df = _bars()
    d = {"filters": [
        {"property": {"fn": "sma", "args": [{"col": "close"}, 50]}, "op": ">", "value": {"col": "close"}},
    ]}
    assert _pl_hits(df, d) == _sql_hits(con, d)


def test_cross_above_golden_cross_same_symbols(con):
    """ema5 x ema20 cross hits match in the mature window (warm-up seeds differ by design)."""
    df = _bars()
    d = {"filters": [
        {"property": "session", "op": ">=", "value": str(T0 + dt.timedelta(days=MATURE))},
        {"property": {"fn": "ema", "args": [{"col": "close"}, 5]},
         "op": "cross_above",
         "value": {"fn": "ema", "args": [{"col": "close"}, 20]}},
    ]}
    assert _pl_hits(df, d) == _sql_hits(con, d)


def test_cross_below_matches(con):
    df = _bars()
    d = {"filters": [
        {"property": "session", "op": ">=", "value": str(T0 + dt.timedelta(days=MATURE))},
        {"property": {"fn": "ema", "args": [{"col": "close"}, 5]},
         "op": "cross_below",
         "value": {"fn": "ema", "args": [{"col": "close"}, 20]}},
    ]}
    assert _pl_hits(df, d) == _sql_hits(con, d)


def test_groups_arith_between_in_limit(con):
    df = _bars()
    d = {"filters": [
        {"any": [
            {"property": "symbol", "op": "in", "value": ["AAA", "BBB"]},
            {"property": "close", "op": "between", "value": [10.0, 12.0]},
        ]},
        {"property": {"*": [{"col": "close"}, 2]}, "op": ">", "value": {"+": [{"col": "close"}, 5]}},
        {"not": {"property": "symbol", "op": "==", "value": "CCC"}},
    ], "order_by": [{"property": "session", "dir": "desc"}, {"property": "symbol"}],
       "limit": 7}
    pol = apply(df, d, catalog=catalog_from_schema(df))
    sql = apply_sql(con, d, relation="bars", catalog=catalog_from_schema(df))
    # same rows in the same order (column order differs: apply() is shape-preserving)
    assert pol.select("symbol", "session").rows() == sql.select("symbol", "session").rows()


def test_contains_and_dates(con):
    df = _bars()
    d = {"filters": [
        {"property": "symbol", "op": "contains", "value": "A"},
        {"property": "session", "op": ">=", "value": "2026-03-01"},
    ]}
    assert _pl_hits(df, d) == _sql_hits(con, d)
    d2 = {"filters": [{"property": "session", "op": "between",
                       "value": ["2026-01-02", "2026-01-04"]}]}
    assert _pl_hits(df, d2) == _sql_hits(con, d2)


def test_nested_indicator_operand_mature(con):
    """sma(rsi(close,14),5) hits and values match in the mature window."""
    df = _bars()
    ind_spec = {"fn": "sma", "args": [{"fn": "rsi", "args": [{"col": "close"}, 14]}, 5]}
    d = {"filters": [
        {"property": "session", "op": ">=", "value": str(T0 + dt.timedelta(days=MATURE + 5))},
        {"property": ind_spec, "op": ">", "value": 60},
    ]}
    pol_hits = _pl_hits(df, d)
    assert pol_hits == _sql_hits(con, d)
    # and the staged SQL column equals the polars composition on those hits
    sql_vals = apply_sql(
        con,
        {"filters": [d["filters"][0], {"property": ind_spec, "op": ">=", "value": 0}]},
        relation="bars",
        catalog=catalog_from_schema(df),
    )
    got = dict(zip(zip(sql_vals["symbol"], sql_vals["session"], strict=True), sql_vals["c0"], strict=True))
    ref = df.with_columns(
        INDICATORS["sma"][1](INDICATORS["rsi"][1](pl.col("close"), 14, "symbol"), 5, "symbol").alias("v")
    )
    ref_idx = dict(zip(zip(ref["symbol"], ref["session"], strict=True), ref["v"], strict=True))
    for sym, sess in pol_hits:
        v, w = got[(sym, sess)], ref_idx[(sym, sess)]
        assert v is not None and w is not None, (sym, sess)
        assert abs(v - w) < 0.01, (sym, sess, v, w)


def test_error_cases_identical(con):
    bad_prop = {"filters": [{"property": "nope", "op": ">=", "value": 1}]}
    assert validate(bad_prop) == ["filters[0]: unknown property: 'nope'"]
    with pytest.raises(ValueError, match="unknown property"):
        compile(bad_prop)
    with pytest.raises(ValueError, match="unknown property"):
        compile_sql(bad_prop, relation="bars")

    bad_fn = {"filters": [{"property": {"fn": "adx", "args": [{"col": "close"}, 14]}, "op": ">", "value": 0}]}
    assert validate(bad_fn) == ["filters[0].property.fn: unknown indicator: 'adx'"]
    with pytest.raises(ValueError, match="unknown indicator"):
        compile_sql(bad_fn, relation="bars")

    for rel in ("'bars.parquet'", "bars; DROP TABLE x", "a-b", ""):
        with pytest.raises(ValueError, match="plain identifier"):
            compile_sql({"filters": []}, relation=rel)


def test_sql_registry_mirrors_indicators():
    from scanlang.indicators import INDICATORS

    for name, (arg_spec, _b, req) in SQL_INDICATORS.items():
        assert name in INDICATORS
        assert INDICATORS[name][0] == arg_spec
        assert INDICATORS[name][2] == req
    assert set(SQL_INDICATORS) == set(INDICATORS)
