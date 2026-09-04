"""Indicator registry: name -> (arg_spec, builder, required_cols).

Each entry:
- ``arg_spec``: tuple with one tag per positional arg — ``"expr"`` (any operand) or
  ``"int"`` (literal int >= 1).
- ``builder(*parsed, partition) -> pl.Expr``: polars-native; every window op uses
  ``.over(partition)``. Exception: ``adx`` is the TA-Lib parity slice — its
  builder returns a ``DataFrame -> DataFrame`` callable for
  ``group_by(partition, maintain_order=True).map_groups(...)`` (eager collect
  required, ``talib`` extra required), matching the duckdb ``t_adx`` values
  exactly, with null (NaN normalized) warm-up for the first ``2n-1`` bars.
- ``required_cols``: columns that must exist in the catalog (e.g. ``atr`` needs
  ``high, low, close``).

Extend by inserting entries; this shape is the contract. The ``talib``
extra's parity builder populates the same dict this way — it cannot
participate in lazy pushdown.

Seeding note (``ema``/``rsi``/``atr``): TA-Lib seeds its recursions with an SMA
of the first ``n`` values; polars ``ewm_mean(adjust=False)`` seeds from the
first value. The recursions match, so values converge after warm-up — on the
convergence test's series all three stay within 0.01 of the TA-Lib-style
reference from bar ~7.6n onward — but diverge in the early window. Accepted
by design, see the 2026-09-02 duckdb-backend plan (Q1).
"""

from __future__ import annotations

from collections.abc import Callable

import polars as pl

__all__ = ["INDICATORS"]


def _rsi(e: pl.Expr, n: int, partition: str) -> pl.Expr:
    delta = e.diff().over(partition)
    gain = delta.clip(lower_bound=0).ewm_mean(alpha=1 / n, adjust=False).over(partition)
    loss = (-delta.clip(upper_bound=0)).ewm_mean(alpha=1 / n, adjust=False).over(partition)
    # Zero-loss guard: avgLoss == 0 -> RSI 100 (math limit when avgGain > 0;
    # deliberate 100 for the flat 0/0 case — the raw formula yields NaN there,
    # and polars keeps NaN rows in `>` filters, so flat symbols would pass a
    # `rsi > 85` scan). Null cond (warm-up) takes the otherwise branch, so the
    # bar-0 null survives.
    return pl.when(loss == 0).then(100.0).otherwise(100 - 100 / (1 + gain / loss))


def _atr(n: int, partition: str) -> pl.Expr:
    pc = pl.col("close").shift(1).over(partition)
    tr = pl.max_horizontal(
        pl.col("high") - pl.col("low"),
        (pl.col("high") - pc).abs(),
        (pc - pl.col("low")).abs(),
    )
    return tr.ewm_mean(alpha=1 / n, adjust=False).over(partition)


def _tr(n: int, partition: str) -> pl.Expr:
    """Raw true range (null pc at bar 0 propagates — same TR the atr builder smooths)."""
    pc = pl.col("close").shift(1).over(partition)
    return pl.max_horizontal(
        pl.col("high") - pl.col("low"),
        (pl.col("high") - pc).abs(),
        (pc - pl.col("low")).abs(),
    )


def _slope(e, n: int, partition: str) -> pl.Expr:
    """Rolling OLS slope of ``e`` against its window position (0..n-1).

    Closed form via rolling sums (no rolling_corr in polars): with x the
    window position and y the value, slope = (Σxy − x̄·Σy) / Σ(x−x̄)². Σxy uses
    the row index offset trick: Σ(k·yₖ) over the window ending at the current
    row = Σ(row·y) − (row − (n−1))·Σ(y).
    """
    s_y = e.rolling_sum(n).over(partition)
    s_xy = (pl.int_range(0, pl.len()).cast(pl.Float64) * e).rolling_sum(n).over(partition) - (
        pl.int_range(0, pl.len()).cast(pl.Float64) - (n - 1)
    ).over(partition) * s_y
    s_xx = n * (n * n - 1) / 12
    return (s_xy - (n - 1) / 2 * s_y) / s_xx


