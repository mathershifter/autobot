"""P6-10..18, P6-41..44, P6-49, P6-51, P6-53, P6-58, P6-60..66, P6-68..73, P6-75, P6-82: the pydantic models and schemas/autobot.2026-10.json agree.

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
            "breakout": [{"control": "]"}],
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
                "breakout": [{"line": "exit"}],
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
    # every optional field, whatever its default: `env:`, `prompts:`, `ignore_error:` as much as `timeout:`
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
    """SPEC "prompts": both reject a send list, a non-string send and a grouped expect."""
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
    "kelvin": chr(0x212A),  # lowercases to "k"
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


@pytest.mark.parametrize("value", ["", "ab", "1", "'", '"', "a\n", "\\n", "{x}"])
def test_p6_63_control_message_quotes_the_value_as_written(value: str):
    """SPEC "control": the message ends `got '<value>'` for every value, also one with a quote or a backslash."""
    [err] = model_errors(s({"control": value}))
    assert err["msg"] == (
        f"a control value is one character, a letter or one of @ ` [ {{ \\ | ] }} ^ ~ _ ?, got '{value}'"
    )


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
    # whitespace for Python (and pexpect's command-line split) but not for an ECMA `\s`, or the other way round
    "spawn-file-separator": (d(attach={"spawn": "\x1c"}), ("attach", "spawn"), "empty_command"),
    "spawn-c0-separators": (d(attach={"spawn": " \x1d\x1e\x1f"}), ("attach", "spawn"), "empty_command"),
    "spawn-unicode-spaces": (
        d(attach={"spawn": "".join(map(chr, [0x85, 0xA0, 0x1680, 0x2003, 0x2028, 0x2029, 0x202F, 0x205F, 0x3000]))}),
        ("attach", "spawn"),
        "empty_command",
    ),
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
    "spawn-bom": d(attach={"spawn": chr(0xFEFF)}),  # whitespace for an ECMA `\s`, not for Python: not blank
    "spawn-zero-width-space": d(attach={"spawn": chr(0x200B)}),
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


def test_p6_64_blank_spawn_class_is_shared_and_is_python_whitespace(schema: dict[str, Any]):
    """SPEC "attach": schema and model use one explicit character class, not each engine's own `\\S`.

    It is exactly `str.isspace`, which is what pexpect splits the command on and what the runner
    strips from a rendered spawn.
    """
    import re
    import sys

    assert schema["$defs"]["attach"]["properties"]["spawn"]["pattern"] == models.NOT_BLANK
    not_blank = re.compile(models.NOT_BLANK)
    differ = [hex(c) for c in range(sys.maxunicode + 1) if bool(not_blank.fullmatch(chr(c))) == chr(c).isspace()]
    assert differ == []


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


# -- P6-68..70: values a YAML file can hold and JSON can't, or holds differently ----


@pytest.mark.parametrize("literal", ["1.0", "2.0", "3.0e+0", "1.0e+1"])
def test_p6_68_parity_return_whole_float_accepted(both_validate: Callable, literal: str):
    """SPEC "return": a number with a zero fraction is that integer, as in JSON; both accept it."""
    value = yaml.safe_load(f"v: {literal}")["v"]
    assert isinstance(value, float)
    doc = s({"return": value})
    assert both_validate(doc) == (True, True)
    count = Config.model_validate(doc).script[0].newline_count
    assert (count, type(count)) == (int(value), int)


@pytest.mark.parametrize("literal", ["1.5", "0.0", "-1.0", "0.999", ".inf", "-.inf", ".nan", "1.0e+400"])
def test_p6_68_parity_return_other_float_rejected(both_validate: Callable, literal: str):
    """SPEC "return": a fraction, a whole number below 1 and a non-finite number are rejected by both."""
    doc = s({"return": yaml.safe_load(f"v: {literal}")["v"]})
    assert both_validate(doc) == (False, False)
    assert all(e["loc"][:3] == ("script", 0, "return") for e in model_errors(doc))


FLOAT_MAX = 1.7976931348623157e308
# the first integer above the largest double; `float()` would round it down to FLOAT_MAX
ABOVE_FLOAT_MAX = int(FLOAT_MAX) + 1
TOO_LARGE = {
    "inf": float("inf"),
    "-inf": float("-inf"),
    "10**400": 10**400,
    "-10**400": -(10**400),
    "2**1024": 2**1024,
    "above-float-max": ABOVE_FLOAT_MAX,
}


def test_p6_69_schema_duration_maximum_is_the_largest_double(schema: dict[str, Any]):
    """SPEC "Duration Format": the schema's bound and the model's are the same number."""
    import sys

    from autobot import types

    [number] = [alt for alt in schema["$defs"]["duration"]["oneOf"] if alt["type"] == "number"]
    assert number == {"type": "number", "minimum": 0, "maximum": sys.float_info.max}
    assert types.DURATION_MAX == sys.float_info.max == FLOAT_MAX


@pytest.mark.parametrize("field", list(DURATION_FIELDS))
@pytest.mark.parametrize("value", list(TOO_LARGE.values()), ids=list(TOO_LARGE))
def test_p6_69_parity_duration_beyond_a_double_rejected(both_validate: Callable, value: Any, field: str):
    """SPEC "Duration Format": `.inf`, `-.inf` and a number above the largest double are rejected by both.

    The schema used to accept `timeout: .inf` and an integer of 400 digits (`minimum: 0` only).
    """
    doc = DURATION_FIELDS[field](value)
    if field == "call.timeout":
        doc["fn"] = {"f": {"script": []}}
    assert both_validate(doc) == (False, False)
    errs = model_errors(doc)
    assert errs and all("invalid duration" in e["msg"] for e in errs)


@pytest.mark.parametrize("value", [FLOAT_MAX, int(FLOAT_MAX), 1e308, 10**308, 0, 0.0], ids=repr)
def test_p6_69_parity_duration_up_to_a_double_accepted(both_validate: Callable, value: Any):
    """SPEC "Duration Format": the bound is inclusive, and nothing below it is cut off."""
    doc = s({"sleep": value})
    assert both_validate(doc) == (True, True)
    assert Config.model_validate(doc).script[0].sleep == float(value)


@pytest.mark.parametrize("value", [float("nan"), "9" * 400 + "s", "1" + "0" * 310 + "h"], ids=["nan", "400-digits", "hours"])
def test_p6_69_duration_only_the_models_can_reject(both_validate: Callable, value: Any):
    """SPEC "YAML Script Structure": `.nan`, and a string that overflows, are the stated model-only cases.

    No `minimum` or `maximum` compares with NaN, and a pattern can't compute a string's value.
    """
    doc = s({"sleep": value})
    assert both_validate(doc) == (False, True)
    [err] = model_errors(doc)
    assert "not a finite number" in err["msg"]


BINARY = yaml.safe_load("v: !!binary aGk=")["v"]
FN = {"f": {"script": []}}
# every place a script holds a string: doc(value) -> a document with `value` there
STRING_FIELDS: dict[str, Callable[[Any], dict[str, Any]]] = {
    "autobot": lambda v: d(autobot=v),
    "env-value": lambda v: d(env={"A": v}),
    "errors-item": lambda v: d(errors=[v]),
    "prompt-name": lambda v: d(prompts=[{"name": v, "expect": "x"}]),
    "prompt-expect": lambda v: d(prompts=[{"name": "p", "expect": v}]),
    "prompt-expect-item": lambda v: d(prompts=[{"name": "p", "expect": ["x", v]}]),
    "prompt-send": lambda v: prompt(send=v),
    "send-each": lambda v: prompt(send={"each": v}),
    "fields-match": lambda v: d(prompts=[{"name": "p", "send": {"each": "vars.c", "fields": [{"match": v, "field": "u"}]}}]),
    "fields-match-item": lambda v: d(
        prompts=[{"name": "p", "send": {"each": "vars.c", "fields": [{"match": ["x", v], "field": "u"}]}}]
    ),
    "fields-field": lambda v: d(prompts=[{"name": "p", "send": {"each": "vars.c", "fields": [{"match": "x", "field": v}]}}]),
    "attach-spawn": lambda v: d(attach={"spawn": v}),
    "attach-prepare": lambda v: d(attach={"spawn": "ssh host", "prepare": v}),
    "attach-env-value": lambda v: d(attach={"spawn": "ssh host", "env": {"A": v}}),
    "cmd": lambda v: s({"cmd": v}),
    "cmd-item": lambda v: s({"cmd": ["x", v]}),
    "cmd-assert": lambda v: s({"cmd": "x", "assert": v}),
    "cmd-assert-item": lambda v: s({"cmd": "x", "assert": ["x", v]}),
    "cmd-register": lambda v: s({"cmd": "x", "register": v}),
    "cmd-after": lambda v: s({"cmd": "x", "after": v}),
    "cmd-when": lambda v: s({"cmd": "x", "when": v}),
    "call": lambda v: d(fn=FN, script=[{"call": v}]),
    "call-after": lambda v: d(fn=FN, script=[{"call": "f", "after": v}]),
    "block-name": lambda v: s({"block": {"name": v}}),
    "block-when": lambda v: s({"block": {"name": "b"}, "when": v}),
    "line": lambda v: s({"line": v}),
    "line-item": lambda v: s({"line": ["x", v]}),
    "line-after": lambda v: s({"line": "x", "after": v}),
    "return-when": lambda v: s({"return": 1, "when": v}),
    "control": lambda v: s({"control": v}),
    "control-item": lambda v: s({"control": ["a", v]}),
    "control-after": lambda v: s({"control": "a", "after": v}),
    "fn-step": lambda v: d(fn={"f": {"script": [{"cmd": v}]}}),
    "duration": lambda v: s({"sleep": v}),
}


@pytest.mark.parametrize("field", list(STRING_FIELDS))
def test_p6_70_parity_binary_is_not_a_string(both_validate: Callable, field: str):
    """SPEC "YAML Script Structure": a `!!binary` value isn't a string; both reject it wherever a string goes.

    The models used to decode it (`cmd: !!binary aGk=` ran `hi`), while the schema rejected it.
    """
    assert BINARY == b"hi"
    doc = STRING_FIELDS[field](BINARY)
    assert both_validate(doc) == (False, False)
    # and the same document with the text is fine, so it is the bytes that are rejected
    valid = {"autobot": "2026-10", "send-each": "vars.c", "call": "f", "duration": "5s"}
    text = valid.get(field, "a" if field.startswith("control") else "hi")
    assert both_validate(STRING_FIELDS[field](text)) == (True, True)


@pytest.mark.parametrize("field", ["cmd", "attach-spawn", "env-value", "block-name", "cmd-when"])
def test_p6_70_binary_is_a_string_type_error(field: str):
    """The model's error is the string's own type error, at the value."""
    assert {e["type"] for e in model_errors(STRING_FIELDS[field](BINARY))} <= {"string_type", "list_type"}


@pytest.mark.parametrize("field", ["probe-field", "after", "when"])
def test_p6_70_binary_in_a_plugin_step_common_prop(both_validate: Callable, probe: Any, field: str):
    """A plugin step's common properties are strings in the same sense."""
    doc = s({"probe": "x", **({} if field == "probe-field" else {field: BINARY})})
    assert both_validate(doc) == ((True, True) if field == "probe-field" else (False, False))


