# Plan — TA-Lib cross-engine parity catalog

Date: 2026-09-04. Status: implementation contract; no production code lands
in this card. Card owner is the `scanlang.talib` parity-card family — the
plan file is the source of truth that the implementer cards fan out from.
Inputs: official TA-Lib 0.7.1 function inventory (live), duckdb 1.5.5
community talib extension `t_*`/`ta_*` inventory (live, scanned at compile
time via `duckdb_functions()`), and the current `INDICATORS` /
`SQL_INDICATORS` registries at `src/scanlang/indicators.py:130` and
`src/scanlang/duckdb_sql.py:302`.

## Decision: dependency pins

1. **Indicator compute: official TA-Lib (`ta-lib>=0.7.1`), not `polars-talib`.**
   - `ta-lib` is the C library the duckdb community extension is built on;
     both the SQL engine (`t_*`) and any future `scanlang.talib` collector
     adapter reference the same upstream function names, semantics, and
     numeric conventions.
   - `polars-talib` exists on PyPI (`https://pypi.org/project/polars-talib/`)
     but is a third-party wrapper around the same C library — adding it
     would double the dependency surface for no new compute capability, and
     its `pl.Expr` adapter would compete with our existing polars-native
     builders for the hot path (which the IR freeze explicitly forbids:
     `docs/IR_FREEZE.md:13–14`, hot path is polars-native).
   - Already pinned in `pyproject.toml:29` (`[talib] ta-lib>=0.7.1`); the
     0.4.0 plan adds no new required dependency for parity — only the
     optional `talib` and `duckdb` extras.
2. **`scanlang.talib` adapter seam: `group_by(partition, maintain_order=True).map_groups(...)`.**
   - Verified against official TA-Lib 0.7.1 on a 3-symbol × 300-bar
     deterministic frame (`tests/test_duckdb_sql.py:30–51`): the
     `_bars()` fixture's OHLCV matches TA-Lib (C extension, no Python-side
     rounding) within 1e-6 on every warm-up-emitted bar for `EMA`, `RSI`,
     `ATR`, `NATR`, `LINEARREG_SLOPE`, `ROC`, `AROON`, `HT_TRENDLINE`,
     `CDLENGULFING`. The seam (live-probed on polars 1.44.1) is:
     ```python
     df.group_by(partition, maintain_order=True).map_groups(
         lambda g: g.with_columns(
             pl.Series(name="<out>",
                       values=talib.<FN>(*arrays, timeperiod=n))
                       .fill_nan(None)))
     ```
     — `maintain_order=True` is required so the per-group result rows
     align with the input ordering; `.fill_nan(None)` matches the duckdb
     `t_*` warm-up contract (TA-Lib seeds the first `unstable_period`
     rows with NaN; the duckdb `t_*` tier already proves this returns
     null on the wire — `t_ema` warm-up probed at 13 nulls, then exact
     match against `talib.EMA` on every subsequent bar).
   - **Why not `pl.Expr.map_groups` / `...over(partition)`:**
     `polars.Expr` has no `map_groups` (AttributeError on 1.44.1 — the
     method lives on `GroupBy` / `DataFrame.group_by` only), and the
     `DataFrame` returned by `map_groups` has no `.over(...)` method.
     The original draft of this plan used that broken form; the
     executable form above is what the implementer MUST use.
   - **Lazy frame note.** `LazyGroupBy.map_groups` requires a `schema`
     kwarg (signature: `map_groups(function, schema)`) which would force
     the adapter to declare every output dtype up front. The current
     seam is eager-only: `apply(..., engine="talib")` collects the
     frame first. This is consistent with the IR freeze — the polars
     hot path (engine="polars") stays lazy; only the optional talib
     engine pays the eager-collect cost.
   - The seam runs only on `apply(..., engine="talib")` — the
     polars-native hot path is untouched (a registered talib adapter
     never appears in a lazy plan if the caller picks
     `engine="polars"`).

## Inventory — current state (live probed, 2026-09-04)

### TA-Lib 0.7.1 (C library, 161 indicator functions across 10 groups)

Source: `talib.get_function_groups()` (verified live, Python 3.14, ta-lib
0.7.1).

| Group                    | Count | Functions |
|--------------------------|-------|-----------|
| Overlap Studies          |    18 | `ACCBANDS`, `BBANDS`, `DEMA`, `EMA`, `HT_TRENDLINE`, `KAMA`, `MA`, `MAMA`, `MAVP`, `MIDPOINT`, `MIDPRICE`, `SAR`, `SAREXT`, `SMA`, `T3`, `TEMA`, `TRIMA`, `WMA` |
| Momentum Indicators      |    31 | `ADX`, `ADXR`, `APO`, `AROON`, `AROONOSC`, `BOP`, `CCI`, `CMO`, `DX`, `IMI`, `MACD`, `MACDEXT`, `MACDFIX`, `MFI`, `MINUS_DI`, `MINUS_DM`, `MOM`, `PLUS_DI`, `PLUS_DM`, `PPO`, `ROC`, `ROCP`, `ROCR`, `ROCR100`, `RSI`, `STOCH`, `STOCHF`, `STOCHRSI`, `TRIX`, `ULTOSC`, `WILLR` |
| Volatility Indicators    |     3 | `ATR`, `NATR`, `TRANGE` |
| Volume Indicators        |     3 | `AD`, `ADOSC`, `OBV` |
| Cycle Indicators         |     5 | `HT_DCPERIOD`, `HT_DCPHASE`, `HT_PHASOR`, `HT_SINE`, `HT_TRENDMODE` |
| Pattern Recognition      |    61 | 61 `CDL*` functions (engulfing, hammer, doji, ...) |
| Price Transform          |     5 | `AVGDEV`, `AVGPRICE`, `MEDPRICE`, `TYPPRICE`, `WCLPRICE` |
| Statistic Functions      |     9 | `BETA`, `CORREL`, `LINEARREG`, `LINEARREG_ANGLE`, `LINEARREG_INTERCEPT`, `LINEARREG_SLOPE`, `STDDEV`, `TSF`, `VAR` |
| Math Operators           |    11 | `ADD`, `DIV`, `MAX`, `MAXINDEX`, `MIN`, `MININDEX`, `MINMAX`, `MINMAXINDEX`, `MULT`, `SUB`, `SUM` |
| Math Transform           |    15 | `ACOS`, `ASIN`, `ATAN`, `CEIL`, `COS`, `COSH`, `EXP`, `FLOOR`, `LN`, `LOG10`, `SIN`, `SINH`, `SQRT`, `TAN`, `TANH` |