# Temporal z-score normalization (IBD-style RS presentation — see
# docs/stock-screener-learnings.md item 2). RS ratings are already
# cross-sectional percentiles, so each series is z-scored against its OWN
# trailing history and re-centered at 100 (scale 5, clamped to [80, 120]).
# Warm-up is null until ``n`` smoothed values exist (a sub-window z would
# drift with series length); a zero-variance window pins 100.0. The eps
# branch must be an explicit ``when``: on a null cond (rolling_std warm-up)
# when/otherwise takes the otherwise branch, which divides nulls by null —
# null either way, but ordering the eps check first keeps the flat case
# exact. The clamp wraps ``100 + 5z`` directly; clip on null is null, which
# is the warm-up contract.
_RS_SPAN = 5  # EMA span smoothing the raw series (ratio leg)
_RS_MOM_LOOKBACK = 4  # ROC lookback feeding the momentum leg
_RS_MOM_SPAN = 3  # EMA span smoothing the momentum ROC
_RS_SCALE = 5.0  # ±2σ ≈ the conventional 90..110 RRG band
_RS_EPS = 1e-6  # zero-variance guard


def _zscore(e: pl.Expr, n: int, partition: str) -> pl.Expr:
    """Trailing population z-score of ``e``, re-centered at 100 and clamped.

    Null until ``n`` values exist (a short sub-window z would drift with
    series length — the count guard is what makes the warm-up contract match
    the SQL engine's); flat window -> 100.0. Chained ``.over(partition)``
    windows each evaluate against the partition's rows.
    """
    sd = e.rolling_std(n, ddof=0).over(partition)
    z = pl.when(sd < _RS_EPS).then(0.0).otherwise((e - e.rolling_mean(n).over(partition)) / sd)
    return pl.when(sd.is_not_null()).then((100.0 + _RS_SCALE * z).clip(80.0, 120.0)).otherwise(None)


def _rs_ratio(e: pl.Expr, n: int, partition: str) -> pl.Expr:
    """rs_ratio: EMA(_RS_SPAN) of the series, then a trailing ``n`` z-score."""
    return _zscore(e.ewm_mean(span=_RS_SPAN, adjust=False).over(partition), n, partition)


def _rs_momentum(e: pl.Expr, n: int, partition: str) -> pl.Expr:
    """rs_momentum: ROC(_RS_MOM_LOOKBACK) -> EMA(_RS_MOM_SPAN), then z-score.

    Feed it the normalized ratio — ``rs_momentum(rs_ratio(rs, 26), 13)`` — the
    reference pipeline differences the *normalized* series, not the raw one.
    """
    roc = e.diff(_RS_MOM_LOOKBACK).over(partition).ewm_mean(span=_RS_MOM_SPAN, adjust=False).over(partition)
    return _zscore(roc, n, partition)


def _ema(e: pl.Expr, n: int, partition: str) -> pl.Expr:
    return e.ewm_mean(span=n, adjust=False).over(partition)


def _wma(e: pl.Expr, n: int, partition: str) -> pl.Expr:
    """talib WMA (weights 1..n) via the same rolling-sum index trick as ``_slope``."""
    r = pl.int_range(0, pl.len()).cast(pl.Float64)
    s_y = e.rolling_sum(n).over(partition)
    s_ry = (r * e).rolling_sum(n).over(partition)
    return (s_ry - (r - n).over(partition) * s_y) / (n * (n + 1) / 2)


def _dema(e: pl.Expr, n: int, partition: str) -> pl.Expr:
    e1 = _ema(e, n, partition)
    return 2 * e1 - _ema(e1, n, partition)


def _tema(e: pl.Expr, n: int, partition: str) -> pl.Expr:
    e1 = _ema(e, n, partition)
    e2 = _ema(e1, n, partition)
    return 3 * e1 - 3 * e2 + _ema(e2, n, partition)


def _trima(e: pl.Expr, n: int, partition: str) -> pl.Expr:
    """talib TRIMA: SMA of SMA with the talib fold a=(n+1)//2, b=n-a+1.

    Even n: rolling_mean((n+1)//2) first would emit a null at bar (n+1)//2 - 1
    one row earlier than the b-first fold, shifting the whole chain by one —
    hence a on the *inner* window is what matches talib's SMA(n-a+1, n-a/2)
    order (probed exact for both parities at 1e-13).
    """
    a = (n + 1) // 2
    b = n - a + 1
    return e.rolling_mean(a).over(partition).rolling_mean(b).over(partition)


def _mom(e: pl.Expr, n: int, partition: str) -> pl.Expr:
    return e - e.shift(n).over(partition)


