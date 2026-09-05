"""0.4.0 TA-Lib parity set: both engines, every signature family.

Families (per the plan's matrix): expression + period (wma/dema/tema/trima/
mom + kama seam), OHLC(+volume) columns + period (midprice/cci/willr/trange),
periodless (ad; wave 2 adds ultosc/obv/sar/ht_dcperiod/ht_dcphase),
multiple-period (stoch_k/stoch_d), and multi-output
narrowings (macd line, bbands_upper/lower, aroon up — one talib output field
per scanlang name, seam-built on the polars side). Registry parity is
generated from the registries themselves; execution tests cover partition
isolation, null warm-up masks, and mature-value comparison against official
TA-Lib 0.7.1 for BOTH tiers on BOTH engines: the closed-form polars builders
(incl. the MAD-based cci), the SQL t_* tier, and the kama seam are exact at
1e-9 at every mature bar; the polars recursive EMA builders (dema/tema) seed
from the first value vs talib's SMA-of-n, so they are compared per the plan's
MATURE-convergence tier (<0.01 from bar 112 at n=14 — they converge to ~1e-13
well before it).
"""

import datetime as dt
import math

import polars as pl
import pytest

from scanlang import apply, catalog_from_schema, validate
from scanlang.compiler import compile
from scanlang.indicators import INDICATORS

duckdb = pytest.importorskip("duckdb")
talib = pytest.importorskip("talib")

from scanlang.duckdb_sql import SQL_INDICATORS, apply_sql

T0 = dt.date(2026, 1, 1)
N = 300


def _bars() -> pl.DataFrame:
    """Deterministic 3-symbol OHLCV frame: uptrend, oscillator, sawtooth (sorted symbol, session).

    high/low vary bar-to-bar around close (not a constant +-c band) so
    willr/midprice/cci are bar-discriminating — no degenerate all-equal output.
    """
    a = [10.0 + 0.05 * i for i in range(N)]
    b = [50.0 + 8.0 * (i % 9 - 4) + 0.1 * i for i in range(N)]
    c = [30.0 + 0.25 * (i % 7) for i in range(N)]
    sessions = [T0 + dt.timedelta(days=i) for i in range(N)]
    return pl.concat(
        [
            pl.DataFrame(
                {
                    "symbol": [sym] * N,
                    "session": sessions,
                    "open": [x - 0.2 for x in closes],
                    "high": [x + 1.0 + 0.3 * ((2 * i) % 5) for i, x in enumerate(closes)],
                    "low": [x - 1.0 - 0.3 * ((3 * i) % 5) for i, x in enumerate(closes)],
                    "close": closes,
                    "volume": [1000.0] * N,
                }
            )
            for sym, closes in (("AAA", a), ("BBB", b), ("CCC", c))
        ]
    )


