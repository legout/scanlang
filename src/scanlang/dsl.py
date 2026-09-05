"""Text DSL front-end: parse scan text into the scanlang IR.

Pure stdlib tokenizer + recursive-descent parser per docs/IR_FREEZE.md,
section "Text DSL front-end (frozen 2026-08-30, second session)":

    >>> from scanlang.dsl import parse
    >>> parse("close>10 AND ema(20)>ema(50)")  # doctest: +ELLIPSIS
    {'filters': [{'all': [...]}]}

Parse errors raise SyntaxError with a 1-based position. Semantic checks
(unknown column, bad arg counts, dtypes) stay in scanlang.compiler.validate().

Number-first args on ema/sma/rmin/rmax and the C3 corpus names adr/roc/
natr/slope: a single number arg inserts the close column (ema(20) ->
ema(close, 20); adr(20) -> adr(close, 20)); a leading number with a second
arg is corpus order (n, expr) and normalizes to canonical (expr, n) —
sma(200, close(22)) -> sma(shift(close, 22), 200), slope(10, sma(200)) ->
slope(sma(close, 200), 10), max(252, close) -> rmax(close, 252).

Talib-only indicator names (macd, bbands_upper/lower, aroon,
ht_trendline — SQL_INDICATORS registry) parse as fn calls
too; validate() stays the single gate for engine fit (the polars engine
rejects them). ``adx`` also parses here and is dual-engine — its
INDICATORS parity builder (the ``talib`` extra) validates on the polars
engine too. Grammar rule: ``name`` in EITHER registry resolves as an
indicator call before the column-lookback fallback.
"""

from __future__ import annotations

from typing import Any, NamedTuple

from scanlang.compiler import PROPERTY_CATALOG, _sql_indicators
from scanlang.indicators import INDICATORS

__all__ = ["parse"]

# indicators whose number-first args imply the close column
_CLOSE_DEFAULT = frozenset({"ema", "sma", "rmin", "rmax", "adr", "roc", "natr", "slope"})
_SUGAR = {"min": "rmin", "max": "rmax"}
_CROSS = ("cross_above", "cross_below")
_FLIP = {">": "<", "<": ">", ">=": "<=", "<=": ">=", "==": "==", "!=": "!="}
_KEYWORDS = {"and": "AND", "or": "OR", "not": "NOT", "between": "BETWEEN", "in": "IN"}
_PUNCT = "()[]{},+-*/"


class _Name(NamedTuple):
    """Bare column reference pending resolution to {"col": name}."""

    name: str
    pos: int


def _tokenize(text: str) -> list[tuple[str, Any, int]]:
    """text -> [(kind, value, pos), ...]; pos is the 1-based char offset."""
    toks: list[tuple[str, Any, int]] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c.isspace():
            i += 1
            continue
        pos = i + 1
        if c == "'":
            end = text.find("'", i + 1)
            if end < 0:
                raise SyntaxError(f"unterminated string at position {pos}")
            toks.append(("lit", text[i + 1 : end], pos))
            i = end + 1
        elif c.isdigit() or (c == "." and i + 1 < n and text[i + 1].isdigit()):
            j = i + 1
            while j < n and (text[j].isdigit() or text[j] == "."):
                j += 1
            lit = text[i:j]
            if lit.count(".") > 1:
                raise SyntaxError(f"invalid number {lit!r} at position {pos}")
            toks.append(("lit", float(lit) if "." in lit else int(lit), pos))
            i = j
        elif c.isalpha() or c == "_":
            j = i + 1
            while j < n and (text[j].isalnum() or text[j] == "_"):
                j += 1
            word = text[i:j]
            low = word.lower()
            if low in _KEYWORDS:
                toks.append((_KEYWORDS[low], low, pos))
            elif low in ("true", "false"):
                toks.append(("lit", low == "true", pos))
            else:
                toks.append(("name", word, pos))
            i = j
        elif text[i : i + 2] in ("&&", "||"):
            toks.append(("AND" if c == "&" else "OR", text[i : i + 2], pos))
            i += 2
        elif text[i : i + 2] in (">=", "<=", "==", "!="):
            toks.append(("cmp", text[i : i + 2], pos))
            i += 2
        elif c in "<>=":
            toks.append(("cmp", "==" if c == "=" else c, pos))
            i += 1
        elif c in _PUNCT:
            toks.append((c, c, pos))
            i += 1
        else:
            raise SyntaxError(f"unexpected character {c!r} at position {pos}")
    toks.append(("eof", None, n + 1))
    return toks