# -- P6-71: a spawn that names no command --------------------------------------

SPAWN_NO_COMMAND = ["''", '""', "\\", "'", '"', "''\"\"", "'' ''", "'' ls", '"" --version', " '' "]


@pytest.mark.parametrize("value", SPAWN_NO_COMMAND)
def test_p6_71_spawn_of_quotes_or_a_backslash_rejected_on_load(both_validate: Callable, value: str):
    """SPEC "attach": spawn must name a command. Only the models can split a command line.

    Each of these has a non-blank character, so the schema's pattern accepts it; `pexpect.spawn`
    finds no command name in it (`IndexError` for the first six, a command `''` for the rest).
    """
    doc = d(attach={"spawn": value})
    assert both_validate(doc) == (False, True)
    [err] = model_errors(doc)
    assert (err["loc"], err["type"]) == (("attach", "spawn"), "empty_command")
    assert err["msg"] == (
        f"spawn must name a command: the first word of {value!r} is empty "
        "(quotes or a backslash with nothing in them)"
    )


@pytest.mark.parametrize(
    "value",
    ["'ssh' host", '"ssh" host', "s''sh host", "\\ssh host", "ssh ''", "{{ args.cmd }}", "''{{ args.cmd }}", "{# c #}''"],
)
def test_p6_71_spawn_with_a_command_or_a_template_accepted(both_validate: Callable, value: str):
    """A quoted command name is a command, and a template is checked once rendered (P5-54)."""
    assert both_validate(d(attach={"spawn": value})) == (True, True)


