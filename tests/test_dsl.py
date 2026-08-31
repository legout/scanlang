"""Tests for the text DSL parser (docs/IR_FREEZE.md, Text DSL front-end)."""

import pytest

from scanlang.compiler import PROPERTY_CATALOG, validate
from scanlang.dsl import parse

# corpus catalogs carry columns beyond PROPERTY_CATALOG (rs, volume, ...)
CORPUS_CATALOG = {
    **PROPERTY_CATALOG,
    "rs": {"label": "RS", "dtype": "float"},
    "volume": {"label": "Volume", "dtype": "float"},
    "market_cap": {"label": "Market Cap", "dtype": "float"},
}


def p(text, catalog=CORPUS_CATALOG):
    return parse(text, catalog=catalog)


# --- golden: user's example, both shift spellings produce identical defs ----


def test_golden_both_shift_spellings_identical():
    # both spellings of shift produce IDENTICAL scan_defs: close(1) == close[1].
    # Indicator exprs shift only via the bracket form (freeze grammar).
    call = p("close(1)>ema(20)[1]")
    bracket = p("close[1]>ema(20)[1]")
    assert call == bracket
    assert call == {
        "filters": [
            {
                "property": {"fn": "shift", "args": [{"col": "close"}, 1]},
                "op": ">",
                "value": {
                    "fn": "shift",
                    "args": [{"fn": "ema", "args": [{"col": "close"}, 20]}, 1],
                },
            }
        ]
    }


def test_golden_canonical_example():
    d = p("close>10 AND ema(20)>ema(50) AND ema(20)[1]<ema(50)[1]")
    assert d == {
        "filters": [
            {
                "all": [
                    {"property": "close", "op": ">", "value": 10},
                    {
                        "property": {"fn": "ema", "args": [{"col": "close"}, 20]},
                        "op": ">",
                        "value": {"fn": "ema", "args": [{"col": "close"}, 50]},
                    },
                    {
                        "property": {
                            "fn": "shift",
                            "args": [{"fn": "ema", "args": [{"col": "close"}, 20]}, 1],
                        },
                        "op": "<",
                        "value": {
                            "fn": "shift",
                            "args": [{"fn": "ema", "args": [{"col": "close"}, 50]}, 1],
                        },
                    },
                ]
            }
        ]
    }
    assert validate(d) == []


def test_golden_close_call_and_bracket_spelling():
    assert p("close(1)>close(2)") == p("close[1]>close[2]")
    assert p("close(1)>close(2)") == {
        "filters": [
            {
                "property": {"fn": "shift", "args": [{"col": "close"}, 1]},
                "op": ">",
                "value": {"fn": "shift", "args": [{"col": "close"}, 2]},
            }
        ]
    }


# --- real corpus queries (stockScreener/resources/scans.py) -----------------


def test_corpus_trend_template():
    d = p("sma(200,close(22))<sma(200)")
    assert d == {
        "filters": [
            {
                "property": {
                    "fn": "sma",
                    "args": [{"fn": "shift", "args": [{"col": "close"}, 22]}, 200],
                },
                "op": "<",
                "value": {"fn": "sma", "args": [{"col": "close"}, 200]},
            }
        ]
    }
    assert validate(d, catalog=CORPUS_CATALOG) == []


def test_corpus_sma_stack():
    d = p("close>sma(50) AND sma(50)>sma(150)")
    assert d == {
        "filters": [
            {
                "all": [
                    {
                        "property": "close",
                        "op": ">",
                        "value": {"fn": "sma", "args": [{"col": "close"}, 50]},
                    },
                    {
                        "property": {"fn": "sma", "args": [{"col": "close"}, 50]},
                        "op": ">",
                        "value": {"fn": "sma", "args": [{"col": "close"}, 150]},
                    },
                ]
            }
        ]
    }
    assert validate(d) == []


