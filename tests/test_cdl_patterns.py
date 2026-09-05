"""Candlestick-pattern parity: registry-wide execution smoke + deterministic fixtures.

The curated CDL set (_CDL_PARITY: 25 patterns + cdlengulfing = 26 names)
shares one signature on both engines: (open, high, low, close) -> 0/±100
int, no period (the dummy-int precedent). The polars engine runs the
group_by/map_groups seam (bar-0 NaN normalized to null), the duckdb engine
the t_cdl* list lowering (lookback-1 leading nulls). The set is the
value-parity intersection: every registered pattern is asserted identical,
bar-for-bar, between live talib and the duckdb extension on the fixture —
28 further talib patterns (threshold-relative: hammer, spinningtop, ...)
are excluded because the extension diverges on them; see _CDL_PARITY.

Determinism: the OHLC frame is embedded (tests/cdl_fixture_frame.json,
regenerable via scripts/gen_cdl_fixtures.py) and the per-pattern hit pins
are lifted verbatim from live talib (tests/cdl_fixture_pins.json), so a
column-order swap (o/h/l/c transposed) or a sign error flips the pins
loudly. Also pins the 7 penetration-parameter patterns missing from the
duckdb extension — and the 28 divergence exclusions — as unregistered.
"""

import datetime as dt
import json
from pathlib import Path

import polars as pl
import pytest

from scanlang import apply, catalog_from_schema, validate
from scanlang.compiler import compile
from scanlang.duckdb_sql import SQL_INDICATORS, apply_sql
from scanlang.indicators import INDICATORS

duckdb = pytest.importorskip("duckdb")

T0 = dt.date(2026, 1, 1)
_HERE = Path(__file__).parent
_FRAME = json.loads((_HERE / "cdl_fixture_frame.json").read_text())
_PINS = json.loads((_HERE / "cdl_fixture_pins.json").read_text())

PENETRATION_MISSING = [
    "abandonedbaby", "darkcloudcover", "eveningdojistar", "eveningstar",
    "mathold", "morningdojistar", "morningstar",
]

# threshold-relative patterns where the duckdb community talib extension
# diverges in value from live talib (0 vs ±100, or a fire where talib says
# 0) — excluded from both registries rather than emulated.
DIVERGENCE_EXCLUDED = [
    "advanceblock", "belthold", "closingmarubozu", "counterattack", "doji",
    "dojistar", "dragonflydoji", "gapsidesidewhite", "hammer", "hangingman",
    "harami", "haramicross", "highwave", "hikkakemod", "invertedhammer",
    "longleggeddoji", "longline", "marubozu", "matchinglow", "piercing",
    "rickshawman", "shootingstar", "shortline", "spinningtop", "sticksandwich",
    "takuri", "tristar", "3inside",
]


def _bars() -> pl.DataFrame:
    nbars = max(r[1] for r in _FRAME) + 1
    return pl.DataFrame(
        _FRAME,
        schema={"symbol": pl.String, "bar": pl.Int64, "open": pl.Float64,
                "high": pl.Float64, "low": pl.Float64, "close": pl.Float64},
        orient="row",
    ).with_columns(
        session=pl.col("bar").replace_strict(
            list(range(nbars)), [T0 + dt.timedelta(days=i) for i in range(nbars)]
        ),
        volume=pl.lit(1000.0),
    )


def _catalog(frame: pl.DataFrame) -> dict:
    return {**catalog_from_schema(frame), "open": {"label": "Open", "dtype": "float"}}


def _scan(name: str, op: str = ">=", value: int = -200) -> dict:
    # -200 keeps every graded signal (cdlengulfing 80, cdlhikkake ±200) while
    # the predicate still drops the SQL engine's warm-up nulls
    return {"filters": [{"property": {"fn": name, "args": [2]}, "op": op, "value": value}]}


@pytest.fixture(scope="module")
def con():
    path = "/tmp/scanlang_cdl_bars.parquet"
    df = _bars()
    df.write_parquet(path)
    con = duckdb.connect()
    con.execute(f"CREATE VIEW bars AS SELECT * FROM '{path}'")
    yield con
    con.close()


