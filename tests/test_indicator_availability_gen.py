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

import difflib
import subprocess
import sys
import tempfile
from pathlib import Path

from scanlang import INDICATORS as POLARS_REG
from scanlang.duckdb_sql import SQL_INDICATORS as SQL_REG

_HERE = Path(__file__).parent
_ROOT = _HERE.parent
_FULL = _ROOT / "docs" / "reference" / "_indicator_availability_full.md"
_DUAL = _ROOT / "docs" / "reference" / "_indicator_availability.md"


def _regenerate_into(tmp: Path) -> tuple[Path, Path]:
    """Run the generator writing into ``tmp``; return (full, dual) paths."""
    full_out = tmp / "_indicator_availability_full.md"
    dual_out = tmp / "_indicator_availability.md"
    subprocess.run(
        [
            sys.executable, "scripts/gen_indicator_availability.py",
            "--out-full", str(full_out),
            "--out-dual", str(dual_out),
        ],
        cwd=_ROOT, capture_output=True, text=True, check=True,
    )
    return full_out, dual_out


def _assert_match(label: str, committed: str, generated_path: Path) -> None:
    """Fail with a unified diff if ``committed`` doesn't equal the file."""
    generated = generated_path.read_text()
    if committed == generated:
        return
    diff = "".join(difflib.unified_diff(
        committed.splitlines(keepends=True),
        generated.splitlines(keepends=True),
        fromfile=f"committed:{label}",
        tofile=f"generated:{label}",
        n=3,
    ))
    raise AssertionError(
        f"{label} drifted from the live registries. "
        f"Regenerate with: uv run python scripts/gen_indicator_availability.py\n"
        f"{diff}"
    )


def test_generated_files_match_registries():
    """The committed generated files must match the live registries."""
    assert _FULL.exists(), f"{_FULL} missing — run scripts/gen_indicator_availability.py"
    assert _DUAL.exists(), f"{_DUAL} missing — run scripts/gen_indicator_availability.py"

    # Capture committed contents BEFORE invoking the generator so the
    # comparison target can't be clobbered by the subprocess.
    committed_full = _FULL.read_text()
    committed_dual = _DUAL.read_text()

    # Run the generator into a temp dir — never let it touch tracked files.
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        gen_full, gen_dual = _regenerate_into(tmp)
        _assert_match("_indicator_availability_full.md", committed_full, gen_full)
        _assert_match("_indicator_availability.md", committed_dual, gen_dual)
        # Snapshot the generator output for the membership checks below.
        full = gen_full.read_text()
        dual = gen_dual.read_text()

    # Registry-membership invariants on the freshly-generated content.
    for name in set(POLARS_REG) | set(SQL_REG):
        assert f"`{name}`" in full, f"{name} missing from the full table"
    dual_only = set(SQL_REG) - set(POLARS_REG)
    for name in dual_only:
        assert f"`{name}`" not in dual, (
            f"{name} (duckdb-only) must not appear in the dual table"
        )
    for name in set(POLARS_REG) & set(SQL_REG):
        assert f"`{name}`" in dual, f"{name} (dual-engine) missing from the dual table"
