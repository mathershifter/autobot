"""P6-10..18, P6-41..44: the pydantic models and schemas/autobot.2026-08.json agree.

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

from autobot import models
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
    "send-each-nested": prompt(send={"each": "vars.site.creds", "fields": ["u", "p"]}),
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
    "send-each-env": prompt(send={"each": "env.CREDS"}),
    "send-each-bare-key": prompt(send={"each": "creds"}),
    "send-each-bare-vars": prompt(send={"each": "vars"}),
    "send-each-empty-key": prompt(send={"each": "vars..creds"}),
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


# -- P6-41..44: an explicit null for an optional field (decision #11) -------

# Each model with optional fields and where it sits in a document: `build(fields)` returns a
# minimal document holding one instance with `fields` set; `get(config)` returns that instance.
Build = Callable[[dict[str, Any]], dict[str, Any]]
Get = Callable[[Config], Any]


def _step_host(base: dict[str, Any]) -> tuple[Build, Get]:
    def build(fields: dict[str, Any]) -> dict[str, Any]:
        doc = s({**base, **fields})
        if "call" in base:
            doc["fn"] = {"f": {"script": []}}
        return doc

    return build, lambda c: c.script[0]


HOSTS: dict[type[pydantic.BaseModel], tuple[Build, Get]] = {
    models.Config: (lambda f: d(**f), lambda c: c),
    models.Attach: (lambda f: d(attach={"spawn": "ssh host", **f}), lambda c: c.attach),
    models.Breakout: (lambda f: d(attach={"spawn": "ssh host", "breakout": f}), lambda c: c.attach.breakout),
    models.Prompt: (lambda f: prompt(**f), lambda c: c.prompts[0]),
    models.SendEach: (lambda f: prompt(send={"each": "vars.c", **f}), lambda c: c.prompts[0].send),
    models.Block: (lambda f: s({"block": {"name": "b", **f}}), lambda c: c.script[0].block),
    models.CmdStep: _step_host({"cmd": "x"}),
    models.CallStep: _step_host({"call": "f"}),
    models.BlockStep: _step_host({"block": {"name": "b"}}),
    models.LineStep: _step_host({"line": "x"}),
    models.ReturnStep: _step_host({"return": 1}),
    models.ControlStep: _step_host({"control": "c"}),
}


def optional_fields(model: type[pydantic.BaseModel]) -> dict[str, tuple[str, Any]]:
    """``{YAML key: (attribute name, default)}`` for the optional fields of ``model``, internal ones excluded."""
    return {
        f.alias or name: (name, f.get_default(call_default_factory=True))
        for name, f in model.model_fields.items()
        if not f.is_required() and not f.exclude
    }


OPTIONAL = [(model, key) for model in HOSTS for key in optional_fields(model)]
OPTIONAL_IDS = [f"{model.__name__}.{key}" for model, key in OPTIONAL]


def test_p6_41_null_corpus_covers_every_model():
    """Every model with optional fields has a host above, so a new model's fields get null cases.

    ``PluginStep`` is left to P6-43: the static schema's ``pluginStep`` accepts any object.
    """
    with_optional = {
        cls
        for cls in vars(models).values()
        if isinstance(cls, type)
        and issubclass(cls, pydantic.BaseModel)
        and cls.__module__ == models.__name__
        and optional_fields(cls)
    }
    assert with_optional - {models.PluginStep} == set(HOSTS)


@pytest.mark.parametrize(("model", "key"), OPTIONAL, ids=OPTIONAL_IDS)
def test_p6_41_parity_null_optional_field(both_validate: Callable, model: type[pydantic.BaseModel], key: str):
    """SPEC "YAML Script Structure": an explicit null (an empty YAML value) is invalid; both reject it."""
    build, _ = HOSTS[model]
    doc = build({key: None})
    assert both_validate(doc) == (False, False)
    # the send union also reports its list members for a sendEach error; keep the one at the key
    [err] = [e for e in model_errors(doc) if e["loc"][-1] == key]
    if optional_fields(model)[key][1] is None:
        assert err["type"] == "null_value"
        assert err["msg"] == "null (an empty value) is not allowed; omit the key instead"


@pytest.mark.parametrize(("model", "key"), OPTIONAL, ids=OPTIONAL_IDS)
def test_p6_42_omitted_optional_field_gets_default(
    both_validate: Callable, model: type[pydantic.BaseModel], key: str
):
    """Omitting an optional key is valid for both, and the model gives the field its default."""
    build, get = HOSTS[model]
    doc = build({})
    assert both_validate(doc) == (True, True)
    name, default = optional_fields(model)[key]
    assert getattr(get(Config.model_validate(doc)), name) == default


@pytest.mark.parametrize("key", sorted(models._COMMON_PROPS - {"plugin_key_"}))
def test_p6_43_null_plugin_common_prop(probe: Any, key: str):
    """SPEC "Common Step Properties" apply to plugin steps, and an explicit null is rejected there too.

    Only the model is checked: the static schema's ``pluginStep`` accepts any object.
    """
    [err] = model_errors(s({"probe": "x", key: None}))
    assert err["loc"] == ("script", 0, "plugin", key)
    assert err["type"] == "null_value"
    assert model_ok(s({"probe": "x"}))


def test_p6_44_parity_null_sleep(both_validate: Callable):
    """SPEC "Duration Format": null is not a duration, so a bare ``sleep:`` is rejected (it used to sleep 0)."""
    doc = s({"sleep": None})
    assert both_validate(doc) == (False, False)
    [err] = model_errors(doc)
    assert err["loc"] == ("script", 0, "sleep", "sleep")
    assert err["msg"] == "Value error, invalid duration: null"