class _Parser:
    def __init__(self, text: str, catalog: dict):
        self.toks = _tokenize(text)
        self.i = 0
        self.catalog = catalog

    def _peek(self) -> tuple[str, Any, int]:
        return self.toks[self.i]

    def _next(self) -> tuple[str, Any, int]:
        tok = self.toks[self.i]
        self.i += 1
        return tok

    def _expect(self, kind: str) -> tuple[str, Any, int]:
        tok = self._next()
        if tok[0] != kind:
            raise SyntaxError(f"expected {kind!r} at position {tok[2]}, got {tok[1]!r}")
        return tok

    def parse(self) -> dict:
        node = self.expr()
        kind, val, pos = self._peek()
        if kind != "eof":
            raise SyntaxError(f"unexpected {val!r} at position {pos}")
        return {"filters": [node]}

    # OR binds loosest -> {"any": [...]}; single child stays unwrapped
    def expr(self) -> dict:
        node = self.and_expr()
        if self._peek()[0] != "OR":
            return node
        kids = [node]
        while self._peek()[0] == "OR":
            self._next()
            kids.append(self.and_expr())
        return {"any": kids}

    # AND binds tighter than OR -> {"all": [...]}
    def and_expr(self) -> dict:
        node = self.term()
        if self._peek()[0] != "AND":
            return node
        kids = [node]
        while self._peek()[0] == "AND":
            self._next()
            kids.append(self.term())
        return {"all": kids}

    # term := NOT term | '(' expr ')' | comparison
    def term(self) -> dict:
        kind = self._peek()[0]
        if kind == "NOT":
            self._next()
            return {"not": self.term()}
        if kind == "(":
            self._next()
            node = self.expr()
            self._expect(")")
            return node
        return self.comparison()

    # comparison := cross-call | operand (cmp | BETWEEN [lo, hi] | IN [v, ...])?
    def comparison(self) -> dict:
        lhs = self.arith()
        if isinstance(lhs, dict) and lhs.get("__cross__"):
            a = lhs["args"]
            return {"property": a[0], "op": lhs["op"], "value": a[1]}
        bad = self._cross_pos(lhs)
        if bad:
            raise SyntaxError(f"cross call not allowed here at position {bad}")
        kind, val, _ = self._peek()
        if kind == "cmp":
            self._next()
            rhs = self.arith()
            bad = self._cross_pos(rhs)
            if bad:
                raise SyntaxError(f"cross call not allowed here at position {bad}")
            if isinstance(lhs, (_Name, dict)):
                return {
                    "property": self._leaf_prop(lhs),
                    "op": val,
                    "value": self._resolve(rhs),
                }
            if isinstance(rhs, (_Name, dict)):  # scalar LHS: flip (5 < close)
                return {
                    "property": self._leaf_prop(rhs),
                    "op": _FLIP[val],
                    "value": lhs,
                }
            raise SyntaxError(
                f"comparison needs a column or expression at position {self._peek()[2]}"
            )
        if kind == "BETWEEN":
            self._next()
            return {
                "property": self._leaf_prop(lhs),
                "op": "between",
                "value": self._literals(2),
            }
        if kind == "IN":
            self._next()
            return {
                "property": self._leaf_prop(lhs),
                "op": "in",
                "value": self._literals(),
            }
        # bare operand: bool column -> col == true leaf; anything else is an error
        if isinstance(lhs, _Name):
            spec = self.catalog.get(lhs.name)
            if spec is not None and spec["dtype"] == "bool":
                return {"property": lhs.name, "op": "==", "value": True}
            raise SyntaxError(
                f"column {lhs.name!r} needs a comparison at position {lhs.pos}"
            )
        raise SyntaxError(f"expected a comparison at position {self._peek()[2]}")

    @staticmethod
    def _cross_pos(x) -> int | None:
        """Position of a __cross__ marker nested anywhere in an operand
        fragment (call args, arithmetic folds), else None. Head position is
        consumed by comparison(); anything nested would leak the marker
        into validate() error text, so it is rejected at parse time."""
        if isinstance(x, dict):
            if x.get("__cross__"):
                return x["pos"]
            for v in x.values():
                if (p := _Parser._cross_pos(v)) is not None:
                    return p
        if isinstance(x, list):
            for v in x:
                if (p := _Parser._cross_pos(v)) is not None:
                    return p
        return None

    def _leaf_prop(self, lhs) -> str | dict:
        if isinstance(lhs, _Name):
            return lhs.name
        if isinstance(lhs, dict):
            return self._resolve(lhs)  # computed LHS (freeze: operand object)
        raise SyntaxError(f"invalid comparison left side at position {self._peek()[2]}")

    @staticmethod
    def _resolve(x):
        """Walk a parsed fragment turning every _Name into {"col": name}."""
        if isinstance(x, _Name):
            return {"col": x.name}
        if isinstance(x, dict):
            return {k: _Parser._resolve(v) for k, v in x.items()}
        if isinstance(x, list):
            return [_Parser._resolve(v) for v in x]
        return x

    # between/in values: literal-only; barewords become strings
    def _literals(self, count: int | None = None) -> list:
        self._expect("[")
        vals = []
        while self._peek()[0] != "]":
            kind, val, pos = self._next()
            if kind == "name":
                pass
            elif kind != "lit":
                raise SyntaxError(f"expected a literal at position {pos}")
            vals.append(val)
            if self._peek()[0] == ",":
                self._next()
            elif self._peek()[0] != "]":
                raise SyntaxError(f"expected ',' or ']' at position {self._peek()[2]}")
        self._next()
        if count is not None and len(vals) != count:
            raise SyntaxError(f"expected {count} values at position {self._peek()[2]}")
        return vals

    # arith := mul (('+'|'-') mul)*  -> n-ary {"+"/"-": [...]}
    def arith(self):
        node = self.mul()
        while (kind := self._peek()[0]) in ("+", "-"):
            self._next()
            node = self._fold(kind, node, self.mul())
        return node

    # mul := atom (('*'|'/') atom)*
    def mul(self):
        node = self.atom()
        while (kind := self._peek()[0]) in ("*", "/"):
            self._next()
            node = self._fold(kind, node, self.atom())
        return node

    @staticmethod
    def _fold(op: str, lhs, rhs):
        """Fold same-op chains n-ary; mixed chains nest (a+b-c -> {"-": [{"+": ...}, c]})."""
        if isinstance(lhs, dict) and list(lhs) == [op]:
            lhs[op].append(rhs)
            return lhs
        return {op: [lhs, rhs]}

    # atom := number | string | bool | -number | call | bareword
    def atom(self):
        kind, val, pos = self._next()
        if kind == "lit":
            node = val
        elif kind == "-" and self._peek()[0] == "lit":
            node = -self._next()[1]
        elif kind == "name":
            if self._peek()[0] == "(":
                node = self._call(val, pos)
            elif val in self.catalog:
                node = _Name(val, pos)
            else:
                raise SyntaxError(f"unknown column {val!r} at position {pos}")
        else:
            raise SyntaxError(f"unexpected {val!r} at position {pos}")
        return self._postfix(node)

    # name(...): INDICATORS name -> call; min/max -> rmin/rmax; cross_* -> leaf
    # marker; catalog column -> col + lookback (close(1) -> shift(close, 1)).
    # SQL-only indicator names (macd, adx, ...) parse as calls too — validate()
    # stays the gate for engine fit.
    def _call(self, name: str, pos: int):
        self._expect("(")
        args = []
        if self._peek()[0] != ")":
            while True:
                args.append(self.arith())
                if self._peek()[0] != ",":
                    break
                self._next()
        self._expect(")")
        args = [self._resolve(a) for a in args]
        if name in _SUGAR and name not in INDICATORS:
            name = _SUGAR[name]
        if name in _CROSS:
            if len(args) != 2:
                raise SyntaxError(f"{name}() takes 2 args at position {pos}")
            return {"__cross__": name, "op": name, "args": args, "pos": pos}
        if name in INDICATORS:
            return {"fn": name, "args": self._default_close(name, args)}
        if name in _sql_indicators():
            return {"fn": name, "args": self._default_close(name, args)}
        if name in self.catalog:
            if (
                len(args) != 1
                or not isinstance(args[0], int)
                or isinstance(args[0], bool)
            ):
                raise SyntaxError(
                    f"{name!r} lookback must be one int at position {pos}"
                )
            return {"fn": "shift", "args": [{"col": name}, args[0]]}
        raise SyntaxError(f"unknown indicator or column {name!r} at position {pos}")

    @staticmethod
    def _default_close(name: str, args: list) -> list:
        """number-first args: (n) inserts close; corpus (n, expr) -> canonical (expr, n)."""
        if (
            name in _CLOSE_DEFAULT
            and args
            and isinstance(args[0], (int, float))
            and not isinstance(args[0], bool)
        ):
            if len(args) == 1:
                return [{"col": "close"}, *args]
            if len(args) == 2:
                return [args[1], args[0]]  # corpus (n, expr) -> canonical (expr, n)
        return args  # len>2: pass through untouched so validate() reports arity

    # atom[n] -> shift(expr, n)
    def _postfix(self, node):
        while self._peek()[0] == "[":
            self._next()
            tok = self._next()
            if (
                tok[0] != "lit"
                or not isinstance(tok[1], int)
                or isinstance(tok[1], bool)
            ):
                raise SyntaxError(f"lookback must be an int at position {tok[2]}")
            self._expect("]")
            node = {"fn": "shift", "args": [self._resolve(node), tok[1]]}
        return node