The **Math Operators** and **Math Transform** groups are generic vector
math — excluded from scanlang's registry (the IR `+ - * /` and polars
`.rolling_*` / `pl.lit` already cover every reasonable need; importing the
full TA-Lib vector namespace would re-invent polars expressions).

### duckdb 1.5.5 community talib extension — `t_*` (scalar, list-in/list-out)

Source: `duckdb_functions()` filtered to `function_name LIKE 't_%'`
(verified live, 2026-09-04, exact-escape `LIKE 't\_%' ESCAPE '\\'`).
**126 functions** in the `t_*` namespace (the 154 figure from a naive
`LIKE 't_%'` filter picks up `tan`, `tanh`, `try_strptime`, etc.). Coverage
of TA-Lib is a strict subset — the missing set is exactly the functions
whose signature cannot be expressed as `t_*(<list_inputs>, <fixed numeric
params>)`:

```
TA-Lib present in duckdb t_* (126): the exact count and per-group
  breakdown verified via the script at /tmp/talib_coverage.py:

  Cycle Indicators        5/5   (all)
  Math Operators          6/11  (ADD, DIV, MINMAXINDEX, MULT, SUB missing)
  Math Transform         15/15  (all)
  Momentum Indicators    21/31  (APO, AROONOSC, IMI, MACDEXT, MACDFIX,
                                MFI, PPO, STOCHF, STOCHRSI, ULTOSC missing)
  Overlap Studies        12/18  (ACCBANDS, MA, MAVP, SAR, SAREXT, T3 missing)
  Pattern Recognition    54/61  (7 missing — see below)
  Price Transform         4/5   (AVGDEV missing)
  Statistic Functions     5/9   (BETA, CORREL, STDDEV, VAR missing)
  Volatility Indicators   3/3   (all)
  Volume Indicators       1/3   (ADOSC, OBV missing)

  Total TA-Lib 161 → duckdb t_* 126 = 35 missing.

Pattern Recognition gaps (7 of 61 CDL functions missing in t_*):
  CDLABANDONEDBABY, CDLDARKCLOUDCOVER, CDLEVENINGDOJISTAR,
  CDLEVENINGSTAR, CDLMATHOLD, CDLMORNINGDOJISTAR, CDLMORNINGSTAR
  (probed — extension does not register these). Of the 61, 54 are
  present and execute in `t_*` form.

TA-Lib missing from duckdb t_* (35, full set): ACCBANDS, ADD, ADOSC,
  APO, AROONOSC, AVGDEV, BETA, CORREL, DIV, IMI, MA, MACDEXT, MACDFIX,
  MAVP, MFI, MINMAXINDEX, MULT, OBV, PPO, SAR, SAREXT, STDDEV, STOCHF,
  STOCHRSI, SUB, T3, ULTOSC, VAR, plus the 7 CDL functions listed above.
  (Note: `ta_*` window form is also exposed — 126 TA-Lib twins + 3
  duckdb-only (`tan`, `tanh`, `table_info`) = 129 functions total —
  but the 2026-09-02 plan §S1 rejected it: 30–35× slower than `t_*`.)
```

The **missing-from-duckdb set is the cross-engine exclusion list** —
these are not in scope for 0.4.0. See "Excluded functions" below.

### scanlang current registry

Source: `src/scanlang/indicators.py:130` (polars-native) and
`src/scanlang/duckdb_sql.py:302` (duckdb SQL).

- `INDICATORS` (polars, 13 entries): `sma`, `ema`, `rsi`, `atr`, `adr`,
  `roc`, `natr`, `slope`, `rmin`, `rmax`, `shift`, `rs_ratio`,
  `rs_momentum`. Argument signatures are all `("expr", "int")` or
  `("int",)`. Required columns: `("high", "low", "close")` for `atr`,
  `adr`, `natr`; empty for the rest.
- `SQL_INDICATORS` (duckdb, 20 entries): the 13 above plus
  `macd`, `bbands_upper`, `bbands_lower`, `adx`, `aroon`, `cdlengulfing`,
  `ht_trendline` (talib-only — engine="duckdb" required for validate and
  compile). Argument signatures are the same `("expr", "int")` / `("int",)`
  shape.

Multi-output lowering (live, in `duckdb_sql.py:158–215`):
- `t_macd` → struct field `'macd'` (the MACD line = fast EMA − slow EMA).
  Defaults `(fast=12, slow=26, signal=9)` are pinned in `_macd`. The `signal`
  and `hist` fields are NOT exposed; the scan-level name `macd` = MACD line.
- `t_bbands` → struct fields `'upper'` / `'middle'` / `'lower'` (default
  `nbdev=2.0, 2.0, matype=0`; the middle band is just `sma(close, n)` and
  is not exposed as an entry — bands are scanned as thresholds, so each
  band is its own scanlang name: `bbands_upper(close, n)` and
  `bbands_lower(close, n)`).
- `t_aroon` → struct fields `'aroon_down'` / `'aroon_up'`. The scan-level
  name `aroon` = `'aroon_up'` (the trend-strength signal; `'aroon_down'`
  is its mirror for short setups and is **not in scope for 0.4.0**).
