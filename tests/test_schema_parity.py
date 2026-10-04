"""P6-10..18, P6-41..44, P6-49, P6-51, P6-53, P6-58, P6-60: the pydantic models and schemas/autobot.2026-10.json agree.

SPEC.md:12 and 18 say the models validate against the JSON schema, so the
same document must be accepted or rejected by both.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any

import pydantic
import pytest
import yaml
from test_models import (
    EMPTY_BAD,
    RETURN_BAD,
    RETURN_OK,
    SEND_EACH_BAD,
    SEND_EACH_OK,
    SIMPLE_BAD,
    SIMPLE_OK,
)

from autobot import models
from autobot.models import Config

MIN: dict[str, Any] = {"autobot": "2026-10", "attach": {"spawn": "ssh host"}, "script": []}


def d(**kw: Any) -> dict[str, Any]:
    doc = copy.deepcopy(MIN)
    doc.update(kw)
    return doc


def s(*steps: dict[str, Any]) -> dict[str, Any]:
    return d(script=list(steps))


def prompt(**kw: Any) -> dict[str, Any]:
    return d(prompts=[{"name": "p", "expect": ["x"], **kw}])


FIELDS = [{"match": ["login:", "Username:"], "field": "u"}, {"match": "Password:", "field": "p"}]


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
    "send-string": prompt(send="yes"),
    "send-empty-string": prompt(send=""),
    "send-each": d(prompts=[{"name": "p", "send": {"each": "vars.creds", "fields": FIELDS}}]),
    "send-each-no-fields": prompt(send={"each": "vars.pins"}),
    "send-each-nested": d(prompts=[{"name": "p", "send": {"each": "vars.site.creds", "fields": FIELDS}}]),
    "expect-string": d(prompts=[{"name": "p", "expect": "login:"}]),
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
    "send-flat": prompt(send=["a", "b"]),
    "send-grouped": prompt(send=[["a", "b"], ["c", "d"]]),
    "mixed-send": prompt(send=["a", ["b"]]),
    "send-bool": prompt(send=True),
    "expect-grouped": d(prompts=[{"name": "p", "expect": [["login:", "Password:"]]}]),
    "send-each-env": prompt(send={"each": "env.CREDS"}),
    "send-each-bare-key": prompt(send={"each": "creds"}),
    "send-each-bare-vars": prompt(send={"each": "vars"}),
    "send-each-empty-key": prompt(send={"each": "vars..creds"}),
    "old-version": d(autobot="2026-08"),
    "other-version": d(autobot="2026-11"),
    "send-each-old-fields-list": d(prompts=[{"name": "p", "send": {"each": "vars.creds", "fields": ["u", "p"]}}]),
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
    """SPEC prompts: every expect entry is a single regex, so a list in it is rejected by both."""
    doc = d(prompts=[{"name": "p", "expect": ["a", ["b", "c"]]}])
    assert both_validate(doc) == (False, False)


def test_p6_14_parity_version_trailing_newline(both_validate: Callable):
    """SPEC "Top-level fields": ``"2026-10\\n"`` is not ``2026-10`` (finding #11).

    The schema's ``const`` compares whole strings, so both reject it now
    (with the old ``pattern``, Python's ``re.search`` accepted it).
    """
    assert both_validate(d(autobot="2026-10\n")) == (False, False)


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


# a value that drops its key from the built document, so P6-42 can omit a key the host sets
OMIT: Any = object()


def _drop_omitted(node: Any) -> Any:
    if isinstance(node, dict):
        return {k: _drop_omitted(v) for k, v in node.items() if v is not OMIT}
    if isinstance(node, list):
        return [_drop_omitted(v) for v in node]
    return node


def _prompt_host(fields: dict[str, Any]) -> dict[str, Any]:
    """A prompt needs expect, or a send with fields entries (and then no expect); keep the tested key free."""
    base = (
        {"name": "p", "expect": ["x"]}
        if "send" in fields
        else {"name": "p", "send": {"each": "vars.c", "fields": [{"match": "x", "field": "f"}]}}
    )
    return d(prompts=[{**base, **fields}])


HOSTS: dict[type[pydantic.BaseModel], tuple[Build, Get]] = {
    models.Config: (lambda f: d(**f), lambda c: c),
    models.Attach: (lambda f: d(attach={"spawn": "ssh host", **f}), lambda c: c.attach),
    models.Breakout: (lambda f: d(attach={"spawn": "ssh host", "breakout": f}), lambda c: c.attach.breakout),
    models.Prompt: (lambda f: _prompt_host(f), lambda c: c.prompts[0]),
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

    ``PluginStep`` is left to P6-43 and P6-60: a plugin step needs a registered plugin.
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
    doc = _drop_omitted(build({key: OMIT}))
    assert both_validate(doc) == (True, True)
    name, default = optional_fields(model)[key]
    assert getattr(get(Config.model_validate(doc)), name) == default


@pytest.mark.parametrize("key", sorted(models._COMMON_PROPS - {"plugin_key_"}))
def test_p6_43_null_plugin_common_prop(both_validate: Callable, probe: Any, key: str):
    """SPEC "Common Step Properties" apply to plugin steps, and an explicit null is rejected there too.

    The static schema's ``pluginStep`` applies ``stepCommon``, so it rejects the null as well.
    """
    doc = s({"probe": "x", key: None})
    assert both_validate(doc) == (False, False)
    [err] = model_errors(doc)
    assert err["loc"] == ("script", 0, "plugin", key)
    assert err["type"] == "null_value"
    assert both_validate(s({"probe": "x"})) == (True, True)


def test_p6_44_parity_null_sleep(both_validate: Callable):
    """SPEC "Duration Format": null is not a duration, so a bare ``sleep:`` is rejected (it used to sleep 0)."""
    doc = s({"sleep": None})
    assert both_validate(doc) == (False, False)
    [err] = model_errors(doc)
    assert err["loc"] == ("script", 0, "sleep", "sleep")
    assert err["msg"] == "Value error, invalid duration: null"


# -- P6-49: sendEach fields entries, model and schema agree -----------------


@pytest.mark.parametrize("doc", list(SEND_EACH_OK.values()), ids=list(SEND_EACH_OK))
def test_p6_49_parity_send_each_accepted(both_validate: Callable, doc: dict[str, Any]):
    """SPEC sendEach: every accepted shape is accepted by both."""
    assert both_validate(doc) == (True, True)


@pytest.mark.parametrize("doc", [c[0] for c in SEND_EACH_BAD.values()], ids=list(SEND_EACH_BAD))
def test_p6_49_parity_send_each_rejected(both_validate: Callable, doc: dict[str, Any]):
    """SPEC sendEach: every rule is in the schema too, so no rejection is model-only."""
    assert both_validate(doc) == (False, False)


# -- P6-51: a return prompt has no send -------------------------------------


@pytest.mark.parametrize("doc", list(RETURN_OK.values()), ids=list(RETURN_OK))
def test_p6_51_parity_return_without_send_accepted(both_validate: Callable, doc: dict[str, Any]):
    """SPEC prompts: both accept `return: true` without send, and send with `return: false`."""
    assert both_validate(doc) == (True, True)


@pytest.mark.parametrize("doc", [c[0] for c in RETURN_BAD.values()], ids=list(RETURN_BAD))
def test_p6_51_parity_return_with_send_rejected(both_validate: Callable, doc: dict[str, Any]):
    """SPEC prompts: both reject `return: true` with any send form, including a fields prompt with no expect."""
    assert both_validate(doc) == (False, False)


# -- P6-53: simple prompts, model and schema agree --------------------------


@pytest.mark.parametrize("doc", list(SIMPLE_OK.values()), ids=list(SIMPLE_OK))
def test_p6_53_parity_simple_prompt_accepted(both_validate: Callable, doc: dict[str, Any]):
    """SPEC prompts: both accept a single send string and an expect regex or list of regexes."""
    assert both_validate(doc) == (True, True)


@pytest.mark.parametrize("doc", [c[0] for c in SIMPLE_BAD.values()], ids=list(SIMPLE_BAD))
def test_p6_53_parity_removed_prompt_forms_rejected(both_validate: Callable, doc: dict[str, Any]):
    """SPEC "Migrating from 2026-08": both reject a send list, a non-string send and a grouped expect."""
    assert both_validate(doc) == (False, False)


# -- P6-58: expect and match are never empty, model and schema agree ----------


@pytest.mark.parametrize("doc", [c[0] for c in EMPTY_BAD.values()], ids=list(EMPTY_BAD))
def test_p6_58_parity_empty_expect_or_match_rejected(both_validate: Callable, doc: dict[str, Any]):
    """SPEC prompts, sendEach: `expect: []` and an empty regex are rejected by both (schema: minItems, minLength)."""
    assert both_validate(doc) == (False, False)


def test_p6_58_parity_whitespace_regex_accepted(both_validate: Callable):
    """SPEC prompts: both accept a regex of a single space; only the empty string is rejected."""
    assert both_validate(d(prompts=[{"name": "p", "expect": " ", "send": "y"}])) == (True, True)
    fields = [{"match": [" "], "field": "u"}]
    assert both_validate(d(prompts=[{"name": "p", "send": {"each": "vars.c", "fields": fields}}])) == (True, True)


# -- P6-60: the static pluginStep checks the common step properties ---------

PLUGIN_COMMON_BAD = {
    "timeout-space": {"timeout": "5 s"},
    "timeout-bool": {"timeout": True},
    "when-null": {"when": None},
    "after-int": {"after": 3},
    "delay_before-negative": {"delay_before": -1},
}
PLUGIN_COMMON_GOOD = {
    "none": {},
    "timeout": {"timeout": "5s"},
    "all": {"after": "ready", "when": "{{ x }}", "delay_before": "500ms", "delay_after": 2, "timeout": 5},
}


@pytest.mark.parametrize("props", list(PLUGIN_COMMON_BAD.values()), ids=list(PLUGIN_COMMON_BAD))
def test_p6_60_parity_plugin_step_bad_common_prop_rejected(
    both_validate: Callable, probe: Any, props: dict[str, Any]
):
    """SPEC "Common Step Properties": an invalid common prop on a plugin step is rejected by both.

    For a key no plugin registers the model reports ``invalid_step`` for the step; for a registered
    key it reports the prop itself.
    """
    [(key, _)] = props.items()
    unknown = s({"nope": 1, **props})
    assert both_validate(unknown) == (False, False)
    assert [(e["loc"], e["type"]) for e in model_errors(unknown)] == [(("script", 0), "invalid_step")]
    known = s({"probe": "x", **props})
    assert both_validate(known) == (False, False)
    assert [e["loc"] for e in model_errors(known)] == [("script", 0, "plugin", key)]


@pytest.mark.parametrize("props", list(PLUGIN_COMMON_GOOD.values()), ids=list(PLUGIN_COMMON_GOOD))
def test_p6_60_parity_plugin_step_good_common_prop_accepted(
    both_validate: Callable, probe: Any, props: dict[str, Any]
):
    """Valid common props: the static schema still accepts an unknown key (the divergence SPEC allows)."""
    assert both_validate(s({"nope": 1, **props})) == (False, True)
    assert both_validate(s({"probe": "x", **props})) == (True, True)


# -- P6-61..62: non-finite durations and non-ASCII digits --------------------

NONFINITE_YAML = {"nan": ".nan", "inf": ".inf", "-inf": "-.inf"}
DURATION_FIELDS: dict[str, Callable[[Any], dict[str, Any]]] = {
    "sleep": lambda v: s({"sleep": v}),
    "attach.timeout": lambda v: d(attach={"spawn": "ssh host", "timeout": v}),
    "cmd.timeout": lambda v: s({"cmd": "x", "timeout": v}),
    "cmd.delay_before": lambda v: s({"cmd": "x", "delay_before": v}),
    "cmd.delay_after": lambda v: s({"cmd": "x", "delay_after": v}),
    "block.timeout": lambda v: s({"block": {"name": "b", "script": [{"cmd": "x"}]}, "timeout": v}),
    "block.inner.timeout": lambda v: s({"block": {"name": "b", "script": [{"cmd": "x", "timeout": v}]}}),
    "call.timeout": lambda v: s({"call": "f", "timeout": v}),
    "control.timeout": lambda v: s({"control": "c", "timeout": v}),
    "line.delay_before": lambda v: s({"line": "x", "delay_before": v}),
    "return.delay_after": lambda v: s({"return": 1, "delay_after": v}),
    "fn.step.timeout": lambda v: d(fn={"f": {"script": [{"cmd": "x", "timeout": v}]}}),
}


@pytest.mark.parametrize("field", list(DURATION_FIELDS))
@pytest.mark.parametrize("literal", list(NONFINITE_YAML.values()), ids=list(NONFINITE_YAML))
def test_p6_61_nonfinite_duration_rejected_on_load(literal: str, field: str):
    """SPEC "Duration Format": YAML ``.nan``/``.inf``/``-.inf`` are rejected when the script loads.

    A ``timeout: .nan`` made ``get_prompt`` wait forever. JSON can't carry NaN, so
    only the models see these, and they must reject them in every duration field.
    """
    value = yaml.safe_load(f"v: {literal}")["v"]
    assert isinstance(value, float)
    errs = model_errors(DURATION_FIELDS[field](value))
    assert errs, field
    assert all("not a finite number" in e["msg"] for e in errs)


# -- P6-63: control values ---------------------------------------------------

CONTROL_CHARS = [*"abcdefghijklmnopqrstuvwxyz", *"ABCDEFGHIJKLMNOPQRSTUVWXYZ", *"@`[{\\|]}^~_?"]
CONTROL_BAD = {
    "empty": "",
    "two": "ab",
    "digit": "1",
    "space": " ",
    "newline-after": "a\n",
    "non-ascii": "é",
    "kelvin": "K",  # lowercases to "k"
    "caret-name": "^C",
    "in-list": ["a", "ab"],
    "empty-in-list": ["a", ""],
    "number": 3,
}


@pytest.mark.parametrize("value", [*CONTROL_CHARS, CONTROL_CHARS, []], ids=repr)
def test_p6_63_parity_control_accepted(both_validate: Callable, value: Any):
    """SPEC "control": every key with a control character is accepted by both, alone or in a list (even an empty one)."""
    assert both_validate(s({"control": value})) == (True, True)


@pytest.mark.parametrize("value", list(CONTROL_BAD.values()), ids=list(CONTROL_BAD))
def test_p6_63_parity_control_rejected(both_validate: Callable, value: Any):
    """SPEC "control": a value that isn't exactly one control key is rejected by both, at the step's control."""
    doc = s({"control": value})
    assert both_validate(doc) == (False, False)
    assert all(e["loc"][:3] == ("script", 0, "control") for e in model_errors(doc))
    if isinstance(value, (str, list)):
        [err] = model_errors(doc)
        assert (err["loc"], err["type"]) == (("script", 0, "control", "control"), "control_char")


def test_p6_63_control_chars_are_what_sendcontrol_maps():
    """SPEC "control": the accepted characters are exactly the ASCII ones pexpect's sendcontrol sends a byte for."""
    import ptyprocess

    class Pty:
        @staticmethod
        def _writeb(b: bytes) -> int:
            return len(b)

    sends = {c for c in map(chr, range(32, 127)) if ptyprocess.PtyProcess.sendcontrol(Pty(), c)[0]}
    assert sends == set(CONTROL_CHARS)
    assert {c for c in map(chr, range(32, 127)) if models.CONTROL_RE.fullmatch(c)} == sends


# -- P6-64..66: empty patterns and names, invalid regexes, empty lists --------

FIELDS_BAD_RE = [{"match": ["ok", "("], "field": "u"}]
# doc -> (location of the model's error, its type)
EMPTY_VALUE_BAD: dict[str, tuple[dict[str, Any], tuple, str]] = {
    "assert-empty": (s({"cmd": "x", "assert": ""}), ("script", 0, "cmd", "assert"), "string_too_short"),
    "assert-empty-in-list": (s({"cmd": "x", "assert": ["ok", ""]}), ("script", 0, "cmd", "assert"), "string_too_short"),
    "errors-empty": (d(errors=["% .*", ""]), ("errors", 1), "string_too_short"),
    "register-empty": (s({"cmd": "x", "register": ""}), ("script", 0, "cmd", "register"), "string_too_short"),
    "spawn-empty": (d(attach={"spawn": ""}), ("attach", "spawn"), "empty_command"),
    "spawn-blank": (d(attach={"spawn": "  "}), ("attach", "spawn"), "empty_command"),
    "spawn-newline": (d(attach={"spawn": "\n"}), ("attach", "spawn"), "empty_command"),
}
INVALID_REGEX: dict[str, tuple[dict[str, Any], tuple]] = {
    "errors": (d(errors=["% .*", "("]), ("errors", 1)),
    "expect": (d(prompts=[{"name": "p", "expect": "("}]), ("prompts", 0, "expect")),
    "expect-in-list": (d(prompts=[{"name": "p", "expect": ["ok", "[a"]}]), ("prompts", 0, "expect", 1)),
    "match": (
        d(prompts=[{"name": "p", "send": {"each": "vars.c", "fields": [{"match": "(", "field": "u"}]}}]),
        ("prompts", 0, "send", "fields", 0, "match"),
    ),
    "match-in-list": (
        d(prompts=[{"name": "p", "send": {"each": "vars.c", "fields": FIELDS_BAD_RE}}]),
        ("prompts", 0, "send", "fields", 0, "match", 1),
    ),
    "block-expect": (
        s({"block": {"name": "b", "prompts": [{"name": "p", "expect": ["*"]}]}}),
        ("script", 0, "block", "block", "prompts", 0, "expect", 0),
    ),
    "assert": (s({"cmd": "x", "assert": "("}), ("script", 0, "cmd", "assert")),
    "assert-in-list": (s({"cmd": "x", "assert": ["ok", "a{2,1}"]}), ("script", 0, "cmd", "assert")),
    "after-cmd": (s({"cmd": "x", "after": "("}), ("script", 0, "cmd", "after")),
    "after-line": (s({"line": "x", "after": "(?P<n"}), ("script", 0, "line", "after")),
    "after-in-fn": (d(fn={"f": {"script": [{"control": "c", "after": ")"}]}}), ("fn", "f", "script", 0, "control", "after")),
}
STILL_ACCEPTED = {
    "assert-empty-list": s({"cmd": "x", "assert": []}),
    "cmd-empty-list": s({"cmd": []}),
    "cmd-empty-string": s({"cmd": ""}),
    "line-empty-list": s({"line": []}),
    "control-empty-list": s({"control": []}),
    "errors-empty-list": d(errors=[]),
    "assert-blank": s({"cmd": "x", "assert": " "}),
    "assert-template-of-invalid-regex": s({"cmd": "x", "assert": "{{ '(' }}"}),
    "assert-template-invalid-as-written": s({"cmd": "x", "assert": ["({{ vars.x }}"]}),
    "after-template-invalid-as-written": s({"cmd": "x", "after": "{% if vars.x %}({% endif %}"}),
    "spawn-template": d(attach={"spawn": "{{ args.spawn }}"}),
    "register-any-name": s({"cmd": "x", "register": "values"}),
    "valid-regexes": d(
        errors=["^% .*", "(?i)error"],
        prompts=[{"name": "p", "expect": [r"[\w.-]+[$#] ?$", r"\(config[^)]*\)# $"]}],
        script=[{"cmd": "x", "assert": [r"a{2}", r"\d+ packets"], "after": r"login: $"}],
    ),
}


@pytest.mark.parametrize("case", EMPTY_VALUE_BAD)
def test_p6_64_parity_empty_pattern_name_or_command_rejected(both_validate: Callable, case: str):
    """SPEC cmd, top-level fields, attach: an empty assert or errors pattern, register name or spawn is rejected by both."""
    doc, loc, type_ = EMPTY_VALUE_BAD[case]
    assert both_validate(doc) == (False, False)
    assert [(e["loc"], e["type"]) for e in model_errors(doc)] == [(loc, type_)]


@pytest.mark.parametrize("case", INVALID_REGEX)
def test_p6_65_invalid_regex_rejected_on_load(both_validate: Callable, case: str):
    """SPEC "YAML Script Structure": a regex that doesn't compile is rejected when the script is loaded.

    A static schema can't check Python regex syntax, so only the models reject it.
    """
    doc, loc = INVALID_REGEX[case]
    assert both_validate(doc) == (False, True)
    [err] = model_errors(doc)
    assert (err["loc"], err["type"]) == (loc, "invalid_regex")
    assert err["msg"].startswith("invalid regex '")


@pytest.mark.parametrize("doc", list(STILL_ACCEPTED.values()), ids=list(STILL_ACCEPTED))
def test_p6_66_parity_empty_lists_and_templates_accepted(both_validate: Callable, doc: dict[str, Any]):
    """Empty lists stay valid for cmd, line, control and assert, and a templated regex is checked only once rendered."""
    assert both_validate(doc) == (True, True)


@pytest.mark.parametrize("value", ["٥s", "1.٥s"])
def test_p6_62_parity_non_ascii_digit_duration(both_validate: Callable, value: str):
    """SPEC "Duration Format": the schema's ``[0-9]`` and the model agree on non-ASCII digits."""
    assert both_validate(s({"sleep": value})) == (False, False)
    assert both_validate(s({"sleep": "5s"})) == (True, True)