def parse(text: str, *, catalog: dict = PROPERTY_CATALOG) -> dict:
    """Parse DSL text into a scan-def dict: ``{"filters": [node]}``.

    Pure stdlib tokenizer + recursive-descent parser per the frozen
    grammar (see [IR design](../explanation/ir-design.md)). Handles
    AND/OR/NOT, nested groups, comparison ops, arithmetic, indicator
    calls, history lookbacks, the `in`/`between`/`contains` shortcuts,
    and the `cross_above`/`cross_below` leaf ops.

    Number-first args on ``ema``/``sma``/``rmin``/``rmax`` insert the
    ``close`` column (``ema(20)`` -> ``ema(close, 20)``). A leading
    number with a second arg is corpus order and normalizes to canonical
    ``(expr, n)``: ``sma(200, close(22))`` -> ``sma(shift(close, 22), 200)``.

    Args:
        text: DSL source.
        catalog: Used to resolve bareword bool columns and unknown-column
            parse errors. Default ``PROPERTY_CATALOG``.

    Returns:
        A ``{"filters": [node, ...]}`` dict. ``order_by`` and ``limit`` are
        NOT emitted (the parser only handles boolean logic; combine with a
        dict spread).

    Raises:
        SyntaxError: on bad syntax, unknown column, cross call in wrong
            slot. The position is 1-based.

    Examples:
        >>> parse("ema(20) > ema(50)")
        {'filters': [{'property': {'fn': 'ema', 'args': [{'col': 'close'}, 20]}, 'op': '>', 'value': {'fn': 'ema', 'args': [{'col': 'close'}, 50]}}]}
        >>> parse("phase in [BREAKOUT, TREND] and not spring")
        {'filters': [{'all': [{'property': 'phase', 'op': 'in', 'value': ['BREAKOUT', 'TREND']}, {'not': {'property': 'spring', 'op': '==', 'value': True}}]}]}
    """
    return _Parser(text, catalog).parse()