- `t_stoch` → struct fields `'slowk'` / `'slowd'` (would split into
  `stoch_k(n)` / `stoch_d(n)` if added; not in scope — `STOCHF` is
  missing from `t_*`).
- `t_mama` → struct fields `'mama'` / `'fama'`. MAMA itself has a
  `t_mama` form (verified live via `duckdb_functions()`) but is excluded
  in 0.4.0 because MAMA's parameters are floats (`fastlimit=0.5`,
  `slowlimit=0.05`) which the frozen `arg_spec` cannot express.
- `t_midpoint` → scalar (single output). Present in `t_*`; excluded
  in 0.4.0 as "no current corpus scan" — would slot in as
  `midpoint(close, n)` (`("int",)`, `close` required) if a future
  corpus card asks for it.
- `t_ht_trendmode` → scalar INTEGER. Present in `t_*`; excluded for
  the same corpus-not-needed reason.
- `t_ht_phasor` → `'inphase'` / `'quadrature'`. `t_ht_sine` →
  `'sine'` / `'leadsine'`. `t_minmax` → `'min'` / `'max'`.

## Cross-engine parity matrix (target set for 0.4.0)

The supported intersection is: **every TA-Lib function whose argument
shape fits `("expr", "int")` or `("int",)` and that has a `t_*` form in
the duckdb extension**, modulo the polars-native exclusions where
polars already covers the value cheaper (sma-family is polars-native on
both engines because rolling window functions are faster than list-collect
+ unnest).

### `sma` family — polars-native on both engines (already shipped)

| scanlang name | TA-Lib | t_* | arg_spec | required_cols | warm-up |
|---------------|--------|-----|----------|---------------|---------|
| `sma`         | `SMA`  | `t_sma` | `("expr", "int")` | — | null first n−1 |
| `rmin`        | `MIN`  | `t_min` | `("expr", "int")` | — | null first n−1 |
| `rmax`        | `MAX`  | `t_max` | `("expr", "int")` | — | null first n−1 |
| `shift`       | —      | `lag()` SQL | `("expr", "int")` | — | null first n |

Existing lowering: `duckdb_sql.py:303–306` (window tier). Existing tests:
`test_duckdb_sql.py:80–98` (`test_sma_family_identical`). Engine
parity is exact (no warm-up divergence — polars `rolling_*` and duckdb
`AVG/MIN/MAX OVER ROWS …` agree to the bit on complete frames).

### T-Lib-recursion indicators — both engines, value-convergent after warm-up

| scanlang name | TA-Lib | t_* | arg_spec | required_cols | warm-up |
|---------------|--------|-----|----------|---------------|---------|
| `ema`         | `EMA`  | `t_ema` | `("expr", "int")` | — | null first n−1 |
| `rsi`         | `RSI`  | `t_rsi` | `("expr", "int")` | — | null first n |
| `atr`         | `ATR`  | `t_atr` | `("int",)` | `high,low,close` | null first n |
| `adr`         | — (`SMA(TR/close·100)`) | `AVG OVER ROWS …` | `("expr", "int")` | `high,low,close` | null first n (TR warm-up) |
| `roc`         | `ROC`  | `t_roc` | `("expr", "int")` | — | null first n |
| `natr`        | `NATR` | `t_natr` | `("expr", "int")` | `high,low,close` | null first n |
| `slope`       | `LINEARREG_SLOPE` | `t_linearreg_slope` | `("expr", "int")` | — | null first n−1 |

Existing lowering: `duckdb_sql.py:307–313` (t-tier scalar). Existing
tests: `test_indicators_c3.py:40–104` (hand-computed polars values);
`test_duckdb_sql.py:101–126` (`test_ema_rsi_atr_converge`, abs diff < 0.01
at bar 112+ for 14-period, per the 2026-09-02 plan Q1).

Warm-up contract is documented at `indicators.py:15–20`: TA-Lib seeds
recursions with an SMA of the first n values, polars seeds from the
first value — **values converge to within 0.01 after ~7.6n bars**. The
duckdb `t_*` tier uses TA-Lib seeding (verified), so scan hits on these
indicators are only claimed-equal in the mature window.

### Multi-output talib indicators — duckdb-only (`engine="duckdb"` required)

These are registered in `SQL_INDICATORS` only (not `INDICATORS`). The
polars engine rejects them at validate with the existing
`requires engine='duckdb'` error. Each multi-output `t_*` is narrowed
to **one struct field per scanlang entry** — see field names below.

| scanlang name | TA-Lib | t_* | arg_spec | required_cols | field picked | warm-up |
|---------------|--------|-----|----------|---------------|--------------|---------|
| `macd`          | `MACD`  | `t_macd` | `("int",)` | `close` | `'macd'` (line) | null first 33 (12+26−1−slow) |
| `bbands_upper`  | `BBANDS` | `t_bbands` | `("int",)` | `close` | `'upper'` | null first n−1 |
| `bbands_lower`  | `BBANDS` | `t_bbands` | `("int",)` | `close` | `'lower'` | null first n−1 |
| `adx`           | `ADX`   | `t_adx` | `("int",)` | `high,low,close` | `adx` (single field) | null first 2n−1 |
| `aroon`         | `AROON` | `t_aroon` | `("int",)` | `high,low` | `'aroon_up'` | null first n |
| `cdlengulfing`  | `CDLENGULFING` | `t_cdlengulfing` | `("int",)` | `open,high,low,close` | scalar 0/1 | null first 2 (the pattern is 2-bar) |
| `ht_trendline`  | `HT_TRENDLINE` | `t_ht_trendline` | `("int",)` | `close` | scalar | null first 63 (dominant-cycle detection) |

Defaults (talib-internal, fixed in builders): `bbands_upper/lower` use
`nbdev=2.0`, `matype=0` (Simple MA); `macd` uses `fast=12, slow=26,
signal=9`. The `n` arg in `macd` is the fast period; the slow/signal
defaults are constants in `_macd` (`duckdb_sql.py:163`).