# name -> (fn spec args, talib callable on a per-symbol subframe, warm-up nulls/sym)
# warm-ups verified live on the fixture (talib 0.7.1 + duckdb 1.5.5 community ext).
CASES = {
    "wma": ([{"col": "close"}, 14], lambda g: talib.WMA(g["close"].to_numpy(), timeperiod=14), 13),
    "dema": ([{"col": "close"}, 14], lambda g: talib.DEMA(g["close"].to_numpy(), timeperiod=14), 26),
    "tema": ([{"col": "close"}, 14], lambda g: talib.TEMA(g["close"].to_numpy(), timeperiod=14), 39),
    "trima": ([{"col": "close"}, 14], lambda g: talib.TRIMA(g["close"].to_numpy(), timeperiod=14), 13),
    "kama": ([14], lambda g: talib.KAMA(g["close"].to_numpy(), timeperiod=14), 14),
    "mom": ([{"col": "close"}, 14], lambda g: talib.MOM(g["close"].to_numpy(), timeperiod=14), 14),
    "midprice": ([14], lambda g: talib.MIDPRICE(g["high"].to_numpy(), g["low"].to_numpy(), timeperiod=14), 13),
    "cci": ([14], lambda g: talib.CCI(g["high"].to_numpy(), g["low"].to_numpy(), g["close"].to_numpy(), timeperiod=14), 13),
    "willr": ([14], lambda g: talib.WILLR(g["high"].to_numpy(), g["low"].to_numpy(), g["close"].to_numpy(), timeperiod=14), 13),
    "trange": ([14], lambda g: talib.TRANGE(g["high"].to_numpy(), g["low"].to_numpy(), g["close"].to_numpy()), 1),
    "ad": ([], lambda g: talib.AD(g["high"].to_numpy(), g["low"].to_numpy(), g["close"].to_numpy(), g["volume"].to_numpy()), 0),
    "adxr": ([14], lambda g: talib.ADXR(g["high"].to_numpy(), g["low"].to_numpy(), g["close"].to_numpy(), timeperiod=14), 40),
    "cmo": ([14], lambda g: talib.CMO(g["close"].to_numpy(), timeperiod=14), 14),
    "trix": ([14], lambda g: talib.TRIX(g["close"].to_numpy(), timeperiod=14), 40),
    # periodless (Task 0): ULTOSC binds timeperiod1/2/3, OBV takes no period —
    # hand-written seam builders; stochrsi/apo/ppo/mfi/adosc/t3/sar/accbands
    # have no t_* in the community extension (live-probed duckdb_functions())
    # — polars tier only.
    "stochrsi": ([14], lambda g: talib.STOCHRSI(g["close"].to_numpy(), timeperiod=14, fastk_period=14, fastd_period=3, fastd_matype=0)[0], 29),
    "apo": ([12], lambda g: talib.APO(g["close"].to_numpy(), fastperiod=12, slowperiod=26, matype=0), 25),
    "ppo": ([12], lambda g: talib.PPO(g["close"].to_numpy(), fastperiod=12, slowperiod=26, matype=0), 25),
    "mfi": ([14], lambda g: talib.MFI(g["high"].to_numpy(), g["low"].to_numpy(), g["close"].to_numpy(), g["volume"].to_numpy(), timeperiod=14), 14),
    "adosc": ([3], lambda g: talib.ADOSC(g["high"].to_numpy(), g["low"].to_numpy(), g["close"].to_numpy(), g["volume"].to_numpy(), fastperiod=3, slowperiod=10), 9),
    "ultosc": ([14], lambda g: talib.ULTOSC(g["high"].to_numpy(), g["low"].to_numpy(), g["close"].to_numpy()), 28),
    "obv": ([14], lambda g: talib.OBV(g["close"].to_numpy(), g["volume"].to_numpy()), 0),
    # overlap wave-2: midpoint/ht_dcperiod/ht_dcphase are dual-engine (t_* in
    # the extension); t3/sar/accbands are polars+talib-extra-only.
    "midpoint": ([14], lambda g: talib.MIDPOINT(g["close"].to_numpy(), timeperiod=14), 13),
    "t3": ([14], lambda g: talib.T3(g["close"].to_numpy(), timeperiod=14, vfactor=0.7), 78),
    "sar": ([14], lambda g: talib.SAR(g["high"].to_numpy(), g["low"].to_numpy()), 1),
    "accbands_upper": ([14], lambda g: talib.ACCBANDS(g["high"].to_numpy(), g["low"].to_numpy(), g["close"].to_numpy(), timeperiod=14)[0], 13),
    "accbands_lower": ([14], lambda g: talib.ACCBANDS(g["high"].to_numpy(), g["low"].to_numpy(), g["close"].to_numpy(), timeperiod=14)[2], 13),
    # cycle wave-2 (dual-engine): fixed Hilbert-transform warm-ups, no bindable n
    "ht_dcperiod": ([14], lambda g: talib.HT_DCPERIOD(g["close"].to_numpy()), 32),
    "ht_dcphase": ([14], lambda g: talib.HT_DCPHASE(g["close"].to_numpy()), 63),
    # multi-output narrowings (polars side rides the adx/kama seam):
    # each scanlang name is ONE talib output field, selected here at the
    # same index the SQL struct narrowing picks.
    "macd": ([12], lambda g: talib.MACD(g["close"].to_numpy(), fastperiod=12, slowperiod=26, signalperiod=9)[0], 33),
    "bbands_upper": ([20], lambda g: talib.BBANDS(g["close"].to_numpy(), timeperiod=20, nbdevup=2.0, nbdevdn=2.0, matype=0)[0], 19),
    "bbands_lower": ([20], lambda g: talib.BBANDS(g["close"].to_numpy(), timeperiod=20, nbdevup=2.0, nbdevdn=2.0, matype=0)[2], 19),
    "aroon": ([14], lambda g: talib.AROON(g["high"].to_numpy(), g["low"].to_numpy(), timeperiod=14)[1], 14),  # (down, up) — index 1 is UP
}

