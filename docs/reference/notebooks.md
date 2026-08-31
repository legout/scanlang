# Notebooks

Two checked-in notebooks walk the same happy path on the same
two-symbol OHLCV fixture. Pick the one that matches your environment.

| Notebook | Engine | Use it when |
| --- | --- | --- |
| [`01_first_scan.ipynb`][jup] | Jupyter / IPython | You're in a classic notebook, JupyterLab, VS Code, Colab, or any `%`-magics environment. |
| [`02_first_scan_marimo.py`][mar] | marimo | You want reactive cells, deterministic re-runs, or a script-as-app workflow. |

[jup]: ../../notebooks/01_first_scan.ipynb
[mar]: ../../notebooks/02_first_scan_marimo.py

Both notebooks:

- import the deterministic fixture from `docs/notebooks/_fixture.py`
  (60 trading days × 2 symbols, no network or lake dependency);
- cover the same five-step scan: fixture → score → validate → apply
  → lazy-variant;
- end with `assert` statements so the notebook fails loudly if a
  future change breaks the happy path.

## Running the Jupyter notebook

The notebook is plain nbformat 4 with a `python3` kernel. Execute it
headlessly with the project's docs interpreter:

```sh
uv run jupyter nbconvert --execute --to notebook --inplace \
  docs/notebooks/01_first_scan.ipynb
```

CI-friendly, no browser required.

## Running the marimo notebook

`marimo` ships its own export path that runs every cell and writes a
standalone HTML snapshot:

```sh
uv run marimo export html docs/notebooks/02_first_scan_marimo.py \
  -o /tmp/scanlang-marimo.html --force
```

To open it interactively in a browser:

```sh
uv run marimo edit docs/notebooks/02_first_scan_marimo.py
```

To run it as a script (no browser):

```sh
uv run marimo run --headless docs/notebooks/02_first_scan_marimo.py
```

## Why two engines

The `apply` / `score_bars` API is shape-preserving regardless of
engine; the user-visible difference is the cell model:

- Jupyter: cells are independent, ordered text blocks; outputs are
  sticky and only refresh when you re-run.
- marimo: cells are a reactive graph; if you change `score_bars(df)`
  in cell N, every downstream cell re-runs automatically.

The same scan against the same fixture lets a reader compare the two
styles without learning a new example each time.

## Where to next

- [First scan in 5 minutes](../tutorials/first-scan.md) — the written
  tutorial these notebooks are based on
- [Eager vs lazy frames](../how-to/eager-frames.md) — the four
  execution shapes a notebook can take
- [Examples](../reference/examples.md) — the seven runnable Python
  scripts these notebooks share their fixture with