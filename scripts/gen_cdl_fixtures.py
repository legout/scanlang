"""Fixture builder: deterministic multi-scenario OHLC frame + the live-talib pin table.

Run from the repo root (``uv run python scripts/gen_cdl_fixtures.py``):
generates the frame the CDL parity tests embed — 5 seeded random walks
(+ one walk mirrored into AAA/BBB/CCC with per-symbol offsets so partition
bugs show) plus ~37 crafted/degenerate scenarios (canonical reversals,
doji family, flat, wide spike) — then rewrites tests/cdl_fixture_frame.json
and the per-pattern pin table tests/cdl_fixture_pins.json (first two
non-zero live-talib hits per pattern per symbol, registry patterns only).

``--classify`` re-derives the talib/duckdb-extension value-parity split over
this frame and checks it against ``indicators._CDL_PARITY`` (exit 1 on
drift). Run it when bumping talib or the duckdb talib extension; if the
safe set changes, update _CDL_PARITY and the DIVERGENCE_EXCLUDED list in
tests/test_cdl_patterns.py, then regenerate the pins.

Not imported by the suite — the test file carries the generated literals.
"""
import sys

import numpy as np

N = 150
WALK_SEEDS = (11, 777, 2026, 31, 4242)

# crafted scenarios: (open, high, low, close) tuples; degenerate ones (FLAT,
# SPIKE) included because the extension/talib flip on exactly those shapes
DOWN3 = [(100, 100.4, 99.6, 100.2), (99.5, 99.6, 99.0, 99.05), (99.0, 99.1, 98.5, 98.55),
         (98.0, 98.4, 97.6, 97.7), (97.0, 97.4, 96.6, 96.7)]
UP3 = [(100.0, 100.4, 99.6, 100.3), (100.2, 100.5, 99.8, 100.4), (100.3, 100.6, 99.9, 100.5)]
CTX = [(100, 100.4, 99.6, 100.2)] * 5

