"""Regenerate the ``_TALIB_SEAM`` table block in ``src/scanlang/indicators.py``.

The SPEC dict below is the single source of truth for the TA-Lib parity seam
(same 5-field row shape as the table it renders, the Task 0 decision in
``docs/plans/2026-09-04-talib-seam-codegen.md``): name -> ``(talib_fn,
inputs, n_kw, fixed_kwargs, slot)``. Adding a wave of parity names is a SPEC
edit plus this rerun — no closure surgery.

    uv run python scripts/gen_talib_seam.py            # the committed table
    uv run python scripts/gen_talib_seam.py --target P # any copy of the module

Contract (same shape as ``scripts/gen_indicator_availability.py``):
committed source, regenerated only when SPEC changes, and idempotent — a
second run writes nothing. Before writing, the script VALIDATES the target:
it executes the target file standalone (``spec_from_file_location``) and
asserts every SPEC name is registered in ``INDICATORS`` with SPEC's exact
``required_cols`` (the guard polars_ta lacks — a talib wrapper is never
trusted sight unseen). A mismatch refuses the write and exits 1.
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

DEFAULT_TARGET = Path("src/scanlang/indicators.py")
BEGIN = "# --- BEGIN GENERATED talib seam table (scripts/gen_talib_seam.py) ---"
END = "# --- END GENERATED ---"

# name -> (talib_fn, inputs, n_kw, fixed kwargs, output slot). Semantics of
# each field are documented on the table in indicators.py and on
# _seam_builder; slot None = scalar output, int = tuple index.
SPEC: dict[str, tuple] = {
    "adx": ("ADX", ("high", "low", "close"), "timeperiod", {}, None),
    "kama": ("KAMA", ("close",), "timeperiod", {}, None),
    "macd": ("MACD", ("close",), "fastperiod", {"slowperiod": 26, "signalperiod": 9}, 0),
    "bbands_upper": ("BBANDS", ("close",), "timeperiod", {"nbdevup": 2.0, "nbdevdn": 2.0, "matype": 0}, 0),
    "bbands_lower": ("BBANDS", ("close",), "timeperiod", {"nbdevup": 2.0, "nbdevdn": 2.0, "matype": 0}, 2),
    "aroon": ("AROON", ("high", "low"), "timeperiod", {}, 1),
}


def _render_row(name: str, row: tuple) -> str:
    fn, cols, n_kw, kw, slot = row
    # repr() then ' -> ": row content is identifiers and numbers only (no
    # apostrophes), so the swap is byte-exact and matches the repo's
    # double-quote style (keeps ruff format from churning the block).
    parts = repr((fn, tuple(cols), n_kw, kw, slot)).replace("'", '"')
    return f'    "{name}": {parts},'


def _render_table() -> str:
    lines = [BEGIN, "_TALIB_SEAM = {", *(_render_row(n, r) for n, r in SPEC.items()), "}", END]
    return "\n".join(lines)


def _spec_from_source(text: str) -> dict:
    """The current _TALIB_SEAM table, parsed (not imported) from the source."""
    tree = ast.parse(text)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            getattr(t, "id", None) == "_TALIB_SEAM" for t in node.targets
        ):
            return ast.literal_eval(node.value)
    raise SystemExit("no _TALIB_SEAM assignment found in the target module")


def _validate(target: Path) -> None:
    """Execute the target module standalone; every SPEC name must register.

    Loaded by file path (``spec_from_file_location``), NOT via the
    ``scanlang`` package: package resolution would find the repo's
    ``src/scanlang`` (regular packages beat sys.path order — a temp-copy
    namespace portion loses), importing the real module instead of the file
    under test — the exact false-pass this gate exists to prevent.
    ``indicators.py`` has no intra-package imports, so standalone execution
    is faithful. A module whose registration loop is broken (or whose exec
    raises) refuses the write.
    """
    import importlib.util

    mod_spec = importlib.util.spec_from_file_location("scanlang.indicators", target)
    if mod_spec is None or mod_spec.loader is None:  # pragma: no cover - target is a .py we just read
        raise SystemExit(f"cannot load module from {target}")
    mod = importlib.util.module_from_spec(mod_spec)
    mod_spec.loader.exec_module(mod)
    got = {n: list(mod.INDICATORS[n][2]) for n in SPEC if n in mod.INDICATORS}
    missing = [n for n in SPEC if n not in got]
    wrong = {
        n: (got[n], list(SPEC[n][1])) for n in SPEC if n in got and got[n] != list(SPEC[n][1])
    }
    if missing or wrong:
        raise SystemExit(
            "validation failed, refusing to write:\n"
            f"  missing from INDICATORS: {missing}\n"
            + "\n".join(f"  {n}: required_cols {g} != SPEC {w}" for n, (g, w) in wrong.items())
        )


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--target", type=Path, default=DEFAULT_TARGET, help="indicators.py to regenerate")
    args = p.parse_args()
    target: Path = args.target
    if not target.exists():
        raise SystemExit(f"target module not found: {target}")
    text = target.read_text()
    current = _spec_from_source(text)
    if current != SPEC:
        raise SystemExit(
            "target table drifted from SPEC — rerun this script to regenerate, "
            "or edit SPEC."
        )
    _validate(target)
    new_block = _render_table()
    start = text.index(BEGIN)
    stop = text.index(END) + len(END)
    updated = text[:start] + new_block + text[stop:]
    if updated == text:
        print(f"{target}: already up to date (no write)")
        return 0
    target.write_text(updated)
    print(f"{target}: regenerated {len(SPEC)} seam row(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