def _midprice(n: int, partition: str) -> pl.Expr:
    return (
        pl.col("high").rolling_max(n).over(partition)
        + pl.col("low").rolling_min(n).over(partition)
    ) / 2


def _cci(n: int, partition: str) -> pl.Expr:
    tp = (pl.col("high") + pl.col("low") + pl.col("close")) / 3
    # talib CCI denominator is the window MEAN ABSOLUTE DEVIATION, not std
    # (rolling_map: no native rolling-MAD expr; probed == talib.CCI <= 3.5e-12)
    mad = tp.rolling_map(lambda s: (s - s.mean()).abs().mean(), window_size=n).over(partition)
    # talib guards the 0/0 flat-window case to 0.0 (and duckdb t_cci does too);
    # NaN would pass polars `>` filters (same trap _rsi guards). Null cond
    # (rolling_map warm-up) must be checked FIRST or it takes the otherwise
    # branch and warm-up would emit 0.0 instead of the pinned null mask.
    return (
        pl.when(mad.is_null())
        .then(None)
        .when(mad == 0)
        .then(0.0)
        .otherwise((tp - tp.rolling_mean(n).over(partition)) / (0.015 * mad))
    )


def _willr(n: int, partition: str) -> pl.Expr:
    hh = pl.col("high").rolling_max(n).over(partition)
    ll = pl.col("low").rolling_min(n).over(partition)
    return -100 * (hh - pl.col("close")) / (hh - ll)


def _trange(n: int, partition: str) -> pl.Expr:
    pc = pl.col("close").shift(1).over(partition)
    tr = pl.max_horizontal(
        pl.col("high") - pl.col("low"), (pl.col("high") - pc).abs(), (pc - pl.col("low")).abs()
    )
    # bar 0 must be null, not max_horizontal's h-l fallback (talib TRANGE nulls it)
    return pl.when(pc.is_not_null()).then(tr).otherwise(None)


def _ad(_n=None, partition: str = "symbol") -> pl.Expr:
    # `_n` is always None: ad is the one periodless entry (empty arg_spec),
    # but the builder keeps a (n, partition)-compatible call shape
    # talib contributes 0.0 on zero-range bars (high==low); the raw 0/0 NaN
    # would poison cum_sum for the rest of the partition (NaN passes polars
    # `>` filters — same trap _rsi guards)
    clv = pl.when(pl.col("high") != pl.col("low")).then(
        ((pl.col("close") - pl.col("low")) - (pl.col("high") - pl.col("close")))
        / (pl.col("high") - pl.col("low"))
    ).otherwise(0.0)
    return (clv * pl.col("volume")).cum_sum().over(partition)


def _adx(n: int, partition: str):
    """talib ADX per partition via the group_by/map_groups seam (0.4.0 parity plan).

    Eager-only by contract (map_groups has no lazy form) and requires the
    ``talib`` extra; collected results are exact TA-Lib — null for the first
    ``2n-1`` bars per partition (NaN warm-up normalized), exact afterwards.
    """
    import talib

    def _apply(df: pl.DataFrame) -> pl.DataFrame:
        arr = talib.ADX(
            df["high"].to_numpy(), df["low"].to_numpy(), df["close"].to_numpy(), timeperiod=n
        )
        # reserved "__adx" output name — apply() renames it to a unique alias
        # (a user column literally named "adx" must not be clobbered)
        return df.with_columns(pl.Series("__adx", arr).fill_nan(None))

    return _apply


def _kama(n: int, partition: str):
    """talib KAMA per partition via the same group_by/map_groups seam as ``_adx``.

    KAMA's adaptive filtering ratio has no exact polars-native expression;
    eager-only, ``talib`` extra required, values exact TA-Lib, null warm-up
    for the first ``n`` bars per partition.
    """
    import talib

    def _apply(df: pl.DataFrame) -> pl.DataFrame:
        arr = talib.KAMA(df["close"].to_numpy(), timeperiod=n)
        return df.with_columns(pl.Series("__adx", arr).fill_nan(None))

    return _apply