SCENARIOS = {
    "PIERCE": DOWN3 + [(96.0, 96.3, 95.7, 95.75), (95.2, 96.1, 95.1, 96.0)],
    "ENGULF": DOWN3 + [(97.2, 97.3, 96.6, 96.65), (96.5, 97.5, 96.4, 97.4)],
    "HAM2": DOWN3 + [(96.3, 96.4, 95.4, 96.35)],
    "HARAMIX": DOWN3 + [(97.8, 97.9, 97.0, 97.05), (97.4, 97.5, 97.2, 97.45)],
    "HARAMICROSS": DOWN3 + [(97.8, 97.9, 97.0, 97.05), (97.5, 97.55, 97.45, 97.5)],
    "HPIGEON": DOWN3 + [(97.8, 97.9, 97.0, 97.05), (97.5, 97.55, 97.3, 97.4)],
    "COUNTER": DOWN3 + [(96.6, 96.7, 96.0, 96.1), (96.0, 96.15, 95.9, 96.1)],
    "GDOJI2": DOWN3 + [(96.4, 96.5, 95.8, 95.85), (95.5, 95.5, 94.5, 95.5)],
    "DDOJI2": DOWN3 + [(96.4, 96.5, 95.8, 95.85), (95.5, 95.6, 94.6, 95.5)],
    "TAKURI2": DOWN3 + [(96.4, 96.5, 95.8, 95.85), (95.5, 95.55, 94.3, 95.52)],
    "3SOLDIERS": DOWN3 + [(96.3, 96.95, 96.2, 96.9), (96.85, 97.5, 96.75, 97.45), (97.4, 98.05, 97.3, 98.0)],
    "KICK2": DOWN3 + [(97.9, 97.9, 97.0, 97.0), (98.2, 99.1, 98.2, 99.1)],
    "KICKLEN": DOWN3 + [(97.9, 97.9, 97.0, 97.0), (98.2, 99.1, 98.2, 99.1)],
    "SEPLINE": DOWN3[:3] + [(96.6, 96.7, 96.0, 96.1), (96.6, 96.7, 96.0, 96.1), (96.1, 96.75, 96.1, 96.7)],
    "ONNECK": DOWN3 + [(96.6, 96.7, 96.0, 96.1), (96.0, 96.3, 95.9, 96.1)],
    "INNECK": DOWN3 + [(96.6, 96.7, 96.0, 96.1), (96.0, 96.3, 95.9, 96.08)],
    "THRUST": DOWN3 + [(96.6, 96.7, 96.0, 96.1), (96.0, 96.4, 95.9, 96.3)],
    "TRISTAR": CTX + [(100, 100.2, 99.8, 100.0), (100.6, 100.8, 100.4, 100.6), (100, 100.2, 99.8, 100.0)],
    "DOJISTAR": DOWN3 + [(95.7, 95.75, 95.65, 95.7), (95.7, 96.4, 95.6, 96.3)],
    "BREAKAWAY": DOWN3[:2] + [(98.0, 98.4, 97.6, 97.0), (97.0, 97.4, 96.6, 95.7),
                              (95.2, 95.5, 94.8, 95.4), (95.3, 95.6, 94.9, 95.5), (95.4, 96.6, 95.3, 96.4)],
    "LADDERB": DOWN3[:3] + [(98.3, 98.4, 97.6, 97.7), (97.6, 97.7, 96.8, 96.9), (96.8, 96.9, 96.0, 96.1),
                            (95.7, 95.8, 95.2, 95.4), (95.5, 96.3, 95.4, 96.2)],
    "HIKKAKE": DOWN3 + [(97.8, 97.9, 97.0, 97.05), (97.4, 97.5, 97.3, 97.45), (97.3, 98.0, 97.2, 97.9)],
    "UNIQUE3R": DOWN3 + [(96.6, 96.7, 96.0, 96.1), (95.7, 95.75, 95.4, 95.5), (95.3, 96.4, 95.2, 96.3)],
    "CONCEAL": UP3 + [(99.9, 100.3, 99.4, 100.2), (99.4, 99.8, 99.0, 99.7), (99.0, 99.4, 98.6, 99.3),
                      (98.6, 99.0, 98.1, 98.9), (97.9, 98.05, 97.0, 98.0)],
    "RISEFALL": DOWN3[:2] + [(97.2, 97.75, 97.1, 97.7), (97.65, 98.2, 97.55, 98.15), (98.1, 98.65, 98.0, 98.6),
                             (98.55, 99.1, 98.45, 99.05), (99.0, 99.55, 98.9, 99.5)],
    "UPSIDE2C": DOWN3 + [(96.6, 97.5, 96.5, 97.45), (97.9, 98.3, 97.8, 98.2), (98.15, 98.25, 97.5, 97.55)],
    "XSIDE3": DOWN3 + [(96.6, 97.5, 96.5, 97.45), (97.9, 98.3, 97.8, 98.2), (98.25, 98.35, 97.4, 97.5),
                       (97.45, 98.3, 97.4, 98.25)],
    "3INSIDE": DOWN3 + [(97.8, 97.9, 97.0, 97.05), (97.4, 97.5, 97.2, 97.45), (97.3, 98.2, 97.2, 98.1)],
    "3OUTSIDE": DOWN3 + [(97.2, 97.3, 96.6, 96.65), (96.5, 97.5, 96.4, 97.4), (97.3, 98.1, 97.2, 98.0)],
    "3STARS": DOWN3 + [(96.6, 96.7, 96.0, 96.1), (96.0, 96.05, 95.6, 96.05), (95.5, 95.55, 95.1, 95.55),
                       (95.1, 96.0, 95.0, 95.9)],
    "3LINESTRIKE": DOWN3[:2] + [(97.2, 97.75, 97.1, 97.7), (97.65, 98.2, 97.55, 98.15), (98.1, 98.65, 98.0, 98.6),
                                (98.5, 98.55, 96.8, 96.9)],
    "IDENT3C": UP3 + [(100.4, 100.9, 100.3, 100.4), (100.0, 100.1, 99.2, 99.3), (99.6, 99.7, 98.8, 98.9),
                      (99.2, 99.3, 98.4, 98.5)],
    "3CROWS": UP3 + [(100.4, 100.9, 100.3, 100.4), (99.9, 100.0, 99.1, 99.2), (99.5, 99.6, 98.7, 98.8),
                     (99.1, 99.2, 98.3, 98.4)],
    "STALLED": UP3 + [(100.4, 100.9, 100.3, 100.8), (100.7, 101.2, 100.6, 101.1), (101.0, 101.4, 100.9, 101.3),
                      (101.25, 101.35, 101.05, 101.35)],
    "ADVANCE": UP3 + [(100.4, 100.9, 100.3, 100.8), (100.7, 101.4, 100.6, 101.3), (101.2, 101.7, 101.1, 101.6),
                      (101.5, 101.9, 101.2, 101.85)],
    "MATCHLOW": DOWN3 + [(96.6, 96.7, 96.0, 96.1), (96.3, 96.4, 95.7, 96.1)],
    "STICK": DOWN3 + [(96.6, 96.7, 96.0, 96.1), (96.4, 97.3, 96.3, 97.2), (96.1, 96.2, 95.5, 96.1)],
    "TASUKI": UP3 + [(101.2, 101.6, 101.1, 101.5), (101.9, 102.2, 101.8, 102.1), (101.6, 101.7, 101.1, 101.15)],
    "FLAT": [(100, 100.2, 99.8, 100.0)] * 6,
    "SPIKE": [(100, 130, 70, 100), (100, 100.4, 99.6, 100.2), (90, 110, 85, 95),
              (100.1, 100.2, 100.0, 100.05)],
    # second-generation crafted bars (bigger bodies/shadows): these exposed the
    # extension divergences for sticksandwich/matchinglow/doji-family that the
    # subtler scenarios above miss — keep both generations
    "XD3": [(100, 100.5, 95, 95.5), (97, 97.5, 92, 92.5), (94, 94.5, 89, 89.5)],
    "XU3": [(90, 95, 89.5, 94.5), (95, 100, 94.5, 99.5), (100, 105, 99.5, 104.5)],
    "XSTICK": [(100, 100.5, 95, 95.5), (97, 97.5, 92, 92.5), (94, 94.5, 89, 89.5),
               (92, 92.5, 87, 87.0), (88, 97, 87.9, 96.8), (87.1, 88, 86.9, 87.0)],
    "XMATCHLOW": [(100, 100.5, 95, 95.5), (97, 97.5, 92, 92.5), (94, 94.5, 89, 89.5),
                  (92, 92.5, 87, 87.0), (91.5, 92, 86.8, 87.0)],
    "XDOJI": [(100, 104, 96, 100.05), (100, 103.8, 96.2, 99.95)],
    "XGDOJI": [(100, 104, 100, 100)],
    "XDDOJI": [(100, 100, 96, 100)],
    "XMARU": [(96, 104, 96, 104)],
    "XCMARU": [(100, 100.5, 95, 95.5), (97, 97.5, 92, 92.5), (94, 94.5, 89, 89.5),
               (105, 105.5, 96, 96)],
    "XLLINE": [(100, 100.5, 95, 95.5), (97, 97.5, 92, 92.5), (94, 94.5, 89, 89.5),
               (90, 100.5, 89.5, 100)],
    "XSLINE": [(100, 100.6, 99.4, 100.1), (100, 100.7, 99.3, 100.2)],
    "XSPTOP": [(100, 104, 96, 100.2), (100, 104, 96, 99.8)],
    "XHWAVE": [(100, 104, 96, 100.1), (100, 103.5, 96.5, 99.9)],
    "XHARAMI": [(100, 100.5, 95, 95.5), (97, 97.5, 92, 92.5), (94, 94.5, 89, 89.5),
                (105, 105.5, 94.5, 95), (96.2, 98.2, 95.8, 97.8)],
    "XHIKKAMOD": [(100, 100.5, 95, 95.5), (97, 97.5, 92, 92.5), (94, 94.5, 89, 89.5),
                  (105, 105.5, 94.5, 95), (99, 99.5, 98.5, 99.0)],
    "XSEPLINE": [(90, 95, 89.5, 94.5), (95, 100, 94.5, 99.5), (100, 105, 99.5, 104.5),
                 (105, 105.3, 99.8, 100.5), (100.5, 105.2, 100.2, 104.8)],
    "XONNECK": [(100, 100.5, 95, 95.5), (97, 97.5, 92, 92.5), (94, 94.5, 89, 89.5),
                (89.5, 94.8, 89.3, 94.5), (89.6, 90, 85.5, 86)],
    "XKICK": [(100, 100.2, 95, 95.1), (94.9, 104, 94.8, 103.9)],
    "XHAM": [(100, 100.5, 95, 95.5), (97, 97.5, 92, 92.5), (94, 94.5, 89, 89.5),
             (90, 90.5, 82, 90.3)],
    "XIHAM": [(100, 100.5, 95, 95.5), (97, 97.5, 92, 92.5), (94, 94.5, 89, 89.5),
              (90, 98, 89.8, 90.3)],
    "XHANG": [(90, 95, 89.5, 94.5), (95, 100, 94.5, 99.5), (100, 105, 99.5, 104.5),
              (105, 105.5, 97, 105.3)],
    "XPIERCE": [(100, 100.5, 95, 95.5), (97, 97.5, 92, 92.5), (94, 94.5, 89, 89.5),
                (88, 98, 87.9, 97)],
    "XENGULF": [(100, 100.5, 95, 95.5), (97, 97.5, 92, 92.5), (94, 94.5, 89, 89.5),
                (88.8, 97, 88.7, 96.5)],
    "XTAKURI": [(100, 100.5, 95, 95.5), (97, 97.5, 92, 92.5), (94, 94.5, 89, 89.5),
                (90, 90.3, 80, 90.2)],
    "XRICKSHAW": [(100, 104, 96, 100.05), (100, 103.8, 96.2, 99.95)],
    "XSOLDIERS": [(100, 100.5, 95, 95.5), (97, 97.5, 92, 92.5), (94, 94.5, 89, 89.5),
                  (89.2, 94.3, 89, 94.2), (94.3, 99.3, 94.1, 99.2), (99.3, 104.3, 99.1, 104.2)],
}