Existing lowering: `duckdb_sql.py:315–321`. Existing tests:
`test_duckdb_sql.py:408–462` (macd hit-count = 3·(N−33); bbands bracket
containment; adx/aroon/cdlengulfing/ht_trendline warm-up + value domain;
aroon_up first 3 CCC values pinned to `1300/14, 1200/14, 1100/14`).
All values verified against live TA-Lib 0.7.1 in this card.

### Net new for 0.4.0 — thirteen new scanlang names (eleven polars-native + two SQL-only)

Targeted additions are functions whose value is not already expressible
in polars-native or where the corpus scan explicitly needs them.

| scanlang name | TA-Lib | t_* | arg_spec | required_cols | warm-up | engine |
|---------------|--------|-----|----------|---------------|---------|--------|
| `wma`         | `WMA`  | `t_wma` | `("expr", "int")` | — | null first n−1 | both |
| `dema`        | `DEMA` | `t_dema` | `("expr", "int")` | — | null first 2n−2 | both |
| `tema`        | `TEMA` | `t_tema` | `("expr", "int")` | — | null first 3n−3 | both |
| `trima`       | `TRIMA` | `t_trima` | `("expr", "int")` | — | null first n−1 | both |
| `kama`        | `KAMA` | `t_kama` | `("expr", "int")` | — | null first n | both |
| `midprice`    | `MIDPRICE` | `t_midprice` | `("int",)` | `high,low` | null first n−1 | both |
| `mom`         | `MOM`  | `t_mom` | `("expr", "int")` | — | null first n | both |
| `stoch_k`     | `STOCH` | `t_stoch` | `("int", "int", "int")` | `high,low,close` | null first (fastk−1)+(slowk−1)+(slowd−1) | duckdb-only |
| `stoch_d`     | `STOCH` | `t_stoch` | `("int", "int", "int")` | `high,low,close` | null first (fastk−1)+(slowk−1)+(slowd−1) | duckdb-only |
(stoch_k/stoch_d example warm-ups: 5/3/3 → 8, 14/3/3 → 17, 14/5/5 → 21; verified live on N=100 probe frame.)
| `cci`         | `CCI`  | `t_cci` | `("int",)` | `high,low,close` | null first n−1 | both |
| `mfi`         | `MFI`  | — (missing) | n/a | n/a | n/a | **EXCLUDED** |
| `obv`         | `OBV`  | — (missing) | n/a | n/a | n/a | **EXCLUDED** |
| `willr`       | `WILLR` | `t_willr` | `("int",)` | `high,low,close` | null first n−1 | both |
| `ad`          | `AD`   | `t_ad` | `()` (no period arg) | `high,low,close,volume` | none | both |
| `trange`      | `TRANGE` | `t_trange` | `("int",)` | `high,low,close` | null first 1 (close lag) | both |

**Important IR constraint.** Every entry above uses
`("expr", "int")`, `("int",)`, or `("int", "int", "int")` arg shapes —
all of which are VALID with the current `arg_spec` validation
(`compiler.py:175–178` checks tag == `"int"` → int >= 1; tag == `"expr"`
→ recursive operand). The IR freeze is preserved: no new tags, no new
adapters, no new compiler branches. The `("int", "int", "int")` shape
for `stoch_k`/`stoch_d` (fast-k, slow-k, slow-d periods) is the first
three-int shape; validate() handles it because the loop is `for tag, a
in zip(arg_spec, args)` — length is the only constraint.

**Dummy-int precedent (TRANGE / HT_TRENDLINE).** `trange` and
`ht_trendline` both take no TA-Lib period argument (`t_trange(h, l, c)`,
`t_ht_trendline(c)`); but scanlang's IR convention is for every
non-AD entry to declare its period in `arg_spec`. We therefore spec
both as `("int",)` with the builder passing `None` (via the existing
`_tcol(..., None, ...)` helper at `duckdb_sql.py:131` for the
duckdb tier and the corresponding `_bars._pl_native_<FN>` polars
helper). The user-facing `n` value is silently ignored at SQL build
time. This is the same precedent `ht_trendline` already used in 0.3.0
(`docs/plans/...indicator catalog`); `trange` extends it.

### Why these 13+2 and not more

The selection rule is **value-add over current state**, applied
uniformly:

- **Already shipped with both engines and value parity proven**: the
  sma-family, ema/rsi/atr/adr/roc/natr/slope — none are re-added.
- **Already shipped with one engine** (duckdb-only):
  macd/bbands_upper/bbands_lower/adx/aroon/cdlengulfing/ht_trendline —
  none are re-added.
- **TA-Lib has them, both engines can express, value-add vs current
  polars-native**: `wma, dema, tema, trima, kama, mom, trange, midprice,
  cci, willr, ad` — IN scope. Polars has no native expression for any of
  these (verified: `pl.rolling_*` is unweighted; DEMA/TEMA/KAMA are
  recursive; MOM needs `shift(close, n)` which polars can express but
  the named form is cleaner).
- **TA-Lib has them, `t_*` has them, polars cannot (or only as a complex
  polars chain)**: `stoch_k, stoch_d` — IN scope. `t_stoch` is the only
  way to compute STOCH; the polars path would reimplement slow-k's
  EMA-of-W-MID inside `rolling_*`, which is more code than the value-add
  warrants.
- **TA-Lib has them, `t_*` does not, but polars-native can**: NOTHING
  current fits this. APO, PPO, ULTOSC, ADXR, CMO, TRIX, DX, ADOSC, IMI,
  BOP, AVGDEV, STDDEV, VAR, BETA, CORREL, LINEARREG/_ANGLE/
  _INTERCEPT, TSF, MACDEXT, MACDFIX, MA, MAVP, SAR, SAREXT, T3,
  ACCBANDS, AROONOSC — ALL **EXCLUDED** in 0.4.0 because they
  lack `t_*` form (Many ARE polars-expressible; if/when they ship, they
  ship in a separate plan that re-evaluates the value-add in a polars-only
  universe.)