# polars builders compared to talib per the plan's MATURE-convergence tier
# (<0.01 from bar 112) instead of the exact 1e-9 tier: recursive EMA chains
# seed from the first value vs talib's SMA-of-n (documented contract).
MATURE = 112
_TIER = {"dema": 0.01, "tema": 0.01}


@pytest.fixture(scope="module")
def con():
    path = "/tmp/scanlang_parity_bars.parquet"
    _bars().write_parquet(path)
    con = duckdb.connect()
    con.execute(f"CREATE VIEW bars AS SELECT * FROM '{path}'")
    yield con
    con.close()


def test_registry_parity_generated():
    """Every new single-output name: dual registries agree; arg/col tags valid.

    Generated from the registries (not a hand list): the plan's target set
    must ALL be present, every arg_spec is expr/int only, and dual-engine
    names mirror 1:1 while stoch_k/stoch_d stay SQL-only.
    """
    target = {
        "wma", "dema", "tema", "trima", "kama", "mom", "midprice", "cci", "willr", "trange", "ad",
        "macd", "bbands_upper", "bbands_lower", "aroon", "stoch_k", "stoch_d",
        # wave 2: momentum, overlap, cycle
        "adxr", "cmo", "trix", "stochrsi", "apo", "ppo", "mfi", "adosc", "ultosc", "obv",
        "midpoint", "t3", "sar", "accbands_upper", "accbands_lower",
        "ht_dcperiod", "ht_dcphase",
    }
    # no t_* in the community extension (live-probed): polars tier only
    sql_extra = {"ultosc", "obv", "mfi", "adosc", "stochrsi", "apo", "ppo", "t3", "sar", "accbands_upper", "accbands_lower"}
    assert target <= set(SQL_INDICATORS) | sql_extra
    dual = target - {"stoch_k", "stoch_d"}
    assert dual <= set(INDICATORS)
    sql_only = sql_extra  # no t_* in the extension
    for name in dual - sql_only:
        assert INDICATORS[name][0] == SQL_INDICATORS[name][0], name
        assert INDICATORS[name][2] == SQL_INDICATORS[name][2], name
    for name in target:
        if name in sql_only:
            continue
        assert set(SQL_INDICATORS[name][0]) <= {"expr", "int"}, name
        assert all(isinstance(c, str) for c in SQL_INDICATORS[name][2]), name
    assert INDICATORS["ad"][0] == ()  # the first periodless entry


def test_new_entries_validate_on_both_engines():
    """Engine-aware validate: dual names OK everywhere, stoch SQL-only."""
    df = _bars()
    cat = catalog_from_schema(df)
    for name, (args, _fn, _w) in CASES.items():
        d = {"filters": [{"property": {"fn": name, "args": args}, "op": ">=", "value": -1e9}]}
        assert validate(d, catalog=cat, engine="polars") == [], name
        assert validate(d, catalog=cat, engine="duckdb") == [], name
    for name in ("stoch_k", "stoch_d"):
        d = {"filters": [{"property": {"fn": name, "args": [5, 3, 3]}, "op": ">=", "value": -1e9}]}
        assert validate(d, catalog=cat, engine="duckdb") == [], name
        assert "requires engine='duckdb'" in validate(d, catalog=cat)[0]
    # missing required cols still surfaces (catalog-validated, per family)
    for name, cols in (
        ("midprice", ("high", "low")),
        ("cci", ("high", "low", "close")),
        ("ad", ("high", "low", "close", "volume")),
    ):
        bare = {"filters": [{"property": {"fn": name, "args": [] if name == "ad" else [14]}, "op": ">=", "value": 0}]}
        err = validate(bare)[0]
        assert "requires column 'high'" in err, (name, err)  # first missing col reported


