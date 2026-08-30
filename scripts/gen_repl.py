"""Generate the README REPL transcript by actually executing it.

Each repl_lines entry carries its own prompt (">>> " or "... "); the text
after the prompt is pushed through a real InteractiveConsole.
"""

import code
import contextlib
import io

import polars as pl

repl_lines = [
    ">>> import datetime as dt",
    ">>> import polars as pl",
    ">>> from scanlang import apply, compile, score_bars, validate",
    ">>> T0 = dt.date(2026, 1, 1)",
    ">>> n = 60",
    ">>> sessions = [T0 + dt.timedelta(days=i) for i in range(n)]",
    ">>> closes = [10.0 + i for i in range(n)] + [60.0 - i for i in range(n)]",
    ">>> bars = pl.DataFrame({",
    "...     'symbol': ['AAA'] * n + ['BBB'] * n,",
    "...     'session': sessions * 2,",
    "...     'open': [c - 0.5 for c in closes],",
    "...     'high': [c + 1.0 for c in closes],",
    "...     'low': [c - 1.0 for c in closes],",
    "...     'close': closes,",
    "...     'volume': [1000.0] * (2 * n),",
    "... })",
    ">>> scored = score_bars(bars.lazy()).collect()",
    ">>> scored.select('symbol', 'close', 'score', 'phase')",
    ">>> scan_def = {",
    "...     'filters': [",
    "...         {'property': 'score', 'op': '>=', 'value': 40},",
    "...         {'any': [",
    "...             {'property': 'phase', 'op': 'in', 'value': ['BREAKOUT', 'TREND']},",
    "...             {'not': {'property': 'phase', 'op': '==', 'value': 'NONE'}},",
    "...         ]},",
    "...     ],",
    "...     'order_by': [{'property': 'score', 'dir': 'desc'}],",
    "...     'limit': 5,",
    "... }",
    ">>> validate(scan_def)",
    ">>> apply(scored, scan_def).select('symbol', 'score', 'phase')",
    ">>> expr = compile(scan_def)",
    ">>> expr",
]

ns: dict = {"pl": pl}
console = code.InteractiveConsole(ns)
transcript = io.StringIO()
with contextlib.redirect_stdout(transcript), contextlib.redirect_stderr(transcript):
    for line in repl_lines:
        transcript.write(line + "\n")
        console.push(line[4:])

out = transcript.getvalue()
print(out)
