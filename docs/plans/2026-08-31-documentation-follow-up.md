# Documentation follow-up plan

Status: planning only. Do not create Kanban cards until the user confirms this plan.

## Goal

Make scanlang documentation easy to use for three audiences:

- someone trying the package for the first time;
- someone using it from a synchronous Python session, Jupyter, or marimo;
- someone looking up exact IR, DSL, and API behavior.

Keep the repository docs in Markdown, build them with Zensical, and generate API
pages with mkdocstrings-python. Do not duplicate implementation logic in the docs.

## Current state and known gaps

The first documentation pass already landed:

- seven runnable examples, including eager and lazy modes;
- a 149-line structured README;
- a Diataxis tree under `docs/`;
- `zensical.toml` and mkdocstrings API pages;
- `uv run zensical build` passes and all examples run.

The next pass should fix accuracy and maintenance issues rather than recreate that
work:

1. `README.md` links to `docs/zensical.toml`, but the file is `zensical.toml` at the
   repository root.
2. `zensical.toml` advertises `https://scanlang.readthedocs.io/`, which currently
   returns 404. There is no GitHub Pages site configured.
3. The first tutorial's snippets use a pre-existing `bars` variable. That is fine
   for a fragment, but the page calls itself a five-minute first scan. The reader
   should be able to copy one complete example or be told exactly which lines are
   intentionally omitted.
4. README and site content repeat some reference material. The README should point
   to the site for detail; the site should hold the full explanations.
5. There is no docs-specific CI check. A broken example or API directive can land
   without the package test suite noticing.

## Proposed Kanban graph

Use implementation cards only. Each implementation card must request the built-in
review lane explicitly with `kanban_request_review(reviewers=reviewer)`. No dedicated
review cards.

```
D1 docs audit + source-of-truth fixes
├── D2 runnable examples and example verification
├── D3 README rewrite and link cleanup
├── D4 Diataxis content pass
├── D5 API reference and docstring accuracy
└── D6 docs CI / hosting (after D2-D5)
```

D2-D5 can run in parallel after D1. D6 waits for all four because its checks must
cover the final examples, Markdown tree, API pages, and selected README links.

### D1: audit and source-of-truth fixes

**Files:** `README.md`, `pyproject.toml`, `zensical.toml`, selected `docs/*.md`.

**Work:**

- Fix the README link to `zensical.toml`.
- Decide the public docs URL and make `site_url` match it. Do not leave a dead
  Read the Docs URL in the config.
- Correct stale package wording if it claims a DuckDB backend; the current design
  is polars-only, as documented in `docs/RESEARCH_DUCKDB.md`.
- Establish the rule: README gives the short path; site pages give detail; source
  docstrings define signatures and API behavior.
- Check navigation for every intended page and remove dead or duplicate navigation
  entries.

**Acceptance:** all internal README links resolve to tracked files; `zensical build`
passes with no missing-page warnings; the chosen URL is recorded in the config.

### D2: runnable examples and sync usage

**Files:** `docs/examples/*.py`, `docs/notebooks/01_first_scan.ipynb`,
`docs/notebooks/02_first_scan_marimo.py`, `docs/tutorials/first-scan.md`,
`docs/how-to/eager-frames.md`, `docs/reference/examples.md`,
`docs/reference/notebooks.md`, `zensical.toml`, `pyproject.toml`, optionally
`tests/test_examples.py`.

**Work:**

- Keep one short eager example as the default REPL/Jupyter starting point.
- Keep one lazy example that shows `.collect()` at the caller's boundary.
- Keep one mixed example: build a lazy scoring plan, apply a scan, then collect
  only for display/export.
- Make the first tutorial copy-pasteable by either creating its small OHLCV frame
  in the page or linking clearly to `docs/examples/06_eager_quickstart.py` as the
  complete script. Avoid long repeated fixtures in every page.
- Show both dict IR and text DSL, but do not explain the whole IR in the README.
- Add one checked-in Jupyter notebook at `docs/notebooks/01_first_scan.ipynb`
  and one marimo notebook at `docs/notebooks/02_first_scan_marimo.py`. Both
  should use the same deterministic OHLCV fixture and cover the same first scan.
  Add `jupyter`, `nbconvert`, and `marimo` to the `docs` dependency group with
  `uv add --group docs jupyter nbconvert marimo`.
- Show Jupyter/marimo-friendly display steps in the written tutorial without
  duplicating the complete notebook contents there.
- Run every example with `uv run python docs/examples/<file>.py`; if an example is
  intentionally a fragment, label it as such and do not count it as runnable.

**Acceptance:** every advertised script exits successfully; the checked-in Jupyter
notebook executes headlessly with `uv run jupyter nbconvert --execute --to notebook
--inplace docs/notebooks/01_first_scan.ipynb`; the marimo notebook exports with
`uv run marimo export html docs/notebooks/02_first_scan_marimo.py -o /tmp/scanlang-marimo.html
--force`; at least one example proves eager `DataFrame` input and one proves lazy
`LazyFrame` input; tutorial commands work from a clean checkout.

### D3: README rewrite and link cleanup

**Files:** `README.md` only, except for links fixed by D1.

**Target shape:**

1. one-sentence description and badges;
2. install;
3. a minimal eager quickstart that defines its data source or points to the full
   example;
4. the human DSL quickstart, including the golden-cross example;
5. a short eager-vs-lazy table;
6. links to tutorials, how-to guides, explanations, and API reference;
7. development commands and license.

