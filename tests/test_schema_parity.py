"""P6-10..14: the pydantic models and schemas/autobot.2026-08.json agree.

SPEC.md:12 and 18 say the models validate against the JSON schema, so the
same document must be accepted or rejected by both.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any

import pydantic
import pytest
from conftest import model_ok

from autobot.models import Config

MIN: dict[str, Any] = {"autobot": "2026-08", "attach": {"spawn": "ssh host"}, "script": []}


def d(**kw: Any) -> dict[str, Any]:
    doc = copy.deepcopy(MIN)
    doc.update(kw)
    return doc


def s(*steps: dict[str, Any]) -> dict[str, Any]:
    return d(script=list(steps))


def prompt(**kw: Any) -> dict[str, Any]:
    return d(prompts=[{"name": "p", "expect": ["x"], **kw}])


ACCEPT = {
    "minimal": MIN,
    "all-top-level": d(
        env={"A": "a"},
        vars={"creds": [{"u": "a", "p": "b"}], "n": 1},
        prompts=[{"name": "sh", "expect": ["\\$ "], "return": True}],
        errors=["% .*"],
        fn={"f": {"script": [{"cmd": "x"}]}},
        attach={
            "prepare": "#!/bin/sh\ntrue\n",
            "spawn": "ssh host",
            "timeout": "30s",
            "env": {"TERM": "dumb"},
            "script": [{"line": "x"}],
            "breakout": {"script": [{"control": "]"}]},
        },
    ),
    "cmd-full": s(
        {
            "cmd": ["a", "b"],
            "after": "x",
            "when": "true",
            "assert": ["ok"],
            "ignore_error": True,
            "register": "out",
            "delay_before": 1,
            "delay_after": "1s",
            "timeout": "2m",
        }
    ),
    "every-step": d(
        fn={"f": {"script": []}},
        script=[
            {"cmd": "x"},
            {"sleep": 1},
            {"call": "f"},
            {"block": {"name": "b"}},
            {"line": ["a", "b"]},
            {"return": 2},
            {"control": ["a", "x"]},
        ],
    ),
    "send-flat": prompt(send=["a", "b"]),
    "send-grouped": prompt(send=[["a", "b"], ["c", "d"]]),
    "send-each": prompt(send={"each": "vars.creds", "fields": ["u", "p"]}),
    "send-each-no-fields": prompt(send={"each": "vars.pins"}),
    "expect-grouped": d(prompts=[{"name": "p", "expect": [["login:", "Password:"]]}]),
    "nested-block": s(
        {
            "block": {
                "name": "outer",
                "prompts": [{"name": "o", "expect": ["x"], "return": True}],
                "enter": [{"line": "x"}],
                "script": [{"block": {"name": "inner", "script": [{"cmd": "y"}]}}],
                "breakout": {"script": [{"line": "exit"}]},
            }
        }
    ),
    **{
        f"duration-{v}": s({"sleep": v})
        for v in (5, 1.5, "5s", "500ms", "2m", "1h", "1.5s")
    },
}

REJECT = {
    "missing-autobot": {k: v for k, v in MIN.items() if k != "autobot"},
    "missing-attach": {k: v for k, v in MIN.items() if k != "attach"},
    "missing-script": {k: v for k, v in MIN.items() if k != "script"},
    "missing-spawn": d(attach={}),
    "missing-prompt-expect": d(prompts=[{"name": "p"}]),
    "missing-block-name": s({"block": {}}),
    "extra-top-level": d(bogus=1),
    "extra-attach": d(attach={"spawn": "x", "bogus": 1}),
    "extra-prompt": prompt(bogus=1),
    "extra-cmd": s({"cmd": "x", "bogus": 1}),
    "extra-block": s({"block": {"name": "b", "bogus": 1}}),
    "bad-version": d(autobot="2026/08"),
    "bad-duration-unit": s({"sleep": "5x"}),
    "bad-duration-string-number": s({"sleep": "5"}),
    "sleep-with-when": s({"sleep": 1, "when": "true"}),
    "line-with-timeout": s({"line": "x", "timeout": 1}),
    "return-with-timeout": s({"return": 1, "timeout": 1}),
    "mixed-send": prompt(send=["a", ["b"]]),
}


def test_p6_10_schema_is_valid_draft_2020_12(schema_validator: Any, schema: dict[str, Any]):
    """SPEC.md:12: the schema itself is a valid 2020-12 schema."""
    type(schema_validator).check_schema(schema)


@pytest.mark.parametrize("doc", list(ACCEPT.values()), ids=list(ACCEPT))
def test_p6_11_parity_accept_corpus(both_validate: Callable, doc: dict[str, Any]):
    """SPEC.md:12: valid documents are accepted by both."""
    assert both_validate(doc) == (True, True)


@pytest.mark.parametrize("doc", list(REJECT.values()), ids=list(REJECT))
def test_p6_12_parity_reject_corpus(both_validate: Callable, doc: dict[str, Any]):
    """SPEC.md:12: invalid documents are rejected by both."""
    assert both_validate(doc) == (False, False)


def test_p6_13_parity_mixed_expect(both_validate: Callable):
    """SPEC.md:37: an expect list may mix strings and lists; both accept it (finding #11)."""
    doc = d(prompts=[{"name": "p", "expect": ["a", ["b", "c"]]}])
    assert both_validate(doc) == (True, True)


def test_p6_14_parity_version_trailing_newline():
    """SPEC.md:24: ``"2026-08\\n"`` is not YYYY-MM; the model rejects it (finding #11).

    Only the model side is asserted. Python's ``jsonschema`` evaluates
    ``pattern`` with ``re.search``, where ``$`` also matches before a
    trailing newline, so the Python validator accepts it too (an ECMA-262
    validator would not).
    """
    assert model_ok(d(autobot="2026-08\n")) is False


# -- P6-15..18: the schema is normative (decision #11) ----------------------


def model_errors(doc: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        Config.model_validate(doc)
    except pydantic.ValidationError as e:
        return e.errors()
    raise AssertionError("the model accepted the document")


@pytest.mark.parametrize("value", [0, -2, True, "1"], ids=["zero", "negative", "bool", "string"])
def test_p6_15_parity_return_minimum(both_validate: Callable, value: Any):
    """SPEC "return": the value is an integer >= 1; both reject anything else."""
    doc = s({"return": value})
    assert both_validate(doc) == (False, False)
    assert any(e["loc"][:3] == ("script", 0, "return") for e in model_errors(doc))


def test_p6_16_parity_negative_duration(both_validate: Callable):
    """SPEC "Duration Format": a bare number must be >= 0."""
    doc = s({"sleep": -1})
    assert both_validate(doc) == (False, False)
    [err] = model_errors(doc)
    assert err["loc"] == ("script", 0, "sleep", "sleep")
    assert "invalid duration" in err["msg"]


def test_p6_17_parity_bool_duration(both_validate: Callable):
    """SPEC "Duration Format": a boolean is not a duration."""
    doc = s({"sleep": True})
    assert both_validate(doc) == (False, False)
    [err] = model_errors(doc)
    assert err["loc"] == ("script", 0, "sleep", "sleep")
    assert "invalid duration" in err["msg"]


def test_p6_18_parity_fn_script_required(both_validate: Callable):
    """SPEC "fn": each function's ``script`` is required (it may be empty)."""
    doc = d(fn={"f": {}})
    assert both_validate(doc) == (False, False)
    [err] = model_errors(doc)
    assert err["type"] == "missing"
    assert err["loc"] == ("fn", "f", "script")
    assert both_validate(d(fn={"f": {"script": []}})) == (True, True)


STRICT_BOOL_VALUES = ["yes", "true", 1, 0]


@pytest.mark.parametrize("value", STRICT_BOOL_VALUES, ids=["yes", "true-str", "one", "zero"])
def test_p6_strict_bool_ignore_error(both_validate: Callable, value: Any):
    """Decision #11: ``ignore_error`` is a strict boolean; both reject coercible values."""
    doc = s({"cmd": "x", "ignore_error": value})
    assert both_validate(doc) == (False, False)
    [err] = model_errors(doc)
    assert err["loc"] == ("script", 0, "cmd", "ignore_error")
    assert err["type"] == "bool_type"


@pytest.mark.parametrize("value", STRICT_BOOL_VALUES, ids=["yes", "true-str", "one", "zero"])
def test_p6_strict_bool_prompt_return(both_validate: Callable, value: Any):
    """SPEC "prompts": ``return`` is an optional boolean; both reject coercible values."""
    doc = prompt(**{"return": value})
    assert both_validate(doc) == (False, False)
    [err] = model_errors(doc)
    assert err["loc"] == ("prompts", 0, "return")
    assert err["type"] == "bool_type"