def test_corpus_52w_high():
    # max(252, close): corpus (n, expr) order, explicit col beats close default
    d = p("close=max(252,close)")
    assert d == {
        "filters": [
            {
                "property": "close",
                "op": "==",
                "value": {"fn": "rmax", "args": [{"col": "close"}, 252]},
            }
        ]
    }
    assert validate(d) == []


def test_corpus_rs_relative_strength():
    d = p("rs=max(5,rs) AND close<max(21)")
    assert d == {
        "filters": [
            {
                "all": [
                    {
                        "property": "rs",
                        "op": "==",
                        "value": {"fn": "rmax", "args": [{"col": "rs"}, 5]},
                    },
                    {
                        "property": "close",
                        "op": "<",
                        "value": {"fn": "rmax", "args": [{"col": "close"}, 21]},
                    },
                ]
            }
        ]
    }
    assert validate(d, catalog=CORPUS_CATALOG) == []


# --- precedence & boolean structure -----------------------------------------


def test_and_binds_tighter_than_or():
    d = p("spring OR close>10 AND ema(20)>5")
    assert d == {
        "filters": [
            {
                "any": [
                    {"property": "spring", "op": "==", "value": True},
                    {
                        "all": [
                            {"property": "close", "op": ">", "value": 10},
                            {
                                "property": {
                                    "fn": "ema",
                                    "args": [{"col": "close"}, 20],
                                },
                                "op": ">",
                                "value": 5,
                            },
                        ]
                    },
                ]
            }
        ]
    }


def test_single_filter_not_wrapped_in_all():
    assert p("close>10") == {"filters": [{"property": "close", "op": ">", "value": 10}]}


def test_case_insensitive_and_symbol_aliases():
    assert p("close>10 and spring") == p("close>10 AND spring")
    assert p("close>10 && spring || ema_stack") == p("close>10 AND spring OR ema_stack")
    assert p("not spring") == p("NOT spring")


def test_symbol_alias_precedence_shape():
    d = p("close>10 && spring || ema_stack")
    assert d["filters"][0] == {
        "any": [
            {
                "all": [
                    {"property": "close", "op": ">", "value": 10},
                    {"property": "spring", "op": "==", "value": True},
                ]
            },
            {"property": "ema_stack", "op": "==", "value": True},
        ]
    }


def test_parens_grouping():
    d = p("(close>10 OR spring) AND ema(20)>5")
    assert d["filters"][0] == {
        "all": [
            {
                "any": [
                    {"property": "close", "op": ">", "value": 10},
                    {"property": "spring", "op": "==", "value": True},
                ]
            },
            {
                "property": {"fn": "ema", "args": [{"col": "close"}, 20]},
                "op": ">",
                "value": 5,
            },
        ]
    }


def test_not():
    assert p("NOT spring") == {
        "filters": [{"not": {"property": "spring", "op": "==", "value": True}}]
    }


# --- rules ------------------------------------------------------------------


def test_equals_single():
    assert p("close=10") == p("close==10")


def test_bare_bool_and_nonbool_error_position():
    assert p("spring") == {
        "filters": [{"property": "spring", "op": "==", "value": True}]
    }
    with pytest.raises(SyntaxError, match="position 1"):
        p("close")


def test_between_and_in_barewords_to_strings():
    assert p("score between [50, 90]") == {
        "filters": [{"property": "score", "op": "between", "value": [50, 90]}]
    }
    assert p("phase in [BREAKOUT, 'MARKUP']") == {
        "filters": [{"property": "phase", "op": "in", "value": ["BREAKOUT", "MARKUP"]}]
    }
    assert p("phase in [breakout]")["filters"][0]["value"] == ["breakout"]


def test_cross_calls():
    above = p("cross_above(ema(20), ema(50))")
    assert above == {
        "filters": [
            {
                "property": {"fn": "ema", "args": [{"col": "close"}, 20]},
                "op": "cross_above",
                "value": {"fn": "ema", "args": [{"col": "close"}, 50]},
            }
        ]
    }
    assert p("cross_below(close, sma(50))")["filters"][0]["op"] == "cross_below"


