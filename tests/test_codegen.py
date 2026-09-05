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


def test_validation_gate_refuses_broken_registration(tmp_path):
    """Never trust the wrapper: a table whose registration loop is broken
    must fail the import gate and NOT be written."""
    committed = _INDICATORS.read_text()
    target = tmp_path / "scanlang" / "indicators.py"
    target.parent.mkdir()
    target.write_text(committed)

    loop_line = '    INDICATORS.setdefault(_name, (("int",), _seam_builder(_fn, _cols, _nkw, _kw, _slot), _cols))'
    assert loop_line in committed, "registration loop moved — update this test"
    broken = committed.replace(loop_line, "    pass")
    target.write_text(broken)

    result = _run(target)
    assert result.returncode != 0, "gate must refuse a table that does not register"
    assert target.read_text() == broken, "gate wrote despite validation failure"
