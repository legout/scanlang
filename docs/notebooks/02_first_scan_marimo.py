"""First scan in scanlang — marimo edition.

A reactive marimo notebook that walks the same happy path as
`01_first_scan.ipynb`, against the same fixture the
`docs/examples/*.py` scripts use.

Run interactively:

    uv run marimo edit docs/notebooks/02_first_scan_marimo.py

Run as a script (no browser):

    uv run marimo run --headless docs/notebooks/02_first_scan_marimo.py

Export a static HTML snapshot (this is what CI uses):

    uv run marimo export html docs/notebooks/02_first_scan_marimo.py \\
        -o /tmp/scanlang-marimo.html --force

The notebook stays headless: no network, no parquet, no lake — just
120 rows of synthetic OHLCV data and one `apply()`.
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

    # make _fixture importable when the notebook is run from the repo root
    sys.path.insert(0, ".")

    import polars as pl
    from _fixture import SCAN_DEF, bars_eager, bars_lazy

    from scanlang import apply, score_bars, validate

    return SCAN_DEF, apply, bars_eager, bars_lazy, pl, score_bars, validate


@app.cell
def _(mo):
    mo.md(
        r"""
        # First scan in scanlang — marimo edition

        Same fixture as `01_first_scan.ipynb`. Each cell is reactive:
        re-running a cell refreshes every cell that depends on it.

        Run headlessly with:

        ```sh
        uv run marimo export html docs/notebooks/02_first_scan_marimo.py \\
            -o /tmp/scanlang-marimo.html --force
        ```
        """
    )


@app.cell
def _(bars_eager, bars_lazy, pl):
    df_eager = bars_eager()
    df_lazy = bars_lazy()
    print(type(df_eager).__name__, df_eager.shape)
    print(type(df_lazy).__name__, df_lazy.collect().shape)
    return df_eager, df_lazy


@app.cell
def _(df_eager, score_bars):
    scored = score_bars(df_eager).collect()
    scored.select("symbol", "session", "close", "score", "phase")
    return (scored,)


@app.cell
def _(SCAN_DEF, validate):
    errors = validate(SCAN_DEF)
    print("errors:", errors)  # []
    return (errors,)


@app.cell
def _(SCAN_DEF, apply, scored):
    picks = apply(scored, SCAN_DEF)
    picks.select("symbol", "score", "phase")
    return (picks,)


@app.cell
def _(SCAN_DEF, df_lazy, pl, score_bars, apply):
    lazy_picks = apply(score_bars(df_lazy), SCAN_DEF)
    assert isinstance(lazy_picks, pl.LazyFrame)
    lazy_picks.select("symbol", "score", "phase").collect()
    return (lazy_picks,)


@app.cell
def _(errors, lazy_picks, picks, pl, scored):
    # Lock the behaviour. marimo will surface these assertions as cell
    # errors if a future change breaks the happy path.
    assert errors == []
    assert picks.height == 1
    assert picks["symbol"][0] == "AAA"
    assert picks["score"][0] == scored["score"].max()
    assert isinstance(lazy_picks, pl.LazyFrame)
    print("02_first_scan_marimo OK")


@app.cell
def _(mo):
    mo.md(
        r"""
        ## Where to next

        - [`01_first_scan.ipynb`](./01_first_scan.ipynb) — same scan
          in a Jupyter notebook, executed by `nbconvert --execute`.
        - Tutorial: [First scan in 5 minutes](../tutorials/first-scan.md)
        - How-to: [Eager vs lazy frames](../how-to/eager-frames.md)
        - Examples: [docs/examples/](../examples/)
        """
    )


if __name__ == "__main__":
    app.run()