# name -> (arg_spec, builder, required_cols)
#
# Extend by inserting entries; the entry shape is the contract. See
# docs/how-to/extend-indicators.md for the extension recipe.
INDICATORS: dict[str, tuple[tuple[str, ...], Callable, tuple[str, ...]]] = {
    "sma": (
        ("expr", "int"),
        lambda e, n, partition: e.rolling_mean(n).over(partition),
        (),
    ),
    "ema": (
        ("expr", "int"),
        lambda e, span, partition: e.ewm_mean(span=span, adjust=False).over(partition),
        (),
    ),
    "rsi": (("expr", "int"), _rsi, ()),
    "atr": (("int",), _atr, ("high", "low", "close")),
    "adr": (
        ("expr", "int"),
        lambda e, n, partition: (_tr(n, partition) / pl.col("close") * 100)
        .rolling_mean(n)
        .over(partition),
        ("high", "low", "close"),
    ),
    "roc": (
        ("expr", "int"),
        lambda e, n, partition: (e / e.shift(n).over(partition) - 1) * 100,
        (),
    ),
    "natr": (
        ("expr", "int"),
        lambda e, n, partition: (_atr(n, partition) / pl.col("close") * 100).over(partition),
        ("high", "low", "close"),
    ),
    "slope": (
        ("expr", "int"),
        _slope,
        (),
    ),
    "rmin": (
        ("expr", "int"),
        lambda e, n, partition: e.rolling_min(n).over(partition),
        (),
    ),
    "rmax": (
        ("expr", "int"),
        lambda e, n, partition: e.rolling_max(n).over(partition),
        (),
    ),
    "shift": (
        ("expr", "int"),
        lambda e, n, partition: e.shift(n).over(partition),
        (),
    ),
    "rs_ratio": (("expr", "int"), _rs_ratio, ()),
    "rs_momentum": (("expr", "int"), _rs_momentum, ()),
    # --- 0.4.0 single-output TA-Lib parity set ---
    "wma": (("expr", "int"), _wma, ()),
    "dema": (("expr", "int"), _dema, ()),
    "tema": (("expr", "int"), _tema, ()),
    "trima": (("expr", "int"), _trima, ()),
    "mom": (("expr", "int"), _mom, ()),
    "midprice": (("int",), _midprice, ("high", "low")),
    "cci": (("int",), _cci, ("high", "low", "close")),
    "willr": (("int",), _willr, ("high", "low", "close")),
    # dummy-int precedent (ht_trendline): TRANGE takes no period; n is ignored
    "trange": (("int",), _trange, ("high", "low", "close")),
    "ad": ((), _ad, ("high", "low", "close", "volume")),
}

if "adx" not in INDICATORS:  # optional talib parity builders (eager, needs the talib extra)
    INDICATORS["adx"] = (("int",), _adx, ("high", "low", "close"))
if "kama" not in INDICATORS:  # same seam: KAMA's adaptive ratio has no polars-native form
    INDICATORS["kama"] = (("int",), _kama, ("close",))

"""Indicator registry: the ``{"fn": name}`` operand extension point.

Maps indicator name -> ``(arg_spec, builder, required_cols)``:

- ``arg_spec``: one tag per positional arg — ``"expr"`` (any operand:
  column ref, nested indicator, arithmetic) or ``"int"`` (a literal int
  >= 1, i.e. a window length).
- ``builder(*parsed, partition) -> pl.Expr``: called with the parsed
  args in order plus the partition column name; must return a
  polars-native expression, with every window op under ``.over(partition)``
  so scans stay correct per symbol.
- ``required_cols``: catalog columns the builder needs (validated by
  [`validate`](api.md#scanlang.compiler.validate)); empty for most entries.

Registry mutation is the extension point:
``INDICATORS["stdev"] = (("expr", "int"), builder, ())`` — register
idempotently (guard with ``if "stdev" not in INDICATORS``) at import
time. See [Extend INDICATORS](../how-to/extend-indicators.md).

Multi-output talib indicators are NOT entries here — the polars engine has
no native expression for them. ``macd``, ``bbands``, ``aroon``,
``cdlengulfing``, and ``ht_trendline`` live only in
``scanlang.duckdb_sql.SQL_INDICATORS`` (duckdb engine, talib extension);
``scanlang.compiler.validate(scan_def, engine="duckdb")`` accepts them.
``adx`` and ``kama`` are dual-engine exceptions: a registered parity builder
here (the group_by/map_groups seam above, exact TA-Lib values) plus their
``SQL_INDICATORS`` ``t_adx``/``t_kama`` entries — the same names validate
and execute on both engines.
"""
