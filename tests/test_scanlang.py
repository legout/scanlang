"""Freeze-contract checks: groups, operand exprs, indicators, lazy scoring, stats."""

import datetime as dt

import polars as pl
import pytest

from scanlang import (
    PROPERTY_CATALOG,
    apply,
    backtest_summary,
    catalog_from_schema,
    compile,
    forward_stats,
    score_bars,
    validate,
)

T0 = dt.date(2026, 1, 1)


def _bars() -> pl.DataFrame:
    n = 60
    sessions = [T0 + dt.timedelta(days=i) for i in range(n)]
    def rows(sym, closes):
        return {
            "symbol": [sym] * n,
            "session": sessions,
            "open": [c - 0.5 for c in closes],
            "high": [c + 1.0 for c in closes],
            "low": [c - 1.0 for c in closes],
            "close": closes,
            "volume": [1000.0] * n,
        }
    a = [10.0 + i for i in range(n)]  # steady uptrend
    b = [50.0] * n  # flat
    return pl.DataFrame(rows("AAA", a)).vstack(pl.DataFrame(rows("BBB", b)))


def test_score_bars_lazy_output_matches_catalog():
    out = score_bars(_bars().lazy()).collect()
    assert set(out.columns) == set(PROPERTY_CATALOG)
    assert set(out["symbol"]) == {"AAA", "BBB"}  # one row per symbol
    assert out["score"].dtype == pl.Int16


def test_flat_def_backcompat_and_apply():
    scored = score_bars(_bars()).collect()  # eager in works too
    out = apply(
        scored,
        {"filters": [{"property": "score", "op": ">=", "value": 0}],
         "order_by": [{"property": "score", "dir": "desc"}], "limit": 1},
    )
    assert len(out) == 1 and out["score"][0] == scored["score"].max()


def test_groups_any_not():
    scored = score_bars(_bars()).collect()
    d = {"filters": [
        {"any": [
            {"property": "symbol", "op": "==", "value": "AAA"},
            {"property": "symbol", "op": "in", "value": ["ZZZ"]},
        ]},
        {"not": {"property": "symbol", "op": "==", "value": "BBB"}},
    ]}
    assert validate(d) == []
    assert set(apply(scored, d)["symbol"]) == {"AAA"}


def test_computed_operand_cross_and_arithmetic():
    bars = _bars().lazy()
    d = {"filters": [{
        "property": {"fn": "ema", "args": [{"col": "close"}, 5]},
        "op": "cross_above",
        "value": {"fn": "ema", "args": [{"col": "close"}, 20]},
    }]}
    out = apply(bars, d, catalog=catalog_from_schema(bars)).collect()
    # linear uptrend crosses exactly once (bar 1); flat series never
    assert out["symbol"].to_list() == ["AAA"]
    assert out["session"].to_list() == [T0 + dt.timedelta(days=1)]

    d2 = {"filters": [{
        "property": {"*": [{"col": "close"}, 2]},
        "op": ">",
        "value": {"+": [{"col": "close"}, 50]},
    }]}
    out2 = apply(bars, d2, catalog=catalog_from_schema(bars)).collect()
    assert set(out2["symbol"]) == {"AAA"}  # close*2 > close+50 iff close > 50


def test_custom_partition_and_rsi():
    bars = _bars().rename({"symbol": "sym"})
    d = {"filters": [
        {"property": {"fn": "rsi", "args": [{"col": "close"}, 14]}, "op": ">", "value": 0},
    ]}
    assert validate(d, catalog=catalog_from_schema(bars)) == []
    out = apply(bars.lazy(), d, catalog=catalog_from_schema(bars), partition="sym").collect()
    assert len(out) == 120  # rsi fill_null(50) keeps every bar non-null


def test_validate_total_for_literals():
    assert validate({"filters": [{"property": "nope", "op": ">=", "value": 1}]}) == [
        "filters[0]: unknown property: 'nope'"
    ]
    assert validate({"filters": [{"property": "score", "op": ">>", "value": 1}]}) == [
        "filters[0]: unknown operator: '>>'"
    ]
    assert "must be int" in validate({"filters": [{"property": "score", "op": ">=", "value": 1.5}]})[0]
    assert validate({"filters": [{"property": "score", "op": ">=", "value": True}]}) != []
    assert "nonempty list" in validate({"filters": [{"property": "phase", "op": "in", "value": []}]})[0]
    assert validate({"filters": [{"all": []}]}) == ["filters[0].all must be a nonempty list"]
    assert validate({"filters": [{"not": 3}]}) == ["filters[0].not must be an object"]
    assert validate({"limit": True}) == ["limit must be a nonnegative integer"]
    with pytest.raises(ValueError):
        compile({"filters": [{"property": "nope", "op": ">=", "value": 1}]})


def test_validate_structural_for_computed():
    errs = validate({"filters": [{
        "property": {"fn": "ema", "args": [{"col": "close"}, "x"]}, "op": ">", "value": 1,
    }]})
    assert errs == ["filters[0].property.args[1]: must be an int >= 1, got 'x'"]
    assert validate({"filters": [{
        "property": {"fn": "nope", "args": []}, "op": ">", "value": 1,
    }]}) == ["filters[0].property.fn: unknown indicator: 'nope'"]
    # atr requires high/low/close columns in the catalog
    small = {"open": {"label": "open", "dtype": "float"}, "close": {"label": "close", "dtype": "float"}}
    assert validate({"filters": [{"property": {"fn": "atr", "args": [14]}, "op": ">", "value": 0}]},
                    catalog=small) == [
        "filters[0].property: indicator 'atr' requires column 'high'",
        "filters[0].property: indicator 'atr' requires column 'low'",
    ]


def test_catalog_from_schema_skips_unmapped():
    bars = _bars().with_columns(tags=pl.Series([["a"]] * 120, dtype=pl.List(pl.String)))
    cat = catalog_from_schema(bars)
    assert cat["close"]["dtype"] == "float"
    assert cat["symbol"]["dtype"] == "str"
    assert "tags" not in cat


def test_stats():
    sessions = [T0 + dt.timedelta(days=i) for i in range(30)]
    closes = [100.0 + i for i in range(30)]
    st = forward_stats(sessions, closes, sessions[0])
    assert st == pytest.approx({"5d": 5.0, "10d": 10.0, "20d": 20.0}, rel=1e-6)
    assert forward_stats(sessions, closes, sessions[25]) is None
    runs = [{"ran_at": "2026-01-01T22:00", "symbols": ["AAA"]}]
    summary = backtest_summary(runs, lambda sym, ran_on: st)
    assert summary["included"] == 1 and summary["total"] == 1
    label, hit, avg, n = summary["horizons"][0]
    assert (label, n, hit) == ("5d", 1, 100.0) and avg == pytest.approx(5.0)
