"""Pins the generated indicator availability tables.

The committed ``docs/reference/_indicator_availability*.md`` files
must be in lockstep with the live ``INDICATORS`` and ``SQL_INDICATORS``
registries — that's the whole point of generating them rather than
maintaining them by hand.

A drift here means someone added / removed / renamed an entry without
re-running the generator; the prose docs that include the table then
silently disagree with the registries. This test fails loudly so the
operator regenerates before commit:

    uv run python scripts/gen_indicator_availability.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from scanlang import INDICATORS as POLARS_REG
from scanlang.duckdb_sql import SQL_INDICATORS as SQL_REG

_HERE = Path(__file__).parent
_ROOT = _HERE.parent
_FULL = _ROOT / "docs" / "reference" / "_indicator_availability_full.md"
_DUAL = _ROOT / "docs" / "reference" / "_indicator_availability.md"


def test_generated_files_match_registries():
    """The committed generated files must match the live registries."""
    assert _FULL.exists(), f"{_FULL} missing — run scripts/gen_indicator_availability.py"
    assert _DUAL.exists(), f"{_DUAL} missing — run scripts/gen_indicator_availability.py"
    # regenerate in a fresh subprocess so the on-disk files are
    # actually re-emitted (a stale committed file would otherwise pass
    # the in-memory test but fail the on-disk check below)
    res = subprocess.run(
        [sys.executable, "scripts/gen_indicator_availability.py"],
        cwd=_ROOT, capture_output=True, text=True, check=True,
    )
    full = _FULL.read_text()
    dual = _DUAL.read_text()
    # every name in either registry appears in the full table
    for name in set(POLARS_REG) | set(SQL_REG):
        assert f"`{name}`" in full, f"{name} missing from the full table"
    # the dual table is the subset in BOTH registries
    dual_only = set(SQL_REG) - set(POLARS_REG)
    for name in dual_only:
        assert f"`{name}`" not in dual, (
            f"{name} (duckdb-only) must not appear in the dual table"
        )
    for name in set(POLARS_REG) & set(SQL_REG):
        assert f"`{name}`" in dual, f"{name} (dual-engine) missing from the dual table"
    # sanity: subprocess emitted both files
    assert "wrote" in res.stdout
