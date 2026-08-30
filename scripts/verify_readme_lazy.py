"""Execute the README 'Any LazyFrame' snippet and print its exact output."""

import datetime as dt

import polars as pl

from scanlang import apply, catalog_from_schema

T0 = dt.date(2026, 1, 1)
n = 60
sessions = [T0 + dt.timedelta(days=i) for i in range(n)]
closes = [10.0 + i for i in range(n)] + [60.0 - i for i in range(n)]
bars = pl.DataFrame({
    "symbol": ["AAA"] * n + ["BBB"] * n,
    "session": sessions * 2,
    "open": [c - 0.5 for c in closes],
    "high": [c + 1.0 for c in closes],
    "low": [c - 1.0 for c in closes],
    "close": closes,
    "volume": [1000.0] * (2 * n),
})

lf = bars.rename({"symbol": "ticker"}).lazy()
cat = catalog_from_schema(lf)
rsi_hot = {"filters": [{
    "property": {"fn": "rsi", "args": [{"col": "close"}, 14]},
    "op": ">", "value": 70,
}]}
out = apply(lf, rsi_hot, catalog=cat, partition="ticker")
print("catalog sample:", {k: cat[k] for k in ("ticker", "close")})
print(out.collect().select("ticker", "session", "close").head(3))
print("rows:", out.select(pl.len()).collect().item())