def test_p6_71_names_command_is_what_pexpect_spawns():
    """`names_command` is false exactly when pexpect has no command name to look up."""
    import pexpect

    for value in [*SPAWN_NO_COMMAND, "", "  ", "\x1c", "ssh host", "'a", "\\ ", "' '"]:
        words = pexpect.split_command_line(value)
        assert models.names_command(value) == bool(words and words[0]), value
    assert not models.names_command("''")
    assert models.names_command("'ssh' host")


# -- P6-72: an `autobot` value that isn't a string ------------------------------

# YAML text of the value -> how the message shows it
VERSION_NOT_A_STRING = {
    "2026": "2026",
    "2026.10": "2026.1",
    "2026-10-04": "2026-10-04",
    "202610": "202610",
    "true": "true",
    "no": "false",
    "[2026-10]": "['2026-10']",
    "{v: 2026-10}": "{'v': '2026-10'}",
    "!!binary MjAyNi0xMA==": "b'2026-10'",
    "0": "0",
    "''": "",
}


@pytest.mark.parametrize("literal", list(VERSION_NOT_A_STRING))
def test_p6_72_parity_version_of_another_type_is_unsupported(both_validate: Callable, literal: str):
    """SPEC "Top-level fields": any value but `2026-10` is `unsupported_version`, also one that isn't a string.

    `autobot: 2026` used to be `string_type` ("Input should be a valid string"). The schema's `const` rejects it too.
    """
    value = yaml.safe_load(f"v: {literal}")["v"]
    doc = d(autobot=value)
    assert both_validate(doc) == (False, False)
    [err] = model_errors(doc)
    assert (err["loc"], err["type"]) == (("autobot",), "unsupported_version")
    assert err["msg"] == f"unsupported autobot version {VERSION_NOT_A_STRING[literal]!r}; expected 2026-10"


def test_p6_72_version_null_and_missing_keep_their_errors(both_validate: Callable):
    """An explicit null and a missing key aren't versions: `string_type` and `missing`, as before."""
    doc = d(autobot=None)
    assert both_validate(doc) == (False, False)
    [err] = model_errors(doc)
    assert (err["loc"], err["type"]) == (("autobot",), "string_type")
    missing = {k: v for k, v in MIN.items() if k != "autobot"}
    assert both_validate(missing) == (False, False)
    [err] = model_errors(missing)
    assert (err["loc"], err["type"]) == (("autobot",), "missing")
    assert both_validate(d(autobot=yaml.safe_load("v: 2026-10")["v"])) == (True, True)