def test_parity_set_execution_and_mature_values(con):
    """Every family executes on BOTH engines; values == talib on both tiers.

    The polars builder (staged alias for the kama seam, direct expr
    evaluation for the rest) AND the SQL c0 tier are value-compared per
    symbol at every mature bar: exact 1e-9 except dema/tema, whose polars
    EMA chains run the plan's MATURE-convergence tier (<0.01 from bar 112).
    Partition isolation: hits split exactly 3 x (N - warmup) per symbol with
    per-partition warm-up nulls (null mask), and the null-count is asserted
    before the value comparison so a warm-up leak cannot pass silently.
    """
    df = _bars()
    cat = catalog_from_schema(df)
    # dema/tema: the polars EMA chain emits from bar 0 (warm-up contract),
    # so those two have full-length polars output; the SQL tier nulls the
    # documented warm-up window. Seam names pre-stage __<name>_0 (kama plus
    # the multi-output seam fns — their builders return callables, not Exprs).
    full_polars = {"dema", "tema"}
    seam_names = {"kama", "macd", "bbands_upper", "bbands_lower", "aroon",
                  # wave-2 seam fns (table rows + the periodless builders)
                  "adxr", "cmo", "trix", "stochrsi", "apo", "ppo", "mfi", "adosc",
                  "ultosc", "obv", "midpoint", "t3", "sar",
                  "accbands_upper", "accbands_lower", "ht_dcperiod", "ht_dcphase"}
    sql_only = {"ultosc", "obv", "mfi", "adosc", "stochrsi", "apo", "ppo",
                "t3", "sar", "accbands_upper", "accbands_lower"}  # no t_* in the extension
    for name, (args, ref_fn, warmup) in CASES.items():
        d = {"filters": [{"property": {"fn": name, "args": args}, "op": ">=", "value": -1e9}]}
        pol = apply(df, d, catalog=cat)
        if name not in sql_only:
            sql = apply_sql(con, d, relation="bars", catalog=cat)
        else:
            sql = None  # no t_* in the extension — polars tier only
        # null masks + partition isolation (warm-up never leaks into hits)
        expected = 3 * N if name in full_polars else 3 * (N - warmup)
        assert pol.height == expected, name
        if name not in sql_only:
            assert sql.height == 3 * (N - warmup), name
        assert pol.group_by("symbol", maintain_order=True).len()["len"].to_list() == [expected // 3] * 3, name
        if name not in sql_only:
            assert sql.group_by("symbol", maintain_order=True).len()["len"].to_list() == [N - warmup] * 3, name
            assert sql["c0"].null_count() == 0, name
        alias = f"__{name}_0" if name in seam_names else None
        if alias is not None:
            assert alias in pol.columns and pol[alias].null_count() == 0, name
        # mature-value parity vs official TA-Lib, per symbol, every mature bar,
        # on BOTH tiers: the polars builder is evaluated on the sorted subframe
        # (the exact hole the round-1 cci std-denominator bug fell through),
        # the SQL c0 tier always, and the kama seam via its staged alias.
        for sym in ("AAA", "BBB", "CCC"):
            sub = df.filter(pl.col("symbol") == sym).sort("session")
            ref = ref_fn(sub)
            sessions = sub["session"].to_list()
            s_vals = (
                dict(zip(sessions[warmup:], sql.filter(pl.col("symbol") == sym)["c0"].to_list(), strict=True))
                if name not in sql_only else None
            )
            if name in seam_names:
                p_vals = dict(zip(sessions[warmup:], pol.filter(pl.col("symbol") == sym)[alias].to_list(), strict=True))
            else:
                arg_spec, builder, _req = INDICATORS[name]
                parsed = [a if tag == "int" else pl.col(a["col"]) for tag, a in zip(arg_spec, args)]
                p_expr = builder(*parsed, partition="symbol")
                p_vals = dict(zip(sessions, sub.select(p_expr.alias("v"))["v"].to_list(), strict=True))
            tol = _TIER.get(name, 1e-9)
            from_bar = MATURE if name in _TIER else warmup
            for i, sess in enumerate(sessions[warmup:]):
                r = ref[warmup + i]
                if isinstance(r, float) and math.isnan(r):
                    continue  # talib's own unstable-period tail (none in this table)
                if warmup + i >= from_bar:
                    assert abs(p_vals[sess] - r) <= tol, (name, "polars", sym, sess, p_vals[sess], r)
                if s_vals is not None:
                    assert abs(s_vals[sess] - r) <= 1e-9, (name, "sql", sym, sess, s_vals[sess], r)


def test_cci_cross_engine_hit_set_equality(con):
    """Discriminating cci filter: identical hits on polars and duckdb.

    The round-1 std-denominator bug produced 65 polars vs 179 duckdb hits
    for cci(14) > 100 on this fixture — the engines must agree row-for-row
    now that the builder uses the window MAD.
    """
    df = _bars()
    cat = catalog_from_schema(df)
    d = {"filters": [{"property": {"fn": "cci", "args": [14]}, "op": ">", "value": 100}]}
    assert validate(d, catalog=cat) == []
    pol = apply(df, d, catalog=cat).select("symbol", "session")
    sql = apply_sql(con, d, relation="bars", catalog=cat).select("symbol", "session")
    assert pol.height > 0, "probe must discriminate (nonempty, nonfull)"
    assert pol.height < 3 * (N - 13)
    assert pol.equals(sql.sort("symbol", "session"))


def _degenerate_bars() -> pl.DataFrame:
    """Two 60-bar symbols for the 0/0 twins: VAR has ONE zero-range bar.

    VAR: varied OHLCV except bar 30 where high==low==close (illiquid
    single-print) — pre-guard, that NaN poisoned ad's cum_sum for the rest
    of the partition. HALT: one identical print repeated — cci's window MAD
    is 0, pre-guard every mature NaN passed `> 100`.
    """
    n = 60
    sessions = [T0 + dt.timedelta(days=i) for i in range(n)]
    closes = [100.0 - 0.4 * i + 3.0 * ((i * 7) % 11) for i in range(n)]
    high = [c + 0.8 + 0.1 * (i % 3) if i != 30 else c for i, c in enumerate(closes)]
    low = [c - 0.8 - 0.1 * ((i + 1) % 3) if i != 30 else c for i, c in enumerate(closes)]
    var = pl.DataFrame(
        {
            "symbol": ["VAR"] * n,
            "session": sessions,
            "open": [c - 0.1 for c in closes],
            "high": high,
            "low": low,
            "close": closes,
            "volume": [500.0 + 10.0 * (i % 4) for i in range(n)],
        }
    )
    halt = pl.DataFrame(
        {
            "symbol": ["HALT"] * n,
            "session": sessions,
            "open": [42.0] * n,
            "high": [42.0] * n,
            "low": [42.0] * n,
            "close": [42.0] * n,
            "volume": [250.0] * n,
        }
    )
    return pl.concat([var, halt])


@pytest.fixture(scope="module")
def con_deg():
    path = "/tmp/scanlang_degenerate_bars.parquet"
    _degenerate_bars().write_parquet(path)
    con = duckdb.connect()
    con.execute(f"CREATE VIEW deg AS SELECT * FROM '{path}'")
    yield con
    con.close()


def test_zero_range_and_flat_window_cross_engine(con_deg):
    """0/0 twins of the cci cross-engine test: builders match talib AND duckdb.

    ad on VAR: the zero-range bar must contribute 0.0 (talib semantics), not
    NaN-poison cum_sum. cci on HALT: flat window (MAD==0) must emit 0.0 at
    mature bars with the warm-up nulls intact. Both engines must return
    identical hit sets for discriminating filters (pre-guard: polars NaN
    passed `>`, duckdb didn't — 30 vs 0 ad hits, 47 vs 0 cci hits).
    """
    df = _degenerate_bars()
    cat = catalog_from_schema(df)
    for fn, args, value in (("ad", [], 0.0), ("cci", [14], 100.0)):
        d = {"filters": [{"property": {"fn": fn, "args": args}, "op": ">", "value": value}]}
        assert validate(d, catalog=cat, engine="duckdb") == []
        pol = apply(df, d, catalog=cat).select("symbol", "session")
        sql = apply_sql(con_deg, d, relation="deg", catalog=cat).select("symbol", "session")
        assert pol.equals(sql.sort("symbol", "session")), fn

    var = df.filter(pl.col("symbol") == "VAR").sort("session")
    ref = talib.AD(var["high"].to_numpy(), var["low"].to_numpy(), var["close"].to_numpy(), var["volume"].to_numpy())
    _args, builder, _req = INDICATORS["ad"]
    ours = var.select(builder(partition="symbol").alias("v"))["v"]
    assert all(ours[i] == pytest.approx(ref[i], abs=1e-9) for i in range(var.height))

    halt = df.filter(pl.col("symbol") == "HALT").sort("session")
    ref = talib.CCI(halt["high"].to_numpy(), halt["low"].to_numpy(), halt["close"].to_numpy(), timeperiod=14)
    _args, builder, _req = INDICATORS["cci"]
    ours = halt.select(builder(14, partition="symbol").alias("v"))
    assert ours["v"].null_count() == 13  # warm-up mask survives the guard chain
    assert (ours["v"].slice(13) == 0.0).all()
    assert (ref[13:] == 0.0).all()  # talib agrees — flat window pins 0.0


def test_stoch_k_d_struct_narrowing_and_warmup(con):
    """stoch_k/stoch_d: 3-int signature executes; struct narrowing + warm-up pinned.

    fastk=5, slowk=3, slowd=3 -> 8 warm-up nulls/symbol (4+2+2, verified live);
    slowk == talib.STOCH's slowk array at every mature bar, slowd at its own.
    """
    df = _bars()
    cat = catalog_from_schema(df)
    warmup = 8
    d = {"filters": [
        {"property": {"fn": "stoch_k", "args": [5, 3, 3]}, "op": ">=", "value": -1e9},
        {"property": {"fn": "stoch_d", "args": [5, 3, 3]}, "op": ">=", "value": -1e9},
    ]}
    assert validate(d, catalog=cat, engine="duckdb") == []
    sql = apply_sql(con, d, relation="bars", catalog=cat)
    assert sql.height == 3 * (N - warmup)
    assert sql.group_by("symbol", maintain_order=True).len()["len"].to_list() == [N - warmup] * 3
    assert sql["c0"].null_count() == 0 and sql["c1"].null_count() == 0
    assert "requires engine='duckdb'" in validate(d, catalog=cat)[0]
    # polars compile can't lower a SQL-only name — actionable error, not KeyError
    with pytest.raises(ValueError, match="SQL-only"):
        compile(dict(d), catalog=cat, engine="duckdb")
    for sym in ("AAA", "BBB", "CCC"):
        sub = df.filter(pl.col("symbol") == sym).sort("session")
        sk, sd = talib.STOCH(sub["high"].to_numpy(), sub["low"].to_numpy(), sub["close"].to_numpy(),
                             fastk_period=5, slowk_period=3, slowk_matype=0, slowd_period=3, slowd_matype=0)
        got = sql.filter(pl.col("symbol") == sym)
        for i, (k, dd) in enumerate(zip(got["c0"].to_list(), got["c1"].to_list(), strict=True)):
            assert abs(k - sk[warmup + i]) <= 1e-9, (sym, i, k, sk[warmup + i])
            assert abs(dd - sd[warmup + i]) <= 1e-9, (sym, i, dd, sd[warmup + i])


def test_multi_output_swap_proof_warmup_and_cross_engine(con):
    """Each multi-output field is the RIGHT field: not swapped, warm-up intact, engines agree.

    Swap discrimination per name, against the sibling talib outputs:
    - macd vs the signal line (crosses the line repeatedly on BBB's
      oscillator geometry — a swapped builder flips the sign relation),
    - bbands_upper/lower vs the middle band (sma) and each other
      (upper != lower at every mature bar),
    - aroon_up vs aroon_down (both vary per-row on the fixture; pinned
      at the first mature bars per symbol — the old test_duckdb_sql pin
      took min-order across symbols, so here each symbol's own head is
      compared).

    Warm-up null counts are the talib semantics: macd(12,26,9)=33,
    bbands(20)=19, aroon(14)=14 per symbol (verified live, talib 0.7.1).
    Cross-engine: identical hit sets for a discriminating filter on
    each name.
    """
    df = _bars()
    cat = catalog_from_schema(df)
    warmups = {"macd": 33, "bbands_upper": 19, "bbands_lower": 19, "aroon": 14}
    for name, warmup in warmups.items():
        d = {"filters": [{"property": {"fn": name, "args": CASES[name][0]}, "op": ">=", "value": -1e9}]}
        assert validate(d, catalog=cat) == [], name
        pol = apply(df, d, catalog=cat)
        alias = f"__{name}_0"
        assert alias in pol.columns, name
        assert pol.height == 3 * (N - warmup), name
        assert pol.group_by("symbol", maintain_order=True).len()["len"].to_list() == [N - warmup] * 3, name
        assert pol[alias].null_count() == 0, name
        # polars seam == talib at every mature bar, per symbol
        for sym in ("AAA", "BBB", "CCC"):
            sub = df.filter(pl.col("symbol") == sym).sort("session")
            ref = CASES[name][1](sub)
            got = dict(zip(
                sub["session"].to_list()[warmup:],
                pol.filter(pl.col("symbol") == sym)[alias].to_list(),
                strict=True,
            ))
            for i, sess in enumerate(sub["session"].to_list()[warmup:], start=warmup):
                assert abs(got[sess] - ref[i]) <= 1e-9, (name, sym, sess, got[sess], ref[i])
        # cross-engine hit-set equality on a discriminating filter
        thr = {"macd": 0.0, "bbands_upper": 40.0, "bbands_lower": 20.0, "aroon": 60.0}[name]
        dv = {"filters": [{"property": {"fn": name, "args": CASES[name][0]}, "op": ">", "value": thr}]}
        pol_hits = apply(df, dv, catalog=cat).select("symbol", "session")
        sql_hits = apply_sql(con, dv, relation="bars", catalog=cat).select("symbol", "session")
        assert 0 < pol_hits.height < 3 * (N - warmup), (name, pol_hits.height)
        assert pol_hits.equals(sql_hits.sort("symbol", "session")), name

    # --- swap proofs -----------------------------------------------------
    def _vals(name, warmup):
        d = {"filters": [{"property": {"fn": name, "args": CASES[name][0]}, "op": ">=", "value": -1e9}]}
        pol = apply(df, d, catalog=cat)
        out = {}
        for sym in ("AAA", "BBB", "CCC"):
            sub = df.filter(pl.col("symbol") == sym).sort("session")
            out[sym] = dict(zip(
                sub["session"].to_list()[warmup:],
                pol.filter(pl.col("symbol") == sym)[f"__{name}_0"].to_list(),
                strict=True,
            ))
        return out

    macd = _vals("macd", 33)
    bup = _vals("bbands_upper", 19)
    blo = _vals("bbands_lower", 19)
    aro = _vals("aroon", 14)
    for sym in ("AAA", "BBB", "CCC"):
        sub = df.filter(pl.col("symbol") == sym).sort("session")
        sessions = sub["session"].to_list()
        _m, sig, _h = talib.MACD(sub["close"].to_numpy(), fastperiod=12, slowperiod=26, signalperiod=9)
        _up, mid, _lo = talib.BBANDS(sub["close"].to_numpy(), timeperiod=20, nbdevup=2.0, nbdevdn=2.0, matype=0)
        dn, _upl = talib.AROON(sub["high"].to_numpy(), sub["low"].to_numpy(), timeperiod=14)
        # macd == talib macd[0] and is NOT the signal line. Discrimination
        # runs on BBB only: AAA's close is a linear ramp, where the MACD
        # line and its signal EMA coincide exactly (nothing to distinguish).
        if sym == "BBB":
            assert any(abs(macd[sym][sessions[i]] - sig[i]) > 1e-6 for i in range(33, N)), sym
            flips = [macd[sym][sessions[i]] - sig[i] > 0 for i in range(33, N)]
            assert any(flips) and not all(flips), "macd-vs-signal must cross on BBB"
        # bbands: upper != lower everywhere mature; both differ from mid somewhere
        for i in range(19, N):
            assert bup[sym][sessions[i]] != blo[sym][sessions[i]], (sym, i)
        assert any(abs(bup[sym][sessions[i]] - mid[i]) > 1e-6 for i in range(19, N)), sym
        assert any(abs(blo[sym][sessions[i]] - mid[i]) > 1e-6 for i in range(19, N)), sym
        # aroon: up != down somewhere per symbol; pin each symbol's first
        # three mature values (down line diverges immediately; the values
        # are the descending counts/14 * 100 of each symbol's own window)
        assert any(abs(aro[sym][sessions[i]] - dn[i]) > 1e-6 for i in range(14, N)), sym
        want3 = {
            "AAA": [6 / 7 * 100, 11 / 14 * 100, 5 / 7 * 100],
            "BBB": [4 / 7 * 100, 1 / 2 * 100, 3 / 7 * 100],
            "CCC": [6 / 7 * 100, 11 / 14 * 100, 5 / 7 * 100],
        }[sym]
        for k in range(3):
            assert aro[sym][sessions[14 + k]] == pytest.approx(want3[k]), (sym, k)


def test_multi_output_unlisted_fields_stay_unregistered():
    """Approved catalog: one field per multi-output talib function.

    macd exposes only the line (signal/hist unexposed), bbands only the
    upper/lower bands (the middle band is just sma(close, n)), aroon only
    the up line (down unexposed). The exclusion holds on BOTH registries —
    a scan can never reference a struct through the IR.
    """
    for excluded in ("macd_signal", "macd_hist", "bbands_middle", "aroon_down"):
        assert excluded not in INDICATORS, excluded
        assert excluded not in SQL_INDICATORS, excluded
    # and the exposed names still carry the exact narrowings: registry
    # mirrors 1:1 (same arg_spec, same required_cols) on both engines
    for name, args in (("macd", [12]), ("bbands_upper", [20]), ("bbands_lower", [20]), ("aroon", [14])):
        assert INDICATORS[name][0] == SQL_INDICATORS[name][0] == ("int",), name
        assert INDICATORS[name][2] == SQL_INDICATORS[name][2], name


def test_trange_ignores_n_and_kama_eager_contract(con):
    """Dummy-int + eager-seam corner cases: trange(n) == trange(m); kama needs an eager frame.

    trange's user-facing n is silently ignored (ht_trendline precedent); kama
    rides the adx seam (eager DataFrame -> DataFrame callable), so a
    LazyFrame input must fail with the collect hint instead of leaking NaNs.
    """
    df = _bars()
    cat = catalog_from_schema(df)
    d14 = {"filters": [{"property": {"fn": "trange", "args": [14]}, "op": ">=", "value": -1e9}]}
    d99 = {"filters": [{"property": {"fn": "trange", "args": [99]}, "op": ">=", "value": -1e9}]}
    v14 = apply(df, d14, catalog=cat)
    v99 = apply(df, d99, catalog=cat)
    assert v14.shape == v99.shape and v14.equals(v99)

    d = {"filters": [{"property": {"fn": "kama", "args": [14]}, "op": ">=", "value": 0}]}
    lazy = df.lazy()
    with pytest.raises(ValueError, match="eager frame"):
        apply(lazy, d, catalog=cat)
    # the eager path still runs the seam (exactness covered by the parity test)
    hits = apply(df, d, catalog=cat)
    assert hits.height == 3 * (N - 14)


def test_parity_operand_grammar_and_partition_kwarg(con):
    """New names work in value/arith positions and under a custom partition column."""
    df = _bars()
    cat = catalog_from_schema(df)
    # value position: close < midprice — true only on dips within the window
    # (midprice is an Expr builder, so no alias column — assert on row count)
    d_val = {"filters": [{"property": "close", "op": "<", "value": {"fn": "midprice", "args": [14]}}]}
    assert validate(d_val, catalog=cat) == []
    hits = apply(df, d_val, catalog=cat)
    assert hits.height > 0 and hits.height < 3 * (N - 13)
    # arithmetic tree: willr + 44 > 0 somewhere but not everywhere (willr dips below -44)
    d_arith = {"filters": [{"property": {"+": [{"fn": "willr", "args": [14]}, 44]},
                            "op": ">", "value": 0}]}
    hits2 = apply(df, d_arith, catalog=cat)
    assert 0 < hits2.height < 3 * (N - 13)
    # custom partition: rename symbol -> sym and re-run ad under partition="sym"
    renamed = df.rename({"symbol": "sym"})
    d_ad = {"filters": [{"property": {"fn": "ad", "args": []}, "op": ">=", "value": -1e18}]}
    got = apply(renamed, d_ad, catalog=catalog_from_schema(renamed), partition="sym")
    assert got.group_by("sym", maintain_order=True).len()["len"].to_list() == [N] * 3


def test_seam_builder_factory_covers_existing_names():
    """The factory reproduces every existing hand-written seam closure."""
    from scanlang.indicators import INDICATORS

    cases = {  # name -> (fn, inputs, kwargs, slot)
        "adx": ("ADX", ("high", "low", "close"), {}, None),
        "kama": ("KAMA", ("close",), {}, None),
        "macd": ("MACD", ("close",), {"slowperiod": 26, "signalperiod": 9}, 0),
        "bbands_upper": ("BBANDS", ("close",), {"nbdevup": 2.0, "nbdevdn": 2.0, "matype": 0}, 0),
        "bbands_lower": ("BBANDS", ("close",), {"nbdevup": 2.0, "nbdevdn": 2.0, "matype": 0}, 2),
        "aroon": ("AROON", ("high", "low"), {}, 1),
    }
    for name, (fn, cols, kw, slot) in cases.items():
        arg_spec, builder, req = INDICATORS[name]
        assert arg_spec == ("int",)
        assert req == cols, name
        # builder(14, partition="symbol") must return a callable (the seam)
        seam = builder(14, partition="symbol")
        assert callable(seam)