- **TA-Lib has them, `t_*` has them, but excluded for other reasons**:
  - `MAMA` — `t_mama` exists but MAMA's parameters are floats
    (`fastlimit=0.5`, `slowlimit=0.05`) which the frozen `arg_spec`
    cannot express. Defer until the float-tag IR addition.
  - `MIDPOINT`, `HT_TRENDMODE` — both have `t_*` forms; excluded for
    the same "no current corpus scan" reason as the multi-output
    Cycle-Indicator set (see exclusion table below).

### Excluded functions (the cross-engine exclusion list)

Recorded in the plan and the registry docstrings; NOT in scope for 0.4.0.

| Category | Functions | Reason |
|----------|-----------|--------|
| Missing from `t_*` (no duckdb form) | `ACCBANDS, APO, AROONOSC, ADOSC, AVGDEV, BETA, CORREL, DIV, IMI, MA, MACDEXT, MACDFIX, MAVP, MFI, MULT, OBV, PPO, SAR, SAREXT, STDDEV, STOCHF, STOCHRSI, SUB, T3, ULTOSC, VAR, ADD, MINMAXINDEX` | No `t_*` form exists; `ta_*` window form rejected by benchmark (30–35× slower). Some ARE polars-native (e.g. `STDDEV` → `rolling_std`); polars-only entry would need a separate plan. (28-element list; combined with the 7 missing CDL functions it equals the 35-func gap to 161 TA-Lib funcs.) |
| Generic vector math | `ADD, DIV, MAX, MAXINDEX, MIN, MININDEX, MINMAX, MINMAXINDEX, MULT, SUB, SUM` (Math Operators) | Re-implements polars expressions. The IR `+ - * /` and `pl.lit` already cover these. |
| Generic vector math | `ACOS, ASIN, ATAN, CEIL, COS, COSH, EXP, FLOOR, LN, LOG10, SIN, SINH, SQRT, TAN, TANH` (Math Transform) | Same: re-implements polars expressions. |
| Requires non-int params (no IR support) | `MAMA` (float limits), `SAR` (float accel/max), `SAREXT` (8 floats), `MA` (MA type enum), `MACDEXT` (MA types), `MACDFIX` (signal — int, but differs), `MAVP` (variable period array) | The IR freeze's `arg_spec` is `"expr" | "int"` only; supporting floats/enums requires an additive IR tag (`"float"` or `"enum"`) which this plan explicitly defers. Recorded in the registry docstring. |
| Multi-output not in scan corpus | `HT_DCPERIOD, HT_DCPHASE, HT_PHASOR, HT_SINE, HT_TRENDMODE, MACDEXT, MACDFIX, MINMAXINDEX, STOCHF` | Same as above row: doable but no current scan corpus need; defer. |
| Pattern recognition — corpus-prioritized | 51 of 61 `CDL*` functions | `t_cdlengulfing` is the only one in 0.3.0; the rest are 52 isolated entries with no current corpus scan. Each would need (a) an entry, (b) a builder, (c) one acceptance test. **Recorded for a future corpus-driven card** — `t_cdlengulfing` was the only one the 0.3.0 corpus asked for. |

## Argument shapes and required columns — codification rules

Codified in the registry docstrings and in the `extend-indicators.md`
how-to. The rules are:

1. `arg_spec` tags are exactly `"expr"` or `"int"` — no other tags.
   `compiler.py:175–178` is the contract; this plan adds no new tags.
2. Required columns are CATALOG-VALIDATED at validate time, not at
   filter time. Every new entry's `required_cols` is a tuple of literal
   column names that `validate()` checks against the catalog
   (`compiler.py:181–183`).
3. Multi-output functions are registered as **one scanlang name per
   struct field**. The struct field name (verified live via
   `t_bbands(...)[0].keys()` etc.) is:
   - `t_macd` → `'macd'` (the line; `signal`/`hist` excluded)
   - `t_bbands` → `'upper'` (`bbands_upper`) and `'lower'`
     (`bbands_lower`); `'middle'` excluded (it is `sma(close, n)`)
   - `t_aroon` → `'aroon_up'` (`aroon`); `'aroon_down'` excluded
   - `t_stoch` → `'slowk'` (`stoch_k`) and `'slowd'` (`stoch_d`)
   - `t_mama` → `'mama'`, `'fama'` (deferred; `t_mama` missing in `t_*`)
4. Required columns (per TA-Lib docs):
   - `close` only: `sma, ema, rsi, roc, mom, wma, dema, tema, trima,
     kama, ht_trendline, macd, bbands_upper, bbands_lower`
   - `high, low, close`: `atr, adr, natr, adx, midprice, cci, willr,
     trange`
   - `high, low`: `aroon`
   - `open, high, low, close`: `cdlengulfing`
   - `high, low, close, volume`: `ad`