# -- P6-73: an empty `after` -----------------------------------------------------

EMPTY_AFTER: dict[str, tuple[dict[str, Any], tuple]] = {
    "cmd": (s({"cmd": "x", "after": ""}), ("script", 0, "cmd", "after")),
    "call": (d(fn=FN, script=[{"call": "f", "after": ""}]), ("script", 0, "call", "after")),
    "block": (s({"block": {"name": "b"}, "after": ""}), ("script", 0, "block", "after")),
    "line": (s({"line": "x", "after": ""}), ("script", 0, "line", "after")),
    "return": (s({"return": 1, "after": ""}), ("script", 0, "return", "after")),
    "control": (s({"control": "c", "after": ""}), ("script", 0, "control", "after")),
    "in-fn": (d(fn={"f": {"script": [{"cmd": "x", "after": ""}]}}), ("fn", "f", "script", 0, "cmd", "after")),
    "in-attach-script": (
        d(attach={"spawn": "ssh host", "script": [{"line": "x", "after": ""}]}),
        ("attach", "script", 0, "line", "after"),
    ),
    "in-breakout": (
        d(attach={"spawn": "ssh host", "breakout": [{"line": "x", "after": ""}]}),
        ("attach", "breakout", 0, "line", "after"),
    ),
    "in-block": (
        s({"block": {"name": "b", "enter": [{"cmd": "x", "after": ""}]}}),
        ("script", 0, "block", "block", "enter", 0, "cmd", "after"),
    ),
}
EMPTY_AFTER_MSG = "an after pattern must not be empty: an empty regex matches at once, so the step would wait for nothing"


@pytest.mark.parametrize("case", EMPTY_AFTER)
def test_p6_73_parity_empty_after_rejected(both_validate: Callable, case: str):
    """SPEC "Common Step Properties": `after: ''` is rejected by both, like an empty `assert` pattern.

    It used to be accepted and silently skipped: the step ran without waiting for anything.
    """
    doc, loc = EMPTY_AFTER[case]
    assert both_validate(doc) == (False, False)
    [err] = model_errors(doc)
    assert (err["loc"], err["type"], err["msg"]) == (loc, "string_too_short", EMPTY_AFTER_MSG)


def test_p6_73_parity_empty_after_on_a_plugin_step_rejected(both_validate: Callable, probe: Any):
    """The common step properties of a plugin step follow the same rule (the schema's `stepCommon`)."""
    doc = s({"probe": "x", "after": ""})
    assert both_validate(doc) == (False, False)
    [err] = model_errors(doc)
    assert (err["loc"], err["type"], err["msg"]) == (("script", 0, "plugin", "after"), "string_too_short", EMPTY_AFTER_MSG)
    # a key no plugin registers: the static schema checks stepCommon, the model rejects the step
    assert both_validate(s({"nope": 1, "after": ""})) == (False, False)
    assert both_validate(s({"nope": 1, "after": "x"})) == (False, True)


def test_p6_73_schema_after_is_non_empty_everywhere(schema: dict[str, Any]):
    """Every definition with an `after` has the same one, so no step type is left out."""
    found = [
        (name, definition["properties"]["after"])
        for name, definition in schema["$defs"].items()
        if "after" in definition.get("properties", {})
    ]
    assert sorted(name for name, _ in found) == [
        "blockStep", "callStep", "cmdStep", "controlStep", "lineStep", "returnStep", "stepCommon",
    ]
    assert all(prop == {"type": "string", "minLength": 1} for _, prop in found)


@pytest.mark.parametrize("value", [" ", "x", "{{ vars.p }}", "{{ '' }}", "{# only a comment #}", "^$"])
def test_p6_73_parity_blank_and_templated_after_accepted(both_validate: Callable, value: str):
    """Only the empty string is rejected at load; a template that renders to nothing is caught when it is used (P3-21)."""
    assert both_validate(s({"cmd": "x", "after": value})) == (True, True)


# -- P6-75: mapping keys are strings -------------------------------------------

# what YAML makes of a key that isn't a string
KEY_LITERALS = {"int": "1", "float": "1.5", "bool": "true", "null": "~", "date": "2026-01-01", "binary": "!!binary aGk="}


def yaml_key(literal: str) -> Any:
    return next(iter(yaml.safe_load(f"{literal}: x")))


