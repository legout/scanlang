"""Raw-first scan notebook in scanlang - marimo edition.

This notebook runs a scan on raw OHLCV bars before scoring them. It then
validates and applies a second screen to the scored output.

Run interactively:

    uv run marimo edit docs/notebooks/02_first_scan_marimo.py

Run headlessly:

    uv run marimo run --headless docs/notebooks/02_first_scan_marimo.py

Export HTML:

    uv run marimo export html docs/notebooks/02_first_scan_marimo.py \\
        -o /tmp/scanlang-marimo.html --force
"""

import marimo

__generated_with = "0.24.0"
app = marimo.App(width="medium")


@app.cell
def _():
    import marimo as mo

    return (mo,)


@app.cell
def _():
    import sys
    from pathlib import Path

    notebook_dir = Path("docs/notebooks")
    if not (notebook_dir / "_fixture.py").exists():
        notebook_dir = Path(".")
    sys.path.insert(0, str(notebook_dir))

    import polars as pl

    from _fixture import RAW_SCAN_DEF, SCORE_SCAN_DEF, bars_eager, bars_lazy
    from scanlang import apply, parse, score_bars, validate

    return (
        RAW_SCAN_DEF,
        SCORE_SCAN_DEF,
        apply,
        bars_eager,
        bars_lazy,
        parse,
        pl,
        score_bars,
        validate,
    )


@app.cell
def _(mo):
    mo.md(
        r"""
        # First scan in scanlang - marimo edition

        This notebook tests a scan on raw OHLCV bars first. It then scores the
        bars and applies a second screen to the scored output.

        Run headlessly with:

        ```sh
        uv run marimo run --headless docs/notebooks/02_first_scan_marimo.py
        ```
        """
    )


@app.cell
def _(bars_eager, bars_lazy):
    df_eager = bars_eager()
    df_lazy = bars_lazy()
    assert df_eager.shape == (120, 7)
    assert df_lazy.collect().shape == (120, 7)
    return df_eager, df_lazy


@app.cell
def _(RAW_SCAN_DEF, apply, df_eager, parse, validate):
    raw_scan = parse("ema(5) > ema(20)")
    assert raw_scan == RAW_SCAN_DEF
    raw_errors = validate(raw_scan)
    assert raw_errors == []
    raw_picks = apply(df_eager, raw_scan)
    assert raw_picks.height == 59
    assert raw_picks["symbol"].unique().to_list() == ["AAA"]
    raw_picks.select("symbol", "session", "close").head()
    return raw_errors, raw_picks, raw_scan


@app.cell
def _(df_eager, score_bars):
    scored = score_bars(df_eager).collect()
    scored.select("symbol", "session", "close", "score", "phase")
    return (scored,)


@app.cell
def _(SCORE_SCAN_DEF, apply, scored, validate):
    errors = validate(SCORE_SCAN_DEF)
    assert errors == []
    picks = apply(scored, SCORE_SCAN_DEF)
    assert picks.height == 1
    assert picks["symbol"][0] == "AAA"
    picks.select("symbol", "score", "phase")
    return errors, picks


@app.cell
def _(SCORE_SCAN_DEF, apply, df_lazy, pl, score_bars):
    lazy_picks = apply(score_bars(df_lazy), SCORE_SCAN_DEF)
    assert isinstance(lazy_picks, pl.LazyFrame)
    lazy_result = lazy_picks.select("symbol", "score", "phase").collect()
    assert lazy_result.height == 1
    lazy_result
    return lazy_picks, lazy_result


@app.cell
def _(errors, lazy_result, picks, raw_errors, raw_picks, scored):
    assert raw_errors == []
    assert errors == []
    assert raw_picks.height == 59
    assert picks.height == 1
    assert picks["score"][0] == scored["score"].max()
    assert lazy_result.rows() == picks.select("symbol", "score", "phase").rows()
    print("02_first_scan_marimo OK")


@app.cell
def _(mo):
    mo.md(
        r"""
        ## Where to next

        - [`01_first_scan.ipynb`](./01_first_scan.ipynb) - the same raw-first
          workflow in Jupyter, executed by `nbconvert --execute`.
        - [Use it](../use.md) - the complete library workflow.
        - [Language](../language.md) - DSL and dict syntax.
        - [Examples](../more.md#examples-and-notebooks) - runnable scripts.
        """
    )


if __name__ == "__main__":
    app.run()
