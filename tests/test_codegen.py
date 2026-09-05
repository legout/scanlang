"""Pins the ``scripts/gen_talib_seam.py`` codegen contract.

The committed ``_TALIB_SEAM`` table in ``src/scanlang/indicators.py``
must be byte-identical to what the generator renders from its ``SPEC``
dict — that is the whole point of generating it. A drift here means
someone hand-edited the table or edited SPEC without rerunning:

    uv run python scripts/gen_talib_seam.py

Temp copies mirror the ``<parent>/scanlang/indicators.py`` layout so the
script's validation gate imports the copy under test (PYTHONPATH
precedence), never a tracked file.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_HERE = Path(__file__).parent
_ROOT = _HERE.parent
_INDICATORS = _ROOT / "src" / "scanlang" / "indicators.py"


def _run(target: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "scripts/gen_talib_seam.py", "--target", str(target)],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_generator_idempotent(tmp_path):
    """Run the script twice over a temp copy; file unchanged both times."""
    committed = _INDICATORS.read_text()
    target = tmp_path / "scanlang" / "indicators.py"
    target.parent.mkdir()
    target.write_text(committed)

    result = _run(target)
    assert result.returncode == 0, result.stderr
    once = target.read_text()
    assert once == committed, "first run drifted from the committed table — regenerate"

    _run(target)
    assert target.read_text() == once, "second run is not a no-op"


def test_spec_matches_committed_table():
    """The script's SPEC and the committed ``_TALIB_SEAM`` are one dataset."""
    sys.path.insert(0, str(_ROOT / "scripts"))
    import gen_talib_seam

    from scanlang.indicators import _TALIB_SEAM

    assert gen_talib_seam.SPEC == dict(_TALIB_SEAM)


def test_regeneration_updates_table_and_gate_guards_output(tmp_path):
    """The SPEC-differs path: a stale table is regenerated to SPEC — and the
    gate validates the REGENERATED output, refusing the write if it would not
    register (leaving the stale target untouched)."""
    committed = _INDICATORS.read_text()
    target = tmp_path / "scanlang" / "indicators.py"
    target.parent.mkdir()

    # Stale copy: drop the aroon row (present in SPEC) by hand.
    aroon_line = '    "aroon": ("AROON", ("high", "low"), "timeperiod", {}, 1),\n'
    assert aroon_line in committed, "table rows moved — update this test"
    stale = committed.replace(aroon_line, "")
    target.write_text(stale)

    # Rerun regenerates: the rendered table replaces the stale block.
    result = _run(target)
    assert result.returncode == 0, result.stderr
    assert target.read_text() == committed, "regeneration did not render SPEC"
    assert "already up to date" not in result.stdout

    # Same path with a broken registration loop: the gate validates the
    # regenerated output and refuses — target keeps the stale table.
    loop_line = '    INDICATORS.setdefault(_name, (("int",), _seam_builder(_fn, _cols, _nkw, _kw, _slot), _cols))'
    broken_stale = stale.replace(loop_line, "    pass")
    assert loop_line in stale and broken_stale != stale
    target.write_text(broken_stale)

    result = _run(target)
    assert result.returncode != 0, "gate must refuse a broken regenerated block"
    assert target.read_text() == broken_stale, "gate wrote despite validation failure"
    assert not list(target.parent.glob("*.tmp")), "staging temp file left behind"
