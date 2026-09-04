"""Golden cross-engine suite: same scan_defs through apply() (polars) and apply_sql().

sma-family columns identical, ema/rsi/atr abs diff < 0.01 at mature bars
(after 4*n, per the 2026-09-02 duckdb-backend plan Q1), hit sets equal for
sma-only scans, golden-cross hits equal in the mature window. Whole module
skips when duckdb is not importable (the talib community extension is ensured
by apply_sql itself).
"""

import copy
import datetime as dt
import math

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
    cat = kw.pop("catalog", None)
    return [
        tuple(r)
        for r in apply(df, d, catalog=cat or catalog_from_schema(df), **kw)
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

    bad_fn = {"filters": [{"property": {"fn": "nosuchfn", "args": [{"col": "close"}, 14]}, "op": ">", "value": 0}]}
    assert validate(bad_fn) == ["filters[0].property.fn: unknown indicator: 'nosuchfn'"]
    with pytest.raises(ValueError, match="unknown indicator"):
        compile_sql(bad_fn, relation="bars")

    for rel in ("'bars.parquet'", "bars; DROP TABLE x", "a-b", ""):
        with pytest.raises(ValueError, match="plain identifier"):
            compile_sql({"filters": []}, relation=rel)


def test_sql_registry_superset_of_indicators():
    """Every INDICATORS name mirrors 1:1; talib-only names live SQL-side only."""
    from scanlang.indicators import INDICATORS

    for name, (arg_spec, _b, req) in INDICATORS.items():
        assert name in SQL_INDICATORS
        assert SQL_INDICATORS[name][0] == arg_spec
        assert SQL_INDICATORS[name][2] == req
    assert set(SQL_INDICATORS) > set(INDICATORS)
    assert set(SQL_INDICATORS) - set(INDICATORS) == {
        "macd", "bbands_upper", "bbands_lower", "aroon", "cdlengulfing", "ht_trendline",
    }


# --- review round 1 regressions (card comment repros A-J) -------------------


def test_cross_with_literal_operand(con):
    """Repros A/F: cross against a literal — fragment renders once, params bind once, hits non-empty.

    No session guard on the close shapes: close is exact-tier (no warm-up
    divergence) and AAA's only close×11.0 cross sits at day 21 — the round-1
    version guarded past it and passed as a vacuous 0==0 (review round 2).
    """
    df = _bars()
    d = {"filters": [{"property": "close", "op": "cross_above", "value": 11.0}]}
    golden = [("AAA", T0 + dt.timedelta(days=21))]  # close[20]=11.0 <= 11.0 < close[21]=11.05
    assert _pl_hits(df, d) == golden == _sql_hits(con, d)
    d = {"filters": [{"property": "close", "op": "cross_above", "value": {"+": [5.0, 6.0]}}]}
    assert _pl_hits(df, d) == golden == _sql_hits(con, d)  # folds to the literal 11.0
    d = {"filters": [{"property": "close", "op": "cross_above",
                      "value": {"fn": "sma", "args": [{"col": "close"}, 30]}}]}
    hits = _sql_hits(con, d)
    assert hits and hits == _pl_hits(df, d)  # BBB/CCC sawtooth keeps crossing its sma30
    d = {"filters": [
        {"property": "session", "op": ">=", "value": str(T0 + dt.timedelta(days=MATURE))},
        {"property": {"fn": "rsi", "args": [{"col": "close"}, 14]}, "op": "cross_above", "value": 50},
    ]}
    hits = _sql_hits(con, d)  # BBB/CCC oscillate across RSI 50; 70 never crosses on this frame
    assert hits and hits == _pl_hits(df, d)


def test_cross_literal_inside_groups(con):
    """Repro G: cross-with-literal inside not/any groups — non-vacuous (round 2)."""
    df = _bars()
    d = {"filters": [
        {"not": {"property": "close", "op": "cross_below", "value": 60.0}},
        {"any": [
            {"property": "close", "op": "cross_above", "value": 11.0},
            {"property": "symbol", "op": "==", "value": "CCC"},
        ]},
    ]}
    hits = _pl_hits(df, d)
    assert hits == _sql_hits(con, d)
    # all mature CCC bars ride in on symbol==CCC (row 0 is dropped by the
    # NOT-cross warm-up NULL, identically in both engines) + AAA's day-21 cross
    assert ("AAA", T0 + dt.timedelta(days=21)) in hits and len(hits) == 300


def test_cross_then_tier_sibling(con):
    """Cross alias columns survive a later t-tier CTE's projection restructure.

    RHS is sma (exact-tier) instead of a literal: the original close×11.0
    crossed only at day 21, so the MATURE guard (needed for the rsi sibling)
    scoped past it and the assert could not fail (review round 2).
    """
    df = _bars()
    d = {"filters": [
        {"property": "session", "op": ">=", "value": str(T0 + dt.timedelta(days=MATURE))},
        {"property": "close", "op": "cross_above",
         "value": {"fn": "sma", "args": [{"col": "close"}, 30]}},
        {"property": {"fn": "rsi", "args": [{"col": "close"}, 14]}, "op": "<", "value": 100},
    ]}
    hits = _sql_hits(con, d)
    assert hits and hits == _pl_hits(df, d)


def test_sibling_filters_across_tiers(con):
    """Repros B/D: siblings after a t-tier fn (cols discovered later, w-then-t aliases)."""
    df = _bars()
    cat = catalog_from_schema(df)
    mature = str(T0 + dt.timedelta(days=MATURE))
    # Repro B shape uses ema/rsi whose warm-up rows diverge by contract, so the
    # session guard scopes to the mature window where both engines agree.
    d_b = {"filters": [
        {"property": "session", "op": ">=", "value": mature},
        {"property": {"fn": "ema", "args": [{"col": "close"}, 14]}, "op": ">", "value": 0},
        {"property": {"fn": "rsi", "args": [{"col": "close"}, 14]}, "op": "<", "value": 100},
        {"property": "volume", "op": ">", "value": 0},
    ]}
    assert _pl_hits(df, d_b) == _sql_hits(con, d_b, catalog=cat)
    d_d = {"filters": [
        {"property": "session", "op": ">=", "value": mature},
        {"property": {"fn": "sma", "args": [{"col": "close"}, 20]}, "op": ">", "value": 0},
        {"property": {"fn": "ema", "args": [{"col": "close"}, 14]}, "op": ">", "value": 0},
        {"property": "volume", "op": ">", "value": 0},
    ]}
    assert _pl_hits(df, d_d) == _sql_hits(con, d_d, catalog=cat)


def test_plain_leaf_after_fn_leaf(con):
    """fn leaf then plain-column leaf: the plain column reaches earlier CTEs.

    sma (not ema): rolling warm-up NULLs identically on both engines, so the
    full-frame hit sets must match exactly — ema's pre-lookback rows diverge
    by the documented warm-up contract.
    """
    df = _bars()
    d = {"filters": [
        {"property": {"fn": "sma", "args": [{"col": "close"}, 20]}, "op": ">", "value": 0},
        {"property": "volume", "op": ">", "value": 0},
    ]}
    pol = apply(df, d, catalog=catalog_from_schema(df))
    sql = apply_sql(con, d, relation="bars", catalog=catalog_from_schema(df))
    assert pol.select("symbol", "session").sort("symbol", "session").rows() == (
        sql.select("symbol", "session").sort("symbol", "session").rows()
    )


def test_order_by_unreferenced_column(con):
    """Repro J: order_by on a column no filter references (+ empty filters)."""
    df = _bars()
    d = {"filters": [{"property": "symbol", "op": "==", "value": "AAA"}],
         "order_by": [{"property": "volume", "dir": "desc"}], "limit": 5}
    pol = apply(df, d, catalog=catalog_from_schema(df))
    sql = apply_sql(con, d, relation="bars", catalog=catalog_from_schema(df))
    assert pol.select("symbol", "session").rows() == sql.select("symbol", "session").rows()
    d2 = {"filters": [], "order_by": [{"property": "close", "dir": "desc"}], "limit": 3}
    pol2 = apply(df, d2, catalog=catalog_from_schema(df))
    sql2 = apply_sql(con, d2, relation="bars", catalog=catalog_from_schema(df))
    assert pol2.select("symbol", "session").rows() == sql2.select("symbol", "session").rows()


# --- C3: corpus indicators both engines, talib-only SQL-side -----------------


def test_corpus_indicators_match_benchmark(con):
    """adr/roc/natr/slope SQL output == benchmark talib values at mature bars."""
    df = _bars()
    cat = catalog_from_schema(df)
    d = {"filters": [{"property": {"fn": name, "args": [{"col": "close"}, n]},
                     "op": ">=", "value": -1000}
        for name, n in (("adr", 14), ("roc", 60), ("natr", 14), ("slope", 10))]}
    sql = apply_sql(con, d, relation="bars", catalog=cat)
    # benchmark: polars builders (talib-seeded, C1-verified) on the same frame
    ref = df.with_columns(
        INDICATORS["adr"][1](pl.col("close"), 14, "symbol").alias("adr"),
        INDICATORS["roc"][1](pl.col("close"), 60, "symbol").alias("roc"),
        INDICATORS["natr"][1](pl.col("close"), 14, "symbol").alias("natr"),
        INDICATORS["slope"][1](pl.col("close"), 10, "symbol").alias("slope"),
    )
    idx = {(s, sess): i for i, (s, sess) in enumerate(zip(ref["symbol"], ref["session"], strict=True))}
    for j, name in enumerate(("adr", "roc", "natr", "slope")):
        got = dict(zip(zip(sql["symbol"], sql["session"], strict=True), sql[f"c{j}"], strict=True))
        for (sym, sess), v in got.items():
            if (sess - T0).days < 150:  # well past every lookback incl. adr(14) warm-up
                continue
            w = ref[name][idx[(sym, sess)]]
            assert v is not None and w is not None, (name, sym, sess)
            assert abs(v - w) < 0.01, (name, sym, sess, v, w)


def test_adr_roc_hits_equal_both_engines(con):
    """sma-family adr is exact-tier: full-frame hit sets identical (corpus scans)."""
    df = _bars()
    cat = catalog_from_schema(df)
    d = {"filters": [
        {"property": {"fn": "adr", "args": [{"col": "close"}, 20]}, "op": ">", "value": 3},
        {"property": {"fn": "roc", "args": [{"col": "close"}, 60]}, "op": ">", "value": 5},
    ]}
    assert _sql_hits(con, d, catalog=cat) == _pl_hits(df, d, catalog=cat)


def test_sql_only_names_run_on_duckdb(con):
    """macd executes; polars engine refuses the same def."""
    df = _bars()
    cat = catalog_from_schema(df)
    d = {"filters": [{"property": {"fn": "macd", "args": [12]}, "op": ">=", "value": -1000}]}
    hits = _sql_hits(con, d, catalog=cat)
    assert len(hits) == 3 * (N - 33)  # macd(12,26,9): first 33 bars NULL per symbol
    assert "requires engine='duckdb'" in validate(d, catalog=cat)[0]
    assert validate(d, catalog=cat, engine="duckdb") == []


def test_bbands_brackets_close_sql(con):
    """bbands_lower < close < bbands_upper holds somewhere mature (duckdb only)."""
    df = _bars()
    cat = catalog_from_schema(df)
    d = {"filters": [{"property": "session", "op": ">=", "value": str(T0 + dt.timedelta(days=60))},
                     {"property": {"fn": "bbands_lower", "args": [20]}, "op": "<", "value": {"col": "close"}},
                     {"property": {"fn": "bbands_upper", "args": [20]}, "op": ">", "value": {"col": "close"}}]}
    hits = _sql_hits(con, d, catalog=cat)
    assert hits  # BBB's +/-8 oscillation crosses both bands
    assert "requires engine='duckdb'" in validate(d, catalog=cat)[0]
    # upper > lower everywhere mature (bands never invert on this frame)
    vals = apply_sql(con, {"filters": [{"property": {"fn": "bbands_upper", "args": [20]}, "op": ">=", "value": -1e9},
                                       {"property": {"fn": "bbands_lower", "args": [20]}, "op": ">=", "value": -1e9}]},
                     relation="bars", catalog=cat)
    assert all(u > l for u, l in zip(vals["c0"], vals["c1"], strict=True) if u is not None)


def test_talib_only_builders_execute(con):
    """Execution smoke for adx/aroon/cdlengulfing/ht_trendline: exact talib warm-up counts + value domains.

    Warm-ups (null bars per symbol) are the talib semantics: adx(14)=27 (2*14-1),
    aroon(14)=14, cdlengulfing=2 (2-bar pattern), ht_trendline=63 (dominant cycle);
    verified against live duckdb+talib.
    """
    df = _bars()
    cat = catalog_from_schema(df)
    cases = {
        "adx": (14, 27, lambda v: v > 0),
        "aroon": (14, 14, lambda v: 0 <= v <= 100),
        "cdlengulfing": (2, 2, lambda v: v in (0, 1)),
        "ht_trendline": (2, 63, lambda v: v is not None),
    }
    for fn, (n, warmup, domain) in cases.items():
        d = {"filters": [{"property": {"fn": fn, "args": [n]}, "op": ">=", "value": -1e9}]}
        assert len(_sql_hits(con, d, catalog=cat)) == 3 * (N - warmup), fn
        for (v,) in _sql_hits(con, d, cols=("c0",), catalog=cat):
            assert domain(v), (fn, v)

    # Pin aroon's struct narrowing: scanlang "aroon" is the UP line (down differs per-row).
    # CCC (sawtooth) first non-null values descend 13/14, 12/14, 11/14 * 100; aroon_down
    # would start 57.14/50/42.86 there instead, and AAA (uptrend) is constant 100 either way.
    d = {"filters": [{"property": {"fn": "aroon", "args": [14]}, "op": ">=", "value": -1e9}]}
    ccc = [v for s, _, v in _sql_hits(con, d, cols=("symbol", "session", "c0"), catalog=cat) if s == "CCC"]
    assert ccc[:3] == pytest.approx([1300 / 14, 1200 / 14, 1100 / 14])


def test_adx_parity_two_partitions_warmup_and_cross_engine(con):
    """The 0.4.0 parity slice: adx(14) validate/compile/executes on BOTH engines.

    polars = the INDICATORS talib builder via group_by(partition,
    maintain_order=True).map_groups (NaN warm-up -> null); duckdb = the
    existing SQL_INDICATORS['adx'] t_adx lowering. Covers: two partitions,
    the 2n-1 warm-up contract, no NaN leaking into filters (null-filtered
    identically), and exact mature-value equality cross-engine.
    """
    talib = pytest.importorskip("talib")
    df = _bars()
    cat = catalog_from_schema(df)
    n, warmup = 14, 2 * 14 - 1
    d = {"filters": [{"property": {"fn": "adx", "args": [n]}, "op": ">=", "value": 0}]}
    assert validate(d, catalog=cat, engine="polars") == []
    assert validate(d, catalog=cat, engine="duckdb") == []
    # bare compile() targets the reserved staging column (apply() pre-stages __adx)
    assert compile(dict(d), catalog=cat).meta.root_names() == ["__adx"]

    # warm-up pinned directly on the unstaged builder seam: exactly 2n-1 nulls
    # per partition (NaN normalized to null) on the fixture
    _, builder, _ = INDICATORS["adx"]
    unstaged = (
        df.group_by("symbol", maintain_order=True)
        .map_groups(lambda g: builder(n, "symbol")(g))
    )
    assert unstaged["__adx"].null_count() == 3 * warmup
    for sym in ("AAA", "BBB", "CCC"):
        assert unstaged.filter(pl.col("symbol") == sym)["__adx"].null_count() == warmup
    # and the mature region is provably positive here, so the >= 0 filter below
    # keeps every mature bar (the count assertions are not vacuous)
    assert unstaged.filter(pl.col("__adx").is_not_null())["__adx"].min() > 0

    # polars engine: apply() drives the map_groups builder over both partitions
    d_reuse = {"filters": [{"property": {"fn": "adx", "args": [n]}, "op": ">=", "value": 0}]}
    snapshot = copy.deepcopy(d_reuse)
    pol = apply(df, d_reuse, catalog=cat)
    assert d_reuse == snapshot  # apply() leaves the caller's scan_def untouched
    assert set(pol["symbol"]) == {"AAA", "BBB", "CCC"} and pol.height == 3 * (N - warmup)

    # warm-up: exactly 2n-1 hits per partition (null warm-up rows drop out of
    # the filter — nulls and NaNs both fail the predicate, so nothing leaks)
    per_sym = pol.group_by("symbol", maintain_order=True).len()
    assert per_sym["len"].to_list() == [N - warmup] * 3
    got = {(s, sess): v for s, sess, v in
           pol.select("symbol", "session", next(c for c in pol.columns if c.startswith("__adx"))).rows()
           if v is not None}
    assert len(got) == 3 * (N - warmup)

    # duckdb engine: same scan dict through t_adx (apply() no longer rewrites it)
    sql = apply_sql(con, d_reuse, relation="bars", catalog=cat)
    assert sql.height == 3 * (N - warmup)
    sql_vals = {(s, sess): v for s, sess, v in sql.select("symbol", "session", "c0").rows()}
    assert set(sql_vals) == set(got)  # identical mature hit sets (warm-up filtered identically)

    # cross-engine + official-talib equality on every mature bar (exact tier)
    ref = {}
    for sym in ("AAA", "BBB", "CCC"):
        sub = df.filter(pl.col("symbol") == sym).sort("session")
        ref[sym] = talib.ADX(sub["high"].to_numpy(), sub["low"].to_numpy(), sub["close"].to_numpy(), timeperiod=n)
    for (sym, sess), v in got.items():
        bar = (sess - T0).days
        r = ref[sym][bar]
        assert not math.isnan(r), (sym, sess)
        assert v == pytest.approx(float(r), abs=1e-9), (sym, sess, v, r)
        assert sql_vals[(sym, sess)] == pytest.approx(float(r), abs=1e-9), (sym, sess)


def test_adx_staging_covers_full_operand_grammar():
    """Staging regressions: value-position + arith-tree adx, scan_def reuse.

    validate() accepts fn operands in value position and inside arithmetic
    property trees (so does polars-native sma); apply()'s eager staging must
    pre-stage every one of them — previously only top-level property fns were
    staged, so the predicate referenced a never-materialized ``__adx`` — and
    must not rewrite the caller's scan_def (a reused dict then fails with
    ``unknown column: '__adx_0'``).
    """
    pytest.importorskip("talib")
    df = _bars()
    cat = catalog_from_schema(df)
    fn = {"fn": "adx", "args": [14]}
    mature = 3 * (N - 27)

    # control: polars-native fn in value position already ran
    ctrl = apply(df, {"filters": [{"property": "close", "op": ">",
                                   "value": {"fn": "sma", "args": [{"col": "close"}, 20]}}]}, catalog=cat)
    assert ctrl.height < 3 * N  # warm-up rows drop; the position itself works

    # value position: adx > 0 side of close > adx (was: ColumnNotFoundError __adx)
    d_val = {"filters": [{"property": "close", "op": ">", "value": dict(fn)}]}
    assert validate(d_val, catalog=cat, engine="polars") == []
    assert "__adx" in compile(dict(d_val), catalog=cat).meta.root_names()
    hits = apply(df, d_val, catalog=cat)
    assert hits.height <= mature  # warm-up nulls fail the predicate — no leakage
    alias = next(c for c in hits.columns if c.startswith("__adx"))
    assert hits[alias].null_count() == 0

    # arithmetic property tree: adx - close > 0 (same staging gap)
    d_arith = {"filters": [{"property": {"-": [dict(fn), {"col": "close"}]}, "op": ">", "value": 0}]}
    assert validate(d_arith, catalog=cat) == []
    hits2 = apply(df, d_arith, catalog=cat)
    alias2 = next(c for c in hits2.columns if c.startswith("__adx"))
    assert hits2[alias2].null_count() == 0 and hits2.height <= mature

    # dict reuse: the same scan_def applies twice, byte-identical afterwards
    d_twice = {"filters": [{"property": dict(fn), "op": ">=", "value": 0}]}
    snapshot = copy.deepcopy(d_twice)
    r1 = apply(df, d_twice, catalog=cat)
    r2 = apply(df, d_twice, catalog=cat)  # was: ValueError unknown column '__adx_0'
    assert d_twice == snapshot
    assert r1.equals(r2)
