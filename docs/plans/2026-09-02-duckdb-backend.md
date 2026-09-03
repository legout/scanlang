# Plan — scanlang duckdb backend + indicator expansion

Date: 2026-09-02. Status: proposal, awaiting decision confirmations.
Input: `research/talib_benchmark_2026-09-02.{md,html}` (marketdata-screens repo),
`docs/RESEARCH_DUCKDB.md` (earlier verdict, now partially superseded).

## What the benchmark settled

1. **The viable duckdb indicator form is `t_*` scalar** (list-collect per symbol →
   indicator over the list → unnest back). Exact full-history values, one O(N)
   pass, and the **fastest engine measured**: 3.8 s for 6 indicators over the
   full 25.7M-row universe (polars native 6.0 s, polars + py-talib 4.6 s).
2. **`ta_*` window-aggregate form is rejected for scans**: recomputes per frame,
   30–35× slower (125 s full history). Dashboard-only use case.
3. **The talib extension covers what polars cannot express**: `ht_trendline`
   (impossible as pl.Expr, 737 ms as `t_*`), 49 candle patterns, efficient
   `aroon` (0.93 s vs 271.7 s polars python-fallback).
4. **Value parity where both engines emit**: RSI agrees to <0.5 points
   everywhere measured. Scanner hit-count differences come from null semantics:
   py-talib NaN-poisons after one null close (2,884 symbols); duckdb frames
   skip nulls and still emit values. Minervini scan matched exactly (493=493).
5. **WASM is out for talib**: the extension excludes all wasm targets
   (`wasm_mvp;wasm_eh;wasm_threads` in its descriptor). Browser-local scans
   would need the hand-rolled SQL subset (the 2–3 week path from
   `RESEARCH_DUCKDB.md`) — deferred.

This revises the `RESEARCH_DUCKDB.md` "no SQL backend" verdict: that verdict
assumed hand-rolled CTEs for ema/rsi/atr (the expensive part). The community
talib extension removes exactly that cost, and the `t_*` form beats polars on
speed. The "polars has no Expr→SQL translator" argument is unchanged — we
compile the IR dict directly, never polars expressions.

## Proposed scope

### S1 — duckdb backend (`scanlang.duckdb_sql`, targets 0.3.0)

New module, no changes to the existing polars path.

```python
def compile_sql(
    scan_def: dict, *, relation: str,
    catalog: dict = PROPERTY_CATALOG,
    partition: str = "symbol", order_column: str = "session",
) -> tuple[str, list]: ...          # parameterized SQL + params

def apply_sql(
    con, scan_def: dict, *, relation: str, ...
) -> "pl.DataFrame": ...            # eager; duckdb has no polars-lazy plan
```

- **Indicator lowering, two tiers:**
  - `sma, rmin, rmax, shift` → duckdb-native window functions
    (`AVG/MIN/MAX/LAG OVER (PARTITION BY p ORDER BY o ROWS …)`). Semantics
    identical to polars — cross-engine parity for free, no extension needed
    for basic scans.
  - `ema, rsi, atr` → `t_*` scalar form (per-symbol list CTE + unnest, join
    back on `(partition, order_column)`). Emits TA-Lib values (Wilder); the
    polars engine is aligned to the same definitions (see Q1), so values
    converge after warm-up.
- Groups/ops/arithmetic/`between`/`in`/`contains`/`cross_above`/`cross_below`/
  `order_by`/`limit` → mechanical SQL with `?` params (prepared-statement
  style; nothing string-interpolated, same contract as `compile()`).
- Nested indicator operands (`sma(rsi(close,14),5)`): needs one probe — does
  the extension accept `t_sma(t_rsi(list(close), 14), 5)` (list-in/list-out
  nesting)? If yes: trivial. If no: stage nested calls as successive list CTEs.
- Registry: parallel `SQL_INDICATORS` dict in the new module
  (`name -> (arg_spec, sql_builder, required_cols)`), mirroring the
  `INDICATORS` contract. `INDICATORS` itself stays polars-only; the freeze's
  entry shape is untouched.
- `validate()` reuse as-is for the shared surface.
- **Tests:** golden cross-engine suite — same scan_defs through both engines on
  a deterministic synthetic frame: sma-family identical, ema converged at
  tail, hit counts equal for sma-only scans; plus one lake-scale integration
  test reproducing the benchmark's Minervini 493=493. ruff clean, full pytest.