def test_registry_wide_cdl_execution_smoke(con):
    """Every registered CDL executes on both engines with identical semantics.

    - identical name sets (_CDL_PARITY = 25 patterns + cdlengulfing = 26)
    - ("int",) arg_spec and (open, high, low, close) required_cols on every
      entry, both registries — cdlengulfing's 0.3.0 contract untouched
    - scan-level (symbol, session) hit sets are identical across engines
      (the >= -200 predicate drops warm-up nulls and 0-rows identically)
    - polars apply() executes through the eager seam: no NaN leak, no row
      dropped by the >= -200 predicate (talib emits ints from bar 0)
    """
    from scanlang.indicators import _CDL_PARITY

    names = sorted(n for n in INDICATORS if n.startswith("cdl"))
    assert len(names) == len(_CDL_PARITY) == 26, len(names)
    assert "cdlengulfing" in names
    assert names == sorted(n for n in SQL_INDICATORS if n.startswith("cdl"))
    assert names == sorted(_CDL_PARITY)
    for name in names:
        for registry in (INDICATORS, SQL_INDICATORS):
            arg_spec, _b, req = registry[name]
            assert arg_spec == ("int",), name
            assert req == ("open", "high", "low", "close"), name

    df = _bars()
    cat = _catalog(df)
    # talib's CDL domain: ±100, graded ±200 (kicking/hikkake families) and 80
    # (engulfing retracement) — 0 means "no pattern"
    legal = {-100, 0, 80, 100, -200, 200}
    for name in names:
        d = _scan(name)
        assert validate(d, catalog=cat) == [], name
        assert validate(d, catalog=cat, engine="duckdb") == [], name
        sql = apply_sql(con, d, relation="bars", catalog=cat)
        # the >= -200 predicate drops the warm-up nulls; every surviving
        # value is a legal pattern signal
        assert set(sql["c0"].to_list()) <= legal, name
        pol = apply(df, dict(d), catalog=cat)
        assert pol.height == df.height, name
        pcol = next(c for c in pol.columns if c.startswith("__"))
        assert set(pol[pcol].to_list()) <= legal, name


def test_penetration_patterns_missing_from_duckdb_stay_unregistered():
    """The 7 penetration-parameter patterns are excluded, not emulated."""
    for name in PENETRATION_MISSING:
        assert f"cdl{name}" not in INDICATORS, name
        assert f"cdl{name}" not in SQL_INDICATORS, name
        d = {"filters": [{"property": {"fn": f"cdl{name}", "args": [2]},
                          "op": "==", "value": 1}]}
        assert "unknown indicator" in validate(d)[0], name


def test_divergence_excluded_patterns_stay_unregistered():
    """The 20 value-divergent patterns are excluded, not emulated."""
    for name in DIVERGENCE_EXCLUDED:
        assert f"cdl{name}" not in INDICATORS, name
        assert f"cdl{name}" not in SQL_INDICATORS, name


def test_cdl_pins_match_live_talib_both_engines(con):
    """Pinned hit rows equal live talib on this frame, on BOTH engines.

    The pins are (symbol, bar, value) triples lifted from live talib
    (regenerate with scripts/gen_cdl_fixtures.py). A column-order mistake in
    either builder transposes o/h/l/c and flips or kills these hits; a sign
    mistake flips them. Also pins full-registry scan-level equality:
    for every pattern, the two engines hit the identical (symbol, session)
    sets, and every non-null duckdb value equals live talib bar-for-bar —
    which is exactly the property the 28 exclusions guard.
    """
    import talib

    df = _bars()
    cat = _catalog(df)

    for name, hits in _PINS.items():
        d = _scan(name)  # >= -200: keep every signal, drop SQL warm-up nulls
        sql = apply_sql(con, d, relation="bars", catalog=cat)
        pol = apply(df, dict(d), catalog=cat)
        pcol = next(c for c in pol.columns if c.startswith("__"))
        # identical hit sets cross-engine: the (symbol, session) pairs where
        # the pattern actually fires (value != 0)
        pol_hits = {(s, se) for s, se, v in pol.select("symbol", "session", pcol).rows() if v != 0}
        sql_hits = {(s, se) for s, se, v in sql.select("symbol", "session", "c0").rows() if v != 0}
        assert pol_hits == sql_hits, name
        # each pinned first-hit: polars value, duckdb membership, live talib
        pol_map = {(s, se): v for s, se, v in pol.select("symbol", "session", pcol).rows()}
        for sym, bar, want in hits:
            se = T0 + dt.timedelta(days=bar)
            assert pol_map[(sym, se)] == want, (name, sym, bar, pol_map[(sym, se)], want)
            sql_vals = sql.filter((pl.col("symbol") == sym) & (pl.col("session") == se))["c0"].to_list()
            assert sql_vals == [want], (name, sym, bar, sql_vals, want)
            sub = df.filter(pl.col("symbol") == sym).sort("session")
            r = getattr(talib, name.upper())(
                sub["open"].to_numpy(), sub["high"].to_numpy(),
                sub["low"].to_numpy(), sub["close"].to_numpy())
            assert r[bar] == want, (name, sym, bar, r[bar])


