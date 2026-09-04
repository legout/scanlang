"""rs_ratio / rs_momentum: temporal z-score RS normalization.

Polars: EMA5 -> trailing population z (n) -> 100 + 5z clamped [80, 120].
Momentum: 4w ROC of the *normalized* ratio -> EMA3 -> its own z. Warm-up is
null until the first n smoothed values exist; a flat window pins 100.0.
SQL lowering mirrors the polars math (t_ema with null strip/re-pad,
window-tier z) and is pinned cross-engine below (skip = no duckdb).
"""

import math

import polars as pl
import pytest

from scanlang import apply, catalog_from_schema, validate
from scanlang.indicators import INDICATORS

RATIO = {"fn": "rs_ratio", "args": [{"col": "rs"}, 26]}
MOMENTUM = {"fn": "rs_momentum", "args": [RATIO, 13]}
SPAN, ZW, MOM_LB, MOM_SPAN = 5, 26, 4, 3
# polars null-seeds its ewm, duckdb t_ema SMA-seeds (the accepted warm-up
# divergence, Q1 of the 2026-09-02 plan). The seed-tail gap decays
# geometrically and is <1e-6 from bar ~72 on the momentum leg; every parity
# assertion compares from MATURE, and this engine may emit values from
# different bars (54 vs 60) by contract.
MATURE = 100


def _nullseed_ewm(e: pl.Expr, span: int) -> pl.Expr:
    """polars null-seeded ewm chain helper (the reference for the polars engine)."""
    return e.ewm_mean(span=span, adjust=False).over("symbol")


def _reference(vals: list[float]) -> dict[str, list[float | None]]:
    """Pure-python pipeline with a TA-Lib-style SMA-seeded ewm (duckdb's t_ema).

    polars null-seeds its ewm instead, so cross-engine values converge only
    after the seed tail decays (<1e-9 by bar SPAN+ZW; the parity assertions
    pin the mature tail, the polars self-check compares exactly).
    """
    scale, eps, lo, hi = 5.0, 1e-6, 80.0, 120.0

    def ewm(vs: list, s: int) -> list:
        a = 2.0 / (s + 1.0)
        out = [None] * len(vs)
        if len(vs) < s:
            return out
        prev = sum(vs[:s]) / s
        out[s - 1] = prev
        for i in range(s, len(vs)):
            prev = a * vs[i] + (1 - a) * prev
            out[i] = prev
        return out

    def zsc(vs: list, n: int) -> list:
        out = []
        for i in range(len(vs)):
            if i < n - 1 or vs[i] is None or any(v is None for v in vs[i - n + 1 : i + 1]):
                out.append(None)
                continue
            w = vs[i - n + 1 : i + 1]
            mu = sum(w) / n
            sd = math.sqrt(sum((v - mu) ** 2 for v in w) / n)
            zz = 0.0 if sd < eps else (vs[i] - mu) / sd
            out.append(max(lo, min(hi, 100.0 + scale * zz)))
        return out

    ratio = zsc(ewm([float(v) for v in vals], SPAN), ZW)
    roc = [
        None if (i < MOM_LB or ratio[i] is None or ratio[i - MOM_LB] is None) else ratio[i] - ratio[i - MOM_LB]
        for i in range(len(ratio))
    ]
    lead = next((i for i, v in enumerate(roc) if v is not None), len(roc))  # t_ema strips+repads
    mom = ewm(roc[lead:], MOM_SPAN)
    return {"ratio": ratio, "momentum": zsc([None] * lead + mom, ZW)}


def _bars() -> pl.DataFrame:
    """One partition of 300 wavy bars (deterministic)."""
    vals = [100.0 + 10 * math.sin(i / 7.0) + 0.05 * i + (2.0 if i % 11 == 0 else 0.0) for i in range(300)]
    return pl.DataFrame({"symbol": ["A"] * 300, "session": list(range(300)), "rs": vals})


def test_validate_accepts_and_rejects():
    cat = catalog_from_schema(_bars())
    assert validate({"filters": [{"property": RATIO, "op": ">=", "value": 110.0}]}, catalog=cat) == []
    bad = {"filters": [{"property": {"fn": "rs_ratio", "args": [{"col": "rs"}, 1.5]}, "op": ">", "value": 0}]}
    assert "must be an int" in validate(bad, catalog=cat)[0]