- **Docs:** reference page, how-to (duckdb backend), and a "revisited 2026-09"
  section in the why-no-duckdb explanation linking the benchmark report.
- Effort: ~1–1.5 weeks (the benchmark already proved every SQL pattern).

### S2 — indicator expansion (folds into 0.3.0 unless Q4 says split)

- Corpus gap (polars + SQL both): `adr(n)`, `roc(n)`, `natr(n)`, `slope(n)`
  — polars-native builders exist for all four; SQL side has `t_roc`,
  `t_natr`, `t_linearreg_slope`; `adr` = sma(TR/close·100) native both sides.
- talib-only names (`macd, bbands, adx, aroon, cdlengulfing, ht_trendline`):
  available on the duckdb engine only. `validate(scan_def, *, engine=...)`
  gains an additive kwarg (default `"polars"`, backward compatible — freeze
  allows additive API kwargs, no IR change). A scan_def using `adx` validates
  for `engine="duckdb"` and fails validation for `"polars"` with a clear error.
- Each new indicator: arg_spec + builder + unit test vs hand-computed values,
  plus 2–3 corpus scans as parse/compile tests (existing convention).

### S3 — screens integration (after S1, marketdata-screens repo)

Run scans as SQL **inside the lake connection** (hotlake `connect()`): the
indicator computation pushes down to the lake host; only hits cross the
network — no 25.7M-row pull for a full-universe scan. Lab UI gets an engine
switch (polars local replica / duckdb lake). Measure against the benchmark's
local-parquet numbers; expected: similar compute time + near-zero transfer.

### Deferred (recorded, not carded)

- **DuckDB-Wasm / browser engine**: talib is excluded from all wasm targets.
  The hand-rolled-SQL subset from `RESEARCH_DUCKDB.md` §2.1 remains the only
  browser path (~2–3 weeks). Revisit only if browser-local scans become a
  product requirement; note that S1's two-tier lowering already isolates the
  extension-dependent indicators, so a "portable SQL" lowering tier could be
  added later without re-architecting.
- **ta_* window form**: rejected by the benchmark (30–35× slower).

## Decisions (confirmed 2026-09-02)

- **Q1 — align polars `INDICATORS` to TA-Lib definitions.** Cross-engine
  identical math. Concrete change: polars `rsi` and `atr` move from simple
  rolling means (Cutler) to Wilder smoothing (`ewm_mean(alpha=1/n)`); `ema`
  already uses the same recursion as TA-Lib (k=2/(n+1)), only the seed
  differs. TA-Lib seeds EMA/RSI/ATR with an SMA of the first n values, polars
  seeds from the first value — exact match is not expr-expressible, so the
  contract is: **values converge after warm-up (typically ~3–4× period);
  golden suite asserts |diff| < 0.01 at mature bars**, early-window divergence
  documented. Accepted consequence: marketdata-screens `score_bars` values
  (RSI thresholds 70/85, atr_ratio) shift slightly; near-threshold scores can
  flip. Called out in the 0.3.0 release notes.
- **Q2 — packaging:** `scanlang.duckdb_sql` module + `duckdb` extra
  (`duckdb>=1.5`); `apply_sql` ensures the extension
  (`INSTALL talib FROM community; LOAD talib`) on the connection.
- **Q3 — talib-only indicators ship in 0.3.0** with engine-aware
  `validate(scan_def, *, engine="polars")` (additive kwarg, backward
  compatible).
- **Q4 — S1 + S2 land together as 0.3.0.**

## Execution model

Serial card chain on the `scanlang` kanban board after decisions land:
C1 align polars `INDICATORS` to TA-Lib recursion + plan-file commit (coder,
review lane) → C2 S1 backend impl (coder, review lane) → C3 S2 indicators
incl. engine-aware validate (coder, review lane) → C4 S1+S2 docs (scribe) →
C5 bump/publish 0.3.0 with release notes calling out the alignment (coder) →
C6 S3 screens integration (coder, marketdata-screens repo).
No dedicated review cards; every implementer card calls
`kanban_request_review(reviewers=reviewer)` explicitly.
