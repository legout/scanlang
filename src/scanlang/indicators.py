"""Indicator registry: name -> (arg_spec, builder, required_cols).

Each entry:
- ``arg_spec``: tuple with one tag per positional arg — ``"expr"`` (any operand) or
  ``"int"`` (literal int >= 1).
- ``builder(*parsed, partition) -> pl.Expr``: polars-native; every window op uses
  ``.over(partition)``. Exception: the TA-Lib parity slice (``adx``, ``kama``,
  ``macd``, ``bbands_upper``/``bbands_lower``, ``aroon``) — its builders come from
  ``_seam_builder`` (the one seam closure, fed by the ``_TALIB_SEAM`` table) and
  return a ``DataFrame -> DataFrame`` callable for
  ``group_by(partition, maintain_order=True).map_groups(...)`` (eager collect
  required, ``talib`` extra required), matching the duckdb ``t_*`` values
  exactly, with null (NaN normalized) warm-up.
- ``required_cols``: columns that must exist in the catalog (e.g. ``atr`` needs
  ``high, low, close``).

Extend by inserting entries; this shape is the contract. The ``talib``
extra's parity builder populates the same dict this way — it cannot
participate in lazy pushdown. The parity wave-2 workflow adds seam names
by editing ``SPEC`` in ``scripts/gen_talib_seam.py`` and rerunning it
(the committed table block is generated; see the BEGIN/END markers on
``_TALIB_SEAM``).

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


def _seam_builder(
    fn: str, inputs: tuple[str, ...], n_kw: str, kwargs: dict, slot: int | None
):
    """Eager talib seam: bind the call at n-bind time, per-partition map_groups.

    One factory for every bindable-period parity function (adx, kama, macd,
    bbands_upper/lower, aroon, ...) — the ``_TALIB_SEAM`` table below is
    data, this is the only seam closure. Row order here matches the table
    rows 1:1. Returns a builder compatible with the apply() staging in
    compiler.py: called with (n, partition=...) it returns a
    ``(df) -> DataFrame`` callable whose output lands in the reserved
    ``__adx`` column (apply() renames it to a unique alias; a user column
    named like the indicator must not be clobbered), NaN warm-up normalized
    to null. Eager-only by contract (map_groups has no lazy form); talib is
    imported at n-bind time so registry insertion stays extra-free
    (test_talib_missing.py contract).

    ``n`` binds to ``n_kw``: ``timeperiod`` for most fns; fast/slow fns
    (macd) take no timeperiod — n is the fast period and the slow/signal
    pair is pinned in ``kwargs`` (no parameter polymorphism — same
    signature both engines). ``slot`` narrows multi-output functions to the
    one approved scalar per scanlang name (talib BBANDS order: upper,
    middle, lower; the middle band is plain sma and stays unexposed; talib
    AROON order is (down, up), slot 1 = the up line — the swap is pinned by
    test).
    """

    def build(n: int, partition: str):
        import talib

        call = getattr(talib, fn)
        kw = {n_kw: n, **kwargs}

        def _apply(df: pl.DataFrame) -> pl.DataFrame:
            arr = call(*[df[c].to_numpy() for c in inputs], **kw)
            if slot is not None:
                arr = arr[slot]
            return df.with_columns(pl.Series("__adx", arr).fill_nan(None))

        return _apply

    return build


# candlestick patterns: every registered CDL* has the identical (o,h,l,c)
# -> int32 0/±100 signature, so one closure registers them all; the pattern
# name is bound at builder-call time (default arg — the registry loop would
# otherwise close over the loop variable)
def _cdl(fn: str):
    """talib candlestick pattern per partition via the group_by/map_groups seam.

    ``fn`` is the exact talib function name (e.g. ``CDLHAMMER``); the SQL
    side mirrors it as ``t_cdl<name>``. talib patterns read (open, high,
    low, close), take no period, and return 0/±100 (int) — null nowhere
    after bar 0 per partition.
    """

    def build(_n=None, partition: str = "symbol"):
        import talib

        def _apply(df: pl.DataFrame) -> pl.DataFrame:
            arr = getattr(talib, fn)(
                df["open"].to_numpy(),
                df["high"].to_numpy(),
                df["low"].to_numpy(),
                df["close"].to_numpy(),
            )
            return df.with_columns(pl.Series("__adx", arr).fill_nan(None))

        return _apply

    return build


_CDL_PARITY = (
    # the 25 patterns (+ cdlengulfing = 26 names) where the community duckdb
    # talib extension (t_cdl*) is value-identical to talib on every probed
    # bar: 5 random walks (seeds 11/777/2026/31/4242, 150 bars each) plus 60+
    # crafted/degenerate scenarios (canonical reversals, doji family, gap
    # structures, flat, wide spike — two generations of hand-tuned bars).
    # All are structural/gap/count patterns. The other 27 extension-registered
    # names diverge in VALUE (everything threshold-relative via talib's
    # CandleSettings): the extension uses textbook thresholds, so e.g.
    # t_cdlhammer says 0 where CDLHAMMER says 100 on identical bars, and
    # t_cdldoji fires on a flat series where talib says 0. Parity beats
    # coverage: excluded, not emulated (same policy as the 7 penetration-
    # parameter patterns the extension lacks). Re-derive with
    # scripts/gen_cdl_fixtures.py --classify when bumping either library.
    "cdl2crows", "cdl3blackcrows", "cdl3linestrike", "cdl3outside",
    "cdl3starsinsouth", "cdl3whitesoldiers", "cdlbreakaway",
    "cdlconcealbabyswall", "cdlengulfing", "cdlgravestonedoji", "cdlhikkake",
    "cdlhomingpigeon", "cdlidentical3crows", "cdlinneck", "cdlkicking",
    "cdlkickingbylength", "cdlladderbottom", "cdlonneck",
    "cdlrisefall3methods", "cdlseparatinglines", "cdlstalledpattern",
    "cdltasukigap", "cdlthrusting", "cdlunique3river", "cdlupsidegap2crows",
    "cdlxsidegap3methods",
)


def _cdl_names() -> list[str]:
    """Registered candlestick patterns (shared by both registries).

    The curated ``_CDL_PARITY`` intersection — static so registry insertion
    stays talib-free (the module must import without the extra; a live-talib
    membership check happens in the tests and in scripts/gen_cdl_fixtures.py).
    cdlengulfing is in the set: its 0.3.0 SQL builder already lowers
    identically, and the polars loop gives it its first eager builder without
    touching its arg_spec/required_cols contract.
    """
    return list(_CDL_PARITY)


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

# name -> (talib_fn, inputs, n_kw, fixed kwargs, output slot)
#
# The rows between the markers are GENERATED by scripts/gen_talib_seam.py
# from its SPEC dict — edit SPEC, not this block, then rerun:
#
#     uv run python scripts/gen_talib_seam.py
#
# The comments below the markers are hand-written and survive regeneration.
#
# One row per seam name; the single ``_seam_builder`` factory above consumes
# these (row order 1:1 — Task 0 decision in the plan). ``inputs`` are the
# catalog columns the C function reads (they are the registration's
# required_cols). ``slot``: None = scalar output; int = index into the
# tuple return (the one-field-per-name narrowing). ``n_kw`` is the kwarg n
# binds to — ``timeperiod`` for most fns; fast/slow fns (macd) take no
# timeperiod: n is the fast period, the pair is pinned in kwargs. Fns with
# no bindable period (ad, CDL; wave-2 ultosc/obv/sar/ht_*) stay hand-written.
# --- BEGIN GENERATED talib seam table (scripts/gen_talib_seam.py) ---
_TALIB_SEAM = {
    "adx": ("ADX", ("high", "low", "close"), "timeperiod", {}, None),
    "kama": ("KAMA", ("close",), "timeperiod", {}, None),
    "macd": ("MACD", ("close",), "fastperiod", {"slowperiod": 26, "signalperiod": 9}, 0),
    "bbands_upper": ("BBANDS", ("close",), "timeperiod", {"nbdevup": 2.0, "nbdevdn": 2.0, "matype": 0}, 0),
    "bbands_lower": ("BBANDS", ("close",), "timeperiod", {"nbdevup": 2.0, "nbdevdn": 2.0, "matype": 0}, 2),
    "aroon": ("AROON", ("high", "low"), "timeperiod", {}, 1),
}
# --- END GENERATED ---

# optional talib parity builders (eager, needs the talib extra). Registry
# mutation is idempotent (the extension point contract); the seam builders
# import talib only when n is bound, so insertion stays talib-free and the
# names validate on a talib-less interpreter (test_talib_missing.py).
for _name, (_fn, _cols, _nkw, _kw, _slot) in _TALIB_SEAM.items():
    INDICATORS.setdefault(_name, (("int",), _seam_builder(_fn, _cols, _nkw, _kw, _slot), _cols))
# candlestick-pattern parity (same seam, extra-free import): every CDL name
# is the dummy-int precedent (patterns take no period); on a talib-less
# interpreter the entry still registers (the name set is static, no import
# -time talib probe — the seam builders import talib only when n is bound,
# the test_talib_missing.py contract), validate() passes, and apply()/
# compile() report the install hint. cdlengulfing rides the same loop (its
# 0.3.0 duckdb-only entry gets a polars builder, but the registry-insertion
# contract — arg_spec/required_cols — is untouched).
for _fn in _cdl_names():
    INDICATORS.setdefault(_fn.lower(), (("int",), _cdl(_fn.upper()), ("open", "high", "low", "close")))

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

Multi-output talib indicators narrow to **one scalar per scanlang name**
(never a struct through the IR). ``macd`` (the MACD line),
``bbands_upper``/``bbands_lower``, and ``aroon`` (the up line) are
dual-engine: the polars side uses the same group_by/map_groups seam as
``adx``/``kama`` (exact TA-Lib values, eager + ``talib`` extra required),
the duckdb side narrows the multi-output ``t_*`` struct in SQL.
``signal``/``hist``, the middle band, and ``aroon_down`` are not exposed
(approved catalog: one field per name; the middle band is ``sma``).
``cdlengulfing`` is dual-engine now (the curated ``_CDL_PARITY``
candlestick set shares its dummy-int signature on both engines);
``ht_trendline`` remains duckdb-only, and
``stoch_k``/``stoch_d`` stay SQL-only by design.
``scanlang.compiler.validate(scan_def, engine="duckdb")`` accepts every
SQL-side name.
"""
