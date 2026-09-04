"""New C3 indicators: hand-computed polars values + engine-aware validate.

adr = sma(TR/close*100); roc = close/shift(n)-1 in %; natr = atr/close*100;
slope = rolling OLS slope of close against window position. talib-only
names (macd, adx, ...) validate only with engine="duckdb".
"""

import polars as pl
import pytest

from scanlang.compiler import PROPERTY_CATALOG, compile, validate
from scanlang.indicators import INDICATORS

OHLC_CATALOG = {
    **PROPERTY_CATALOG,
    "open": {"label": "Open", "dtype": "float"},
    "high": {"label": "High", "dtype": "float"},
    "low": {"label": "Low", "dtype": "float"},
}


def _frame() -> pl.DataFrame:
    """4 bars per symbol: pc=None at bar 0 makes TR/atr warm-up hand-checkable."""
    return pl.DataFrame(
        {
            "symbol": ["A"] * 4 + ["B"] * 4,
            "session": [f"2026-01-0{i}" for i in range(1, 5)] * 2,
            "open": [10.0] * 8,
            "high": [11.0, 12.0, 13.0, 14.0, 10.0, 10.0, 10.0, 10.0],
            "low": [9.0, 10.0, 11.0, 12.0, 8.0, 8.0, 8.0, 8.0],
            "close": [10.0, 11.0, 12.0, 13.0, 9.0, 9.0, 9.0, 9.0],
            "volume": [1.0] * 8,
        }
    )


# --- hand-computed polars values ---------------------------------------------


def test_adr_hand_computed():
    # TR = max(h-l, |h-pc|, |pc-l|); bar0 has no pc -> TR = h-l (nulls skipped).
    # A: TR 2,2,2,2 over close 10,11,12,13 -> TR/c*100 = 20, 200/11, 50/3, 200/13;
    # sma(2) pairs the last two emitted windows. B flat: 2/9*100.
    got = (
        _frame()
        .with_columns(INDICATORS["adr"][1](pl.col("close"), 2, "symbol").alias("v"))
        .filter(pl.col("session") >= "2026-01-03")
        ["v"]
        .to_list()
    )
    assert got == pytest.approx([(200 / 11 + 50 / 3) / 2, (50 / 3 + 200 / 13) / 2, 200 / 9, 200 / 9])


def test_roc_hand_computed():
    # roc(1): (close/prev - 1)*100; bar 0 shift -> null
    got = (
        _frame()
        .with_columns(INDICATORS["roc"][1](pl.col("close"), 1, "symbol").alias("v"))
        .filter(pl.col("session") >= "2026-01-02")
        ["v"]
        .to_list()
    )
    assert got == pytest.approx([10.0, 100 / 11, 100 / 12, 0.0, 0.0, 0.0])


def test_natr_hand_computed():
    # A: TR 2.0 constant, atr(2) Wilder seeded sma -> 2.0 from day 2;
    # natr = atr/close*100 -> 2/12*100, 2/13*100. B: flat, same atr.
    got = (
        _frame()
        .with_columns(INDICATORS["natr"][1](pl.col("close"), 2, "symbol").alias("v"))
        .filter(pl.col("session") >= "2026-01-03")
        ["v"]
        .to_list()
    )
    assert got == pytest.approx([2 / 12 * 100, 2 / 13 * 100, 2 / 9 * 100, 2 / 9 * 100])


def test_slope_hand_computed():
    # A closes 10,11,12,13: slope over any window is exactly 1.0 (linear +1);
    # B flat -> 0.0. Window 3 emits from bar 2.
    got = (
        _frame()
        .with_columns(INDICATORS["slope"][1](pl.col("close"), 3, "symbol").alias("v"))
        .filter(pl.col("session") >= "2026-01-03")
        ["v"]
        .to_list()
    )
    assert got == pytest.approx([1.0, 1.0, 0.0, 0.0])


def test_slope_matches_numpy_lstsq():
    """Independent check on a non-linear path: rolling OLS == per-window lstsq."""
    rng = pl.Series("c", [5.0, 7.0, 6.0, 9.0, 8.0, 12.0, 11.0, 15.0])
    df = pl.DataFrame({"symbol": ["A"] * 8, "close": rng})
    got = df.with_columns(INDICATORS["slope"][1](pl.col("close"), 4, "symbol").alias("v"))["v"].to_list()
    xs = [0.0, 1.0, 2.0, 3.0]
    for i in range(3, 8):
        ys = rng[i - 3 : i + 1].to_list()
        xbar, ybar = sum(xs) / 4, sum(ys) / 4
        want = sum((x - xbar) * (y - ybar) for x, y in zip(xs, ys, strict=True)) / sum(
            (x - xbar) ** 2 for x in xs
        )
        assert got[i] == pytest.approx(want)


