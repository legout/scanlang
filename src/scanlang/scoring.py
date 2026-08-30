"""Parabolic breakout scoring: vectorized polars over OHLCV bars.

Detects BASE / BREAKOUT / TREND / CLIMAX phases per symbol using EMA
alignment, ATR expansion, volume confirmation, accumulation, spring
patterns, and RSI. One lazy polars pass over all symbols.

Input: LazyFrame (eager DataFrame accepted, coerced) with columns
``symbol, session, open, high, low, close, volume``, sorted
``symbol, session`` ascending (caller guarantees the sort, caller
collects at its edge — the return is lazy). Output columns mirror
``compiler.PROPERTY_CATALOG``.

Composite weights: spring +15, accumulation +10, volume +5/+15, EMA stack
+10/+20, fresh EMA5/20 cross +15, EMA50 rising +10, price>EMA5 +5, ATR
expansion +10/+15, wide range +5, RSI>70 +5, upper wick +10, near-52w-low
+5. Phase thresholds: CLIMAX >=70 with a blow-off condition, TREND >=60
with stack+ATR, BREAKOUT >=50 with volume or spring + EMA5>EMA20, BASE >=40.
"""

from __future__ import annotations

import polars as pl

__all__ = ["FRESHNESS_DAYS", "MIN_BARS", "score_bars"]

# Skip symbols with too little history for ATR/EMA50 to be meaningful.
MIN_BARS = 30
# Only score symbols whose latest bar is this close to the lake's max date.
FRESHNESS_DAYS = 5