def test_min_max_sugar():
    assert p("close/min(21)<1.1") == p("close/rmin(21)<1.1")
    # close/max(252) inside a division: rmax(close, 252) as the divisor operand
    div = p("close/max(252)>0.85")["filters"][0]["property"]["/"][1]
    assert div == {"fn": "rmax", "args": [{"col": "close"}, 252]}


def test_min_max_sugar_vs_rsi_style_indicator():
    # min/max sugar holds even when followed by other comparisons
    d = p("rs=max(5,rs) AND close<max(21)")
    assert d["filters"][0]["all"][1] == {
        "property": "close",
        "op": "<",
        "value": {"fn": "rmax", "args": [{"col": "close"}, 21]},
    }


def test_default_close_insertion():
    assert p("ema(20)>5") == p("ema(close,20)>5")
    assert p("sma(volume,20)>1000")["filters"][0]["property"] == {
        "fn": "sma",
        "args": [{"col": "volume"}, 20],
    }
    # rsi/atr are not close-defaulted (freeze: rsi(14)/atr(14) unchanged)
    assert p("rsi(14)>70")["filters"][0]["property"] == {"fn": "rsi", "args": [14]}


def test_arithmetic_nary_and_precedence():
    assert p("vol_ratio*2+1>5") == {
        "filters": [
            {
                "property": {"+": [{"*": [{"col": "vol_ratio"}, 2]}, 1]},
                "op": ">",
                "value": 5,
            }
        ]
    }
    # same-op folds n-ary
    assert p("close+1+2>5")["filters"][0]["property"] == {"+": [{"col": "close"}, 1, 2]}


def test_unary_minus_and_scalar_flip():
    assert p("-5<close") == {"filters": [{"property": "close", "op": ">", "value": -5}]}
    assert p("5>close") == {"filters": [{"property": "close", "op": "<", "value": 5}]}


def test_strings_bools_dates():
    assert p("symbol=='AAPL'")["filters"][0]["value"] == "AAPL"
    assert p("session>'2026-01-01'")["filters"][0]["value"] == "2026-01-01"
    assert p("spring==false")["filters"][0]["value"] is False


def test_postfix_on_calls_and_columns():
    d = p("ema(20)[5]>1")
    assert d["filters"][0]["property"] == {
        "fn": "shift",
        "args": [{"fn": "ema", "args": [{"col": "close"}, 20]}, 5],
    }


# --- parse errors carry positions; validation stays in validate() -----------


def test_error_positions():
    with pytest.raises(SyntaxError, match="position \\d+"):
        p("close>")
    with pytest.raises(SyntaxError, match="position 1"):
        p("nosuchcol>5")
    with pytest.raises(SyntaxError, match="position \\d+"):
        p("close>10 AND")
    with pytest.raises(SyntaxError, match="unexpected"):
        p("close>10 trailing")


def test_validate_stays_out_of_parse():
    # arg-count problems parse fine; the registry reports them in validate()
    d = p("rsi(14)>70")
    assert isinstance(d, dict)
    assert "takes 2 args" in validate(d)[0]
    assert validate(p("close>10")) == []


def test_extra_call_args_reach_validate():
    # regression: a 3-arg close-defaulted call must pass through untouched so
    # validate() reports the arity — never silently reordered/dropped (parse
    # must NOT raise; validate() is the single gate)
    d = p("sma(200, close(22), 7)>5")
    assert "'sma' takes 2 args, got 3" in validate(d)[0]


def test_cross_call_rejected_outside_head():
    # cross_* is only valid in head position; anywhere nested it is a parse
    # error — never a __cross__ marker leaking into validate() error text
    for text in (
        "close>cross_above(ema(20),ema(50))",
        "1+cross_above(ema(20),ema(50))>5",
        "ema(cross_above(ema(20),ema(50)),20)>5",
    ):
        with pytest.raises(SyntaxError, match="position \\d+") as exc:
            p(text)
        assert "__cross__" not in str(exc.value)