def test_cdl_value_parity_bar_for_bar(con):
    """Full column equality: live talib == duckdb == polars seam, every bar.

    The registry-wide version of the pin test — this is the assertion the
    28 divergence exclusions exist to satisfy. Runs on the walk fixture
    (3 offset copies of the same 155-bar walk; the crafted scenarios only
    carry walk-0's offset copy). The SQL engine drops its warm-up null rows
    at the predicate, so the duckdb column is compared as a value-set keyed
    by (session, value) against talib's full column.
    """
    import talib

    df = _bars()
    cat = _catalog(df)
    syms = sorted(set(df["symbol"].to_list()))
    for name in sorted(n for n in INDICATORS if n.startswith("cdl")):
        fn = name.upper()
        d = _scan(name)
        sql = apply_sql(con, d, relation="bars", catalog=cat)
        pol = apply(df, dict(d), catalog=cat)
        pcol = next(c for c in pol.columns if c.startswith("__"))
        for sym in syms:
            sub = df.filter(pl.col("symbol") == sym).sort("session")
            want = getattr(talib, fn)(sub["open"].to_numpy(), sub["high"].to_numpy(),
                                      sub["low"].to_numpy(), sub["close"].to_numpy())
            got_sql = sql.filter(pl.col("symbol") == sym).sort("session")
            got_pol = pol.filter(pl.col("symbol") == sym).sort("session")[pcol].to_list()
            assert len(got_pol) == sub.height, name
            # polars seam: full column, bar-for-bar
            for i in range(sub.height):
                assert got_pol[i] == int(want[i]), (name, sym, i, got_pol[i], int(want[i]))
            # duckdb: the >= -200 predicate keeps every non-null row (including
            # 0s) and drops exactly the leading warm-up nulls; every surviving
            # row must carry talib's value for its bar
            k = sub.height - got_sql.height  # leading null rows dropped
            se_to_bar = {T0 + dt.timedelta(days=i): i for i in range(sub.height)}
            sql_pairs = list(zip(got_sql["session"].to_list(), got_sql["c0"].to_list()))
            assert {se for se, _ in sql_pairs} == {T0 + dt.timedelta(days=i) for i in range(k, sub.height)}, name
            for se, v in sql_pairs:
                i = se_to_bar[se]
                assert v == int(want[i]), (name, sym, i, v, int(want[i]))


def test_cdl_column_swap_breaks_the_hits():
    """Mutation check: swapping open/close shifts cdlengulfing's hits.

    The fixture data is load-bearing: if this fails after a fixture regen,
    the fixture no longer guards the column order. (A pure o/c transpose is
    symmetry-invisible to high/low-driven patterns like hikkake, so the
    check uses cdlengulfing, whose hits are body-driven and shift loudly.)
    """
    df = _bars()
    cat = _catalog(df)
    swapped = df.with_columns(pl.col("close").alias("open"), pl.col("open").alias("close"))
    d = _scan("cdlengulfing")
    normal_hits = {(s, se) for s, se, v in
                   apply(df, dict(d), catalog=cat).select("symbol", "session",
                                                          "__cdlengulfing_0").rows() if v != 0}
    swapped_hits = {(s, se) for s, se, v in
                    apply(swapped, dict(d), catalog=cat).select("symbol", "session",
                                                                "__cdlengulfing_0").rows() if v != 0}
    assert len(normal_hits) > 0
    assert normal_hits != swapped_hits


def test_cdl_dual_engine_validate_paths():
    """cdlhikkake validates on both engines; stoch_k is still SQL-only."""
    from scanlang.compiler import PROPERTY_CATALOG

    ohlc = {**PROPERTY_CATALOG, "open": {"label": "Open", "dtype": "float"},
            "high": {"label": "High", "dtype": "float"}, "low": {"label": "Low", "dtype": "float"}}
    d = {"filters": [{"property": {"fn": "cdlhikkake", "args": [1]}, "op": "==", "value": 100}]}
    assert validate(d, catalog=ohlc) == []
    assert validate(d, catalog=ohlc, engine="duckdb") == []
    with pytest.raises(ValueError, match="requires engine='duckdb'"):
        compile({"filters": [{"property": {"fn": "stoch_k", "args": [5, 3, 3]},
                              "op": "==", "value": 100}]}, catalog=ohlc)


def test_cdl_seam_without_talib_reports_install_hint(monkeypatch):
    """A talib-less interpreter still validates cdl entries; apply() reports the hint."""
    import builtins
    import importlib
    import sys

    real_import = builtins.__import__

    def _blocked(name, *a, **k):
        if name == "talib" or name.startswith("talib."):
            raise ImportError("No module named 'talib' (blocked by test)")
        return real_import(name, *a, **k)

    monkeypatch.setitem(sys.modules, "talib", None)
    monkeypatch.setattr(builtins, "__import__", _blocked)
    importlib.invalidate_caches()
    df = _bars()
    cat = _catalog(df)
    d = _scan("cdlhikkake", "==", 100)
    assert validate(d, catalog=cat) == []  # entry exists; no import-time talib probe
    with pytest.raises(ValueError, match="requires the optional 'talib' extra"):
        apply(df, dict(d), catalog=cat)