# the mappings whose keys the script chooses: doc(key) -> a document with `key` in that mapping
FREE_KEYS: dict[str, tuple[Callable[[Any], dict[str, Any]], tuple]] = {
    "env": (lambda k: d(env={"A": "a", k: "x"}), ("env",)),
    "vars": (lambda k: d(vars={"a": 1, k: "x"}), ("vars",)),
    "fn": (lambda k: d(fn={"f": {"script": []}, k: {"script": []}}), ("fn",)),
    "attach-env": (lambda k: d(attach={"spawn": "ssh host", "env": {"A": "a", k: "x"}}), ("attach", "env")),
}
# the mappings with fixed keys: doc(key) -> a document with `key` as one more key of that mapping
FIXED_KEYS: dict[str, Callable[[Any], dict[str, Any]]] = {
    "top-level": lambda k: {**d(), k: "x"},
    "attach": lambda k: d(attach={"spawn": "ssh host", k: "x"}),
    "function": lambda k: d(fn={"f": {"script": [], k: "x"}}),
    "prompt": lambda k: d(prompts=[{"name": "p", "expect": "x", k: "x"}]),
    "send-each": lambda k: d(prompts=[{"name": "p", "expect": "x", "send": {"each": "vars.c", k: "x"}}]),
    "fields-entry": lambda k: d(
        prompts=[{"name": "p", "send": {"each": "vars.c", "fields": [{"match": "x", "field": "u", k: "x"}]}}]
    ),
    "cmd": lambda k: s({"cmd": "x", k: "x"}),
    "sleep": lambda k: s({"sleep": 1, k: "x"}),
    "call": lambda k: d(fn=FN, script=[{"call": "f", k: "x"}]),
    "block-step": lambda k: s({"block": {"name": "b"}, k: "x"}),
    "block": lambda k: s({"block": {"name": "b", k: "x"}}),
    "line": lambda k: s({"line": "x", k: "x"}),
    "return": lambda k: s({"return": 1, k: "x"}),
    "control": lambda k: s({"control": "a", k: "x"}),
    "fn-step": lambda k: d(fn={"f": {"script": [{"cmd": "x", k: "x"}]}}),
}


@pytest.mark.parametrize("literal", list(KEY_LITERALS.values()), ids=list(KEY_LITERALS))
@pytest.mark.parametrize("where", list(FREE_KEYS))
def test_p6_75_parity_non_string_key_rejected(both_validate: Callable, where: str, literal: str):
    """SPEC "YAML Script Structure": a key of `env`, `vars`, `fn` or `attach.env` is a string, in both.

    The models always rejected `vars: {1: x}`; the schema accepted it, having nothing to say about a key's type.
    """
    build, loc = FREE_KEYS[where]
    key = yaml_key(literal)
    assert not isinstance(key, str)
    doc = build(key)
    assert both_validate(doc) == (False, False)
    [err] = model_errors(doc)
    assert (err["type"], err["loc"][: len(loc)], err["loc"][-1]) == ("string_type", loc, "[key]")
    # the key isn't converted: only the quoted key is the string
    assert both_validate(build(str(key))) == (True, True)


@pytest.mark.parametrize("literal", list(KEY_LITERALS.values()), ids=list(KEY_LITERALS))
@pytest.mark.parametrize("where", list(FIXED_KEYS))
def test_p6_75_parity_non_string_key_in_a_fixed_mapping_rejected(both_validate: Callable, where: str, literal: str):
    """A mapping with fixed keys has no key that isn't a string: both reject it (they did before, too)."""
    doc = FIXED_KEYS[where](yaml_key(literal))
    assert both_validate(doc) == (False, False)
    assert "invalid_key" in {e["type"] for e in model_errors(doc)}


@pytest.mark.parametrize("literal", list(KEY_LITERALS.values()), ids=list(KEY_LITERALS))
def test_p6_75_parity_keys_inside_a_vars_value_are_data(both_validate: Callable, literal: str):
    """Only the structure's keys are strings: a mapping inside a `vars` value keeps the keys YAML gives it."""
    key = yaml_key(literal)
    assert both_validate(d(vars={"ports": {key: "up"}})) == (True, True)
    assert both_validate(d(vars={"items": [{key: {key: 1}}]})) == (True, True)
    assert Config.model_validate(d(vars={"ports": {key: "up"}})).vars["ports"] == {key: "up"}


@pytest.mark.parametrize("literal", list(KEY_LITERALS.values()), ids=list(KEY_LITERALS))
def test_p6_75_parity_non_string_key_in_a_plugin_step_rejected(both_validate: Callable, probe: Any, literal: str):
    """A step's keys are strings for plugin steps too: `stepCommon` says so for `pluginStep` and every `<key>Step`."""
    key = yaml_key(literal)
    assert both_validate(s({"probe": "x", key: "x"})) == (False, False)
    assert "invalid_key" in {e["type"] for e in model_errors(s({"probe": "x", key: "x"}))}
    # a step no plugin provides: the static schema used to accept these two as possible plugin steps
    assert both_validate(s({"nope": 1, key: "x"})) == (False, False)
    assert both_validate(s({key: "x"})) == (False, False)


