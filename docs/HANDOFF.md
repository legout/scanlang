# HANDOFF — scanlang (screener/scanner split-off)

Origin: `~/projects/marketdata-screens` session 2026-08-30.
Source modules to extract:
- `src/marketdata_screens/scanner.py` (233 lines — `fetch_recent_bars`, `score_bars`; note `fetch_recent_bars` moves to hotlake, only scoring/indicator side stays)
- `src/marketdata_screens/web/scan_compiler.py` (174 lines — `_collect`, `_errors`, `compile(scan_def) -> pl.Expr`, `validate`, `apply`)
- `src/marketdata_screens/web/preview.py` (169 lines — `scan_def_from_signals`, `preview(scored, scan_def)`)

## What this lib is

The screen language: **signal dict (IR) → validated `pl.Expr` → lazy pushdown** over any `LazyFrame` source (hotlake parquet/duckdb, direct polars scan, arrow table). The IR is the frozen contract — the existing structured dict from the lab UI, NOT text. A text DSL (`close > sma(50) and vol > 3*avg(vol,20)`) is a *planned optional front-end* that parses to the same IR — build only if hand-written scans actually happen (grilling first).

## Architecture position (agreed in session)

- scanlang sits between klinepy (rendering) and hotlake (data). It owns compile/validate/apply + scoring/indicators. It does NOT own connections, caching, or data fetching — those are hotlake's.
- **Lazy contract:** lib never takes/returns eager frames at internal boundaries. Inputs: `LazyFrame` / `pl.Expr`-compatible sources. Collect only at the caller's edge (`.collect()` / `.pl()` / `.arrow()` at the API boundary).
- Indicators stay polars-native in the hot path. ta-lib = optional extra (`[project.optional-dependencies] talib`), for exact-value parity on collected results — it cannot participate in lazy pushdown.
- duckdb path: `duckdb.sql("...").pl()`/`.arrow()` interop — exprs compile once, run on either engine where each is stronger. Research task (below).

## Design rules

- IR freeze before any DSL/parser work (finvizp precedent: grilling → freeze → implement).
- `validate()` returns human-facing error strings keyed to fields — the lab UI surfaces them inline today; keep that contract.
- py3.11+, polars>=1.44 (matches marketdata-screens venvs).

## Consumers

- marketdata-screens lab/preview/run/backtest/CSV export (all currently call `preview()` / `compile()` / `apply()`)
- marimo notebooks (scan over the lake interactively)

## First tasks

1. Grilling/design-freeze session on IR + DSL scope (user convention: freeze before implement).
2. Extract `scan_compiler.py` + `preview.py` as-is; they are app-agnostic already.
3. Split `scanner.py`: scoring → scanlang; `fetch_recent_bars` → hotlake.
4. Research: polars-expr → duckdb SQL translation (sqlglot is already in marketdata-screens deps) vs filter-in-polars-after-duckdb-scan.