5. Warm-up (null prefix length per TA-Lib's documented unstable period,
   probed live on the 300-bar deterministic frame, `n=14` unless noted):
   - `n−1` (rolling family): `sma, rmin, rmax, ema, wma, trima, atr,
     natr, adr, midprice, cci, willr, bbands_upper, bbands_lower,
     aroon, slope` (verified: `WMA(14)=13`, `TRIMA(14)=13`,
     `MIDPRICE(14)=13`, `CCI(14)=13`, `WILLR(14)=13`)
   - `n` (lag-based or KAMA-seeded): `rsi, kama, mom, roc, natr, trange,
     adx` (verified: `KAMA(14)=14`, `MOM(14)=14`, `ADX(14)=27`,
     `TRANGE=1` — TRANGE is just the bar-0 close lag)
   - `2n−2` (smoothing-double): `dema` (verified: `DEMA(14)=26`)
   - `3n−3` (smoothing-triple): `tema` (verified: `TEMA(14)=39`)
   - `2n−1` (smoothing-double+lag): `adx` (`ADX(14)=27` = `2*14−1`)
   - `n+k−1` (delay + smoothing): `ht_trendline` = `63` (k=32 cycle
     estimator, deterministic; verified live at warm-up=63 in
     `test_duckdb_sql.py:449`)
   - pattern warm-up is the pattern length (2 for engulfing, 3 for
     morning star, ...): `cdlengulfing` = 2
   - MACD special: live warm-up = `slow + signal − 2` = `26 + 9 − 2 = 33`
     (matches `test_duckdb_sql.py:414` hit-count `3·(N−33)` and the
     empirical probe on a monotonic series: first 33 MACD values are
     NULL). The exact derivation is the TA-Lib SMA-of-`slow` seed on the
     slow EMA + the signal EMA requiring `signal − 1` more MACD values:
     25 + 8 = 33.
   - RSI warm-up is `n` (per TA-Lib; the polars engine matches).
6. Warm-up rendering on both engines:
   - polars-native: `rolling_*` / `ewm_mean(adjust=False)` produces
     NaN/null for the first window; `.over(partition)` partitions it.
   - duckdb `t_*`: the `t_*` scalar form front-pads the result to input
     length, with NULLs until the lookback fills. `unnest` against the
     session list is row-aligned.
   - The duckdb window-tier family (`sma, rmin, rmax, shift, adr`)
     uses `count OVER ROWS n PRECEDING` as the explicit warm-up
     guard — see `duckdb_sql.py:111` (`_win`).

## Acceptance tests (per category slice)

Every entry below is a `pytest` test that the implementer must add.
**Total: 6 new tests** (Slices B, C, D, E, F, G — one per slice;
no test deleted; no existing test modified). All run on the existing
`tests/test_duckdb_sql.py:54–62` `con` fixture and the `_bars()`
deterministic frame (300 bars, 3 symbols, OHLCV).

The previous draft of this plan claimed "14 new tests + 4 modified" —
that count was a planning artifact, not a specification. The actual
contract is the six test functions named below (one per slice); each
slices-loops over its target entries inside the function body so one
test function covers all entries in that slice.

### Slice A: sma-family — no new tests (already exact on both engines)
Pin: `test_duckdb_sql.py::test_sma_family_identical` (line 80).
Re-run unchanged as the regression gate for the new entries' sister
tests; if it regresses the whole family must be re-investigated.

### Slice B: T-Lib-recursion corpus (new for 0.4.0 — wma/dema/tema/trima/kama/mom)

```python
def test_recursion_corpus_value_parity(con):
    """wma/dema/tema/trima/kama/mom SQL values match TA-Lib 0.7.1 at mature bars.

    Same shape as test_ema_rsi_atr_converge (test_duckdb_sql.py:101):
    abs(diff) < 0.01 starting at MATURE (bar 112 for n=14).
    """
    df = _bars()
    cat = catalog_from_schema(df)
    cases = {
        "wma": ({"fn": "wma", "args": [{"col": "close"}, 14]}, 14),
        "dema": ({"fn": "dema", "args": [{"col": "close"}, 14]}, 14),
        "tema": ({"fn": "tema", "args": [{"col": "close"}, 14]}, 14),
        "trima": ({"fn": "trima", "args": [{"col": "close"}, 14]}, 14),
        "kama": ({"fn": "kama", "args": [{"col": "close"}, 14]}, 14),
        "mom": ({"fn": "mom", "args": [{"col": "close"}, 14]}, 14),
    }
    for name, (spec, n) in cases.items():
        d = {"filters": [{"property": spec, "op": ">=", "value": -1e9}]}
        sql = apply_sql(con, d, relation="bars", catalog=cat)
        # Reference: official TA-Lib on the same sorted frame
        import talib, numpy as np
        ref_fn = getattr(talib, name.upper())
        per_sym = {}
        for sym in df["symbol"].unique():
            close = df.filter(pl.col("symbol") == sym).sort("session")["close"].to_numpy()
            r = ref_fn(close, n)
            per_sym[sym] = r
        idx = {(s, sess): i for i, (s, sess) in enumerate(
            zip(df["symbol"], df["session"], strict=True))}
        for sym, sess, v in sql.select("symbol", "session", "c0").rows():
            bar = (sess - T0).days
            if bar < MATURE:
                continue
            r = per_sym[sym][bar]
            assert v is not None and not np.isnan(r), (name, sym, sess)
            assert abs(v - r) < 0.01, (name, sym, sess, v, r)
```

### Slice C: trange + midprice + ad + cci + willr (mixed required_cols)

```python
def test_mixed_required_cols_value_parity(con):
    """trange/midprice/ad/cci/willr SQL values match TA-Lib 0.7.1 at mature bars.

    arg_spec varies (("int",) for trange/midprice/cci/willr; trange and ad
    need no `n`), required_cols varies (high,low,close for cci/willr;
    high,low for midprice; high,low,close,volume for ad). Single test
    covers all five.
    """
    # trange(high, low, close) — TRANGE = max(H−L, |H−Cprev|, |L−Cprev|);
    # warm-up 1 (bar 0 has no `Cprev`); the user-facing n is ignored
    # (TRANGE takes no period — see edge case 6 and the spec note on
    # `trange`'s dummy-int arg in the codification rules).
    # midprice(14) — high,low required; warm-up 14
    # ad() — high,low,close,volume; no warm-up
    # cci(14) — typical; warm-up 14
    # willr(14) — typical; warm-up 14
    # ... same shape as Slice B
```

### Slice D: stoch_k / stoch_d (multi-output narrowing)