def _walk(seed: int):
    rng = np.random.default_rng(seed)
    close = 100 + np.cumsum(rng.normal(0, 2, N))
    o = np.round(close - rng.uniform(0.5, 1.5, N), 2)
    h = np.round(np.maximum(o, close) + rng.uniform(0.2, 2.0, N), 2)
    l = np.round(np.minimum(o, close) - rng.uniform(0.2, 2.0, N), 2)
    c = np.round(close, 2)
    return list(zip(o, h, l, c))


def build_frame():
    """(symbol, bar, open, high, low, close) rows for the whole fixture."""
    rows = []
    walk0 = _walk(WALK_SEEDS[0])
    for sym, off in (("AAA", 0.0), ("BBB", 50.0), ("CCC", 100.0)):
        rows.append((sym, [(o + off, h + off, l + off, c + off) for o, h, l, c in walk0 + CTX]))
    for seed in WALK_SEEDS:
        rows.append((f"W{seed}", _walk(seed)))
    for name, bars in SCENARIOS.items():
        rows.append((f"S_{name}", [(float(o), float(h), float(l), float(c)) for o, h, l, c in bars]))
    out = []
    for sym, bars in rows:
        for i, (o, h, l, c) in enumerate(bars):
            out.append((sym, i, o, h, l, c))
    return out