# --- engine-aware validate ----------------------------------------------------


def _adx_scan():
    return {"filters": [{"property": {"fn": "adx", "args": [14]},
                         "op": ">", "value": 20}],
            }


def _aroon_scan():
    return {"filters": [{"property": {"fn": "aroon", "args": [14]},
                         "op": ">", "value": 50}],
            }


def test_sql_only_indicator_rejected_on_polars():
    """stoch_k is the SQL-only example; cdlengulfing/aroon/macd are dual-engine now."""
    errs = validate({"filters": [{"property": {"fn": "stoch_k", "args": [5, 3, 3]},
                                  "op": ">", "value": 0}]}, catalog=OHLC_CATALOG)
    assert "filters[0].property: indicator 'stoch_k' requires engine='duckdb'" in errs
    assert "filters[0].property: indicator 'stoch_k' requires column 'high'" not in errs
    # cdlengulfing is dual-engine since the candlestick-parity card (its
    # catalog shape — arg_spec/required_cols — is unchanged); the polars
    # engine rejects it only on missing catalog cols now
    eng = validate({"filters": [{"property": {"fn": "cdlengulfing", "args": [14]},
                                 "op": ">", "value": 0}]})
    # all three required-col errors, in required_cols order (no engine error)
    assert eng == [
        f"filters[0].property: indicator 'cdlengulfing' requires column {c!r}"
        for c in ("open", "high", "low")
    ]
    # aroon is dual-engine (INDICATORS parity builder): validates on polars
    # when the catalog carries its required cols
    assert validate(_aroon_scan(), catalog=OHLC_CATALOG) == []


def test_sql_only_indicator_ok_on_duckdb():
    assert validate(_aroon_scan(), catalog=OHLC_CATALOG, engine="duckdb") == []
    assert validate({"filters": [
        {"property": {"fn": "macd", "args": [12]}, "op": ">", "value": 0},
        {"property": {"fn": "bbands_upper", "args": [20]}, "op": ">", "value": {"col": "close"}},
        {"property": {"fn": "bbands_lower", "args": [20]}, "op": "<", "value": {"col": "close"}},
        {"property": {"fn": "aroon", "args": [25]}, "op": ">", "value": 50},
        {"property": {"fn": "cdlengulfing", "args": [14]}, "op": "==", "value": 1},
        {"property": {"fn": "ht_trendline", "args": [14]}, "op": ">", "value": {"col": "close"}},
    ]}, catalog=OHLC_CATALOG, engine="duckdb") == []


def test_sql_only_never_compiles_on_polars():
    with pytest.raises(ValueError, match="requires engine='duckdb'"):
        compile({"filters": [{"property": {"fn": "stoch_k", "args": [5, 3, 3]},
                              "op": ">", "value": 0}]}, catalog=OHLC_CATALOG)


def test_adx_dual_engine_validate():
    """adx is the parity slice: validates on BOTH engines (INDICATORS builder)."""
    errs = validate(_adx_scan(), catalog=OHLC_CATALOG, engine="polars")
    assert errs == []
    assert validate(_adx_scan(), catalog=OHLC_CATALOG, engine="duckdb") == []
    # missing high/low/close still surfaces on either engine (required_cols)
    bad = {"filters": [{"property": {"fn": "adx", "args": [14]}, "op": ">", "value": 20}]}
    assert "indicator 'adx' requires column 'high'" in validate(bad)[0]


def test_engine_kwarg_default_unchanged():
    # corpus names keep validating on both engines; unknown stays unknown
    assert validate({"filters": [{"property": {"fn": "sma", "args": [{"col": "close"}, 20]},
                                  "op": ">", "value": 0}]}, engine="duckdb") == []
    assert validate({"filters": [{"property": {"fn": "nosuch", "args": []},
                                  "op": ">", "value": 0}]}) == [
        "filters[0].property.fn: unknown indicator: 'nosuch'"
    ]