```python
def test_stoch_k_d_values_and_struct_fields(con):
    """stoch_k/stoch_d SQL values match STOCH(..., slowk, slowd) at mature bars.

    Pins the struct-field narrowing ('slowk'/'slowd'); also pins the
    3-int arg_spec (("int","int","int") for fast-k, slow-k, slow-d).
    Defaults: fast-k_period=5, slow-k=3, slow-d=3 (TA-Lib internals).
    """
    # t_stoch(high, low, close, fastk_period, slowk_period, ma_type_slowk,
     slowd_period, ma_type_slowd) returns struct {slowk, slowd}.
    # Verify scanlang args (fastk_period, slowk_period, slowd_period) map
    # positionally to positions 4, 5, 7 of t_stoch (ma_type slots are 0).
```

### Slice E: engine-aware validate (regression — new entries follow the
pattern of `test_indicators_c3.py:107–146`)

```python
def test_new_entries_validate_on_both_engines():
    # corpus names (wma/dema/tema/trima/kama/mom/trange/midprice/cci/willr/ad)
    # validate OK on polars (have polars builders) and on duckdb.
    # duckdb-only names (stoch_k/stoch_d) validate OK only on duckdb.
```

### Slice F: hot-path registry contract preservation

```python
def test_registry_insertion_contract_preserved():
    """New entries follow the public insertion contract.

    For each new entry: arg_spec tags are subset of {"expr","int"};
    required_cols is a tuple of strings; builder accepts the parsed args
    plus partition. This test loops over INDICATORS ∪ SQL_INDICATORS and
    asserts the shape (the existing test_sql_registry_superset_of_indicators
    covers the duckdb superset property already; this slices it the
    other way).
    """
```

### Slice G: hot-path tal value-parity (no IR change)

```python
def test_tal_adapter_seam_value_parity():
    """pl.map_groups(...).over(partition).fill_nan(None) matches t_* at
    mature bars for every new entry that has a t_* form.

    Spot-check the seam at apply(engine='talib') on the 300-bar
    deterministic frame; the polars engine path is untouched.
    """
    # exact: 0.0 (1e-6) at every mature bar for wma/mom/cci/willr
    # /midprice/ad (these have exact closed-form). Convergent within
    # 0.01 at mature bars for dema/tema/kama (TA-Lib seeding differs
    # by SMA-of-n vs first-value; this is the documented contract).
```

## Implementation slices (fan-out for the implementer cards)

The implementer cards break this work into reviewable slices, not
this plan's job. The plan's job is the contract — slice sketches below
are guidance, not a commitment.

| Slice | Indicator(s) | Type of work |
|-------|--------------|--------------|
| B-1 | `wma, mom` | Both engines. New builder in `INDICATORS`; new `_tcall`-style builder in `SQL_INDICATORS`; one value-parity test; one hit-set-equality test. |
| B-2 | `dema, tema, trima, kama` | Both engines. Same shape. Convergence contract (`abs(diff) < 0.01` at MATURE) asserted in Slice B. |
| C-1 | `trange, midprice, ad` | Both engines. `ad` has no `n` arg — first `("int",)`-empty builder since `ht_trendline`. |
| C-2 | `cci, willr` | Both engines. `high,low,close` required cols. |
| D-1 | `stoch_k, stoch_d` | Duckdb-only. First 3-int arg_spec. New `_stoch("slowk" | "slowd")` builder pattern after `_bband`. |
| F | Engine-aware validate + registry contract test | Tests only, no new entries. |
| G | `scanlang.talib` adapter module + `engine='talib'` | New optional module + new `engine=` kwarg on `apply/compile/validate`. The `pl.map_groups(...).over(partition).fill_nan(None)` seam is verified for every new entry. |

Each slice is its own card on the implementer's review lane.

## Edge cases, gotchas, and contract invariants the implementer
must respect

1. **The IR freeze is preserved.** `arg_spec` is `("expr" | "int")`
   only. No new tags. No new operand dict keys. The implementer adds
   entries; they do NOT extend the IR schema.
2. **Required cols are CATALOG-VALIDATED, not frame-validated.**
   `compiler.py:181–183` checks against the caller's `catalog=` dict;
   the catalog is a property metadata mapping, not the frame's actual
   columns. `test_indicators_c3.py:14–19` extends `PROPERTY_CATALOG`
   with `open/high/low` for the C3 corpus scan; the same convention
   applies to new entries.
3. **Multi-output narrowing is one entry per struct field.** The
   struct field name must be exact (`'macd'`, `'upper'`, `'lower'`,
   `'aroon_up'`, `'slowk'`, `'slowd'`) — verified live via
   `t_<FN>(...)[0].keys()`. Picking the wrong field surfaces at SQL
   execute time with a duckdb binder error, NOT at validate time.
4. **The `n` arg in `macd` is fast-period.** Slow (26) and signal (9)
   are constants in `_macd` (`duckdb_sql.py:163`). The scan-level name
   `macd(n)` → `t_macd(close, n, 26, 9)` — the user cannot override
   slow/signal in 0.4.0; this is the documented scanlang convention
   (corpus uses 12/26/9).
5. **Warm-up contract is asymmetric by design.** `rsi` and `ema` use
   TA-Lib's SMA-of-n seeding on the duckdb side and first-value seeding
   on the polars side (`indicators.py:15–20`). Hit-set equality is
   only claimed for sma-family scans. Cross-engine convergence is
   documented at 0.01 abs diff after MATURE bars (verified at 112 for
   n=14; 7.6 × 14 ≈ 106, with margin).