def main():
    import json
    from pathlib import Path

    import polars as pl
    import talib

    from scanlang.indicators import _CDL_PARITY

    rows = build_frame()
    Path("tests/cdl_fixture_frame.json").write_text(json.dumps(rows))
    f = pl.DataFrame(rows, schema={"symbol": pl.String, "bar": pl.Int64, "open": pl.Float64,
                                   "high": pl.Float64, "low": pl.Float64, "close": pl.Float64},
                     orient="row")

    pins = {}
    for sym in sorted(set(f["symbol"].to_list())):
        sub = f.filter(pl.col("symbol") == sym).sort("bar")
        o, h, l, c = (sub[k].to_numpy() for k in ("open", "high", "low", "close"))
        for name in _CDL_PARITY:
            r = getattr(talib, name.upper())(o, h, l, c)
            nz = [(i, int(v)) for i, v in enumerate(r) if v][:2]
            if nz:
                pins.setdefault(name, []).append([sym, *nz[0]])
    Path("tests/cdl_fixture_pins.json").write_text(json.dumps(pins, indent=1, sort_keys=True))
    print("frame rows:", len(rows), "symbols:", f["symbol"].n_unique(),
          "patterns with hits:", len(pins))

    if "--classify" in sys.argv:
        import duckdb

        f.write_parquet("/tmp/cdl_classify.parquet")
        con = duckdb.connect()
        con.execute("LOAD talib")
        con.execute("CREATE VIEW cb AS SELECT * FROM '/tmp/cdl_classify.parquet'")
        ext_fns = {
            r[0] for r in con.execute(
                "SELECT DISTINCT function_name FROM duckdb_functions() WHERE function_name LIKE 't_%'"
            ).fetchall()
        }
        all_cdl = sorted(n for n in dir(talib) if n.startswith("CDL") and n.isupper())
        safe = []
        for name in all_cdl:
            low = name.lower()
            if f"t_{low}" not in ext_fns:
                continue  # penetration-parameter patterns the extension lacks
            vmap = dict(con.execute(
                f"SELECT symbol, t_{low}(list(open ORDER BY bar), list(high ORDER BY bar), "
                "list(low ORDER BY bar), list(close ORDER BY bar)) FROM cb GROUP BY symbol"
            ).fetchall())
            ok = True
            for sym, vlist in vmap.items():
                sub = f.filter(pl.col("symbol") == sym).sort("bar")
                o, h, l, c = (sub[k].to_numpy() for k in ("open", "high", "low", "close"))
                t = getattr(talib, name)(o, h, l, c)
                if any(v is not None and v != int(tv) for v, tv in zip(vlist, t)):
                    ok = False
                    break
            if ok:
                safe.append(low)
        print("SAFE:", len(safe), safe)
        ext_registered = {n.lower() for n in all_cdl if f"t_{n.lower()}" in ext_fns}
        print("UNSAFE (divergent, extension-registered):", len(ext_registered - set(safe)),
              sorted(ext_registered - set(safe)))
        print("extension-lacks (penetration):",
              sorted({n.lower() for n in all_cdl} - ext_registered))
        reg = sorted(n for n in (*_CDL_PARITY,) if n != "cdlengulfing")
        drift = set(reg) - set(safe)
        if drift:
            print("DRIFT: registered-but-divergent:", sorted(drift))
            sys.exit(1)
        print("registry subset of SAFE: OK (missing from registry:", sorted(set(safe) - set(reg)), ")")


if __name__ == "__main__":
    main()