def test_p6_75_schema_names_string_keys_on_every_open_mapping(schema: dict[str, Any]):
    """`propertyNames` is on the four free-form mappings and on `stepCommon`; every other object is closed.
    `attach.env` also says which names a process environment takes (P6-90)."""
    names = {"type": "string"}
    props, defs = schema["properties"], schema["$defs"]
    for node in (props["env"], props["vars"], props["fn"], defs["stepCommon"]):
        assert node["propertyNames"] == names
    assert defs["attach"]["properties"]["env"]["propertyNames"] == {**names, "pattern": "^[^=\\u0000]+$"}

    def objects(node: Any, path: str = "") -> Any:
        if isinstance(node, dict):
            if node.get("type") == "object":
                yield path, node
            for k, v in node.items():
                yield from objects(v, f"{path}/{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                yield from objects(v, f"{path}/{i}")

    open_ = sorted(p for p, n in objects(schema) if n.get("additionalProperties") is not False and "propertyNames" not in n)
    # pluginStep gets the rule from stepCommon; the `if` of the prompt's fields rule only tests a value
    assert open_ == ["/$defs/pluginStep", "/$defs/prompt/allOf/0/if/properties/send"]


def test_p6_75_non_string_key_is_a_validation_error_on_load(tmp_path: Any):
    """Through the CLI's loader: `vars: {1: x}` is reported as a validation error, without a traceback."""
    from conftest import run_cli

    res = run_cli(None, tmp_path, raw="autobot: 2026-10\nattach: {spawn: 'true'}\nvars: {1: x}\nscript: []\n")
    assert res.returncode == 1
    assert "Validation errors:" in res.stderr and "string_type" in res.stderr and "Traceback" not in res.stderr


# -- P6-78: a YAML boolean is never text ---------------------------------------

# unquoted, YAML reads each of these as a boolean
BOOLEAN_LITERALS = ["true", "True", "yes", "on", "false", "No", "off"]
ENV_HINT = (
    "an environment value is a string, and unquoted this one is a boolean ({}); "
    "quote it to set it as written, e.g. 'true' or 'yes'"
)


@pytest.mark.parametrize("literal", BOOLEAN_LITERALS)
@pytest.mark.parametrize("where", ["env-value", "attach-env-value"])
def test_p6_78_boolean_env_value_says_to_quote_it(both_validate: Callable, where: str, literal: str):
    """SPEC "Booleans are not text": `DEBUG: true` or `NO_COLOR: yes` is rejected by both, and the models say to quote it.

    It was always `string_type`; the message used to be only `Input should be a valid string`.
    """
    value = yaml.safe_load(literal)
    assert isinstance(value, bool)
    doc = STRING_FIELDS[where](value)
    assert both_validate(doc) == (False, False)
    [err] = model_errors(doc)
    loc = ("env", "A") if where == "env-value" else ("attach", "env", "A")
    assert (err["loc"], err["type"], err["msg"]) == (loc, "string_type", ENV_HINT.format(str(value).lower()))
    # quoted, it is the text as written
    assert both_validate(STRING_FIELDS[where](literal)) == (True, True)


@pytest.mark.parametrize("value", [1, 2.5], ids=repr)
@pytest.mark.parametrize("where", ["env-value", "attach-env-value"])
def test_p6_78_number_env_value_keeps_its_error(both_validate: Callable, where: str, value: Any):
    """Only booleans get the hint: a number is still the plain `string_type` it was."""
    doc = STRING_FIELDS[where](value)
    assert both_validate(doc) == (False, False)
    [err] = model_errors(doc)
    assert (err["type"], err["msg"]) == ("string_type", "Input should be a valid string")


@pytest.mark.parametrize("value", [True, False], ids=repr)
@pytest.mark.parametrize("field", [f for f in STRING_FIELDS if f != "duration"])
def test_p6_78_parity_boolean_is_not_a_string(both_validate: Callable, field: str, value: bool):
    """Wherever a script holds a string, a boolean is rejected by both when the script is loaded:
    `cmd: yes`, `line: on`, `register: true`, `when: true`, `spawn: no`. It is never turned into text."""
    assert both_validate(STRING_FIELDS[field](value)) == (False, False)


@pytest.mark.parametrize("value", [True, False], ids=repr)
def test_p6_78_boolean_flags_and_data_are_still_booleans(both_validate: Callable, value: bool):
    """Where a boolean is the value that is meant, nothing changes: the two flags, and `vars`."""
    assert both_validate(s({"cmd": "x", "ignore_error": value})) == (True, True)
    assert both_validate(d(prompts=[{"name": "p", "expect": "x", "return": value}])) == (True, True)
    assert both_validate(d(vars={"debug": value, "flags": [value], "site": {"up": value}})) == (True, True)
    assert Config.model_validate(d(vars={"debug": value})).vars["debug"] is value


# -- P6-79: leading whitespace in spawn -----------------------------------------


@pytest.mark.parametrize(
    "value",
    [" ssh host", "  ssh host", "\tssh host", "\n  ssh host\n", " ssh host", " 'ssh' host", " {{ args.cmd }}", " s''sh"],
    ids=repr,
)
def test_p6_79_spawn_with_leading_whitespace_accepted(both_validate: Callable, value: str):
    """SPEC "attach": whitespace before the command is not part of it; both accept such a `spawn`.

    The models used to reject `" ssh host"` with the message about quotes: pexpect splits it into
    `['', 'ssh', 'host']`, an empty first word.
    """
    assert both_validate(d(attach={"spawn": value})) == (True, True)
    assert models.names_command(value)


@pytest.mark.parametrize("value", [" ''", "  '' ls", '\t"" --version', " \\", "\n''\n"], ids=repr)
def test_p6_79_quotes_behind_leading_whitespace_still_rejected(both_validate: Callable, value: str):
    """What is left after the whitespace must still name a command; the message shows the value as written."""
    doc = d(attach={"spawn": value})
    assert both_validate(doc) == (False, True)
    [err] = model_errors(doc)
    assert (err["loc"], err["type"]) == (("attach", "spawn"), "empty_command")
    assert err["msg"] == (
        f"spawn must name a command: the first word of {value!r} is empty "
        "(quotes or a backslash with nothing in them)"
    )


def test_p6_79_names_command_is_what_pexpect_spawns_once_stripped():
    """`names_command` is about the command line the runner spawns: the value without its leading whitespace."""
    import pexpect

    for value in [" ssh host", "\tssh", " ''", "  '' ls", " ", "", " 'a b' c", "ssh "]:
        words = pexpect.split_command_line(value.lstrip())
        assert models.names_command(value) == bool(words and words[0]), value


# -- P6-82: breakout is a list of steps ----------------------------------------

# where -> (doc(breakout), the path of `breakout`, its path in the `call` check, the validated field)
BREAKOUT_HOSTS: dict[str, tuple[Callable[[Any], dict[str, Any]], tuple, tuple, Callable[[Config], Any]]] = {
    "attach": (
        lambda v: d(fn=FN, attach={"spawn": "ssh host", "breakout": v}),
        ("attach", "breakout"),
        ("attach", "breakout"),
        lambda c: c.attach.breakout,
    ),
    "block": (
        lambda v: d(fn=FN, script=[{"block": {"name": "b", "breakout": v}}]),
        ("script", 0, "block", "block", "breakout"),
        ("script", 0, "block", "breakout"),
        lambda c: c.script[0].block.breakout,
    ),
}
BREAKOUT_LISTS = {
    "one": [{"line": "exit"}],
    "several": [{"control": "]"}, {"line": "q"}, {"cmd": "true", "timeout": "5s"}, {"call": "f"}],
    "nested-block": [{"block": {"name": "inner", "breakout": [{"line": "exit"}]}}],
}
BREAKOUT_NOT_A_LIST = {
    "script-mapping": {"script": [{"line": "exit"}]},
    "empty-script-mapping": {"script": []},
    "empty-mapping": {},
    "step": {"line": "exit"},
    "string": "exit",
}
# a step that isn't valid -> (the step, the error's path below the step, its type; None: several errors)
BREAKOUT_BAD_STEPS: dict[str, tuple[dict[str, Any], tuple, str | None]] = {
    "unknown-step": ({"bogus": 1}, (), "invalid_step"),
    "extra-key": ({"cmd": "x", "bogus": 1}, ("cmd", "bogus"), "extra_forbidden"),
    "empty-after": ({"line": "x", "after": ""}, ("line", "after"), "string_too_short"),
    "bad-value": ({"line": 1}, (), None),
}


@pytest.mark.parametrize("steps", list(BREAKOUT_LISTS.values()), ids=list(BREAKOUT_LISTS))
@pytest.mark.parametrize("where", list(BREAKOUT_HOSTS))
def test_p6_82_parity_breakout_list_accepted(both_validate: Callable, where: str, steps: list):
    """SPEC "attach", "block": `breakout` is a list of steps, like `enter` and every `script`."""
    build, _, _, get = BREAKOUT_HOSTS[where]
    assert both_validate(build(steps)) == (True, True)
    got = get(Config.model_validate(build(steps)))
    assert isinstance(got, list) and len(got) == len(steps)


@pytest.mark.parametrize("where", list(BREAKOUT_HOSTS))
def test_p6_82_parity_empty_and_omitted_breakout_are_no_breakout(both_validate: Callable, where: str):
    """`breakout: []` and no `breakout` key both validate, and the models read both as no steps."""
    build, _, _, get = BREAKOUT_HOSTS[where]
    empty, omitted = build([]), _drop_omitted(build(OMIT))
    assert both_validate(empty) == both_validate(omitted) == (True, True)
    assert get(Config.model_validate(empty)) == get(Config.model_validate(omitted)) == []


@pytest.mark.parametrize("value", list(BREAKOUT_NOT_A_LIST.values()), ids=list(BREAKOUT_NOT_A_LIST))
@pytest.mark.parametrize("where", list(BREAKOUT_HOSTS))
def test_p6_82_parity_breakout_that_is_not_a_list_rejected(both_validate: Callable, where: str, value: Any):
    """A mapping with a `script` key is not a list of steps: both reject it, with the ordinary type error."""
    build, loc, _, _ = BREAKOUT_HOSTS[where]
    doc = build(value)
    assert both_validate(doc) == (False, False)
    [err] = model_errors(doc)
    assert (err["loc"], err["type"], err["msg"]) == (loc, "list_type", "Input should be a valid list")


@pytest.mark.parametrize("where", list(BREAKOUT_HOSTS))
def test_p6_82_parity_null_breakout_rejected(both_validate: Callable, where: str):
    """An explicit null is `null_value`, as for every optional field (P6-41)."""
    build, loc, _, _ = BREAKOUT_HOSTS[where]
    doc = build(None)
    assert both_validate(doc) == (False, False)
    [err] = model_errors(doc)
    assert (err["loc"], err["type"]) == (loc, "null_value")
    assert err["msg"] == "null (an empty value) is not allowed; omit the key instead"


@pytest.mark.parametrize("case", list(BREAKOUT_BAD_STEPS))
@pytest.mark.parametrize("where", list(BREAKOUT_HOSTS))
def test_p6_82_parity_invalid_breakout_step_reported_at_its_index(both_validate: Callable, where: str, case: str):
    """A step of a breakout is at `<breakout>.<index>`: nothing stands between the key and the index."""
    build, loc, _, _ = BREAKOUT_HOSTS[where]
    step, tail, kind = BREAKOUT_BAD_STEPS[case]
    doc = build([{"line": "ok"}, step])
    # the static schema takes a step with an unknown key for a plugin step; only the models know the plugins
    assert both_validate(doc) == (False, case == "unknown-step")
    errs = model_errors(doc)
    assert {e["loc"][: len(loc) + 1] for e in errs} == {(*loc, 1)}
    if kind is not None:
        [err] = errs
        assert (err["loc"], err["type"]) == ((*loc, 1, *tail), kind)


@pytest.mark.parametrize("where", list(BREAKOUT_HOSTS))
def test_p6_82_undefined_call_in_a_breakout_reported_at_its_index(both_validate: Callable, where: str):
    """The `call` check reports the step the same way (only the models know the functions)."""
    build, _, loc, _ = BREAKOUT_HOSTS[where]
    doc = build([{"line": "ok"}, {"call": "nope"}])
    assert both_validate(doc) == (False, True)
    [err] = model_errors(doc)
    assert (err["loc"], err["type"]) == ((*loc, 1, "call"), "undefined_function")


def test_p6_82_schema_and_models_give_breakout_the_shape_of_enter(schema: dict[str, Any]):
    """Both breakouts are an array of steps in the schema and a list of steps in the models, like `enter`."""
    steps = {"type": "array", "items": {"$ref": "#/$defs/step"}}
    block = schema["$defs"]["blockStep"]["properties"]["block"]["properties"]
    assert schema["$defs"]["attach"]["properties"]["breakout"] == steps
    assert block["breakout"] == block["enter"] == steps
    fields = models.Block.model_fields
    assert fields["breakout"].annotation == fields["enter"].annotation
    assert models.Attach.model_fields["breakout"].annotation == models.Attach.model_fields["script"].annotation


# -- P6-90: what is handed to exec (attach.spawn, attach.env) -------------------

ENV_NAME_MSG = "an environment variable name must not be empty or contain '=' or a NUL character (\\0)"
NOT_FOR_EXEC = {
    "spawn-nul": ({"spawn": "bash\0 --norc"}, ("attach", "spawn"), "nul_character"),
    "spawn-nul-in-an-argument": ({"spawn": "echo a\0b"}, ("attach", "spawn"), "nul_character"),
    "spawn-nul-in-a-template": ({"spawn": "{{ args.c }}\0"}, ("attach", "spawn"), "nul_character"),
    "env-value-nul": ({"spawn": "sh", "env": {"A": "x\0y"}}, ("attach", "env", "A"), "nul_character"),
    "env-name-nul": ({"spawn": "sh", "env": {"A\0B": "x"}}, ("attach", "env", "A\0B", "[key]"), "env_name"),
    "env-name-equals": ({"spawn": "sh", "env": {"A=B": "x"}}, ("attach", "env", "A=B", "[key]"), "env_name"),
    "env-name-empty": ({"spawn": "sh", "env": {"": "x"}}, ("attach", "env", "", "[key]"), "env_name"),
}


@pytest.mark.parametrize("case", NOT_FOR_EXEC)
def test_p6_90_parity_what_exec_cannot_take_is_rejected(both_validate: Callable, case: str):
    """SPEC "attach": a NUL in `spawn` or in an `attach.env` value, and an `attach.env` name that is empty or
    has `=` or a NUL, can't be handed to a process: the schema and the models reject them when the script is
    loaded."""
    attach, loc, type_ = NOT_FOR_EXEC[case]
    doc = d(attach=attach)
    assert both_validate(doc) == (False, False)
    [err] = model_errors(doc)
    assert (err["loc"], err["type"]) == (loc, type_)
    assert "\0" not in err["msg"]  # the message names the character, it doesn't hold one
    if type_ == "env_name":
        assert err["msg"] == ENV_NAME_MSG


@pytest.mark.parametrize(
    "attach",
    [
        {"spawn": "ssh host", "env": {"TERM": "dumb", "A_b1": "", "lower": "x=y", "X Y": "multi\nline"}},
        {"spawn": "ssh\thost\n", "env": {}},
    ],
)
def test_p6_90_parity_ordinary_spawn_and_env_accepted(both_validate: Callable, attach: dict):
    """Only those characters are refused: `=` in a value, an empty value and a line break are fine."""
    assert both_validate(d(attach=attach)) == (True, True)


def test_p6_90_top_level_env_is_not_handed_to_exec(both_validate: Callable):
    """The top-level `env` holds template values, not a process environment: its names aren't checked."""
    assert both_validate(d(env={"A=B": "x"})) == (True, True)