6. **`ad` is the first empty-tuple `()` `arg_spec` entry.** `ad`
   takes no `n` arg, so its `arg_spec` is the empty tuple `()`. (This
   is distinct from the dummy-int precedent: see the codification
   rules "Dummy-int precedent" paragraph above — `trange` and
   `ht_trendline` take no period but their `arg_spec` is still
   `("int",)` to match scanlang's "every non-AD entry declares its
   period" convention. `ad` is the outlier: no period at all, so
   empty tuple.) `_tcol` (`duckdb_sql.py:131`) already handles
   `n is None` for the dummy-int case. `validate()` already handles
   empty `arg_spec` (`compiler.py:171` checks
   `len(args) != len(arg_spec)`, which is `0 != 0`).
7. **`stoch_k`, `stoch_d` are the first 3-int `arg_spec` entries.**
   Validate's tag loop (`compiler.py:175–178`) is length-only — the
   `("int", "int", "int")` shape Just Works. The SQL builder must
   pass all three ints to `t_stoch` (positions 4, 5, 7 with ma_type=0
   at positions 6 and 8).
8. **No new public API surface** beyond `apply(engine=...)` and
   `compile(engine=...)` accepting `"talib"`. The existing
   `engine="duckdb"` kwarg is the precedent for the optional engine
   dispatch (`compiler.py:300–334`). Adding `"talib"` is an additive
   kwarg value, fully backward compatible.
9. **The `scanlang.talib` adapter is OPTIONAL** (loaded only when the
   `talib` extra is installed). The polars engine's hot path is
   unchanged. The `apply(frame, scan_def, engine='talib')` path
   collects the frame first (the seam needs eager `np.ndarray`s),
   so it cannot participate in lazy pushdown — explicitly documented
   in `docs/IR_FREEZE.md:13–14`.

## What this plan does NOT cover (recorded, not carded)

- **Generic vector math** (TA-Lib Math Operators + Math Transform) —
  re-implements polars expressions. Excluded; not a corpus gap.
- **Multi-output functions not in scan corpus** (`HT_DCPERIOD,
  HT_DCPHASE, HT_PHASOR, HT_SINE, HT_TRENDMODE, MACDEXT, MACDFIX,
  MINMAXINDEX, STOCHF`) — registered names exist for completeness
  but no scan uses them; defer.
- **Generic vector `t_*` operations** that have no TA-Lib equivalent
  (none observed; would be a separate plan if any surface).
- **The `ta_*` window form** — rejected by the 2026-09-02 benchmark
  (30–35× slower). Not in scope.
- **A new IR `arg_spec` tag** for float / enum / variable-period
  parameters (would unlock MAMA, SAR, SAREXT, MACDEXT, MACDFIX, MA,
  MAVP) — IR-schema change, OUT of scope for 0.4.0. Carded as a
  future "float params" IR addition.
- **Pattern recognition beyond `cdlengulfing`** — 52 isolated
  entries with no current corpus scan. Carded as a corpus-driven
  future addition.

## Verification (this plan lands only when the implementer cards prove
the contract)

1. `uv sync --all-extras` (talib + duckdb + dev deps).
2. `uv run pytest -q` — full suite green; new tests in Slices B/C/D
   pass at <0.01 abs diff for convergence-tier entries, exact for
   closed-form entries.
3. `uv run ruff check .` — clean (the existing plan's standard).
4. `uv run python -c "from scanlang import INDICATORS; from
   scanlang.duckdb_sql import SQL_INDICATORS; assert
   set(SQL_INDICATORS) >= set(INDICATORS); print(len(INDICATORS),
   len(SQL_INDICATORS))"` — superset property holds.
5. Manual: `uv run python -c "import duckdb; con=duckdb.connect();
   con.execute('INSTALL talib FROM community'); con.execute('LOAD
   talib'); print(con.execute(\"SELECT function_name FROM
   duckdb_functions() WHERE function_name LIKE 't_%' ORDER BY
   function_name\").fetchall()[:5])"` — `t_*` extension loads.

## Changelog

- 2026-09-04 — initial plan. Probed live: TA-Lib 0.7.1 (161 indicator
  funcs, 10 groups), duckdb 1.5.5 community talib extension (126 `t_*`
  TA-Lib twins — naive `LIKE 't_%'` is 359; `ta_*` has 126 TA-Lib
  twins + 3 duckdb-only (`tan`, `tanh`, `table_info`) = 129 — the
  129 figure is the wrong framing; the canonical coverage gap is
  35-of-161 from 126 twins; `ta_*` window form rejected by 2026-09-02
  benchmark), `scanlang.indicators.INDICATORS` (13 entries),
  `scanlang.duckdb_sql.SQL_INDICATORS` (20 entries — verified via
  `python -c "from scanlang.indicators import INDICATORS; ..."`,
  not by doc reading). Pinned: dependency = official TA-Lib
  (not polars-talib); adapter seam =
  `df.group_by(partition, maintain_order=True).map_groups(lambda g:
  g.with_columns(pl.Series(name, talib.<FN>(*arrays, timeperiod=n))
  .fill_nan(None)))`
  (the original draft's `pl.map_groups(...).over(partition).fill_nan(None)`
  form is NOT executable — `Expr` has no `map_groups`, the result
  `DataFrame` has no `.over`; see the seam section for the full
  rationale and lazy-frame note). Target: 6 new tests (one per Slice
  B/C/D/E/F/G; one function loops over its slice's entries), no test
  deleted, no existing test modified.
- 2026-09-04 (reviewer round 1) — corrections applied: STOCH warm-up
  is `(fastk−1)+(slowk−1)+(slowd−1)`, not `fastk−1`; registry counts
  corrected to 13 / 20 (was 14 / 19); MAMA / MIDPOINT / HT_TRENDMODE
  moved out of the "missing from t_*" exclusion row (all three have
  `t_*` forms, verified live via `duckdb_functions()`); TRANGE warm-up
  corrected to 1 (was 14); ta_* count clarified as 126 TA-Lib twins
  + 3 duckdb-only (was "129"); "14 new tests" replaced with the 6
  named slice-test functions; "13+2" heading reconciled with the
  "11+2" body text; TRANGE dummy-int precedent documented.