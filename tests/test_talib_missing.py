"""Missing-talib behavior for the adx parity slice (the `talib` extra is optional).

Simulates a talib-less environment by blocking the ``talib`` import (the C
package is installed in dev, so we hide it instead of uninstalling): the
registry entry still exists (import-time guard didn't run the builder), and
validate() passes, but compile/apply fail with the concise install hint.
"""

import builtins
import importlib
import sys

import polars as pl
import pytest

from scanlang.compiler import PROPERTY_CATALOG, apply, compile, validate
from scanlang.indicators import INDICATORS

OHLC_CATALOG = {
    **PROPERTY_CATALOG,
    "high": {"label": "High", "dtype": "float"},
    "low": {"label": "Low", "dtype": "float"},
}

ADX_SCAN = {"filters": [{"property": {"fn": "adx", "args": [14]}, "op": ">", "value": 20}]}


@pytest.fixture()
def no_talib(monkeypatch):
    """Hide the talib module: fresh subprocesses behave as if the extra is absent."""
    monkeypatch.setitem(sys.modules, "talib", None)  # import talib -> ImportError
    real_import = builtins.__import__

    def _blocked(name, *a, **k):
        if name == "talib" or name.startswith("talib."):
            raise ImportError("No module named 'talib' (blocked by test)")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", _blocked)
    importlib.invalidate_caches()
    yield


def test_adx_registered_and_validates_even_without_talib():
    # the registry entry exists unconditionally; required_cols still gate the catalog
    assert INDICATORS["adx"][0] == ("int",)
    assert validate(ADX_SCAN, catalog=OHLC_CATALOG) == []


def test_compile_without_talib_gives_install_hint(no_talib):
    with pytest.raises(ValueError, match=r"adx.*requires the optional 'talib' extra") as ei:
        compile(ADX_SCAN, catalog=OHLC_CATALOG)
    assert "pip install 'scanlang[talib]'" in str(ei.value)


def test_lazy_apply_without_talib_gives_install_hint(no_talib):
    lf = pl.LazyFrame(
        {"symbol": ["A"], "session": [None], "high": [1.0], "low": [1.0], "close": [1.0]}
    )
    with pytest.raises(ValueError, match=r"requires the optional 'talib' extra"):
        apply(lf, ADX_SCAN, catalog=OHLC_CATALOG)