def test_ratio_warmup_flat_and_nullseed_reference():
    df = _bars()
    got = df.select(INDICATORS["rs_ratio"][1](pl.col("rs"), ZW, "symbol").alias("v"))["v"].to_list()
    assert got[: ZW - 1] == [None] * (ZW - 1)  # warm-up: null until 26 smoothed values
    assert all(80.0 <= v <= 120.0 for v in got if v is not None)
    # exact vs a null-seeded (polars-native) reference of the same math
    e = _nullseed_ewm(pl.col("rs"), SPAN)
    sd = e.rolling_std(ZW, ddof=0).over("symbol")
    z = pl.when(sd < 1e-6).then(0.0).otherwise((e - e.rolling_mean(ZW).over("symbol")) / sd)
    ref = df.select((100.0 + 5.0 * z).clip(80.0, 120.0).alias("v"))["v"].to_list()
    assert max(abs(a - b) for a, b in zip(got, ref) if a is not None) < 1e-9
    # flat series: zero-variance window pins exactly 100.0 after warm-up
    flat = pl.DataFrame({"symbol": ["A"] * 60, "session": list(range(60)), "rs": [100.0] * 60})
    fg = flat.select(INDICATORS["rs_ratio"][1](pl.col("rs"), ZW, "symbol").alias("v"))["v"].to_list()
    assert fg[: ZW - 1] == [None] * (ZW - 1)
    assert fg[ZW - 1 :] == [100.0] * (60 - ZW + 1)


def test_momentum_reference_and_composition():
    df = _bars()
    builder = INDICATORS["rs_momentum"][1]
    ratio = INDICATORS["rs_ratio"][1](pl.col("rs"), ZW, "symbol")
    got = df.select(builder(ratio, ZW, "symbol").alias("v"))["v"].to_list()
    want = _reference(df["rs"].to_list())["momentum"]
    first = 25 + MOM_LB + 12  # 25 ratio warm-up + 4 roc + 12 z warm-up (null-seeded)
    assert got[:first] == [None] * first
    assert max(abs(a - b) for a, b in zip(got[MATURE:], want[MATURE:])) < 1e-5
    assert all(80.0 <= v <= 120.0 for v in got if v is not None)


def test_scan_end_to_end():
    df = _bars()
    d = {"filters": [{"property": MOMENTUM, "op": ">=", "value": 80.0}]}
    assert validate(d, catalog=catalog_from_schema(df)) == []
    out = apply(df, d, catalog=catalog_from_schema(df))
    assert len(out) == 300 - 41  # warm-up rows drop, mature rows all pass (clamped >= 80)


def test_sql_parity():
    """Cross-engine: same scan through apply() and apply_sql() on one fixture.

    Hit-set equality is claimed for the mature window only: polars'
    null-seeded ewm emits from bar 0, duckdb's SMA-seeded t_ema from bar
    span-1, so the warm-up bar ranges differ (the accepted Q1 contract, same
    reason the golden suite claims hit equality for sma-family only).
    """
    duckdb = pytest.importorskip("duckdb")
    from scanlang.duckdb_sql import apply_sql

    df = _bars()
    path = "/tmp/scanlang_rs_bars.parquet"
    df.write_parquet(path)
    c = duckdb.connect()
    try:
        c.execute(f"CREATE VIEW bars AS SELECT * FROM '{path}'")
        cat = catalog_from_schema(df)
        builder = INDICATORS["rs_momentum"][1]
        ratio = INDICATORS["rs_ratio"][1](pl.col("rs"), ZW, "symbol")
        ref = df.with_columns(
            INDICATORS["rs_ratio"][1](pl.col("rs"), ZW, "symbol").alias("ratio"),
            builder(ratio, 13, "symbol").alias("momentum"),  # matches MOMENTUM's z-window
        )
        for spec, col, alias in ((RATIO, "ratio", "c0"), (MOMENTUM, "momentum", "c1")):
            want = dict(zip(ref["session"], ref[col], strict=True))
            d = {"filters": [{"property": spec, "op": ">=", "value": 80.0}]}
            sql_out = apply_sql(c, d, relation="bars", catalog=cat)
            got = dict(zip(sql_out["session"], sql_out[alias], strict=True))
            mature = [s for s, v in want.items() if v is not None and s >= MATURE]
            assert mature, spec
            assert set(got) & set(mature) == set(mature), spec  # every mature bar computed on SQL too
            assert all(got[s] >= 80.0 for s in mature), spec  # and hits the >= 80 scan
            worst = max(abs(got[s] - want[s]) for s in mature)
            assert worst < 1e-6, (spec, worst)
    finally:
        c.close()
