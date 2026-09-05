"""Scan session API: bound catalog, text-first apply, materialized indicators."""

import datetime as dt

import polars as pl
import pytest

from scanlang import Scan, materialize, parse
from scanlang.compiler import PROPERTY_CATALOG

TEXT = (
    "close>4 AND sma(50,volume)>200000 AND volume*close>2000000 "
    "AND sma(20)>sma(50) AND close>sma(50) AND open/close(1)>1.03 "
    "AND market_cap>500000000 AND market_cap<100000000000"
)


def _bars(n=120):
    t0 = dt.date(2026, 1, 1)
    sessions = [t0 + dt.timedelta(days=i) for i in range(n)]
    closes = [400.0 * 1.002**i for i in range(n)]
    opens = [closes[0]] + [c * 1.04 for c in closes[:-1]]
    return pl.DataFrame(
        {
            "symbol": ["AAA"] * n,
            "session": sessions,
            "open": opens,
            "high": [c * 1.03 for c in closes],
            "low": [c * 0.99 for c in closes],
            "close": closes,
            "volume": [3e6] * n,
        }
    ).with_columns(market_cap=pl.col("close") * pl.col("volume"))


def test_scan_binds_catalog_from_frame():
    sl = Scan(_bars())
    assert "market_cap" in sl.catalog
    assert "open" in sl.catalog and "volume" in sl.catalog
    assert sl.result is None


def test_scan_apply_text_uses_bound_catalog():
    sl = Scan(_bars())
    out = sl.apply(TEXT)
    assert isinstance(out, pl.DataFrame)
    assert out.height > 0
    assert sl.result is out


def test_scan_apply_dict_equivalent():
    sl = Scan(_bars())
    d = parse(TEXT, catalog=sl.catalog)
    assert sl.apply(dict(d)).height == sl.apply(TEXT).height  # type: ignore[union-attr]


def test_scan_result_lazy_preserved():
    sl = Scan(_bars().lazy())
    assert isinstance(sl.apply(TEXT), pl.LazyFrame)


def test_materialized_columns_and_values():
    bars = _bars()
    sl = Scan(bars)
    sl.apply(TEXT)
    m = sl.materialized
    assert {"sma_50", "sma_20"} <= set(m.columns)
    assert "market_cap" in m.columns
    # sma_50 tracks mean of volume (corpus order sma(50,volume))
    win = bars["volume"].tail(50)
    assert m["sma_50"][-1] == pytest.approx(win.mean())  # type: ignore[index]
    assert m["sma_20"][-1] == pytest.approx(bars["close"].tail(20).mean())  # type: ignore[index]


def test_standalone_materialize():
    bars = _bars()
    cat = Scan(bars).catalog
    out = materialize(bars, TEXT, catalog=cat)
    assert {"sma_50", "sma_20"} <= set(out.columns)
    assert out.height == bars.height


def test_materialized_requires_apply():
    sl = Scan(_bars())
    with pytest.raises(ValueError, match="apply"):
        _ = sl.materialized


def test_seam_indicator_raises_in_materialize():
    bars = _bars()
    sl = Scan(bars)
    # talib present -> the not-an-Expr seam guard; talib absent -> install hint
    with pytest.raises(ValueError, match="talib seam|talib' extra"):
        _ = materialize(bars, "adx(14) > 20", catalog=sl.catalog)


def test_scored_catalog_merge_still_works():
    from scanlang import catalog_from_schema

    bars = _bars()
    cat = {**PROPERTY_CATALOG, **catalog_from_schema(bars)}
    sl = Scan(bars, catalog=cat)
    assert sl.apply("close > 4").height > 0