def score_bars(
    bars: pl.LazyFrame | pl.DataFrame,
    *,
    min_bars: int = MIN_BARS,
    freshness_days: int = FRESHNESS_DAYS,
) -> pl.LazyFrame:
    """Score every symbol's latest bar in ``bars`` and classify its phase."""
    bars = bars.lazy()
    c = pl.col

    ind = (
        bars.with_columns(
            ema5=c("close").ewm_mean(span=5, adjust=False).over("symbol"),
            ema20=c("close").ewm_mean(span=20, adjust=False).over("symbol"),
            ema50=c("close").ewm_mean(span=50, adjust=False).over("symbol"),
            _pc=c("close").shift(1).over("symbol"),
            _up=c("close") > c("open"),
            _dn=c("close") < c("open"),
            _n=c("close").count().over("symbol"),
        )
        .with_columns(
            _tr=pl.max_horizontal(
                c("high") - c("low"),
                (c("high") - c("_pc")).abs(),
                (c("low") - c("_pc")).abs(),
            ),
        )
        .with_columns(atr=c("_tr").rolling_mean(14).over("symbol"))
        .with_columns(
            baseline_atr=c("atr").rolling_mean(20, min_samples=1).over("symbol"),
            avg_vol=c("volume").cast(pl.Float64).rolling_mean(20, min_samples=1).over("symbol"),
            _delta=c("close").diff().over("symbol"),
        )
        .with_columns(
            avg_up_vol=pl.when(c("_up"))
            .then(c("volume").cast(pl.Float64))
            .otherwise(None)
            .rolling_mean(20, min_samples=1)
            .over("symbol"),
            avg_dn_vol=pl.when(c("_dn"))
            .then(c("volume").cast(pl.Float64))
            .otherwise(None)
            .rolling_mean(20, min_samples=1)
            .over("symbol"),
            _gain=c("_delta").clip(lower_bound=0).rolling_mean(14).over("symbol"),
            _loss=(-c("_delta").clip(upper_bound=0)).rolling_mean(14).over("symbol"),
        )
        .with_columns(
            acc_score=pl.when(c("avg_vol") == 0)
            .then(0.0)
            .when(c("_dn").cast(pl.UInt8).rolling_sum(20, min_samples=1).over("symbol") == 0)
            .then(1.0)
            .otherwise((c("avg_up_vol").fill_null(0.0) - c("avg_dn_vol").fill_null(0.0)) / c("avg_vol")),
            rsi=(100 - 100 / (1 + c("_gain") / c("_loss"))).fill_null(50.0),
            vol_ratio=pl.when(c("avg_vol") > 0).then(c("volume") / c("avg_vol")).otherwise(1.0),
            atr_ratio=pl.when(c("baseline_atr") > 0)
            .then(c("atr").fill_null(c("baseline_atr")) / c("baseline_atr"))
            .otherwise(1.0),
            _spring=(c("low").shift(1).over("symbol") < c("low").shift(2).over("symbol"))
            .and_(c("close").shift(1).over("symbol") > c("open").shift(1).over("symbol"))
            .and_(c("close").shift(1).over("symbol") > c("low").shift(2).over("symbol")),
            _ema_stack=(c("ema5") > c("ema20")).and_(c("ema20") > c("ema50")),
            _cross=(c("ema5") > c("ema20")).and_(
                c("ema5").shift(1).over("symbol") <= c("ema20").shift(1).over("symbol")
            ),
        )
        .with_columns(
            recent_cross=c("_cross")
            .fill_null(False)
            .cast(pl.UInt8)
            .rolling_sum(5, min_samples=1)
            .over("symbol")
            > 0,
            ema50_rising=c("ema50") > c("ema50").shift(1).over("symbol"),
            _low52=c("low").rolling_min(252, min_samples=1).over("symbol"),
            _range=c("high") - c("low"),
            _last=c("session").max().over("symbol"),
        )
        .with_columns(
            upper_wick_pct=pl.when(c("_range") > 0)
            .then((c("high") - pl.max_horizontal(c("close"), c("open"))) / c("_range"))
            .otherwise(0.0),
            near_52w_low=(c("_low52") > 0).and_(c("close") < c("_low52") * 1.30),
            is_latest=c("session") == c("_last"),
        )
    )

    score = (
        pl.when(c("_spring")).then(15).otherwise(0)
        + pl.when(c("acc_score") > 0.10).then(10).otherwise(0)
        + pl.when(c("vol_ratio") > 1.5).then(15).when(c("vol_ratio") > 1.0).then(5).otherwise(0)
        + pl.when(c("_ema_stack")).then(20).when(c("ema5") > c("ema20")).then(10).otherwise(0)
        + pl.when(c("recent_cross")).then(15).otherwise(0)
        + pl.when(c("ema50_rising")).then(10).otherwise(0)
        + pl.when(c("close") > c("ema5")).then(5).otherwise(0)
        + pl.when(c("atr_ratio") > 2.0).then(15).when(c("atr_ratio") > 1.5).then(10).otherwise(0)
        + pl.when(c("_range") > 2 * c("atr").fill_null(c("baseline_atr"))).then(5).otherwise(0)
        + pl.when(c("rsi") > 70).then(5).otherwise(0)
        + pl.when(c("upper_wick_pct") > 0.4).then(10).otherwise(0)
        + pl.when(c("near_52w_low")).then(5).otherwise(0)
    )

    return (
        ind.with_columns(score=score.cast(pl.Int16))
        .with_columns(
            phase=pl.when(
                (c("score") >= 70).and_((c("vol_ratio") > 3).or_(c("atr_ratio") > 2.5).or_(c("rsi") > 85))
            )
            .then(pl.lit("CLIMAX"))
            .when((c("score") >= 60).and_(c("_ema_stack")).and_(c("atr_ratio") > 1.5))
            .then(pl.lit("TREND"))
            .when(
                (c("score") >= 50)
                .and_((c("vol_ratio") > 1.5).or_(c("_spring")))
                .and_(c("ema5") > c("ema20"))
            )
            .then(pl.lit("BREAKOUT"))
            .when(c("score") >= 40)
            .then(pl.lit("BASE"))
            .otherwise(pl.lit("NONE")),
        )
        .filter(c("is_latest"))
        .filter(c("_n") >= min_bars)
        # freshness: the symbol's latest bar must be within N days of the
        # GLOBAL max session in the frame (not its own max — that's always 0)
        .filter((c("session").max() - c("session")).dt.total_days() <= freshness_days)
        .select(
            "symbol",
            "session",
            "close",
            "score",
            "phase",
            vol_ratio=c("vol_ratio").round(2),
            atr_ratio=c("atr_ratio").round(2),
            rsi=c("rsi").round(1),
            acc_score=c("acc_score").round(3),
            spring=c("_spring"),
            ema_stack=c("_ema_stack"),
            recent_cross=c("recent_cross"),
            upper_wick_pct=c("upper_wick_pct").round(2),
            near_52w_low=c("near_52w_low"),
            bars=c("_n"),
        )
        .sort(["score", "symbol"], descending=[True, False])
    )