**Rules:**

- Keep the README below roughly 120 lines.
- Replace large explanatory blocks with headings, tables, and links.
- Keep only the operator and indicator information needed to choose a starting
  point; put exhaustive details in site reference pages.
- Use repository links that work on GitHub and PyPI where possible. Test relative
  links against the built artifact or use canonical GitHub links when a file is
  outside the published site.

**Acceptance:** a new user can install and run the first example without reading
another page; the README contains no dead links or stale version/API claims.

### D4: Diataxis content pass

**Files:**

- `docs/index.md`
- `docs/tutorials/*.md`
- `docs/how-to/*.md`
- `docs/explanation/*.md`
- `docs/reference/*.md`

**Work:**

- Tutorials teach one path and end with a working result. Move rationale out of
  tutorials.
- How-to pages answer one task each: eager frames, custom catalog/partition,
  extending indicators, scanning from text, and score/stats.
- Explanations cover the frozen IR, lazy contract, validation split, null behavior,
  and why there is no SQL backend. Keep these pages conceptual and link to exact
  reference entries instead of repeating signatures.
- Reference pages list exact operators, DSL grammar, indicator arguments, IR shape,
  and API directives.
- Use sentence-case headings, short paragraphs, tables where lookup is involved,
  and code blocks with expected output only when it helps diagnose a mistake.
- Remove duplicated copies of the freeze/research docs from the site or clearly mark
  which page is the user-facing version and which file is the repository record.

**Acceptance:** every nav item has the correct Diataxis type, no page mixes tutorial
instructions with a long design essay, and all code samples use current public API.

### D5: API reference and docstring accuracy

**Files:** `src/scanlang/*.py`, `docs/reference/api.md`,
`docs/reference/indicators.md`.

**Work:**

- Keep mkdocstrings as the API source; do not hand-copy function signatures.
- Add concise Google-style docstrings for public functions that still lack useful
  parameter, return-shape, or eager/lazy notes.
- Document the public `INDICATORS` entry tuple precisely, including the builder
  calling convention and the fact that registry mutation is the extension point.
- Verify `parse`, `compile`, `validate`, `apply`, `score_bars`,
  `catalog_from_schema`, `forward_stats`, and `backtest_summary` render with useful
  headings and signatures.
- Make the API page a lookup page, not a second tutorial.

**Acceptance:** `uv run zensical build` renders all API directives; the generated
API page includes the public functions and registry without private helpers;
docstrings agree with behavior covered by tests.

### D6: CI and hosting

**Files:** `.github/workflows/docs.yml`, possibly `zensical.toml` and repository
settings.

**Work:**

- Add a docs-check workflow on pull requests and pushes to `master` that installs
  the docs group, runs the package tests, runs every advertised example, and runs
  `uv run zensical build`.
- Decide whether the site should remain a local Zensical build or be public.
- Recommended public option: GitHub Pages with the Actions builder. Add a separate
  deploy job only after the docs-check job is green, grant `pages: write` and
  `id-token: write`, upload `site/`, and deploy with the official Pages actions.
- If GitHub Pages is chosen, enable Pages with the workflow builder and verify the
  deployed root page by checking rendered article text, not only HTTP status. Loop
  through every nav URL and check for HTTP 200.
- Do not publish docs as part of the PyPI workflow. Package release and docs deploy
  should remain separate so a docs edit does not create a package release.

**Acceptance:** a clean PR fails when an example or API page breaks; a successful
build produces a usable root page. If hosting is selected, the live URL and root
article text are verified.

## Decisions needed before card creation

1. **Public site:** keep `scanlang.readthedocs.io` only if a Read the Docs project is
   actually created, or use GitHub Pages. Recommendation: GitHub Pages because the
   repository is already on GitHub and the build is already Zensical-based.
2. **Docs CI scope:** recommendation is examples + tests + Zensical build on every PR;
   deployment only from `master` after the check passes.
3. **Old reference files:** recommendation is to keep `docs/IR_FREEZE.md` and
   `docs/RESEARCH_DUCKDB.md` as repository records and maintain the user-facing copies
   under `docs/reference/`, with an explicit link rather than two competing sources.
4. **Notebook files:** keep checked-in notebooks under `docs/notebooks/`. Add one
  Jupyter notebook and one marimo notebook, both using the same small deterministic
  OHLCV fixture and covering the same first scan. The Jupyter notebook should run
  headlessly in CI with `jupyter nbconvert --execute --to notebook --inplace`; the
  marimo notebook should run with `marimo export html` or the installed non-browser
  execution command. If marimo cannot execute headlessly in the CI environment,
  keep its source checked in and add a syntax/import smoke check, but do not claim
  rendered output was verified. Link both notebooks from the tutorial and examples
  reference pages.

## Verification commands for the final integration card

```sh
uv sync --group docs
uv run pytest tests/ -q
uv run ruff check src tests
for f in docs/examples/*.py; do uv run python "$f" >/dev/null; done
uv run jupyter nbconvert --execute --to notebook --inplace docs/notebooks/01_first_scan.ipynb
uv run marimo export html docs/notebooks/02_first_scan_marimo.py -o /tmp/scanlang-marimo.html --force
uv run zensical build
```

Expected result: tests pass, ruff is clean, every advertised example exits zero, and
Zensical reports `No issues found` with a generated `site/index.html` containing the
home article rather than the 404 template.
